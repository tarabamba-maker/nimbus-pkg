import SwiftUI
import AppKit

struct SyncStatus: Codable {
    var log: [String] = []
    var progress: String = ""
    var running: Bool = false
}

struct BrowserView: View {
    @Environment(AppModel.self) private var model
    @State private var status = SyncStatus()
    @State private var poller: Task<Void, Never>?
    @State private var inspectorURL = ""

    private let syncStocks = ["Adobe Stock", "Shutterstock", "iStock", "Depositphotos", "Envato"]

    var body: some View {
        VStack(spacing: 12) {
            syncBar
            logPanel
            inspectorBar
        }
        .task { await refresh(); startPolling() }
        .onDisappear { poller?.cancel() }
    }

    // MARK: sync controls

    private var syncBar: some View {
        HStack(spacing: 8) {
            actionButton(status.running ? "Syncing…" : "Sync All",
                         icon: "arrow.triangle.2.circlepath", primary: true) {
                Task { await start(stock: nil) }
            }
            .disabled(status.running)

            if status.running {
                actionButton("Stop", icon: "stop.fill") { Task { await stop() } }
            }

            Divider().frame(height: 20)

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(syncStocks, id: \.self) { s in
                        Button { Task { await start(stock: s) } } label: {
                            HStack(spacing: 5) {
                                Circle().fill(Theme.color(for: s)).frame(width: 7, height: 7)
                                Text(Theme.stockAbbr[s] ?? s).font(.system(size: 11, weight: .semibold))
                            }
                            .padding(.horizontal, 10).padding(.vertical, 6)
                            .foregroundStyle(Theme.t2)
                        }
                        .buttonStyle(.plain).glassPill()
                        .disabled(status.running)
                    }
                }
            }
            Spacer()
            if status.running { ProgressView().controlSize(.small) }
        }
        .padding(12).glassCard(16)
    }

    private var logPanel: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    if status.log.isEmpty {
                        Text("Log will appear after sync starts…")
                            .font(.system(size: 12)).foregroundStyle(Theme.t3)
                    }
                    ForEach(Array(status.log.enumerated()), id: \.offset) { i, line in
                        Text(line)
                            .font(.system(size: 11.5, design: .monospaced))
                            .foregroundStyle(Theme.t2)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .id(i)
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .glassCard(16)
            .overlay(alignment: .topTrailing) {
                Button { copyLogs() } label: {
                    Label("Copy", systemImage: "doc.on.doc").font(.system(size: 11))
                        .padding(.horizontal, 10).padding(.vertical, 5)
                }
                .buttonStyle(.plain).glassPill().padding(10)
            }
            .onChange(of: status.log.count) { _, n in
                withAnimation { proxy.scrollTo(n - 1, anchor: .bottom) }
            }
        }
    }

    private var inspectorBar: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass").foregroundStyle(Theme.t2)
            Text("Network Inspector").font(.system(size: 13, weight: .bold))
            Text("універсальний — введи будь-який URL")
                .font(.system(size: 11)).foregroundStyle(Theme.t3)
            Spacer()
            TextField("URL (необов'язково)", text: $inspectorURL)
                .textFieldStyle(.plain).frame(width: 260)
                .padding(.horizontal, 10).padding(.vertical, 6).glassPill()
            actionButton("Open Inspector", icon: "binoculars") { Task { await openInspector() } }
        }
        .padding(12).glassCard(16)
    }

    private func actionButton(_ title: String, icon: String, primary: Bool = false,
                              _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: icon)
                .font(.system(size: 13, weight: .semibold))
                .padding(.horizontal, 16).padding(.vertical, 8)
                .foregroundStyle(primary ? .white : .secondary)
        }
        .buttonStyle(.plain).glassPill(active: primary)
    }

    // MARK: networking

    private func start(stock: String?) async {
        let body: [String: String] = stock.map { ["stock": $0] } ?? [:]
        _ = try? await post("/api/sync/start", body)
        await refresh()
    }
    private func stop() async { _ = try? await post("/api/sync/stop", [:]); await refresh() }
    private func openInspector() async {
        _ = try? await post("/api/inspector/start",
                            ["stock": "Universal", "url": inspectorURL.trimmingCharacters(in: .whitespaces)])
    }

    private func refresh() async {
        if let url = URL(string: API.base + "/api/sync/status"),
           let (data, _) = try? await URLSession.shared.data(from: url),
           let s = try? JSONDecoder().decode(SyncStatus.self, from: data) {
            let wasRunning = status.running
            status = s
            if wasRunning && !s.running { await model.refresh() }
        }
    }

    private func startPolling() {
        poller?.cancel()
        poller = Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 1_200_000_000)
                await refresh()
            }
        }
    }

    private func copyLogs() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(status.log.joined(separator: "\n"), forType: .string)
    }

    @discardableResult
    private func post(_ path: String, _ body: [String: String]) async throws -> Data {
        var req = URLRequest(url: URL(string: API.base + path)!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, _) = try await URLSession.shared.data(for: req)
        return data
    }
}
