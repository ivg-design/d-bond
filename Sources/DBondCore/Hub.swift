import CoreFoundation
import CryptoKit
import Darwin
import Foundation

public extension Notification.Name {
    static let dbondLedgerUpdated = Notification.Name("DBondLedgerUpdated")
}

public struct OwnerRule: Codable, Equatable, Sendable { public var id,text:String; public init(id:String,text:String) { self.id=id; self.text=text } }
public struct OwnerSOP: Codable, Equatable, Sendable { public var version:Int; public var rules:[OwnerRule]; public init(version:Int,rules:[OwnerRule]) { self.version=version; self.rules=rules } }
public struct TraceStage: Codable, Equatable { public var state,at,basis:String }
public struct EventTrace: Codable {
    public var service,eventID,state:String
    public var sopVersion:Int?
    public var stages:[TraceStage]
    /// The direction is relative to the connection whose log was requested.
    public var direction:String?
    public var counterpartID:String?
    public var counterpartName:String?
    public var deliveryMeaning:String?
    public var inReplyTo:String?
    public var brokerSequence:Int?
    public var preview:String?
    public var wakeCheck:Bool?
    public var dotReplyPreview:String?
    public var dotReplyAt:String?
}
/// A stable slice of one connection's retained ledger history, newest first.
public struct EventTracePage {
    public let traces:[EventTrace]
    public let snapshotLastEventID:String?
    public let nextBeforeEventID:String?
    public let total:Int
}
public struct BrokerEventSummary {
    public let eventID, state, createdAt:String
    public let preview:String?
    public init(eventID:String,state:String,createdAt:String,preview:String?) {
        self.eventID=eventID; self.state=state; self.createdAt=createdAt; self.preview=preview
    }
}
public struct Pairing: Codable { public let id,secret,endpoint,destination: String; public let herald:Bool; public let chromeOrigin:String? }
public struct ServiceView { public let id,name,endpoint,destination: String; public let lastSeen,lastState,chromeOrigin: String?; public let herald,paused:Bool; public let queued,inFlight,subscribers,directSubscribers:Int; public let sop:OwnerSOP?; public let wakeState:String; public let lastVerifiedAt:String? }
public struct Snapshot { public let endpoint: String; public let services:[ServiceView]; public let eventCount:Int }
struct Service: Codable, Equatable { var id,name,digest:String; var seen,state:String?; var herald,paused:Bool?; var destination,chromeOrigin:String?; var sop:OwnerSOP? = nil }
struct HeraldRecipientPolicy: Codable, Equatable {
    let recipient: String
    let version: Int
    let rule: OwnerRule
}
struct Event: Codable, Equatable { var service,id:String; var payload:Data; var state:String; var sequence:Int?; var reply:String?; var sourceID:String? = nil; var payloadDigest:String? = nil; var retrying:Bool? = nil; var route:String? = nil; var sop:OwnerSOP? = nil; var recipientPolicy:HeraldRecipientPolicy? = nil; var stages:[TraceStage]? = nil; var preview:String? = nil; var replyToID:String? = nil }
struct Receipt: Codable, Equatable {
    var service,id:String; var sequence:Int; var state,reply:String
    var recordedAt:String? = nil
    var wire:[String:Any] {
        var result:[String:Any] = ["id":id,"sequence":sequence,"state":state,"reply":reply]
        if let recordedAt { result["recorded_at"] = recordedAt }
        return result
    }
}
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
    private var directSubscribers:[String:Int] = [:]
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
            if event.service == "owner", event.id.hasPrefix("@wake:"), event.route?.hasPrefix("peer:") == true {
                if let sequence = event.sequence {
                    let cleaned = try Self.wakePayload(state: event.state, sequence: sequence)
                    if cleaned != event.payload {
                        recovery.events[i].payload = cleaned
                        recovery.events[i].payloadDigest = Self.digest(cleaned)
                    }
                }
                continue
            }
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
        for event in ledger.events where event.service == "owner" && event.state == "wake_scheduled" {
            scheduleWake(event)
        }
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
    private func persist(_ candidate:Ledger) throws {
        var next = candidate
        let existingKeys = Set(ledger.events.map { $0.service + "\u{0}" + $0.id })
        for index in next.events.indices where !existingKeys.contains(next.events[index].service + "\u{0}" + next.events[index].id) {
            let event = next.events[index]
            next.events[index].preview = EventMessagePreview.make(event.payload,eventID:event.id,service:event.service)
            if event.route?.hasPrefix("peer:") == true,
               let body = (try? JSONSerialization.jsonObject(with:event.payload)) as? [String:Any] {
                next.events[index].replyToID = body["in_reply_to"] as? String
            }
        }
        try store.save(old:ledger,next:next)
        ledger = next
        DispatchQueue.main.async { NotificationCenter.default.post(name: .dbondLedgerUpdated, object: self) }
    }
    // Caller holds lock.
    private func receiptRows(service:String, after:Int) -> [Receipt] {
        let history = (ledger.receipts ?? []).filter { $0.service == service }
        let recordedIDs = Set(history.map { $0.id })
        let legacy = ledger.events.filter { $0.service == service && !($0.route ?? "").hasPrefix("peer:") && !recordedIDs.contains($0.id) && ($0.sequence ?? 0) > after }
            .map { Receipt(service:$0.service,id:$0.id,sequence:$0.sequence!,state:$0.state,reply:$0.reply ?? "") }
        // Version 0.2 receipts have no timestamp field, but the event trace
        // contains the original observation. Never timestamp a replay with "now".
        var eventsByID: [String:Event] = [:]
        return (legacy + history.filter { $0.sequence > after }).sorted { $0.sequence < $1.sequence }.prefix(32).map { row in
            guard row.recordedAt == nil else { return row }
            if eventsByID.isEmpty {
                eventsByID = Dictionary(uniqueKeysWithValues: ledger.events.filter { $0.service == service }.map { ($0.id,$0) })
            }
            var dated = row
            dated.recordedAt = eventsByID[row.id]?.stages?.first(where: { $0.state == row.state })?.at
            return dated
        }
    }
    public func snapshot() -> Snapshot {
        lock.lock(); defer { lock.unlock() }
        return Snapshot(endpoint:base,services:ledger.services.map { service in
            let events = ledger.events.filter { $0.service == service.id }
            return ServiceView(id:service.id,name:service.name,endpoint:endpoint(service.id),destination:service.destination ?? (service.herald == true ? "herald" : "demo"),lastSeen:service.seen,lastState:service.state,chromeOrigin:service.chromeOrigin,herald:service.herald == true,paused:service.paused == true,queued:events.filter { $0.state == "queued" }.count,inFlight:events.filter { ["dispatching","sending_banner"].contains($0.state) }.count,subscribers:subscribers[service.id] ?? 0,directSubscribers:directSubscribers[service.id] ?? 0,sop:service.sop,wakeState:wakeState(for:service.id),lastVerifiedAt:lastVerifiedAt(for:service.id))
        },eventCount:ledger.events.count)
    }
    public func dotLinkStatus() -> String { nativeStatus() }
    private func wakeRow(for service:String) -> Int? {
        ledger.events.lastIndex { $0.service == "owner" && $0.route == "peer:" + service && $0.id.hasPrefix("@wake:") &&
            ["wake_scheduled","wake_pending","wake_verified"].contains($0.state) }
    }
    private static func inactiveWake(_ event:Event) -> Bool {
        event.service == "owner" && event.id.hasPrefix("@wake:") && event.state != "wake_pending"
    }
    private func lastVerifiedAt(for service:String) -> String? {
        ledger.events.reversed().first { $0.service == "owner" && $0.route == "peer:" + service &&
            $0.id.hasPrefix("@wake:") && $0.state == "wake_verified" }?.stages?.last?.at
    }
    private func wakeState(for service:String) -> String {
        guard ledger.services.contains(where:{$0.id==service && $0.paused != true}) else { return "offline" }
        guard (directSubscribers[service] ?? 0) > 0 else { return "offline" }
        guard let index=wakeRow(for:service) else { return "pending" }
        return ledger.events[index].state == "wake_verified" ? "verified" : "pending"
    }
    private static func wakePayload(state: String, sequence: Int) throws -> Data {
        let text: String
        switch state {
        case "wake_pending":
            text = "D-Bond wake check. After reading this message in the paired session, acknowledge its message number with ~/.local/bin/dbond wake-ack ALIAS \(sequence). Replace ALIAS with this session's paired alias."
        case "wake_verified":
            text = "D-Bond wake check acknowledged."
        default:
            text = "D-Bond wake check superseded."
        }
        return try JSONSerialization.data(withJSONObject: ["text": text, "in_reply_to": NSNull()], options: [.sortedKeys])
    }

    /// Registration reuses its persisted check. Only an explicit retry replaces it.
    public func beginWakeTest(service:String,retry:Bool = false) throws -> (id:String,state:String) {
        lock.lock(); defer { lock.unlock() }
        guard ledger.services.contains(where:{$0.id==service && ["dot","peer"].contains($0.destination ?? "") && $0.paused != true}) else {
            throw Fault.message("Wake test requires an active paired direct connection.")
        }
        if let index = wakeRow(for: service), !retry {
            return (ledger.events[index].id, wakeState(for: service))
        }
        let event = try createWake(service: service, delayed: false)
        return (event.id, wakeState(for: service))
    }

    private func createWake(service: String, delayed: Bool) throws -> Event {
        var next = ledger
        for index in next.events.indices where next.events[index].service == "owner" &&
            next.events[index].route == "peer:" + service && next.events[index].id.hasPrefix("@wake:") &&
            ["wake_scheduled", "wake_pending"].contains(next.events[index].state) {
            next.events[index] = Self.marked(next.events[index], "wake_superseded")
            let payload = try Self.wakePayload(state: "wake_superseded", sequence: next.events[index].sequence ?? 0)
            next.events[index].payload = payload
            next.events[index].payloadDigest = Self.digest(payload)
        }
        let state = delayed ? "wake_scheduled" : "wake_pending"
        let id = "@wake:" + UUID().uuidString.lowercased()
        var event = Event(service: "owner", id: id, payload: Data(), state: state, route: "peer:" + service)
        if !delayed { try Self.assignWakeSequence(to: &event, in: &next) }
        event = Self.marked(event, state)
        next.events.append(event)
        try persist(next)
        if delayed { scheduleWake(event) }
        lock.broadcast()
        return event
    }

    private static func assignWakeSequence(to event: inout Event, in next: inout Ledger) throws {
        next.seq += 1
        event.sequence = next.seq
        event.payload = try wakePayload(state: "wake_pending", sequence: next.seq)
        event.payloadDigest = digest(event.payload)
    }

    /// Give the setup turn time to yield. Only the first registration creates a
    /// check, and its durable scheduled row survives a restart before delivery.
    public func finishRegistration(service:String) throws -> (id:String?,state:String,scheduled:Bool) {
        lock.lock(); defer { lock.unlock() }
        guard ledger.services.contains(where:{$0.id==service && ["peer","dot"].contains($0.destination ?? "")}) else {
            throw Fault.message("Direct connection no longer exists.")
        }
        let state = wakeState(for: service)
        if let index = wakeRow(for: service) {
            let event = ledger.events[index]
            return (event.id, state, event.state == "wake_scheduled")
        }
        guard state != "offline" else { return (nil, state, false) }
        let event = try createWake(service: service, delayed: true)
        return (event.id, state, true)
    }

    private func scheduleWake(_ event: Event) {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let scheduledAt = event.stages?.first.flatMap { formatter.date(from: $0.at) } ?? Date()
        let delay = max(0, scheduledAt.addingTimeInterval(9).timeIntervalSinceNow)
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + delay) { [weak self] in self?.deliverScheduledWake(id: event.id) }
    }

    private func deliverScheduledWake(id: String) {
        lock.lock(); defer { lock.unlock() }
        guard let index = ledger.events.firstIndex(where: { $0.service == "owner" && $0.id == id && $0.state == "wake_scheduled" }) else { return }
        var next = ledger
        var event = next.events[index]
        do {
            try Self.assignWakeSequence(to: &event, in: &next)
            next.events[index] = Self.marked(event, "wake_pending")
            try persist(next)
            lock.broadcast()
        } catch {
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 2) { [weak self] in self?.deliverScheduledWake(id: id) }
        }
    }

    private func acknowledgeWake(service:String, sequence:Int) -> HTTPReply {
        guard let index = wakeRow(for: service), ledger.events[index].sequence == sequence,
              ["wake_pending", "wake_verified"].contains(ledger.events[index].state) else {
            return HTTPReply(422, ["error":"This message is not the current wake check for this pairing."])
        }
        let event = ledger.events[index]
        let duplicate = event.state == "wake_verified"
        if !duplicate {
            do {
                var next = ledger
                next.events[index] = Self.marked(event, "wake_verified", basis: "authenticated_connection_ack")
                let payload = try Self.wakePayload(state: "wake_verified", sequence: sequence)
                next.events[index].payload = payload
                next.events[index].payloadDigest = Self.digest(payload)
                try persist(next)
                lock.broadcast()
            } catch { return HTTPReply(507, ["error":"Could not persist wake acknowledgement."]) }
        }
        return HTTPReply(200, ["id":event.id, "sequence":sequence, "state":"verified", "duplicate":duplicate])
    }
    /// Explicit owner-local note to one already paired, actively listening agent.
    /// This creates no pairing and offers no impersonation of another service.
    public func sendOwnerDirectNote(to recipientID:String, id messageID:String, text:String) throws -> Int {
        guard !messageID.isEmpty, messageID.utf8.count <= 120, !messageID.hasPrefix("@dot:"), !messageID.hasPrefix("@wake:"),
              !messageID.unicodeScalars.contains(where:CharacterSet.controlCharacters.contains),
              !text.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty, text.utf8.count <= 65536 else {
            throw Fault.message("Invalid owner direct note.")
        }
        lock.lock(); defer { lock.unlock() }
        guard ledger.services.contains(where: { $0.id == recipientID && $0.paused != true && ["dot","peer"].contains($0.destination ?? "") }),
              (directSubscribers[recipientID] ?? 0) > 0 else {
            throw Fault.message("Target agent has no active direct listener.")
        }
        let payload = try JSONSerialization.data(withJSONObject:["text":text,"in_reply_to":NSNull()],options:[.sortedKeys])
        if let old = ledger.events.first(where: { $0.service == "owner" && $0.id == messageID }) {
            guard old.route == "peer:" + recipientID && old.payloadDigest == Self.digest(payload) else { throw Fault.message("Owner note ID conflicts with existing content.") }
            return old.sequence ?? 0
        }
        var next=ledger; next.seq += 1
        var event=Event(service:"owner",id:messageID,payload:payload,state:"peer_delivered",sequence:next.seq,
                        payloadDigest:Self.digest(payload),route:"peer:" + recipientID)
        event=Self.marked(event,"peer_delivered")
        next.events.append(event)
        try persist(next); lock.broadcast()
        return next.seq
    }
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
        guard ["dot","demo","herald","peer"].contains(mode) else { throw Fault.message("Unknown destination.") }
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
    private static func visibility(_ event:Event, viewedBy service:String) -> (direction:String,counterpartID:String?)? {
        if let target=event.route?.hasPrefix("peer:") == true ? String(event.route!.dropFirst(5)) : nil {
            if event.service == service && target == service {
                return ("self",service)
            } else if event.service == service {
                return ("outbound",target)
            } else if target == service {
                return ("inbound",event.service)
            }
            return nil
        } else {
            return event.service == service ? ("local",nil) : nil
        }
    }
    private func trace(_ event:Event, viewedBy service:String) -> EventTrace? {
        guard let (direction,counterpartID)=Self.visibility(event,viewedBy:service) else { return nil }
        let name=counterpartID.map { id in
            id == "owner" ? "D-Bond owner" : (ledger.services.first(where:{$0.id==id})?.name ?? "Unknown connection")
        }
        let replyTo:String?
        if direction == "local" { replyTo=nil }
        else {
            let body=(try? JSONSerialization.jsonObject(with:event.payload)) as? [String:Any]
            replyTo=event.replyToID ?? body?["in_reply_to"] as? String
        }
        let fallback: String? = event.id.hasPrefix("@dot:") && event.payload.isEmpty
            ? (ledger.receipts ?? []).last(where: { $0.service == event.service && $0.id == event.id && $0.state == "dot_push" })
                .flatMap { EventMessagePreview.make(Data($0.reply.utf8),eventID:event.id,service:event.service) } : nil
        let preview = event.preview ?? EventMessagePreview.make(event.payload,eventID:event.id,service:event.service) ?? fallback
        let finalReply = event.state == "dot_replied"
            ? (ledger.receipts ?? []).last(where: { $0.service == event.service && $0.id == event.id && $0.state == "dot_replied" }) : nil
        let dotReplyPreview = finalReply.flatMap { EventMessagePreview.make(Data($0.reply.utf8),eventID:event.id,service:event.service) }
        let dotReplyAt = finalReply.flatMap { $0.recordedAt } ?? (event.stages ?? []).last(where: { $0.state == "dot_replied" })?.at
        let isWake=event.service == "owner" && event.id.hasPrefix("@wake:") &&
            ["wake_scheduled","wake_pending","wake_verified","wake_superseded"].contains(event.state) && event.route?.hasPrefix("peer:") == true
        return EventTrace(service:event.service,eventID:event.id,state:event.state,
                          sopVersion:event.sop?.version,stages:event.stages ?? [],
                          direction:direction,counterpartID:counterpartID,counterpartName:name,
                          deliveryMeaning:isWake
                            ? (event.state == "wake_verified" ? "Authenticated connection acknowledged wake check; AI turn not independently proven" :
                               event.state == "wake_scheduled" ? "Registration check scheduled" :
                               event.state == "wake_superseded" ? "A later wake check replaced this one" : "Wake check stored for recipient; acknowledgement pending")
                            : direction == "local" ? nil : "Stored by D-Bond for recipient; agent read unconfirmed.",
                          inReplyTo:replyTo,brokerSequence:direction == "local" ? nil : event.sequence,preview:preview,wakeCheck:isWake,
                          dotReplyPreview:dotReplyPreview,dotReplyAt:finalReply == nil ? nil : dotReplyAt)
    }
    private static func traceCursor(_ event:Event) -> String { event.service + "\u{0}" + event.id }
    public func eventTrace(service:String,eventID:String,direction:String? = nil,counterpartID:String? = nil) throws -> EventTrace? {
        lock.lock(); defer { lock.unlock() }
        guard ledger.services.contains(where:{$0.id==service}) else { throw Fault.message("Service connection no longer exists.") }
        let matches=ledger.events.compactMap { event -> EventTrace? in
            guard event.id==eventID, let row=trace(event,viewedBy:service),
                  direction == nil || row.direction == direction,
                  counterpartID == nil || row.counterpartID == counterpartID else { return nil }
            return row
        }
        guard matches.count <= 1 else { throw Fault.message("Event ID is ambiguous in this connection. Specify direction and counterpart ID.") }
        return matches.first
    }
    public func recentEventTraces(service:String,limit:Int = 20) throws -> [EventTrace] {
        lock.lock(); defer { lock.unlock() }
        guard ledger.services.contains(where:{$0.id==service}) else { throw Fault.message("Service connection no longer exists.") }
        return Array(ledger.events.reversed().lazy.compactMap { self.trace($0,viewedBy:service) }.prefix(max(0,min(limit,100))))
    }
    /// Read persisted service receipts under the broker's own lock. The caller
    /// can compare them with a native host without opening this store's WAL.
    public func storedEventSummaries(service:String) throws -> [BrokerEventSummary] {
        lock.lock(); defer { lock.unlock() }
        guard ledger.services.contains(where:{$0.id==service}) else { throw Fault.message("Service connection no longer exists.") }
        return ledger.events.compactMap { event in
            guard event.service == service,
                  let at = event.stages?.first?.at else { return nil }
            return BrokerEventSummary(eventID:event.id,state:event.state,createdAt:at,preview:event.preview)
        }
    }
    /// Cursor IDs refer to retained rows, so appends do not shift pages. The first
    /// call captures an upper bound; pass it back on every subsequent page.
    public func eventTracePage(service:String,through snapshotLastEventID:String? = nil,
                               before beforeEventID:String? = nil,limit:Int = 50) throws -> EventTracePage {
        lock.lock(); defer { lock.unlock() }
        guard ledger.services.contains(where:{$0.id==service}) else { throw Fault.message("Service connection no longer exists.") }
        let events=ledger.events
        let upper:Int
        if let snapshotLastEventID {
            guard let index=events.lastIndex(where:{Self.traceCursor($0)==snapshotLastEventID && Self.visibility($0,viewedBy:service) != nil}) else {
                throw Fault.message("Event history changed. Reopen the log.")
            }
            upper=index
        } else {
            upper=events.lastIndex(where:{Self.visibility($0,viewedBy:service) != nil}) ?? -1
        }
        let boundary:Int
        if let beforeEventID {
            guard let index=events.lastIndex(where:{Self.traceCursor($0)==beforeEventID && Self.visibility($0,viewedBy:service) != nil}), index<=upper else {
                throw Fault.message("Event history changed. Reopen the log.")
            }
            boundary=index
        } else { boundary=upper+1 }
        let pageSize=max(1,min(limit,100))
        var total=0, rows:[EventTrace]=[], hasOlder=false
        if upper>=0 {
            for index in 0...upper where Self.visibility(events[index],viewedBy:service) != nil { total+=1 }
            if boundary>0 {
                for index in stride(from:boundary-1,through:0,by:-1) {
                    guard Self.visibility(events[index],viewedBy:service) != nil else { continue }
                    if rows.count==pageSize { hasOlder=true; break }
                    if let row=trace(events[index],viewedBy:service) { rows.append(row) }
                }
            }
        }
        return EventTracePage(traces:rows,
                              snapshotLastEventID:snapshotLastEventID ?? (upper>=0 ? Self.traceCursor(events[upper]) : nil),
                              nextBeforeEventID:hasOlder ? rows.last.map { $0.service + "\u{0}" + $0.eventID } : nil,total:total)
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
        var next = ledger; next.services.removeAll { $0.id == id }; next.events.removeAll { $0.service == id || $0.route == "peer:" + id }; next.receipts?.removeAll { $0.service == id }
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
        guard [4,5].contains(p.count), p[0] == "v1", p[1] == "services", (p[3] == "events" || p[3] == "messages" || (p.count == 4 && p[3] == "peers")) else { return HTTPReply(404,[:]) }
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
        if p[3] == "messages" { return directRoute(r, url:url, parts:p, senderIndex:ix) }
        if p[3] == "peers" {
            guard r.method == "GET", url.query == nil else { return HTTPReply(405, [:]) }
            guard ledger.services[ix].paused != true else { return HTTPReply(423, ["error":"Service connection is paused."]) }
            let peers = ledger.services.filter { $0.destination == "dot" || $0.herald == true }
            return HTTPReply(200, ["services": peers.map { ["id":$0.id, "name":$0.name, "paused":$0.paused == true] as [String:Any] }])
        }
        if r.method == "GET", p.count == 5, p[4] == "history" {
            guard ledger.services[ix].paused != true else { return HTTPReply(423,["error":"Service connection is paused."]) }
            guard ledger.services[ix].destination == "dot" else { return HTTPReply(403,[:]) }
            let items = url.queryItems ?? []
            guard items.count == 2, let first = items.first(where: { $0.name == "after" })?.value,
                  let last = items.first(where: { $0.name == "through" })?.value,
                  let after = Int(first), let through = Int(last), after >= 0, through >= after else {
                return HTTPReply(400,["error":"Supply an inclusive upper sequence and an exclusive lower sequence."])
            }
            // Use the same retained receipts as SSE, without waiting or moving its cursor.
            let page = receiptRows(service:id,after:after).filter { $0.sequence <= through }
            return HTTPReply(200,["messages":page.filter { $0.state == "dot_push" }.map { $0.wire },
                                  "lastSequence":page.last?.sequence ?? after, "hasMore":page.count == 32 && (page.last?.sequence ?? after) < through])
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
                if !rows.isEmpty { return HTTPReply(200,["messages":rows.map { $0.wire }]) }
                if !lock.wait(until:until) { return HTTPReply(200,["messages":[]]) }
            }
        }
        guard r.method == "POST", p.count == 4, url.query == nil else { return HTTPReply(405,[:]) }
        guard ledger.services[ix].destination != "peer" else { return HTTPReply(422,["error":"Peer connections use /messages."]) }
        guard r.headers["content-type"]?.components(separatedBy:";").first?.lowercased() == "application/json",
              let body = (try? JSONSerialization.jsonObject(with:r.body)) as? [String:Any], Set(body.keys) == ["id","payload"],
              let eventID = body["id"] as? String, !eventID.isEmpty, !eventID.hasPrefix("@dot:"), eventID.utf8.count <= 120,
              !eventID.unicodeScalars.contains(where:CharacterSet.controlCharacters.contains), let payload = body["payload"],
              let data = try? JSONSerialization.data(withJSONObject:payload,options:[.sortedKeys,.fragmentsAllowed]) else { return HTTPReply(422,["error":"Expected {id, payload}."]) }
        if let old = ledger.events.first(where:{$0.service == id && $0.id == eventID}) {
            guard !(old.route ?? "").hasPrefix("peer:"),
                  (old.payloadDigest ?? Self.digest(old.payload)) == Self.digest(data) else { return HTTPReply(409,["error":"Same ID with different payload or route."]) }
            return HTTPReply(200,["id":eventID,"state":old.state,"duplicate":true])
        }
        guard ledger.services[ix].paused != true else { return HTTPReply(423,["error":"Service connection is paused; no new event was queued."]) }
        let toDot = (payload as? [String:Any])?["kind"] as? String == "conversation.request" && ledger.services[ix].herald == true
        var event = Event(service:id,id:eventID,payload:data,state:"queued",payloadDigest:Self.digest(data),route:toDot ? "dot_return" : nil)
        event.sop=ledger.services[ix].sop
        if ledger.services[ix].destination == "dot",
           let request = payload as? [String:Any], Set(request.keys) == ["kind","recipient_service","text"],
           request["kind"] as? String == "conversation.request", request["text"] is String,
           let targetID = request["recipient_service"] as? String,
           let target = ledger.services.first(where: { $0.id == targetID && $0.destination == "herald" && $0.paused != true }),
           let sop = target.sop, let rule = sop.rules.first(where: { $0.id == "general-notifications" }) {
            event.recipientPolicy = HeraldRecipientPolicy(recipient:targetID, version:sop.version, rule:rule)
        }
        event=Self.marked(event,"queued")
        var next = ledger; next.services[ix].seen = event.stages?.first?.at; next.services[ix].state = "queued"; next.events.append(event)
        do { try persist(next) } catch { return HTTPReply(507,["error":"Could not persist receipt."]) }
        schedule(event)
        return HTTPReply(202,["id":eventID,"state":"queued","duplicate":false])
    }
    /// Direct agent exchange is a separate namespace from Dot-bound events.
    /// Caller holds the Hub lock. Messages remain in the WAL ledger for offline replay.
    private func directRoute(_ r:Request, url:URLComponents, parts:[String], senderIndex:Int) -> HTTPReply {
        let sender = ledger.services[senderIndex]
        guard ["dot","peer"].contains(sender.destination ?? "") else { return HTTPReply(403,["error":"This connection cannot send direct messages."]) }
        guard sender.paused != true else { return HTTPReply(423,["error":"Service connection is paused."]) }
        if parts.count == 5 && parts[4] == "wake-ack" {
            guard r.method == "POST", url.query == nil,
                  r.headers["content-type"]?.components(separatedBy:";").first?.lowercased() == "application/json",
                  let body=(try? JSONSerialization.jsonObject(with:r.body)) as? [String:Any],
                  Set(body.keys) == ["sequence"], let number = body["sequence"] as? NSNumber,
                  CFGetTypeID(number) != CFBooleanGetTypeID(), let sequence = number as? Int, sequence > 0 else {
                return HTTPReply(422, ["error":"Expected a positive wake message sequence."])
            }
            return acknowledgeWake(service:sender.id, sequence:sequence)
        }
        if parts.count == 5 && parts[4] == "peers" {
            guard r.method == "GET", url.query == nil else { return HTTPReply(405,[:]) }
            let rows = ledger.services.filter { ["dot","peer"].contains($0.destination ?? "") && $0.id != sender.id }
            return HTTPReply(200,["services":rows.map { ["id":$0.id,"name":$0.name,"paused":$0.paused == true] as [String:Any] }])
        }
        if parts.count == 5 && parts[4] == "head" {
            guard r.method == "GET", url.query == nil else { return HTTPReply(405,[:]) }
            let latest = ledger.events.reversed().first { $0.route == "peer:" + sender.id }
            return HTTPReply(200,["sequence":latest?.sequence ?? 0])
        }
        let streaming = parts.count == 5 && parts[4] == "stream"
        guard parts.count == 4 || streaming else { return HTTPReply(404,[:]) }
        if r.method == "GET" {
            let query = url.queryItems ?? []
            guard query.count <= 1, query.allSatisfy({ $0.name == "after" && $0.value != nil }),
                  let after = Int(query.first?.value ?? "0"), after >= 0 else { return HTTPReply(400,[:]) }
            if streaming {
                guard r.headers["accept"]?.contains("text/event-stream") == true else { return HTTPReply(406,[:]) }
                return HTTPReply(200, stream: { [weak self] write in self?.directStream(service:sender.id,after:after,write:write) })
            }
            func rows() -> [[String:Any]] {
                ledger.events.filter { $0.route == "peer:" + sender.id && ($0.sequence ?? 0) > after && !Self.inactiveWake($0) }
                    .sorted { ($0.sequence ?? 0) < ($1.sequence ?? 0) }.prefix(64).compactMap { event in
                        guard let body = try? JSONSerialization.jsonObject(with:event.payload) as? [String:Any] else { return nil }
                        return ["id":event.id,"from":event.service,"to":sender.id,"text":body["text"] ?? "",
                                "in_reply_to":body["in_reply_to"] ?? NSNull(),"sequence":event.sequence ?? 0]
                    }
            }
            let until = Date().addingTimeInterval(12)
            while true {
                guard ledger.services.contains(where: { $0.id == sender.id && $0.paused != true }) else { return HTTPReply(423,[:]) }
                let messages = rows()
                if !messages.isEmpty { return HTTPReply(200,["messages":messages]) }
                if !lock.wait(until:until) { return HTTPReply(200,["messages":[]]) }
            }
        }
        guard !streaming, r.method == "POST", url.query == nil,
              r.headers["content-type"]?.components(separatedBy:";").first?.lowercased() == "application/json",
              let body = (try? JSONSerialization.jsonObject(with:r.body)) as? [String:Any],
              Set(body.keys).isSubset(of:["id","to","text","in_reply_to"]),
              let messageID = body["id"] as? String, !messageID.isEmpty, messageID.utf8.count <= 120,
              !messageID.hasPrefix("@dot:"), !messageID.hasPrefix("@wake:"), !messageID.unicodeScalars.contains(where:CharacterSet.controlCharacters.contains),
              let target = body["to"] as? String, let text = body["text"] as? String,
              !text.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty, text.utf8.count <= 65536,
              body["in_reply_to"] == nil || body["in_reply_to"] is String else { return HTTPReply(422,["error":"Expected {id,to,text,in_reply_to?}."]) }
        guard let recipient = ledger.services.first(where: { $0.id == target && ["dot","peer"].contains($0.destination ?? "") }) else { return HTTPReply(404,["error":"Direct recipient is unavailable."]) }
        guard recipient.paused != true else { return HTTPReply(423,["error":"Direct recipient is paused."]) }
        if let replyID = body["in_reply_to"] as? String {
            guard ledger.events.contains(where: { $0.id == replyID && $0.service == target && $0.route == "peer:" + sender.id }) else {
                return HTTPReply(422,["error":"Reply does not belong to these two connections."])
            }
        }
        let payload = (try? JSONSerialization.data(withJSONObject:["text":text,"in_reply_to":body["in_reply_to"] ?? NSNull()],options:[.sortedKeys])) ?? Data()
        guard !payload.isEmpty else { return HTTPReply(422,[:]) }
        if let old = ledger.events.first(where: { $0.service == sender.id && $0.id == messageID }) {
            guard old.route == "peer:" + target && (old.payloadDigest ?? Self.digest(old.payload)) == Self.digest(payload) else {
                return HTTPReply(409,["error":"Same ID with different direct message."])
            }
            return HTTPReply(200,["id":messageID,"sequence":old.sequence ?? 0,"duplicate":true])
        }
        var next = ledger
        next.seq += 1
        var event = Event(service:sender.id,id:messageID,payload:payload,state:"peer_delivered",sequence:next.seq,
                          payloadDigest:Self.digest(payload),route:"peer:" + target)
        event = Self.marked(event,"peer_delivered",basis:"broker_recorded")
        next.events.append(event)
        next.services[senderIndex].seen = Self.now()
        next.services[senderIndex].state = "peer_delivered"
        do { try persist(next); lock.broadcast() } catch { return HTTPReply(507,["error":"Could not persist direct message."]) }
        return HTTPReply(202,["id":messageID,"sequence":next.seq,"duplicate":false])
    }
    private func directStream(service:String, after:Int, write:(Data)->Bool) {
        lock.lock()
        directSubscribers[service,default:0] += 1
        lock.unlock()
        defer {
            lock.lock(); directSubscribers[service,default:1] -= 1
            if directSubscribers[service] == 0 { directSubscribers.removeValue(forKey:service) }
            lock.unlock()
        }
        var cursor = after
        while true {
            lock.lock()
            guard ledger.services.contains(where: { $0.id == service && $0.paused != true && ["dot","peer"].contains($0.destination ?? "") }) else { lock.unlock(); return }
            let rows = Array(ledger.events.filter { $0.route == "peer:" + service && ($0.sequence ?? 0) > cursor && !Self.inactiveWake($0) }
                .sorted { ($0.sequence ?? 0) < ($1.sequence ?? 0) }.prefix(64))
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
                let inactive = ledger.events.contains { $0.service == row.service && $0.id == row.id &&
                    $0.route == row.route && Self.inactiveWake($0) }
                lock.unlock()
                if inactive { cursor=row.sequence ?? cursor; continue }
                guard active, let body = try? JSONSerialization.jsonObject(with:row.payload) as? [String:Any] else { return }
                let value:[String:Any] = ["id":row.id,"from":row.service,"to":service,"text":body["text"] ?? "",
                                          "in_reply_to":body["in_reply_to"] ?? NSNull(),"sequence":row.sequence ?? 0]
                guard let json = try? JSONSerialization.data(withJSONObject:value) else { return }
                var frame=Data("id: \(row.sequence ?? 0)\ndata: ".utf8)
                frame.append(json);frame.append(Data("\n\n".utf8))
                guard write(frame) else { return }
                cursor=row.sequence ?? cursor
            }
        }
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
                let value = row.wire
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
        next.receipts = (next.receipts ?? []) + [Receipt(service:service,id:id,sequence:next.seq,state:state,reply:reply,recordedAt:Self.now())]
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
        next.receipts = (next.receipts ?? []) + [Receipt(service:service,id:id,sequence:next.seq,state:"dot_push",reply:detail,recordedAt:Self.now())]
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
            if let path = heraldSupport { reply = try sendHerald(service:service,id:id,payload:payload,supportDirectory:path,sender:.dot) }
            else { reply = try sendHerald(service:service,id:id,payload:payload,sender:.dot) }
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
        next.receipts = (next.receipts ?? []) + [Receipt(service:service,id:id,sequence:next.seq,state:state,reply:reply,recordedAt:Self.now())]
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
