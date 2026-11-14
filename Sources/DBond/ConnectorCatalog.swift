import Foundation

/// Pure catalog decisions shared by AppKit and the disposable setup fixture.
enum ConnectorCatalog {
    struct Route { let name: String; let destination: String; let paused: Bool }
    enum HeraldState: Equatable {
        case needsApp(String?), needsConnection, connected(String), paused(String), multiple
    }

    static let heraldRelease = URL(string: "https://github.com/ivg-design/herald/releases/latest")!
    static let heraldDMG = URL(string: "https://github.com/ivg-design/herald/releases/download/v1.9.2/Herald-1.9.2-build24-macOS.dmg")!
    static let heraldDMGName = "Herald-1.9.2-build24-macOS.dmg"
    static let heraldDMGSize: Int64 = 15_275_407
    static let heraldDMGSHA256 = "d3f99e2c9b22a9c44adc94c063e7428ecd733bd28e876cfa4500a66abe5f7279"
    static let claudeDesktopDownload = URL(string: "https://claude.com/download")!
    static let claudeCodeSetup = URL(string: "https://code.claude.com/docs/en/setup")!
    static let codexDownload = URL(string: "https://openai.com/codex/")!

    static func herald(hasApp: Bool, routes: [Route]) -> HeraldState {
        let matching = routes.filter { $0.destination == "herald" }
        if matching.count > 1 { return .multiple }
        if !hasApp { return .needsApp(matching.first?.name) }
        if let route = matching.first {
            return route.paused ? .paused(route.name) : .connected(route.name)
        }
        return .needsConnection
    }
}
