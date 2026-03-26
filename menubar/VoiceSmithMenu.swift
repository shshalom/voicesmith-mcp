import SwiftUI
import AppKit

// MARK: - Data Models

struct VoiceSession: Identifiable, Codable {
    var id: Int { port }
    let name: String
    let voice: String
    let port: Int
    let pid: Int?
    let started_at: String?
}

struct SessionStatus: Codable {
    let ready: Bool?
    let name: String?
    let voice: String?
    let port: Int?
    let pid: Int?
    let muted: Bool?
    let listening: Bool?
    let uptime_s: Int?
    let last_tool_call_age_s: Int?
    let tts: ComponentStatus?
    let stt: STTStatus?
    let vad: ComponentStatus?
    let wake_word: WakeWordStatus?
    let queue_depth: Int?
}

struct ComponentStatus: Codable {
    let loaded: Bool?
    let model: String?
}

struct STTStatus: Codable {
    let loaded: Bool?
    let model: String?
    let language: String?
    let nudge_on_timeout: Bool?
}

struct WakeWordStatus: Codable {
    let enabled: Bool?
    let listening: Bool?
    let state: String?
}

struct VoiceInfo {
    let id: String
    let name: String
    let accent: String
}

/// A single activity data point for sparkline rendering
struct ActivityPoint: Identifiable {
    let id = UUID()
    let timestamp: Date
    let active: Bool        // had recent tool call
    let queueDepth: Int
    let muted: Bool
}

// MARK: - Voice Catalog

let voiceCatalog: [VoiceInfo] = {
    let raw: [(String, String)] = [
        ("af_alloy", "american"), ("af_aoede", "american"), ("af_bella", "american"),
        ("af_heart", "american"), ("af_jessica", "american"), ("af_kore", "american"),
        ("af_nicole", "american"), ("af_nova", "american"), ("af_river", "american"),
        ("af_sarah", "american"), ("af_sky", "american"),
        ("am_adam", "american"), ("am_echo", "american"), ("am_eric", "american"),
        ("am_fenrir", "american"), ("am_liam", "american"), ("am_michael", "american"),
        ("am_onyx", "american"), ("am_puck", "american"), ("am_santa", "american"),
        ("bf_alice", "british"), ("bf_emma", "british"), ("bf_isabella", "british"), ("bf_lily", "british"),
        ("bm_daniel", "british"), ("bm_fable", "british"), ("bm_george", "british"), ("bm_lewis", "british"),
        ("ef_dora", "spanish"), ("em_alex", "spanish"), ("em_santa", "spanish"),
        ("ff_siwis", "french"),
        ("hf_alpha", "hindi"), ("hf_beta", "hindi"), ("hm_omega", "hindi"), ("hm_psi", "hindi"),
        ("if_sara", "italian"), ("im_nicola", "italian"),
        ("jf_alpha", "japanese"), ("jf_gongitsune", "japanese"), ("jf_nezumi", "japanese"),
        ("jf_tebukuro", "japanese"), ("jm_kumo", "japanese"),
        ("pf_dora", "portuguese"), ("pm_alex", "portuguese"), ("pm_santa", "portuguese"),
        ("zf_xiaobei", "mandarin"), ("zf_xiaoni", "mandarin"), ("zf_xiaoxiao", "mandarin"),
        ("zf_xiaoyi", "mandarin"), ("zm_yunjian", "mandarin"), ("zm_yunxi", "mandarin"),
        ("zm_yunxia", "mandarin"), ("zm_yunyang", "mandarin"),
    ]
    return raw.map { id, accent in
        let name = id.split(separator: "_", maxSplits: 1).last.map { String($0).capitalized } ?? id
        return VoiceInfo(id: id, name: name, accent: accent)
    }
}()

let accentLabels: [(key: String, label: String)] = [
    ("american", "American English"), ("british", "British English"),
    ("spanish", "Spanish"), ("french", "French"), ("hindi", "Hindi"),
    ("italian", "Italian"), ("japanese", "Japanese"),
    ("portuguese", "Portuguese"), ("mandarin", "Mandarin"),
]

struct WhisperModel {
    let id: String
    let desc: String
    let sizeBytes: Int64  // expected download size for progress
    let repoName: String  // HuggingFace repo suffix
}

let whisperModels: [WhisperModel] = [
    WhisperModel(id: "base", desc: "~150MB, fastest", sizeBytes: 150_000_000, repoName: "faster-whisper-base"),
    WhisperModel(id: "small", desc: "~500MB, better accuracy", sizeBytes: 500_000_000, repoName: "faster-whisper-small"),
    WhisperModel(id: "medium", desc: "~1.5GB, very accurate", sizeBytes: 1_500_000_000, repoName: "faster-whisper-medium"),
    WhisperModel(id: "large-v3", desc: "~3GB, best accuracy", sizeBytes: 3_000_000_000, repoName: "faster-whisper-large-v3"),
]

// MARK: - Paths

let dataDir = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".local/share/voicesmith-mcp")
let sessionsFile = dataDir.appendingPathComponent("sessions.json")
let configFile = dataDir.appendingPathComponent("config.json")
let templatesDir = dataDir.appendingPathComponent("templates")

let ideRulesPaths: [(name: String, path: URL)] = [
    ("Claude Code", FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude/CLAUDE.md")),
    ("Cursor", FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cursor/rules/voicesmith.mdc")),
    ("Codex", FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".codex/AGENTS.md")),
]

// MARK: - HTTP Helpers

func httpGet(port: Int, path: String, timeout: TimeInterval = 1) -> Data? {
    guard let url = URL(string: "http://127.0.0.1:\(port)\(path)") else { return nil }
    var request = URLRequest(url: url, timeoutInterval: timeout)
    request.httpMethod = "GET"
    let sem = DispatchSemaphore(value: 0)
    var result: Data?
    URLSession.shared.dataTask(with: request) { data, _, _ in
        result = data; sem.signal()
    }.resume()
    sem.wait()
    return result
}

func httpPost(port: Int, path: String, body: [String: Any]? = nil, timeout: TimeInterval = 5) -> [String: Any]? {
    guard let url = URL(string: "http://127.0.0.1:\(port)\(path)") else { return nil }
    var request = URLRequest(url: url, timeoutInterval: timeout)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try? JSONSerialization.data(withJSONObject: body ?? [:])
    let sem = DispatchSemaphore(value: 0)
    var result: [String: Any]?
    URLSession.shared.dataTask(with: request) { data, _, _ in
        if let data = data { result = try? JSONSerialization.jsonObject(with: data) as? [String: Any] }
        sem.signal()
    }.resume()
    sem.wait()
    return result
}

nonisolated func checkNpmVersion() -> String? {
    guard let url = URL(string: "https://registry.npmjs.org/voicesmith-mcp/latest") else { return nil }
    var request = URLRequest(url: url, timeoutInterval: 5)
    request.setValue("application/json", forHTTPHeaderField: "Accept")
    let sem = DispatchSemaphore(value: 0)
    var version: String?
    URLSession.shared.dataTask(with: request) { data, _, _ in
        if let data = data,
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            version = json["version"] as? String
        }
        sem.signal()
    }.resume()
    sem.wait()
    return version
}

