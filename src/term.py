"""Web 终端（P2-B：hub 自建 pty + WebSocket + xterm.js）

安全模型：
- 可执行命令 = profiles 画像白名单（terminal.cmd），API 只接受 agent_id，绝不接受任意命令
- v0.13.0 续聊：命令仍只出自画像白名单 + 后端模板（sessions_store.resume_argv），
  客户端最多传一个过正则且实盘存在的 session id，传不进命令
- 会话仅创建者主机可见（服务本身 LAN/Tailscale 信任域）；可选 TERM_TOKEN 鉴权（ws ?token=）
- 空闲 TTL（默认 45min）自动回收；hub 重启即全部销毁（无残留 shell）
"""
import asyncio
import errno
import hmac
import json
import logging
import os
import pty
import re
import secrets
import shlex
import signal
import struct
import termios
import time
import uuid
import fcntl
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

import db
import dshws
import modelcfg
import profiles
import sessions_store
import term_record

log = logging.getLogger(__name__)

router = APIRouter()

TERM_TOKEN = os.getenv("TERM_TOKEN", "")
if not TERM_TOKEN:
    # 兜底：未配置 token 时自动生成，避免"空 token=不鉴权"的裸奔状态
    TERM_TOKEN = secrets.token_urlsafe(32)
    log.warning(f"[term] TERM_TOKEN 未配置，已自动生成随机 token（前4位={TERM_TOKEN[:4]}，len={len(TERM_TOKEN)}）")
IDLE_TTL_S = int(os.getenv("TERM_IDLE_TTL", "600"))
#: **无人观看**时的更短 TTL（PT-20261024-08 B2）。
#: 为什么需要两级：IDLE_TTL_S 判的是「会话整体静默」，
#: 而「客户端已断开（viewers 空）但 agent 还在跑」是另一种形态 ——
#: 用户关掉页面后，agent 进程继续占着 485MB（实测单个 codebuddy CLI 485MB，
#: 而 hub 的 cgroup 记账里它占大头，判例 agent-knowledge/259）。
#: ⇒ 无人观看时用更短的 TTL 收掉，**但**仍受「pty 有产出」的豁免保护
#:   （见 `_reap()`：pty 有产出 ⇒ `last_activity` 续命 ⇒ 不回收）。
#: 默认 120s：给「用户只是切走了页面、马上回来」留缓冲，同时不让
#: 一个关掉的页面把 485MB 挂几十分钟。
#: 设 0 = 关闭这一档（只按 IDLE_TTL_S 判）。
UNWATCHED_TTL_S = int(os.getenv("TERM_UNWATCHED_TTL", "120"))
MAX_SESSIONS = 8
# 回收心跳间隔（P0-4）：早先 _reap() 只寄生在 list_sessions() 上，前端一关就没人回收
REAP_INTERVAL_S = float(os.getenv("TERM_REAP_INTERVAL", "60"))
# ── 子进程零输出即死的快速通道（PT-20260929-02 补做，2026-10-08）──
# 缺口的确切形状：探活机制**已在**（`poll_exited` 用 waitpid(WNOHANG) 独立探活、
# `_reap()` 遍历全部 `_sessions`、`reap_loop` 是 startup 里 spawn 的后台任务），
# 唯一的洞是**节律**：REAP_INTERVAL_S=60 意味着子进程零输出即死后最长 60 秒
# 才被发现。这60 秒里前端是永久空白：
#   · PTY master 只产生 POLLHUP、不产生 EPOLLIN ⇒ asyncio `add_reader` 回调不触发
#     ⇒ `on_readable` 不跑 ⇒ 没人置 alive=False；
#   · `pump()` 的 `timeout=2.0` 分支只判 `if not sess.alive: break`，而 alive 只由
#     `_cleanup()` 置 ⇒ 也不 break ⇒ 一直等；
#   · `GET /api/term/sessions` 里该 sid 的 exit_reason 为空。
# 也就是 09-30 报障「hermes 秒死、on_readable 一次不进、前端永久空白」的现象。
#
# 为什么不能靠「给add_reader 也监听 HUP」：POLLHUP 在 fd 层与「对端刚写完最后
# 一个字节」的瞬时态同签名，一次就收会把正常收尾误判成死亡（_REAP_EOF_CONFIRM
# 正是为这个才要 3 次确认）。而 waitpid(WNOHANG) 是**无歧义**的探针 —— 只是需要
# 有人定期调它。
#
# 所以修法不是重写 reap，而是给「唯一无事件可监听」这条路径加一条快车道：
# 每 2s 跑一次**只做探活不做 TTL 判定**的轻量扫描。分工：
#   fast_reap_loop（2s）  —— 只答「进程还在不在」，死了立刻 _drop
#   reap_loop（60s）      —— TTL / 无生命迹象判定、配额释放、_dying 兜底
# 两者都走`poll_exited()`，幂等（exit_status 首次记录优先），并发跑不冲突。
#
# 设 0 即完全关闭快速通道（退回 v0.13.94 行为，只变慢不变错，留作逃生口）。
_FAST_REAP_INTERVAL_S = float(os.getenv("TERM_FAST_REAP_INTERVAL", "2.0"))
# P0-3：PTY 写背压时的让出时长。5ms 与 coalescer 窗口同量级——短到用户无感，
# 长到足以让事件循环去处理别的 IO（否则忙等反而更糟）。
_WRITE_BACKPRESSURE_S = 0.005
# P0-3：这些 errno 是「对端还活着，只是现在写不进去」⇒ 重试而不是断链。
_WRITE_RETRY_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR})
# P0-2：连续多少次「PTY 读端可读、但读到 EIO」才判定子进程已死并收尸。
# 单次 EIO 就回收是错的——见 _reap_once 的注释（EIO 也可能是对端刚写完最后一个
# 字节的瞬时态）。这里给 3 次连续确认，配合 REAP 间隔，最坏多占 3 分钟配额。
_REAP_EOF_CONFIRM = 3

# ── auth_url 旁路（P2/P3，v0.13.64）────────────────────────────────────────
# v0.13.87 双通道改造（用户 2026-10-07 报障「嵌入式终端总是提示登录链接」）：
# 原来只有一条通道，判据是「pty 输出里出现 http(s) URL」⇒ 凡是 agent 印一条文档/
# 仓库地址（`See https://github.com/openai/codex`、`https://code.claude.com/docs/...`）
# 就弹「检测到登录链接，点此打开 ↗」—— 纯误报，它根本不是登录链接。
# 现拆成两条，判据各归各：
#   ① 登录通道 `_scan_login_urls` —— URL **必须**配上同一扇输出里的登录线索词
#      （见 DEFAULT_AUTH_CUES）才播。线索是**准入条件**而非加分项：宁可漏一次登录
#      提示（画面里 URL 仍可复制，只是少了可点按钮），也不能再把文档链接叫登录。
#      用户口径（2026-10-07）：本机所有终端都**不需要登录**（凭据在 env/配置文件里，
#      各 agent 自读），故①的收益本来就极小、误报代价却极大。
#   ② 页面通道 `_scan_page_urls` —— 无线索词，但 URL 与一句人话同处一行时播
#      `page_url`（文案必须走正文归一化，**不许**再叫「登录」）。这不丢用户真正要的
#      「agent 贴了链接让我打开」。
# v0.13.88（用户 2026-10-07 **二次**报障「页面一出现链接就弹登录远程链接」）：
# v0.13.87 只做了"线索词 + 方向"，**判定范围仍是整段 16KB scrollback** ⇒ 一次真
# 登录提示播过之后，`AUTH_CUE_BACK_LINES = 10` 把它撑成一条**会跟着输出走的中毒带**：
# 其后任意普通链接只要落进那 10 行内就被追认成登录（影子实例实测复现 4 例：裸文档
# URL、提示符后 8 行的两条链接、人话行上方的链接）。根因在**位置轴**，不在线索词：
# 线索与 URL 必须同属**当前这一扇输出**。故登录通道只在 `sess.last_chunk`（本轮
# PTY 读到的这块）里判，`AUTH_CUE_BACK_LINES` 同步收到 3 行；页面通道继续用
# scrollback（链接发现需要跨块）。这一轴正是实测能证伪的那一条：只把窗口从 10 行收到
# 3 行**不够** —— 误报里紧挨线索的那条文档链接仍在 3 行内，只有"换一块输出就清零"
# 才能把它们分到两侧。
# 取舍：流式 agent 把登录话术与链接隔了多行/多块时会漏播登录提示 —— 画面里 URL 仍
# 可复制，只少了可点按钮，而误报「登录」是天天可见的噪声（用户口径：本机终端都不需要
# 登录，误报远比漏报烦人）。
# 线索表留运维口子（DB `auth_url_cues` / 环境变量，逗号分隔）。
# 为什么要在**服务端**做：URL 出现在 pty 输出字节流里，而前端拿到的是「已经过
# 一轮 JSON/WS 封装」的数据；更关键的是要看到未渲染的原始行，才能把被终端宽度
# 折断的 URL 拼回来（见 _scan_urls 的跨行拼接）。
# v0.13.89（用户二次报障当天的第三次裁定）：
#   「总是出现 检测到链接 这样的提示」⇒ 处理口径「**全部静默，连登录提示也不要**」。
#   前情：同日先报「总是提示登录链接」，v0.13.87 把标签纠正成登录/链接两路之后，
#   浮层仍然照弹 ⇒ 真实诉求不是"标签写错了"，而是**不要在终端里弹这类提示**。
#: ★★ 带外链接通道**总开关**（v0.13.89）。默认 False = 关闭整条通道：
#: 任何链接（普通文档链接与真登录提示一视同仁）都**不再弹浮层**，也**不在终端里插
#: 图注行**（`[链接] …` / `[登录链接] …` 都不再出现）。pty 输出本身不受影响 ——
#: URL 照旧画在画面上，可选中复制，xterm 的 WebLinksAddon 也照旧让它可点。
#: 为什么留开关而不是删代码：整条通道有 E/F/D 组十几条回归闸门护着（判例 87），
#: 删掉要连带撤掉这些判据，日后想用还得重写一遍。要再打开：把本常量改成 True 并重启
#: hub —— 届时行为即 v0.13.88 的语义（登录路只认当前这一块的线索词、页面路要同行正文）。
LINK_BYPASS_ENABLED = False
URL_SCAN_MAX = 16384      # 扫描窗口上限（16KB）。够覆盖一屏多行，不会无界增长
URL_MIN_LEN = 20          # 短于此不像真的登录 URL（避免把 http://x 这类噪声弹出去）
#: 登录线索词（**准入条件**），分**方向**两类。取证来源：本机实测的真实 OAuth 提示
#: （`claude setup-token` v2.1.292）原文 —— "Browser didn't open? Use the url
#: below to sign in (c to copy)"；其余取自各 CLI 的 device-flow / OAuth 惯用文案。
#:
#: ★ 形状要求：**不许出现"光秃秃的单词"**（`auth` / `login` / `oauth` / `signin` /
#: `authenticate` 这类）。理由可证：这类词在技术文档与代码讨论里随处可见
#: （"the auth module"、"authenticate with the API"），一旦入表就等于把误报装回去。
#: 故一律要求带分隔（空格/连字符/冒号）的**短语**。
#: `tests/test_term_touch_authurl.py::test_e5` 把这条钉成可断言判据。
#:
#: ★★ 方向（v0.13.87 第二轮实测逼出来的）：线索词**自己就说明了 URL 在它的哪一侧**
#: —— "use the url **below**" 说的是"URL 在下面"。第一版不分方向、只看邻近窗口，
#: 于是影子实例里出现了可证伪的误报：`echo ... gateway http://127.0.0.1:3456/v1`
#: 的输出（URL 在**上**），被**下一行**那句 "Use the url below to sign in" 追认成登录。
#: 分方向后这个形状天然不成立 —— 前置线索不认它上方的 URL。
#: 故：PRE 表 = 线索必须在 URL **之前**（含同行）；POST 表 = 线索必须在 URL **之后**。
AUTH_CUES_PRE: List[str] = [
    "use the url below", "use this url", "sign in", "sign-in",
    "log in", "log in at", "login at", "log-in at",
    "please authenticate", "authenticate at", "to authenticate",
    "please authorize", "authorize access", "device code", "device auth",
    "verification code", "enter the code", "copy the code", "open this url",
]
#: 跟在 URL **之后**的线索（"打开上面这条"的语气）。
AUTH_CUES_POST: List[str] = [
    "press enter to open", "continue in your browser", "open_url:",
]
#: 兼容旧名字：`url_has_auth_cue` 未指定方向时按 PRE 表判（这是绝大多数提示的形状）。
DEFAULT_AUTH_CUES: List[str] = AUTH_CUES_PRE
#: 页面通道的正文判据：URL 必须与一句**讲人话**的行内文本同处一行。
#: 只认 ≥3 个字母/汉字的词 ⇒ "-> / --" 之类的符号行不会被当成人话。
_PAGE_TEXT_RE = re.compile(r"[A-Za-z\u4e00-\u9fff]{3,}")
# ANSI/VT 转义序列：CSI（含私有参数 ?>）、OSC（到 BEL 或 ST）、单字符 ESC 序列。
# OSC 尤其要紧 —— 很多 CLI 用 OSC 把超链接写进输出（OSC 8 ; url ; text ST），
# 不剥掉就会把 "8;;https://..." 当成 URL 弹出去。
_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"      # OSC ... BEL / ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"                # CSI（含 ?private）
    r"|\x1b[@-Z\\-_]"                          # 两字符转义
)
# URL 尾随标点：终端输出里 URL 常直接跟句号/括号，去掉才是真 URL。
_URL_TRAIL = "()[]{}<>.,;:!?'\""
_URL_RE = re.compile(r"https?://[^\s\x1b\x07<>\"'`\\]+")
# 「合法 URL 字符」整行：用于判断下一行是否只是上一行 URL 的折行续接。
_URL_CONT_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "-._~:/?#[]@!$&'()*+,;=%")


