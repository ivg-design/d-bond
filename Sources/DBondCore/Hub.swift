import CryptoKit
import Darwin
import Foundation

public struct OwnerRule: Codable, Equatable { public var id,text:String; public init(id:String,text:String) { self.id=id; self.text=text } }
public struct OwnerSOP: Codable, Equatable { public var version:Int; public var rules:[OwnerRule]; public init(version:Int,rules:[OwnerRule]) { self.version=version; self.rules=rules } }
public struct TraceStage: Codable, Equatable { public var state,at,basis:String }
public struct EventTrace: Codable { public var service,eventID,state:String; public var sopVersion:Int?; public var stages:[TraceStage] }
public struct Pairing: Codable { public let id,secret,endpoint,destination: String; public let herald:Bool; public let chromeOrigin:String? }
public struct ServiceView { public let id,name,endpoint,destination: String; public let lastSeen,lastState,chromeOrigin: String?; public let herald,paused:Bool; public let queued,inFlight,subscribers:Int; public let sop:OwnerSOP? }
public struct Snapshot { public let endpoint: String; public let services:[ServiceView]; public let eventCount:Int }
struct Service: Codable, Equatable { var id,name,digest:String; var seen,state:String?; var herald,paused:Bool?; var destination,chromeOrigin:String?; var sop:OwnerSOP? = nil }
struct Event: Codable, Equatable { var service,id:String; var payload:Data; var state:String; var sequence:Int?; var reply:String?; var sourceID:String? = nil; var payloadDigest:String? = nil; var retrying:Bool? = nil; var route:String? = nil; var sop:OwnerSOP? = nil; var stages:[TraceStage]? = nil }
struct Receipt: Codable, Equatable { var service,id:String; var sequence:Int; var state,reply:String }
struct Ledger: Codable { var services:[Service] = []; var events:[Event] = []; var receipts:[Receipt]?; var seq = 0 }
public typealias Handler = (String,String,Data) throws -> String
public typealias DotSender = (String,String,Data,@escaping (String,String)->Void) throws -> (state:String,reply:String)

