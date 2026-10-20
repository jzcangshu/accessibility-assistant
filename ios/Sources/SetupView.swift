import AVFoundation
import SwiftUI

/// 连接页：自动搜索 / 扫码 / 手填，三条路都留着——哪条能用用哪条。
///
/// 整个类型标 `@MainActor`：`search()`/`scanQR()` 是 async，里面的 `@State` 赋值
/// 如果不在主线程上做，SwiftUI 会报「后台线程改状态」甚至偶发崩溃。
@MainActor
struct SetupView: View {
    @AppStorage("serverURL") private var serverURL = ""
    /// 键名要和 `HapticsPlayer.styleKey` 一致（`@AppStorage` 只吃字面量，所以这里没法引用它）。
    @AppStorage("hapticStyle") private var styleRaw = HapticStyle.buzz.rawValue
    var onConnected: () -> Void = {}

    @State private var manual = ""
    @State private var searching = false
    @State private var found: [DiscoveredServer] = []
    @State private var message: String?
    @State private var scanning = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("连接电脑端").font(.title2.bold())
                Text("手机和电脑要在同一个 WiFi 或手机热点下。电脑端启动后，控制台会打印地址和二维码。")
                    .font(.footnote)
                    .foregroundColor(.secondary)

                Button {
                    Task { await search() }
                } label: {
                    HStack(spacing: 8) {
                        if searching { ProgressView().scaleEffect(0.75) }
                        Text(searching ? "正在搜索…" : "自动搜索同网络的电脑端")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(searching)

                Button {
                    Task { await scanQR() }
                } label: {
                    Label("扫电脑端控制台上的二维码", systemImage: "qrcode.viewfinder")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)

                if let message {
                    Text(message)
                        .font(.footnote)
                        .foregroundColor(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                ForEach(found) { server in
                    Button { connect(server.url) } label: {
                        HStack(spacing: 10) {
                            Image(systemName: server.viaBonjour ? "wifi" : "magnifyingglass")
                                .foregroundColor(.secondary)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(server.host).font(.body.monospaced())
                                Text("端口 \(server.port) · v\(server.version)\(server.mode.isEmpty ? "" : " · \(server.mode)")")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                            Spacer()
                            Image(systemName: "chevron.right").foregroundColor(.secondary)
                        }
                    }
                    .buttonStyle(.bordered)
                }

                Divider().padding(.vertical, 2)

                Text("或手动填写地址").font(.caption).foregroundColor(.secondary)
                TextField("192.168.1.5:8765", text: $manual)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .textFieldStyle(.roundedBorder)
                Button("连接") { connect(ServerDiscovery.normalized(manual)) }
                    .buttonStyle(.borderedProminent)
                    .disabled(ServerDiscovery.normalized(manual) == nil)

                Divider().padding(.vertical, 2)

                Text("震动方式").font(.caption).foregroundColor(.secondary)
                Picker("震动方式", selection: $styleRaw) {
                    ForEach(HapticStyle.allCases, id: \.rawValue) { style in
                        Text(style.label).tag(style.rawValue)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()

                Text(HapticStyle(rawValue: styleRaw)?.detail ?? "")
                    .font(.footnote)
                    .foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                Button {
                    HapticsPlayer.shared.playPreview(style: HapticStyle(rawValue: styleRaw))
                } label: {
                    Label("试一下（选项 C = 嗡三下）", systemImage: "waveform")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)

                Label(HapticsPlayer.isSupported
                        ? "这台设备支持触觉反馈，网页里的震动会走 CoreHaptics。"
                        : "这台设备不支持触觉反馈（模拟器或老机型），震动不会有反应。",
                      systemImage: HapticsPlayer.isSupported ? "checkmark.circle" : "exclamationmark.triangle")
                    .font(.footnote)
                    .foregroundColor(.secondary)
            }
            .padding(20)
        }
        .sheet(isPresented: $scanning) {
            QRScannerView { text in
                scanning = false
                if let url = ServerDiscovery.normalized(text) {
                    connect(url)
                } else {
                    message = "二维码内容看不懂：\(text)"
                }
            }
        }
    }

    private func connect(_ url: URL?) {
        guard let url else { return }
        serverURL = url.absoluteString
        onConnected()
    }

    private func search() async {
        searching = true
        message = nil
        found = []
        let hits = await ServerDiscovery.discover()
        found = hits
        searching = false
        if hits.isEmpty {
            message = "没找到。确认手机与电脑在同一 WiFi、电脑端已启动、防火墙放行了端口 "
                + "\(ServerDiscovery.defaultPort)；也可以直接扫控制台上的二维码。"
        }
    }

    private func scanQR() async {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            scanning = true
        case .notDetermined:
            if await AVCaptureDevice.requestAccess(for: .video) {
                scanning = true
            } else {
                message = "没有摄像头权限，扫不了码，请手动填地址。"
            }
        default:
            message = "摄像头权限被拒绝了，可去「设置 → 隐私与安全性 → 相机」里打开，或手动填地址。"
        }
    }
}
