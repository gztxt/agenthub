"""技能相关性打分（同步内存 BM25F）—— `/api/skill/relevant` 的快路径。

【为什么不借 SQLite FTS5】本仓已经踩过这个坑并把结论写进了代码注释：
- `kb.py:20`：「**没有 `tokenize=` 子句**（默认 unicode61），中文查询『端口』FTS 只召回 **2** 条」
- `memindex.py:431` 用的是 `fts5(tokenize='trigram')`，`memindex.py:80` 补刀
  「这不是调优问题，是**分词器决定的事实**」。
技能条目是**名称以 ASCII 为主、描述以中文为主**的混合语料，unicode61 切不出中文，
trigram 又对短查询不友好 ⇒ 这里自己切，两种语言都切得开。

【为什么不引 rank_bm25 / jieba】`requirements.txt` 里没有它们。语料只有 ~400 条、
总量几百 KB，纯 Python 打分的成本可忽略（与 `skill.py` 模块 docstring 里「实测扫描是
毫秒级」同量级）。为了 400 条数据引入两个新依赖，是拿维护成本换零收益。

【打分口径：BM25F 两字段】标准 BM25 把整篇文档当一个袋子，name 和 description 等权，
于是「描述里出现 3 次 query 词」会盖过「名字就叫这个」。技能匹配里**名字命中远比描述命中
可信**（`agent-dispatch` 就是唯一该叫这个名字的东西），所以用 BM25F：
每个字段先各自算长度归一化，权重再乘上去（W_NAME > W_DESC）。

【不做的事】本模块**不发网络请求**、不读磁盘、不碰 jev。它是纯函数：
输入 query + 已扫好的 items，输出带分数和命中词的排序。外部精排在 `jev_client.py`，
由端点决定要不要调、调不通就原样退回本层结果——本层永远给得出答案，所以它是可测的。
"""
import math
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

# ── BM25F 参数（取值是常规默认；W_NAME 是本项目自己的判断，见模块 docstring）──
K1 = 1.5
B = 0.75
W_NAME = 2.5
W_DESC = 1.0

#: 2026-10-22 批 A1：第三字段「触发词」。
#:
#: 【为什么必须有】实测（10-22，8 条真实中文任务）：`/relevant` rank1 正确 **1/8**，
#: 而同样 6 条英文任务 rank1 正确 **5/6**。根因不是排序算法，是**分词与语料不同语言**:
#: 查询是中文（切出二元组），技能名与绝大多数描述是英文 ⇒ 中文查询**永远打不中 name 字段**
#: （`matched_in` 恒空），只能靠少数几条中文描述里的泛词（测试/检查）碰运气，
#: 于是 `aidev-02-implement-audit` 在「调试一个偶发失败的测试」和「给这个新功能写测试」
#: 两条不同查询上**得到完全相同的 4.7677 分** —— 说明只有「测试」这一个词在贡献。
#:
#: 触发词是**检索词表**（技能名 → 中文触发词），不是技能内容副本：SKILL.md 仍是唯一权威，
#: 这里只补「中文用户会怎么说这件事」。技能可自己在 frontmatter 写 `triggers:`（优先），
#: 没有则由 `skill_triggers.py` 的策展表补（见那里的 provenance 说明）。
#: 权重取 2.2：接近 name(2.5) 而非 desc(1.0)。
#: 2026-10-22 实测标定（不是拍脑袋）：
#:  · W_TRIG=1.5 时「检查我的改动是否符合规范」top1 是 `web-ui-cdp-verify`（15.15，
#:    仅凭描述里的「改动/动是/是否」三个词）而 `code-review` 10.45 排第二——
#:    它的三个命中词（检查/改动/规范）**全部来自触发词**，却因为权重低反而输给描述噪声。
#:  · 提到 2.2 后 code-review 反超（×2.2/1.5=1.67 ⇒ 17.4 > 15.15），且英文侧不变
#:    （英文名本身命中 name 字段，不靠 trig）。
#: 仍低于 name：名字是作者给的唯一标识，触发词只是「中文用户会怎么说」。
W_TRIG = 2.2

