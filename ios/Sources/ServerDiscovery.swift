import Darwin
import Foundation
import Network

/// 局域网里的电脑端。
struct DiscoveredServer: Identifiable, Hashable {
    let id: String
    let host: String
    let port: Int
    let url: URL
    let version: String
    let mode: String
    /// 来源，用来在界面上区分「Bonjour 秒回」和「网段扫描找到」
    let viaBonjour: Bool
}

/// 自动寻找同网络的电脑端，两条路一起走：
///
/// 1. **Bonjour/mDNS**：电脑端会广播 `_qaassist._tcp`，并把 ip/port 写进 TXT 记录，
///    手机这边连地址解析都省了。秒回，但依赖 mDNS 能通（有的路由器或防火墙会拦）。
/// 2. **扫网段兜底**：拿手机自己的 Wi-Fi 地址，扫本 /24 逐个请求 `GET /api/status`，
///    认「ok + version」这个签名。慢几秒，但走的是 App 本来就要用的那条 TCP 通道——
///    只要能打开页面，它就一定能找到。
enum ServerDiscovery {
    static let defaultPort = 8765
    static let bonjourType = "_qaassist._tcp"   // 要和 Info.plist 的 NSBonjourServices、电脑端广播的一致
    private static let probeTimeout: TimeInterval = 0.8
    private static let concurrency = 64

    /// 补协议、补默认端口，免得少打几个字符就连不上。
    static func normalized(_ raw: String) -> URL? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.lowercased().hasPrefix("http://") && !text.lowercased().hasPrefix("https://") {
            text = "http://" + text
        }
        guard var comps = URLComponents(string: text),
              let host = comps.host, !host.isEmpty else { return nil }
        if comps.port == nil { comps.port = defaultPort }
        if comps.path.isEmpty { comps.path = "/" }
        return comps.url
    }

    /// 先 Bonjour，没结果（或广播里的地址连不上）再扫网段。
    static func discover(port: Int = ServerDiscovery.defaultPort) async -> [DiscoveredServer] {
        // Bonjour 拿到的地址也要实测一次再信：电脑端可能同时挂着 VPN/虚拟网卡，
        // 广播里挑中的那个 IP 手机不一定连得上（比如 Clash 的 TUN 地址 198.18.x.x）。
        // 连不上就当没找到，交给网段扫描兜底——那条路走的是 App 本来就要用的 TCP 通道。
        let broadcast = await BonjourFinder().find(timeout: 2.0, fallbackPort: port)
        var verified: [DiscoveredServer] = []
        for server in broadcast {
            if let hit = await probe(server.url, viaBonjour: true) {
                verified.append(hit)
            }
        }
        let found = verified.isEmpty ? await scan(port: port) : verified
        var seen = Set<String>()
        return found.filter { seen.insert($0.id).inserted }
    }

    // ---------- 兜底：扫本 /24 网段 ----------

    /// 本机 Wi-Fi 的 IPv4 地址（拿不到说明没连 Wi-Fi）。
    static func localIPv4() -> String? {
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return nil }
        defer { freeifaddrs(head) }

        var fallback: String?
        var cursor: UnsafeMutablePointer<ifaddrs>? = first
        while let cur = cursor {
            defer { cursor = cur.pointee.ifa_next }
            let flags = Int32(cur.pointee.ifa_flags)
            guard flags & IFF_UP != 0, flags & IFF_LOOPBACK == 0 else { continue }
            guard let sa = cur.pointee.ifa_addr, sa.pointee.sa_family == UInt8(AF_INET) else { continue }
            guard let text = numericHost(sa) else { continue }
            let name = cur.pointee.ifa_name.map { String(cString: $0) } ?? ""
            if name == "en0" { return text }   // iPhone 的 Wi-Fi 固定是 en0
            if fallback == nil { fallback = text }
        }
        return fallback
    }

    private static func numericHost(_ sa: UnsafeMutablePointer<sockaddr>) -> String? {
        var buf = [CChar](repeating: 0, count: Int(NI_MAXHOST))
        let len = socklen_t(sa.pointee.sa_len)
        guard getnameinfo(sa, len, &buf, socklen_t(buf.count), nil, 0, NI_NUMERICHOST) == 0 else { return nil }
        return String(cString: buf)
    }

    /// 候选地址：手机所在 /24 网段里除自己以外的 1~254。
    static func candidates(port: Int) -> [URL] {
        guard let ip = localIPv4() else { return [] }
        let parts = ip.split(separator: ".").compactMap { Int($0) }
        guard parts.count == 4 else { return [] }
        let base = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8)
        let selfValue = base | parts[3]
        var urls: [URL] = []
        urls.reserveCapacity(254)
        for i in 1...254 {
            let value = base | i
            if value == selfValue { continue }
            let host = "\((value >> 24) & 255).\((value >> 16) & 255).\((value >> 8) & 255).\(value & 255)"
            if let url = URL(string: "http://\(host):\(port)") { urls.append(url) }
        }
        return urls
    }

    /// 扫一遍，返回所有应答的电脑端。分批跑，避免一次性开 254 个连接。
    static func scan(port: Int = ServerDiscovery.defaultPort) async -> [DiscoveredServer] {
        let urls = candidates(port: port)
        guard !urls.isEmpty else { return [] }

        var found: [DiscoveredServer] = []
        var index = 0
        while index < urls.count {
            if Task.isCancelled { break }
            let batch = Array(urls[index..<min(index + concurrency, urls.count)])
            index += concurrency
            let hits = await withTaskGroup(of: DiscoveredServer?.self) { group -> [DiscoveredServer] in
                for url in batch {
                    group.addTask { await probe(url) }
                }
                var out: [DiscoveredServer] = []
                for await hit in group {
                    if let hit { out.append(hit) }
                }
                return out
            }
            found.append(contentsOf: hits)
        }
        return found.sorted { $0.host < $1.host }
    }

    private static func probe(_ base: URL, viaBonjour: Bool = false) async -> DiscoveredServer? {
        var request = URLRequest(url: base.appendingPathComponent("api/status"))
        request.timeoutInterval = probeTimeout
        request.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else { return nil }
            guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  obj["ok"] as? Bool == true,
                  let version = obj["version"] as? String else { return nil }
            return DiscoveredServer(
                id: base.absoluteString,
                host: base.host ?? "",
                port: base.port ?? defaultPort,
                url: base,
                version: version,
                mode: (obj["mode"] as? String) ?? "",
                viaBonjour: viaBonjour
            )
        } catch {
            return nil
        }
    }
}

