"""相同请求（内容 + 参数）共享一次上游调用；只在内存保留短期结果，不记录凭据。"""
import asyncio
import hashlib
import json
import time
from collections import OrderedDict

from . import llm_proxy


class SharedLLM:
    def __init__(self, limit=96, ttl=600):
        self.pending = {}
        self.results = OrderedDict()
        self.limit, self.ttl = limit, ttl

    async def forward(self, client, body):
        if not isinstance(body, dict):
            return {"ok": False, "error": "请求格式无效"}
        question = body.get("question_id")
        if not isinstance(question, str) or not question:
            return await llm_proxy.forward(client, body)
        key = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).digest()
        now = time.monotonic()
        for old, (ts, _) in list(self.results.items()):
            if now - ts > self.ttl:
                del self.results[old]
        if key in self.results:
            return self.results[key][1]
        if key not in self.pending:
            if len(self.pending) >= 16:
                return {"ok": False, "error": "正在处理的请求过多，请稍后重试"}
            self.pending[key] = asyncio.create_task(self._run(key, client, body))
        # 一个页面取消请求不能中断仍在等待同一结果的另一个页面。
        return await asyncio.shield(self.pending[key])

    async def _run(self, key, client, body):
        try:
            result = await llm_proxy.forward(client, body)
            if result.get("ok"):
                self.results[key] = (time.monotonic(), result)
                while len(self.results) > self.limit:
                    self.results.popitem(last=False)
            return result
        finally:
            self.pending.pop(key, None)

    async def close(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.results.clear()
