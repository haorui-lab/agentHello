"""启动 Hello Center。配置或 Redis 不满足条件时退出码是 2，并且还没有开始监听。"""

from __future__ import annotations

import asyncio
import logging
import sys

from mcp.server.transport_security import TransportSecuritySettings
from redis.exceptions import BusyLoadingError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from hello_center import __version__
from hello_center.config import TESTED_REDIS_VERSIONS, Settings
from hello_center.errors import HelloError
from hello_center.mcp_server import build_server
from hello_center.redis_client import connect
from hello_center.service import HelloService

log = logging.getLogger("hello_center")


async def _check_redis(settings: Settings) -> tuple[str, bool]:
    redis = connect(settings.redis_url)
    try:
        return await HelloService(redis, settings).prepare()
    finally:
        await redis.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = Settings.from_env()
    except HelloError as exc:
        print(f"hello-center: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from exc

    try:
        version, tested = asyncio.run(_check_redis(settings))
    except HelloError as exc:
        print(f"hello-center: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from exc
    except (RedisConnectionError, RedisTimeoutError, BusyLoadingError, OSError) as exc:
        print("hello-center: 连不上 Redis，没有开始监听。", file=sys.stderr)
        raise SystemExit(2) from exc

    if tested:
        log.info("redis %s 在已实测列表 %s 中", version, ",".join(TESTED_REDIS_VERSIONS))
    else:
        log.warning(
            "redis %s 不在已实测列表 %s 中。进程可以启动，这不是兼容性结论。",
            version,
            ",".join(TESTED_REDIS_VERSIONS),
        )

    if settings.bind not in {"127.0.0.1", "localhost", "::1"} and not settings.allowed_hosts:
        log.warning("绑定地址不是回环，且没有 HELLO_ALLOWED_HOSTS。DNS 重绑定检查关着。不要把这个端口暴露到公网。")

    kwargs: dict = {
        "host": settings.bind,
        "port": settings.port,
        "json_response": True,
        "stateless_http": True,
        "max_request_body_size": 2 * 1024 * 1024,
    }
    if settings.allowed_hosts:
        kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=list(settings.allowed_origins),
        )

    log.info("hello-center %s 监听 http://%s:%s/mcp", __version__, settings.bind, settings.port)
    try:
        build_server(settings).run("streamable-http", **kwargs)
    except HelloError as exc:
        print(f"hello-center: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
