import AudioToolbox
import CoreHaptics
import Foundation

/// 震动方式。两种都是"嗡嗡"，不是瞬态的那一记"嗒"——瞬态太轻，放兜里/桌上基本感觉不到。
enum HapticStyle: String, CaseIterable {
    /// 连续触觉（CoreHaptics `.hapticContinuous`）：一段实打实的嗡鸣，节奏完全按网页给的时间轴走。
    case buzz
    /// 经典系统震动（`kSystemSoundID_Vibrate`）：来电/消息那种最重的嗡嗡，但每声多长由系统定，
    /// 且靠太近会被系统吞掉，所以下面自己排期、强制拉开间隔。
    case classic

    var label: String {
        switch self {
        case .buzz: return "嗡鸣（推荐）"
        case .classic: return "经典震动"
        }
    }

    var detail: String {
        switch self {
        case .buzz:
            return "连续嗡鸣，节奏精确、能数出第几下；「试一下」按最强力度播放，正式识别时按网页设置的强度走。"
        case .classic:
            return "系统来电那种最重的震动，每下更长；连续几下之间会自动隔开，避免有的声被系统吞掉。系统在「设置 → 声音与触感」里关掉振动时它不会响。"
        }
    }
}

/// 把网页传来的震动模式转成原生触觉。
///
/// 网页给的是 Vibration API 的格式 `[震, 停, 震, 停, …]`（毫秒，以「震」开头），
/// 例如选项 C 就是 `[180, 260, 180, 260, 180]`，即嗡三下。
/// 每个「震」映射成一记「瞬态起手 + 一段连续嗡鸣」，触发时刻按累积的「震 + 停」推算——
/// 所以三下之间的间隔是稳定的，不会因为前一段时长不同而走样。
final class HapticsPlayer {
    /// 和 SetupView 里 `@AppStorage("hapticStyle")` 的键必须一致。
    static let styleKey = "hapticStyle"
    /// 连接页的「试一下」和网页的「测试提醒」共用这一个播放器（引擎只建一个）。
    static let shared = HapticsPlayer()

    private var engine: CHHapticEngine?
    private var running = false
    private var player: CHHapticAdvancedPatternPlayer?
    /// 经典震动模式下排出去的震动任务，换新模式时要把没响的撤掉，否则新旧会叠在一起。
    private var pending: [DispatchWorkItem] = []

    /// 每下嗡鸣至少持续这么久——短于这个就又变回"嗒"了，感觉不到。
    /// 0.25 → 0.30（加重：每下更满；仍不超过到下一记的窗口）。
    private let minBuzz: Double = 0.30
    /// 经典震动两下之间至少隔这么久：上一声还在响时，紧跟的那一声会被系统直接丢弃（不排队）。
    /// 0.5 秒实测仍偶尔被吞（三下变两下、剩两下间隔翻倍），放宽到 0.8 秒留出余量。
    private let classicMinGap: Double = 0.8

    /// 设备是否支持触觉反馈（模拟器与老机型为 false）。
    static var isSupported: Bool {
        CHHapticEngine.capabilitiesForHardware().supportsHaptics
    }

    /// 用户选的震动方式（连接页里改）。
    static var style: HapticStyle {
        HapticStyle(rawValue: UserDefaults.standard.string(forKey: styleKey) ?? "") ?? .buzz
    }

    /// 启动引擎。App 进入前台时调用一次，避免第一下震动有启动延迟。
    func prepare() {
        guard Self.isSupported else { return }
        if engine != nil { startIfNeeded(); return }
        do {
            let engine = try CHHapticEngine()
            engine.playsHapticsOnly = true
            engine.isAutoShutdownEnabled = true
            // 引擎被系统回收/复位后要重新拉起来，否则放一会儿再震就没反应了
            engine.resetHandler = { [weak self] in
                self?.running = false
                self?.startIfNeeded()
            }
            engine.stoppedHandler = { [weak self] _ in
                self?.running = false
            }
            try engine.start()
            self.engine = engine
            running = true
        } catch {
            engine = nil
            running = false
        }
    }

    private func startIfNeeded() {
        guard let engine = engine, !running else { return }
        do {
            try engine.start()
            running = true
        } catch {
            running = false
        }
    }

