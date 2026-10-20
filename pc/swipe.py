"""横向滑动的真实判定与采样；正式运行、调试页共用。"""
import logging
import math
import sys
import time

log = logging.getLogger("pc.swipe")

# 横向平移判定用的阈值。都是模糊门槛，不是精确判据：人手划不出直线，
# 所以只要求"大致如此"。调紧了会漏触发（再划一次就行），调松了会误触发（平白多截一张图）。
SWIPE_POLL = 0.02       # 光标采样间隔（秒）
SWIPE_WINDOW = 2.6      # 轨迹窗口下限（秒）；实际窗口见 swipe_window()
SWIPE_STILL = 0.25      # 光标停这么久算手势结束，之后才能重新触发
SWIPE_DIST_RATIO = 0.137  # 默认划动距离 = 屏幕宽度 × 这个比例（2560 宽的屏约 350px）
SWIPE_MAX_DIST = 700    # 划动距离上限（px）：再宽的屏也不必让人划到手酸
SWIPE_LIMITS = {
    "min_samples": 6,
    "min_span": 0.25,   # 手势至少持续这么久，免得"甩一下"也算
    "min_speed": 150.0,  # px/s：比这慢，算随手挪动
    "max_speed": 380.0,  # px/s：比这快，算甩，不算平移
    "max_slope": 0.15,  # |纵向位移| / |横向位移| 上限，保证大体是横着走
    "max_path": 1.5,    # 路径长度 / 直线位移 上限，排除来回蹭
}

DEFAULT_PARAMS = {
    "min_distance": None, "cooldown": 2.0, "min_speed": 150.0, "max_speed": 380.0,
    "max_slope": 0.15, "max_path": 1.5, "min_span": 0.25, "still_time": 0.25,
}
PARAM_BOUNDS = {
    "min_distance": (50, 2000), "cooldown": (0.2, 10), "min_speed": (20, 2500),
    "max_speed": (40, 5000), "max_slope": (0.02, 0.8), "max_path": (1, 3),
    "min_span": (0.1, 3), "still_time": (0.1, 1.5),
}
PARAM_LABELS = {
    "min_distance": "滑动距离", "cooldown": "触发冷却", "min_speed": "最低速度",
    "max_speed": "最高速度", "max_slope": "纵向偏移容忍", "max_path": "路径偏移容忍",
    "min_span": "最短滑动时长", "still_time": "停止后恢复时间",
}


def normalize_params(values, strict=False):
    """校验完整参数。接口严格拒绝错误；加载旧配置时对非法值使用默认。"""
    if not isinstance(values, dict):
        if strict:
            raise ValueError("参数必须是对象")
        values = {}
    if strict and set(values) - set(DEFAULT_PARAMS):
        raise ValueError("参数包含不支持的字段")
    result = {}
    for key, default in DEFAULT_PARAMS.items():
        value = values.get(key, default)
        if key == "min_distance" and value is None:
            result[key] = None
            continue
        lo, hi = PARAM_BOUNDS[key]
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and lo <= value <= hi
        if not valid and strict:
            raise ValueError(f"{PARAM_LABELS[key]}需在 {lo:g}～{hi:g} 之间")
        result[key] = float(value) if valid else default
    if result["min_speed"] > result["max_speed"]:
        if strict:
            raise ValueError("最低速度不能高于最高速度")
        result.update(min_speed=DEFAULT_PARAMS["min_speed"], max_speed=DEFAULT_PARAMS["max_speed"])
    return result


def parse_import(data):
    """导入有版本标记的参数，拒绝将整个程序配置误当成滑动参数。"""
    if not isinstance(data, dict) or data.get("type") != "swipe-parameters" or data.get("version") != 1:
        raise ValueError("请粘贴滑动调试页复制的参数")
    return normalize_params(data.get("parameters"), strict=True)


def parameter_document(params):
    return {"type": "swipe-parameters", "version": 1, "parameters": normalize_params(params)}


def screen_width():
    if sys.platform == "win32":
        import ctypes
        return int(ctypes.windll.user32.GetSystemMetrics(0)) or 1920
    return 1920


def gesture_metrics(samples):
    if len(samples) < 2:
        return {"n": len(samples), "span": 0.0, "dist": 0.0, "drift": 0.0, "speed": 0.0, "pathRatio": 0.0}
    t0, x0, y0 = samples[0]
    t1, x1, y1 = samples[-1]
    span, dist = t1 - t0, abs(x1 - x0)
    path = sum(abs(b[1] - a[1]) for a, b in zip(samples, samples[1:]))
    return {"n": len(samples), "span": span, "dist": dist, "drift": abs(y1 - y0),
            "speed": dist / span if span > 0 else 0.0, "pathRatio": path / dist if dist else 0.0}


