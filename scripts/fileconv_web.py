#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fileconv-web — agenthub「基础设施 → 文件转换」子菜单的本地后端。

上游：KnifeLemon/Filee v2.5.0（MIT）https://github.com/KnifeLemon/Filee
引入理由：Filee 只有桌面 GUI + CLI，无 HTTP 端口；hub 的 panel/embed 入口要求 URL+端口，
故把 `filee convert` 包成「上传 → 转换 → 下载」，只绑 127.0.0.1:8085、noauth，
入口鉴权全部交给 hub 桥（embed_proxy.py 未登录 302 到 hub 登录页，全站只一套登录）。
安全：上传体上限 50MB、扩展名/格式正则白名单、subprocess 参数数组（不拼 shell）、
每个请求独立 job 目录且用后即删。
"""
import json
import os
import re
import shutil
import subprocess
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8085
FILEE = "/vol1/1000/filee/Filee-2.5.0-linux-x64/filee-cli"
JOBS = Path("/vol1/1000/filee/jobs")
MAX_UPLOAD = 50 * 1024 * 1024
MAX_BODY = MAX_UPLOAD + 64 * 1024
FMT_RE = re.compile(r"^[a-z0-9][a-z0-9_+-]{0,15}$")
EXT_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
TIMEOUT = 120

PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>文件转换 · Filee</title><style>
body{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#222}
h1{font-size:20px}p{font-size:14px;color:#555}.row{display:flex;gap:8px;align-items:center;margin:12px 0}
input,button{font-size:14px;padding:8px}button{cursor:pointer}
#msg{margin-top:16px;white-space:pre-wrap;font-size:13px}</style></head><body>
<h1>文件转换（Filee v2.5.0 · 本机离线）</h1>
<p>选择文件、填目标格式（如 <code>webp</code>、<code>pdf</code>），转换后自动下载。</p>
<form id="f"><div class="row"><input type="file" name="file" required></div>
<div class="row">目标格式 <input name="fmt" value="webp" required size="10"></div>
<div class="row"><button type="submit">转换并下载</button></div></form>
<div id="msg"></div><script>
document.getElementById('f').addEventListener('submit',async function(e){
 e.preventDefault();const m=document.getElementById('msg');m.textContent='转换中…';
 const fd=new FormData(e.target);
 try{const r=await fetch('/convert',{method:'POST',body:fd});
  if(!r.ok){m.textContent='失败：'+r.status+' '+await r.text();return;}
  const b=await r.blob();const cd=r.headers.get('Content-Disposition')||'';
  const mm=/filename="([^"]+)"/.exec(cd);const name=mm?mm[1]:'converted';
  const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=name;a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),10000);m.textContent='完成：'+name;
 }catch(err){m.textContent='请求失败：'+err;}
});</script></body></html>"""


def _reply(h, code, body, ctype="text/plain; charset=utf-8", extra=None):
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    for k, v in (extra or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)


def parse_multipart(body, boundary):
    """只取 file / fmt 两个字段，不引入第三方解析器。"""
    out = {"file": None, "fmt": None}
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        if b'name="file"' in head:
            m = re.search(rb'filename="([^"]*)"', head)
            name = m.group(1).decode("utf-8", "replace") if m else "upload"
            out["file"] = (name, data)
        elif b'name="fmt"' in head:
            out["fmt"] = data.decode("utf-8", "replace").strip()
    return out


def _do_convert(h, fields):
    if not fields["file"]:
        _reply(h, 400, "缺少文件".encode())
        return
    fmt = (fields["fmt"] or "").lower()
    if not FMT_RE.fullmatch(fmt):
        _reply(h, 400, "目标格式不合法".encode())
        return
    name, blob = fields["file"]
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(name).suffix.lower()
    if not EXT_RE.fullmatch(suffix):
        _reply(h, 400, "文件扩展名不合法".encode())
        return
    JOBS.mkdir(parents=True, exist_ok=True)
    job = JOBS / uuid.uuid4().hex
    out = job / "out"
    out.mkdir(parents=True)
    src = job / ("input" + suffix)
    try:
        src.write_bytes(blob)
        proc = subprocess.run(
            [FILEE, "convert", str(src), "--to", fmt, "-o", str(out)],
            capture_output=True, timeout=TIMEOUT, check=False)
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout).decode("utf-8", "replace")[-800:]
            _reply(h, 400, ("转换失败：\n" + msg).encode())
            return
        files = [p for p in out.iterdir() if p.is_file()]
        if not files:
            _reply(h, 500, b"no output")
            return
        _reply(h, 200, files[0].read_bytes(), "application/octet-stream",
               {"Content-Disposition": 'attachment; filename="input.%s"' % fmt})
    except subprocess.TimeoutExpired:
        _reply(h, 504, b"timeout")
    finally:
        shutil.rmtree(job, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "fileconv-web/1.0"

    def do_GET(self):
        if self.path.startswith("/health"):
            ok = Path(FILEE).is_file() and os.access(FILEE, os.X_OK)
            _reply(h=self, code=200 if ok else 503,
                   body=json.dumps({"ok": ok, "filee": FILEE}).encode(),
                   ctype="application/json")
            return
        if self.path in ("/", "/index.html"):
            _reply(self, 200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        _reply(self, 404, b"not found")

    def do_POST(self):
        if self.path != "/convert":
            _reply(self, 404, b"not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            _reply(self, 400, b"bad length")
            return
        if length <= 0 or length > MAX_BODY:
            _reply(self, 413, b"too large")
            return
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=([^;]+)", ctype)
        if not ctype.startswith("multipart/form-data") or not m:
            _reply(self, 400, b"expect multipart/form-data")
            return
        _do_convert(self, parse_multipart(self.rfile.read(length),
                                          m.group(1).strip('"').encode()))

    def log_message(self, fmt, *args):
        print("[fileconv-web] " + (fmt % args), flush=True)


def main():
    JOBS.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print("fileconv-web on %s:%d" % (HOST, PORT), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
