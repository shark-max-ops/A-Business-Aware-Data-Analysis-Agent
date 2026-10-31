# 阶段 0：工程、配置、数据库与页面

更新日期：2026-10-31（Asia/Shanghai）。[返回导读目录](README.md)。

## 1. 阶段目标

阶段 0 先让项目成为一个可以安装、启动、连接和检查状态的应用。它建立后端 API、元数据库、前端页面框架，以及前后端共同遵守的数据格式。

这一阶段解决的是“系统怎样可靠地运行起来”。真实数据导入属于阶段 1，知识管理属于阶段 2，模型分析属于阶段 3；完整报告尚未实施。

## 2. 执行流程

以用户打开网页为例：

```text
启动后端
  → 读取本地配置
  → 建立 SQLite 连接并检查迁移状态
  → 注册 API 和统一错误处理

打开前端
  → 请求 /api/v1/health
  → 校验响应格式
  → 展示正常、降级、未配置或连接失败状态
  → 进入知识、分析或报告页面
```

这里的健康检查只读取本地依赖和配置状态，不向模型发送付费请求。“模型配置已填写”表示配置完整，不等于本次健康检查已经验证远程模型可用。

## 3. 代码文件讲解

### 3.1 `settings.py`：集中管理配置

[settings.py](../../backend/src/metric_pilot/settings.py) 的 `Settings` 读取环境变量和本机 `.env`，校验端口、预算等配置，并用 `SecretStr` 包装密钥字段。

`integration_configured()` 检查地址、模型名称和密钥是否填写完整，并排除示例占位值。它不会调用远程服务。仓库里的 [.env.example](../../.env.example) 只提供配置项目和占位内容。

今天这个文件还包含分析轮数、SQL 超时等后续配置；阶段 0 建立的是统一配置入口，具体分析能力是之后接入的。

### 3.2 `database.py` 与迁移：保存应用自己的状态

[database.py](../../backend/src/metric_pilot/database.py) 中的 `create_metadata_engine()` 创建 SQLite 引擎，为连接配置外键、WAL 和锁等待。SQLite 保存的是应用元数据，例如设置，以及后续增加的文档修订、运行和事件；航班分析数据在阶段 1 使用 DuckDB 保存。

数据库结构通过 Alembic 显式迁移，不在每次启动时悄悄建表。

- [0001_foundation.py](../../backend/migrations/versions/0001_foundation.py)：阶段 0 的基础设置表。
- [migrations/env.py](../../backend/migrations/env.py)：与服务共用数据库配置，加载需要比较的模型结构。
- `inspect_database()`：检查迁移版本、需要的表和 FTS5 能力，返回是否需要迁移。

当前自检已经随阶段 1—3 扩展，检查的是完整当前结构；不能因为文件起源于阶段 0，就以为它今天只检查基础表。

### 3.3 `main.py`：组合后端应用

[main.py](../../backend/src/metric_pilot/main.py) 的 `create_app()` 创建 FastAPI 应用，注册路由、健康接口和错误处理。`lifespan()` 管理服务启动与退出时的资源。

`identify_request()` 给请求生成服务端追踪 ID。`error_response()` 与异常处理器把错误统一为包含错误码、说明、安全细节和请求 ID 的响应，避免直接暴露底层异常里的路径或凭据。

当前 `lifespan()` 还会启动知识与分析 worker。这是阶段 2、3 对同一个应用入口的扩展，不是阶段 0 当时已有的功能。

### 3.4 `contracts.py` 与契约生成：让前后端说同一种格式

[contracts.py](../../backend/src/metric_pilot/contracts.py) 定义 `StrictDTO`、`ErrorResponse` 和 `HealthResponse`。严格 DTO 拒绝契约外字段，避免调用双方对接口产生不同理解。

类型同步经过这条路径：

```text
后端 Pydantic 契约
  → scripts/export_openapi.py
  → frontend/openapi.json
  → npm run generate:api
  → frontend/src/api/schema.d.ts
```

[export_openapi.py](../../scripts/export_openapi.py) 使用空模型配置导出结构，不需要读取真实密钥或启动数据库服务。生成的 [schema.d.ts](../../frontend/src/api/schema.d.ts) 给前端提供接口类型；改后端字段后，需要同步生成结果。

### 3.5 前端：真实地显示服务状态

[main.tsx](../../frontend/src/main.tsx) 挂载 React 应用；[App.tsx](../../frontend/src/App.tsx) 提供主导航、页面路由和服务状态。

[api/client.ts](../../frontend/src/api/client.ts) 的 `loadHealth()` 请求健康接口，`isHealth()` 在运行时检查返回内容。这样即使代理返回了 HTML 错误页，也不会因为 TypeScript 声明写着“这是健康结果”就显示服务正常。

阶段 0 建立了三个页面框架。当前分析页和知识页已经由后续阶段填入真实能力，报告页仍明确显示尚未开放。

## 4. 具体例子

假设用户没有配置模型密钥，但已经完成数据库迁移：

1. 后端仍然可以启动。
2. 健康接口返回本地服务实际状态，模型配置显示为 `not_configured`。
3. 前端可以打开并解释未配置状态，不显示假成功。
4. 需要模型的操作不能据此被当作已经可用。

如果数据库没有迁移，健康检查会返回需要处理的状态，页面提示先完成迁移。若请求失败，用户会看到连接错误与重试入口。

## 5. 验证与限制

[test_foundation.py](../../backend/tests/test_foundation.py) 验证迁移、SQLite 策略、无密钥启动、健康状态、统一错误格式与脱敏；[client.test.ts](../../frontend/src/api/client.test.ts) 验证正常、降级、离线和错误响应的处理。这些文件后来也随阶段推进同步更新。

实际验收记录见 [IMPLEMENTATION_STATUS.md](../../IMPLEMENTATION_STATUS.md)。本文整理时没有重新运行这些测试，也没有发起模型请求。

阶段 0 提供工程基础，不代表具备自然语言问数、真实数据查询、知识生成或报告能力。当前部署边界仍是单用户本地应用，不能把基础错误处理和回环监听当作企业级鉴权。
