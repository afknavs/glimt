import SwiftUI

// MARK: symbols

enum Symbols {
    static func exists(_ name: String) -> Bool { NSImage(systemSymbolName: name, accessibilityDescription: nil) != nil }

    static func device(_ device: Device) -> String {
        if let icon = device.icon, exists(icon) { return icon }
        switch device.kind {
        case "light": return device.isColorLight ? "light.ribbon.fill" : "lightbulb.fill"
        case "outlet": return "powerplug.fill"
        case "switch": return "lightswitch.on.fill"
        case "climate": return "snowflake"
        case "fan": return "fan.fill"
        case "speaker": return "speaker.wave.2.fill"
        default: return "circle.grid.2x2.fill"
        }
    }

    /// Common action names get a glyph; anything else falls back to its name.
    static func action(_ name: String) -> String? {
        let known = ["faster": "plus", "slower": "minus", "up": "chevron.up", "down": "chevron.down",
                     "turbo_on": "bolt.fill", "turbo_off": "bolt.slash.fill", "on": "power", "off": "poweroff",
                     "play": "play.fill", "pause": "pause.fill", "next": "forward.fill", "previous": "backward.fill"]
        return known[name]
    }
}

let cueColors: [(name: String, color: Color)] = [
    ("done", Color(red: 0.19, green: 0.82, blue: 0.35)), ("attention", Color(red: 1, green: 0.62, blue: 0.04)),
    ("error", Color(red: 1, green: 0.27, blue: 0.23)), ("info", Color(red: 0.04, green: 0.52, blue: 1)),
    ("working", Color(red: 0.37, green: 0.36, blue: 0.9)),
]
/// One quiet glyph per cue.
func cueSymbol(_ name: String) -> String {
    ["done": "checkmark.circle.fill", "attention": "exclamationmark.circle.fill", "error": "xmark.circle.fill",
     "info": "info.circle.fill", "working": "ellipsis.circle.fill"][name] ?? "circle.fill"
}
func cueColor(_ name: String) -> Color { cueColors.first { $0.name == name }?.color ?? .accentColor }

let swatches: [(name: String, hex: String)] = [
    ("White", "#ffffff"), ("Warm", "#ff9a3c"), ("Pink", "#ff375f"),
    ("Purple", "#bf5af2"), ("Blue", "#0a84ff"), ("Green", "#30d158"),
]

/// Opens the system colour panel from a small wheel swatch (the stock ColorPicker well is too wide).
final class ColorPanel: NSObject {
    static let shared = ColorPanel()
    private var onPick: ((Color) -> Void)?
    func open(current: Color, onPick: @escaping (Color) -> Void) {
        self.onPick = onPick
        let panel = NSColorPanel.shared
        panel.showsAlpha = false
        panel.color = NSColor(current)
        panel.setTarget(self)
        panel.setAction(#selector(picked(_:)))
        panel.orderFront(nil)
    }
    @objc private func picked(_ sender: NSColorPanel) { onPick?(Color(nsColor: sender.color)) }
}

extension Color {
    init(hex: String) {
        let value = UInt32(hex.trimmingCharacters(in: CharacterSet(charactersIn: "#")), radix: 16) ?? 0xFFFFFF
        self.init(red: Double((value >> 16) & 0xFF) / 255, green: Double((value >> 8) & 0xFF) / 255, blue: Double(value & 0xFF) / 255)
    }
    /// Bright enough that a white glyph on it would be hard to read.
    var isLight: Bool {
        let c = NSColor(self).usingColorSpace(.sRGB) ?? .white
        return 0.299 * c.redComponent + 0.587 * c.greenComponent + 0.114 * c.blueComponent > 0.72
    }
    var hex: String {
        let c = NSColor(self).usingColorSpace(.sRGB) ?? .white
        return String(format: "#%02x%02x%02x", Int(round(c.redComponent * 255)), Int(round(c.greenComponent * 255)), Int(round(c.blueComponent * 255)))
    }
}

// MARK: building blocks

/// A round glyph button: the one control style used everywhere.
struct GlyphButton: View {
    let symbol: String
    var active = false
    var tint: Color = .accentColor
    var size: CGFloat = 30
    let label: String
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: size * 0.43, weight: .semibold))
                .foregroundStyle(active ? Color.white : Color.primary.opacity(0.85))
                .frame(width: size, height: size)
                .background(Circle().fill(active ? AnyShapeStyle(tint) : AnyShapeStyle(Color.primary.opacity(0.1))))
                .contentShape(Circle())
        }
        .buttonStyle(.plain).help(label).accessibilityLabel(label)
    }
}

