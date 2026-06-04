// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "StockMac",
    platforms: [.macOS(.v26)],
    targets: [
        .executableTarget(
            name: "StockMac",
            path: "Sources/StockMac"
        )
    ]
)
