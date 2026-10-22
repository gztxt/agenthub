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

import re
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



# ── 派生词表：把英文技能名按词元映射成中文触发词（2026-10-22 PT-20261022-14）──
#
# 【为什么用"派生"而不是手写 170 行】276 个技能里，策展表只覆盖 106 个；剩下 170 个
# 没有中文触发词 ⇒ 中文查询打不中它们（holdout 实测 S 类短词 rank1 仅 33%，
# `证书`/`邮件` 直接零召回）。手写 170 条固然"更准"，但**改一个技能的英文名就漂**，
# 而派生表只要词元还在就自动跟着走；且派生规则可被单测验算（`derive()`）。
#
# 【派生与策展的关系】自述(frontmatter) > 策展(TRIGGERS) > **派生(本表)**。
# 派生只是兜底：一旦某技能进了策展表或自己写了 triggers，派生自动让位。
#
# 【派生给几个词】按名字切词后逐个查表，去重保序，最多 6 个。
# 查不到的英文词元**不硬造**（宁可少一个词，也不要把 `1password` 翻成奇怪的中文）。
_EN_ZH = {
    # 文档 / 办公
    "doc": "文档", "docs": "文档", "docx": "word 文档", "pdf": "pdf 文档",
    "xlsx": "excel 表格", "excel": "excel 表格", "spreadsheet": "表格",
    "ppt": "幻灯片", "slide": "幻灯片", "slides": "幻灯片", "powerpoint": "幻灯片",
    "markdown": "markdown", "note": "笔记", "notes": "笔记", "notion": "笔记",
    "obsidian": "笔记 知识库", "box": "网盘 文件", "airtable": "表格 数据库",
    "markitdown": "格式转换", "ocr": "文字识别",
    # 邮件 / 沟通
    "mail": "邮件", "email": "邮件", "imap": "邮件", "himalaya": "邮件",
    "imessage": "短信", "message": "消息", "calendar": "日历", "meeting": "会议",
    "feishu": "飞书", "google": "谷歌", "workspace": "办公套件",
    # 证书 / 安全 / 网络
    "cert": "证书", "certbot": "证书", "ssl": "证书", "tls": "证书",
    "acme": "证书", "security": "安全", "scan": "扫描", "auth": "鉴权",
    "login": "登录", "token": "令牌", "password": "密码", "1password": "密码 保险库",
    "proxy": "代理", "vpn": "网络", "dns": "域名", "tailscale": "内网 链路",
    "webhook": "回调", "subscription": "订阅",
    # 媒体
    "image": "图片", "img": "图片", "video": "视频", "audio": "音频",
    "music": "音乐", "song": "音乐", "spotify": "音乐", "podcast": "播客",
    "comic": "漫画", "infographic": "信息图", "art": "绘图", "pixel": "像素画",
    "remotion": "视频合成", "manim": "动画", "gif": "动图",
    # 数据 / 检索
    "search": "搜索", "fetch": "抓取", "crawl": "爬取", "scrape": "抓取",
    "cache": "缓存", "index": "索引", "knowledge": "知识库", "kb": "知识库",
    "memory": "记忆", "recall": "召回", "research": "调研", "survey": "调研",
    "arxiv": "论文", "paper": "论文", "huggingface": "模型库", "hub": "仓库",
    "dataset": "数据集", "data": "数据", "finance": "行情", "stock": "股票",
    "trading": "交易", "polymarket": "预测市场", "weather": "天气", "map": "地图",
    # 开发
    "code": "代码", "coding": "代码", "git": "版本控制", "github": "仓库",
    "repo": "仓库", "issue": "工单", "issues": "工单", "pr": "合并请求",
    "review": "评审", "test": "测试", "debug": "调试", "bug": "缺陷",
    "lint": "静态检查", "refactor": "重构", "build": "构建", "deploy": "部署",
    "docker": "容器", "k8s": "集群", "kubernetes": "集群", "ssh": "远程登录",
    "terminal": "终端", "shell": "命令行", "cli": "命令行", "browser": "浏览器",
    "agent": "代理", "skill": "技能", "skills": "技能", "plugin": "插件",
    "mcp": "工具协议", "api": "接口", "sdk": "工具包", "llm": "模型",
    "prompt": "提示词", "model": "模型", "models": "模型", "token": "令牌",
    "cost": "成本", "billing": "计费", "pricing": "价格",
    # 流程 / 协作
    "plan": "计划", "planning": "计划", "spec": "规格", "ticket": "工单",
    "tickets": "工单", "triage": "分诊", "kanban": "看板", "swarm": "蜂群",
    "handoff": "交接", "resume": "续接", "session": "会话", "wizard": "向导",
    "setup": "配置", "install": "安装", "config": "配置", "backup": "备份",
    "restore": "恢复", "migrate": "迁移", "monitor": "监控", "watch": "监视",
    "alert": "告警", "log": "日志", "logs": "日志", "metric": "指标",
    # 内容 / 其他
    "blog": "博客", "news": "新闻", "trending": "趋势", "hot": "热点",
    "wechat": "微信", "twitter": "推特", "youtube": "视频", "douyin": "抖音",
    "xiaohongshu": "小红书", "summarize": "摘要", "summary": "摘要",
    "translate": "翻译", "humanizer": "润色", "slides": "幻灯片",
    "comfyui": "绘图流程", "jupyter": "笔记本", "notebook": "笔记本",
    "prototype": "原型", "design": "设计", "ui": "界面", "css": "样式",
    "diagram": "图表", "mermaid": "图表", "excalidraw": "白板",
    "jira": "工单", "linear": "工单", "slack": "消息", "discord": "社群",
    "maps": "地图", "map": "地图", "hardware": "硬件", "blogwatcher": "博客 订阅 监控",
    "last30days": "近30天 舆情", "recap": "复盘 回顾", "trading": "交易",
    "tradingagents": "交易代理", "risk": "风险", "detector": "检测",
    "guidelines": "准则 规范", "songsee": "音乐 识别", "aihot": "热点",
    # ── 2026-10-22 第二轮补齐（覆盖 106→200 后剩下的口语词）──
    "reminder": "提醒 待办", "reminders": "提醒 待办", "todo": "待办",
    "blog": "博客", "watcher": "监控 订阅", "watch": "监视", "top": "榜单 热门",
    "computer": "电脑 自动化", "use": "使用", "dogfood": "自己试用 内测",
    "productivity": "效率", "ideation": "想法 构思", "idea": "想法",
    "implement": "实现", "implementation": "实现", "spike": "技术验证 探针",
    "teach": "教学 讲解", "wizard": "向导 逐步", "questionnaire": "问卷",
    "wayfinder": "路线图 决策清单", "pretext": "预文本", "grounded": "有据可依",
    "citation": "引用 出处", "citations": "引用 出处", "hallmark": "设计规范 界面评审",
    "impeccable": "界面打磨", "sketch": "草图", "designs": "设计",
    "popular": "流行 常见", "web": "网页", "speech": "语音", "text": "文字",
    "record": "录制", "replay": "回放", "tmux": "终端复用", "xdg": "桌面",
    "tmux": "终端复用", "open": "打开", "hue": "灯光 智能家居",
    "home": "家居", "find": "查找", "findmy": "查找 定位",
    "diff": "对比 差异", "extract": "抽取", "similar": "相似",
    "weights": "权重", "biases": "偏差", "harness": "评测框架",
    "evaluating": "评估", "llms": "模型", "serving": "部署 推理",
    "vllm": "推理服务", "litellm": "模型网关", "llama": "本地模型",
    "cpp": "本地推理", "dspy": "提示优化", "kdocs": "文档 金山",
    "yuanbao": "元宝", "xurl": "请求 重放", "supermemory": "长期记忆",
    "heartmula": "日志 解析", "headroom": "上下文 预算",
    "hyperframes": "分帧 视频", "obliteratus": "对抗 提示注入",
    "godmode": "越狱 边界测试", "imagegen": "生成图片",
    "mergereconciler": "合并 冲突", "merge": "合并", "reconciler": "对账 冲突",
    "minecraft": "我的世界 服务器", "modpack": "模组包",
    "p5js": "创意编程 画布", "pokemon": "游戏", "player": "游戏",
    "macos": "mac 桌面", "desktop": "桌面", "dom": "页面结构",
    "inspecting": "检查", "console": "控制台", "debugging": "调试",
    "hermes": "hermes", "tui": "终端界面", "commands": "命令",
    "opencode": "opencode", "controller": "控制器", "assistant": "助手",
    "developer": "开发", "communication": "沟通", "project": "项目",
    "analysis": "分析", "collab": "协作", "group": "群组", "default": "默认",
    "passive": "被动", "aone": "aone", "qoderwake": "qoder", "vibe": "氛围编程",
    "architecture": "架构", "interaction": "交互", "harness": "评测",
}

