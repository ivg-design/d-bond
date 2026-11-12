import Darwin
import Foundation

/// Pair a local session without stopping the menu app. No HTTP/admin route; newly
/// minted single-service grants only. Both the containing directory and the peer
/// UID are checked; this cannot read another service's stored secret.
public final class OwnerSocket {
    private let fd:Int32
    private let path:String
    public init(hub:Hub,directory:URL? = nil) throws {
        let base=directory ?? FileManager.default.urls(for:.applicationSupportDirectory,in:.userDomainMask)[0].appendingPathComponent("D-Bond Proof of Concept")
        let dir=try FileManager.default.attributesOfItem(atPath:base.path)
        guard (dir[.ownerAccountID] as? NSNumber)?.uint32Value==getuid(),dir[.type] as? FileAttributeType == .typeDirectory,((dir[.posixPermissions] as? NSNumber)?.intValue ?? 511)&0o077==0 else {throw Fault.message("Unsafe local pairing directory.")}
        path=base.appendingPathComponent("pair.sock").path
        var addr=sockaddr_un(); addr.sun_family=sa_family_t(AF_UNIX)
        let bytes=Array(path.utf8CString); guard bytes.count <= MemoryLayout.size(ofValue:addr.sun_path) else {throw Fault.message("Local pairing path is too long.")}
        withUnsafeMutableBytes(of:&addr.sun_path) { $0.copyBytes(from:bytes.map { UInt8(bitPattern:$0) }) }
        var st=stat()
        if lstat(path,&st)==0 {
            guard (st.st_mode&S_IFMT)==S_IFSOCK,st.st_uid==getuid() else {throw Fault.message("Unsafe pairing socket.")}
            unlink(path)
        }
        let s=socket(AF_UNIX,SOCK_STREAM,0); guard s>=0 else {throw Fault.message("Could not open pairing socket.")}
        let size=socklen_t(MemoryLayout<sockaddr_un>.size)
        let ok=withUnsafePointer(to:&addr){ p in p.withMemoryRebound(to:sockaddr.self,capacity:1){ Darwin.bind(s,$0,size)==0 }}
        guard ok,chmod(path,0o600)==0,listen(s,8)==0 else {close(s); throw Fault.message("Local pairing socket unavailable.")}
        fd=s
        DispatchQueue.global(qos:.utility).async { [weak hub] in
            while let hub {
                let c=accept(s,nil,nil); if c<0 {if errno==EINTR {continue}; break}
                defer {close(c)}
                var uid:uid_t=0, gid:gid_t=0
                guard getpeereid(c,&uid,&gid)==0,uid==getuid() else {continue}
                var tv=timeval(tv_sec:2,tv_usec:0), yes:Int32=1
                setsockopt(c,SOL_SOCKET,SO_RCVTIMEO,&tv,socklen_t(MemoryLayout.size(ofValue:tv)))
                setsockopt(c,SOL_SOCKET,SO_SNDTIMEO,&tv,socklen_t(MemoryLayout.size(ofValue:tv)))
                setsockopt(c,SOL_SOCKET,SO_NOSIGPIPE,&yes,socklen_t(MemoryLayout.size(ofValue:yes)))
                var data=Data(), buf=[UInt8](repeating:0,count:512)
                while data.count<=262144 && !data.contains(10) {let n=recv(c,&buf,buf.count,0); if n<=0 {break};data.append(contentsOf:buf.prefix(n))}
                let reply:Data
                do {
                    guard data.count<=262144,let obj=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any],let op=obj["op"] as? String else {throw Fault.message("Invalid owner request.")}
                    func sop(_ value:Any?) throws -> OwnerSOP? {
                        guard let value, !(value is NSNull) else { return nil }
                        return try JSONDecoder().decode(OwnerSOP.self,from:JSONSerialization.data(withJSONObject:value))
                    }
                    func output(_ value:[String:Any]) throws -> Data { try JSONSerialization.data(withJSONObject:value,options:[.sortedKeys]) }
                    switch op {
                    case "pair":
                        guard let name=obj["name"] as? String, Set(obj.keys).isSubset(of:["op","name","sop","chromeOrigin"]),
                              obj["chromeOrigin"] == nil || obj["chromeOrigin"] is String else {throw Fault.message("Invalid pairing request.")}
                        reply=try JSONEncoder().encode(hub.pair(name:name,destination:"dot",chromeOrigin:obj["chromeOrigin"] as? String,sop:try sop(obj["sop"])))
                    case "rename":
                        guard Set(obj.keys)==["op","id","name"],let id=obj["id"] as? String,let name=obj["name"] as? String else {throw Fault.message("Invalid rename request.")}
                        try hub.rename(id:id,name:name);reply=Data(#"{"renamed":true}"#.utf8)
                    case "list":
                        guard Set(obj.keys)==["op"] else {throw Fault.message("Invalid list request.")}
                        let rows=hub.snapshot().services.map { row -> [String:Any] in
                            ["id":row.id,"name":row.name,"destination":row.destination,"paused":row.paused,"sopVersion":row.sop?.version ?? 0]
                        }
                        reply=try output(["services":rows])
                    case "get_sop":
                        guard Set(obj.keys)==["op","id"],let id=obj["id"] as? String else {throw Fault.message("Invalid SOP request.")}
                        let current=try hub.getSOP(service:id)
                        reply=try output(["id":id,"sop":current.map { (try? JSONSerialization.jsonObject(with:JSONEncoder().encode($0))) ?? NSNull() } ?? NSNull()])
                    case "set_sop":
                        guard Set(obj.keys)==["op","id","expectedVersion","sop"],let id=obj["id"] as? String,
                              let expected=obj["expectedVersion"] as? Int,let next=try sop(obj["sop"]) else {throw Fault.message("Invalid SOP update.")}
                        try hub.setSOP(service:id,sop:next,expectedVersion:expected)
                        reply=try output(["id":id,"sop":try JSONSerialization.jsonObject(with:JSONEncoder().encode(next))])
                    case "list_events":
                        guard Set(obj.keys).isSubset(of:["op","id","limit"]),let id=obj["id"] as? String else {throw Fault.message("Invalid event list request.")}
                        let events=try hub.recentEventTraces(service:id,limit:obj["limit"] as? Int ?? 20)
                        reply=try output(["events":try JSONSerialization.jsonObject(with:JSONEncoder().encode(events))])
                    case "trace":
                        guard Set(obj.keys)==["op","id","eventID"],let id=obj["id"] as? String,let eventID=obj["eventID"] as? String else {throw Fault.message("Invalid trace request.")}
                        let event=try hub.eventTrace(service:id,eventID:eventID)
                        reply=try output(["event":try event.map { try JSONSerialization.jsonObject(with:JSONEncoder().encode($0)) } ?? NSNull()])
                    default: throw Fault.message("Invalid owner operation.")
                    }
                } catch { reply=(try? JSONSerialization.data(withJSONObject:["error":String(describing:error)])) ?? Data(#"{"error":"Owner request failed."}"#.utf8) }
                let frame=reply+Data([10])
                frame.withUnsafeBytes { bytes in
                    var offset=0
                    while offset < bytes.count {
                        let sent=write(c,bytes.baseAddress!.advanced(by:offset),bytes.count-offset)
                        if sent > 0 { offset += sent }
                        else if sent < 0 && errno == EINTR { continue }
                        else { break }
                    }
                }
            }
        }
    }
    deinit {shutdown(fd,SHUT_RDWR);close(fd);unlink(path)}
}
