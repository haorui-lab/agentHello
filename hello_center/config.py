"""运行配置。状态不写在这里，也不写本地数据库。"""

from __future__ import annotations

import os
from dataclasses import dataclass

from hello_center.errors import HelloError

# 这些版本在本仓库的 Linux 上用官方源码编译后跑过完整验证。
# 不表示未列出的版本可用。下限以下直接拒绝启动。
TESTED_REDIS_VERSIONS = ("5.0.14", "6.2.18", "7.2.11")
MINIMUM_REDIS_VERSION = (5, 0, 14)

# 实际发往 Redis 的命令。刻意不用 Streams、HELLO、FUNCTION、JSON、HEXPIRE、ACL。
REDIS_COMMANDS = (
    "PING",
    "INFO",
    "AUTH",
    "SET",
    "GET",
    "DEL",
    "EXISTS",
    "INCR",
    "EXPIRE",
    "TTL",
    "HSET",
    "HSETNX",
    "HGET",
    "HGETALL",
    "LPUSH",
    "RPUSH",
    "LTRIM",
    "LRANGE",
    "LLEN",
    "LREM",
    "SADD",
    "SMEMBERS",
    "SCARD",
    "EVAL",
    "BLPOP",
)


def _int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise HelloError("bad_config", f"{name} 必须是整数") from exc
    if value < low or value > high:
        raise HelloError("bad_config", f"{name} 必须在 {low} 和 {high} 之间")
    return value


@dataclass(frozen=True)
class Settings:
    redis_url: str
    bind: str
    port: int
    register_secret: str
    presence_ttl: int = 45
    await_default_ms: int = 20_000
    await_max_ms: int = 50_000
    max_image_bytes: int = 512 * 1024
    max_text_bytes: int = 16 * 1024
    max_session_messages: int = 500
    max_mailbox: int = 100
    max_sessions: int = 200
    event_log_max: int = 500
    history_default: int = 20
    history_max: int = 50
    await_batch: int = 4
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> Settings:
        secret = os.environ.get("HELLO_REGISTER_SECRET", "")
        if len(secret) < 8:
            raise HelloError(
                "bad_config",
                "没有 HELLO_REGISTER_SECRET，或短于 8 个字符。写进 config/hello-center.env 再启动。中心没有开始监听。",
            )
        hosts = tuple(h for h in os.environ.get("HELLO_ALLOWED_HOSTS", "").split(",") if h)
        origins = tuple(h for h in os.environ.get("HELLO_ALLOWED_ORIGINS", "").split(",") if h)
        return cls(
            redis_url=os.environ.get("HELLO_REDIS_URL", "redis://127.0.0.1:6379/0"),
            bind=os.environ.get("HELLO_BIND", "127.0.0.1"),
            port=_int("HELLO_PORT", 8731, 1, 65535),
            register_secret=secret,
            presence_ttl=_int("HELLO_PRESENCE_TTL", 45, 2, 300),
            await_default_ms=_int("HELLO_AWAIT_DEFAULT_MS", 20_000, 0, 50_000),
            await_max_ms=_int("HELLO_AWAIT_MAX_MS", 50_000, 0, 55_000),
            max_image_bytes=_int("HELLO_MAX_IMAGE_BYTES", 512 * 1024, 32, 512 * 1024),
            max_text_bytes=_int("HELLO_MAX_TEXT_BYTES", 16 * 1024, 1, 16 * 1024),
            max_session_messages=_int("HELLO_MAX_SESSION_MESSAGES", 500, 1, 5000),
            max_mailbox=_int("HELLO_MAX_MAILBOX", 100, 1, 1000),
            max_sessions=_int("HELLO_MAX_SESSIONS", 200, 1, 2000),
            event_log_max=_int("HELLO_EVENT_LOG_MAX", 500, 2, 5000),
            allowed_hosts=hosts,
            allowed_origins=origins,
        )
