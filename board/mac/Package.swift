// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "AgentBoard",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "AgentBoard",
            path: "Sources/AgentBoard",
            // the Mail tab's widget renderer (MailWidgets.script), and the cases --selftest-mail checks the reply's
            // text on, which the phone's copy of it is checked on too (MailTest.sharedCases)
            resources: [.copy("widgets.js"), .copy("reply-text-cases.json")],
            linkerSettings: [.linkedFramework("Carbon")]
        ),
    ]
)
