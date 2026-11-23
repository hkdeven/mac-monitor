import AppKit

enum Level {
    case good, warning, critical, unknown

    /// Dot colour.
    var color: NSColor {
        switch self {
        case .good: return .systemGreen
        case .warning: return .systemOrange
        case .critical: return .systemRed
        case .unknown: return .systemGray
        }
    }

    /// Colour of a status word written as text. On a light panel the system
    /// green, orange and red are too pale to read as text, so there they are
    /// mixed with black; on a dark panel they are used as they are.
    var textColor: NSColor {
        if self == .unknown { return .secondaryLabelColor }
        let base = color
        return NSColor(name: nil) { look in
            if look.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua { return base }
            return base.usingColorSpace(.sRGB)?.blended(withFraction: 0.4, of: .black) ?? base
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

extension Monitor {
    /// The dot rule, shared by the menu bar item and the panel header: red when
    /// a critical event is open or the monitor is not responding, amber when a
    /// warning event is open, green otherwise, grey while there is no reading yet.
    var level: Level {
        switch phase {
        case .connecting, .starting:
            return .unknown
        case .down:
            return .critical
        case .live:
            let open = now?.active_events ?? []
            if open.contains(where: { $0.severity == "critical" }) { return .critical }
            if open.contains(where: { $0.severity == "warning" }) { return .warning }
            return now?.ready == true ? .good : .unknown
        }
    }
}

private let panelWidth: CGFloat = 340
private let panelInset: CGFloat = 14
private let contentWidth = panelWidth - 2 * panelInset
private let tileGap: CGFloat = 8
private let tileWidth = (contentWidth - tileGap) / 2

private func font(_ size: CGFloat, _ weight: NSFont.Weight = .regular) -> NSFont {
    .monospacedDigitSystemFont(ofSize: size, weight: weight)
}

private func makeLabel(_ size: CGFloat, _ weight: NSFont.Weight = .regular, _ color: NSColor = .labelColor, wraps: Bool = false) -> NSTextField {
    let f = wraps ? NSTextField(wrappingLabelWithString: "") : NSTextField(labelWithString: "")
    f.font = font(size, weight)
    f.textColor = color
    f.isSelectable = false
    if wraps {
        f.preferredMaxLayoutWidth = contentWidth
    } else {
        f.lineBreakMode = .byTruncatingTail
    }
    return f
}

/// A 12 pt detail line in the tertiary colour. `lead` is a status word put first in its own colour.
private func detail(_ text: String, lead: String? = nil, level: Level = .unknown) -> NSAttributedString {
    let out = NSMutableAttributedString()
    var rest = text
    if let lead = lead {
        out.append(NSAttributedString(string: lead, attributes: [.font: font(12, .medium), .foregroundColor: level.textColor]))
        if !rest.isEmpty { rest = ", " + rest }
    }
    // A line with a coloured word is drawn without the panel's vibrancy, where
    // the tertiary colour is too faint on a dark backdrop; use secondary there.
    let quiet: NSColor = lead == nil ? .tertiaryLabelColor : .secondaryLabelColor
    out.append(NSAttributedString(string: rest, attributes: [.font: font(12), .foregroundColor: quiet]))
    let para = NSMutableParagraphStyle()
    para.lineBreakMode = .byTruncatingTail
    out.addAttribute(.paragraphStyle, value: para, range: NSRange(location: 0, length: out.length))
    return out
}

/// "27.7 of 48 GB" when both figures share a unit, "512 MB of 48 GB" when they do not.
private func part(_ a: String, of b: String) -> String {
    let x = a.split(separator: " "), y = b.split(separator: " ")
    if x.count == 2, y.count == 2, x[1] == y[1] { return "\(x[0]) of \(b)" }
    return "\(a) of \(b)"
}

/// A view filled with one semantic colour, redrawn when light or dark changes.
private class FillView: NSView {
    var fill: NSColor = .clear { didSet { needsDisplay = true } }
    var radius: CGFloat = 0

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        layer?.cornerRadius = radius
        // Resolve against plain light or dark. Inside a popover the appearance is
        // a vibrant one, where label colours are opaque greys meant for blending.
        let plain = effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) ?? .aqua
        NSAppearance(named: plain)?.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = fill.cgColor
        }
    }
}

