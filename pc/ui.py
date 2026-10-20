# -*- coding: utf-8 -*-
"""主窗口：本进程内嵌 WebView2（Chromium）窗口，承载与手机端同一份网页的 PC 布局。

为什么是"内嵌 WebView2"而不是外部浏览器窗口、也不是继续 Tk（2026-10 实测定案）：
- 与手机端共用同一份页面代码：设置、状态、日志两边渲染完全一致，设置统一存服务端；
- Chromium 按所在屏 DPI 渲染，多屏不同缩放都清晰正确（Tk 只按进程启动屏渲染，
  这是"笔记本屏上 UI 很小、2K 主屏正常"的根因）；
- 窗口属主是本进程 —— win_stealth 的 WDA 防截屏、TOOLWINDOW 隐身才能生效：
  跨进程 SetWindowDisplayAffinity 被系统拒绝（tools/_probe_edge.py 实测），
  嵌入本进程后 PrintWindow 抓图比率 1.0 → 0.0，防护完整（tools/_probe_wv2.py 实测）。

线程模型：webview.start() 必须跑在主线程（WinForms 要求），start() 阻塞到窗口关闭；
服务器与业务逻辑跑在 start(server_main=...) 传入的工作线程里。窗口显隐/关闭走
Win32 HWND，任意线程可调。✕ 不退出：closing 事件返回 False 取消关闭并隐藏（与
旧版一致）；真正退出由 stop()（服务器结束 / Ctrl+Alt+Q / 手机端「关闭电脑端」）触发。

窗口起不来（缺 WebView2 运行时 / pythonnet）不影响主功能：start() 返回 False，
调用方改为无窗口模式运行，手机端照常，控制台页面可用任意浏览器打开同一地址。
"""
import ctypes
import logging
import threading
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

# 窗口底色与页面背景一致，页面加载期不闪白（单色体系，见 web/style.css）
BG = "#141414"
WIN_W, WIN_H = 1280, 880  # 默认窗口逻辑尺寸（会按目标屏工作区收缩）
MIN_W, MIN_H = 960, 620

WM_CLOSE = 0x0010
SW_HIDE, SW_SHOW, SW_RESTORE = 0, 5, 9
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOMOVE, SWP_NOSIZE = 0x2, 0x1

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

# HWND 是指针宽度；避免在 64 位进程中把前台窗口句柄截断。
u32.GetForegroundWindow.restype = wintypes.HWND
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u32.SetForegroundWindow.argtypes = [wintypes.HWND]
u32.BringWindowToTop.argtypes = [wintypes.HWND]
u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.UINT]
u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]

_st = {
    "cfg": None,
    "win": None,          # pywebview Window（仅创建/标题用，管理一律走 HWND）
    "hwnd": 0,
    "really_closing": False,  # True 时 ✕ / stop() 才真的关窗
    "visible": False,
    "overlay": None,
}


def qr_box_size(modules, border=4, target=140):
    """每个二维码模块的像素边长（纯逻辑，供自测）：把成品边长控制在 target 附近。"""
    return max(3, round(target / (modules + border * 2)))


