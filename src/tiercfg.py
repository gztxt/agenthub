"""难度 → **起始档位**（starting tier）。只决定第一次发给哪个模型，不做别的。

## 为什么这个东西长这样（2026-10-29 联网调研，两条直接决定形状）

1. **模型自评的难度是"信心剧场"，不能当路由器。**
   scixa《Model Tier Switching Within the Execution Paths of AI Agents》把
   「拿模型自己说的 `difficulty: low` 当路由信号」列为最常见的路由器病害；
   arXiv 2605.24299（20 个前沿模型 × 6 个基准）实测：把「所有模型都答对的题」剔掉后，
   置信度与表现的关系基本消失，模型只对"这类模型整体能做什么"有认知，**没有对自己的**。
   ⇒ Leader 的 `difficulty` 只能当**起点先验**，绝不能当放行/降级判据。
   真正的判据是执行后的 verdict，以及失败后的升档（cascade）。

2. **难度信号本身不产生收益，收益必须由证据承担。**
   TokenTrim 在 RouterArena 的诚实消融：把难度路由器（Jev）整条拿掉，
   成本-准确率前沿几乎不动（62.4% / $26.69 vs 62.4% / $26.73）——
   增益来自检索证据，不来自难度估计。
   ⇒ 本模块必须**自带证伪工具**（`GET /api/tasks/tiers` 的 `audit` 段）：
   若 difficulty 与首轮成功率无关联，这个映射就该被删掉，而不是留着好看。

## 因此语义刻意收窄

  · 只填空：Leader 显式给了 `model` 就**一律不动**（`explicit`）；
  · 不降档：重试只由 `escalate_target()` 向**上**走，本模块不参与；
  · 不猜 ID：清单取不到（`checked=False`）或 ID 不在清单里 ⇒ 返回 `None` + 原因，
    「返回默认模型」等于把"不知道"伪装成"知道"；
  · 模型域不是 CCR 的 agent（codebuddy / cursor）不注入 —— 塞 CCR ID 必报 unknown model，
    那是把"选型"做成"必失败"。

## 档位顺序是**声明**，不是测量

`t1 < t2 < t3` 的能力排序来自模型命名与定价的常识判断，**本机没有任何测量支撑**。
所以它只能是"起点"，且必须由 `audit` 用真实 verdict 回来校验；顺序错了要能看出来、改得掉
（`TASK_TIERS` 全量可覆盖，代码里不留第二份真值）。
"""
import os
from typing import Any, Dict, List, Optional, Tuple

#: 默认档位。ID 必须在 CCR 清单里（运行时逐个校验，不在就跳过并给原因）。
#: 2026-11-02 重排：原 t1 第二员 agnes 与 t2 首位 deepseek/deepseek-v4-pro、t3 唯一成员
#: openrouter/openai/gpt-6.1-terra-pro 均已不在网关清单 ⇒ 全部换掉。现全部为在役
#: `alibaba/*`，且三档确实不同档（快 / 中 / 强），不再是三个同源 ID。
#: 档内成员只做「挂了换一个」，所以同档成员能力应接近；跨档才是能力阶梯。
#: 2026-11-02 23:05 再修：t1 第二员 `deepseek-v4-flash-0731` 也被从清单摘掉（清单现 11 个 ID，
#: 无此条）⇒ 换成 `deepseek-v4.1-flash-1017`（同族、实测 871ms）。t2 的第二员换成
#: `deepseek-v4-pro`（清单内、比 flash 强，充当中档降级位）。
#: ⚠ 档位顺序本身**仍无测量支撑**（只有时延实测），别把本表当成能力排序的证据。
DEFAULT_TIERS: Dict[str, Tuple[str, ...]] = {
    "t1": ("alibaba/deepseek-v4.1-flash", "alibaba/deepseek-v4.1-flash-1017"),
    "t2": ("alibaba/qwen3.8-flash", "alibaba/deepseek-v4-pro"),
    "t3": ("alibaba/qwen3.8-max", "alibaba/deepseek-v4-pro-0813"),
}

