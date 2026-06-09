import SwiftUI
import AppKit

// ─────────────────────────────────────────────────────────────
//  MaterialsLab — повний редактор теми.
//  Всі зміни ЖИВІ (@Observable → auto-rerender).
//  "Save" → зберігає в recipes/surface_theme.json.
//  Вмикається: Settings → Show Materials Lab button (🖌 у TopBar).
// ─────────────────────────────────────────────────────────────

struct MaterialsLab: View {
    var onClose: () -> Void = {}
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var s = model.surfaces
        let dk = model.isDark

        VStack(spacing: 0) {
            header(s: s)
            Divider().opacity(0.15)

            ScrollView {
                VStack(alignment: .leading, spacing: 0) {

                    // ────────────────────────────────────────
                    //  FOUNDATION
                    // ────────────────────────────────────────
                    grp("FOUNDATION") {
                        colorRow("Accent color") {
                            ColorPicker("", selection: Binding(
                                get: { Color(hex: s.accentHex) ?? Theme.accent },
                                set: { s.accentHex = $0.hexString; s.save() }
                            )).labelsHidden()
                        }
                        togRow("Glass interactive (refraction)", val: $s.glassInteractive, s: s)
                        slRow("Inactive window dim",  val: $s.inactiveDim,  r: 0...0.8,  fmt: pct, s: s)
                    }

                    // ────────────────────────────────────────
                    //  WINDOW & LAYOUT (spacing)
                    // ────────────────────────────────────────
                    grp("ВІКНО / LAYOUT — відступи") {
                        slRow("Padding горизонт.",   val: cgBinding($s.contentPaddingH), r: 0...60,  fmt: pt, s: s, def: 20)
                        slRow("Padding вертикал.",   val: cgBinding($s.contentPaddingV), r: 0...60,  fmt: pt, s: s, def: 20)
                        slRow("Відстань між картками", val: cgBinding($s.cardSpacing),   r: 2...30,  fmt: pt, s: s, def: 10)
                        slRow("Висота фільтр панелі", val: cgBinding($s.panelH),         r: 36...80, fmt: pt, s: s, def: 52)
                        slRow("Відстань між стат.",  val: cgBinding($s.statsSpacing),    r: 4...24,  fmt: pt, s: s, def: 10)
                    }

                    // ────────────────────────────────────────
                    //  POSITION (зсув елементів X / Y)
                    // ────────────────────────────────────────
                    grp("ПОЗИЦІЯ ЕЛЕМЕНТІВ  (зсув X / Y · 2× клік або ⌖ = центр)") {
                        HStack {
                            Text("Подвійний клік по назві = скинути на дефолт")
                                .font(.system(size: 10)).foregroundStyle(.secondary.opacity(0.5))
                            Spacer()
                            Button { s.centerAllOffsets() } label: {
                                Label("Центрувати все", systemImage: "scope")
                                    .font(.system(size: 11, weight: .semibold)).foregroundStyle(.white)
                            }.buttonStyle(.pill(.primary))
                        }.frame(height: 30)
                        posRow("Top bar",      slot: .topBar,        s: s)
                        posRow("  ↳ Title",    slot: .topBarTitle,   s: s)
                        posRow("  ↳ Buttons",  slot: .topBarButtons, s: s)
                        posRow("Stats row",    slot: .statsRow,      s: s)
                        posRow("Tab bar",      slot: .tabBarRow,     s: s)
                        posRow("Content",      slot: .content,       s: s)
                    }

                    // ────────────────────────────────────────
                    //  SIZES (розмір карток)
                    // ────────────────────────────────────────
                    grp("РОЗМІРИ  (2× клік по назві = дефолт)") {
                        slRow("Заголовок (font)",               val: s.sizeBinding("titleSize", 16),   r: 11...28,   fmt: pt, s: s, def: 16)
                        slRow("Картка ширина (Downloads/Best)", val: s.sizeBinding("cardW", 168),   r: 120...260, fmt: pt, s: s, def: 168)
                        slRow("Картка фото висота",             val: s.sizeBinding("cardImgH", 112), r: 70...200,  fmt: pt, s: s, def: 112)
                        slRow("Group картка ширина",            val: s.sizeBinding("groupCardW", 188), r: 140...300, fmt: pt, s: s, def: 188)
                        slRow("Group фото висота",              val: s.sizeBinding("groupImgH", 110),  r: 70...200,  fmt: pt, s: s, def: 110)
                    }

                    // ────────────────────────────────────────
                    //  APP BACKGROUND
                    // ────────────────────────────────────────
                    grp("APP BACKGROUND  (матеріал фону вікна)") {
                        matRow("Матеріал фону", slot: .appBackground, dk: dk, s: s)
                        note("Власна картинка: Налаштування → Import background")
                        note("Без картинки — Liquid Glass градієнт (тема)")
                    }

                    // ────────────────────────────────────────
                    //  TOP BAR
                    // ────────────────────────────────────────
                    grp("TOP BAR  (заголовок + кнопки)") {
                        matRow("Матеріал", slot: .topBar, dk: dk, s: s)
                        refrRow(.topBar, s: s)
                        slRow("Radius", val: cgBinding($s.topBarRadius), r: 0...40, fmt: pt, s: s, def: 22)
                    }

                    // ────────────────────────────────────────
                    //  STATS ROW
                    // ────────────────────────────────────────
                    grp("STATS BLOCKS  (Today / Week / Month / Year)") {
                        matRow("Матеріал", slot: .statsBlock, dk: dk, s: s)
                        refrRow(.statsBlock, s: s)
                        slRow("Radius", val: cgBinding($s.statsRadius), r: 4...32, fmt: pt, s: s, def: 16)
                    }

                    // ────────────────────────────────────────
                    //  TAB BAR
                    // ────────────────────────────────────────
                    grp("TAB BAR  (Downloads / Best Sellers / Groups / Browser)") {
                        matRow("Container", slot: .tabBar, dk: dk, s: s)
                        refrRow(.tabBar, s: s)
                        slRow("Radius",         val: cgBinding($s.tabBarRadius), r: 4...999, fmt: pt, s: s, def: 999)
                        slRow("Tint selection", val: $s.pillSelTint, r: 0...1, fmt: pct, s: s)
                    }

                    // ────────────────────────────────────────
                    //  SCROLL PANEL BACKGROUND
                    // ────────────────────────────────────────
                    grp("SCROLL PANEL BACKGROUND  (підкладка всього полотна)") {
                        matRow("Матеріал", slot: .scrollPanelBg, dk: dk, s: s)
                        refrRow(.scrollPanelBg, s: s)
                        slRow("Radius", val: cgBinding($s.scrollPanelRadius), r: 4...40, fmt: pt, s: s, def: 22)
                    }

                    // ────────────────────────────────────────
                    //  FILTER PANEL (floating controls bar)
                    // ────────────────────────────────────────
                    grp("FILTER PANEL  (плаваюча панель зі стоками / сортуванням)") {
                        matRow("Матеріал", slot: .filterPanel, dk: dk, s: s)
                        refrRow(.filterPanel, s: s)
                        slRow("Radius", val: cgBinding($s.filterPanelRadius), r: 4...40, fmt: pt, s: s, def: 22)
                        fadeRow("Fade полотна ЗВЕРХУ (під панеллю)",
                                config: dk ? $s.underlayFade : $s.underlayFadeLight, s: s)
                        fadeRow("Fade полотна ЗНИЗУ (нижній край вікна)",
                                config: dk ? $s.canvasBottomFade : $s.canvasBottomFadeLight, s: s)
                    }

                    // ────────────────────────────────────────
                    //  CONTENT CARDS
                    // ────────────────────────────────────────
                    grp("CONTENT CARDS  (SaleCard / TopPhotoCard / GroupCard)") {
                        matRow("Матеріал картки",      slot: .cards,      dk: dk, s: s)
                        refrRow(.cards, s: s)
                        slRow("Card radius",           val: cgBinding($s.cardRadius), r: 0...32, fmt: pt, s: s, def: 16)
                        matRow("Info section (знизу)", slot: .cardInfoBg, dk: dk, s: s)
                        slRow("Info radius",           val: cgBinding($s.cardInfoRadius), r: 0...32, fmt: pt, s: s, def: 0)
                    }

                    // ────────────────────────────────────────
                    //  TEXT COLORS
                    // ────────────────────────────────────────
                    grp("ТЕКСТ  (колір + alpha · \(dk ? "DARK" : "LIGHT") тема)") {
                        textColorRow("Primary колір",   level: 1, dk: dk, s: s)
                        slRow("Primary alpha",          val: $s.t1Alpha, r: 0.3...1.0, fmt: pct, s: s, def: 1.0)
                        textColorRow("Secondary колір", level: 2, dk: dk, s: s)
                        slRow("Secondary alpha",        val: $s.t2Alpha, r: 0.1...1.0, fmt: pct, s: s, def: 0.72)
                        textColorRow("Tertiary колір",  level: 3, dk: dk, s: s)
                        slRow("Tertiary alpha",         val: $s.t3Alpha, r: 0.1...1.0, fmt: pct, s: s, def: 0.5)
                        note("Колір окремо для DARK і LIGHT — перемкни тему вгорі (🌙/☀️)")
                    }

                    // ────────────────────────────────────────
                    //  PILLS & BUTTONS
                    // ────────────────────────────────────────
                    grp("BUTTONS / PILLS  (всі кнопки + сегменти в контейнерах)") {
                        colorRow("Активний колір пілза") {
                            ColorPicker("", selection: Binding(
                                get: { Color(hex: s.pillColorHex) ?? s.accent },
                                set: { s.pillColorHex = $0.hexString; s.save() }
                            )).labelsHidden()
                        }
                        colorRow("Колір тексту (\(dk ? "DARK" : "LIGHT"))") {
                            ColorPicker("", selection: s.pillTextBinding(isDark: dk)).labelsHidden()
                        }
                        slRow("Радіус округлення",      val: cgBinding($s.pillRadius), r: 4...999, fmt: pt, s: s, def: 999)
                        slRow("Inactive fill alpha",   val: $s.pillFillAlpha,   r: 0...0.8,  fmt: pct, s: s)
                        slRow("Inactive border alpha", val: $s.pillBorderAlpha, r: 0...0.4,  fmt: pct, s: s)
                        slRow("Active tint",           val: $s.pillSelTint,     r: 0.1...1.0, fmt: pct, s: s)
                        slRow("Розмір (padding ×)",    val: $s.pillPadScale,    r: 0.5...2.0, fmt: { String(format: "%.2f×", $0) }, s: s)
                        HStack(spacing: 6) {
                            Text("Позиція сорт-пілзів").font(.system(size: 12)).foregroundStyle(.primary.opacity(0.8))
                                .frame(width: 130, alignment: .leading)
                                .contentShape(Rectangle())
                                .onTapGesture(count: 2) { s.pillOffsetX = 0; s.pillOffsetY = 0; s.save() }
                            Text("X").font(.system(size: 10)).foregroundStyle(.secondary)
                            GlassSlider(value: cgBinding($s.pillOffsetX), range: -60...60)
                                .onChange(of: s.pillOffsetX) { _, _ in s.save() }
                            Text("Y").font(.system(size: 10)).foregroundStyle(.secondary)
                            GlassSlider(value: cgBinding($s.pillOffsetY), range: -40...40)
                                .onChange(of: s.pillOffsetY) { _, _ in s.save() }
                        }.frame(height: 30)
                        note("Колір/радіус застосовуються до ВСІХ кнопок і сегментів (earnings/sales/name теж)")
                    }

                    // ────────────────────────────────────────
                    //  BROWSER PANELS
                    // ────────────────────────────────────────
                    grp("BROWSER  (sync / log / inspector панелі)") {
                        matRow("Матеріал", slot: .browserPanel, dk: dk, s: s)
                        refrRow(.browserPanel, s: s)
                        slRow("Radius", val: cgBinding($s.browserPanelRadius), r: 4...40, fmt: pt, s: s, def: 22)
                    }

                    // ────────────────────────────────────────
                    //  POPUPS & MODALS
                    // ────────────────────────────────────────
                    grp("POPUPS & MODALS  (фото / групи)") {
                        matRow("Матеріал",      slot: .popup,   dk: dk, s: s)
                        refrRow(.popup, s: s)
                        slRow("Radius",         val: cgBinding($s.popupRadius),     r: 8...40, fmt: pt, s: s, def: 22)
                        slRow("Blur за попапом", val: $s.popupBlurRadius,           r: 0...30, fmt: { String(format: "%.0fpx", $0) }, s: s)
                        slRow("Затемнення фону", val: $s.popupDimAlpha,             r: 0...0.6, fmt: pct, s: s)
                    }

                    // ────────────────────────────────────────
                    //  SETTINGS WINDOW
                    // ────────────────────────────────────────
                    grp("SETTINGS WINDOW  (вікно налаштувань)") {
                        matRow("Матеріал", slot: .settingsPopup, dk: dk, s: s)
                        refrRow(.settingsPopup, s: s)
                        slRow("Radius", val: cgBinding($s.settingsRadius), r: 8...40, fmt: pt, s: s, def: 22)
                        slRow("Blur за вікном", val: $s.settingsBlurRadius, r: 0...30, fmt: { String(format: "%.0fpx", $0) }, s: s, def: 12)
                        note("Затемнення за вікном = popupDimAlpha (POPUPS вище)")
                    }

                    // ────────────────────────────────────────
                    //  SCROLL
                    // ────────────────────────────────────────
                    grp("SCROLL") {
                        togRow("Показати скролбар (правий)", val: $s.showScrollBar, s: s)
                        togRow("Bounce (elasticity)",        val: $s.scrollBounce, s: s)
                        note("Fade-ефект полотна = у секції FILTER PANEL (solid/fade)")
                        note("Нативний скролбар macOS не приймає кастомний матеріал — лише показ/ховання")
                    }

                    Divider().opacity(0.1).padding(.vertical, 10)

                    // ────────────────────────────────────────
                    //  МАТЕРІАЛИ — ДОВІДНИК
                    // ────────────────────────────────────────
                    grp("⚠️ ПРИМІТКИ") {
                        note("• glass .clear = майже прозорий, слабо видно. Використовуй .regular або tint40+")
                        note("• Зміни ЖИВІ — відразу видно без перезапуску")
                        note("• Save зберігає між сесіями в recipes/surface_theme.json")
                        note("• Для повного скидання — видали файл surface_theme.json")
                    }

                    grp("МАТЕРІАЛИ — ДОВІДНИК (для вибору в пікерах вище)") {
                        swatches("Liquid Glass", [
                            .glassRegular, .glassClear,
                            .glassRegularTint20, .glassRegularTint40, .glassRegularTint60, .glassRegularTint80, .glassRegularTint100,
                            .glassClearTint20,  .glassClearTint40,  .glassClearTint60,  .glassClearTint80, .glassClearTint100,
                        ])
                        swatches("NSVisualEffectView", [
                            .visMenu, .visMenuFade, .visPopover, .visSidebar,
                            .visHeaderView, .visHudWindow, .visHudFade,
                            .visWindowBG, .visSelection,
                        ])
                        swatches("Plain", [
                            .plainBlack10, .plainBlack20, .plainBlack40, .plainBlack60,
                        ])
                    }

                    Color.clear.frame(height: 24)
                }
                .padding(.horizontal, 14).padding(.top, 6)
            }
        }
        .frame(width: 490)
        .frame(maxHeight: .infinity)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .shadow(color: .black.opacity(0.3), radius: 24, x: -4, y: 0)
        .padding(.vertical, 12).padding(.trailing, 12)
    }

    // ── Header ────────────────────────────────────────────────

    private func header(s: SurfaceTheme) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "paintbrush.pointed").foregroundStyle(Theme.accent)
            Text("Materials Lab").font(.system(size: 15, weight: .bold))
            Text(model.isDark ? "DARK" : "LIGHT")
                .font(.system(size: 9, weight: .bold)).tracking(1)
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(model.isDark ? Color.white.opacity(0.1) : Color.black.opacity(0.08),
                            in: .capsule)
                .foregroundStyle(Theme.t3)
            Spacer()
            Button {
                s.save()
            } label: {
                Label("Save", systemImage: "checkmark.circle.fill")
                    .font(.system(size: 12, weight: .semibold)).foregroundStyle(.white)
            }
            .buttonStyle(.pill(.primary))
            CloseButton { onClose() }
        }
        .padding(.horizontal, 16).padding(.top, 14).padding(.bottom, 10)
    }

    // ── Section wrapper ───────────────────────────────────────

    private func grp<C: View>(_ title: String, @ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.system(size: 9, weight: .bold)).tracking(0.8)
                .foregroundStyle(.secondary.opacity(0.5))
                .padding(.top, 14).padding(.bottom, 4)
            content()
        }
    }

    // ── Row types ─────────────────────────────────────────────

    private func note(_ t: String) -> some View {
        Text(t).font(.system(size: 10)).foregroundStyle(.secondary.opacity(0.45)).padding(.vertical, 2)
    }

    private func colorRow<C: View>(_ label: String, @ViewBuilder _ ctrl: () -> C) -> some View {
        HStack { Text(label).labFont; Spacer(); ctrl() }.frame(height: 28)
    }

    private func togRow(_ label: String, val: Binding<Bool>, s: SurfaceTheme) -> some View {
        HStack {
            Text(label).labFont
            Spacer()
            Toggle("", isOn: val).labelsHidden()
                .onChange(of: val.wrappedValue) { _, _ in s.save() }
        }.frame(height: 28)
    }

    /// Slider row. Pass `def` to enable double-click-label reset to that default.
    private func slRow(_ label: String, val: Binding<Double>, r: ClosedRange<Double>,
                       fmt: @escaping (Double) -> String, s: SurfaceTheme,
                       def: Double? = nil) -> some View {
        HStack(spacing: 8) {
            Text(label).labFont
                .contentShape(Rectangle())
                .onTapGesture(count: 2) { if let d = def { val.wrappedValue = d; s.save() } }
            GlassSlider(value: val, range: r).onChange(of: val.wrappedValue) { _, _ in s.save() }
            Text(fmt(val.wrappedValue)).monoLab
        }.frame(height: 30)
    }

    private func matRow(_ label: String, slot: SurfaceSlot, dk: Bool, s: SurfaceTheme) -> some View {
        HStack(spacing: 8) {
            Text(label).labFont
            Spacer()
            Picker("", selection: dk
                ? Binding(get: { s.slots[slot] ?? .none },
                          set: { s.slots[slot] = $0; s.save() })
                : Binding(get: { s.lightSlots[slot] ?? .none },
                          set: { s.lightSlots[slot] = $0; s.save() })
            ) {
                ForEach(SurfaceMat.allCases, id: \.rawValue) { m in Text(m.rawValue).tag(m) }
            }
            .frame(width: 220)
        }.frame(height: 30)
    }

    /// Two compact sliders (X / Y) that move an element via its offset override.
    /// Double-click the label (or tap ⌖) to re-center this element to default (0,0).
    private func posRow(_ label: String, slot: SurfaceSlot, s: SurfaceTheme) -> some View {
        HStack(spacing: 6) {
            Text(label).font(.system(size: 12)).foregroundStyle(.primary.opacity(0.8))
                .frame(width: 90, alignment: .leading)
                .contentShape(Rectangle())
                .onTapGesture(count: 2) { s.resetOffset(slot) }
            Text("X").font(.system(size: 10)).foregroundStyle(.secondary)
            GlassSlider(value: s.offsetBinding(slot, axis: 0), range: -120...120)
            Text("Y").font(.system(size: 10)).foregroundStyle(.secondary)
            GlassSlider(value: s.offsetBinding(slot, axis: 1), range: -120...120)
            Button { s.resetOffset(slot) } label: {
                Image(systemName: "scope").font(.system(size: 11)).foregroundStyle(.secondary)
            }.buttonStyle(.plain).help("Центрувати (reset)")
        }.frame(height: 30)
    }

    /// Per-element refraction (interactive Liquid Glass) toggle.
    private func refrRow(_ slot: SurfaceSlot, s: SurfaceTheme) -> some View {
        togRow("  ↳ Рефракція (interactive glass)", val: s.interactiveBinding(slot), s: s)
    }

    /// Text color picker for one level on the current theme (+ reset to white/black).
    private func textColorRow(_ label: String, level: Int, dk: Bool, s: SurfaceTheme) -> some View {
        HStack {
            Text(label).labFont
            Spacer()
            Button { s.resetTextColor(level, isDark: dk) } label: {
                Image(systemName: "arrow.uturn.backward").font(.system(size: 10)).foregroundStyle(.secondary)
            }.buttonStyle(.plain).help("Reset → \(dk ? "white" : "black")")
            ColorPicker("", selection: s.textColorBinding(level, isDark: dk)).labelsHidden()
        }.frame(height: 28)
    }

    private func fadeRow(_ label: String, config: Binding<FadeConfig>, s: SurfaceTheme) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(label).labFont; Spacer()
                Toggle("", isOn: config.enabled).labelsHidden()
                    .onChange(of: config.enabled.wrappedValue) { _, _ in s.save() }
            }
            if config.enabled.wrappedValue {
                HStack(spacing: 6) {
                    Text("solid").font(.system(size: 10)).foregroundStyle(.secondary).frame(width: 32)
                    GlassSlider(value: config.solidEnd, range: 0...1)
                        .onChange(of: config.solidEnd.wrappedValue) { _, _ in s.save() }
                    Text(pct(config.solidEnd.wrappedValue)).monoLab
                    Spacer()
                    Text("fade").font(.system(size: 10)).foregroundStyle(.secondary).frame(width: 30)
                    GlassSlider(value: config.fadeEnd, range: 0...1)
                        .onChange(of: config.fadeEnd.wrappedValue) { _, _ in s.save() }
                    Text(pct(config.fadeEnd.wrappedValue)).monoLab
                }
                Toggle("Top → bottom", isOn: config.topToBottom)
                    .font(.system(size: 10)).labelsHidden()
                    .onChange(of: config.topToBottom.wrappedValue) { _, _ in s.save() }
            }
        }
        .padding(.vertical, 4)
    }

    // ── Swatch grid ───────────────────────────────────────────

    private func swatches(_ title: String, _ mats: [SurfaceMat]) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
                .padding(.top, 6).padding(.bottom, 2)
            ForEach(mats, id: \.rawValue) { m in
                HStack(spacing: 8) {
                    Text(m.rawValue)
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.secondary.opacity(0.65))
                        .frame(width: 200, alignment: .leading)
                    Color.clear.surfaceMat(m, interactive: false, radius: 8)
                        .frame(maxWidth: .infinity).frame(height: 24)
                        .overlay(
                            Text("Aa  $1,234  Adobe Stock")
                                .font(.system(size: 9, weight: .semibold))
                                .foregroundStyle(.primary.opacity(0.55))
                        )
                }
            }
        }
    }

    // ── cgFloat binding helper ────────────────────────────────

    private func cgBinding(_ b: Binding<CGFloat>) -> Binding<Double> {
        Binding(get: { Double(b.wrappedValue) }, set: { b.wrappedValue = CGFloat($0) })
    }

    // ── Format helpers ────────────────────────────────────────

    private func pct(_ v: Double) -> String { String(format: "%3.0f%%", v * 100) }
    private func pt(_ v: Double)  -> String { String(format: "%.0fpt", v) }
}

// ── Private text style extensions ────────────────────────────

private extension Text {
    var labFont: some View {
        self.font(.system(size: 12)).foregroundStyle(.primary.opacity(0.8))
            .frame(maxWidth: .infinity, alignment: .leading)
    }
    var monoLab: some View {
        self.font(.system(size: 10, design: .monospaced))
            .foregroundStyle(.secondary)
            .frame(width: 38, alignment: .trailing)
    }
}
