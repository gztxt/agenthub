#!/usr/bin/env bash
# 扫描原生只读数据源，重建/增量刷新派生 catalog.db。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="${2:?--source 需要 src_id}"; shift 2 ;;
    -h|--help)
      printf '用法：%s [--source agents.registry|skills.disk|projects.local]\n' "$0"
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
"$PYTHON" - <<'PY'
import json
import os
import sys

from catalog import adapter, adapters, store

source = os.environ.get("CATALOG_SOURCE", "")
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
    print(json.dumps({"ok": all(r.get("ok") for r in results),
                      "db": str(store.default_path()), "results": results},
                     ensure_ascii=False, indent=2))
    sys.exit(0 if results and all(r.get("ok") for r in results) else 1)
except Exception as exc:
    print(json.dumps({"ok": False,
                      "error": "%s: %s" % (type(exc).__name__, exc)},
                     ensure_ascii=False))
    sys.exit(1)
PY
