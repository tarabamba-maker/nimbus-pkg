import SwiftUI
import AppKit

@main
struct StockMacApp: App {
    @State private var model = AppModel()
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .frame(minWidth: 980, minHeight: 640)
                .task { await model.boot() }
        }
        .windowStyle(.hiddenTitleBar)
        .windowToolbarStyle(.unified(showsTitle: false))
        .defaultSize(width: 1280, height: 820)
    }
}

/// Tears the Python backend down when the app quits.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated { Backend.shared.stop() }
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        adjustWindowButtons()
    }
    private func adjustWindowButtons() {
        guard let window = NSApp.windows.first else { return }
        let buttons: [NSWindow.ButtonType] = [.closeButton, .miniaturizeButton, .zoomButton]
        // Move traffic lights down and right for more breathing room from edges
        let offsetX: CGFloat = 18
        let offsetY: CGFloat = -8   // negative = move down in flipped coords
        for type in buttons {
            guard let btn = window.standardWindowButton(type) else { continue }
            var f = btn.frame
            f.origin.x = offsetX + (f.origin.x - (window.standardWindowButton(.closeButton)?.frame.origin.x ?? f.origin.x))
            btn.setFrameOrigin(NSPoint(x: f.origin.x, y: btn.frame.origin.y + offsetY))
        }
        // Re-anchor close button first, then space others relative to it
        let btns = buttons.compactMap { window.standardWindowButton($0) }
        if let first = btns.first {
            var x = offsetX
            for btn in btns {
                var f = btn.frame
                f.origin.x = x
                f.origin.y = first.superview!.frame.height - offsetX - f.height
                btn.setFrameOrigin(f.origin)
                x += f.width + 6
            }
        }
    }
}
