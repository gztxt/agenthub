#!/usr/bin/env bash
# 非 MCP 工具链的统一编目查询入口；与 hub_query 共用 REST 契约。
set -euo pipefail
BASE="${HUB_URL:-http://127.0.0.1:${PORT:-3102}}"
CURL=(curl --noproxy '*' -fsS --max-time "${HUBQ_TIMEOUT:-10}")
if [[ $# -eq 0 ]]; then
  printf '用法：%s "查询词" [--domains a,b] [--types a,b] [--k 10] [--since TIME]\n' "$0"
  printf '      %s --status [--domain agent]\n' "$0"
  printf '      %s --get OID\n' "$0"
  exit 2
fi
case "$1" in
  --status)
    shift
    domain=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --domain) domain="${2:?--domain 需要值}"; shift 2 ;;
        *) printf '未知参数：%s\n' "$1" >&2; exit 2 ;;
      esac
    done
    exec "${CURL[@]}" --get --data-urlencode "domain=$domain" "$BASE/api/catalog/status"
    ;;
  --get)
    [[ $# -eq 2 ]] || { printf '%s --get 需要且只需要一个 OID\n' "$0" >&2; exit 2; }
    exec "${CURL[@]}" --get --data-urlencode "oid=$2" "$BASE/api/catalog/get"
    ;;
  -h|--help)
    printf '用法：%s "查询词" [--domains a,b] [--types a,b] [--k 10] [--since TIME]\n' "$0"
    printf '      %s --status [--domain agent]；%s --get OID\n' "$0" "$0"
    exit 0
    ;;
  *)
    q="$1"; shift
    args=()
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --domains|--types|--k|--since)
          [[ $# -ge 2 ]] || { printf '%s 需要值\n' "$1" >&2; exit 2; }
          args+=(--data-urlencode "${1#--}=$2"); shift 2 ;;
        *) printf '未知参数：%s\n' "$1" >&2; exit 2 ;;
      esac
    done
    exec "${CURL[@]}" --get --data-urlencode "q=$q" "${args[@]}" "$BASE/api/catalog/query"
    ;;
esac
