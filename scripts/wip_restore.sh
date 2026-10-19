#!/usr/bin/env bash
# 查看 / 恢复 `wip_snapshot.sh` 留下的未提交改动快照。
#
# 用法：
#   scripts/wip_restore.sh              # 列出最近 10 条快照
#   scripts/wip_restore.sh <sha>        # 把该快照的内容应用回工作区（不动 HEAD、不提交）
#   scripts/wip_restore.sh <sha> --diff # 只看这个快照改了什么
set -euo pipefail

REPO="/fs/1000/ftp/技术文档/agenthub"
cd "$REPO"
LOG="$REPO/.git/wip-snapshots.log"

SHA="${1:-}"
FLAG="${2:-}"

if [ -z "$SHA" ]; then
    if [ ! -f "$LOG" ]; then
        echo "还没有快照：$LOG 不存在（说明从未有过未提交改动，或还没跑过 snapshot）"
        exit 0
    fi
    echo "最近 10 条快照（时间 / commit / ref）："
    grep -v '^    ' "$LOG" | tail -n 10
    echo
    echo "恢复：   scripts/wip_restore.sh <sha>"
    echo "看差异： scripts/wip_restore.sh <sha> --diff"
    exit 0
fi

if ! git cat-file -e "${SHA}^{commit}" 2>/dev/null; then
    echo "找不到 commit：$SHA（可能已被 gc 回收）" >&2
    exit 1
fi

if [ "$FLAG" = "--diff" ]; then
    git show --stat "$SHA"
else
    echo "把 $SHA 的内容应用回工作区（不移动 HEAD、不自动提交）..."
    git stash apply "$SHA"
fi
