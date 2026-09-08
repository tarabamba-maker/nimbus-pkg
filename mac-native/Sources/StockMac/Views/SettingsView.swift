import SwiftUI
import AppKit
import UniformTypeIdentifiers

struct SettingsView: View {
    @Binding var showLabButton: Bool
    var onShowLab: () -> Void = {}
    var onClose: () -> Void = {}
    @Environment(AppModel.self) private var model
    @State private var busy = ""
    @State private var msg = ""

    private let stockColorOrder = ["Adobe Stock", "Shutterstock", "iStock", "Depositphotos", "Envato"]

    @State private var cookiesBusy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("Settings").font(.system(size: 18, weight: .bold))
                Spacer()
                Text("native").font(.system(size: 11)).foregroundStyle(Theme.t3)
                CloseButton { onClose() }
            }

            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    appearance
                    cookies
                    database
                    colors
                }
            }
            if !msg.isEmpty {
                Text(msg).font(.system(size: 11)).foregroundStyle(Theme.green)
            }
        }
        .padding(20)
        .settingsChrome(width: 460, height: 620)
    }

    // MARK: appearance + background

    private var appearance: some View {
        section("APPEARANCE") {
            Toggle(isOn: Binding(get: { model.isDark }, set: { model.isDark = $0 })) {
                Label("Dark theme", systemImage: "moon.fill")
            }
            .toggleStyle(.switch)

            HStack(spacing: 10) {
                btn("Import dark background", icon: "photo") { importBackground(dark: true) }
                btn("Import light background", icon: "photo") { importBackground(dark: false) }
            }
            Text("Background saved separately for light / dark theme")
                .font(.system(size: 10)).foregroundStyle(Theme.t3)

            Divider().opacity(0.3)

            Toggle(isOn: $showLabButton) {
                Label("Show Materials Lab button", systemImage: "paintbrush.pointed")
            }
            .toggleStyle(.switch).font(.system(size: 12))

            btn("Open Materials Lab", icon: "paintbrush.pointed", wide: true) { onShowLab() }
        }
    }

    // MARK: cookies

    private var cookies: some View {
        section("BROWSER AUTH") {
            Text("Імпортує cookies з Chrome/Safari для всіх стоків. Спершу закрий браузер (Cmd+Q).")
                .font(.system(size: 10)).foregroundStyle(Theme.t3)
            btn(cookiesBusy ? "Importing…" : "Import Cookies", icon: "key.fill", wide: true) {
                Task { await importCookies() }
            }
        }
    }

    private func importCookies() async {
        cookiesBusy = true
        defer { cookiesBusy = false }
        var req = URLRequest(url: URL(string: API.base + "/api/import-chrome-cookies")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = Data("{}".utf8)
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            msg = "Не вдалося звʼязатися з бекендом"; return
        }
        if (obj["status"] as? String) == "ok" {
            let n = obj["total_imported"] as? Int ?? 0
            let src = obj["source"] as? String ?? ""
            msg = n > 0 ? "Імпортовано \(n) cookies (\(src))" : "0 cookies — увійди у браузері й повтори"
        } else {
            msg = (obj["msg"] as? String) ?? "Помилка імпорту"
        }
    }

    // MARK: database

    private var database: some View {
        section("DATABASE") {
            HStack(spacing: 10) {
                btn("Export backup", icon: "square.and.arrow.down") { exportBackup() }
                btn("Import backup", icon: "square.and.arrow.up") { importBackup() }
            }
            btn("Deduplicate", icon: "eraser", wide: true) { Task { await postConfirm("/api/deduplicate", "Deduplicate?") } }
            btn("Rebuild from DB", icon: "arrow.clockwise", wide: true) { Task { await postConfirm("/api/rebuild-from-db", "Rebuild groups from DB?", confirm: "REBUILD") } }
            btn("Full Reset (як свіже встановлення)", icon: "trash", wide: true, danger: true) {
                Task { await postConfirm("/api/full-reset", "FULL RESET — зітре всі дані. Точно?", confirm: "RESET") }
            }
        }
    }

    private var colors: some View {
        section("STOCK COLORS") {
            ForEach(stockColorOrder, id: \.self) { s in
                HStack {
                    Circle().fill(model.color(for: s)).frame(width: 10, height: 10)
                    Text(s).font(.system(size: 12))
                    Spacer()
                    ColorPicker("", selection: Binding(
                        get: { model.color(for: s) },
                        set: { c in Task { await model.saveStockColor(s, c) } }
                    ))
                    .labelsHidden().frame(width: 44)
                }
            }
        }
    }

    // MARK: helpers

    private func section(_ title: String, @ViewBuilder _ content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.system(size: 10, weight: .bold)).tracking(0.8).foregroundStyle(Theme.t3)
            content()
        }
    }

    private func btn(_ title: String, icon: String, wide: Bool = false, danger: Bool = false,
                     _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: icon)
                .font(.system(size: 12, weight: .semibold))
                .frame(maxWidth: wide ? .infinity : nil)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .foregroundStyle(danger ? Theme.red : .secondary)
        }
        .buttonStyle(.plain).glassPill()
    }

    private func importBackground(dark: Bool) {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.image]
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let src = panel.url else { return }
        let dst = Backend.shared.dataDir.appendingPathComponent(dark ? "mac-bg-dark.jpg" : "mac-bg-light.jpg")
        try? FileManager.default.removeItem(at: dst)
        try? FileManager.default.copyItem(at: src, to: dst)
        model.bgTick += 1
        msg = "Фон оновлено"
    }

    private func exportBackup() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "stock_backup.zip"
        panel.allowedContentTypes = [UTType.zip]
        guard panel.runModal() == .OK, let dst = panel.url else { return }
        Task {
            busy = "export"
            if let url = URL(string: API.base + "/api/export"),
               let (data, _) = try? await URLSession.shared.data(from: url) {
                try? data.write(to: dst)
                msg = "Експортовано \(data.count / 1024) KB"
            }
            busy = ""
        }
    }

    private func importBackup() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [UTType.zip]
        guard panel.runModal() == .OK, let src = panel.url, let data = try? Data(contentsOf: src) else { return }
        Task {
            var req = URLRequest(url: URL(string: API.base + "/api/import-raw")!)
            req.httpMethod = "POST"
            req.setValue("application/zip", forHTTPHeaderField: "Content-Type")
            req.httpBody = data
            _ = try? await URLSession.shared.data(for: req)
            await model.refresh()
            msg = "Імпортовано — перезавантажую дані"
        }
    }

    private func postConfirm(_ path: String, _ prompt: String, confirm: String = "") async {
        let alert = NSAlert()
        alert.messageText = prompt
        alert.addButton(withTitle: "OK"); alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        var req = URLRequest(url: URL(string: API.base + path)!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // Destructive endpoints require a confirm token in the body.
        let body = confirm.isEmpty ? [:] : ["confirm": confirm]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        // rebuild-from-db / reset run a full rebuild (~60s+) — don't let the default
        // 60s timeout abort it and refresh stale data.
        req.timeoutInterval = 600
        _ = try? await URLSession.shared.data(for: req)
        await model.refresh()
        // These endpoints rewrite groups + matches → invalidate the Groups cache so
        // the tab reloads the fresh data (otherwise it shows the pre-rebuild state).
        model.groupsStore.loaded = false
        await model.groupsStore.reload(period: model.period)
        msg = "Готово"
    }
}
