# -*- coding: utf-8 -*-
"""工具描述质量（P2 C17；手册 C17：「工具瓶颈不在数量而在**描述质量**：
统一描述模板 + 完备性校验 + 按任务裁剪工具集」）。

【为什么必须有 —— 实测，不是假想】

本仓两处门面此前**只做截断，不做任何质量判断**：

- `mcpgw._tool_shape`：`description` 超 160 字截断（留痕 `description_truncated`），
  但**不看描述里有没有说清输入/输出/边界** —— 一句「do it」与一段完整说明，
  回给 agent 的**差不多是同一档**。
- `skill.py` 的技能条目：`description` 直接透传，**不做结构化**。

后果正是手册说的那件事：**模型靠 name+description 选工具**，描述含混时它只能猜
（第 17 章表 17.1-1：工具选错既可能源于模型能力不足，**也可能源于工具描述含混**；
第 15 章：更有效的描述应说明它**解决什么任务、需要什么输入、产生什么结果、哪些情况不适用**）。
描述质量是**可断言**的，不该只当成主观印象。

【三件事分开，各自可测】本模块**只做判定与整形**，不碰存储、不发网络、不读磁盘：

1. `parse_sections()` —— **统一描述模板**：把描述切成四段
   （`purpose` / `inputs` / `outputs` / `limits`），能认出的格式（`输入:`/`Inputs:`/`Args:`…）
   归一化；认不出就整段归入 `purpose`（**不编造**没写的段）。
2. `completeness()` —— **完备性校验**：三态 `complete|partial|insufficient`，
   **缺项显式列出**（`missing`），不把「没写」静默当成「没问题」。
3. `select()` —— **按任务裁剪**：给定任务描述，选相关工具子集。
   **复用 `skill_relevance` 的打分**（不建第二套 BM25），并**如实报被裁掉多少**。

【三态为什么不是二态】`complete`（四段齐）/ `partial`（有主体但缺段）/ `insufficient`
（连主体都没有，比如空串或纯噪声）。这与 P0-4「证据分层三态」「判不了做成一等公民」
同口径 —— 二态会把 `partial` 偷偷塞进 `complete`，完备率虚高。

【标签识别口径】只认「标签 + 冒号」结构（中英文均可）：
- purpose：无标签正文 或 `purpose/描述/功能/简介/说明`
- inputs：`输入/Input/Args/参数/Argument` 后跟冒号
- outputs：`输出/Output/Returns/结果` 后跟冒号
- limits：`限制/Limit/注意/Warning/约束` 后跟冒号
裸子串（如文本中出现 "input" 但不带冒号）**不算** inputs 标签 —— 防止误识别。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import skill_relevance

# ── 标签正则：中英文 + 常见变体，后跟可选空白 + 冒号 ──────────────────────
_LABEL_PATTERNS: List[tuple] = [
    # (section_key, compiled regex)
    ("purpose", re.compile(
        r"^(?:purpose|功能|描述|简介|说明|目的|about|what\s+it\s+does)\s*:\s*",
        re.IGNORECASE,
    )),
    ("inputs", re.compile(
        r"^(?:输入|inputs?|args?|参数|argument|uses?|requires?|accepts?)\s*:\s*",
        re.IGNORECASE,
    )),
    ("outputs", re.compile(
        r"^(?:输出|outputs?|returns?|结果|返回|response|yields?)\s*:\s*",
        re.IGNORECASE,
    )),
    ("limits", re.compile(
        r"^(?:限制|limits?|注意|warning|constraint|caveat|scope)\s*:\s*",
        re.IGNORECASE,
    )),
]

#: 四段名称（顺序固定，便于测试断言）
SECTIONS = ("purpose", "inputs", "outputs", "limits")

#: 判为 insufficient 的最小有效字符数
_INSUFFICIENT_CHAR_THRESHOLD = 10


def parse_sections(description: str) -> Dict[str, str]:
    """把描述文本按标签切成四段。认不出的归入 purpose。

    返回 `{section: text}`，四段都有键，没认出的值为空串。
    **不编造**没写的段 —— 这是判例 185「判据量错对象」的同族防线。
    """
    result: Dict[str, str] = {k: "" for k in SECTIONS}
    if not description or not description.strip():
        return result

    lines = description.split("\n")
    current_section = "purpose"
    current_lines: List[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # 检查是否匹配某个标签
        matched = False
        for section_key, pattern in _LABEL_PATTERNS:
            if pattern.match(stripped):
                # 保存当前段
                if current_lines:
                    result[current_section] = (
                        result.get(current_section, "") + "\n" + "\n".join(current_lines)
                    ).lstrip("\n")
                    current_lines = []
                current_section = section_key
                # 标签后的文本作为该段的开始
                after_match = pattern.sub("", stripped, count=1).strip()
                if after_match:
                    current_lines.append(after_match)
                matched = True
                break
        if not matched:
            current_lines.append(stripped)

    # 保存最后一段
    if current_lines:
        result[current_section] = (
            result.get(current_section, "") + "\n" + "\n".join(current_lines)
        ).lstrip("\n")

    # 如果 purpose 为空但有其他段，说明整段都是带标签的，purpose 应为空
    # 如果 purpose 不为空且没有任何标签匹配，说明整段都是 purpose
    return result


def completeness(description: str) -> Dict[str, Any]:
    """判定描述的完备性三态。

    返回：
      - verdict: ``complete`` | ``partial`` | ``insufficient``
      - missing: 缺少的段名列表（仅 partial/insufficient 时有意义）
      - sections: parse_sections 的结果
    """
    sections = parse_sections(description)
    filled = [k for k in SECTIONS if sections.get(k, "").strip()]

    # 先判 insufficient：有效字符太少
    total_chars = sum(len(s.strip()) for s in sections.values())
    if total_chars < _INSUFFICIENT_CHAR_THRESHOLD:
        return {
            "verdict": "insufficient",
            "missing": list(SECTIONS),
            "sections": sections,
        }

    if len(filled) >= 3:
        # 有三段及以上 ⇒ complete（即使 limits 缺失，主体已齐）
        missing = [k for k in SECTIONS if k not in filled]
        return {
            "verdict": "complete",
            "missing": missing,
            "sections": sections,
        }

    # 1-2 段 ⇒ partial
    missing = [k for k in SECTIONS if k not in filled]
    return {
        "verdict": "partial",
        "missing": missing,
        "sections": sections,
    }


def quality(description: str) -> Dict[str, Any]:
    """**入口函数**：统一调用完整性判定，返回供门面使用的质量块。

    对外接口返回格式：
      {
        "verdict": "complete" | "partial" | "insufficient",
        "missing": [...],
        "sections": {...},
      }
    """
    return completeness(description)


def select(
    task: str,
    tools: List[Dict[str, Any]],
    *,
    n: int = 8,
) -> Dict[str, Any]:
    """按任务描述从工具列表中裁剪相关子集。

    复用 `skill_relevance.rank` 做 BM25F 打分，**不建第二套口径**。
    如实报告 `considered/selected/dropped`，选不出时给 `why`。

    返回：
      {
        "items": [...],          # 裁剪后的工具列表（每个含 name/description/server 等）
        "considered": int,        # 候选总数
        "selected": int,          # 实际选中数
        "dropped": int,           # 被裁掉数
        "why": str | None,        # 选不出时的原因说明
      }
    """
    if not task or not task.strip():
        return {
            "items": [],
            "considered": len(tools),
            "selected": 0,
            "dropped": len(tools),
            "why": "任务描述为空，无法裁剪",
        }

    considered = len(tools)
    if considered == 0:
        return {
            "items": [],
            "considered": 0,
            "selected": 0,
            "dropped": 0,
            "why": "没有可用工具",
        }

    # 构造 skill_relevance.rank 需要的格式：每条 {name, description}
    items_for_rank = [
        {"name": t.get("name", ""), "description": t.get("description", "")}
        for t in tools
    ]

    ranked = skill_relevance.rank(task.strip(), items_for_rank, n=max(n, considered))
    ranked_names = {item["item"]["name"] for item in ranked.get("items", [])}

    selected_items = [
        t for t in tools
        if t.get("name") in ranked_names
    ][:n]

    dropped = considered - len(selected_items)
    why = None
    if not selected_items and considered > 0:
        why = ranked.get("why") or "所有工具与任务描述相关性均低于阈值"

    return {
        "items": selected_items,
        "considered": considered,
        "selected": len(selected_items),
        "dropped": dropped,
        "why": why,
    }
