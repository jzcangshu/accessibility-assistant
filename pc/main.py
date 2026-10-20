"""PC 端入口。

用法：
  python -m pc.main                # 局域网模式（默认），手机浏览器打开主窗口里显示的地址
  python -m pc.main --port 9000    # 指定端口
  python -m pc.main --calibrate    # 框选"区域截图"范围（校准过程会显示覆盖层，仅在设置时使用）
  python -m pc.main --relay        # 以中转服务器模式运行（部署在公网机器上）

主窗口（pc/ui.py）：本进程内嵌 WebView2 的 Chromium 窗口，加载与手机端同一份网页的
PC 布局（/?pc=1）——左边连接/状态/日志，右边设置。关闭（✕）只隐藏、程序继续在后台
运行；退出走页面按钮、Ctrl+Alt+Q 或手机端「关闭电脑端」。
快捷键（可在 config.json 修改）：
  F8 全屏截图推送 / F9 框选 / F10 区域截图 / Ctrl+Alt+H 窗口显隐 / Ctrl+Alt+Q 退出

设置统一：网页端的设置（API 渠道、提示词、提醒等）存进 config.json 的 settings 段，
手机页与 PC 控制台页面是同一套渲染代码，任何一边改，另一边实时同步（WS 广播）；
手机在旧版中转服务器下自动退化为本地缓存模式。
另有两种"免按键"的全屏截图触发：
  鼠标触碰屏幕角落（corner_snap.enabled，默认开）
  鼠标横向平移划一下（swipe_snap.enabled，默认开）
手机端主页面「遥控截图」按钮：手机上直接触发一次全屏截图，入口有两级——
  POST /api/snap（WS 断连时的兜底）；WS 消息 {"type":"snap"}（正常路径，回 snap_ack）
"""
import argparse
import asyncio
import base64
import collections
import copy
import io
import json
import logging
import logging.handlers
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from . import capture
from .config import BASE_DIR, RESOURCE_ROOT, load_config, save_config
from . import discovery
from . import firewall
from . import llm_proxy
from . import ui
from .relay_client import RelayClient
from .swipe import (SWIPE_DIST_RATIO, SWIPE_LIMITS, SwipeTracker, default_min_distance,
                    mouse_button_down, run_swipe_monitor, swipe_hit, swipe_window)
from .swipe_lab import SwipeLab
from .local_access import local_only
from .shared_llm import SharedLLM

VERSION = "1.4.2"
STATIC_FILES = ("app.js", "style.css", "manifest.json", "swipe-settings.js", "swipe-lab.js", "swipe.css", "overlay.css")
WEB_DIR = RESOURCE_ROOT / "web"
log = logging.getLogger("pc")
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")