// MARK: - App State

@MainActor
class AppState: ObservableObject {
    @Published var sessions: [VoiceSession] = []
    @Published var selectedPort: Int?
    @Published var statusCache: [Int: SessionStatus] = [:]
    @Published var isMuted = false
    @Published var duckMedia = false
    @Published var nudgeOnTimeout = false
    @Published var currentModelSize = "base"
    @Published var configuredModelSize = "base"
    @Published var currentVoice = ""
    @Published var audioInputDevices: [[String: Any]] = []
    @Published var audioOutputDevices: [[String: Any]] = []
    @Published var currentInputDevice: Int? = nil   // sounddevice index
    @Published var currentOutputDevice: String? = nil  // mpv device name
    @Published var latestVersion: String?
    @Published var installedVersion: String?
    @Published var showRulesEditor = false
    @Published var showSessionsWindow = false
    @Published var rulesText = ""
    @Published var rulesIDEName = ""
    @Published var rulesPath: URL?

    // Model download state
    @Published var isDownloadingModel = false
    @Published var downloadModelName = ""
    @Published var downloadProgress: Double = 0  // 0.0 to 1.0
    @Published var downloadStatus = ""
    private var downloadProcess: Process?

    // Wake word state
    @Published var wakeEnabled = false
    @Published var wakeState = "disabled"  // disabled, listening, recording, transcribing, yielded
    private var wakeProcess: Process?
    private var wakeOutputPipe: Pipe?

    /// Rolling activity history per port — last 30 data points (~5 min at 10s intervals)
    @Published var activityHistory: [Int: [ActivityPoint]] = [:]

    private var pollTimer: Timer?
    private var lastUpdateCheck: Date = .distantPast
    private var pollingStarted = false
    private var pollSkipCounter = 0

