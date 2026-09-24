# 地震灾害科学协同服务

这是一个面向地震台网与应急指挥中心的模块化后端，集中管理地震事件、台站观测、震情计算、灾情协同、用户权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 震情档案：登记地震事件、台站观测和震源参数版本（草稿/已发布/已撤销的不可变快照），保留计算输入摘要。
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

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、事件与台站观测、烈度计算、参数版本生命周期与并发冲突、计算结果的输入版本引用，以及后台任务去重与领取和数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 震源参数版本管理

同一地震会因新增台站多次修订震级和震源深度。系统为每个事件维护一串**不可变参数快照**（`seismic_parameter_versions`），每次修订都记录参数全文、内容哈希、变更原因、操作者和父版本。版本状态分三种：

- `draft` 草稿：可继续编辑，不影响当前生效结论；每个事件至多一个未发布草稿。
- `published` 已发布：当前生效版本，每个事件至多一个，发布新版本会自动把旧发布版本置为 `revoked`。
- `revoked` 已撤销：被新结论取代或被人工撤回的历史快照，永久冻结、保留可查。

典型流程（创建事件即生成 v1 草稿）：

```bash
# 1. 基于最新版本创建修订草稿（base_version 做乐观并发控制）
curl -sS -X POST http://127.0.0.1:8432/api/seismic/events/1/parameter-versions \
  -H 'Content-Type: application/json' \
  -d '{"magnitude":6.1,"depth_km":15,"reason":"新增3个台站重新定位","base_version":1,"created_by":"analyst-b"}'

# 2. 发布前可用 If-Match: <content_hash> 防止草稿被他人并发改动
curl -sS -X POST http://127.0.0.1:8432/api/seismic/parameter-versions/2/publish \
  -H 'Content-Type: application/json' -H 'If-Match: "<content_hash>"' \
  -d '{"reason":"正式修订","operator":"analyst-b"}'

# 3. 撤回当前生效结论（原因必填）
curl -sS -X POST http://127.0.0.1:8432/api/seismic/parameter-versions/2/revoke \
  -H 'Content-Type: application/json' -d '{"reason":"误报撤回","operator":"chief"}'
```

查询与回放：

- `GET /api/seismic/events/{id}`：返回当前生效参数，附带 `latest_parameter_version`、`current_parameter_version` 和版本数量。
- `GET /api/seismic/events/{id}?parameter_version=N`：按第 N 版快照回放震级/深度，响应同时给出 `replayed_parameter_version` 与当前生效版本，新旧结论不会混淆。
- `GET /api/seismic/events/{id}/parameter-versions`（版本清单）、`.../parameter-versions/current`（当前生效）、`.../parameter-versions/{n}`（指定版本）。

并发安全：草稿创建携带 `base_version`，落后于服务端最新版本时返回 `409 conflict` 并给出双方版本号；草稿编辑、发布、撤销可用 `If-Match` 内容哈希做条件更新，冲突一律拒绝（409），不会静默覆盖新数据。

计算任务明确引用输入版本：`POST /api/seismic/events/{id}/computations` 可指定 `parameter_version_id`（缺省引用当前已发布版本），任务记录与计算结果（`parameter_version_info`、`input_digest`）都带版本号、状态和哈希；旧引用版本事后被撤销也不影响已完成结果，便于比较每次修订对烈度和告警阈值的影响。

旧的事件创建/读取接口保持不变；旧的 `PATCH /api/seismic/events/{id}` 仍可使用——直接改参数会自动生成并发布一个版本快照，仅改 `status` 不产生新版本，但存在未发布草稿时会拒绝覆盖。升级前的历史事件在服务启动（`ensure_schema`）时自动补建为 v1 已发布快照。

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
