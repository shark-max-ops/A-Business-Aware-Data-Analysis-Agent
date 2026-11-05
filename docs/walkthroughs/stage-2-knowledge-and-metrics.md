# 阶段 2：文档版本、检索、Wiki 与指标审核

更新日期：2026-10-31（Asia/Shanghai）。[返回导读目录](README.md)。

## 1. 阶段目标

阶段 2 为数据分析建立业务依据。数据库里的字段能告诉我们“有什么数据”，但不能独自决定“延误率的分母是什么、取消和备降算不算、采用哪个版本的规则”。

这一阶段把原文、Wiki 和结构化指标分开管理：原文保存来源与字段含义，Wiki 帮助阅读，已审核指标固定计算口径。生成一段说明不会自动让它成为正式规则。

## 2. 执行流程

```text
资料上传或种子材料导入
  → 保存不可变文档修订
  → 后台分块，保存原文位置
  → 建立关键词索引
  → 显式构建 embedding 后，可做混合检索

整理知识
  → 选择固定来源生成 Wiki 草稿
  → 校验引用并供人工编辑
  → 人工发布，保留历史与回滚草稿

定义指标
  → 保存结构化草稿
  → 独立人工审核，创建已审核修订
  → 在固定知识快照与适用日期中解析
```

“固定知识快照”是一份修订清单。资料或指标后来更新，不会把已经固定的清单改成新版。

## 3. 代码文件讲解

### 3.1 `seed.py` 与 `documents.py`：保存能回查的原文

[knowledge/seed.py](../../backend/src/metric_pilot/knowledge/seed.py) 的 `import_seed()` 读取 [材料清单](../../knowledge_seed/manifest.json)，核对文件哈希，导入实际准备材料，并把指标草稿绑定到真实文档修订。重复导入不覆盖已经编辑的指标，也不会自动审核。

[documents.py](../../backend/src/metric_pilot/knowledge/documents.py) 的 `DocumentService` 处理文档写入与索引；`split_document()` 根据标题和段落拆分正文。每个片段保留所属修订、字符范围、行号、章节和已有来源定位，不只保存模型摘要。

新建或更新文档都会保存不可变修订。编辑请求携带 `expected_version`，旧版本编辑不能静默覆盖后来保存的内容。当前上传入口接收 UTF-8 Markdown/TXT；准备材料里的 PDF/HTML 摘录不是通用在线解析能力。

### 3.2 `store.py` 与 `models.py`：保存版本关系和固定范围

[knowledge/models.py](../../backend/src/metric_pilot/knowledge/models.py) 定义文档、片段、向量、Wiki、指标、知识快照及任务的表结构；[0003_knowledge.py](../../backend/migrations/versions/0003_knowledge.py) 创建这些表和 FTS5 索引。

[knowledge/store.py](../../backend/src/metric_pilot/knowledge/store.py) 的 `KnowledgeStore` 管理写事务与引用检查。`snapshot()` 固定当前文档、已发布 Wiki、活动已审核指标，以及它们依赖的旧文档修订。

因此，规则引用的是旧原文时，旧原文也会进入可回查范围；不会因为当前文档更新，就断开规则与来源的关系。

### 3.3 `retrieval.py` 与 `relevance.py`：从问题找到实际原文

[retrieval.py](../../backend/src/metric_pilot/knowledge/retrieval.py) 的 `Retriever.search()` 在固定快照中检索，并按数据适用日期过滤明确不适用的材料。

| 路径 | 做什么 |
| --- | --- |
| 关键词 | 使用 SQLite FTS5、中文索引及中英术语扩展找候选 |
| 向量 | 使用实际 embedding 与已持久化向量比较相似度 |
| 混合 | 用 RRF 融合两路名次，不直接相加不同量纲的分数 |

`index_embeddings()` 记录向量模型/端点键、文本哈希和维度，避免把不同模型的向量混用。

[relevance.py](../../backend/src/metric_pilot/knowledge/relevance.py) 负责候选是否有正文内容、相关性调整和 `focused_excerpt()` 原文摘录。提前到达等字段解释问题优先直接定义；这些术语处理用于找资料，不是把自然语言问题映射成预置 SQL。

知识页默认展示前五个主题和短摘录，长文按需展开。返回摘录是对应原文的连续子串，不用模型改写摘要替代来源。已发布 Wiki 另作关键词导航，不能说它的正文也已独立建立向量索引。

### 3.4 `wiki.py` 与 `provider.py`：生成有来源的草稿

[wiki.py](../../backend/src/metric_pilot/knowledge/wiki.py) 的 `WikiService` 管理草稿保存、生成、发布和回滚。`queue_generation()` 固定输入来源与编辑基线；`generate()` 请求模型生成候选，再校验引用。

[knowledge/provider.py](../../backend/src/metric_pilot/knowledge/provider.py) 的 `KnowledgeProvider` 提供 embedding 和 Wiki 文本生成接口。模型只返回候选内容和引用，没有直接发布或审核的权限；适配器只记录最终候选与白名单数值用量，不保存内部思维正文。