    static func createAndStartPolling() -> AppState {
        let state = AppState()
        // Delay slightly so SwiftUI is ready
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            state.startPolling()
        }
        return state
    }

    func startPolling() {
        guard !pollingStarted else { return }
        pollingStarted = true
        installedVersion = readInstalledVersion()

        // Auto-start wake detector if enabled in config
        readConfigFallback()
        if wakeEnabled && wakeProcess == nil {
            startWakeDetector()
        }

        poll()
        // Adaptive polling: 1s when any session is active, 5s when idle
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self = self else { return }
                let anyActive = self.sessions.contains { s in
                    (self.statusCache[s.port]?.last_tool_call_age_s ?? 999) < 30
                }
                // Skip every other tick when idle to save CPU
                if !anyActive {
                    self.pollSkipCounter += 1
                    if self.pollSkipCounter % 5 != 0 { return }
                }
                self.poll()
            }
        }
    }

    func poll() {
        let allSessions = readSessions()

        // Only keep sessions that respond to /status (filters dead/crashed ones)
        // Sort: recently active sessions first (lowest last_tool_call_age_s)
        var aliveSessions: [VoiceSession] = []
        var newCache = statusCache
        var newHistory = activityHistory
        let now = Date()

        for s in allSessions {
            if let data = httpGet(port: s.port, path: "/status"),
               let status = try? JSONDecoder().decode(SessionStatus.self, from: data) {
                aliveSessions.append(s)
                newCache[s.port] = status

                // Append activity point
                let active = (status.last_tool_call_age_s ?? 999) < 15
                let point = ActivityPoint(
                    timestamp: now,
                    active: active,
                    queueDepth: status.queue_depth ?? 0,
                    muted: status.muted ?? false
                )
                var history = newHistory[s.port] ?? []
                history.append(point)
                if history.count > 30 { history.removeFirst(history.count - 30) }
                newHistory[s.port] = history
            }
        }
        // Sort by most recently active first
        aliveSessions.sort { a, b in
            let ageA = newCache[a.port]?.last_tool_call_age_s ?? Int.max
            let ageB = newCache[b.port]?.last_tool_call_age_s ?? Int.max
            return ageA < ageB
        }
        sessions = aliveSessions
        statusCache = newCache
        activityHistory = newHistory

        if selectedPort == nil, let last = sessions.last { selectedPort = last.port }
        if let port = selectedPort, !sessions.contains(where: { $0.port == port }) {
            selectedPort = sessions.last?.port
        }

        // Update state from selected session
        if let port = selectedPort, let status = statusCache[port] {
            isMuted = status.muted ?? false
            currentVoice = status.voice ?? ""
            if let stt = status.stt {
                nudgeOnTimeout = stt.nudge_on_timeout ?? false
                if let model = stt.model { currentModelSize = model.replacingOccurrences(of: "whisper-", with: "") }
            }
        }
        readConfigFallback()
        if currentVoice.isEmpty, let port = selectedPort,
           let s = sessions.first(where: { $0.port == port }) { currentVoice = s.voice }

        if Date().timeIntervalSince(lastUpdateCheck) > 6 * 3600 {
            lastUpdateCheck = Date()
            Task {
                let version = await Task.detached { checkNpmVersion() }.value
                self.latestVersion = version
            }
        }
    }

    private func readSessions() -> [VoiceSession] {
        let path = sessionsFile.path(percentEncoded: false)
        guard FileManager.default.fileExists(atPath: path),
              let data = try? Data(contentsOf: sessionsFile),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let arr = json["sessions"] as? [[String: Any]] else { return [] }
        return arr.compactMap { d in
            guard let name = d["name"] as? String, let voice = d["voice"] as? String,
                  let port = d["port"] as? Int else { return nil }
            return VoiceSession(name: name, voice: voice, port: port,
                                pid: d["pid"] as? Int, started_at: d["started_at"] as? String)
        }
    }

    private func readConfigFallback() {
        guard FileManager.default.fileExists(atPath: configFile.path(percentEncoded: false)),
              let data = try? Data(contentsOf: configFile),
              let cfg = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        if let tts = cfg["tts"] as? [String: Any] { duckMedia = tts["duck_media"] as? Bool ?? false }
        if let stt = cfg["stt"] as? [String: Any] {
            nudgeOnTimeout = stt["nudge_on_timeout"] as? Bool ?? false
            configuredModelSize = stt["model_size"] as? String ?? "base"
            if currentModelSize.isEmpty { currentModelSize = configuredModelSize }
        }
        if let ww = cfg["wake_word"] as? [String: Any] {
            let enabled = ww["enabled"] as? Bool ?? false
            wakeEnabled = enabled
        }
        if let tts = cfg["tts"] as? [String: Any] {
            currentOutputDevice = tts["audio_output_device"] as? String
        }
        if let stt = cfg["stt"] as? [String: Any] {
            currentInputDevice = stt["audio_input_device"] as? Int
        }
        // Fetch device lists from server (once)
        if audioInputDevices.isEmpty { fetchAudioDevices() }
    }

    private func readInstalledVersion() -> String? {
        let pkg = dataDir.appendingPathComponent("package.json")
        if let data = try? Data(contentsOf: pkg),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            return json["version"] as? String
        }
        return nil
    }

    // MARK: - Actions

    func muteAll() {
        let ep = isMuted ? "/unmute" : "/mute"
        for s in sessions { _ = httpPost(port: s.port, path: ep) }
        isMuted.toggle()
    }

    func muteSession(_ port: Int) {
        let status = statusCache[port]
        let muted = status?.muted ?? false
        _ = httpPost(port: port, path: muted ? "/unmute" : "/mute")
        poll()
    }

    func toggleDuck() {
        guard let port = selectedPort else { return }
        duckMedia.toggle()
        _ = httpPost(port: port, path: "/config", body: ["key": "tts.duck_media", "value": duckMedia])
    }

    func toggleNudge() {
        guard let port = selectedPort else { return }
        nudgeOnTimeout.toggle()
        _ = httpPost(port: port, path: "/config", body: ["key": "stt.nudge_on_timeout", "value": nudgeOnTimeout])
    }

    func setVoice(_ voiceId: String, port: Int? = nil) {
        let p = port ?? selectedPort ?? 0
        guard p > 0 else { return }
        _ = httpPost(port: p, path: "/set_voice", body: ["voice": voiceId])
        poll()
    }

    func setModel(_ modelId: String) {
        guard let model = whisperModels.first(where: { $0.id == modelId }) else { return }

        // Check if already cached
        let cacheDir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".cache/huggingface/hub/models--Systran--\(model.repoName)")
        let isCached = FileManager.default.fileExists(atPath: cacheDir.path(percentEncoded: false))

        if isCached {
            // Already downloaded — just update config
            applyModelConfig(modelId)
            return
        }

        // Need to download — show progress and run in background
        isDownloadingModel = true
        downloadModelName = modelId
        downloadProgress = 0
        downloadStatus = "Downloading whisper-\(modelId)..."

        let expectedSize = model.sizeBytes
        let cachePath = cacheDir.path(percentEncoded: false)

        // Find the Python venv
        let pythonPath = dataDir.appendingPathComponent(".venv/bin/python3").path(percentEncoded: false)

        Thread.detachNewThread { [weak self] in
            // Start the download via faster-whisper (triggers HuggingFace download)
            let process = Process()
            process.executableURL = URL(fileURLWithPath: pythonPath)
            process.arguments = ["-c", "from faster_whisper import WhisperModel; WhisperModel('\(modelId)')"]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice

            DispatchQueue.main.async { self?.downloadProcess = process }

            do {
                try process.run()
            } catch {
                DispatchQueue.main.async {
                    self?.downloadStatus = "Download failed: \(error.localizedDescription)"
                    self?.isDownloadingModel = false
                }
                return
            }

            // Poll cache directory size for progress
            while process.isRunning {
                let currentSize = self?.directorySize(at: cachePath) ?? 0
                let progress = min(Double(currentSize) / Double(expectedSize), 0.99)
                let mbDone = currentSize / 1_000_000
                let mbTotal = expectedSize / 1_000_000

                DispatchQueue.main.async {
                    self?.downloadProgress = progress
                    self?.downloadStatus = "Downloading whisper-\(modelId)... \(mbDone)MB / \(mbTotal)MB"
                }
                Thread.sleep(forTimeInterval: 0.5)
            }

            DispatchQueue.main.async {
                if process.terminationStatus == 0 {
                    self?.downloadProgress = 1.0
                    self?.downloadStatus = "Restarting sessions..."
                    self?.applyModelConfig(modelId)
                    // Auto-restart all sessions so they pick up the new model
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                        self?.restartAllSessions()
                        self?.downloadStatus = "Done! Sessions restarting."
                        DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                            self?.isDownloadingModel = false
                        }
                    }
                } else {
                    self?.downloadStatus = "Download failed (exit \(process.terminationStatus))"
                    DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                        self?.isDownloadingModel = false
                    }
                }
            }
        }
    }

    func cancelDownload() {
        downloadProcess?.terminate()
        downloadProcess = nil
        isDownloadingModel = false
        downloadStatus = ""
        downloadProgress = 0
    }

    private func applyModelConfig(_ modelId: String) {
        // Try HTTP first (new server), fall back to direct config.json write
        if let port = selectedPort {
            let result = httpPost(port: port, path: "/config", body: ["key": "stt.model_size", "value": modelId])
            if result?["success"] as? Bool == true {
                configuredModelSize = modelId
                currentModelSize = modelId
                return
            }
        }
        // Fallback: write config.json directly
        let path = configFile.path(percentEncoded: false)
        if FileManager.default.fileExists(atPath: path),
           let data = try? Data(contentsOf: configFile),
           var cfg = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            var stt = cfg["stt"] as? [String: Any] ?? [:]
            stt["model_size"] = modelId
            cfg["stt"] = stt
            if let jsonData = try? JSONSerialization.data(withJSONObject: cfg, options: .prettyPrinted) {
                try? jsonData.write(to: configFile)
            }
        }
        configuredModelSize = modelId
        currentModelSize = modelId
    }

    /// Calculate total size of a directory recursively
    nonisolated private func directorySize(at path: String) -> Int64 {
        let fm = FileManager.default
        guard let enumerator = fm.enumerator(atPath: path) else { return 0 }
        var total: Int64 = 0
        while let file = enumerator.nextObject() as? String {
            let fullPath = (path as NSString).appendingPathComponent(file)
            if let attrs = try? fm.attributesOfItem(atPath: fullPath),
               let size = attrs[.size] as? Int64 {
                total += size
            }
        }
        return total
    }

    func restartAllSessions() {
        // Kill all session server PIDs — the IDE respawns them automatically
        for s in sessions {
            if let pid = s.pid, pid > 0 {
                kill(Int32(pid), SIGTERM)
            }
        }
    }

    func stopSession(_ port: Int) { _ = httpPost(port: port, path: "/stop") }
    func testSession(_ port: Int) {
        let name = statusCache[port]?.name ?? sessions.first(where: { $0.port == port })?.name ?? "Agent"
        _ = httpPost(port: port, path: "/speak", body: ["name": name, "text": "VoiceSmith is working.", "block": false])
    }

    func openRulesEditor() {
        for (_, path) in ideRulesPaths {
            let p = path.path(percentEncoded: false)
            if FileManager.default.fileExists(atPath: p) {
                // Open with the same app that handles config (text editor)
                Process.launchedProcess(launchPath: "/usr/bin/open", arguments: ["-t", p])
                return
            }
        }
    }

    func saveRules() {
        guard let path = rulesPath else { return }
        try? rulesText.write(to: path, atomically: true, encoding: .utf8)
        showRulesEditor = false
    }

    func resetRules() {
        var mainAgent = "Eric"
        if let data = try? Data(contentsOf: configFile),
           let cfg = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            mainAgent = cfg["main_agent"] as? String ?? "Eric"
        }
        let templatePath = templatesDir.appendingPathComponent("voice-rules.md")
        guard let template = try? String(contentsOf: templatePath, encoding: .utf8) else { return }
        let rendered = template.replacingOccurrences(of: "{{MAIN_AGENT}}", with: mainAgent)
        let sentinel = "<!-- installed by voicesmith-mcp -->"
        for (_, path) in ideRulesPaths {
            guard FileManager.default.fileExists(atPath: path.path),
                  var content = try? String(contentsOf: path, encoding: .utf8),
                  let idx = content.range(of: sentinel) else { continue }
            let before = content[content.startIndex..<idx.lowerBound].trimmingCharacters(in: .whitespacesAndNewlines)
            content = "\(before)\n\n\(sentinel)\n\(rendered)"
            try? content.write(to: path, atomically: true, encoding: .utf8)
        }
        showRulesEditor = false
    }

    func openConfig() {
        Process.launchedProcess(launchPath: "/usr/bin/open", arguments: ["-t", configFile.path(percentEncoded: false)])
    }

    // MARK: - Audio Devices

    func fetchAudioDevices() {
        // Try server endpoint first
        if let port = selectedPort,
           let data = httpGet(port: port, path: "/audio_devices", timeout: 3),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           json["error"] == nil {
            if let inputs = json["input"] as? [[String: Any]] { audioInputDevices = inputs }
            if let mpvOutputs = json["mpv_output"] as? [[String: Any]] { audioOutputDevices = mpvOutputs }
            else if let outputs = json["output"] as? [[String: Any]] { audioOutputDevices = outputs }
            if let current = json["current"] as? [String: Any] {
                currentInputDevice = current["input_device"] as? Int
                currentOutputDevice = current["output_device"] as? String
            }
            return
        }

        // Fallback: query mpv directly for output devices
        // Find mpv in common locations
        let mpvPaths = ["/opt/homebrew/bin/mpv", "/usr/local/bin/mpv", "/usr/bin/mpv"]
        let mpvPath = mpvPaths.first { FileManager.default.fileExists(atPath: $0) }
        if let mpvPath = mpvPath {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: mpvPath)
            task.arguments = ["--audio-device=help"]
            let pipe = Pipe()
            task.standardOutput = pipe
            task.standardError = FileHandle.nullDevice
            try? task.run()
            task.waitUntilExit()
            let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            var mpvDevices: [[String: Any]] = []
            var seenNames = Set<String>()
            for line in output.split(separator: "\n") {
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                if trimmed.hasPrefix("'"), let endQuote = trimmed.dropFirst().firstIndex(of: "'") {
                    let deviceId = String(trimmed[trimmed.index(after: trimmed.startIndex)...trimmed.index(before: endQuote)])
                    let rest = String(trimmed[trimmed.index(after: endQuote)...]).trimmingCharacters(in: .whitespaces)
                    let name = rest.trimmingCharacters(in: CharacterSet(charactersIn: "()")).trimmingCharacters(in: .whitespaces)
                    // Only include coreaudio devices (skip avfoundation duplicates) and deduplicate by name
                    if !deviceId.isEmpty && deviceId.hasPrefix("coreaudio/") && !seenNames.contains(name) {
                        seenNames.insert(name)
                        mpvDevices.append(["id": deviceId, "name": name.isEmpty ? deviceId : name])
                    }
                }
            }
            audioOutputDevices = mpvDevices
        }

        // Query input devices via system_profiler (native macOS, no Python needed)
        let spTask = Process()
        spTask.executableURL = URL(fileURLWithPath: "/usr/sbin/system_profiler")
        spTask.arguments = ["SPAudioDataType", "-json"]
        let spPipe = Pipe()
        spTask.standardOutput = spPipe
        spTask.standardError = FileHandle.nullDevice
        try? spTask.run()
        spTask.waitUntilExit()
        let spData = spPipe.fileHandleForReading.readDataToEndOfFile()
        if let json = try? JSONSerialization.jsonObject(with: spData) as? [String: Any],
           let sections = json["SPAudioDataType"] as? [[String: Any]] {
            var inputs: [[String: Any]] = []
            var idx = 0
            for section in sections {
                if let items = section["_items"] as? [[String: Any]] {
                    for item in items {
                        let name = item["_name"] as? String ?? ""
                        let inputChannels = item["coreaudio_device_input"] as? Int ?? 0
                        if inputChannels > 0 {
                            inputs.append(["index": idx, "name": name, "default": false])
                            idx += 1
                        }
                    }
                }
            }
            audioInputDevices = inputs
        }
    }

    func setInputDevice(_ index: Int?) {
        // Try HTTP first, fallback to direct config write
        if let port = selectedPort {
            let result = httpPost(port: port, path: "/config", body: ["key": "stt.audio_input_device", "value": index as Any? ?? NSNull()])
            if result?["success"] as? Bool == true {
                currentInputDevice = index
                return
            }
        }
        writeConfigValue(section: "stt", key: "audio_input_device", value: index as Any? ?? NSNull())
        currentInputDevice = index
    }

    func setOutputDevice(_ deviceId: String?) {
        // Try HTTP first, fallback to direct config write
        if let port = selectedPort {
            let result = httpPost(port: port, path: "/config", body: ["key": "tts.audio_output_device", "value": deviceId as Any? ?? NSNull()])
            if result?["success"] as? Bool == true {
                currentOutputDevice = deviceId
                return
            }
        }
        writeConfigValue(section: "tts", key: "audio_output_device", value: deviceId as Any? ?? NSNull())
        currentOutputDevice = deviceId
    }

    private func writeConfigValue(section: String, key: String, value: Any) {
        let path = configFile.path(percentEncoded: false)
        guard FileManager.default.fileExists(atPath: path),
              let data = try? Data(contentsOf: configFile),
              var cfg = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        var sect = cfg[section] as? [String: Any] ?? [:]
        sect[key] = value is NSNull ? nil : value
        cfg[section] = sect
        if let jsonData = try? JSONSerialization.data(withJSONObject: cfg, options: .prettyPrinted) {
            try? jsonData.write(to: configFile)
        }
    }

    // MARK: - Wake Word

    func toggleWake() {
        if wakeEnabled {
            stopWakeDetector()
        } else {
            startWakeDetector()
        }
        // Persist to config
        let path = configFile.path(percentEncoded: false)
        if FileManager.default.fileExists(atPath: path),
           let data = try? Data(contentsOf: configFile),
           var cfg = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            var ww = cfg["wake_word"] as? [String: Any] ?? [:]
            ww["enabled"] = !wakeEnabled  // will be toggled below
            cfg["wake_word"] = ww
            if let jsonData = try? JSONSerialization.data(withJSONObject: cfg, options: .prettyPrinted) {
                try? jsonData.write(to: configFile)
            }
        }
        wakeEnabled.toggle()
    }

    func startWakeDetector() {
        // Kill any existing detector first (prevents duplicates)
        stopWakeDetector()
        guard wakeProcess == nil else { return }

        let pythonPath = dataDir.appendingPathComponent(".venv/bin/python3").path(percentEncoded: false)
        let scriptPath = dataDir.appendingPathComponent("wake_detector.py").path(percentEncoded: false)

        // Fall back to project source if not installed
        let finalScript: String
        if FileManager.default.fileExists(atPath: scriptPath) {
            finalScript = scriptPath
        } else {
            let projectScript = URL(fileURLWithPath: ProcessInfo.processInfo.arguments[0])
                .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
                .appendingPathComponent("wake_detector.py").path
            finalScript = projectScript
        }

        guard FileManager.default.fileExists(atPath: pythonPath) else {
            wakeState = "disabled"
            return
        }

        let pipe = Pipe()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonPath)
        process.arguments = [finalScript]
        process.standardOutput = pipe
        // Log stderr for debugging
        let errLog = FileHandle(forWritingAtPath: "/tmp/voicesmith-wake-err.log")
            ?? FileHandle(forUpdatingAtPath: "/tmp/voicesmith-wake-err.log")
            ?? { let _ = FileManager.default.createFile(atPath: "/tmp/voicesmith-wake-err.log", contents: nil)
                 return FileHandle(forWritingAtPath: "/tmp/voicesmith-wake-err.log")! }()
        errLog.seekToEndOfFile()
        process.standardError = errLog

        wakeOutputPipe = pipe
        wakeProcess = process

        // Read stdout events in a background thread, respawn if it dies
        let fileHandle = pipe.fileHandleForReading
        Thread.detachNewThread { [weak self] in
            while let line = self?.readLine(from: fileHandle) {
                DispatchQueue.main.async {
                    self?.handleWakeEvent(line)
                }
            }
            // Process died — respawn if still enabled
            DispatchQueue.main.async {
                self?.wakeProcess = nil
                self?.wakeOutputPipe = nil
                if self?.wakeEnabled == true {
                    self?.wakeState = "listening"
                    // Respawn after a short delay
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                        if self?.wakeEnabled == true && self?.wakeProcess == nil {
                            self?.startWakeDetector()
                        }
                    }
                } else {
                    self?.wakeState = "disabled"
                }
            }
        }

        do {
            try process.run()
            wakeState = "listening"
        } catch {
            wakeState = "disabled"
            wakeProcess = nil
        }
    }

    func stopWakeDetector() {
        wakeProcess?.terminate()
        wakeProcess = nil
        wakeOutputPipe = nil
        wakeState = "disabled"
        // Also kill any stray detector processes
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        task.arguments = ["-f", "wake_detector.py"]
        try? task.run()
        task.waitUntilExit()
    }

    private nonisolated func readLine(from handle: FileHandle) -> String? {
        var buffer = Data()
        while true {
            let byte = handle.readData(ofLength: 1)
            if byte.isEmpty { return nil }  // EOF
            if byte[0] == UInt8(ascii: "\n") {
                return String(data: buffer, encoding: .utf8)
            }
            buffer.append(byte)
        }
    }

    private func handleWakeEvent(_ line: String) {
        let parts = line.split(separator: " ", maxSplits: 1)
        let event = String(parts.first ?? "")

        switch event {
        case "LISTENING": wakeState = "listening"
        case "WAKE": wakeState = "recording"
        case "RECORDING": wakeState = "recording"
        case "TRANSCRIBING": wakeState = "transcribing"
        case "INJECTED": wakeState = "listening"
        case "YIELDED": wakeState = "yielded"
        case "RESUMED": wakeState = "listening"
        case "ERROR": break  // could show notification
        default: break
        }
    }
}

