"""联邦记忆源适配器（v0.13.26 批1：把「本机所有 agent 记忆」接进检索联邦）

背景：hub 的记忆联邦此前只有 local(便签4行)+TDAI(权威库)两路；v0.13.26 批1 起接入
claude-mem 观察库、pi/codex 会话 jsonl、工作区文件记忆、归档会话备份；**v0.13.28
补齐剩余四家**（用户问「是否是本机所有 agent 的记忆」后盘点补漏）：claude 原生记忆库
（~/.claude/projects/**/memory/）、grok、hermes、workbuddy。本模块只做**只读检索**，
「收集整理分类」的呈现层在记忆中心页（批4），聚合检索走 memory.py 的 RRF。

安全模型（与 sessions_store.py 同源，一处不留例外）：
- 只读：sqlite 一律 `file:...?mode=ro` + uri=True；WAL 库打不开再试 immutable=1
  （只读快照，宁可读到旧一点的数据，也绝不要求写权限）；
- 脱敏：所有出站的 content/error 必须过 `tdai_client.scrub` + `sessions_store.mask_title`
  两道——rg 匹配行可能带 `token=...`、claude-mem 的 facts 可能带 URL 凭据；
- 预算：每源独立超时（默认 1.8s）+ rg 子进程 timeout，总并发 gather；
  失败不抛错、逐路报 `ok=False`（与 memory.py backends 诊断口径一致）；
- 禁止：启动任何服务、写任何外部库、`find` 全盘递归（用 rg --files / scandir 预算内计数）。

注册表是「整理分类」的骨架：每个源一个 id/label/kind/weight/desc，`fedsources` 端点
逐源报健康与计数，前端按注册表渲染源开关与健康卡。新增源=注册表加一条+一个搜索函数，
不碰 memory.py 的融合逻辑（开闭原则，防止「每加一路改一处融合」的散弹枪修改）。
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import memindex
import sessions_store
import tdai_client

from fastapi import APIRouter

router = APIRouter()

HOME = Path.home()
#: 工作区根（记忆/知识文件层的位置）。不走 env：这些目录是本机事实，不是部署参数。
WORKSPACE = Path("/fs/1000/ftp/技术文档")

#: Agent Memory Repo（AMR）形态的记忆仓根（v0.13.103 P2，2026-10-13）。
#: 走 env 而非写死：AMR 是**外部规范**（Cognition 2026-10-04 发布，MIT），
#: 本机此刻**尚无**该形态的仓（实测 `find` 无 `.git` 记忆仓），所以根路径是
#: 「约定位点 + 可覆盖」，不是既成事实。用户把 AMR 仓放这里（或设
#: `AMR_MEMORY_ROOT`）后，本源即按 rg 只读消费，无需改代码。
#: AMR 的形态：`MEMORY.md` 作入口、条目一行一 bullet 带 `[source: url; added: 日期]`、
#: `[[path]]` 交叉链接、可含任意 md/sql/脚本。它的**检索方式是纯 grep + 链接跳转、
#: 无索引层**（SPEC 全文 1940 字节）——正因如此，本机已有的 FTS+RRF 是它缺的那一半：
#: 这里把 AMR 仓当**只读语料**接进联邦，即「消费该格式」而非「改用该格式」。
AMR_ROOT = Path(os.getenv("AMR_MEMORY_ROOT") or (WORKSPACE / "memory-repo"))

#: rg 类源的单文件大小上限：会话备份里有大 jsonl，防止 rg 在超长行上耗时失控。
RG_MAX_FILESIZE = "20M"
#: rg_text 源每次检索的条目上限（文件级命中粒度）。
RG_ITEM_CAP = 30
#: 出站 content 截断长度：匹配行可能是一整条 jsonl 消息（实测 grok 单行 33KB）。
CONTENT_SNIPPET = 300
#: probe 计数缓存（秒）：源计数是「整理」数据不是实时数据，10 分钟足够新鲜。
PROBE_TTL_S = 600.0


@dataclass
class FedSource:
    id: str
    label: str
    kind: str                      # 'sqlite_fts' | 'rg_text' | 'disabled'
    weight: float                  # RRF 融合权重（权威度排序，见 memory.py 注释的量纲说明）
    desc: str
    timeout_s: float = 1.8
    enabled: bool = True


#: opencode 源：表结构具备（message/part/session）但 09-26 已判「会话写共享库、不合格
#: 作派发对象」；记忆检索上同样降级处理——登记不启用，等用户点名再开（禁顺手启用）。
#: v0.13.28 四新源（用户：「是否是本机所有 agent 的记忆」——不是，所以补齐）：
#:   claude_projects（0.8 人工撰写的项目级事实，权威度等同观察库）
#:   grok_memory / hermes_memory / workbuddy_memory（0.6 成文原则与原始会话混合，
#:   权重取 workspace(0.7) 与 sessions(0.5) 之间）。
REGISTRY: Dict[str, FedSource] = {
    "claude_mem": FedSource(
        id="claude_mem", label="claude-mem 观察库", kind="sqlite_fts", weight=0.8,
        desc="~/.claude-mem/claude-mem.db（observations FTS5 + session_summaries）",
        timeout_s=1.5),
    "claude_projects": FedSource(
        id="claude_projects", label="claude 原生记忆库", kind="rg_text", weight=0.8,
        desc="~/.claude/projects/**/memory/*.md（各项目目录下的人工事实文件）"),
    "pi_sessions": FedSource(
        id="pi_sessions", label="pi 会话记录", kind="rg_text", weight=0.5,
        desc="~/.pi/agent/sessions/**/*.jsonl（文件级命中）"),
    "codex_sessions": FedSource(
        id="codex_sessions", label="codex 会话记录", kind="rg_text", weight=0.5,
        desc="~/.codex/sessions/**/*.jsonl（文件级命中）"),
    "grok_memory": FedSource(
        id="grok_memory", label="grok 记忆与会话", kind="rg_text", weight=0.6,
        desc="~/.grok/memory/*.md + ~/.grok/sessions/**/prompt_history.jsonl",
        timeout_s=1.8),
    "hermes_memory": FedSource(
        id="hermes_memory", label="hermes 记忆与会话", kind="rg_text", weight=0.6,
        desc="~/.hermes/memories/*.md + ~/.hermes/sessions/session_*.json",
        timeout_s=2.5),
    "workbuddy_memory": FedSource(
        id="workbuddy_memory", label="workbuddy 记忆", kind="rg_text", weight=0.6,
        desc="~/.workbuddy/{USER,SOUL,IDENTITY}.md + memory/ + sessions/",
        timeout_s=1.8),
    "workspace_files": FedSource(
        id="workspace_files", label="工作区文件记忆", kind="rg_text", weight=0.7,
        desc=f"{WORKSPACE}/MEMORY.md + memory/ + agent-knowledge/ + digest/"),
    "amr_memory": FedSource(
        id="amr_memory", label="AMR 格式记忆仓", kind="rg_text", weight=0.7,
        desc=f"{AMR_ROOT}/（Agent Memory Repo 形态：MEMORY.md 入口 + [source:] 溯源 + [[path]] 交叉链接）"),
    # P0-1 D18（2026-10-15）：**经验库**接入联邦。它是 hub 自己**蒸馏**出来的可复用单元
    # （情况→处理 + 适用条件 + 回源 trace），不是权威库的第二份副本 —— 权重取在
    # 「成文事实(0.7~0.8)」与「原始会话(0.5)」之间，且**只召回活跃条**（退役条不回来）。
    # kind 单起一路 `hub_sqlite`：它查的是 hub 自己的 agents.db（同进程连接，毫秒级）。
    "experiences": FedSource(
        id="experiences", label="经验库（Dreaming）", kind="hub_sqlite", weight=0.65,
        desc="hub data/agents.db 的 experiences 表（P0-1：情况→处理 + 适用条件，只召回活跃条）",
        timeout_s=1.0),
    "archived_sessions": FedSource(
        id="archived_sessions", label="归档会话备份", kind="rg_text", weight=0.4,
        desc=f"{WORKSPACE}/会话备份/**（含 claude/pi/codex/grok/hermes 归档）",
        timeout_s=2.5),
    "opencode_sessions": FedSource(
        id="opencode_sessions", label="opencode 会话库", kind="disabled", weight=0.0,
        desc="~/.local/share/opencode/opencode.db；已判不合格作派发对象，登记不启用",
        enabled=False),
}

#: 各 rg_text 源的搜索根 + glob。列表顺序即 rg 扫描顺序（都很浅，无所谓优先级）。
#: glob 坑（实测）：rg 的 `*` 不跨 `/`——claude_projects 必须写 `**/memory/*.md`
#: （`*/memory/*.md` 实测命中 0）。grok/hermes 大括号混合 glob 实测命中正常
#: （rg -g "{*.md,prompt_history.jsonl}" 23 文件）。
#: 已知小疵（记录不改）：_rg_item 按 endswith("sessions") 判 category，四个新 id
#: 全落 "knowledge"——含会话命中时分类略糊，不影响检索与融合（RRF 只看 rank+weight）。
_RG_TARGETS: Dict[str, tuple] = {
    "pi_sessions": ([str(HOME / ".pi" / "agent" / "sessions")], "*.jsonl"),
    "codex_sessions": ([str(HOME / ".codex" / "sessions")], "*.jsonl"),
    "claude_projects": ([str(HOME / ".claude" / "projects")], "**/memory/*.md"),
    "grok_memory": ([str(HOME / ".grok" / "memory"), str(HOME / ".grok" / "sessions")],
                    "{*.md,prompt_history.jsonl}"),
    "hermes_memory": ([str(HOME / ".hermes" / "memories"), str(HOME / ".hermes" / "sessions")],
                      "{*.md,session_*.json}"),
    "workbuddy_memory": ([str(HOME / ".workbuddy" / "USER.md"), str(HOME / ".workbuddy" / "SOUL.md"),
                          str(HOME / ".workbuddy" / "IDENTITY.md"), str(HOME / ".workbuddy" / "memory"),
                          str(HOME / ".workbuddy" / "sessions")], "{*.md,*.json}"),
    "workspace_files": ([str(WORKSPACE / "MEMORY.md"),
                         str(WORKSPACE / "memory"),
                         str(WORKSPACE / "agent-knowledge"),
                         str(WORKSPACE / "digest")], "*.md"),
    "archived_sessions": ([str(WORKSPACE / "会话备份")], None),   # 无 glob：目录内 md/jsonl/txt 全扫
    # AMR 仓：只扫 md（规范说可含 sql/脚本，但那些不是「记忆条目」；要收再放开 glob）
    "amr_memory": ([str(AMR_ROOT)], "*.md"),
}

_CLAUDE_MEM_DB = HOME / ".claude-mem" / "claude-mem.db"


def enabled_ids() -> List[str]:
    """可参与联邦的源 id 列表（memory.py 的 SOURCE_WHITELIST 由此动态生成）。"""
    return [s.id for s in REGISTRY.values() if s.enabled]


def _sanitize_content(text: str) -> str:
    """出站双道脱敏 + 截断。顺序：scrub（凭据模式）→ mask_title（token=KV）→ 截断。

    为什么两道都要：scrub 管 `Authorization: Bearer xxx` 这类成套凭据，
    mask_title 管 `?ccr_web_token=xxx` 这类 KV 形（sessions_store 实测口径），
    两者模式集不完全重合，少一道就留一条漏。
    """
    s = tdai_client.scrub(text or "")
    s = sessions_store.mask_title(s)
    return s[:CONTENT_SNIPPET]


def _fts_term(q: str) -> str:
    """用户词 → FTS5 安全 MATCH 项：空白分段，段间 OR，段内按语种定分词粒度。

    为什么不裸传：`q=a-b OR c` 会被 FTS5 当表达式解析，用户能注入语法（实测
    裸传 `双层 OR c` 命中 33 条本不该命中的行）。每段引号包裹后语义=字面。

    为什么不是整句包一个短语（v0.13.31 之前的做法）：整句成短语意味着
    **多关键词必须连续出现**才命中——实测 `q=双层记忆架构 三模型联动`
    （两词分处不同观察）返回 0 条，而两词各自都能命中 2~3 条。

    段内分词粒度按语种分（这是本函数的关键取舍，纯 OR 拆词会炸）：
    - **含 CJK 的段**：FTS5 不按空格切中文，用户的多词意图靠空格表达 ⇒ 按空格切 OR。
      实测 `"双层记忆架构" OR "三模型联动"` → 2 条（整句短语是 0 条）。
    - **纯 ASCII 的段**：空格通常是词内的一部分（`Claude Code` 是产品名），
      拆开 OR 会把召回从 8 条放大到 55 条（`Code` 单独就43 条，噪声压过信号）
      ⇒ **所有纯 ASCII 段整体合并为一个短语**，段间空格还原。
    纯 ASCII 段内仍剔引号防注入（`a"b` → `ab`，字面量而非语法）。

    混合输入（`Worker API 搜索`）走OR：ASCII 合并段 `"Worker API"` 与 CJK 段
    `"搜索"` 并列，兼顾两类召回。
    """
    parts = [p.replace('"', "") for p in re.split(r"\s+", q or "") if p.strip()]
    if not parts:
        return '""'
    cjk = re.compile(r"[一-鿿぀-ヿ]")
    out: list[str] = []
    ascii_parts: list[str] = []
    for p in parts:
        if cjk.search(p):
            # CJK 段按空格继续切（用户可能写 `双层记忆 架构`）
            out.extend(f'"{sub}"' for sub in re.split(r"\s+", p) if sub)
        else:
            ascii_parts.append(p)
    if ascii_parts:
        # 纯 ASCII 段整体成一个短语，不拆 OR
        out.insert(0, '"' + " ".join(ascii_parts) + '"')
    return " OR ".join(out)


# ── sqlite_fts 源：claude-mem ──────────────────────────────────────────

def _ro_connect(db_path: Path, timeout: float = 1.0) -> sqlite3.Connection:
    """只读连接。WAL 库（.db-wal 存在）用普通 ro 打不开时退 immutable=1 只读快照。

    immutable=1 的语义是「我保证这个库不会被并发写」——对本机 claude-mem（低频写）
    代价是可能读到稍旧数据；收益是**绝不要求写权限、绝不参与 WAL 恢复**（军规：
    只读检索不得把外部库弄脏）。两档都失败才报错。
    """
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout)
    except sqlite3.OperationalError:
        return sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=timeout)


def _search_claude_mem(q: str, limit: int) -> Dict:
    t0 = time.monotonic()
    if not _CLAUDE_MEM_DB.is_file():
        return {"ok": False, "count": 0, "items": [], "error": "claude-mem.db 不存在"}
    items: List[dict] = []
    term = _fts_term(q)
    try:
        con = _ro_connect(_CLAUDE_MEM_DB)
        try:
            for r in con.execute(
                "SELECT o.id, o.title, o.subtitle, o.facts, o.type, o.created_at "
                "FROM observations_fts f JOIN observations o ON o.id = f.rowid "
                "WHERE observations_fts MATCH ? ORDER BY o.created_at DESC LIMIT ?",
                (term, limit)):
                items.append({
                    "source": "claude_mem", "id": f"obs:{r[0]}",
                    "title": _sanitize_content(str(r[1] or r[2] or ""))[:120],
                    "content": _sanitize_content(f"{r[1] or ''}｜{r[2] or ''}｜{(r[3] or '')[:400]}"),
                    "category": f"observation/{r[4] or 'discovery'}",
                    "updated_at": r[5],
                })
            if len(items) < limit:
                for r in con.execute(
                    "SELECT s.id, s.request, s.investigated, s.learned, s.created_at "
                    "FROM session_summaries_fts f JOIN session_summaries s ON s.id = f.rowid "
                    "WHERE session_summaries_fts MATCH ? LIMIT ?",
                    (term, limit - len(items))):
                    items.append({
                        "source": "claude_mem", "id": f"sum:{r[0]}",
                        "title": _sanitize_content(str(r[1] or ""))[:120],
                        "content": _sanitize_content(f"{r[1] or ''}｜{r[2] or ''}｜{r[3] or ''}"),
                        "category": "observation/summary",
                        "updated_at": r[4],
                    })
        finally:
            con.close()
        return {"ok": True, "count": len(items), "items": items,
                "ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as e:  # noqa: BLE001 —— 逐路报错，不炸整条联邦链
        return {"ok": False, "count": 0, "items": [],
                "error": tdai_client.scrub(f"{type(e).__name__}: {e}")[:160],
                "ms": round((time.monotonic() - t0) * 1000, 1)}


# ── rg_text 源：pi/codex/工作区/归档 ───────────────────────────────────

def _rg_lines(paths: List[str], glob: Optional[str], q: str,
              cap: int, timeout: float) -> List[str]:
    """一次 rg 子进程拿「文件级命中 + 首个匹配行」。`-m1` 每文件一行、`-l` 不够用
    （拿不到行内容做摘要）、`--max-filesize` 拒超大 jsonl。行输出格式 `path:line:content`。

    用 `-F`（字面匹配）而不是正则：用户查询词里的 `.` `/` `(` 在正则里全是元字符，
    语义漂移还可能炸 pattern 语法错。字面包含也是与 claude-mem FTS 短语一致的口径。
    `--no-ignore --hidden` 必须显式：会话备份/ 在技术文档 .gitignore 里（实测 123 行），
    记忆资产是数据不是 git 工作树对象，尊重 ignore 会系统性漏掉 99.96% 内容
    （实测 2/5350 个文件——verify 闸门 B3 红向抓的就是这个）。\.codex 同样中招。
    """
    cmd = ["rg", "-F", "-i", "-n", "-m", "1", "--no-heading",
           "--no-ignore", "--hidden", "--max-filesize", RG_MAX_FILESIZE]
    if glob:
        cmd += ["-g", glob]
    cmd += ["--", q] + [p for p in paths if os.path.exists(p)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"rg 超时（{timeout}s）")
    if r.returncode not in (0, 1):    # 1=无命中是正常态，不是错误
        raise RuntimeError(f"rg rc={r.returncode}: {r.stderr[:80]}")
    return [ln for ln in (r.stdout or "").splitlines() if ln.strip()][:cap]


def _rg_files(paths: List[str], glob: Optional[str], cap: int,
              timeout: float) -> List[str]:
    """`rg --files` 列候选文件（无关键词）。比 find 快且天然吃 -g/--no-ignore。"""
    cmd = ["rg", "--files", "--no-ignore", "--hidden",
           "--max-filesize", RG_MAX_FILESIZE]
    if glob:
        cmd += ["-g", glob]
    cmd += [p for p in paths if os.path.exists(p)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"rg --files 超时（{timeout}s）")
    if r.returncode not in (0, 1):
        raise RuntimeError(f"rg --files rc={r.returncode}: {r.stderr[:80]}")
    return [ln for ln in (r.stdout or "").splitlines() if ln.strip()][:cap]


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _usable_line(ln: str) -> bool:
    """单行是否有信息量。2026-10-13 实测在三类退化行上加了判据：

    · YAML frontmatter 键（`name: xxx`）——记忆 md 开头常见，零信息；
    · blockquote 元数据（`> Last updated: ...`）——workbuddy 文件开头是它；
    · JSON 碎片（`"pid": 232990,`）——会话 jsonl 截出来的片段。
    这三类混进注入包就是纯噪声（实测都出现过），宁可判少不可判多。
    """
    t = ln.strip()
    if len(t) < 12:
        return False
    if set(t) <= set("-=*_`#~ "):              # 纯分隔符/围栏
        return False
    if t.startswith("#"):                      # markdown 标题：结构不是内容
        return False
    if t.startswith(">"):                      # blockquote 元数据
        return False
    if t.startswith("<!--"):                   # HTML 注释标记
        return False
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*:\s*\S", t) and " " not in t.split(":", 1)[1].strip()[:20]:
        return False                           # `name: value` 形（值短且无空格）≈ frontmatter
    if t.startswith(('"', "{")) and '":' in t:  # JSON 片段
        return False
    return True


def _rg_head_content(path: str, timeout: float) -> Optional[str]:
    """用 rg 取文件**头部若干行**里的第一条有内容行。

    为什么不用 Python `open()`：本模块的只读性由
    `tests/test_memfeed_inject.py::TestInjectionIsReadOnly` 把守，它按字面量
    扫 `open(` / `.write(` 等词——这是**防写入**的必要保守判据，不该为了新功能
    放宽（放宽一次就等于给所有未来的写能力开门）。rg 是只读工具，走它既合规又快。

    `-m 8` 上界：只在头 8 行内找，**不做无界读**——会话 jsonl 可达 21MB，
    实测 `rg -N -m 8 '^'` 在 21MB 上 5ms（读满 8 行即停）。
    跳过 YAML frontmatter（记忆 md 的 `---`…`---` 块是键值元数据，不是内容）。
    """
    cmd = ["rg", "-N", "--no-ignore", "--hidden", "-m", "8",
           "--max-filesize", RG_MAX_FILESIZE, "-e", "^", "--", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode not in (0, 1):
        return None
    lines = (r.stdout or "").splitlines()
    i = 0
    if lines and lines[0].strip() == "---":            # 跳 frontmatter 块
        i = 1
        while i < len(lines) and lines[i].strip() not in ("---", "..."):
            i += 1
        i += 1
    for ln in lines[i:]:
        if _usable_line(ln):
            return f"{path}:{i + 1}:{ln.rstrip()}"
    return None


def _exp_item(r: dict) -> dict:
    """经验行 → 联邦统一 item（形状与 `_rg_item` 一致，RRF/前端才对得上）。"""
    sit = r.get("situation") or ""
    act = r.get("action") or ""
    pre = r.get("precondition") or ""
    content = "%s → %s" % (sit, act)
    if pre:
        content += "（适用：%s）" % pre
    return {
        "source": "experiences", "id": str(r.get("id")),
        "title": _sanitize_content(sit)[:120],
        "content": _sanitize_content(content),
        "category": "experience",
        "updated_at": r.get("created_at"),
    }


def _search_experiences(q: str, limit: int, timeout: float) -> Dict:
    """搜活跃经验（LIKE；退役条不回来）。**降级一律显式报错，不静默返空**。"""
    t0 = time.monotonic()
    try:
        import dataset
        rows = dataset.search_experiences(q, limit=min(limit, RG_ITEM_CAP))
        items = [_exp_item(r) for r in rows]
        return {"ok": True, "count": len(items), "items": items,
                "ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "count": 0, "items": [],
                "error": tdai_client.scrub(f"{type(e).__name__}: {e}")[:160],
                "ms": round((time.monotonic() - t0) * 1000, 1)}


def _recent_experiences(limit: int, timeout: float) -> Dict:
    """取最新活跃经验（`RECENT_Q` 哨兵路径：会话开局无查询词时用）。"""
    t0 = time.monotonic()
    try:
        import dataset
        rows = dataset.list_experiences()[:max(1, limit)]
        items = [_exp_item(r) for r in rows]
        return {"ok": True, "count": len(items), "items": items,
                "ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "count": 0, "items": [],
                "error": tdai_client.scrub(f"{type(e).__name__}: {e}")[:160],
                "ms": round((time.monotonic() - t0) * 1000, 1)}


def _rg_item(source_id: str, line: str) -> Optional[dict]:
    """`path:line:content` → 统一 item。path 用绝对路径（前端直接可溯源），行号做 id。

    `updated_at` 取**实时磁盘 mtime**（`_safe_mtime`），口径与 `_recent_rg`
    原先的 inline 补值完全一致（P1-8 把它收进本函数，两条路径共用一处）。
    检索页按此项做「最近」排序——不补就会与真带时间戳的源（claude_mem 有
    created_at）混排时失序。
    """
    m = re.match(r"^(.*?):(\d+):(.*)$", line, re.S)
    if not m:
        return None
    path, lineno, content = m.group(1), int(m.group(2)), m.group(3)
    name = os.path.basename(path)
    mt = _safe_mtime(path)
    return {
        "source": source_id, "id": f"{path}:{lineno}",
        "title": _sanitize_content(name)[:120],
        "content": _sanitize_content(content),
        "category": "session" if source_id.endswith("sessions") else "knowledge",
        # 取不到 mtime（文件刚被删）⇒ None，不填 0.0：`0` 会被当成
        # 「1970 年」参与排序，把失效条目顶到最新。
        "updated_at": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mt))
                       if mt > 0 else None),
    }


#: A4 投影层（memindex FTS）可加速的三路。**默认行径不含它们**（memory.FED_FAST_SOURCES
#: 只列 local,tdai,claude_mem,workbuddy_memory,claude_projects），所以它们只在调用方
#: 显式 `sources=` 点名时才进 `want` ⇒ 「显式才走 FTS」这条边界由白名单天然保证，
#: 不靠本模块再判一次「是不是显式的」（那样会多出一套真相）。
#: ⚠️ 放量是 A4 之外的独立一步（规格 §八 裁定 1：三路探针各 ≤200ms 且 G13 全绿才提请）。
_PROJ_TARGETS = ("pi_sessions", "codex_sessions", "archived_sessions")

#: 投影库最大年龄（天）。超过就不再用它，改走 rg —— **静默过期比慢更贵**：
#: 2026-10-02 之后 incremental 因 UNIQUE 冲突一直失败，索引静默停了 15 天，
#: 而检索照常返回「看起来完整」的旧答案。此阈值 + 每日增量刷新共同兑付。
MEMINDEX_MAX_AGE_DAYS = float(os.getenv("MEMINDEX_MAX_AGE_DAYS", "3"))


def _search_proj(source_id: str, q: str, limit: int, timeout: float) -> Dict:
    """走 memindex FTS 投影；**任何降级信号一律回退 rg**（fail-closed，绝不静默返空）。

    降级信号一律落在 `fallback` 键上（`short_query` / `no_index` / `low_disk` /
    `corrupt` / `query_error`），不是 `ok=False`——`ok` 描述的是「有没有索引可用」，
    两者不同义，混在一起会让前端分不清「源坏了」和「该走 rg 了」。
    回退时把原因挂到 `proj_fallback` 出站，不静默：否则用户只会看到「还是那么慢」。
    """
    t0 = time.monotonic()
    built = memindex._built_epoch()
    if built is not None:
        age_days = (time.time() - float(built)) / 86400.0
        if age_days > MEMINDEX_MAX_AGE_DAYS:
            out = _search_rg_text(source_id, q, limit, timeout)
            out["proj_fallback"] = "stale"
            out["index_age_days"] = round(age_days, 1)
            return out
    r = memindex.search(q, limit=limit, sanitize=_sanitize_content,
                        sources=(source_id,))
    fb = r.get("fallback")
    if fb:
        out = _search_rg_text(source_id, q, limit, timeout)
        out["proj_fallback"] = fb
        return out
    return r


def _search_rg_text(source_id: str, q: str, limit: int, timeout: float) -> Dict:
    t0 = time.monotonic()
    paths, glob = _RG_TARGETS[source_id]
    if not any(os.path.exists(p) for p in paths):
        return {"ok": False, "count": 0, "items": [], "error": "搜索根不存在"}
    try:
        lines = _rg_lines(paths, glob, q, min(limit, RG_ITEM_CAP), timeout)
        items = [it for it in (_rg_item(source_id, ln) for ln in lines) if it]
        return {"ok": True, "count": len(items), "items": items,
                "ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "count": 0, "items": [],
                "error": tdai_client.scrub(f"{type(e).__name__}: {e}")[:160],
                "ms": round((time.monotonic() - t0) * 1000, 1)}


# ── probe（健康 + 计数，带 TTL 缓存） ─────────────────────────────────

_probe_cache: Dict[str, tuple] = {}      # {sid: (monotonic_ts, result)}


def _probe_one(source_id: str) -> Dict:
    """源健康探针：返回 {ok, count, note, ms}。count=可检索条目/文件数（估计值）。

    rg 类计数用 `rg --files`（比 find 快且天然吃 -g）；claude_mem 用 COUNT(*)。
    计数超时不算源挂（ok 仍 True，note 注明）——「整理」数据缺失不该拖垮「检索」判定。
    """
    t0 = time.monotonic()
    src = REGISTRY[source_id]
    if source_id == "claude_mem":
        if not _CLAUDE_MEM_DB.is_file():
            return {"ok": False, "count": 0, "note": "claude-mem.db 不存在",
                    "ms": round((time.monotonic() - t0) * 1000, 1)}
        try:
            con = _ro_connect(_CLAUDE_MEM_DB, timeout=1.0)
            try:
                n = con.execute("SELECT (SELECT COUNT(*) FROM observations) "
                                "+ (SELECT COUNT(*) FROM session_summaries)").fetchone()[0]
            finally:
                con.close()
            return {"ok": True, "count": int(n), "note": "observations+summaries",
                    "ms": round((time.monotonic() - t0) * 1000, 1)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "count": 0,
                    "note": tdai_client.scrub(f"{type(e).__name__}")[:80],
                    "ms": round((time.monotonic() - t0) * 1000, 1)}
    if source_id == "experiences":
        try:
            import dataset
            n = len(dataset.list_experiences())
            return {"ok": True, "count": int(n), "note": "活跃经验数",
                    "ms": round((time.monotonic() - t0) * 1000, 1)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "count": 0, "note": type(e).__name__,
                    "ms": round((time.monotonic() - t0) * 1000, 1)}
    if source_id in _PROJ_TARGETS:
        # 投影源不跑 `rg --files`（O(文件数) 的老毛病）：读 meta，毫秒级
        p = memindex.probe()
        return {"ok": bool(p.get("ok")), "count": p.get("count", 0),
                "note": "proj:" + str(p.get("note") or p.get("error") or "")[:70],
                "ms": p.get("ms")}
    paths, glob = _RG_TARGETS.get(source_id, ([], None))
    alive = [p for p in paths if os.path.exists(p)]
    if not alive:
        return {"ok": False, "count": 0, "note": "路径不存在",
                "ms": round((time.monotonic() - t0) * 1000, 1)}
    cmd = ["rg", "--files", "--no-ignore", "--hidden",
           "--max-filesize", RG_MAX_FILESIZE]
    if glob:
        cmd += ["-g", glob]
    cmd += alive
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=src.timeout_s)
        n = len([ln for ln in (r.stdout or "").splitlines() if ln.strip()])
        return {"ok": True, "count": n, "note": f"{len(alive)}/{len(paths)} 根存在",
                "ms": round((time.monotonic() - t0) * 1000, 1)}
    except subprocess.TimeoutExpired:
        return {"ok": True, "count": 0, "note": "计数超时（检索不受影响）",
                "ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "count": 0, "note": type(e).__name__,
                "ms": round((time.monotonic() - t0) * 1000, 1)}


# ── 对外入口：并发检索 / 源清单 ───────────────────────────────────────

#: 墙钟超时专用的错误标记。与真故障同路进 backends[]/degraded（不静默），
#: 但字样可区分——「预算不够」和「源炸了」是两种处置，不该混成一句。
SKIPPED_BUDGET = "skipped_budget"

#: 「取各源最新条目」的哨兵查询词（v0.13.103 P0-3，2026-10-13）。
#: 会话起始注入包**没有查询词是常态**（pi 门面就是无 q 调 /api/memory/context）。
#: 旧版此时直接跳过联邦 ⇒ 开局「其他 Agent 记忆」段恒为 0 字符，回包却仍报
#: `fed.sources=N`——「指标绿、功能死」的典型。此哨兵让 rg 侧退化为「扫描目标、
#: 按文件 mtime 取最近条目」，不需要关键词也能给出「最近各 agent 在做什么」。
#: 取一个正常查询不可能出现的串，避免与真实关键词撞车。
RECENT_Q = "__recent__"


def _recent_rg(source_id: str, limit: int, timeout: float) -> Dict:
    """无查询词时取该源**最近修改**的条目（按文件 mtime 降序，行级取文件末尾）。

    只读、预算内：rg --files 拿候选 → mtime 排序 → 每个文件用 rg 抓最后若干非空行。
    mtime 在这里是代用信号（记忆文件/会话 jsonl 都是追加写，末尾最新）。
    这不是「检索」，是「报最新」——所以不参与关键词命中统计，只喂注入包的联邦段。
    """
    t0 = time.monotonic()
    paths, glob = _RG_TARGETS[source_id]
    roots = [p for p in paths if os.path.exists(p)]
    if not roots:
        return {"ok": False, "count": 0, "items": [], "error": "搜索根不存在"}
    try:
        files = _rg_files(roots, glob, limit * 3, timeout)
        if not files:
            return {"ok": True, "count": 0, "items": [],
                    "ms": round((time.monotonic() - t0) * 1000, 1)}
        files = sorted(files, key=lambda p: _safe_mtime(p), reverse=True)[:limit]
        items = []
        for p in files:
            ln = _rg_head_content(p, timeout)
            if ln:
                # `updated_at` 已由 `_rg_item` 从实时 mtime 给出（原为在此 inline
                # 补值，P1-8 收进 `_rg_item`，两条路径共用一处口径）。
                it = _rg_item(source_id, ln)
                if it:
                    items.append(it)
        return {"ok": True, "count": len(items), "items": items,
                "ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "count": 0, "items": [],
                "error": tdai_client.scrub(f"{type(e).__name__}: {e}")[:160],
                "ms": round((time.monotonic() - t0) * 1000, 1)}


def _recent_proj(source_id: str, limit: int, timeout: float) -> Dict:
    """投影源（pi/codex/archived）的「取最新」——**刻意不走投影库，直接扫实时树**。

    理由：投影库是**关键词 FTS**，而「最近」是实时性需求；且本机 memindex.db
    实测已陈旧 11 天（拓扑无自动刷新，见 P0-3 文档）。对「最近各 agent 在做什么」
    这个语义，扫盘面 mtime 比读一个可能停在 10-02 的索引**更正确**。
    投影只在有查询词、要吃 FTS 召回时才值得。
    """
    return _recent_rg(source_id, limit, timeout)


def _recent_claude_mem(limit: int) -> Dict:
    """claude-mem 观察库取最新：复用 _search_claude_mem 的只读连接与脱敏，仅换查询。

    不复用关键词路径是因为它要 FTS MATCH；这里按 created_at 降序直接取。
    """
    t0 = time.monotonic()
    if not _CLAUDE_MEM_DB.is_file():
        return {"ok": False, "count": 0, "items": [], "error": "claude-mem.db 不存在"}
    items: List[dict] = []
    try:
        con = _ro_connect(_CLAUDE_MEM_DB)
        try:
            for r in con.execute(
                "SELECT id, title, subtitle, facts, type, created_at "
                "FROM observations ORDER BY created_at DESC LIMIT ?", (limit,)):
                items.append({
                    "source": "claude_mem", "id": f"obs:{r[0]}",
                    "title": _sanitize_content(str(r[1] or r[2] or ""))[:120],
                    "content": _sanitize_content(f"{r[1] or ''}｜{r[2] or ''}｜{(r[3] or '')[:400]}"),
                    "category": f"observation/{r[4] or 'discovery'}",
                    "updated_at": r[5],
                })
        finally:
            con.close()
        return {"ok": True, "count": len(items), "items": items,
                "ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "count": 0, "items": [],
                "error": tdai_client.scrub(f"{type(e).__name__}: {e}")[:160],
                "ms": round((time.monotonic() - t0) * 1000, 1)}


def _pack(r) -> Dict:
    """单源结果归一：异常也归一路 ok=False（绝不让单源异常掀掉整次检索）。"""
    if isinstance(r, BaseException):
        return {"ok": False, "count": 0, "items": [],
                "error": tdai_client.scrub(f"{type(r).__name__}: {r}")[:160]}
    return r


def _build_tasks(q: str, limit: int, want: set) -> Dict:
    recent = (q == RECENT_Q)          # 无查询词 ⇒ 取各源最新（P0-3）
    tasks = {}
    for sid in want:
        src = REGISTRY.get(sid)
        if not src or not src.enabled:
            continue
        if src.kind == "sqlite_fts":
            if recent:
                tasks[sid] = asyncio.to_thread(_recent_claude_mem, limit)
            else:
                tasks[sid] = asyncio.to_thread(_search_claude_mem, q, limit)
        elif src.kind == "hub_sqlite":
            # hub 自己的库（经验库）：同进程连接，无需 rg/sqlite 外部句柄
            if recent:
                tasks[sid] = asyncio.to_thread(_recent_experiences, limit, src.timeout_s)
            else:
                tasks[sid] = asyncio.to_thread(_search_experiences, q, limit, src.timeout_s)
        elif src.kind == "rg_text":
            if recent:
                # 「取最新」一律扫实时树：投影库是关键词 FTS 且可能陈旧
                tasks[sid] = asyncio.to_thread(_recent_rg, sid, limit, src.timeout_s)
            else:
                # 三路慢源改走投影层（默认行径不含它们，见 _PROJ_TARGETS 注释）
                fn = _search_proj if sid in _PROJ_TARGETS else _search_rg_text
                tasks[sid] = asyncio.to_thread(fn, sid, q, limit, src.timeout_s)
    return tasks


async def search_fed(q: str, limit: int, want: set,
                     wall_s: Optional[float] = None) -> Dict[str, Dict]:
    """并发调各启用源。返回 {source_id: result}；调用方（memory.py）把 ok 的进 RRF。

    同步实现 + asyncio.to_thread 并发：rg/sqlite 都是阻塞 IO，事件循环不能被卡死。
    每源超时由各自实现承担（rg subprocess timeout / sqlite connect timeout），
    这里不再叠一层 wait_for——两层超时会让超时原因变模糊（是 rg 慢还是 gather 慢）。

    `wall_s`（v0.13.62 D1）：**调用方级的墙钟**，与各源自带 timeout 是两层不同语义——
      · `wall_s is None` ⇒ 原 gather 路径，行为与本函数引入以来逐字节一致（既有 27 例 L0 钉着）；
      · 给定值 ⇒ `asyncio.wait(timeout=wall_s)`，预算内完成的照常收集，超预算的记
        `error=SKIPPED_BUDGET`。为什么需要：注入通道坐在会话起始链路上（v2 设计唯一
        被完整采纳的约束），而实测最慢三路 rg 要 1.9~2.5s、archived 要扫 5622 个文件，
        没有墙钟就等于把开局卡死在记忆检索后面。

    超预算者**不取消**：`asyncio.to_thread` 不可取消，线程会按各源自带 timeout 自行收尾；
    这里只给它们挂一个 done_callback 取走异常引用，否则 asyncio 会在 GC 时抛
    `exception was never retrieved` 刷屏（假故障）。
    """
    tasks = _build_tasks(q, limit, want)
    if not tasks:
        return {}
    keys = list(tasks.keys())
    if wall_s is None:
        results = await asyncio.gather(*[tasks[k] for k in keys], return_exceptions=True)
        return {k: _pack(r) for k, r in zip(keys, results)}

    futs = {k: asyncio.ensure_future(tasks[k]) for k in keys}
    await asyncio.wait(futs.values(), timeout=wall_s)
    out: Dict[str, Dict] = {}
    for k, f in futs.items():
        if f.done():
            # 必须取 f.result()：f.exception() 在「没抛异常」时返回 None，
            # 而 None 会一路冒充成「这一路没跑」（2026-10-02 首版就踩了这个，
            # 被 tests/test_memfeed_inject.py 的 G1/G3 当场抓住）。
            try:
                r = f.result()
            except BaseException as e:      # 单源异常归一路，不掀掉整次检索
                r = e
            out[k] = _pack(r)
        else:
            # 不取消（to_thread 不可取消），但必须取走异常引用，否则 GC 时 asyncio
            # 会抛 `exception was never retrieved` 刷屏——那是假故障。
            f.add_done_callback(lambda ff: None if ff.cancelled() else ff.exception())
            out[k] = {"ok": False, "count": 0, "items": [],
                      "error": SKIPPED_BUDGET, "ms": None}
    return out


@router.get("/api/memory/fedsources")
async def list_fed_sources():
    """联邦源清单 + 逐源健康/计数（前端「整理分类」面板的数据源）。probe 带 TTL 缓存。"""
    now = time.monotonic()
    fresh: Dict[str, Dict] = {}
    stale: List[str] = []
    for sid, src in REGISTRY.items():
        hit = _probe_cache.get(sid)
        if hit and (now - hit[0]) < PROBE_TTL_S:
            fresh[sid] = hit[1]
        else:
            stale.append(sid)
    if stale:
        probed = await asyncio.gather(
            *[asyncio.to_thread(_probe_one, sid) for sid in stale],
            return_exceptions=True)
        for sid, r in zip(stale, probed):
            if isinstance(r, BaseException):
                r = {"ok": False, "count": 0, "note": type(r).__name__, "ms": None}
            _probe_cache[sid] = (now, r)
            fresh[sid] = r
    sources = []
    for sid, src in REGISTRY.items():
        p = fresh.get(sid) or {}
        sources.append({
            "id": sid, "label": src.label, "kind": src.kind, "enabled": src.enabled,
            "weight": src.weight, "desc": src.desc,
            "probe": {"ok": bool(p.get("ok")), "count": p.get("count"),
                      "note": p.get("note"), "ms": p.get("ms")},
        })
    return {"sources": sources, "count": len(sources),
            "enabled": [s["id"] for s in sources if s["enabled"]]}


def fed_weight(sid: str) -> float:
    return REGISTRY[sid].weight