#: difficulty 1/2/3 → 档位。
DEFAULT_MAP: Dict[int, str] = {1: "t1", 2: "t2", 3: "t3"}

#: 难度地板：3 分任务**永远不落 t1**。即使有人把 mapping 改坏，地板仍然生效 ——
#: 这是"难任务别省钱"的唯一硬约束（软约束是 mapping，改一下就没了）。
DEFAULT_FLOORS: Dict[int, str] = {3: "t2"}

_OFF = ("0", "false", "no", "off")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def parse_tiers(raw: Optional[str] = None) -> Dict[str, List[str]]:
    """`t1=a,b;t2=c` → `{"t1": ["a","b"], "t2": ["c"]}`。

    格式错（没有 `=`、档位名为空）**整条忽略该项而不是整表丢弃**：
    一个手抖的档位不该让其余档位一起失效。
    """
    raw = _env("TASK_TIERS", "") if raw is None else raw
    if not raw:
        return {k: list(v) for k, v in DEFAULT_TIERS.items()}
    out: Dict[str, List[str]] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, ids = part.split("=", 1)
        name = name.strip()
        ms = [m.strip() for m in ids.split(",") if m.strip()]
        if name and ms:
            out[name] = ms
    return out or {k: list(v) for k, v in DEFAULT_TIERS.items()}


def parse_mapping(raw: Optional[str] = None) -> Dict[int, str]:
    """`1:t1,2:t2,3:t3` → `{1:"t1", ...}`；坏项忽略，缺项用默认。"""
    raw = _env("TASK_DIFF_TIER", "") if raw is None else raw
    out = dict(DEFAULT_MAP)
    if not raw:
        return out
    for part in raw.split(","):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        try:
            d = int(k.strip())
        except ValueError:
            continue
        if v.strip():
            out[d] = v.strip()
    return out


def _rank_of(tier: str) -> Tuple[int, str]:
    """`t2` → (2, "")；非 `t<数字>` 形式排在数字档之后（按名字）。"""
    t = (tier or "").strip()
    if t[:1] == "t" and t[1:].isdigit():
        return (int(t[1:]), "")
    return (10 ** 6, t)


def order(tiers: Optional[Dict[str, List[str]]] = None) -> List[str]:
    tiers = parse_tiers() if tiers is None else tiers
    return sorted(tiers.keys(), key=_rank_of)


def rank(tier: Optional[str], tiers: Optional[Dict[str, List[str]]] = None) -> int:
    """档位在阶梯上的位置（越小越便宜/越弱）。未知档位返回 -1。"""
    order_ = order(tiers) if tiers is None else order(tiers)
    t = (tier or "").strip()
    return order_.index(t) if t in order_ else -1


def next_tier(tier: Optional[str], tiers: Optional[Dict[str, List[str]]] = None) -> Optional[str]:
    """上一档（升档方向）；已在最高档或档位未知 ⇒ None。**永不向下**。"""
    tiers = parse_tiers() if tiers is None else tiers
    order_ = order(tiers)
    t = (tier or "").strip()
    if t not in order_:
        return None
    i = order_.index(t)
    return order_[i + 1] if i + 1 < len(order_) else None


# ── 超时按档位（2026-11-03 混排五缺陷 · 任务A）───────────────────────────
#
# 动机：全链路只有一个 `TASK_TIMEOUT_S`（tasks_tiers.py:23）当所有档位的超时，
# 便宜档和最强档跑同一个 600s。本段让超时能按档位分档（t1 短、t3 长），
# 并提供 `prev_tier`（降档方向）给「超时后降一档重试」的决议用。
#
# 纪律（对齐本模块既有 parse_tiers/next_tier 的语义）：
#   · 空/坏配置 ⇒ 回落 `TASK_TIMEOUT_S`（老行为），绝不因为分档没配就崩；
#   · `prev_tier` 与 `next_tier` 严格相反，且**永不向上**；触底/未知档 ⇒ None
#     （None 是 B 的降档决议「已触底、别再降」的信号，不能拿首档本身冒充）。

