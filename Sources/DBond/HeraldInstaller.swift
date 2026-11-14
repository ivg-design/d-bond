import CryptoKit
import Foundation

/// Downloads one pinned, public Herald release. Opening the verified disk image
/// is a separate owner-initiated UI step; this helper never installs an app.
enum HeraldInstaller {
    enum Failure: LocalizedError {
        case response, size, checksum

        var errorDescription: String? {
            switch self {
            case .response: "The Herald release could not be downloaded."
            case .size: "The Herald download did not match the published release size."
            case .checksum: "The Herald download failed its pinned SHA-256 check."
            }
        }
    }

    static func verify(_ file: URL) throws {
        let size = try FileManager.default.attributesOfItem(atPath: file.path)[.size] as? NSNumber
        guard size?.int64Value == ConnectorCatalog.heraldDMGSize else { throw Failure.size }
        let stream = try FileHandle(forReadingFrom: file)
        defer { try? stream.close() }
        var hash = SHA256()
        while let block = try stream.read(upToCount: 1_048_576), !block.isEmpty {
            hash.update(data: block)
        }
        let digest = hash.finalize().map { String(format: "%02x", $0) }.joined()
        guard digest == ConnectorCatalog.heraldDMGSHA256 else { throw Failure.checksum }
    }

    static func download(to directory: URL) async throws -> URL {
        let manager = FileManager.default
        try manager.createDirectory(at: directory, withIntermediateDirectories: true)
        let preferred = directory.appendingPathComponent(ConnectorCatalog.heraldDMGName)
        if manager.fileExists(atPath: preferred.path), (try? verify(preferred)) != nil { return preferred }
        let (temporary, response) = try await URLSession.shared.download(from: ConnectorCatalog.heraldDMG)
        defer { try? FileManager.default.removeItem(at: temporary) }
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw Failure.response }
        try verify(temporary)
        if manager.fileExists(atPath: preferred.path), (try? verify(preferred)) != nil { return preferred }
        let destination = manager.fileExists(atPath: preferred.path)
            ? directory.appendingPathComponent("Herald-1.9.2-build24-verified-\(UUID().uuidString).dmg")
            : preferred
        try manager.moveItem(at: temporary, to: destination)
        return destination
    }
}
