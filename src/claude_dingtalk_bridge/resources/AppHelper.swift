// Helper executable bundled inside the daemon's .app.
//
// macOS 26 forbids regular processes from writing ~/Library/LaunchAgents, so
// the launch agent is shipped *inside* this .app and registered through
// SMAppService. SMAppService only works when the calling process's main
// bundle is the .app — which is the case here, because this binary lives at
// <App>.app/Contents/MacOS/.
//
// Modes:
//   register / unregister / status — drive SMAppService for our agent
//   run <program> [args...]        — replace this process with the daemon;
//                                    this is what the agent's plist invokes

import Darwin
import Foundation
import ServiceManagement

// Keep this in sync with launchd.LABEL on the Python side.
let plistName = "com.claude-dingtalk-bridge.plist"

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

let args = CommandLine.arguments
let mode = args.count > 1 ? args[1] : "status"

switch mode {
case "run":
    let rest = Array(args.dropFirst(2))
    guard let program = rest.first else { fail("run: missing program") }
    let cargs: [UnsafeMutablePointer<CChar>?] = rest.map { strdup($0) } + [nil]
    execv(program, cargs)
    fail("run: exec failed for \(program): \(String(cString: strerror(errno)))")

case "register":
    do {
        try SMAppService.agent(plistName: plistName).register()
        print("registered")
    } catch {
        fail("register failed: \(error.localizedDescription)")
    }

case "unregister":
    do {
        try SMAppService.agent(plistName: plistName).unregister()
        print("unregistered")
    } catch {
        fail("unregister failed: \(error.localizedDescription)")
    }

case "status":
    switch SMAppService.agent(plistName: plistName).status {
    case .notRegistered: print("notRegistered")
    case .enabled: print("enabled")
    case .requiresApproval: print("requiresApproval")
    case .notFound: print("notFound")
    @unknown default: print("unknown")
    }

default:
    fail("unknown mode: \(mode)")
}