#: 名称/触发词/描述命中权重，供端点回显，避免「权重写死在别处、这里改了没人知道」
FIELD_WEIGHTS = {"name": W_NAME, "trig": W_TRIG, "desc": W_DESC}

#: 2026-10-22 批 A2：**覆盖度系数**（惩罚「单泛词支配」）。
#:
#: 纯 BM25 只奖励词频，不区分「命中 1 个词 10 次」与「命中 4 个不同词」。
#: 实测病态：一条长中文描述含「测试」二字，就能对所有含「测试」的查询拿第一，
#: 无论查询还想表达什么（调试/写测试/跑测试 全被吞成一个词）。
#: 故在总分上乘 `1 + ALPHA * (命中的不同查询词数 / 查询词总数)`：
#:   · 单词查询：cov=1 ⇒ 全体同乘 2，**次序不变**（不影响既有单测与短查询）；
#:   · 多词查询：只命中 1 个词的条目被压到 (1+1/n)，命中多个的条目接近 ×2。
#: ALPHA=1.0 时「命中 1/5 词」= ×1.2，「命中 5/5 词」= ×2.0，比值 1.67 —— 足以翻转
#: 单泛词的支配，又不会把真正的强命中埋掉。
COVERAGE_ALPHA = 1.0

#: CJK 字符类。用 \uXXXX 转义而不是直接写「㐀-䶿」这类字面量：
#: 字面量在源码里会因编辑器/终端/复制粘贴而丢字或变码位，而正则悄悄少一段区间不会报错，
#: 只会让某些汉字静默切不出词——这正是本模块最不该发生的那种失败。
_CJK_CLASS = (
    "\u3400-\u4dbf"   # 扩展 A
    "\u4e00-\u9fff"   # 基本区
    "\uf900-\ufaff"   # 兼容汉字
    "\u3040-\u30ff"   # 日文假名
    "\uac00-\ud7af"   # 谚文
)
_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9_.+-]*")
_SEP_RE = re.compile(r"[._+-]+")
_CJK_RE = re.compile("[" + _CJK_CLASS + "]+")

#: 2026-10-22 批 A2：**功能字二元组噪声过滤**。
#:
#: 实测病态（真实语料）：「检查我的改动是否符合规范」的命中词里出现了 `动是`、`是否`、`我的`
#: —— 它们跨词边界拼出来的**垃圾二元组**。更糟的是垃圾二元组 IDF **很高**（只在 1~2 篇
#: 文档里出现），于是拿到的分比真词还多，把 `web-ui-cdp-verify` 抬到 `code-review` 前面。
#: 规则：只要二元组**任一字**是功能字，整条丢。代价是少数真词会被误伤（含「不/可/要」的），
#: 与 `intake._tokens()` 的选择一致：宁可少召回也不能让噪声拿到高 IDF。
_CJK_STOP = set("的了是在和与及或就都而这那有为对把被从到会可要我们你他她它个些没不很更最是否")

#: query 分词后少于此数 ⇒ 结果不可信，端点要回 short_query=true 让调用方自己决定要不要用
#: （与 `memindex.py` 的 `is_short_query()`、`short_query_fallback` 同一口径）
SHORT_QUERY_TOKENS = 2


