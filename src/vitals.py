"""可用心跳 vitals —— 「which 到了」≠「能用」（2026-09-21 判定方式第二轮优化）

背景（实测根因）：v0.10.x 的判定只到 `which(name)` 命中为止。本机 13 张 agent 卡里
6 张是假可用：
  · muse / cline / dsh / aider / opencode —— 命中的是 free-claude-code 的**入口壳**
    （~/.local/bin/fcc-*），壳自己 rc=0 地打印一句「Could not find X command」就退出，
    目标程序根本没装。`--version` 也是 rc=0，所以「看返回码」同样骗得过。
  · qoder（qodercli）—— 真装了、能起，但账号 Credits 耗尽，一问就报错。
  · 附带一条端口判定缺陷：qwenpaw 2.2.1 起只监听 IPv6（127.0.0.1:8088 拒连、[::1]:8088 通），
    单栈探测把它误报成「没活」。

分层取证（代码只负责取证 + 硬规则，语义判断交本地模型）：
  L1 身份  which 解析到的路径 + 文件头（ELF / 脚本 / 入口壳特征）
  L2 自述  `<cli> --version`（超时 8s）→ 拿不出版本号或疑似入口壳时递升 `<cli> --help`
  L4 应答  一次性真实请求（烧 token，只在显式 verify / 慢周期跑）
  EP 端点  双栈（127.0.0.1 与 ::1）+ HTTP 状态 —— 服务型 agent 的存在性证据

裁决落 `verdict ∈ usable|blocked_by_account|not_installed|broken|stopped|pending|unknown`。
本地判定不可用（脚本缺失 / Ollama 不在 / 超时）时退回硬规则，并把来源标成 rule，
**规则只在证据无歧义时才摘卡**（关键字误伤是上一版的病，见 hermes「Install method:」）。
菜单闸门取自 `list_in_agents_menu`：**假卡 0.07~0.12、真卡 0.64~0.86、取 0.5 这条刻度是
外部 jev 时代实测的**（2026-09-22）；2026-10-23 切本地模型后题面未改、阈值未重校，
复测前保持 0.5（宁严勿宽），判据见判例 244 §5.3。

实测踩过的坑（都是本轮修的）：
  · 看返码会被骗：入口壳 rc=0；
  · 看关键字也会被骗：hermes 自述里的「Install method: git」曾被旧版粗正则当成未安装；
  · 探针自己也会错：codex 在非同仓目录会 rc=1 报「Not inside a trusted directory」，
    把探针参数不够说成「Agent 坏了」是第二层误报 —— 故有 probe_invalid 这一判。
    现 codex 的 verify_argv 带 --skip-git-repo-check，实测回「好」。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import local_judge
import profiles

# ── 开关与阈值（env 可覆写，默认对现有部署零影响）────────────────
#: 语义判定开关：本地模型（local_judge → Ollama qwen2.5:1.5b）。0 = 只用硬规则。
#: 2026-10-23：外部 jev（api.typesafe.ai）已下线，原 VITALS_JEV/TYPESAFE_* 一并作废。
JUDGE_ENABLED = os.getenv("VITALS_JUDGE", "1") == "1"
#: 单轮判定的硬超时（秒）。一次 5 题 = 5 次 CPU 推理。实测（2026-10-23，本机
#: qwen2.5:1.5b）：空闲时 5 题 37~41s（≈7.5s/题，冷启动含加载 9s）；sweep 用
#: `max_workers=4` 并发打同一台 Ollama，2 题并发实测 15~24s ⇒ 5 题会逼近 60s。
#: 取 180 而不是 60：超时后 `judge()` 会把这一轮记成 rule（判定降级），若 60s
#: 在并发下必然踩线，等于「本地判定永远不生效」。硬超时只用来防挂死，不承担限流。
JUDGE_TIMEOUT_S = float(os.getenv("VITALS_JUDGE_TIMEOUT", "180"))
MENU_MIN = float(os.getenv("VITALS_MENU_MIN", "0.5"))       # 菜单闸门概率下限
PROBE_T = float(os.getenv("VITALS_PROBE_TIMEOUT", "8"))     # L2 单次探针硬超时
SWEEP_EVERY = float(os.getenv("VITALS_SWEEP_SEC", "900"))   # 慢周期（15min）
# L4（真跑一次请求）是否进自动周期：默认开，但**每人每 RT_TTL 只一次**（默认 24h），
# 且只对「L2 看着正常」的候选跑（假卡/坏卡不花 token）。
# 理由：qoder 实测证明 L2-only 会把「额度耗尽」报成可用 —— 不端到端就是误报。
# 不想花 token：VITALS_RT_SWEEP=0 关掉，改人工点「实测应答」。
RT_IN_SWEEP = os.getenv("VITALS_RT_SWEEP", "1") == "1"
RT_PROMPT = os.getenv("VITALS_RT_PROMPT", "只回复一个字：好")
# 一次抖动不得写成永久结论（2026-09-21 实测：hermes 偶发 401，下一轮又是好的，
# 而单次 L4 的负结论会被 24h 保鲜窗冻住）。负结论连错 N 次才允许改判。
RT_NEG_STREAK = int(os.getenv("VITALS_NEG_STREAK", "2"))
# 真请求用的测试模型（2026-09-22 用户指定 Agnes-2.0-flash）。CCR 认斜杠形态，实测可用；
# 写成下划线形态会被 CCR 拒（军规：CCR 用 /，FCC 用 _，两者不通用）。
RT_MODEL = os.getenv("VITALS_RT_MODEL", "agnes/agnes-2.0-flash")
# 实测一轮真请求耗时：claude 14s / jcode 9s / grok 19s / hermes 21s，40s 够宽容抖。
RT_TIMEOUT = float(os.getenv("VITALS_RT_TIMEOUT", "90"))
# 2026-09-24：40s → 90s。实测 claude 走 CCR 在 40s 窗口下跑出一例
# run_rc=124 "TIMEOUT after 40.0s"，而同一个 Agent 稍后又 answered
# ⇒ 超时是瞬时抖动，窗口太短会把「可用」刷成「未实测」（另见 judge 的凭据保持）。
# L4 探活预算（2026-09-23 用户裁定：「探活测试 2 次即结束，不要反复频繁探测」）：
# 外部条件失败态在一个 RT_TTL 窗内最多真跑这么多次，用完即停到窗过期。
# 改前该态完全不设保鲜 ⇒ 每 SWEEP_EVERY(900s) 重烧一次 ⇒ 96 轮/天/家；实测 09-22 16:45
# →09-23 07:37 单 claude 一家连烧 68 次，单次峰值 RSS 270MB（L4 并发 2 ⇒ 540MB），
# 而本机 swap 已用 90% ⇒ 抖动期必然演成内存尖峰风暴。上限走 env，不写死在代码里。
RT_MAX_TRIES = int(os.getenv("VITALS_RT_MAX_TRIES", "2"))
# 归为「外部条件」的态：可重试，但**计入预算**（区别于 answered / blocked_by_account）
RT_RETRYABLE = ("timeout", "rate_limited", "model_unsupported", "no_output")
TRANSIENT = ("unknown", "broken", "pending")
def _default_state_path() -> Path:
    """跟随 DATA_DIR（影子实例/测试隔离）。**2026-10-23 修**：原实现直接写
    `agenthub/data/vitals.json`，只覆盖 DATA_DIR 起影子时仍会写生产状态文件——
    实测沙箱 sweep 把生产台账覆盖成 2 条。"""
    root = os.getenv("DATA_DIR")
    base = Path(root) if root else (Path(__file__).resolve().parent.parent / "data")
    return base / "vitals.json"


STATE_PATH = Path(os.getenv("VITALS_STATE") or str(_default_state_path()))

# 入口壳的**无歧义**特征：壳自己声明目标命令不存在（fcc-* 家族实测原文）
SHIM_RE = re.compile(r"could not find\b.{0,48}\bcommand\b", re.I)
# 外部条件阻断（账号/额度/登录），不是程序坏
BLOCK_RE = re.compile(r"credits?\b[^.\n]{0,24}exhausted|please check your plan|not logged in|"
                      r"login required|quota exceeded|insufficient (credit|balance)|"
                      r"account.{0,24}(disabled|not configured)|"
                      # 同一台 qodercli 会换说法（2026-09-21 实测两种文案），关键字只能当兜底
                      r"reached your\b[^.\n]{0,24}(limit|quota)|upgrade your subscription|"
                      r"credit usage limit|api key doesn't exist|rejected your api key", re.I)
# 下面两类都是「外部条件」而非程序故障，但**不是账号问题**，所以不进 BLOCK_RE：
# BLOCK_RE 一路都往 blocked_by_account 送，把模型标识/限流说成额度问题是错的归类。
RATE_RE = re.compile(r"\b429\b|too many requests|exceeded retry limit|rate limit", re.I)
MODEL_UNK_RE = re.compile(r"unrecognized_model|unknown model id|model not found|"
                          r"couldn't set model|model metadata for", re.I)
# 真请求输出里的噪声行（版本告警、node warning、CLI 自己的 tip），不算应答正文
RT_NOISE_RE = re.compile(r"^\s*(\[[a-z0-9_-]+[:,]|warning:|\(node:|\(Use `node|tip:|"
                         r"╭|│|╰|━|✓|\s*$)", re.I)
VERSION_RE = re.compile(r"\d+\.\d+")
# 探针被 CLI 自身的参数/信任检查拒了 → 不能拿来当 Agent 的坏证据
USAGE_RE = re.compile(r"not inside a trusted directory|unexpected argument|unknown option|"
                      r"usage: |error: unknown|invalid argument|too many arguments", re.I)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")

VERDICTS = ("usable", "blocked_by_account", "not_installed", "broken", "stopped",
            "pending", "unknown", "probe_invalid")

# 真请求（L4）的结果只进这个表，**不进 verdict**：2026-09-22 用户口径——
# 「模型超时是正常情况，不要依据此项判定 agent 无法运行；判定依据是可以正常打开
# 窗口、正常自检」。实测更坐实了这一点：6 个 CLI 里 4 个失败态的退出码都是 0
# （codex 内部 429 也 rc=0），拿 rc/应答当生死闸门必错。
RT_STATES = ("answered", "blocked_by_account", "rate_limited", "model_unsupported",
             "timeout", "probe_rejected", "no_output", "skipped")


def classify_rt(rc: Optional[int], out: str) -> str:
    """把一次真请求的原始输出归档成一个情报态。纯代码分类，Jev 不可用时也能跑。"""
    if rc is None:
        return "skipped"
    if rc == 124:
        return "timeout"
    body = "\n".join(l for l in (out or "").splitlines() if not RT_NOISE_RE.match(l))
    if USAGE_RE.search(out or "") and not body.strip():
        return "probe_rejected"
    if BLOCK_RE.search(body):
        return "blocked_by_account"
    if RATE_RE.search(body):
        return "rate_limited"
    if MODEL_UNK_RE.search(body):
        return "model_unsupported"
    if body.strip():
        return "answered"
    return "no_output"


# ── 取证原语：一律硬超时 + 显式失败，绝不无限等 ───────────────────
def _window(s: str, n: int = 1400) -> str:
    """留头留尾：只留尾会把 stdout/stderr 交错的关键行弄丢
    （实测：qoder 的「Credits have been exhausted」一度落在窗口外 → 误报 usable）"""
    s = _ANSI_RE.sub("", s or "")      # TUI 会刷 ANSI 控制码，不洗掉就连关键字都匹不上
    if len(s) <= n:
        return s
    half = n // 2
    return s[:half] + "\n…[中略 %d 字]…\n" % (len(s) - n) + s[-half:]


def run_argv(argv: List[str], timeout: float = PROBE_T) -> dict:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=launch_env())
        return {"rc": r.returncode, "out": _window((r.stdout or "") + "\n" + (r.stderr or ""),
                                                   6000),
                "timeout": False}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "out": "TIMEOUT after %ss" % timeout, "timeout": True}
    except OSError as e:
        return {"rc": 127, "out": "EXEC_ERROR: %s" % e, "timeout": False}


def launch_env() -> Dict[str, str]:
    """与 term.py 拉起终端时同口径的 PATH —— 探针环境必须等于运行环境，
    否则会把「hub 其实起不来的 CLI」判成可用（服务进程 PATH 不含 ~/.local/bin 等）。"""
    e = dict(os.environ)
    extra = [str(Path.home() / ".local/bin"), str(Path.home() / ".npm-global/bin"),
             "/usr/local/bin", "/usr/bin", "/bin"]
    e["PATH"] = ":".join(dict.fromkeys((e.get("PATH") or "").split(":") + extra))
    return e


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    try:
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(1.0)
        ok = s.connect_ex((host, port)) == 0
        s.close()
        return ok
    except OSError:
        return False


def _http_ok_raw(url: str, timeout: float) -> Optional[int]:
    """裸 GET（不含退避/熔断）。返回 HTTP 状态码；连不上None。"""
    req = urllib.request.Request(url, headers={"Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def http_ok(url: str, timeout: float = 3.0) -> Optional[int]:
    """返回 HTTP 状态码；连不上返回 None。只 GET，不改状态。

    **刻意零重试、且不走熔断**（PT-20260927-12 A 档的例外，不是漏接）：
    本函数是L4 探活的组成部分，而 AGENTS.md 立着「探活最多 2 次即结束」的硬线
    （理由：实测 68 次/天把swap 打到 90%）。任何形式的重试/退避都会在探活路径上
    引入不可预测的等待，把「一轮最多 2 次」变成「2 次 × N 个退避」。
    端口探活要的是**当下这一刻**的应答，不是「等一会儿能不能好」——
    后者是 `vitals` 自己的 RT_TTL保鲜机制该管的事。
    """
    try:
        return _http_ok_raw(url, timeout)
    except urllib.error.HTTPError as e:
        return e.code           # 401/404 也算「端口在应答」
    except Exception:  # noqa: BLE001
        return None


def file_identity(path: str) -> dict:
    """L1：文件类型 + 入口壳特征（读前 400 字节，只读）"""
    out = {"file_kind": "unreadable", "is_entry_shim": False}
    try:
        head = Path(path).read_bytes()[:400]
    except OSError as e:
        out["file_kind"] = "unreadable: %s" % e
        return out
    if head.startswith(b"\x7fELF"):
        out["file_kind"] = "ELF binary"
    else:
        txt = head.decode("utf-8", "replace")
        out["file_kind"] = "script: " + (txt.split("\n")[:1] or [""])[0][:110]
        out["is_entry_shim"] = bool(re.search(r"free_claude_code\.cli\.launchers", txt))
    return out


# ── 判定问题集（本地模型；题面契约同外部 jev 时代，语义未改）─────────
QUESTIONS = {
    "underlying_program_present": {
        "type": "noul",
        "instructions": {
            "question": ("Reading `version_output`, `help_output`, `file_kind` and "
                         "`resolved_path`: is the agent program itself actually installed and "
                         "startable on this machine right now?"),
            "note": ("A launcher/wrapper binary can exist while the tool it forwards to is "
                     "absent; in that case the real agent is NOT installed even if the "
                     "launcher exits successfully with rc=0. When `agent_shape` is "
                     "web-service there is no CLI by design — an HTTP endpoint answering on "
                     "its own port is proof the program is present and running.")},
        "criteria": {
            "true": ("Output shows a concrete version/identity of the agent itself, real "
                     "program output, or an answering HTTP endpoint for a web-service agent"),
            "false": ("Output says the target command could not be found, asks the user to "
                      "install it, or nothing of the agent itself ever started")}},
    "model_round_trip_ok": {
        "type": "noul",
        "instructions": {
            "question": ("Based on `run_output` (what happened when this agent was asked to "
                         "answer one short prompt): did the agent actually complete a model "
                         "round trip and produce an assistant reply?"),
            "note": ("If `run_evidence` says the round trip was not probed, judge only on what "
                     "the shown output proves and do not assume success.")},
        "criteria": {
            "true": "A genuine assistant reply to the prompt is visible in the transcript",
            "false": ("Errors, an install hint, a silent hang, or an account/credit/login "
                      "block instead of a reply")}},
    "external_block_only": {
        "type": "noul",
        "instructions": ("Is this agent's inability to serve caused purely by an external "
                         "condition (exhausted credits, missing login, unconfigured account) "
                         "while the software itself is installed and working?"),
        "criteria": {
            "true": "Program is fine and would work as soon as the account condition is fixed",
            "false": "The program is missing, broken, hangs, or no blocking is evidenced"}},
    "roundtrip_state": {
        "type": "choice",
        "instructions": {
            "question": ("Read `run_output`, the result of sending this agent one short prompt. "
                         "Which single outcome happened? This advises on the MODEL LAYER only: "
                         "it never decides whether the agent is installed or usable, so do not "
                         "reason about the program's health here."),
            "note": ("A rate limit, an exhausted credit balance, an unsupported model id and a "
                     "timeout are four different external causes with four different fixes — "
                     "tell them apart. `run_rc` is not evidence: several of these CLIs exit 0 "
                     "even when the call failed.")},
        "criteria": {
            "answered": "A genuine assistant reply to the prompt is present in the transcript",
            "blocked_by_account": ("An exhausted quota/credit, a missing login or an "
                                   "unconfigured account is named as the reason"),
            "rate_limited": ("The upstream refused for volume: HTTP 429, 'too many requests', "
                             "'exceeded retry limit', 'rate limit'"),
            "model_unsupported": ("The requested model id is unknown to this CLI or router: "
                                  "'unrecognized_model', 'unknown model id', 'model not found'"),
            "timeout": "The probe's own deadline cut the invocation off before it finished",
            "probe_rejected": ("The CLI rejected the probe's arguments/trust before any model "
                               "call, so the attempt proves nothing about anything"),
            "no_output": "The invocation ended with nothing readable"}},
    "list_in_agents_menu": {
        "type": "noul",
        "instructions": ("Is this card a real agent installed on this machine, as opposed to a "
                         "phantom card left behind by a launcher whose target program is "
                         "absent? Judge existence only — a quota problem, a rate limit or a "
                         "wrong model id does not make the card a phantom."),
        "criteria": {
            "true": ("The agent's own program started and identified itself (a version string, "
                     "or an HTTP endpoint answering on its own port)"),
            "false": ("Only a wrapper exists and it says the target command cannot be found or "
                      "asks the user to install it")}},
}


#: 缓存里的语义结论多久算「还是本轮依据」。超时即降级显示为 rule（见 judge 的缓存分支）。
#: 取 6h：vitals 的 sweep 周期是 900s，同一天内换证据就该重问；跨天必然过期。
#: 2026-10-23：外部 jev 下线后该判据原样保留并**扩展到本地判定**——`by_sha` 复用意味着
#: 30 天前的 `source=local` 同样会被显示成「本轮判的」，与当年 jev 假绿同构；
#: 历史数据里的 `jev` / `jev-lowconf` 也仍按本条降级（不因端点下线就篡改旧记录）。
JUDGE_CACHE_FRESH_H = float(os.getenv("VITALS_JUDGE_CACHE_FRESH_H", "6"))

#: 判定结论多久强制重判一次。`by_sha` 以 core_sha 为键（路径/类型/版本），
#: agent 不升级就永远命中 ⇒ 本地判定会一直休眠。24h 是「每天至少真跑一次」的兜底，
#: 不是精度要求：证据变了键就变，会立刻重判。
JUDGE_REFRESH_H = float(os.getenv("VITALS_JUDGE_REFRESH_H", "24"))
#: 每轮 sweep 最多重判几个（首轮回填 12 个 agent × 5 题会把 CPU 打满，分 6 轮摊开）。
JUDGE_REFRESH_BUDGET = int(os.getenv("VITALS_JUDGE_BUDGET", "2"))
#: 每轮最多补几次 L4 真请求。判定上线后单轮 2 个 agent 实测 100s（L1+L4 共 4 次推理），
#: 不设上限时 12 个 agent 一起到点会跑 10 分钟、与 900s 周期重叠。
RT_BUDGET = int(os.getenv("VITALS_RT_BUDGET", "2"))


def present_from_evidence(ev: dict, model_noul=None):
    """安装状态：**硬证据优先，模型只在证据模糊时生效**。

    2026-10-23 实测：同一份“路径缺失 + file_kind=missing”证据，模型三态问法仍答
    「已安装」（置信 1.0）；换 JSON/散文/标签都复现。既然证据本身已能判定，
    就不该让模型推翻它——这正是「判定必须可复核」的最低要求。
    返回 `(noul, source)`；source ∈ rule|local|unknown。
    """
    kind = str(ev.get("file_kind") or ev.get("kind") or "")
    if ev.get("is_entry_shim"):
        return 0.0, "rule"
    resolved = ev.get("resolved")
    if resolved is False or kind in ("missing", "unreadable"):
        return 0.0, "rule"
    if resolved is True:
        return 1.0, "rule"
    if kind and kind not in ("missing", "unreadable"):
        return 1.0, "rule"
    if model_noul is not None:
        return model_noul, "local"
    return None, "unknown"


def menu_from_evidence(ev: dict):
    """菜单闸门：**确定性证据优先，模型不参与**。

    2026-10-23 实测：1.5b 对「是否应出现在菜单」恒定答「应显示」（置信 0.9），
    连证据写「未安装」时也如此；同一份证据正反问都返回 1.0。把它当门禁等于没有门禁。
    证据里本来就有 `is_entry_shim`（入口壳：launcher 存在但目标程序缺失），
    直接用它判定，模型只保留在 truly 语义题（应答态/封锁原因）上。
    """
    if ev.get("is_entry_shim"):
        return 0.0
    noul, _ = present_from_evidence(ev)
    if noul is not None:
        return noul
    return None


def judge_status() -> Dict[str, Any]:
    """本地判定后端状态（薄封装：api_vitals / sweep 日志用，逻辑只在 local_judge）。"""
    return local_judge.status()


def judge_alerts(n: int = 5) -> list:
    """最近 n 条判定告警/恢复记录（面板用）。"""
    return local_judge.recent_alerts(n)


def ask_local(state: dict, key: Optional[List[str]] = None,
              timeout: float = JUDGE_TIMEOUT_S) -> Optional[dict]:
    """一次调用问多个独立判断（本地模型，子进程内串行执行）。

    与外部 jev 时代的 `ask_jev` 同构：失败返回 `{"_error": ...}`（judge() 会把它
    如实记进载荷），调用方退回硬规则。区别只在失败原因——从「451 / 超时 / key 没
    生效」变成「脚本缺失 / Ollama 不在 / 超时 / 输出不可解析」，且零 key、零外网。
    """
    if not JUDGE_ENABLED:
        return None
    key = [k for k in (key or list(QUESTIONS)) if k != "list_in_agents_menu"]
    # 没有真请求（L2 轮）时，应答态/封锁原因只能得到 `skipped`：问了也是白花 3 次推理。
    # 实测 2 个 agent 一轮从 159s 降到 ~10s，且不影响判定字段（rt_code 本来就是 skipped）。
    if state.get("run_rc") is None:
        key = [k for k in key if k == "underlying_program_present"]
    if not key:
        return {}
    # 缓存键要剔掉每轮都会变的字段，否则 state 每次都不同 ⇒ 缓存永不命中。
    _VOLATILE = ("checked_at", "evidence_sha", "core_sha", "run_note", "probe_rejected")
    model_state = {k: v for k, v in state.items() if k not in _VOLATILE}
    payload = {}
    for k in key:
        q = QUESTIONS[k]
        if k == "underlying_program_present":
            # 2026-10-23 实测：`noul` 形态（"p 是多少"）对本机 1.5b 恒返回 1.0，
            # 连「路径缺失、command not found」也判 1.0；正反问同值。改成**三态具体
            # 标签**后 2/2 正确（已安装/未安装/无法判断）。这里只换问法，对外契约不变：
            # 仍回 `noul`（1.0/0.0/None），judge() 不必改。
            payload[k] = {
                "type": "choice",
                "instructions": ("根据证据判断该 CLI 的安装状态。证据含 path/file_kind/"
                                 "version/run_rc/run_output；只看事实，不要猜。"),
                "criteria": {"已安装": "可执行文件存在且能出版本或运行成功",
                             "未安装": "路径缺失或命令不存在",
                             "无法判断": "证据不足以判断"},
            }
        else:
            payload[k] = q
    try:
        r = local_judge.judge(payload,
                              state=json.dumps(model_state, ensure_ascii=False, default=str),
                              timeout_s=timeout)
        answers = dict(r.get("answers") or {})
        present = answers.get("underlying_program_present")
        if isinstance(present, dict) and present.get("type") == "choice":
            label = present.get("choice")
            noul = (1.0 if label == "已安装" else 0.0 if label == "未安装" else None)
            # 硬证据优先：模型与“路径/类型/版本”冲突时以证据为准（判例 245 §4）
            present["noul"], present["source"] = present_from_evidence(state, noul)
        return answers
    except Exception as e:  # noqa: BLE001 —— 判定是增强，任何失败都退回硬规则
        return {"_error": "%s: %s" % (type(e).__name__, str(e)[:140])}


# ── 存在性闸门（只用 L1/L2 自检证据；真请求结果一律不进这里）────────
def rule_verdict(ev: dict) -> str:
    """能打开窗口 + 能自检 = 在。真请求结果（run_* / rt_state）一律不进这里。

    2026-09-22 用户口径：「模型超时是正常情况，不要依据此项判定 agent 无法运行；
    判定依据是可以正常打开窗口、正常自检。」实测亦坐实这一点：6 个 CLI 里 4 个
    失败态的退出码都是 0（codex 内部 429 也 rc=0），拿 rc/应答当生死闸门必错。
    """
    if ev.get("agent_shape") == "web-service":
        return "usable" if ev.get("endpoint_serving") else "stopped"
    if not ev.get("resolved"):
        return "not_installed" if ev.get("candidates_tried") else "unknown"
    # 入口壳的自我声明优先于任何版本号：fcc-dsh 的安装提示里就带着 "dsh@0.1.0-rc.8"，
    # 拿版本号当「装着呢」的证据会被它骗过去。
    selfcheck = " ".join([ev.get("version_output") or "", ev.get("help_output") or ""])
    if SHIM_RE.search(selfcheck):
        return "not_installed"
    # 两个不碰模型的探针全部超时 = 窗口根本打不开，这才是程序级故障
    if ev.get("version_rc") == 124 and ev.get("help_rc") == 124:
        return "broken"
    if ev.get("version_rc") == 127:
        return "not_installed"
    if VERSION_RE.search(selfcheck):
        return "usable"
    return "unknown"


# ── 采集 + 裁决 ─────────────────────────────────────────────────
def ordered_candidates(primary: Optional[str],
                       aliases: Optional[List[str]]) -> List[str]:
    """候选名顺序：**真名（非 fcc-* 入口壳）一律优先**，fcc-* 垫后（稳定排序，保留原序）。

    2026-09-24 修（opencode 实例）：原实现把画像里**冻结的** cli 字段当首选，
    而候补序只在「首选 which() 完全落空」时才往下走。于是 09-22 命中的假壳
    `fcc-opencode` 会一直压着后面的真二进制：壳会退 rc=127 打印
    “Could not find OpenCode CLI command”，verdict 停在 not_installed，
    **真装好后也翻不了案**。改为不依赖冻结字段，按名性质定序。

    单一真相源：collect() 与 Vitals.rejudge_stale() 共用此函数，不得各写一份。"""
    seen: set = set()
    names: List[str] = []
    for n in [primary] + list(aliases or []):
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    return sorted(names, key=lambda n: 1 if n.rsplit("/", 1)[-1].startswith("fcc-") else 0)


def collect(prof: dict, do_roundtrip: bool = False) -> dict:
    """单个画像 → 一份证据。CLI 型走 L1/L2(/L4)；服务型走 EP。"""
    pid, kind = prof.get("id"), prof.get("kind")
    ev: dict = {"agent_id": pid, "display_name": prof.get("name"),
                "profile_kind": kind}
    ui = prof.get("ui")
    port = prof.get("port") or (ui.get("port") if isinstance(ui, dict) else None)
    cli = prof.get("cli")
    term = (prof.get("terminal") or {}).get("cmd")
    name = cli or term
    if name:                                   # 终端型 Agent
        ev["agent_shape"] = "terminal-cli"
        # 真名优先逐个 which（ordered_candidates 为唯一定序口径）
        cands = ordered_candidates(name, prof.get("aliases"))
        for n in cands:
            p2 = profiles.which(n)
            if p2:
                name, path = n, p2
                break
        else:
            name, path = (cands[0] if cands else None), None
        ev["candidates_tried"] = cands
        ev["resolved_name"], ev["resolved_path"], ev["resolved"] = name, path, bool(path)
        if path:
            ev.update(file_identity(path))
            r = run_argv([path, "--version"])
            ev["version_rc"], ev["version_output"] = r["rc"], r["out"][:400]
            if ev.get("is_entry_shim") or not VERSION_RE.search(ev["version_output"]):
                r2 = run_argv([path, "--help"])
                ev["help_rc"], ev["help_output"] = r2["rc"], r2["out"][:600]
            if do_roundtrip:
                spec = prof.get("verify_argv")
                if spec:
                    model = prof.get("rt_model") or RT_MODEL
                    argv = [a.replace("{p}", RT_PROMPT).replace("{m}", model) for a in spec]
                    r3 = run_argv(argv, RT_TIMEOUT)
                    ev["run_rc"], ev["run_output"] = r3["rc"], r3["out"]
                    ev["rt_state"] = classify_rt(r3["rc"], r3["out"])
                    ev["run_model"] = model if "{m}" in " ".join(spec) else "(cli default)"
                    # run_ok 语义收窄为「真请求拿到应答」，仅供展示与前端旧字段兼容；
                    # 它**不参与** verdict —— 应答失败不得推翻自检结论。
                    ev["run_ok"] = ev["rt_state"] == "answered"
                    ev["run_evidence"] = "live one-shot model request (advisory only)"
                    if ev["rt_state"] != "answered":
                        lines = [x.strip() for x in (r3["out"] or "").splitlines()
                                 if x.strip() and "Warning:" not in x
                                 and "trace-warnings" not in x]
                        ev["run_note"] = (lines[0] if lines else "TIMEOUT")[:150]
                    if ev["rt_state"] == "probe_rejected":
                        ev["probe_rejected"] = True
                        ev["run_evidence"] = ("probe rejected by the CLI's own argument/trust "
                                              "check before any model call happened")
                else:
                    ev["rt_state"] = "skipped"
                    ev["run_evidence"] = "no verify_argv declared for this agent"
            else:
                ev["run_evidence"] = "round trip not probed this cycle"
        else:
            ev["version_rc"], ev["version_output"] = 127, "no executable resolved for candidates"
    elif port:                                 # 服务型 Agent（WebUI 宿主）
        ev["agent_shape"] = "web-service"
        ev["port"] = port
        ev["endpoint_v4_open"], ev["endpoint_v6_open"] = port_open(port), port_open(port, "::1")
        v4 = http_ok("http://127.0.0.1:%d" % port)
        v6 = http_ok("http://[::1]:%d" % port)
        ev["endpoint_http_v4"], ev["endpoint_http_v6"] = v4, v6
        ev["endpoint_serving"] = any(
            c is not None and c < 500 for c in (v4, v6))
        ev["ipv6_only_listener"] = bool(v6 is not None and v4 is None)
        ev["version_output"] = "(web-service agent: no CLI by design)"
        ev["how_identity_is_proven"] = "own HTTP server answering on port %s" % port
    else:
        ev["agent_shape"] = "unknown"
        ev["version_output"] = "no cli and no port declared in profile"
    ev["evidence_sha"] = hashlib.sha256(json.dumps(
        {k: v for k, v in ev.items() if k != "checked_at"}, sort_keys=True,
        ensure_ascii=False, default=str).encode()).hexdigest()[:12]
    # 缓存主键只跟「不跑模型就会变」的证据挂钩（剔掉 run_* ）：
    # 否则 L4 一次体检的结论会被 15 分钟后的 L2 轻证据覆盖掉（实测会发生）。
    ev["core_sha"] = hashlib.sha256(json.dumps(
        {k: v for k, v in ev.items()
         if k not in ("checked_at", "run_rc", "run_output", "run_ok", "run_evidence",
                      "run_note", "run_model", "rt_state", "probe_rejected")},
        sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:12]
    return ev


class Vitals:
    """裁决表 + 缓存。/api/agents 热路径只读缓存，探针一律在后台慢周期跑。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_sha: Dict[str, dict] = {}     # core_sha -> 裁决
        self._latest: Dict[str, dict] = {}     # agent_id -> {evidence, verdict}
        self._rt: Dict[str, dict] = {}         # agent_id -> 最近一次 L4 真实应答结果
        self._judge_budget = 0                 # 本轮 sweep 还能重判几个（防 CPU 打满）
        self.last_sweep = 0.0
        self._load()

    RT_TTL = float(os.getenv("VITALS_RT_TTL", "86400"))   # L4 结论保鲜 24h

    # ── 落盘（重启不重跑、假卡不会重新冒出来）
    def _load(self):
        try:
            d = json.loads(STATE_PATH.read_text())
            self._by_sha = d.get("by_sha", {})
            self._latest = d.get("latest", {})
            self._rt = d.get("rt", {})
            self.last_sweep = d.get("last_sweep", 0.0)
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            # tmp 名带 pid+线程 id：并发 sweep 各写各的 tmp，os.replace 才是唯一落盘点。
            # 固定名 .json.tmp 时两个线程互相 truncate/write ⇒ 落盘 JSON 偶发残缺。
            tmp = STATE_PATH.with_suffix(f".json.tmp.{os.getpid()}.{threading.get_ident()}")
            tmp.write_text(json.dumps(
                {"by_sha": self._by_sha, "latest": self._latest, "rt": self._rt,
                 "last_sweep": self.last_sweep, "saved_at": time.time()},
                ensure_ascii=False, indent=1))
            os.replace(tmp, STATE_PATH)
        except OSError:
            pass

    # ── 单实体：取证 → （sha 命中则复用）→ 裁决
    def judge(self, prof: dict, do_roundtrip: bool = False,
              refresh: bool = False) -> dict:
        ev = collect(prof, do_roundtrip)
        core = ev.get("core_sha") or ev["evidence_sha"]
        aid = ev["agent_id"]
        with self._lock:
            cached = self._by_sha.get(core)
            rt = dict(self._rt.get(aid) or {})
        # 生死闸门只认自检（L1/L2）。语义判定与真请求都不许改它（2026-09-22 用户口径）。
        verdict = rule_verdict(ev)
        rt_code = (classify_rt(ev.get("run_rc"), ev.get("run_output") or "")
                   if ev.get("run_rc") is not None else "skipped")
        reuse = bool(cached) and not do_roundtrip
        # 2026-10-23（用户问「本地判定是否自动调用」时查出来的休眠）：`by_sha` 以
        # `core_sha`（只含路径/类型/版本）为键，agent 二进制不变就**永不重判** ——
        # 16 条记录全是 jev 时代的 `rule`，本地判定一次都没跑过。判据改成两层：
        # ① 超过 `JUDGE_REFRESH_H`（默认 24h）视为陈旧，sweep 里按预算重判；
        # ② 语义字段为空（menu_noul / present_noul 都没有）也算陈旧，补一次历史欠账。
        # 预算存在是为了让首轮回填不把 CPU 打满：每轮最多 N 个，其余下轮继续。
        stale = False
        if reuse:
            age_h = (time.time() - float(cached.get("checked_at") or 0)) / 3600.0
            missing_semantics = (cached.get("menu_noul") is None
                                 and cached.get("present_noul") is None
                                 and cached.get("source") in (None, "rule"))
            stale = age_h > JUDGE_REFRESH_H or missing_semantics
            if stale and refresh:
                with self._lock:
                    if self._judge_budget > 0:
                        self._judge_budget -= 1
                    else:
                        stale = False        # 预算用完：保留旧结论，下一轮再来
            if stale:
                reuse = False
        if reuse:
            rec = dict(cached)
            rec.update(verdict=verdict, rule_verdict=verdict)
            # 2026-10-22（用户问「jev 是否参与链路」时查出来的展示层假绿）：
            # `by_sha` 缓存让同一份证据**永不重问判定**，于是 30 天前的 `source=jev`
            # 一直显示成「本轮判的」。实测 `data/vitals.json` 270 条里有
            # jev 78 + jev-lowconf 99，最老 checked_at 是 2026-09-22（30.3 天前）。
            # 口径：**source 表示「本轮判定依据」**，不是「历史上曾用过什么」。
            # 故缓存里的语义结论（jev 史实 + local 现值）超过 `JUDGE_CACHE_FRESH_H`
            # 小时就降级显示为 rule，原值留在 `source_cached` / `cached_age_h` 供复核。
            if str(rec.get("source") or "").startswith(("jev", "local")):
                age_h = (time.time() - float(rec.get("checked_at") or 0)) / 3600.0
                if age_h > JUDGE_CACHE_FRESH_H:
                    rec["source_cached"] = rec.get("source")
                    rec["cached_age_h"] = round(age_h, 1)
                    rec["source"] = "rule"
                    rec["note"] = ("%s 结论来自 %.1f 天前缓存（本行判定依据是代码规则）"
                                   % (rec["source_cached"], age_h / 24.0))
        else:
            ans = ask_local(ev) or {}
            err = ans.get("_error") if isinstance(ans, dict) else None
            jrt = (ans.get("roundtrip_state") or {}).get("choice")
            conf = (ans.get("roundtrip_state") or {}).get("confidence")
            menu = menu_from_evidence(ev)
            # 应答态：本地判定读文案、代码兜底；两者都只出情报，不动 verdict
            if jrt in RT_STATES and rt_code != "skipped" and (conf is None or conf >= 0.45):
                rt_state, source = jrt, "local"
            else:
                rt_state, source = rt_code, ("local-lowconf" if jrt else "rule")
            rec = {"verdict": verdict, "rule_verdict": verdict, "rt_state": rt_state,
                   "source": source, "judge_error": err, "confidence": conf,
                   "menu_noul": menu, "is_entry_shim": bool(ev.get("is_entry_shim")),
                   "present_source": (ans.get("underlying_program_present") or {}).get("source"),
                   "present_noul": (ans.get("underlying_program_present") or {}).get("noul"),
                   "roundtrip_noul": (ans.get("model_round_trip_ok") or {}).get("noul"),
                   "block_noul": (ans.get("external_block_only") or {}).get("noul"),
                   "checked_at": time.time()}
            with self._lock:
                self._by_sha[core] = rec
                if len(self._by_sha) > 400:            # 只留近期，防无界增长
                    for k in sorted(self._by_sha, key=lambda k: self._by_sha[k]["checked_at"])[:100]:
                        self._by_sha.pop(k, None)
        if do_roundtrip:                # 应答历史：仅供展示与保鲜复用
            # 预算记账（跳 needs_rt 的用户裁定）：成功即归零；跳窗算新一轮从 1 起；窗内失败累加
            now = time.time()
            new_state = rec.get("rt_state")
            answered = new_state == "answered"
            new_window = (not rt) or (now - rt.get("at", 0)) >= self.RT_TTL
            # 2026-09-24（claude 实例）：**一次 L4 失败不得作废已有的 answered 凭据**。
            # 原行为：失败就把 _rt[aid] 覆盖成 timeout ⇒ 之后每轮 L2 sweep 都从 _rt 复原成
            # timeout（见下方 not do_roundtrip 分支）⇒ 卡片从「可用」退回「未实测」，
            # 而用户实测是能用 的 —— 瞬时抖动被固化成了永久降级。
            # 现在：失败**照记**为 rt.last_fail（不藏证据），但凭据保持 answered 直到过 RT_TTL 窗。
            keep = (not answered and new_state in RT_RETRYABLE
                    and rt.get("rt_state") == "answered"
                    and (now - rt.get("at", 0)) < self.RT_TTL)
            tries = 0 if answered else (1 if new_window else int(rt.get("tries") or 0) + 1)
            with self._lock:
                if keep:
                    self._rt[aid] = {"rt_state": "answered",
                                     "tries": tries,
                                     "run_note": rt.get("run_note"),
                                     "run_model": rt.get("run_model"),
                                     "menu_noul": rec.get("menu_noul"),
                                     "source": rt.get("source") or rec.get("source"),
                                     "at": rt.get("at"),          # 凭据时刻保持原值，不虚增
                                     "last_fail": new_state,
                                     "last_fail_at": now,
                                     "last_fail_note": (ev.get("run_note") or "")[:150]}
                else:
                    self._rt[aid] = {"rt_state": new_state,
                                     "tries": tries,
                                     "run_note": (ev.get("run_note") or "")[:150],
                                     "run_model": ev.get("run_model"),
                                     "menu_noul": rec.get("menu_noul"),
                                     "source": rec.get("source"), "at": now,
                                     "last_fail": None, "last_fail_at": None}
            if keep:
                rec = dict(rec)
                rec["rt_state"] = "answered"
                rec["rt_note"] = ("本轮 L4 %s（%ss 窗口），沿用旧 answered 凭据不降级"
                                  % (new_state, int(RT_TIMEOUT)))
                rec["rt_last_fail"] = new_state
        if not do_roundtrip and rt and (time.time() - rt.get("at", 0)) < self.RT_TTL:
            rec = dict(rec)
            rec["rt_state"] = rt.get("rt_state") or rec.get("rt_state")
            rec["rt_at"], rec["rt_note"] = rt.get("at"), rt.get("run_note")
            rec["rt_model"] = rt.get("run_model")
            rec["rt_tries"] = int(rt.get("tries") or 0)   # 供 UI/排查看“本轮已烧几次”
            if rec["rt_state"] in ("timeout", "rate_limited", "model_unsupported"):
                rec["rt_flaky"] = True  # 外部条件，不是本机故障，不固化成坏结论
        with self._lock:
            self._latest[aid] = {"evidence": ev, **rec}
        return self._latest[aid]

    def snapshot(self) -> Dict[str, dict]:
        """当前快照。**返回前统一做「语义结论陈旧降级」**。

        为什么放在这里而不是只放在 `judge()` 的缓存分支：`_latest` 是另一份字典，
        已经不在扫描面的 agent（实测 aider/cline/dsh）**永远不会被覆盖** ⇒ 它们 30 天前
        的 `source=jev-lowconf` / `source=local-lowconf` 会一直出现在面板上。
        同一个判据只在一处实现必然漏。
        """
        with self._lock:
            out = json.loads(json.dumps(self._latest))
        now = time.time()
        for aid, rec in out.items():
            src = str(rec.get("source") or "")
            if not src.startswith(("jev", "local")):
                continue
            age_h = (now - float(rec.get("checked_at") or 0)) / 3600.0
            if age_h > JUDGE_CACHE_FRESH_H:
                rec["source_cached"] = src
                rec["cached_age_h"] = round(age_h, 1)
                rec["source"] = "rule"
                rec["note"] = ("%s 结论来自 %.1f 天前（该 agent 已不在本轮扫描面）；"
                               "本行判定依据是代码规则" % (src, age_h / 24.0))
        return out

    def get(self, agent_id: str) -> Optional[dict]:
        with self._lock:
            rec = self._latest.get(agent_id)
            return dict(rec) if rec else None

    # ── 慢周期：先全量 L1/L2（不碰模型），再按保鲜窗补 L4
    def needs_rt(self, prof: dict, ev: dict) -> bool:
        """本轮是否该花一次真请求：verify_argv + 自检过关 + 预算未用完 + 已过保鲜窗

        2026-09-23 用户裁定「探活测试 2 次即结束，不要反复频繁探测」：外部条件失败态
        仍要重试（免得一次抖动被固化成永久坏情报），但一个 RT_TTL 窗内最多 RT_MAX_TRIES
        次；用完即停到窗过期，窗过再给一整轮——既不自愈断线，也不每 15min 无限重烧。
        """
        if not (RT_IN_SWEEP and prof.get("verify_argv")):
            return False
        if ev.get("agent_shape") != "terminal-cli" or not ev.get("resolved"):
            return False
        if rule_verdict(ev) in ("not_installed", "broken"):
            return False                       # 假卡/坏卡不花 token
        with self._lock:
            rt = self._rt.get(prof["id"]) or {}
        age = time.time() - rt.get("at", 0)
        if rt.get("rt_state") in RT_RETRYABLE:
            if age >= self.RT_TTL:
                return True                    # 新窗 ⇒ 预算重新给满
            return int(rt.get("tries") or 0) < RT_MAX_TRIES
        # answered / blocked_by_account / probe_rejected / skipped：吃保鲜，到点再探
        return age >= self.RT_TTL

    def sweep(self, profs: List[dict]) -> dict:
        t0 = time.time()
        from concurrent.futures import ThreadPoolExecutor
        st0 = judge_status()
        with self._lock:
            self._judge_budget = max(0, JUDGE_REFRESH_BUDGET)
        with ThreadPoolExecutor(max_workers=4) as ex:
            recs = list(ex.map(lambda p: self.judge(p, False, refresh=True), profs))
        rt_jobs = [p for p, r in zip(profs, recs) if self.needs_rt(p, r.get("evidence", {}))]
        if RT_BUDGET > 0:
            rt_jobs = rt_jobs[:RT_BUDGET]
        rt_done = 0
        if rt_jobs:
            with ThreadPoolExecutor(max_workers=2) as ex:   # L4 串行度更低，免得挤垮 CCR
                list(ex.map(lambda p: self.judge(p, True), rt_jobs))
            rt_done = len(rt_jobs)
        st1 = judge_status()
        judged = int(st1.get("calls") or 0) - int(st0.get("calls") or 0)
        hits = int((st1.get("cache") or {}).get("hits") or 0) - \
               int((st0.get("cache") or {}).get("hits") or 0)
        self.last_sweep = time.time()
        self._save()
        return {"agents": len(profs), "rt_runs": rt_done, "secs": round(time.time() - t0, 2),
                "judged": max(0, judged), "cache_hits": max(0, hits),
                "judge": {"ready": st1.get("ready"), "backend": st1.get("backend"),
                          "model": st1.get("model"), "alert": st1.get("alert"),
                          "consecutive_fail": st1.get("consecutive_fail")}}

    def verify(self, prof: dict) -> dict:
        """L4：一次性真实请求（用户点「体检」时才跑）"""
        rec = self.judge(prof, do_roundtrip=True)
        self._save()
        return rec

    def rejudge_stale(self, profs: List[dict]) -> dict:
        """定向重判：只处理「可能被陈旧结论压住」的候补，只跑 L1/L2（不碰模型、不烧 token）。

        为何需要：卡片闸门 `show_in_menu()` 读的是**上一轮 sweep 的结论**，而不是当下磁盘状态；
        新装 CLI 会被上一轮 not_installed 固化，理应当等 SWEEP_EVERY(900s)，但候补有轮换降频
        （实测部分候补陈旧 32h）⇒「装好了但菜单里没有」可拖半日以上。
        命中任一条即重判：无记录 / 上次判 not_installed / 记录的路径已不在 / 现在选出的首选名与当时不同。
        返回 changed 供调用方决定要不要刷 discovery。"""
        t0 = time.time()
        checked: List[str] = []
        changed: List[dict] = []
        for p in profs:
            pid = p.get("id")
            if not pid:
                continue
            rec = self.get(pid)
            old = (rec or {}).get("verdict")
            ev = (rec or {}).get("evidence", {}) or {}
            cands = ordered_candidates(p.get("cli") or (p.get("terminal") or {}).get("cmd"),
                                       p.get("aliases"))
            hit = next((n for n in cands if profiles.which(n)), None)
            stale = (rec is None) or (old == "not_installed") or (hit and ev.get("resolved_name") != hit)
            if not stale and ev.get("resolved_path") and not os.path.exists(ev["resolved_path"]):
                stale = True                      # 记录里那个可执行文件已被删/换位
            if not stale:
                continue
            checked.append(pid)
            new = self.judge(p, False)
            if new.get("verdict") != old:
                changed.append({"id": pid, "from": old, "to": new.get("verdict")})
        if checked:
            self._save()
        return {"checked": checked, "changed": changed,
                "secs": round(time.time() - t0, 2)}

    # ── 菜单闸门：假卡不出卡；判不出来时保留（宁多不误删）
    def show_in_menu(self, agent_id: str) -> bool:
        rec = self.get(agent_id)
        if not rec:
            return True
        if rec["verdict"] == "not_installed":
            return False
        # 2026-10-23：入口壳（launcher 在、目标程序缺失）由证据直接判，不再问模型。
        # 实测 1.5b 对菜单题恒答「应显示」，模型闸门等于没有闸门（判例 245 §4）。
        if rec.get("is_entry_shim"):
            return False
        m = rec.get("menu_noul")
        # 2026-09-23 修复：原条件额外要求 source == "jev"，但 source 取 "jev" 需
        # rt_code != "skipped"，而默认不跑 L4 真请求 → rt_code 恒为 skipped →
        # source 永远只能是 rule/jev-lowconf，menu_noul 被无条件忽略（门禁形同虚设）。
        # （2026-10-23 起 source 词表为 local/local-lowconf，判据同理：只看 Noul 本身。）
        # Noul 与 roundtrip 是两条独立判定，此处只看 Noul 本身；判不出来(m=None)仍保留。
        if m is not None:
            return m >= MENU_MIN
        return True


vitals = Vitals()