/// Control Center style slider: a thick capsule you drag anywhere on.
struct CapsuleSlider: View {
    let value: Double
    let symbol: String
    var tint: Color = .white
    let label: String
    let onChange: (Double) -> Void
    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.1))
                Capsule().fill(tint.opacity(0.92)).frame(width: max(28, geometry.size.width * value / 100))
                Image(systemName: symbol).font(.system(size: 12, weight: .semibold)).foregroundStyle(.black.opacity(0.55)).padding(.leading, 9)
            }
            .contentShape(Capsule())
            .gesture(DragGesture(minimumDistance: 0).onChanged { drag in
                onChange(min(100, max(1, (drag.location.x / geometry.size.width * 100).rounded())))
            })
        }
        .frame(height: 26).accessibilityLabel(label).accessibilityValue("\(Int(value)) percent")
    }
}

/// The same capsule in steps, for a setting with a few levels such as fan speed.
struct StepSlider: View {
    let steps: Int
    /// How many steps are lit; 0 when the device is choosing for itself.
    let level: Int
    let symbol: String
    let label: String
    let onChange: (Int) -> Void
    var body: some View {
        GeometryReader { geometry in
            HStack(spacing: 2) {
                ForEach(1...max(1, steps), id: \.self) { step in
                    Rectangle().fill(step <= level ? AnyShapeStyle(Color.accentColor) : AnyShapeStyle(Color.primary.opacity(0.1)))
                }
            }
            .clipShape(Capsule())
            .overlay(alignment: .leading) {
                Image(systemName: symbol).font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(level > 0 ? Color.white : Color.primary.opacity(0.55)).padding(.leading, 9)
            }
            .contentShape(Capsule())
            .gesture(DragGesture(minimumDistance: 0).onChanged { drag in
                let step = min(steps, max(1, Int((drag.location.x / geometry.size.width * Double(steps)).rounded(.up))))
                if step != level { onChange(step) }
            })
        }
        .frame(height: 26).accessibilityLabel(label).accessibilityValue(level > 0 ? "\(level) of \(steps)" : "Automatic")
    }
}

// MARK: the room, a tile per device

extension Store {
    /// The colour a device's icon takes when it is on: a light shows the colour it is lit in.
    func tint(_ device: Device) -> Color {
        device.isColorLight ? Color(hex: string(device, "color") ?? "#ffffff")
            : device.kind == "light" ? Color(red: 1, green: 0.8, blue: 0.2) : .accentColor
    }
}

/// The round icon. Its colour is the state.
struct Chip: View {
    let symbol: String
    let on: Bool
    var tint: Color = .accentColor
    var size: CGFloat = 28
    var body: some View {
        let fill = on ? tint : Color.primary.opacity(0.1)
        Image(systemName: symbol)
            .font(.system(size: size * 0.46, weight: .medium))
            .foregroundStyle(!on ? Color.secondary : fill.isLight ? Color.black.opacity(0.7) : Color.white)
            .frame(width: size, height: size)
            .background(Circle().fill(fill))
            .overlay(Circle().strokeBorder(Color.primary.opacity(on && fill.isLight ? 0.12 : 0), lineWidth: 0.5))
            .contentShape(Circle())
    }
}

/// The rounded square every tile sits in. Clicking it anywhere but on a control runs `open`.
struct TileFrame<Content: View>: View {
    var lit = false
    var height: CGFloat = 50
    var dimmed = false
    let open: () -> Void
    @ViewBuilder let content: () -> Content
    @State private var hovering = false
    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 12, style: .continuous)
        HStack(spacing: 8, content: content)
            .padding(.horizontal, 9).frame(maxWidth: .infinity, minHeight: height, maxHeight: height, alignment: .leading)
            .background(shape.fill(Color.primary.opacity((lit ? 0.12 : 0.055) + (hovering ? 0.035 : 0))))
            .contentShape(shape)
            .onHover { hovering = $0 }
            .onTapGesture(perform: open)
            .opacity(dimmed ? 0.45 : 1)
            .accessibilityAddTraits(.isButton)
    }
}

