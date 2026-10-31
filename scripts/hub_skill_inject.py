#!/usr/bin/env python3
"""Claude Code `UserPromptSubmit` hook：把 hub 的技能候选注入 Claude 的上下文。

**为什么是这个形状**
- Claude Code 的 UserPromptSubmit hook 是**同步阻塞在用户输入之前**的 ⇒ 本脚本的第一属性
  不是「检索得多准」，是**绝不拖慢、绝不拦住用户**。故：
  · 全程零第三方依赖（只用标准库），不 import 仓内模块（避免连累 hub 的 import 链）；
  · 任何异常都吞掉并 `exit 0` —— hook 返回非零会让 Claude **拒绝这次输入**，
    把「技能没检索到」升级成「用户发不出消息」；
  · 单次 HTTP + 硬超时（`urllib` 的 `timeout=`），默认 500ms。
- 与 pi 的 `hub-facade.ts`（D4）**同源同参**：都打 `/api/skill/relevant`、都 `rerank=false`。
  理由是实测：该端点 rerank=true 时 `took_ms=1064.5`（2026-10-22 外部 jev 实测；
  切本地模型后更慢），两条注入通道的预算都小于它，开着判定附注等于「每次都超时静默丢弃」。

**注入什么、不注入什么**：只推「名字 + 一句用途 + 命中词」，不推正文。正文由 Claude
自己决定要不要读（Read 磁盘路径）。推正文等于替 Claude 做「值不值得读」的判断，
而 hub 只知道词面相关——D3 实测里外部 jev 对噪声的置信(0.85)高于对正确答案的(0.53)，
足以说明「让打分器决定读什么」这件事本身不可靠（该端点 2026-10-23 已下线）。

用法：
    echo '{"prompt":"..."}' | python3 scripts/hub_skill_inject.py
    python3 scripts/hub_skill_inject.py --self-test   # 不联网的纯函数自测
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

HUB = os.environ.get("HUB_FACADE_URL", "http://127.0.0.1:3102")
#: 注入预算（ms）。实测 BM25 层 12~53ms；500ms 是墙钟上限，不是目标值。
TIMEOUT_MS = int(os.environ.get("HUB_SKILL_TIMEOUT_MS", "500"))
#: top-N。设计书：注入时 top-3 保留全描述。
TOP_N = 3
#: 后端侧 token 上限；防止极端查询把注入块撑大。
MAX_TOKENS = 600
MIN_PROMPT_LEN = 4
#: 2026-10-31 用户裁定「尽量自动调用技能，提高效率、减少消耗」：
#: 光给名字，agent 还得再发一轮工具调用去 Read 正文 —— 那一轮本身就是消耗。
#: 命中够强时**把正文一起内联**，直接省掉一轮 LLM 回合。
#: 判据（三条同时成立才内联，避免把噪声正文硬塞进上下文）：
#:   ① `HUB_SKILL_INLINE_BODY` 未关（默认开）；
#:   ② top-1 的 bm25 分数 ≥ `HUB_SKILL_INLINE_MIN`（默认 20；实测「派发并行子代理」= 55.0，
#:      而纯噪声查询通常 <10，两者量级差得开）；
#:   ③ 正文长度 ≤ `HUB_SKILL_INLINE_MAX`（默认 6000 字符）——超长就只给名字 + 指引。
INLINE_BODY = os.environ.get("HUB_SKILL_INLINE_BODY", "1") not in ("0", "false", "no", "off")
INLINE_MIN = float(os.environ.get("HUB_SKILL_INLINE_MIN", "20"))
INLINE_MAX = int(os.environ.get("HUB_SKILL_INLINE_MAX", "6000"))
#: 埋点里的调用方标识。写死为 claude：本脚本只挂在 Claude Code 的 UserPromptSubmit。
CALLER = "claude"


def first_prompt(raw: str) -> str:
    """从 hook 的 stdin 里取用户输入。

    **容错是第一要求**：Claude 给的 JSON 结构若与预期不同（字段改名、被包一层），
    这里必须安静返回空串而不是抛栈 —— 抛栈会让 hook 非零退出，拦住用户输入。
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        d = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(d, dict):
        return ""
    for k in ("prompt", "user_prompt", "text", "message"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # 退一步：任何够长的字符串字段都算（总比不注入强）
    for v in d.values():
        if isinstance(v, str) and len(v.strip()) >= MIN_PROMPT_LEN:
            return v.strip()
    return ""


#: 正文只注入**一次/会话/技能**：不去重的话每一轮用户输入都塞 6KB 正文，
#: 那是把「省一轮工具调用」换成「每轮多 6KB」——净亏，正撞用户「减少消耗」的要求。
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "hub-skill-inject")
CACHE_TTL_S = int(os.environ.get("HUB_SKILL_INLINE_TTL", str(2 * 3600)))


