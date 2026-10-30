# 行社风险提示智能体 · 本地测试版 v0.1

本项目将“数据导入—指标筛选—规则检测—证据查看—函件草稿—审核—模拟下发”连成一条可运行流程。机构范围按本次需求设置为11个地区、82家行社，并保留第4、5级机构。

**这是本地测试工程。内置机构名称、地区归属、指标数值均为合成数据。Pi 已通过真实 stdio MCP 主动查询数据库中的快照，并将补查证据用于分析与函件。默认仍使用本地确定性逻辑模拟模型响应，不请求外部模型。小Q尚未接入；模拟下发只生成本地回执。**

## 1. 运行

需要 Python 3.11 或更高版本，以及 Node.js 22.19 或更高版本。首次安装需要访问 Python 包源和 npm。前端页面及 Agent 的 JavaScript 已编译保留；日常运行不需要启动前端开发服务，但需要 Node.js 执行 Pi Agent。

### Windows

解压后双击 `start_windows.bat`，或在 PowerShell 中执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm.cmd --prefix agent ci --ignore-scripts
npm.cmd --prefix agent run build
.\.venv\Scripts\python.exe run.py
```

浏览器打开 <http://127.0.0.1:8000>。接口文档：<http://127.0.0.1:8000/docs>。

### macOS / Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm --prefix agent ci --ignore-scripts
npm --prefix agent run build
.venv/bin/python run.py
```

默认使用项目 `data/` 目录下的 SQLite 数据库，首次启动自动创建表和合成数据。关闭服务后记录仍会保留。重新启动不会自动清空数据。Windows 启动脚本会检查 Python、Node、FastMCP 与 MCP SDK，并安装缺失的项目依赖。MCP 子进程由 Node 自动启动，使用后端当前的 Python 解释器，无需另外开启服务窗口。

## 2. 先体验一条完整流程

1. 打开“风险监测”，选择最新合成数据批次。
2. 输入一个风险主题，例如“关注信用风险与资金业务不良”，运行监测。
3. 查看排名提醒、存在提醒和数据不足的数量。占比阈值尚未配置时，应显示待配置，不视为正常。
4. 点击某家行社，检查触发证据、补查证据及工具轨迹；补查数据保留各自实际日期。
5. 生成函件草稿；如需编辑，保存后重新审核。
6. 点击模拟下发，核对带有 `SIM-` 前缀的本地回执。同一版本重复操作返回同一回执。
7. 在规则页把排名边界从72改为73，再运行一次，观察边界变化和新规则版本。
8. 占比规则可以输入测试阈值，观察新增结果；该输入只是测试配置，不代表行内正式阈值。

本轮留存的[单行社样例](docs/examples/issue-1-single-society.json)中，合成行社082的五级不良贷款率为3.15%、排名82/82，触发排名规则；Pi 经 MCP 查得逾期贷款率4.04%，以及8月、9月的不良贷款率3.07%、3.11%，分析引用了这些观测并生成[函件草稿](docs/examples/issue-1-letter.txt)。[默认规则批量样例](docs/examples/issue-1-batch.json)得到38家风险行社、49项触发、3项数据不足、82项占比待配置。这里验证的是实际 MCP、数据库查询与引用链路，模型响应仍为确定性替身。

## 3. 本轮确认与暂定项

| 内容 | 本版处理 |
| --- | --- |
| 机构层级 | 1我行、2地区、3行社、4支行、5预留机构；本期只监测第3级 |
| 地区和行社数量 | 11个地区、82家行社；合成归属仅用于演示，不对应真实组织关系 |
| 排名 | 暂沿用附件的 `rank >= 72`，82家全参评无并列时为11家；并列按条件全部命中 |
| 排名来源 | 使用导入数据已有排名；监测过程中不重新计算排名 |
| 占比 | 以“五级不良贷款率”演示比率规则；阈值默认空，正式指标口径和阈值待确认 |
| 存在提醒 | 资金业务不良余额有效且非零即提醒；缺失与零分开 |
| 规则组合 | 测试版每条规则独立触发，同一行社同次运行合并为一项事件；正式是否有AND组合待业务确认 |
| 数据来源 | 页面支持CSV/XLSX导入；脚本支持SQLite/PostgreSQL标准视图只读导入 |
| Agent | Pi 负责指标选择，经只读 MCP 补查当前值和历史，并提交有证据引用的分析；默认模型替身 |
| 小Q | 尚未接入，实际协议、认证和请求/响应字段待提供，不假设兼容现有模型协议 |
| 函件 | 当前为通用测试模板，正式模板尚未提供 |
| 下发 | 页面只允许本地模拟；内部风险管理系统的真实协议未知 |

