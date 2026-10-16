// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "ToDo",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "ToDo",
            path: "Sources/ToDo",
            linkerSettings: [.linkedFramework("Carbon")]
        ),
    ]
)