def make_qr_image(url, target=140, border=4):
    """生成可扫描的二维码图像（PIL）。box 尺寸按模块数先算好，不做事后缩放（缩放会掉模块）。"""
    import qrcode

    qr = qrcode.QRCode(border=border, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    qr.box_size = qr_box_size(qr.modules_count, border, target)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


# ---------- DPI / 目标屏 ----------

def set_dpi_awareness():
    """进程级 Per-Monitor V2：窗口在 150% 屏上就按 150% 渲染。

    必须在创建任何窗口前调用（webview.start 之前）。WinForms 自己只会调
    SetProcessDPIAware（系统级、按主屏），对"窗口开在另一块高缩放屏"无效。
    失败静默退化：老系统至少不会崩。
    """
    try:
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2（伪句柄）
        if u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        return True
    except Exception:
        return False


class _MONINFO(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _monitor_dpi(mon):
    """目标屏的有效 DPI（每逻辑英寸像素）；拿不到给 96。"""
    try:
        dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
        ctypes.windll.shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        return int(dpi_x.value or 96)
    except Exception:
        return 96


def _cursor_monitor():
    """鼠标所在显示器：工作区（物理像素）+ DPI。窗口开在用户正看着的屏上。"""
    pt = wintypes.POINT()
    if u32.GetCursorPos(ctypes.byref(pt)):
        mon = u32.MonitorFromPoint(pt, 1)  # MONITOR_DEFAULTTONEAREST
        mi = _MONINFO()
        mi.cb = ctypes.sizeof(_MONINFO)
        if u32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            r = mi.rcWork
            return (r.left, r.top, r.right - r.left, r.bottom - r.top), _monitor_dpi(mon), mon
    r = wintypes.RECT()
    ok = u32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    wa = (r.left, r.top, r.right - r.left, r.bottom - r.top) if ok else (0, 0, 1920, 1040)
    return wa, 96, 0


def _window_geometry():
    """默认窗口位置与逻辑尺寸：目标屏工作区的 94%，居中；返回 (x, y, w, h) 逻辑像素。

    pywebview 在创建窗体时会按"窗体所在屏的 DPI"把逻辑值换算成物理像素，
    所以这里给逻辑坐标（物理工作区 ÷ 缩放比）。
    """
    (x, y, w, h), dpi, _mon = _cursor_monitor()
    scale = dpi / 96.0
    lw, lh = w / scale, h / scale
    ww = min(WIN_W, round(lw * 0.94))
    wh = min(WIN_H, round(lh * 0.92))
    return round(x / scale + (lw - ww) / 2), round(y / scale + (lh - wh) / 2), ww, wh


# ---------- HWND ----------

def _find_hwnd(deadline=20.0):
    """找到 WebView2 宿主窗体（WinForms Form）的 HWND：枚举本进程顶层窗口，取面积最大者。

    窗口以 hidden 创建（先套隐身再显示），所以不能按可见性过滤。
    """
    pid = k32.GetCurrentProcessId()
    end = time.time() + deadline
    while time.time() < end:
        found = []
        cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def fn(hwnd, _):
            p = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid:
                cls = ctypes.create_unicode_buffer(64)
                u32.GetClassNameW(hwnd, cls, 64)
                if cls.value.startswith("WindowsForms10.Window"):
                    rc = wintypes.RECT()
                    u32.GetWindowRect(hwnd, ctypes.byref(rc))
                    found.append(((rc.right - rc.left) * (rc.bottom - rc.top), int(hwnd)))
            return True

        u32.EnumWindows(cb(fn), 0)
        if found:
            found.sort(reverse=True)
            return found[0][1]
        time.sleep(0.15)
    return 0


def _hwnd():
    h = _st["hwnd"]
    return h if h and u32.IsWindow(h) else 0


# ---------- 对外接口（任意线程可调） ----------

def start(cfg, version="", server_main=None, on_ready=None, on_gui_exit=None):
    """创建主窗口并进入 GUI 主循环（阻塞到窗口关闭），返回 True。

    server_main：GUI 线程之外的工作线程里运行（放服务器与业务主逻辑）；
    它返回（含退出指令）后本模块自动关窗，让主循环结束。
    on_ready：窗口找到句柄、套好隐身后回调一次（启动自检用）。
    on_gui_exit：GUI 先于服务器结束（如终端里 Ctrl+C）时回调，让服务器有序停下。
    窗口模块不可用时返回 False，调用方自行无窗口运行。
    """
    _st["cfg"] = cfg
    _st["version"] = version
    set_dpi_awareness()
    try:
        import webview
    except Exception as e:
        log.warning("窗口模块不可用（无窗口模式运行，页面可用浏览器打开）：%s: %s",
                    type(e).__name__, e)
        return False

    port = (cfg.get("server") or {}).get("port", 8765)
    title = str((cfg.get("stealth") or {}).get("title") or "")
    x, y, w, h = _window_geometry()
    _st["win"] = webview.create_window(
        title, f"http://127.0.0.1:{port}/?pc=1",
        width=w, height=h, x=x, y=y,
        min_size=(MIN_W, MIN_H), hidden=True, background_color=BG,
        text_select=True, confirm_close=False)

    # ✕ = 隐藏：closing 订阅者返回 False 即取消关闭（pywebview 契约）；
    # stop() 置 really_closing 后返回 None 放行，窗口才真关。
    def _on_closing():
        if _st["really_closing"]:
            return None
        hide("窗口已隐藏，程序继续在后台运行（Ctrl+Alt+H 可唤回）")
        return False

    _st["win"].events.closing += _on_closing

    def _server():
        try:
            if server_main:
                server_main()
        except Exception:
            log.exception("服务器线程异常退出")
        finally:
            stop()  # 服务器结束（正常退出或异常）都把窗口带上，别留一个连不上服务的空壳

    threading.Thread(target=lambda: _stealth_when_ready(on_ready), daemon=True).start()
    try:
        webview.start(func=_server)
    except Exception:
        log.exception("主窗口启动失败（无窗口模式继续）")
        return False
    finally:
        # GUI 先于服务器结束（终端里 Ctrl+C 等）时把服务器叫停；服务器正常退出
        # 已经把窗口带上了，这里再调一次也只是幂等的 no-op。
        if on_gui_exit:
            try:
                on_gui_exit()
            except Exception:
                log.debug("gui 退出回调失败", exc_info=True)
    return True


def _stealth_when_ready(on_ready):
    """窗口句柄出现后：套任务栏隐身 / 标题 / 防截屏（配置驱动），再显示。

    必须在显示前套好，避免任务栏/截图里闪一下。UI 线程显示窗口用的是 hidden=True +
    Win32 ShowWindow，两边的时序都在这里收敛。
    """
    hwnd = _find_hwnd()
    if not hwnd:
        log.warning("没找到主窗口句柄：隐身与防截屏未生效（其余功能不受影响）")
        return
    _st["hwnd"] = hwnd
    from . import win_stealth

    win_stealth.attach(hwnd)
    log.debug("主窗口隐身：%s", win_stealth.apply(_st["cfg"]))
    from .overlay import FloatingAnswer
    from .main import refresh_status, save_overlay_opacity
    _st["overlay"] = FloatingAnswer(
        _st["cfg"], _st["win"], refresh_status,
        lambda value: save_overlay_opacity(_st["cfg"], value))
    show()
    if on_ready:
        try:
            on_ready()
        except Exception:
            log.exception("窗口就绪回调失败")


def hide(note=None):
    hwnd = _hwnd()
    if hwnd:
        u32.ShowWindow(hwnd, SW_HIDE)
    _st["visible"] = False
    if note:
        log.info("%s", note)


def _bring_to_front(hwnd):
    """响应用户唤回：恢复焦点与前后顺序，不留下永久置顶或共享输入状态。"""
    u32.SetForegroundWindow(hwnd)
    if u32.GetForegroundWindow() == hwnd:
        return True
    current = k32.GetCurrentThreadId()
    foreground = u32.GetForegroundWindow()
    threads = {u32.GetWindowThreadProcessId(hwnd, None)}
    if foreground:
        threads.add(u32.GetWindowThreadProcessId(foreground, None))
    attached = []
    try:
        # 全局快捷键回调在工作线程；短暂连接输入队列后再请求激活。
        for thread in sorted(threads - {0, current}):
            if u32.AttachThreadInput(current, thread, True):
                attached.append(thread)
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
    finally:
        for thread in reversed(attached):
            u32.AttachThreadInput(current, thread, False)
    if u32.GetForegroundWindow() == hwnd:
        return True

    # 系统仍拒绝转移焦点时，至少把窗口显示到前面；立即恢复原置顶属性。
    was_topmost = bool(u32.GetWindowLongW(hwnd, -20) & 0x8)
    flags = SWP_NOMOVE | SWP_NOSIZE
    try:
        u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        u32.SetForegroundWindow(hwnd)
    finally:
        if not was_topmost:
            u32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
    return u32.GetForegroundWindow() == hwnd


def show():
    hwnd = _hwnd()
    if not hwnd:
        return
    u32.ShowWindow(hwnd, SW_RESTORE if u32.IsIconic(hwnd) else SW_SHOW)
    if not _bring_to_front(hwnd):
        log.debug("窗口已置前，系统暂未转移键盘焦点")
    _st["visible"] = True


def toggle():
    if _st["visible"]:
        hide("窗口已隐藏（Ctrl+Alt+H 可唤回）")
    else:
        show()


def is_visible():
    hwnd = _hwnd()
    return bool(hwnd and u32.IsWindowVisible(hwnd))


def overlay_status():
    overlay = _st.get("overlay")
    return overlay.status() if overlay else {"available": False, "visible": False, "opacity": 90}


def set_overlay_visible(visible):
    overlay = _st.get("overlay")
    if not overlay:
        raise RuntimeError("悬浮窗需要电脑主程序窗口，请等待窗口启动完成")
    return overlay.set_visible(visible)


def stop():
    """真正关窗并结束 GUI 主循环（服务器已停 / 退出指令后由 _server 调用）。"""
    _st["really_closing"] = True
    overlay = _st.get("overlay")
    if overlay:
        try:
            overlay.close()
        except Exception:
            log.debug("悬浮窗退出中", exc_info=True)
    hwnd = _hwnd()
    if hwnd:
        u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return
    win = _st.get("win")
    if win:  # 句柄还没找到就关窗：走 pywebview 自己的销毁路径
        try:
            win.destroy()
        except Exception:
            log.debug("关窗失败（进程退出时兜底）", exc_info=True)


def message_box(text, title="无障碍助手 PC"):
    """系统消息框：不依赖 GUI 线程，窗口起没起来都能弹。"""
    try:
        u32.MessageBoxW(0, text, title, 0x40 | 0x10000)
    except Exception:
        log.warning("%s：%s", title, text)
