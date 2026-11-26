# 行社风险提示智能体（Risk Agent Workbench）v1.0.0

面向省级农商行 / 省联社非现场风险监测场景的单页智能分析工作台，完整打通 **「全辖 82 家行社扫描 → 口径校验与确定性核验算子 → 重点行社后台自动生成正式风险报告 → 在线修订定稿与 Word 导出 → 全程执行轨迹溯源与证据追问」** 业务闭环。

> **数据与运行边界声明**
> - 外网默认运行 `RISK_DATA_FETCHER=local`，使用本地 SQLite 快照与合成测试数据，不连接内网生产库；内网部署切换环境变量即可对接 QuickBI 合规票据免登与 OLAP 取数接口。
> - 默认模型模式为 `RISK_PI_MODE=demo`（零密钥离线演示，数据查询与规则校验仍走真实数据库）；启用 `RISK_PI_MODE=model`（如 DeepSeek / OpenAI）时需通过环境变量或本地 `.env` 提供密钥，严禁将真实密钥或生产数据写入代码仓库。

---

## 核心特性（v1.0.0）

### 1. 全辖多维风险扫描与多批并发归并
- 覆盖 **82 家三级行社**（11 个地区），内置信用风险防控、风险抵补与资本管理、资金业务管控等多维度指标知识库与参考筛查规则。
- 扫描结果按批并发（默认 5 家/批、6 批并发）交给 **Pi Agent** 归并风险事项，要求所有定性归纳严格绑定同机构有效 `evidence_id`，杜绝跨行社串数与编造指标。
- 支持 **DeepSeek（含思考链 `reasoning_content` 透出）**、OpenAI 等真实大模型，以及确定性离线 `demo` 模式。

### 2. 两层确定性核验算子 + 只读 MCP 业务总线
- **只读 MCP 服务**（`backend/mcp_server.py`）：按任务与行社隔离上下文，向 Agent 暴露 `fetch_org_metrics`、`check_risk_rules`、`run_verified_operator` 等只读取证工具，模型无法篡改原始观测或审批状态。
- **确定性核验层**（`backend/verification_primitives.py` + `backend/verification_layer.py`）：
  - 全程采用 Python `Decimal` 高精度计算，坚持「缺数不补零、不跳期拼接、率类拒绝打总、实测与推演严格隔离」；
  - 内置 7 大标准分析算子：`consecutive_trend`（连续趋势）、`benchmark_deviation`（基准偏离）、`metric_divergence`（指标背离）、`concentration_cr`（集中度 CR）、`incremental_contribution`（增量贡献）、`bucket_distribution`（分档分布）、`stress_test`（压力测算，强制标记 `simulated`）。

### 3. 正式「风险报告」自动生成、精修定稿与 Word 导出
- **扫描后自动出报告**：全辖扫描完成后立即返回总览，并在后台并发池自动为重点高风险行社（默认优先 `≥3 项 P0 监管底线`，单批上限 `8` 家）撰写正式风险报告；总览页每 4 秒静默更新进度并支持失败重试，其余行社可按需手动生成。
- **严口径公文撰写与复核**：
  - 声明式监管底线（`letter_regulatory_floors`）与审慎表述护栏（`letter_caution_guards`）驱动；
  - 强制 **Read-Before-Cite（先查后引）**、**P0 底线零遗漏**、无内部代码/脏文案拦截；
  - **底层全精度 + 对外两位小数**：数据库与核验算子保留全精度，正式风险报告统一按财务四舍五入（`ROUND_HALF_UP`）保留 2 位小数（极小非零百分点变动保留 4 位防归零）。
- **在线修订、审核定稿与零依赖 Word 导出**：
  - 支持「编辑修订 → 保存新版本」（带乐观锁版本冲突保护）与「审核定稿」（定稿后锁定防覆盖）；
  - 内置纯 Python 标准库公文生成器（`backend/docx_export.py`，零第三方依赖），一键导出符合银行公文版式（A4 页边距、小标宋标题、仿宋正文、带表头与数值右对齐的附表）的 `.docx` 文件，同时保留 Markdown 导出。

