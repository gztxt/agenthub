# 契约路径互校（方案 §P2「文档中的文件路径不存在」CI 防漂移）：
# docs/CONTRACTS.md 点名的 src 路径必须真实存在，缺一即红。
import os

_HERE = os.path.dirname(__file__)


def _repo():
    return os.path.abspath(os.path.join(_HERE, ".."))


def _contract_named_paths():
    text = open(os.path.join(_repo(), "docs", "CONTRACTS.md")).read()
    found = []
    i = 0
    while True:
        i = text.find("`src/", i)
        if i < 0:
            break
        j = text.find("`", i + 4)
        tok = text[i + 4:j].split(":")[0]  # 只校文件名，行号漂移由 L0 钉
        found.append(tok)
        i = j + 1
    return found


def test_contracts_named_paths_exist():
    missing = [p for p in _contract_named_paths() if not os.path.exists(os.path.join(_repo(), p))]
    assert not missing, f"CONTRACTS.md 点名的路径在仓里不存在: {missing}"
