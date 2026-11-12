import CryptoKit
import Darwin
import Foundation

private enum HeraldAdapterError: LocalizedError {
    case unavailable(String)
    case invalidPayload(String)
    case transport(String)
    case rejected(String)

    var errorDescription: String? {
        switch self {
        case .unavailable(let text), .invalidPayload(let text), .transport(let text), .rejected(let text): return text
        }
    }
}

private final class RedirectBlockingDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

private final class HeraldHTTPResult: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: (Data?, URLResponse?, Error?)?

    func set(data: Data?, response: URLResponse?, error: Error?) {
        lock.lock(); defer { lock.unlock() }
        stored = (data, response, error)
    }

    func get() -> (Data?, URLResponse?, Error?)? {
        lock.lock(); defer { lock.unlock() }
        return stored
    }
}

/// Send a safe, text-only Herald banner for an opted-in webhook service.
/// This overload resolves Herald's owner-only `token` and `port` files from the default support directory.
func sendHerald(service: String, id: String, payload: Data) throws -> String {
    let support = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Herald", isDirectory: true)
    return try sendHerald(service: service, id: id, payload: payload, supportDirectory: support)
}

/// Fixture-friendly overload. `supportDirectory` may point to a temporary private fixture containing
/// `token` and `port`; the HTTP destination remains literal IPv4 loopback in all cases.
func sendHerald(service: String, id: String, payload: Data, supportDirectory: URL) throws -> String {
    guard service.utf8.count <= 128, id.utf8.count <= 120,
          !service.isEmpty, !id.isEmpty,
          service.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_" )).contains($0) }),
          !id.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains) else {
        throw HeraldAdapterError.invalidPayload("Service or event ID is invalid.")
    }
    let object: Any
    do { object = try JSONSerialization.jsonObject(with: payload) }
    catch { throw HeraldAdapterError.invalidPayload("Herald payload must be a JSON object containing only title and body.") }
    guard let fields = object as? [String: Any], Set(fields.keys) == ["title", "body"],
          let title = fields["title"] as? String,
          let body = fields["body"] as? String else {
        throw HeraldAdapterError.invalidPayload("Herald accepts exactly two fields: title and body, both strings.")
    }
    try validateText(title, field: "title", characterLimit: 120, byteLimit: 480)
    try validateText(body, field: "body", characterLimit: 1200, byteLimit: 4800)
    guard payload.count <= 8192 else { throw HeraldAdapterError.invalidPayload("Herald payload is too large.") }

    let token = try readPrivateToken(at: supportDirectory.appendingPathComponent("token"))
    let portURL = supportDirectory.appendingPathComponent("port")
    let port = try readPort(at: portURL)
    let deterministicID = "dbond-" + SHA256.hash(data: Data("\(service):\(id)".utf8))
        .prefix(16).map { String(format: "%02x", $0) }.joined()

    let endpoint = URL(string: "http://127.0.0.1:\(port)/v1/notify")!
    let requestBody: [String: Any] = [
        "app": "agent.dbond",
        "id": deterministicID,
        "title": title,
        "body": body
    ]
    let encoded: Data
    do { encoded = try JSONSerialization.data(withJSONObject: requestBody, options: [.sortedKeys]) }
    catch { throw HeraldAdapterError.invalidPayload("Could not encode the restricted Herald banner payload.") }

    var request = URLRequest(url: endpoint, timeoutInterval: 3.0)
    request.httpMethod = "POST"
    request.httpBody = encoded
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

    let configuration = URLSessionConfiguration.ephemeral
    configuration.timeoutIntervalForRequest = 3.0
    configuration.timeoutIntervalForResource = 4.0
    configuration.httpCookieStorage = nil
    configuration.httpCookieAcceptPolicy = .never
    configuration.httpShouldSetCookies = false
    configuration.connectionProxyDictionary = [:]
    let delegate = RedirectBlockingDelegate()
    let session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: nil)
    defer { session.invalidateAndCancel() }

    let result = HeraldHTTPResult()
    let completed = DispatchSemaphore(value: 0)
    session.dataTask(with: request) { data, response, error in
        result.set(data: data, response: response, error: error)
        completed.signal()
    }.resume()
    guard completed.wait(timeout: .now() + 4.5) == .success else {
        throw HeraldAdapterError.transport("Herald request timed out; delivery outcome may be unknown. Check Herald History before retrying.")
    }
    guard let (data, response, error) = result.get() else {
        throw HeraldAdapterError.transport("Herald returned no response; check Herald History before retrying.")
    }
    if let error { throw HeraldAdapterError.transport("Herald request failed: \(error.localizedDescription). Check Herald History before retrying.") }
    guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        throw HeraldAdapterError.rejected("Herald rejected the banner request (HTTP \(status)).")
    }
    guard let data, data.count <= 16_384,
          let receipt = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
          receipt["ok"] as? Bool == true,
          receipt["id"] as? String == deterministicID else {
        throw HeraldAdapterError.rejected("Herald response did not confirm the expected notification ID; check Herald History before retrying.")
    }
    return "Herald accepted notification \(deterministicID); banner visibility was not verified."
}