/// A tile's two lines: what it is, and how it is.
struct TileLabel: View {
    let name: String
    let status: String
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(name).font(.system(size: 12.5, weight: .medium)).lineLimit(1).truncationMode(.tail)
            if !status.isEmpty { Text(status).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1) }
        }
    }
}

/// A device in the grid. The icon switches it; the rest of the tile opens its controls,
/// or switches it too when there is nothing more to show.
struct DeviceTile: View {
    @ObservedObject var store: Store
    let device: Device

    var body: some View {
        let on = store.bool(device, "power")
        let offline = device.online == false
        TileFrame(lit: on, dimmed: offline, open: { if device.hasControls { store.detail = device.id } else { toggle() } }) {
            Button(action: toggle) { Chip(symbol: Symbols.device(device), on: on, tint: store.tint(device)) }
                .buttonStyle(.plain).disabled(!device.has("power"))
                .accessibilityLabel(device.name).accessibilityValue(on ? "On" : "Off")
            if offline || device.quickActions.isEmpty {
                TileLabel(name: device.name, status: status)
            } else {
                VStack(alignment: .leading, spacing: 3) {   // a couple of actions fit where the status would be
                    Text(device.name).font(.system(size: 12.5, weight: .medium)).lineLimit(1)
                    HStack(spacing: 5) {
                        ForEach(device.quickActions, id: \.self) { action in
                            GlyphButton(symbol: Symbols.action(action) ?? "circle", size: 20,
                                        label: action.replacingOccurrences(of: "_", with: " ").capitalized) { store.act(device, action) }
                        }
                    }
                }
            }
        }
        .help(offline ? "\(device.name): \(device.error ?? "unreachable")" : "")
    }

    private var status: String {
        if device.online == false { return "No response" }
        guard device.has("power") else { return "" }
        guard store.bool(device, "power") else { return "Off" }
        return store.int(device, "brightness").map { "\($0)%" } ?? "On"
    }

    private func toggle() {
        guard device.has("power") else { return }
        store.set(device, ["power": !store.bool(device, "power")])
    }
}

/// A thermostat gets a wide tile: its reading, and the temperature within reach.
struct ClimateTile: View {
    @ObservedObject var store: Store
    let device: Device
    var body: some View {
        let on = store.bool(device, "power")
        let offline = device.online == false
        let spec = device.capabilities["temperature"]!
        let target = store.int(device, "temperature") ?? spec.min
        let room = (store.value(device, "current_temperature") as? Int).map { "Room \($0)°" }
        TileFrame(lit: on, height: 64, dimmed: offline, open: { store.detail = device.id }) {
            Button { store.set(device, ["power": !on]) } label: { Chip(symbol: Symbols.device(device), on: on, size: 32) }
                .buttonStyle(.plain).disabled(!device.has("power"))
                .accessibilityLabel(device.name).accessibilityValue(on ? "On" : "Off")
            TileLabel(name: device.name, status: offline ? "No response" : on ? room ?? "On" : "Off")
            Spacer(minLength: 4)
            HStack(spacing: 4) {
                GlyphButton(symbol: "minus", size: 24, label: "Cooler") { store.set(device, ["temperature": max(spec.min, target - 1)]) }
                Text("\(target)°").font(.system(size: 26, weight: .light, design: .rounded)).monospacedDigit()
                    .foregroundStyle(on ? Color.primary : Color.secondary).contentTransition(.numericText())
                    .frame(minWidth: 46)
                GlyphButton(symbol: "plus", size: 24, label: "Warmer") { store.set(device, ["temperature": min(spec.max, target + 1)]) }
            }
            .disabled(offline)
        }
        .help(offline ? "\(device.name): \(device.error ?? "unreachable")" : "")
    }
}

/// Several plain switches of a kind in one tile. It only opens: switching a whole set at once is rarely meant.
struct GroupTile: View {
    @ObservedObject var store: Store
    let kind: String
    let devices: [Device]
    var body: some View {
        let on = devices.filter { store.bool($0, "power") }.count
        TileFrame(lit: on > 0, open: { store.detail = "kind:" + kind }) {
            Chip(symbol: devices.first.map(Symbols.device) ?? "circle.grid.2x2.fill", on: on > 0)
            TileLabel(name: Tile.folded[kind] ?? kind.capitalized,
                      status: on == 0 ? "All off" : on == devices.count ? "All on" : "\(on) of \(devices.count) on")
        }
    }
}

