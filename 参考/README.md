# 项目参考资料

这个文件夹包含开始 MetricPilot 个人项目所需的全部设计参考资料，可以整体复制到其他位置。它是开发起点，不是已经实现的应用。

## 当前工作区与使用方式

工作区根目录包含 `参考/`，蓝图位于 `参考/docs/blueprint/`，DB-GPT 源码快照位于 `参考/docs/blueprint/references/dbgpt/`。目前只有参考资料，尚无应用代码、数据、依赖或实施状态文件。

从 [蓝图目录](docs/blueprint/README.md) 开始阅读，再按需阅读 01—08 和源码快照。工作区根目录的 `AGENTS.md` 适用于整个工作区。蓝图中的应用目录、命令和功能是拟议设计，实施时再创建和验证。项目开始实施后可在工作区根目录创建 `IMPLEMENTATION_STATUS.md`，记录实际状态。

## 包内内容

- [完整项目蓝图与文档目录](docs/blueprint/README.md)：范围、架构、Agent、数据、Wiki、报告、页面、API、实施与评估。
- [AI 开发约束](../AGENTS.md)：适用于整个工作区。
- [DB-GPT 参考阅读路线](docs/blueprint/08-dbgpt-reading-map.md)。
- [12 份源码完整快照](docs/blueprint/references/dbgpt/README.md)：附来源清单、文件哈希和许可证。

不包含真实 BTS 数据、模型密钥或安装好的依赖。实际开发时按照蓝图下载数据、配置服务并创建锁文件；不需要搬运原 DB-GPT 的虚拟环境和 node_modules。

