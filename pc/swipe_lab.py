"""本机滑动试调：会话草稿、正式参数与实际鼠标截图相互隔离。"""
import asyncio
import contextlib
import ipaddress
import json
import logging
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from .swipe import (DEFAULT_PARAMS, PARAM_BOUNDS, default_min_distance, gesture_metrics,
                    normalize_params, parse_import, run_swipe_monitor, screen_width)

log = logging.getLogger(__name__)


def local_only(request):
    try:
        local = ipaddress.ip_address(request.remote or "").is_loopback
        host = request.url.host
        local_host = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = local_host = False
    origin = request.headers.get("Origin")
    source = urlsplit(origin) if origin else None
    if not local or not local_host or (source and (source.netloc != request.host or source.scheme != request.scheme)):
        raise web.HTTPForbidden(text="调试与参数设置仅限电脑本机使用")


def snapshot(tracker, pressed, fired, count, recent):
    metrics = tracker.last_gesture if fired else gesture_metrics(tracker.samples)
    lim, need = tracker.limits, tracker.min_distance
    checks = [
        ("取样点", metrics["n"], lim["min_samples"], metrics["n"] >= lim["min_samples"], "个", "至少"),
        ("滑动时长", metrics["span"], lim["min_span"], metrics["span"] >= lim["min_span"], "秒", "至少"),
        ("横向距离", metrics["dist"], need, metrics["dist"] >= need, "像素", "至少"),
        ("纵向偏移", metrics["drift"], lim["max_slope"] * metrics["dist"],
         metrics["drift"] <= lim["max_slope"] * metrics["dist"], "像素", "至多"),
        ("路线偏移", metrics["pathRatio"], lim["max_path"],
         metrics["dist"] > 0 and metrics["pathRatio"] <= lim["max_path"], "倍", "至多"),
    ]
    result = [{"label": label, "value": round(value, 2), "limit": f"{prefix} {bound:.2f} {unit}",
               "unit": unit, "ok": ok} for label, value, bound, ok, unit, prefix in checks]
    result.insert(3, {"label": "移动速度", "value": round(metrics["speed"]), "unit": "像素/秒",
                      "limit": f'{lim["min_speed"]:g}～{lim["max_speed"]:g} 像素/秒',
                      "ok": lim["min_speed"] <= metrics["speed"] <= lim["max_speed"]})
    trail = tracker.last_trail if fired else []
    if tracker.samples and not fired:
        _, x0, y0 = tracker.samples[0]
        trail = [[x - x0, y - y0] for _, x, y in tracker.samples[-160:]]
    return {"type": "sample", "metrics": metrics, "need": need, "checks": result,
            "pressed": pressed, "armed": tracker.armed, "fired": fired, "count": count,
            "recent": list(recent), "cooldown": round(tracker.cooldown_remaining, 2),
            "still_time": tracker.still_time, "trail": trail}


@dataclass
class Session:
    cfg: dict
    ws: object
    stop: threading.Event = field(default_factory=threading.Event)
    recent: deque = field(default_factory=lambda: deque(maxlen=8))
    count: int = 0
    latest: dict | None = None
    last_trigger: dict | None = None
    lock: object = field(default_factory=threading.Lock)


