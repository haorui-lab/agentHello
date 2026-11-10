"""独立进程验证。

进程包括：Redis、Hello Center、两个（或更多）MCP 客户端进程。
这是同一台机器上的协议验证，不是跨机器验收，也不是 Claude Code 或 dsh 的真机验收。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import signal
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hello_center.images import make_png

PYTHON = sys.executable


class Suite:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        mark = "PASS" if ok else "FAIL"
        extra = f" {detail}" if detail and not ok else ""
        print(f"{mark} {name}{extra}", flush=True)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.checks if not item["ok"])


def loads(resp: dict) -> dict | None:
    if resp.get("transport_error") or resp.get("is_error"):
        return None
    try:
        return json.loads(resp.get("text") or "")
    except json.JSONDecodeError:
        return None


def error_code(resp: dict) -> str:
    try:
        return json.loads(resp.get("text") or "").get("error", "")
    except json.JSONDecodeError:
        return ""


class Worker:
    def __init__(self, proc: asyncio.subprocess.Process, name: str) -> None:
        self.proc = proc
        self.name = name
        self.next_id = 1
        self.lock = asyncio.Lock()

    async def request(self, tool: str, arguments: dict | None = None, timeout: float = 30) -> dict:
        if self.proc.stdin is None or self.proc.stdout is None:
            raise RuntimeError("worker pipes missing")
        async with self.lock:
            request_id = self.next_id
            self.next_id += 1
            line = json.dumps({"id": request_id, "tool": tool, "arguments": arguments or {}}, ensure_ascii=False)
            self.proc.stdin.write((line + "\n").encode())
            await self.proc.stdin.drain()
            raw = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
        if not raw:
            raise RuntimeError(f"{self.name} closed stdout")
        return json.loads(raw.decode())

    async def close(self) -> None:
        if self.proc.returncode is None:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()


async def wait_http(port: int) -> dict:
    url = f"http://127.0.0.1:{port}/healthz"
    last = ""
    for _ in range(50):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return json.loads(response.read().decode())
        except Exception as exc:
            last = str(exc)
            await asyncio.sleep(0.2)
    raise RuntimeError(f"center not healthy: {last}")


async def wait_redis(port: int) -> None:
    from hello_center.redis_client import connect

    last = ""
    for _ in range(50):
        redis = connect(f"redis://127.0.0.1:{port}/0")
        try:
            await redis.ping()
            return
        except Exception as exc:
            last = str(exc)
            await asyncio.sleep(0.2)
        finally:
            await redis.aclose()
    raise RuntimeError(f"redis not ready: {last}")


async def start_redis(binary: str, directory: Path, port: int, log_path: Path) -> asyncio.subprocess.Process:
    directory.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    proc = await asyncio.create_subprocess_exec(
        binary,
        "--bind",
        "127.0.0.1",
        "--port",
        str(port),
        "--dir",
        str(directory),
        "--dbfilename",
        "dump.rdb",
        "--appendonly",
        "yes",
        "--appendfsync",
        "always",
        "--save",
        "",
        "--protected-mode",
        "yes",
        "--daemonize",
        "no",
        stdout=log,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await wait_redis(port)
    except Exception:
        if proc.returncode is None:
            proc.kill()
        raise
    return proc


async def start_center(port: int, redis_url: str, secret: str, log_path: Path, extra: dict | None = None) -> asyncio.subprocess.Process:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1",
            "HELLO_REGISTER_SECRET": secret,
            "HELLO_REDIS_URL": redis_url,
            "HELLO_BIND": "127.0.0.1",
            "HELLO_PORT": str(port),
            "HELLO_PRESENCE_TTL": "8",
        }
    )
    if extra:
        env.update(extra)
    log = log_path.open("ab")
    proc = await asyncio.create_subprocess_exec(
        PYTHON,
        "-m",
        "hello_center",
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await wait_http(port)
    except Exception:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        text = log_path.read_text(errors="replace")[-2000:]
        raise RuntimeError(text) from None
    return proc


async def start_worker(url: str, log_path: Path, authorization: str = "", secret: str = "") -> Worker:
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1", "HELLO_URL": url})
    env.pop("HELLO_AUTHORIZATION", None)
    env.pop("HELLO_REGISTER_SECRET", None)
    if authorization:
        env["HELLO_AUTHORIZATION"] = authorization
    if secret:
        env["HELLO_REGISTER_SECRET"] = secret
    log = log_path.open("ab")
    proc = await asyncio.create_subprocess_exec(
        PYTHON,
        str(ROOT / "scripts" / "mcp_worker.py"),
        cwd=str(ROOT),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=log,
    )
    assert proc.stdout is not None
    raw = await asyncio.wait_for(proc.stdout.readline(), 20)
    ready = json.loads(raw.decode())
    if not ready.get("ready"):
        raise RuntimeError(f"worker not ready: {ready}")
    return Worker(proc, "worker")


async def stop_proc(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None or proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 3)
    except TimeoutError:
        proc.kill()
        await proc.wait()


def image_ok(resp: dict, expected: bytes) -> bool:
    images = resp.get("images") or []
    if not images or "base64" not in images[0]:
        return False
    got = base64.b64decode(images[0]["base64"])
    return got == expected and images[0].get("mime_type") == "image/png"


async def run(args: argparse.Namespace) -> dict:
    suite = Suite()
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(24)
    url = f"http://127.0.0.1:{args.center_port}/mcp"
    redis_url = f"redis://127.0.0.1:{args.redis_port}/0"
    redis_proc = None
    center = None
    limits = None
    workers: list[Worker] = []
    pids: dict[str, int] = {}
    agent_ids: dict[str, str] = {}
    health: dict = {}
    try:
        redis_proc = await start_redis(args.redis_server, work / "redis", args.redis_port, work / "redis.log")
        pids["redis"] = redis_proc.pid
        center = await start_center(args.center_port, redis_url, secret, work / "center.log")
        pids["center"] = center.pid
        health = await wait_http(args.center_port)
        suite.check("healthz", health.get("ok") is True and bool(health.get("redis_version")), str(health))

        bootstrap_a = await start_worker(url, work / "boot-a.log", secret=secret)
        bootstrap_b = await start_worker(url, work / "boot-b.log", secret=secret)
        workers.extend([bootstrap_a, bootstrap_b])
        reg_a = loads(await bootstrap_a.request("hello_register", {"display_name": "甲"}))
        reg_b = loads(await bootstrap_b.request("hello_register", {"display_name": "乙"}))
        suite.check("register A", bool(reg_a and reg_a.get("agent_id") and reg_a.get("token") and reg_a.get("online") is False))
        suite.check("register B", bool(reg_b and reg_b.get("agent_id") and reg_b.get("token")))
        suite.check(
            "two agent ids",
            bool(reg_a and reg_b and reg_a["agent_id"] != reg_b["agent_id"]),
            "",
        )
        if not reg_a or not reg_b:
            raise RuntimeError("registration failed")
        agent_ids["A"] = reg_a["agent_id"]
        agent_ids["B"] = reg_b["agent_id"]
        token_a = reg_a["token"]
        token_b = reg_b["token"]
        bare = await start_worker(url, work / "bare.log")
        workers.append(bare)
        denied = await bare.request("hello_whoami")
        suite.check("missing credential", denied.get("is_error") is True and error_code(denied) == "unauthenticated")
        bad_secret = await bare.request("hello_register", {"display_name": "不该成功"})
        suite.check("bad register secret", bad_secret.get("is_error") is True and error_code(bad_secret) == "forbidden")
        await bare.close()
        await bootstrap_a.close()
        await bootstrap_b.close()

        agent_a = await start_worker(url, work / "agent-a.log", authorization=f"Bearer {token_a}")
        agent_b = await start_worker(url, work / "agent-b.log", authorization=f"Bearer {token_b}")
        workers.extend([agent_a, agent_b])
        pids["agent_a"] = agent_a.proc.pid
        pids["agent_b"] = agent_b.proc.pid
        suite.check(
            "distinct pids",
            len({pids["redis"], pids["center"], pids["agent_a"], pids["agent_b"]}) == 4,
            str(pids),
        )
        who_a = loads(await agent_a.request("hello_whoami"))
        suite.check(
            "stable id after reconnect",
            bool(who_a and who_a.get("agent_id") == agent_ids["A"] and who_a.get("online") is False),
        )
        suite.check("heartbeat A", (loads(await agent_a.request("hello_heartbeat")) or {}).get("online") is True)
        suite.check("heartbeat B", (loads(await agent_b.request("hello_heartbeat")) or {}).get("online") is True)
        looked = loads(await agent_a.request("hello_lookup", {"peer_agent_id": agent_ids["B"]}))
        suite.check("lookup online", bool(looked and looked.get("registered") and looked.get("online") is True))

        call1 = loads(
            await agent_a.request(
                "hello_call",
                {"peer_agent_id": agent_ids["B"], "text": "S1-INTRO", "client_msg_id": "call-s1"},
            )
        )
        call2 = loads(await agent_a.request("hello_call", {"peer_agent_id": agent_ids["B"], "text": "S2-INTRO"}))
        suite.check("auto session 1", bool(call1 and call1.get("outcome") == "session"))
        suite.check("auto session 2", bool(call2 and call2.get("outcome") == "session" and call2.get("session_id") != (call1 or {}).get("session_id")))
        if not call1 or not call2:
            raise RuntimeError("calls failed")
        s1 = call1["session_id"]
        s2 = call2["session_id"]
        again = loads(
            await agent_a.request(
                "hello_call",
                {"peer_agent_id": agent_ids["B"], "text": "SHOULD-NOT-STORE", "client_msg_id": "call-s1"},
            )
        )
        suite.check(
            "duplicate call",
            bool(again and again.get("duplicate") is True and again.get("session_id") == s1),
        )

        png = make_png(2, 2, (200, 10, 10))
        sent_image = loads(
            await agent_a.request(
                "hello_send",
                {
                    "session_id": s1,
                    "text": "S1-TEXT-ALPHA",
                    "image_base64": base64.b64encode(png).decode(),
                    "image_mime": "image/png",
                    "client_msg_id": "img-1",
                },
            )
        )
        suite.check("send image", bool(sent_image and sent_image.get("duplicate") is False))
        dup_image = loads(
            await agent_a.request(
                "hello_send",
                {
                    "session_id": s1,
                    "text": "DIFFERENT-BODY",
                    "image_base64": base64.b64encode(png).decode(),
                    "image_mime": "image/png",
                    "client_msg_id": "img-1",
                },
            )
        )
        suite.check(
            "duplicate send keeps first id",
            bool(dup_image and dup_image.get("duplicate") is True and dup_image.get("message_id") == (sent_image or {}).get("message_id")),
        )
        suite.check(
            "send other session",
            (loads(await agent_a.request("hello_send", {"session_id": s2, "text": "S2-TEXT-BETA"})) or {}).get("ok") is True,
        )
        awaited = await agent_b.request("hello_await", {"after_seq": 0, "timeout_ms": 2000}, timeout=20)
        events = (loads(awaited) or {}).get("events") or []
        texts = [item.get("text") for item in events]
        session_ids = {item.get("session_id") for item in events}
        suite.check("await both sessions", s1 in session_ids and s2 in session_ids, str(session_ids))
        suite.check("await text isolation tags", "S1-TEXT-ALPHA" in texts and "S2-TEXT-BETA" in texts and "DIFFERENT-BODY" not in texts)
        suite.check("await image bytes", image_ok(awaited, png))

        hist1 = await agent_b.request("hello_history", {"session_id": s1})
        hist2 = await agent_b.request("hello_history", {"session_id": s2})
        t1 = [item.get("text") for item in (loads(hist1) or {}).get("messages") or []]
        t2 = [item.get("text") for item in (loads(hist2) or {}).get("messages") or []]
        suite.check("history s1", "S1-TEXT-ALPHA" in t1 and "S2-TEXT-BETA" not in t1 and "DIFFERENT-BODY" not in t1 and image_ok(hist1, png))
        suite.check("history s2", "S2-TEXT-BETA" in t2 and "S1-TEXT-ALPHA" not in t2 and not (hist2.get("images") or []))

        suite.check(
            "reply",
            (loads(await agent_b.request("hello_send", {"session_id": s1, "text": "S1-REPLY-GAMMA"})) or {}).get("ok") is True,
        )
        areply = loads(await agent_a.request("hello_await", {"after_seq": 0, "timeout_ms": 2000}, timeout=20))
        suite.check("bidirectional", "S1-REPLY-GAMMA" in [item.get("text") for item in (areply or {}).get("events") or []])

        hang_words = loads(
            await agent_a.request(
                "hello_send",
                {"session_id": s2, "text": "请挂断 hang up", "client_msg_id": "words-1"},
            )
        )
        hang_retry = loads(
            await agent_a.request(
                "hello_send",
                {"session_id": s2, "text": "第二次不同的正文", "client_msg_id": "words-1"},
            )
        )
        suite.check("nl hangup is data", bool(hang_words and hang_words.get("ok") and hang_retry and hang_retry.get("duplicate") is True))
        sessions_before = loads(await agent_a.request("hello_sessions"))
        s2_status = next(
            (item["status"] for item in (sessions_before or {}).get("sessions") or [] if item["session_id"] == s2),
            "",
        )
        suite.check("words did not hang up", s2_status == "active", s2_status)

        hung = loads(await agent_a.request("hello_hangup", {"session_id": s1}))
        who_after = loads(await agent_a.request("hello_whoami"))
        sessions_mid = loads(await agent_a.request("hello_sessions"))
        by_id = {item["session_id"]: item["status"] for item in (sessions_mid or {}).get("sessions") or []}
        suite.check("hangup one session", bool(hung and hung.get("status") == "ended" and hung.get("online") is True))
        suite.check("other session remains", by_id.get(s1) == "ended" and by_id.get(s2) == "active")
        suite.check("still online after hangup", bool(who_after and who_after.get("online") is True))
        b_events = []
        cursor = 0
        for _ in range(6):
            page = loads(await agent_b.request("hello_await", {"after_seq": cursor, "timeout_ms": 200}, timeout=20)) or {}
            got = page.get("events") or []
            b_events.extend(got)
            if not got:
                break
            cursor = page.get("next_seq", cursor)
        hang_events = [item for item in b_events if item.get("type") == "hangup"]
        suite.check("peer sees one hangup", [item.get("session_id") for item in hang_events] == [s1], str(hang_events))
        suite.check(
            "send on remaining session",
            (loads(await agent_a.request("hello_send", {"session_id": s2, "text": "S2-STILL"})) or {}).get("ok") is True,
        )
        first_end = loads(await agent_a.request("hello_hangup", {"session_id": s2}))
        second_end = loads(await agent_a.request("hello_hangup", {"session_id": s2}))
        suite.check("repeat hangup", bool(first_end and first_end.get("status") == "ended" and second_end and second_end.get("status") == "already_ended"))

        missing = await agent_a.request("hello_call", {"peer_agent_id": "80-000-099", "text": "没人"})
        suite.check("missing peer", missing.get("is_error") is True and error_code(missing) == "not_found")
        self_call = await agent_a.request("hello_call", {"peer_agent_id": agent_ids["A"], "text": "自己"})
        suite.check("self call", self_call.get("is_error") is True and error_code(self_call) == "bad_request")
        started = time.monotonic()
        timed = loads(await agent_a.request("hello_await", {"after_seq": 999999, "timeout_ms": 400}, timeout=20))
        elapsed = time.monotonic() - started
        suite.check(
            "await timeout",
            bool(timed and timed.get("events") == [] and timed.get("timed_out") is True and elapsed < 3),
            f"{elapsed:.2f}s",
        )

        boot_c = await start_worker(url, work / "boot-c.log", secret=secret)
        workers.append(boot_c)
        reg_c = loads(await boot_c.request("hello_register", {"display_name": "丙"}))
        await boot_c.close()
        if not reg_c:
            raise RuntimeError("C failed")
        agent_ids["C"] = reg_c["agent_id"]
        agent_c = await start_worker(url, work / "agent-c.log", authorization=f"Bearer {reg_c['token']}")
        workers.append(agent_c)
        pids["agent_c"] = agent_c.proc.pid
        forbidden_send = await agent_c.request("hello_send", {"session_id": s1, "text": "偷看"})
        forbidden_hist = await agent_c.request("hello_history", {"session_id": s1})
        forbidden_hang = await agent_c.request("hello_hangup", {"session_id": s1})
        suite.check("deny send", forbidden_send.get("is_error") is True and error_code(forbidden_send) == "forbidden")
        suite.check("deny history", forbidden_hist.get("is_error") is True and error_code(forbidden_hist) == "forbidden")
        suite.check("deny hangup", forbidden_hang.get("is_error") is True and error_code(forbidden_hang) == "session_ended" or error_code(forbidden_hang) == "forbidden")
        # s1 已经挂断。非参与者必须是 forbidden，不能因为会话已结束就改成另一种成功。
        suite.check("deny hangup is forbidden", error_code(forbidden_hang) == "forbidden", error_code(forbidden_hang))

        sessions_before_off = loads(await agent_b.request("hello_sessions"))
        off = loads(await agent_b.request("hello_offline"))
        sessions_after_off = loads(await agent_b.request("hello_sessions"))
        looked_off = loads(await agent_a.request("hello_lookup", {"peer_agent_id": agent_ids["B"]}))
        suite.check("explicit offline", bool(off and off.get("online") is False and looked_off and looked_off.get("online") is False))
        suite.check(
            "offline keeps sessions",
            [item["session_id"] for item in (sessions_before_off or {}).get("sessions") or []]
            == [item["session_id"] for item in (sessions_after_off or {}).get("sessions") or []],
        )
        mail_png = make_png(2, 2, (10, 40, 200))
        before_sessions = len((loads(await agent_b.request("hello_sessions")) or {}).get("sessions") or [])
        mailed = loads(
            await agent_a.request(
                "hello_call",
                {
                    "peer_agent_id": agent_ids["B"],
                    "text": "MAIL-TEXT",
                    "image_base64": base64.b64encode(mail_png).decode(),
                    "image_mime": "image/png",
                    "client_msg_id": "mail-1",
                },
            )
        )
        suite.check("offline mailbox", bool(mailed and mailed.get("outcome") == "mailbox" and mailed.get("session_id") is None))
        mail_again = loads(
            await agent_a.request(
                "hello_call",
                {
                    "peer_agent_id": agent_ids["B"],
                    "text": "OTHER",
                    "client_msg_id": "mail-1",
                },
            )
        )
        suite.check("duplicate mailbox", bool(mail_again and mail_again.get("duplicate") is True and mail_again.get("mailbox_id") == (mailed or {}).get("mailbox_id")))
        listed = loads(await agent_b.request("hello_mailbox_list"))
        suite.check(
            "list mailbox does not call",
            bool(listed and listed.get("started_session") is False and any(item.get("text") == "MAIL-TEXT" for item in listed.get("items") or [])),
        )
        suite.check(
            "list did not create session",
            len((loads(await agent_b.request("hello_sessions")) or {}).get("sessions") or []) == before_sessions,
        )
        who_b = loads(await agent_b.request("hello_whoami"))
        suite.check("reading mail stays offline", bool(who_b and who_b.get("online") is False))
        mail_id = (mailed or {}).get("mailbox_id")
        denied_mail = await agent_c.request("hello_mailbox_read", {"mailbox_id": mail_id})
        suite.check("deny mailbox", denied_mail.get("is_error") is True and error_code(denied_mail) == "forbidden")
        read_mail = await agent_b.request("hello_mailbox_read", {"mailbox_id": mail_id})
        read_body = loads(read_mail) or {}
        suite.check("mailbox image bytes", read_body.get("text") == "MAIL-TEXT" and read_body.get("started_session") is False and image_ok(read_mail, mail_png))
        empty_await = loads(await agent_b.request("hello_await", {"after_seq": 0, "timeout_ms": 300}, timeout=20))
        suite.check(
            "await ignores mailbox",
            not any(item.get("text") == "MAIL-TEXT" for item in (empty_await or {}).get("events") or []),
        )
        ack1 = loads(await agent_b.request("hello_mailbox_ack", {"mailbox_ids": [mail_id]}))
        ack2 = loads(await agent_b.request("hello_mailbox_ack", {"mailbox_ids": [mail_id]}))
        left = loads(await agent_b.request("hello_mailbox_list"))
        suite.check(
            "ack then repeat ack",
            bool(ack1 and ack1["results"][0]["status"] == "acked" and ack2 and ack2["results"][0]["status"] == "already_acked"),
        )
        suite.check("acked mail hidden", not any(item.get("mailbox_id") == mail_id for item in (left or {}).get("items") or []))

        # hello_await 会把 B 重新标成在线。留言恢复测试前要再次离线。
        suite.check("offline again", (loads(await agent_b.request("hello_offline")) or {}).get("online") is False)
        recover = loads(
            await agent_a.request(
                "hello_call",
                {"peer_agent_id": agent_ids["B"], "text": "RECOVER-ME", "client_msg_id": "mail-recover"},
            )
        )
        suite.check("second offline mail", bool(recover and recover.get("outcome") == "mailbox"))

        bad_image = await agent_a.request(
            "hello_send",
            {"session_id": s2, "image_base64": base64.b64encode(b"not-a-png").decode(), "image_mime": "image/png"},
        )
        suite.check("bad image", bad_image.get("is_error") is True and error_code(bad_image) == "bad_image")

        # 过期与进程退出共用同一次等待。B 先心跳，然后不再调用；D 心跳后杀掉进程。
        suite.check("B heartbeat before stale", (loads(await agent_b.request("hello_heartbeat")) or {}).get("online") is True)
        boot_d = await start_worker(url, work / "boot-d.log", secret=secret)
        workers.append(boot_d)
        reg_d = loads(await boot_d.request("hello_register"))
        await boot_d.close()
        agent_d = await start_worker(url, work / "agent-d.log", authorization=f"Bearer {reg_d['token']}")
        workers.append(agent_d)
        pids["agent_d"] = agent_d.proc.pid
        suite.check("D heartbeat", (loads(await agent_d.request("hello_heartbeat")) or {}).get("online") is True)
        agent_d.proc.kill()
        await agent_d.proc.wait()
        await asyncio.sleep(10)
        stale_b = loads(await agent_a.request("hello_lookup", {"peer_agent_id": agent_ids["B"]}))
        stale_d = loads(await agent_a.request("hello_lookup", {"peer_agent_id": reg_d["agent_id"]}))
        suite.check("stale presence", bool(stale_b and stale_b.get("online") is False))
        suite.check("dead process presence", bool(stale_d and stale_d.get("online") is False))

        # Redis 被强杀后，中心必须报错；用同一数据目录拉起后，身份和未确认留言还在。
        assert redis_proc is not None
        os.kill(redis_proc.pid, signal.SIGKILL)
        await redis_proc.wait()
        redis_proc = None
        down = await agent_a.request("hello_whoami")
        suite.check("redis down", down.get("is_error") is True and error_code(down) == "redis_unavailable", error_code(down) or down.get("error", ""))
        redis_proc = await start_redis(args.redis_server, work / "redis", args.redis_port, work / "redis-restart.log")
        pids["redis_restart"] = redis_proc.pid
        who_back = loads(await agent_a.request("hello_whoami"))
        suite.check("identity after redis restart", bool(who_back and who_back.get("agent_id") == agent_ids["A"]))
        # B 的旧进程可能在 Redis 中断时把连接弄坏了，换一个进程使用同一个凭证。
        await agent_b.close()
        agent_b = await start_worker(url, work / "agent-b2.log", authorization=f"Bearer {token_b}")
        workers.append(agent_b)
        recovered = loads(await agent_b.request("hello_mailbox_list"))
        suite.check(
            "mailbox after redis restart",
            any(item.get("text") == "RECOVER-ME" for item in (recovered or {}).get("items") or []),
        )
        hist_back = await agent_b.request("hello_history", {"session_id": s1})
        suite.check("image after redis restart", image_ok(hist_back, png))

        limits = await start_center(
            args.center_port + 1,
            f"redis://127.0.0.1:{args.redis_port}/1",
            secret,
            work / "limits.log",
            extra={
                "HELLO_MAX_SESSIONS": "2",
                "HELLO_MAX_SESSION_MESSAGES": "8",
                "HELLO_MAX_MAILBOX": "1",
                "HELLO_MAX_TEXT_BYTES": "8",
                "HELLO_MAX_IMAGE_BYTES": "64",
                "HELLO_EVENT_LOG_MAX": "2",
                "HELLO_PRESENCE_TTL": "30",
            },
        )
        limit_url = f"http://127.0.0.1:{args.center_port + 1}/mcp"
        lb = await start_worker(limit_url, work / "limit-boot.log", secret=secret)
        workers.append(lb)
        la = loads(await lb.request("hello_register"))
        lb2 = loads(await lb.request("hello_register"))
        lc = loads(await lb.request("hello_register"))
        await lb.close()
        if not (la and lb2 and lc):
            raise RuntimeError("limit registration failed")
        wa = await start_worker(limit_url, work / "limit-a.log", authorization=f"Bearer {la['token']}")
        wb = await start_worker(limit_url, work / "limit-b.log", authorization=f"Bearer {lb2['token']}")
        workers.extend([wa, wb])
        await wa.request("hello_heartbeat")
        await wb.request("hello_heartbeat")
        big_text = await wa.request("hello_call", {"peer_agent_id": lb2["agent_id"], "text": "123456789"})
        suite.check("text limit", big_text.get("is_error") is True and error_code(big_text) == "payload_too_large")
        # 留言上限要在会话数还没被占满时测。C 从未心跳，所以走留言而不是会话。
        full = loads(await wa.request("hello_call", {"peer_agent_id": lc["agent_id"], "text": "box"}))
        overflow = await wa.request("hello_call", {"peer_agent_id": lc["agent_id"], "text": "more"})
        suite.check("mailbox cap", bool(full and full.get("outcome") == "mailbox") and overflow.get("is_error") is True and error_code(overflow) == "mailbox_full", error_code(overflow))
        small_png = make_png(8, 8, (1, 2, 3))
        suite.check("fixture image exceeds tiny limit", len(small_png) > 64)
        opened = loads(await wa.request("hello_call", {"peer_agent_id": lb2["agent_id"], "text": "ok"}))
        too_big = await wa.request(
            "hello_send",
            {
                "session_id": (opened or {}).get("session_id", "s0000000000000000"),
                "image_base64": base64.b64encode(small_png).decode(),
                "image_mime": "image/png",
            },
        )
        suite.check("image limit", too_big.get("is_error") is True and error_code(too_big) == "payload_too_large")
        sid = (opened or {}).get("session_id")
        for n in range(4):
            await wa.request("hello_send", {"session_id": sid, "text": f"e{n}"})
        expired = await wb.request("hello_await", {"after_seq": 1, "timeout_ms": 200})
        suite.check("cursor expired", expired.get("is_error") is True and error_code(expired) == "cursor_expired", error_code(expired))
        second = loads(await wa.request("hello_call", {"peer_agent_id": lb2["agent_id"], "text": "two"}))
        third = await wa.request("hello_call", {"peer_agent_id": lb2["agent_id"], "text": "three"})
        suite.check("session cap", bool(second and second.get("outcome") == "session") and third.get("is_error") is True and error_code(third) == "too_many_sessions", error_code(third))
    finally:
        for worker in workers:
            try:
                await worker.close()
            except Exception:
                pass
        await stop_proc(limits)
        await stop_proc(center)
        await stop_proc(redis_proc)

    summary = {
        "kind": "same-machine-independent-processes",
        "not": ["cross-machine", "claude-code-host", "dsh-host"],
        "pids": pids,
        "agent_ids": agent_ids,
        "health": {key: health.get(key) for key in ("ok", "service", "version", "redis_version", "redis_version_tested")},
        "passed": sum(1 for item in suite.checks if item["ok"]),
        "failed": suite.failed,
        "checks": suite.checks,
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-server", required=True)
    parser.add_argument("--redis-port", type=int, default=56379)
    parser.add_argument("--center-port", type=int, default=8731)
    parser.add_argument("--work", default="")
    parser.add_argument("--report", default=str(ROOT / "docs" / "验证记录.json"))
    args = parser.parse_args()
    if not args.work:
        args.work = str(ROOT / ".redis-data" / "verify")
    summary = asyncio.run(run(args))
    Path(args.report).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(f"RESULT passed={summary['passed']} failed={summary['failed']} redis={summary['health'].get('redis_version')}")
    raise SystemExit(1 if summary["failed"] else 0)


if __name__ == "__main__":
    main()