def to_dataurl(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


# ---------- 统一设置（手机页与 PC 控制台共用，存 config.json 的 settings 段） ----------

# 网页端设置的默认值。与 web/app.js 的 DEF_SETTINGS 保持同一份默认（那边是断连时的
# 本地兜底，这边是权威存储）；改默认时两处同步改。
SETTINGS_DEFAULTS = {
    "channels": [],
    "prompt": (
        "你是专业的无障碍辅助助手。用户会发来一处或多处屏幕内容（文字或截图）。\n"
        "你只负责给出准确的识别结果与选择建议，不要在结果里讲解你的推理过程。\n\n"
        "只输出下面这一个 JSON，前后不要有任何其它文字：不要 Markdown 代码围栏，不要“好的”，\n"
        "不要复述内容，不要写“我先看看”“让我分析一下”这类思考过程或说明。\n"
        "- 只有一条内容：{\"answer\":\"选择建议\",\"analysis\":\"30字以内简短解析\"}\n"
        "- 有多条内容（按图中从上到下的顺序）：[{\"no\":\"序号\",\"answer\":\"选择建议\","
        "\"analysis\":\"30字以内简短解析\"}, ...]\n"
        "  no 字段每项都必须有：图中印有编号就用原编号；没有印编号就按出现顺序从1开始编号。"
        "不要遗漏或合并任何一条。\n\n"
        "answer 只填建议本身，不要写理由、不要抄选项内容：\n"
        "- 单选：一个选项字母，如 A\n"
        "- 多选：把所有正确选项的字母连在一起、按字母升序写，如 ACD。\n"
        "  多选必须给全，不要只写第一个，也不要用逗号、空格、“和”分隔\n"
        "- 判断：正确 或 错误\n"
        "- 其他类型：直接给最简洁、可直接使用的文本\n\n"
        "analysis 只写结论性的一句话，不要写完整推导过程。\n"
        "如果内容看不清或信息不足，answer 填 无法确定。"
    ),
    "autoAsk": True,
    "includeImage": True,
    "autoScroll": True,
    "tts": True,
    "ttsAnalysis": False,
    "bdPhonetic": False,
    "vib": True,
    "vibStrength": 1,     # 0 弱 / 1 中 / 2 强（只有配套 App 能调强度，浏览器没有强度 API）
    "vibPulseGap": 260,   # 同一选项内，每下之间隔多久（毫秒）
    "vibOptGap": 650,     # 多选时，选项与选项之间隔多久（毫秒）
    "vibQuesGap": 800,    # 多条内容之间隔多久（毫秒）
    "overlayHover": 0.7,  # 悬浮窗：按钮悬停多久触发；拖动时静止多久退出跟随（秒）
}


def normalize_channels(chans):
    """渠道数组兜底：缺字段补默认，非字典项剔除（JSON 手改 config.json 时防炸）。

    注意：地址/模型名还没填的空渠道要保留——「添加渠道」就是先插一张空卡再让用户填，
    这里把它丢掉会让刚点出来的卡片立刻消失。可用性由 settings_empty / 前端过滤判断。
    """
    out = []
    for c in chans or []:
        if not isinstance(c, dict):
            continue
        c = dict(c)
        c["api"] = c.get("api") or "chat"
        try:
            c["timeout"] = int(c.get("timeout") or 20)
        except (TypeError, ValueError):
            c["timeout"] = 20
        c["enabled"] = bool(c.get("enabled", True))
        for k in ("name", "base", "key", "model"):
            c[k] = str(c.get(k) or "")
        out.append(c)
    return out


def get_settings(cfg):
    """当前生效设置 = 默认值 + config.json 的 settings 段（深拷贝，调用方可放心改）。"""
    st = copy.deepcopy(SETTINGS_DEFAULTS)
    user = cfg.get("settings") or {}
    if isinstance(user, dict):
        for k, v in user.items():
            st[k] = copy.deepcopy(v)
    st["channels"] = normalize_channels(st.get("channels"))
    return st


def settings_empty(cfg):
    """服务端还没有可用设置吗（决定手机端要不要把本地设置播种上来）。"""
    user = cfg.get("settings")
    if not isinstance(user, dict) or not user:
        return True
    chans = [c for c in normalize_channels(user.get("channels"))
             if c["base"] and c["model"]]
    return not chans and "prompt" not in user


def update_settings(cfg, patch):
    """合并补丁进 settings 段：标量逐项覆盖、channels 整组替换（数组不做半合并）。

    保存后广播给所有端（手机 + PC 控制台页面）。返回完整设置。
    """
    if not isinstance(patch, dict) or not patch:
        return get_settings(cfg)
    with _cfg_lock:
        st = cfg.setdefault("settings", {})
        for k, v in patch.items():
            if k in SETTINGS_DEFAULTS:  # 白名单：room 这类手机本地项不进服务端
                st[k] = copy.deepcopy(v)
        save_config(cfg)
        full = get_settings(cfg)
    log.info("设置已更新（%s）", "、".join(sorted(patch.keys()))[:80])
    push_event({"type": "settings", "data": full,
                "empty": settings_empty(cfg)}, phones=True)
    refresh_status()
    return full


class PanelBus:
    """控制台页面（PC 布局）的推送通道：日志、状态、设置。

    任意线程可 publish（线程安全地跳进事件循环）；clients 是 /ws/panel 的连接。
    日志同时进环形缓冲，晚连上的页面也能拿到历史。
    """

    def __init__(self):
        self.loop = None
        self.q = None
        self.clients = set()
        self.logbuf = collections.deque(maxlen=400)

    def start(self, loop):
        self.loop = loop
        self.q = asyncio.Queue(maxsize=500)

    def publish(self, item):
        if item.get("type") == "log":
            self.logbuf.append(item)
        loop, q = self.loop, self.q
        if loop is None or q is None:
            return
        try:
            loop.call_soon_threadsafe(self._put, item)
        except RuntimeError:
            pass  # 事件循环已停（退出中）

    def _put(self, item):
        if self.q is None:
            return
        try:
            self.q.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self.q.get_nowait()
                self.q.put_nowait(item)
            except Exception:
                pass


async def panel_sender(bus):
    while True:
        item = await bus.q.get()
        data = json.dumps(item, ensure_ascii=False)
        dead = []
        for ws in list(bus.clients):
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            bus.clients.discard(ws)


class Hub:
    """手机连接集合 + 最近内容缓冲 + 推送队列。"""

    def __init__(self):
        self.loop = None
        self.q = None
        self.phones = set()
        self.overlays = set()
        self.recent = []
        self.relay = None

    async def broadcast(self, item, record=True):
        if record:
            self.recent.append(item)
            del self.recent[:-20]
        if "type" not in item:
            item = {"type": "question", **item}
        data = json.dumps(item, ensure_ascii=False)
        dead = []
        for ws in list(self.phones | self.overlays):
            try:
                await ws.send_str(data)
            except Exception:
                self.phones.discard(ws)
                self.overlays.discard(ws)
                dead.append(ws)
        # 发送失败只把 ws 从集合里摘掉不够：客户端那边连接还开着，心跳也正常，
        # 它会一直以为自己在线却再也收不到内容（悬浮窗就会一直重识别旧内容）。主动关掉，
        # 让页面 onclose 重连并 resync。
        for ws in dead:
            try:
                await ws.close(code=1011, message=b"push failed", drain=False)
            except Exception:
                pass
        if self.relay is not None:
            try:
                await self.relay.send(item)
            except Exception as e:
                log.debug("中转发送失败: %s", e)

    def publish(self, item):
        """线程安全投递。事件循环尚未就绪（启动早期 / 单测环境）时静默丢弃。"""
        loop, q = self.loop, self.q
        if loop is None or q is None:
            return

        def _put():
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                q.get_nowait()
                q.put_nowait(item)

        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass  # 事件循环已停（退出中）


async def sender_task(hub):
    while True:
        item = await hub.q.get()
        try:
            # 只有内容进"最近缓冲"（断线重连补发用）；状态/设置类广播不占历史
            await hub.broadcast(item, record=(item.get("type") or "question") == "question")
        except Exception:
            log.exception("推送失败")


# ---------- 捕获流水线 ----------

_last_fire = [0.0]
_calibrating = [False]
_region_state = {"region": None}  # 最新框选区域；confirm_save 实时更新，热键即取即用
_corner_stop = threading.Event()
_swipe_stop = threading.Event()


def process_from_jpeg(hub, cfg, jpeg, jpeg_full=None):
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        hist = BASE_DIR / cfg["history_dir"]
        hist.mkdir(exist_ok=True)
        (hist / f"{ts}_{uuid.uuid4().hex[:4]}.jpg").write_bytes(jpeg)

        item = {"id": uuid.uuid4().hex[:12], "ts": int(time.time() * 1000), "image": None, "text": None}
        if cfg["ocr"]["enabled"]:
            try:
                from . import ocr_engine

                item["text"] = ocr_engine.recognize(jpeg_full or jpeg) or ""
                if not item["text"]:
                    item["ocr_note"] = "电脑端OCR未识别到文字（已自动附图）"
            except ImportError:
                item["ocr_note"] = "电脑端未安装OCR组件，已改传图片"
                log.warning("OCR 组件未安装，本次直接传图片")
            except Exception as e:
                item["ocr_note"] = "电脑端OCR出错，已改传图片"
                log.error("OCR 失败，改为直接发图: %s", e)
            if not (cfg["ocr"]["send_text_only"] and item["text"]):
                item["image"] = to_dataurl(jpeg)
        else:
            item["image"] = to_dataurl(jpeg)

        hub.publish(item)
        log.info("已捕获并推送内容 %s", item["id"])
    except Exception:
        log.exception("处理截图失败")


def make_capture_handler(hub, cfg):
    def handler(region=None):
        """触发一次截图。返回 (ok, reason)：被忽略时给出原因，手机端「遥控截图」会原样提示。"""
        if _calibrating[0]:
            return False, "电脑端正在框选中"
        now = time.time()
        if now - _last_fire[0] < 0.4:  # 按键去抖
            return False, "刚刚已触发过一次，请稍候"
        _last_fire[0] = now

        def work():
            try:
                cap = cfg["capture"]
                jpeg, jpeg_full, _ = capture.grab(
                    region=region,
                    monitor=cap["monitor"],
                    max_width=int(cap["max_width"]),
                    quality=int(cap["jpeg_quality"]),
                )
            except Exception:
                log.exception("截屏失败")
                return
            process_from_jpeg(hub, cfg, jpeg, jpeg_full)

        _pool.submit(work)
        return True, ""

    return handler


def trigger_snap(app):
    """「遥控截图」的统一入口（手机 / 悬浮窗 / HTTP / 中转共用）：触发一次全屏截图。

    截图结果不在这里返回——它走正常推送链路（WS 或重连补发）到手机和悬浮窗。
    这里只回答「指令是否被受理」，失败时带上原因（框选中 / 去抖 / 未就绪）。
    """
    h = app.get("snap")
    if not h:
        return {"ok": False, "error": "电脑端未就绪"}
    ok, err = h()
    if ok:
        log.info("遥控触发全屏截图（手机或悬浮窗）")
    return {"ok": ok, "error": err or ""}


def make_sample_jpeg():
    """生成一张示例图，用于自测链路（POST /api/test）。"""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1000, 430), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 34)
    except Exception:
        font = ImageFont.load_default()
    lines = [
        "【自检示例】请选择要执行的操作：",
        "A. 继续    B. 取消",
        "C. 重试    D. 退出",
        "（此图由 /api/test 生成，仅用于验证链路）",
    ]
    y = 40
    for ln in lines:
        d.text((40, y), ln, fill="black", font=font)
        y += 80
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


# ---------- HTTP / WS 路由 ----------

async def ws_phone(request):
    hub = request.app["hub"]
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)
    hub.phones.add(ws)
    log.info("手机已连接（当前 %d 台）", len(hub.phones))
    refresh_status()
    try:
        await ws.send_json({"type": "settings", "data": get_settings(request.app["cfg"]),
                            "empty": settings_empty(request.app["cfg"])})
        recent = list(hub.recent)[-3:]
        if recent:
            await ws.send_str(json.dumps({"type": "sync", "items": recent}, ensure_ascii=False))
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                t = data.get("type")
                if t == "ping":
                    await ws.send_json({"type": "pong"})
                elif t == "ack":
                    log.info("手机已确认内容 %s", data.get("id"))
                elif t == "resync":
                    recent = list(hub.recent)[-5:]
                    if recent:
                        await ws.send_str(json.dumps({"type": "sync", "items": recent}, ensure_ascii=False))
                    # （重）连接后顺手推一次最新状态：中转模式下手机只有这条路径能拿到快照
                    refresh_status()
                elif t == "snap":
                    await ws.send_json({"type": "snap_ack", **trigger_snap(request.app)})
                elif t in ("get_settings", "settings_set", "action"):
                    # 手机端统一设置：直接局域网时这里处理；中转时走 relay_client 转发
                    await handle_client_message(request.app, data)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        hub.phones.discard(ws)
        log.info("手机断开（当前 %d 台）", len(hub.phones))
        refresh_status()
    return ws


