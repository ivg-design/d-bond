import Foundation

/// Durable, sender-visible metadata, separate from inbound messages and Dot receipts.
/// `sequence` is the immutable original message cursor; receiptSequence is ledger ordering.
struct DirectReceipt: Codable, Equatable {
    var from,to,id:String
    var sequence,receiptSequence:Int
    var state,adapter,basis,recordedAt:String
    var rank:Int { state == "acknowledged" ? (basis == "participant" ? 4 : basis == "connection_reply" ? 3 : 2) : state == "received" ? 1 : 0 }
    var wire:[String:Any] {
        ["id":id,"from":from,"to":to,"sequence":sequence,"receipt_sequence":receiptSequence,
         "state":state,"adapter":adapter,"basis":basis,"recorded_at":recordedAt]
    }
}
