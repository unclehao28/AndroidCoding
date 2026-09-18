#!/usr/bin/env bash
# 安卓源码工作台后端启动脚本（Linux 服务器）
#
# 用法：
#   cp config.example.json config.json   # 只改 roots（允许的源码根目录）
#   ./run.sh                             # 等价于 python3 -m app --config config.json
#   ./run.sh --check-config              # 只校验配置
#   ASW_CONFIG=/data/asw/config.json ./run.sh
#
# 依赖：python3.9+ 与 server/requirements.txt。
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
CONFIG="${ASW_CONFIG:-config.json}"
if [ ! -f "$CONFIG" ]; then
  CONFIG="config.example.json"
  echo "[提示] 未找到 config.json，改用 $CONFIG（示例配置只指向仓库内的 fixtures）" >&2
fi

if ! "$PY" -c 'import fastapi, uvicorn' 2>/dev/null; then
  echo "[错误] 缺少依赖，请先执行：$PY -m pip install -r requirements.txt" >&2
  exit 3
fi

echo "[启动] $PY -m app --config $CONFIG $*"
exec "$PY" -m app --config "$CONFIG" "$@"
