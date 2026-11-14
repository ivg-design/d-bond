import Foundation

@main struct HeraldInstallerFixture {
    static func main() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("dbond-herald-download-\(UUID().uuidString)", isDirectory:true)
        defer { try? FileManager.default.removeItem(at:directory) }
        let downloaded = try await HeraldInstaller.download(to:directory)
        try HeraldInstaller.verify(downloaded)
        let reused = try await HeraldInstaller.download(to:directory)
        guard reused == downloaded else { fatalError("A verified download should be reused") }
        let invalid = directory.appendingPathComponent("invalid.dmg")
        try FileManager.default.copyItem(at:downloaded,to:invalid)
        let reader = try FileHandle(forReadingFrom:invalid)
        let first = try reader.read(upToCount:1)![0]
        try reader.close()
        let file = try FileHandle(forWritingTo:invalid)
        try file.write(contentsOf:Data([first ^ 0xff]))
        try file.close()
        do {
            try HeraldInstaller.verify(invalid)
            fatalError("Corrupt download was accepted")
        } catch HeraldInstaller.Failure.checksum {
            print("PASS: public Herald DMG downloaded to temp, pinned SHA-256 verified, reused; corrupt file rejected")
        }
    }
}