/// 浏览电脑端广播的 `_qaassist._tcp`。
///
/// iOS 的 mDNS 是系统守护进程（mDNSResponder）代发的，App 自己不发多播包，
/// 所以不需要那个受限的 multicast 权限；但要靠 Info.plist 里的 NSBonjourServices
/// 声明服务类型，否则浏览会被直接拒绝。
final class BonjourFinder {
    private let queue = DispatchQueue(label: "qa.bonjour")
    private let lock = NSLock()
    private var results: [String: DiscoveredServer] = [:]
    private var browser: NWBrowser?
    private var finished = false

    func find(timeout: TimeInterval, fallbackPort: Int) async -> [DiscoveredServer] {
        // 失败类型必须是 Never（checked continuation 不会抛错）；写成 Void 会被拒，
        // 因为 Void 不满足 Error 协议。
        await withCheckedContinuation { (continuation: CheckedContinuation<[DiscoveredServer], Never>) in
            let params = NWParameters.tcp
            params.includePeerToPeer = false
            let browser = NWBrowser(for: .bonjour(type: ServerDiscovery.bonjourType, domain: nil), using: params)
            self.browser = browser

            let finish: () -> Void = { [weak self] in
                guard let self else { return }
                self.lock.lock()
                if self.finished { self.lock.unlock(); return }
                self.finished = true
                self.lock.unlock()
                browser.cancel()
                self.browser = nil
                let out = Array(self.results.values).sorted { $0.host < $1.host }
                continuation.resume(returning: out)
            }

            browser.browseResultsChangedHandler = { [weak self] browsed, _ in
                guard let self else { return }
                for result in browsed {
                    guard let server = Self.server(from: result, fallbackPort: fallbackPort) else { continue }
                    self.lock.lock()
                    self.results[server.id] = server
                    self.lock.unlock()
                }
            }
            browser.stateUpdateHandler = { state in
                // 被拒（没声明服务类型、或系统不允许）或出错时立刻收手，交给网段扫描
                switch state {
                case .failed, .cancelled: finish()
                default: break
                }
            }
            browser.start(queue: queue)
            queue.asyncAfter(deadline: .now() + timeout) { finish() }
        }
    }

    /// 电脑端把 ip/port 写进了 TXT 记录，所以不用再做一次服务解析。
    private static func server(from result: NWBrowser.Result, fallbackPort: Int) -> DiscoveredServer? {
        guard case let .bonjour(txt) = result.metadata,
              let ip = txt["ip"], !ip.isEmpty else { return nil }
        let port = Int(txt["port"] ?? "") ?? fallbackPort
        guard let url = URL(string: "http://\(ip):\(port)/") else { return nil }
        return DiscoveredServer(
            id: url.absoluteString,
            host: ip,
            port: port,
            url: url,
            version: txt["version"] ?? "",
            mode: txt["mode"] ?? "",
            viaBonjour: true
        )
    }
}
