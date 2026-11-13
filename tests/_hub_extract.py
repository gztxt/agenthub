"""从 static/hub.js 里**原样抽出**指定函数/常量，供真浏览器探针使用。

为什么要有这个文件：探针若手抄一份"我以为是这个样子"的实现去跑，
真代码被删改时探针照样绿 —— 那是假绿。抽真代码就消除了这条路。

收尾判据用「第 0 列的 }」而不是花括号配对：
hub.js 的注释里就有 `{prefix:"?",final:"c"}` 这类字面量，
朴素配对会被**注释里的** `}` 提前闭合而截断函数体（实测栽过一次）。
本文件风格里顶层函数一律第 0 列收尾，内部块全部缩进。
"""
import re
from pathlib import Path

HUB_JS = Path(__file__).resolve().parents[1] / "static" / "hub.js"


def read_hub():
    return HUB_JS.read_text(encoding="utf-8")


def extract_function(src, name):
    """返回 `function name(...) { ... }` 原文（含结尾第 0 列的 }）。"""
    i = src.find("function %s(" % name)
    if i < 0:
        return None
    end = src.find("\n}\n", i)
    if end < 0:
        return None
    return src[i:end + 2]


def extract_line(src, prefix):
    """抽单行（如 `const TERM_QUERY_OSC = [...]`）。

    行首允许空白缩进：`let termMouseLive` 这类声明在 02 分片里带 1 空格缩进，
    顶格锚定会假性「抽不到」（2026-11-13 verify_stream_decode 实抓）。"""
    m = re.search(r"^[ \t]*%s.*$" % re.escape(prefix), src, re.M)
    return m.group(0) if m else None


def build_page(extra_src, body_js, vendor_rel="vendor"):
    """拼一个最小测试页：加载本仓 vendor 的 xterm，注入抽出来的真代码，再跑 body_js。"""
    return (
        '<!doctype html><html><head><meta charset="utf-8">\n'
        '<link rel="stylesheet" href="%s/xterm.css"></head>\n'
        '<body><div id="a"></div><div id="b"></div><div id="c"></div><div id="d"></div><pre id="out">PENDING</pre>\n'
        '<script src="%s/xterm.js"></script>\n<script>\n'
        "/* ==== 以下从 static/hub.js 原样抽出，非手抄 ==== */\n%s\n/* ==== 抽取结束 ==== */\n%s\n"
        "</script></body></html>\n"
    ) % (vendor_rel, vendor_rel, extra_src, body_js)