#: 默认分档超时（秒）。env `TASK_TIMEOUT_TIERS` 可全量覆盖；空/坏 ⇒ 回落 `TASK_TIMEOUT_S`。
#: ⚠ 数值是**声明**（t1 快给短、t3 强给长），无本机测量支撑，与 DEFAULT_TIERS 同口径。
DEFAULT_TIMEOUT_TIERS: Dict[str, int] = {"t1": 300, "t2": 600, "t3": 900}


def _fallback_timeout() -> int:
    """回落超时 = `TASK_TIMEOUT_S`（tasks_tiers.py:23，唯一定义处，不留第二份真值）。

    延迟 import：① 避开顶层 import 顺序耦合；② import 失败（tasks_tiers 依赖变动）
    也要给出可用值，绝不让「取超时」这一步抛异常 —— 回落到硬编码 600（= TASK_TIMEOUT_S 默认）。
    """
    try:
        import tasks_tiers
        val = getattr(tasks_tiers, "TASK_TIMEOUT_S", None)
    except Exception:
        return 600
    # 2026-11-03 集成修正：不能 `int()` 截断 —— 既有用例用 `TASK_TIMEOUT_S = 0.05`
    # 模拟超时（tests/test_task_attribution.py::test_verdict_null_on_timeout），
    # int(0.05) == 0 会被下面的 `>0` 判成无效而回落 600 ⇒ 那个用例永远不超时。
    # 数值型（含 float）且 >0 一律原样返回；`wait_for` 本来就收 float。
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return 600
    return val if val > 0 else 600


def parse_timeout_tiers(raw: Optional[str] = None) -> Dict[str, int]:
    """`'t1=300;t2=600;t3=900'` → `{'t1':300,'t2':600,'t3':900}`。

    env 名 `TASK_TIMEOUT_TIERS`。语义对齐 `parse_tiers`：
      · 空（None/'') ⇒ `{}`（调用方据此回落 `TASK_TIMEOUT_S`，不是整表用默认）；
      · 单项格式错（无 `=`、档位名空、秒数非正整数如 `'abc'`/`'0'`/`'-5'`）
        ⇒ **整项忽略，其余保留**（一个手抖的档位不该让其余档位一起失效）；
      · 重复档位名 ⇒ 后者覆盖前者（最后一次赋值胜出）；
      · 返回值按档位 rank 升序（复用 `_rank_of`，与 `order()` 同一把尺）⇒ 确定性排序。
    """
    raw = _env("TASK_TIMEOUT_TIERS", "") if raw is None else raw
    if not raw or not str(raw).strip():
        return {}
    vals: Dict[str, int] = {}
    for part in str(raw).split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, sec = part.split("=", 1)
        name = name.strip()
        sec = sec.strip()
        if not name or not sec:
            continue
        try:
            n = int(sec)
        except ValueError:
            continue
        if n <= 0:
            continue
        vals[name] = n          # 重复档位名：后者覆盖前者
    # 按档位 rank 升序（确定性）；非 t<数字> 的档位名排在其后、按名字典序。
    return {k: vals[k] for k in sorted(vals, key=_rank_of)}


def timeout_for_tier(tier: Optional[str], raw: Optional[str] = None) -> int:
    """该档位的超时秒数。

    回落 `TASK_TIMEOUT_S`（tasks_tiers.py:23）的情形，**一律不抛未捕获异常**：
      · `tier` 为空 / None；
      · `raw` 与 env 均空（分档未配置）；
      · `tier` 不在分档表里（含 `'explicit'` 这类非档位名、未知档位）；
      · 档位对应的值非法（parse 阶段已滤掉非正整数，双保险再挡一次）。
    命中且为正整数 ⇒ 返回该档秒数。

    默认表（2026-11-03 集成修正）：`raw is None` 且 env `TASK_TIMEOUT_TIERS` 为空时，
    用 `DEFAULT_TIMEOUT_TIERS`（t1=300/t2=600/t3=900）—— 否则「改了代码但没人配 env」
    会让缺陷1 永远不生效（默认值仍全 600，与改动前逐字相同）。显式传 `raw`（含空串）
    则**不**用默认表，保持一致纯函数语义（测试就是这么隔离的）。
    """
    fb = _fallback_timeout()
    t = (tier or "").strip() if isinstance(tier, str) else ""
    if not t:
        return fb
    table = parse_timeout_tiers(raw)
    if not table and raw is None:
        table = dict(DEFAULT_TIMEOUT_TIERS)
    val = table.get(t)
    if isinstance(val, int) and not isinstance(val, bool) and val > 0:
        return val
    return fb


