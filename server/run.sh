#!/usr/bin/env bash
# 安卓源码工作台后端启动脚本（Linux 服务器）
#
# 用法：
#   cp config.example.json config.json   # 只改 roots（允许的源码根目录）
#   ./run.sh                             # 自动选择依赖清单并启动
#   ./run.sh --check-config              # 只做配置与环境自查
#   ASW_CONFIG=/data/asw/config.json ./run.sh
#
# 依赖清单按解释器版本自动选择：
#   Python >= 3.10 → requirements.txt
#   Python 3.8/3.9 → requirements-py38.txt（Ubuntu 20.04 自带 3.8，走这一条）
set -euo pipefail
cd "$(dirname "$0")"

# 免 root 安装的 ripgrep 一般放在 ~/bin；这里补上，避免换了 shell 之后 rg 找不到而退回慢引擎
export PATH="$HOME/bin:$PATH"

PY="${PYTHON:-python3}"
CONFIG="${ASW_CONFIG:-config.json}"
if [ ! -f "$CONFIG" ]; then
  CONFIG="config.example.json"
  echo "[提示] 未找到 config.json，改用 $CONFIG（示例配置只指向仓库内的 fixtures）" >&2
fi

PYVER="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
if "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  REQUIREMENTS="requirements.txt"
else
  REQUIREMENTS="requirements-py38.txt"
fi

if ! "$PY" -m pip --version >/dev/null 2>&1; then
  cat >&2 <<EOF
[错误] $PY（Python $PYVER）没有可用的 pip，无法安装依赖。三种解决办法：
  1) 有 sudo：sudo apt-get install -y python3-pip
  2) 无 sudo 但有外网：
     curl -sS https://bootstrap.pypa.io/pip/3.8/get-pip.py -o /tmp/get-pip.py && "$PY" /tmp/get-pip.py --user
  3) 公司内网源：参照 docs/DEPLOY.md 第 6 节
EOF
  exit 3
fi

if ! "$PY" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
  echo "[错误] $PY（Python $PYVER）缺少依赖，请执行：" >&2
  echo "    $PY -m pip install --user -r $REQUIREMENTS" >&2
  exit 3
fi

if [ "$REQUIREMENTS" = "requirements-py38.txt" ]; then
  echo "[提示] 检测到 Python $PYVER（< 3.10），使用 $REQUIREMENTS" >&2
fi
echo "[启动] $PY（Python $PYVER） -m app --config $CONFIG $*"
exec "$PY" -m app --config "$CONFIG" "$@"
