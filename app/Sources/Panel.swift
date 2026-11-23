import AppKit

enum Level {
    case good, warning, critical, unknown

    var color: NSColor {
        switch self {
        case .good: return .systemGreen
        case .warning: return .systemOrange
        case .critical: return .systemRed
        case .unknown: return .systemGray
        }
    }

    init(severity: String) {
        switch severity {
        case "critical": self = .critical
        case "warning": self = .warning
        case "good": self = .good
        default: self = .unknown
        }
    }
}

private let panelWidth: CGFloat = 320
private let panelInset: CGFloat = 14
private let textWidth = panelWidth - 2 * panelInset

private func makeLabel(_ size: CGFloat, _ weight: NSFont.Weight = .regular, _ color: NSColor = .labelColor, wraps: Bool = false) -> NSTextField {
    let f = wraps ? NSTextField(wrappingLabelWithString: "") : NSTextField(labelWithString: "")
    f.font = .monospacedDigitSystemFont(ofSize: size, weight: weight)
    f.textColor = color
    f.isSelectable = false
    if wraps {
        f.preferredMaxLayoutWidth = textWidth
    } else {
        f.lineBreakMode = .byTruncatingTail
    }
    return f
}

/// "● text" with the dot in the level's colour, or plain text when there is no level.
private func dotted(_ text: String, _ level: Level?, size: CGFloat = 13) -> NSAttributedString {
    let font = NSFont.monospacedDigitSystemFont(ofSize: size, weight: .semibold)
    let out = NSMutableAttributedString()
    if let level = level {
        out.append(NSAttributedString(string: "\u{25CF} ", attributes: [.foregroundColor: level.color, .font: NSFont.systemFont(ofSize: size - 3)]))
    }
    out.append(NSAttributedString(string: text, attributes: [.foregroundColor: NSColor.labelColor, .font: font]))
    return out
}

/// One tile: name on the left, value on the right, a detail line under them.
private final class TileRow: NSStackView {
    private let key = makeLabel(12, .regular, .secondaryLabelColor)
    private let value = makeLabel(13, .semibold)
    private let sub = makeLabel(11, .regular, .secondaryLabelColor, wraps: true)

    init(_ name: String) {
        super.init(frame: .zero)
        orientation = .vertical
        alignment = .leading
        spacing = 1
        key.stringValue = name
        key.setContentHuggingPriority(.required, for: .horizontal)
        key.setContentCompressionResistancePriority(.required, for: .horizontal)
        value.alignment = .right
        value.setContentHuggingPriority(.defaultLow, for: .horizontal)
        value.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let top = NSStackView(views: [key, value])
        top.orientation = .horizontal
        top.distribution = .fill
        top.spacing = 8
        addArrangedSubview(top)
        addArrangedSubview(sub)
        top.widthAnchor.constraint(equalTo: widthAnchor).isActive = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func set(_ text: String, level: Level? = nil, sub detail: String) {
        let styled = NSMutableAttributedString(attributedString: dotted(text, level))
        let right = NSMutableParagraphStyle()
        right.alignment = .right
        right.lineBreakMode = .byTruncatingTail
        styled.addAttribute(.paragraphStyle, value: right, range: NSRange(location: 0, length: styled.length))
        value.attributedStringValue = styled
        value.toolTip = text
        sub.stringValue = detail
    }
}

@MainActor
final class PanelController: NSViewController {
    var onOpenDashboard: (() -> Void)?
    var onQuit: (() -> Void)?

    private let root = NSStackView()
    private let state = makeLabel(11, .regular, .secondaryLabelColor)
    private let spinner = NSProgressIndicator()
    private let message = makeLabel(13, .regular, .labelColor, wraps: true)
    private let waiting = NSStackView()
    private let tiles = NSStackView()
    private let events = NSStackView()
    private let eventsTitle = makeLabel(12, .regular, .secondaryLabelColor)
    private let openButton = NSButton(title: "Open dashboard", target: nil, action: nil)
    private let quitButton = NSButton(title: "Quit", target: nil, action: nil)

    private let cpu = TileRow("CPU")
    private let mem = TileRow("Memory pressure")
    private let swap = TileRow("Swap used")
    private let disk = TileRow("Disk free")
    private let net = TileRow("Network")
    private let inet = TileRow("Internet")
    private let therm = TileRow("Thermal")

