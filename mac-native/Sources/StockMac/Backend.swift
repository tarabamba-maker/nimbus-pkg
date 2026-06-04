import Foundation

/// Spawns and tears down the Python Flask backend (main.py), mirroring what the
/// Tauri Rust layer does: cwd + STOCK_DATA_DIR = ~/Library/Application Support/
/// StockAutomation, env passed through.
@MainActor
final class Backend {
    static let shared = Backend()
    private var process: Process?

    var dataDir: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        return base.appendingPathComponent("StockAutomation", isDirectory: true)
    }

    /// Locate main.py: STOCK_MAIN_PY env → next to the .app → known repo path.
    private func findMainPy() -> URL? {
        if let p = ProcessInfo.processInfo.environment["STOCK_MAIN_PY"] {
            let u = URL(fileURLWithPath: p)
            if FileManager.default.fileExists(atPath: u.path) { return u }
        }
        // Bundled next to the executable (Resources) for a packaged .app.
        let exeDir = Bundle.main.bundleURL.deletingLastPathComponent()
        for rel in ["main.py", "../Resources/main.py", "Contents/Resources/main.py"] {
            let u = exeDir.appendingPathComponent(rel).standardizedFileURL
            if FileManager.default.fileExists(atPath: u.path) { return u }
        }
        // Dev fallback: the repo checkout.
        let repo = URL(fileURLWithPath: "/Users/admin/Desktop/Stock_Automation/main.py")
        if FileManager.default.fileExists(atPath: repo.path) { return repo }
        return nil
    }

    private func findPython() -> String? {
        let candidates = [
            "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ]
        for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
            if pythonHasFlask(c) { return c }
        }
        return nil
    }

    private func pythonHasFlask(_ python: String) -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-c", "import flask, PIL, bs4"]
        p.standardOutput = nil; p.standardError = nil
        do { try p.run(); p.waitUntilExit(); return p.terminationStatus == 0 }
        catch { return false }
    }

    func start() {
        // Kill any stale instance first.
        kill(pattern: "main.py")
        try? FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)

        guard let main = findMainPy() else {
            NSLog("[Backend] main.py not found"); return
        }
        guard let python = findPython() else {
            NSLog("[Backend] python3 with flask/PIL/bs4 not found"); return
        }

        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [main.path]
        p.currentDirectoryURL = dataDir
        var env = ProcessInfo.processInfo.environment
        env["STOCK_DATA_DIR"] = dataDir.path
        p.environment = env

        let log = dataDir.appendingPathComponent("mac_python.log")
        FileManager.default.createFile(atPath: log.path, contents: nil)
        if let fh = try? FileHandle(forWritingTo: log) {
            p.standardOutput = fh; p.standardError = fh
        }
        do {
            try p.run()
            process = p
            NSLog("[Backend] spawned python pid=\(p.processIdentifier)")
        } catch {
            NSLog("[Backend] failed to spawn: \(error)")
        }
    }

    func stop() {
        process?.terminate()
        process = nil
        kill(pattern: "main.py")
    }

    private func kill(pattern: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        p.arguments = ["-f", pattern]
        try? p.run(); p.waitUntilExit()
    }
}