def strip_ansi(text: str) -> str:
    """剥掉 ANSI/VT 转义序列（纯函数，供单测直接喂值）。

    不剥会有两个真后果：① OSC 8 超链接协议里的 URL 被当正文扫出来（弹一堆假链接）；
    ② 光标定位/清行序列（\\x1b[2J、\\x1b[H）混在 URL 里，URL 就拼不完整。
    """
    return _ANSI_RE.sub("", text)


def _normalize_url(raw: str) -> Optional[str]:
    """清洗并校验单个 URL；不合格返回 None（调用方据此不播）。

    只放行 http/https —— pty 输出里的 file://、javascript: 之类一律不往浏览器送。
    两头都要剥标点：输出里 URL 经常被夹在 " (...)" 或 "<...>" 里，只 rstrip 的话
    前面那个 '(' 会留在串首，urlparse 直接解析不出 scheme ⇒ 整条被丢掉。
    """
    u = raw.strip().strip(_URL_TRAIL)
    if len(u) < URL_MIN_LEN:
        return None
    try:
        p = urlparse(u)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    return u


#: 登录线索的判定窗口（URL 所在行**之前/之后**各看几行）。
#: ★ 三层收紧都是实测逼出来的：
#:   ① v0.13.87 第一版把线索词与**整个 16KB 滚动窗口**比 ⇒ 真登录提示出现后，屏上
#:      **先前的**文档链接被追认为"登录"（影子实例实测连播 3 条误报）。真提示的线索
#:      就在 URL 邻近几行（claude setup-token 实测：上一段 2 行内），故窗口收到 ±10/3 行。
#:   ② v0.13.87 第二轮：只收窗口还不够 —— 线索词自带方向（"use the url below" =
#:      URL 在下面），不分方向时"上一行的普通地址 + 下一行的登录话术"这个形状照样误报。
#:      故最终判据是 `auth_cue_for()` 的方向表（见 AUTH_CUES_PRE/POST）。
#:   ③ v0.13.88：±10 行本身仍是**滚动窗口**的行数，一次线索播出即等于给其后 10 行
#:      上了毒（用户二次报障的实证）。收到 3，且窗口只取**本轮 PTY 读到的那一块**
#:      （`sess.last_chunk`），不再看整段 scrollback —— 换一块输出就清零。
AUTH_CUE_BACK_LINES = 3
AUTH_CUE_FWD_LINES = 3
#: 登录通道参与判定的**当前块**字节上限：单次 PTY 读可能有几十 KB，全喂给正则
#: 既废算力也没意义（线索必须紧邻 URL，取尾部即含最新输出）。
AUTH_CHUNK_MAX = 8192

#: 折行拼接的**反例排除**：shell 提示符行（`user@host:/path$`）的字符恰好全在
#: URL 续接字符集里，会被误当成上一行 URL 的续接（实测把提示符粘进 URL 尾巴，
#: 生成一个 404 链接）。这不是本批新增的缺陷，但本批重写该函数时有责任把它钉住。
_PROMPT_LINE_RE = re.compile(r"^[\w.\-]+@[\w.\-]+:.*[$#%>]$")


def _url_lines(buf: str) -> "List[Tuple[int, str, str]]":
    """逐行产出 `(行号, URL, 该行原文)`（纯函数）。

    保留**行号与整行原文**是 v0.13.87 的关键：登录线索词与正文判据都必须在
    「URL 邻近几行」这个范围内判定，只拿到裸 URL 就判不了 —— 原来只看「整个滚动
    窗口里有没有 URL」，与上下文无关，这正是误报的成因。
    """
    out: List[Tuple[int, str, str]] = []
    for idx, line in enumerate(buf.split("\n")):
        for m in _URL_RE.finditer(line):
            out.append((idx, m.group(0), line))
    return out


def _join_wrapped_urls(buf: str) -> "List[Tuple[int, str]]":
    """跨行拼接：终端按列宽硬折，长 URL 会被劈成两行（返回 `(行号, 候选)`）。

    正则逐行匹配只能拿到半截 ⇒ 半截 URL 打不开。做法：行内匹配到开头后，
    若后续行**整行都是合法 URL 字符**，就续接到上一行。提示符形状的行排除（见
    `_PROMPT_LINE_RE`）。
    """
    out: List[Tuple[int, str]] = []
    lines = buf.split("\n")
    for i in range(len(lines) - 1):
        head, nxt = lines[i].strip(), lines[i + 1].strip()
        if not head or not nxt:
            continue
        if not _URL_RE.search(head):
            continue
        if nxt.startswith("http"):
            continue
        if not all(ch in _URL_CONT_CHARS for ch in nxt):
            continue
        if _PROMPT_LINE_RE.match(nxt):        # 提示符不是 URL 的续接
            continue
        out.append((i, head + nxt))
    return out


def url_has_auth_cue(window: str, **kw) -> bool:
    """该窗口里有没有登录线索词（纯函数，保留给诊断/测试；播报判据见下）。

    方向不在这里判 —— 走 `auth_cue_for()`。本函数是「窗口里有没有任意线索」的
    粗判，用于排障时回答"这扇输出到底像不像在催登录"。
    """
    if "cues" in kw:
        cues = kw["cues"] or AUTH_CUES_PRE
        return any(k in window for k in cues)
    return any(k in window for k in AUTH_CUES_PRE + AUTH_CUES_POST)


def auth_cue_for(lines: List[str], idx: int, **kw) -> str:
    """返回**命中**的线索词（空串 = 没命中）。方向敏感，这是登录通道的真闸门。

    方向规则（v0.13.87 第二轮实测）：
      · PRE 表 —— 线索必须在 URL **之前**（含同行）：`lines[max(0,idx-BACK):idx]`
      · POST 表 —— 线索必须在 URL **之后**：`lines[idx+1 : idx+1+FWD]`
    为什么非要分方向：线索词自己就说明了 URL 在哪一侧，"use the url below" 说的是
    "URL 在下面"。不分方向时，影子实例实测出现：`gateway http://127.0.0.1:3456/v1`
    这条**在上一行**的普通地址，被**下一行**那句 "Use the url below to sign in"
    追认成登录链接（可证伪的误报）。
    """
    cues_pre = kw.get("cues_pre") or AUTH_CUES_PRE
    cues_post = kw.get("cues_post") or AUTH_CUES_POST
    back = "\n".join(lines[max(0, idx - AUTH_CUE_BACK_LINES):idx]).lower()
    fwd = "\n".join(lines[idx + 1:idx + 1 + AUTH_CUE_FWD_LINES]).lower()
    for k in cues_pre:
        if k in back:
            return k
    for k in cues_post:
        if k in fwd:
            return k
    return ""


def _scan_login_urls(buf: str, **kw) -> List[str]:
    """登录通道：URL 邻近**方向正确**的窗口里必须有线索词才认（v0.13.87）。

    返回**已归一化**的 URL 列表（跨行拼回来的那条也在内），调用方直接播。
    同行线索算 PRE（URL 与线索同处一行时，两种语气都说得通）。

    v0.13.88：`buf` 的**来源**是本通道判据的一部分 —— 泵只把**本轮那块**输出传进来
    （见 pump 里的 `sess.last_chunk`），不再传整段 scrollback。原因是可证伪的：
    传整段时，一次真登录提示会把其后 10 行输出整段毒化（用户二次报障）。
    """
    lines = buf.split("\n")
    cands: List[Tuple[int, str]] = [(i, u) for i, u, _l in _url_lines(buf)]
    cands += _join_wrapped_urls(buf)
    out: List[str] = []
    for idx, raw in cands:
        u = _normalize_url(raw)
        if not u or u in out:
            continue
        if not auth_cue_for(lines, idx, **kw):
            continue
        out.append(u)
    return out


def _scan_page_urls(buf: str) -> List[str]:
    """页面通道：URL 与一句**人话**同处一行 ⇒ 这是「用户想让你打开的页面」，不是登录。

    判据刻意与登录通道完全不同（v0.13.87）：无线索词要求，但要同行有 ≥3 字的正文词。
    这样既覆盖 `See https://github.com/openai/codex for details`（agent 给的参考页），
    又不会把「一整屏机器输出里的裸 URL」刷成弹窗 —— 后者的行里没有正文词。
    """
    out: List[str] = []
    for _idx, raw, line in _url_lines(buf):
        body = _URL_RE.sub("", line)
        if not _PAGE_TEXT_RE.search(body):
            continue
        u = _normalize_url(raw)
        if u and u not in out:
            out.append(u)
    return out


def _scan_urls(buf: str) -> List[str]:
    """所有候选 URL（登录 + 页面两条通道的并集，归一化后去重）。

    v0.13.87 起**仅供诊断/单测**，不再直接决定播报 —— 播报判据在
    `_scan_login_urls`（线索词准入）与 `_scan_page_urls`（正文词准入）里。
    保留本函数是因为「pty 里到底出现了哪些 URL」是排障时要看的第一手证据。
    """
    out: List[str] = []
    for _idx, raw, _line in _url_lines(buf):
        u = _normalize_url(raw)
        if u and u not in out:
            out.append(u)
    for _idx, raw in _join_wrapped_urls(buf):
        u = _normalize_url(raw)
        if u and u not in out:
            out.append(u)
    return out