“排名后72”仍有边界歧义，本版选择第72名及以后，依据为已上传提示词。它不表示“最后72家”。

## 4. 数据导入

页面可下载空白CSV表头；Excel请整理成同样列名的单个工作表。不要直接上传指标名称目录：监测需要机构、日期和数值。

| 字段 | 含义 | 示例 |
| --- | --- | --- |
| org_code | 系统内已存在的机构代码，保留文本格式 | LS072 |
| indicator_id | 系统内已有指标ID | npl_ratio |
| data_date | 该指标实际数据日期 | 2026-10-28 |
| value | 普通数值；空值表示缺失 | 1.65 |
| unit | `%` 或 `元`，与目录一致 | % |
| rank | 已有排名，可空 | 72 |
| population | 排名参评数量，可空 | 82 |
| rank_scope | 排名范围 | province_societies |
| value_encoding | 百分比数值用percent_number；金额用raw | percent_number |

示例仅为合成数据：

```csv
org_code,indicator_id,data_date,value,unit,rank,population,rank_scope,value_encoding
LS072,npl_ratio,2026-10-28,1.65,%,72,82,province_societies,percent_number
LS072,funds_npl_balance,2026-09-30,120000,元,,,,raw
```

`1.65 + % + percent_number` 表示1.65%，不会再乘100。Excel原生百分比单元格（底层0.0165、显示1.65%）需要先转换为明确的普通数值；导入器会拒绝这种隐含编码，不猜单位。Excel公式也需先转换为已核验数值。

本版限制每次5MB、10000条记录。一个快照对同一机构、同一指标只接受一个有效期次；重复、未知机构/指标、非法日期、非有限值和单位冲突会整批拒绝。不同频率指标允许各自的数据日期。

内置目录：

| ID | 指标 |
| --- | --- |
| npl_ratio | 五级不良贷款率 |
| overdue_ratio | 逾期贷款率 |
| new_npl_ratio | 不良发生净生成率 |
| deposit_growth | 各项存款余额（人行）比年初增幅 |
| funds_npl_balance | 资金业务不良余额 |
| funds_npl_change | 资金业务不良余额比年初增量 |
| pure_deposit_growth | 纯存款余额比年初增幅 |
| funds_npl_ratio | 资金业务不良率 |

真实机构代码和指标目录的维护页面尚未实现。当前可使用合成代码测试，也可在首次初始化前修改 `backend/seed.py`；不要通过改名让合成数据看起来像真实经营数据。

## 5. 数据库来源与DataWorks

DataWorks是一套数据开发治理平台，可以连接不同计算引擎与数据源。后续要确认实际引擎、表/视图、字段、取数权限和网络可达性，不能把“有DataWorks权限”直接当成某种数据库连接协议。

本版的 `scripts/import_from_database.py` 只读固定标准视图 `risk_indicator_export`，字段与上表一致。源端账号必须已经具有只读权限；脚本也会为PostgreSQL设置只读事务，为SQLite设置query_only。该脚本不接受任意SQL。

先启动本地应用，再配置环境变量并调用脚本：

```powershell
$env:RISK_SOURCE_DATABASE_URL = 'sqlite:///C:/path/to/test_source.db'
.\.venv\Scripts\python.exe scripts/import_from_database.py --date 2026-10-28
```

使用PostgreSQL来源需先安装 `requirements-postgresql.txt`，并将连接信息通过环境变量提供。不要将真实密码提交到代码中。MaxCompute、Hologres等实际环境的专用适配器需在确认连接方式后实现。

应用自身也可切换PostgreSQL：设置 `RISK_DATABASE_URL` 为SQLAlchemy的 `postgresql+psycopg://...` 连接串后启动。当前已验证SQLite运行；PostgreSQL连接路径预留，未连接真实实例做联调。