// MARK: - Activity Sparkline

struct ActivitySparkline: View {
    let points: [ActivityPoint]
    let height: CGFloat = 32

    var body: some View {
        GeometryReader { geo in
            Canvas { context, size in
                let totalSlots = 30
                let spacing: CGFloat = 2
                let barWidth = (size.width - spacing * CGFloat(totalSlots - 1)) / CGFloat(totalSlots)

                for slot in 0..<totalSlots {
                    let x = CGFloat(slot) * (barWidth + spacing)

                    if slot < points.count {
                        let point = points[slot]

                        // Bar height: active = 70-100%, idle = 20%
                        let barH: CGFloat
                        if point.active {
                            barH = size.height * (0.7 + 0.3 * min(CGFloat(point.queueDepth + 1) / 3.0, 1.0))
                        } else {
                            barH = size.height * 0.2
                        }

                        let barRect = CGRect(
                            x: x, y: size.height - barH,
                            width: barWidth, height: barH
                        )

                        let color: Color
                        if point.muted {
                            color = .red.opacity(0.5)
                        } else if point.active && point.queueDepth > 0 {
                            color = .cyan
                        } else if point.active {
                            color = .green
                        } else {
                            color = .secondary.opacity(0.15)
                        }

                        context.fill(
                            Path(roundedRect: barRect, cornerRadius: 2),
                            with: .color(color)
                        )
                    } else {
                        // Empty slot — baseline dot
                        let barRect = CGRect(
                            x: x, y: size.height - size.height * 0.08,
                            width: barWidth, height: size.height * 0.08
                        )
                        context.fill(
                            Path(roundedRect: barRect, cornerRadius: 1),
                            with: .color(.secondary.opacity(0.07))
                        )
                    }
                }
            }
        }
        .frame(height: height)
        .clipShape(RoundedRectangle(cornerRadius: 4))
        .background(RoundedRectangle(cornerRadius: 4).fill(.secondary.opacity(0.04)))
    }
}

