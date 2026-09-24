# 地震灾害科学协同服务

这是一个面向地震台网与应急指挥中心的模块化后端，集中管理地震事件、台站观测、震情计算、灾情协同、用户权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 震情档案：登记地震事件、震源参数和台站观测，保留计算输入摘要。
- 科学计算：提供震级、距离和烈度的确定性计算，以及可恢复后台任务。
- 灾情协同：管理灾情报告、公告、部门责任和跨部门办理状态。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、事件与台站观测、烈度计算、后台任务去重与领取，以及数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         灾情、事件、公告、部门和信访业务接口
  seismic/         地震事件、台站观测和科学计算服务
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。

### 震源参数版本管理

同一地震会随新增台站反复修订震级与震源深度。为避免值班人员把旧结论当成当前结果，震源参数（震级、震级类型、震源深度）以不可变快照形式保存在 `seismic_event_versions` 中：

- 建档即生成 v1 **已发布（published）**版本；后续修订先创建**草稿（draft）**，显式发布后才生效；发布时旧生效版本自动标记为**已撤销（revoked）**，撤销当前版本会回退到上一版本。
- 每个快照记录变更原因（`change_reason`）、操作者（`actor`）、基线版本（`base_version`）和时间戳，快照内容永不修改。
- 乐观并发控制：创建草稿或 PATCH 修改参数时携带 `base_version`，基线落后于最新版本返回 `409 conflict`（含 `latest_version`），不会静默覆盖；不带该字段的旧客户端保持兼容。
- 计算任务记录引用的 `param_version` 与当时的参数状态（`param_status`），计算始终读取入队时的快照值；入队支持 `param_version` 参数按历史版本回放，相同输入仍按摘要去重。
- `GET /api/seismic/events/{id}` 返回 `current_param_version`（当前生效版本）与 `latest_param_version`，加 `?at_version=N` 可回放历史参数并同时标注当前生效版本，`replay=true` 提醒该结果不是当前结论。
- 版本列表与单版本查询：`GET /api/seismic/events/{id}/versions`、`GET /api/seismic/events/{id}/versions/{version}`；发布与撤销：`POST .../versions/{version}/publish|revoke`。