两个连接变量职责不同：`RISK_DATABASE_URL` 是保存任务、函件和已导入/合成快照的应用库；`RISK_SOURCE_DATABASE_URL` 仅供上述显式导入脚本读取指标源，不进入应用建表或种子初始化。当前 MCP 查询的是应用库内已固定的快照。TBase/PostgreSQL 协议指标源的真实表、字段与权限契约尚未取得，尚未完成实时外部数据库联调。

导入脚本的 `--date` 按来源记录的精确日期筛选；MCP 的“截至日最新值”查询则按指标实际日期取值，可以取到上期季度指标，两种日期语义不能互换。

## 6. 代码结构与阅读顺序

| 路径 | 职责 |
| --- | --- |
| backend/models.py | 机构、指标、快照、事实、规则、任务、事件、函件、修订与审计模型 |
| backend/seed.py | 合成机构、指标与3个数据快照，方便重复演示 |
| backend/app.py | API、导入校验、任务短事务与函件状态处理 |
| backend/services.py | API/MCP共用的目录查询、固定上下文、指标取证、纯规则检测与模板读取 |
| backend/mcp_server.py | FastMCP stdio服务；由宿主绑定任务与行社，只暴露4个只读业务工具 |
| backend/analysis.py | Python 与 Pi Agent 的 JSON 进程调用边界、结果校验与超时处理 |
| agent/src/contracts.ts | Agent 请求、结果、证据和运行轨迹的 TypeScript 类型契约 |
| agent/src/mcp-business.ts | 薄MCP客户端；绑定机构、调用业务工具、分类错误并关闭子进程 |
| frontend/src/api.ts | 页面共用的 JSON 请求、文件上传及接口错误处理 |
| agent/ | TypeScript Pi Agent、业务工具、模型接入、离线演示与契约测试；dist 为编译产物 |
| frontend/ | Vue3/TypeScript页面源码与已编译dist |
| scripts/import_from_database.py | 固定标准视图的只读数据导入 |
| tests/ | 规则边界、数据校验、函件版本与模拟下发的回归验证 |
| docs/architecture.md | 结构说明、业务边界及后续实现路径 |

