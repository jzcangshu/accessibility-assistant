# -*- coding: utf-8 -*-
"""横向平移触发的试验台 —— 让你亲自划着试手感。

    python tools/swipe_lab.py
    浏览器打开 http://127.0.0.1:8770/

它直接复用 pc.swipe 里真实生效的那套代码（SwipeTracker / mouse_button_down /
run_swipe_monitor），所以这里测出来的行为和实际运行完全一致，不存在"另写一份近似实现"
的问题。它只读光标位置和鼠标键状态：**不截屏、不注册热键、不改 config.json**。

页面上能现场调"划动距离"（就是 config.json 的 swipe_snap.min_distance），边划边看
每一项阈值过没过。加 --simulate 可以在没人操作鼠标时喂合成轨迹，用来看页面本身。
"""
import argparse
import asyncio
import json
import logging
import sys
import threading
import time
from collections import deque
from pathlib import Path

from aiohttp import WSMsgType, web

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pc.swipe import (  # noqa: E402
    gesture_metrics,
    SWIPE_DIST_RATIO,
    SWIPE_LIMITS,
    SwipeTracker,
    run_swipe_monitor,
)

PORT = 8770
BROADCAST_HZ = 25
# 真实运行时默认 2.0 秒；试验台缩短一点，方便连着划几次对比
LAB_COOLDOWN = 1.0

log = logging.getLogger("lab")

stop_event = threading.Event()
snapshot = {}
triggers = deque(maxlen=12)
state = {"count": 0}

# 试验台的配置——run_swipe_monitor 每轮都会重新读，所以页面上的滑块能立刻生效
cfg = {"swipe_snap": {"enabled": True, "min_distance": None, "cooldown": LAB_COOLDOWN}}


def build_snapshot(tracker, x, y, pressed, now, fired):
    """把 tracker 的状态摊开成页面能直接画的形状。纯展示，不参与判定。"""
    if fired:
        # 触发那一刻轨迹已被 tracker 清空，改用它存下来的那一笔实测数据
        g = tracker.last_gesture
        m = {k: g.get(k, 0) for k in ("n", "span", "dist", "drift", "speed", "pathRatio")}
    else:
        m = gesture_metrics(tracker.samples)

    lim = tracker.limits
    need = float(tracker.min_distance)
    checks = [
        {"k": "取样点", "v": str(m["n"]), "lim": "≥ %d" % lim["min_samples"],
         "ok": m["n"] >= lim["min_samples"]},
        {"k": "持续时间", "v": "%.2f s" % m["span"], "lim": "≥ %.2f s" % lim["min_span"],
         "ok": m["span"] >= lim["min_span"]},
        {"k": "横向位移", "v": "%d px" % m["dist"], "lim": "≥ %d px" % need,
         "ok": m["dist"] >= need},
        {"k": "纵向偏移", "v": "%d px" % m["drift"], "lim": "≤ %.2f×横向" % lim["max_slope"],
         "ok": m["drift"] <= lim["max_slope"] * m["dist"]},
        {"k": "速度", "v": "%.0f px/s" % m["speed"],
         "lim": "%.0f~%.0f" % (lim["min_speed"], lim["max_speed"]),
         "ok": lim["min_speed"] <= m["speed"] <= lim["max_speed"]},
        {"k": "路线笔直度", "v": ("%.2f×" % m["pathRatio"]) if m["dist"] else "—",
         "lim": "≤ %.2f×" % lim["max_path"],
         "ok": m["dist"] > 0 and m["pathRatio"] <= lim["max_path"]},
    ]
    return {
        "t": now, "x": x, "y": y, "pressed": pressed,
        "armed": tracker.armed, "fired": bool(fired),
        "dist": round(m["dist"]), "need": round(need), "speed": round(m["speed"]),
        "drift": round(m["drift"]), "span": round(m["span"], 2),
        "count": state["count"], "recent": list(triggers),
        "enabled": bool(cfg["swipe_snap"].get("enabled", True)),
        "ratio": SWIPE_DIST_RATIO, "window": round(tracker.window, 2),
        "checks": checks,
    }


