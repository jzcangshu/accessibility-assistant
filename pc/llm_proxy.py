"""大模型 API 代理核心：把手机端的统一请求转换为三种端点格式。

支持：
  chat       OpenAI Chat Completions  POST {base}/chat/completions
  responses  OpenAI Responses         POST {base}/responses
  anthropic  Anthropic Messages       POST {base}/v1/messages（base 以 /v1 结尾时直接拼 /messages）

手机端统一发 {base, key, model, api, timeout, messages}，这里负责转换并提取纯文本。
"""
import asyncio
import json
import re

import aiohttp

_DATAURL_RE = re.compile(r"^data:([^;,]+);base64,(.*)$", re.S)
TYPES = ("chat", "responses", "anthropic")


def _convert_content(user_msg, to_anthropic):
    """把统一消息体转换为 Anthropic / Responses 需要的内容块；字符串原样处理。"""
    content = (user_msg or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if to_anthropic else content
    out = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            url_s = str((part.get("image_url") or {}).get("url", ""))
            if to_anthropic:
                m = _DATAURL_RE.match(url_s)
                if m:
                    out.append({"type": "image", "source": {
                        "type": "base64", "media_type": m.group(1), "data": m.group(2)}})
            else:
                out.append({"type": "input_image", "image_url": url_s})
        elif part.get("type") == "text":
            out.append({"type": "text" if to_anthropic else "input_text",
                        "text": part.get("text", "")})
    return out


def _extract_text(api_type, data):
    if api_type == "responses":
        t = data.get("output_text")
        if isinstance(t, str) and t.strip():
            return t
        parts = []
        for o in data.get("output") or []:
            for b in (o or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "output_text":
                    parts.append(b.get("text", ""))
        return "".join(parts)
    if api_type == "anthropic":
        return "".join(b.get("text", "") for b in data.get("content") or []
                       if isinstance(b, dict) and b.get("type") == "text")
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


async def forward(client, body):
    """发起到渠道的请求，返回可直接 json_response 的 dict。"""
    base = str(body.get("base", "")).strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        return {"ok": False, "error": "渠道地址(base)无效"}
    api_type = str(body.get("api") or "chat").strip().lower()
    if api_type not in TYPES:
        api_type = "chat"
    try:
        timeout = min(max(float(body.get("timeout") or 20), 3), 300)
    except (TypeError, ValueError):
        timeout = 20

    messages = body.get("messages") or []
    system_text = ""
    user_msg = None
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            user_msg = m
        elif isinstance(m, dict) and m.get("role") == "system" and isinstance(m.get("content"), str):
            system_text = m["content"]

    key = str(body.get("key") or "")
    headers = {"Content-Type": "application/json"}
    if api_type == "anthropic":
        if key:
            headers["x-api-key"] = key
            headers["Authorization"] = "Bearer " + key
        headers["anthropic-version"] = "2023-06-01"
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        payload = {
            "model": body.get("model"),
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": _convert_content(user_msg, True)}],
        }
        if system_text:
            payload["system"] = system_text
    elif api_type == "responses":
        if key:
            headers["Authorization"] = "Bearer " + key
        url = base + "/responses"
        payload = {
            "model": body.get("model"),
            "max_output_tokens": 1000,
            "input": _convert_content(user_msg, False),
        }
        if system_text:
            payload["instructions"] = system_text
    else:
        if key:
            headers["Authorization"] = "Bearer " + key
        url = base + "/chat/completions"
        payload = {"model": body.get("model"), "messages": messages,
                   "temperature": 0.2, "max_tokens": 1000, "stream": False}

    try:
        async with client.post(url, json=payload, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            raw = await resp.text()
        if resp.status >= 400:
            return {"ok": False, "error": f"HTTP {resp.status}: {raw[:200]}"}
        text = _extract_text(api_type, json.loads(raw))
        if not str(text).strip():
            return {"ok": False, "error": "空回复"}
        return {"ok": True, "text": text}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout"}
    except json.JSONDecodeError:
        return {"ok": False, "error": "响应不是合法 JSON"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