private func makeDot(_ color: NSColor = .systemGray) -> FillView {
    let d = FillView(frame: .zero)
    d.radius = 3.5
    d.fill = color
    d.translatesAutoresizingMaskIntoConstraints = false
    d.widthAnchor.constraint(equalToConstant: 7).isActive = true
    d.heightAnchor.constraint(equalToConstant: 7).isActive = true
    return d
}

/// One tile: label, value and a one-line detail, stacked and left aligned on a
/// soft rounded fill. A wide tile puts its status word on the right instead.
private final class Tile: FillView {
    private let name = makeLabel(12, .regular, .secondaryLabelColor)
    private let value = makeLabel(18, .medium)
    private let sub = makeLabel(12, .regular, .tertiaryLabelColor)
    private let side = makeLabel(14, .semibold)
    private let stack = NSStackView()

    init(_ title: String, wide: Bool = false) {
        super.init(frame: .zero)
        fill = .quaternaryLabelColor
        radius = 8
        name.stringValue = title
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 2
        stack.translatesAutoresizingMaskIntoConstraints = false
        for l in [name, value, sub] {
            l.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        }
        stack.addArrangedSubview(name)
        if !wide { stack.addArrangedSubview(value) }
        stack.addArrangedSubview(sub)
        addSubview(stack)

        let snug = stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -8)
        snug.priority = NSLayoutConstraint.Priority(200)    // below the stack's own hugging
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 8),
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 10),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: bottomAnchor, constant: -8),
            snug,
        ])
        if wide {
            side.translatesAutoresizingMaskIntoConstraints = false
            side.setContentHuggingPriority(.required, for: .horizontal)
            side.setContentCompressionResistancePriority(.required, for: .horizontal)
            addSubview(side)
            NSLayoutConstraint.activate([
                side.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -10),
                side.centerYAnchor.constraint(equalTo: centerYAnchor),
                stack.trailingAnchor.constraint(lessThanOrEqualTo: side.leadingAnchor, constant: -8),
            ])
        } else {
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -10).isActive = true
        }
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func set(_ text: String, _ size: CGFloat, _ weight: NSFont.Weight, _ color: NSColor, padTop: CGFloat, padBottom: CGFloat) {
        value.stringValue = text
        value.font = font(size, weight)
        value.textColor = color
        stack.setCustomSpacing(padTop, after: name)
        stack.setCustomSpacing(padBottom, after: value)
    }

    /// A figure: 18 pt medium.
    func show(number: String, sub line: NSAttributedString, tip: String) {
        set(number, 18, .medium, .labelColor, padTop: 2, padBottom: 2)
        sub.attributedStringValue = line
        toolTip = tip
    }

    /// A status word: 14 pt semibold in the status colour, on the same baseline as a figure beside it.
    func show(status: String, _ level: Level, sub line: NSAttributedString, tip: String) {
        set(status, 14, .semibold, level.textColor, padTop: 5, padBottom: 4)
        sub.attributedStringValue = line
        toolTip = tip
    }

    /// Two 13 pt lines in the value spot and no detail line.
    func show(lines first: String, _ second: String, tip: String) {
        set(first, 13, .medium, .labelColor, padTop: 4, padBottom: 1)
        sub.attributedStringValue = NSAttributedString(string: second, attributes: [.font: font(13, .medium), .foregroundColor: NSColor.labelColor])
        toolTip = tip
    }

    /// Wide tile: detail under the label, status word on the right.
    func show(side word: String, _ level: Level, sub line: String, tip: String) {
        side.stringValue = word
        side.textColor = level.textColor
        sub.attributedStringValue = detail(line)
        toolTip = tip
    }
}

@MainActor
final class PanelController: NSViewController {
    var onOpenDashboard: (() -> Void)?
    var onQuit: (() -> Void)?

    private let root = NSStackView()
    private let headDot = makeDot()
    private let state = makeLabel(12, .regular, .secondaryLabelColor)
    private let spinner = NSProgressIndicator()
    private let message = makeLabel(13, .regular, .labelColor, wraps: true)
    private let waiting = NSStackView()
    private var grid = NSView()
    private let events = NSStackView()
    private let eventsTitle = makeLabel(12, .regular, .secondaryLabelColor)
    private let openButton = NSButton(title: "Open dashboard", target: nil, action: nil)
    private let quitButton = NSButton(title: "Quit", target: nil, action: nil)

    private let cpu = Tile("CPU")
    private let mem = Tile("Memory pressure")
    private let swap = Tile("Swap used")
    private let disk = Tile("Disk free")
    private let net = Tile("Network")
    private let inet = Tile("Internet")
    private let therm = Tile("Thermal", wide: true)