def tokenize(text: Any) -> List[str]:
    """切词：ASCII/数字按词；CJK 出**相邻二字**（长度为 1 时出该单字）。

    【为什么不同时出单字】2026-10-03 真实盘实测（365 条语料）出过一次假证据：
    同时出单字+二字时，“能/不/登/录/态” 这类高频字各自带一点 IDF，20 个 query 词累加，
    把 `arkcli-auth`/`arkcli-deploy` 抬到“登录态加载不出但能新建”的前四名——
    **三条真实查询的 `name命中` 全为空**，也就是没有任何一条是靠名字命中的，全是噪声。
    只出二字后噪声词没了，“端口/登录/记忆召回”这类真实词反而拉得开。
    单字查询（`q="端"`）由长度为 1 的回落分支接住，不会被漏。

    【为什么还要拆连字符】同一批实测：`_LATIN_RE` 把 `-` 当词内字符，于是
    `agent-dispatch` 是一个不可分的 token，而查询里写的是 `dispatch`——
    **对全部 365 条技能，name命中恒为空**。所以带分隔符的词额外拆出子词。
    两处实测教训是同一类：**分词器的错误不抛异常，只安静地少召回**，所以只能在真实盘上验。
    """
    if not text:
        return []
    s = str(text).lower()
    out: List[str] = []
    for m in _LATIN_RE.finditer(s):
        w = m.group(0).strip("._+-")
        if not w:
            continue
        out.append(w)
        # 本仓技能名几乎全是连字符命名（agent-dispatch / book-to-skill / skill-creator）。
        # 实测：不拆的话 “skill dispatch” 这类查询对它们 **name命中恒为空**——因为
        # 整名是一个不可分的 token，查询里的 “skill” 永远匹配不上。
        if _SEP_RE.search(w):
            out.extend(p for p in _SEP_RE.split(w) if len(p) >= 2)
    for m in _CJK_RE.finditer(s):
        run = m.group(0)
        if len(run) == 1:
            out.append(run)
        else:
            # A2：功能字参与的二元组一律丢（见 _CJK_STOP 注释：垃圾二元组 IDF 偏高）。
            kept = [run[i:i + 2] for i in range(len(run) - 1)
                    if run[i] not in _CJK_STOP and run[i + 1] not in _CJK_STOP]
            # ⚠ **不能把整段丢空**（2026-10-22 评测集当场抓到）：`摘要` 是 2 字且含功能字「要」，
            # 按字面规则整段被丢 ⇒ `tokenize("摘要") == []` ⇒ `/relevant` 回「查询切词后为空」，
            # 一个真实的短查询（且正是 `summarize` 技能的触发词）从此**永远搜不到**。
            # 规则本意是「长串里少几个二元组无妨」，不是「让短词消失」⇒
            # 整段被丢空时回落为未过滤版本（过滤只是在**有余量**时降噪，不是在没余量时删词）。
            out.extend(kept or [run[i:i + 2] for i in range(len(run) - 1)])
    return out


