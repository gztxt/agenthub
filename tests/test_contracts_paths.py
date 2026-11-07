# 契约路径互校（方案 §P2「文档中的文件路径不存在」CI 防漂移）：
# docs/CONTRACTS.md 点名的 src 路径必须真实存在，缺一即红。
# 2026-11-07 收成 TestCase：原模块级裸 test_ 只有 pytest 收、unittest discover 不收
# ⇒ tier parity 对账差一条（PT 判据：标准层不得存在 unittest 收不到的 test_）。
import os
import unittest

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
        found.append("src/" + tok)
        i = j + 1
    return found


class TestContractsPaths(unittest.TestCase):

    def test_contracts_named_paths_exist(self):
        missing = [p for p in _contract_named_paths() if not os.path.exists(os.path.join(_repo(), p))]
        self.assertEqual(missing, [], "CONTRACTS.md 点名的路径在仓里不存在: %s" % missing)


if __name__ == "__main__":
    unittest.main()