def default_min_distance(screen_w):
    """默认门槛距离：屏幕宽度的一小段——小屏不用划太长，超宽屏也不至于划到手腕酸。"""
    return max(160, min(int(screen_w * SWIPE_DIST_RATIO), SWIPE_MAX_DIST))


def swipe_window(min_distance, limits=None):
    """轨迹窗口（秒）：必须装得下"以最慢速度划完门槛距离"的那一笔。

    窗口比这短，最慢速度就永远划不到门槛（早期采样点会被提前丢掉），文档里的速度下限
    也就成了空话——门槛越长这个坑越明显。所以窗口跟着门槛走，而不是钉死一个常数。
    """
    lim = limits or SWIPE_LIMITS
    return max(SWIPE_WINDOW, min_distance / lim["min_speed"] * 1.15, lim["min_span"] * 1.15)


def _notify(on_sample, tracker, x, y, pressed, now, fired):
    """回调给试验台看的那一帧。它只是观察者，出问题不能把监测线程带走。"""
    if not on_sample:
        return
    try:
        on_sample(tracker, x, y, pressed, now, fired)
    except Exception as e:
        if not _notify.warned:
            _notify.warned = True
            log.warning("试验台回调出错（不再重复提示）：%s", e)


_notify.warned = False


# 鼠标按键的虚拟键码：左/右/中/侧键 1/侧键 2
MOUSE_BUTTONS = (0x01, 0x02, 0x04, 0x05, 0x06)


def mouse_button_down(user32):
    """鼠标是否正按着任意一键。

    这是区分「划一下」和「按住拖」最简单也最准的判据：选文字、拖窗口、拖滚动条
    全程都按着键，而刻意横向划一下是松开键平移鼠标。命中就说明当前是在拖拽。
    """
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in MOUSE_BUTTONS)


def swipe_hit(samples, min_distance, limits=None):
    """判断一段光标轨迹像不像"刻意横向平移"。samples 为按时间递增的 [(t, x, y)]。

    四个条件都要大致满足：横向位移够长、大体横着走、速度落在"划一下"的区间、路径别来回蹭。
    纯函数，便于单测。
    """
    lim = limits or SWIPE_LIMITS
    if len(samples) < lim["min_samples"]:
        return False
    t0, x0, y0 = samples[0]
    t1, x1, y1 = samples[-1]
    span = t1 - t0
    if span < lim["min_span"]:
        return False
    dist = abs(x1 - x0)
    if dist < min_distance:
        return False
    if abs(y1 - y0) > lim["max_slope"] * dist:
        return False
    speed = dist / span
    if not (lim["min_speed"] <= speed <= lim["max_speed"]):
        return False
    path = sum(abs(samples[i][1] - samples[i - 1][1]) for i in range(1, len(samples)))
    return path <= lim["max_path"] * dist