// MARK: - Session Activity Window

struct SessionCard: View {
    let session: VoiceSession
    let status: SessionStatus?
    let history: [ActivityPoint]
    @ObservedObject var state: AppState

    var body: some View {
        VStack(spacing: 0) {
            // Top row: name + health dots + actions
            HStack(spacing: 12) {
                // Name + voice
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(session.name)
                            .font(.system(size: 15, weight: .semibold))
                        if status?.muted == true {
                            Image(systemName: "speaker.slash.fill")
                                .font(.system(size: 10))
                                .foregroundColor(.red)
                        }
                    }
                    Text(session.voice)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundColor(.secondary)
                }

                Spacer()

                // Health dots — only show when server reports health data
                if status?.tts != nil || status?.stt != nil || status?.vad != nil {
                    HStack(spacing: 4) {
                        healthDot(status?.tts?.loaded, label: "TTS")
                        healthDot(status?.stt?.loaded, label: "STT")
                        healthDot(status?.vad?.loaded, label: "VAD")
                    }
                }

                // Last active
                VStack(alignment: .trailing, spacing: 1) {
                    if let age = status?.last_tool_call_age_s {
                        let label = age < 60 ? "\(age)s ago" : age < 3600 ? "\(age/60)m ago" : "\(age/3600)h ago"
                        Text(label)
                            .font(.system(size: 10))
                            .foregroundColor(age < 30 ? .green : .secondary)
                    }
                    Text(":\(session.port)")
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundColor(.secondary.opacity(0.4))
                }
            }

            // Sparkline
            ActivitySparkline(points: history)
                .padding(.top, 6)

