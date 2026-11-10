#!/bin/sh
# 最小可重复验收：本机 Redis + Hello Center + 两个 MCP 进程。
# 不打印登记口令或 Bearer。不是跨机器，也不是 Claude Code / dsh。
set -eu
cd "$(dirname "$0")/.."
if [ "${1:-}" = "" ]; then
  echo "用法: scripts/minimal_accept.sh <redis-server 路径>" >&2
  exit 2
fi
REDIS_SERVER=$1
PY=${HELLO_PYTHON:-.venv/bin/python}
if [ ! -x "$PY" ]; then
  echo "找不到 $PY。请先创建 .venv 并安装 requirements.txt。" >&2
  exit 2
fi
if [ -z "${HELLO_REGISTER_SECRET:-}" ]; then
  if [ ! -f config/hello-center.env ]; then
    echo "还没有本地配置。先运行 scripts/init_local_env.sh" >&2
    exit 2
  fi
  set -a
  # shellcheck disable=SC1091
  . ./config/hello-center.env
  set +a
fi
if [ -z "${HELLO_REGISTER_SECRET:-}" ]; then
  echo "HELLO_REGISTER_SECRET 仍然是空的。" >&2
  exit 2
fi

WORK=${HELLO_WORK:-/tmp/hello-minimal-$$}
mkdir -p "$WORK/redis"
REDIS_PORT=${HELLO_REDIS_PORT:-56479}
CENTER_PORT=${HELLO_CENTER_PORT:-8741}
cleanup() {
  if [ -n "${CENTER_PID:-}" ]; then
    kill "$CENTER_PID" 2>/dev/null || true
  fi
  if [ -n "${REDIS_PID:-}" ]; then
    kill "$REDIS_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

"$REDIS_SERVER" \
  --bind 127.0.0.1 \
  --port "$REDIS_PORT" \
  --dir "$WORK/redis" \
  --dbfilename dump.rdb \
  --appendonly yes \
  --appendfsync always \
  --save "" \
  --protected-mode yes \
  --daemonize no \
  >"$WORK/redis.log" 2>&1 &
REDIS_PID=$!

export HELLO_REDIS_URL="redis://127.0.0.1:${REDIS_PORT}/0"
export HELLO_BIND=127.0.0.1
export HELLO_PORT=$CENTER_PORT
export PYTHONPATH=.
export PYTHONUNBUFFERED=1
"$PY" -m hello_center >"$WORK/center.log" 2>&1 &
CENTER_PID=$!

ready=0
i=0
while [ "$i" -lt 50 ]; do
  if "$PY" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${CENTER_PORT}/healthz', timeout=1).read()" >/dev/null 2>&1; then
    ready=1
    break
  fi
  i=$((i + 1))
  sleep 0.2
done
if [ "$ready" -ne 1 ]; then
  echo "中心没有在端口 $CENTER_PORT 就绪。日志末尾:" >&2
  tail -n 40 "$WORK/center.log" >&2 || true
  exit 1
fi

mkdir -p "$WORK/agents"
umask 077
HELLO_URL="http://127.0.0.1:${CENTER_PORT}/mcp" \
  "$PY" scripts/register_once.py --url "http://127.0.0.1:${CENTER_PORT}/mcp" --token-file "$WORK/agents/a.token" --display-name "甲"
HELLO_URL="http://127.0.0.1:${CENTER_PORT}/mcp" \
  "$PY" scripts/register_once.py --url "http://127.0.0.1:${CENTER_PORT}/mcp" --token-file "$WORK/agents/b.token" --display-name "乙"

"$PY" scripts/minimal_exchange.py \
  --url "http://127.0.0.1:${CENTER_PORT}/mcp" \
  --token-file-a "$WORK/agents/a.token" \
  --token-file-b "$WORK/agents/b.token"
echo "最小验收结束。日志在 $WORK（含 Redis 数据，不要打包）。"
