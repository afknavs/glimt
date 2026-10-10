import SwiftUI

struct Capability {
    let type: String
    let min: Int
    let max: Int
    let options: [String]
}

struct Device: Identifiable {
    let id: String
    var name: String
    var kind: String
    var icon: String?
    var capabilities: [String: Capability]
    var actions: [String]
    var state: [String: Any]
    var online: Bool?
    var assumed: Bool
    var error: String?

    init?(_ raw: [String: Any]) {
        guard let id = raw["id"] as? String else { return nil }
        self.id = id
        name = raw["name"] as? String ?? id
        kind = raw["kind"] as? String ?? "other"
        icon = raw["icon"] as? String
        actions = raw["actions"] as? [String] ?? []
        state = raw["state"] as? [String: Any] ?? [:]
        online = raw["online"] as? Bool
        assumed = raw["assumed"] as? Bool ?? false
        error = raw["error"] as? String
        var caps: [String: Capability] = [:]
        for (key, value) in raw["capabilities"] as? [String: [String: Any]] ?? [:] {
            caps[key] = Capability(type: value["type"] as? String ?? "bool", min: value["min"] as? Int ?? 0,
                                   max: value["max"] as? Int ?? 100, options: value["options"] as? [String] ?? [])
        }
        capabilities = caps
    }

    func has(_ capability: String) -> Bool { capabilities[capability] != nil }
    var isColorLight: Bool { has("color") }
    var isClimate: Bool { kind == "climate" && has("temperature") }
    var isSimple: Bool { has("power") && !isColorLight && !isClimate }
    var isActionOnly: Bool { capabilities.isEmpty && !actions.isEmpty && kind != "speaker" }

    /// Worth a line in the panel: something you can switch, adjust or trigger.
    var isShown: Bool { has("power") || isClimate || isActionOnly }
    /// Settings with named options beyond the ones that have a control of their own.
    var choices: [String] {
        capabilities.filter { $0.value.type == "enum" && !$0.value.options.isEmpty && !(isClimate && $0.key == "fan") }.keys.sorted()
    }
    /// Numbers with no control of their own (an effect's speed, say): each gets a plain slider.
    var dials: [String] {
        capabilities.filter { $0.value.type == "int" && !["brightness", "temperature"].contains($0.key) }.keys.sorted()
    }
    /// A few glyph actions fit on the row itself; anything more opens the device.
    var quickActions: [String] {
        let known = actions.filter { Symbols.action($0) != nil }
        return isActionOnly && known.count == actions.count && actions.count <= 3 ? actions : []
    }
    /// More to it than on and off, so its tile opens a page of controls.
    var hasControls: Bool {
        isClimate || has("brightness") || isColorLight || !choices.isEmpty || !dials.isEmpty
            || (!actions.isEmpty && quickActions.isEmpty)
    }
    /// On and off, and nothing else.
    var isPlainSwitch: Bool { has("power") && !hasControls }
}

/// One square of the panel: a device, or several plain switches of a kind folded into one.
enum Tile: Identifiable {
    case device(Device)
    case group(kind: String, devices: [Device])

    /// Kinds that come in sets (the sockets of a power strip) and so share a tile, with its title.
    static let folded = ["outlet": "Outlets"]

    var id: String {
        switch self {
        case .device(let device): return device.id
        case .group(let kind, _): return "kind:" + kind
        }
    }
    /// It has a reading and controls of its own to show, so it takes the full width.
    var isWide: Bool {
        if case .device(let device) = self { return device.isClimate }
        return false
    }
}

struct Cue: Identifiable {
    let id: Int
    let name: String
    let message: String?
    let source: String?
    let time: Date
    let held: Bool
    /// Why it was not shown ("muted", "quiet hours"), if it was not.
    let skipped: String?

    /// Who asked for you: an agent's name when a hook sent it, otherwise the cue's own name.
    var title: String {
        if let source, source.first?.isUppercase == true { return source }
        return name.prefix(1).uppercased() + name.dropFirst()
    }
}

