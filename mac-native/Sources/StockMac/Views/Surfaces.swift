import SwiftUI
import AppKit

// ============================================================================
//  Surfaces.swift — THE single source of truth for every PANEL / CARD / POPUP /
//  INPUT surface in the app (the rounded "blocks", not the pill buttons).
//
//  Tune the look HERE and nowhere else. Call sites only pick a modifier
//  (.glassCard(), .cardSurface(), .popupChrome(), .fieldWell(), …).
//
//  Subgroups:
//    0.  Tokens               — radius / tint / fills / paddings
//    A.  Panels & cards       — GlassCard, CardSurface, ToolbarSurface, SystemMaterial
//    B.  Popups               — PopupChrome
//    C.  Inputs               — FieldWell, SearchWell
//    D.  Effects              — TopFade
//    E.  HPhotoStrip          — NSScrollView-backed horizontal photo carousel
// ============================================================================


// ── 0. Tokens ───────────────────────────────────────────────────────────────

enum SurfaceKit {
    static let cardRadius:  CGFloat = 16
    static let popupRadius: CGFloat = 22
    static let glassTint  = Color.black.opacity(0.50)
    static let wellFill   = Color.white.opacity(0.10)
    static let wellBorder = Color.white.opacity(0.10)
    static let wellPadH:  CGFloat = 12
    static let wellPadV:  CGFloat = 6
}


// ── A. Panels & cards ───────────────────────────────────────────────────────

struct GlassCard: ViewModifier {
    @Environment(AppModel.self) private var model
    func body(content: Content) -> some View {
        let s = model.surfaces
        content.surfaceMat(s.mat(.filterPanel, isDark: model.isDark),
                           interactive: s.interactive(.filterPanel),
                           radius: s.filterPanelRadius)
    }
}

struct CardSurface: ViewModifier {
    @Environment(AppModel.self) private var model
    func body(content: Content) -> some View {
        let s = model.surfaces
        content.surfaceMat(s.mat(.cards, isDark: model.isDark),
                           interactive: s.interactive(.cards),
                           radius: s.cardRadius)
    }
}

struct BrowserCard: ViewModifier {
    @Environment(AppModel.self) private var model
    func body(content: Content) -> some View {
        let s = model.surfaces
        content
            .surfaceMat(s.mat(.browserPanel, isDark: model.isDark),
                        interactive: s.interactive(.browserPanel),
                        radius: s.browserPanelRadius)
            // Clip inner content (log text, inspector field) to the same radius so
            // the corners are actually rounded, not just the glass background.
            .clipShape(RoundedRectangle(cornerRadius: s.browserPanelRadius, style: .continuous))
    }
}

struct ToolbarSurface: ViewModifier {
    var radius: CGFloat = SurfaceKit.cardRadius
    func body(content: Content) -> some View {
        content.background(SystemMaterial(material: .headerView, radius: radius))
    }
}

struct SystemMaterial: NSViewRepresentable {
    var material: NSVisualEffectView.Material = .menu
    var radius: CGFloat = SurfaceKit.cardRadius
    var alpha: CGFloat = 1.0

    func makeNSView(context: Context) -> NSVisualEffectView {
        let v = NSVisualEffectView()
        v.material = material
        v.blendingMode = .withinWindow
        v.state = .active
        v.alphaValue = alpha
        v.wantsLayer = true
        v.layer?.cornerRadius = radius
        v.layer?.masksToBounds = true
        v.layer?.cornerCurve = .continuous
        return v
    }
    func updateNSView(_ v: NSVisualEffectView, context: Context) {
        v.material = material
        v.alphaValue = alpha
        v.layer?.cornerRadius = radius
    }
}


// ── B-D. PopupChrome, FieldWell, TopFade — defined in Components.swift ──────


// ── E. HPhotoStrip ──────────────────────────────────────────────────────────
// NSScrollView-backed horizontal photo carousel for GroupCard.
// Shows 2 photos at a time; progress binding drives the SwiftUI glass indicator.

struct HPhotoStrip: NSViewRepresentable {
    let photos: [GroupPhoto]
    let width:  CGFloat
    let height: CGFloat
    @Binding var progress: CGFloat

    func makeNSView(context: Context) -> NSScrollView {
        let sv = NSScrollView()
        sv.hasHorizontalScroller = false
        sv.hasVerticalScroller   = false
        sv.horizontalScrollElasticity = .allowed
        sv.verticalScrollElasticity   = .none
        sv.drawsBackground = false
        // boundsDidChange fires on EVERY scroll event (trackpad, wheel, any input)
        sv.contentView.postsBoundsChangedNotifications = true

        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = 2
        stack.translatesAutoresizingMaskIntoConstraints = false
        sv.documentView = stack
        context.coordinator.sv = sv
        context.coordinator.stack = stack
        context.coordinator.bind($progress)
        buildThumbs(stack)

        NotificationCenter.default.addObserver(
            context.coordinator,
            selector: #selector(Coordinator.boundsChanged(_:)),
            name: NSView.boundsDidChangeNotification,
            object: sv.contentView)
        return sv
    }

