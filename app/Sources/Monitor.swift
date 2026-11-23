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
    // Failed reads in a row (one every 5 s) before the app starts its own monitor.
    // Counted in reads, not clock time, so a Mac waking from sleep is one miss.
    static let missesBeforeStart = 3        // nothing has ever answered: about 10 s
    static let missesBeforeRestart = 10     // a monitor we did not start went quiet: about 45 s
    static let retrySpawnAfter: TimeInterval = 30

    private enum Probe {
        case ok(Now)
        case free       // nothing is listening on the port
        case absent     // something else answers there, not a monitor
        case unclear    // something is there but slow or failing: maybe a busy monitor
    }

    private(set) var phase: Phase = .connecting
    private(set) var now: Now?
    private(set) var port: Int?
    private(set) var problem: String?
    var onChange: (() -> Void)?

    private let repoPath: String
    private let ports: [Int]
    private let session: URLSession
    private var busy = false
    private var misses = 0
    private var everLive = false
    private var attachedToOther = false
    private var child: Process?
    private var childPort: Int?
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
        let ownRunning = child?.isRunning == true
        // While our own monitor runs, only its port is read.
        var scan = ports
        if ownRunning, let cp = childPort { scan = [cp] }
        if let p = port, let i = scan.firstIndex(of: p) { scan.swapAt(0, i) }
        var unclear = false
        var firstFree: Int?
        for p in scan {
            switch await fetch(p) {
            case .ok(let n): accept(n, p, own: ownRunning); return
            case .unclear: unclear = true
            case .free: if firstFree == nil { firstFree = p }
            case .absent: break
            }
        }
        // Nothing gave a reading.
        port = nil
        now = nil
        let waiting: Phase = (everLive || childStarted != nil) ? .down : .connecting
        if ownRunning, let t = childStarted {
            phase = Date().timeIntervalSince(t) < 20 ? .starting : .down
            return
        }
        if unclear {
            // Something holds a port but is slow or failing. It may be a busy
            // monitor, so never start a second one beside it.
            misses = 0
            phase = waiting
            return
        }
        misses += 1
        let need = attachedToOther ? Monitor.missesBeforeRestart : Monitor.missesBeforeStart
        let lastTry = childStarted.map { Date().timeIntervalSince($0) } ?? .infinity
        if misses >= need && lastTry >= Monitor.retrySpawnAfter, let free = firstFree {
            attachedToOther = false
            phase = spawn(on: free) ? .starting : .down
        } else {
            phase = waiting
        }
    }

    private func accept(_ n: Now, _ p: Int, own: Bool) {
        attachedToOther = !own
        port = p
        now = n
        phase = .live
        problem = nil
        misses = 0
        everLive = true
    }

    private func fetch(_ p: Int) async -> Probe {
        guard let url = URL(string: "http://127.0.0.1:\(p)/api/now") else { return .absent }
        do {
            let (data, resp) = try await session.data(from: url)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            if code == 200 {
                if let n = try? JSONDecoder().decode(Now.self, from: data) { return .ok(n) }
                return .unclear
            }
            return code >= 500 ? .unclear : .absent
        } catch let e as URLError where e.code == .cannotConnectToHost {
            return .free
        } catch {
            return .unclear
        }
    }

    /// Starts `python3 monitor.py serve` from the repo, as ./run does, on a port
    /// the app has just seen free. The port is fixed so that a monitor that is
    /// only slow to answer keeps it and this one gives up, instead of two
    /// monitors writing one history.
    private func spawn(on port: Int) -> Bool {
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
        var env = ProcessInfo.processInfo.environment
        env["MAC_MONITOR_PORT"] = String(port)
        p.environment = env
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
        childPort = port
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
