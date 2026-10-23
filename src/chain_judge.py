# -*- coding: utf-8 -*-
"""主链判定：把本地模型放到「有处置后果、但判错不致命」的两个位置（2026-10-23）。

## 为什么不用技能里的 `choice` 原语

`jev-decision` 的 choice 让模型输出「各选项概率」。实测（`qwen2.5:1.5b`）：

| 输入 | criteria 顺序 | 选中 |
|---|---|---|
| 与任务完全无关的天气文本 | `completed` 在首位 | **completed**（conf 1.0） |
| 同一段文本 | `off_topic` 在首位 | **off_topic**（conf 0.5） |

⇒ 它**总是选第一个**，与内容无关（位置偏置）。这种信号拿去做任何裁决都是假的：
接进主链就是「永远不拦」，等于没接。同理 `noul` 恒 1.0、YES/NO 恒「否」。

## 换成它真能干的两件事

模型的强项是**逐字复述**，不是判断。所以两处落点都改成「让它产出可机械核对的东西」：

1. `classify_output`：要求它**从产物里逐字摘一句能证明任务完成的原文**，无关则输出 `NONE`。
   - 摘到的句子**必须在产物里原样存在**（幻觉守卫），否则按「没判成」处理；
   - 结果只区分 `completed` / `off_topic` —— 刚好是 `tasks.py` 唯一需要的区分
     （只有 `off_topic` 会触发人工复核）。
   - 实测：无关文本→`NONE`；只写函数没写调用示例→摘到函数定义；完整产物→摘到函数+调用。1.5~3.7s。
2. `pick_candidate`：把候选列成编号清单，要求它先写编号再写理由。
   - 实测**不受位置影响**：同一组候选正序/倒序各问一次，两次都指向同一条（1.6~1.8s）。
   - 运行时**两序各问一次并要求一致**；不一致（=偏置回来了）就当没判成，不改序。
     这是自证：不靠「相信模型」，靠「两次独立提问能不能对齐」。

## 纪律

1. **永不抛**：调用点在主链上，`tasks.py` 外层 `except` 会把任何异常记成 `failed`。
2. **输入硬截断**：任务 400 字、产物 1500 字、候选 4 条 × 描述 120 字（ctx 4096）。
3. **统计不另建**：成功/失败都写回 `local_judge`（计数、连续失败告警、`/api/vitals` 可见）。
4. **可关可回滚**：`CHAIN_JUDGE=0`。
5. **单飞**：Ollama 串行，进程内信号量 + `CHAIN_JUDGE_QUEUE_WAIT`，等不到就不判。
"""
import json
import os
import re
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional

import local_judge

