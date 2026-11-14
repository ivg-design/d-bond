import Foundation

private func nativeCommand(_ mode:String) throws -> (Process,Pipe) {
    let fm = FileManager.default
    let environment = ProcessInfo.processInfo.environment
    #if DBOND_APP_BUNDLE
    let home = fm.homeDirectoryForCurrentUser
    let owned = DotConnector().selectedRoute == "owned"
    #else
    let fixture = environment["DBOND_TEST_HOME"].map { URL(fileURLWithPath: $0) }
    let home = fixture ?? fm.homeDirectoryForCurrentUser
    let owned = fixture != nil || DotConnector().selectedRoute == "owned"
    #endif
    let support = home.appendingPathComponent("Library/Application Support")
    let legacyRoot = environment["DBOND_DOTBRIDGE_DIR"] ?? support.appendingPathComponent("DotBridge").path
    let ownedRoot = support.appendingPathComponent("D-Bond/Dot Connector").path
    let root = owned ? ownedRoot : legacyRoot
    let embedded = Bundle.main.resourceURL?.appendingPathComponent("dot-runtime/python/bin/python3.13")
    let development = URL(fileURLWithPath: root).appendingPathComponent(owned ? "runtime/bin/python3" : "venv/bin/python")
    let python = environment["DBOND_DOT_PYTHON"].map { URL(fileURLWithPath: $0) } ??
        embedded.flatMap { fm.isExecutableFile(atPath: $0.path) ? $0 : nil } ?? development
    #if DBOND_APP_BUNDLE
    let helper = Bundle.main.url(forResource: "native_link", withExtension: "py")
    #else
    // SwiftPM probes run directly from the source checkout.
    let source = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("scripts/native_link.py")
    let helper = Bundle.main.bundleURL.pathExtension == "app" ? Bundle.main.url(forResource: "native_link", withExtension: "py") :
        environment["DBOND_DOT_HELPER"].map { URL(fileURLWithPath: $0) } ?? source
    #endif
    guard let helper, fm.isExecutableFile(atPath:python.path), fm.fileExists(atPath:helper.path) else { throw Fault.message("Native Dot helper unavailable.") }
    let p=Process(), out=Pipe()
    var configuration = environment
    configuration["DBOND_DOTBRIDGE_DIR"] = root
    configuration["DBOND_LEGACY_DOTBRIDGE_DIR"] = legacyRoot
    configuration["DBOND_OWNED_DOTBRIDGE_DIR"] = ownedRoot
    configuration["HOME"] = home.path
    configuration["DBOND_SELECTED_DOT_ROUTE"] = owned ? "owned" : "legacy"
    p.environment = configuration
    p.executableURL=python; p.arguments=["-I","-B",helper.path,mode]; p.standardOutput=out; p.standardError=FileHandle.nullDevice
    return (p,out)
}
func nativeStatus() -> String {
    do {
        let (p,out)=try nativeCommand("status"); try p.run()
        let data=out.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
        guard p.terminationStatus==0, let r=try JSONSerialization.jsonObject(with:data) as? [String:Any] else { throw Fault.message("Unavailable") }
        if r["blocked"] as? Bool == true { return "D-Bond route conflict. Sending is blocked." }
        return r["active"] as? Int == 1 ? "Dot is linked to d-bond. All paired services share this connection." : "No standing Dot link. Service pairings are saved; a queued request is not a wake."
    } catch { return "Dot is offline. Open Connectors → Dot to check the connection." }
}
func sendViaDot(service:String,id:String,payload:Data,onUpdate:@escaping (String,String)->Void) throws -> (state:String,reply:String) {
    let (p,out)=try nativeCommand("send"), input=Pipe()
    let body = try JSONSerialization.data(withJSONObject:["service":service,"id":id,"payload":try JSONSerialization.jsonObject(with:payload,options:.fragmentsAllowed)])
    p.standardInput=input; try p.run()
    try input.fileHandleForWriting.write(contentsOf:body); try input.fileHandleForWriting.close()
    let bytes=out.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
    guard p.terminationStatus==0, let obj=try JSONSerialization.jsonObject(with:bytes) as? [String:String], let state=obj["state"],let reply=obj["reply"] else { throw Fault.message("Native submission unconfirmed.") }
    return (state,reply)
}
func observeNativeDot(onExit: @escaping () -> Void, _ onUpdate:@escaping (String,String,String,String)->Bool) -> Process? {
    guard let (p,out)=try? nativeCommand("observe") else { return nil }
    do { try p.run() } catch { return nil }
    DispatchQueue.global(qos:.utility).async {
        // Drain any final committed receipts before opening a replacement observer.
        defer {
            if p.isRunning { p.terminate() }
            p.waitUntilExit()
            onExit()
        }
        var buffer=Data()
        while true {
            let chunk=out.fileHandleForReading.availableData; if chunk.isEmpty { break }; buffer.append(chunk)
            while let end=buffer.firstIndex(of:10) {
                let line=buffer.prefix(upTo:end); let row=(try? JSONSerialization.jsonObject(with:line)) as? [String:String]
                buffer.removeSubrange(...end)
                if let r=row, let s=r["service"], let i=r["id"], let k=r["state"], let v=r["reply"] { var delay=0.2
                    while !onUpdate(s,i,k,v) { Thread.sleep(forTimeInterval:delay); delay=min(8,delay*2) }
                }
            }
            if buffer.count>1024*1024 { break }
        }
    }
    return p
}