# 内存耗尽时 bun/JSC 会**主动** abort：ASSERTION FAILED: MemoryExhaustion →
# JSC::LocalAllocator::allocateSlowCase 调 __builtin_trap() → ud2 → SIGILL。
# 内核只记一行 `trap invalid opcode`，终端上原本只显示「[会话结束]」⇒ 用户无从得知
# 是内存问题（2026-09-25 排查 opencode 菜单秒退时实测：dmesg + objdump + ulimit -v
# 三步才定性，全程 hub 没有任何提示）。SIGKILL 同理：可能是 OOM-killer 也可能是 hub 强杀。
_MEM_SUSPECT_SIGNALS = frozenset({signal.SIGILL, signal.SIGSEGV, signal.SIGBUS,
                                  signal.SIGABRT, signal.SIGKILL})


def describe_exit(status: Optional[int], hub_killed: bool = False) -> str:
    """把 waitpid 的 status 解成人话（纯函数，供单测直接喂值）。

    hub_killed：本进程是不是 hub 自己动的手（用户点 × / 空闲 TTL / 服务退出）。
    SIGKILL 与 SIGTERM 是**歧义信号** —— 既可能是内核 OOM-killer，也可能是我们自己发的。
    知道是自己发的就如实说，绝不把"用户主动关会话"渲染成"内存不足"吓人。

    返回**纯文本**片段（不含 ANSI，调用方自己包样式）；无信息时返回空串，
    调用方据此回落到原来的「[会话结束]」，绝不编造原因。
    """
    if status is None:
        return ""
    try:
        if os.WIFSIGNALED(status):
            sig = os.WTERMSIG(status)
            try:
                name = signal.Signals(sig).name
            except ValueError:
                name = f"signal {sig}"
            if hub_killed and sig in (signal.SIGTERM, signal.SIGKILL):
                return f"由 hub 主动终止（{name}）"
            hint = "（疑似内存不足）" if sig in _MEM_SUSPECT_SIGNALS else ""
            return f"被信号 {name}({sig}) 终止{hint}"
        if os.WIFEXITED(status):
            code = os.WEXITSTATUS(status)
            return "正常退出" if code == 0 else f"退出码 {code}"
    except (ValueError, OSError):
        return ""
    return ""


def _check_term_token(provided: str, source: str) -> None:
    """缺 token 即拒：校验失败记一行拒绝原因（绝不记录 token 值）"""
    if not provided or not hmac.compare_digest(provided, TERM_TOKEN):
        log.warning(f"[term] 拒绝：token 校验失败（{source}）")
        raise HTTPException(status_code=401, detail="term token required or invalid")


def _ensure_settings_table() -> None:
    """登录线索词存 DB 的键值表（与 ghsettings._ensure_table 同形）。"""
    db.execute("CREATE TABLE IF NOT EXISTS term_settings ("
               "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")


def auth_cues() -> List[str]:
    """当前生效的登录线索词：DB `auth_url_cues` → 环境变量 `AUTH_URL_CUES` → 默认表。

    留这个口子的理由：线索表是**行为闸门**（登录提示准不准全靠它），换文案/加语言时
    不该逼人改代码重启。读取顺序与 ghsettings 的 DB → env → 默认同口径。
    任一步读坏都回落到默认表 —— 线索读不到时**不能**把闸门放开（宁漏勿误报）。
    """
    raw = ""
    try:
        _ensure_settings_table()
        rows = db.query("SELECT value FROM term_settings WHERE key=?", ("auth_url_cues",))
        raw = str(rows[0]["value"]) if rows else ""
    except Exception:  # noqa: BLE001  表没建好只影响自定义线索，不许拖垮终端
        raw = ""
    if not raw:
        raw = os.getenv("AUTH_URL_CUES", "")
    cues = [c.strip().lower() for c in raw.split(",") if c.strip()]
    # 运维口子给了就整表替换（PRE 语义）；没给则用内置的**方向**两张表（见
    # AUTH_CUES_PRE/POST）—— 调用方据此决定要不要传 kw。
    return cues or list(AUTH_CUES_PRE)


def child_env() -> Dict[str, str]:
    """终端子进程的环境（纯函数，供单测直接断言）。

    PATH 必须前置 `profiles.extra_path_dirs()`：hub 服务进程 PATH 不含 nvm，而
    `pi` 这类 `#!/usr/bin/env node` 的 npm 全局 CLI 会让内核拿系统 node v20 去跑
    需要 Node 22+ 的 bundle ⇒ 启动即 SyntaxError（详见 profiles.extra_path_dirs）。
    只补子进程、不动服务进程自身 PATH、不动 systemd 单元配置。"""
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    # omp 嵌入式终端跳过首次启动向导（setup wizard），否则每次新建会话都会
    # 弹「选择 provider / 选择模型」交互界面，无法直接进入对话框。omp 的 models.yml
    # 已配置 ccr provider，无需向导。
    env["OMP_SKIP_SETUP"] = "1"
    extra = profiles.extra_path_dirs()
    if extra:
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


#: 新建 PTY 的初始尺寸（2026-10-11）。必须**真**设进 pty（见 Session.__init__ 里的
#: 「初始 PTY 尺寸」注释）：pty.fork() 的 winsize 是 0×0，而 grok 的 TUI 读到 0 尺寸
#: 会永久卡在启动（128 字节、再无输出，且事后 SIGWINCH 唤不醒）。80×24 是前端首帧
#: 抵达前的安全兜底，也是 Session.rows/cols 一直以来的宣布值。
INITIAL_ROWS = 24
INITIAL_COLS = 80


