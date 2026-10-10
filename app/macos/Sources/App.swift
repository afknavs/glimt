import Combine
import SwiftUI

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    private var status: NSStatusItem!
    private let popover = NSPopover()
    private var watch: AnyCancellable?
    private var window: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        Store.shared.start()
        status = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        status.button?.target = self
        status.button?.action = #selector(toggle)
        status.button?.toolTip = "Glimt"
        let host = NSHostingController(rootView: Panel(store: Store.shared))
        host.sizingOptions = [.preferredContentSize]
        popover.contentViewController = host
        popover.behavior = .transient
        // The menu bar bulb fills with the colour of a held cue, so a glance tells you an agent is waiting.
        watch = Store.shared.$activeCue.receive(on: RunLoop.main).sink { [weak self] cue in self?.paint(cue) }
        paint(nil)
        if let index = CommandLine.arguments.firstIndex(of: "--detail"), CommandLine.arguments.count > index + 1 {
            Store.shared.detail = CommandLine.arguments[index + 1]   // open on one device (for screenshots)
        }
        if CommandLine.arguments.contains("--window") { showWindow() }
        if let index = CommandLine.arguments.firstIndex(of: "--snapshot"), CommandLine.arguments.count > index + 1 {
            snapshot(to: CommandLine.arguments[index + 1], dark: !CommandLine.arguments.contains("--light"))
        }
    }

    /// Render the panel to a PNG and quit. Used for documentation screenshots and UI checks.
    private func snapshot(to path: String, dark: Bool) {
        NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        showWindow()
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in   // long enough for agents to load
            guard let view = self?.window?.contentView, let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { exit(1) }
            view.cacheDisplay(in: view.bounds, to: bitmap)
            try? bitmap.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
            NSApp.terminate(nil)
        }
    }

    private func paint(_ cue: Cue?) {
        guard let image = NSImage(systemSymbolName: cue == nil ? "lightbulb" : "lightbulb.fill", accessibilityDescription: "Glimt") else { return }
        if let cue, cueColors.contains(where: { $0.name == cue.name }) {
            let tinted = image.withSymbolConfiguration(.init(paletteColors: [NSColor(cueColor(cue.name))])) ?? image
            tinted.isTemplate = false
            status.button?.image = tinted
        } else {
            image.isTemplate = true
            status.button?.image = image
        }
    }

    @objc private func toggle() {
        guard let button = status.button else { return }
        if popover.isShown {
            popover.performClose(nil)
        } else {
            Store.shared.detail = nil                      // always open on the room, not where you last were
            Task { await Store.shared.refreshAgents() }   // someone may have run `glimt connect` since
            NSApp.activate(ignoringOtherApps: true)
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            popover.contentViewController?.view.window?.makeKey()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return false
    }

    /// The same panel in a normal window (launch with --window, or open the app twice).
    private func showWindow() {
        if window == nil {
            let host = NSHostingController(rootView: Panel(store: Store.shared).background(Color(nsColor: .windowBackgroundColor)))
            host.sizingOptions = [.preferredContentSize]
            let created = NSWindow(contentViewController: host)
            created.styleMask = [.titled, .closable, .fullSizeContentView]
            created.title = "Glimt"
            created.titlebarAppearsTransparent = true
            created.isReleasedWhenClosed = false
            created.center()
            window = created
        }
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}

@main struct GlimtMenu: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    var body: some Scene { Settings { EmptyView() } }
}