    private static let clock: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "HH:mm:ss"
        return f
    }()
    private static let shortClock: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "HH:mm"
        return f
    }()

    override func loadView() {
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 0
        root.edgeInsets = NSEdgeInsets(top: panelInset, left: panelInset, bottom: panelInset, right: panelInset)
        root.translatesAutoresizingMaskIntoConstraints = false

        // Header: name on the left, status dot and words on the right.
        let title = makeLabel(14, .semibold)
        title.stringValue = "Mac Monitor"
        title.setContentHuggingPriority(.defaultLow, for: .horizontal)
        state.setContentHuggingPriority(.required, for: .horizontal)
        let head = NSStackView(views: [title, headDot, state])
        head.orientation = .horizontal
        head.distribution = .fill
        head.alignment = .centerY
        head.spacing = 6

        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false
        message.preferredMaxLayoutWidth = contentWidth - 24
        waiting.orientation = .horizontal
        waiting.spacing = 8
        waiting.alignment = .centerY
        waiting.addArrangedSubview(spinner)
        waiting.addArrangedSubview(message)

        // Tiles: two equal columns, Thermal across both.
        let g = NSGridView(views: [[cpu, mem], [swap, disk], [net, inet], [therm]])
        g.columnSpacing = tileGap
        g.rowSpacing = tileGap
        g.xPlacement = .fill
        g.yPlacement = .fill
        g.column(at: 0).width = tileWidth
        g.column(at: 1).width = tileWidth
        g.mergeCells(inHorizontalRange: NSRange(location: 0, length: 2), verticalRange: NSRange(location: 3, length: 1))
        grid = g

        eventsTitle.stringValue = "Open events"
        events.orientation = .vertical
        events.alignment = .leading
        events.spacing = 4

        let line = NSBox()
        line.boxType = .separator

        // Buttons: same plain bordered style; Open dashboard takes the spare width.
        openButton.target = self
        openButton.action = #selector(openDashboard)
        openButton.image = NSImage(systemSymbolName: "arrow.up.right.square", accessibilityDescription: nil)
        openButton.imagePosition = .imageLeading
        openButton.imageHugsTitle = true
        quitButton.target = self
        quitButton.action = #selector(quit)
        for b in [openButton, quitButton] {
            b.bezelStyle = .rounded
            b.font = .systemFont(ofSize: 13)
        }
        openButton.setContentHuggingPriority(NSLayoutConstraint.Priority(1), for: .horizontal)
        quitButton.setContentHuggingPriority(.required, for: .horizontal)
        let buttons = NSStackView(views: [openButton, quitButton])
        buttons.orientation = .horizontal
        buttons.distribution = .fill
        buttons.spacing = 8

        let all: [NSView] = [head, waiting, grid, eventsTitle, events, line, buttons]
        for v in all {
            root.addArrangedSubview(v)
            v.widthAnchor.constraint(equalToConstant: contentWidth).isActive = true
        }
        root.setCustomSpacing(10, after: head)
        root.setCustomSpacing(12, after: waiting)
        root.setCustomSpacing(12, after: grid)
        root.setCustomSpacing(4, after: eventsTitle)
        root.setCustomSpacing(12, after: events)
        root.setCustomSpacing(10, after: line)

        let box = NSView()
        box.addSubview(root)
        let bottom = root.bottomAnchor.constraint(equalTo: box.bottomAnchor)
        bottom.priority = NSLayoutConstraint.Priority(749)  // the popover grows a moment after its content does
        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: box.topAnchor),
            bottom,
            root.leadingAnchor.constraint(equalTo: box.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: box.trailingAnchor),
            root.widthAnchor.constraint(equalToConstant: panelWidth),
        ])
        view = box
    }

    @objc private func openDashboard() { onOpenDashboard?() }
    @objc private func quit() { onQuit?() }

    /// Redraws the panel from the monitor's latest read. Only called while the panel is open.
    func render(_ m: Monitor) {
        _ = view
        let s = m.now
        let live = m.phase == .live && s?.ready == true
        openButton.isEnabled = m.dashboardURL != nil
        headDot.fill = m.level.color

        switch m.phase {
        case .live:
            if live, let t = s?.ts.map({ Date(timeIntervalSince1970: $0) }) ?? m.lastGood {
                state.stringValue = "Live, updated \(PanelController.clock.string(from: t))"
            } else {
                state.stringValue = "Connecting\u{2026}"
            }
            message.stringValue = "Waiting for the first reading\u{2026}"
        case .connecting:
            state.stringValue = "Connecting\u{2026}"
            message.stringValue = "Looking for the monitor\u{2026}"
        case .starting:
            state.stringValue = "Connecting\u{2026}"
            message.stringValue = "Starting the monitor\u{2026}"
        case .down:
            state.stringValue = "Not responding since \(PanelController.shortClock.string(from: m.lastGood ?? m.started))"
            message.stringValue = m.problem ?? "Trying again every 5 seconds."
        }
        waiting.isHidden = live
        let spinning = !live && m.phase != .down
        spinner.isHidden = !spinning
        if spinning { spinner.startAnimation(nil) } else { spinner.stopAnimation(nil) }
        for v in [grid, eventsTitle, events] { v.isHidden = !live }

        if live, let s = s { fill(s) }
        view.layoutSubtreeIfNeeded()
        preferredContentSize = NSSize(width: panelWidth, height: root.fittingSize.height)
    }

    private func fill(_ s: Now) {
        if let c = s.cpu {
            let l1 = c.load.first ?? 0
            var line = detail(String(format: "load %.1f of %d cores", l1, c.ncpu))
            if let crit = s.limits?.load_critical, l1 > crit {
                line = detail(String(format: "%.1f", l1), lead: "Load critical", level: .critical)
            } else if let warn = s.limits?.load_warn, l1 > warn {
                line = detail(String(format: "%.1f", l1), lead: "Load high", level: .warning)
            }
            cpu.show(number: fmtPct(c.busy), sub: line,
                     tip: String(format: "Load %.1f of %d cores, user %.0f%%, system %.0f%%", l1, c.ncpu, c.user, c.sys))
        }
        if let m = s.mem {
            let names = ["normal": ("Normal", Level.good), "warn": ("Warn", Level.warning), "critical": ("Critical", Level.critical)]
            let (word, level) = names[m.pressure ?? ""] ?? ("Unknown", Level.unknown)
            let used = part(fmtBytes(m.used), of: fmtBytes(m.total, digits: 0)) + " used"
            mem.show(status: word, level, sub: detail(used), tip: "\(used), \(fmtBytes(m.compressed)) compressed")
        }
        if let w = s.swap {
            let line = w.total > 0 ? "of \(fmtBytes(w.total))" : "no swap file in use"
            swap.show(number: fmtBytes(w.used), sub: detail(line),
                      tip: w.total > 0 ? "\(line), in \(fmtRate(w.in_bps)), out \(fmtRate(w.out_bps))" : line)
        }
        if let d = s.disk, let v = d.volumes.min(by: { $0.free_pct < $1.free_pct }) {
            var free = part(fmtBytes(v.free), of: fmtBytes(v.total))
            if v.mount != "/" { free = (v.mount as NSString).lastPathComponent + ", " + free }
            let line = v.free_pct < 5 ? detail(free, lead: "Low", level: .critical)
                : v.free_pct < 10 ? detail(free, lead: "Low", level: .warning) : detail(free)
            let lowest = d.volumes.count > 1 ? " (lowest of \(d.volumes.count) volumes)" : ""
            disk.show(number: String(format: "%.0f%%", v.free_pct), sub: line,
                      tip: "\(part(fmtBytes(v.free), of: fmtBytes(v.total))) free on \(v.mount)\(lowest), reading and writing \(fmtRate(d.read_bps + d.write_bps))")
        } else {
            disk.show(number: "-", sub: detail("no reading this time"), tip: "The monitor could not read the disks this time")
        }
        if let n = s.net {
            let active = n.ifaces.filter { !$0.key.hasPrefix("lo") && ($0.value.in_bps > 0 || $0.value.out_bps > 0) }.keys.sorted()
            net.show(lines: "\u{2193} \(fmtRate(n.in_bps))", "\u{2191} \(fmtRate(n.out_bps))",
                     tip: active.isEmpty ? "Idle" : "Active on " + active.joined(separator: ", "))
        }
        fillInternet(s.internet)
        if let t = s.thermal {
            if t.speed_limit == nil, let st = t.state {
                let names = ["Normal", "Fair", "Serious", "Critical"]
                let line = st >= 2 ? "macOS is slowing the Mac to cool it" : st == 1 ? "Warm, not slowed" : "Not slowed for heat"
                therm.show(side: names.indices.contains(st) ? names[st] : "Unknown",
                           st >= 3 ? .critical : st >= 2 ? .warning : .good, sub: line, tip: line)
            } else if let limit = t.speed_limit {
                let line = String(format: "CPU speed limit %.0f%%", limit)
                therm.show(side: limit < 100 ? String(format: "Throttled %.0f%%", limit) : "Normal",
                           limit < 100 ? .warning : .good, sub: line, tip: line)
            } else {
                let line = "This Mac does not report it"
                therm.show(side: "Not reported", .unknown, sub: line, tip: line)
            }
        }

        events.arrangedSubviews.forEach { $0.removeFromSuperview() }
        let open = s.active_events ?? []
        if open.isEmpty {
            let none = makeLabel(12, .regular, .tertiaryLabelColor)
            none.stringValue = "None"
            events.addArrangedSubview(none)
        }
        let nowTs = Date().timeIntervalSince1970
        for e in open {
            let text = NSMutableAttributedString(string: e.title, attributes: [.font: font(12), .foregroundColor: NSColor.labelColor])
            text.append(NSAttributedString(string: "  open for \(fmtDur(nowTs - e.ts))",
                                           attributes: [.font: font(12), .foregroundColor: NSColor.tertiaryLabelColor]))
            let label = makeLabel(12, wraps: true)
            label.preferredMaxLayoutWidth = contentWidth - 13
            label.attributedStringValue = text
            // The dot sits beside the first line.
            let dot = makeDot(Level(severity: e.severity).color)
            let holder = NSView()
            holder.translatesAutoresizingMaskIntoConstraints = false
            holder.addSubview(dot)
            NSLayoutConstraint.activate([
                holder.widthAnchor.constraint(equalToConstant: 7),
                holder.heightAnchor.constraint(equalToConstant: 15),
                dot.centerXAnchor.constraint(equalTo: holder.centerXAnchor),
                dot.centerYAnchor.constraint(equalTo: holder.centerYAnchor),
            ])
            let row = NSStackView(views: [holder, label])
            row.orientation = .horizontal
            row.distribution = .fill
            row.alignment = .top
            row.spacing = 6
            events.addArrangedSubview(row)
            row.widthAnchor.constraint(equalToConstant: contentWidth).isActive = true
        }
    }

    /// Internet: the verdict's status words (and latency, when it has one) as the
    /// value; loss and the kind of link underneath. monitor.py's verdict text is
    /// "Good, 15 ms, 0% loss", "Slow, 180 ms", "Dropping packets, 12% loss",
    /// "Unsteady, jitter 60 ms", "DNS failing", "DNS slow, 600 ms" or "Down - where".
    private func fillInternet(_ i: Now.Internet?) {
        guard let i = i else {
            inet.show(status: "Checking\u{2026}", .unknown, sub: detail(" "), tip: "The first internet check has not finished")
            return
        }
        let level = Level(severity: i.verdict.level)
        var tips = [i.verdict.text]
        if let avg = i.gateway?.avg { tips.append(String(format: "router %.0f ms", avg)) }
        if i.dns?.ok == true, let ms = i.dns?.ms { tips.append(String(format: "DNS %.0f ms", ms)) }
        else if i.dns?.ok == false, !i.verdict.text.hasPrefix("DNS") { tips.append("last DNS lookup failed") }
        if i.http?.ok == false { tips.append("HTTPS check failing") }
        let tip = tips.joined(separator: ", ")

        let down = "Down - "
        if i.verdict.text.hasPrefix(down) {
            inet.show(status: "Down", level, sub: detail(String(i.verdict.text.dropFirst(down.count))), tip: tip)
            return
        }
        var parts = i.verdict.text.components(separatedBy: ", ")
        var word = parts.removeFirst()
        if let next = parts.first, next.hasSuffix(" ms"), next.first?.isNumber == true {
            word += ", " + parts.removeFirst()
        }
        if !parts.contains(where: { $0.hasSuffix("loss") }), let loss = i.loss {
            parts.append(String(format: "%.0f%% loss", loss))
        }
        if let type = i.link?.type { parts.append(type) }
        let lead: String? = i.http?.ok == false ? "HTTPS check failing" : nil
        inet.show(status: word, level, sub: detail(parts.joined(separator: ", "), lead: lead, level: .warning), tip: tip)
    }
}
