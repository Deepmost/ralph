#!/bin/bash
# 启动 Chrome 并开启 CDP 远程调试端口（macOS，兼容 Linux），供 agent-browser 使用。
#
# Usage:
#   ./scripts/ralph/chrome-cdp.sh [port]        # 默认端口 9222
#
# 说明：
#   - 若端口已有可用的 CDP 服务，则直接复用，不重复启动。
#   - 使用独立 user-data-dir，避免与日常 Chrome 实例冲突。
#   - 启动后输出 WS_URL，并打印在 agent-browser 中可直接使用的命令。

set -euo pipefail

PORT="${1:-9222}"
CDP_VERSION_URL="http://localhost:${PORT}/json/version"
PROFILE_DIR="${CHROME_CDP_PROFILE:-$HOME/.cache/ralph-chrome-cdp}"

# 常见 Chrome/Chromium 可执行文件路径（macOS 优先）
CHROME=""
for c in \
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary" \
  "/Applications/Chromium.app/Contents/MacOS/Chromium" \
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
  "$(command -v google-chrome || true)" \
  "$(command -v chromium || true)" \
  "$(command -v chromium-browser || true)"; do
  if [ -n "$c" ] && [ -x "$c" ]; then
    CHROME="$c"
    break
  fi
done

# 读取 CDP 的 WebSocket URL（用 python3 解析，避免 sed 处理 JSON 的脆弱性）
read_ws_url() {
  curl -sf "$CDP_VERSION_URL" 2>/dev/null \
    | python3 -c "import sys, json; print(json.load(sys.stdin).get('webSocketDebuggerUrl', ''))" \
    2>/dev/null
}

# 1) 端口已有可用 CDP 服务 → 复用
if read_ws_url | grep -q .; then
  echo "检测到端口 ${PORT} 已有 CDP 服务，复用现有实例。"
else
  # 2) 启动新的 Chrome
  if [ -z "$CHROME" ]; then
    echo "错误: 未找到 Google Chrome / Chromium，请先安装。" >&2
    exit 1
  fi

  echo "启动 Chrome: $CHROME"
  echo "  CDP 端口 : ${PORT}"
  echo "  用户目录 : ${PROFILE_DIR}"
  mkdir -p "$PROFILE_DIR"
  "$CHROME" \
    --remote-debugging-port="${PORT}" \
    --user-data-dir="${PROFILE_DIR}" \
    --no-first-run \
    --no-default-browser-check \
    >/dev/null 2>&1 &

  # 3) 轮询等待 CDP 就绪（最多约 10 秒）
  for _ in $(seq 1 50); do
    if read_ws_url | grep -q .; then
      break
    fi
    sleep 0.2
  done
fi

WS_URL="$(read_ws_url)"
if [ -z "$WS_URL" ]; then
  echo "错误: CDP 端口 ${PORT} 未就绪，请检查 Chrome 是否正常启动。" >&2
  exit 1
fi

echo
echo "CDP 就绪。"
echo "WS_URL=${WS_URL}"
echo
echo "在 agent-browser 中按如下方式使用（每条命令都必须带 --cdp）："
echo "  WS_URL=\$(curl -s ${CDP_VERSION_URL} | python3 -c \"import sys,json; print(json.load(sys.stdin)['webSocketDebuggerUrl'])\")"
echo "  agent-browser --cdp \"\$WS_URL\" open https://example.com"