/// An app in the cloud that was let in through remote access.
struct RemoteApp: Identifiable {
    let id: String
    let name: String
}

/// A sign-in waiting for a yes or a no on this computer.
struct SignIn: Identifiable {
    let code: String
    let name: String
    let returnsTo: String
    var id: String { code }
}

struct Agent: Identifiable {
    let id: String
    let name: String
    let found: Bool
    var hooks: Bool
    let tools: Bool
}

enum Shell {
    /// Run a program and collect its output off the main thread, with a deadline.
    static func run(_ command: [String], environment: [String: String]? = nil,
                    timeout: TimeInterval = 45) async -> (ok: Bool, output: String) {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: command[0])
                process.arguments = Array(command.dropFirst())
                if let environment { process.environment = environment }
                let pipe = Pipe()
                process.standardOutput = pipe
                process.standardError = pipe
                process.standardInput = FileHandle.nullDevice
                do { try process.run() } catch {
                    continuation.resume(returning: (false, error.localizedDescription))
                    return
                }
                let deadline = DispatchWorkItem { if process.isRunning { process.terminate() } }
                DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: deadline)
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                deadline.cancel()
                continuation.resume(returning: (process.terminationStatus == 0, String(data: data, encoding: .utf8) ?? ""))
            }
        }
    }
}