class SwipeTracker:
    """横向平移手势的状态机。

    输入是每一帧的光标位置和「鼠标键此刻是否按着」，输出是「这一帧是否刚完成一次
    划动手势」。抽成独立对象是为了能脱开真实鼠标做单测——测试里没法真的按一下键再动鼠标。
    """

    def __init__(self, min_distance, limits=None, still_time=SWIPE_STILL):
        self.min_distance = min_distance
        self.limits = limits or SWIPE_LIMITS
        self.still_time = still_time
        self.cooldown_remaining = 0.0
        self.samples = []       # [(t, x, y)]
        self.prev = None
        self.still_since = None
        self.armed = True
        self.last_dist = 0.0
        self.last_span = 0.0
        # 触发那一次的完整实测指标。feed() 触发时会立刻清空轨迹，所以数据得先存下来，
        # 否则事后（日志、试验台）读到的只会是一段空样本。
        self.last_gesture = {}
        self.last_trail = []

    @property
    def window(self):
        """当前门槛对应的轨迹窗口（门槛变了窗口跟着变，见 swipe_window）。"""
        return swipe_window(self.min_distance, self.limits)

    def reset(self):
        self.samples.clear()
        self.prev = None
        self.still_since = None
        self.armed = True

    def feed(self, now, x, y, button_down):
        """喂一帧光标状态，返回 True 表示这一帧刚完成一次划动（不含冷却判断）。"""
        # 按着鼠标键 = 在拖拽（选文字 / 拖窗口 / 拖滚动条），不是划动。
        # 这条比任何"像不像划动"的阈值都准：刻意划一下是松开键平移鼠标的。
        if button_down:
            self.samples.clear()
            self.still_since = None
            self.prev = (x, y)
            return False

        # 光标停住 → 手势结束：清空轨迹并重新武装
        if self.prev is not None and abs(x - self.prev[0]) + abs(y - self.prev[1]) <= 1:
            if self.still_since is None:
                self.still_since = now
            if now - self.still_since >= self.still_time:
                self.samples.clear()
                self.armed = True
        else:
            self.still_since = None
        self.prev = (x, y)
        if not self.armed:
            return False

        self.samples.append((now, x, y))
        window = self.window
        while self.samples and now - self.samples[0][0] > window:
            self.samples.pop(0)
        if not swipe_hit(self.samples, self.min_distance, self.limits):
            return False

        t0, x0, y0 = self.samples[0]
        t1, x1, y1 = self.samples[-1]
        span = t1 - t0
        dist = abs(x1 - x0)
        drift = abs(y1 - y0)
        path = sum(abs(self.samples[i][1] - self.samples[i - 1][1])
                   for i in range(1, len(self.samples)))
        self.last_span = span
        self.last_dist = dist
        self.last_gesture = {
            "n": len(self.samples),
            "dist": dist,
            "span": span,
            "drift": drift,
            "speed": dist / span if span else 0.0,
            "pathRatio": (path / dist) if dist else 0.0,
        }
        self.last_trail = [[x - x0, y - y0] for _, x, y in self.samples[-160:]]
        self.armed = False
        self.samples.clear()
        return True


def run_swipe_monitor(cfg, handler, stop_event, on_sample=None, paused=None):
    """横向平移监测主循环——**实际生效的就是这个循环**。

    抽成独立函数是为了让试验台（tools/swipe_lab.py）复用同一份代码：那里能边划边看
    每一项阈值过没过，测出来的行为跟真实运行完全一致，而不是"另一个实现的脾气"。
    on_sample(tracker, x, y, pressed, now, fired) 每帧回调一次，只给试验台用，正常运行为 None。
    cfg 每轮重新读，所以试验台能现场改阈值。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        log.warning("横向平移截图：组件缺失，功能不可用")
        return
    user32 = ctypes.windll.user32

    # 默认距离按屏幕宽度的一小段给：小屏不用划太长，超宽屏也不至于划到手腕酸
    screen_w = int(user32.GetSystemMetrics(0)) or 1920
    default_dist = default_min_distance(screen_w)

    pt = wintypes.POINT()
    tracker = SwipeTracker(default_dist)
    previous_params = None
    last = 0.0
    log.debug("横向平移截图监测已启动（划动距离约 %dpx、速度 %.0f~%.0fpx/s，配置 swipe_snap.enabled 可关）",
             default_dist, SWIPE_LIMITS["min_speed"], SWIPE_LIMITS["max_speed"])
    while not stop_event.is_set():
        stop_event.wait(SWIPE_POLL)
        sc = cfg.get("swipe_snap") or {}
        if (paused and paused()) or not sc.get("enabled", True):
            tracker.reset()
            _notify(on_sample, tracker, -1, -1, False, time.time(), False)
            continue
        if not user32.GetCursorPos(ctypes.byref(pt)):
            continue
        now = time.time()
        params = normalize_params(sc)
        if params != previous_params:
            tracker.reset()
            tracker.min_distance = params["min_distance"] or default_dist
            tracker.limits = {**SWIPE_LIMITS, **{k: params[k] for k in SWIPE_LIMITS if k in params}}
            tracker.still_time = params["still_time"]
            previous_params = params
        pressed = mouse_button_down(user32)
        candidate = tracker.feed(now, pt.x, pt.y, pressed)
        fired = candidate and now - last >= params["cooldown"]
        if fired:
            last = now
        tracker.cooldown_remaining = max(0.0, params["cooldown"] - (now - last))
        _notify(on_sample, tracker, pt.x, pt.y, pressed, now, fired)
        if not fired:
            continue
        if not paused or not paused():
            if on_sample is None:
                log.info("检测到横向平移手势（%dpx / %.2fs）", int(tracker.last_dist), tracker.last_span)
            handler()


