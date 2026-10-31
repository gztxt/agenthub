"""参考情报的**长期知识库**（2026-10-31，用户裁定「已检索过的摘要要能当下次参考」）。

## 为什么要有这张表（问题不是缓存命中，是「重复做功」）

三层缓存原来各管一段，但**没有一层记住「这个仓长什么样」**：

| 层 | 管什么 | 记住什么 | 不管什么 |
|---|---|---|---|
| `gh_get` 磁盘/库缓存 | HTTP 响应（搜索 7 天、补全 1 天） | 原始 JSON | 打分、摘要、判定 |
| 卡表 `ref_cards` | 一次检索的**结论** | 该 module 的候选表 + md | 换个 module 再来一次就全丢 |
| 查询级缓存（`REF_QUERY_TTL_H`） | 同一组参数 | 整张卡 | 参数一变就重跑 |

结果：`派发` 查一次、`任务调度` 再查一次，同一批仓库会被**重新拉 README、重新算三轴、重新喂 LLM 重排**
——用户侧看到的是「每次都全量检索，慢且烧钱」。

本表把「仓库本身」抽出来单独记：简介、topics、许可证、三轴分、人工/LLM 判定、README 摘要、
最近一次看到的模块与来源。下一轮检索先用它**垫池**，命中就不必再抓一遍。

## 与「安装测试」的关系（用户同批要求，此处只做**候选标记**）

判据是机械的：`judge ∈ {use, read}` + 非 copyleft + 非 archived + `install_state == ''`
⇒ 标 `candidate`，由 `/api/ref/kb?install=candidate` 列出。
**不自动安装**：装代码是写动作，按工作区军规必须由用户点名，本模块只负责把「够格的」挑出来。

## 口径

- 主键 = `full_name` 小写（GitHub 的 full_name 大小写不敏感，大小写不同会造成同一仓两条记录）。
- `axes`/`topics`/`sources`/`modules` 存 JSON 字符串：SQLite 没有数组类型，别硬拆成多列。
- 只做 upsert（`seen_n` 累加、`last_seen` 刷新），**不删**：删了下次又要重新抓。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import db

#: 知识库垫池的条数上限（越多越省网络，但打分池太大反而稀释重排信号）
KB_SEED_LIMIT = int(os.getenv('REF_KB_SEED_LIMIT', '120'))
#: 判定可用于安装测试的判据
INSTALLABLE_VERDICTS = ('use', 'read')
#: 低于这个 star 的不进安装候选（噪声太多，且不值得占验证时间）
INSTALL_MIN_STARS = int(os.getenv('REF_KB_INSTALL_MIN_STARS', '50'))

SCHEMA = """
CREATE TABLE IF NOT EXISTS ref_repo_kb (
    repo         TEXT PRIMARY KEY,          -- full_name 小写
    full_name    TEXT NOT NULL,
    url          TEXT,
    descr        TEXT,
    topics       TEXT,                      -- JSON array
    stars        INTEGER NOT NULL DEFAULT 0,
    license      TEXT,
    copyleft     INTEGER NOT NULL DEFAULT 0,
    archived     INTEGER NOT NULL DEFAULT 0,
    branch       TEXT,
    head         TEXT,
    pushed_at    TEXT,
    readme_gist  TEXT,
    axes         TEXT,                      -- JSON: {func,impl,design,coverage}
    verdict      TEXT,                      -- use|read|skip|unjudged
    why          TEXT,
    install_state TEXT NOT NULL DEFAULT '', -- ''|candidate|installed|tested|rejected
    sources      TEXT,                      -- JSON array（来自哪些 ref_sources 标签）
    modules      TEXT,                      -- JSON array（哪些检索模块命中过）
    seen_n       INTEGER NOT NULL DEFAULT 1,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ref_kb_verdict ON ref_repo_kb(verdict);
CREATE INDEX IF NOT EXISTS idx_ref_kb_install ON ref_repo_kb(install_state);
CREATE INDEX IF NOT EXISTS idx_ref_kb_stars ON ref_repo_kb(stars);
"""


def ensure_schema() -> None:
    db.execute_script(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def tokens(text: str) -> List[str]:
    """检索用的词元：小写、≥3 字符、去重、按长度降序（长词更有区分度）。

    纯函数，L0 可直测。中文不切词：GitHub 的 name/desc 以英文为主，中文 module 名
    在知识库里靠 `modules` 字段（JSON 数组精确匹配）兜住。
    """
    toks = [t for t in re.split(r'[^0-9a-zA-Z_+#.-]+', (text or '').lower()) if len(t) >= 3]
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return sorted(out, key=lambda x: (-len(x), x))


def _like_escape(s: str) -> str:
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def seed_sql(toks: Iterable[str], limit: int = 0) -> tuple:
    """给定词元，返回 (sql, params)：按相关度粗排的知识库命中。

    粗排只用于**决定拿哪些仓垫池**，最终排序仍由 `build_pre` 的三轴分决定 ——
    这里刻意不复制打分层（两份打分口径必然漂移，判例 296 同族）。
    """
    toks = [t for t in toks][:8]
    limit = limit or KB_SEED_LIMIT
    if not toks:
        return ('SELECT * FROM ref_repo_kb ORDER BY stars DESC LIMIT ?', (limit,))
    conds, params = [], []
    for t in toks:
        like = '%' + _like_escape(t) + '%'
        conds.append('(lower(full_name) LIKE ? ESCAPE \'\\\' OR lower(descr) LIKE ? ESCAPE \'\\\' '
                     'OR lower(topics) LIKE ? ESCAPE \'\\\' OR lower(readme_gist) LIKE ? ESCAPE \'\\\')')
        params += [like, like, like, like]
    sql = ('SELECT * FROM ref_repo_kb WHERE ' + ' OR '.join(conds) +
           ' ORDER BY seen_n DESC, stars DESC LIMIT ?')
    params.append(limit)
    return (sql, tuple(params))


def seed_rows(module: str, keywords: List[str], limit: int = 0) -> List[Dict[str, Any]]:
    """读路径：拿知识库里与本次 module/keywords 相关的仓，供检索**垫池**。"""
    toks = tokens(' '.join([module or ''] + list(keywords or [])))
    sql, params = seed_sql(toks, limit)
    rows = db.query(sql, params)
    for r in rows:
        for k in ('topics', 'axes', 'sources', 'modules'):
            try:
                r[k] = json.loads(r.get(k) or '[]')
            except Exception:  # noqa: BLE001 —— 脏数据不该让检索整卡失败
                r[k] = [] if k != 'axes' else {}
    return rows


def _axes(c: Dict[str, Any]) -> Dict[str, Any]:
    return {'func': c.get('func'), 'impl': c.get('impl'),
            'design': c.get('design'), 'coverage': c.get('coverage')}


def install_worthy(row: Dict[str, Any], min_stars: int = 0) -> bool:
    """够不够格进「安装测试」候选（纯判据，L0 可直测）。

    四条同时成立：判定是 use/read、非 copyleft、非 archived、star 达标。
    `install_state` 非空表示人已经处理过（装了/测了/否了），不再重复推荐。
    """
    min_stars = min_stars or INSTALL_MIN_STARS
    if (row.get('install_state') or '') != '':
        return False
    if (row.get('verdict') or '') not in INSTALLABLE_VERDICTS:
        return False
    if int(row.get('copyleft') or 0) == 1 or int(row.get('archived') or 0) == 1:
        return False
    return int(row.get('stars') or 0) >= min_stars


def upsert(candidates: List[Dict[str, Any]], module: str,
           keywords: Optional[List[str]] = None) -> int:
    """写路径：把本轮候选**写穿**进知识库（幂等；`seen_n` 累加，`last_seen` 刷新）。"""
    if not candidates:
        return 0
    now = _now()
    n = 0
    for c in candidates:
        full = str(c.get('repo') or '').strip()
        if not full:
            continue
        repo = full.lower()
        row = db.query('SELECT seen_n, sources, modules, verdict, why, install_state '
                       'FROM ref_repo_kb WHERE repo=?', (repo,))
        prev = row[0] if row else {}
        try:
            srcs = sorted(set(json.loads(prev.get('sources') or '[]')) | set(c.get('sources') or []))
        except Exception:  # noqa: BLE001
            srcs = sorted(set(c.get('sources') or []))
        try:
            mods = sorted(set(json.loads(prev.get('modules') or '[]')) | {module})
        except Exception:  # noqa: BLE001
            mods = [module]
        verdict = c.get('judge') or prev.get('verdict') or ''
        why = c.get('why') or prev.get('why') or ''
        # 人工已处理过的状态不能被新一轮自动覆盖（否则「已测试」会被悄悄改回 candidate）
        state = prev.get('install_state') or ''
        db.execute(
            'INSERT INTO ref_repo_kb(repo,full_name,url,descr,topics,stars,license,copyleft,archived,'
            'branch,head,pushed_at,readme_gist,axes,verdict,why,install_state,sources,modules,'
            'seen_n,first_seen,last_seen) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?) '
            'ON CONFLICT(repo) DO UPDATE SET '
            'descr=excluded.descr, topics=excluded.topics, stars=excluded.stars, '
            'license=excluded.license, copyleft=excluded.copyleft, archived=excluded.archived, '
            'branch=COALESCE(NULLIF(excluded.branch,\'\'), ref_repo_kb.branch), '
            'head=COALESCE(NULLIF(excluded.head,\'\'), ref_repo_kb.head), '
            'pushed_at=excluded.pushed_at, readme_gist=excluded.readme_gist, axes=excluded.axes, '
            'verdict=excluded.verdict, why=excluded.why, sources=excluded.sources, '
            'modules=excluded.modules, seen_n=ref_repo_kb.seen_n+1, last_seen=excluded.last_seen',
            (repo, full, c.get('url'), (c.get('desc') or '')[:400],
             json.dumps((c.get('topics') or [])[:12], ensure_ascii=False), int(c.get('stars') or 0),
             c.get('license'), 1 if c.get('copyleft') else 0, 1 if c.get('archived') else 0,
             c.get('branch'), c.get('head'), c.get('pushed_at'),
             (c.get('readme_gist') or '')[:4000],
             json.dumps(_axes(c), ensure_ascii=False), verdict, (why or '')[:400],
             state, json.dumps(srcs, ensure_ascii=False), json.dumps(mods, ensure_ascii=False),
             now, now))
        n += 1
    return n


def promote_installable(min_stars: int = 0) -> int:
    """把够格的标成 `candidate`（**只标记**，不安装）。返回新标记条数。"""
    min_stars = min_stars or INSTALL_MIN_STARS
    marks = ','.join("'%s'" % v for v in INSTALLABLE_VERDICTS)
    rows = db.query(
        f'SELECT repo FROM ref_repo_kb WHERE install_state=\'\' AND verdict IN ({marks}) '
        'AND copyleft=0 AND archived=0 AND stars>=?', (min_stars,))
    for r in rows:
        db.execute("UPDATE ref_repo_kb SET install_state='candidate' WHERE repo=?", (r['repo'],))
    return len(rows)


def install_candidates(limit: int = 50) -> Dict[str, Any]:
    rows = db.query("SELECT repo,full_name,url,descr,stars,license,verdict,why,axes,last_seen "
                    "FROM ref_repo_kb WHERE install_state='candidate' "
                    "ORDER BY stars DESC LIMIT ?", (limit,))
    for r in rows:
        try:
            r['axes'] = json.loads(r.get('axes') or '{}')
        except Exception:  # noqa: BLE001
            r['axes'] = {}
    return {'items': rows, 'total': len(rows), 'min_stars': INSTALL_MIN_STARS}


def stats() -> Dict[str, Any]:
    total = (db.query('SELECT COUNT(*) AS n FROM ref_repo_kb') or [{'n': 0}])[0]['n']
    by_state = db.query('SELECT install_state AS s, COUNT(*) AS n FROM ref_repo_kb GROUP BY install_state')
    by_verdict = db.query('SELECT verdict AS v, COUNT(*) AS n FROM ref_repo_kb GROUP BY verdict')
    return {'total': total, 'by_install_state': by_state, 'by_verdict': by_verdict}