def on_sample(tracker, x, y, pressed, now, fired):
    # 注意顺序：先喂快照再记触发，on_trigger 才能读到这一帧的实测数据
    global snapshot
    snapshot = build_snapshot(tracker, x, y, pressed, now, fired)


def on_trigger():
    state["count"] += 1
    s = snapshot
    triggers.appendleft({
        "n": state["count"],
        "dist": s.get("dist"), "span": s.get("span"),
        "speed": s.get("speed"), "drift": s.get("drift"),
        "at": time.strftime("%H:%M:%S"),
    })


def run_simulation():
    """--simulate：不碰真实鼠标，喂一段合成轨迹，用来看页面本身。

    轨迹按新阈值配的：400px / 1.6 秒 ≈ 250px/s，正好落在 150~380px/s 里。
    """
    tracker = SwipeTracker(350.0)
    base = 400
    while not stop_event.is_set():
        tracker.min_distance = float(cfg["swipe_snap"].get("min_distance") or 350)
        for _ in range(60):  # 静止 1.2 秒，重新武装
            if stop_event.is_set():
                return
            now = time.time()
            fired = tracker.feed(now, base, 500, False)
            on_sample(tracker, base, 500, False, now, fired)
            time.sleep(0.02)
        for i in range(80):  # 横划 400px / 1.6 秒 ≈ 250px/s
            if stop_event.is_set():
                return
            now = time.time()
            x = base + i * 5
            fired = tracker.feed(now, x, 500, False)
            on_sample(tracker, x, 500, False, now, fired)
            if fired:
                on_trigger()
                break
            time.sleep(0.02)


async def index(request):
    return web.FileResponse(Path(__file__).with_suffix(".html"))


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)
    request.app["sockets"].add(ws)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            kind = data.get("type")
            if kind == "distance":
                cfg["swipe_snap"]["min_distance"] = float(data.get("value") or 0)
            elif kind == "enabled":
                cfg["swipe_snap"]["enabled"] = bool(data.get("value"))
            elif kind == "reset":
                triggers.clear()
                state["count"] = 0
    finally:
        request.app["sockets"].discard(ws)
    return ws


async def broadcaster(app):
    while True:
        await asyncio.sleep(1 / BROADCAST_HZ)
        if not app["sockets"]:
            continue
        payload = json.dumps(snapshot, ensure_ascii=False)
        for ws in list(app["sockets"]):
            try:
                await ws.send_str(payload)
            except Exception:
                app["sockets"].discard(ws)


async def serve(port, simulate):
    app = web.Application()
    app["sockets"] = set()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()

    if simulate:
        threading.Thread(target=run_simulation, name="sim", daemon=True).start()
    else:
        # 第 4 个参数 on_sample 必须传：没有它就不会生成快照，页面收不到任何数据
        threading.Thread(target=run_swipe_monitor, args=(cfg, on_trigger, stop_event, on_sample),
                         name="monitor", daemon=True).start()
    asyncio.ensure_future(broadcaster(app))

    print(f"\n  试验台已启动 → http://127.0.0.1:{port}/", flush=True)
    if simulate:
        print("  （--simulate：喂的是合成轨迹，不是你的鼠标）", flush=True)
    else:
        print("  把鼠标横着划一下（不要按任何键），页面上会实时显示每一项阈值。", flush=True)
    print("  Ctrl+C 退出。\n", flush=True)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        stop_event.set()
        await runner.cleanup()


def main():
    ap = argparse.ArgumentParser(description="横向平移触发试验台（只读鼠标，不截屏）")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--simulate", action="store_true", help="喂合成轨迹，用于检查页面本身")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        asyncio.run(serve(args.port, args.simulate))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
