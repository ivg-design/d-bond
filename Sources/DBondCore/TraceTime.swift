import Foundation

/// Human-readable local rendering for ISO-8601 ledger timestamps. The stored value
/// remains untouched and precise; this formatter accepts older whole-second rows.
public func displayTraceTime(_ raw: String) -> String {
    let iso = ISO8601DateFormatter()
    iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let date = iso.date(from: raw) ?? {
        iso.formatOptions = [.withInternetDateTime]
        return iso.date(from: raw)
    }()
    guard let date else { return raw }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.timeZone = .autoupdatingCurrent
    formatter.dateFormat = "MMM d, yyyy, h:mm:ss a zzz"
    return formatter.string(from: date)
}
