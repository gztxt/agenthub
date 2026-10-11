"""静态资源服务模块（判据 + 组装 + 发射；纯核心零框架依赖）。

为什么要单独成模块：这条判据此前**内联**在 `src/main.py` 的 `/static/{file_path:path}`
路由里，而 `src/main.py` 一 import 就跑 lifespan（开真库、起后台探针任务、绑端口）
⇒ 单元测试根本没法测它。抽出来之后，"哪些文件许服务"这件事第一次可离线钉住。

判据演进（都有实测红-绿对照，见 tests/verify_p1_backend.py）：
  旧①：str(f).startswith(str(static_path))
       —— 前缀不是**目录边界**：`static/../static_evil/secret.txt` 解析后
          落在 `static_evil/`，仍以 `static` 开头 ⇒ 兄弟目录可逃逸。
          实测改前 HTTP 200 且取回 CANARY 内容。
  旧②：无 .bak 判据 ⇒ hub.js.bak-* 一类历史备份件可被 HTTP 直取（实测 200 / 88779B）。
  新：is_relative_to（真目录边界）+ is_file + 名字含 ".bak" 一律拒。

备份件按工作区铁律必须留在磁盘上供回滚，因此这里只做**服务面**收敛，不删文件。

────────────────────────────────────────────────────────────────────────
2026-10-12 深化（C1，`improve-codebase-architecture` / `codebase-design`）：
此前只抽了**判据**、组装（ETag / gzip / 分支选择 / 返回路径）仍内联在 main.py 的
56 行路由里 ⇒ 「静态资源怎么正确地服务」的 interface 横跨两处，而本仓头号前端事故面
（09-23 ?v=/ETag、10-11 换树静态 404）正落在组装里、却无测试。本版把整条策略收进本模块：

  · **外部 interface** = `serve(static_root, file_path, vtoken, accept_encoding, if_none_match)
    -> ServePlan`（纯值；调用方与测试走同一条接缝）。
  · `to_response(plan)` = 本模块**唯一**框架耦合点（惰性 import starlette），路由退化为一次调用。
  · `resolve_serveable` / `content_token` / `cache_policy` 降为**内部 seam**（保留，供自家测试
    直接钉，但不再是调用方必须知道的东西）。

**行为逐字节冻结**：三条分支（immutable / revalidate-304 / revalidate-200+gzip）的
status / headers / body 与深化前一致 —— golden 矩阵在 `tests/test_staticguard_serve.py`（L0）
与 `tests/verify_static_headers_matrix.py`（L2 影子实例头矩阵）。
"""
from __future__ import annotations

import gzip
import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


def resolve_serveable(static_root: Path, file_path: str) -> Optional[Path]:
    """返回可服务的绝对路径；应 404 时返回 None。

    只判"许不许服务"，不判"怎么服务"（怎么服务见本模块 serve / to_response）。
    """
    root = Path(static_root).resolve()
    try:
        f = (root / file_path).resolve()
        if not f.is_relative_to(root):       # 目录边界，不是字符串前缀
            return None
        if not f.is_file():                  # 目录本身、断链软链、不存在、名字过长 ENAMETOOLONG
            return None
    except (OSError, ValueError):        # 空字节、代理对、超长路径等——只能返回 None，绝不抛
        return None
    if ".bak" in f.name:                 # 备份件永不从 Web 口读
        return None
    return f


# ---------------------------------------------------------------- 缓存策略判据
# 为什么也放这里：09-23 事故证明"能不能回 304"和"许不许服务"是同一类判断，
# 必须同样可单测（内联在路由里就测不到，历史三版判据都是这么漂过去的）。
_tok_cache: dict = {}     # "路径|mtime_ns|size" -> md5[:8]；键随文件变，天然失效


def content_token(f: Path) -> str:
    """产物内容的短哈希（md5 前 8 位）。构建脚本用它写模板的 ?v= 提手。"""
    st = f.stat()
    key = f"{f}|{st.st_mtime_ns}|{st.st_size}"
    tok = _tok_cache.get(key)
    if tok is None:
        tok = hashlib.md5(f.read_bytes(), usedforsecurity=False).hexdigest()[:8]
        if len(_tok_cache) > 64:
            _tok_cache.clear()
        _tok_cache[key] = tok
    return tok


def cache_policy(vtoken: str, f: Path) -> Tuple[str, str]:
    """返回 ("immutable"|"revalidate", 该用的 token)。

    immutable 的**唯一准入条件**：URL 上的 ?v= 等于文件当前内容哈希。
    这样提手写错/忘了改时自动退回 revalidate（no-cache + ETag），
    绝不会把端侧永久钉在旧体上 —— 比"只要带 ?v= 就 immutable"保守一档。
    """
    want = content_token(f)
    if vtoken and vtoken == want:
        return "immutable", want
    return "revalidate", want