    func updateNSView(_ sv: NSScrollView, context: Context) {
        context.coordinator.bind($progress)
        guard let stack = context.coordinator.stack else { return }
        buildThumbs(stack)
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator {
        weak var sv: NSScrollView?
        weak var stack: NSStackView?
        private var setProgress: ((CGFloat) -> Void)?

        func bind(_ b: Binding<CGFloat>) {
            setProgress = { b.wrappedValue = $0 }
        }

        @objc func boundsChanged(_ n: Notification) {
            guard let sv, let stack else { return }
            let docW = stack.frame.width
            let visW = sv.bounds.width
            let travel = docW - visW
            guard travel > 1 else { return }
            let raw = sv.contentView.bounds.origin.x / travel
            let p = max(0, min(1, raw))
            DispatchQueue.main.async { self.setProgress?(p) }
        }
    }

    private func buildThumbs(_ stack: NSStackView) {
        stack.subviews.forEach { $0.removeFromSuperview() }
        if photos.isEmpty {
            let ph = NSView()
            ph.wantsLayer = true
            ph.layer?.backgroundColor = NSColor.quaternaryLabelColor.cgColor
            ph.widthAnchor.constraint(equalToConstant: width).isActive = true
            ph.heightAnchor.constraint(equalToConstant: height).isActive = true
            stack.addArrangedSubview(ph)
            return
        }
        let thumbW = width / 2
        for ph in Array(photos.prefix(6)) {
            let img = ThumbImageView(assetID: ph.asset_id, fallback: ph.thumb,
                                     width: thumbW, height: height)
            img.widthAnchor.constraint(equalToConstant: thumbW).isActive = true
            img.heightAnchor.constraint(equalToConstant: height).isActive = true
            stack.addArrangedSubview(img)
        }
        stack.layoutSubtreeIfNeeded()
    }
}

/// Thin glass scroll progress bar shown at the bottom of GroupCard photo strip.
struct GlassScrollBar: View {
    let progress: CGFloat
    let total: Int                      // total photos (≤6)
    private let visible = 2

    private var thumbFrac: CGFloat { CGFloat(visible) / CGFloat(max(total, visible)) }

    var body: some View {
        GeometryReader { geo in
            let trackW  = geo.size.width
            let thumbW  = trackW * thumbFrac
            let travel  = trackW - thumbW
            ZStack(alignment: .leading) {
                Capsule().fill(Color.black.opacity(0.35))
                    .overlay(Capsule().strokeBorder(Color.white.opacity(0.10), lineWidth: 0.5))
                Capsule()
                    .glassEffect(.clear.tint(Color(red: 0.04, green: 0.52, blue: 1.0).opacity(0.65)).interactive(),
                                 in: Capsule())
                    .frame(width: max(thumbW, 12))
                    .offset(x: travel * progress)
                    .animation(.interactiveSpring(response: 0.22, dampingFraction: 0.7), value: progress)
            }
            .frame(height: 4)
        }
        .frame(height: 4)
    }
}

/// Simple AppKit image view that loads from img_cache or remote URL.
private final class ThumbImageView: NSImageView {
    private var task: URLSessionDataTask?

    init(assetID: String, fallback: String?, width: CGFloat, height: CGFloat) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: height))
        translatesAutoresizingMaskIntoConstraints = false
        imageScaling = .scaleProportionallyUpOrDown
        imageAlignment = .alignCenter
        wantsLayer = true
        layer?.backgroundColor = NSColor.tertiaryLabelColor.withAlphaComponent(0.15).cgColor

        // Try local cache first
        let cacheURL = URL(string: "http://localhost:8000/img/cache/\(assetID)")!
        load(url: cacheURL)
    }

    required init?(coder: NSCoder) { fatalError() }

    private func load(url: URL) {
        task?.cancel()
        task = URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let data, let img = NSImage(data: data) else { return }
            DispatchQueue.main.async { self?.image = img }
        }
        task?.resume()
    }
}


// ── Modifier sugar ──────────────────────────────────────────────────────────

extension View {
    func glassCard() -> some View { modifier(GlassCard()) }
    func cardSurface() -> some View { modifier(CardSurface()) }
    func browserCard() -> some View { modifier(BrowserCard()) }
    func toolbarSurface(_ radius: CGFloat = SurfaceKit.cardRadius) -> some View { modifier(ToolbarSurface(radius: radius)) }
    func glassPill(active: Bool = false) -> some View { modifier(PillSurface(active: active)) }
    func searchWell() -> some View { modifier(SearchWell()) }
}

struct SearchWell: ViewModifier {
    func body(content: Content) -> some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass").font(.system(size: 12)).foregroundStyle(.secondary)
            content
        }
        .padding(.horizontal, 10).padding(.vertical, 6)
        .background(.regularMaterial, in: .rect(cornerRadius: 9))
    }
}
