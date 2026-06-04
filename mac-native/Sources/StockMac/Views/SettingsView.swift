import SwiftUI
import AppKit
import UniformTypeIdentifiers

struct SettingsView: View {
    var onClose: () -> Void = {}
    @Environment(AppModel.self) private var model
    @State private var busy = ""
    @State private var msg = ""

    private let stockColorOrder = ["Adobe Stock", "Shutterstock", "iStock", "Depositphotos", "Envato"]

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
                    database
                    colors
                }
            }
            if !msg.isEmpty {
                Text(msg).font(.system(size: 11)).foregroundStyle(Theme.green)
            }
        }
        .padding(20)
        .popupChrome(width: 460, height: 620)
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
            Text("Картинку зберігається окремо для світлої / темної теми")
                .font(.system(size: 10)).foregroundStyle(Theme.t3)
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
            btn("Rebuild from DB", icon: "arrow.clockwise", wide: true) { Task { await postConfirm("/api/rebuild-from-db", "Rebuild groups from DB?") } }
            btn("Full Reset (як свіже встановлення)", icon: "trash", wide: true, danger: true) {
                Task { await postConfirm("/api/full-reset", "FULL RESET — зітре всі дані. Точно?") }
            }
        }
    }

    private var colors: some View {
        section("STOCK COLORS") {
            ForEach(stockColorOrder, id: \.self) { s in
                HStack {
                    Circle().fill(Theme.color(for: s)).frame(width: 12, height: 12)
                    Text(s).font(.system(size: 12))
                    Spacer()
                    RoundedRectangle(cornerRadius: 5).fill(Theme.color(for: s))
                        .frame(width: 44, height: 22)
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

    private func postConfirm(_ path: String, _ prompt: String) async {
        let alert = NSAlert()
        alert.messageText = prompt
        alert.addButton(withTitle: "OK"); alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        var req = URLRequest(url: URL(string: API.base + path)!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = Data("{}".utf8)
        _ = try? await URLSession.shared.data(for: req)
        await model.refresh()
        msg = "Готово"
    }
}
