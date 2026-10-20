"""中转服务器：部署在任意有公网 IP 的机器上（python -m pc.main --relay）。

PC 用 token 连 /ws/pc，手机用同一 token 作房间码连 /ws/phone，
两端消息按房间互转；同时托管手机端页面与 /api/llm 代理。
"""
import asyncio
import json
import logging
from pathlib import Path

import aiohttp
from aiohttp import web, WSMsgType

from . import llm_proxy
from .config import RESOURCE_ROOT

log = logging.getLogger("relay")
WEB_DIR = RESOURCE_ROOT / "web"
STATIC_FILES = ("app.js", "style.css", "manifest.json")


async def api_llm(request):
    """大模型 API 代理（Chat Completions / Responses / Anthropic）。"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "请求体不是合法 JSON"})
    return web.json_response(await llm_proxy.forward(request.app["client"], body))


def _static_handler(path: Path):
    async def handler(request):
        if not path.exists():
            return web.Response(status=404, text="missing")
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    return handler


async def run_relay(port):
    rooms = {}  # token -> {"pc": ws|None, "phones": set()}

    async def ws_pc(request):
        token = request.query.get("token", "").strip()
        if not token:
            return web.Response(status=400, text="missing token")
        ws = web.WebSocketResponse(heartbeat=15)
        await ws.prepare(request)
        room = rooms.setdefault(token, {"pc": None, "phones": set()})
        room["pc"] = ws
        log.info("PC 上线 room=%s", token)
        for ph in list(room["phones"]):
            try:
                await ph.send_json({"type": "pc_status", "online": True})
            except Exception:
                pass
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    for ph in list(room["phones"]):
                        try:
                            await ph.send_str(msg.data)
                        except Exception:
                            pass
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            if room["pc"] is ws:
                room["pc"] = None
            for ph in list(room["phones"]):
                try:
                    await ph.send_json({"type": "pc_status", "online": False})
                except Exception:
                    pass
            log.info("PC 离线 room=%s", token)
        return ws

    async def ws_phone(request):
        token = request.query.get("room", "").strip()
        ws = web.WebSocketResponse(heartbeat=15)
        await ws.prepare(request)
        room = rooms.setdefault(token, {"pc": None, "phones": set()})
        room["phones"].add(ws)
        await ws.send_json({"type": "pc_status", "online": room["pc"] is not None})
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        continue
                    t = data.get("type")
                    if t == "ping":
                        await ws.send_json({"type": "pong"})
                    # 转发给 PC 的消息白名单：ack/resync/snap 是老协议，
                    # get_settings/settings_set/action 支撑统一设置的跨端同步（旧版
                    # 中转服务器不认识这几个——手机端会自动退回本地设置模式，不炸）
                    elif t in ("ack", "resync", "snap", "get_settings",
                               "settings_set", "action", "llm_request"):
                        pc = room["pc"]
                        if pc is not None:
                            try:
                                await pc.send_str(msg.data)
                            except Exception:
                                if t == "snap":
                                    await ws.send_json({"type": "snap_ack", "ok": False,
                                                        "error": "转发失败"})
                        elif t == "snap":
                            # 电脑端不在线：直接回绝，免得手机一直等
                            await ws.send_json({"type": "snap_ack", "ok": False,
                                                "error": "电脑端未连接中转"})
        finally:
            room["phones"].discard(ws)
        return ws

    async def index(request):
        return web.FileResponse(WEB_DIR / "index.html")

    async def api_snap(request):
        """手机端「遥控截图」在 WS 断连时的 HTTP 兜底：把指令转发给房间里的 PC。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = str(body.get("room") or request.query.get("room") or "").strip()
        room = rooms.get(token)
        if not room or room["pc"] is None:
            return web.json_response({"ok": False, "error": "电脑端未连接中转"})
        try:
            await room["pc"].send_json({"type": "snap"})
        except Exception:
            return web.json_response({"ok": False, "error": "转发失败"})
        return web.json_response({"ok": True, "error": ""})

    async def status(request):
        return web.json_response({"ok": True, "mode": "relay", "version": "1.3.1"})

    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/ws/pc", ws_pc)
    app.router.add_get("/ws/phone", ws_phone)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/llm", api_llm)
    app.router.add_post("/api/snap", api_snap)
    for f in STATIC_FILES:
        app.router.add_get("/static/" + f, _static_handler(WEB_DIR / f))

    app["client"] = aiohttp.ClientSession()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port))
    await site.start()
    log.info("中转服务器已启动，端口 %d", port)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        await app["client"].close()
