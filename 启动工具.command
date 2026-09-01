#!/bin/bash
# 双击启动「问卷批量导入工具」
# 第一次会自动创建 .venv 并安装依赖，之后只需等浏览器打开。
set -e
cd "$(dirname "$0")" || exit 1
PORT="${PORT:-8765}"
URL="http://127.0.0.1:${PORT}"

pick_python() {
  if [ -x ".venv/bin/python" ]; then
    echo ".venv/bin/python"; return
  fi
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"; return
  fi
  if [ -x "/Users/yzz/.workbuddy/binaries/python/envs/default/bin/python" ]; then
    echo "/Users/yzz/.workbuddy/binaries/python/envs/default/bin/python"; return
  fi
  echo ""
}

ensure_venv() {
  if [ -x ".venv/bin/python" ]; then
    return
  fi
  echo "第一次使用：正在创建本地环境并安装依赖（大约半分钟）…"
  local py
  py="$(pick_python)"
  if [ -z "$py" ]; then
    echo "找不到 Python 3。请先安装：https://www.python.org/downloads/"
    echo "装好后再双击本文件。"
    read -r -p "按回车关闭…"
    exit 1
  fi
  "$py" -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
  echo "依赖已装好。"
}

if lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "工具已在运行，正在打开浏览器…"
  open "$URL"
  sleep 2
  exit 0
fi

ensure_venv
PY=".venv/bin/python"

echo "正在启动…"
"$PY" app.py &
PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if curl -sf "$URL" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "启动失败，请看上方报错。"
    read -r -p "按回车关闭…"
    exit 1
  fi
  sleep 0.4
done
open "$URL"
echo
echo "  已打开 $URL"
echo "  关掉这个窗口，或按 Ctrl+C，即可停止工具。"
echo
wait "$PID"
