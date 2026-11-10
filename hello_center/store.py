"""全部持久状态都在 Redis。进程内存里不保存会话或留言。"""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis

from hello_center.config import MINIMUM_REDIS_VERSION, TESTED_REDIS_VERSIONS
from hello_center.errors import HelloError

_CREATE_SESSION = """
local caller = ARGV[1]
local peer = ARGV[2]
local now = ARGV[3]
local sid = ARGV[4]
local max_sessions = tonumber(ARGV[5])
if redis.call('EXISTS', 'hello:agent:'..peer) == 0 then
  return cjson.encode({ok=false, error='not_found'})
end
if redis.call('SCARD', 'hello:agent_sessions:'..caller) >= max_sessions then
  return cjson.encode({ok=false, error='too_many_sessions'})
end
if redis.call('SCARD', 'hello:agent_sessions:'..peer) >= max_sessions then
  return cjson.encode({ok=false, error='too_many_sessions'})
end
if redis.call('EXISTS', 'hello:presence:'..peer) == 0 then
  return cjson.encode({ok=false, error='offline'})
end
if redis.call('EXISTS', 'hello:session:'..sid) == 1 then
  return cjson.encode({ok=false, error='conflict'})
end
redis.call('HSET', 'hello:session:'..sid,
  'id', sid, 'caller', caller, 'peer', peer, 'status', 'active',
  'created_at', now, 'ended_at', '', 'ended_by', '')
redis.call('SADD', 'hello:agent_sessions:'..caller, sid)
redis.call('SADD', 'hello:agent_sessions:'..peer, sid)
return cjson.encode({ok=true, session_id=sid})
"""

_APPEND_MESSAGE = """
local sid = ARGV[1]
local sender = ARGV[2]
local client_msg_id = ARGV[3]
local text = ARGV[4]
local mime = ARGV[5]
local image = ARGV[6]
local now = ARGV[7]
local max_msgs = tonumber(ARGV[8])
local notify = ARGV[9]
local event_max = tonumber(ARGV[10])
local event_json = ARGV[11]
local mid = ARGV[12]
local sess = 'hello:session:'..sid
local status = redis.call('HGET', sess, 'status')
if not status then return cjson.encode({ok=false, error='not_found'}) end
local caller = redis.call('HGET', sess, 'caller')
local peer = redis.call('HGET', sess, 'peer')
if sender ~= caller and sender ~= peer then
  return cjson.encode({ok=false, error='forbidden'})
end
if status ~= 'active' then return cjson.encode({ok=false, error='session_ended'}) end
if client_msg_id ~= '' then
  local existing = redis.call('GET', 'hello:idem:'..sid..':'..sender..':'..client_msg_id)
  if existing then
    return cjson.encode({ok=true, duplicate=true, message_id=existing})
  end
end
if redis.call('LLEN', 'hello:sessmsgs:'..sid) >= max_msgs then
  return cjson.encode({ok=false, error='session_full'})
end
if redis.call('HSETNX', 'hello:msg:'..mid, 'id', mid) == 0 then
  return cjson.encode({ok=false, error='conflict'})
end
local has_image = '0'
if image ~= '' then has_image = '1' end
redis.call('HSET', 'hello:msg:'..mid,
  'session_id', sid, 'sender', sender, 'text', text, 'mime', mime,
  'has_image', has_image, 'created_at', now, 'client_msg_id', client_msg_id)
if image ~= '' then redis.call('SET', 'hello:msgimg:'..mid, image) end
redis.call('RPUSH', 'hello:sessmsgs:'..sid, mid)
if client_msg_id ~= '' then
  redis.call('SET', 'hello:idem:'..sid..':'..sender..':'..client_msg_id, mid)
end
if notify == '1' then
  local target = peer
  if sender == peer then target = caller end
  local seq = redis.call('INCR', 'hello:seq:event:'..target)
  local ev = cjson.decode(event_json)
  ev.seq = seq
  ev.message_id = mid
  redis.call('RPUSH', 'hello:events:'..target, cjson.encode(ev))
  redis.call('LTRIM', 'hello:events:'..target, -event_max, -1)
  redis.call('LPUSH', 'hello:wake:'..target, '1')
  redis.call('LTRIM', 'hello:wake:'..target, 0, 15)
end
return cjson.encode({ok=true, duplicate=false, message_id=mid})
"""

