import AppKit
import ServiceManagement

/// Controls the user's explicit Start at Login preference for the installed app.
/// The login item is the app itself; no helper process or credentials are added.
@MainActor
enum LoginRegistration {
    static var isEnabled: Bool {
        SMAppService.mainApp.status == .enabled
    }

    static func setEnabled(_ enabled: Bool) throws {
        if enabled {
            if SMAppService.mainApp.status != .enabled {
                try SMAppService.mainApp.register()
            }
        } else if SMAppService.mainApp.status == .enabled {
            try SMAppService.mainApp.unregister()
        }
    }
}
