// swift-tools-version: 5.9
// bodh for Mac: a menu bar app around the bundled engine. `swift build` builds the executable;
// `make app` (mac/Makefile) wraps it into Bodh.app with the engine, the plist and the icon;
// `open Package.swift` works in Xcode. The BodhEngine library holds everything that needs no
// AppKit (the API client, the models, the event stream, the supervisor's state machine), so
// its tests run on Linux too.
import PackageDescription

let package = Package(
    name: "Bodh",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "Bodh", targets: ["Bodh"]),
        .library(name: "BodhEngine", targets: ["BodhEngine"]),
    ],
    targets: [
        .target(
            name: "BodhEngine",
            path: "Sources/BodhEngine"
        ),
        .executableTarget(
            name: "Bodh",
            dependencies: ["BodhEngine"],
            path: "Sources/Bodh"
        ),
        .testTarget(
            name: "BodhEngineTests",
            dependencies: ["BodhEngine"],
            path: "Tests/BodhEngineTests"
        ),
    ],
    swiftLanguageVersions: [.v5]
)
