// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "AgentBoard",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "AgentBoard",
            path: "Sources/AgentBoard",
            resources: [.copy("widgets.js")],          // the Mail tab's widget renderer (MailWidgets.script)
            linkerSettings: [.linkedFramework("Carbon")]
        ),
    ]
)