/// Single writer; receipt persists before HTTP 202, responses persist before signaling waiters.
public final class Hub: @unchecked Sendable {
    private let lock = NSCondition(), work = DispatchQueue(label:"life.dbond.demo.delivery")
    private let dotSlots = DispatchSemaphore(value:4)
    private let server:LocalHTTP, store:LedgerStore, ownerLock:Int32, handler:Handler, dotSender:DotSender
    private let heraldSupport: URL?
    private var ledger:Ledger
    private var nativeWorker:Process?
    private var subscribers:[String:Int] = [:]
    public convenience init() throws {
        let base = FileManager.default.urls(for:.applicationSupportDirectory,in:.userDomainMask)[0]
        try self.init(directory:base.appendingPathComponent("D-Bond Proof of Concept"),port:18768)
        startNativeObserver()
    }
    public init(directory:URL, port:UInt16, heraldFixture:URL? = nil, handler:@escaping Handler = { _,id,_ in "Local demo received " + id }, dotSender:DotSender? = nil) throws {
        self.handler = handler; self.dotSender = dotSender ?? { service,id,payload,onUpdate in try sendViaDot(service:service,id:id,payload:payload,onUpdate:onUpdate) }; self.heraldSupport = heraldFixture; let fm = FileManager.default
        try fm.createDirectory(at:directory,withIntermediateDirectories:true,attributes:[.posixPermissions:0o700])
        let attr = try fm.attributesOfItem(atPath:directory.path)
        guard (attr[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
              attr[.type] as? FileAttributeType == .typeDirectory,
              ((attr[.posixPermissions] as? NSNumber)?.intValue ?? 511) & 0o077 == 0 else {
            throw Fault.message("D-Bond storage must be a private directory owned by this user.")
        }
        let descriptor = open(directory.appendingPathComponent(".instance").path,O_RDWR | O_CREAT | O_NOFOLLOW,0o600)
        guard descriptor >= 0 else { throw Fault.message("Cannot lock local storage.") }
        guard flock(descriptor,LOCK_EX | LOCK_NB) == 0 else { close(descriptor); throw Fault.message("Another instance is using this store.") }
        do {
            store = try LedgerStore(file:directory.appendingPathComponent("ledger.sqlite"))
            ledger = try store.loadOrMigrate(legacy:directory.appendingPathComponent("ledger.json"))
            server = try LocalHTTP(port)
        } catch { close(descriptor); throw error }
        ownerLock = descriptor
        var recovery = ledger
        for i in ledger.events.indices {
            let event = ledger.events[i]
            guard let service = ledger.services.first(where: { $0.id == event.service }) else { continue }
            let isDot = event.route == "dot_return" || service.destination == "dot"
            if isDot, !event.id.hasPrefix("@dot:"), !event.payload.isEmpty,
               ["dispatching", "dot_unconfirmed", "outcome_unknown"].contains(event.state) {
                recovery.events[i] = Self.marked(event, "queued")
                recovery.events[i].retrying = true
            } else if ["sending_banner", "dispatching"].contains(event.state) {
                recovery.seq += 1
                recovery.events[i] = Self.marked(event, "outcome_unknown")
                recovery.events[i].sequence = recovery.seq
                recovery.events[i].reply = "Delivery was interrupted. Check the destination for a receipt."
            } else {
                continue
            }
            if let index = ledger.services.firstIndex(where: { $0.id == service.id }) {
                recovery.services[index].state = recovery.events[i].state
            }
        }
        if recovery.events != ledger.events { try persist(recovery) }
        server.start { [weak self] req in self?.route(req) ?? HTTPReply(503,[:]) }
        for event in ledger.events where event.state == "queued" && ledger.services.contains(where: { $0.id == event.service && $0.paused != true }) {
            if event.id.hasPrefix("@dot:") { publishDotPush(service:event.service,id:event.id) }
            else { schedule(event) }
        }
    }
    deinit { if nativeWorker?.isRunning == true { nativeWorker?.terminate() }; close(ownerLock) }
    private func startNativeObserver() {
        nativeWorker = observeNativeDot(onExit: { [weak self] in
            self?.restartNativeObserver()
        }) { [weak self] service,id,state,detail in
            guard let self else { return true }
            if state == "dot_push" { return self.recordDotPush(service:service,sourceID:id,detail:detail) }
            return self.recordDotReceipt(service:service,id:id,state:state,reply:detail)
        }
        if nativeWorker == nil { restartNativeObserver() }
    }

    private func restartNativeObserver() {
        work.asyncAfter(deadline: .now() + 2) { [weak self] in self?.startNativeObserver() }
    }

    private var base:String { "http://127.0.0.1:\(server.port)" }
    private func endpoint(_ id:String) -> String { base + "/v1/services/" + id + "/events" }
    private static func digest(_ data:Data) -> String { SHA256.hash(data:data).map { String(format:"%02x",$0) }.joined() }
    private static func now() -> String {
        let formatter=ISO8601DateFormatter(); formatter.formatOptions=[.withInternetDateTime,.withFractionalSeconds]
        return formatter.string(from:Date())
    }
    private static func marked(_ event:Event, _ state:String, basis:String = "broker_recorded") -> Event {
        var result=event; result.state=state
        result.stages = (result.stages ?? []) + [TraceStage(state:state,at:now(),basis:basis)]
        return result
    }
    private static func validate(_ sop:OwnerSOP?) throws {
        guard let sop else { return }
        guard sop.version > 0, sop.rules.count <= 64 else { throw Fault.message("Invalid owner SOP version or rule count.") }
        var ids=Set<String>()
        var total=0
        for rule in sop.rules {
            total += rule.id.utf8.count + rule.text.utf8.count
            guard !rule.id.isEmpty, rule.id.utf8.count <= 80, !rule.id.unicodeScalars.contains(where:CharacterSet.controlCharacters.contains),
                  !rule.text.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty, rule.text.utf8.count <= 131072, total <= 131072,
                  !rule.text.unicodeScalars.contains(where:{ CharacterSet.controlCharacters.contains($0) && !["\n","\r","\t"].contains(String($0)) }), ids.insert(rule.id).inserted else {
                throw Fault.message("Invalid or duplicate owner SOP rule.")
            }
        }
    }
    private func persist(_ next:Ledger) throws {
        try store.save(old:ledger,next:next)
        ledger = next
    }
    // Caller holds lock.
    private func receiptRows(service:String, after:Int) -> [Receipt] {
        let history = (ledger.receipts ?? []).filter { $0.service == service }
        let recordedIDs = Set(history.map { $0.id })
        let legacy = ledger.events.filter { $0.service == service && !recordedIDs.contains($0.id) && ($0.sequence ?? 0) > after }
            .map { Receipt(service:$0.service,id:$0.id,sequence:$0.sequence!,state:$0.state,reply:$0.reply ?? "") }
        return (legacy + history.filter { $0.sequence > after }).sorted { $0.sequence < $1.sequence }
    }
    public func snapshot() -> Snapshot {
        lock.lock(); defer { lock.unlock() }
        return Snapshot(endpoint:base,services:ledger.services.map { service in
            let events = ledger.events.filter { $0.service == service.id }
            return ServiceView(id:service.id,name:service.name,endpoint:endpoint(service.id),destination:service.destination ?? (service.herald == true ? "herald" : "demo"),lastSeen:service.seen,lastState:service.state,chromeOrigin:service.chromeOrigin,herald:service.herald == true,paused:service.paused == true,queued:events.filter { $0.state == "queued" }.count,inFlight:events.filter { ["dispatching","sending_banner"].contains($0.state) }.count,subscribers:subscribers[service.id] ?? 0,sop:service.sop)
        },eventCount:ledger.events.count)
    }
    public func dotLinkStatus() -> String { nativeStatus() }
    /// Test harness only: available only on an isolated Hub with a fixture Herald directory.
    public func injectNativePushForFixture(service:String, sourceID:String, text:String) throws {
        guard heraldSupport != nil else { throw Fault.message("Native push injection requires a Herald fixture.") }
        guard recordDotPush(service:service,sourceID:sourceID,detail:text) else { throw Fault.message("Could not persist fixture push.") }
    }
    public func pair(name:String, herald:Bool = false, destination:String = "dot", chromeOrigin:String? = nil, sop:OwnerSOP? = nil) throws -> Pairing {
        try Self.validate(sop)
        let name = name.trimmingCharacters(in:.whitespacesAndNewlines)
        guard !name.isEmpty, name.count <= 200, !name.unicodeScalars.contains(where:CharacterSet.controlCharacters.contains) else { throw Fault.message("Enter a name between 1 and 200 printable characters.") }
        let mode = herald ? "herald" : destination
        guard ["dot","demo","herald"].contains(mode) else { throw Fault.message("Unknown destination.") }
        if let chromeOrigin {
            let prefix = "chrome-extension://"
            let suffix = chromeOrigin.dropFirst(prefix.count)
            guard chromeOrigin.hasPrefix(prefix), suffix.count == 32,
                  suffix.unicodeScalars.allSatisfy({ ("a"..."p").contains(String($0)) }) else {
                throw Fault.message("Chrome extension origin must be chrome-extension:// followed by a 32-character extension ID (a–p).")
            }
        }
        let id = UUID().uuidString.lowercased()
        let secret = SymmetricKey(size:.bits256).withUnsafeBytes { Data($0).base64EncodedString() }
        lock.lock(); defer { lock.unlock() }
        guard ledger.services.count < 64 else { throw Fault.message("Prototype pairing limit reached.") }
        guard !ledger.services.contains(where: { $0.name.localizedCaseInsensitiveCompare(name) == .orderedSame }) else { throw Fault.message("A service connection already uses that name.") }
        var next = ledger; next.services.append(Service(id:id,name:name,digest:Self.digest(Data(secret.utf8)),herald:mode == "herald",paused:false,destination:mode,chromeOrigin:chromeOrigin,sop:sop))
        try persist(next); return Pairing(id:id,secret:secret,endpoint:endpoint(id),destination:mode,herald:mode == "herald",chromeOrigin:chromeOrigin)
    }
    public func getSOP(service:String) throws -> OwnerSOP? {
        lock.lock(); defer { lock.unlock() }
        guard let row=ledger.services.first(where:{$0.id==service}) else { throw Fault.message("Service connection no longer exists.") }
        return row.sop
    }
    public func setSOP(service:String,sop:OwnerSOP,expectedVersion:Int? = nil) throws {
        try Self.validate(sop)
        lock.lock(); defer { lock.unlock() }
        guard let ix=ledger.services.firstIndex(where:{$0.id==service}) else { throw Fault.message("Service connection no longer exists.") }
        let current=ledger.services[ix].sop?.version ?? 0
        guard expectedVersion == nil || expectedVersion == current else { throw Fault.message("Owner SOP changed; reload before saving.") }
        guard sop.version > current else { throw Fault.message("Owner SOP version must increase.") }
        var next=ledger; next.services[ix].sop=sop; try persist(next)
    }
    public func eventTrace(service:String,eventID:String) throws -> EventTrace? {
        lock.lock(); defer { lock.unlock() }
        guard ledger.services.contains(where:{$0.id==service}) else { throw Fault.message("Service connection no longer exists.") }
        guard let e=ledger.events.first(where:{$0.service==service && $0.id==eventID}) else { return nil }
        return EventTrace(service:e.service,eventID:e.id,state:e.state,sopVersion:e.sop?.version,stages:e.stages ?? [])
    }
    public func recentEventTraces(service:String,limit:Int = 20) throws -> [EventTrace] {
        lock.lock(); defer { lock.unlock() }
        guard ledger.services.contains(where:{$0.id==service}) else { throw Fault.message("Service connection no longer exists.") }
        return ledger.events.filter{$0.service==service}.suffix(max(0,min(limit,100))).reversed().map {
            EventTrace(service:$0.service,eventID:$0.id,state:$0.state,sopVersion:$0.sop?.version,stages:$0.stages ?? [])
        }
    }
    public func rename(id:String,name:String) throws {
        let name=name.trimmingCharacters(in:.whitespacesAndNewlines)
        guard !name.isEmpty, name.count<=200, !name.unicodeScalars.contains(where:CharacterSet.controlCharacters.contains) else { throw Fault.message("Invalid display name.") }
        lock.lock(); defer {lock.unlock()}
        guard let index=ledger.services.firstIndex(where:{$0.id==id}), !ledger.services.contains(where:{$0.id != id && $0.name.localizedCaseInsensitiveCompare(name) == .orderedSame}) else {throw Fault.message("Unknown service or duplicate name.")}
        var next=ledger; next.services[index].name=name; try persist(next); lock.broadcast()
    }
    public func setPaused(id:String, paused:Bool) throws {
        lock.lock(); defer { lock.unlock() }
        guard let index = ledger.services.firstIndex(where: { $0.id == id }) else { throw Fault.message("Service connection no longer exists.") }
        guard ledger.services[index].paused != paused else { return }
        var next = ledger; next.services[index].paused = paused
        if !paused {
            for ix in next.events.indices where next.events[ix].service == id &&
                (next.services[index].destination == "dot" || next.events[ix].route == "dot_return") &&
                ["dot_unconfirmed","dot_route_blocked","dot_waiting_for_link","dot_stored","outcome_unknown"].contains(next.events[ix].state) &&
                !next.events[ix].payload.isEmpty {
                next.events[ix] = Self.marked(next.events[ix],"queued")
                next.events[ix].retrying = true
            }
        }
        try persist(next); lock.broadcast()
        if !paused { for event in ledger.events where event.service == id && event.state == "queued" {
            if event.id.hasPrefix("@dot:") { DispatchQueue.global(qos:.utility).async { [weak self] in self?.publishDotPush(service:event.service,id:event.id) } }
            else { schedule(event) }
        } }
    }
    public func remove(id:String) throws {
        lock.lock(); defer { lock.unlock() }
        guard ledger.services.contains(where: { $0.id == id }) else { throw Fault.message("Service connection no longer exists.") }
        var next = ledger; next.services.removeAll { $0.id == id }; next.events.removeAll { $0.service == id }; next.receipts?.removeAll { $0.service == id }
        try persist(next); lock.broadcast()
    }
    /// An explicit retry keeps the original immutable service/event key and payload.
    /// The native helper must deduplicate that key; no new caller event is created.
    public func retryDot(service:String, eventID:String) throws {
        lock.lock(); defer { lock.unlock() }
        guard let ix = ledger.events.firstIndex(where: { $0.service == service && $0.id == eventID && !$0.id.hasPrefix("@dot:") }),
              ledger.services.contains(where: { $0.id == service && ($0.destination == "dot" || ledger.events[ix].route == "dot_return") }) else {
            throw Fault.message("Dot event no longer exists.")
        }
        let event = ledger.events[ix]
        guard ["dot_unconfirmed","dot_route_blocked","dot_waiting_for_link","dot_stored","outcome_unknown"].contains(event.state),
              !event.payload.isEmpty else { throw Fault.message("This event cannot be retried safely.") }
        var next = ledger; next.events[ix] = Self.marked(next.events[ix],"queued"); next.events[ix].retrying = true
        try persist(next)
        if ledger.services.contains(where: { $0.id == service && $0.paused != true }) { schedule(next.events[ix]) }
    }
    private func route(_ r:Request) -> HTTPReply {
        guard let url = URLComponents(string:r.target) else { return HTTPReply(400,[:]) }
        let p = url.path.split(separator:"/").map(String.init)
        guard [4,5].contains(p.count), p[0] == "v1", p[1] == "services", (p[3] == "events" || (p.count == 4 && p[3] == "peers")) else { return HTTPReply(404,[:]) }
        let id = p[2]
        if let origin = r.headers["origin"] {
            lock.lock()
            let allowed = ledger.services.contains(where: { $0.id == id && $0.chromeOrigin == origin })
            lock.unlock()
            guard allowed else { return HTTPReply(403,["error":"Origin is not allowed for this service connection."]) }
            if r.method == "OPTIONS", (p.count == 4 || (p.count == 5 && p[4] == "stream")) { return HTTPReply(200,[:]) }
        }
        guard let auth = r.headers["authorization"], auth.hasPrefix("Bearer ") else { return HTTPReply(401,[:]) }
        let digest = Self.digest(Data(auth.dropFirst(7).utf8))
        lock.lock(); defer { lock.unlock() }
        guard let ix = ledger.services.firstIndex(where:{ $0.id == id && $0.digest == digest }) else { return HTTPReply(401,[:]) }
        if p[3] == "peers" {
            guard r.method == "GET", url.query == nil else { return HTTPReply(405, [:]) }
            guard ledger.services[ix].paused != true else { return HTTPReply(423, ["error":"Service connection is paused."]) }
            let peers = ledger.services.filter { $0.destination == "dot" || $0.herald == true }
            return HTTPReply(200, ["services": peers.map { ["id":$0.id, "name":$0.name, "paused":$0.paused == true] as [String:Any] }])
        }
        if r.method == "GET", p.count == 5, p[4] == "stream" {
            let q = url.queryItems ?? []
            guard q.count <= 1, q.allSatisfy({ $0.name == "after" && $0.value != nil }), let after = Int(q.first?.value ?? "0"), after >= 0 else { return HTTPReply(400,[:]) }
            guard ledger.services[ix].paused != true else { return HTTPReply(423,["error":"Service connection is paused."]) }
            if r.headers["accept"]?.contains("text/event-stream") == true {
                return HTTPReply(200, stream: { [weak self] write in self?.stream(service:id,after:after,write:write) })
            }
            let until = Date().addingTimeInterval(12)
            while true {
                guard ledger.services.contains(where:{$0.id == id}) else { return HTTPReply(401,[:]) }
                let rows = receiptRows(service:id,after:after).prefix(32)
                if !rows.isEmpty { return HTTPReply(200,["messages":rows.map { ["id":$0.id,"sequence":$0.sequence,"state":$0.state,"reply":$0.reply] as [String:Any] }]) }
                if !lock.wait(until:until) { return HTTPReply(200,["messages":[]]) }
            }
        }
        guard r.method == "POST", p.count == 4, url.query == nil else { return HTTPReply(405,[:]) }
        guard r.headers["content-type"]?.components(separatedBy:";").first?.lowercased() == "application/json",
              let body = (try? JSONSerialization.jsonObject(with:r.body)) as? [String:Any], Set(body.keys) == ["id","payload"],
              let eventID = body["id"] as? String, !eventID.isEmpty, !eventID.hasPrefix("@dot:"), eventID.utf8.count <= 120,
              !eventID.unicodeScalars.contains(where:CharacterSet.controlCharacters.contains), let payload = body["payload"],
              let data = try? JSONSerialization.data(withJSONObject:payload,options:[.sortedKeys,.fragmentsAllowed]) else { return HTTPReply(422,["error":"Expected {id, payload}."]) }
        if let old = ledger.events.first(where:{$0.service == id && $0.id == eventID}) {
            guard (old.payloadDigest ?? Self.digest(old.payload)) == Self.digest(data) else { return HTTPReply(409,["error":"Same ID with different payload."]) }
            return HTTPReply(200,["id":eventID,"state":old.state,"duplicate":true])
        }
        guard ledger.services[ix].paused != true else { return HTTPReply(423,["error":"Service connection is paused; no new event was queued."]) }
        let toDot = (payload as? [String:Any])?["kind"] as? String == "conversation.request" && ledger.services[ix].herald == true
        var event = Event(service:id,id:eventID,payload:data,state:"queued",payloadDigest:Self.digest(data),route:toDot ? "dot_return" : nil)
        event.sop=ledger.services[ix].sop
        event=Self.marked(event,"queued")
        var next = ledger; next.services[ix].seen = event.stages?.first?.at; next.services[ix].state = "queued"; next.events.append(event)
        do { try persist(next) } catch { return HTTPReply(507,["error":"Could not persist receipt."]) }
        schedule(event)
        return HTTPReply(202,["id":eventID,"state":"queued","duplicate":false])
    }
    private func stream(service:String, after:Int, write:(Data)->Bool) {
        lock.lock(); subscribers[service,default:0] += 1; lock.unlock()
        defer { lock.lock(); subscribers[service,default:1] -= 1; if subscribers[service] == 0 { subscribers.removeValue(forKey:service) }; lock.unlock() }
        var cursor = after
        while true {
            lock.lock()
            guard ledger.services.contains(where: { $0.id == service && $0.paused != true }) else { lock.unlock(); return }
            let rows = Array(receiptRows(service:service,after:cursor).prefix(32))
            if rows.isEmpty {
                let changed = lock.wait(until:Date().addingTimeInterval(18))
                lock.unlock()
                if !changed && !write(Data(": heartbeat\n\n".utf8)) { return }
                continue
            }
            lock.unlock()
            for row in rows {
                lock.lock()
                let active = ledger.services.contains(where: { $0.id == service && $0.paused != true })
                lock.unlock()
                guard active else { return }
                let value:[String:Any] = ["id":row.id,"sequence":row.sequence,"state":row.state,"reply":row.reply]
                guard let json = try? JSONSerialization.data(withJSONObject:value) else { return }
                var frame = Data("id: \(row.sequence)\ndata: ".utf8)
                frame.append(json); frame.append(Data("\n\n".utf8))
                guard write(frame) else { return }
                cursor = row.sequence
            }
        }
    }
    private func schedule(_ e:Event, retryDelay: TimeInterval = 2) {
        work.async { [weak self] in
            guard let self else { return }
            lock.lock()
            guard let initial = ledger.services.first(where: { $0.id == e.service && $0.paused != true }) else { lock.unlock(); return }
            let destination = e.route == "dot_return" ? "dot" : (initial.destination ?? (initial.herald == true ? "herald" : "demo"))
            lock.unlock()
            if destination == "dot" { dotSlots.wait() }
            lock.lock()
            guard let index = ledger.events.firstIndex(where: { $0.service == e.service && $0.id == e.id && $0.state == "queued" }),
                  ledger.services.contains(where: { $0.id == e.service && $0.paused != true }) else {
                lock.unlock(); if destination == "dot" { dotSlots.signal() }; return
            }
            let herald = destination == "herald"
            var claim = ledger
            if retryDelay > 2 {
                claim.events[index].state = "dispatching"
            } else {
                claim.events[index] = Self.marked(claim.events[index],herald ? "sending_banner" : "dispatching")
            }
            do { try persist(claim) } catch { lock.unlock(); if destination == "dot" { dotSlots.signal() }; return }
            lock.unlock()
            if destination == "dot" {
                DispatchQueue.global(qos:.utility).async { [weak self] in
                    defer { self?.dotSlots.signal() }
                    self?.deliverToDot(e, retryDelay: retryDelay)
                }
                return
            }
            let state:String, reply:String
            do {
                if herald {
                    if let path = heraldSupport { reply = try sendHerald(service:e.service,id:e.id,payload:e.payload,supportDirectory:path) }
                    else { reply = try sendHerald(service:e.service,id:e.id,payload:e.payload) }
                    state = "herald_accepted"
                }
                else { reply = try handler(e.service,e.id,e.payload); state = "local_reply" }
            } catch {
                reply = herald ? "No confirmed Herald receipt. Check payload and Herald history before retrying." : "Local adapter failed; check the fixture."
                state = herald ? "herald_unconfirmed" : "adapter_failed"
            }
            lock.lock(); defer { lock.unlock() }
            guard let ix = ledger.events.firstIndex(where:{$0.id == e.id && $0.service == e.service}), let sx = ledger.services.firstIndex(where:{$0.id == e.service}) else { return }
            var next = ledger; next.seq += 1; next.events[ix] = Self.marked(next.events[ix],state); next.events[ix].sequence = next.seq
            next.events[ix].reply = String(reply.prefix(16000))
            if next.events[ix].payloadDigest == nil { next.events[ix].payloadDigest = Self.digest(next.events[ix].payload) }
            next.events[ix].payload = Data(); next.services[sx].state = state
            do { try persist(next); lock.broadcast() } catch { /* Dispatch outcome remains unknown on restart. */ }
        }
    }
    private static func dotRank(_ state:String) -> Int {
        switch state {
        case "dot_acknowledged": return 3
        case "dot_replied", "dot_cancelled", "dot_expired", "dot_quarantined": return 4
        case "dot_stored", "dot_waiting_for_link", "dot_route_blocked": return 1
        default: return 0
        }
    }
    @discardableResult private func recordDotReceipt(service:String, id:String, state:String, reply:String) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard let ix = ledger.events.firstIndex(where: { $0.id == id && $0.service == service }),
              let sx = ledger.services.firstIndex(where: { $0.id == service && ($0.destination == "dot" || ledger.events[ix].route == "dot_return") }) else { return true }
        let old = ledger.events[ix]
        guard Self.dotRank(state) >= Self.dotRank(old.state),
              !(Self.dotRank(old.state) == 4 && old.state != state) else { return true }
        let previous = (ledger.receipts ?? []).contains { $0.service == service && $0.id == id && $0.state == state }
        if previous {
            // Reconcile a repeated receipt after a retry without publishing it twice.
            // The transient state still must leave dispatching so another retry can run.
            if old.state == "dispatching" || old.state == "queued" {
                var next = ledger
                next.events[ix].state = state
                next.events[ix].retrying = false
                next.services[sx].state = state
                do { try persist(next); lock.broadcast() } catch { return false }
            }
            return true
        }
        var next = ledger; next.seq += 1
        next.receipts = (next.receipts ?? []) + [Receipt(service:service,id:id,sequence:next.seq,state:state,reply:reply)]
        next.events[ix] = Self.marked(next.events[ix],state,basis:state.hasPrefix("dot_") && !["dot_unconfirmed","dot_route_blocked","dot_waiting_for_link","dot_stored","dot_quarantined"].contains(state) ? "native_observed" : "broker_recorded"); next.events[ix].sequence = next.seq; next.events[ix].reply = nil; next.events[ix].retrying = false
        if ["dot_replied","dot_cancelled","dot_expired","dot_quarantined"].contains(state) {
            if next.events[ix].payloadDigest == nil { next.events[ix].payloadDigest = Self.digest(next.events[ix].payload) }
            next.events[ix].payload = Data()
        }
        next.services[sx].state = state
        do { try persist(next); lock.broadcast(); return true } catch { return false }
    }
    private func recordDotPush(service:String, sourceID:String, detail:String) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard sourceID.hasPrefix("@dot:"), !sourceID.dropFirst(5).isEmpty,
              sourceID.dropFirst(5).unicodeScalars.allSatisfy(CharacterSet.decimalDigits.contains) else { return true }
        guard ledger.services.contains(where: { $0.id == service && ["dot","herald"].contains($0.destination ?? ($0.herald == true ? "herald" : "demo")) }),
              !ledger.events.contains(where: { $0.service == service && $0.sourceID == sourceID }) else { return true }
        var next = ledger
        var event=Event(service:service,id:sourceID,payload:Data(detail.utf8),state:"queued",sourceID:sourceID,payloadDigest:Self.digest(Data(detail.utf8)))
        event.sop=next.services.first(where:{$0.id==service})?.sop
        next.events.append(Self.marked(event,"queued",basis:"native_observed"))
        do { try persist(next) } catch { return false }
        DispatchQueue.global(qos:.utility).async { [weak self] in self?.publishDotPush(service:service,id:sourceID) }
        return true
    }
    private func publishDotPush(service:String, id:String) {
        lock.lock()
        if ledger.services.contains(where: { $0.id == service && $0.destination == "herald" }) {
            lock.unlock()
            deliverHeraldPush(service:service,id:id)
            return
        }
        defer { lock.unlock() }
        guard let ix = ledger.events.firstIndex(where: { $0.service == service && $0.id == id && $0.state == "queued" }),
              let sx = ledger.services.firstIndex(where: { $0.id == service && $0.destination == "dot" && $0.paused != true }) else { return }
        let detail = String(data:ledger.events[ix].payload,encoding:.utf8) ?? ""
        var next = ledger; next.seq += 1
        next.receipts = (next.receipts ?? []) + [Receipt(service:service,id:id,sequence:next.seq,state:"dot_push",reply:detail)]
        next.events[ix] = Self.marked(next.events[ix],"dot_push"); next.events[ix].sequence = next.seq; next.events[ix].reply = nil
        if next.events[ix].payloadDigest == nil { next.events[ix].payloadDigest = Self.digest(next.events[ix].payload) }
        next.events[ix].payload = Data()
        next.services[sx].state = "dot_push"
        do { try persist(next); lock.broadcast() } catch { /* Still queued for restart or resume. */ }
    }
    private func heraldPayload(_ data:Data) -> Data? {
        guard let raw = try? JSONSerialization.jsonObject(with:data) as? [String:Any],
              Set(raw.keys) == ["kind","title","body"], raw["kind"] as? String == "herald.notify",
              let title = raw["title"] as? String, let body = raw["body"] as? String else { return nil }
        return try? JSONSerialization.data(withJSONObject:["title":title,"body":body],options:[.sortedKeys])
    }
    private func deliverHeraldPush(service:String, id:String) {
        lock.lock()
        guard let ix = ledger.events.firstIndex(where: { $0.service == service && $0.id == id && $0.state == "queued" }),
              ledger.services.contains(where: { $0.id == service && $0.destination == "herald" && $0.paused != true }) else { lock.unlock(); return }
        let event = ledger.events[ix]
        if heraldPayload(event.payload) == nil {
            lock.unlock()
            finishHeraldPush(service:service,id:id,state:"herald_rejected",reply:"Expected exactly {kind: herald.notify, title: string, body: string}; no banner was sent.")
            return
        }
        var claim = ledger; claim.events[ix] = Self.marked(claim.events[ix],"sending_banner")
        do { try persist(claim) } catch { lock.unlock(); return }
        lock.unlock()
        let state:String, reply:String
        do {
            let payload = heraldPayload(event.payload)!
            if let path = heraldSupport { reply = try sendHerald(service:service,id:id,payload:payload,supportDirectory:path) }
            else { reply = try sendHerald(service:service,id:id,payload:payload) }
            state = "herald_accepted"
        } catch {
            state = "herald_unconfirmed"
            reply = "No confirmed Herald receipt. Check Herald History before retrying the same source ID."
        }
        finishHeraldPush(service:service,id:id,state:state,reply:reply)
    }
    private func finishHeraldPush(service:String, id:String, state:String, reply:String) {
        lock.lock(); defer { lock.unlock() }
        guard let ix = ledger.events.firstIndex(where: { $0.service == service && $0.id == id && ["queued","sending_banner"].contains($0.state) }),
              let sx = ledger.services.firstIndex(where: { $0.id == service && $0.destination == "herald" }) else { return }
        var next = ledger; next.seq += 1
        next.receipts = (next.receipts ?? []) + [Receipt(service:service,id:id,sequence:next.seq,state:state,reply:reply)]
        next.events[ix] = Self.marked(next.events[ix],state); next.events[ix].sequence = next.seq; next.events[ix].reply = nil
        if state != "herald_unconfirmed" {
            if next.events[ix].payloadDigest == nil { next.events[ix].payloadDigest = Self.digest(next.events[ix].payload) }
            next.events[ix].payload = Data()
        }
        next.services[sx].state = state
        var returnEvent:Event?
        if state == "herald_accepted" {
            let returnID = "herald-return-" + String(id.dropFirst(5))
            let text = "Herald accepted the notification. Banner visibility was not verified."
            let envelope:[String:String] = ["kind":"service.reply","in_reply_to":id,"text":text]
            if let payload = try? JSONSerialization.data(withJSONObject:envelope,options:[.sortedKeys]) {
                var event = Event(service:service,id:returnID,payload:payload,state:"queued",payloadDigest:Self.digest(payload),route:"dot_return")
                event.sop=next.services[sx].sop
                event=Self.marked(event,"queued")
                next.events.append(event); returnEvent = event
            }
        }
        do {
            try persist(next); lock.broadcast()
            if let returnEvent, next.services[sx].paused != true { schedule(returnEvent) }
        } catch { /* Remains indeterminate on restart; never repeat the Herald side effect automatically. */ }
    }
    private func deliverToDot(_ e:Event, retryDelay: TimeInterval) {
        do {
            let result = try dotSender(e.service,e.id,e.payload) { [weak self] state,reply in
                _ = self?.recordDotReceipt(service:e.service,id:e.id,state:state,reply:reply)
            }
            recordDotReceipt(service:e.service,id:e.id,state:result.state,reply:result.reply)
        } catch {
            recordDotReceipt(service:e.service,id:e.id,state:"dot_unconfirmed",reply:"Waiting for the local Dot transport to recover.")
            work.asyncAfter(deadline: .now() + retryDelay) { [weak self] in
                guard let self else { return }
                lock.lock()
                defer { lock.unlock() }
                guard let i = ledger.events.firstIndex(where: { $0.service == e.service && $0.id == e.id }),
                      ledger.events[i].state == "dot_unconfirmed",
                      ledger.services.contains(where: { $0.id == e.service && $0.paused != true }) else { return }
                var next = ledger
                next.events[i].state = "queued"
                next.events[i].retrying = true
                guard (try? persist(next)) != nil else { return }
                schedule(next.events[i], retryDelay: min(60, retryDelay * 2))
            }
        }
    }
}
