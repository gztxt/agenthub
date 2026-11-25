# vendored `nanobot/apps`（CLI Apps 应用功能模块）清单

| 项 | 值 |
|---|---|
| 上游 | [HKUDS/nanobot](https://github.com/HKUDS/nanobot)，PyPI `nanobot-ai` |
| 版本 | **0.4.2**（本机 `/vol1/tools/nanobot-0.4.2/venv/lib/python3.12/site-packages`） |
| 许可证 | **MIT**（`nanobot_ai-0.4.2.dist-info/METADATA` 实测 `License: MIT`） |
| 拷贝日期 | 2026-11-25 |
| 拷贝方式 | `cp` 原文件，**未做任何编辑**；md5 见下表，闸门 `tests/test_cliapps_l0.py` 会核 |

## 逐字节相同的文件（10 个）

| 文件 | md5 |
|---|---|
| `nanobot/apps/__init__.py` | `daddc3c56e974d6b300c364f22e56473` |
| `nanobot/apps/protocol.py` | `d8a479230c04c04ce93a8d1cddf9784b` |
| `nanobot/apps/cli/__init__.py` | `5c78d3c63cbe24cb358bfe3aaf9dda4f` |
| `nanobot/apps/cli/service.py` | `5ca02b9f470a37377b3a92e7c64f13ef` |
| `nanobot/apps/cli/utils.py` | `4b24b9647ea33619cbf23f9ce31203ea` |
| `nanobot/agent/skill_metadata.py` | `4d94478978aac0de120ba1ccf3438ca9` |
| `nanobot/config/home.py` | `d7b93edef34e54b80177e346d8172398` |
| `nanobot/config/paths.py` | `f5d4f0cebfd1d2780e2677577e073eba` |
| `nanobot/utils/file_io.py` | `ce476257fd451c36cd3078641a65b106` |
| `nanobot/security/__init__.py` | `68b329da9893e34099c7d8ad5cb9c940` |
| `nanobot/security/workspace_policy.py` | `2186db8f566aba804f4234acc7e08567` |

为什么这几份也在里面：`apps/cli/service.py` 在文件头写死了
`from nanobot.{agent.skill_metadata, config.paths, security.workspace_policy} import …`。
不带上它们，字节级副本连 import 都过不去；带上它们，被带进来的全是**纯标准库**小文件
（41 / 74 / 128 行），没有额外依赖。

## 替身 / 桩（**唯一允许的偏离**，全部在此登记）

| 文件 | 类型 | 理由 |
|---|---|---|
| `nanobot/__init__.py` | 空壳 | 上游 3.5KB 版会读 tomllib、探测包版本、懒导出整个 nanobot API；我们只用 `apps` 子包 |
| `nanobot/agent/__init__.py` | 空壳 | 上游懒加载 role 类，与 skills 元数据无关 |
| `nanobot/config/__init__.py` | 空壳 | 上游懒导出 config 模型 |
| `nanobot/utils/__init__.py` | 空壳 | 上游是 `sys.modules` 别名表 |
| `nanobot/agent/plugins.py` | 替身 | 上游 537 行，拖进 `pydantic` + `nanobot.config.schema`；service.py 只用到 `set_agent_plugin_enabled` |
| `loguru.py` | 替身 | hub venv 不装 loguru；service.py 只用 `logger.info("{}", x)` 三处 |
| `httpx.py` | 名字转接 | hub venv 装的是 `httpx2`（同 API）；`sys.modules["httpx"] = httpx2` 零封装 |

## 运行时依赖（hub venv 必须有）

- `httpx2`：已有（2.12.0）
- `PyYAML`：**2026-11-25 新装 6.0.4**（MIT，wheel 离线装）。上游 `skill_metadata.parse_skill_metadata`
  用 `yaml.safe_load` 解析 SKILL.md frontmatter；缺它应用技能会退化成 fallback 模板（能跑但不完美）。
  `GET /api/apps/health` 的 `yaml=false` 就是这条缺失的显式信号。

## 升级上游的做法（**不要手改副本**）

1. 在临时 venv 里装目标版本：`pip install -i https://mirrors.aliyun.com/pypi/simple/ nanobot-ai==<新版本>`
2. 逐文件 `cp` 覆盖本目录（只覆盖上表 10 个 + 逐个 diff 替身是否需要变）。
3. `md5sum` 更新本表 + `tests/test_cliapps_l0.py:UPSTREAM_MD5`。
4. 跑 `pytest tests/test_cliapps_l0.py`；**红了就是有人手改过副本**，先查清再升。
5. 若新版本 `apps/cli/service.py` 多 import 了 `nanobot` 的别的模块，本节「替身」表要跟着扩，
   或者在 `src/cliapps.py` 里加适配——绝不去改副本本体。

## 行为差异声明（hub 适配层做的四件事）

1. `workspace` 指 `/fs/1000/ftp/技术文档`（env `HUB_APPS_WORKSPACE` 可覆盖）；
   上游默认是 `~/.nanobot/workspace`。原因：技能要落进 hub 技能白名单
   （`src/skill.py:SKILL_DIRS["techdocs"] = 技术文档/skills`）。
2. 状态/缓存放 `agenthub/data/cli-apps`（env `HUB_APPS_DATA`）。
3. 安装后补一条软链 `技术文档/skills/cli-app-x → ../plugins/cli-app-x/skills/cli-app-x`。
   上游把技能写在 `plugins/` 下，那个路径**不在任何 agent 的技能发现点里**。
4. `run()` 一律 `restrict_to_workspace=True`（上游默认 False）。
