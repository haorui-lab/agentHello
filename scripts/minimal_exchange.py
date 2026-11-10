"""两个 MCP 进程之间的最小通话。凭证只从文件读取，不打印。"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from hello_center.images import make_png
from verify_e2e import image_ok, loads, start_worker


def read_token(path: Path) -> str:
    token = path.read_text().strip()
    if len(token) < 16:
        raise SystemExit(f"{path} 里没有凭证")
    return token


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file-a", required=True)
    parser.add_argument("--token-file-b", required=True)
    args = parser.parse_args()
    token_a = read_token(Path(args.token_file_a))
    token_b = read_token(Path(args.token_file_b))
    work = Path(args.token_file_a).resolve().parent
    agent_a = await start_worker(args.url, work / "a.log", authorization=f"Bearer {token_a}")
    agent_b = await start_worker(args.url, work / "b.log", authorization=f"Bearer {token_b}")
    try:
        who_a = loads(await agent_a.request("hello_whoami")) or {}
        who_b = loads(await agent_b.request("hello_whoami")) or {}
        if not who_a.get("agent_id") or who_a["agent_id"] == who_b.get("agent_id"):
            raise SystemExit("两个 Agent 的身份不正确")
        print(f"PASS identity {who_a['agent_id']} {who_b['agent_id']}", flush=True)
        if (loads(await agent_a.request("hello_heartbeat")) or {}).get("online") is not True:
            raise SystemExit("A 没有上线")
        if (loads(await agent_b.request("hello_heartbeat")) or {}).get("online") is not True:
            raise SystemExit("B 没有上线")
        call = loads(await agent_a.request("hello_call", {"peer_agent_id": who_b["agent_id"], "text": "minimal-hello"}))
        if not call or call.get("outcome") != "session":
            raise SystemExit(f"没有自动接通: {call}")
        session_id = call["session_id"]
        png = make_png(2, 2, (20, 180, 40))
        sent = loads(
            await agent_a.request(
                "hello_send",
                {
                    "session_id": session_id,
                    "text": "minimal-image",
                    "image_base64": base64.b64encode(png).decode(),
                    "image_mime": "image/png",
                },
            )
        )
        if not sent or not sent.get("ok"):
            raise SystemExit("图片没有发出")
        awaited = await agent_b.request("hello_await", {"after_seq": 0, "timeout_ms": 2000}, timeout=20)
        body = loads(awaited) or {}
        texts = [item.get("text") for item in body.get("events") or []]
        if "minimal-hello" not in texts or "minimal-image" not in texts or not image_ok(awaited, png):
            raise SystemExit("B 没有收到文字或原图")
        print("PASS text-and-image", flush=True)
        hung = loads(await agent_a.request("hello_hangup", {"session_id": session_id}))
        if not hung or hung.get("status") != "ended" or hung.get("online") is not True:
            raise SystemExit("挂断没有只结束这一通")
        print("PASS hangup", flush=True)
        if (loads(await agent_b.request("hello_offline")) or {}).get("online") is not False:
            raise SystemExit("B 没有离线")
        mail = loads(
            await agent_a.request(
                "hello_call",
                {"peer_agent_id": who_b["agent_id"], "text": "minimal-mail"},
            )
        )
        if not mail or mail.get("outcome") != "mailbox":
            raise SystemExit("离线时没有进入留言箱")
        listed = loads(await agent_b.request("hello_mailbox_list")) or {}
        if not any(item.get("text") == "minimal-mail" for item in listed.get("items") or []):
            raise SystemExit("留言箱里没有这条留言")
        if listed.get("started_session") is not False:
            raise SystemExit("读留言不该建立会话")
        print("PASS mailbox", flush=True)
        print(json.dumps({"ok": True, "agent_a": who_a["agent_id"], "agent_b": who_b["agent_id"]}, ensure_ascii=False))
    finally:
        await agent_a.close()
        await agent_b.close()


if __name__ == "__main__":
    asyncio.run(main())
