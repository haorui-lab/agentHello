#!/bin/sh
# 启动一个只监听本机的 Redis。数据目录由调用者提供，持久化使用 AOF。
set -eu
if [ "$#" -ne 3 ]; then
  echo "用法: scripts/run_redis.sh <redis-server> <数据目录> <端口>" >&2
  exit 2
fi
SERVER=$1
DIR=$2
PORT=$3
mkdir -p "$DIR"
exec "$SERVER" \
  --bind 127.0.0.1 \
  --port "$PORT" \
  --dir "$DIR" \
  --dbfilename dump.rdb \
  --appendonly yes \
  --appendfsync everysec \
  --save "" \
  --protected-mode yes \
  --daemonize no
