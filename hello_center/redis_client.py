"""Redis 连接。协议固定为 RESP2，避免对 Redis 6 的 HELLO 命令产生依赖。"""

from __future__ import annotations

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry


def connect(url: str) -> Redis:
    # driver_info=None：不要发 CLIENT SETINFO（那是 Redis 7.2 才有的子命令）。
    # protocol=2：不要发 HELLO（那是 Redis 6 才有的命令）。
    # 重试设为 0，Redis 断开时马上返回错误，而不是在客户端里假装还连着。
    return Redis.from_url(
        url,
        protocol=2,
        driver_info=None,
        decode_responses=False,
        socket_timeout=15,
        socket_connect_timeout=2,
        health_check_interval=0,
        retry=Retry(NoBackoff(), 0),
        retry_on_error=[],
    )