def session_key(raw: str) -> str:
    """从 hook JSON 里取会话标识（取不到就退到 ppid —— 至少同一进程内稳定）。"""
    try:
        d = json.loads(raw or "{}")
    except Exception:
        d = {}
    if isinstance(d, dict):
        for k in ("session_id", "conversation_id", "transcript_path"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:120]
    return "ppid-%d" % os.getppid()


def already_inlined(sess: str, name: str) -> bool:
    """本会话是否已经内联过这个技能（写过标记文件，按 TTL 过期）。"""
    try:
        path = os.path.join(CACHE_DIR, "%s-%s" % (re.sub(r"[^0-9a-zA-Z_.-]", "_", sess)[:60],
                                                  re.sub(r"[^0-9a-zA-Z_.-]", "_", name)[:60]))
        if not os.path.exists(path):
            return False
        if time.time() - os.path.getmtime(path) > CACHE_TTL_S:
            return False
        return True
    except Exception:
        return False


def mark_inlined(sess: str, name: str) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = os.path.join(CACHE_DIR, "%s-%s" % (re.sub(r"[^0-9a-zA-Z_.-]", "_", sess)[:60],
                                                  re.sub(r"[^0-9a-zA-Z_.-]", "_", name)[:60]))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(datetime.now().isoformat())
    except Exception:
        pass      # 标记写不进去最多多注入一次，不该影响注入本身


def format_skills(payload: Dict[str, Any]) -> str:
    """把端点响应渲染成注入块。字段缺失/变形一律返回空串（不抛）。"""
    bm25 = (payload or {}).get("bm25")
    if not isinstance(bm25, dict):
        return ""
    items = bm25.get("items")
    if not isinstance(items, list) or not items:
        return ""
    lines: List[str] = []
    for it in items[:TOP_N]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        desc = str(it.get("description") or "").split("。")[0].split("\n")[0][:70]
        matched = it.get("matched")
        hit = " ".join(str(m) for m in matched[:4]) if isinstance(matched, list) else ""
        tail = "（命中：%s）" % hit if hit else ""
        lines.append("· %s — %s%s" % (name, desc, tail))
    if not lines:
        return ""
    return ("## 可能用得上的技能（agent-hub BM25 top-%d · 只给线索；"
            "要读全文自己 Read 该技能的 SKILL.md）\n%s" % (len(lines), "\n".join(lines)))


def inline_body(item: Dict[str, Any], deadline_ms: int) -> str:
    """把 top-1 技能的正文取回来（`/api/skill/read` 是 GET，不过写闸门）。

    **三条失败路径一律静默降级**：拿不到就只输出名字 —— 内联是优化，不是前提。
    超时按剩余预算算（`deadline_ms`），不给 hook 叠加第二个 500ms。
    """
    if not INLINE_BODY or not isinstance(item, dict):
        return ""
    try:
        if float(item.get("bm25") or 0) < INLINE_MIN:
            return ""
    except Exception:
        return ""
    name = str(item.get("name") or "").strip()
    if not name:
        return ""
    qs = urllib.parse.urlencode({"name": name, "route": item.get("route") or "",
                                 "with_body": "true"})
    req = urllib.request.Request(HUB + "/api/skill/read?" + qs,
                                 headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=max(0.05, deadline_ms / 1000.0)) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return ""
    body = str(d.get("content") or "")
    if not body:
        return ""
    if len(body) > INLINE_MAX:
        return ("\n\n### %s 正文（前 %d / 共 %d 字；其余自己 Read %s）\n%s\n"
                % (name, INLINE_MAX, len(body), d.get("path") or name, body[:INLINE_MAX]))
    return "\n\n### %s 正文（已自动内联，直接照做，不必再查）\n%s\n" % (name, body)


