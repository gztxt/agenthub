"""登录闸（2026-11-07）适配：未登录页面不再注入 hub.js ⇒ 打开首页的 CDP 探针
全部「hub.js 未执行」假红（2026-11-13 实测 6 个探针同此症状）。

本 helper 在页面就绪后**手动注入** `/static/hub.js`（裸路径，no-cache 但 200 可访问），
并等 `go / initSidebar` 就绪 —— 等价于已登录用户拿到的页面。注入的是服务真身而非手抄，
不改变任何被测行为，只恢复探针曾经成立的前提。

注意：`Page.navigate` 之后页面上下文重建，**每次导航后都要重新注入**。
Fetch 拦截型探针（掐 hub.js 的红测）不受影响：手动注入产生的请求同样命中
`urlPattern`，拦截计数语义保持。
"""
import hashlib
import pathlib
import time

_HUB_DISK = pathlib.Path(__file__).resolve().parents[1] / "static" / "hub.js"


def _token():
    """产物内容 md5 前 8 位，与 build_hubjs.sh 的 ?v= 同口径 —— 注入带 token，
    `?diag=1` 面板的「hub.js 标签带版本号」与 hist_tdz 的提手断言才判得出。"""
    try:
        return hashlib.md5(_HUB_DISK.read_bytes(), usedforsecurity=False).hexdigest()[:8]
    except Exception:
        return ""

HUB_JS_SRC = "/static/hub.js?v=" + _token()

_INJECT_JS = (
    # 登录闸的 gate 遮罩必须一并隐藏：loadHub() 正常流程会 display:none，
    # 不然它整层盖住页面，探针的点击/聚焦全都落在 gate 上（2026-11-13 实测：
    # 「点了 localprojects 停在 classroom」即此因）。
    "(function(){var g=document.getElementById('loginGate');if(g)g.style.display='none';"
    "var s=document.createElement('script');"
    "s.src='%s';document.body.appendChild(s);})()" % HUB_JS_SRC
)

_READY_JS = ("typeof window.go==='function' && "
             "typeof window.initSidebar==='function'")


def inject_hub(c, wait_ms=10000, ready_js=_READY_JS):
    """向当前页面注入 hub.js 并等核心符号就绪。返回 True=就绪 / False=超时。"""
    try:
        c.eval(_INJECT_JS)
    except Exception:
        return False
    deadline = time.time() + wait_ms / 1000.0
    while time.time() < deadline:
        try:
            if c.eval(ready_js):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False
