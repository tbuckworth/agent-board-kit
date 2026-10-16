// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "AgentBoard",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "AgentBoard",
            path: "Sources/AgentBoard",
            linkerSettings: [.linkedFramework("Carbon")]
        ),
    ]
)