_EMIT = """
local agent = ARGV[1]
local event_json = ARGV[2]
local event_max = tonumber(ARGV[3])
local seq = redis.call('INCR', 'hello:seq:event:'..agent)
local ev = cjson.decode(event_json)
ev.seq = seq
redis.call('RPUSH', 'hello:events:'..agent, cjson.encode(ev))
redis.call('LTRIM', 'hello:events:'..agent, -event_max, -1)
redis.call('LPUSH', 'hello:wake:'..agent, '1')
redis.call('LTRIM', 'hello:wake:'..agent, 0, 15)
return seq
"""

_HANGUP = """
local sid = ARGV[1]
local actor = ARGV[2]
local now = ARGV[3]
local event_max = tonumber(ARGV[4])
local event_json = ARGV[5]
local sess = 'hello:session:'..sid
local status = redis.call('HGET', sess, 'status')
if not status then return cjson.encode({ok=false, error='not_found'}) end
local caller = redis.call('HGET', sess, 'caller')
local peer = redis.call('HGET', sess, 'peer')
if actor ~= caller and actor ~= peer then
  return cjson.encode({ok=false, error='forbidden'})
end
if status == 'ended' then
  return cjson.encode({ok=true, status='already_ended', session_id=sid})
end
redis.call('HSET', sess, 'status', 'ended', 'ended_at', now, 'ended_by', actor)
local target = peer
if actor == peer then target = caller end
local seq = redis.call('INCR', 'hello:seq:event:'..target)
local ev = cjson.decode(event_json)
ev.seq = seq
redis.call('RPUSH', 'hello:events:'..target, cjson.encode(ev))
redis.call('LTRIM', 'hello:events:'..target, -event_max, -1)
redis.call('LPUSH', 'hello:wake:'..target, '1')
redis.call('LTRIM', 'hello:wake:'..target, 0, 15)
return cjson.encode({ok=true, status='ended', session_id=sid})
"""

_LEAVE_MAIL = """
local recipient = ARGV[1]
local mail_id = ARGV[2]
local sender = ARGV[3]
local text = ARGV[4]
local mime = ARGV[5]
local image = ARGV[6]
local now = ARGV[7]
local max_mailbox = tonumber(ARGV[8])
if redis.call('EXISTS', 'hello:mail:'..mail_id) == 1 then
  return cjson.encode({ok=false, error='conflict'})
end
if redis.call('LLEN', 'hello:mailbox:'..recipient) >= max_mailbox then
  return cjson.encode({ok=false, error='mailbox_full'})
end
local has_image = '0'
if image ~= '' then has_image = '1' end
redis.call('HSET', 'hello:mail:'..mail_id,
  'id', mail_id, 'to', recipient, 'from', sender, 'text', text, 'mime', mime,
  'has_image', has_image, 'created_at', now, 'acked', '0')
if image ~= '' then redis.call('SET', 'hello:mailimg:'..mail_id, image) end
redis.call('RPUSH', 'hello:mailbox:'..recipient, mail_id)
return cjson.encode({ok=true, mailbox_id=mail_id})
"""

_ACK_MAIL = """
local agent = ARGV[1]
local mail_id = ARGV[2]
local mail = 'hello:mail:'..mail_id
if redis.call('EXISTS', mail) == 0 then
  return cjson.encode({ok=false, error='not_found'})
end
local owner = redis.call('HGET', mail, 'to')
if owner ~= agent then
  return cjson.encode({ok=false, error='forbidden'})
end
if redis.call('HGET', mail, 'acked') == '1' then
  return cjson.encode({ok=true, status='already_acked', mailbox_id=mail_id})
end
redis.call('HSET', mail, 'acked', '1')
redis.call('LREM', 'hello:mailbox:'..agent, 0, mail_id)
return cjson.encode({ok=true, status='acked', mailbox_id=mail_id})
"""


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _pairs(raw: Any) -> dict[str, str]:
    # RESP2 的 HGETALL 在 redis-py 里是字典；少数路径会给扁平列表。两种都接受。
    if isinstance(raw, dict):
        return {_text(key): _text(value) for key, value in raw.items()}
    out: dict[str, str] = {}
    items = list(raw)
    for index in range(0, len(items), 2):
        out[_text(items[index])] = _text(items[index + 1])
    return out