async def ws_overlay(request):
    local_only(request)
    hub, cfg = request.app["hub"], request.app["cfg"]
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)
    hub.overlays.add(ws)

    async def sync():
        await ws.send_json({"type": "settings", "data": get_settings(cfg), "empty": settings_empty(cfg)})
        await ws.send_json({"type": "sync", "items": list(hub.recent)[-1:]})

    try:
        await sync()
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
            elif data.get("type") == "resync":
                await sync()
            elif data.get("type") == "snap":
                # 悬浮窗「识别」按钮（截屏+识别）：不连手机也要能独立使用。
                await ws.send_json({"type": "snap_ack", **trigger_snap(request.app)})
            # 除截屏外，悬浮页仍只有订阅能力，不接收设置写入指令。
    finally:
        hub.overlays.discard(ws)
    return ws


async def api_overlay(request):
    local_only(request)
    if request.method == "GET":
        return web.json_response({"ok": True, **ui.overlay_status()})
    try:
        body = await request.json()
        if not isinstance(body, dict) or type(body.get("visible")) is not bool:
            raise ValueError("请指定悬浮窗开关状态")
        state = await asyncio.to_thread(ui.set_overlay_visible, body["visible"])
        return web.json_response({"ok": True, **state})
    except (ValueError, RuntimeError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        log.exception("悬浮窗操作失败")
        return web.json_response({"ok": False, "error": "悬浮窗未能打开，请查看运行日志"}, status=500)


async def ws_panel(request):
    """PC 控制台页面（主窗口里的 WebView2，或任意浏览器打开 /?pc=1）的 WS。

    连上先补历史（日志 + 状态 + 设置 + 连接目标），之后实时推。
    页面发来的消息与手机共用同一个分发器。
    """
    bus = request.app["bus"]
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)
    bus.clients.add(ws)
    cfg = request.app["cfg"]
    try:
        for item in list(bus.logbuf):
            await ws.send_str(json.dumps(item, ensure_ascii=False))
        await ws.send_str(json.dumps({"type": "status", **build_status()},
                                     ensure_ascii=False))
        await ws.send_str(json.dumps(
            {"type": "settings", "data": get_settings(cfg), "empty": settings_empty(cfg)},
            ensure_ascii=False))
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                if data.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
                else:
                    await handle_client_message(request.app, data)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        bus.clients.discard(ws)
    return ws


async def handle_client_message(app, data):
    """手机页 / 控制台页发来的 WS 消息统一处理（设置同步、动作、窗口控制）。"""
    t = data.get("type")
    cfg = app["cfg"]
    try:
        if t == "get_settings":
            push_event({"type": "settings", "data": get_settings(cfg),
                        "empty": settings_empty(cfg)}, phones=True)
        elif t == "settings_set":
            update_settings(cfg, data.get("patch") or {})
        elif t == "action":
            name = str(data.get("name") or "")
            if name:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, handle_action, name)
        elif t == "win_ctrl":
            act = data.get("action")
            if act == "hide":
                ui.hide("窗口已隐藏（Ctrl+Alt+H 可唤回）")
            elif act == "show":
                ui.show()
            elif act == "toggle":
                ui.toggle()
        elif t == "pick":
            try:
                _conn["pick"] = max(0, int(data.get("index", 0)))
            except (TypeError, ValueError):
                return
            log.info("手机连接地址已切换为第 %d 个网卡", _conn["pick"] + 1)
            refresh_status()
        else:
            log.debug("未处理的客户端消息类型：%s", t)
    except Exception:
        log.exception("处理客户端消息失败：%s", t)


