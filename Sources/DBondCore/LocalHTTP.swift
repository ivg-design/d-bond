import Darwin
import Foundation

enum Fault: Error, LocalizedError {
    case message(String)
    var errorDescription: String? { if case let .message(s) = self { return s }; return nil }
}
struct HTTPReply {
    var code: Int; var value: [String:Any]
    var stream: ((@escaping (Data)->Bool)->Void)?
    init(_ code:Int, stream:@escaping (@escaping (Data)->Bool)->Void) { self.code=code; self.value=[:]; self.stream=stream }
    init(_ code: Int, _ value: [String:Any]) { self.code = code; self.value = value }
}
struct Request { var method: String; var target: String; var headers: [String:String]; var body: Data }

// Bounded test listener: one HTTP/1.1 request per connection, literal IPv4 loopback only.
final class LocalHTTP {
    let fd: Int32; let port: Int
    private let slots = DispatchSemaphore(value: 32)
    init(_ wanted: UInt16) throws {
        let s = socket(AF_INET, SOCK_STREAM, 0)
        guard s >= 0 else { throw Fault.message("Could not open socket.") }
        var reuse: Int32 = 1
        setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout.size(ofValue: reuse)))
        var a = sockaddr_in(); a.sin_len = UInt8(MemoryLayout<sockaddr_in>.size); a.sin_family = sa_family_t(AF_INET)
        a.sin_addr.s_addr = inet_addr("127.0.0.1"); a.sin_port = wanted.bigEndian
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let success = withUnsafeMutablePointer(to: &a) { p in p.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            Darwin.bind(s,$0,length) == 0 && listen(s,32) == 0 && getsockname(s,$0,&length) == 0
        }}
        guard success else { close(s); throw Fault.message("Could not bind loopback port \(wanted); another instance may be running.") }
        fd = s; port = Int(UInt16(bigEndian:a.sin_port))
    }
    func start(_ handle: @escaping (Request)->HTTPReply) {
        let server = fd, port = port, slots = slots
        DispatchQueue.global(qos:.utility).async {
            while true {
                let c = accept(server,nil,nil)
                guard c >= 0 else { if errno == EINTR { continue }; return }
                guard slots.wait(timeout:.now()) == .success else { close(c); continue }
                DispatchQueue.global().async {
                    defer { close(c); slots.signal() }
                    var timeout = timeval(tv_sec:5,tv_usec:0); var yes: Int32 = 1
                    setsockopt(c,SOL_SOCKET,SO_RCVTIMEO,&timeout,socklen_t(MemoryLayout.size(ofValue:timeout)))
                    setsockopt(c,SOL_SOCKET,SO_SNDTIMEO,&timeout,socklen_t(MemoryLayout.size(ofValue:timeout)))
                    setsockopt(c,SOL_SOCKET,SO_NOSIGPIPE,&yes,socklen_t(MemoryLayout.size(ofValue:yes)))
                    let req=Self.read(c,port)
                    let result=req.map(handle) ?? HTTPReply(400,["error":"Invalid loopback HTTP request"])
                    // Hub validates the paired service's exact Origin; only a successful authorized
                    // request or preflight advertises CORS. Never reflect arbitrary web origins.
                    let cors: String
                    if [200,202,409,422,423,507].contains(result.code), let origin=req?.headers["origin"] {
                        cors="Access-Control-Allow-Origin: \(origin)\r\nVary: Origin\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: authorization, content-type, accept\r\n"
                    } else { cors="" }
                    func writeBytes(_ data:Data)->Bool {
                        data.withUnsafeBytes { buf in
                            var n=0
                            while n<buf.count {
                                let w=send(c,buf.baseAddress!.advanced(by:n),buf.count-n,0)
                                if w<=0 { return false }; n+=w
                            }
                            return true
                        }
                    }
                    if let stream=result.stream {
                        guard writeBytes(Data("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: close\r\n\(cors)\r\n".utf8)) else { return }
                        stream(writeBytes); return
                    }
                    let body=(try? JSONSerialization.data(withJSONObject:result.value)) ?? Data("{}".utf8)
                    var out=Data("HTTP/1.1 \(result.code) \(HTTPURLResponse.localizedString(forStatusCode:result.code))\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nContent-Length: \(body.count)\r\n\(cors)Connection: close\r\n\r\n".utf8)
                    out.append(body); _=writeBytes(out)
                }
            }
        }
    }
    private static func read(_ fd: Int32, _ port: Int) -> Request? {
        var data = Data(); let sep = Data([13,10,13,10]); var buf = [UInt8](repeating:0,count:4096)
        let deadline = ProcessInfo.processInfo.systemUptime + 8
        func fill() -> Bool {
            guard ProcessInfo.processInfo.systemUptime < deadline else { return false }
            let count = recv(fd,&buf,buf.count,0)
            guard count > 0 else { return false }; data.append(contentsOf:buf.prefix(count)); return true
        }
        while data.range(of:sep) == nil { guard data.count <= 8192, fill() else { return nil } }
        guard let range = data.range(of:sep), range.lowerBound <= 8192,
              let head = String(data:data[..<range.lowerBound],encoding:.utf8) else { return nil }
        var lines = head.components(separatedBy:"\r\n"); let parts = lines.removeFirst().split(separator:" ")
        guard parts.count == 3, parts[2] == "HTTP/1.1", parts[1].hasPrefix("/") else { return nil }
        var h = [String:String]()
        for line in lines {
            guard let i = line.firstIndex(of:":") else { return nil }
            let key = String(line[..<i]).lowercased()
            guard !key.isEmpty, h[key] == nil else { return nil }
            h[key] = line[line.index(after:i)...].trimmingCharacters(in:.whitespaces)
        }
        guard h["host"] == "127.0.0.1:\(port)", (h["origin"].map { $0.range(of:"^chrome-extension://[a-p]{32}$",options:.regularExpression) != nil } ?? true), h["transfer-encoding"] == nil,
              let len = Int(h["content-length"] ?? "0"), (0...196608).contains(len) else { return nil }
        data.removeSubrange(..<range.upperBound)
        while data.count < len { guard fill() else { return nil } }
        guard data.count == len else { return nil }
        return Request(method:String(parts[0]),target:String(parts[1]),headers:h,body:data)
    }
    deinit { shutdown(fd,SHUT_RDWR); close(fd) }
}
