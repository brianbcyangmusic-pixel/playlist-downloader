// Playlist Downloader — macOS menu bar controller.
//
// Lives in the menu bar (no Dock icon) and owns the local Python backend:
//   * starts app.server on launch and stops it on quit
//   * shows live status: backend, yt-dlp/FFmpeg, queue, app updates
//   * opens the local web UI in the default browser
//
// Build with scripts/build_macos_app.sh (swiftc). No Xcode project required.

import Cocoa

let PREFERRED_PORT = 8765

final class AppDelegate: NSObject, NSApplicationDelegate {

    // MARK: - State

    private var statusItem: NSStatusItem!
    private let menu = NSMenu()

    private var server: Process?
    private var ownsServer = false
    private var port = PREFERRED_PORT
    private var starting = false

    private var appState: [String: Any] = [:]
    private var updateState: [String: Any] = [:]
    private var lastUpdateCheck = Date.distantPast

    private var pollTimer: Timer?
    private var logHandle: FileHandle?

    // Menu rows that change while the app runs.
    private let headerItem = NSMenuItem(title: "Playlist Downloader", action: nil, keyEquivalent: "")
    private let backendItem = NSMenuItem(title: "Backend: starting…", action: nil, keyEquivalent: "")
    private let queueItem = NSMenuItem(title: "Queue: —", action: nil, keyEquivalent: "")
    private let toolsItem = NSMenuItem(title: "Tools: checking…", action: nil, keyEquivalent: "")
    private let updateItem = NSMenuItem(title: "Updates: checking…", action: nil, keyEquivalent: "")
    private let openItem = NSMenuItem(title: "Open web page", action: #selector(openWeb), keyEquivalent: "o")
    private let toggleItem = NSMenuItem(title: "Stop backend", action: #selector(toggleBackend), keyEquivalent: "")

    private var baseURL: URL { URL(string: "http://127.0.0.1:\(port)")! }

    // MARK: - Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            if let image = NSImage(systemSymbolName: "arrow.down.circle",
                                   accessibilityDescription: "Playlist Downloader") {
                image.isTemplate = true
                button.image = image
            } else {
                button.title = "⬇"
            }
            button.toolTip = "Playlist Downloader"
        }

        buildMenu()
        statusItem.menu = menu

        startBackend()

        let timer = Timer(timeInterval: 5, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        RunLoop.main.add(timer, forMode: .common)
        pollTimer = timer
    }

    func applicationWillTerminate(_ notification: Notification) {
        pollTimer?.invalidate()
        stopBackend(blocking: true)
        try? logHandle?.close()
    }

    // MARK: - Menu

    private func buildMenu() {
        for item in [headerItem, backendItem, queueItem, toolsItem, updateItem] {
            item.isEnabled = false
            menu.addItem(item)
        }
        menu.addItem(.separator())

        openItem.target = self
        openItem.isEnabled = false
        menu.addItem(openItem)

        toggleItem.target = self
        menu.addItem(toggleItem)

        let dataItem = NSMenuItem(title: "Open data folder", action: #selector(openDataFolder), keyEquivalent: "")
        dataItem.target = self
        menu.addItem(dataItem)

        let updatesAction = NSMenuItem(title: "Check for updates now", action: #selector(checkUpdatesNow), keyEquivalent: "")
        updatesAction.target = self
        menu.addItem(updatesAction)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit Playlist Downloader", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
    }

    private func setStatusLine(_ text: String) {
        backendItem.title = text
        statusItem.button?.toolTip = "Playlist Downloader — \(text)"
    }

    // MARK: - Backend process

    @objc private func startBackend() {
        guard server == nil, !starting else { return }
        starting = true
        setStatusLine("Backend: starting…")

        // A previous launch (or the packaged app) may already serve the port.
        if ping(port: port) {
            ownsServer = false
            starting = false
            setStatusLine("Backend: running (attached)")
            openItem.isEnabled = true
            toggleItem.title = "Stop backend"
            refresh()
            return
        }

        guard let launch = backendLaunchCommand() else {
            starting = false
            setStatusLine("Backend: not installed")
            toggleItem.title = "Show setup instructions"
            toggleItem.action = #selector(showSetup)
            return
        }

        port = freePort(startingAt: PREFERRED_PORT)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: launch.executable)
        process.arguments = launch.arguments
        process.currentDirectoryURL = launch.directory
        process.environment = launch.environment

        openLog()
        if let handle = logHandle {
            process.standardOutput = handle
            process.standardError = handle
        }

        process.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self else { return }
                if self.ownsServer && self.server === proc {
                    self.server = nil
                    self.ownsServer = false
                    self.setStatusLine("Backend: stopped")
                    self.openItem.isEnabled = false
                    self.toggleItem.title = "Start backend"
                    self.toggleItem.action = #selector(self.startBackend)
                }
            }
        }

