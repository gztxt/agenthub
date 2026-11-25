#!/usr/bin/env bash
# hub-apps.sh — 给**非 MCP 工具链**（pi / codex / grok / jcode / opencode / 纯 shell）用的 CLI Apps 入口。
#
# 为什么要有它：应用资源对 agent 的可见性有三条路，缺一条就有 agent 用不上——
#   ① MCP：hub_apps_list / hub_apps_run（claude / codebuddy 这些挂 MCP 的）
#   ② 技能：装好的应用会生成 SKILL.md 并软链进 技术文档/skills/ ⇒ hub_skill_auto 与
#      各 agent 自己的技能发现都能命中（这条对所有 agent 生效，不需要适配）
#   ③ 本脚本：上面两条都够不着时（没有 MCP、技能发现点没配）的最小兜底
#
# 纪律：本脚本只调 hub 自己的 REST（/api/apps*），不重写业务。
# 凭据沿用 hub 既有会话/口令口径：本机回环直连（loopback 免登录），
# 非回环要走 HUB_URL + HUB_PASS 或已登录的 Cookie——本脚本不新造凭据面。
#
# 用法：
#   bash scripts/hub-apps.sh list [过滤词]      # 目录（含已装状态）
#   bash scripts/hub-apps.sh installed          # 只列已装
#   bash scripts/hub-apps.sh health             # 自检（vendored/写权限/PyYAML）
#   bash scripts/hub-apps.sh run <name> [args…] # 运行已装应用
#   bash scripts/hub-apps.sh install <name>     # 安装（会真的装包）
#   bash scripts/hub-apps.sh uninstall <name>   # 卸载
#   bash scripts/hub-apps.sh bridge             # 重扫技能桥（修「装了但找不到」）
#
# 输出一律是服务端原样的 JSON（不在这里裁剪），agent 自己解析。
set -euo pipefail

HUB=${HUB_URL:-http://127.0.0.1:3102}
CURL=(curl -sS --max-time 320)

usage() { sed -n '18,27p' "$0"; exit 2; }
[ $# -ge 1 ] || usage
cmd=$1; shift || true

case "$cmd" in
  list)
    q=${1:-}
    if [ -n "$q" ]; then
      "${CURL[@]}" "$HUB/api/apps" | python3 -c '
import json,sys
d=json.load(sys.stdin); q=sys.argv[1].lower()
rows=[a for a in d.get("apps",[]) if q in (a.get("name","")+" "+a.get("display_name","")+" "+a.get("description","")).lower()]
print(json.dumps({"count":len(rows),"installed_count":sum(1 for a in rows if a.get("installed")),
                  "items":[{"name":a.get("name"),"installed":a.get("installed"),
                            "entry_point":a.get("entry_point"),
                            "desc":(a.get("description") or "")[:160]} for a in rows]},
                 ensure_ascii=False, indent=1))
' "$q"
    else
      "${CURL[@]}" "$HUB/api/apps"
    fi ;;
  installed) "${CURL[@]}" "$HUB/api/apps/installed" ;;
  health)    "${CURL[@]}" "$HUB/api/apps/health" ;;
  bridge)    "${CURL[@]}" -X POST "$HUB/api/apps/bridge" ;;
  install|uninstall)
      [ $# -ge 1 ] || usage
      "${CURL[@]}" -X POST "$HUB/api/apps/$1/$cmd" ;;
  run)
      [ $# -ge 1 ] || usage
      name=$1; shift || true
      # 参数按 JSON 数组传（不经 shell 展开，避免管道/重定向被当成参数）
      body=$(python3 -c 'import json,sys;print(json.dumps({"args":sys.argv[1:]}))' "$@")
      "${CURL[@]}" -X POST -H 'Content-Type: application/json' -d "$body" \
        "$HUB/api/apps/$name/run" ;;
  *) usage ;;
esac