#: 主链判定总开关。默认开——否则又回到「接了但从不调用」的休眠态。
ENABLED = os.getenv("CHAIN_JUDGE", "1").strip().lower() not in ("0", "false", "off", "no")
#: 单次超时。实测 1.5~3.7s，45s 只是兜底（Ollama 冷加载 / 别的活占着）。
TIMEOUT_S = float(os.getenv("CHAIN_JUDGE_TIMEOUT", "45"))
#: 产物短于此长度不值得判（「完成」两个字复述不出证据）。
MIN_OUTPUT_CHARS = int(os.getenv("CHAIN_JUDGE_MIN_CHARS", "40"))
OUTPUT_CHARS = int(os.getenv("CHAIN_JUDGE_OUTPUT_CHARS", "1500"))
TASK_CHARS = 400
MAX_CANDIDATES = 4
#: 排队等待上限（秒）。等不到就别等——这一次不判比把任务拖 100s 好。
QUEUE_WAIT_S = float(os.getenv("CHAIN_JUDGE_QUEUE_WAIT", "5"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.getenv("CHAIN_JUDGE_MODEL", "qwen2.5:1.5b")

_SEM = threading.BoundedSemaphore(1)


def enabled() -> bool:
    return ENABLED


def _norm(s: str) -> str:
    """比对用归一化：折叠所有空白（模型复述时常把换行改成空格）。"""
    return re.sub(r"\s+", "", s or "")


def _fail(why: str, ms: int = 0, skipped: bool = False) -> Dict[str, Any]:
    return {"ok": False, "skipped": skipped, "why": why, "ms": ms,
            "model": None, "evidence": None}


def _gen(prompt: str, timeout_s: Optional[float] = None) -> str:
    """打一次本机 Ollama。异常交给 `_call` 统一处理（主链上必须自包）。"""
    body = json.dumps({
        "model": MODEL, "stream": False, "prompt": prompt,
        "options": {"temperature": 0.1, "num_ctx": 4096},
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s or TIMEOUT_S) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    return str(d.get("response") or "").strip()


def _call(fn, *a, **kw):
    """统一入口：信号量 + 失败写回 local_judge（成功由各分支自己记，便于带 model）。"""
    if not _SEM.acquire(timeout=QUEUE_WAIT_S):
        return _fail("判定队列忙（另一路在跑），本次不判", skipped=True)
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001 —— 主链上任何异常都不许外溢
        why = "%s: %s" % (type(e).__name__, str(e)[:120])
        local_judge.record(False, 0, why, kind="chain")
        return _fail(why)
    finally:
        _SEM.release()


def classify_output(task: str, output: str,
                    timeout_s: Optional[float] = None,
                    use_cache: bool = True) -> Dict[str, Any]:
    """执行产物：`off_topic` 或 `completed`（附逐字证据）。

    返回 `{ok,label,evidence,ms,model,why,skipped}`。`ok=False` ⇒ 调用方按接入前行为走。
    `use_cache` 仅为签名兼容：本实现单次 1.5~3.7s，不落缓存（见模块头）。
    """
    if not ENABLED:
        return _fail("CHAIN_JUDGE=0（主链判定已关闭）", skipped=True)
    task = (task or "").strip()[:TASK_CHARS]
    body = (output or "").strip()
    if not task:
        return _fail("任务描述为空，无法判", skipped=True)
    if len(body) < MIN_OUTPUT_CHARS:
        return _fail("产物仅 %d 字（<%d），不足以判定" % (len(body), MIN_OUTPUT_CHARS),
                     skipped=True)
    prompt = ("任务：%s\n\n执行产物：\n%s\n\n"
              "请只从产物里摘录一句能直接证明任务已完成的原文（必须逐字摘录，不许改写）。"
              "如果产物与任务无关、或没有完成，只输出 NONE。"
              % (task, body[:OUTPUT_CHARS]))
    return _call(_classify_once, prompt, body, timeout_s)


def _classify_once(prompt: str, body: str, timeout_s: Optional[float]) -> Dict[str, Any]:
    t0 = time.monotonic()
    txt = _gen(prompt, timeout_s)
    ms = int((time.monotonic() - t0) * 1000)
    if not txt or txt.strip().upper().startswith("NONE"):
        local_judge.record(True, ms, kind="chain")
        return {"ok": True, "label": "off_topic", "evidence": None,
                "ms": ms, "model": MODEL, "why": "", "skipped": False}
    ev = txt.strip().strip("`\"'“”‘’")
    if _norm(ev) and _norm(ev) in _norm(body):
        local_judge.record(True, ms, kind="chain")
        return {"ok": True, "label": "completed", "evidence": ev[:300],
                "ms": ms, "model": MODEL, "why": "", "skipped": False}
    # 摘出来的句子产物里没有 ⇒ 幻觉，不能据此判定（也不许当成 off_topic 去拦人）
    local_judge.record(False, ms, "evidence_not_in_output", kind="chain")
    return _fail("摘录不在产物里（疑似幻觉），本次不判", ms=ms)


def pick_candidate(query: str, candidates: List[Dict[str, str]],
                   timeout_s: Optional[float] = None,
                   use_cache: bool = True) -> Dict[str, Any]:
    """在同层候选里选一条：正序 / 倒序各问一次，**两次一致才算数**。

    返回 `{ok,picked,index,agreed,ms,model,why}`；`index` 是 `candidates` 里的下标。
    """
    if not ENABLED:
        return _fail("CHAIN_JUDGE=0（主链判定已关闭）", skipped=True)
    q = (query or "").strip()[:TASK_CHARS]
    cands = [c for c in (candidates or [])[:MAX_CANDIDATES] if (c.get("name") or "").strip()]
    if not q:
        return _fail("查询为空，无法判", skipped=True)
    if len(cands) < 3:
        return _fail("候选仅 %d 条（<3），不值得花一次推理" % len(cands), skipped=True)
    return _call(_pick_once, q, cands, timeout_s)


def _pick_once(q: str, cands: List[Dict[str, str]],
               timeout_s: Optional[float]) -> Dict[str, Any]:
    t0 = time.monotonic()
    first = _ask_pick(q, cands, timeout_s)
    rev = list(reversed(cands))
    second = _ask_pick(q, rev, timeout_s)
    ms = int((time.monotonic() - t0) * 1000)
    a = cands[first]["name"] if first is not None else None
    b = rev[second]["name"] if second is not None else None
    if a and a == b:
        local_judge.record(True, ms, kind="chain")
        return {"ok": True, "picked": a, "index": first, "agreed": True,
                "ms": ms, "model": MODEL, "why": "", "skipped": False}
    local_judge.record(False, ms, "pick_disagree", kind="chain")
    return _fail("两次顺序选择不一致（位置偏置），本次不判", ms=ms)


def _ask_pick(q: str, cands: List[Dict[str, str]],
              timeout_s: Optional[float]) -> Optional[int]:
    lines = "\n".join("%d. %s —— %s" % (i + 1, c["name"], str(c.get("desc") or "")[:120])
                      for i, c in enumerate(cands))
    prompt = ("任务：%s\n\n候选参考资料：\n%s\n\n哪个候选最适合作为该任务的参考？"
              "先单独一行写编号（只要数字），再写一句理由。" % (q, lines))
    txt = _gen(prompt, timeout_s)
    m = re.search(r"([1-9]\d?)", txt or "")
    if not m:
        return None
    i = int(m.group(1)) - 1
    return i if 0 <= i < len(cands) else None


def accept_decision(auto_accept: bool, judge: Dict[str, Any]) -> Dict[str, Any]:
    """把「自动接受开关 + 判定结果」折成处置。**纯函数**，便于 L0 钉死方向。

    返回 `{accept, status, review, reason}`：
    - `accept=True`  ⇒ 走原 `success` 分支（AUTO_ACCEPT 且未判成 off_topic）；
    - `accept=False` ⇒ 走 `submitted`（AUTO_ACCEPT 关，或判定说答非所问）；
    - `review=True`  ⇒ 其中「判定拦下」的那一种，`reason` 必须落库，否则复核的人
      会把「模型认为答非所问」误读成「verifier 失败」。
    """
    off = bool(judge.get("ok") and judge.get("label") == "off_topic")
    if auto_accept and not off:
        return {"accept": True, "status": "success", "review": False, "reason": None}
    if not off:
        return {"accept": False, "status": "submitted", "review": False, "reason": None}
    return {"accept": False, "status": "submitted", "review": True,
            "reason": ("judge off_topic（证据：未摘到任何与任务相关的原文）："
                       "产物与任务要求无关，待人工复核——verifier 结构判据已通过")}


def promote_within_bucket(rows: List[Dict[str, Any]], index: int) -> bool:
    """把 `rows[index]` 提到**它所在桶的首位**。返回是否真的移动过。

    「桶」= `rows[i]["bucket"]`。没有 bucket 字段时整列视为一桶。
    越界 / 已在首位 ⇒ 不动并返回 False（调用方据此知道「判定选了但次序没变」）。
    """
    if index < 0 or index >= len(rows):
        return False
    bucket = rows[index].get("bucket")
    head = next((k for k, r in enumerate(rows) if r.get("bucket") == bucket), index)
    if head == index:
        return False
    rows.insert(head, rows.pop(index))
    return True