        do {
            try process.run()
        } catch {
            starting = false
            setStatusLine("Backend: failed to launch")
            appendLog("launch failed: \(error.localizedDescription)\n")
            return
        }

        server = process
        ownsServer = true
        toggleItem.title = "Stop backend"
        toggleItem.action = #selector(toggleBackend)

        waitUntilReady(attempts: 100)
    }

    private struct Launch {
        let executable: String
        let arguments: [String]
        let directory: URL
        let environment: [String: String]
    }

    private func resources() -> URL {
        if let override = ProcessInfo.processInfo.environment["PLAYLIST_DOWNLOADER_RES"] {
            return URL(fileURLWithPath: override)
        }
        return Bundle.main.resourceURL ?? URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    }

    /// Walk up from Resources until the repo root is found (dev builds).
    private func repoRoot(from url: URL) -> URL? {
        var dir = url
        for _ in 0..<5 {
            if FileManager.default.fileExists(atPath: dir.appendingPathComponent("app/server.py").path) {
                return dir
            }
            let parent = dir.deletingLastPathComponent()
            if parent.path == dir.path { break }
            dir = parent
        }
        return nil
    }

    /// Prefer a bundled PyInstaller backend; otherwise the bundled/system Python.
    private func backendLaunchCommand() -> Launch? {
        let fm = FileManager.default
        let res = resources()

        let bundled = res.appendingPathComponent("backend/PlaylistDownloaderBackend")
        if fm.isExecutableFile(atPath: bundled.path) {
            return Launch(executable: bundled.path,
                          arguments: ["--port", String(port), "--no-browser"],
                          directory: res,
                          environment: baseEnvironment(res))
        }

        let bundledPython = res.appendingPathComponent("py/bin/python3")
        if fm.isExecutableFile(atPath: bundledPython.path) {
            return Launch(executable: bundledPython.path,
                          arguments: ["-m", "app.server", "--port", String(port), "--no-browser"],
                          directory: res,
                          environment: baseEnvironment(res))
        }

        // Development builds: use the repo virtualenv or a system Python that
        // already has the requirements installed.
        var candidates: [String] = []
        if let python = ProcessInfo.processInfo.environment["PLAYLIST_DOWNLOADER_PYTHON"] {
            candidates.append(python)
        }
        if let repo = repoRoot(from: res) {
            candidates.append(repo.appendingPathComponent(".venv/bin/python").path)
        }
        candidates.append(contentsOf: [
            res.appendingPathComponent(".venv/bin/python").path,
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3"
        ])
        for path in candidates where fm.isExecutableFile(atPath: path) {
            return Launch(executable: path,
                          arguments: ["-m", "app.server", "--port", String(port), "--no-browser"],
                          directory: res,
                          environment: baseEnvironment(res))
        }
        return nil
    }

    private func baseEnvironment(_ res: URL) -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let path = "/opt/homebrew/bin:/usr/local/bin:\(NSHomeDirectory())/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PATH"] = path
        env["PYTHONPATH"] = res.path
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PLAYLIST_DOWNLOADER_PROJECT"] = res.path
        return env
    }

    private func waitUntilReady(attempts: Int) {
        guard attempts > 0 else {
            starting = false
            setStatusLine("Backend: did not start")
            toggleItem.title = "Stop backend"
            toggleItem.action = #selector(toggleBackend)
            showAlert(title: "The backend did not start",
                      text: "Check \(launcherLog().path) for details.")
            return
        }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 0.3) { [weak self] in
            guard let self else { return }
            if self.ping(port: self.port) {
                DispatchQueue.main.async {
                    self.starting = false
                    self.setStatusLine("Backend: running")
                    self.openItem.isEnabled = true
                    self.refresh()
                }
            } else {
                self.waitUntilReady(attempts: attempts - 1)
            }
        }
    }

    private func stopBackend(blocking: Bool) {
        guard ownsServer else { return }
        // Ask nicely first: this lets the server close the DB cleanly.
        var request = URLRequest(url: baseURL.appendingPathComponent("api/shutdown"))
        request.httpMethod = "POST"
        request.timeoutInterval = 2
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { _, _, _ in done.signal() }.resume()
        if blocking { _ = done.wait(timeout: .now() + 2.5) } else { done.wait() }

        if let process = server, process.isRunning {
            if blocking {
                let deadline = Date().addingTimeInterval(3)
                while process.isRunning && Date() < deadline { usleep(100_000) }
                if process.isRunning { process.terminate() }
            } else {
                process.terminate()
            }
        }
        server = nil
        ownsServer = false
    }

    @objc private func toggleBackend() {
        if ownsServer {
            stopBackend(blocking: false)
            setStatusLine("Backend: stopping…")
            openItem.isEnabled = false
            toggleItem.title = "Start backend"
            toggleItem.action = #selector(startBackend)
        } else {
            startBackend()
        }
    }

    // MARK: - API polling

    private func refresh() {
        getJSON(path: "api/state") { [weak self] json in
            guard let self, let json else { return }
            self.appState = json
            self.render(from: json)
            if Date().timeIntervalSince(self.lastUpdateCheck) > 300 {
                self.lastUpdateCheck = Date()
                self.getJSON(path: "api/update/check") { info in
                    guard let info else { return }
                    self.updateState = info
                    self.renderUpdates(info)
                }
            } else {
                self.renderUpdates(self.updateState)
            }
        }
    }

    private func render(from json: [String: Any]) {
        if let version = json["version"] as? String {
            headerItem.title = "Playlist Downloader v\(version)"
        }
        setStatusLine("Backend: running · port \(port)")

        let run = json["run"] as? [String: Any] ?? [:]
        let stats = json["stats"] as? [String: Any] ?? [:]
        let running = (run["running"] as? Bool) ?? false
        let queued = (run["queued"] as? Int) ?? 0
        let done = (stats["done"] as? Int) ?? 0
        let failed = (stats["failed"] as? Int) ?? 0
        let pending = (stats["pending"] as? Int) ?? 0
        queueItem.title = "\(running ? "Downloading" : "Idle") · \(done) done · \(pending) pending · \(failed) failed" +
            (queued > 0 ? " · \(queued) queued" : "")

        let tools = json["tools"] as? [String: Any] ?? [:]
        let yt = (tools["ytdlp_ok"] as? Bool) ?? false
        let ff = (tools["ffmpeg_ok"] as? Bool) ?? false
        let js = (tools["js_ok"] as? Bool) ?? false
        var toolBits: [String] = []
        toolBits.append("yt-dlp \(yt ? "OK" : "MISSING")")
        toolBits.append("FFmpeg \(ff ? "OK" : "MISSING")")
        if !js { toolBits.append("JS runtime missing") }
        toolsItem.title = "Tools: " + toolBits.joined(separator: " · ")
        toolsItem.isEnabled = !(yt && ff)

        if let auth = json["auth"] as? [String: Any], (auth["enabled"] as? Bool) == true {
            let who = (auth["signed_in"] as? Bool) == true
                ? ((auth["user"] as? [String: Any])?["email"] as? String ?? "signed in")
                : "not signed in"
            toolsItem.title += " · \(who)"
        }
    }

    private func renderUpdates(_ info: [String: Any]) {
        let current = info["current"] as? String ?? (appState["version"] as? String ?? "?")
        if (info["available"] as? Bool) == true {
            let latest = info["latest"] as? String ?? "?"
            updateItem.title = "Update available: v\(latest) (you have v\(current))"
            updateItem.isEnabled = true
            updateItem.action = #selector(openReleasePage)
            updateItem.target = self
        } else {
            updateItem.title = "Updates: v\(current) is up to date"
            updateItem.isEnabled = false
        }
    }

    @objc private func checkUpdatesNow() {
        lastUpdateCheck = Date()
        getJSON(path: "api/update/check?force=1") { [weak self] info in
            guard let self, let info else {
                self?.showAlert(title: "Could not check for updates",
                                text: "The backend did not answer the update check.")
                return
            }
            self.updateState = info
            self.renderUpdates(info)
            if (info["available"] as? Bool) == true {
                self.showAlert(title: "Update available",
                               text: "Version \(info["latest"] as? String ?? "?") is available. Open the web page and use Update & restart.")
            } else if let current = info["current"] as? String {
                self.showAlert(title: "You're up to date", text: "This build is v\(current).")
            }
        }
    }

    // MARK: - Actions

    @objc private func openWeb() {
        guard ping(port: port) else {
            showAlert(title: "Backend is not running", text: "Start it from the menu first.")
            return
        }
        NSWorkspace.shared.open(baseURL)
    }

    @objc private func openDataFolder() {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/PlaylistDownloader")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(dir)
    }

    @objc private func openReleasePage() {
        // Keep this in sync with REPO_DEFAULT in app/updater.py.
        if let url = URL(string: "https://github.com/brianbcyangmusic-pixel/playlist-downloader/releases/latest") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func showSetup() {
        let res = resources().path
        let commands = """
        cd "\(res)"
        python3 -m venv .venv
        .venv/bin/python -m pip install -r requirements.txt
        """
        let alert = NSAlert()
        alert.messageText = "Backend not installed"
        alert.informativeText = "No usable Python backend was found in this app.\n\nRun these commands in Terminal, then relaunch:\n\n\(commands)"
        alert.addButton(withTitle: "Copy commands")
        alert.addButton(withTitle: "Close")
        if alert.runModal() == .alertFirstButtonReturn {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(commands, forType: .string)
        }
    }

    @objc private func quit() {
        stopBackend(blocking: true)
        NSApp.terminate(nil)
    }

    // MARK: - Helpers

    private func ping(port: Int) -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/ping") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.0
        var ok = false
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { _, response, _ in
            if let http = response as? HTTPURLResponse, http.statusCode == 200 { ok = true }
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 1.5)
        return ok
    }

    private func getJSON(path: String, completion: @escaping ([String: Any]?) -> Void) {
        guard let url = URL(string: "http://127.0.0.1:\(port)/\(path)") else {
            completion(nil)
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 4
        URLSession.shared.dataTask(with: request) { data, _, _ in
            guard let data,
                  let object = try? JSONSerialization.jsonObject(with: data),
                  let json = object as? [String: Any] else {
                DispatchQueue.main.async { completion(nil) }
                return
            }
            DispatchQueue.main.async { completion(json) }
        }.resume()
    }

    private func freePort(startingAt preferred: Int) -> Int {
        for candidate in (preferred...(preferred + 20)) {
            let fd = socket(AF_INET, SOCK_STREAM, 0)
            if fd < 0 { continue }
            defer { close(fd) }
            var yes: Int32 = 1
            setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
            var addr = sockaddr_in()
            addr.sin_family = sa_family_t(AF_INET)
            addr.sin_port = in_port_t(UInt16(candidate).bigEndian)
            addr.sin_addr.s_addr = inet_addr("127.0.0.1")
            let bound = withUnsafePointer(to: &addr) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
            if bound == 0 { return candidate }
        }
        return preferred
    }

    private func launcherLog() -> URL {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/PlaylistDownloader")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("launcher.log")
    }

    private func openLog() {
        guard logHandle == nil else { return }
        let url = launcherLog()
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        logHandle = try? FileHandle(forWritingTo: url)
        logHandle?.seekToEndOfFile()
        appendLog("\n=== launcher start \(Date()) ===\n")
    }

    private func appendLog(_ text: String) {
        if let data = text.data(using: .utf8) {
            logHandle?.write(data)
        }
    }

    private func showAlert(title: String, text: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.addButton(withTitle: "OK")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)  // menu bar only, no Dock icon
app.run()