### 4. 可视化执行轨迹（Trace）与历史任务管理
- **三轨交互式时间轴**：按输入 / 模型 / 工具三轨展示执行过程，支持按时长、轮次、调用顺序切换；**默认自动折叠超过 15 秒的空闲间隙**并支持一键切换真实跨度。
- **可审计的模型诊断**：完整留存模型公开思考链、工具参数与耗时、token 用量、证据 ID 与 3 次有界纠错记录，所有凭据与超长正文均做脱敏与截断标注。
- **历史列表治理**：侧边栏展示时间、模型/模式标签与报告数/进度，支持单条安全删除（事务加锁拦截运行中或含已定稿/已下发报告的任务）与「一键清理失败任务」。

### 5. 多轮证据追问、小 Q 补查与自主代码分析
- **多轮证据追问**：支持切换「当前行社 / 全辖范围」连续追问，回答强制引用任务归档证据 ID 并支持定位对应轮次轨迹。
- **小 Q 实时补查**：支持对归档外指标自动改写规范问句并调用小 Q 流式接口补查（主口径仍以 QuickBI 源表为准）。
- **Docker 隔离自主代码分析**：支持在无网络、限资源 Docker 容器内运行 Python 脚本分析 Parquet 导出数据，并由宿主核验清单与原始证据哈希；另内置可选 QuickJS `risk_js` Code Mode 实验入口（默认关闭）。

### 6. 双轨数据接入（外网开发 + 内网 QuickBI 就绪）
- **外网开发模式（`local`）**：默认读取 `data/risk-testdata.db`，支持页面上传 CSV/XLSX 快照及 `scripts/import_from_database.py` 只读视图导入。
- **内网 QuickBI 模式（`quickbi`）**：已按内网协议对齐 **合规票据纯 HTTP 免登身份层**（`IDENTITY_PROVIDER=smartq_ticket`，无浏览器、逐跳严格同源校验、5 分钟缓存、`AUTHX_01002` 自动重出票重试）与 **真实 `olapQueryParam` 表单取数层**（长表/宽表自适应、动态 `fid` 解析、`live_indicators()` 实时指标清单）。详见 [docs/quickbi-integration.md](docs/quickbi-integration.md)。

---

## 系统架构

```text
┌────────────────────────────────────────────────────────────────────┐
│  Vue 3 + TypeScript + TDesign 单页工作台 (frontend/src → dist)       │
│  总览与分析 · 风险报告(编辑/定稿/Word导出) · 执行轨迹(折叠空闲) · 追问 │
└───────────────────────────────┬────────────────────────────────────┘
                                │ HTTP / JSON / File Download
┌───────────────────────────────▼────────────────────────────────────┐
│  FastAPI 业务宿主 (backend/app.py · risk_scans.py · risk_letter.py) │
│  ├─ 数据取数层 (data_fetcher.py): LocalSnapshot | QuickBiOlapFetcher│
│  ├─ 确定性核验层 (metric_validator · verification_primitives/layer) │
│  ├─ 知识与公文层 (risk_knowledge · docx_export · run_cleanup)       │
│  └─ 持久化归档 (SQLAlchemy · SQLite WAL / PostgreSQL)               │
└──────────────────┬───────────────────────────────────┬─────────────┘
                   │ JSON Subprocess                   │ Read-only stdio MCP
┌──────────────────▼───────────────────────────────────▼─────────────┐
│  Pi Agent 运行时 (agent/src → dist · @earendil-works/pi-agent-core) │
│  ├─ risk-synthesis.ts (多批并发归并 + 定性表述护栏)                  │
│  ├─ risk-letter.ts    (工具取证 + 算子核验 + P0底线复核撰函)         │
│  ├─ follow-up.ts      (多轮归档证据问答 + 小Q改写补查)               │
│  └─ code-exploration.ts (Docker 隔离 Python 自主分析)               │
└────────────────────────────────────────────────────────────────────┘
```

---

## 快速开始

### 环境要求
- **Python 3.11+**（已在 Python 3.14 `.venv` 全量验证）
- **Node.js 22.19+**（运行 Pi Agent 与构建前端/Agent）
- **Docker**（仅使用「自主代码分析」功能时需要；常规全辖风险扫描、报告撰写与追问无需 Docker）

