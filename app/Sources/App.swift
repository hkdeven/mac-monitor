import AppKit

// Mac Monitor: a menu bar view of the mac-monitor dashboard.
// Hidden arguments for testing: --show-panel (open the panel at launch and keep
// it open), --appearance light|dark (this app only, not the system setting).

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    private var monitor: Monitor!
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let popover = NSPopover()
    private let panel = PanelController()
    private var timer: Timer?
    private var signals: [DispatchSourceSignal] = []
    private var dots: [NSColor: NSImage] = [:]

    func applicationDidFinishLaunching(_ note: Notification) {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--appearance"), i + 1 < args.count {
            let look = NSAppearance(named: args[i + 1] == "dark" ? .darkAqua : .aqua)
            NSApp.appearance = look
            popover.appearance = look
        }
        let keepOpen = args.contains("--show-panel")

        let repo = Bundle.main.object(forInfoDictionaryKey: "MacMonitorRepoPath") as? String ?? ""
        monitor = Monitor(repoPath: repo)
        monitor.onChange = { [weak self] in self?.refreshViews() }

        if let button = statusItem.button {
            button.font = .monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
            button.imagePosition = .imageLeading
            button.target = self
            button.action = #selector(togglePanel)
        }
        popover.contentViewController = panel
        popover.behavior = keepOpen ? .applicationDefined : .transient
        popover.animates = false
        popover.delegate = self
        panel.onOpenDashboard = { [weak self] in self?.openDashboard() }
        panel.onQuit = { NSApp.terminate(nil) }

        // Quit cleanly (and stop a monitor we started) when asked to stop from outside.
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler { NSApp.terminate(nil) }
            src.resume()
            signals.append(src)
        }

        refreshViews()
        let t = Timer(timeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.monitor.tick() }
        }
        t.tolerance = 0.5
        RunLoop.main.add(t, forMode: .common)
        timer = t
        monitor.tick()

        if keepOpen {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.togglePanel() }
        }
    }

    func applicationWillTerminate(_ note: Notification) {
        timer?.invalidate()
        monitor.stopChild()
    }

    @objc private func togglePanel() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        panel.render(monitor)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func openDashboard() {
        guard let url = monitor.dashboardURL else { return }
        popover.performClose(nil)
        NSWorkspace.shared.open(url)
    }

    /// The menu bar item is always kept current; the panel only while it is open.
    private func refreshViews() {
        let s = monitor.now
        var level = Level.unknown
        var text = "\u{2026}"
        var tip = "Mac Monitor: connecting"
        switch monitor.phase {
        case .connecting, .starting:
            break
        case .down:
            level = .critical
            text = "--"
            tip = "Mac Monitor: the monitor is not responding"
        case .live:
            let open = s?.active_events ?? []
            if open.contains(where: { $0.severity == "critical" }) { level = .critical }
            else if open.contains(where: { $0.severity == "warning" }) { level = .warning }
            else { level = .good }
            if let busy = s?.cpu?.busy {
                text = String(format: "%.0f%%", busy)
                let n = open.count
                tip = "Mac Monitor: CPU \(text), " + (n == 0 ? "no open events" : n == 1 ? "1 open event" : "\(n) open events")
            }
        }
        if let button = statusItem.button {
            button.image = dot(level.color)
            button.title = text
            button.toolTip = tip
            button.setAccessibilityLabel(tip)
        }
        if popover.isShown { panel.render(monitor) }
    }

    private func dot(_ color: NSColor) -> NSImage {
        if let img = dots[color] { return img }
        let img = NSImage(size: NSSize(width: 12, height: 14), flipped: false) { rect in
            color.setFill()
            NSBezierPath(ovalIn: NSRect(x: 1, y: (rect.height - 9) / 2, width: 9, height: 9)).fill()
            return true
        }
        img.isTemplate = false
        dots[color] = img
        return img
    }
}

@main
enum MacMonitorApp {
    @MainActor static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}
