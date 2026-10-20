# -*- coding: utf-8 -*-
"""主窗口隐身：任务栏 / Alt-Tab 隐身 + 防截屏开关。

Win11 23H2（build 22631，2026-10）实测：

- WS_EX_TOOLWINDOW + 去 WS_EX_APPWINDOW：窗口从任务栏 / Alt-Tab / 任务视图消失
  （对普通窗口跨进程有效）。
- SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)：自家普通窗口有效——被
  PrintWindow / DXGI 桌面复制 / Windows.Graphics.Capture 隐藏；注意属主进程自己
  永远能截到自己的窗口（验证防截屏不能用同进程自截）。边界：屏幕 DC 的 GDI 位块
  复制（BitBlt，含 CAPTUREBLT）不受 WDA 过滤，那种老式抓屏路径仍能看到窗口
  （2026-10 实测，属系统机制范围）。
- 空标题：Chromium（Edge/Chrome）「共享窗口」选择器按 kIgnoreUntitled 过滤掉
  无标题窗口，留空等于从那个列表里消失。

历史备注：旧版（v1.2 及以前）主界面是控制台窗口，控制台拒绝 WDA、且 Windows
Terminal 承载时连任务栏隐身都做不了——那时靠"默认终端改 conhost / 防截屏即隐藏
控制台"绕路。v1.3 起主界面是无控制台的自家窗口，以上 API 全部直接可用，那些
前置条件与绕路一并作废。
"""
import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

u32 = ctypes.WinDLL("user32", use_last_error=True)
# 显式声明指针宽度参数：默认 c_int 会在 64 位进程里截断窗口句柄。
u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.UINT]
u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
u32.GetWindowLongW.restype = ctypes.c_long
u32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
u32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
u32.GetAncestor.restype = wintypes.HWND
u32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
u32.SetWindowDisplayAffinity.restype = wintypes.BOOL
u32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u32.GetWindowDisplayAffinity.restype = wintypes.BOOL

GWL_EXSTYLE = -20
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x2, 0x1, 0x4, 0x20
WDA_NONE, WDA_MONITOR, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x1, 0x11

# 防截屏各状态（实时读回）： (界面行文案, 按钮文字, 日志短语)
# 三个地方共用同一张表，避免窗口、按钮、日志各说各话。
_CAP_INFO = {
    "excluded": ("已开启 · 截图、录屏中隐藏", "关闭",
                 "防截屏已开启（截图、录屏中隐藏本窗口）"),
    "monitor": ("已开启 · 截图、录屏中显示黑块", "关闭",
                "防截屏已开启（截图、录屏中显示黑块）"),
    "off": ("已关闭 · 截图、录屏中可见", "开启",
            "防截屏已关闭（截图、录屏中显示本窗口）"),
    "denied": ("未生效 · 系统拒绝防截屏", "开启",
               "防截屏未生效（系统拒绝设置）"),
    "unknown": ("状态未知", "开启", "防截屏状态未知"),
}

_win = {"hwnd": 0}
_last = {"taskbar": "未检查", "taskbar_ok": None}


# ---------- Win32 封装 ----------

def top_hwnd_of_tk(root) -> int:
    """tkinter 顶层窗口的真实 HWND（带装饰时 winfo_id 是子窗口，要 GA_ROOT 上溯）。"""
    try:
        return int(u32.GetAncestor(int(root.winfo_id()), 2))
    except Exception:
        return 0


def strip_taskbar(hwnd: int) -> bool:
    """该窗口不再出现在任务栏 / Alt-Tab / 任务视图。幂等，返回是否设置成功。"""
    if not hwnd:
        return False
    ex = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex2 = (ex & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
    if ex2 != ex:
        u32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex2)
        u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
    now = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    return bool(now & WS_EX_TOOLWINDOW) and not (now & WS_EX_APPWINDOW)


def set_title(hwnd: int, text: str):
    try:
        u32.SetWindowTextW(hwnd, text)
    except Exception:
        pass


