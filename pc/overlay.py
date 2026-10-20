"""悬浮窗：整窗穿透的正文 + 仅按钮可命中的原生工具条。

两窗都先独立设置防截屏再显示，不读取主窗口保护开关。
所有原生对象只在 GUI 线程访问；外部开关串行执行，关闭页面后不再自动识别。

跨屏：工具条落到哪块显示器，就按那块屏的 DPI 重新排一次版。正文窗口的物理
尺寸 = 逻辑尺寸 × 该屏缩放，这样网页里的 340×300 视口在任何屏上都一致。
悬停：鼠标在按钮上停留 overlayHover 秒（默认 0.7）触发点击，按钮顶部画出进度；
拖动按钮是"悬停即跟随"，跟随中鼠标静止同样时长则退出跟随（进入跟随后先给
1 秒缓冲，期间不开始静止计时，免得刚触发还来不及移动就退出）。
"""
import ctypes
import json
import logging
import threading
import time
from ctypes import wintypes

from . import win_stealth

log = logging.getLogger(__name__)
u32 = ctypes.WinDLL("user32", use_last_error=True)
shcore = ctypes.WinDLL("shcore", use_last_error=True)
u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.UINT]
u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.BYTE,
                                           wintypes.DWORD]
u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
u32.GetWindowLongW.restype = ctypes.c_long
u32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
u32.GetDpiForWindow.argtypes = [wintypes.HWND]
u32.GetDpiForWindow.restype = wintypes.UINT
u32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
u32.MonitorFromWindow.restype = wintypes.HMONITOR
shcore.GetDpiForMonitor.argtypes = [wintypes.HMONITOR, ctypes.c_int,
                                    ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
shcore.GetDpiForMonitor.restype = ctypes.c_long

WIDTH, HEIGHT, BAR_HEIGHT = 340, 300, 32
GAP = 6
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x8000000
MONITOR_DEFAULTTONEAREST = 2
# (名称, 文字, 说明, 左边距, 宽度)；value 只是数值显示，不接鼠标。
LAYOUT = (
    ("drag", "拖动", "按住拖动；悬停后跟随鼠标", 4, 46),
    ("up", "↑", "向上翻页", 56, 30),
    ("down", "↓", "向下翻页", 92, 30),
    ("retry", "识别", "截屏并识别屏幕内容（不连手机也能用）", 128, 54),
    ("minus", "−", "降低不透明度", 188, 26),
    ("value", "", "当前不透明度", 218, 52),
    ("plus", "+", "提高不透明度", 274, 26),
    ("close", "×", "关闭悬浮窗", 306, 30),
)
BUTTONS = tuple(item for item in LAYOUT if item[0] != "value")
FONT_SIZES = {"drag": 13, "up": 15, "down": 15, "retry": 13,
              "minus": 17, "value": 11, "plus": 17, "close": 16}
ALPHA_STEP = 10
MIN_OPACITY, MAX_OPACITY = 30, 100
DEFAULT_OPACITY = 90
DEFAULT_HOVER = 0.7
DRAG_GRACE = 1.0  # 秒：刚进入跟随后先留一段缓冲，期间不累计"静止释放"时间


def drag_progress(now, started, last_move, hold_ms, grace=DRAG_GRACE):
    """跟随拖动的静止时长进度（0~1）；触发后 grace 秒内固定为 0。

    用户刚触发跟随时手往往还没开始移动，直接按 hold_ms 计时会立刻判定"静止"
    并退出跟随。静止计时从 max(最后一次移动, 触发时刻 + grace) 才开始累计。
    """
    count_from = max(last_move, started + grace)
    return max(0.0, min(1.0, (now - count_from) * 1000 / hold_ms))


def normalize_opacity(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_OPACITY
    if not MIN_OPACITY <= value <= MAX_OPACITY:
        return DEFAULT_OPACITY
    return round(value / 10) * 10


def adjust_opacity(value, delta):
    return max(MIN_OPACITY, min(MAX_OPACITY, value + delta))


def hover_milliseconds(cfg):
    """设置项 overlayHover（秒）→ 毫秒，越界与脏值回落到 0.7 秒。"""
    try:
        value = float((cfg.get("settings") or {}).get("overlayHover", DEFAULT_HOVER))
    except (TypeError, ValueError):
        value = DEFAULT_HOVER
    return max(0.2, min(3.0, value)) * 1000


def protect_window(hwnd, passthrough, opacity):
    """必须读回保护与穿透属性后才允许显示。"""
    win_stealth.strip_taskbar(hwnd)
    ex = u32.GetWindowLongW(hwnd, -20) | WS_EX_LAYERED | WS_EX_NOACTIVATE
    ex = ex | WS_EX_TRANSPARENT if passthrough else ex & ~WS_EX_TRANSPARENT
    u32.SetWindowLongW(hwnd, -20, ex)
    if not u32.SetLayeredWindowAttributes(hwnd, 0, round(opacity * 255 / 100), 2):
        log.error("设置悬浮窗透明度失败：错误 %s，样式 %#x",
                  ctypes.get_last_error(), u32.GetWindowLongW(hwnd, -20))
        raise RuntimeError("系统未能设置悬浮窗透明度")
    actual = u32.GetWindowLongW(hwnd, -20)
    if bool(actual & WS_EX_TRANSPARENT) != passthrough or not actual & WS_EX_LAYERED:
        raise RuntimeError("系统未能设置悬浮窗点击穿透")
    state = win_stealth.set_anti_capture(hwnd, True)
    if state not in ("excluded", "monitor"):
        raise RuntimeError("系统未能启用悬浮窗防截屏，已保持关闭")
    return state


class FloatingAnswer:
    def __init__(self, cfg, owner, on_change, save):
        self.cfg, self.owner = cfg, owner
        self.on_change, self.save = on_change, save
        self.window = self.bar = None
        self.hwnd = self.bar_hwnd = 0
        self.opacity = normalize_opacity((cfg.get("overlay") or {}).get("opacity", DEFAULT_OPACITY))
        self.visible = False
        self.error = ""
        self.protection = "off"
        self.scale = 1.0
        self._operation = threading.Lock()
        self._disposed = False
        self._syncing = False
        self._buttons = {}      # 可点击控件
        self._controls = {}     # 可点击控件 + 数值标签
        self._progress = {}     # 每个按钮顶部的进度条 Panel
        self._hover_progress = {}
        self._fonts = []
        self._region = None
        self._timer = None
        self._hovered = None
        self._hover_start = 0.0
        self._suppress = None
        self._dragging = False
        self._grab = (0, 0)
        self._last_cursor = None
        self._last_move = 0.0
        self._drag_start = 0.0

    def status(self):
        return {"available": not self._disposed, "visible": self.visible,
                "opacity": self.opacity, "protection": self.protection, "error": self.error}

    def _invoke(self, action):
        from System import Action
        form = self.owner.native
        if form.InvokeRequired:
            errors = []

            def wrapped():
                try:
                    action()
                except Exception as exc:
                    errors.append(exc)

            form.Invoke(Action(wrapped))
            if errors:
                raise errors[0]
        else:
            action()

    def set_visible(self, visible):
        with self._operation:
            if self._disposed:
                raise RuntimeError("电脑端正在退出")
            try:
                if visible and self.window is None:
                    self._create()
                self._invoke(lambda: self._show() if visible else self._hide())
                self.error = ""
            except Exception as exc:
                self.error = str(exc)
                try:
                    self._invoke(self._hide)
                except Exception:
                    log.debug("悬浮窗打开失败后的清理也失败", exc_info=True)
                log.exception("悬浮窗未能打开")
                raise
            finally:
                self.on_change()
        return self.status()

    def _create(self):
        import webview

        port = (self.cfg.get("server") or {}).get("port", 8765)
        self.window = webview.create_window(
            "", f"http://127.0.0.1:{port}/?overlay=1", width=WIDTH, height=HEIGHT,
            min_size=(240, 160), hidden=True, frameless=True, resizable=False,
            on_top=True, focus=False, easy_drag=False, background_color="#141414",
            text_select=False, confirm_close=False)
        self.window.events.loaded.wait(20)
        if not self.window.events.loaded.is_set() or self.window.native is None:
            failed, self.window = self.window, None
            failed.destroy()
            raise RuntimeError("悬浮页面加载超时，请重试")
        self._invoke(self._create_bar)

    def _monitor_dpi(self, hwnd):
        try:
            monitor = u32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            dx, dy = wintypes.UINT(), wintypes.UINT()
            if shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
                if dx.value:
                    return dx.value
        except Exception:
            log.debug("读取显示器 DPI 失败", exc_info=True)
        try:
            return u32.GetDpiForWindow(hwnd) or 96
        except Exception:
            return 96

    def _create_bar(self):
        from System.Drawing import (Color, ContentAlignment, Font, FontStyle, GraphicsUnit,
                                    Point, Rectangle, Region, Size)
        from System.Windows.Forms import (AutoScaleMode, Button, FlatStyle, Form,
                                         FormBorderStyle, FormStartPosition, Label,
                                         MouseButtons, Panel, Screen, Timer)

        # 先改窗体属性再取句柄：WinForms 在 ShowInTaskbar 变化时会重建 HWND。
        self.window.native.ShowInTaskbar = False
        self.hwnd = int(self.window.native.Handle.ToInt64())
        owner_hwnd = int(self.owner.native.Handle.ToInt64())
        self.scale = max(1.0, self._monitor_dpi(owner_hwnd) / 96)
        self.bar = Form()
        self.bar.Text = ""
        self.bar.FormBorderStyle = getattr(FormBorderStyle, "None")
        self.bar.StartPosition = FormStartPosition.Manual
        self.bar.AutoScaleMode = getattr(AutoScaleMode, "None")
        self.bar.ShowInTaskbar = False
        self.bar.TopMost = True
        self.bar.BackColor = Color.FromArgb(20, 20, 20)
        self.bar_hwnd = int(self.bar.Handle.ToInt64())
        for name, label, description, x, width in LAYOUT:
            if name == "value":
                control = Label()
                control.TextAlign = ContentAlignment.MiddleCenter
                control.BackColor = Color.FromArgb(20, 20, 20)
                control.ForeColor = Color.FromArgb(230, 230, 230)
                control.AutoSize = False
            else:
                control = Button()
                control.Text, control.AccessibleName = label, description
                control.TabStop = False
                control.FlatStyle = FlatStyle.Flat
                control.FlatAppearance.BorderSize = 1
                control.FlatAppearance.BorderColor = Color.FromArgb(56, 56, 56)
                control.FlatAppearance.MouseOverBackColor = Color.FromArgb(42, 42, 42)
                control.BackColor = Color.FromArgb(24, 24, 24)
                control.ForeColor = Color.FromArgb(224, 224, 224)
                self._buttons[name] = control
                if name == "drag":
                    control.MouseDown += lambda sender, event: self._start_native_drag(event)
                else:
                    control.Click += lambda sender, event, action=name: self._click(action)
                # 进度条是工具条上的独立 Panel，浮在按钮顶部 3px；不接鼠标。
                strip = Panel()
                strip.Enabled = False
                strip.Visible = False
                strip.BackColor = Color.FromArgb(20, 20, 20)
                strip.Paint += lambda sender, event, key=name: self._paint_progress(key, event)
                self._progress[name] = strip
                self.bar.Controls.Add(strip)
            self.bar.Controls.Add(control)
            self._controls[name] = control
        self._layout()
        area = Screen.FromControl(self.owner.native).WorkingArea
        self.bar.Location = Point(area.Right - self._px(WIDTH + 24), area.Top + self._px(48))
        self.bar.LocationChanged += lambda *_: self._sync_position()
        self.bar.MouseCaptureChanged += lambda *_: self._native_drag_ended()
        self._timer = Timer()
        self._timer.Interval = 25
        self._timer.Tick += lambda *_: self._tick()
        self._sync_position()

    def _px(self, value):
        return round(value * self.scale)

    def _layout(self):
        """按当前 scale 重排工具条；改变 DPI 后只走这一条路径。"""
        from System.Drawing import Font, FontStyle, GraphicsUnit, Rectangle, Region, Size

        if not self.bar:
            return
        old_fonts = self._fonts
        self._fonts = []
        self.bar.ClientSize = Size(self._px(WIDTH), self._px(BAR_HEIGHT))
        region = Region()
        region.MakeEmpty()
        for name, label, description, x, width in LAYOUT:
            control = self._controls[name]
            control.Bounds = Rectangle(self._px(x), 0, self._px(width), self._px(BAR_HEIGHT))
            font = Font("Microsoft YaHei UI", FONT_SIZES[name] * self.scale,
                        FontStyle.Regular, GraphicsUnit.Pixel)
            self._fonts.append(font)
            control.Font = font
            region.Union(control.Bounds)
            strip = self._progress.get(name)
            if strip is not None:
                strip.Bounds = Rectangle(self._px(x), 0, self._px(width), max(2, self._px(3)))
                strip.BringToFront()
        old_region = self._region
        self._region = region
        self.bar.Region = region
        if old_region is not None:
            try:
                old_region.Dispose()
            except Exception:
                pass
        for font in old_fonts:
            try:
                font.Dispose()
            except Exception:
                pass
        self._update_opacity_label()

    def _update_opacity_label(self):
        label = self._controls.get("value")
        if label is not None:
            label.Text = f"不透明度\n{self.opacity}%"
            label.AccessibleName = f"不透明度 {self.opacity}%"
        minus, plus = self._buttons.get("minus"), self._buttons.get("plus")
        if minus is not None:
            minus.Enabled = self.opacity > MIN_OPACITY
        if plus is not None:
            plus.Enabled = self.opacity < MAX_OPACITY

    def _clamp(self):
        from System.Drawing import Point
        from System.Windows.Forms import Screen

        area = Screen.FromControl(self.bar).WorkingArea
        width = self.bar.Width
        height = self.bar.Height + self._px(GAP) + self._px(HEIGHT)
        x = min(max(area.Left, self.bar.Left), area.Right - width)
        y = min(max(area.Top, self.bar.Top), area.Bottom - height)
        if (x, y) != (self.bar.Left, self.bar.Top):
            self.bar.Location = Point(x, y)

    def _sync_position(self):
        if not self.bar or not self.hwnd or self._syncing:
            return
        self._syncing = True
        try:
            scale = max(1.0, self._monitor_dpi(self.bar_hwnd or self.hwnd) / 96)
            if abs(scale - self.scale) > 0.01:
                log.debug("悬浮窗跨屏：DPI 缩放 %s → %s", self.scale, scale)
                self.scale = scale
                self._layout()
            self._clamp()
            x, y = self.bar.Left, self.bar.Top
            u32.SetWindowPos(self.hwnd, -1, x, y + self.bar.Height + self._px(GAP),
                             self._px(WIDTH), self._px(HEIGHT), 0x10)
        finally:
            self._syncing = False

    def _set_progress(self, name, value):
        self._hover_progress[name] = value
        strip = self._progress.get(name)
        if strip is None:
            return
        visible = value > 0.001
        if strip.Visible != visible:
            strip.Visible = visible
        if visible:
            strip.Invalidate()

    def _paint_progress(self, name, event):
        from System.Drawing import Color, SolidBrush

        strip = self._progress.get(name)
        if strip is None:
            return
        graphics = event.Graphics
        width, height = strip.Width, strip.Height
        brushes = []
        try:
            track = SolidBrush(Color.FromArgb(56, 56, 56))
            fill = SolidBrush(Color.FromArgb(222, 222, 222))
            shine = SolidBrush(Color.FromArgb(255, 255, 255))
            brushes = [track, fill, shine]
            graphics.FillRectangle(track, 0, 0, width, height)
            progress = max(0.0, min(1.0, self._hover_progress.get(name, 0.0)))
            filled = int(width * progress)
            if filled <= 0:
                return
            graphics.FillRectangle(fill, 0, 0, filled, height)
            # 跑马灯：一段高亮在已填充部分来回扫过，提示"还要等一会儿"。
            span = max(6, width // 6)
            x = (int(time.monotonic() * 1000) // 6) % (width + span) - span
            left, right = max(0, x), min(filled, x + span)
            if right > left:
                graphics.FillRectangle(shine, left, 0, right - left, height)
        finally:
            for brush in brushes:
                try:
                    brush.Dispose()
                except Exception:
                    pass

    def _button_at(self, point):
        for name, button in self._buttons.items():
            if not button.Enabled:
                continue
            if button.RectangleToScreen(button.ClientRectangle).Contains(point):
                return name
        return None

    def _tick(self):
        if not self.visible or self._disposed:
            return
        try:
            from System.Windows.Forms import Cursor

            now = time.monotonic()
            hold_ms = hover_milliseconds(self.cfg)
            point = Cursor.Position
            if self._dragging:
                self._follow_drag(point, now, hold_ms)
                return
            hovered = self._button_at(point)
            if hovered != self._hovered:
                if self._hovered:
                    self._set_progress(self._hovered, 0.0)
                self._hovered = hovered
                self._hover_start = now
                if hovered != self._suppress:
                    self._suppress = None
            if not hovered:
                return
            if hovered == self._suppress:
                self._set_progress(hovered, 0.0)
                return
            progress = min(1.0, (now - self._hover_start) * 1000 / hold_ms)
            self._set_progress(hovered, progress)
            if progress >= 1.0:
                self._set_progress(hovered, 0.0)
                self._hovered = None
                self._suppress = hovered
                self._trigger(hovered, point, now)
        except Exception:
            log.exception("悬浮窗悬停检测失败")

    def _trigger(self, name, point, now):
        if name == "drag":
            self._dragging = True
            self._drag_start = now
            self._grab = (point.X - self.bar.Left, point.Y - self.bar.Top)
            self._last_cursor = (point.X, point.Y)
            self._last_move = now
            log.debug("悬浮窗进入跟随拖动")
        else:
            self._action(name)

    def _follow_drag(self, point, now, hold_ms):
        from System.Drawing import Point
        from System.Windows.Forms import Screen

        position = (point.X, point.Y)
        if position != self._last_cursor:
            self._last_cursor = position
            self._last_move = now
            area = Screen.FromPoint(point).WorkingArea
            x = min(max(area.Left, point.X - self._grab[0]), area.Right - self.bar.Width)
            y = min(max(area.Top, point.Y - self._grab[1]), area.Bottom - self.bar.Height)
            self.bar.Location = Point(x, y)
            self._set_progress("drag", 0.0)
            return
        progress = drag_progress(now, self._drag_start, self._last_move, hold_ms)
        self._set_progress("drag", progress)
        if progress >= 1.0:
            self._dragging = False
            self._suppress = "drag"
            self._set_progress("drag", 0.0)
            log.debug("悬浮窗停止跟随拖动")

    def _start_native_drag(self, event):
        from System.Windows.Forms import MouseButtons

        if event.Button != MouseButtons.Left:
            return
        # 按住拖动期间关掉悬停触发；松手后要先把鼠标移开才会再次触发。
        self._suppress = "drag"
        self._hovered = None
        self._set_progress("drag", 0.0)
        u32.ReleaseCapture()
        u32.SendMessageW(self.bar_hwnd, 0xA1, 2, 0)  # WM_NCLBUTTONDOWN / HTCAPTION

    def _native_drag_ended(self):
        self._suppress = "drag"
        self._set_progress("drag", 0.0)

    def _click(self, name):
        self._suppress = name
        self._hovered = None
        self._set_progress(name, 0.0)
        self._action(name)

    def _action(self, action):
        if action == "close":
            self._hide()
            self.on_change()
        elif action in ("minus", "plus"):
            self._adjust_opacity(-ALPHA_STEP if action == "minus" else ALPHA_STEP)
        else:
            self._javascript(action)

    def _adjust_opacity(self, delta):
        new = adjust_opacity(self.opacity, delta)
        if new == self.opacity:
            return
        old = self.opacity
        self.opacity = new
        try:
            for hwnd in (self.hwnd, self.bar_hwnd):
                if hwnd and not u32.SetLayeredWindowAttributes(
                        hwnd, 0, round(new * 255 / 100), 2):
                    raise RuntimeError("系统未能设置悬浮窗透明度")
            self.save(new)
            self.error = ""
        except Exception as exc:
            log.exception("悬浮窗不透明度设置失败")
            self.opacity = old
            self.error = str(exc)
            for hwnd in (self.hwnd, self.bar_hwnd):
                if hwnd:
                    u32.SetLayeredWindowAttributes(hwnd, 0, round(old * 255 / 100), 2)
        self._update_opacity_label()
        self.on_change()

    def _show(self):
        if not self.window or not self.bar:
            return
        log.debug("悬浮窗显示：内容=%s 工具条=%s", self.hwnd, self.bar_hwnd)
        content = protect_window(self.hwnd, True, self.opacity)
        toolbar = protect_window(self.bar_hwnd, False, self.opacity)
        self.protection = "monitor" if "monitor" in (content, toolbar) else "excluded"
        self._update_opacity_label()
        self._sync_position()
        u32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
        u32.ShowWindow(self.bar_hwnd, 4)
        u32.SetWindowPos(self.bar_hwnd, -1, 0, 0, 0, 0, 0x13)
        self.visible = True
        self._hovered = None
        self._suppress = None
        self._dragging = False
        for name in self._progress:
            self._set_progress(name, 0.0)
        if self._timer is not None:
            self._timer.Start()
        self._javascript("resume")

    def _hide(self):
        if self._timer is not None:
            self._timer.Stop()
        self._dragging = False
        for name in self._progress:
            self._set_progress(name, 0.0)
        for hwnd in (self.hwnd, self.bar_hwnd):
            if hwnd:
                u32.ShowWindow(hwnd, 0)
        self.visible = False
        self._javascript("pause")

    def _javascript(self, action):
        # evaluate_js 等待脚本结果，不能阻塞 GUI 线程。
        window = self.window
        if not window:
            return

        def run():
            try:
                window.evaluate_js("window.overlayAction?.(" + json.dumps(action) + ")")
            except Exception:
                log.debug("悬浮页面正在关闭或尚未就绪", exc_info=True)

        threading.Thread(target=run, daemon=True).start()

    def close(self):
        with self._operation:
            self._disposed = True

            def dispose():
                self._hide()
                if self._timer is not None:
                    try:
                        self._timer.Stop()
                        self._timer.Dispose()
                    except Exception:
                        pass
                    self._timer = None
                if self.bar:
                    self.bar.Dispose()
                    self.bar = None

            self._invoke(dispose)
            if self.window:
                self.window.destroy()
                self.window = None
