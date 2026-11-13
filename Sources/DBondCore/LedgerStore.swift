import Darwin
import Foundation
import SQLite3

// A single Hub owns this connection under its NSCondition. Each state transition is
// one WAL transaction; unchanged rows are never rewritten.
final class LedgerStore {
    private let db: OpaquePointer
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()
    private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

    init(file: URL) throws {
        var handle: OpaquePointer?
        guard sqlite3_open_v2(file.path, &handle, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nil) == SQLITE_OK,
              let handle else { throw Fault.message("Cannot open durable link ledger.") }
        db = handle
        chmod(file.path, 0o600)
        do {
            try exec("PRAGMA journal_mode=WAL")
            try exec("PRAGMA synchronous=FULL")
            try exec("CREATE TABLE IF NOT EXISTS services (id TEXT PRIMARY KEY, body BLOB NOT NULL)")
            try exec("CREATE TABLE IF NOT EXISTS events (service TEXT NOT NULL, id TEXT NOT NULL, body BLOB NOT NULL, PRIMARY KEY(service,id))")
            try exec("CREATE TABLE IF NOT EXISTS receipts (sequence INTEGER PRIMARY KEY, body BLOB NOT NULL)")
            try exec("CREATE TABLE IF NOT EXISTS direct_receipts (sequence INTEGER PRIMARY KEY, body BLOB NOT NULL)")
            try exec("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        } catch { sqlite3_close(handle); throw error }
    }
    deinit { sqlite3_close(db) }

    private func exec(_ sql:String) throws {
        guard sqlite3_exec(db,sql,nil,nil,nil) == SQLITE_OK else { throw Fault.message("Ledger transaction failed: \(String(cString:sqlite3_errmsg(db)))") }
    }
    private func statement(_ sql:String) throws -> OpaquePointer {
        var stmt:OpaquePointer?
        guard sqlite3_prepare_v2(db,sql,-1,&stmt,nil) == SQLITE_OK, let stmt else { throw Fault.message("Ledger statement failed: \(String(cString:sqlite3_errmsg(db)))") }
        return stmt
    }
    private func bind(_ value:String, to stmt:OpaquePointer, at index:Int32) {
        value.withCString { _ = sqlite3_bind_text(stmt,index,$0,-1,transient) }
    }
    private func bind(_ value:Data, to stmt:OpaquePointer, at index:Int32) {
        value.withUnsafeBytes { bytes in _ = sqlite3_bind_blob(stmt,index,bytes.baseAddress,Int32(bytes.count),transient) }
    }
    private func rows<T:Decodable>(_ table:String) throws -> [T] {
        let stmt = try statement("SELECT body FROM \(table) ORDER BY rowid")
        defer { sqlite3_finalize(stmt) }
        var result:[T] = []
        while true {
            let stepResult = sqlite3_step(stmt)
            if stepResult == SQLITE_DONE { break }
            guard stepResult == SQLITE_ROW else { throw Fault.message("Ledger read failed: \(String(cString:sqlite3_errmsg(db)))") }
            let size = Int(sqlite3_column_bytes(stmt,0))
            guard let pointer = sqlite3_column_blob(stmt,0) else { throw Fault.message("Ledger row is empty.") }
            result.append(try decoder.decode(T.self,from:Data(bytes:pointer,count:size)))
        }
        return result
    }
    func load() throws -> Ledger {
        let services:[Service] = try rows("services")
        let events:[Event] = try rows("events")
        let receipts:[Receipt] = try rows("receipts")
        let directReceipts:[DirectReceipt] = try rows("direct_receipts")
        let stmt = try statement("SELECT value FROM meta WHERE key='seq'")
        defer { sqlite3_finalize(stmt) }
        let outcome = sqlite3_step(stmt)
        guard outcome == SQLITE_ROW || outcome == SQLITE_DONE else { throw Fault.message("Ledger metadata read failed.") }
        let seq = outcome == SQLITE_ROW ? Int(sqlite3_column_int64(stmt,0)) : 0
        return Ledger(services:services,events:events,receipts:receipts,directReceipts:directReceipts,seq:seq)
    }
    func loadOrMigrate(legacy:URL) throws -> Ledger {
        let marker = try statement("SELECT value FROM meta WHERE key='legacy_imported'")
        let outcome = sqlite3_step(marker)
        guard outcome == SQLITE_ROW || outcome == SQLITE_DONE else { sqlite3_finalize(marker); throw Fault.message("Ledger migration marker read failed.") }
        let marked = outcome == SQLITE_ROW
        sqlite3_finalize(marker)
        var current = try load()
        if !marked {
            if current.services.isEmpty && current.events.isEmpty && current.receipts?.isEmpty != false,
               FileManager.default.fileExists(atPath:legacy.path) {
                let imported = try decoder.decode(Ledger.self,from:Data(contentsOf:legacy))
                try save(old:current,next:imported)
                current = imported
            }
            try exec("INSERT OR REPLACE INTO meta(key,value) VALUES('legacy_imported',1)")
        }
        return current
    }
    private func changed<K:Hashable,V:Encodable & Equatable>(old:[V], next:[V], key:(V)->K, remove:(K)throws->Void, upsert:(K,Data)throws->Void) throws {
        let oldRows = Dictionary(uniqueKeysWithValues:old.map { (key($0),$0) })
        let newRows = Dictionary(uniqueKeysWithValues:next.map { (key($0),$0) })
        for id in oldRows.keys where newRows[id] == nil { try remove(id) }
        for value in next {
            let id = key(value)
            if oldRows[id] != value { try upsert(id,try encoder.encode(value)) }
        }
    }
    func save(old:Ledger, next:Ledger) throws {
        try exec("BEGIN IMMEDIATE")
        do {
            let upService = try statement("INSERT INTO services(id,body) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body")
            let rmService = try statement("DELETE FROM services WHERE id=?")
            let upEvent = try statement("INSERT INTO events(service,id,body) VALUES(?,?,?) ON CONFLICT(service,id) DO UPDATE SET body=excluded.body")
            let rmEvent = try statement("DELETE FROM events WHERE service=? AND id=?")
            let upReceipt = try statement("INSERT INTO receipts(sequence,body) VALUES(?,?) ON CONFLICT(sequence) DO UPDATE SET body=excluded.body")
            let rmReceipt = try statement("DELETE FROM receipts WHERE sequence=?")
            let upDirectReceipt = try statement("INSERT INTO direct_receipts(sequence,body) VALUES(?,?) ON CONFLICT(sequence) DO UPDATE SET body=excluded.body")
            let rmDirectReceipt = try statement("DELETE FROM direct_receipts WHERE sequence=?")
            defer { for stmt in [upService,rmService,upEvent,rmEvent,upReceipt,rmReceipt,upDirectReceipt,rmDirectReceipt] { sqlite3_finalize(stmt) } }
            func step(_ stmt:OpaquePointer) throws {
                guard sqlite3_step(stmt) == SQLITE_DONE else { throw Fault.message("Ledger write failed: \(String(cString:sqlite3_errmsg(db)))") }
                sqlite3_reset(stmt); sqlite3_clear_bindings(stmt)
            }
            try changed(old:old.services,next:next.services,key:{$0.id},remove:{ id in bind(id,to:rmService,at:1); try step(rmService) },upsert:{ id,body in bind(id,to:upService,at:1); bind(body,to:upService,at:2); try step(upService) })
            try changed(old:old.events,next:next.events,key:{ $0.service + "\u{0}" + $0.id },remove:{ id in
                let pair=id.split(separator:"\u{0}",maxSplits:1).map(String.init); bind(pair[0],to:rmEvent,at:1); bind(pair[1],to:rmEvent,at:2); try step(rmEvent)
            },upsert:{ id,body in
                let pair=id.split(separator:"\u{0}",maxSplits:1).map(String.init); bind(pair[0],to:upEvent,at:1); bind(pair[1],to:upEvent,at:2); bind(body,to:upEvent,at:3); try step(upEvent)
            })
            try changed(old:old.receipts ?? [],next:next.receipts ?? [],key:{$0.sequence},remove:{ seq in sqlite3_bind_int64(rmReceipt,1,Int64(seq)); try step(rmReceipt) },upsert:{ seq,body in sqlite3_bind_int64(upReceipt,1,Int64(seq)); bind(body,to:upReceipt,at:2); try step(upReceipt) })
            try changed(old:old.directReceipts ?? [],next:next.directReceipts ?? [],key:{$0.receiptSequence},remove:{ seq in sqlite3_bind_int64(rmDirectReceipt,1,Int64(seq)); try step(rmDirectReceipt) },upsert:{ seq,body in sqlite3_bind_int64(upDirectReceipt,1,Int64(seq)); bind(body,to:upDirectReceipt,at:2); try step(upDirectReceipt) })
            if old.seq != next.seq {
                let stmt = try statement("INSERT OR REPLACE INTO meta(key,value) VALUES('seq',?)")
                defer { sqlite3_finalize(stmt) }
                sqlite3_bind_int64(stmt,1,Int64(next.seq)); try step(stmt)
            }
            try exec("COMMIT")
        } catch { try? exec("ROLLBACK"); throw error }
    }
}
