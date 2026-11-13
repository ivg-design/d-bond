import Foundation

func json(_ value: Any) -> Data { try! JSONSerialization.data(withJSONObject: value) }
func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { fatalError(message) }
}

let launcher = URL(fileURLWithPath: "/test/D-Bond.app/Contents/Resources/agent-installers/direct-stdio/launch.sh")
let localPlugin = ["installed": [["pluginId": "d-bond-local-control@d-bond-local", "installed": true,
                                  "enabled": true, "version": "0.1.1"]]]
let matchingServer: [[String: Any]] = [["name": "d-bond-agent", "enabled": true,
                                        "transport": ["type": "stdio", "command": "/bin/sh",
                                                      "args": [launcher.path]]]]
let local = AgentInstallState.inspectCodex(pluginJSON: json(localPlugin), mcpJSON: json(matchingServer),
                                           expectedVersion: "0.1.2", expectedLauncher: launcher)
require(local.plugin.status == .installed && local.plugin.enabled == true && local.plugin.updateAvailable,
        "Existing local plugin should be detected as updateable")
require(local.plugin.source == "Codex marketplace: d-bond-local", "Marketplace source")
require(local.directMCP.status == .installed && local.directMCP.enabled == true, "Exact direct launcher")

let githubPlugin = ["installed": [["pluginId": "d-bond-local-control@dbond-github", "installed": true,
                                   "enabled": false, "version": "0.2.0"]]]
let disabledServer: [[String: Any]] = [["name": "d-bond-agent", "enabled": false,
                                        "transport": ["type": "stdio", "command": "/bin/sh",
                                                      "args": [launcher.path]]]]
let github = AgentInstallState.inspectCodex(pluginJSON: json(githubPlugin), mcpJSON: json(disabledServer),
                                            expectedVersion: "0.1.2", expectedLauncher: launcher)
require(github.plugin.status == .installed && github.plugin.enabled == false && !github.plugin.updateAvailable,
        "Official GitHub plugin should not trigger a duplicate or downgrade")
require(github.directMCP.status == .installed && github.directMCP.enabled == false, "Disabled MCP is still installed")

let alternateServer: [[String: Any]] = [["name": "d-bond-agent", "enabled": true,
                                         "transport": ["type": "stdio", "command": "/bin/sh",
                                                       "args": ["/other/server.sh"]]]]
let conflict = AgentInstallState.inspectCodex(pluginJSON: json(["installed": []]), mcpJSON: json(alternateServer),
                                              expectedVersion: "0.1.2", expectedLauncher: launcher)
require(conflict.plugin.status == .missing && conflict.directMCP.status == .conflict,
        "Different command must be distinct from missing")
let alternateName: [[String: Any]] = [["name": "custom-dbond", "enabled": true,
                                       "transport": ["type": "stdio", "command": "/bin/sh",
                                                     "args": [launcher.path]]]]
let renamed = AgentInstallState.inspectCodex(pluginJSON: json(["installed": []]), mcpJSON: json(alternateName),
                                             expectedVersion: "0.1.2", expectedLauncher: launcher)
require(renamed.directMCP.status == .installed && renamed.directMCP.source == "Codex MCP: custom-dbond",
        "Recognize exact launcher under a custom server name")
let unavailable = AgentInstallState.inspectCodex(pluginJSON: nil, mcpJSON: nil,
                                                 expectedVersion: "0.1.2", expectedLauncher: launcher)
require(unavailable.plugin.status == .unavailable && unavailable.directMCP.status == .unavailable,
        "CLI failure is not absence")

let claude = AgentInstallState.inspectClaudeCode(pluginJSON: json([["id": "d-bond@d-bond-local",
    "version": "0.1.3", "enabled": true, "scope": "user", "installPath": "/private/path"]]),
    expectedVersion: "0.1.3")
require(claude.status == .installed && claude.enabled == true && !claude.updateAvailable,
        "Claude local plugin current")
require(claude.source == "Claude Code marketplace: d-bond-local · user", "Do not expose install path")
let claudeOther = AgentInstallState.inspectClaudeCode(pluginJSON: json([["id": "d-bond@other",
    "version": "0.1.3", "enabled": true]]), expectedVersion: "0.1.3")
require(claudeOther.status == .conflict, "Other marketplace needs review")
require(AgentInstallState.inspectClaudeCode(pluginJSON: json([]), expectedVersion: "0.1.3").status == .missing,
        "Valid empty list means missing")
require(AgentInstallState.inspectClaudeCode(pluginJSON: Data("{}".utf8), expectedVersion: "0.1.3").status == .unavailable,
        "Unexpected JSON is unavailable")

print("PASS: read-only Codex and Claude install-state parsing, official sources, version/update, disabled and alternate MCP, unavailable")