def prev_tier(tier: Optional[str], tiers: Optional[Dict[str, List[str]]] = None) -> Optional[str]:
    """下一档（**降档**方向）；已在最低档或档位未知 ⇒ None。与 `next_tier` 严格相反。

    镜像 `next_tier`：同一张 `parse_tiers()` 表、同一个 `order()` 序。
    `None` 是「已触底/不认识这个档位」的明确信号（供降档决议终止），**绝不返回首档本身冒充**。
    """
    tiers = parse_tiers() if tiers is None else tiers
    order_ = order(tiers)
    t = (tier or "").strip()
    if t not in order_:
        return None
    i = order_.index(t)
    return order_[i - 1] if i - 1 >= 0 else None


def tier_for(difficulty: int, mapping: Optional[Dict[int, str]] = None,
             floors: Optional[Dict[int, str]] = None,
             tiers: Optional[Dict[str, List[str]]] = None) -> str:
    """难度 → 档位，并抬高到地板。返回的档位**保证在 tiers 里**，否则退回最低档。"""
    tiers = parse_tiers() if tiers is None else tiers
    mapping = parse_mapping() if mapping is None else mapping
    floors = DEFAULT_FLOORS if floors is None else floors
    order_ = order(tiers)
    if not order_:
        return ""
    try:
        d = max(1, min(3, int(difficulty)))
    except (TypeError, ValueError):
        d = 2
    want = mapping.get(d) or DEFAULT_MAP.get(d) or order_[0]
    if want not in order_:
        want = order_[0]
    floor = floors.get(d)
    if floor in order_ and rank(floor, tiers) > rank(want, tiers):
        want = floor
    return want


def first_usable(tier: str, tiers: Dict[str, List[str]],
                 catalog: Optional[set], checked: bool) -> Tuple[Optional[str], str]:
    """该档位下第一个**在清单里**的模型。返回 `(model, reason)`。

    `checked=False`（清单取不到）⇒ 不返回任何模型：这时"选一个"与"编一个"没区别。
    """
    ids = tiers.get(tier) or []
    if not ids:
        return None, "tier_empty"
    if not checked or catalog is None:
        return None, "catalog_unavailable"
    for m in ids:
        if m in catalog:
            return m, "ok"
    return None, "no_tier_model_in_catalog"


