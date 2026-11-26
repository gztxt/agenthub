#!/usr/bin/env bash
# nanobot 原生界面（WebUI :18880）按需拉起
#
# 为什么需要脚本（2026-11-24 接入）：
#   nanobot 的 WebUI 是 gateway 进程内提供的（同一进程既监听 WebUI :18880 又监听
#   gateway health :18796），登录态靠 URL fragment 里的 bootstrapSecret 换取短期
#   token（channels.websocket.tokenIssueSecret，tokenTtlS=300）。
#   ⇒ 嵌进 hub 必须带这枚参数，否则是登录页。
#
# 为什么 secret 从配置读而不是从日志抓：它**落盘在 config.json**（首次 webui 运行时生成），
#   跨重启不变；日志里只印一次完整 URL，滚动后就没了。
#
# 落点：
#   ~/.nanobot/config.json               tokenIssueSecret（0600 由上游保证）
#   /vol1/tools/nanobot-0.4.2/venv       运行时
#   ~/.nanobot/logs/gateway.log          gateway 日志
set -uo pipefail

BIN=/vol1/tools/nanobot-0.4.2/venv/bin/nanobot
CFG="$HOME/.nanobot/config.json"
LOG="$HOME/.nanobot/logs/gateway.log"

port_of() { python3 -c "import json,sys;d=json.load(open('$CFG'));print(d['channels']['websocket'].get('port') or 18880)"; }
WEB_PORT="$(port_of)"
GW_PORT="$(python3 -c "import json;d=json.load(open('$CFG'));print(d.get('gateway',{}).get('port') or 18796)")"

port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

if ! port_open "$WEB_PORT"; then
  mkdir -p "$(dirname "$LOG")"
  setsid nohup "$BIN" gateway --background >>"$LOG" 2>&1 </dev/null &
  # 有界等待：最多 ~20s，不做无限轮询
  for _ in $(seq 1 40); do
    port_open "$WEB_PORT" && break
    sleep 0.5
  done
  port_open "$WEB_PORT" || { echo "nanobot WebUI 未在 ${WEB_PORT} 起来，见 $LOG" >&2; exit 11; }
fi

SECRET="$(python3 -c "import json;d=json.load(open('$CFG'));print((d['channels']['websocket'].get('tokenIssueSecret') or '').strip())")"
[ -n "$SECRET" ] || { echo "config.json 缺 channels.websocket.tokenIssueSecret" >&2; exit 12; }

# 自证：WebUI 首页须 200 且是 HTML（桥转发的前提）；gateway health 须可读
CODE="$(curl -sS --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:${WEB_PORT}/")"
[ "$CODE" = "200" ] || { echo "自证失败：WebUI 首页返回 ${CODE}" >&2; exit 13; }
port_open "$GW_PORT" || { echo "自证失败：gateway health ${GW_PORT} 未监听" >&2; exit 14; }

echo "nanobot WebUI ready on http://127.0.0.1:${WEB_PORT} (gateway ${GW_PORT})"
echo "bootstrapSecret=${SECRET}"