async def api_llm(request):
    """大模型 API 代理：手机页面 → 本机 → 渠道（Chat Completions / Responses / Anthropic）。"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "请求体不是合法 JSON"})
    return web.json_response(await request.app["shared_llm"].forward(request.app["client"], body))


async def api_status(request):
    """结构化状态快照：手机页「电脑端」区与 PC 控制台左栏共用同一份数据。"""
    return web.json_response({"ok": True, **build_status()})


async def api_settings(request):
    """统一设置的 HTTP 读写（局域网直连时可用；中转模式走 WS 消息）。"""
    cfg = request.app["cfg"]
    if request.method == "GET":
        return web.json_response({"ok": True, "data": get_settings(cfg),
                                  "empty": settings_empty(cfg)})
    try:
        body = await request.json()
    except Exception:
        body = {}
    patch = body.get("patch") if isinstance(body, dict) else None
    if patch is None and isinstance(body, dict):
        patch = {k: v for k, v in body.items() if k in SETTINGS_DEFAULTS}
    full = update_settings(cfg, patch or {})
    return web.json_response({"ok": True, "data": full})


def _conn_targets():
    """给手机用的候选地址列表（按“最可能连上”排序）+ 当前点选的下标。"""
    cfg = _ui["cfg"]
    port = cfg["server"]["port"]
    targets = ([("指定地址", _ui["host"])] if _ui["host"]
               else (list_interfaces() or [("本机", "127.0.0.1")]))
    tlist = [{"name": (name or "").split("[")[0].strip() or f"接口 {i + 1}",
              "url": f"http://{ip}:{port}/"} for i, (name, ip) in enumerate(targets)]
    if _conn["pick"] >= len(tlist):
        _conn["pick"] = 0
    return tlist, _conn["pick"]


async def api_conn(request):
    """连接目标（网卡候选 + 当前点选）；控制台页面点选切换二维码/地址用。"""
    if request.method == "POST":
        try:
            body = await request.json()
            _conn["pick"] = max(0, int((body or {}).get("pick", 0)))
            log.info("手机连接地址已切换为第 %d 个网卡", _conn["pick"] + 1)
            refresh_status()
        except (ValueError, TypeError):
            pass
    tlist, pick = _conn_targets()
    return web.json_response({"ok": True, "targets": tlist, "pick": pick})


_qr_cache = {"url": "", "png": b""}


async def api_qr(request):
    """当前点选地址的二维码 PNG（控制台左栏 <img> 直接引用）。"""
    tlist, pick = _conn_targets()
    url = tlist[pick]["url"] if tlist else ""
    if url != _qr_cache["url"]:
        buf = io.BytesIO()
        ui.make_qr_image(url).save(buf, "PNG")
        _qr_cache.update(url=url, png=buf.getvalue())
    return web.Response(body=_qr_cache["png"], content_type="image/png",
                        headers={"Cache-Control": "no-cache"})


async def api_calibrate(request):
    if open_calibrate_async():
        return web.json_response({"ok": True, "msg": "已在电脑屏幕上弹出框选窗口，拉框后点「确定」"})
    return web.json_response({"ok": False, "error": "电脑端已在框选中，请先完成或按 Esc 取消"})


async def api_stealth(request):
    """防截屏开关（实时生效，写入 config.json）。窗口里的行内按钮与手机端共用同一实现。

    给主窗口加系统级防截屏（WDA）：窗口照常可见，但截图/录屏/屏幕共享里看不到它；
    属主进程自己的截图不受影响（手机遥控截图照常）。请求体两种写法都认：
    {"anti_capture": bool}（本程序内部）/ {"enabled": bool}（手机页发送的是这种）。
    """
    cfg = request.app["cfg"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    if "anti_capture" in body or "enabled" in body:
        switch_anti_capture(cfg, bool(body.get("anti_capture", body.get("enabled"))))
    return web.json_response({
        "ok": True,
        "anti_capture": bool((cfg.get("stealth") or {}).get("anti_capture", True)),
        "state": _stealth_state(),
    })


async def api_quit(request):
    """手机端「关闭电脑端」：优雅退出（等价 ctrl+alt+q）。"""
    evt = request.app.get("stop_evt")
    if evt is None:
        return web.json_response({"ok": False, "error": "停止信号未就绪"}, status=409)
    evt.set()
    return web.json_response({"ok": True, "msg": "电脑端正在退出"})


async def api_snap(request):
    """手机端「遥控截图」（HTTP 兜底）：WS 临时断连时也能触发；内容随后经推送/重连补发到达。"""
    return web.json_response(trigger_snap(request.app))


async def api_corner(request):
    """开关「鼠标触碰边角全屏截图」（实时生效，写入 config.json）。"""
    cfg = request.app["cfg"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    if "enabled" in body:
        switch_flag(cfg, "corner_snap", "enabled", bool(body["enabled"]), "边角触碰截图")
    return web.json_response({"ok": True, "enabled": bool(cfg.get("corner_snap", {}).get("enabled", True))})


async def api_swipe(request):
    """开关「鼠标横向平移全屏截图」（实时生效，写入 config.json）。"""
    cfg = request.app["cfg"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    if "enabled" in body:
        switch_flag(cfg, "swipe_snap", "enabled", bool(body["enabled"]), "横向平移截图")
    return web.json_response({"ok": True, "enabled": bool(cfg.get("swipe_snap", {}).get("enabled", True))})


async def api_ocr(request):
    """开关「文字识别 OCR」（实时生效，写入 config.json）。"""
    cfg = request.app["cfg"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    if "enabled" in body:
        on = bool(body["enabled"])
        switch_flag(cfg, "ocr", "enabled", on, "文字识别")
        if on:
            def _check_ocr():
                try:
                    import rapidocr_onnxruntime  # noqa: F401
                except ImportError:
                    log.warning("未检测到 OCR 组件：截图仍会按图片发送（安装方法见 README 的 OCR 一节）")
            _pool.submit(_check_ocr)
    return web.json_response({"ok": True, "enabled": bool(cfg.get("ocr", {}).get("enabled", False))})


async def api_test(request):
    cfg = request.app["cfg"]
    hub = request.app["hub"]
    if not cfg.get("allow_test_api", True):
        return web.json_response({"ok": False, "error": "test api disabled"}, status=403)
    jpeg = make_sample_jpeg()
    _pool.submit(process_from_jpeg, hub, cfg, jpeg)
    return web.json_response({"ok": True})


def _static_handler(path: Path):
    async def handler(request):
        if not path.exists():
            return web.Response(status=404, text="missing")
        # no-cache：每次协商缓存，保证页面文件更新后手机端立即生效
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    return handler


def build_app(cfg, hub):
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app["cfg"] = cfg
    app["hub"] = hub
    app["shared_llm"] = SharedLLM()
    app["llm_tasks"] = set()

    async def close_shared(app):
        for task in list(app["llm_tasks"]):
            task.cancel()
        await asyncio.gather(*app["llm_tasks"], return_exceptions=True)
        await app["shared_llm"].close()

    app.on_cleanup.append(close_shared)
    app["swipe_lab"] = SwipeLab(cfg, lambda params: update_swipe_parameters(cfg, params))
    app["swipe_lab"].register(app, WEB_DIR / "swipe-lab.html")

    async def index(request):
        return web.FileResponse(WEB_DIR / "index.html")

    async def favicon(request):
        return web.Response(status=204)

    app.router.add_get("/", index)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/ws", ws_phone)
    # 手机页在"有房间码"时会连 /ws/phone（那是中转服务器的路径）。局域网这里也认一下，
    # 免得手机里残留的房间码把连接打到不存在的地址上，一直连不上还看不出原因。
    app.router.add_get("/ws/phone", ws_phone)
    app.router.add_get("/ws/panel", ws_panel)
    app.router.add_get("/ws/overlay", ws_overlay)
    app.router.add_get("/api/overlay", api_overlay)
    app.router.add_post("/api/overlay", api_overlay)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/settings", api_settings)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_get("/api/conn", api_conn)
    app.router.add_post("/api/conn", api_conn)
    app.router.add_get("/api/qr", api_qr)
    app.router.add_post("/api/calibrate", api_calibrate)
    app.router.add_post("/api/corner", api_corner)
    app.router.add_post("/api/swipe", api_swipe)
    app.router.add_post("/api/ocr", api_ocr)
    app.router.add_post("/api/stealth", api_stealth)
    app.router.add_post("/api/quit", api_quit)
    app.router.add_post("/api/snap", api_snap)
    app.router.add_post("/api/test", api_test)
    app.router.add_post("/api/llm", api_llm)
    for f in STATIC_FILES:
        app.router.add_get("/static/" + f, _static_handler(WEB_DIR / f))
    return app


# ---------- 热键 / 校准 / 展示 ----------

def register_hotkeys(cfg, hub):
    handler = make_capture_handler(hub, cfg)
    try:
        import keyboard
    except ImportError:
        log.warning("未安装 keyboard 库，快捷键不可用（pip install -r requirements.txt）")
        _checks["hotkeys"] = ("不可用：缺少 keyboard 库", False)
        refresh_status()
        return
    hk = cfg["hotkeys"]
    win_hot = hk.get("window") or hk.get("console") or "ctrl+alt+h"
    keyboard.add_hotkey(hk.get("capture", "f8"), handler, suppress=False)

    def on_calibrate():
        out("打开框选窗口：拖框后点「确定」，F10 即按最新区域截图（Esc 取消）")
        open_calibrate_async()

    def on_region_capture():
        r = _region_state.get("region")
        if not r:
            out("尚未框选区域：按 F9 框选一次（或手机端设置里远程框选），F10 才生效")
            return
        handler(region=r)

    def on_toggle_window():
        """boss 键：藏/显示主窗口（程序始终继续运行；退出用 Ctrl+Alt+Q）。"""
        from . import win_stealth

        if ui.is_visible():
            ui.hide()
            log.info("窗口已隐藏（%s 可唤回）", win_hot)
        else:
            ui.show()
            if win_stealth.anti_capture_active():
                log.info("窗口已显示（仍处于防截屏状态，截图/录屏里看不到）")
            else:
                log.info("窗口已显示（此刻可被截屏软件捕获）")

    keyboard.add_hotkey(hk.get("calibrate", "f9"), on_calibrate, suppress=False)
    if hk.get("capture_region"):
        keyboard.add_hotkey(hk.get("capture_region", "f10"), on_region_capture, suppress=False)
    keyboard.add_hotkey(win_hot, on_toggle_window, suppress=False)
    log.debug("快捷键已注册：全屏=%s 框选=%s 区域截图=%s 窗口=%s 退出=%s",
              hk.get("capture"), hk.get("calibrate"), hk.get("capture_region"),
              win_hot, hk.get("quit"))
    _checks["hotkeys"] = ("已注册（5 个键）", True)
    refresh_status()


def start_corner_snap(cfg, handler, paused=None):
    """鼠标触碰截图屏幕的任一角落时自动触发一次全屏截图（悬停即触发，无需点击）。

    检测屏幕范围与 capture.grab 的 monitor 配置保持一致；触发一次后，
    需鼠标离开角落并满足冷却时间才可再次触发。开关/半径/冷却实时读 config。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        import mss  # noqa: F401  —— 这里只是探一下依赖在不在
    except Exception:
        log.warning("边角触碰截图：组件缺失，功能不可用")
        return
    user32 = ctypes.windll.user32

    def loop():
        try:
            with capture.open_screen() as sct:
                # 必须和 capture.grab 用同一套解析：取错了屏，"碰哪块屏的角落"和
                # "截哪块屏"就对不上，推到该截的那块屏的角落反而不触发。
                mon = capture.pick_monitor(sct, cfg["capture"].get("monitor", "primary"))
        except Exception:
            log.exception("边角触碰截图：获取屏幕范围失败")
            return
        left, top = int(mon["left"]), int(mon["top"])
        right, bottom = left + int(mon["width"]), top + int(mon["height"])
        pt = wintypes.POINT()
        armed = True
        if user32.GetCursorPos(ctypes.byref(pt)):
            # 启动时鼠标已停在角落则不立即触发
            near_x = pt.x <= left + 8 or pt.x >= right - 9
            near_y = pt.y <= top + 8 or pt.y >= bottom - 9
            armed = not (near_x and near_y)
        last = 0.0
        log.debug("边角触碰截图监测已启动（范围 %dx%d，配置 corner_snap.enabled 可关）",
                 right - left, bottom - top)
        while not _corner_stop.is_set():
            _corner_stop.wait(0.03)
            sc = cfg.get("corner_snap") or {}
            if paused and paused():
                armed = False
                continue
            if not sc.get("enabled", True):
                armed = True
                continue
            if not user32.GetCursorPos(ctypes.byref(pt)):
                continue
            r = max(2, int(sc.get("radius", 8) or 8))
            in_corner = (pt.x <= left + r or pt.x >= right - 1 - r) and (
                pt.y <= top + r or pt.y >= bottom - 1 - r)
            if not in_corner:
                armed = True
                continue
            if armed:
                armed = False
                now = time.time()
                if now - last >= float(sc.get("cooldown", 1.5) or 1.5):
                    last = now
                    if not paused or not paused():
                        handler()  # 全屏截图（不带区域）

    threading.Thread(target=loop, name="corner-snap", daemon=True).start()


