import DBondCore
import Foundation

struct ProbeError: Error {}
// Runner for integration tests only. Its state path, optional fixture port, and
// stdin lifetime come from the harness. It never reads production bridge credentials.
func run() throws {
    let args = CommandLine.arguments
    guard args.count >= 3 else { throw ProbeError() }
    let directory = URL(fileURLWithPath:args[1]); let fixture = UInt16(args[2]) ?? 0
    let handler:Handler = { service,id,payload in
        guard fixture != 0 else { return "Local demo received " + id }
        let value = try JSONSerialization.jsonObject(with:payload,options:.fragmentsAllowed)
        let body = try JSONSerialization.data(withJSONObject:["service":service,"id":id,"payload":value])
        let p = Process(), input = Pipe(), output = Pipe()
        p.executableURL = URL(fileURLWithPath:"/usr/bin/curl")
        p.arguments = ["-q","--noproxy","*","--silent","--fail","--max-time","7","-H","Content-Type: application/json","--data-binary","@-","http://127.0.0.1:\(fixture)/fixture"]
        p.standardInput = input; p.standardOutput = output; p.standardError = FileHandle.nullDevice
        try p.run(); input.fileHandleForWriting.write(body); try input.fileHandleForWriting.close()
        let data = output.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
        guard p.terminationStatus == 0, let obj = try JSONSerialization.jsonObject(with:data) as? [String:Any],
              obj["signed"] as? Bool == true, obj["opaque"] as? Bool == true, obj["started"] as? Bool == true, obj["replied"] as? Bool == true, obj["request_id"] is String,
              let reply = obj["reply"] as? String else { throw ProbeError() }
        return reply
    }
    let fixtureDot:DotSender = { service,id,payload,_ in
        let object = try JSONSerialization.jsonObject(with:payload) as? [String:Any]
        if (object?["fixtureDot"] as? Bool == true || object?["kind"] as? String == "service.message"), fixture != 0 {
            let body = try JSONSerialization.data(withJSONObject:["service":service,"id":id,"payload":object as Any])
            let p = Process(), input = Pipe(), output = Pipe()
            p.executableURL = URL(fileURLWithPath:"/usr/bin/curl")
            p.arguments = ["-q","--noproxy","*","--silent","--fail","--max-time","7","-H","Content-Type: application/json","--data-binary","@-","http://127.0.0.1:\(fixture)/fixture-dot"]
            p.standardInput = input; p.standardOutput = output; p.standardError = FileHandle.nullDevice
            try p.run(); input.fileHandleForWriting.write(body); try input.fileHandleForWriting.close()
            let data = output.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
            guard p.terminationStatus == 0, let result = try JSONSerialization.jsonObject(with:data) as? [String:String],
                  let state = result["state"], let reply = result["reply"] else { throw ProbeError() }
            return (state,reply)
        }
        guard object?["kind"] as? String == "service.reply",
              let source = object?["in_reply_to"] as? String,
              object?["text"] as? String != nil else { throw ProbeError() }
        return ("dot_stored","fixture return \(source) stored")
    }
    let hub = try Hub(directory:directory,port:0,heraldFixture:directory.appendingPathComponent("fake-herald"),handler:handler,dotSender:fixtureDot)
    let ownerSocket = try OwnerSocket(hub:hub,directory:directory)
    _ = ownerSocket
    func emit(_ obj:[String:Any]) throws { let data = try JSONSerialization.data(withJSONObject:obj); FileHandle.standardOutput.write(data + Data([10])) }
    try emit(["endpoint":hub.snapshot().endpoint])
    while let command = readLine() {
        if command == "quit" { break }
        if command.hasPrefix("json ") {
            guard let object=try JSONSerialization.jsonObject(with:Data(command.dropFirst(5).utf8)) as? [String:Any],
                  let op=object["op"] as? String else { throw ProbeError() }
            switch op {
            case "pair":
                let sop=try object["sop"].map { try JSONDecoder().decode(OwnerSOP.self,from:JSONSerialization.data(withJSONObject:$0)) }
                let destination=object["destination"] as? String ?? "demo"
                guard ["demo", "dot", "peer"].contains(destination) else { throw ProbeError() }
                let p=try hub.pair(name:object["name"] as? String ?? "",destination:destination,
                                   chromeOrigin:object["chromeOrigin"] as? String,sop:sop)
                try emit(["id":p.id,"endpoint":p.endpoint,"secret":p.secret])
            case "set_sop":
                let id=object["id"] as? String ?? ""
                let sop=try JSONDecoder().decode(OwnerSOP.self,from:JSONSerialization.data(withJSONObject:object["sop"] ?? [:]))
                try hub.setSOP(service:id,sop:sop,expectedVersion:object["expectedVersion"] as? Int)
                try emit(["saved":true])
            case "trace":
                let id=object["id"] as? String ?? "", eventID=object["eventID"] as? String ?? ""
                let value=try hub.eventTrace(service:id,eventID:eventID)
                try emit(["event":try value.map { try JSONSerialization.jsonObject(with:JSONEncoder().encode($0)) } ?? NSNull()])
            default: throw ProbeError()
            }
            continue
        }
        if command.hasPrefix("inject-push ") {
            let parts = String(command.dropFirst(12)).split(separator:" ",maxSplits:2).map(String.init)
            guard parts.count == 3 else { throw ProbeError() }
            try hub.injectNativePushForFixture(service:parts[0],sourceID:parts[1],text:parts[2])
            try emit(["injected":true])
            continue
        }
        if command == "status" { try emit(["bridge":hub.bridgeStatus(),"herald":hub.heraldStatus()]); continue }
        if command == "list" {
            try emit(["services":hub.snapshot().services.map { ["id":$0.id,"name":$0.name,"endpoint":$0.endpoint,"destination":$0.destination,"paused":$0.paused,"queued":$0.queued,"in_flight":$0.inFlight,"subscribers":$0.subscribers,"directSubscribers":$0.directSubscribers] as [String:Any] }])
            continue
        }
        if command.hasPrefix("pause ") || command.hasPrefix("resume ") {
            let paused = command.hasPrefix("pause ")
            let id = String(command.dropFirst(paused ? 6 : 7))
            try hub.setPaused(id:id,paused:paused)
            try emit(["id":id,"paused":paused])
            continue
        }
        if command.hasPrefix("remove ") { try hub.remove(id:String(command.dropFirst(7))); try emit(["removed":true]); continue }
        let chromePrefix = "--chrome-origin "
        let chromeParts = command.hasPrefix(chromePrefix) ? String(command.dropFirst(chromePrefix.count)).split(separator:" ",maxSplits:1).map(String.init) : []
        let chromeOrigin = chromeParts.count == 2 ? chromeParts[0] : nil
        let name = chromeParts.count == 2 ? chromeParts[1] : command
        let p = try hub.pair(name:name,destination:name == "fixture-herald" ? "herald" : "demo",chromeOrigin:chromeOrigin)
        try emit(["id":p.id,"endpoint":p.endpoint,"secret":p.secret])
    }
}
do { try run() } catch { FileHandle.standardError.write(Data("Probe failed: \(error)\n".utf8)); exit(1) }