    override func loadView() {
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 10
        root.edgeInsets = NSEdgeInsets(top: panelInset, left: panelInset, bottom: panelInset, right: panelInset)
        root.translatesAutoresizingMaskIntoConstraints = false

        let title = makeLabel(13, .semibold)
        title.stringValue = "Mac Monitor"
        title.setContentHuggingPriority(.required, for: .horizontal)
        state.alignment = .right
        state.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let head = NSStackView(views: [title, state])
        head.orientation = .horizontal
        head.distribution = .fill

        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false
        message.preferredMaxLayoutWidth = textWidth - 24
        waiting.orientation = .horizontal
        waiting.spacing = 8
        waiting.alignment = .centerY
        waiting.addArrangedSubview(spinner)
        waiting.addArrangedSubview(message)

        tiles.orientation = .vertical
        tiles.alignment = .leading
        tiles.spacing = 9
        for row in [cpu, mem, swap, disk, net, inet, therm] { tiles.addArrangedSubview(row) }

        eventsTitle.stringValue = "Open events"
        events.orientation = .vertical
        events.alignment = .leading
        events.spacing = 4

        openButton.target = self
        openButton.action = #selector(openDashboard)
        openButton.bezelStyle = .rounded
        openButton.keyEquivalent = "\r"
        quitButton.target = self
        quitButton.action = #selector(quit)
        quitButton.bezelStyle = .rounded
        let gap = NSView()
        gap.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let buttons = NSStackView(views: [openButton, gap, quitButton])
        buttons.orientation = .horizontal
        buttons.distribution = .fill

        let line1 = NSBox(), line2 = NSBox(), line3 = NSBox()
        for b in [line1, line2, line3] { b.boxType = .separator }
        let all: [NSView] = [head, line1, waiting, tiles, line2, eventsTitle, events, line3, buttons]
        for v in all {
            root.addArrangedSubview(v)
            v.widthAnchor.constraint(equalToConstant: textWidth).isActive = true
        }
        for row in tiles.arrangedSubviews { row.widthAnchor.constraint(equalToConstant: textWidth).isActive = true }
        root.setCustomSpacing(4, after: eventsTitle)
        self.separatorBeforeEvents = line2

        let box = NSView()
        box.addSubview(root)
        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: box.topAnchor),
            root.bottomAnchor.constraint(equalTo: box.bottomAnchor),
            root.leadingAnchor.constraint(equalTo: box.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: box.trailingAnchor),
            root.widthAnchor.constraint(equalToConstant: panelWidth),
        ])
        view = box
    }

    private var separatorBeforeEvents: NSView?

    @objc private func openDashboard() { onOpenDashboard?() }
    @objc private func quit() { onQuit?() }

    /// Redraws the panel from the monitor's latest read. Only called while the panel is open.
    func render(_ m: Monitor) {
        _ = view
        let s = m.now
        let live = m.phase == .live && s?.ready == true
        openButton.isEnabled = m.dashboardURL != nil

        switch m.phase {
        case .live:
            state.stringValue = "Live on port \(m.port ?? 0)"
            message.stringValue = "Waiting for the first reading\u{2026}"
        case .connecting:
            state.stringValue = ""
            message.stringValue = "Connecting\u{2026}"
        case .starting:
            state.stringValue = ""
            message.stringValue = "Starting the monitor\u{2026}"
        case .down:
            state.stringValue = "Not responding"
            message.stringValue = m.problem ?? "The monitor is not responding. Trying again every 5 seconds."
        }
        waiting.isHidden = live
        let spinning = !live && m.phase != .down
        spinner.isHidden = !spinning
        if spinning { spinner.startAnimation(nil) } else { spinner.stopAnimation(nil) }
        for v in [tiles, eventsTitle, events, separatorBeforeEvents] as [NSView?] { v?.isHidden = !live }

        if live, let s = s { fill(s) }
        view.layoutSubtreeIfNeeded()
        preferredContentSize = NSSize(width: panelWidth, height: root.fittingSize.height)
    }

    private func fill(_ s: Now) {
        if let c = s.cpu {
            let l1 = c.load.first ?? 0
            var level: Level?
            var text = fmtPct(c.busy)
            if let crit = s.limits?.load_critical, l1 > crit { level = .critical; text += "  Load critical" }
            else if let warn = s.limits?.load_warn, l1 > warn { level = .warning; text += "  Load high" }
            cpu.set(text, level: level,
                    sub: String(format: "load %.1f of %d cores \u{00B7} user %.0f%% \u{00B7} sys %.0f%%", l1, c.ncpu, c.user, c.sys))
        }
        if let m = s.mem {
            let names = ["normal": ("Normal", Level.good), "warn": ("Warn", Level.warning), "critical": ("Critical", Level.critical)]
            let (name, level) = names[m.pressure ?? ""] ?? ("Unknown", Level.unknown)
            mem.set(name, level: level,
                    sub: "\(fmtBytes(m.used)) of \(fmtBytes(m.total, digits: 0)) used \u{00B7} \(fmtBytes(m.compressed)) compressed")
        }
        if let w = s.swap {
            swap.set(fmtBytes(w.used),
                     sub: w.total > 0 ? "of \(fmtBytes(w.total)) \u{00B7} in \(fmtRate(w.in_bps)) \u{00B7} out \(fmtRate(w.out_bps))" : "no swap file in use")
        }
        if let d = s.disk, let v = d.volumes.min(by: { $0.free_pct < $1.free_pct }) {
            let level: Level? = v.free_pct < 5 ? .critical : v.free_pct < 10 ? .warning : nil
            let lowest = d.volumes.count > 1 ? " (lowest of \(d.volumes.count))" : ""
            disk.set(String(format: "%.0f%%", v.free_pct) + (level == nil ? "" : "  Low"), level: level,
                     sub: "\(fmtBytes(v.free)) of \(fmtBytes(v.total)) \u{00B7} \(v.mount)\(lowest) \u{00B7} IO \(fmtRate(d.read_bps + d.write_bps))")
        }
        if let n = s.net {
            let active = n.ifaces.filter { !$0.key.hasPrefix("lo") && ($0.value.in_bps > 0 || $0.value.out_bps > 0) }.keys.sorted()
            net.set("\u{2193} \(fmtRate(n.in_bps))   \u{2191} \(fmtRate(n.out_bps))",
                    sub: active.isEmpty ? "idle" : active.joined(separator: ", "))
        }
        if let i = s.internet {
            var bits: [String] = []
            if i.link?.type == "Wi-Fi" {
                var t = "Wi-Fi " + (i.link?.rssi.map { String(format: "%.0f dBm", $0) } ?? "no signal")
                if let tx = i.link?.tx_rate, tx > 0 { t += String(format: ", %.0f Mbps", tx) }
                bits.append(t)
            } else if let type = i.link?.type {
                bits.append("\(type) \(i.link?.iface ?? "")")
            } else {
                bits.append("no active interface")
            }
            if let avg = i.gateway?.avg { bits.append(String(format: "router %.0f ms", avg)) }
            else { bits.append(i.gateway?.ip != nil ? "router no reply" : "router none") }
            if i.dns?.ok == true, let ms = i.dns?.ms { bits.append(String(format: "DNS %.0f ms", ms)) }
            else { bits.append("DNS failing") }
            if i.http?.ok == false { bits.append("HTTPS check failing") }
            inet.set(i.verdict.text, level: Level(severity: i.verdict.level), sub: bits.joined(separator: " \u{00B7} "))
        } else {
            inet.set("Checking\u{2026}", level: .unknown, sub: " ")
        }
        if let t = s.thermal {
            if t.speed_limit == nil, let st = t.state {
                let names = ["Normal", "Fair", "Serious", "Critical"]
                therm.set(names.indices.contains(st) ? names[st] : "Unknown",
                          level: st >= 3 ? .critical : st >= 2 ? .warning : .good,
                          sub: st >= 2 ? "macOS is slowing the Mac to cool it" : st == 1 ? "Warm, not slowed" : "Not slowed for heat")
            } else if let limit = t.speed_limit {
                therm.set(limit < 100 ? String(format: "Throttled %.0f%%", limit) : "Normal",
                          level: limit < 100 ? .warning : .good,
                          sub: String(format: "CPU speed limit %.0f%%", limit))
            } else {
                therm.set("Not reported", level: .unknown, sub: "pmset gives no CPU speed limit on this Mac")
            }
        }

        events.arrangedSubviews.forEach { $0.removeFromSuperview() }
        let open = s.active_events ?? []
        if open.isEmpty {
            let none = makeLabel(12)
            none.stringValue = "None"
            events.addArrangedSubview(none)
        }
        let nowTs = Date().timeIntervalSince1970
        for e in open {
            let row = makeLabel(12, .regular, .labelColor, wraps: true)
            let text = NSMutableAttributedString(attributedString: dotted(e.title, Level(severity: e.severity), size: 12))
            text.append(NSAttributedString(string: "  open for \(fmtDur(nowTs - e.ts))",
                                           attributes: [.foregroundColor: NSColor.secondaryLabelColor, .font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .regular)]))
            row.attributedStringValue = text
            events.addArrangedSubview(row)
        }
    }
}