def build_index(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, float], Dict[str, float]]:
    """预计算：(docs, idf, avg_len)。语料不变时可以复用，避免每次请求重算 IDF。

    `docs[i]["tf"]["name"|"desc"]` 是该条的两字段词频，`len` 是字段长度（BM25F 归一化要用）。
    """
    docs: List[Dict[str, Any]] = []
    for it in items:
        nt = tokenize(it.get("name"))
        tt = tokenize(it.get("triggers"))
        dt = tokenize(it.get("description"))
        docs.append({"item": it,
                     "tf": {"name": Counter(nt), "trig": Counter(tt), "desc": Counter(dt)},
                     "len": {"name": len(nt), "trig": len(tt), "desc": len(dt)}})
    n = len(docs)
    df: Counter = Counter()
    for d in docs:
        for f in ("name", "trig", "desc"):
            df.update(set(d["tf"][f].keys()))
    idf = {t: math.log(1.0 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()} if n else {}
    avg = {f: (sum(d["len"][f] for d in docs) / n if n else 0.0)
           for f in ("name", "trig", "desc")}
    return docs, idf, avg


def score(docs: List[Dict[str, Any]], idf: Dict[str, float],
          avg: Dict[str, float], q_tokens: List[str]) -> List[Dict[str, Any]]:
    """逐条打分。返回与 docs 同序的 `{item, score, matched, matched_in}`。

    `matched` / `matched_in` 是给「相关性实验室」看的**可解释性**：
    面板要能回答「为什么这条排第一」，只给一个分数就答不上来。
    """
    out: List[Dict[str, Any]] = []
    qn = len(q_tokens) or 1
    for d in docs:
        total = 0.0
        matched: List[str] = []
        in_name: List[str] = []
        trig_hit = False
        for t in q_tokens:
            w = idf.get(t)
            if not w:
                continue
            for f in ("name", "trig", "desc"):
                tf = d["tf"][f].get(t, 0)
                if not tf:
                    continue
                # avg[f] 为 0（该字段全空）时把归一化系数兜成 1，避免除零把整条打成 0 分
                norm = 1.0 - B + B * (d["len"][f] / avg[f] if avg[f] else 1.0)
                total += FIELD_WEIGHTS[f] * w * tf * (K1 + 1.0) / (tf + K1 * norm)
                if t not in matched:
                    matched.append(t)
                if f == "name" and t not in in_name:
                    in_name.append(t)
                if f == "trig":
                    trig_hit = True
        # A2：覆盖度系数（见 COVERAGE_ALPHA 注释）。matches=0 时不乘，保持 0 分语义。
        if total:
            total *= 1.0 + COVERAGE_ALPHA * (len(matched) / qn)
        out.append({"item": d["item"], "score": round(total, 4),
                    "matched": matched, "matched_in": in_name,
                    "trig_hit": trig_hit,
                    "coverage": round(len(matched) / qn, 4)})
    return out


def collapse_by_name(rows: List[Dict[str, Any]], keep: int = 1) -> List[Dict[str, Any]]:
    """把**同名多副本**折叠成一行（2026-10-22 批 C5）。

    【实测病根】380 行条目只有 **276 个不同技能名**：85 个名字重复、多出 104 行
    （hermes / hermes-agent / hermes-web 是**真实不同文件**，realpath 去重压不下去）。
    这些副本内容相同 ⇒ 在 top-3 里占 2 个名额。实测「debug flaky test failure」的
    top-3 是 `['systematic-debugging','test-driven-development','test-driven-development']`，
    「decompose task into parallel subtasks」是 `[…,'simplify-code','simplify-code']`
    —— 注入链预算 600 tok 买到的其实只有 2 个技能。

    折叠是**排序后**做的（`rows` 已按 score 降序）：保留第一条（分最高/最靠前），
    把其余副本的 route 收进 `same_name_routes` 并给出 `same_name_count`，
    **不静默丢弃**（审计要看得出「还有 3 份副本」）。

    `keep>1` 保留前若干条同名（默认 1）。
    """
    out: List[Dict[str, Any]] = []
    idx: Dict[str, int] = {}
    for r in rows or []:
        name = str(((r.get("item") or r).get("name") or "")).strip().lower()
        if not name:
            out.append(r)
            continue
        if name not in idx:
            idx[name] = len(out)
            out.append(dict(r, same_name_routes=[], same_name_count=1))
            continue
        slot = out[idx[name]]
        if slot["same_name_count"] <= keep:
            slot["same_name_count"] += 1
            rt = (r.get("item") or r).get("route")
            if rt and rt not in slot["same_name_routes"]:
                slot["same_name_routes"].append(rt)
    return out


def rank(query: str, items: List[Dict[str, Any]], n: int = 5) -> Dict[str, Any]:
    """把 items 按 query 相关度排序，返回前 n 条 + 足够的降级说明。

    **三条不静默的规矩**（承接 `skill.py` 模块 docstring 的「失败表态纪律」）：
    1. 查询切不出词 ⇒ 回 `why`，不返回一份看着像「没找到相关内容」的空表；
    2. 语料为空 ⇒ 回 `why`，不说成「都不相关」；
    3. 一条都没命中 ⇒ 回 `why`，不说成「匹配成功但为空」。
    """
    q_tokens = tokenize(query)
    base: Dict[str, Any] = {
        "query": query,
        "query_tokens": q_tokens,
        "corpus": len(items),
        "short_query": len(q_tokens) < SHORT_QUERY_TOKENS,
        "weights": dict(FIELD_WEIGHTS),
    }
    if not q_tokens:
        return dict(base, items=[], why="查询切词后为空（无 ASCII 词也无 CJK 字），无从打分")
    if not items:
        return dict(base, items=[], why="语料为空：没有任何技能条目进入打分")

    docs, idf, avg = build_index(items)
    scored = score(docs, idf, avg, q_tokens)
    scored.sort(key=lambda x: (-x["score"], str(x["item"].get("name") or "")))
    hit = [s for s in scored if s["score"] > 0]
    if not hit:
        return dict(base, items=[], why="没有词命中任何技能（查询词全部不在技能名称与描述中）")
    top = hit[:max(1, int(n))]
    return dict(base, items=top, total_hits=len(hit),
                top_score=top[0]["score"], lowest_score=top[-1]["score"])
