import Foundation

// The parts of /api/now the app shows. Field names match monitor.py's api_now().
// Anything monitor.py can leave out or send as null is optional here.
struct Now: Decodable {
    let ready: Bool
    let cpu: CPU?
    let mem: Mem?
    let swap: Swap?
    let disk: Disk?
    let net: Net?
    let thermal: Thermal?
    let active_events: [Event]?
    let internet: Internet?
    let limits: Limits?

    struct CPU: Decodable { let busy: Double; let user: Double; let sys: Double; let load: [Double]; let ncpu: Int }
    struct Mem: Decodable { let total: Double; let used: Double; let compressed: Double; let pressure: String? }
    struct Swap: Decodable { let total: Double; let used: Double; let in_bps: Double; let out_bps: Double }
    struct Volume: Decodable { let mount: String; let total: Double; let free: Double; let free_pct: Double }
    struct Disk: Decodable { let volumes: [Volume]; let read_bps: Double; let write_bps: Double }
    struct Iface: Decodable { let in_bps: Double; let out_bps: Double }
    struct Net: Decodable { let in_bps: Double; let out_bps: Double; let ifaces: [String: Iface] }
    struct Thermal: Decodable { let speed_limit: Double?; let state: Int?; let state_name: String? }
    struct Event: Decodable { let id: Int; let ts: Double; let severity: String; let title: String }
    struct Limits: Decodable { let load_warn: Double?; let load_critical: Double? }
    struct Verdict: Decodable { let level: String; let text: String }
    struct Link: Decodable { let iface: String?; let type: String?; let rssi: Double?; let tx_rate: Double? }
    struct Gateway: Decodable { let avg: Double?; let ip: String? }
    struct DNS: Decodable { let ok: Bool?; let ms: Double? }
    struct HTTP: Decodable { let ok: Bool? }
    struct Internet: Decodable {
        let verdict: Verdict
        let link: Link?
        let gateway: Gateway?
        let dns: DNS?
        let http: HTTP?
    }
}

/// Finds a running mac-monitor on 127.0.0.1, or starts one from the repo when
/// none answers. A monitor it did not start is never stopped.
@MainActor
final class Monitor {
    enum Phase { case connecting, starting, live, down }

    static let portRange = 5180...5199          // PORT_RANGE in monitor.py
    static let startAfterSilence: TimeInterval = 10      // nothing has ever answered
    static let restartAfterSilence: TimeInterval = 45    // a monitor we did not start went quiet
    static let retrySpawnAfter: TimeInterval = 30

    private(set) var phase: Phase = .connecting
    private(set) var now: Now?
    private(set) var port: Int?
    private(set) var problem: String?
    var onChange: (() -> Void)?

    private let repoPath: String
    private let ports: [Int]
    private let session: URLSession
    private var busy = false
    private var silentSince = Date()
    private var attachedToOther = false
    private var child: Process?
    private var childStarted: Date?

    init(repoPath: String) {
        self.repoPath = repoPath
        if let forced = ProcessInfo.processInfo.environment["MAC_MONITOR_PORT"].flatMap({ Int($0) }) {
            ports = [forced]
        } else {
            ports = Array(Monitor.portRange)
        }
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 3
        cfg.timeoutIntervalForResource = 4
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        cfg.connectionProxyDictionary = [:]
        cfg.httpMaximumConnectionsPerHost = 1
        session = URLSession(configuration: cfg)
    }

    var dashboardURL: URL? { port.flatMap { URL(string: "http://127.0.0.1:\($0)/") } }
    var startedOwnMonitor: Bool { child?.isRunning == true }

    /// One read. Called by the app's single 5 second timer.
    func tick() {
        guard !busy else { return }
        busy = true
        Task {
            await refresh()
            busy = false
            onChange?()
        }
    }

    private func refresh() async {
        if let p = port, let n = await fetch(p) { accept(n, p); return }
        port = nil
        for p in ports {
            if let n = await fetch(p) { accept(n, p); return }
        }
        // Nothing answers.
        let hadData = now != nil
        now = nil
        let silence = Date().timeIntervalSince(silentSince)
        if let c = child, c.isRunning, let t = childStarted {
            phase = Date().timeIntervalSince(t) < 20 ? .starting : .down
            return
        }
        let wait = attachedToOther ? Monitor.restartAfterSilence : Monitor.startAfterSilence
        let lastTry = childStarted.map { Date().timeIntervalSince($0) } ?? .infinity
        if silence >= wait && lastTry >= Monitor.retrySpawnAfter {
            attachedToOther = false
            phase = spawn() ? .starting : .down
        } else {
            phase = (hadData || attachedToOther || childStarted != nil) ? .down : .connecting
        }
    }

    private func accept(_ n: Now, _ p: Int) {
        if port != p && !(child?.isRunning ?? false) { attachedToOther = true }
        port = p
        now = n
        phase = .live
        problem = nil
        silentSince = Date()
    }

    private func fetch(_ p: Int) async -> Now? {
        guard let url = URL(string: "http://127.0.0.1:\(p)/api/now") else { return nil }
        do {
            let (data, resp) = try await session.data(from: url)
            guard (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
            return try JSONDecoder().decode(Now.self, from: data)
        } catch {
            return nil
        }
    }

    /// Starts `python3 monitor.py serve` from the repo, the same way ./run does.
    private func spawn() -> Bool {
        childStarted = Date()
        let script = (repoPath as NSString).appendingPathComponent("monitor.py")
        guard FileManager.default.fileExists(atPath: script) else {
            problem = "monitor.py was not found in \(repoPath). Run app/install again from the mac-monitor folder."
            return false
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["python3", "monitor.py", "serve"]
        p.currentDirectoryURL = URL(fileURLWithPath: repoPath)
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
        } catch {
            problem = "The monitor could not be started: \(error.localizedDescription)"
            return false
        }
        child = p
        problem = nil
        return true
    }

    /// Stops the monitor this app started, if any. Ctrl-C first, as in a terminal.
    func stopChild() {
        guard let c = child, c.isRunning else { return }
        c.interrupt()
        let deadline = Date().addingTimeInterval(3)
        while c.isRunning && Date() < deadline { usleep(50_000) }
        if c.isRunning {
            c.terminate()
            let hard = Date().addingTimeInterval(2)
            while c.isRunning && Date() < hard { usleep(50_000) }
        }
        child = nil
    }
}

// Same wording as the dashboard's helpers.
func fmtBytes(_ value: Double, digits: Int? = nil) -> String {
    let units = ["B", "KB", "MB", "GB", "TB"]
    var n = value
    var i = 0
    while abs(n) >= 1024 && i < units.count - 1 { n /= 1024; i += 1 }
    let d = digits ?? (i < 2 ? 0 : (n >= 100 ? 0 : 1))
    return String(format: "%.\(d)f %@", n, units[i])
}

func fmtRate(_ value: Double) -> String { fmtBytes(value) + "/s" }

func fmtPct(_ n: Double) -> String {
    String(format: (n >= 99.95 || n >= 10) ? "%.0f%%" : "%.1f%%", n)
}

func fmtDur(_ seconds: Double) -> String {
    let s = Int(max(0, seconds.rounded()))
    let d = s / 86400, h = s % 86400 / 3600, m = s % 3600 / 60
    if d > 0 { return "\(d)d \(h)h" }
    if h > 0 { return "\(h)h \(m)m" }
    if m > 0 { return "\(m)m \(s % 60)s" }
    return "\(s)s"
}
