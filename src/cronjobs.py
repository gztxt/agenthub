"""定时任务引擎（部署方案 S4：croniter tick + shell/agent_prompt/http 三类）

安全边界（相对文档的有意收敛）：
- shell 类命令走 argv[0] 白名单（JOB_SHELL_ALLOW），杜绝 Web 面上任意执行
- 不写 /etc/cron.d（NAS 军规），纯应用层 tick（20s 轮询，本地时区语义）
- 每次执行留痕 jobs.last_* + profile_events(cron_run)
"""
import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from croniter import croniter
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import db

log = logging.getLogger(__name__)

router = APIRouter()

SHELL_ALLOW = {x.strip() for x in os.getenv(
    "JOB_SHELL_ALLOW", "echo,date,uptime,free,df,ls,whoami").split(",") if x.strip()}
SHELL_TIMEOUT = 120
TICK_S = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    cron TEXT NOT NULL,
    kind TEXT NOT NULL,               -- shell|agent_prompt|http
    payload TEXT NOT NULL,
    agent_id TEXT,
    enabled INTEGER DEFAULT 1,
    last_run TEXT, last_status TEXT, last_result TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""

_ctx = {}
_engine_task = None


def set_context(chat_fn=None):
    if chat_fn:
        _ctx["chat_fn"] = chat_fn


def ensure_schema():
    db.execute_script(SCHEMA)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    cron: str = Field(min_length=9, max_length=100)
    kind: str = "shell"               # shell|agent_prompt|http
    payload: str = Field(min_length=1, max_length=4000)
    agent_id: Optional[str] = None
    enabled: bool = True


@router.post("/api/jobs")
async def create_job(body: JobIn):
    if body.kind not in ("shell", "agent_prompt", "http"):
        raise HTTPException(400, "kind 仅支持 shell|agent_prompt|http")
    if not croniter.is_valid(body.cron):
        raise HTTPException(400, f"非法 cron: {body.cron}")
    if body.kind == "shell":
        try:
            argv = shlex.split(body.payload)
        except ValueError as e:
            raise HTTPException(400, f"命令解析失败: {e}")
        if not argv or os.path.basename(argv[0]) not in SHELL_ALLOW:
            raise HTTPException(400,
                                f"shell 白名单拒绝（允许: {sorted(SHELL_ALLOW)}；可用 JOB_SHELL_ALLOW 扩展）")
    jid = f"{int(time.time()*1000):x}"
    now = _now_iso()
    db.execute(
        "INSERT INTO jobs(id,name,cron,kind,payload,agent_id,enabled,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (jid, body.name, body.cron, body.kind, body.payload, body.agent_id,
         1 if body.enabled else 0, now, now))
    return {"id": jid, "status": "created"}


@router.get("/api/jobs")
async def list_jobs():
    return {"jobs": db.query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100"),
            "shell_allow": sorted(SHELL_ALLOW)}


class JobPatch(BaseModel):
    enabled: Optional[bool] = None
    cron: Optional[str] = None
    name: Optional[str] = None


@router.put("/api/jobs/{jid}")
async def patch_job(jid: str, body: JobPatch):
    sets, params = [], []
    if body.enabled is not None:
        sets.append("enabled=?")
        params.append(1 if body.enabled else 0)
    if body.cron:
        if not croniter.is_valid(body.cron):
            raise HTTPException(400, "非法 cron")
        sets.append("cron=?")
        params.append(body.cron)
    if body.name:
        sets.append("name=?")
        params.append(body.name)
    if not sets:
        raise HTTPException(400, "无字段更新")
    sets.append("updated_at=?")
    params += [_now_iso(), jid]
    if not db.execute(f"UPDATE jobs SET {','.join(sets)} WHERE id=?", tuple(params)):
        raise HTTPException(404, "job not found")
    return {"status": "updated"}


@router.delete("/api/jobs/{jid}")
async def delete_job(jid: str):
    if not db.execute("DELETE FROM jobs WHERE id=?", (jid,)):
        raise HTTPException(404, "job not found")
    return {"status": "deleted"}


@router.post("/api/jobs/{jid}/run")
async def run_job_now(jid: str):
    row = db.query("SELECT * FROM jobs WHERE id=?", (jid,))
    if not row:
        raise HTTPException(404, "job not found")
    await _fire(row[0])
    after = db.query("SELECT last_status,last_result FROM jobs WHERE id=?", (jid,))[0]
    return {"status": after["last_status"], "result": after["last_result"]}


async def _fire(job: dict) -> None:
    jid, kind, payload = job["id"], job["kind"], job["payload"]
    t0 = time.monotonic()
    status, result = "fail", None
    try:
        if kind == "shell":
            argv = shlex.split(payload)
            if os.path.basename(argv[0]) not in SHELL_ALLOW:
                result = f"白名单拒绝: {argv[0]}"
            else:
                proc = await asyncio.to_thread(
                    subprocess.run, argv, capture_output=True, text=True,
                    timeout=SHELL_TIMEOUT)
                ok = proc.returncode == 0
                status = "success" if ok else "fail"
                result = (proc.stdout or proc.stderr)[:1000]
        elif kind == "agent_prompt":
            agent = job.get("agent_id") or "jcode"
            chat_fn = _ctx.get("chat_fn")
            if not chat_fn:
                result = "chat 引擎未就绪"
            else:
                r = await chat_fn(agent, payload, f"cron-{jid}-{int(t0)}")
                status = "success" if r.get("success") else "fail"
                result = (r.get("response") or r.get("error"))[:1000]
        elif kind == "http":
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    status = "success" if resp.status < 400 else "fail"
                    result = f"HTTP {resp.status}"
    except subprocess.TimeoutExpired:
        result = f"执行超时 {SHELL_TIMEOUT}s"
    except Exception as e:  # noqa: BLE001
        result = repr(e)[:500]
    db.execute("UPDATE jobs SET last_run=?, last_status=?, last_result=?, updated_at=? WHERE id=?",
               (_now_iso(), status, result, _now_iso(), jid))
    db.log_profile_event("cron_run", jid, status,
                         int((time.monotonic() - t0) * 1000),
                         detail={"kind": kind, "name": job.get("name")})


async def _tick_loop():
    """本地时区语义的分钟级 tick：last_run 早于本分钟窗口内应有触发点则执行"""
    while True:
        try:
            now = datetime.now()
            due = []
            for job in db.query("SELECT * FROM jobs WHERE enabled=1"):
                try:
                    base = (datetime.fromisoformat(job["last_run"])
                            .astimezone().replace(tzinfo=None)
                            if job["last_run"]
                            else now.replace(second=0, microsecond=0))
                    nxt = croniter(job["cron"], base).get_next(datetime)
                    if nxt <= now:
                        due.append(job)
                except Exception:  # noqa: BLE001
                    continue
            # 并发触发 + 异常隔离：串行 await 时一个 job 抛错会拖住本轮其余 job 的调度，
            # 且异常被 continue 吞掉、无日志，事后无从判断是哪个 job 没跑。
            if due:
                res = await asyncio.gather(*(_fire(j) for j in due), return_exceptions=True)
                for job, r in zip(due, res):
                    if isinstance(r, BaseException):
                        log.warning(f"[cron] job {job.get('name') or job.get('id')} 触发失败: {r!r}")
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(TICK_S)


def start_engine():
    global _engine_task
    if _engine_task is None or _engine_task.done():
        _engine_task = asyncio.create_task(_tick_loop())