def set_anti_capture(hwnd: int, on: bool) -> str:
    """开关防截屏。返回 excluded / monitor / off / denied。

    excluded = WDA_EXCLUDEFROMCAPTURE（Win10 2004+，截图/录屏/共享里窗口消失）
    monitor  = 旧系统回退 WDA_MONITOR（截图里是黑块）
    denied   = 系统拒绝（罕见）
    """
    if not hwnd:
        return "denied"
    aff = WDA_EXCLUDEFROMCAPTURE if on else WDA_NONE
    u32.SetWindowDisplayAffinity(hwnd, aff)
    val = wintypes.UINT()
    u32.GetWindowDisplayAffinity(hwnd, ctypes.byref(val))
    if val.value == aff:
        return "excluded" if on else "off"
    if not on:
        return "off"
    u32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR)
    val = wintypes.UINT()
    u32.GetWindowDisplayAffinity(hwnd, ctypes.byref(val))
    return "monitor" if val.value == WDA_MONITOR else "denied"


def affinity_name() -> str:
    """主窗口当前防截屏状态（实时读回）：excluded / monitor / off / unknown。"""
    hwnd = _win["hwnd"]
    if not hwnd or not u32.IsWindow(hwnd):
        return "unknown"
    val = wintypes.UINT()
    if not u32.GetWindowDisplayAffinity(hwnd, ctypes.byref(val)):
        return "unknown"
    return {WDA_NONE: "off", WDA_MONITOR: "monitor",
            WDA_EXCLUDEFROMCAPTURE: "excluded"}.get(val.value, "unknown")


def anti_capture_active() -> bool:
    return affinity_name() in ("excluded", "monitor")


def cap_row(state=None):
    """「防截屏」状态行：返回 (行文案, 按钮文字)；state 缺省时实时读回。"""
    text, btn, _ = _CAP_INFO.get(state or affinity_name(), _CAP_INFO["unknown"])
    return text, btn


def cap_phrase(state=None):
    """防截屏的日志短语（“防截屏已开启（…）”这类），开关操作后播报用。"""
    return _CAP_INFO.get(state or affinity_name(), _CAP_INFO["unknown"])[2]


# ---------- 主流程 ----------

def attach(hwnd: int):
    """把主窗口的 HWND 交给本模块（界面构建完、显示前调用）。"""
    _win["hwnd"] = int(hwnd or 0)


def apply(cfg: dict) -> str:
    """按配置把隐身属性套到主窗口；返回一行结论（日志里用）。"""
    st = {"enabled": True, "anti_capture": True, "title": ""}
    st.update(cfg.get("stealth") or {})
    hwnd = _win["hwnd"]
    if not hwnd or not u32.IsWindow(hwnd):
        _last.update(taskbar="窗口未就绪", taskbar_ok=None)
        return "窗口未就绪"
    if not st["enabled"]:
        _last.update(taskbar="未启用（窗口会显示在任务栏）", taskbar_ok=None)
        return "隐身未启用（stealth.enabled=false）"
    ok_taskbar = strip_taskbar(hwnd)
    set_title(hwnd, str(st.get("title") or ""))
    wda = set_anti_capture(hwnd, bool(st["anti_capture"]))
    cap_txt = cap_phrase(wda)
    if ok_taskbar:
        _last.update(taskbar="任务栏和 Alt-Tab 里已隐藏", taskbar_ok=True)
        return f"任务栏已隐藏 · {cap_txt}"
    _last.update(taskbar="任务栏隐藏失败（详见日志）", taskbar_ok=False)
    return "任务栏隐藏失败"


def taskbar_text() -> str:
    """给界面「窗口隐身」行用的一行摘要（防截屏另有独立一行，见 cap_row）。"""
    return _last["taskbar"]


def taskbar_ok():
    return _last["taskbar_ok"]


def protect_tk_window(root, cfg: dict) -> str:
    """给其他自家 tkinter 窗口（如 F9 框选遮罩）按其配置加防截屏。"""
    try:
        st = {"enabled": True, "anti_capture": True}
        st.update(cfg.get("stealth") or {})
        if not (st["enabled"] and st["anti_capture"]):
            return "off"
        hwnd = top_hwnd_of_tk(root)
        return set_anti_capture(hwnd, True) if hwnd else "none"
    except Exception as e:
        log.debug("遮罩防截屏设置失败：%s", e)
        return "none"
