#!/usr/bin/env bash
# 测试与实况探针的统一入口（口径唯一真相源：tests/tiers.py + tests/README.md）
#
#   bash scripts/run_tests.sh hermetic        # L0：干净机器/CI 上可跑，且要求零跳过
#   bash scripts/run_tests.sh hermetic-clean  # 同上，但把 HOME 换成空目录，真模拟干净 runner
#   bash scripts/run_tests.sh all             # L0 + L1（本机全量）
#   bash scripts/run_tests.sh host            # 只 L1（本机真实仓库形态断言）
#   bash scripts/run_tests.sh changed [文件...]   # 按改动挑子集（**开发用，不是闸门**）
#   bash scripts/run_tests.sh probe verify_p1_backend.py [http://127.0.0.1:3199]
#   bash scripts/run_tests.sh                 # 不带参数 = all
#
# 每次 hermetic/host/all 跑完都会附带一步**收集器对账**：unittest 收进几例 vs
# pytest 收进几例，两者不等即红。防的正是「标准套件全绿，可它压根没跑某些闸门」
# （2026-10-02 实测：模块级 pytest 风格闸门 14 条 ⇒ 961 vs 975）。只收不跑，2s。
#
# 为什么不直接 `python -m unittest discover`：那样 L0 里偷偷 skipTest 的用例
# 会和 L1 的显式跳过混进同一个 skipped=N，"全绿"就变成谎报。
set -euo pipefail
cd "$(dirname "$0")/.."
VENV="${HUB_VENV:-venv/bin/python}"
[ -x "$VENV" ] || VENV="$(command -v python3)"
MODE="${1:-all}"; shift || true

case "$MODE" in
  hermetic)       exec "$VENV" scripts/run_tier.py hermetic ;;
  hermetic-clean) exec "$VENV" scripts/run_tier.py hermetic --fake-home ;;
  host)           exec "$VENV" scripts/run_tier.py host ;;
  all)            exec "$VENV" scripts/run_tier.py all ;;
  changed)        exec "$VENV" scripts/run_changed.py "$@" ;;
  pytest)         exec "$VENV" -m pytest tests -v ;;
  probe)
    f="${1:?用法：run_tests.sh probe <verify_*.py|probe_*.py> [base-url]}"; shift
    [ -f "tests/$f" ] || { echo "找不到 tests/$f"; exit 2; }
    echo "+ $VENV tests/$f $*"
    exec "$VENV" "tests/$f" "$@" ;;
  *) echo "未知模式：$MODE（可用 hermetic|hermetic-clean|host|all|changed|pytest|probe）"; exit 2 ;;
esac