# ---------------------------------------------------------------- 组装 + 发射
# 2026-10-12 C1：原先这些内联在 main.py 的 56 行路由体里。
_GZ_SUFFIX = {".js", ".css", ".svg", ".json", ".map"}
_GZ_MIN = 1024
_gz_cache: dict = {}   # "路径|mtime_ns|size" -> gzip 字节；键随文件变，天然失效


@dataclass(frozen=True)
class ServePlan:
    """一次静态请求该回的响应 —— **纯值**，不含任何框架（starlette/fastapi）类型。

    字段即调用方/tests 必须知道的一切（`codebase-design` 的 interface 含义）：
      · status      —— 404（不可服务）/ 304（revalidate 命中 If-None-Match）/ 200
      · headers     —— 已就绪的响应头（Vary / X-Asset-Token / Cache-Control / ETag…）
      · media_type  —— content-type；304/404 为 None（与深化前逐字一致）
      · body        —— 200 且内存体（immutable 全字节 / gzip 压缩体）
      · file        —— 200 且流式体（revalidate 非 gzip ⇒ FileResponse，会自带 accept-ranges）
    """
    status: int
    headers: dict
    media_type: Optional[str] = None
    body: Optional[bytes] = None
    file: Optional[Path] = None

    @property
    def is_file_backed(self) -> bool:
        return self.file is not None


def _etag_of(st) -> str:
    """revalidate 分支的校验器：md5("mtime-size")，与 FileResponse 自带那套同算法。"""
    return '"%s"' % hashlib.md5(
        f"{st.st_mtime}-{st.st_size}".encode(),
        usedforsecurity=False).hexdigest()


def _gzip_body(f: Path, st) -> bytes:
    """有界（32 项）gzip 缓存；键随文件变，天然失效。"""
    key = f"{f}|{st.st_mtime_ns}|{st.st_size}"
    body = _gz_cache.get(key)
    if body is None:
        body = gzip.compress(f.read_bytes(), 6)
        if len(_gz_cache) > 32:
            _gz_cache.clear()
        _gz_cache[key] = body
    return body


def serve(static_root: Path, file_path: str, vtoken: str = "",
          accept_encoding: str = "", if_none_match: str = "") -> ServePlan:
    """静态资源服务的**唯一外部入口**：给定请求要素，回一个纯值 ServePlan。

    不变量（逐字节冻结，见 tests/test_staticguard_serve.py 的 golden 矩阵）：
      · immutable 唯一准入 = vtoken == 文件当前内容哈希；否则退回 revalidate（安全阀）。
      · immutable 分支**不发任何校验器、永不 304**：换新体的唯一途径是 URL 变化本身
        （09-23 事故：ETag 是 mtime+size 的函数，与 URL 提手无关 ⇒ 不同 ?v= 共用同一 ETag）。
      · revalidate 分支发 ETag；If-None-Match 命中 ⇒ 304，且 304 **带 Vary**。
      · gzip 仅对 _GZ_SUFFIX 且 size≥_GZ_MIN 且客户端 Accept-Encoding 含 gzip 时生效。
      · immutable 与 gzip 分支给全量字节（不走 FileResponse，避免它补出与提手无关的 etag）。
    """
    f = resolve_serveable(static_root, file_path)
    if f is None:
        return ServePlan(status=404, headers={})
    st = f.stat()
    policy, want_tok = cache_policy(vtoken, f)
    headers = {"Vary": "Accept-Encoding", "X-Asset-Token": want_tok}
    if policy == "immutable":
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        etag = _etag_of(st)
        headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache", "ETag": etag})
        # 304 分支带 Vary：共享缓存可能把 gzip 版回给不接受 gzip 的客户端（此前漏过）。
        if if_none_match == etag:
            return ServePlan(status=304, headers=headers)
    media = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
    if (f.suffix.lower() in _GZ_SUFFIX and st.st_size >= _GZ_MIN
            and "gzip" in (accept_encoding or "")):
        return ServePlan(status=200, media_type=media, body=_gzip_body(f, st),
                         headers={**headers, "Content-Encoding": "gzip",
                                  "Vary": "Accept-Encoding"})
    if policy == "immutable":
        return ServePlan(status=200, media_type=media, body=f.read_bytes(), headers=headers)
    return ServePlan(status=200, media_type=media, file=f, headers=headers)


def to_response(plan: ServePlan):
    """把 ServePlan 贴成 starlette 响应 —— 本模块唯一的框架耦合点（惰性 import）。

    逐字对齐深化前的构造方式（含 304/404 **不带 media_type**）：
      · file-backed（revalidate 非 gzip）走 FileResponse —— 它会补 accept-ranges/etag，
        正是 revalidate 分支需要的；
      · immutable 与 gzip 分支走 Response(bytes) —— 不带 accept-ranges。
    """
    from starlette.responses import FileResponse, Response
    if plan.is_file_backed:
        return FileResponse(str(plan.file), headers=plan.headers, media_type=plan.media_type)
    if plan.status in (304, 404):
        return Response(status_code=plan.status, headers=plan.headers)
    return Response(content=plan.body or b"", media_type=plan.media_type, headers=plan.headers)