@MainActor final class Store: ObservableObject {
    static let shared = Store()
    @Published var devices: [Device] = []
    @Published var connected = false
    @Published var activeCue: Cue?
    /// The last few cues, newest first: who wanted you, and what you missed while muted.
    @Published var recent: [Cue] = []
    @Published var muteUntil: Date?
    @Published var agents: [Agent] = []
    /// The agent being connected or disconnected right now.
    @Published var busy: String?
    @Published var hint: String?
    @Published var failure: String?
    @Published var remoteApps: [RemoteApp] = []
    @Published var signIns: [SignIn] = []
    /// The device whose controls are showing, if the panel is not on the list.
    @Published var detail: String?
    private var daemonCLI: [String]?
    private var shellPath: String?
    private var unmuteTimer: Task<Void, Never>?
    /// Values the user just set, shown until the daemon's echo arrives.
    private var pending: [String: (value: Any, until: Date)] = [:]
    private var debounce: [String: Task<Void, Never>] = [:]
    private var listener: Task<Void, Never>?
    private let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 90
        config.timeoutIntervalForResource = 60 * 60 * 24 * 7
        return URLSession(configuration: config)
    }()

    private var home: URL {
        if let custom = ProcessInfo.processInfo.environment["GLIMT_HOME"] { return URL(fileURLWithPath: custom) }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".glimt")
    }
    private var port: Int {
        // Just enough TOML to find `port` under [daemon]; everything else comes from the API.
        let text = (try? String(contentsOf: home.appendingPathComponent("config.toml"), encoding: .utf8)) ?? ""
        var section = ""
        for line in text.split(separator: "\n").map({ $0.trimmingCharacters(in: .whitespaces) }) {
            if line.hasPrefix("[") { section = line; continue }
            let parts = line.split(separator: "=").map { $0.trimmingCharacters(in: .whitespaces) }
            if section == "[daemon]", parts.count == 2, parts[0] == "port", let value = Int(parts[1]) { return value }
        }
        return 18770
    }
    private func request(_ method: String, _ path: String, _ body: [String: Any]? = nil) throws -> URLRequest {
        let token = try String(contentsOf: home.appendingPathComponent("token"), encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)\(path)")!)
        request.httpMethod = method
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        request.setValue("app", forHTTPHeaderField: "X-Glimt-Source")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        return request
    }

    func openDashboard() {
        guard let token = try? String(contentsOf: home.appendingPathComponent("token"), encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines),
              let url = URL(string: "http://127.0.0.1:\(port)/#token=\(token)") else { return }
        NSWorkspace.shared.open(url)
    }

    // MARK: live state

    func start() {
        guard listener == nil else { return }
        listener = Task { await listen() }
        Task { await refreshAgents() }   // agents do not depend on the daemon being up
    }

    private func listen() async {
        while !Task.isCancelled {
            do {
                let (bytes, response) = try await session.bytes(for: try request("GET", "/v1/events"))
                guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.userAuthenticationRequired) }
                connected = true
                failure = nil
                for try await line in bytes.lines where line.hasPrefix("data: ") {
                    if let data = line.dropFirst(6).data(using: .utf8),
                       let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { handle(event) }
                }
            } catch {}
            connected = false
            activeCue = nil
            try? await Task.sleep(for: .seconds(2))
        }
    }

    private func cue(_ raw: Any?) -> Cue? {
        guard let raw = raw as? [String: Any], let name = raw["name"] as? String else { return nil }
        return Cue(id: raw["id"] as? Int ?? 0, name: name, message: raw["message"] as? String, source: raw["source"] as? String,
                   time: Date(timeIntervalSince1970: raw["time"] as? Double ?? Date().timeIntervalSince1970),
                   held: (raw["hold"] as? Double ?? 0) > 0, skipped: raw["skipped"] as? String)
    }

    private func remember(_ cue: Cue) {
        // Your own taps on the test dots are not news.
        guard cue.source != "app", !recent.contains(where: { $0.id == cue.id }) else { return }
        recent = Array(([cue] + recent).sorted { $0.time > $1.time }.prefix(8))
    }

    private func setMute(_ raw: Any?) {
        unmuteTimer?.cancel()
        guard let until = raw as? Double, until > Date().timeIntervalSince1970 else { muteUntil = nil; return }
        muteUntil = Date(timeIntervalSince1970: until)
        unmuteTimer = Task { [weak self] in   // the bell comes back by itself when the time is up
            try? await Task.sleep(for: .seconds(min(until - Date().timeIntervalSince1970, 86_400 * 30)))
            if !Task.isCancelled, let self, (self.muteUntil ?? .distantFuture) <= Date() { self.muteUntil = nil }
        }
    }

    private func setRemote(_ raw: Any?) {
        let remote = raw as? [String: Any] ?? [:]
        remoteApps = (remote["apps"] as? [[String: Any]] ?? []).compactMap { app in
            (app["id"] as? String).map { RemoteApp(id: $0, name: app["name"] as? String ?? "App") }
        }
        signIns = (remote["pending"] as? [[String: Any]] ?? []).compactMap { item in
            (item["code"] as? String).map { SignIn(code: $0, name: item["name"] as? String ?? "An app",
                                                   returnsTo: item["returns_to"] as? String ?? "the app") }
        }
    }

    private func handle(_ event: [String: Any]) {
        switch event["type"] as? String {
        case "hello":
            devices = (event["devices"] as? [[String: Any]] ?? []).compactMap(Device.init)
            activeCue = cue(event["signal"])
            setMute(event["mute"])
            setRemote(event["remote"])
            Task { await self.loadBackground() }
        case "device":
            guard let raw = event["device"] as? [String: Any], let device = Device(raw) else { return }
            if let index = devices.firstIndex(where: { $0.id == device.id }) { devices[index] = device } else { devices.append(device) }
        case "signal":
            let latest = cue(event["signal"])
            if let latest { remember(latest) }
            if latest?.skipped == nil { activeCue = latest }   // a cue that was not shown changes nothing on screen
        case "mute":
            setMute(event["mute"])
        case "remote":
            setRemote(event["remote"])
        default: break
        }
    }

    /// What the event stream does not carry: earlier cues, and where this Glimt's command lives.
    private func loadBackground() async {
        if let status = await fetch("/v1/signals") {
            for raw in (status["recent"] as? [[String: Any]] ?? []).reversed() { if let old = cue(raw) { remember(old) } }
        }
        if let health = await fetch("/v1/health"), let cli = health["cli"] as? [String], !cli.isEmpty {
            daemonCLI = cli
            if ProcessInfo.processInfo.environment["GLIMT_HOME"] == nil { UserDefaults.standard.set(cli, forKey: "cli") }
        }
        await refreshAgents()
    }

    private func fetch(_ path: String) async -> [String: Any]? {
        guard let request = try? request("GET", path), let (data, _) = try? await session.data(for: request) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    var isMuted: Bool { (muteUntil ?? .distantPast) > Date() }

    /// The panel's tiles, in the order of the config, with sets of plain switches folded.
    var tiles: [Tile] {
        let shown = devices.filter { $0.isShown }
        var tiles: [Tile] = [], done: Set<String> = []
        for device in shown {
            let alike = shown.filter { $0.kind == device.kind && $0.isPlainSwitch }
            if Tile.folded[device.kind] != nil, device.isPlainSwitch, alike.count > 1 {
                if done.insert(device.kind).inserted { tiles.append(.group(kind: device.kind, devices: alike)) }
            } else {
                tiles.append(.device(device))
            }
        }
        return tiles
    }

    // MARK: reading

    func value(_ device: Device, _ key: String) -> Any? {
        if let held = pending[device.id + "." + key], held.until > Date() { return held.value }
        return device.state[key]
    }
    func bool(_ device: Device, _ key: String) -> Bool { value(device, key) as? Bool ?? false }
    func int(_ device: Device, _ key: String) -> Int? { value(device, key) as? Int }
    func string(_ device: Device, _ key: String) -> String? { value(device, key) as? String }

    // MARK: commands

    /// Apply a change. `after` debounces continuous controls (sliders) so a drag sends the
    /// final value rather than every step.
    func set(_ device: Device, _ changes: [String: Any], after delay: Duration = .zero) {
        for (key, value) in changes { pending[device.id + "." + key] = (value, Date().addingTimeInterval(1.5)) }
        objectWillChange.send()
        let path = "/v1/devices/\(device.id)"
        if delay == .zero {
            Task { await send("POST", path, changes) }
            return
        }
        let key = device.id + "." + changes.keys.sorted().joined(separator: ",")
        debounce[key]?.cancel()
        debounce[key] = Task {
            try? await Task.sleep(for: delay)
            if Task.isCancelled { return }
            Task { await self.send("POST", path, changes) }
        }
    }
    func act(_ device: Device, _ action: String) { Task { await send("POST", "/v1/devices/\(device.id)/actions/\(action)", [:]) } }
    /// The test dots always show, even when muted: you asked to see one.
    func signal(_ name: String) { Task { await send("POST", "/v1/signal", ["name": name, "source": "app", "urgent": true]) } }
    func clear() { Task { await send("DELETE", "/v1/signal", nil) } }
    func mute(minutes: Int?) { Task { await send("POST", "/v1/mute", minutes.map { ["minutes": $0] } ?? [:]) } }
    func unmute() { Task { await send("DELETE", "/v1/mute", nil) } }
    /// Let an app in, or turn it away: the same as `glimt remote approve <code>`.
    func answer(_ signIn: SignIn, allow: Bool) {
        Task { await send("POST", "/v1/remote/\(allow ? "approve" : "deny")", ["code": signIn.code]) }
    }
    func disconnect(_ app: RemoteApp) { Task { await send("DELETE", "/v1/remote/apps/\(app.id)", nil) } }

    // MARK: the command line: setup, agents, starting the daemon

    /// The `glimt` command of the daemon we are talking to, or failing that the likeliest install.
    private var cli: [String] {
        let files = FileManager.default
        if let daemonCLI, files.isExecutableFile(atPath: daemonCLI[0]) { return daemonCLI }
        if ProcessInfo.processInfo.environment["GLIMT_HOME"] == nil,
           let saved = UserDefaults.standard.stringArray(forKey: "cli"), let first = saved.first,
           files.isExecutableFile(atPath: first) { return saved }
        let user = files.homeDirectoryForCurrentUser.path
        let likely = [home.appendingPathComponent("venv/bin/glimt").path, user + "/.local/bin/glimt",
                      "/opt/homebrew/bin/glimt", "/usr/local/bin/glimt"]
        if let found = likely.first(where: { files.isExecutableFile(atPath: $0) }) { return [found] }
        return ["/usr/bin/env", "glimt"]
    }

    /// Apps start with a bare PATH. Agents live wherever your shell says, so ask it once.
    private func environment() async -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        if shellPath == nil {
            let user = FileManager.default.homeDirectoryForCurrentUser.path
            let usual = [user + "/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
            let asked = await Shell.run([environment["SHELL"] ?? "/bin/zsh", "-ilc", "printf '\\n__PATH__%s\\n' \"$PATH\""], timeout: 5)
            let found = asked.output.split(separator: "\n").last { $0.hasPrefix("__PATH__") }.map { String($0.dropFirst(8)) }
            shellPath = ([found].compactMap { $0 } + usual).joined(separator: ":")
        }
        environment["PATH"] = shellPath
        return environment
    }

    @discardableResult private func run(_ arguments: [String]) async -> (ok: Bool, output: String) {
        await Shell.run(cli + arguments, environment: await environment())
    }

    func refreshAgents() async {
        let result = await run(["agents", "--json"])
        guard result.ok, let line = result.output.split(separator: "\n").last, let data = line.data(using: .utf8),
              let rows = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return }
        agents = rows.compactMap { row in
            guard let id = row["id"] as? String else { return nil }
            return Agent(id: id, name: row["name"] as? String ?? id, found: row["found"] as? Bool ?? false,
                         hooks: row["hooks"] as? Bool ?? false, tools: row["tools"] as? Bool ?? false)
        }
    }

    /// Connect or disconnect an agent: the same thing as `glimt connect claude`.
    func setAgent(_ agent: Agent, connected: Bool) {
        guard busy == nil else { return }
        busy = agent.id
        hint = nil
        Task {
            let result = await run([connected ? "connect" : "disconnect", agent.id])
            await refreshAgents()
            busy = nil
            if !result.ok { failure = result.output.split(separator: "\n").last.map(String.init) ?? "Could not change \(agent.name)" }
            else if connected { hint = "Restart \(agent.name) to start getting cues." }
        }
    }

    func startDaemon() { Task { await run(["start"]) } }
    func restartDaemon() { Task { await run(["restart"]) } }

    /// Things that ask questions (adding a device shows a QR code) run in Terminal.
    func openInTerminal(_ arguments: [String]) {
        var lines = ["#!/bin/zsh", "clear"]
        if let custom = ProcessInfo.processInfo.environment["GLIMT_HOME"] { lines.append("export GLIMT_HOME=\(quote(custom))") }
        lines.append((cli + arguments).map(quote).joined(separator: " "))
        let script = FileManager.default.temporaryDirectory.appendingPathComponent("Glimt \(arguments.joined(separator: " ")).command")
        do {
            try lines.joined(separator: "\n").write(to: script, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
            NSWorkspace.shared.open(script)
        } catch { failure = "Could not open Terminal" }
    }
    private func quote(_ text: String) -> String { "'" + text.replacingOccurrences(of: "'", with: "'\\''") + "'" }

    private func send(_ method: String, _ path: String, _ body: [String: Any]?) async {
        do {
            let (data, response) = try await session.data(for: try request(method, path, body))
            if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
                let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                failure = json?["error"] as? String ?? "Request failed"
                pending.removeAll()
            } else { failure = nil }
        } catch {
            failure = "Glimt isn't running"
            pending.removeAll()
        }
    }
}