            // Bottom row: controls
            HStack(spacing: 8) {
                // Voice picker
                Menu {
                    ForEach(accentLabels, id: \.key) { accent in
                        Menu(accent.label) {
                            ForEach(voiceCatalog.filter { $0.accent == accent.key }, id: \.id) { voice in
                                Button(action: { state.setVoice(voice.id, port: session.port) }) {
                                    HStack {
                                        Text("\(voice.name) (\(voice.id))")
                                        if session.voice == voice.id { Image(systemName: "checkmark") }
                                    }
                                }
                            }
                        }
                    }
                } label: {
                    Label("Voice", systemImage: "waveform")
                        .font(.system(size: 11))
                }
                .menuStyle(.borderlessButton)
                .fixedSize()

                Spacer()

                // Mute toggle
                Button(action: { state.muteSession(session.port) }) {
                    Image(systemName: status?.muted == true ? "speaker.slash.fill" : "speaker.wave.2.fill")
                        .font(.system(size: 12))
                        .foregroundColor(status?.muted == true ? .red : .secondary)
                }
                .buttonStyle(.plain)
                .help(status?.muted == true ? "Unmute" : "Mute")

                // Test voice
                Button(action: { state.testSession(session.port) }) {
                    Image(systemName: "play.fill")
                        .font(.system(size: 12))
                        .foregroundColor(.secondary)
                }
                .buttonStyle(.plain)
                .help("Test Voice")

                // Stop
                Button(action: { state.stopSession(session.port) }) {
                    Image(systemName: "stop.fill")
                        .font(.system(size: 12))
                        .foregroundColor(.secondary)
                }
                .buttonStyle(.plain)
                .help("Stop Playback")
            }
            .padding(.top, 8)
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(.regularMaterial)
                .shadow(color: .black.opacity(0.08), radius: 4, y: 2)
        )
    }

    private func healthDot(_ loaded: Bool?, label: String) -> some View {
        Circle()
            .fill(loaded == true ? Color.green : (loaded == false ? Color.red : Color.secondary.opacity(0.3)))
            .frame(width: 7, height: 7)
            .help("\(label): \(loaded == true ? "loaded" : (loaded == false ? "error" : "unknown"))")
    }
}

struct SessionsWindow: View {
    @ObservedObject var state: AppState

    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Image(systemName: "waveform.circle.fill")
                    .font(.system(size: 20))
                    .foregroundStyle(.linearGradient(
                        colors: [.blue, .purple],
                        startPoint: .topLeading, endPoint: .bottomTrailing))
                Text("Session Activity")
                    .font(.system(size: 16, weight: .semibold))
                Spacer()
                Text("\(state.sessions.count) active")
                    .font(.system(size: 12))
                    .foregroundColor(.secondary)
            }
            .padding(.horizontal, 16).padding(.top, 16).padding(.bottom, 12)

            Divider()

            if state.sessions.isEmpty {
                VStack(spacing: 12) {
                    Image(systemName: "mic.badge.xmark")
                        .font(.system(size: 36))
                        .foregroundColor(.secondary.opacity(0.4))
                    Text("No active sessions")
                        .font(.system(size: 14))
                        .foregroundColor(.secondary)
                    Text("Start a coding session in Claude Code, Cursor, or Codex")
                        .font(.system(size: 11))
                        .foregroundColor(.secondary.opacity(0.7))
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .padding(40)
            } else {
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(state.sessions) { session in
                            SessionCard(
                                session: session,
                                status: state.statusCache[session.port],
                                history: state.activityHistory[session.port] ?? [],
                                state: state
                            )
                        }
                    }
                    .padding(12)
                }
            }

            // Legend
            HStack(spacing: 16) {
                legendItem(color: .green, label: "Active")
                legendItem(color: .blue, label: "Queue")
                legendItem(color: .secondary.opacity(0.2), label: "Idle")
                legendItem(color: .red.opacity(0.4), label: "Muted")
                Spacer()
                Text("5 min window · 10s intervals")
                    .font(.system(size: 9))
                    .foregroundColor(.secondary.opacity(0.5))
            }
            .padding(.horizontal, 16).padding(.vertical, 8)
            .background(.ultraThinMaterial)
        }
        .frame(width: 440)
        .frame(minHeight: 300, maxHeight: 600)
    }

    private func legendItem(color: Color, label: String) -> some View {
        HStack(spacing: 4) {
            RoundedRectangle(cornerRadius: 2).fill(color).frame(width: 10, height: 10)
            Text(label).font(.system(size: 10)).foregroundColor(.secondary)
        }
    }
}

// MARK: - Rules Editor Window

struct RulesEditorView: View {
    @ObservedObject var state: AppState

    var body: some View {
        VStack(spacing: 12) {
            HStack {
                Text("Voice Rules — \(state.rulesIDEName)").font(.headline)
                Spacer()
                Button("Reset to Default") { state.resetRules() }.foregroundColor(.red)
            }
            TextEditor(text: $state.rulesText)
                .font(.system(.body, design: .monospaced))
                .frame(minHeight: 300)
            HStack {
                Spacer()
                Button("Cancel") { state.showRulesEditor = false }.keyboardShortcut(.cancelAction)
                Button("Save") { state.saveRules() }.keyboardShortcut(.defaultAction)
            }
        }
        .padding()
        .frame(minWidth: 600, minHeight: 400)
    }
}

// MARK: - Model Download Progress

struct ModelDownloadView: View {
    @ObservedObject var state: AppState

    var body: some View {
        VStack(spacing: 16) {
            HStack(spacing: 12) {
                Image(systemName: "arrow.down.circle.fill")
                    .font(.system(size: 28))
                    .foregroundStyle(.linearGradient(
                        colors: [.blue, .purple],
                        startPoint: .topLeading, endPoint: .bottomTrailing))

                VStack(alignment: .leading, spacing: 4) {
                    Text("Downloading Whisper Model")
                        .font(.system(size: 14, weight: .semibold))
                    Text("whisper-\(state.downloadModelName)")
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundColor(.secondary)
                }
                Spacer()
            }

            // Progress bar
            VStack(alignment: .leading, spacing: 6) {
                ProgressView(value: state.downloadProgress)
                    .progressViewStyle(.linear)
                    .tint(.blue)

                HStack {
                    Text(state.downloadStatus)
                        .font(.system(size: 11))
                        .foregroundColor(.secondary)
                    Spacer()
                    Text("\(Int(state.downloadProgress * 100))%")
                        .font(.system(size: 11, weight: .medium, design: .monospaced))
                        .foregroundColor(.secondary)
                }
            }

            if state.downloadProgress >= 1.0 {
                HStack(spacing: 6) {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundColor(.green)
                    Text("Close and reopen your IDE session to use the new model.")
                        .font(.system(size: 11))
                        .foregroundColor(.secondary)
                }
            }
        }
        .padding(20)
        .frame(width: 380)
        .onAppear {
            // Make this window float
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                for window in NSApplication.shared.windows where window.title == "Model Download" {
                    window.level = .floating
                }
            }
        }
    }
}

// MARK: - Menu Bar Panel (lean)