/// Tiles two to a row; a wide one takes a row to itself.
struct TileGrid: View {
    @ObservedObject var store: Store
    let tiles: [Tile]
    var body: some View {
        VStack(spacing: 6) {
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                HStack(spacing: 6) {
                    ForEach(row) { tile in
                        switch tile {
                        case .device(let device) where device.isClimate: ClimateTile(store: store, device: device)
                        case .device(let device): DeviceTile(store: store, device: device)
                        case .group(let kind, let devices): GroupTile(store: store, kind: kind, devices: devices)
                        }
                    }
                    if row.count == 1, !row[0].isWide { Color.clear.frame(maxWidth: .infinity, maxHeight: 1) }   // keep a lone tile half width
                }
            }
        }
    }

    private var rows: [[Tile]] {
        var rows: [[Tile]] = [], waiting: Tile?
        for tile in tiles {
            if tile.isWide { rows.append([tile]) }
            else if let first = waiting { rows.append([first, tile]); waiting = nil }
            else { waiting = tile }
        }
        if let waiting { rows.append([waiting]) }
        return rows
    }
}

// MARK: one device's controls

/// The top of a page inside the panel: the way back, what it is, and its switch if it has one.
struct DetailHeader<Trailing: View>: View {
    @ObservedObject var store: Store
    let title: String
    @ViewBuilder let trailing: () -> Trailing
    var body: some View {
        HStack(spacing: 8) {
            Button { store.detail = nil } label: {
                Image(systemName: "chevron.left").font(.system(size: 11, weight: .semibold))
                    .frame(width: 22, height: 22).contentShape(Rectangle())
            }.buttonStyle(.plain).keyboardShortcut(.cancelAction).accessibilityLabel("Back").help("Back")
            Text(title).font(.system(size: 13, weight: .semibold)).lineLimit(1)
            Spacer(minLength: 6)
            trailing()
        }
        .frame(height: 24)
    }
}

/// What opens when you click a tile: the same panel, showing only that device.
struct DeviceDetail: View {
    @ObservedObject var store: Store
    let device: Device
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            DetailHeader(store: store, title: device.name) {
                if device.has("power") {
                    Toggle("", isOn: Binding(get: { store.bool(device, "power") }, set: { store.set(device, ["power": $0]) }))
                        .toggleStyle(.switch).controlSize(.small).labelsHidden().accessibilityLabel("Power")
                }
            }
            Group {
                if device.isClimate { ClimateControls(store: store, device: device) }
                if device.has("brightness") || device.isColorLight { LightControls(store: store, device: device) }
                ForEach(device.choices, id: \.self) { key in ChoiceRow(store: store, device: device, key: key) }
                ForEach(device.dials, id: \.self) { key in DialRow(store: store, device: device, key: key) }
                if !device.isClimate, !device.actions.isEmpty { ActionButtons(store: store, device: device, actions: device.actions) }
            }
            .padding(.horizontal, 6)
            .disabled(device.online == false).opacity(device.online == false ? 0.45 : 1)
        }
    }
}

/// A folded tile, opened: each switch in the set, by name.
struct GroupDetail: View {
    @ObservedObject var store: Store
    let kind: String
    let devices: [Device]
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            DetailHeader(store: store, title: Tile.folded[kind] ?? kind.capitalized) { EmptyView() }
            VStack(spacing: 0) {
                ForEach(devices) { device in
                    let offline = device.online == false
                    HStack(spacing: 8) {
                        Text(device.name).font(.system(size: 13)).lineLimit(1)
                        Spacer(minLength: 6)
                        if offline { Text("No response").font(.system(size: 11)).foregroundStyle(.secondary) }
                        Toggle("", isOn: Binding(get: { store.bool(device, "power") }, set: { store.set(device, ["power": $0]) }))
                            .toggleStyle(.switch).controlSize(.small).labelsHidden().accessibilityLabel(device.name)
                            .disabled(offline)
                    }
                    .frame(height: 32).opacity(offline ? 0.45 : 1)
                    if device.id != devices.last?.id { Divider().opacity(0.5) }
                }
            }
            .padding(.horizontal, 6)
        }
    }
}

