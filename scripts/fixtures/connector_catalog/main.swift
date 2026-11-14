import Foundation

func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { fatalError(message) }
}

let peer = ConnectorCatalog.Route(name:"Claude / Review",destination:"peer",paused:false)
let herald = ConnectorCatalog.Route(name:"Herald / Notifications",destination:"herald",paused:false)
require(ConnectorCatalog.herald(hasApp:false,routes:[peer]) == .needsApp(nil), "Missing Herald needs its public installer")
require(ConnectorCatalog.herald(hasApp:false,routes:[peer,herald]) == .needsApp("Herald / Notifications"), "Keep existing pairing while installing host")
require(ConnectorCatalog.herald(hasApp:true,routes:[peer]) == .needsConnection, "Offer one explicit pair")
require(ConnectorCatalog.herald(hasApp:true,routes:[peer,herald]) == .connected("Herald / Notifications"), "Adopt existing route")
require(ConnectorCatalog.herald(hasApp:true,routes:[herald,.init(name:"Other Herald",destination:"herald",paused:false)]) == .multiple, "Do not mint over duplicates")
require(ConnectorCatalog.herald(hasApp:true,routes:[.init(name:herald.name,destination:"herald",paused:true)]) == .paused(herald.name), "Respect pause")
require(ConnectorCatalog.heraldRelease.host == "github.com", "Use the verified public release page")
require(ConnectorCatalog.heraldDMG.host == "github.com" && ConnectorCatalog.heraldDMG.lastPathComponent == ConnectorCatalog.heraldDMGName, "Use the pinned official DMG")
require(ConnectorCatalog.heraldDMGSHA256.count == 64 && ConnectorCatalog.heraldDMGSize > 0, "Pin checksum and release size")
require(ConnectorCatalog.claudeDesktopDownload.host == "claude.com", "Use official Claude Desktop download")
require(ConnectorCatalog.claudeCodeSetup.host == "code.claude.com", "Use official Claude Code setup")
require(ConnectorCatalog.codexDownload.host == "openai.com", "Use official Codex download")
print("PASS: connector catalog host links, Herald pairing, pause and duplicate decisions")
