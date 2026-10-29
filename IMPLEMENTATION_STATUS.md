# MetricPilot 实施状态

更新日期：2026-10-29（Asia/Shanghai）。

## 当前阶段

**阶段 0 已完成，并通过 Windows 本地验收。** 下一入口为蓝图 07 的阶段 1。阶段 1—5 尚未实现。

## 实际实现

- 根目录 Python 包配置、Python 3.12 版本声明、`.venv`、`uv.lock`；React + TypeScript + Vite + Ant Design 和 npm 锁文件。
- FastAPI `/api/v1/health`、OpenAPI、统一错误模型、服务端请求标识和错误脱敏。
- SQLite / SQLAlchemy、Alembic 初始迁移、应用设置表；连接开启 WAL、外键与 5 秒 busy timeout。
- 实际检测迁移版本、表可读性和 FTS5。未迁移为 HTTP 503；无模型密钥仍能启动。
- 配置完整性与远程验证分开；示例占位值视为未配置。健康检查不会调用远程模型。worker / analysis 明确标记未实现。
- 知识、分析、报告三个页面及报告详情路由占位；真实健康状态、加载、降级、连接失败与重试；分析提交保持禁用。
- 后端 OpenAPI 导出与前端类型生成；前端运行时检查健康响应。未引入示例业务结果或静态成功报告。
- `.env.example`、本机 `.env`、gitignore、合成 fixture、Windows / Linux 启动说明和阶段设计决策记录。

## 已执行检查

测试环境：Windows，Python 3.12.13，uv 0.12.3，Node.js 26.12.1，npm 11.20.0。主要实际安装版本：FastAPI 0.162.0、Pydantic 2.14.1、SQLAlchemy 2.1.6、Alembic 1.20.1、Vite 6.4.5、Vitest 3.2.7；完整版本以锁文件为准。

| 检查 | 实际结果 |
| --- | --- |
| `uv sync --locked` | 通过；31 个包解析/检查完成 |
| `uv run pytest` | 5 passed：迁移重复/回退/再升级、SQLite 策略、无密钥健康检查、未迁移诊断、配置与错误脱敏 |
| `uv run alembic upgrade head` | 通过，实际创建本地元数据库 |
| `uv run alembic check` | 通过，无待生成的结构差异 |
| `uv run ruff check .` | 通过；已排除缓存、虚拟环境与参考资料 |
| `uv run ruff format --check backend scripts` | 通过，9 个 Python 文件格式一致 |
| `uv run python -m compileall -q backend/src scripts` | 通过 |
| `uv run python scripts/export_openapi.py` | 通过；重复导出的文件哈希一致 |
| `npm.cmd ci --cache ../.cache/npm --no-audit --no-fund` | 通过，按锁文件安装 204 个包 |
| `npm.cmd run generate:api` | 通过，前端声明由后端契约生成 |
| `npm.cmd test` | 4 passed：无密钥健康响应、503、网络失败、错误代理响应 |
| `npm.cmd run build` | TypeScript 检查与 Vite 生产构建通过 |
| `npm.cmd run test:e2e` | Chromium：2 passed；真实后端、页面导航、刷新、数据源抽屉、禁用提交、失败与重试恢复、手机宽度检查 |
| 真实 Uvicorn 启动与 HTTP 请求 | 通过；8000 健康检查返回 `ok / ready / available / not_configured` |
| 人工查看截图 | 已检查桌面和手机分析页；截图保存在忽略的 `.cache/screenshots/` |
| 参考快照完整性 | 12/12 文件与附带 manifest 的 SHA-256 一致 |

验收中修复过 PowerShell 管道字符编码问题，并限制检查工具范围。误扫本地依赖缓存后，已删除受影响缓存，使用干净缓存重新安装全部依赖，以上后端结果来自恢复后的复验。

浏览器测试初次完成用例后，受限 Windows 环境未能自动清理 Vite 子进程；核实并结束本次测试的进程后命令正常退出，另在允许子进程管理的环境复验。未结束其他项目进程。