struct LightControls: View {
    @ObservedObject var store: Store
    let device: Device
    var body: some View {
        let on = store.bool(device, "power")
        let color = device.isColorLight ? Color(hex: store.string(device, "color") ?? "#ffffff") : Color(red: 1, green: 0.8, blue: 0.2)
        if device.has("brightness") {
            CapsuleSlider(value: Double(store.int(device, "brightness") ?? 50), symbol: "sun.max.fill",
                          tint: on ? color : .gray, label: "Brightness") {
                store.set(device, ["brightness": Int($0)], after: .milliseconds(70))
            }
        }
        if device.isColorLight {
            HStack(spacing: 0) {
                ForEach(swatches, id: \.hex) { swatch in
                    Button { store.set(device, ["power": true, "color": swatch.hex]) } label: {
                        Circle().fill(Color(hex: swatch.hex)).frame(width: 22, height: 22)
                            .overlay(Circle().strokeBorder(Color.primary.opacity(0.15), lineWidth: 0.5))
                    }.buttonStyle(.plain).help(swatch.name).accessibilityLabel(swatch.name)
                    Spacer(minLength: 0)
                }
                Button {
                    ColorPanel.shared.open(current: color) { store.set(device, ["power": true, "color": $0.hex], after: .milliseconds(90)) }
                } label: {
                    Circle().fill(AngularGradient(colors: [.red, .yellow, .green, .cyan, .blue, .purple, .red], center: .center))
                        .frame(width: 22, height: 22).overlay(Circle().strokeBorder(Color.primary.opacity(0.15), lineWidth: 0.5))
                }.buttonStyle(.plain).help("Any colour").accessibilityLabel("Choose any colour")
            }
        }
    }
}

struct ClimateControls: View {
    @ObservedObject var store: Store
    let device: Device
    var body: some View {
        let on = store.bool(device, "power")
        let spec = device.capabilities["temperature"]!
        let target = store.int(device, "temperature") ?? spec.min
        HStack {
            GlyphButton(symbol: "minus", size: 34, label: "Cooler") { store.set(device, ["temperature": max(spec.min, target - 1)]) }
            Spacer()
            VStack(spacing: 0) {
                Text("\(target)°").font(.system(size: 44, weight: .light, design: .rounded)).monospacedDigit()
                    .foregroundStyle(on ? Color.primary : Color.secondary).contentTransition(.numericText())
                if let room = store.value(device, "current_temperature") as? Int {
                    Text("Room \(room)°").font(.system(size: 11.5)).foregroundStyle(.secondary)
                }
            }
            Spacer()
            GlyphButton(symbol: "plus", size: 34, label: "Warmer") { store.set(device, ["temperature": min(spec.max, target + 1)]) }
        }
        .padding(.vertical, 2)
        if let speeds = device.capabilities["fan"]?.options, !speeds.isEmpty {
            let current = store.string(device, "fan")
            let levels = speeds.filter { $0 != "auto" }
            HStack(spacing: 8) {
                if !levels.isEmpty {
                    StepSlider(steps: levels.count, level: (levels.firstIndex(of: current ?? "") ?? -1) + 1,
                               symbol: "fan.fill", label: "Fan speed") {
                        store.set(device, ["fan": levels[$0 - 1]], after: .milliseconds(120))
                    }
                }
                if speeds.contains("auto") {
                    GlyphButton(symbol: Symbols.exists("fan.badge.automatic.fill") ? "fan.badge.automatic.fill" : "a.circle.fill",
                                active: current == "auto", size: 30, label: "Automatic fan speed") { store.set(device, ["fan": "auto"]) }
                }
            }
        }
        let modes = ["eco", "sleep"].filter { device.has($0) }
        if !modes.isEmpty || !device.actions.isEmpty {
            HStack(spacing: 8) {
                ForEach(modes, id: \.self) { key in
                    GlyphButton(symbol: key == "eco" ? "leaf.fill" : "moon.fill", active: store.bool(device, key),
                                tint: key == "eco" ? .green : .indigo, size: 30, label: key.capitalized) {
                        store.set(device, [key: !store.bool(device, key)])
                    }
                }
                ActionButtons(store: store, device: device, actions: device.actions, size: 30)
            }
        }
    }
}