class SwipeLab:
    def __init__(self, cfg, save, monitor=run_swipe_monitor, opener=webbrowser.open):
        self.cfg, self.save = cfg, save
        self.monitor, self.opener = monitor, opener
        self.session = None

    @property
    def active(self):
        return self.session is not None

    def parameters(self):
        return normalize_params(self.cfg.get("swipe_snap"))

    def info(self):
        return {"ok": True, "parameters": self.parameters(), "defaults": DEFAULT_PARAMS,
                "bounds": PARAM_BOUNDS, "default_distance": default_min_distance(screen_width())}

    async def parameter_api(self, request):
        local_only(request)
        if request.method == "GET":
            return web.json_response(self.info())
        try:
            body = await request.json()
            params = parse_import(body) if request.path.endswith("/import") else normalize_params(body, strict=True)
            self.save(params)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except OSError:
            return web.json_response({"ok": False, "error": "保存失败，请检查配置文件是否可写"}, status=500)
        return web.json_response(self.info())

    async def open_browser(self, request):
        local_only(request)
        url = f"http://127.0.0.1:{request.url.port}/swipe-lab"
        opened = await asyncio.to_thread(self.opener, url)
        return web.json_response({"ok": bool(opened), "url": url,
                                  "error": f"浏览器未能打开，请手动打开：{url}" if not opened else ""})

    def sample(self, session, tracker, x, y, pressed, now, fired):
        with session.lock:
            if fired:
                session.count += 1
                session.recent.appendleft({"sequence": session.count, "at": time.strftime("%H:%M:%S"),
                                           **tracker.last_gesture})
            current = snapshot(tracker, pressed, fired, session.count, session.recent)
            if fired:
                session.last_trigger = {key: current[key] for key in ("metrics", "checks", "trail")}
            current["last_trigger"] = session.last_trigger
            session.latest = current

    def run_session(self, session, loop):
        try:
            self.monitor(session.cfg, lambda: None, session.stop, lambda *args: self.sample(session, *args))
        except Exception:
            log.exception("滑动调试采样已停止")
        finally:
            if not session.stop.is_set():
                loop.call_soon_threadsafe(lambda: asyncio.create_task(session.ws.close(code=1011)))

    async def socket(self, request):
        local_only(request)
        if self.active:
            return web.json_response({"ok": False, "error": "已有调试页正在运行，请先结束该次调试"}, status=409)
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=16384)
        session = Session({"swipe_snap": {"enabled": True, **self.parameters()}}, ws)
        self.session = session
        sender = None
        try:
            await ws.prepare(request)
            await ws.send_json({"type": "ready", **self.info()})
            threading.Thread(target=self.run_session, args=(session, asyncio.get_running_loop()),
                             daemon=True, name="swipe-lab").start()

            async def broadcast():
                while not ws.closed:
                    await asyncio.sleep(0.05)
                    if session.latest is not None:
                        await ws.send_json(session.latest)

            sender = asyncio.create_task(broadcast())
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                    kind = data.get("type")
                    if kind in ("preview", "apply"):
                        params = normalize_params(data.get("parameters"), strict=True)
                        if kind == "apply":
                            self.save(params)
                        session.cfg["swipe_snap"] = {"enabled": True, **params}
                        await ws.send_json({"type": "applied" if kind == "apply" else "parameters",
                                            "parameters": params, "requestId": data.get("requestId")})
                    elif kind == "reset":
                        with session.lock:
                            session.count = 0
                            session.recent.clear()
                            session.latest = session.last_trigger = None
                    else:
                        raise ValueError("不支持的调试操作")
                except (ValueError, TypeError, AttributeError, OSError) as exc:
                    await ws.send_json({"type": "error", "error": str(exc) if not isinstance(exc, OSError) else "保存失败，请稍后重试"})
        finally:
            session.stop.set()
            if sender:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                    await sender
            self.session = None
        return ws

    async def shutdown(self, app):
        if self.session:
            self.session.stop.set()
            await self.session.ws.close(code=1001)

    def register(self, app, page):
        async def index(request):
            local_only(request)
            return web.FileResponse(page, headers={"Cache-Control": "no-cache"})
        app.router.add_get("/swipe-lab", index)
        app.router.add_get("/api/swipe/parameters", self.parameter_api)
        app.router.add_post("/api/swipe/parameters", self.parameter_api)
        app.router.add_post("/api/swipe/import", self.parameter_api)
        app.router.add_post("/api/swipe-lab/open", self.open_browser)
        app.router.add_get("/ws/swipe-lab", self.socket)
        app.on_shutdown.append(self.shutdown)