    /// 播放一次震动模式。
    ///
    /// - Parameters:
    ///   - pattern: 毫秒时间轴 `[震, 停, 震, 停, …]`，以「震」开头
    ///   - intensity: 力度 0…1（网页的「弱/中/强」三挡，默认中）
    ///   - sharpness: 音色 0…1，越小越闷越像"嗡"、越大越像"嗒"
    ///   - style: 留空则用用户在连接页里选的那种
    func play(pattern: [Int], intensity: Double = 0.75, sharpness: Double = 0.55,
              style: HapticStyle? = nil) {
        guard !pattern.isEmpty else { return }
        cancelPending()
        switch style ?? Self.style {
        case .buzz: playBuzz(pattern: pattern, intensity: intensity, sharpness: sharpness)
        case .classic: playClassic(pattern: pattern)
        }
    }

    /// 连接页的「试一下」：按选项 C 的模式嗡三下。
    /// 力度固定按最强档（试的是"这种方式最多能有多重"；正式识别时仍按网页设置走）。
    func playPreview(style: HapticStyle? = nil) {
        play(pattern: [180, 260, 180, 260, 180], intensity: 1.0, style: style)
    }

    // MARK: - 嗡鸣（默认）

    private func playBuzz(pattern: [Int], intensity: Double, sharpness: Double) {
        // 没有触觉引擎的机型（模拟器、老 iPad）直接退回系统震动，别什么都不做
        guard Self.isSupported else { playClassic(pattern: pattern); return }
        if engine == nil { prepare() }
        startIfNeeded()
        guard let engine = engine, running else { playClassic(pattern: pattern); return }

        let level = Float(min(max(intensity, 0), 1))
        // 连续嗡鸣的音色只看 sharpness：网页那三挡最高 0.72，是给瞬态"嗒"用的，
        // 这里封顶 0.12（越低越闷、越像"重低音"），保证不管选哪挡出来都是"嗡"而不是"嗒"。
        let body = Float(min(max(sharpness, 0), 0.12))

        var events: [CHHapticEvent] = []
        var time: Double = 0
        var i = 0
        while i < pattern.count {
            let on = Double(max(0, pattern[i])) / 1000
            // 最后一记后面没有间隔了，给一段固定的富余，好让它也能嗡满 minBuzz
            let after = (i + 1 < pattern.count) ? Double(max(0, pattern[i + 1])) / 1000 : 0.3
            if on > 0 {
                // 起手一记瞬态：让"这是第几下"清晰可数，也让每下更有冲击感
                events.append(CHHapticEvent(
                    eventType: .hapticTransient,
                    parameters: [
                        CHHapticEventParameter(parameterID: .hapticIntensity, value: level),
                        CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.6),
                    ],
                    relativeTime: time
                ))
                // 主体：一段连续嗡鸣。时长取「这一下的时长」和「到下一记之前的全部时间」里小的那个——
                // 网页默认是震 180ms 停 260ms，这里就能拉到 250ms，比网页标的更长、更好感受；
                // 但它永远不会压到下一记上，所以"三下"依旧分得开。
                let duration = min(on + after, max(on, minBuzz))
                if duration > 0.01 {
                    events.append(CHHapticEvent(
                        eventType: .hapticContinuous,
                        parameters: [
                            CHHapticEventParameter(parameterID: .hapticIntensity, value: level),
                            CHHapticEventParameter(parameterID: .hapticSharpness, value: body),
                        ],
                        relativeTime: time,
                        duration: duration
                    ))
                }
            }
            time += on + after
            i += 2
        }
        guard !events.isEmpty else { return }

        do {
            try player?.stop(atTime: 0)
            let hapticPattern = try CHHapticPattern(events: events, parameters: [])
            let next = try engine.makeAdvancedPlayer(with: hapticPattern)
            try next.start(atTime: 0)
            player = next
        } catch {
            // 震动失败就静默跳过：页面本身不该因此受影响
        }
    }

    // MARK: - 经典系统震动

    private func playClassic(pattern: [Int]) {
        var at: Double = 0
        var last: Double?
        var i = 0
        while i < pattern.count {
            let on = Double(max(0, pattern[i])) / 1000
            let off = (i + 1 < pattern.count) ? Double(max(0, pattern[i + 1])) / 1000 : 0
            if on > 0 {
                // 系统每声大约 0.4 秒且不受我们控制，所以逐记往后推到至少隔开 classicMinGap
                var fire = last.map { max(at, $0 + classicMinGap) } ?? at
                if fire < 0 { fire = 0 }
                scheduleVibrate(at: fire)
                last = fire
            }
            at += on + off
            i += 2
        }
    }

    private func scheduleVibrate(at seconds: Double) {
        let item = DispatchWorkItem {
            AudioServicesPlaySystemSound(kSystemSoundID_Vibrate)
        }
        pending.append(item)
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: item)
    }

    private func cancelPending() {
        for item in pending { item.cancel() }
        pending.removeAll()
    }
}
