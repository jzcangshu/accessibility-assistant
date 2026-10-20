"""局域网自动发现：把「电脑端在哪」通过 mDNS/Bonjour 广播出去。

手机端 App 用 Network framework 的 NWBrowser 一看就知道，不用扫网段。
把 ip/port 写进 TXT 记录，手机端连地址解析都省了。

zeroconf 是可选依赖：没装、或广播失败（防火墙拦了 5353、端口被别的
mDNS 响应器占了）都只是静默跳过——手机端还有网段扫描兜底，功能不受影响。
"""
import logging
import threading
import time

log = logging.getLogger("pc")

SERVICE_TYPE = "_qaassist._tcp.local."
BONJOUR_TYPE = "_qaassist._tcp"  # iOS 的 NSBonjourServices 里要写这个


class Advertiser:
    """注册 _qaassist._tcp 服务；任何失败都不影响其它功能。

    传了 provider 就会定期复查地址：换网络（Wi-Fi ↔ 手机热点 ↔ 插网线）后 IP 会变，
    广播里的旧地址手机连不上，原本只能重启电脑端才能跟上。
    """

    def __init__(self):
        self._zc = None
        self._info = None
        self._host = "pc"
        self._stop = threading.Event()
        self._watch = None

    def _build_info(self, name, host, port, addresses, props):
        import socket

        from zeroconf import ServiceInfo

        extra = {str(k): str(v) for k, v in (props or {}).items()}
        # 手机端直接读 TXT 里的 ip/port 拼地址，省掉一次服务解析
        extra.setdefault("ip", addresses[0])
        extra["port"] = str(int(port))
        return ServiceInfo(
            SERVICE_TYPE,
            name,
            addresses=[socket.inet_aton(ip) for ip in addresses],
            port=int(port),
            properties=extra,
            server=f"{host}.local.",
        )

    def start(self, port, addresses, props=None, provider=None, interval=15.0, on_change=None):
        try:
            import socket

            from zeroconf import Zeroconf
        except ImportError:
            log.info("未安装 zeroconf，跳过 mDNS 广播（手机端仍可用「自动搜索」的网段扫描找到）")
            return False
        if not addresses:
            log.info("没有可用的局域网地址，跳过 mDNS 广播")
            return False
        try:
            host = socket.gethostname() or "pc"
            self._host = host
            self._zc = Zeroconf()
            self._info = self._build_info(f"{host}.{SERVICE_TYPE}", host, port, addresses, props)
            try:
                # 重名时自动改名，免得另一台机器占了同名服务就注册不上
                self._zc.register_service(self._info, allow_name_change=True)
            except TypeError:  # 老版本 zeroconf 没这个参数
                self._zc.register_service(self._info)
            log.info("已用 mDNS 广播 %s（端口 %d，地址 %s），手机端 App 可自动发现",
                     BONJOUR_TYPE, int(port), ", ".join(addresses))
            if provider:
                self._watch = threading.Thread(target=self._watch_loop,
                                               args=(provider, port, props, interval, on_change),
                                               name="mdns-watch", daemon=True)
                self._watch.start()
            return True
        except Exception as e:
            # 带上异常类型：有些超时类异常 str(e) 是空的，只打消息会看不到原因
            log.warning("mDNS 广播失败（不影响使用，手机端会自动改用网段扫描）：%s: %r",
                        type(e).__name__, e)
            self.stop()
            return False

    def _watch_loop(self, provider, port, props, interval, on_change=None):
        """定期比对网卡地址，变了就重播一次。"""
        while not self._stop.wait(interval):
            info, zc = self._info, self._zc   # 先取快照：stop() 可能把它们置空
            if info is None or zc is None:
                return
            try:
                addrs = [a for a in (provider() or []) if a]
                if not addrs or set(addrs) == set(info.parsed_addresses()):
                    continue
                # 先注销（发 goodbye，让手机那边立刻丢掉旧记录）再重新注册。
                # 用 update_service 只改记录的话，接收端会"新旧并存"，手机可能挑到
                # 已经失效的旧地址（实测确认）。名字沿用注册时的那个，注销后名字是空的，
                # 重注册不会漂移成 xxx-2。
                zc.unregister_service(info)
                time.sleep(0.4)
                new = self._build_info(info.name, self._host, port, addrs, props)
                try:
                    zc.register_service(new, allow_name_change=True)
                except TypeError:  # 老版本 zeroconf 没这个参数
                    zc.register_service(new)
                self._info = new
                log.info("网络有变化，已重新广播 mDNS（地址 %s）", ", ".join(addrs))
                if on_change:
                    # 换了网络，防火墙那条「按网络类型分开管」的坑就可能复发，回调去重查一遍。
                    # 回调自己吞异常，别让它把广播线程带走。
                    try:
                        on_change()
                    except Exception as e:
                        log.debug("网络变化回调异常：%s: %r", type(e).__name__, e)
            except Exception as e:
                log.warning("重新广播 mDNS 失败（继续用旧地址）：%s: %r", type(e).__name__, e)

    def stop(self):
        """注销并关闭。同样是阻塞调用，丢到线程里做，免得在事件循环线程上抛 EventLoopBlocked。"""
        self._stop.set()
        zc, info = self._zc, self._info
        self._zc = None
        self._info = None
        if zc is None:
            return

        def work():
            try:
                if info:
                    zc.unregister_service(info)
            except Exception:
                pass
            try:
                zc.close()
            except Exception:
                pass

        t = threading.Thread(target=work, name="mdns-stop", daemon=True)
        t.start()
        t.join(timeout=3)
