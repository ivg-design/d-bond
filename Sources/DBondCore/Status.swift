import Foundation

public extension Hub {
    func bridgeStatus() -> String {
        return nativeStatus()
    }
    func heraldStatus() -> String {
        let path = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Herald/port")
        guard let raw = try? String(contentsOf:path), let port = UInt16(raw.trimmingCharacters(in:.whitespacesAndNewlines)), port > 0,
              let obj = probe("/usr/bin/curl",["-q","--noproxy","*","--fail","--silent","--max-time","2","http://127.0.0.1:\(port)/v1/health"]), obj["ok"] as? Bool == true else { return "Herald not reachable on its local health endpoint." }
        return "Herald \(obj["version"] as? String ?? "") reachable on this Mac. Health check only; no banner sent."
    }
}
private func probe(_ executable:String, _ args:[String]) -> [String:Any]? {
    let p = Process(), pipe = Pipe(), done = DispatchSemaphore(value:0)
    p.executableURL = URL(fileURLWithPath:executable); p.arguments = args; p.standardOutput = pipe; p.standardError = FileHandle.nullDevice
    p.terminationHandler = { _ in done.signal() }
    do { try p.run() } catch { return nil }
    if done.wait(timeout:.now()+5) == .timedOut { if p.isRunning { p.terminate() }; return nil }
    guard p.terminationStatus == 0 else { return nil }
    return (try? JSONSerialization.jsonObject(with:pipe.fileHandleForReading.readDataToEndOfFile())) as? [String:Any]
}
