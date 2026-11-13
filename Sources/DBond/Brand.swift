import AppKit
import DBondCore
import ImageIO
import UniformTypeIdentifiers

@MainActor
enum Brand {
    static let app = Bundle.main.url(forResource: "D-Bond", withExtension: "icns").flatMap(NSImage.init(contentsOf:)) ?? NSApp.applicationIconImage
    static func alert() -> NSAlert {
        let alert = NSAlert()
        alert.icon = app
        return alert
    }
    private static let directory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/D-Bond Proof of Concept/Service Icons", isDirectory: true)
    private static var cache: [String: NSImage] = [:]
    private static func raster(_ url: URL) -> NSImage? {
        guard let info = try? url.resourceValues(forKeys: [.isRegularFileKey,.fileSizeKey,.isSymbolicLinkKey]), info.isSymbolicLink != true,
              info.isRegularFile == true, let size = info.fileSize, size <= 2_097_152,
              let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let thumb = CGImageSourceCreateThumbnailAtIndex(src, 0, [kCGImageSourceCreateThumbnailFromImageAlways:true, kCGImageSourceThumbnailMaxPixelSize:128] as CFDictionary) else { return nil }
        return NSImage(cgImage:thumb,size:NSSize(width:thumb.width,height:thumb.height))
    }
    static func service(_ item: ServiceView) -> NSImage? {
        if let image = cache[item.id] { return image }
        let name = (item.name.components(separatedBy:" / ").first ?? "").lowercased()
        var image = raster(directory.appendingPathComponent(item.id + ".png"))
        if image == nil, ["codex","claude","herald"].contains(name) {
            image = raster(directory.appendingPathComponent(name + ".png"))
            if image == nil, let source = Bundle.main.url(forResource:name,withExtension:"png",subdirectory:"ServiceIcons") { image = raster(source) }
            let apps = ["codex":"com.openai.codex", "claude":"com.anthropic.claudefordesktop"]
            if image == nil, let bundle = apps[name], let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier:bundle) {
                if name == "codex", let source = Bundle(url:url)?.url(forResource:"icon-codex-light",withExtension:"png") { image = raster(source) }
                if image == nil { image = NSWorkspace.shared.icon(forFile:url.path) }
            }
        }
        if image == nil { image = NSImage(systemSymbolName: item.chromeOrigin == nil ? "point.3.connected.trianglepath.dotted" : "puzzlepiece.extension", accessibilityDescription:"Service") }
        if let image { cache[item.id] = image }
        return image
    }
    static func chooseIcon(for item: ServiceView) throws {
        let panel = NSOpenPanel()
        panel.message = "Choose a logo for " + item.name
        panel.allowedContentTypes = [.png,.jpeg,.icns,.tiff]; panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        guard let image = raster(url), let tiff = image.tiffRepresentation, let png = NSBitmapImageRep(data:tiff)?.representation(using:.png,properties:[:]) else {
            throw NSError(domain:"D-Bond",code:1,userInfo:[NSLocalizedDescriptionKey:"Choose a PNG, JPEG, TIFF or macOS icon smaller than 2 MB."])
        }
        try FileManager.default.createDirectory(at:directory,withIntermediateDirectories:true,attributes:[.posixPermissions:0o700])
        try png.write(to:directory.appendingPathComponent(item.id + ".png"),options:.atomic)
        cache[item.id] = nil
    }
}