#: 派生结果最多几个词（与策展表同口径：3~8 个为宜）
_DERIVE_MAX = 6
#: 派生时不作为触发词的通用词元（出现也不产生中文词，避免"技能=skill"这种空转）
_DERIVE_SKIP = frozenset({"the", "a", "an", "and", "or", "to", "of", "for", "with",
                          "by", "in", "on", "my", "your", "app", "tool", "tools",
                          "helper", "utils", "demo", "example"})


def derive(name: str) -> str:
    """把英文技能名派生为中文触发词（**兜底用**，见 `_EN_ZH` 注释）。

    规则：按 `-`/`_`/空格切词元 → 小写 → 查 `_EN_ZH` → 去重保序 → 最多 `_DERIVE_MAX` 个。
    查不到的词元**丢弃**（不硬造译文）：宁可少一个词，也不要造出没人会输入的词。
    """
    if not name:
        return ""
    toks = [t for t in re.split(r"[-_\s/.]+", str(name).lower()) if t]
    out: List[str] = []
    for t in toks:
        if t in _DERIVE_SKIP:
            continue
        zh = _EN_ZH.get(t)
        if not zh:
            continue
        for w in zh.split():
            if w and w not in out:
                out.append(w)
        if len(out) >= _DERIVE_MAX:
            break
    return " ".join(out[:_DERIVE_MAX])