private func validateText(_ value: String, field: String, characterLimit: Int, byteLimit: Int) throws {
    guard !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
          value.count <= characterLimit, value.utf8.count <= byteLimit,
          !value.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
          !value.localizedCaseInsensitiveContains("http://"),
          !value.localizedCaseInsensitiveContains("https://"),
          !(value.contains("[" ) && value.contains("](")) else {
        throw HeraldAdapterError.invalidPayload("Herald \(field) must be short plain text without control characters or links (maximum \(characterLimit) characters).")
    }
}

private func readPort(at url: URL) throws -> UInt16 {
    let data = try readOwnerFile(url, maximumBytes: 16, privateToOwner: false, description: "port")
    guard let raw = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) else {
        throw HeraldAdapterError.unavailable("Herald port file is not valid text.")
    }
    guard let port = UInt16(raw), port > 0 else { throw HeraldAdapterError.unavailable("Herald port file has an invalid port.") }
    return port
}

private func readPrivateToken(at url: URL) throws -> String {
    let data = try readOwnerFile(url, maximumBytes: 256, privateToOwner: true, description: "owner-only token")
    guard data.count >= 32, data.count <= 256,
          let token = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
          token.utf8.count >= 32, token.utf8.count <= 256,
          token.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "._-" )).contains($0) }) else {
        throw HeraldAdapterError.unavailable("Herald token file has an unsupported format.")
    }
    return token
}

private func readOwnerFile(_ url: URL, maximumBytes: Int, privateToOwner: Bool, description: String) throws -> Data {
    let descriptor = open(url.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
    guard descriptor >= 0 else { throw HeraldAdapterError.unavailable("Herald \(description) file is missing or inaccessible.") }
    defer { close(descriptor) }
    var info = stat()
    guard fstat(descriptor, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
          info.st_uid == getuid(), (info.st_mode & 0o022) == 0,
          (!privateToOwner || ((info.st_mode & 0o077) == 0 && (info.st_mode & 0o400) != 0)) else {
        throw HeraldAdapterError.unavailable("Herald \(description) file has unsafe ownership or permissions.")
    }
    var output = Data()
    var buffer = [UInt8](repeating: 0, count: min(maximumBytes + 1, 257))
    while output.count <= maximumBytes {
        let count = buffer.withUnsafeMutableBytes { Darwin.read(descriptor, $0.baseAddress, $0.count) }
        if count == 0 { break }
        guard count > 0 else { throw HeraldAdapterError.unavailable("Could not read Herald's \(description) file.") }
        output.append(contentsOf: buffer.prefix(count))
    }
    guard output.count <= maximumBytes else { throw HeraldAdapterError.unavailable("Herald \(description) file is unexpectedly large.") }
    return output
}
