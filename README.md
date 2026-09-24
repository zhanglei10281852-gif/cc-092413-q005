# 地震灾害科学协同服务

这是一个面向地震台网与应急指挥中心的模块化后端，集中管理地震事件、台站观测、震情计算、灾情协同、用户权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 震情档案：登记地震事件、震源参数和台站观测，保留计算输入摘要。
- 科学计算：提供震级、距离和烈度的确定性计算，以及可恢复后台任务。
- 暴露清单：导入带坐标和分区编码的建筑与关键设施记录，按来源批次幂等去重，校验坐标范围，结合烈度网格阈值生成分区高风险汇总并可追溯到原始记录。
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

## 暴露清单流程

暴露清单把建筑（`building`）和关键设施（`facility`）记录与烈度网格结合，输出每个行政分区的高风险统计。流程可重复执行：导入按来源批次幂等，评估按“确认数据 + 网格 + 阈值”内容寻址。

导入规则：

- 每条记录须含 `record_type`、`name`、`division_code`、`latitude`、`longitude`，可选 `record_key`、`population`、`critical_level`（`normal`/`key`/`critical`）。
- 坐标按有效范围校验（默认中国陆域纬度 3~54、经度 73~136，可用请求中的 `bounds` 覆盖）。
- 同一批次（`batch_id`）再次导入直接返回首次结果；批次内或跨批次的同址同名同类型记录按指纹去重，只计一次，跨批重复时刷新来源批次与更新时间，但保留首次来源批次用于追溯。
- 坏行被拒绝并返回从 1 开始的 `line_number` 与原因，不影响同批已确认的合法行；原始行内容保存在错误记录中。

命令行导入与评估（支持 JSON 信封或 CSV）：

```bash
python -m app.cli exposure-import buildings.csv --batch-id census-20260924 --source census
python -m app.cli exposure-assess --threshold 6 --grid grid.json --grid-step-km 10
python -m app.cli exposure-query --assessment latest
python -m app.cli exposure-query --batch census-20260924
python -m app.cli exposure-query --assessment latest --division 510121001 --high-risk
```

API：

```bash
# 导入（201，返回数据质量统计与坏行行号）
curl -sS -X POST http://127.0.0.1:8432/api/exposure/imports \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"census-1","records":[{"record_type":"building","name":"安居楼1号","division_code":"510121001","latitude":30.1,"longitude":103.2,"population":320}]}'

# 评估：直接给烈度网格点，或用 computation_id 引用已完成的烈度计算
curl -sS -X POST http://127.0.0.1:8432/api/exposure/assessments \
  -H 'Content-Type: application/json' \
  -d '{"intensity_threshold":6.0,"points":[{"latitude":30.1,"longitude":103.2,"intensity":8.2}],"grid_step_km":10}'
```

- `GET /api/exposure/assessments/{assessment_key}` 返回按 `division_code` 稳定排序的分区摘要（高风险建筑、设施、关键设施、人口）与数据质量统计。
- `GET /api/exposure/records?assessment_key=...&high_risk_only=true` 列出高风险记录及其匹配烈度、来源批次、首次来源批次与原始记录；`GET /api/exposure/records/{id}` 可回溯单条原始记录。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         灾情、事件、公告、部门和信访业务接口
  seismic/         地震事件、台站观测和科学计算服务
  exposure/        建筑与关键设施暴露清单的导入、去重和分区高风险汇总
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