def start_swipe_snap(cfg, handler, paused=None):
    """鼠标以相对稳定的速度横向平移时触发一次全屏截图（无需按键）。

    首先排除掉"按住拖"（选文字、拖窗口、拖滚动条）——只要鼠标键处于按下状态，
    轨迹就直接作废，所以这类操作永远不会触发。剩下的交给 SwipeTracker 判定：
    横向位移够长、大体横着走、速度落在「刻意划一下」的区间里、路径别来回蹭。
    阈值宁可紧一点——漏触发只是再划一次，误触发却会平白多截一张图。
    触发后要先停下来，才能重新触发。
    """
    threading.Thread(target=run_swipe_monitor, args=(cfg, handler, _swipe_stop, None, paused),
                     name="swipe-snap", daemon=True).start()


# 虚拟网卡 / 隧道的关键字：这些网卡上的地址手机大概率连不上，排到真实网卡后面
VIRTUAL_HINTS = ("zerotier", "tailscale", "vmware", "virtualbox", "hyper-v", "vethernet",
                 "wsl", "tap", "tun", "vpn", "loopback", "bluetooth", "docker")


def ip_rank(name, ip):
    """给候选地址打分，越小越优先。

    手机得和电脑在同一张网里，所以优先「真实网卡 + 常见家用/办公网段」。VPN 隧道
    （Clash 之类的 TUN 模式会占用 198.18.0.0/15 基准测试段）、ZeroTier、虚拟机网卡
    一律往后排——这些地址手机根本连不上，排前面会把 mDNS 广播里的 ip 写成它们，
    手机「自动搜索」找到电脑也连不上。
    """
    low = (name or "").lower()
    score = 6 if any(h in low for h in VIRTUAL_HINTS) else 0
    if ip.startswith(("198.18.", "198.19.")):
        score += 8
    elif ip.startswith("100."):
        score += 4  # 100.64/10，CGNAT，Tailscale 常用
    if ip.startswith("192.168."):
        pass
    elif ip.startswith(("10.", "172.")):
        score += 1
    else:
        score += 3
    return score


def is_virtual_iface(name, ip):
    """是不是 VPN / 虚拟机 / 隧道网卡——这种地址手机基本连不上。"""
    low = (name or "").lower()
    if any(h in low for h in VIRTUAL_HINTS):
        return True
    return ip.startswith(("198.18.", "198.19."))  # 基准测试保留段，Clash 之类 TUN 常用


def advertise_addresses():
    """给 mDNS 广播用的地址：只播真实网卡。

    虚拟网卡（Clash 的 TUN、ZeroTier、虚拟机网卡）的地址手机连不上，写进 A 记录只会多给
    手机一个错选项，也把内网拓扑白送给同一广播域；但如果机器上只剩虚拟网卡（比如全靠
    ZeroTier 组网），那就退回播全部——有总比没有强。
    """
    cands = list_interfaces()
    real = [ip for name, ip in cands if not is_virtual_iface(name, ip)]
    return real or [ip for _, ip in cands]


def start_firewall_check(cfg, on_change=False):
    """在后台查一次防火墙，有问题就明确说出来。

    放在线程里：要起一个 PowerShell（约 0.5 秒），不该拖慢启动。
    没问题时一声不吭——免得每次启动都刷一段用不上的提示。
    """
    port = cfg["server"]["port"]
    if cfg["transfer"]["mode"] != "lan":
        _checks["fw"] = ("非局域网模式，不适用", None)
        refresh_status()
        return

    def work():
        try:
            _, msg = firewall.check(port)
        except Exception as e:
            log.debug("防火墙自检异常：%s: %r", type(e).__name__, e)
            _checks["fw"] = ("自检异常（详见 logs/pc.log）", None)
            refresh_status()
            return
        if msg:
            # 走 log 而不是 out()：这样页面日志区和 logs/pc.log 里都有，事后能翻
            log.warning(("换了网络，重新自检防火墙：\n" if on_change else "") + msg)
            _checks["fw"] = ("需要处理（放行命令见日志）", False)
        else:
            _checks["fw"] = ("已放行（当前网络）", True)
        refresh_status()

    threading.Thread(target=work, name="fw-check", daemon=True).start()


def start_advertiser(cfg):
    """在后台线程里注册 mDNS 广播，返回 advertiser（关闭时用）。

    必须丢到别的线程里做：zeroconf 的注册是阻塞调用（还要等一轮重名检测），而这里是在
    事件循环线程上被调用的——新版 zeroconf 一旦发现阻塞调用发生在事件循环线程上，会直接
    抛 EventLoopBlocked，广播就静默失败了（现象：手机端只能靠扫网段找到电脑，慢好几秒）。
    另外要定期复查网卡：换了网络（Wi-Fi ↔ 热点 ↔ 插网线）IP 会变，广播得跟着变，
    否则手机搜到的还是旧地址，只能重启电脑端。
    """
    advertiser = discovery.Advertiser()
    if cfg["transfer"]["mode"] != "lan":
        return advertiser
    props = {"version": VERSION, "mode": cfg["transfer"]["mode"]}
    threading.Thread(target=advertiser.start,
                     args=(cfg["server"]["port"], advertise_addresses(), props),
                     kwargs={"provider": advertise_addresses, "interval": 15.0,
                             # 换网络时顺带重查防火墙：规则是按「网络类型」分开管的，
                             # 换个 WiFi 就可能失配（手机打不开、但别的互传 App 也一起不行）
                             # 换网络 IP 会变：顺带把窗口里的地址/二维码刷新一下
                             "on_change": lambda: (start_firewall_check(cfg, on_change=True),
                                                   refresh_status())},
                     name="mdns-advertise", daemon=True).start()
    return advertiser


def list_interfaces():
    """返回 [(网卡名, ip)]，最可能是"手机能连上的那张网"的排最前。"""
    results = []
    seen = set()
    try:
        import psutil

        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET and a.address:
                    ip = a.address
                    if ip.startswith(("127.", "169.254.")) or ip in seen:
                        continue
                    seen.add(ip)
                    results.append((name, ip))
    except ImportError:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith(("127.", "169.254.")) and ip not in seen:
                    seen.add(ip)
                    results.append(("网络接口", ip))
        except Exception:
            pass
    primary_ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("8.8.8.8", 80))
        primary_ip = s.getsockname()[0]
    except Exception:
        pass
    if primary_ip:
        results.sort(key=lambda t: (ip_rank(t[0], t[1]), t[1] != primary_ip))
    else:
        results.sort(key=lambda t: ip_rank(t[0], t[1]))
    return results


# ---------- 界面 / 日志 / 状态快照 ----------

_ui = {"cfg": None, "host": None, "hub": None, "bus": None, "stop_evt": None}
_checks = {}  # 启动自检：键 -> (文本, True/False/None)
_conn = {"pick": 0}  # 当前点选的连接地址下标（窗口里点选切换二维码/地址）


def out(text):
    """所有面向用户输出的统一出口：进控制台页面日志（在终端里跑时同时回显）。"""
    try:
        if sys.stdout is not None and sys.stdout.isatty():
            print(text)
    except Exception:
        pass
    push_event({"type": "log", "ts": time.strftime("%H:%M:%S"), "mark": "·", "text": text})


def push_event(item, phones=False):
    """把一条 WS 消息推给控制台页面；phones=True 时手机端（含中转）也收一份。

    任意线程可调：面板走线程安全的队列投递，手机走 hub 的队列。两处都自带
    「循环未就绪 / 已关闭」防护，退出收尾阶段调用不会炸。
    """
    bus = _ui.get("bus")
    if bus is not None:
        bus.publish(item)
    if phones:
        hub = _ui.get("hub")
        if hub is not None:
            hub.publish(item)