struct MenuPanel: View {
    @ObservedObject var state: AppState
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header
            HStack {
                Image(systemName: "mic.fill").foregroundColor(.accentColor)
                Text("VoiceSmith MCP").font(.headline)
                Spacer()
                if let v = state.installedVersion {
                    Text("v\(v)").font(.caption).foregroundColor(.secondary)
                }
            }
            .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 8)

            Divider()

            VStack(alignment: .leading, spacing: 0) {
                // Sessions — opens window
                Button(action: { openWindow(id: "sessions") }) {
                    HStack(spacing: 8) {
                        Image(systemName: "waveform.circle.fill").frame(width: 16)
                            .foregroundStyle(.linearGradient(
                                colors: [.blue, .purple],
                                startPoint: .topLeading, endPoint: .bottomTrailing))
                        Text("Sessions (\(state.sessions.count))")
                            .font(.system(size: 13, weight: .medium))
                        Spacer()
                        Image(systemName: "arrow.up.right.square")
                            .font(.system(size: 10))
                            .foregroundColor(.secondary)
                    }
                    .padding(.horizontal, 14).padding(.vertical, 6)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)

                Divider().padding(.vertical, 4)

                // Mute All
                menuButton(
                    state.isMuted ? "Unmute All" : "Mute All",
                    icon: state.isMuted ? "speaker.wave.2.fill" : "speaker.slash.fill",
                    action: state.muteAll
                )

                Divider().padding(.vertical, 4)

                // Toggles
                toggleRow("Media Ducking", icon: "music.note", isOn: state.duckMedia, action: state.toggleDuck)
                toggleRow("Nudge on Timeout", icon: "bubble.left", isOn: state.nudgeOnTimeout, action: state.toggleNudge)

                Divider().padding(.vertical, 4)

                // Whisper Model
                HStack {
                    Text("WHISPER MODEL")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundColor(.secondary)
                    Spacer()
                }
                .padding(.horizontal, 14).padding(.top, 6).padding(.bottom, 2)

                if state.isDownloadingModel {
                    // Inline download progress
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            ProgressView(value: state.downloadProgress)
                                .progressViewStyle(.linear)
                                .tint(.blue)
                            Button(action: state.cancelDownload) {
                                Image(systemName: "xmark.circle.fill")
                                    .font(.system(size: 12))
                                    .foregroundColor(.secondary)
                            }
                            .buttonStyle(.plain)
                            .help("Cancel download")
                        }
                        HStack {
                            Text(state.downloadStatus)
                                .font(.system(size: 10))
                                .foregroundColor(.secondary)
                            Spacer()
                            Text("\(Int(state.downloadProgress * 100))%")
                                .font(.system(size: 10, weight: .medium, design: .monospaced))
                                .foregroundColor(.secondary)
                        }
                    }
                    .padding(.horizontal, 14).padding(.vertical, 4)
                } else {
                    ForEach(whisperModels, id: \.id) { model in
                        Button(action: { state.setModel(model.id) }) {
                            HStack(spacing: 8) {
                                Image(systemName: state.currentModelSize == model.id ? "checkmark.circle.fill" : "circle")
                                    .foregroundColor(state.currentModelSize == model.id ? .accentColor : .secondary)
                                    .font(.system(size: 11))
                                Text(model.id).font(.system(size: 12, weight: .medium))
                                Text("(\(model.desc))").font(.system(size: 10)).foregroundColor(.secondary)
                                Spacer()
                            }
                            .padding(.horizontal, 14).padding(.vertical, 3)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }

                Divider().padding(.vertical, 4)

                // Audio Devices
                DisclosureGroup {
                    // Output
                    Button(action: { state.setOutputDevice(nil) }) {
                        HStack(spacing: 6) {
                            Image(systemName: state.currentOutputDevice == nil ? "checkmark" : "")
                                .frame(width: 12)
                            Text("System Default").font(.system(size: 11))
                            Spacer()
                        }.contentShape(Rectangle())
                    }.buttonStyle(.plain).padding(.horizontal, 4).padding(.vertical, 1)

                    ForEach(Array(state.audioOutputDevices.enumerated()), id: \.offset) { _, device in
                        let deviceId = device["id"] as? String ?? ""
                        let deviceName = device["name"] as? String ?? deviceId
                        Button(action: { state.setOutputDevice(deviceId) }) {
                            HStack(spacing: 6) {
                                Image(systemName: state.currentOutputDevice == deviceId ? "checkmark" : "")
                                    .frame(width: 12)
                                Text(deviceName).font(.system(size: 11)).lineLimit(1)
                                Spacer()
                            }.contentShape(Rectangle())
                        }.buttonStyle(.plain).padding(.horizontal, 4).padding(.vertical, 1)
                    }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "speaker.wave.2").frame(width: 16).foregroundColor(.secondary)
                        Text("Audio Output").font(.system(size: 12))
                        Spacer()
                        Text(state.currentOutputDevice ?? "Default")
                            .font(.system(size: 10)).foregroundColor(.secondary).lineLimit(1)
                    }
                }
                .padding(.horizontal, 14).padding(.vertical, 2)
                .font(.system(size: 12))

                DisclosureGroup {
                    Button(action: { state.setInputDevice(nil) }) {
                        HStack(spacing: 6) {
                            Image(systemName: state.currentInputDevice == nil ? "checkmark" : "")
                                .frame(width: 12)
                            Text("System Default").font(.system(size: 11))
                            Spacer()
                        }.contentShape(Rectangle())
                    }.buttonStyle(.plain).padding(.horizontal, 4).padding(.vertical, 1)

                    ForEach(Array(state.audioInputDevices.enumerated()), id: \.offset) { _, device in
                        let idx = device["index"] as? Int ?? 0
                        let name = device["name"] as? String ?? "Device \(idx)"
                        Button(action: { state.setInputDevice(idx) }) {
                            HStack(spacing: 6) {
                                Image(systemName: state.currentInputDevice == idx ? "checkmark" : "")
                                    .frame(width: 12)
                                Text(name).font(.system(size: 11)).lineLimit(1)
                                Spacer()
                            }.contentShape(Rectangle())
                        }.buttonStyle(.plain).padding(.horizontal, 4).padding(.vertical, 1)
                    }
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "mic").frame(width: 16).foregroundColor(.secondary)
                        Text("Audio Input").font(.system(size: 12))
                        Spacer()
                        Text(state.currentInputDevice != nil ? "Custom" : "Default")
                            .font(.system(size: 10)).foregroundColor(.secondary)
                    }
                }
                .padding(.horizontal, 14).padding(.vertical, 2)
                .font(.system(size: 12))

                Divider().padding(.vertical, 4)

                menuButton("Edit Voice Rules...", icon: "doc.text", action: state.openRulesEditor)
                menuButton("Open Config...", icon: "gearshape", action: state.openConfig)

                if let latest = state.latestVersion, let installed = state.installedVersion, latest != installed {
                    Divider().padding(.vertical, 4)
                    menuButton("⬆ Update Available (\(latest))", icon: "arrow.up.circle.fill") {}
                }
            }
            .padding(.bottom, 4)

            Divider()

            Button(action: { NSApplication.shared.terminate(nil) }) {
                HStack {
                    Text("Quit").foregroundColor(.secondary).font(.system(size: 12))
                    Spacer()
                }
                .padding(.horizontal, 14).padding(.vertical, 6)
            }
            .buttonStyle(.plain)
        }
        .frame(width: 280)
    }

    private func menuButton(_ title: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: icon).frame(width: 16).foregroundColor(.secondary)
                Text(title).font(.system(size: 13))
                Spacer()
            }
            .padding(.horizontal, 14).padding(.vertical, 5)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func toggleRow(_ title: String, icon: String, isOn: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: icon).frame(width: 16).foregroundColor(.secondary)
                Text(title).font(.system(size: 13))
                Spacer()
                Image(systemName: isOn ? "checkmark.circle.fill" : "circle")
                    .foregroundColor(isOn ? .green : .secondary)
            }
            .padding(.horizontal, 14).padding(.vertical, 5)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