## 尚未执行的验证

- 真实 BTS 数据导入与 DuckDB 聚合：属于阶段 1，当前未实现导入器和执行器。fixture 为 8 行人工合成数据，不是官方数据，也不是完整导入字段契约。
- 真实 LLM / embedding 调用：尚未接入 adapter；未填写真实密钥，没有声称远程模型通过验证。
- Linux / macOS 安装、迁移、构建与运行：未在对应系统运行。README 提供对应命令，不声称跨平台已验收。
- 问答准确率、口径遵守率、成本与性能评估：属于后续阶段，尚无结果。

## 当前限制

- 没有数据源登记、SQL 执行、知识处理、Wiki、指标审核、Run / Job worker、SSE、模型分析或报告保存能力。
- RunContext、ToolResult、QueryArtifact、MetricDefinition、ReportDraft 与 SSE envelope 将按 02—06 在对应阶段实现，不制造另一套临时业务契约。
- 阶段 0 只有应用设置表，不预建整个平台的数据模型；当前目录已初始化 Git。
- 模型已填配置只表示格式完整，不表示服务可连通。APP_HOST / APP_PORT 尚由启动命令显式指定。
- 当前 Ant Design 主包构建约 675 kB（gzip 约 219 kB），Vite 给出大于 500 kB 的提示；阶段 0 尚未做路由拆包。
- 当前 Starlette 测试客户端对 httpx 给出弃用提示，5 项测试仍通过；依赖已锁定，后续升级时迁移测试客户端。

## 下一阶段入口

按蓝图 03 / 07 实现阶段 1：扩充合成 fixture、BTS CSV 导入 CLI、manifest 与质量报告、DataSnapshot 和业务视图、SQL AST 允许集、受限只读连接、可终止的查询子进程、成功 QueryArtifact，以及前端数据源抽屉的真实路径。优先验证分母、空值、负延误、SQL 防护、超时/取消与快照不可覆盖。


## 阶段 0 后续修正：CSS 导入类型声明

根据编辑器截图定位到 `main.tsx` 的 CSS 副作用导入缺少类型声明。启用严格副作用导入检查后在本地复现了错误。

新增 `frontend/src/vite-env.d.ts` 引用 `vite/client`，并在 `frontend/tsconfig.json` 开启 `noUncheckedSideEffectImports`，使构建也覆盖这一检查。

实际复验：`node frontend/node_modules/typescript/bin/tsc --project frontend/tsconfig.json --noEmit` 和前端 `npm.cmd run build` 均通过。原有大包提示仍存在；无需为纯类型配置变更重复运行后端或浏览器测试。

## 阶段 0 全面复验

按蓝图 07 的阶段 0 验收项再次检查，Windows 本机阶段 0 已完成：`uv sync --locked`、后端测试 5 项、Ruff、Alembic 结构检查、前端测试 4 项和生产构建均通过；后端 OpenAPI 和生成的前端类型与现有文件一致。

使用独立数据库和端口完成迁移、启动及无密钥健康检查；使用独立目录从锁文件离线执行 `npm ci` 成功。真实浏览器在现有后端与重启后的前端上完成导航、刷新、移动端、断线和恢复检查，2 项通过。`.env`、虚拟环境、数据和缓存由 `.gitignore` 排除，检查的敏感本地路径没有被 Git 跟踪。

复验发现 README 原有复制配置命令可能覆盖现有 `.env`，现已改为仅在文件不存在时复制，并更新了已初始化 Git 的说明。运行中的 Vite 会锁住 Windows 依赖文件：尝试在原目录 `npm ci` 未成功，随后已恢复依赖、重启本会话启动的 Vite 并重新通过前端测试、构建和浏览器检查；README 已补充重新安装前先停止前端服务的说明。

Linux / macOS、真实 BTS 数据及远程模型仍未验证，原因与上文相同，不把这些后续阶段工作算入阶段 0 验收。