class _UiLogHandler(logging.Handler):
    """日志进控制台页面：带时间与级别符；HTTP 访问日志不进（刷屏且对用户无意义）。"""

    def emit(self, record):
        try:
            if record.name.startswith("aiohttp.access"):
                return
            ts = time.strftime("%H:%M:%S", time.localtime(record.created))
            mark = {"WARNING": "!", "ERROR": "x"}.get(record.levelname, "·")
            push_event({"type": "log", "ts": ts, "mark": mark,
                        "text": record.getMessage()})
        except Exception:
            pass


def _stealth_state():
    from . import win_stealth

    try:
        return win_stealth.affinity_name()
    except Exception:
        return "unknown"


# ---------- 开关：控制台页面 / 手机端走同一实现 ----------

_cfg_lock = threading.Lock()


def update_swipe_parameters(cfg, params):
    """保存成功后再替换运行中配置，失败时维持原手感与开关。"""
    with _cfg_lock:
        updated = copy.deepcopy(cfg)
        updated["swipe_snap"] = {**updated.get("swipe_snap", {}), **params}
        save_config(updated)
        cfg["swipe_snap"] = updated["swipe_snap"]
    log.info("滑动触发参数已更新")
    refresh_status()


def save_overlay_opacity(cfg, opacity):
    """悬浮窗透明度：只持久化透明度，写入与其它配置修改共用同一把锁。"""
    with _cfg_lock:
        cfg.setdefault("overlay", {})["opacity"] = int(opacity)
        save_config(cfg)


def switch_anti_capture(cfg, on):
    """防截屏开关：写配置 → 立即对窗口生效 → 刷状态。锁只罩住配置与 Win32 这一段。"""
    from . import win_stealth

    with _cfg_lock:
        cfg.setdefault("stealth", {})["anti_capture"] = bool(on)
        save_config(cfg)
        win_stealth.apply(cfg)
    log.info("%s", win_stealth.cap_phrase())
    refresh_status()


def switch_flag(cfg, section, key, on, label):
    """通用开关（边角 / 划动 / 文字识别）：写配置即可——功能侧每轮都实时读它，下一轮生效。"""
    with _cfg_lock:
        cfg.setdefault(section, {})[key] = bool(on)
        save_config(cfg)
    log.info("%s已%s", label, "开启" if on else "关闭")
    refresh_status()


def handle_action(name):
    """页面动作（控制台左栏 / 手机设置页的开关按钮）统一走这里。

    可能从 WS 事件循环线程的执行器里被调用，只做轻量操作；重活（OCR 探测）丢线程池。
    """
    cfg = _ui.get("cfg")
    if cfg is None:
        return
    from . import win_stealth

    try:
        if name == "toggle_anti_capture":
            switch_anti_capture(cfg, not win_stealth.anti_capture_active())
        elif name == "toggle_corner":
            on = not bool((cfg.get("corner_snap") or {}).get("enabled", True))
            switch_flag(cfg, "corner_snap", "enabled", on, "边角触碰截图")
        elif name == "toggle_swipe":
            on = not bool((cfg.get("swipe_snap") or {}).get("enabled", True))
            switch_flag(cfg, "swipe_snap", "enabled", on, "横向平移截图")
        elif name == "toggle_ocr":
            on = not bool((cfg.get("ocr") or {}).get("enabled", False))
            switch_flag(cfg, "ocr", "enabled", on, "文字识别")
            if on:
                def _check_ocr():
                    try:
                        import rapidocr_onnxruntime  # noqa: F401
                    except ImportError:
                        log.warning("未检测到 OCR 组件：截图仍会按图片发送（安装方法见 README 的 OCR 一节）")
                _pool.submit(_check_ocr)
        else:
            log.debug("未知的界面动作：%s", name)
    except Exception:
        log.exception("界面动作执行失败：%s", name)


def _checks_list():
    """启动自检：[{label, ok}]（ok: True/False/None=未知）。"""
    out = []
    for key, label in (("stealth", "窗口隐身"), ("hotkeys", "快捷键"), ("fw", "防火墙")):
        text, ok = _checks.get(key, ("", None))
        out.append({"label": label, "ok": ok, "text": text})
    return out


def _monitors_brief():
    """显示器摘要：`2 块屏（主屏 2560x1440）`——太长的逐屏清单不值得占版面。"""
    try:
        with capture.open_screen() as sct:
            mons = list(sct.monitors[1:])
        if not mons:
            return "未检测到显示器"
        primary = next((m for m in mons if m.get("is_primary")), mons[0])
        return f"共 {len(mons)} 块屏 · 主屏 {primary['width']}x{primary['height']}"
    except Exception:
        return "读取失败"


def build_status():
    """结构化状态快照：手机页与 PC 控制台左栏渲染同一份数据。"""
    cfg = _ui["cfg"]
    hub = _ui["hub"]
    phones = len(hub.phones) if hub is not None else 0
    hk = cfg["hotkeys"]
    region = _region_state.get("region")
    ocr_on = bool((cfg.get("ocr") or {}).get("enabled", False))
    if ocr_on:
        try:
            import rapidocr_onnxruntime  # noqa: F401

            ocr_v = ("先识别成文字再发" if cfg["ocr"]["send_text_only"]
                     else "文字和截图一起发")
        except ImportError:
            ocr_v = "已开启，但缺少组件（暂时按图片发送）"
    else:
        ocr_v = "截图直接发到手机"
    cap_mon = (cfg.get("capture") or {}).get("monitor", "primary")
    cap_v = {"primary": "主屏", "all": "全部拼合"}.get(str(cap_mon), f"第 {cap_mon} 块屏")
    from . import win_stealth

    cap_text, _btn = win_stealth.cap_row()
    tlist, pick = _conn_targets()
    if phones == 0:
        head = "运行中 · 手机未连接，等待扫码"
        conn_hint = "手机和电脑连同一 Wi-Fi / 热点后，扫二维码或输入地址即可。"
    else:
        head = f"运行中 · 手机 {phones} 台已连接"
        conn_hint = f"已连接 {phones} 台手机；新设备仍可扫此码。"
    return {
        "ver": f"v{VERSION}",
        "version": VERSION,
        "mode": cfg["transfer"]["mode"],
        "phones": phones,
        "relay_connected": bool(hub.relay and hub.relay.connected),
        "head": (head + " · 中转已连" if cfg["transfer"]["mode"] == "relay" else head),
        "conn_hint": conn_hint,
        "hotkeys": {
            "capture": hk.get("capture", "f8"),
            "calibrate": hk.get("calibrate", "f9"),
            "region": hk.get("capture_region", "f10"),
            "window": hk.get("window") or "ctrl+alt+h",
            "quit": hk.get("quit", "ctrl+alt+q"),
        },
        "ocr_enabled": ocr_on,
        "ocr_detail": ocr_v,
        "region_set": bool(region),
        "region_detail": ("已设置（按 %s 截取该区域）" % hk.get("capture_region", "f10")
                          if region else "未设置（先按 %s 框选一次）" % hk.get("calibrate", "f9")),
        "region_rect": region,
        "corner_snap": bool((cfg.get("corner_snap") or {}).get("enabled", True)),
        "swipe_snap": bool((cfg.get("swipe_snap") or {}).get("enabled", True)),
        "anti_capture": win_stealth.anti_capture_active(),
        "anti_capture_state": _stealth_state(),
        "anti_capture_detail": cap_text,
        "monitors": _monitors_brief(),
        "capture_monitor": cap_v,
        "checks": _checks_list(),
        "taskbar": win_stealth.taskbar_text(),
        "window_visible": ui.is_visible(),
        "overlay": ui.overlay_status(),
        "shared_llm": True,
        "targets": tlist,
        "pick": pick,
    }


def refresh_status():
    """把最新状态推给所有端（手机连/断、自检更新、开关切换都会调）。"""
    if _ui["cfg"] is None:
        return
    try:
        push_event({"type": "status", **build_status()}, phones=True)
    except Exception:
        log.debug("状态快照构建失败", exc_info=True)


def request_stop():
    """跨线程请求退出（窗口按钮 / 热键 / 手机端共用），等价 ctrl+alt+q。

    事件循环可能已经关闭（GUI 先于服务器结束时的收尾回调）：这时投递会抛
    RuntimeError，属于正常收尾路径，静默即可。
    """
    hub = _ui.get("hub")
    evt = _ui.get("stop_evt")
    if hub is None or hub.loop is None or evt is None:
        return
    try:
        hub.loop.call_soon_threadsafe(evt.set)
    except RuntimeError:
        pass