// MARK: - App

/// Ensure only one instance of VoiceSmith menu bar is running
func ensureSingleInstance() {
    let myPID = ProcessInfo.processInfo.processIdentifier
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
    task.arguments = ["-f", "VoiceSmith.app/Contents/MacOS/VoiceSmith"]
    let pipe = Pipe()
    task.standardOutput = pipe
    try? task.run()
    task.waitUntilExit()
    let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    let pids = output.split(separator: "\n").compactMap { Int32($0.trimmingCharacters(in: .whitespaces)) }
    for pid in pids where pid != myPID {
        kill(pid, SIGTERM)
    }
}

/// Set the app icon (shown in Activity Monitor, Dock, etc.) from bundled PNG
func setAppIcon() {
    let binaryURL = URL(fileURLWithPath: ProcessInfo.processInfo.arguments[0])
    let iconPath = binaryURL.deletingLastPathComponent().appendingPathComponent("app-icon.png")
    if let img = NSImage(contentsOf: iconPath) {
        NSApplication.shared.applicationIconImage = img
    }
}

@main
struct VoiceSmithMenuApp: App {
    @StateObject private var state = AppState.createAndStartPolling()

    var body: some Scene {
        MenuBarExtra {
            MenuPanel(state: state)
        } label: {
            MenuBarIcon(state: state)
        }
        .menuBarExtraStyle(.window)

        // Sessions activity window
        Window("Session Activity", id: "sessions") {
            SessionsWindow(state: state)
                .onAppear {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                        for window in NSApplication.shared.windows where window.title == "Session Activity" {
                            window.level = .floating
                            window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
                        }
                    }
                }
        }
        .windowResizability(.contentSize)
        .defaultPosition(.center)

        // Rules editor window
        Window("Voice Rules", id: "rules-editor") {
            if state.showRulesEditor {
                RulesEditorView(state: state)
            }
        }
        .windowResizability(.contentSize)

    }

    init() {
        ensureSingleInstance()
        setAppIcon()
    }
}

/// Compose an NSImage with the mic SF Symbol and status indicator
func makeMenuBarIcon(sessions: [VoiceSession], isMuted: Bool, statusCache: [Int: SessionStatus], wakeState: String = "disabled") -> NSImage {
    // Check if any session is actively listening (mic open) or wake is recording
    let anyListening = wakeState == "recording" || wakeState == "transcribing" || sessions.contains { s in
        statusCache[s.port]?.listening == true
    }

    let symbolName: String
    if sessions.isEmpty {
        symbolName = "mic.badge.xmark"
    } else if isMuted {
        symbolName = "mic.slash.fill"
    } else {
        symbolName = "mic.fill"
    }

    let config = NSImage.SymbolConfiguration(pointSize: 14, weight: .medium)
    let baseIcon = NSImage(systemSymbolName: symbolName, accessibilityDescription: "VoiceSmith")!
        .withSymbolConfiguration(config)!

    if sessions.isEmpty {
        baseIcon.isTemplate = true
        return baseIcon
    }

    // Fixed canvas size for all states — prevents menu bar resize/squish
    let padding: CGFloat = 4
    let canvasSize = NSSize(
        width: baseIcon.size.width + padding * 2 + 4,
        height: baseIcon.size.height + padding * 2
    )
    let iconOrigin = NSPoint(x: padding, y: padding)

    // Active/listening mode: wide orange pill (matches macOS native mic indicator)
    if anyListening && !isMuted {
        let pillHeight: CGFloat = 24
        let pillWidth: CGFloat = 38
        let pillSize = NSSize(width: pillWidth, height: pillHeight)

        let iconConfig = NSImage.SymbolConfiguration(pointSize: 12, weight: .semibold)
        let micIcon = NSImage(systemSymbolName: "mic.fill", accessibilityDescription: nil)!
            .withSymbolConfiguration(iconConfig)!

        let composited = NSImage(size: pillSize, flipped: false) { rect in
            // Orange pill background
            let bgPath = NSBezierPath(roundedRect: rect, xRadius: rect.height / 2, yRadius: rect.height / 2)
            NSColor.systemOrange.setFill()
            bgPath.fill()

            // White mic centered
            let tinted = NSImage(size: micIcon.size, flipped: false) { r in
                micIcon.draw(in: r)
                NSColor.white.set()
                r.fill(using: .sourceAtop)
                return true
            }
            tinted.draw(at: NSPoint(x: (rect.width - micIcon.size.width) / 2,
                                     y: (rect.height - micIcon.size.height) / 2),
                       from: .zero, operation: .sourceOver, fraction: 1.0)
            return true
        }
        composited.isTemplate = false
        return composited
    }

    // Normal mode: white icon with colored status dot
    let tinted = NSImage(size: baseIcon.size, flipped: false) { rect in
        baseIcon.draw(in: rect)
        NSColor.white.set()
        rect.fill(using: .sourceAtop)
        return true
    }

    let dotNSColor: NSColor
    if isMuted {
        dotNSColor = .systemRed
    } else {
        let anyActive = sessions.contains { s in
            (statusCache[s.port]?.last_tool_call_age_s ?? 999) < 15
        }
        dotNSColor = anyActive ? .systemGreen : .systemOrange
    }

    let composited = NSImage(size: canvasSize, flipped: false) { rect in
        // Draw mic centered in canvas
        tinted.draw(at: NSPoint(x: (rect.width - tinted.size.width) / 2,
                                 y: (rect.height - tinted.size.height) / 2),
                   from: .zero, operation: .sourceOver, fraction: 1.0)

        // Status dot in top-right
        let dotSize: CGFloat = 7
        let dotRect = NSRect(
            x: rect.width - dotSize - 1,
            y: rect.height - dotSize - 1,
            width: dotSize, height: dotSize
        )
        NSColor.black.withAlphaComponent(0.3).setFill()
        NSBezierPath(ovalIn: dotRect.insetBy(dx: -1, dy: -1)).fill()
        dotNSColor.setFill()
        NSBezierPath(ovalIn: dotRect).fill()

        return true
    }
    composited.isTemplate = false
    return composited
}

/// Wrapper view that renders the composited NSImage
struct MenuBarIcon: View {
    @ObservedObject var state: AppState

    var body: some View {
        Image(nsImage: makeMenuBarIcon(
            sessions: state.sessions,
            isMuted: state.isMuted,
            statusCache: state.statusCache,
            wakeState: state.wakeState
        ))
    }
}
