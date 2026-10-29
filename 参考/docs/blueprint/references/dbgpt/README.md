# DB-GPT 源码阅读快照

本目录随蓝图一起复制，离开原仓库后仍能阅读。共 12 份完整文件快照，来自编写蓝图时的本地工作树，不保证等同某个上游发布版本。

文件使用 `.py.txt` / `.tsx.txt` 后缀，保留原始字节，仅作设计参考，不导入执行。它们依赖的其他 DB-GPT 模块没有一起打包；新项目应按蓝图重新实现接口，不尝试直接运行这些快照。

先读 [阅读路线](../../08-dbgpt-reading-map.md)，再按需要查看下面的文件。原路径用于追溯，实际读取包内快照即可。

| 快照 | 原仓库路径 |
| --- | --- |
| [Agent 抽象](01-agent.py.txt) | `packages/dbgpt-core/src/dbgpt/agent/core/agent.py` |
| [Agent 生命周期](02-base-agent.py.txt) | `packages/dbgpt-core/src/dbgpt/agent/core/base_agent.py` |
| [ReAct Agent](03-react-agent.py.txt) | `packages/dbgpt-core/src/dbgpt/agent/expand/react_agent.py` |
| [原生工具调用](04-tool-calling-agent.py.txt) | `packages/dbgpt-core/src/dbgpt/agent/expand/tool_calling_agent.py` |
| [工具执行](05-tool-action.py.txt) | `packages/dbgpt-core/src/dbgpt/agent/expand/actions/tool_action.py` |
| [上下文管理](06-context-manager.py.txt) | `packages/dbgpt-core/src/dbgpt/agent/core/context/manager.py` |
| [中间件](07-middleware.py.txt) | `packages/dbgpt-core/src/dbgpt/agent/middleware/base.py` |
| [Wiki 生成管线](08-wiki-pipeline.py.txt) | `packages/dbgpt-serve/src/dbgpt_serve/rag/service/wiki/pipeline.py` |
| [Wiki 数据模型](09-wiki-models.py.txt) | `packages/dbgpt-serve/src/dbgpt_serve/rag/models/wiki_db.py` |
| [SQL 只读检查](10-sql-read-only.py.txt) | `packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/tools/sql_read_only.py` |
| [报告证据](11-report-evidence.py.txt) | `packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/tools/report_evidence.py` |
| [Wiki 浏览器](12-wiki-browser.tsx.txt) | `web/components/knowledge/wiki/wiki-browser.tsx` |

## 来源与校验

[manifest.json](manifest.json) 保存每份文件的原路径、SHA-256、大小和工作树状态，以及采集时的 Git HEAD。HEAD 仅提供版本背景，不表示当前工作树内容完全等于该提交。复制后可重新计算哈希验证完整性。

附上原仓库根目录的 [MIT 许可证原文](LICENSE.txt)。文件内的来源、注释和声明保持不变；涉及第三方参考时继续保留并核实相关来源。参考代码中的 `Thought` / `thought` 等字段用于理解原实现；新项目遵守蓝图要求，不输出内部思维链。

查阅这些文件不需要原 DB-GPT 路径、网络或其 Python 环境。只有要追踪这些文件之外的调用关系时，才需要另外打开完整原仓库。