def lookup(name: str) -> str:
    """按技能名取触发词（找不到回空串，**不猜**）。大小写不敏感。

    回空串而不是 `name` 本身：`name` 已经作为 BM25F 的 name 字段参与打分了，
    在这里再回一次等于把名字权重偷偷加成两倍。
    """
    if not name:
        return ""
    return TRIGGERS.get(str(name).strip(), "") or TRIGGERS.get(str(name).strip().lower(), "")


def merge(name: str, own: str = "") -> str:
    """触发词优先级：**自述 > 策展 > 派生**（2026-10-22 加第三级）。

    · 自述：SKILL.md frontmatter 的 `triggers:`（作者表态，最高）；
    · 策展：`TRIGGERS`（本文件人工维护，覆盖高频技能）；
    · 派生：`derive()` 按英文名词元查 `_EN_ZH`（兜底，覆盖剩下 170 个）；
    三级**不合并**：合并会让「作者写 3 个词」与「策展写 8 个词」在同一条技能上摊平，
    作者改自己的触发词就失去意义。
    """
    own = (own or "").strip()
    if own:
        return own
    cur = lookup(name)
    return cur if cur else derive(name)


def known_names() -> List[str]:
    """**策展表**登记的全部技能名（供 L0 闸门与真实语料对账，防死条目）。"""
    return sorted(TRIGGERS)


def source_of(name: str, own: str = "") -> str:
    """这条技能的触发词来自哪一级：`own` / `curated` / `derived` / `none`。

    为什么要暴露：出问题时要能回答「这个词是谁加的」——派生词错了就改 `_EN_ZH`，
    策展词错了就改 `TRIGGERS`，自述词错了去改 SKILL.md。三级混在一起时无法定位。
    """
    if (own or "").strip():
        return "own"
    if lookup(name):
        return "curated"
    if derive(name):
        return "derived"
    return "none"