### 1. 安装与启动（Windows PowerShell）

也可直接双击根目录 `start_windows.bat`，或手动执行：

```powershell
# 1. 创建并安装 Python 依赖
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 安装并构建 Pi Agent 与前端（仓库已附带编译产物，二次开发时重新构建）
npm.cmd --prefix agent ci --ignore-scripts
npm.cmd --prefix agent run build
npm.cmd --prefix frontend ci --ignore-scripts
npm.cmd --prefix frontend run build

# 3. 初始化本地 82 家行社测试快照（仅首次需要，不覆盖已有库）
.\.venv\Scripts\python.exe scripts/import_named_testdata.py --database data/risk-testdata.db

# 4. 启动单页工作台（默认 127.0.0.1:8000，可传 --port 8010）
.\.venv\Scripts\python.exe run.py --host 127.0.0.1 --port 8000
```

### 2. 安装与启动（macOS / Linux）

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm --prefix agent ci --ignore-scripts && npm --prefix agent run build
npm --prefix frontend ci --ignore-scripts && npm --prefix frontend run build
.venv/bin/python scripts/import_named_testdata.py --database data/risk-testdata.db
.venv/bin/python run.py --host 127.0.0.1 --port 8000
```

启动后访问：
- 工作台首页：<http://127.0.0.1:8000>
- FastAPI 接口文档：<http://127.0.0.1:8000/docs>

---

## 模型与数据源配置

应用启动时自动读取项目根目录下被 Git 忽略的 `.env` 文件（也可直接通过系统环境变量配置）。

### 离线演示模式（默认，无需 API Key）

```dotenv
RISK_DATA_FETCHER=local
RISK_PI_MODE=demo
```

### 真实大模型模式（以 DeepSeek 为例）

```dotenv
RISK_DATA_FETCHER=local
RISK_PI_MODE=model
RISK_PI_PROVIDER=deepseek
RISK_PI_MODEL=deepseek-flash
RISK_PI_API_KEY=<你的 API Key，勿提交到 Git>
RISK_PI_THINKING_LEVEL=low
```

也支持 `RISK_PI_PROVIDER=openai`（配合 `RISK_PI_MODEL=gpt-5.4-mini`、`RISK_PI_API_KEY` 或 `RISK_OPENAI_ENV_FILE`）以及 `RISK_PI_BASE_URL` 指向兼容协议网关。

### 关键运行参数

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RISK_DATA_FETCHER` | `local` | `local`（本地 SQLite 快照）或 `quickbi`（内网 QuickBI OLAP） |
| `RISK_DATABASE_URL` | `sqlite:///data/risk-testdata.db` | 应用归档库连接串，支持 SQLite（WAL）与 PostgreSQL |
| `RISK_AUTO_REPORT_MAX` | `8` | 全辖扫描后后台自动生成重点风险报告的单批上限 |
| `RISK_PI_THINKING_LEVEL` | `low` | 思考模型推理深度（`off` / `low` / `medium` / `high`） |
| `RISK_PI_TIMEOUT_MS` | `90000` | 单次模型请求超时预算（毫秒） |
| `RISK_PI_BATCH_TIMEOUT_MS` | `360000` | 批量风险归并总截止预算（毫秒） |
| `RISK_CODEMODE` | `0` | 设为 `1` 启用可选 QuickJS `risk_js` 实验通道 |

内网 QuickBI 接入所需的 `IDENTITY_PROVIDER=smartq_ticket`、`SMARTQ_SVC_URL`、`QB_HOST`、`QB_REPORT_ID`、`RISK_QUICKBI_DICT_FILE`、`RISK_QUICKBI_ORG_FILE` 等完整说明，请参见 [QuickBI 内网取数配置文档](docs/quickbi-integration.md)。

---

## 项目结构与文档索引

### 代码目录

