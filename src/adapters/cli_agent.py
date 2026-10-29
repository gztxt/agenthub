"""CLI Agent 适配器：一次子任务 = 一个**无头进程**。

为什么需要（2026-10-29 实测）：派发池此前只有 claude/jcode 两个 OpenAI 兼容端点，
`chat()` 只发 messages、不带 `tools` ⇒ 文件/命令类子任务只能**编造**，而文本层
verifier 判不出来（实测 `/etc/hostname` 真值 `zzst`，claude 回 `AGNES-2.5-FLASH`
且 `verdict=pass`）。这些 CLI 自带工具环，是唯一能把这类活干完的执行者。

判据（每条都有实测依据，改之前先看）：
  ① **一进程一任务**，stdout 即产物；不做 `--resume` 续接 —— 子任务之间本不该共享上下文，
     续接还会把上一次的失败上下文带进重试。
  ② 超时由调用方给（`timeout_s`），到点杀**整个进程组**：这些 CLI 都会派生子进程，
     只 kill 父进程会留孤儿继续烧 token。
  ③ **空输出 = 失败**。`pi` 的默认模型实测返回空响应（退出码 0），记 success 就是
     又一次假绿 —— 与 `openai_compat` 的 `nonempty` 判据同口径。
  ④ 二进制找不到 ⇒ `available()` 为假 ⇒ registry 不把它放进派发池（运行期才炸太贵）。
  ⑤ `model_path` 分两档：`ccr` 的 CLI 直接吃 CCR 模型 ID（实测 claude/pi 生效）；
     `native` 的（codebuddy/cursor）模型域是自己的厂商清单，塞 CCR ID 必报
     `unknown model` ⇒ **不透传**，只记 `model="native"`。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseAdapter

#: dsh 的 headless 需要 provider 凭据；与 `systemctl --user cat dsh` 的
#: `EnvironmentFile=` 同一来源（同一台机、同一个用户，不另存一份）。
DSH_ENV_FILE = "/fs/1000/ftp/技术文档/dsh-src/.env"

#: pi 的默认模型（`ccr-free` → `deepseek/deepseek-flash`）实测**返回空响应**，
#: 必须显式给一个可用模型才出话。默认值与 dsh/claude 当前生产口径一致。
PI_DEFAULT_MODEL = os.getenv("TASK_CLI_MODEL", "agnes/agnes-2.5-flash")


# ── argv 构造（每个 CLI 一行，便于逐条核对）────────────────────────────

def _argv_claude(p: str, m: Optional[str], x: List[str] = ()) -> List[str]:
    return ["-p"] + list(x) + [p, "--output-format", "text"] + (["--model", m] if m else [])


def _argv_codex(p: str, m: Optional[str], x: List[str] = ()) -> List[str]:
    # --json 出 NDJSON：能同时拿到正文与 usage，省掉解析横幅
    a = ["exec", "--skip-git-repo-check", "--json"] + list(x)
    return a + (["-m", m] if m else []) + [p]


def _argv_dsh(p: str, m: Optional[str], x: List[str] = ()) -> List[str]:
    # 模型在 profile 的 `agent-default-model` 里，无 CLI 开关
    return list(x) + ["headless", p]


def _argv_pi(p: str, m: Optional[str], x: List[str] = ()) -> List[str]:
    return ["-p"] + list(x) + (["--model", m] if m else []) + [p]


def _argv_codebuddy(p: str, m: Optional[str], x: List[str] = ()) -> List[str]:
    a = ["-p", p, "--output-format", "text"] + list(x)
    return a + (["--model", m] if m else [])


def _argv_grok(p: str, m: Optional[str], x: List[str] = ()) -> List[str]:
    return ["-p", p] + list(x) + (["--model", m] if m else [])


def _argv_hermes(p: str, m: Optional[str], x: List[str] = ()) -> List[str]:
    return ["-z", p] + list(x) + (["--model", m] if m else [])


def _argv_omp(p: str, m: Optional[str], x: List[str] = ()) -> List[str]:
    return ["-p", p] + list(x) + (["--model", m] if m else [])


def _argv_cursor(p: str, m: Optional[str], x: List[str] = ()) -> List[str]:
    return ["-p", p] + list(x) + (["--model", m] if m else [])


# ── 输出清洗 ─────────────────────────────────────────────────────────

_PI_BANNER = re.compile(
    r"^\[(?:auto-plan|mem|empty-response-guard|hub-facade|security-guard|summary-owner|wigolo)\]")


def _clean_text(out: str) -> Tuple[str, Optional[dict]]:
    return out.strip(), None


def _clean_codex(out: str) -> Tuple[str, Optional[dict]]:
    """NDJSON：正文在 `item.completed/agent_message`，用量在 `turn.completed/usage`。"""
    texts: List[str] = []
    usage: Optional[dict] = None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "item.completed":
            it = ev.get("item") or {}
            if it.get("type") == "agent_message" and it.get("text"):
                texts.append(it["text"])
        elif ev.get("type") == "turn.completed":
            u = ev.get("usage") or {}
            if u.get("output_tokens") is not None:
                usage = {"prompt_tokens": u.get("input_tokens"),
                         "completion_tokens": u.get("output_tokens"),
                         "total_tokens": (u.get("input_tokens") or 0)
                         + (u.get("output_tokens") or 0)}
    if not texts:   # 非 JSON 尾部（旧版 codex）回落纯文本
        return out.strip(), usage
    return "\n".join(texts).strip(), usage


def _clean_pi(out: str) -> Tuple[str, Optional[dict]]:
    lines = [ln for ln in out.splitlines() if not _PI_BANNER.match(ln)]
    return "\n".join(lines).strip(), None


def _clean_omp(out: str) -> Tuple[str, Optional[dict]]:
    lines = [ln for ln in out.splitlines() if ln.strip() != "Working..."]
    return "\n".join(lines).strip(), None


def _clean_dsh(out: str) -> Tuple[str, Optional[dict]]:
    """`dsh headless` 会先打 `dsh: reasoning:` 一段推理，再空两行给正文。"""
    s = out.strip()
    if s.startswith("dsh: reasoning:"):
        parts = re.split(r"\n\s*\n", s, maxsplit=1)
        s = parts[1].strip() if len(parts) > 1 else ""
    return s, None


# ── 规格表 ───────────────────────────────────────────────────────────

SPECS: Dict[str, Dict[str, Any]] = {
    # 注意 id 是 `claude-code` 不是 `claude`：`claude` 这个 id 在 registry 里
    # 早已是 CCR 的 OpenAI 兼容端点（对话页/设置页都在用），改名会静默改变既有语义。
    # 想用带工具环的 Claude Code 就选这个新 id。
    "claude-code": {"bin": "claude", "argv": _argv_claude, "clean": _clean_text,
                    "model_path": "ccr", "approve": ("--permission-mode", "bypassPermissions"),
                    "label": "Claude Code（-p）"},
    "codex": {"bin": "codex", "argv": _argv_codex, "clean": _clean_codex,
              "model_path": "ccr", "approve": ("-s", "workspace-write"),
              "label": "Codex CLI（exec --json）"},
    "dsh": {"bin": "dsh", "argv": _argv_dsh, "clean": _clean_dsh,
            "model_path": None, "label": "DeepSeek Harness（headless）",
            "env_file": DSH_ENV_FILE, "env_keys": ("DEEPSEEK_API_KEY",)},
    "pi": {"bin": "pi", "argv": _argv_pi, "clean": _clean_pi,
           "model_path": "ccr", "model_fallback": PI_DEFAULT_MODEL,
           "label": "pi（-p）"},
    "codebuddy": {"bin": "/opt/WorkBuddy/resources/app.asar.unpacked/cli/bin/codebuddy",
                  "argv": _argv_codebuddy, "clean": _clean_text,
                  "model_path": "native", "approve": ("-y",),
                  "label": "CodeBuddy CLI（-p）"},
    "grok": {"bin": "grok", "argv": _argv_grok, "clean": _clean_text,
             "model_path": "ccr", "approve": ("--permission-mode", "bypassPermissions"),
             "label": "grok（-p）"},
    "hermes": {"bin": "hermes", "argv": _argv_hermes, "clean": _clean_text,
               "model_path": "ccr", "approve": ("--yolo",),
               "label": "hermes（-z）"},
    "omp": {"bin": "omp", "argv": _argv_omp, "clean": _clean_omp,
            "model_path": "ccr", "approve": ("--auto-approve",),
            "label": "omp / oh-my-pi（-p）"},
    "cursor": {"bin": "cursor-agent", "argv": _argv_cursor, "clean": _clean_text,
               "model_path": "native", "approve": ("--force",),
               "label": "Cursor Agent（-p）"},
}

#: 默认工作目录：与终端入口/交接口令一致（这些 CLI 都以 cwd 为工作根）
DEFAULT_CWD = "/fs/1000/ftp/技术文档"

#: 非交互模式下，CLI 遇到工具调用会**停下来等审批**并以退出码 0 结束
#: （2026-10-29 实测 claude-code：输出「请允许以下权限…」→ verifier 判 pass ⇒
#:  什么都没干却记成功）。两条处置缺一不可：
#:   ① 起进程时带各家的免审批旗标（下表 `approve`），让工具真的能跑；
#:   ② 万一没跑起来，把这些"在要权限"的说法判成**失败**，不许静默 pass。
_YOLO_OFF = ("0", "false", "no", "off")


def _yolo_enabled() -> bool:
    """`TASK_CLI_YOLO=0` 可关掉免审批（关掉后 CLI 只会要求授权 ⇒ 任务显式失败）。"""
    return os.getenv("TASK_CLI_YOLO", "1").strip().lower() not in _YOLO_OFF


def _approve_flags(agent_id: str) -> List[str]:
    if not _yolo_enabled():
        return []
    return list(SPECS[agent_id].get("approve") or ())


_NEEDS_APPROVAL = re.compile(
    r"(请允许(?:以下|我)?(?:权限|执行|读取|使用)|需要(?:您|你)?(?:批准|授权)"
    r"|等待(?:权限|批准)|approval required|waiting for (?:approval|permission)"
    r"|would you like (?:me )?to (?:proceed|allow))", re.I)


def _read_env_file(path: str, keys: Tuple[str, ...]) -> Dict[str, str]:
    """只取 `keys` 里的键。整份吞进来会把无关凭据带进子进程（放大面）。"""
    out: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k in keys:
                    out[k] = v.strip().strip('"').strip("'")
    except OSError:
        return out
    return out


def _kill_group(proc) -> None:
    """杀整个进程组：CLI 会派生 node/bun 子进程，只 kill 父进程会留孤儿。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


