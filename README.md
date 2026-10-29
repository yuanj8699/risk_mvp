# 行社风险提示智能体 · 本地测试版 v0.1

本项目将“数据导入—指标筛选—规则检测—证据查看—函件草稿—审核—模拟下发”连成一条可运行流程。机构范围按本次需求设置为11个地区、82家行社，并保留第4、5级机构。

**这是本地测试工程。内置机构名称、地区归属、指标数值均为合成数据。分析目前使用目录匹配与模板，尚未连接小Q或真实大模型；模拟下发只生成本地回执。**

## 1. 运行

需要 Python 3.11 或更高版本，推荐 Python 3.12。首次安装依赖需要访问可用的 Python 包源。页面已编译并包含在压缩包内，运行页面不需要安装 Node.js。

### Windows

解压后双击 `start_windows.bat`，或在 PowerShell 中执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

浏览器打开 <http://127.0.0.1:8000>。接口文档：<http://127.0.0.1:8000/docs>。

### macOS / Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

默认使用项目 `data/` 目录下的 SQLite 数据库，首次启动自动创建表和合成数据。关闭服务后记录仍会保留。重新启动不会自动清空数据。

## 2. 先体验一条完整流程

1. 打开“风险监测”，选择最新合成数据批次。
2. 输入一个风险主题，例如“关注信用风险与资金业务不良”，运行监测。
3. 查看排名提醒、存在提醒和数据不足的数量。占比阈值尚未配置时，应显示待配置，不视为正常。
4. 点击某家行社，检查每项触发证据的数值、排名、规则和数据期次。
5. 生成函件草稿；如需编辑，保存后重新审核。
6. 点击模拟下发，核对带有 `SIM-` 前缀的本地回执。同一版本重复操作返回同一回执。
7. 在规则页把排名边界从72改为73，再运行一次，观察边界变化和新规则版本。
8. 占比规则可以输入测试阈值，观察新增结果；该输入只是测试配置，不代表行内正式阈值。

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
| 小Q | `AnalysisProvider` 接口预留，实际协议、认证和请求/响应字段待提供 |
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

## 6. 代码结构与阅读顺序

| 路径 | 职责 |
| --- | --- |
| backend/models.py | 机构、指标、快照、事实、规则、任务、事件、函件、修订与审计模型 |
| backend/seed.py | 合成机构、指标与3个数据快照，方便重复演示 |
| backend/app.py | API、规则执行、导入校验、函件状态处理 |
| backend/analysis.py | 目录选择与模板分析，以及模型适配接口 |
| frontend/ | Vue3/TypeScript页面源码与已编译dist |
| scripts/import_from_database.py | 固定标准视图的只读数据导入 |
| tests/ | 规则边界、数据校验、函件版本与模拟下发的回归验证 |
| docs/architecture.md | 结构说明、业务边界及后续实现路径 |

运行测试：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q
```

编辑前端时，在 `frontend/` 运行 `npm ci`、`npm run build`；Python服务提供编译后的资源。日常运行不需要Node，也不使用外部CDN。

## 7. 尚未实现的部分

- 未连接真实DataWorks数据源、小Q模型接口或内部风险管理系统。
- 当前主题选择是可解释的预置匹配，不是大模型自主推理；真实模型需要实现 `AnalysisProvider`。
- 监测为本地小批次同步执行，已有运行记录与幂等处理；尚未实现持久worker、跨进程任务租约和自动调度。
- 当前没有生产登录、机构数据权限或正式授权策略。默认仅绑定127.0.0.1，适用于本地测试。
- 模拟下发验证版本与状态，不等于实现真实渠道的outbox、重试和送达确认。
- 日频7天、季频120天为可见的测试新鲜度策略，正式运行需按行内数据更新周期配置。
- 函件关键事实来自固定证据；人工编辑后的自由文本尚未实现自动语义核验。
- 正式函件模板、占比阈值、比率口径、排名边界与规则组合需要业务确认。

## 8. 接下来怎样补后端和数据库知识

先读 `models.py`，理解为什么指标定义、指标事实、快照和函件修订分表；再读 `evaluate()`，把三类规则与NULL/0状态对应起来；最后读函件编辑/审批/模拟发送API，理解事务、乐观并发与幂等。

完成这条业务链路后，再增加真实模型适配和后台worker，会比同时学习多个框架更容易定位问题。

资料：

- [李博杰《深入理解AI Agent》](https://github.com/bojieli/ai-agent-book)
- [FastAPI数据库文档](https://fastapi.tiangolo.com/tutorial/sql-databases/)
- [阿里云：什么是DataWorks](https://help.aliyun.com/zh/dataworks/user-guide/what-is-dataworks)
