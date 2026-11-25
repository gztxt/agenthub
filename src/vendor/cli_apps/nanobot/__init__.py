"""本地 vendored 的 `nanobot` 包根（**唯一非字节级文件**）。

上游 `nanobot/__init__.py`（3505B）会在 import 时读 tomllib、探测包版本、做
lazy 符号导出——那是给「整包安装的 nanobot 应用」用的，我们只借用 `apps` 子包。
硬把这 3.5KB 搬过来只会让 hub 启动多一层无用的 import 风险，故这里写空壳。

**其余全部文件与上游逐字节相同**，清单与 md5 见 `../../MANIFEST.md`。
升级上游时：只替换 MANIFEST 里列出的文件，本文件不必跟着变。
"""