/// A setting with named options (a mode, an effect): the system's own pop-up button.
struct ChoiceRow: View {
    @ObservedObject var store: Store
    let device: Device
    let key: String
    var body: some View {
        let options = device.capabilities[key]?.options ?? []
        HStack {
            Text(key.replacingOccurrences(of: "_", with: " ").capitalized).font(.system(size: 12))
            Spacer()
            Picker("", selection: Binding(get: { store.string(device, key) ?? "" }, set: { store.set(device, [key: $0]) })) {
                if !options.contains(store.string(device, key) ?? "") { Text("None").tag(store.string(device, key) ?? "") }
                ForEach(options, id: \.self) { Text($0.replacingOccurrences(of: "-", with: " ").replacingOccurrences(of: "_", with: " ").capitalized).tag($0) }
            }
            .labelsHidden().pickerStyle(.menu).controlSize(.small).fixedSize()
            .accessibilityLabel(key.capitalized)
        }
    }
}

/// A number with a range: the system slider, with its name.
struct DialRow: View {
    @ObservedObject var store: Store
    let device: Device
    let key: String
    var body: some View {
        let spec = device.capabilities[key]!
        HStack(spacing: 10) {
            Text(key.replacingOccurrences(of: "_", with: " ").capitalized).font(.system(size: 12))
            Slider(value: Binding(get: { Double(store.int(device, key) ?? spec.min) },
                                  set: { store.set(device, [key: Int($0.rounded())], after: .milliseconds(120)) }),
                   in: Double(spec.min)...Double(max(spec.max, spec.min + 1)))
                .controlSize(.small).accessibilityLabel(key.capitalized)
        }
    }
}

struct ActionButtons: View {
    @ObservedObject var store: Store
    let device: Device
    let actions: [String]
    var size: CGFloat = 26
    var body: some View {
        HStack(spacing: 8) {
            ForEach(actions, id: \.self) { action in
                let label = action.replacingOccurrences(of: "_", with: " ").capitalized
                if let symbol = Symbols.action(action) {
                    GlyphButton(symbol: symbol, size: size, label: label) { store.act(device, action) }
                } else {
                    Button(label) { store.act(device, action) }.controlSize(.small)
                }
            }
            Spacer(minLength: 0)
        }
    }
}

// MARK: who wants you

/// Shown only while a cue is holding the lights: who wants you, and a way to dismiss it.
struct CueBanner: View {
    @ObservedObject var store: Store
    let cue: Cue
    var body: some View {
        let color = cueColor(cue.name)
        HStack(spacing: 8) {
            Image(systemName: cueSymbol(cue.name)).font(.system(size: 15, weight: .medium)).foregroundStyle(color)
            VStack(alignment: .leading, spacing: 1) {
                Text(headline).font(.system(size: 12.5, weight: .semibold)).lineLimit(1)
                if let message = cue.message { Text(message).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2) }
            }
            Spacer(minLength: 4)
            Button { store.clear() } label: {
                Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).foregroundStyle(.secondary)
                    .frame(width: 20, height: 20).background(Circle().fill(Color.primary.opacity(0.1)))
            }.buttonStyle(.plain).help("Dismiss, and put the lights back").accessibilityLabel("Dismiss the cue")
        }
        .padding(.horizontal, 9).padding(.vertical, 7).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(color.opacity(0.16)))
    }

    private var headline: String {
        let named = cue.source?.first?.isUppercase == true
        switch cue.name {
        case "attention": return named ? "\(cue.title) needs you" : "Something needs you"
        case "working": return named ? "\(cue.title) is working" : "Working"
        default: return cue.title
        }
    }
}

