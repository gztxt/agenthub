# -*- coding: utf-8 -*-
"""技能检索触发词表（2026-10-22 批 A1）—— **检索词表**，不是技能内容副本。

【为什么需要这一层（实测，不是推断）】
2026-10-22 对生产 `/api/skill/relevant` 做了一次真实任务评测（8 中文 + 6 英文）：

| 语言 | rank1 正确 | `/auto` 归一化分达标 |
|---|---|---|
| 中文 | **1/8** | **1/8** |
| 英文 | **5/6** | **4/6** |

根因不是排序算法，而是**查询与语料不同语言**：中文查询被切成 CJK 二元组，而 276 个技能名
几乎全是英文（`systematic-debugging` / `test-driven-development`），绝大多数 description 也是英文
⇒ 中文查询**打不中 name 字段**（`matched_in` 恒空），只能靠少数中文描述里的泛词
（「测试」「检查」）碰运气。病态证据：`aidev-02-implement-audit` 在
「调试一个偶发失败的测试」与「给这个新功能写测试」两条**不同**查询上得到完全相同的
`4.7677` 分 —— 只有「测试」一个词在贡献。

【本表是什么 / 不是什么】
- 是：`技能名 → 中文触发词`，供 `skill_relevance` 当 BM25F 的第三字段（`W_TRIG=1.5`）。
- **不是**技能正文/描述的第二副本：不放 SKILL.md 的任何段落，只放「中文用户会怎么称呼这件事」。
  技能的权威内容仍然只有一处 —— 磁盘上的 `SKILL.md`。
- 优先级：`SKILL.md` frontmatter 里写了 `triggers:` 就用技能的**自述**；没写才用本表兜底。
  （改一个技能的触发词，首选改它自己的 frontmatter —— 这里只是「作者没写」时的公共桥。）

【维护纪律】
1. 只登记**确实存在**的技能名。名字写错不会报错，只会安静失效 —— 与判例
   「分词器的错误不抛异常，只安静地少召回」同族。`tests/test_skill_retrieval_cn_l0.py`
   会拿本表的键与真实语料对账，防止留下死条目。
2. 触发词不用泛词（测试/检查/分析/执行）。泛词会让所有含该字的技能同分，
   正是本批要消灭的病态；本表只放**能区分技能**的词。
3. 一条技能 3~8 个词为宜。超过 10 个等于把权重摊薄成噪声。
"""

from typing import Dict, List

