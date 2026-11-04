#!/usr/bin/env bash
# 扫描原生只读数据源，重建/增量刷新派生 catalog.db。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE=""
PURGE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="${2:?--source 需要 src_id}"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    -h|--help)
      printf '用法：%s [--source <src_id>] [--purge]\n' "$0"
      printf '  P0: agents.registry skills.disk projects.local\n'
      printf '  P1: sessions.archived docs.filesystem memory.archive logs.hub\n'
      printf '  省略 --source = 全部已注册源（run_all）\n'
      printf '  --purge = 扫描后物理删除 status=missing 的对象（跳过即出库）\n' "$0"
      exit 0 ;;
    *) printf '未知参数：%s\n' "$1" >&2; exit 2 ;;
  esac
done
if [[ -x "$ROOT/venv/bin/python3" ]]; then
  PYTHON="$ROOT/venv/bin/python3"
else
  PYTHON="${PYTHON:-python3}"
fi
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export CATALOG_SOURCE="$SOURCE"
export CATALOG_PURGE="$PURGE"
"$PYTHON" - <<'PY'
import json
import os
import sys

from catalog import adapter, adapters, store

source = os.environ.get("CATALOG_SOURCE", "")
purged = {}
try:
    with store.CatalogWriter() as writer:
        if source:
            selected = adapter.get_adapter(source)
            if selected is None:
                results = [{"src_id": source, "ok": False,
                            "error": "未知 src_id",
                            "available": sorted(adapters.REGISTRY)}]
            else:
                try:
                    results = [adapter.run_adapter(writer, selected)]
                except Exception as exc:
                    results = [{"src_id": source, "ok": False,
                                "error": "%s: %s" % (type(exc).__name__, exc)}]
        else:
            results = adapter.run_all(writer)
        if os.environ.get("CATALOG_PURGE") == "1":
            for r in results:
                sid = r.get("src_id")
                if r.get("ok") and sid:
                    purged[sid] = writer.purge_missing(sid)
    print(json.dumps({"ok": all(r.get("ok") for r in results),
                      "purged": purged,
                      "db": str(store.default_path()), "results": results},
                     ensure_ascii=False, indent=2))
    sys.exit(0 if results and all(r.get("ok") for r in results) else 1)
except Exception as exc:
    print(json.dumps({"ok": False,
                      "error": "%s: %s" % (type(exc).__name__, exc)},
                     ensure_ascii=False))
    sys.exit(1)
PY
