#!/bin/sh
# 生成本地配置。口令写进文件，不打印，也不放进命令行。
set -eu
cd "$(dirname "$0")/.."
umask 077
dest=${1:-config/hello-center.env}
if [ -e "$dest" ]; then
  echo "已存在 $dest，没有覆盖。需要重做时先自己移走这个文件。" >&2
  exit 1
fi
mkdir -p "$(dirname "$dest")"
secret=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24), end="")')
cat > "$dest" <<EOF
HELLO_REGISTER_SECRET=${secret}
HELLO_REDIS_URL=redis://127.0.0.1:6379/0
HELLO_BIND=127.0.0.1
HELLO_PORT=8731
HELLO_ALLOWED_HOSTS=
EOF
chmod 600 "$dest"
unset secret
echo "已写入 $dest，权限 600。口令没有打印。"
echo "启动前执行: set -a; . $dest; set +a"