def fetch(prompt: str, timeout_ms: int = TIMEOUT_MS, sess: str = '') -> str:
    """打一次 hub，返回注入块；**任何异常都返回空串**。

    连接被拒 / 超时 / 非 JSON / 404（旧版后端没有该端点）——这四种在本 hook 的语义里
    全部等价于「这次没有候选」，都不该拦住用户输入。
    """
    if not prompt or len(prompt) < MIN_PROMPT_LEN:
        return ""
    qs = urllib.parse.urlencode({
        "q": prompt, "n": TOP_N, "max_tokens": MAX_TOKENS, "rerank": "false",
        # 2026-10-20：自报身份，让 /api/skill/relevant 的埋点能回答「谁自动拿的候选」。
        # via=hook 是唯一的「自动」判据（runlog._mode_of）；缺了它，这条会被记成
        # 人工查询，权重统计里就看不到 agent 的自动发现行为。
        "caller": CALLER, "via": "hook",
    })
    req = urllib.request.Request(HUB + "/api/skill/relevant?" + qs,
                                 headers={"Accept": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=max(0.05, timeout_ms / 1000.0)) as r:
            if getattr(r, "status", 200) != 200:
                return ""
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        # 刻意宽泛：这一层的存在意义就是「把任何故障都压成无候选」
        return ""
    block = format_skills(payload)
    if not block:
        return ""
    # 剩余预算才去取正文（不给 hook 叠加第二个满额超时）
    left = timeout_ms - int((time.monotonic() - t0) * 1000)
    if left > 80:
        items = ((payload or {}).get("bm25") or {}).get("items") or []
        top = items[0] if items else {}
        name = str((top or {}).get("name") or "")
        # 本会话这个技能已经内联过 ⇒ 只给名字（否则每轮都塞一遍正文 = 净亏）
        if name and not already_inlined(sess, name):
            try:
                extra = inline_body(top, left)
            except Exception:
                extra = ""
            if extra:
                block += extra
                mark_inlined(sess, name)
    return block


def _self_test() -> int:
    ok = fail = 0

    def check(label: str, got: Any, want: Any) -> None:
        nonlocal ok, fail
        if got == want:
            ok += 1
            print("  PASS  %s" % label)
        else:
            fail += 1
            print("  FAIL  %s：got=%r want=%r" % (label, got, want))

    # ── stdin 解析：坏输入必须安静 ──
    check("空 stdin", first_prompt(""), "")
    check("非法 JSON 不抛", first_prompt("{not json"), "")
    check("非 dict 不抛", first_prompt("[1,2]"), "")
    check("取 prompt 字段", first_prompt('{"prompt":"帮我整理报告"}'), "帮我整理报告")
    check("字段改名仍能取到", first_prompt('{"text":"帮我整理报告"}'), "帮我整理报告")
    check("未知结构退到长字符串", first_prompt('{"foo":"帮我整理报告"}'), "帮我整理报告")
    check("prompt 为空串", first_prompt('{"prompt":"  "}'), "")

    # ── 渲染：字段变形不得抛 ──
    check("无 bm25", format_skills({}), "")
    check("bm25 非 dict", format_skills({"bm25": "x"}), "")
    check("items 非 list", format_skills({"bm25": {"items": {}}}), "")
    check("items 空", format_skills({"bm25": {"items": []}}), "")
    check("条目指针非 dict", format_skills({"bm25": {"items": ["x", 3]}}), "")
    check("条目无 name", format_skills({"bm25": {"items": [{"description": "d"}]}}), "")

    good = {"bm25": {"items": [{"name": "agent-dispatch", "description": "派活用的。后续略",
                                "matched": ["派活", "任务"]}]}}
    blk = format_skills(good)
    check("渲染含名字", "agent-dispatch" in blk, True)
    check("渲染含命中词", "命中：派活 任务" in blk, True)
    check("描述只取首句", "后续略" in blk, False)

    # ── fetch 的短路条件（不发请求）──
    check("空 prompt 不打网络", fetch(""), "")
    check("过短 prompt 不打网络", fetch("ab"), "")
    return 0 if fail == 0 else 1


def main(argv: List[str]) -> int:
    if "--self-test" in argv:
        return _self_test()
    raw = sys.stdin.read()
    prompt = first_prompt(raw)
    if prompt:
        block = fetch(prompt, sess=session_key(raw))
        if block:
            # 注入块走 stdout（Claude Code 会把它并入上下文）。**不写 stderr**——
            # stderr 在非调试模式下会进用户的转录，成为噪音。
            sys.stdout.write(block + "\n")
            return 0
    # 没候选、或 hub 不可达、或 stdin 是坏的：一律静默成功。
    # 非零退出会让 Claude 拒绝这次输入 —— 那比「没注入技能」严重得多。
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        # 最后一道闸：main 自己抛了也不能让 hook 非零退出。
        sys.exit(0)