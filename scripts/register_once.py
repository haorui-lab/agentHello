"""操作者登记一次 Agent。凭证写入文件，标准输出只有 Agent ID。

这不是 Agent 运行时，也不会替任何人守着连接。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def register(url: str, secret: str, token_file: Path, display_name: str) -> str:
    headers = {"X-Hello-Register-Secret": secret}
    timeout = httpx2.Timeout(30.0, read=30.0)
    async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=30) as session:
                await session.initialize()
                result = await session.call_tool(
                    "hello_register",
                    {"display_name": display_name} if display_name else {},
                    read_timeout_seconds=30,
                )
    if getattr(result, "is_error", False):
        text = " ".join(getattr(block, "text", "") for block in result.content)
        raise SystemExit(f"登记失败: {text}")
    text = next(block.text for block in result.content if getattr(block, "type", "") == "text")
    payload = json.loads(text)
    token = payload.get("token") or ""
    agent_id = payload.get("agent_id") or ""
    if not token or not agent_id:
        raise SystemExit("登记返回里没有身份信息")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(token_file, flags, 0o600)
    try:
        os.write(fd, (token + "\n").encode())
    finally:
        os.close(fd)
    return agent_id


def main() -> None:
    parser = argparse.ArgumentParser(description="登记一个 Agent，凭证只写入文件")
    parser.add_argument("--url", default=os.environ.get("HELLO_URL", "http://127.0.0.1:8731/mcp"))
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--display-name", default="")
    args = parser.parse_args()
    secret = os.environ.get("HELLO_REGISTER_SECRET", "")
    if len(secret) < 8:
        raise SystemExit("没有 HELLO_REGISTER_SECRET。先运行 scripts/init_local_env.sh，再 set -a; . ./config/hello-center.env; set +a。不要把口令写进命令。")
    token_file = Path(args.token_file)
    if token_file.exists():
        raise SystemExit(f"已存在 {token_file}，没有覆盖。")
    agent_id = asyncio.run(register(args.url, secret, token_file, args.display_name))
    print(agent_id)


if __name__ == "__main__":
    main()