def plan(items: List[dict], *, catalog: Optional[set], checked: bool,
         ccr_agents: set, pool: Optional[set] = None,
         tiers: Optional[Dict[str, List[str]]] = None,
         mapping: Optional[Dict[int, str]] = None,
         floors: Optional[Dict[int, str]] = None,
         agent_tiers: Optional[Dict[str, List[str]]] = None,
         agent_floor: Optional[Dict[int, str]] = None
         ) -> Tuple[List[dict], Dict[str, Any]]:
    """给每个子任务补 `tier` / `model_planned` / `tier_reason`（就地改并返回同一列表）。

    优先级（从高到低，**任何一条都不许被后面的覆盖**）：
      1. `pinned_tier`（代码钉死，目前只有汇总节点用）→ 该档位；
      2. Leader 显式 `model` → `explicit`，原样保留；
      3. 模型域不是 CCR 的 agent → `agent_model_domain_not_ccr`（不注入）；
      4. difficulty → 档位 → 该档第一个在清单里的模型。
    """
    tiers = parse_tiers() if tiers is None else tiers
    mapping = parse_mapping() if mapping is None else mapping
    floors = DEFAULT_FLOORS if floors is None else floors
    order_ = order(tiers)
    top = order_[-1] if order_ else ""

    pool = pool if pool is not None else set()
    a_cfg = parse_agent_tiers() if agent_tiers is None else agent_tiers
    a_floor = parse_agent_floor() if agent_floor is None else agent_floor
    stats: Dict[str, int] = {}
    astats: Dict[str, int] = {}
    for it in items:
        it["tier"] = None
        it["model_planned"] = None
        it["tier_reason"] = ""
        it["agent_promoted_from"] = ""
        it["agent_tier_reason"] = ""

        # 先定执行者（它决定"能不能用模型 ID"），再定模型。
        # 钉了 `pinned_tier` 的节点（汇总）**执行者也钉顶层**：只钉模型没用 ——
        # 本机网关会把模型 ID 改写成同一个（2026-10-29 实测），真正变的是执行者。
        floor_i = a_floor
        if it.get("pinned_tier"):
            top_a = sorted(a_cfg, key=_rank_letter)[-1]
            floor_i = dict(a_floor)
            floor_i[int(it.get("difficulty") or 2)] = top_a
        it["agent_id"], it["agent_promoted_from"], it["agent_tier_reason"] = promote_agent(
            it.get("agent_id") or "", it.get("difficulty") or 2, pool, a_cfg, floor_i)
        astats[it["agent_tier_reason"]] = astats.get(it["agent_tier_reason"], 0) + 1

        pinned = it.get("pinned_tier")
        if pinned:
            tier = pinned if pinned in order_ else top
            model, why = first_usable(tier, tiers, catalog, checked)
            it.update(tier=tier, model_planned=model, tier_reason="pinned:%s" % why)
        elif it.get("model"):
            it.update(tier="explicit", model_planned=it["model"], tier_reason="explicit")
        elif (it.get("agent_id") or "") not in ccr_agents:
            it.update(tier=None, model_planned=None,
                      tier_reason="agent_model_domain_not_ccr")
        else:
            tier = tier_for(it.get("difficulty") or 2, mapping, floors, tiers)
            model, why = first_usable(tier, tiers, catalog, checked)
            it.update(tier=tier or None, model_planned=model, tier_reason=why)

        key = it["tier_reason"] or "unknown"
        stats[key] = stats.get(key, 0) + 1
    return items, {"counts": stats, "order": order_,
                   "mapping": {str(k): v for k, v in sorted(mapping.items())},
                   "tiers": {k: list(v) for k, v in tiers.items()},
                   "agent_counts": astats,
                   "agent_order": sorted(a_cfg.keys(), key=_rank_letter),
                   "agent_tiers": {k: list(v) for k, v in a_cfg.items()},
                   "agent_floor": {str(k): v for k, v in sorted(a_floor.items())}}


# ── 执行者档位：本机唯一**真的会改变能力**的那根杠杆 ──────────────────────
#
# 2026-10-29 实测：CCR 当前对 5 个不同模型 ID（alibaba/deepseek-v4.1-flash、
# deepseek/deepseek-v4-pro、alibaba/qwen3.8-max、openrouter/openai/gpt-6.1-terra-pro、
# agnes/agnes-2.5-flash）**一律回包 `agnes-2.5-flash`**（凭据取
# ~/.claude-code-router/config.json.bak-20261029_005625-agnes-fallback 那次的回落态）。
# ⇒ 只改 `model` 的档位映射在本机**当前等于空转**。这一点由 audit 的
# `model_tier_effective` 指标暴露（计划模型 ≠ 回包模型的比例），不许靠"代码写了"冒充"生效了"。
#
# 真正改变能力的是**执行者**：纯文本端点 vs 带工具环的 CLI。所以执行者档位是主档位，
# 模型档位是附带的（网关肯认就多一层便宜，不认也有记录）。

