"""中转模式客户端：PC 主动连出（无需公网 IP），断线自动重连。"""
import asyncio
import json
import logging

import aiohttp

log = logging.getLogger("relay-client")


class RelayClient:
    def __init__(self, url, token):
        self.url = str(url).strip().rstrip("/")
        self.token = str(token).strip()
        self.ws = None

    @property
    def connected(self):
        return self.ws is not None

    async def run(self, on_msg):
        """连上中转服务器并把收到的每条 JSON 消息交给 on_msg 处理（resync / 统一设置 /
        遥控截图 / ack 都在 on_msg 里分发，见 main.on_relay_msg）。

        断线自动重连；on_msg 抛异常不影响连接本身。
        """
        if not self.url or not self.token:
            log.error("中转模式需要在 config.json 配置 transfer.relay_url 和 relay_token")
            return
        backoff = 1
        async with aiohttp.ClientSession() as ses:
            while True:
                try:
                    async with ses.ws_connect(
                        f"{self.url}/ws/pc?token={self.token}",
                        heartbeat=15,
                        timeout=aiohttp.ClientWSTimeout(ws_close=10),
                    ) as ws:
                        self.ws = ws
                        backoff = 1
                        log.info("已连接中转服务器 %s", self.url)
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(msg.data)
                                except Exception:
                                    continue
                                try:
                                    await on_msg(data)
                                except asyncio.CancelledError:
                                    raise
                                except Exception:
                                    log.exception("处理中转消息失败：%s", data.get("type"))
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("中转连接断开：%s，%ds 后重连", e, backoff)
                finally:
                    self.ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    async def send(self, item):
        ws = self.ws
        if ws is None:
            raise RuntimeError("中转未连接")
        await ws.send_json(item, dumps=lambda o: json.dumps(o, ensure_ascii=False))