class CliAgentAdapter(BaseAdapter):
    """按 SPECS 把一个 agent id 变成一次无头 CLI 调用。"""

    def __init__(self, config, agent_id: str):
        super().__init__(config)
        self.agent_id = agent_id
        self.spec = SPECS[agent_id]
        self.display_name = self.spec.get("label") or agent_id
        self._bin: Optional[str] = None

    # ── 解析与自检 ──────────────────────────────────────────────────
    def bin_path(self) -> Optional[str]:
        if self._bin:
            return self._bin
        raw = self.spec["bin"]
        if os.path.isabs(raw):
            self._bin = raw if os.access(raw, os.X_OK) else None
            return self._bin
        try:
            from profiles import which as _which   # 延迟导入：adapters 先于 profiles 加载
            self._bin = _which(raw)
        except Exception:  # noqa: BLE001
            self._bin = None
        return self._bin

    def env(self) -> Dict[str, str]:
        try:
            from profiles import cli_env
            env = cli_env()
        except Exception:  # noqa: BLE001
            env = dict(os.environ)
        keys = tuple(self.spec.get("env_keys") or ())
        if keys and self.spec.get("env_file"):
            missing = [k for k in keys if not env.get(k)]
            if missing:
                env.update(_read_env_file(self.spec["env_file"], keys))
        return env

    def available(self) -> Tuple[bool, str]:
        """(能否派发, 原因)。原因要能直接给人看 —— 池子里少了谁必须说得出口。"""
        if not self.bin_path():
            return False, "binary_not_found: %s" % self.spec["bin"]
        keys = tuple(self.spec.get("env_keys") or ())
        if keys:
            env = self.env()
            miss = [k for k in keys if not env.get(k)]
            if miss:
                return False, "missing_env: %s（来源 %s）" % (",".join(miss),
                                                             self.spec.get("env_file") or "环境")
        return True, ""

    def resolve_model(self, requested: Optional[str]) -> Optional[str]:
        """要注入给 CLI 的模型；None = **不要**注入（用 CLI 自己的配置）。

        `native` 也返回 None：Leader 的 `model` 字段是拿 CCR 清单校验过的，
        塞给 codebuddy/cursor 必报 unknown model —— 这两个的模型只能在它们自己的
        配置文件里改。**"设了但不生效"要能在库里看出来**：`model` 列写 `native`。
        """
        if self.spec.get("model_path") not in ("ccr",):
            return None
        m = (requested or "").strip()
        if m:
            return m
        return self.spec.get("model_fallback") or None

    # ── 对话 ────────────────────────────────────────────────────────
    async def chat(self, message: str, session_id: Optional[str] = None,
                   model: Optional[str] = None, history: Optional[list] = None,
                   cwd: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        ok, why = self.available()
        if not ok:
            return {"success": False, "agent": self.agent_id, "error": why}
        if not (message or "").strip():
            return {"success": False, "agent": self.agent_id, "error": "empty-prompt"}

        prompt = message
        if history:   # CLI 无会话续接：把历史压成前言，别丢上下文也別装成同一会话
            head = "\n".join("%s: %s" % (h.get("role"), h.get("content"))
                             for h in history if h.get("content"))
            if head:
                prompt = "以下是同一会话的历史（仅供参考）：\n%s\n\n当前任务：\n%s" % (
                    head[-4000:], message)

        used_model = self.resolve_model(model)
        argv = [self.bin_path()] + self.spec["argv"](prompt, used_model,
                                                     _approve_flags(self.agent_id))
        try:
            timeout_s = float(kwargs.get("timeout_s") or 600)
        except (TypeError, ValueError):
            timeout_s = 600.0

        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=cwd or self.spec.get("cwd") or DEFAULT_CWD,
                env=self.env(), start_new_session=True)
        except Exception as e:  # noqa: BLE001
            return {"success": False, "agent": self.agent_id,
                    "error": "spawn_failed: %s: %s" % (type(e).__name__, str(e)[:200])}

        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            _kill_group(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except Exception:  # noqa: BLE001
                pass
            return {"success": False, "agent": self.agent_id,
                    "error": "timeout(%ds)：进程组已终止" % int(timeout_s),
                    "ms": int((time.monotonic() - t0) * 1000)}
        except Exception as e:  # noqa: BLE001
            _kill_group(proc)
            return {"success": False, "agent": self.agent_id,
                    "error": "run_failed: %s: %s" % (type(e).__name__, str(e)[:200])}

        dur = int((time.monotonic() - t0) * 1000)
        stdout = out_b.decode("utf-8", "ignore")
        stderr = err_b.decode("utf-8", "ignore")
        text, usage = self.spec["clean"](stdout)
        rc = proc.returncode or 0
        base = {"agent": self.agent_id, "ms": dur, "exit_code": rc,
                "model": used_model if self.spec.get("model_path") == "ccr" else "native",
                "usage": usage, "raw_chars": len(stdout)}
        if rc != 0:
            tail = (stderr or stdout).strip()[-400:]
            base.update({"success": False,
                         "error": ("exit:%d: %s" % (rc, tail))[:500]})
            return base
        if not text.strip():
            # ③ 退出码 0 但没说话 —— 静默记 success 会把"什么都没干"记成"干完了"
            base.update({"success": False,
                         "error": "empty-output: 退出码 0 但无正文（stderr %d 字）"
                                  % len(stderr)})
            return base
        if _NEEDS_APPROVAL.search(text):
            # 退出码 0、有正文，但正文是在**要权限**：这是最危险的一种假绿
            base.update({"success": False,
                         "error": ("needs_approval: 执行者在等审批，未真正执行（%s；"
                                   "设 TASK_CLI_YOLO=1 可让 CLI 非交互执行）"
                                   % text.strip()[:160])})
            return base
        base.update({"success": True, "response": text})
        return base

    async def chat_stream(self, message: str, session_id: Optional[str] = None,
                          model: Optional[str] = None, history: Optional[list] = None,
                          **kwargs):
        r = await self.chat(message, session_id=session_id, model=model,
                            history=history, **kwargs)
        if r.get("success"):
            yield r.get("response") or ""
        else:
            yield "[error] %s" % (r.get("error") or "unknown")

    async def get_sessions(self, limit: int = 10) -> list:
        return []   # CLI 一次性调用，不建 hub 侧会话

    async def get_models(self) -> list:
        return []   # 模型域在各 CLI 自己那边，hub 不猜

    async def health_check(self) -> Dict[str, Any]:
        ok, why = self.available()
        return {"status": "ok" if ok else "error", "agent": self.agent_id,
                "binary": self.bin_path(), "reason": why or None}
