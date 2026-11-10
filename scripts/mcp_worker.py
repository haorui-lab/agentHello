"""测试夹具：一个独立进程里的 MCP 客户端。

这不是 Agent 产品，也没有自己的通话逻辑。它只把 stdin 上的工具调用原样交给 Hello Center。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import traceback

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _block(item: object) -> dict:
    kind = getattr(item, "type", "")
    if kind == "text":
        return {"type": "text", "text": item.text}
    if kind == "image":
        mime = getattr(item, "mime_type", None) or getattr(item, "mimeType", "")
        raw = base64.b64decode(item.data)
        out = {
            "type": "image",
            "mime_type": mime,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_length": len(raw),
        }
        if len(raw) <= 8192:
            out["base64"] = base64.b64encode(raw).decode("ascii")
        return out
    return {"type": str(kind)}


async def _serve() -> None:
    url = os.environ["HELLO_URL"]
    headers = {}
    if os.environ.get("HELLO_AUTHORIZATION"):
        headers["Authorization"] = os.environ["HELLO_AUTHORIZATION"]
    if os.environ.get("HELLO_REGISTER_SECRET"):
        headers["X-Hello-Register-Secret"] = os.environ["HELLO_REGISTER_SECRET"]
    timeout = httpx2.Timeout(80.0, read=80.0)
    async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=80) as session:
                await session.initialize()
                print(json.dumps({"ready": True, "pid": os.getpid()}, ensure_ascii=False), flush=True)
                while True:
                    line = await asyncio.to_thread(sys.stdin.readline)
                    if not line:
                        return
                    request = json.loads(line)
                    tool = request["tool"]
                    arguments = request.get("arguments") or {}
                    read_timeout = 80.0
                    if tool == "hello_await":
                        wait = arguments.get("timeout_ms")
                        read_timeout = ((wait if isinstance(wait, int) else 20000) / 1000) + 20
                    try:
                        result = await session.call_tool(tool, arguments, read_timeout_seconds=read_timeout)
                    except Exception as exc:
                        response = {
                            "id": request.get("id"),
                            "transport_error": True,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    else:
                        blocks = [_block(item) for item in getattr(result, "content", []) or []]
                        texts = [item["text"] for item in blocks if item["type"] == "text"]
                        response = {
                            "id": request.get("id"),
                            "is_error": bool(getattr(result, "is_error", False)),
                            "text": "\n".join(texts),
                            "images": [item for item in blocks if item["type"] == "image"],
                        }
                    print(json.dumps(response, ensure_ascii=False), flush=True)


def main() -> None:
    try:
        asyncio.run(_serve())
    except Exception:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ready": False, "error": "worker failed"}, ensure_ascii=False), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
