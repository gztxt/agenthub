#!/usr/bin/env bash
# 统一编目质量闸门：六项判据，非 0 退出码 = 有项不达标。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "$ROOT/venv/bin/python3" ]]; then PYTHON="$ROOT/venv/bin/python3"; else PYTHON="${PYTHON:-python3}"; fi
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
"$PYTHON" - <<'PY'
import json
import sys

from catalog import store, verify

try:
    con = store.connect(readonly=True)
except Exception as exc:  # noqa: BLE001
    print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
    sys.exit(2)
try:
    out = verify.verify(con)
finally:
    con.close()
print(json.dumps(out, ensure_ascii=False, indent=2))
sys.exit(0 if out.get("ok") else 1)
PY