运行测试：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q
npm.cmd --prefix agent test
```

复用时，Python 调用 `provider.select(topic, indicators, mandatory_ids)` 或 `provider.explain_batch(cases, context)`；分析请求只带触发证据与固定任务上下文，不预装辅助数组，辅助值由 MCP 实际查询取得。TypeScript 调用 `runRequest(request, options?)`，输入输出类型见 `contracts.ts`。CLI 入口 `runner.ts` 只负责 JSON 读写。前端的 `api<T>()` 与 `send<T>()` 集中处理 HTTP，页面只维护业务状态。

编辑 Agent 时，在项目根目录运行 `npm.cmd --prefix agent run build`。编辑前端时，在 `frontend/` 运行 `npm ci --ignore-scripts`、`npm run build`；Python 服务提供编译后的页面，不使用外部 CDN。

### Pi Agent 的运行模式

使用的是 GitHub 上原 `badlogic/pi-mono` 项目，现已迁移到 [earendil-works/pi](https://github.com/earendil-works/pi)。本版锁定 `@earendil-works/pi-agent-core` 与 `@earendil-works/pi-ai` 1.4.0、`@modelcontextprotocol/sdk` 1.32.2、Python `fastmcp` 4.1.4。

默认 `RISK_PI_MODE=demo`。指标选择使用本地目录工具，事件分析通过 MCP 执行 `search_indicators`、`query_metrics`、`check_risks`、`get_letter_template` 等只读业务查询，再由本地 `submit_analysis` 提交结构化结果。模型响应由确定性逻辑生成，具体查询仍真实访问数据库。此模式无需 API 密钥，也不会发送指标数据到外部服务。

真实模型模式通过以下环境变量配置，然后重新启动服务：

```powershell
$env:RISK_PI_MODE = 'model'
$env:RISK_PI_PROVIDER = '<Pi 内置目录中的供应商 ID>'
$env:RISK_PI_MODEL = '<模型 ID>'
$env:RISK_PI_API_KEY = '<该服务的 API 密钥>'
.\.venv\Scripts\python.exe run.py
```

`RISK_PI_PROVIDER` 与 `RISK_PI_MODEL` 的组合必须存在于 Pi 内置模型目录。可选 `RISK_PI_BASE_URL` 用于替换为采用同一协议的 HTTP(S) 服务地址，不能在 URL 内嵌用户名或密码；它不能把任意小Q接口自动转换成兼容协议。

仅对获准的模型服务和数据启用 `model` 模式：该模式会将本次指标目录、风险证据及工具查询结果发送给所配置的模型。密钥通过环境变量提供，不写进代码或 Git。缺少配置、模型失败、结果不合格或执行超时会记录失败，不会悄悄改用演示结果。真实模型接口尚未联调，小Q需确认协议后接入。

Python 保留取数、规则计算、数据库写入、函件审核和模拟下发。每次监测分别调用一次 Pi 指标选择和一次批量证据分析；Agent 不能修改排名、规则阈值或审批状态。

### 固定上下文、失败重试与预算

任务创建时固定 `run_id / topic / snapshot_id / as_of / rule_snapshot / org_scope / history_snapshot_ids / source`，存入 `run.selection.task_context`。MCP 从已提交任务加载范围，并按当前行社进一步收窄；模型不能指定另一个任务。后续规则修改、新导入或不同来源数据不会混入本次分析。详情见[架构说明](docs/architecture.md)。

运行先提交 `running` 任务和检测证据，再在写事务外调用 Pi/MCP，最后用新短事务保存 `completed` 或 `failed`。失败保留已提交证据，暂不可拟函。第一次执行失败返回 HTTP 502，`detail` 为 `{ "run_id": "...", "message": "..." }`；重复提交相同输入默认返回已有任务，包括 `failed` 状态。修复原因后向 `/api/runs` 提交相同输入并显式设置 `"retry": true` 才会重试；条件更新只允许一个请求执行该次重试。改变规则版本、主题、机构范围或模型配置会形成不同运行身份。

`RISK_PI_BATCH_TIMEOUT_MS` 默认120000毫秒，控制每次 Node 请求的任务预算；Python 使用同一值并额外留10秒退出清理。每个行社另受 `RISK_PI_TIMEOUT_MS`（默认30000毫秒）、`RISK_PI_MAX_REQUESTS`（默认10）、`RISK_PI_MAX_TOOLS`（默认12）限制。Node 在完成、失败或取消时关闭 MCP 会话；若超出清理余量，Python 终止该任务进程树。当前仍为同步任务，没有持久 worker；服务被强制终止后可能留下 `running` 记录，尚未实现自动恢复或租约接管。

## 7. 尚未实现的部分

- 未连接真实DataWorks数据源、小Q模型接口或内部风险管理系统。
- Pi 工具执行与离线演示已实现；真实模型模式已提供配置入口，但尚未完成真实服务联调。离线演示不代表模型推理效果。
- 监测为本地小批次同步执行，已有运行去重、失败记录和显式重试；尚未实现持久worker、强制进程崩溃后的恢复、跨进程任务租约和自动调度。
- 当前没有生产登录、机构数据权限或正式授权策略。默认仅绑定127.0.0.1，适用于本地测试。
- 模拟下发验证版本与状态，不等于实现真实渠道的outbox、重试和送达确认。
- 日频7天、季频120天为可见的测试新鲜度策略，正式运行需按行内数据更新周期配置。
- 函件关键事实来自固定证据；人工编辑后的自由文本尚未实现自动语义核验。
- 正式函件模板、占比阈值、比率口径、排名边界与规则组合需要业务确认。

## 8. 接下来怎样补后端和数据库知识

先读 `models.py`，理解为什么指标定义、指标事实、快照和函件修订分表；再读 `services.py` 的 `evaluate()` 与 `query_metrics()`，区分规则检测、缺数和截至日取证；最后读 `app.py` 的短事务与函件状态API，理解事务、乐观并发与幂等。

随后阅读 `backend/analysis.py`、`backend/mcp_server.py` 与 `agent/`，跟踪“固定任务 → Pi发起MCP查询 → 数据库返回证据 → 引用校验 → 函件草稿”。真实模型联调与后台 worker 可在这条链路上逐项补充。

资料：

- [李博杰《深入理解AI Agent》](https://github.com/bojieli/ai-agent-book)
- [FastAPI数据库文档](https://fastapi.tiangolo.com/tutorial/sql-databases/)
- [阿里云：什么是DataWorks](https://help.aliyun.com/zh/dataworks/user-guide/what-is-dataworks)
