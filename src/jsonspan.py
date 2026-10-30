"""JSON 的**定点编辑**：只改目标 key 的字符区间，绝不整文件重新序列化。

为什么需要它（2026-10-30，借 magpie 的「只动那一个 key」设计）：

`json.loads` → 改 dict → `json.dumps` 这条路在语义上没错，但**落盘字节全变**：
实测 `~/.claude/settings.json` 是 4 空格缩进、55 行，hub 每次保存都按 `indent=2`
重排整个文件 ⇒ 用户看到的 diff 是"整份文件都被改了"，而 09-28 那个
「CCR 启动时重写 claude settings」的漂移问题，正是靠人读 diff 发现的。
把无关字节也改掉，等于自己制造同款噪音、并让下一个写入方的 diff 无从比对。

magpie 的做法（README 原文）是「只改唯一那个 key，原子写，注释/顺序/格式照旧」，
并且"抹掉注释后连 JSON 都解析不了的文件**报错而不当空文件读**"（never read as empty）。
本模块按这两条实现：

- `blank_comments()`：把 `//` 与 `/* */` 抹成等长空格（保持偏移与行号不变），
  仅用于**校验**；写入始终基于原文区间替换。
- `set_string()` / `set_value()` / `append_object()` / `append_member()`：返回改好的全文，
  未触碰的字节逐字不变。

⚠ 边界：这是**区间替换**不是 JSON 解析器，本模块只承诺两件事 ——
① 改写后的全文仍能通过 `json.loads`（用例逐条断言）；② 区间外字节不变。
不支持注释内嵌在目标 value 内部的畸形写法（那种情况 `blank_comments` 校验会先报错）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

__all__ = ["JsonSpanError", "blank_comments", "validate", "set_string", "set_value",
           "append_object", "append_member", "get_string"]

_WS = " \t\r\n"


class JsonSpanError(ValueError):
    """文件形状不支持定点编辑（调用方应回落到整份重写，或直接拒绝落笔）。"""


def blank_comments(text: str) -> str:
    """把 `//` 行注释与 `/* */` 块注释抹成空格，字符串内部不动。

    抹成**等长空格**而不是删除：行号、字符偏移全部保持不变，报错位置才能对上原文件。
    """
    out = list(text)
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            if j < 0:
                raise JsonSpanError("块注释未闭合（拒绝按空文件处理）")
            for k in range(i, j + 2):
                out[k] = "\n" if out[k] == "\n" else " "
            i = j + 2
            continue
        i += 1
    return "".join(out)


def validate(text: str) -> Any:
    """抹注释后解析；失败抛 `JsonSpanError`（**绝不**把坏文件当空对象）。"""
    try:
        return json.loads(blank_comments(text))
    except json.JSONDecodeError as e:
        raise JsonSpanError(f"不是合法 JSON（{e.msg} @ 行{e.lineno} 列{e.colno}）") from e


class _Node:
    __slots__ = ("kind", "start", "end", "members")

    def __init__(self, kind: str, start: int, end: int,
                 members: List[Tuple[Any, int, int, int, int]]):
        self.kind = kind            # "object" | "array" | "scalar"
        self.start = start          # 指向 '{' / '[' / 首字符
        self.end = end              # 指向闭合括号之后 / 标量之后
        self.members = members      # (key, key_start, key_end, val_start, val_end)


class _Scan:
    def __init__(self, s: str):
        self.s = s
        self.n = len(s)

    # ── 基本动作 ───────────────────────────────────────────────
    def skip(self, i: int) -> int:
        s, n = self.s, self.n
        while i < n:
            c = s[i]
            if c in _WS:
                i += 1
            elif c == "/" and i + 1 < n and s[i + 1] == "/":
                j = s.find("\n", i)
                i = n if j < 0 else j + 1
            elif c == "/" and i + 1 < n and s[i + 1] == "*":
                j = s.find("*/", i + 2)
                if j < 0:
                    raise JsonSpanError("块注释未闭合")
                i = j + 2
            else:
                return i
        return i

    def str_end(self, i: int) -> int:
        if i >= self.n or self.s[i] != '"':
            raise JsonSpanError(f"期望字符串，得到 {self.s[i:i+1]!r}")
        j = i + 1
        while j < self.n:
            c = self.s[j]
            if c == "\\":
                j += 2
                continue
            if c == '"':
                return j + 1
            j += 1
        raise JsonSpanError("字符串未闭合")

    def value_end(self, i: int) -> int:
        i = self.skip(i)
        if i >= self.n:
            raise JsonSpanError("值缺失")
        c = self.s[i]
        if c == '"':
            return self.str_end(i)
        if c in "{[":
            return self.node(i).end
        j = i
        while j < self.n and self.s[j] not in ",}]" + _WS:
            j += 1
        if j == i:
            raise JsonSpanError(f"无法识别的值 {self.s[i:i+12]!r}")
        return j

    # ── 结构 ───────────────────────────────────────────────────
    def node(self, i: int) -> _Node:
        i = self.skip(i)
        if i >= self.n:
            raise JsonSpanError("文件在期望值处结束")
        c = self.s[i]
        if c == "{":
            return self._obj(i)
        if c == "[":
            return self._arr(i)
        return _Node("scalar", i, self.value_end(i), [])

    def _obj(self, i: int) -> _Node:
        members: List[Tuple[Any, int, int, int, int]] = []
        start = i
        i += 1
        while True:
            i = self.skip(i)
            if i >= self.n:
                raise JsonSpanError("对象未闭合")
            if self.s[i] == "}":
                return _Node("object", start, i + 1, members)
            if self.s[i] != '"':
                raise JsonSpanError(f"期望键名，得到 {self.s[i:i+12]!r}")
            ks = i
            ke = self.str_end(i)
            key = json.loads(self.s[ks:ke])
            i = self.skip(ke)
            if i >= self.n or self.s[i] != ":":
                raise JsonSpanError("键名后缺冒号")
            i = self.skip(i + 1)
            vs = i
            ve = self.value_end(i)
            members.append((key, ks, ke, vs, ve))
            i = self.skip(ve)
            if i < self.n and self.s[i] == ",":
                i += 1
                continue
            if i < self.n and self.s[i] == "}":
                continue

    def _arr(self, i: int) -> _Node:
        members: List[Tuple[Any, int, int, int, int]] = []
        start = i
        i += 1
        idx = 0
        while True:
            i = self.skip(i)
            if i >= self.n:
                raise JsonSpanError("数组未闭合")
            if self.s[i] == "]":
                return _Node("array", start, i + 1, members)
            vs = i
            ve = self.value_end(i)
            members.append((idx, vs, vs, vs, ve))
            idx += 1
            i = self.skip(ve)
            if i < self.n and self.s[i] == ",":
                i += 1
                continue
            if i < self.n and self.s[i] == "]":
                continue


def _root(text: str) -> _Node:
    sc = _Scan(text)
    n = sc.node(0)
    if n.kind != "object":
        raise JsonSpanError("顶层不是对象")
    return n


def _walk(text: str, path: Tuple[str, ...]) -> _Node:
    node = _root(text)
    for key in path:
        if node.kind != "object":
            raise JsonSpanError(f"{'.'.join(path)}：中途遇到非对象")
        hit = next((m for m in node.members if m[0] == key), None)
        if hit is None:
            raise KeyError(key)
        node = _Scan(text).node(hit[3])
    return node


def _member(text: str, path: Tuple[str, ...]) -> Tuple[_Node, Tuple[Any, int, int, int, int]]:
    if not path:
        raise JsonSpanError("路径为空")
    parent = _walk(text, path[:-1])
    if parent.kind != "object":
        raise JsonSpanError(f"{'.'.join(path[:-1]) or '<root>'} 不是对象")
    hit = next((m for m in parent.members if m[0] == path[-1]), None)
    if hit is None:
        raise KeyError(path[-1])
    return parent, hit


def get_string(text: str, path: Tuple[str, ...]) -> Optional[str]:
    """取字符串值；键不存在返回 None，值不是字符串抛 JsonSpanError。"""
    try:
        _, m = _member(text, path)
    except KeyError:
        return None
    raw = text[m[3]:m[4]]
    if not raw.startswith('"'):
        raise JsonSpanError(f"{'.'.join(path)} 不是字符串值")
    return json.loads(raw)


def set_string(text: str, path: Tuple[str, ...], value: str) -> Tuple[str, Optional[str]]:
    """只替换该 key 的字符串值，返回 (新全文, 旧值)。键不存在抛 KeyError（调用方回落）。

    旧值取不到（值不是字符串）时抛 JsonSpanError —— 静默覆盖掉一个对象/数字，
    属于"看起来成功、其实改了用户别的东西"。
    """
    _, m = _member(text, path)
    raw = text[m[3]:m[4]]
    if not raw.startswith('"'):
        raise JsonSpanError(f"{'.'.join(path)} 不是字符串值，拒绝覆盖")
    old = json.loads(raw)
    lit = json.dumps(value, ensure_ascii=False)
    return text[:m[3]] + lit + text[m[4]:], old


def set_value(text: str, path: Tuple[str, ...], value: Any) -> Tuple[str, Any]:
    """把**标量**值替换成任意 JSON 值（布尔/数字/字符串），返回 (新全文, 旧值)。

    只允许覆盖标量：原值是对象/数组时抛 `JsonSpanError` —— 那等于"改个默认值顺手把
    用户整段配置删了"。键不存在抛 KeyError（调用方回落整份重写）。
    """
    _, m = _member(text, path)
    raw = text[m[3]:m[4]]
    if raw[:1] in ("{", "["):
        raise JsonSpanError(f"{'.'.join(path)} 是对象/数组，拒绝整体覆盖")
    return text[:m[3]] + json.dumps(value, ensure_ascii=False) + text[m[4]:], json.loads(raw)


def _line_indent(text: str, pos: int) -> str:
    """pos 所在行的前导空白（用于对齐插入）。"""
    ls = text.rfind("\n", 0, pos) + 1
    j = ls
    while j < len(text) and text[j] in " \t":
        j += 1
    return text[ls:j]


def _indent_unit(text: str, container: _Node) -> str:
    """从容器内第一个成员反推缩进单位；推不出来按两个空格。"""
    if container.members:
        first = container.members[0]
        key_pos = first[1] if container.kind == "object" else first[3]
        ci = _line_indent(text, container.start)
        ki = _line_indent(text, key_pos)
        if len(ki) > len(ci) and ki.startswith(ci):
            return ki[len(ci):]
    return "  "


def append_object(text: str, path: Tuple[str, ...], obj: Dict[str, Any]) -> str:
    """在 `path` 指向的数组末尾追加一个对象（只插入，不改任何已有字节）。"""
    arr = _walk(text, path)
    if arr.kind != "array":
        raise JsonSpanError(f"{'.'.join(path)} 不是数组")
    close = arr.end - 1                       # 指向 ']'
    if "\n" not in text[arr.start:arr.end]:
        # 单行数组（{"a":[1]}）：整块内联追加，不要硬塞多行缩进把一行拆成五行
        lit = json.dumps(obj, ensure_ascii=False)
        chunk = ", " + lit if arr.members else lit
        return text[:close] + chunk + text[close:]
    cind = _line_indent(text, close)
    unit = _indent_unit(text, arr)
    eind = cind + unit
    body = json.dumps(obj, ensure_ascii=False, indent=len(unit)).replace("\n", "\n" + eind)
    if not arr.members:
        new_chunk = "\n" + eind + body + "\n" + cind
        at = close
        return text[:at] + new_chunk + text[at:]
    last_end = arr.members[-1][4]
    new_chunk = ",\n" + eind + body
    return text[:last_end] + new_chunk + text[last_end:]


def append_member(text: str, path: Tuple[str, ...], key: str, value: Any) -> str:
    """在 `path` 指向的**对象**末尾插入一个成员（只插入，不改任何已有字节）。

    键已存在抛 `KeyError`（调用方据此走"已登记，无需追加"的分支），
    路径/形状不认识抛 `JsonSpanError`。
    """
    obj = _walk(text, path)
    if obj.kind != "object":
        raise JsonSpanError(f"{'.'.join(path)} 不是对象")
    if any(m[0] == key for m in obj.members):
        raise KeyError(key)
    close = obj.end - 1                       # 指向 '}'
    lit = json.dumps(key, ensure_ascii=False) + ": " + json.dumps(value, ensure_ascii=False)
    if "\n" not in text[obj.start:obj.end]:
        chunk = (", " + lit) if obj.members else lit
        return text[:close] + chunk + text[close:]
    cind = _line_indent(text, close)
    unit = _indent_unit(text, obj)
    eind = cind + unit
    body = (json.dumps(key, ensure_ascii=False) + ": "
            + json.dumps(value, ensure_ascii=False, indent=len(unit)).replace("\n", "\n" + eind))
    if not obj.members:
        return text[:close] + "\n" + eind + body + "\n" + cind + text[close:]
    last_end = obj.members[-1][4]
    return text[:last_end] + ",\n" + eind + body + text[last_end:]
