"""通话、在线和留言的规则。这里不执行消息里的任何内容。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any

from redis.asyncio import Redis

from hello_center.config import Settings
from hello_center.errors import HelloError
from hello_center.images import decode_image
from hello_center.store import Store

_AGENT_ID = re.compile(r"^(?:(\d{2})-(\d{3})-(\d{3})|(\d{8}))$")
_CLIENT_MSG = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_SESSION_ID = re.compile(r"^s[0-9a-f]{16}$")
_MAIL_ID = re.compile(r"^m[0-9a-f]{16}$")


def canonical_agent_id(raw: str) -> str:
    match = _AGENT_ID.match(raw.strip())
    if not match:
        raise HelloError("bad_agent_id", "Agent ID 要像电话号码，是 8 位数字，例如 80-000-001")
    digits = match.group(4) or (match.group(1) + match.group(2) + match.group(3))
    return f"{digits[0:2]}-{digits[2:5]}-{digits[5:8]}"


def format_agent_id(number: int) -> str:
    digits = f"{number:08d}"
    if len(digits) != 8:
        raise HelloError("agent_id_space_exhausted", "Agent ID 号码空间已用完")
    return f"{digits[0:2]}-{digits[2:5]}-{digits[5:8]}"


def _now() -> str:
    return str(int(time.time()))


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(8)


def _check_client_msg_id(value: str | None) -> str:
    if value is None or value == "":
        return ""
    if not _CLIENT_MSG.match(value):
        raise HelloError("bad_client_msg_id", "client_msg_id 只能是 1 到 64 位的字母、数字、点、下划线、冒号或连字符")
    return value


def _check_session_id(value: str) -> str:
    if not _SESSION_ID.match(value):
        raise HelloError("bad_session_id", "会话编号不正确")
    return value


def _check_mail_id(value: str) -> str:
    if not _MAIL_ID.match(value):
        raise HelloError("bad_mailbox_id", "留言编号不正确")
    return value


class HelloService:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(redis, settings.event_log_max)

    async def prepare(self) -> tuple[str, bool]:
        return await self.store.prepare()

    def register_secret_ok(self, presented: str) -> bool:
        expected = self.settings.register_secret
        if len(presented) != len(expected):
            return False
        return hmac.compare_digest(presented, expected)

    async def register(self, display_name: str | None) -> dict[str, Any]:
        name = (display_name or "").strip()
        if len(name) > 40 or any(ord(ch) < 32 for ch in name):
            raise HelloError("bad_display_name", "显示名最多 40 个字符，且不能包含控制字符")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        number = await self.store.next_agent_number()
        agent_id = format_agent_id(number)
        created = _now()
        await self.store.register_agent(agent_id, digest, name, created)
        # 登记只颁发长期身份，不把 Agent 标成在线。进程退出后不该还显示可呼叫。
        return {
            "ok": True,
            "agent_id": agent_id,
            "token": token,
            "display_name": name,
            "online": False,
        }

    async def agent_for_token(self, token: str) -> str | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return await self.store.agent_id_for_token(digest)

    async def whoami(self, agent_id: str) -> dict[str, Any]:
        agent = await self.store.agent(agent_id)
        if agent is None:
            raise HelloError("unauthenticated", "凭证无效")
        ttl = await self.store.presence_ttl(agent_id)
        online = ttl >= 0
        return {
            "ok": True,
            "agent_id": agent_id,
            "display_name": agent.get("display_name", ""),
            "online": online,
            "presence_ttl_seconds": ttl if online else None,
        }

    async def heartbeat(self, agent_id: str) -> dict[str, Any]:
        await self.store.set_presence(agent_id, _now(), self.settings.presence_ttl)
        ttl = await self.store.presence_ttl(agent_id)
        return {"ok": True, "agent_id": agent_id, "online": True, "presence_ttl_seconds": ttl}

    async def offline(self, agent_id: str) -> dict[str, Any]:
        await self.store.clear_presence(agent_id)
        return {
            "ok": True,
            "agent_id": agent_id,
            "online": False,
            "sessions_unchanged": True,
        }

    async def lookup(self, agent_id: str, peer_raw: str) -> dict[str, Any]:
        del agent_id
        peer = canonical_agent_id(peer_raw)
        exists = await self.store.agent(peer) is not None
        online = await self.store.is_online(peer) if exists else False
        return {"ok": True, "agent_id": peer, "registered": exists, "online": online}

    def _content(
        self,
        text: str | None,
        image_base64: str | None,
        image_mime: str | None,
        *,
        allow_empty: bool,
    ) -> tuple[str, bytes, str]:
        body = "" if text is None else text
        if "\x00" in body:
            raise HelloError("bad_text", "文字不能包含空字节")
        encoded = body.encode("utf-8")
        if len(encoded) > self.settings.max_text_bytes:
            raise HelloError("payload_too_large", f"文字超过 {self.settings.max_text_bytes} 字节")
        image = decode_image(image_base64, image_mime, self.settings.max_image_bytes)
        if not allow_empty and body == "" and image is None:
            raise HelloError("bad_request", "文字和图片至少要有一个")
        if image is None:
            return body, b"", ""
        return body, image[0], image[1]

    async def _touch(self, agent_id: str, ttl: int | None = None) -> None:
        await self.store.set_presence(agent_id, _now(), ttl or self.settings.presence_ttl)

    async def call(
        self,
        agent_id: str,
        peer_raw: str,
        text: str | None,
        image_base64: str | None,
        image_mime: str | None,
        client_msg_id: str | None,
    ) -> dict[str, Any]:
        peer = canonical_agent_id(peer_raw)
        if peer == agent_id:
            raise HelloError("bad_request", "不能呼叫自己")
        body, image, mime = self._content(text, image_base64, image_mime, allow_empty=True)
        msg_key = _check_client_msg_id(client_msg_id)
        if msg_key:
            if not await self.store.claim_call_idem(agent_id, msg_key):
                cached = await self.store.read_call_idem(agent_id, msg_key)
                if not cached or cached == "pending":
                    raise HelloError("conflict", "相同 client_msg_id 的呼叫还在进行，请稍后重试")
                previous = json.loads(cached)
                previous["duplicate"] = True
                await self._touch(agent_id)
                return previous
        try:
            outcome = await self._place_call(agent_id, peer, body, image, mime, msg_key)
        except Exception:
            if msg_key:
                await self.store.release_call_idem(agent_id, msg_key)
            raise
        outcome["duplicate"] = False
        if msg_key:
            saved = dict(outcome)
            saved.pop("duplicate", None)
            await self.store.finish_call_idem(agent_id, msg_key, json.dumps(saved, ensure_ascii=True))
        return outcome

    async def _place_call(
        self, agent_id: str, peer: str, body: str, image: bytes, mime: str, msg_key: str
    ) -> dict[str, Any]:
        created = _now()
        result: dict[str, Any] | None = None
        session_id = ""
        for _ in range(3):
            session_id = _new_id("s")
            result = await self.store.create_session(
                agent_id, peer, created, session_id, self.settings.max_sessions
            )
            if result.get("error") != "conflict":
                break
        if result is None or not result.get("ok"):
            error = (result or {}).get("error")
            if error == "offline":
                return await self._leave_mail(agent_id, peer, body, image, mime)
            if error == "not_found":
                raise HelloError("not_found", "没有这个 Agent ID")
            if error == "too_many_sessions":
                raise HelloError("too_many_sessions", "会话数量已达上限。已挂断的会话也会计入，直到上限")
            raise HelloError("conflict", "创建会话失败，请重试")

        message_id = ""
        if body or image:
            message_id = await self._append(
                session_id, agent_id, msg_key, body, mime, image, created, notify=False
            )
        await self.store.emit(
            peer,
            {
                "type": "session",
                "session_id": session_id,
                "peer": agent_id,
                "sender": agent_id,
                "text": body,
                "has_image": bool(image),
                "message_id": message_id,
                "created_at": created,
            },
        )
        await self._touch(agent_id)
        return {
            "ok": True,
            "outcome": "session",
            "session_id": session_id,
            "peer": peer,
            "message_id": message_id or None,
        }

    async def _leave_mail(
        self, sender: str, recipient: str, body: str, image: bytes, mime: str
    ) -> dict[str, Any]:
        if body == "" and not image:
            raise HelloError(
                "peer_offline",
                "对方不在线。要留言必须带文字或图片。留言不会建立会话，对方以后读留言也不会自动回拨。",
            )
        created = _now()
        result: dict[str, Any] | None = None
        mail_id = ""
        for _ in range(3):
            mail_id = _new_id("m")
            result = await self.store.leave_mail(
                recipient,
                mail_id,
                sender,
                body,
                mime,
                image,
                created,
                self.settings.max_mailbox,
            )
            if result.get("error") != "conflict":
                break
        if result is None or not result.get("ok"):
            if (result or {}).get("error") == "mailbox_full":
                raise HelloError("mailbox_full", "对方的留言箱已满，这条留言没有收下")
            raise HelloError("conflict", "留言没有写成，请重试")
        await self._touch(sender)
        return {
            "ok": True,
            "outcome": "mailbox",
            "mailbox_id": mail_id,
            "peer": recipient,
            "session_id": None,
        }

    async def _append(
        self,
        session_id: str,
        sender: str,
        client_msg_id: str,
        body: str,
        mime: str,
        image: bytes,
        created: str,
        *,
        notify: bool,
    ) -> str:
        event = {
            "type": "message",
            "session_id": session_id,
            "sender": sender,
            "text": body,
            "has_image": bool(image),
            "created_at": created,
        }
        result: dict[str, Any] | None = None
        for _ in range(3):
            message_id = _new_id("g")
            result = await self.store.append_message(
                session_id,
                sender,
                client_msg_id,
                body,
                mime,
                image,
                created,
                self.settings.max_session_messages,
                notify,
                message_id,
                event,
            )
            if result.get("error") != "conflict":
                break
        if result is None or not result.get("ok"):
            error = (result or {}).get("error")
            if error == "not_found":
                raise HelloError("not_found", "会话不存在")
            if error == "forbidden":
                raise HelloError("forbidden", "你不是这个会话的参与者")
            if error == "session_ended":
                raise HelloError("session_ended", "这个会话已经挂断")
            if error == "session_full":
                raise HelloError("session_full", "这个会话的消息数量已达上限")
            raise HelloError("conflict", "消息没有写成，请重试")
        return str(result["message_id"])

    async def send(
        self,
        agent_id: str,
        session_id: str,
        text: str | None,
        image_base64: str | None,
        image_mime: str | None,
        client_msg_id: str | None,
    ) -> dict[str, Any]:
        """发送，并告诉调用方这次是不是同一次 client_msg_id 的重试。"""
        sid = _check_session_id(session_id)
        body, image, mime = self._content(text, image_base64, image_mime, allow_empty=False)
        msg_key = _check_client_msg_id(client_msg_id)
        created = _now()
        event = {
            "type": "message",
            "session_id": sid,
            "sender": agent_id,
            "text": body,
            "has_image": bool(image),
            "created_at": created,
        }
        result: dict[str, Any] | None = None
        for _ in range(3):
            message_id = _new_id("g")
            result = await self.store.append_message(
                sid,
                agent_id,
                msg_key,
                body,
                mime,
                image,
                created,
                self.settings.max_session_messages,
                True,
                message_id,
                event,
            )
            if result.get("duplicate") or result.get("error") != "conflict":
                break
        if result is None or not result.get("ok"):
            error = (result or {}).get("error")
            if error == "not_found":
                raise HelloError("not_found", "会话不存在")
            if error == "forbidden":
                raise HelloError("forbidden", "你不是这个会话的参与者")
            if error == "session_ended":
                raise HelloError("session_ended", "这个会话已经挂断")
            if error == "session_full":
                raise HelloError("session_full", "这个会话的消息数量已达上限")
            raise HelloError("conflict", "消息没有写成，请重试")
        await self._touch(agent_id)
        return {
            "ok": True,
            "session_id": sid,
            "message_id": result["message_id"],
            "duplicate": bool(result.get("duplicate")),
        }

    async def hangup(self, agent_id: str, session_id: str) -> dict[str, Any]:
        sid = _check_session_id(session_id)
        created = _now()
        result = await self.store.hangup(
            sid,
            agent_id,
            created,
            {"type": "hangup", "session_id": sid, "by": agent_id, "created_at": created},
        )
        if not result.get("ok"):
            if result.get("error") == "not_found":
                raise HelloError("not_found", "会话不存在")
            if result.get("error") == "forbidden":
                raise HelloError("forbidden", "你不是这个会话的参与者")
            raise HelloError("conflict", "挂断失败")
        # 挂断不改变在线状态，也不碰其他会话。
        ttl = await self.store.presence_ttl(agent_id)
        return {
            "ok": True,
            "session_id": sid,
            "status": result["status"],
            "online": ttl >= 0,
        }

    async def sessions(self, agent_id: str) -> dict[str, Any]:
        items = []
        for sid in await self.store.session_ids(agent_id):
            data = await self.store.session(sid)
            if data is None:
                continue
            if agent_id not in (data.get("caller"), data.get("peer")):
                continue
            peer = data["peer"] if data["caller"] == agent_id else data["caller"]
            items.append(
                {
                    "session_id": sid,
                    "peer": peer,
                    "role": "caller" if data["caller"] == agent_id else "callee",
                    "status": data.get("status", ""),
                    "created_at": data.get("created_at", ""),
                    "ended_at": data.get("ended_at", ""),
                    "ended_by": data.get("ended_by", ""),
                }
            )
        return {"ok": True, "sessions": items}

    async def await_events(self, agent_id: str, after_seq: int, timeout_ms: int | None) -> tuple[dict[str, Any], list[tuple[bytes, str]]]:
        if after_seq < 0:
            raise HelloError("bad_cursor", "after_seq 不能是负数")
        requested = self.settings.await_default_ms if timeout_ms is None else timeout_ms
        if requested < 0:
            raise HelloError("bad_timeout", "timeout_ms 不能是负数")
        timeout_ms = min(requested, self.settings.await_max_ms)
        # 等待期间视为在线。时长要盖住这次等待，结束后恢复成普通 TTL。
        wait_ttl = max(self.settings.presence_ttl, int(timeout_ms / 1000) + 5)
        await self._touch(agent_id, wait_ttl)
        deadline = time.monotonic() + (timeout_ms / 1000)
        try:
            while True:
                events, expired, oldest = await self._read_events(agent_id, after_seq)
                if expired:
                    raise HelloError(
                        "cursor_expired",
                        f"after_seq 已经超出保留范围。改查 hello_sessions 和 hello_history。现在最早的序号是 {oldest}。",
                    )
                if events:
                    payload, images = await self._pack_events(agent_id, events, after_seq, timed_out=False)
                    return payload, images
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return (
                        {"ok": True, "events": [], "next_seq": after_seq, "timed_out": timeout_ms > 0, "image_refs": []},
                        [],
                    )
                if remain >= 1:
                    await self.store.wait_wake(agent_id, 1)
                else:
                    await asyncio.sleep(min(0.05, remain))
        finally:
            await self._touch(agent_id, self.settings.presence_ttl)

    async def _read_events(self, agent_id: str, after_seq: int) -> tuple[list[dict[str, Any]], bool, int]:
        stored = await self.store.events(agent_id)
        if not stored:
            return [], False, 0
        oldest = int(stored[0]["seq"])
        if after_seq > 0 and after_seq < oldest - 1:
            return [], True, oldest
        chosen = [item for item in stored if int(item["seq"]) > after_seq][: self.settings.await_batch]
        return chosen, False, oldest

    async def _pack_events(
        self, agent_id: str, events: list[dict[str, Any]], after_seq: int, timed_out: bool
    ) -> tuple[dict[str, Any], list[tuple[bytes, str]]]:
        images: list[tuple[bytes, str]] = []
        refs: list[dict[str, str]] = []
        public: list[dict[str, Any]] = []
        for event in events:
            item = {
                "seq": int(event["seq"]),
                "type": event.get("type", ""),
                "session_id": event.get("session_id", ""),
                "peer": event.get("peer", ""),
                "sender": event.get("sender", ""),
                "by": event.get("by", ""),
                "text": event.get("text", ""),
                "has_image": bool(event.get("has_image")),
                "message_id": event.get("message_id", ""),
                "created_at": event.get("created_at", ""),
            }
            public.append(item)
            if item["has_image"] and item["message_id"]:
                blob, mime = await self._session_image(agent_id, item["message_id"])
                if blob is not None and mime:
                    images.append((blob, mime))
                    refs.append({"message_id": item["message_id"], "mime_type": mime})
        next_seq = int(events[-1]["seq"]) if events else after_seq
        return (
            {"ok": True, "events": public, "next_seq": next_seq, "timed_out": timed_out, "image_refs": refs},
            images,
        )

    async def _require_participant(self, agent_id: str, session_id: str) -> dict[str, str]:
        data = await self.store.session(session_id)
        if data is None:
            raise HelloError("not_found", "会话不存在")
        if agent_id not in (data.get("caller"), data.get("peer")):
            raise HelloError("forbidden", "你不是这个会话的参与者")
        return data

    async def _session_image(self, agent_id: str, message_id: str) -> tuple[bytes | None, str]:
        message = await self.store.message(message_id)
        if message is None:
            return None, ""
        await self._require_participant(agent_id, message["session_id"])
        if message.get("has_image") != "1":
            return None, ""
        return await self.store.message_image(message_id), message.get("mime", "")

    async def history(
        self, agent_id: str, session_id: str, after_message_id: str | None, limit: int | None
    ) -> tuple[dict[str, Any], list[tuple[bytes, str]]]:
        sid = _check_session_id(session_id)
        await self._require_participant(agent_id, sid)
        size = self.settings.history_default if limit is None else limit
        if size < 1 or size > self.settings.history_max:
            raise HelloError("bad_limit", f"limit 必须在 1 和 {self.settings.history_max} 之间")
        ids = await self.store.message_ids(sid)
        if after_message_id:
            if after_message_id not in ids:
                raise HelloError("not_found", "这个会话里没有该消息编号")
            ids = ids[ids.index(after_message_id) + 1 :]
        ids = ids[:size]
        messages = []
        images: list[tuple[bytes, str]] = []
        refs: list[dict[str, str]] = []
        for mid in ids:
            message = await self.store.message(mid)
            if message is None or message.get("session_id") != sid:
                continue
            has_image = message.get("has_image") == "1"
            messages.append(
                {
                    "message_id": mid,
                    "sender": message.get("sender", ""),
                    "text": message.get("text", ""),
                    "has_image": has_image,
                    "mime_type": message.get("mime", ""),
                    "created_at": message.get("created_at", ""),
                    "client_msg_id": message.get("client_msg_id", ""),
                }
            )
            if has_image:
                blob = await self.store.message_image(mid)
                if blob is not None:
                    images.append((blob, message.get("mime", "")))
                    refs.append({"message_id": mid, "mime_type": message.get("mime", "")})
        return {"ok": True, "session_id": sid, "messages": messages, "image_refs": refs}, images

    async def mailbox_list(self, agent_id: str) -> dict[str, Any]:
        items = []
        for mail_id in await self.store.mailbox_ids(agent_id):
            mail = await self.store.mail(mail_id)
            if mail is None or mail.get("to") != agent_id or mail.get("acked") == "1":
                continue
            items.append(
                {
                    "mailbox_id": mail_id,
                    "sender": mail.get("from", ""),
                    "text": mail.get("text", ""),
                    "has_image": mail.get("has_image") == "1",
                    "mime_type": mail.get("mime", ""),
                    "created_at": mail.get("created_at", ""),
                }
            )
        return {"ok": True, "items": items, "started_session": False}

    async def mailbox_read(self, agent_id: str, mail_id: str) -> tuple[dict[str, Any], list[tuple[bytes, str]]]:
        mid = _check_mail_id(mail_id)
        mail = await self.store.mail(mid)
        if mail is None:
            raise HelloError("not_found", "没有这条留言")
        if mail.get("to") != agent_id:
            raise HelloError("forbidden", "这不是你的留言")
        images: list[tuple[bytes, str]] = []
        refs: list[dict[str, str]] = []
        if mail.get("has_image") == "1":
            blob = await self.store.mail_image(mid)
            if blob is not None:
                images.append((blob, mail.get("mime", "")))
                refs.append({"mailbox_id": mid, "mime_type": mail.get("mime", "")})
        payload = {
            "ok": True,
            "mailbox_id": mid,
            "sender": mail.get("from", ""),
            "text": mail.get("text", ""),
            "has_image": mail.get("has_image") == "1",
            "mime_type": mail.get("mime", ""),
            "created_at": mail.get("created_at", ""),
            "acked": mail.get("acked") == "1",
            "started_session": False,
        }
        return payload, images

    async def mailbox_ack(self, agent_id: str, mail_ids: list[str]) -> dict[str, Any]:
        if not mail_ids:
            raise HelloError("bad_request", "要给出留言编号")
        if len(mail_ids) > 50:
            raise HelloError("bad_request", "一次最多确认 50 条留言")
        results = []
        for mail_id in mail_ids:
            mid = _check_mail_id(mail_id)
            result = await self.store.ack_mail(agent_id, mid)
            if not result.get("ok"):
                results.append({"mailbox_id": mid, "ok": False, "error": result.get("error", "conflict")})
                continue
            results.append({"mailbox_id": mid, "ok": True, "status": result.get("status")})
        return {"ok": True, "results": results, "started_session": False}
