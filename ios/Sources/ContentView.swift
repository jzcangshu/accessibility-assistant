import SwiftUI
import UIKit
import WebKit

/// 原生触觉桥。网页里执行
///   `window.webkit.messageHandlers.haptics.postMessage({pattern:[180,260,180], intensity:0.75, sharpness:0.55})`
/// 就会落到这里，交给 CoreHaptics 播放。也兼容旧写法（直接传数组 `[180,260,180]`）。
/// `style` 可省略（省略就用连接页里选的那种）。
final class HapticsBridge: NSObject, WKScriptMessageHandler {
    private let player = HapticsPlayer.shared

    func prepare() {
        player.prepare()
    }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        var pattern: [Int] = []
        var intensity = 0.75
        var sharpness = 0.55
        var style: HapticStyle?
        if let dict = message.body as? [String: Any] {
            if let raw = dict["pattern"] as? [Any] {
                pattern = raw.compactMap { ($0 as? NSNumber)?.intValue }
            }
            if let v = dict["intensity"] as? NSNumber { intensity = v.doubleValue }
            if let v = dict["sharpness"] as? NSNumber { sharpness = v.doubleValue }
            if let s = dict["style"] as? String { style = HapticStyle(rawValue: s) }
        } else if let raw = message.body as? [Any] {
            pattern = raw.compactMap { ($0 as? NSNumber)?.intValue }
        }
        guard !pattern.isEmpty else { return }
        player.play(pattern: pattern, intensity: intensity, sharpness: sharpness, style: style)
    }
}

/// 用 WKWebView 装现有的手机端页面——页面里的设置、渠道、历史、语音全部原样可用，
/// 我们只额外接上震动这一件事。
struct WebView: UIViewRepresentable {
    let url: URL
    let bridge: HapticsBridge

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        // 语音播报要在没有用户手势时也能出声，这里放开自动播放限制
        config.mediaTypesRequiringUserActionForPlayback = []
        config.userContentController.add(bridge, name: "haptics")

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}
}

struct ContentView: View {
    @AppStorage("serverURL") private var serverURL = ""
    @Environment(\.scenePhase) private var scenePhase
    @State private var bridge = HapticsBridge()
    @State private var editing = false

    var body: some View {
        ZStack(alignment: .topTrailing) {
            if !editing, let url = ServerDiscovery.normalized(serverURL) {
                WebView(url: url, bridge: bridge).ignoresSafeArea()
                Button {
                    editing = true
                } label: {
                    Image(systemName: "gearshape.fill")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundColor(.white)
                        .padding(10)
                        .background(Color.black.opacity(0.55), in: Circle())
                }
                .padding(.trailing, 12)
                .padding(.top, 6)
            } else {
                SetupView(onConnected: { editing = false })
            }
        }
        .preferredColorScheme(.dark)
        .onAppear {
            // 熄屏会让网页被挂起、收不到新内容，所以这个 App 在前台时不自动锁屏
            UIApplication.shared.isIdleTimerDisabled = true
            bridge.prepare()
        }
        .onChange(of: scenePhase) { phase in
            // 从后台回来时引擎可能已被系统回收，重新拉起来
            if phase == .active { bridge.prepare() }
        }
    }
}