class Session:
    def __init__(self, sid: str, agent_id: str, cmd: List[str], cwd: str):
        self.id = sid
        self.agent_id = agent_id
        self.cmd = cmd
        self.cwd = cwd
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # 子进程：替换为终端程序
            try:
                env = child_env()
                os.chdir(cwd)
                os.execvpe(cmd[0], cmd, env)
            except Exception as exc:  # noqa: BLE001
                # PT-20260929-02 补做（此项从未进 master）：原实现 `os._exit(127)`
                # 把死因**完全吞掉**——父进程只看到「PTY 零字节 + 干净关闭」，
                # 与「CLI 自己启动即退」在 fd 层同签名，于是无法区分「exec 失败」
                # 和「CLI 崩了」，排障时是个哑谜。现在双写：
                #   ① stdout（journald 可 grep）——服务侧留证据
                #   ② PTY（用户可见）——终端里直接看到「无法启动 X」
                # 成本只在失败路径（正常启动零开销）。
                try:
                    import traceback
                    log.warning("[term] 子进程 exec 失败 cmd=%r cwd=%r: %s"
                                % (cmd[0], cwd, traceback.format_exc()))
                except Exception:  # noqa: BLE001
                    log.warning("[term] 子进程 exec 失败 cmd=%r（traceback 也拿不到）"
                                % (cmd[0],))
                try:
                    os.write(2, ("\r\n[hub: 无法启动 %s —— %s]\r\n"
                                 % (cmd[0], exc)).encode())
                except OSError:
                    pass
                os._exit(127)
        # 关掉从端行规回显（父进程侧；2026-10-08 用户报障「cursor 启动会带入乱码字符」）。
        # pty.fork() 的从端默认 ECHO 打开，而 Node/Ink 类 CLI（cursor-agent）是**先发终端
        # 查询、之后才进 raw 模式**：它开机第一帧发 `\x1b]11;?\x07`（问背景色），浏览器
        # xterm 对**实时帧**里的这个查询自动作答，把 `\x1b]11;rgb:…` 灌回 pty 时正落在
        # 「ECHO 还开着」的窗口里 ⇒ 被 tty 原样回显成 `^[]11;rgb:1c1c/…^\`，即用户看到的乱码。
        # 回放查询闸门（前端 termWriteReplay）只作用于连接后**第一帧**，而 cursor 启动慢
        # （≈0.6s）⇒ 查询落在实时帧，闸门拦不到（jcode/claude 查询在 ring 里，故无此症）。
        # 只清 ECHO 系标志、不动其它位：交互式 shell（bash/readline）起来时自己重设 termios
        # 会恢复回显，TUI 应用也各自设 raw ⇒ 两类画像均无回归（本地实测）。
        try:
            _attr = termios.tcgetattr(self.fd)
            _attr[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHOCTL)
            termios.tcsetattr(self.fd, termios.TCSANOW, _attr)
        except OSError:
            pass   # 关不掉也不能让建会话失败（最坏退化为「仍可能回显」）
        # ── 初始 PTY 尺寸（2026-10-11，grok 卡死根因）──────────────────────────────
        # pty.fork() 给的是 **0×0** winsize（本机实测 TIOCGWINSZ = (0,0,0,0)），
        # 而原实现只在 Session 上填了 rows/cols 这两个 **Python 属性**、从不调 ioctl
        # ⇒ 子进程开局拿到的就是「终端 0 行 0 列」，而 _apply_size 又拿被硬的
        # 「80×24」跟这个假值比 ⇒ 前端若首帧 resize 恰是 80×24，短路生效、真 ioctl
        # 永不下发，pty 于是一直停在 0×0。
        # 实测（真 pty，只差这一个变量，各 3 轮全一致）：
        #   0×0      ⇒ grok 只吐 128 字节就永久停住（不发 `Grok Build` 界面）
        #   120×40   ⇒ 4.3s 正常渲染
        # 而且**事后补尺寸救不回来**：先 9s 0×0、再 TIOCSWINSZ 到 120×40，
        # 新增 0 字节（grok 已经把「没有尺寸」读死，SIGWINCH 唤不醒）。
        # 别的画像不受影响（同为 0×0，claude/codex/opencode/qodercli 照常出界面），
        # 是 grok 独有硬失败 —— 但它也是本机唯一的「嵌入式终端起不来」报告。
        # 先给 80×24（= 前端首帧前的安全兜底，也是原 rows/cols 的宣布值）：
        # 好处是「宣布值」与「真值」从此一致，前端随后的 resize 只在几何真的不同时才
        # 短路，`_apply_size` 的语义不再踩到一个自己没设过的尺寸。
        _set_winsize(self.fd, INITIAL_ROWS, INITIAL_COLS)
        self.alive = True
        self.created = time.time()
        # 两个时钟刻意分开（v0.13.58 / 档二）：
        #   last_io      = 客户端最后一次**真实交互**（心跳不算）。只用于「用户多久没动手」的展示。
        #   last_activity = 会话最后一次**有生命迹象**（客户端交互 或 pty 产出）。
        # 旧实现只有一个时钟且被回收逻辑当「死活」判据 ⇒ 换设备/切后台时客户端静默
        # 45min，正在跑的任务被腰斩（实弹：重连收到 4410）。两个时钟分开后，
        # 回收只看 last_activity，「客户端不在」不再等价于「会话该死」。
        self.last_io = time.time()
        self.last_activity = time.time()
        self.cols, self.rows = INITIAL_COLS, INITIAL_ROWS
        # 每个观看者一条**独立**队列（P1-5）。
        # 旧做法：全会话共用一个 outputs 队列，而每个 WS 连接的 pump 都在同一个队列上 get()
        # ⇒ 两台设各（桌面 + 手机）同时看同一会话时，两个消费者会**互相偷字节**，
        #   各自只拿到一半输出（流被劈成两半，不是「少看到一些」而是内容永久错乱）。
        self.viewers: Dict[str, asyncio.Queue] = {}
        # 每个观看者一个输出合并器（B1）：与 viewers 同生共死，键同为 vid。
        # 放在「读一块就发一帧」之前，PTY 突发输出才不会被原样放大成 WS 帧洪水。
        self.coalescers: Dict[str, "_OutputCoalescer"] = {}
        self.dropped: Dict[str, int] = {}   # 观看者 -> 累计丢弃字节（慢消费者必须可见）
        self.ring = bytearray()  # 输出环形缓冲：重连回放，避免"重挂后白屏"
        self._cleaned = False    # 资源回收幂等守卫
        self.exit_status = None  # waitpid 原始 status；解出人话见 describe_exit()
        self.hub_killed = False  # True = 这次是 hub 自己动的手（点 × / TTL / 服务退出）
        self.resume_of = ""     # v0.13.0：非空 = 由某条磁盘历史续聊而来
        # v0.13.64 auth_url 旁路（P2/P3）：扫 pty 输出里的链接，带外发给前端开浏览器。
        # 手机上跑 `claude setup-token` 原本只能肉眼抄 URL。
        # url_buf = 只存**剥过 ANSI** 的文本滚动窗口（URL 可能被折行，见 _join_wrapped_urls）；
        # announced = 已播过的**键**，**会话级**共享（换端重连不重播，同端也不刷屏）。
        # v0.13.87：键带通道前缀 —— 同一条 URL 在登录/页面两条通道下语义不同
        # （文案与点击行为都不同），只按 URL 去重会让后到的那条被静默吞掉。
        # v0.13.88：last_chunk = **本轮 PTY 读到的这块**（剥过 ANSI），登录通道的
        # 判定范围（见 AUTH_CUE_BACK_LINES ③）。与 url_buf 分开是**位置轴**的要求：
        # url_buf 是整段 scrollback，拿它判登录会让一次真提示毒化其后 10 行输出。
        self.url_buf = ""
        self.last_chunk = ""
        self.announced_urls: set = set()
        # v0.13.59 终端会话录制（PT-20260927-16，2026-10-08 重做接线）：
        # TERM_RECORD 缺省 0 ⇒ Recorder.__init__ 直接短路，active=False，
        # 下面三个调用点全是`if _rec.active` 的一次属性读 ⇒ 关闭态零开销、不建表写入。
        # 刻意挂在 Session 上而不是模块全局：一条会话一条录制，且随会话一起走完生命周期。
        # Recorder 只吃 pty 字节 / 客户端按键，从签名上够不到 self.child_env()
        # （env 里有 TERM_TOKEN）—— 断"记初始环境变量"这条路靠签名，不靠自觉。
        self.recorder = term_record.Recorder(sid, agent_id)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, os.O_NONBLOCK)

    def to_dict(self):
        return {"id": self.id, "agent_id": self.agent_id, "cmd": " ".join(self.cmd),
                "cwd": self.cwd, "alive": self.alive,
                "created": self.created, "resume_of": self.resume_of,
                "exit_reason": describe_exit(self.exit_status, self.hub_killed),
                "idle_s": round(time.time() - self.last_io),
                # activity_s：距上次「有生命迹象」的秒数。idle_s 涨而 activity_s 不涨
                # = 客户端不在、agent 也没在跑（这才是真该回收的形态）。
                # getattr 兜底同 _reap()：有测试用 Session.__new__ 绕过 __init__ 造实例，
                # 读路径不该因为少一个时钟字段就 AttributeError（契约不该由实现细节决定）。
                "activity_s": round(time.time() - getattr(
                    self, "last_activity", self.last_io))}

    def _signal_group(self, sig):
        """pty.fork 子进程是会话首进程（pgid=pid）→ 组灭可带走它派生的子进程"""
        try:
            os.killpg(self.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(self.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def poll_exited(self) -> bool:
        """非阻塞探活（P0-2）：子进程是否已经退出。退出则顺带记录 exit_status。

        为什么必须用 waitpid 而不能只看 PTY 可读性：
        「PTY 读端可读」既可能是对端退出了，也可能是对端刚写了数据、也可能对端还活着
        只是暂时没输出——三种情况在 fd 层同签名。waitpid(WNOHANG) 是唯一能区分
        「进程还在」与「进程没了」的探针，而且不阻塞、不依赖有没有观看者。
        已经退出时把状态记进 exit_status，让 describe_exit() 能把死因上屏。
        """
        if self.exit_status is not None:
            return True                     # 已有结论：不必再探（_cleanup 已收尸过则此处无副作用）
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except (ChildProcessError, ProcessLookupError):
            # 没有这个子进程（已被 _cleanup 收走或压根没起来）⇒ 视同已退出
            self.exit_status = self.exit_status if self.exit_status is not None else -1
            return True
        except OSError:
            return False                    # 真·暂时查不到：保守当作还活着，下轮再探
        if pid == 0:
            return False                    # WNOHANG 返回 0 = 还在跑
        self.exit_status = status           # 已退出：记下原因（首次记录优先）
        return True

    def kill(self):
        """TUI 常忽略 SIGHUP：SIGTERM → 2s 后仍活则 SIGKILL 升级（组级）"""
        self.hub_killed = True   # 之后看到的 SIGTERM/SIGKILL 是我们自己发的，不是 OOM
        self._signal_group(signal.SIGTERM)
        try:
            loop = asyncio.get_event_loop()
            loop.call_later(2.0, self._force_kill)
        except RuntimeError:
            pass

    def _force_kill(self):
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid == 0:  # 还活着 → 强杀整组
                self._signal_group(signal.SIGKILL)
            elif self.exit_status is None:
                self.exit_status = status   # 已自行退出：留下面因（首次记录优先，不被后续覆盖）
        except (ChildProcessError, ProcessLookupError, PermissionError):
            pass
        # 升级后收尾：短暂宽限再收尸并释放资源（不依赖列表还在展示它）
        try:
            loop = asyncio.get_event_loop()
            loop.call_later(1.5, self._cleanup)
        except RuntimeError:
            self._cleanup()

    def _cleanup(self):
        """幂等回收：摘 reader → 关 fd → 收尸。任何路径（正常退出/组灭/reap）都走这里"""
        if self._cleaned:
            return
        self._cleaned = True
        self.alive = False
        try:
            asyncio.get_event_loop().remove_reader(self.fd)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        for _ in range(2):
            try:
                pid, st = os.waitpid(self.pid, os.WNOHANG)
                if pid == 0:
                    break
                if self.exit_status is None:
                    self.exit_status = st   # 原为 `_st` 直接丢弃 ⇒ 崩溃原因永远上不了屏
            except (ChildProcessError, ProcessLookupError, OSError):
                break
        # v0.13.59 录制收尾：落 ended + 跑 7 天保留期清理。
        # 挂在这里而不是 kill()/某一条退出路径 ⇒ _cleanup 是**全仓唯一**的收口
        # （正常退出/组灭/reap/TTL 都走它），漏挂一处就等于该路径的录制永远不 close。
        # close() 内部对未开启的录制是no-op，关闭态零开销。
        try:
            self.recorder.close()
        except Exception:  # noqa: BLE001  旁路功能不许打断会话回收
            pass


_sessions: Dict[str, Session] = {}
#: 被显式杀掉、等待 reap_loop 兜底收尾的会话（key=id(sess) 防 sid 复用撞车）。
#: 与 _sessions 分开：已杀的会话不能继续出现在列表/配额里，但资源还得有人收。
_dying: Dict[int, "Session"] = {}


# 输出合并窗口（毫秒）。5ms 是 paseo 实测值（上游 docs/terminal-performance.md:5-16；摘引见本仓 docs/PASEO-FUSION-PLAN-20260929.md），
# 不是随手取的：再大就会让按键回显可见地发黏，再小则合并不住 npm run build 这种
# 持续高频吐字节的场景。
TERM_COALESCE_MS = 5.0


class _OutputCoalescer:
    """前后沿节流的输出合并器（思路取自 paseo TerminalOutputCoalescer，按 Hub 形态重写）。

    为什么不能只用后沿（普通 debounce）：那样**每次**按键回显都会被平白加一个窗口的
    延迟（paseo 文档原话：reverting to trailing-only adds a full window to every
    keystroke echo）。前沿规则是：距上次刷出已超过 delay ⇒ 立刻发，交互延迟不升反降。

    部署位置：挂在「PTY 读回调 → 每观看者队列」之间，即离 PTY 最近的那一层。
    挪到更下游（比如 WS 发送前）只是换了个挨打的地方——主循环照样被高频唤醒。
    """

    def __init__(self, on_flush, delay_ms: float = TERM_COALESCE_MS):
        self._on_flush = on_flush
        self._delay = delay_ms / 1000.0
        self._buf = bytearray()
        self._timer = None          # asyncio TimerHandle；非 None = 正在攒一趟
        self._last = None           # 上次刷出的时刻（前沿判定用）

    def handle(self, data: bytes) -> None:
        if not data:
            return
        self._buf += data
        if self._timer is not None:
            return                  # 已在攒，等那个定时器统一刷
        now = time.monotonic()
        if self._last is None or now - self._last >= self._delay:
            self.flush()            # 前沿：空闲后的第一块立刻发（保按键回显）
            return
        self._timer = asyncio.get_event_loop().call_later(self._delay, self.flush)

    def flush(self) -> None:
        """立刻刷出缓冲。带外消息（退出提示等）发送前必须调它，否则会插到输出前面。"""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._buf:
            return
        payload = bytes(self._buf)
        self._buf.clear()
        self._last = time.monotonic()
        self._on_flush(payload)

    def close(self) -> None:
        """观看者离开时收尸：定时器不取消的话会一直攥着 loop 的引用。"""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._buf.clear()


# sid -> 持有尺寸所有权的观看者 vid。用 vid 字符串而非对象引用：
# Python 的弱引用语义与 paseo 的 WeakRef<object> 不同，拿 vid 记账更简单也更可查。
_size_owner: Dict[str, str] = {}

# 行列上限。pty 尺寸是 struct.pack("HHHH")，超过 65535 会抛 struct.error
# 打断整条 WS 收包循环；下界 1 是防止 0 行让 TUI 直接崩。
_ROWS_RANGE = (1, 500)
_COLS_RANGE = (2, 1000)


def _clamp(v, lo: int, hi: int) -> Optional[int]:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def _norm_intent(raw) -> str:
    """归一化尺寸意图。

    缺省（老客户端没有 intent 字段）⇒ claim：否则一次前后端版本错配就会让尺寸永远改不动
    （paseo 同口径，terminal-size-ownership.ts:32-38）。
    未知的非空值 ⇒ **update**（不放宽）：_apply_size 里只有 claim 能夺权，
    一切非 claim（含看不懂的值）都要求「本端已是所有者」。把未知值当 claim 等于让
    一个拼错的字段直接绕过所有权检查、把别人正在用的终端尺寸改掉 —— 与实现相反的
    旧 docstring 写的是「宁可要它先拿到所有权」，那正好是被刻意否掉的那条路。
    """
    if raw is None or str(raw).strip() == "":
        return "claim"
    s = str(raw).strip().lower()
    return s if s in ("claim", "update") else "update"


def _set_winsize(fd: int, rows: int, cols: int) -> bool:
    """把行列数写进真 PTY（TIOCSWINSZ）。返回是否成功。

    抽成函数是为了让「初始尺寸」这条路径可测（tests/test_term_size_owner.py
    用 openpty 的真 fd 钉它），并让 `_apply_size` 与本函数只有一份 ioctl 细节。

    行列上限不再是可选约束：struct.pack("HHHH") 超过 65535 会抛 struct.error，
    而调用方（`_apply_size`）已用 _ROWS_RANGE/_COLS_RANGE 夹过，本函数只负责
    「写不进就返回 False」，绝不让异常穿到调用者的高频路径上。
    """
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", int(rows), int(cols), 0, 0))
        return True
    except (OSError, struct.error):
        return False


def _apply_size(sess: Session, vid: str, rows, cols, intent: str) -> bool:
    """按所有权语义改 PTY 尺寸；返回是否真的改了（未改/被拒都返回 False）。

    纯记账 + 一次 ioctl，不抛异常：前端几何事件是高频且不可信的输入，
    让它炸掉 WS 收包循环等于把「拖一下窗口」变成「终端断开」。
    """
    # 只有 claim 能夺权；**其余一切**（含看不懂的意图）都要求「本端已是所有者」，
    # 否则一个手滑拼错的字段就等于把所有权检查整个旁路掉。
    if intent == "claim":
        _size_owner[sess.id] = vid        # claim 无条件夺权（含同尺寸也要转移）
    elif _size_owner.get(sess.id) != vid:
        return False                      # 非所有者：静默忽略（这就是要防的"偷尺寸"）
    r = _clamp(rows, *_ROWS_RANGE)
    c = _clamp(cols, *_COLS_RANGE)
    if r is None or c is None:
        return False
    if sess.rows == r and sess.cols == c:
        return False                      # 尺寸未变：不打扰 pty（等价于 paseo 的服务端短路）
    if not _set_winsize(sess.fd, r, c):
        return False
    sess.rows, sess.cols = r, c
    return True


class CreateIn(BaseModel):
    agent_id: str
    session_id: Optional[str] = None    # v0.13.0：续聊某条历史；仅接受形状合法且实盘存在的 id
    cwd: Optional[str] = Field(default=None, max_length=500)   # v0.13.30：本机项目页——pty 起在指定项目目录；只进 os.chdir，绝不进命令拼装
    # ⚠ P1-7 刻意**不**加 `allow_cwd_fallback` 字段：本端点的既有安全不变量是
    # 「请求体只许 agent_id / session_id / cwd 三个 id 型字段」（tests/test_term_launch_guard.py），
    # 加任何新字段都会破它。目录消失时的逃生路是「不带 session_id 的新会话」，本来就有。


def _cwd_or_none(raw: Optional[str]) -> Optional[str]:
    """body.cwd 的唯一入口校验（v0.13.30）。
    空/None → None（回落画像 cwd）；非空必须：绝对路径、实盘目录存在、
    不含 \\x00\\n;|&\\$\\`（与 resume_argv 的兜底栅栏同口径）。
    只返回已校验的字符串——create_session 里不许出现第二条读 body.cwd 的路径。"""
    if raw is None or not raw.strip():
        return None
    c = raw.strip()
    if not os.path.isabs(c):
        raise ValueError(f"cwd 必须是绝对路径: {c!r}")
    if any(ch in c for ch in "\x00\n;|&$`"):
        raise ValueError("cwd 含可疑字符")
    if not os.path.isdir(c):
        raise ValueError(f"cwd 目录不存在: {c}")
    return c





@router.post("/api/term/sessions")
async def create_session(body: CreateIn, request: Request):
    _check_term_token(request.headers.get("x-term-token", "")
                      or request.query_params.get("token", ""), "POST /api/term/sessions")
    prof = profiles.get_profile(body.agent_id)
    if not prof or not prof.get("terminal"):
        raise HTTPException(400, f"{body.agent_id} 无终端入口（仅画像白名单可拉起）")
    if len([s for s in _sessions.values() if s.alive]) >= MAX_SESSIONS:
        raise HTTPException(429, f"终端会话数达上限 {MAX_SESSIONS}")
    # v0.13.30：body.cwd 是新会话的起点目录（本机项目页）；校验失败 400。
    # cwd 只进 os.chdir（Session 子进程），绝不进命令拼装——命令仍只出自画像白名单。
    try:
        req_cwd = _cwd_or_none(body.cwd)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    cwd = req_cwd or prof["terminal"].get("cwd") or os.path.expanduser("~")
    if body.session_id:
        # 客户端只能给 id：命令仍由后端模板拼装，id 必须过形状正则 + 实盘存在双校验
        try:
            cmd = sessions_store.resume_argv(prof["id"], body.session_id, cwd)
        except ValueError as e:
            if "不在实盘清单" in str(e):
                raise HTTPException(404, str(e)) from e
            raise HTTPException(400, str(e)) from e
    else:
        cmd = shlex.split(prof["terminal"]["cmd"])
    if body.session_id:
        # 跳目录口径（09-22 裁定）之后，历史条目可能属于别的目录：pty 要起在**会话自己的 cwd**，
        # 否则等于"在技术文档目录里打开一条 agent-hub 的会话"。
        # P1-7：这里**不再静默回退**。记录过 cwd 但目录已消失 ⇒ 显式拒启（409），
        # 否则用户会在画像目录里拿到一条空的新会话，把「恢复失败」当成「已经续上」。
        # 记录里本就没 cwd（source='none'，如 cursor 空 cwd 条目）不算异常，照旧回落。
        cwd_ex = sessions_store.session_cwd_ex(prof["id"], body.session_id, cwd)
        reason = sessions_store.resume_cwd_guard(cwd_ex)
        if reason:
            raise HTTPException(409, reason)
        cwd = cwd_ex["cwd"]
    # v0.13.148：dsh 工作区联动（判例 257）。本机项目/GitHub 项目页按 cwd 拉起
    # dsh 时，先把 cwd 注册成 dsh 原生界面的工作区 —— 否则会话落在 Ungrouped，
    # 侧栏看不到项目目录，即用户报的「工作目录没有正常转到项目目录」。
    # 判据用 req_cwd（显式选了项目目录才联动；裸「新会话」用画像默认目录不动），
    # best-effort：dsh web 没开/超时/失败只记日志，绝不阻塞建会话。
    if prof["id"] == "dsh" and req_cwd:
        await asyncio.to_thread(dshws.ensure_workspace, cwd)
    resolved = profiles.which(cmd[0])
    if not resolved:
        raise HTTPException(400, f"命令 {cmd[0]} 未在本机找到")
    cmd[0] = resolved
    # v0.13.41：设置→模型里给该 agent 存过默认模型 ⇒ 按白名单追加 --model。
    # v0.13.51：续聊**同样**追加（此前只对的新会话追加）。原因见 modelcfg.terminal_argv：
    # 不带 --model 的启动走的是 agent 配置文件，而 CCR 每次启动会把 claude 的
    # env 三兄弟改回旧模型 ⇒ 续聊"重启后退回 qwen"正是这条缝。逐家 `--help` 实测
    # --model/-m 都在（codex 的 `resume` 子命令 help 里也有 -m），追加在模板末尾
    # 不动 resume 的位置参数；白名单外的 agent 一律返回 []，绝不猜 flag。
    cmd += modelcfg.terminal_argv(prof["id"], modelcfg.hub_model(prof["id"]))
    sid = uuid.uuid4().hex[:10]
    sess = Session(sid, prof["id"], cmd, cwd)
    sess.resume_of = body.session_id or ""
    _sessions[sid] = sess
    _attach_reader(sess)
    # P0-8：live_titles 实测 jcode 分支要 json.load 159 个文件/29MB（冷缓存 130ms+），
    # 跑在 async handler 里会按住整个事件循环。丢进线程池，标题晚几十毫秒无所谓，
    # 终端卡一下很在意。
    title = (await asyncio.to_thread(sessions_store.live_titles_cached, prof["id"]) or {}).get(sess.pid, "")
    if not title and sess.resume_of:     # 刚起来时各 CLI 未必已登记 pid，直接按 id 查盘上标题
        title = await asyncio.to_thread(sessions_store.title_for, prof["id"], sess.resume_of, cwd)
    return {"session": dict(sess.to_dict(), title=title)}


@router.get("/api/term/activity")
async def term_activity(request: Request):
    """**免 token** 的只读活动摘要（2026-10-07）。

    为什么需要独立端点（不是「顺手把 token 放开」）：
    顶栏「在跑 N / 会话 N」是**被动展示**，用户从未为看它提供任何凭据；而
    /api/term/sessions 是**控制平面入口**（带 sid + agent_id + cmd + cwd），
    P1-7 堵它的理由——「列 → 拿 sid → 杀」接力——今天依然成立。两者诉求相反：
    计数要人人可见，清单必须有凭据。

    所以这里给的是**聚合计数**，不含任何 sid / agent_id / cmd / cwd：
    泄出去也只是「此刻有几条会话」，拿不到任何可操作句柄。

    判据与 /api/term/sessions 严格同源（同一个 _reap() + alive），
    不另算一套：两处口径分叉会让顶栏和终端栏互相打脸（P1-20 的老教训）。
    """
    _reap()   # 读路径顺手回收：与 list_sessions 同款（顶栏 8s 轮询就是最稳的回收心跳）
    live = [s for s in _sessions.values() if s.alive]
    # 按 agent 聚合（侧栏忙碌点要用到），但**只给 id 与条数** ——
    # agent_id 本来就是导航里公开可见的标识，不是凭据；sid 才是可操作句柄，一律不给。
    per: Dict[str, Dict[str, float]] = {}
    now = time.time()
    for s in live:
        if not s.agent_id:
            continue
        cur = per.setdefault(s.agent_id, {"n": 0, "activity_s": 0.0})
        cur["n"] += 1
        # activity_s 取该 agent 名下**最活跃**那条（与 /api/term/sessions 的字段同名同义）。
        # 侧栏忙碌点的「亮/闪」分档靠它 —— 不给就要么全体常闪、要么删掉这个分档，
        # 那属于静默改行为。标量秒数不含任何句柄，粒度与计数同级。
        act = round(now - getattr(s, "last_activity", s.last_io))
        cur["activity_s"] = min(cur["activity_s"], act) if cur["n"] > 1 else act
    return {"alive": len(live), "agents": len(per),
            "per_agent": [{"agent_id": k, "n": int(v["n"]), "activity_s": int(v["activity_s"])}
                          for k, v in sorted(per.items())]}


@router.get("/api/term/sessions")
async def list_sessions(request: Request):
    # P1-7：列表本身是**控制平面入口** —— 它漏 sid + agent_id + cmd + cwd + alive，
    # 而 DELETE 以前不鉴权 ⇒ 任何能撑到 3102 的一方（单前 **绑定 0.0.0.0**，局域网可达）
    # 可以「列 → 拿 sid → 杀」接力杀掉别人正在跑的会话。只堵 DELETE 不堵 GET 等于没堵。
    _check_term_token(request.headers.get("x-term-token", "")
                      or request.query_params.get("token", ""), "GET /api/term/sessions")
    _reap()
    # 只展示活会话：已退出记录不再以"僵尸条目"出现（历史改由 /api/term/history 从磁盘直读）
    titles: Dict[str, Dict[int, str]] = {}
    out = []
    for s in _sessions.values():
        if not s.alive:
            continue
        if s.agent_id not in titles:
            # P0-8：同上，走线程池 + 指纹缓存，别把列表轮询变成阻塞源。
            titles[s.agent_id] = await asyncio.to_thread(
                sessions_store.live_titles_cached, s.agent_id) or {}
        t = titles[s.agent_id].get(s.pid, "")
        if not t and s.resume_of:      # pid 反查不到（jcode 只在退出时写 last_pid、codex/qoder 无映射）
            t = await asyncio.to_thread(              # P0-8：同上
                sessions_store.title_for, s.agent_id, s.resume_of, s.cwd)   # 按 resume_of 直查盘上标题
        out.append(dict(s.to_dict(), title=t))
    return {"sessions": out}


@router.get("/api/term/history/{agent_id}")
async def agent_history(agent_id: str, request: Request, limit: int = Query(default=3, ge=1, le=20)):
    _check_term_token(request.headers.get("x-term-token", "")
                      or request.query_params.get("token", ""), "GET /api/term/history")
    prof = profiles.get_profile(agent_id)
    if not prof or not prof.get("terminal"):
        raise HTTPException(400, f"{agent_id} 无终端入口，谈不上续聊历史")
    if not sessions_store.supports(prof["id"]):
        raise HTTPException(400, f"{agent_id} 无历史会话仓库")
    cwd = prof["terminal"].get("cwd") or os.path.expanduser("~")
    return dict(sessions_store.list_history(prof["id"], cwd, limit), agent=prof["id"])


# ── 终端会话录制（v0.13.59/ PT-20260927-16，2026-10-08 重做接线）────────
# 鉴权口径与 /api/term/sessions 同级：sid 是**可操作句柄**（能取回整段会话的终端内容），
# 比列表更敏感，故一律要 TERM_TOKEN，绝不开免token（对照 /api/term/activity 的取舍逻辑：
# 那个端点只给聚合计数、不含任何句柄，才敢免 token）。
@router.get("/api/term/recording/{sid}")
async def term_recording_status(sid: str, request: Request):
    """本会话录了多少、有没有触顶停录、上限是多少。界面据此明示，不能静默丢帧。"""
    _check_term_token(request.headers.get("x-term-token", "")
                      or request.query_params.get("token", ""), "GET /api/term/recording")
    sess = _sessions.get(sid)
    # 活会话报内存真值（含 capped 一等状态）；已退出的从盘上重建（进程重启后 Recorder
    # 已不在，capped 真值无从恢复 —— 那种情形 term_record.status只给 at_limit 推断值）。
    if sess is not None:
        return sess.recorder.to_dict()
    if not term_record.enabled():
        raise HTTPException(404, "recording 未启用")
    return await asyncio.to_thread(term_record.status, sid)


@router.get("/api/term/recording/{sid}/frames")
async def term_recording_frames(sid: str, request: Request,
                                limit: int = Query(default=2000, ge=1, le=10000),
                                after_seq: Optional[int] = Query(default=None, ge=0)):
    """回放：按 seq 升序返回已脱敏的帧（data 为 base64）。

    ⚠️ 这是**用户键入与 agent 输出的全量文本**出口 —— 即便是脱敏后的，也比会话列表
    敏感得多，所以与 kill 同级鉴权（要 TERM_TOKEN），且绝不接受「免 token 读一帧」。

    B14 续传点：带 `after_seq` 只取**严格更大**的 seq；回包里的 `next_after_seq`
    仅在 `more=True` 时给出 —— 客户端原样回传即可续接，不会重复边界帧，
    也不会拿到一个永远取不到新帧的游标而空转轮询。
    """
    _check_term_token(request.headers.get("x-term-token", "")
                      or request.query_params.get("token", ""), "GET /api/term/recording/frames")
    if not term_record.enabled():
        raise HTTPException(404, "recording 未启用")
    page = await asyncio.to_thread(term_record.frames_page, sid, limit, after_seq)
    return {"session_id": sid, **page,
            "limits": {"max_session": term_record.max_session_bytes(),
                       "max_total": term_record.max_total_bytes()}}


@router.delete("/api/term/sessions/{sid}")
async def kill_session(sid: str, request: Request):
    # P1-7：杀掉别的会话 = 服务打断，与“建会话”同级危险（建已经被闸了）。
    # 前端六个调用点本来就带 termHeaders()，故此处只补服务端，不会造成界面断流。
    _check_term_token(request.headers.get("x-term-token", "")
                      or request.query_params.get("token", ""), "DELETE /api/term/sessions/{sid}")
    # 先摘出登记表 → 列表/新 WS 立即看不到（❌ 即时生效）；进程灭杀与资源回收走 kill→_force_kill→_cleanup
    sess = _sessions.pop(sid, None)
    if not sess:
        raise HTTPException(404, "session not found")
    sess.kill()
    # 旧实现只 pop + kill，把「关 fd / 摘 add_reader / 收尸」全交给 kill 内的
    # call_later(2s)→_force_kill→call_later(1.5s)。正常路径没事，但**服务正在关闭**
    # 时 loop 不再跑这些定时器 ⇒ fd 与 reader 泄漏（hub 重启即清，问题不大）；
    # 更常见的是 WS 仍挂着时用户连点多次 ×。这里显式登记一条「已被显式杀掉」的会话，
    # 由 reap_loop 兜底收尾，不依赖定时器一定跑得起来。
    _dying[id(sess)] = sess
    # 尺寸所有权是按 sid 记的全局表，会话被 pop 掉后这条记录永无回收点
    # （正常关闭路径在 WS finally 里清，但显式 kill 走不到那里）⇒ 长跑累积，
    # 且 sid 复用时新会话会被一条陈旧的 owner 记录误判成「非所有者」。
    _size_owner.pop(sid, None)
    return {"status": "killed", "id": sid}


# ── P0-1：last_io 的「什么才算交互」唯一判据 ──────────────────
# 前端 15s 一帧心跳（02-nav-and-poll.js:206 TERM_HB_SEND_MS）在旧实现里每次 receive
# 都无条件续 last_io ⇒ 45min TTL 形同虚设。这里把判据收敛成两个函数，
# _reap() / to_dict() / 前端「空闲多久」展示全部复用，杜绝各算各的。
_HEARTBEAT_TYPES = frozenset({"hb", "ping", "pong", "ack"})


def _touch(sess: "Session", text: str) -> None:
    """收到一帧客户端文本：只有**非心跳、非回执**的内容才算用户交互。

    同时续 last_activity（会话活着）；last_io（客户端静默）语义不变。

    - resize：用户拖窗口 ⇒ 真交互，必须续命（否则拖窗口看源码会被 TTL 杀掉）。
    - 其余文本帧（输入的按键/粘贴）⇒ 真交互。
    - hb/ping/pong/ack ⇒ 链路保活，不续命（这正是 P0-1 的病根）。
    纯心跳帧不抛异常、不记日志——它每 15s 来一次，日志会被刷爆。
    """
    t = text.strip()
    if not t:
        return
    if t[0] not in "{[":
        _mark_interaction(sess)           # 非 JSON 的裸帧：保守当交互
        return
    try:
        j = json.loads(t)
    except (json.JSONDecodeError, ValueError):
        _mark_interaction(sess)           # 解析不了当输入处理，不因格式怪就丢 TTL
        return
    if not isinstance(j, dict):
        _mark_interaction(sess)
        return
    if str(j.get("type") or "") in _HEARTBEAT_TYPES:
        return                             # 心跳：不续命（P0-1 的病根）
    _mark_interaction(sess)


def _mark_interaction(sess: "Session") -> None:
    """一次真实交互：续 last_io（客户端静默）与 last_activity（会话存活）。"""
    now = time.time()
    sess.last_io = now
    sess.last_activity = now


def _touch_bytes(sess: "Session") -> None:
    """收到二进制帧：xterm 的输入走 data 通道 ⇒ 一定是交互，两个时钟都续。"""
    _mark_interaction(sess)


def effective_ttl(s: "Session") -> int:
    """该会话此刻适用的 TTL（秒）。

    **单一真相源**：`_reap()` 与测试都用它算阈值 —— 两处各算一套必然漂移
    （本批实踩：既有测试 `test_borderline_not_reaped` 拿 `IDLE_TTL_S - 5`
    当边界，而两级 TTL 下无人观看的有效阈值是 `UNWATCHED_TTL_S` ⇒ 判红）。

    规则：
    - 有人在看（viewers 非空）⇒ **不适用 TTL**（永不被回收），返回 0 哨兵值；
    - 无人观看 ⇒ `min(IDLE_TTL_S, UNWATCHED_TTL_S)`（后者为 0 时关闭该档）。
    """
    if bool(getattr(s, "viewers", None)):
        return 0                                  # 0 = 「有人在看，永不回收」
    if UNWATCHED_TTL_S > 0:
        return min(IDLE_TTL_S, UNWATCHED_TTL_S)
    return IDLE_TTL_S


def _reap():
    """回收两件事，缺一不可（这两条曾各自单独失效 ⇒ 配额被占死）：

    ① **进程已退但登记还在**：PTY 对端关闭后，内核把 master fd 标为可读且 read() 返回
       0 或 EIO。若这条链路没人触发（没有观看者 WS ⇒ 没有 pump；前端只有心跳 ⇒
       也没有人发数据），`add_reader` 回调也可能因 `os.read` 返回空而早退 ⇒
       session 永远 alive=True 地留在 `_sessions`，一直占 MAX_SESSIONS(8) 的名额，
       用户表现为「开过几个终端之后再也开不出新的」。

    ② **无生命迹象超 TTL**（v0.13.58 档二改判据）：判据是 last_activity 且要求**无人观看**。
       「没人观看」用 `viewers` 为空判断，而不是「客户端静默」——
       客户端静默只是「这台设备不在」，不等于「没人要这个会话」。

    P0-2 的修法：这里主动 waitpid(WNOHANG) 探活——它是唯一不依赖「有没有人在看」
    的探针。子进程一退出，waitpid 立刻返回 pid，登记当轮就被摘掉。

    ② 判据为什么换过（实弹证据，不是推断）：
       旧口径 `now - s.last_io > TTL` 把「客户端静默」当死活判据，实测在
       TERM_IDLE_TTL=45s 的实例上：agent 明明每 2s 在产出（idle_s 涨到 40s），
       会话仍被回收，另一端重连直接收到 4410（已结束）。
       用户要求的语义是「状态跨客户端连续」⇒ 客户端不在 ≠ 会话该死。
       现在：**pty 有产出**（_attach_reader 续 last_activity）或**有人看着**（viewers 非空）
       都不会被回收；只有「没人看 + 进程活着但 pty 长时间一个字节都不吐」才收。
    """
    now = time.time()
    for s in list(_sessions.values()):
        if not s.alive:
            _drop(s)
            continue
        # ① 探活（P0-2）：waitpid 能独立回答「子进程还在不在」，不依赖有没有人看。
        # getattr 兜底是为了让轻量 stub（tests/test_term_reaper._Stub 只暴露
        # id/alive/last_io/kill/_cleanup）也能过 —— 契约不该由实现细节决定。
        probe = getattr(s, "poll_exited", None)
        if callable(probe) and probe():
            _drop(s)
            continue
        # ② 无生命迹象超 TTL（档二）：三个条件同时成立才回收。
        #    watched：有观看者 ⇒ 有人在用，绝不回收（哪怕客户端静止不动）。
        #    activity：last_activity 比 last_io 更能代表「会话活着」；
        #      老 stub 没有这个字段 ⇒ getattr 兜底退回 last_io（契约由实现细节决定不得人心）。
        watched = bool(getattr(s, "viewers", None))
        activity = getattr(s, "last_activity", None)
        base = s.last_io if activity is None else max(activity, s.last_io)
        if watched:
            continue
        # ③ **无人观看**档（PT-20261024-08 B2）：viewers 空 = 客户端已断开。
        #    为什么单独一档：`watched` 为空时上面的 IDLE_TTL_S 才生效，
        #    而 IDLE_TTL_S 是 10 分钟 —— 用户关掉页面后，一个 485MB 的 agent
        #    CLI 仍会挂在 hub 的 cgroup 账上 10 分钟（实测单个 codebuddy CLI
        #    485MB，判例 agent-knowledge/259）。
        #    ⇒ 无人观看时用更短的 TTL。
        #    ⚠ **仍受「pty 有产出」豁免**：base 取的是 max(last_activity, last_io)，
        #      pty 有产出时 last_activity 会续命 ⇒ `now - base` 不涨 ⇒ 不回收。
        #      即「agent 还在真干活」时**不会**被这一档误杀（这正是 09-19 那次
        #      误杀「agent 明明每 2s 在产出却被回收」的同型坑，必须守住）。
        #    ⚠ UNWATCHED_TTL_S=0 = 关闭这一档（只按 IDLE_TTL_S 判）。
        #    ⚠ 阈值一律走 `effective_ttl()`（单一真相源）：`_reap()` 与测试
        #      各算一套必然漂移（本批实踩过：既有测试拿 IDLE_TTL_S 当边界）。
        ttl = effective_ttl(s)
        if ttl > 0 and now - base > ttl:
            s.kill()


def _drop(s: "Session") -> None:
    """把一条会话从登记表里摘掉并回收资源（幂等）。"""
    s._cleanup()
    _sessions.pop(s.id, None)


async def reap_loop():
    """独立回收心跳（P0-4）。

    缺陷根因（实测）：`_reap()` 全仓唯一调用点是 `list_sessions()`，而 startup 只建了
    sweep_stale_tasks / vitals_loop 两个后台任务 ⇒ 浏览器一关就再没人调
    GET /api/term/sessions ⇒ 45min 空闲 TTL 形同虚设、死会话不从 _sessions 摘除，
    MAX_SESSIONS(8) 被占满后新终端直接 429「开不出来」。
    回收必须由服务自己按时做，不能依赖有没有人在看。
    单轮异常不许打死循环 —— 否则又回到「静默不回收」。
    """
    while True:
        await asyncio.sleep(REAP_INTERVAL_S)
        try:
            _reap()
            _reap_dying()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning(f"[term] reap_loop 异常（下轮重试）：{type(e).__name__}: {e}")


def _fast_reap_enabled() -> bool:
    """快速通道是否启用（间隔 > 0）。设 TERM_FAST_REAP_INTERVAL=0 即关闭。"""
    return _FAST_REAP_INTERVAL_S > 0


def _reap_fast() -> None:
    """只做一件事：发现「零输出即死」的子进程并立刻收尸（PT-20260929-02）。

    **刻意只探活、不做 TTL 判定**。分工理由：
    - TTL 判据（无生命迹象 + 无人观看）误伤代价高 —— 误收一个正在跑任务的会话，
      用户会丢现场。而`poll_exited()` 是无歧义的：waitpid 明确说进程没了。
    - 所以快车道只做无歧义的那部分；判定「活着但该不该收」留给 60s 的 `_reap()`。

    与 `_reap()` 的关系：两者都调`poll_exited()`，而它幂等
    （`exit_status is not None` 即早退），并发跑不会互相污染。
    `_drop()` 亦幂等。唯一需要防的是「同一轮里两者都判死」——
    `_sessions.pop(s.id, None)` 与 `_drop` 内的 `pop` 都走默认 None，天然容忍。
    """
    for s in list(_sessions.values()):
        if not s.alive:
            _drop(s)
            continue
        probe = getattr(s, "poll_exited", None)
        # getattr 兜底同`_reap()`：轻量 stub 只暴露 id/alive/last_io/kill/_cleanup，
        # 契约不该由实现细节决定。
        if callable(probe) and probe():
            _drop(s)


async def fast_reap_loop() -> None:
    """子进程即时感知快车道（每 2s）。启动见 `src/main.py` 的 `_spawn`。

    单轮异常不许打死循环 —— 与 `reap_loop` 同一条纪律：后台任务静默死掉
    就等于把缺口原样放回去，而且更难发现（没有报错、没有日志）。
    """
    if not _fast_reap_enabled():
        log.info("[term] 子进程即时感知快车道已关闭"
                 "（TERM_FAST_REAP_INTERVAL<=0）⇒ 零输出即死最长需等 %.0fs"
                 % REAP_INTERVAL_S)
        return
    log.info("[term] 子进程即时感知快车道已启用：每 %.1fs 探活一次"
             "（常规 reap %.0fs）" % (_FAST_REAP_INTERVAL_S, REAP_INTERVAL_S))
    while True:
        await asyncio.sleep(_FAST_REAP_INTERVAL_S)
        try:
            _reap_fast()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning(f"[term] fast_reap_loop 异常（下轮重试）：{type(e).__name__}: {e}")


def _reap_dying() -> None:
    """收尾已被显式杀掉的会话：进程确实没了就回收资源；还在等 SIGTERM 升级就跳过。"""
    for key, sess in list(_dying.items()):
        if sess.poll_exited():
            sess._cleanup()
            _dying.pop(key, None)


def alive_count() -> int:
    """活会话数（自证端点与限流判据共用一个口径，别再各算各的）"""
    return len([s for s in _sessions.values() if s.alive])


def idle_max_s() -> int:
    """最久没有生命迹象（无输出且无人观看）的活会话闲置秒数（判断 TTL 有没有真的在跑）

    口径必须与 _reap() 的判据一致（档二）：报 last_io 而回收看 last_activity，
    会让 /health 的自证字段证明「一条正在被回收的会话也不忙」⇒ 假绿。
    """
    live = [time.time() - max(getattr(s, "last_activity", s.last_io), s.last_io)
            for s in _sessions.values() if s.alive]
    return int(max(live)) if live else 0


def _attach_reader(sess: Session):
    loop = asyncio.get_event_loop()

    def on_readable():
        try:
            data = os.read(sess.fd, 65536)
            if data:
                # 档二关键一行：pty 有产出 = 会话活着，续 last_activity。
                # 刻意**不**续 last_io —— 「agent 在跑」不等于「用户刚敲过键盘」，
                # 两者混成一个时钟才会把正在跑的任务判成空闲（见 __init__ 注释）。
                sess.last_activity = time.time()
                sess.ring.extend(data)
                # v0.13.59 录制：pty→客户端方向。挂在**读出源头**而非 pump/合并器出口——
                # 那样录到的是「合并器攒过的帧」，而合并器会为省 WS 帧做截断/合并（见
                # _OutputCoalescer），录下来的就不是真实回放内容了。ring.extend 同一处。
                # active 为 False（TERM_RECORD 缺省 0）时只是一次属性读，零开销。
                if sess.recorder.active:
                    sess.recorder.feed("out", data)
                if len(sess.ring) > 65536:
                    del sess.ring[:len(sess.ring) - 65536]
                # 不再「读一块发一块」：交给各自的合并器攒一趟（前沿立刻刷 / 后沿攒 5ms）。
                # 合并器内部才 put 进队列 —— 队列帧数下降与 WS 帧数下降是同一件事。
                for co in list(sess.coalescers.values()):
                    co.handle(data)
        except (OSError, BlockingIOError) as e:
            if isinstance(e, OSError) and e.errno in (errno.EIO, errno.EBADF):
                sess.alive = False
                sess._cleanup()  # 摘 reader + 关 fd + 收尸（幂等，替代原散落逻辑）
                # _cleanup() 已记下 exit_status ⇒ 这里能把「为什么没了」一起说出来。
                # 原样只有一句「[会话结束]」，用户看到的就是"点一下闪退、什么都不告诉我"。
                reason = describe_exit(sess.exit_status, sess.hub_killed)
                tail = f"\r\n\x1b[90m[进程 {reason}]\x1b[0m" if reason else ""
                # 保序（P1-1）：[会话结束] 是带外消息，必须先让合并器把攒着的输出落进
                # 队列，才能排到它后面。不 flush 的话退出提示会插在最后一段输出**之前**，
                # 表现为「屏幕上一半输出提示在前、内容在后」这类极难复现的错乱。
                for co in list(sess.coalescers.values()):
                    co.flush()
                # P1：旧写法 try 包住整个 for ⇒ **第一个**观看者队列满就跳过其余全部，
                # 后来的观看者永远收不到「[会话结束]」，表现为「第二条终端不提示已结束、
                # 画面停住」。改成逐个 try，满了记丢弃、其余照常送达。
                end_payload = ("\x1b[?25h\r\n[会话结束]" + tail).encode()
                for vid, q in list(sess.viewers.items()):
                    try:
                        q.put_nowait(end_payload)
                    except asyncio.QueueFull:
                        sess.dropped[vid] = sess.dropped.get(vid, 0) + len(end_payload)

    loop.add_reader(sess.fd, on_readable)


@router.websocket("/ws/term/{sid}")
async def term_ws(ws: WebSocket, sid: str, token: str = Query(default="")):
    # 缺 token 即拒：query ?token= 与 header X-TERM-TOKEN 两种都接受
    provided = token or ws.headers.get("x-term-token", "")
    if not provided or not hmac.compare_digest(provided, TERM_TOKEN):
        log.warning("[term] 拒绝：ws 握手 token 校验失败（/ws/term）")
        # WS 握手阶段不能用 HTTPException（Starlette 在 ws 上下文不可靠），显式关闭 4401=未授权
        await ws.close(code=4401)
        return
    sess = _sessions.get(sid)
    reject = None
    if not sess:
        reject = 4404
    elif not sess.alive:
        # 死会话不再服务：避免"回放旧画面+输入无效"的假加载（4410=已结束）
        reject = 4410
    if reject is not None:
        # 业务码必须在 accept() **之后** close。早先是在 accept 前 close：
        # Starlette 此时只会回一个 HTTP 403 拒掉握手 ⇒ 浏览器侧看到的是 1006
        # （异常关闭），与"链路断了"同签名 —— 前端就会对着一条已不存在的 sid
        # 无限重连。实测（pre-accept 版）：websockets 客户端 InvalidStatus、
        # http=403、close code = None。4401（鉴权失败）仍留在 accept 前：
        # 不给未授权方完成握手。
        await ws.accept()
        await ws.close(code=reject)
        return
    await ws.accept()
    # 每个观看者一条**独立**队列（P1-5）。旧做法全会话共用一个 outputs 队列，
    # 而每条 WS 的 pump 都在同一个队列上 get() ⇒ 桌面 + 手机同看一条会话时，
    # 两个消费者会互相偷字节，各自只拿到一半流（内容永久错乱，不是“少看几行”）。
    vid = uuid.uuid4().hex[:8]
    vq: asyncio.Queue = asyncio.Queue(maxsize=2000)
    sess.viewers[vid] = vq

    def _enqueue(payload: bytes) -> None:
        """合并器的出口：入本观看者的队列。慢消费者照旧记账，绝不静默吞字节。"""
        try:
            vq.put_nowait(payload)
        except asyncio.QueueFull:
            sess.dropped[vid] = sess.dropped.get(vid, 0) + len(payload)

    sess.coalescers[vid] = _OutputCoalescer(_enqueue)

    # ── auth_url 旁路（P2/P3，v0.13.64）──────────────────────────────────
    # 挂在 pump 里而非 on_readable：on_readable 是 add_reader 回调，只认 PTY 没有 ws，
    # 而旁路帧要**逐观看者**发（照抄上游单 session.ws 会退回到"多观看者互相偷字节"）。
    # 关键约束：旁路帧走 send_text 独立通道，**绝不能** put 进 vq —— 那条队列是
    # 输出字节流，混进 JSON 会让 xterm 把 {"type":...} 当字符画到屏幕上。
    async def _scan_auth_urls(data: bytes) -> None:
        """扫本轮 PTY 输出：登录链接与页面链接**分两路**播（v0.13.87）。

        为什么要分两路（用户 2026-10-07 报障）：原来凡是见到 http(s) URL 就发
        `auth_url`，前端文案写死「检测到登录链接」⇒ agent 印一条文档地址就弹登录提示。
        现在：
          · 登录路：`_scan_login_urls` 必须命中登录线索词（见 DEFAULT_AUTH_CUES），
            才发 `auth_url` + 原来的 auto 判定（真登录提示仍会被自动打开）；
          · 页面路：`_scan_page_urls` 命中「URL + 同行人话」就发 `page_url`，
            auto 恒为 False —— 普通页面链接绝不该替用户自动弹浏览器。
        两条路都**只走 send_text 带外通道**，绝不 put 进 vq（那会被 xterm 当正文画出来）。

        v0.13.88：**两路的扫描范围不同** —— 登录路只看 `sess.last_chunk`（本轮这块），
        页面路看 `sess.url_buf`（整段 scrollback）。这不是笔误：一条线索播出后必须
        "换一块就清零"，否则它会追认紧随其后的普通链接（用户二次报障的形态）；
        而链接发现本身要跨块，否则折行/分批打印的 URL 会被漏掉。

        v0.13.89：整条通道由 `LINK_BYPASS_ENABLED` 总开关把着，**默认关闭** ⇒ 本函数
        直接返回，一个帧都不发（用户口径「全部静默，连登录提示也不要」）。下面的
        扫描逻辑与判据全部保留，重开开关即恢复 v0.13.88 的语义。
        """
        # 总开关必须闸在**最前面**：连已知状态（url_buf / last_chunk）都不再维护，
        # 关掉时这一段就是一次函数调用，没有 strip_ansi、没有正则、没有帧。
        if not LINK_BYPASS_ENABLED:
            return
        try:
            chunk = strip_ansi(data.decode("utf-8", errors="replace"))
            sess.url_buf = (sess.url_buf + chunk)[-URL_SCAN_MAX:]
            sess.last_chunk = chunk[-AUTH_CHUNK_MAX:]
            # 线索表可由 DB/env 覆盖；覆盖时**整表替换 PRE/POST 两张**（运维口子
            # 只需要"加一种文案"这一档能力，分两张表反而给不出可读的 diff）。
            cues = auth_cues()
            kw = {"cues_pre": cues, "cues_post": []} if cues != AUTH_CUES_PRE else {}
            auth_urls = _scan_login_urls(sess.last_chunk, **kw)
            # **登录路优先**：同一条 URL 命中了登录就不在页面路重复播 ——
            # 否则同一串 URL 会在屏上留两行（`[登录链接] …` + `[链接] …`）并弹两次。
            # 两条通道的语义是「更具体的那条胜出」，不是"两个都要说"。
            # v0.13.88 追加一条：登录路只看当前块后，一条**已判过登录**的 URL 仍留在
            # url_buf 里，会被页面路看到并再播一遍 —— 已播过的登录 URL 必须一并退出
            # 页面路（`auth|u` 就是它在 announced_urls 里的会话级键）。
            page_urls = [u for u in _scan_page_urls(sess.url_buf)
                         if u not in auth_urls
                         and ("auth|" + u) not in sess.announced_urls]
            for ch, urls in (("auth", auth_urls), ("page", page_urls)):
                for u in urls:
                    key = ch + "|" + u
                    if key in sess.announced_urls:
                        continue
                    sess.announced_urls.add(key)
                    # auto 只对「明确在催你点浏览器」的场景开；默认给按钮。
                    # 自动开在手机上必被浏览器拦（无用户手势），反而让人以为功能坏了。
                    # v0.13.87：auto 只可能出现在登录路 —— 页面链接永远只给按钮。
                    # v0.13.88：auto 的判据也跟着收到当前块，否则很久以前的一句
                    # "press enter to open" 会让后来的登录提示被自动打开。
                    auto = ch == "auth" and any(
                        k in sess.last_chunk.lower() for k in
                        ("press enter to open", "open this url",
                         "continue in your browser", "open_url:"))
                    try:
                        await ws.send_text(json.dumps(
                            {"type": "auth_url" if ch == "auth" else "page_url",
                             "url": u, "auto": bool(auto)}))
                    except Exception:  # noqa: BLE001
                        return    # WS 已断，别再扫了
        except Exception:  # noqa: BLE001
            return          # 旁路功能绝不能拖垮终端主链路

    # 回放最近输出（重连不白屏）
    if sess.ring:
        try:
            await ws.send_bytes(bytes(sess.ring))
        except Exception:  # noqa: BLE001
            pass
    # 输出泵：queue → ws；带超时兜底，进程死亡且队列排空即收口，绝不无限挂起
    async def pump():
        while True:
            try:
                data = await asyncio.wait_for(vq.get(), timeout=2.0)
            except asyncio.TimeoutError:
                if not sess.alive:
                    break
                continue
            # 慢消费者必须可见（P1-5）：丢了就明说，绝不静默吞字节
            lost = sess.dropped.pop(vid, 0)
            if lost:
                try:
                    await ws.send_bytes(("\r\n\x1b[90m[hub: 输出过快，已丢弃 %d 字节]\x1b[0m\r\n"
                                          % lost).encode())
                except Exception:  # noqa: BLE001
                    break
            # 真正修：send_bytes 必须 try（手机断网/切网络 → WS 已断 → 1006）
            try:
                await ws.send_bytes(data)
            except Exception:  # noqa: BLE001
                # WS 已断开：pump 退场，由收尾 try 兜底
                break
            await _scan_auth_urls(data)   # 旁路：登录 URL 带外播报（不改画面）
            if not sess.alive:
                break
        # 收尾：捕获 WS 已断开的情况（手机重连/切网络 → 客户端 1006），不再把异常抛回 event loop
        # 与 on_readable 的 EIO 分支同理：有面因就一并说出来，别让「为什么没了」变成哑谜。
        # 走到这里必然 alive=False，而 alive 只由 _cleanup() 置（全仓两处，另一处紧随其后调它）
        # ⇒ exit_status 已记录，直接读即可，不必补调 _cleanup()。
        # 保序（P1-1）：[process exited] 是带外消息，必须排在最后一段输出**之后**。
        # 两步都不能省：① flush —— 把合并器里攒着的落进队列；
        #              ② 排空 —— pump 循环已退出，队列里剩下的帧没人再发了，
        #                 不排空的话退出提示会抢在刚 flush 出来的输出前面。
        co = sess.coalescers.get(vid)
        if co is not None:
            co.flush()
        lost = sess.dropped.pop(vid, 0)
        if lost:
            try:
                await ws.send_bytes(("\r\n\x1b[90m[hub: 输出过快，已丢弃 %d 字节]\x1b[0m\r\n"
                                      % lost).encode())
            except Exception:  # noqa: BLE001
                pass
        while True:
            try:
                pending = vq.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                await ws.send_bytes(pending)
            except Exception:  # noqa: BLE001
                break
        reason = describe_exit(sess.exit_status, sess.hub_killed)
        tail = f" ({reason})" if reason else ""
        try:
            await ws.send_bytes(("\r\n\x1b[90m[process exited" + tail + "]\x1b[0m").encode())
        except Exception:  # noqa: BLE001
            pass
        try:
            await ws.close(code=4410)
        except Exception:  # noqa: BLE001
            pass
    pump_task = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive()
            # P0-1：last_io 只认「真实交互」，**不信心跳**。
            # 前端每 15s 发一帧 {"type":"hb"}（02-nav-and-poll.js:206），而旧实现在
            # 每次 receive 后无条件续 last_io ⇒ 45min TTL 对任何还开着的终端**永不触发**，
            # 「空闲自动回收」这个承诺实际是废的。这里只在有意义的帧上续命：
            #   resize = 用户拖了窗口；其余文本帧按内容判（hb/回执不算）。
            # 判据放在 _touch() 里，_reap() 与空闲统计共用同一口径。
            if msg.get("text") is not None:
                _touch(sess, msg["text"])
            elif msg.get("bytes"):
                _touch_bytes(sess)
            if msg.get("text") is not None:
                try:
                    j = json.loads(msg["text"])
                    if j.get("type") == "resize":
                        # 尺寸所有权（B2 / paseo 融合）：任何连接都能改尺寸 ⇒ 后台的手机端
                        # 一次 ResizeObserver 就能把桌面上正开着的 vim 压扁，
                        # 用户看到的是「我什么都没做，终端自己乱了」，极难归因。
                        #   claim  = 「我是主人，按我的尺寸来」（无条件夺权，含同尺寸）
                        #   update = 「我只是几何变了」；非所有者一律静默忽略
                        # 老客户端没有 intent 字段 ⇒ 缺省按 claim 处理（与 paseo 同口径），
                        # 否则一次前后端版本错配就会让尺寸永远改不动。
                        _apply_size(sess, vid, j.get("rows"), j.get("cols"),
                                    _norm_intent(j.get("intent")))
                        continue
                    if j.get("type") == "hb":
                        # 应用层心跳（P1-1 前端自愈靠它识半开连接）：只回执，
                        # **绕不写进 pty** —— 否则每秒往终端里灌垃圾。
                        try:
                            await ws.send_text(json.dumps({"type": "hb", "t": time.time()}))
                        except Exception:  # noqa: BLE001
                            break
                        continue
                    data = str(j.get("data", "")).encode()
                except (json.JSONDecodeError, KeyError):
                    data = msg["text"].encode()
            else:
                data = msg.get("bytes") or b""
            if data and sess.alive:
                # v0.13.59 录制：客户端→pty 方向（用户键入）。
                # 刻意记**解出来的 data**（跨过 JSON 包装与心跳判据之后）——
                # 记原始 WS 帧会把 {"type":"hb"} 心跳也录进去，回放时满屏JSON。
                # 位置也在 os.write 之前：写失败（EAGAIN 背压）那批字节最终也可能进 pty，
                # 但为免「录了却没写进去」的错觉，只录确定写下去的部分 —— 取 write 之后。
                try:
                    os.write(sess.fd, data)
                    if sess.recorder.active:
                        sess.recorder.feed("in", data)
                except BlockingIOError:
                    # P0-3：BlockingIOError **是** OSError 的子类 ⇒ 旧 `except OSError: break`
                    # 把「PTY 缓冲区瞬时写不进去（EAGAIN，正常的背压）」当成致命错误，
                    # 直接 break 掉整个收包循环 ⇒ 用户表现为「敲键盘偶尔就掉线」。
                    # 正确处置：让出事件循环重试，而不是杀连接。fd 是 O_NONBLOCK 的，
                    # 这里改成 await sleep 让 add_reader 再来；剩余尾巴丢了也比断线好
                    # （真丢内容前端会有回显错位，但链路不断）。
                    await asyncio.sleep(_WRITE_BACKPRESSURE_S)
                except OSError as e:
                    # 真死信号（EIO/EBADF：pty 对端已关）才收口。
                    if e.errno not in _WRITE_RETRY_ERRNOS:
                        break
                    await asyncio.sleep(_WRITE_BACKPRESSURE_S)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        pump_task.cancel()
        sess.viewers.pop(vid, None)
        sess.dropped.pop(vid, None)
        # 所有者走了必须交还所有权：留着悬挂的 vid 会让**所有**客户端的 update 全被忽略，
        # 表现就是「重连之后尺寸再也改不动了」。
        if _size_owner.get(sid) == vid:
            _size_owner.pop(sid, None)
        co = sess.coalescers.pop(vid, None)
        if co is not None:
            co.close()   # 定时器不收会让 loop 一直攥着一个已离开的观看者


def kill_all():
    """服务退出：立即 TERM+KILL 双发（组级），不等 call_later（loop 即将关闭）"""
    for s in _sessions.values():
        s.hub_killed = True      # 同上：别让"服务重启"被报成"内存不足"
        s._signal_group(signal.SIGTERM)
        s._signal_group(signal.SIGKILL)
