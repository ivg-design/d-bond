// swift-tools-version: 5.9
import PackageDescription
let package = Package(name: "DBond", platforms: [.macOS(.v13)], products: [.executable(name: "DBond", targets: ["DBond"]), .executable(name: "dbond-probe", targets: ["DBondProbe"])], targets: [.target(name:"DBondCore"),.executableTarget(name:"DBond",dependencies:["DBondCore"]),.executableTarget(name:"DBondProbe",dependencies:["DBondCore"]),.testTarget(name:"DBondCoreTests",dependencies:["DBondCore"],path:"SwiftTests")])
