# 08 DB-GPT 设计参考与阅读路线

本蓝图已附带 [12 份关键源码完整快照](references/dbgpt/README.md)。在当前工作区可直接通过下表的相对链接读取，不需要访问原仓库。快照仅供阅读，不是可独立运行的依赖包。

文件来自编写蓝图时的本地工作树，不代表 DB-GPT 全部历史版本或所有设计的原创性。原路径、文件哈希与工作树状态见快照清单。

## 1. 阅读顺序

下表链接均指向包内快照。原仓库路径记录在 [快照索引](references/dbgpt/README.md)，仅用于追溯。

| 顺序 | 包内源码快照 | 学习重点 | 在新项目中的简化 |
| --- | --- | --- | --- |
| 1 | [agent.py](references/dbgpt/01-agent.py.txt) | Agent、上下文、消息等抽象 | RunContext、ModelTurn、ToolResult |
| 2 | [base_agent.py](references/dbgpt/02-base-agent.py.txt) | 回复生成与行动执行的生命周期 | 一个 AgentRuntime + 显式服务依赖 |
| 3 | [react_agent.py](references/dbgpt/03-react-agent.py.txt) | ReAct 提示、动作空间、目标保留 | 原生工具调用循环；不做文本 ReAct 兼容 |
| 4 | [tool_calling_agent.py](references/dbgpt/04-tool-calling-agent.py.txt) | 原生工具调用适配与执行 | 单一模型适配层；保留消息配对和错误反馈 |
| 5 | [tool_action.py](references/dbgpt/05-tool-action.py.txt) | 工具输入、ActionOutput、执行结果与展示分离 | Tool Protocol + Registry + ToolResult |
| 6 | [manager.py](references/dbgpt/06-context-manager.py.txt) | 多层上下文预算与压缩 | 固定目标 + 完整工具组裁剪 + 产物摘要 |
| 7 | [base.py](references/dbgpt/07-middleware.py.txt) | 生命周期 hook | 四个 hook，限制仍在核心执行器落实 |
| 8 | [pipeline.py](references/dbgpt/08-wiki-pipeline.py.txt) | 提取、合并、分类、来源、增量生成 | 候选提取 → 确定性合并 → 草稿 → 审核发布 |
| 9 | [wiki_db.py](references/dbgpt/09-wiki-models.py.txt) | Wiki 页面、修订、乐观锁 | 不可变 WikiRevision + expected_version |
| 10 | [sql_read_only.py](references/dbgpt/10-sql-read-only.py.txt) | 完整语句检查的必要性 | AST 作用域+函数允许集+受限连接+子进程超时 |
| 11 | [report_evidence.py](references/dbgpt/11-report-evidence.py.txt) | 结果产物摘要、选择器、来源上下文 | QueryArtifact + ValueRef + 报告校验 |
| 12 | [wiki-browser.tsx](references/dbgpt/12-wiki-browser.tsx.txt) | Wiki 目录、阅读器、历史抽屉 | 三页应用中的知识管理页 |

## 2. 每段代码怎么读

不要从文件首行顺序读完整个仓库。带着具体问题：

- 一次模型回复在哪里进入工具执行？输入输出分别是什么？
- 工具报错如何返回模型？是否存在重试上限？
- 上下文裁剪后，用户目标和证据引用怎样保留？
- Wiki 生成依据如何绑定到原文？人工编辑是否会被覆盖？
- 报告里的数字是模型重写的，还是从执行产物取出的？

读完后在新项目写一页设计决策记录：借鉴了什么、为什么缩小、当前契约如何实现、有什么测试。避免逐行搬运自己无法解释的继承树。

## 3. 参考不是全盘继承

原项目的通用抽象承担许多数据源、模型、Agent 和产品能力。本项目只有单用户、单 Agent、一个分析引擎，不需要相同规模的组件系统。

当前 Wiki pipeline 的模块说明提到 WeKnora wiki_ingest，middleware 的说明提到参考 deepagents，工具调用 Agent 的说明提到 Hermes 相关思路。技术报告和简历应准确描述“参考 DB-GPT 及其相关实现”，不要把间接参考内容都宣称为自己的首创。

如直接复制源码，逐文件检查项目及第三方来源的许可证，保留需要的声明。若只参考结构并自行实现，记录设计来源仍有助于讲清楚个人贡献。

## 4. 推荐可讲清楚的三个设计决定

1. **为什么单 Agent**：问题主要是规则、查询与证据的一致性；一个受控循环足以验证，不先引入协作调度复杂度。
2. **为什么 Wiki 与指标定义分开**：Wiki 用于理解和导航，已审核指标用于固定计算语义，避免生成摘要悄悄改变分母。
3. **为什么报告冻结产物**：数据和规则会变化，报告需要保留当时的执行依据，才能重现分析和解释差异。

解释这些取舍、展示真实测试与失败案例，比仅罗列使用过多少框架更能体现理解。
