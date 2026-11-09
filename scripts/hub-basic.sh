#!/usr/bin/env bash
# hub-basic.sh — 机器侧 HTTP Basic 凭据的**唯一解析实现**。
#
# 被 scripts/intake.sh 与 scripts/selfcheck_intake.sh source（两处曾各写一份，必然漂移）。
#
# 口径（2026-11-07 用户裁定）：机器侧身份 = HTTP Basic（用户名+密码），Cookie 只给浏览器；
#   · 用户名：HUB_BASIC_USER，缺省 gztxt；
#   · 密码：**以 agenthub/.env 的 HUB_SUPERADMIN_PW 为唯一权威**，环境变量只作兜底。
#
# 为什么 .env 必须压过环境变量（2026-11-09 实测 401 的根因）：
#   PTY 会话在启动时把当时 .env 的内容导出进 shell，之后用户改了口令、.env 已更新，
#   但 shell 里仍是**旧口令快照**。脚本若先读环境变量 ⇒ 恒 401「未登录」，
#   而 .env 与新口令一直是对的。实测复现：HUB_SUPERADMIN_PW=<旧值> 时 RC=3 + 401。
#
# 为什么不用 curl -u user:pw：那会把口令放进 argv，ps/审计日志/子代理回显全都能看到。
#   本文件用 curl --config <(…) 从进程替换里喂凭据，argv 里只有 /dev/fd/NN。

hub_basic_resolve() {  # [env 文件] → 设 HUB_BASIC_USER / HUB_BASIC_PW；无密码返回 1
  local envf="${1:-/fs/1000/ftp/技术文档/agenthub/.env}" v=""
  if [ -r "$envf" ]; then
    # 剥 CR 与成对引号：dotenv 允许 KEY="value"，不剥就把引号一起当密码发出去。
    v=$(grep -m1 '^HUB_SUPERADMIN_PW=' "$envf" 2>/dev/null | cut -d= -f2- | tr -d '\r')
    case "$v" in
      \"*\") v="${v#\"}"; v="${v%\"}" ;;
      \'*\') v="${v#\'}"; v="${v%\'}" ;;
    esac
  fi
  if [ -n "$v" ] && [ -n "${HUB_SUPERADMIN_PW:-}" ] && [ "$v" != "${HUB_SUPERADMIN_PW:-}" ]; then
    # 只报「不一致」，绝不打印任一值。
    echo "WARN: 环境变量 HUB_SUPERADMIN_PW 与 $envf 不一致（环境值多为旧快照），以 .env 为准" >&2
  fi
  HUB_BASIC_PW="${v:-${HUB_SUPERADMIN_PW:-}}"
  HUB_BASIC_USER="${HUB_BASIC_USER:-gztxt}"
  [ -n "$HUB_BASIC_PW" ]
}

hub_basic_curl() {  # 用法同 curl：hub_basic_curl <curl 参数...>；凭据只走进程替换
  local esc
  esc=$(printf '%s' "$HUB_BASIC_PW" | sed 's/\\/\\\\/g; s/"/\\"/g')
  curl --config <(printf 'user = "%s:%s"\n' "$HUB_BASIC_USER" "$esc") "$@"
}
