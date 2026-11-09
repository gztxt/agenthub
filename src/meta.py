"""协议能力协商（P4 批次，借鉴 t3code 工程思维）。

只读端点 GET /api/meta：返回带版本号的元信息，让旧客户端能**明确降级**
而非静默错乱。本批只做到「返回元信息」，不做服务端强制 426（那要改 WS
handler，超出本批范围）。

设计边界
--------
- 只读新增，不改任何现有端点行为：GET 不进 write_gate（writeauth.decide()
  对非写方法返回 "allow"），也不进 authz 写路由声明表（该表只收
  post/put/patch/delete）。
- `version` 不自造字面量：取 `request.app.version`。单一版本源是
  main.py:128 的 `VERSION`，由 main.py:650 `FastAPI(version=VERSION)`
  注入 app 元数据，/health（main.py:964）也取同一处 ⇒ 运行时该值恒等于
  服务端版本。放在**请求期**读、而非模块期 `from main import VERSION`，
  是为了避开循环导入（main 在定义 VERSION 之前就 import 了本模块），且
  让 L0 测试能用「只挂本 router 的全新 app」而不 import main（会起 vitals
  后台循环，见 tests/test_settings_bundle_l0.py 口径）。
"""

from fastapi import APIRouter, Request

router = APIRouter(tags=["meta"])

#: 协议契约版本（整数）。客户端读到不认识的 protocol 时应走降级路径。
PROTOCOL_VERSION = 1

#: 最低客户端版本。主会话 2026-11-09 裁定：**None = 未启用**。
#: 本机前端（static/hub/*.js）随服务端同源发布、没有独立版本号，写一个
#: 「0.13.0」之类看似权威的值等于回填假数据（同 db.py 里 pw_hash='' 的诚实表达）。
#: 能力协商的降级判据靠 `protocol` + `capabilities`，不依赖本字段；
#: 未来接入有独立版本号的原生客户端时，再赋语义版本并让客户端据此比对。
MIN_CLIENT_VERSION = None

#: 协议层能力标签（v1 初始集，后续批次按需扩展；均为非空字符串）。
#: 取的是已落地的只读服务端能力，不臆造未验证的特性。
CAPABILITIES = [
    "meta",                 # GET /api/meta 协议能力协商端点（本端点）
    "version_negotiation",  # 返回 protocol/version/min_client 供判兼容
    "health",               # /health 自证端点（main.py:949）
]


@router.get("/api/meta")
def get_meta(request: Request) -> dict:
    """协议能力协商元信息（GET、只读）。"""
    return {
        "protocol": PROTOCOL_VERSION,
        "capabilities": list(CAPABILITIES),
        "version": request.app.version,
        "min_client": MIN_CLIENT_VERSION,
    }