#: a1 = 纯文本端点（无 tools，文件/命令类任务必编造）；a2 = 带工具但工具环较浅；
#: a3 = 工具环最完整（文件 + 命令 + 多轮）。**顺序是声明，不是测量**。
DEFAULT_AGENT_TIERS: Dict[str, Tuple[str, ...]] = {
    "a1": ("claude", "jcode"),
    "a2": ("pi", "omp", "grok", "hermes", "codebuddy", "cursor", "dsh"),
    "a3": ("claude-code", "codex"),
}

#: 难度地板 → 执行者档位。**默认只对难度 3 生效**：degrade 需要有证据，
#: promote 只需要一个理由（scixa：任何"降级"都必须由结果证据支撑；
#: 自评难度可以触发升档，不能触发降档）。难 1/2 一律不动 Leader 的选择。
DEFAULT_AGENT_FLOOR: Dict[int, str] = {3: "a3"}


def parse_agent_tiers(raw: Optional[str] = None) -> Dict[str, List[str]]:
    raw = _env("TASK_AGENT_TIERS", "") if raw is None else raw
    if not raw:
        return {k: list(v) for k, v in DEFAULT_AGENT_TIERS.items()}
    out: Dict[str, List[str]] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, ids = part.split("=", 1)
        name = name.strip()
        ms = [m.strip() for m in ids.split(",") if m.strip()]
        if name and ms:
            out[name] = ms
    return out or {k: list(v) for k, v in DEFAULT_AGENT_TIERS.items()}


def parse_agent_floor(raw: Optional[str] = None) -> Dict[int, str]:
    raw = _env("TASK_AGENT_FLOOR", "") if raw is None else raw
    out = dict(DEFAULT_AGENT_FLOOR)
    if not raw:
        return out
    if raw.strip() in _OFF:
        return {}
    for part in raw.split(","):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        try:
            d = int(k.strip())
        except ValueError:
            continue
        if v.strip():
            out[d] = v.strip()
    return out


def agent_tier_of(agent_id: str, cfg: Optional[Dict[str, List[str]]] = None) -> Optional[str]:
    cfg = parse_agent_tiers() if cfg is None else cfg
    for name, members in cfg.items():
        if agent_id in members:
            return name
    return None


def promote_agent(agent_id: str, difficulty: int, pool: set,
                  cfg: Optional[Dict[str, List[str]]] = None,
                  floor: Optional[Dict[int, str]] = None) -> Tuple[str, str, str]:
    """按难度**只升不降**地挑执行者。返回 `(agent, promoted_from, reason)`。

    这是本模块唯一会改 `agent_id` 的地方，边界刻意收紧：
      · 只在难度地板存在（默认仅 3）且当前档位**低于**地板时动；
      · 目标必须在**当前可用池**里，否则原样返回 + `agent_floor_unavailable`；
      · 不做"顺手换成更便宜的"——降级要结果证据，这里没有。
    """
    cfg = parse_agent_tiers() if cfg is None else cfg
    floor = parse_agent_floor() if floor is None else floor
    try:
        d = max(1, min(3, int(difficulty)))
    except (TypeError, ValueError):
        d = 2
    want = floor.get(d)
    if not want:
        return agent_id, "", "no_agent_floor"

    tiers = {k: v for k, v in cfg.items()}
    order_ = sorted(tiers.keys(), key=_rank_letter)
    if want not in order_:
        return agent_id, "", "agent_floor_unknown"
    cur = agent_tier_of(agent_id, cfg)
    # 未知档位的执行者（自定义端口/未来新增）不参与升降：不认识就别动。
    if cur is None:
        return agent_id, "", "agent_tier_unknown"
    if order_.index(cur) >= order_.index(want):
        return agent_id, "", "already_at_floor"

    for cand in tiers[want]:
        if cand in pool and cand != agent_id:
            return cand, agent_id, "agent_floor:%s" % cand
    return agent_id, "", "agent_floor_unavailable"


def _rank_letter(name: str) -> Tuple[int, str]:
    t = (name or "").strip()
    if t[:1] in ("a", "t") and t[1:].isdigit():
        return (int(t[1:]), "")
    return (10 ** 6, t)