def _probe_port(port):
    """端口预检：running=本程序已在跑 / busy=被别的程序占了 / free=空闲。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.8) as s:
            s.settimeout(1.0)
            s.sendall(b"GET /api/status HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            data = s.recv(4096).decode("utf-8", "replace")
            return "running" if '"version"' in data else "busy"
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return "free"
    except OSError:
        return "busy"
    finally:
        s.close()












CALIBRATE_DIM = 0.85  # 框选底图的亮度：1.0 = 和屏幕一样亮，越小越暗（0.85 只轻微压暗，内容照样看得清）


def screen_png_b64(mon, size=None):
    """把当前屏幕抓成一张压暗的 PNG（base64），当作框选时的底图。

    必须在覆盖层窗口出现之前调用，否则底图里会拍进覆盖层自己。
    size 传画布尺寸：进程 DPI 感知与系统缩放不一致时，抓到的物理像素和 Tk 的
    逻辑像素会对不上，按画布尺寸缩一下至少保证底图铺满、不错位。
    """
    from PIL import Image, ImageEnhance

    with capture.open_screen() as sct:
        shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    if size and (img.width, img.height) != tuple(size):
        img = img.resize(tuple(size), Image.LANCZOS)
    img = ImageEnhance.Brightness(img).enhance(CALIBRATE_DIM)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue())


def build_calibrate_ui(root, mon, bg_png=None):
    """在 root 上构建拉框界面；拖框 → 确定/重画 → 确定后立即销毁窗口。返回画布供测试注入事件。

    底图用"抓下来的一张压暗屏幕"，而不是半透明覆盖层：整窗 alpha 会把窗口里的一切
    （按钮、说明文字、选框）一起调淡，屏幕越亮越看不清，两个按钮几乎消失。换成底图 +
    实心工具条之后，压暗是烘焙进图片的，界面元素全是不透明的，怎么都看得清。
    """
    import tkinter as tk

    # 覆盖层必须盖住"将要截图的那块屏"。Windows 上 Tk 的 -fullscreen 只认主屏：
    # 实测先 geometry 再 fullscreen、fullscreen 后再 geometry、或只 fullscreen，
    # 窗口一律留在主屏（0,0）；只有"无边框窗口 + 显式摆到目标屏整块区域"才真的落对屏。
    # 落错屏的后果是底图、选框、保存的区域整体偏一块屏的宽度。
    root.overrideredirect(True)
    root.geometry("%dx%d+%d+%d" % (int(mon["width"]), int(mon["height"]),
                                   int(mon["left"]), int(mon["top"])))
    root.attributes("-topmost", True)
    root.configure(bg="black")
    cv = tk.Canvas(root, cursor="cross", bg="#000000", highlightthickness=0)
    cv.pack(fill="both", expand=True)
    st = {"sx": 0, "sy": 0, "x0": 0, "y0": 0, "x1": 0, "y1": 0,
          "rect": None, "rect2": None, "bar": None, "timer": None, "has": False, "bg": None}

    if bg_png:
        try:
            st["bg"] = tk.PhotoImage(data=bg_png)  # 必须留引用，否则被回收后底图变白
            cv.create_image(0, 0, anchor="nw", image=st["bg"])
        except Exception as e:
            log.warning("框选底图加载失败（%s），改用纯黑底", e)

    def arm_timeout():
        if st["timer"]:
            root.after_cancel(st["timer"])
        st["timer"] = root.after(30000, on_timeout)

    def on_timeout():
        out("框选超时未操作，已自动取消")
        root.destroy()

    def redo():
        if st["rect"]:
            cv.delete(st["rect"])
            st["rect"] = None
        if st["rect2"]:
            cv.delete(st["rect2"])
            st["rect2"] = None
        st["has"] = False
        arm_timeout()

    def confirm_save():
        if not st["has"]:
            out("还没拉框：按住左键拖一个矩形，再点「确定」")
            return
        region = [mon["left"] + min(st["x0"], st["x1"]), mon["top"] + min(st["y0"], st["y1"]),
                  abs(st["x1"] - st["x0"]), abs(st["y1"] - st["y0"])]
        if region[2] < 10 or region[3] < 10:
            out("区域太小，未保存")
        else:
            cfg = load_config()
            cfg["region"] = region
            save_config(cfg)
            _region_state["region"] = region  # 热键立即使用最新区域
            out(f"截图区域已保存 {region}（覆盖层已消失，F10 立即按此区域截图）")
        root.destroy()

    # 工具条：不透明的实心面板，压在画布上方，位置固定不跟着选框跑（永远看得见）
    bar = tk.Frame(root, bg="#141414", highlightthickness=1, highlightbackground="#555555")
    tk.Label(bar, text="拖动拉框选择识别区域 → 点「确定」立即生效（F10 截取该区域）｜Esc 取消",
             bg="#141414", fg="#f2f2f2", font=("Microsoft YaHei", 14)).pack(
        side="left", padx=(16, 18), pady=13)
    tk.Button(bar, text="确定", command=confirm_save, font=("Microsoft YaHei", 13, "bold"),
              bg="#f2f2f2", fg="#000000", activebackground="#ffffff", activeforeground="#000000",
              relief="flat", padx=20, pady=5, cursor="hand2").pack(side="left", pady=13)
    tk.Button(bar, text="重画", command=redo, font=("Microsoft YaHei", 13),
              bg="#2e2e2e", fg="#f2f2f2", activebackground="#3f3f3f", activeforeground="#ffffff",
              relief="flat", padx=20, pady=5, cursor="hand2").pack(side="left", padx=(10, 16), pady=13)
    bar.place(relx=0.5, y=14, anchor="n")
    st["bar"] = bar

    def press(e):
        if st["rect"]:
            cv.delete(st["rect"])
        if st["rect2"]:
            cv.delete(st["rect2"])
        st["has"] = False
        st["sx"], st["sy"] = e.x, e.y
        # 一黑一白两层描边：屏幕底色不管是亮是暗，选框都看得见
        st["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#000000", width=5)
        st["rect2"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#ffffff", width=2)
        arm_timeout()

    def move(e):
        if st["rect"]:
            cv.coords(st["rect"], st["sx"], st["sy"], e.x, e.y)
            cv.coords(st["rect2"], st["sx"], st["sy"], e.x, e.y)
        arm_timeout()

    def release(e):
        st["x0"], st["y0"], st["x1"], st["y1"] = st["sx"], st["sy"], e.x, e.y
        st["has"] = abs(e.x - st["sx"]) >= 10 and abs(e.y - st["sy"]) >= 10
        arm_timeout()

    def esc(e):
        out("已取消框选")
        root.destroy()

    cv.bind("<ButtonPress-1>", press)
    cv.bind("<B1-Motion>", move)
    cv.bind("<ButtonRelease-1>", release)
    root.bind("<Escape>", esc)
    arm_timeout()
    # 无边框窗口不会自己拿到键盘焦点（Esc 就收不到），摆好位置后显式抢一次
    root.update()
    root.focus_force()
    return cv


def calibrate():
    """在屏幕上拉框设置区域截图范围。拉框 → 点「确定」→ 覆盖层立即消失（远小于3秒）。"""
    import tkinter as tk

    if _calibrating[0]:
        return
    _calibrating[0] = True
    try:
        # 框选哪块屏，就跟截屏取哪块屏保持一致（capture.monitor）
        spec = (load_config().get("capture") or {}).get("monitor") or "primary"
        with capture.open_screen() as sct:
            mon = capture.pick_monitor(sct, spec)
        # 先抓底图再建窗口：这会儿屏幕上还没有覆盖层，抓到的就是用户原本看到的画面
        try:
            bg_png = screen_png_b64(mon, (int(mon["width"]), int(mon["height"])))
        except Exception as e:
            log.warning("框选底图抓取失败（%s），改用纯黑底", e)
            bg_png = None
        root = tk.Tk()
        build_calibrate_ui(root, mon, bg_png)
        # 遮罩是自家窗口，能真防截屏（控制台窗口不行，那是系统限制）；映射完成后再设
        from . import win_stealth

        root.after(120, lambda: win_stealth.protect_tk_window(root, load_config()))
        root.mainloop()
    finally:
        _calibrating[0] = False


def open_calibrate_async():
    if _calibrating[0]:
        return False
    threading.Thread(target=calibrate, daemon=True).start()
    return True


# ---------- 启动 ----------

async def run_pc(cfg, host_override=None):
    hub = Hub()
    hub.loop = asyncio.get_running_loop()
    hub.q = asyncio.Queue(maxsize=200)
    bus = PanelBus()
    bus.start(hub.loop)
    _region_state["region"] = cfg.get("region")
    # 界面状态上下文：早设，让防火墙自检等后台事件能尽早把状态/日志推给页面
    _ui.update(cfg=cfg, host=host_override, hub=hub, bus=bus)
    if cfg["transfer"]["mode"] == "relay" and cfg["transfer"]["relay_url"]:
        hub.relay = RelayClient(cfg["transfer"]["relay_url"], cfg["transfer"]["relay_token"])

    app = build_app(cfg, hub)
    app["bus"] = bus
    # 手机端「遥控截图」、鼠标边角/横划三个入口共用同一个截图处理器（去抖也是共享的）
    snap = make_capture_handler(hub, cfg)
    app["snap"] = snap
    app["client"] = ClientSession()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host_override or cfg["server"]["host"], int(cfg["server"]["port"]))
    try:
        await site.start()
    except OSError as e:
        log.error("端口 %s 启动失败（%s）。请修改 config.json 的 server.port", cfg["server"]["port"], e)
        ui.message_box(f"端口 {cfg['server']['port']} 启动失败。\n\n{e}\n\n"
                       "请关掉占用它的程序，或修改 config.json 的 server.port 后重试。")
        await app["client"].close()
        return

    sender = asyncio.ensure_future(sender_task(hub))
    panel_task = asyncio.ensure_future(panel_sender(bus))

    # 广播出去让手机端 App 自动发现（局域网模式才有意义；失败不影响任何功能）
    advertiser = start_advertiser(cfg)
    # 防火墙自检：手机连不上时最常见的根因，早说早省事
    start_firewall_check(cfg)

    async def on_resync():
        items = list(hub.recent)[-5:]
        if items:
            await hub.broadcast({"type": "sync", "items": items}, record=False)

    async def on_snap():
        """中转模式下手机发来的「遥控截图」指令：触发后把受理结果回给手机。"""
        res = trigger_snap(app)
        if hub.relay is not None:
            try:
                await hub.relay.send({"type": "snap_ack", **res})
            except Exception as e:
                log.debug("snap_ack 发送失败: %s", e)

    async def on_relay_msg(data):
        """中转发来的手机消息（统一设置 / 动作 / 遥控截图），与直连手机共用一套处理。"""
        t = data.get("type")
        if t == "resync":
            await on_resync()
        elif t == "snap":
            await on_snap()
        elif t == "ack":
            log.info("手机已收到内容 %s", data.get("id"))
        elif t in ("get_settings", "settings_set", "action"):
            await handle_client_message(app, data)
        elif t == "llm_request":
            async def answer():
                result = await app["shared_llm"].forward(app["client"], data.get("body"))
                await hub.relay.send({"type": "llm_result", "request_id": data.get("request_id"), **result})
            if len(app["llm_tasks"]) < 32:
                task = asyncio.create_task(answer())
                app["llm_tasks"].add(task)
                task.add_done_callback(app["llm_tasks"].discard)
        else:
            log.debug("未处理的中转消息：%s", t)

    relay_task = asyncio.ensure_future(hub.relay.run(on_relay_msg)) if hub.relay else None

    register_hotkeys(cfg, hub)
    # 两个"免按键"触发与手机遥控截图共用同一个截图处理函数，按键去抖也是共享的。
    # 注意去抖窗口只有几百毫秒：从屏幕角落起手、划满门槛距离后才结束的那种动作，
    # 两次触发间隔超过窗口，是会各截一张的（角落那次 + 划动那次）。
    mouse_paused = lambda: app["swipe_lab"].active
    start_corner_snap(cfg, snap, mouse_paused)
    start_swipe_snap(cfg, snap, mouse_paused)
    stop_evt = asyncio.Event()
    app["stop_evt"] = stop_evt  # 手机端「关闭电脑端」/api/quit 用
    _ui["stop_evt"] = stop_evt  # 窗口按钮 / 热键退出走 request_stop()
    try:
        import keyboard

        keyboard.add_hotkey(cfg["hotkeys"].get("quit", "ctrl+alt+q"), request_stop, suppress=False)
    except Exception:
        pass

    refresh_status()  # 首屏：把全部状态填上
    log.info("已就绪：等待手机扫码连接")
    await stop_evt.wait()

    log.info("正在退出…")
    _corner_stop.set()
    _swipe_stop.set()
    # 先断开所有 WebSocket（控制台页面 / 手机 / 中转），否则 runner.cleanup()
    # 会一直等它们关闭——页面那头（WebView2）不会自己断，退出就永远卡住。
    for ws in list(bus.clients):
        try:
            await ws.close(code=1001, message=b"bye")
        except Exception:
            pass
    bus.clients.clear()
    for ws in list(hub.phones | hub.overlays):
        try:
            await ws.close(code=1001, message=b"bye")
        except Exception:
            pass
    hub.phones.clear()
    hub.overlays.clear()
    advertiser.stop()
    try:
        import keyboard

        keyboard.unhook_all()
    except Exception:
        pass
    sender.cancel()
    panel_task.cancel()
    if relay_task:
        relay_task.cancel()
    await runner.cleanup()
    await app["client"].close()


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    sh = _UiLogHandler()
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    logdir = BASE_DIR / "logs"
    logdir.mkdir(exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(logdir / "pc.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="无障碍助手 PC 端")
    ap.add_argument("--relay", action="store_true", help="以中转服务器模式运行")
    ap.add_argument("--calibrate", action="store_true", help="框选区域截图范围")
    ap.add_argument("--port", type=int)
    ap.add_argument("--host")
    ap.add_argument("--config")
    args = ap.parse_args()

    setup_logging()
    if args.calibrate:
        calibrate()
        return
    cfg = load_config(args.config)
    if args.port:
        cfg["server"]["port"] = args.port
    if args.relay:
        from .relay_server import run_relay

        asyncio.run(run_relay(cfg["server"]["port"]))
        return

    # 端口预检：已在跑的实例（窗口可能被隐藏）给出明确指引，而不是启动到一半失败
    state = _probe_port(cfg["server"]["port"])
    if state == "running":
        ui.message_box("无障碍助手已在运行。\n\n"
                       "窗口可能被隐藏了——在电脑上按 Ctrl+Alt+H 可重新显示，"
                       "或直接用手机端页面操作。")
        return
    if state == "busy":
        ui.message_box(f"端口 {cfg['server']['port']} 已被其他程序占用。\n\n"
                       "请关掉占用它的程序，或修改 config.json 的 server.port 后重试。")
        return

    # 主窗口（WebView2 网页窗）：GUI 主循环在主线程，服务器跑在它拉起的工作线程里；
    # 页面按钮 / Ctrl+Alt+Q / 手机端「关闭电脑端」都会结束服务器，随后窗口自动关上。
    # 窗口模块不可用（缺 WebView2）就退化为无窗口模式：手机端不受影响，
    # 控制台页面可用任意浏览器打开主窗口原本显示的地址。
    from . import win_stealth

    _checks["stealth"] = ("无窗口模式（页面用浏览器打开）", None)

    def _on_window_ready():
        _checks["stealth"] = (win_stealth.taskbar_text(), win_stealth.taskbar_ok())
        refresh_status()

    def _server_main():
        asyncio.run(run_pc(cfg, args.host))

    if ui.start(cfg, version=VERSION, server_main=_server_main,
                on_ready=_on_window_ready, on_gui_exit=request_stop):
        return
    asyncio.run(run_pc(cfg, args.host))


if __name__ == "__main__":
    main()