| 路径 | 职责 |
| --- | --- |
| `backend/app.py` | FastAPI 路由入口、请求校验、短事务与报告/导出/清理接口 |
| `backend/models.py` | SQLAlchemy 数据模型（机构、指标、快照、观测、任务、函件与版本历史） |
| `backend/risk_scans.py` | 全辖五维风险扫描、后台自动生成重点报告队列、归档取证封装 |
| `backend/risk_letter.py` | 风险报告只读取证、底线与护栏校验、财务两位小数展示格式化 |
| `backend/docx_export.py` | 零第三方依赖的标准银行公文 OOXML（`.docx`）排版生成器 |
| `backend/run_cleanup.py` | 事务级加锁的历史任务安全删除与失败任务批量清理 |
| `backend/verification_primitives.py` | L1 确定性金融计算原语（单位、精度、排名、聚合校验） |
| `backend/verification_layer.py` | L2 7 大标准分析算子与后置恒等核验契约 |
| `backend/metric_validator.py` | 源表口径体检、全省/地区合计勾稽与权限裁剪校验 |
| `backend/data_fetcher.py` | 统一只读 `DataFetcher` 协议与本地快照实现 |
| `backend/quickbi_client.py` | QuickBI 合规票据纯 HTTP 免登身份层、OpenAPI 签名与 OLAP 表单客户端 |
| `backend/quickbi_adapter.py` | QuickBI 长表/宽表动态 `fid` 取数适配器与实时指标清单 |
| `backend/mcp_server.py` | FastMCP 只读业务工具服务（stdio 子进程） |
| `backend/smartq_followup.py` | 小 Q 追问自动改写与流式补查编排 |
| `backend/code_tasks.py` / `sandbox/` | Docker 隔离沙箱自主 Python 分析与 Parquet 导出核验 |
| `agent/` | TypeScript Pi Agent（风险归并、撰函、追问、代码分析、Model Tap） |
| `frontend/` | Vue 3 + TypeScript 单页工作台（总览、风险报告、折叠时间轴、历史管理） |
| `scripts/` | 本地测试数据导入、大数据样例生成与外部只读视图导入脚本 |
| `tests/` | 后端业务、QuickBI 协议桩、核验算子、Word 导出与清理回归测试 |

### 设计与规格文档

- [系统架构说明](docs/architecture.md)
- [风险提示函可扩展工具架构规格（v3.2）](docs/risk-letter-spec.md)
- [确定性核验层与 7 大算子规格说明书](docs/verification-layer-spec.md)
- [第一梯队体验升级设计规格（自动报告 / 公文精修 / 时间轴折叠）](docs/tier1-upgrade-spec.md)
- [QuickBI 内网身份层与 OLAP 取数配置说明](docs/quickbi-integration.md)
- [小 Q 补查问句库规范](docs/smartq-question-bank.md)
- [Code Mode 实验说明](docs/codemode-poc.md)

---

## 测试与验证

安装开发测试依赖后即可运行全套三端自动化测试：

```powershell
# 后端 Python 回归测试（排除需要本机 Docker 引擎的容器用例可加 -k "not docker"）
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests/test_auto_reports.py tests/test_docx_export.py tests/test_formal_display.py tests/test_run_cleanup.py tests/test_quickbi_adapter.py tests/test_smartq_followup.py tests/test_verification_layer.py tests/test_verification_primitives.py tests/test_risk_scan_workflow.py tests/test_services.py tests/test_query_budget.py tests/test_api_workflow.py -q

# Pi Agent (TypeScript / Node) 契约与单测
npm.cmd --prefix agent run build
npm.cmd --prefix agent test

# 前端 TypeScript 校验、单元测试与生产构建
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
```

> 使用「自主代码分析」前需构建离线沙箱镜像：
> ```powershell
> docker build -t risk-python-sandbox:0.1 sandbox
> ```

---

## 已知边界与后续规划

- **权限与多租户**：当前默认绑定 `127.0.0.1` 用于单人/演示工作台；生产部署需配合行内统一认证网关与按身份隔离的归档库。
- **内网实机联调**：QuickBI 合规票据免登与 `olapQueryParam` 表单协议已完成本地 HTTP 协议桩全链路验证，部署内网后需结合实际账号校验行级权限裁剪与宽表度量映射。
- **后续演进方向**：流式实时思考轨迹推送（SSE）、公开银行风险报告语料库驱动的撰函评测基准，以及企业级持久化异步任务队列。