def parse_redis_version(raw: str) -> tuple[int, int, int]:
    head = raw.split("-", 1)[0]
    parts = head.split(".")
    nums = [int(part) for part in parts[:3]]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


class Store:
    def __init__(self, redis: Redis, event_log_max: int) -> None:
        self.r = redis
        self.event_log_max = event_log_max

    async def prepare(self) -> tuple[str, bool]:
        info = await self.r.info("server")
        version = info.get("redis_version") or info.get(b"redis_version")
        version = _text(version)
        if not version:
            raise HelloError("redis_unavailable", "Redis 没有返回版本号")
        parsed = parse_redis_version(version)
        if parsed < MINIMUM_REDIS_VERSION:
            raise HelloError(
                "redis_too_old",
                f"Redis {version} 低于已实测下限 {'.'.join(map(str, MINIMUM_REDIS_VERSION))}，拒绝启动",
            )
        # 只保证序号从 80000000 起跳一次。已有数据时不会重置。
        await self.r.set("hello:seq:agent", 80000000, nx=True)
        tested = version in TESTED_REDIS_VERSIONS
        return version, tested

    async def _eval(self, script: str, *args: str | bytes) -> dict[str, Any]:
        raw = await self.r.eval(script, 0, *args)
        return json.loads(_text(raw))

    async def register_agent(self, agent_id: str, token_hash: str, display_name: str, created_at: str) -> None:
        # 哈希碰撞时让调用方换 token。SET NX 保证一个哈希只对应一个 Agent。
        placed = await self.r.set(f"hello:token:{token_hash}", agent_id, nx=True)
        if not placed:
            raise HelloError("conflict", "凭证冲突，请重试登记")
        await self.r.hset(
            f"hello:agent:{agent_id}",
            mapping={
                "id": agent_id,
                "token_hash": token_hash,
                "display_name": display_name,
                "created_at": created_at,
            },
        )

    async def next_agent_number(self) -> int:
        return int(await self.r.incr("hello:seq:agent"))

    async def agent_id_for_token(self, token_hash: str) -> str | None:
        raw = await self.r.get(f"hello:token:{token_hash}")
        if raw is None:
            return None
        return _text(raw)

    async def agent(self, agent_id: str) -> dict[str, str] | None:
        raw = await self.r.hgetall(f"hello:agent:{agent_id}")
        if not raw:
            return None
        return _pairs(raw)

    async def set_presence(self, agent_id: str, timestamp: str, ttl: int) -> None:
        await self.r.set(f"hello:presence:{agent_id}", timestamp, ex=ttl)

    async def clear_presence(self, agent_id: str) -> None:
        await self.r.delete(f"hello:presence:{agent_id}")

    async def presence_ttl(self, agent_id: str) -> int:
        ttl = int(await self.r.ttl(f"hello:presence:{agent_id}"))
        return ttl

    async def is_online(self, agent_id: str) -> bool:
        return bool(await self.r.exists(f"hello:presence:{agent_id}"))

    async def create_session(
        self, caller: str, peer: str, now: str, session_id: str, max_sessions: int
    ) -> dict[str, Any]:
        return await self._eval(_CREATE_SESSION, caller, peer, now, session_id, str(max_sessions))

    async def append_message(
        self,
        session_id: str,
        sender: str,
        client_msg_id: str,
        text: str,
        mime: str,
        image: bytes,
        now: str,
        max_messages: int,
        notify: bool,
        message_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._eval(
            _APPEND_MESSAGE,
            session_id,
            sender,
            client_msg_id,
            text,
            mime,
            image,
            now,
            str(max_messages),
            "1" if notify else "0",
            str(self.event_log_max),
            json.dumps(event, ensure_ascii=True),
            message_id,
        )

    async def emit(self, agent_id: str, event: dict[str, Any]) -> int:
        seq = await self.r.eval(
            _EMIT,
            0,
            agent_id,
            json.dumps(event, ensure_ascii=True),
            str(self.event_log_max),
        )
        return int(seq)

    async def hangup(self, session_id: str, actor: str, now: str, event: dict[str, Any]) -> dict[str, Any]:
        return await self._eval(
            _HANGUP,
            session_id,
            actor,
            now,
            str(self.event_log_max),
            json.dumps(event, ensure_ascii=True),
        )

    async def leave_mail(
        self,
        recipient: str,
        mail_id: str,
        sender: str,
        text: str,
        mime: str,
        image: bytes,
        now: str,
        max_mailbox: int,
    ) -> dict[str, Any]:
        return await self._eval(
            _LEAVE_MAIL,
            recipient,
            mail_id,
            sender,
            text,
            mime,
            image,
            now,
            str(max_mailbox),
        )

    async def ack_mail(self, agent_id: str, mail_id: str) -> dict[str, Any]:
        return await self._eval(_ACK_MAIL, agent_id, mail_id)

    def _call_idem_key(self, sender: str, client_msg_id: str) -> str:
        return f"hello:callidem:{sender}:{client_msg_id}"

    async def claim_call_idem(self, sender: str, client_msg_id: str) -> bool:
        placed = await self.r.set(self._call_idem_key(sender, client_msg_id), "pending", nx=True)
        return bool(placed)

    async def read_call_idem(self, sender: str, client_msg_id: str) -> str | None:
        raw = await self.r.get(self._call_idem_key(sender, client_msg_id))
        if raw is None:
            return None
        return _text(raw)

    async def finish_call_idem(self, sender: str, client_msg_id: str, payload: str) -> None:
        await self.r.set(self._call_idem_key(sender, client_msg_id), payload)

    async def release_call_idem(self, sender: str, client_msg_id: str) -> None:
        key = self._call_idem_key(sender, client_msg_id)
        current = await self.r.get(key)
        if _text(current) == "pending":
            await self.r.delete(key)

    async def wait_wake(self, agent_id: str, seconds: int) -> None:
        # Redis 5 的 BLPOP 超时只能是整数秒。0 表示永远阻塞，这里禁止。
        await self.r.blpop(f"hello:wake:{agent_id}", timeout=max(1, int(seconds)))

    async def events(self, agent_id: str) -> list[dict[str, Any]]:
        raw_items = await self.r.lrange(f"hello:events:{agent_id}", 0, -1)
        return [json.loads(_text(item)) for item in raw_items]

    async def session(self, session_id: str) -> dict[str, str] | None:
        raw = await self.r.hgetall(f"hello:session:{session_id}")
        if not raw:
            return None
        return _pairs(raw)

    async def session_ids(self, agent_id: str) -> list[str]:
        raw = await self.r.smembers(f"hello:agent_sessions:{agent_id}")
        return sorted(_text(item) for item in raw)

    async def message_ids(self, session_id: str) -> list[str]:
        raw = await self.r.lrange(f"hello:sessmsgs:{session_id}", 0, -1)
        return [_text(item) for item in raw]

    async def message(self, message_id: str) -> dict[str, str] | None:
        raw = await self.r.hgetall(f"hello:msg:{message_id}")
        if not raw:
            return None
        return _pairs(raw)

    async def message_image(self, message_id: str) -> bytes | None:
        return await self.r.get(f"hello:msgimg:{message_id}")

    async def mailbox_ids(self, agent_id: str) -> list[str]:
        raw = await self.r.lrange(f"hello:mailbox:{agent_id}", 0, -1)
        return [_text(item) for item in raw]

    async def mail(self, mail_id: str) -> dict[str, str] | None:
        raw = await self.r.hgetall(f"hello:mail:{mail_id}")
        if not raw:
            return None
        return _pairs(raw)

    async def mail_image(self, mail_id: str) -> bytes | None:
        return await self.r.get(f"hello:mailimg:{mail_id}")
