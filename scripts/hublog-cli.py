#!/usr/bin/env python3
"""hublog 直查 CLI（v0.13.48）——给「操作 agent / 运维脚本」用的日志取数入口。

为什么要有它：设置→日志页是给人看的 UI，操作 agent 排障时不该去点浏览器。
`GET /api/hublog` 本就是**只读**接口，但它按写方法鉴权（日志含 IP/路径/查询词），
裸 curl 要自己拼口令与端口——口令一不小心就被写进命令历史或子代理回显。
本脚本自己读 `.env`，口令只进请求头、从不回显任何输出。

用法（exit 0 有内容 / 2 鉴权 / 3 连不上 / 4 参数非法 / 5 服务端错）：
    python3 scripts/hublog-cli.py                        # 近 24h 全部，文本
    python3 scripts/hublog-cli.py --source error         # 只看错误（journald+事件表）
    python3 scripts/hublog-cli.py --source journal --window 1
    python3 scripts/hublog-cli.py --source rest --subject kb.search
    python3 scripts/hublog-cli.py --q "model/apply" --limit 50
    python3 scripts/hublog-cli.py --json                 # 机器读（原样透传）
    python3 scripts/hublog-cli.py --list-subjects        # 列出合法 subject

口径与页面完全一致（同一端点、同一过滤、同一次序）：
    source = all|journal|event|rest|error，level = all|info|warn|error
    subject 只作用于事件表（runlog.SUBJECTS 白名单，非法 400）
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 15
EXIT_AUTH, EXIT_NET, EXIT_PARAM, EXIT_SERVER = 2, 3, 4, 5


def _read_env(path):
    """解析 KEY=VALUE（跳过注释/空行，剥掉成对引号）。缺文件返回 {}。"""
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return out
    for ln in raw.splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _build_url(base, params):
    """只走 urlencode —— 关键字是自由输入，绝不能拼进命令行或裸字符串。"""
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)},
                                encoding="utf-8")
    return base.rstrip("/") + "/api/hublog" + ("?" + qs if qs else "")


def _fetch(url, auth, timeout=DEFAULT_TIMEOUT):
    req = urllib.request.Request(url, method="GET", headers=(auth or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:                 # 4xx/5xx 也带响应体（原因在里面）
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def _die(code, msg):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def main(argv=None):
    ap = argparse.ArgumentParser(description="agenthub 日志直查（只读，与设置→日志页同口径）")
    ap.add_argument("--source", default="all",
                    choices=["all", "journal", "event", "rest", "error"])
    ap.add_argument("--level", default="all", choices=["all", "info", "warn", "error"])
    ap.add_argument("--subject", default="", help="只作用于事件表（--list-subjects 看合法值）")
    ap.add_argument("--q", default="", help="关键字（msg/tag 子串，最长 120）")
    ap.add_argument("--window", type=int, default=24, help="时间窗小时数，0=不限，上限 720")
    ap.add_argument("--limit", type=int, default=200, help="返回条数，1~500")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON（默认输出文本行）")
    ap.add_argument("--list-subjects", action="store_true", help="只打印合法 subject 后退出")
    ap.add_argument("--base", default="", help="覆盖 http://127.0.0.1:<PORT>")
    ap.add_argument("--port", type=int, default=0, help="覆盖 .env 里的 PORT")
    ap.add_argument("--env", default="", help="覆盖 .env 路径")
    a = ap.parse_args(argv)

    # realpath 而不是 abspath：~/bin/hublog 是指向本脚本的软链接，abspath 会停在
    # 链接那一侧 ⇒ 仓库根算成 /home/gztxt，读不到 agenthub/.env（实跑踩过）。
    repo = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    env = _read_env(a.env or os.path.join(repo, ".env"))
    # 2026-11-07 用户裁定：机器侧身份 = HTTP Basic（共享 token 兼容面已删除）。
    # 用户名缺省 gztxt，密码取 .env 既有的 HUB_SUPERADMIN_PW ⇒ 零新增凭据。
    _user = env.get("HUB_BASIC_USER") or "gztxt"
    _pw = env.get("HUB_SUPERADMIN_PW", "")
    auth = {}
    if _pw:
        import base64 as _b64
        auth = {"Authorization": "Basic " + _b64.b64encode(
            ("%s:%s" % (_user, _pw)).encode("utf-8")).decode("ascii")}
    port = a.port or int(env.get("PORT") or 3102)
    base = a.base or ("http://127.0.0.1:%d" % port)

    params = {"source": a.source, "level": a.level, "subject": a.subject,
              "q": (a.q or "")[:120], "window": a.window, "limit": a.limit,
              "format": "text"}
    if a.list_subjects:
        params.update({"limit": 1, "format": "json"})
    if a.json:
        params["format"] = "json"

    url = _build_url(base, params)
    if not auth:
        _die(EXIT_AUTH, "未读到 HUB_SUPERADMIN_PW（.env=%s）——服务端也会按未登录拒绝"
             % (a.env or os.path.join(repo, ".env")))
    status, body = _fetch(url, auth)
    if status == 0:
        _die(EXIT_NET, "连不上 %s —— %s（服务没起？systemctl --user restart agenthub.service）"
             % (base, body))
    if status in (401, 403):
        _die(EXIT_AUTH, "鉴权被拒（HTTP %d）：用户名/密码与服务端不一致" % status)
    if status == 503:
        _die(EXIT_AUTH, "服务端用户库不可用（HTTP 503）：先修 agenthub 启动")
    if status == 400:
        _die(EXIT_PARAM, "参数非法（HTTP 400）：%s" % body[:300])
    if status >= 500:
        _die(EXIT_SERVER, "服务端报错（HTTP %d）：%s" % (status, body[:300]))

    if a.list_subjects:
        try:
            print("\n".join(json.loads(body).get("subjects") or []))
        except ValueError:
            _die(EXIT_SERVER, "解析 subjects 失败：%s" % body[:200])
        return 0
    if not body.strip():
        # 空结果必须说清"查了什么"——否则子代理会把"没匹配"当成"接口坏了/没日志"
        print("（无匹配条目：source=%s level=%s subject=%s q=%r window=%sh）"
              % (a.source, a.level, a.subject or "-", a.q or "", a.window))
        return 0
    print(body, end="" if body.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