引用检查要求片段属于本次输入、哈希匹配、摘录真实包含在原文中，正文也要有引用标记。这个检查保证引用可查，不能自动证明解释语义正确，所以仍需人工核对。

保存与编辑追加修订；发布修改发布指针；回滚创建新草稿，不删除历史，也不自动替换发布版本。

### 3.5 `metrics.py`：让指标公式经过明确审核

[metrics.py](../../backend/src/metric_pilot/knowledge/metrics.py) 的 `MetricService` 负责三个不同动作：

| 方法 | 职责 |
| --- | --- |
| `save()` | 保存结构化指标草稿 |
| `approve()` | 从当前草稿创建新的已审核修订 |
| `resolve()` | 在固定快照里找唯一已审核、日期适用的定义 |

[knowledge/contracts.py](../../backend/src/metric_pilot/knowledge/contracts.py) 的 `MetricDefinition` 包含单位、分子/分母、统计范围、所需列、缺失处理、生效期和来源。

没有适用候选就返回缺失，有多个冲突候选就返回冲突，不擅自拼接。首批八项指标属于 `project_policy`，自定义分母不能标成官方指标。公式字符串不执行，当前也没有实现 `template_key` 对应的 SQL 构建器。

### 3.6 `jobs.py`：把慢操作做成真实后台任务

[jobs.py](../../backend/src/metric_pilot/knowledge/jobs.py) 的 `KnowledgeWorker.step()` 在短事务里领取任务，事务结束后再执行索引或网络请求，最后保存实际成功/失败与尝试历史。

任务在数据库中持久化。重启时，原来运行中的知识任务标为中断失败，用户可显式重试；不能把重启描述成恢复了模型内部状态。

### 3.7 知识页面：让资料和指标方便阅读

[KnowledgePage.tsx](../../frontend/src/components/KnowledgePage.tsx) 连接 [knowledge/api.py](../../backend/src/metric_pilot/knowledge/api.py)，将参考资料和计算规则统一放入“文档管理”，另保留 Wiki；支持直接撰写或上传文档、原文阅读和规则审核。固定版本检索和后台任务服务仍供调查使用，不再单列常驻界面。

三个相关展示组件分别解决不同问题：

- [ReadableMarkdown.tsx](../../frontend/src/components/ReadableMarkdown.tsx)：渲染表格与引用，限制原始 HTML、图片和链接行为。
- [MetricSummary.tsx](../../frontend/src/components/MetricSummary.tsx)：先展示分子、分母、范围、缺失处理和来源，编辑细节按需打开。
- [KnowledgeGraph.tsx](../../frontend/src/components/KnowledgeGraph.tsx)：展示实际资料、引用主题、Wiki 与指标的关系；后端关系来自 [graph.py](../../backend/src/metric_pilot/knowledge/graph.py)，连线不代表因果。

关系图采用圆形节点，悬停或键盘聚焦时放大；点击后在图下展示完整标题、相关文档及原文入口。圆内长标题省略，详情保留全文；支持缩放、滚动与减少动态效果偏好。

## 4. 具体例子

调查中的知识检索工具收到“航班早到了，为什么还有负的延误分钟？”时，会把中文说法与 ArrDelay 等术语联系起来，返回官方字段说明及逐字摘录。用户也可在文档管理直接打开原文。

这里需要解释的是两个不同字段：ArrDelay 保留实际与计划到达时间的偏差，提前到达为负；ArrDelayMinutes 把提前到达记为 0。展示来源原文与定位，用户可以继续核对。

这次操作是资料检索。只有到阶段 3，模型才会按问题需要进一步选工具、看结构和查数据库。Wiki 草稿和未审核的指标仍不能因此自动成为正式计算口径。

## 5. 验证与限制

[test_knowledge.py](../../backend/tests/test_knowledge.py) 覆盖修订、引用、向量、Wiki、审核和适用期；[test_knowledge_reading.py](../../backend/tests/test_knowledge_reading.py) 覆盖改写检索、逐字摘录及实际引用关系等阅读反馈。

[verify_knowledge.py](../../scripts/verify_knowledge.py) 和 [verify_knowledge_reading.py](../../scripts/verify_knowledge_reading.py) 提供真实材料与显式模型验收路径；是否请求真实模型取决于命令参数，不能把离线检查写成模型检查通过。

既有记录包含真实 embedding、Wiki 草稿生成与引用验收；阶段 3 更新后的使用说明也在用户授权后完成向量更新。实际八项指标仍待人工审核，Wiki 仍未发布。测试里的审核与发布发生在隔离数据库。

本文创建时只核对代码和已有记录，没有重新生成 Wiki、构建向量或修改实际资料。完整检查结果见 [实施记录](../../IMPLEMENTATION_STATUS.md)。资料引用真实不自动证明所有解释正确，也没有据此测得整体问答准确率。