/// The cue that just went by: who it was and what they said, for a glance after the lights have gone back.
struct LastCue: View {
    let cue: Cue
    var body: some View {
        let minutes = Int(-cue.time.timeIntervalSinceNow / 60)
        HStack(spacing: 6) {
            Image(systemName: cue.skipped == nil ? cueSymbol(cue.name) : "bell.slash.fill").font(.system(size: 11))
                .foregroundStyle(cue.skipped == nil ? cueColor(cue.name) : Color.secondary)
                .help(cue.skipped.map { "Arrived while cues were \($0)" } ?? cue.name.capitalized)
            Text(cue.title).fontWeight(.medium).foregroundStyle(.primary).lineLimit(1).layoutPriority(1)
            if let message = cue.message, message != cue.title { Text(message).lineLimit(1).truncationMode(.tail) }
            Spacer(minLength: 4)
            Text(minutes < 1 ? "now" : "\(minutes) min").monospacedDigit().layoutPriority(1)
        }
        .font(.system(size: 11.5)).foregroundStyle(.secondary).padding(.horizontal, 6)
    }
}

/// An app in the cloud asking to be let in. Its sign-in page shows the same code.
struct SignInRow: View {
    @ObservedObject var store: Store
    let signIn: SignIn
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("\(signIn.name) wants to connect").font(.system(size: 12.5, weight: .semibold)).lineLimit(1)
            HStack(spacing: 6) {
                Text("Code \(String(signIn.code.prefix(3))) \(String(signIn.code.dropFirst(3)))").font(.system(size: 11.5)).monospacedDigit()
                    .foregroundStyle(.secondary).help("The same code is on its sign-in page. It returns to \(signIn.returnsTo).")
                Spacer(minLength: 4)
                Button("Deny") { store.answer(signIn, allow: false) }.controlSize(.small)
                Button("Allow") { store.answer(signIn, allow: true) }.controlSize(.small).buttonStyle(.borderedProminent)
            }
        }
        .padding(.horizontal, 9).padding(.vertical, 8).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.accentColor.opacity(0.14)))
    }
}

// MARK: the header: a name, a bell, and everything occasional

struct Header: View {
    @ObservedObject var store: Store
    var body: some View {
        HStack(spacing: 10) {
            Text("Glimt").font(.system(size: 13, weight: .semibold))
            Spacer()
            MuteButton(store: store)
            MoreMenu(store: store)
        }
        .padding(.horizontal, 6).frame(height: 24)
    }
}

/// Things you set once or look at rarely live here, not in the panel.
struct MoreMenu: View {
    @ObservedObject var store: Store
    var body: some View {
        Menu {
            if !store.recent.isEmpty {
                Menu("Recent Cues") {
                    ForEach(store.recent) { cue in
                        Text("\(cue.time.formatted(date: .omitted, time: .shortened))  \(cue.title)"
                             + (cue.message.map { ": \($0)" } ?? "") + (cue.skipped.map { " (\($0))" } ?? ""))
                    }
                }
            }
            Menu("Send a Test Cue") {
                ForEach(cueColors, id: \.name) { cue in
                    Button(cue.name.prefix(1).uppercased() + cue.name.dropFirst()) { store.signal(cue.name) }
                }
            }
            Divider()
            let agents = store.agents.filter { $0.found || $0.hooks }
            if !agents.isEmpty {
                Menu("Agents That Cue You") {
                    ForEach(agents) { agent in
                        Toggle(agent.name, isOn: Binding(get: { agent.hooks }, set: { store.setAgent(agent, connected: $0) }))
                            .disabled(store.busy != nil)
                    }
                }
            }
            if !store.remoteApps.isEmpty {
                Menu("Connected Apps") {
                    ForEach(store.remoteApps) { app in
                        Menu(app.name) { Button("Disconnect") { store.disconnect(app) } }
                    }
                }
            }
            Button("Add a Device…") { store.openInTerminal(["setup"]) }
            Button("Check Setup…") { store.openInTerminal(["doctor"]) }
            Button("Open Dashboard") { store.openDashboard() }
            Divider()
            Button("Restart Glimt") { store.restartDaemon() }
            Button("Quit Glimt Menu") { NSApplication.shared.terminate(nil) }
        } label: { Image(systemName: "ellipsis.circle").font(.system(size: 13)) }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().accessibilityLabel("More")
    }
}