#: 技能名 → 中文（及少量英文别名）触发词，空格分隔。
TRIGGERS: Dict[str, str] = {
    # ── 排障 / 调试 ──
    "systematic-debugging": "调试 排查 报障 根因 复现 报错 异常 崩溃 失败原因 测试失败 日志 502 偶发 间歇 定位",
    "diagnosing-bugs": "诊断 缺陷 定位问题 疑难 日志 报错分析",
    "diagnosing-superpowers": "会话复盘 为什么慢 为什么重复",
    "python-debugpy": "python 断点 调试器",
    "node-inspect-debugger": "node 调试 断点",
    "blocked-page-recovery": "页面打不开 恢复",
    "tailscale-offline-diagnose": "tailscale 离线 内网 链路 延迟 断连",

    # ── 测试 / 验收 ──
    "test-driven-development": "单元测试 红绿 测试先行 先写测试 补测试 TDD",
    "tdd": "单元测试 红绿 测试先行 TDD",
    "verification-before-completion": "验收 完成前 证据 自证 断言",
    "aidev-00-self-audit": "自我审计 交付前 反证 来源标注",
    "aidev-03-verify-maintain": "发布前 真机验证 文档漂移 防腐",

    # ── 编排 / 派发 ──
    "dispatching-parallel-agents": "并行 派发 子代理 并发 拆解 分工",
    "subagent-driven-development": "子代理 分派 任务执行",
    "swarm": "蜂群 团队 多agent 协作",
    "agent-dispatch": "派给谁 派发 子代理 分工 子任务",
    "kanban-orchestrator": "看板 编排 调度",
    "kanban-worker": "看板 执行",

    # ── 计划 / 方案 / 评审 ──
    "writing-plans": "写计划 实施计划 步骤拆解",
    "executing-plans": "执行计划 按计划施工",
    "execute-plan": "执行计划",
    "plan": "计划 方案",
    "code-review": "代码审查 评审 检查改动 是否符合规范 代码质量 标准 review",
    "review": "复审 复核",
    "sdlc-review": "评审 交付评审",
    "requesting-code-review": "请求评审",
    "receiving-code-review": "评审意见 反馈处理",
    "grill-me": "拷问 质疑 盘问 挑刺 挑毛病 找茬 挑战方案 方案评审",
    "grilling": "拷问 质疑",
    "grill-with-docs": "拷问 决策记录 ADR",
    "brainstorming": "头脑风暴 想法 发散",
    "prototype": "原型 试验 验证想法",
    "to-spec": "规格 spec 需求文档",
    "to-tickets": "拆票 工单 任务分解",
    "triage": "分诊 归类 优先级",
    "domain-modeling": "领域模型 术语表 上下文",
    "codebase-design": "模块设计 接口 深模块",
    "improve-codebase-architecture": "架构改进 重构机会",
    "aidev-01-define-design": "需求定义 方案设计 非目标 威胁建模",
    "aidev-02-implement-audit": "代码审计 对抗审计 边界值 故障注入 威胁建模",

    # ── 技能自身 ──
    "writing-skills": "写技能 技能编写 技能规范 新建技能 怎么建技能",
    "skill-creator": "创建技能 技能生成",
    "skill-installer": "安装技能",
    "using-superpowers": "技能怎么用 技能发现",
    "agent-ecosystem": "技能生态 插件市场 governance",
    "writing-for-agents": "写给agent 文档规范",

    # ── 本机记忆 / 文档 / 规则 ──
    "techdocs-search": "技术文档 检索文档 搜索笔记 查文档 本机文档",
    "unified-memory": "记忆 召回 上下文 历史结论 以前怎么定",
    "system-memory": "记忆 项目上下文",
    "system-rules": "规则 铁律 备份 军规 文件修改",
    "llm-wiki": "wiki 死链 巡检 卡片",
    "knowledge-base": "知识库",
    "repo-to-local-kb-fusion": "仓库 知识库 融合",
    "research": "调研 研究 资料收集",
    "scholarly-search": "论文 文献 学术 arXiv",
    "internet-search": "联网搜索 搜索策略",
    "web-search": "搜索 百度 必应",
    "summarize": "摘要 总结 压缩",
    "humanizer-zh": "去AI味 润色 自然语言",
    "handoff": "交接 接力 换会话",
    "resume-claude": "恢复会话 续接",
    "resume-codex": "恢复会话 续接",

    # ── 抓取 / 浏览器 ──
    "crawl4ai": "抓网页 爬取 正文 markdown 整页",
    "obscura": "无头浏览器 渲染 截图 抓取",
    "agent-browser": "浏览器自动化 签到 填表 点击",
    "web-ui-cdp-verify": "浏览器验证 CDP 截图",
    "wigolo": "联网搜索 抓取 本地缓存",
    "wigolo-search": "搜网页 联网搜索 多引擎",
    "wigolo-fetch": "抓页面 取网页 正文",
    "wigolo-crawl": "爬站 整站抓取 文档站",
    "wigolo-cache": "缓存 查缓存 已抓过",
    "wigolo-research": "深度调研 对比报告",
    "wigolo-agent": "自动采集 结构化抽取",

    # ── 文档 / 办公 ──
    "docx-cn": "word 文档 合同 排版",
    "docx": "word 文档",
    "xlsx": "excel 表格",
    "pdf": "pdf 文档",
    "pdf-extract": "pdf 提取 文字",
    "markitdown": "文档转markdown 格式转换",
    "tencent-docs": "腾讯文档 云文档 在线表格",
    "腾讯文档Markdown": "腾讯文档 markdown",
    "markdown-viewer": "markdown 预览",
    "meeting-action-items": "会议纪要 待办",
    "document-to-action-items": "文档 行动项",

    # ── 媒体 / 内容 ──
    "video-cut": "视频剪辑 剪辑 剪切",
    "video-frames": "视频抽帧",
    "youtube-content": "youtube 字幕 视频内容",
    "wechat-mp-cn": "公众号 阅读量 舆情",
    "xiaohongshu-cover-generator": "封面图 小红书 配图",
    "image-gen": "生成图片 绘图",
    "ascii-art": "字符画",
    "mermaid-diagram": "流程图 时序图 图表",
    "architecture-diagram": "架构图",

    # ── 开发 / 版本 ──
    "github": "github 仓库",
    "gh-issues": "issue 工单",
    "github-trending-cn": "github趋势 热门项目 开源动态",
    "resolving-merge-conflicts": "冲突 合并 解冲突",
    "using-git-worktrees": "worktree 隔离 并行施工 多人同时改 几个人一起改 打架 同时施工",
    "finishing-a-development-branch": "分支收尾 合并",
    "simplify-code": "简化代码 精简",
    "codebase-inspection": "代码库勘察",
    "apikey-image-gen": "图片生成 api key 绘图接口",
    "apis-lookup": "免费API 接口 黄页 选型",
    "token-cost-optimization": "token 成本 省钱 计费 太贵 费用 模型调用 账单",
    "agent-hub-cli-registration": "纳管 注册 agent",
    "typesafe-ai": "jev 判定 概率 分类",
    "content-risk-detector": "内容风险 合规 敏感",
}


def lookup(name: str) -> str:
    """按技能名取触发词（找不到回空串，**不猜**）。大小写不敏感。

    回空串而不是 `name` 本身：`name` 已经作为 BM25F 的 name 字段参与打分了，
    在这里再回一次等于把名字权重偷偷加成两倍。
    """
    if not name:
        return ""
    return TRIGGERS.get(str(name).strip(), "") or TRIGGERS.get(str(name).strip().lower(), "")


def merge(name: str, own: str = "") -> str:
    """技能自述的 `triggers:` 优先；没有才用本表。两者**不合并**。

    不合并的理由：合并会让「作者写了 3 个词」与「作者写 20 个词」在同一条技能上
    差距被本表抹平，作者改自己的触发词就失去意义。自述存在即视为作者已表态。
    """
    own = (own or "").strip()
    return own if own else lookup(name)


def known_names() -> List[str]:
    """本表登记的全部技能名（供 L0 闸门与真实语料对账，防死条目）。"""
    return sorted(TRIGGERS)