/// A bell. Tap to silence cues for a while; tap again to hear from your agents.
struct MuteButton: View {
    @ObservedObject var store: Store
    var body: some View {
        if store.isMuted {
            Button { store.unmute() } label: {
                Image(systemName: "bell.slash.fill").font(.system(size: 11, weight: .semibold)).foregroundStyle(.white)
                    .frame(width: 22, height: 22).background(Circle().fill(Color.orange))
            }
            .buttonStyle(.plain).accessibilityLabel("Turn cues back on")
            .help(muted(until: store.muteUntil))
        } else {
            Menu {
                Button("Mute for 1 Hour") { store.mute(minutes: 60) }
                Button("Mute Until Morning") { store.mute(minutes: minutesUntilMorning()) }
                Button("Mute Until I Turn It Back On") { store.mute(minutes: nil) }
            } label: { Image(systemName: "bell").font(.system(size: 12.5)) }
                .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                .accessibilityLabel("Mute cues").help("Mute cues")
        }
    }

    private func muted(until: Date?) -> String {
        guard let until, until.timeIntervalSinceNow < 86_400 * 365 else { return "Cues are muted. Click to turn them back on." }
        return "Cues are muted until \(until.formatted(date: .omitted, time: .shortened)). Click to turn them back on."
    }

    private func minutesUntilMorning() -> Int {
        let calendar = Calendar.current
        let morning = calendar.nextDate(after: Date(), matching: DateComponents(hour: 8, minute: 0), matchingPolicy: .nextTime) ?? Date().addingTimeInterval(8 * 3600)
        return max(1, Int(morning.timeIntervalSinceNow / 60))
    }
}

/// What to show instead of devices: a sentence and the one button that fixes it.
struct Prompt: View {
    let symbol: String
    let text: String
    let button: String
    let action: () -> Void
    var body: some View {
        VStack(spacing: 9) {
            Image(systemName: symbol).font(.system(size: 22, weight: .light)).foregroundStyle(.secondary)
            Text(text).font(.system(size: 12)).foregroundStyle(.secondary).multilineTextAlignment(.center)
            Button(button, action: action).controlSize(.small)
        }.frame(maxWidth: .infinity).padding(.vertical, 14)
    }
}

// MARK: the panel

/// The room at a glance: who wants you, then a tile per device.
struct Overview: View {
    @ObservedObject var store: Store
    var body: some View {
        let held = store.activeCue.flatMap { $0.held ? $0 : nil }
        // The cue that just went by stays for half an hour; older ones are in the menu.
        let last = store.recent.first { $0.id != held?.id && $0.time.timeIntervalSinceNow > -1800 }
        let tiles = store.tiles
        VStack(alignment: .leading, spacing: 8) {
            Header(store: store)
            ForEach(store.signIns) { SignInRow(store: store, signIn: $0) }
            if let held { CueBanner(store: store, cue: held) } else if let last { LastCue(cue: last) }
            if tiles.count > 16 {  // a big home scrolls instead of running off the screen
                ScrollView { TileGrid(store: store, tiles: tiles) }.frame(height: 8 * 56)
            } else if !tiles.isEmpty {
                TileGrid(store: store, tiles: tiles)
            }
            if !store.connected && store.devices.isEmpty {
                Prompt(symbol: "moon.zzz", text: "Glimt isn't running.", button: "Start Glimt") { store.startDaemon() }
            } else if !store.connected {
                // Devices are on screen from before, but nothing is listening behind them.
                HStack(spacing: 6) {
                    Text("Glimt stopped. These may be out of date.").foregroundStyle(.secondary)
                    Spacer(minLength: 4)
                    Button("Start") { store.startDaemon() }.controlSize(.small)
                }.font(.system(size: 11)).padding(.horizontal, 6)
            } else if store.devices.isEmpty {
                Prompt(symbol: "lightbulb", text: "No devices yet. Setup finds your lights\nand asks which to add.", button: "Set Up Devices…") {
                    store.openInTerminal(["setup"])
                }
            }
            if let note = store.failure ?? store.hint {
                Text(note).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2).padding(.horizontal, 6)
            }
        }
    }
}

struct Panel: View {
    @ObservedObject var store: Store
    var body: some View {
        Group {
            if let device = store.devices.first(where: { $0.id == store.detail }) {
                DeviceDetail(store: store, device: device)
            } else if case .group(let kind, let devices)? = store.tiles.first(where: { $0.id == store.detail }) {
                GroupDetail(store: store, kind: kind, devices: devices)
            } else {
                Overview(store: store)
            }
        }
        .padding(10).frame(width: 296)
    }
}
