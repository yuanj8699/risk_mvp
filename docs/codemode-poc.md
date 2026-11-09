# Code Mode小验证

基于PR #3的`eb7472f`实现Issue #4，保留一个Pi Agent，精确锁定`@earendil-works/pi-codemode@1.4.0`。实验默认关闭，不作为PR #3合并前提。

## 启用与交接

```powershell
$env:RISK_CODEMODE = '1'
.\.venv\Scripts\python.exe run.py
```

取消该环境变量或设为`0`恢复原逐工具模式。两种模式参与任务身份计算。已有应用库启动时补建三个额度表，原业务表不重写。

Pi外层获得risk_js、任务文件、Python执行和结果提交能力；JS仅注入check_risks与search_indicators。MCP实际call入口检查允许集合，register_query_plan只由Node宿主调用，不向JS/Pi注入。

JS返回`{org_ids,indicator_ids,history}`。宿主拒绝多余字段、非法类型、越界机构和未知指标，规范化后按冻结上下文生成稳定plan_id，原子写`data/plans/<plan_id>.json`并保存可信清单。模型收到路径和小摘要，Python只读取路径，不抄写机构数组。计划不是授权凭证，实际导出仍校验服务端任务范围。

零候选必须关联本任务真实完整检查回执。`no_candidates`表示没有补查候选，保留触发、缺数、待配置统计，不表示所有机构无风险；不导出、不启动Python、不生成综述函。正常结果继续沿用原执行/hash/原始证据校验。

## 边界与额度

QuickJS最多10秒且不超过任务剩余时间，堆32MiB，源码/参数32KiB，单响应64KiB、累计响应256KiB，日志/返回计划各16KiB，最多16次嵌套调用。超限拒绝，不把截断输出当完整结果；清理等待实际worker退出和桥接调用结束。无文件、网络、SQL、Bash或Python执行注入。

自主分析的MCP读取与Docker取数broker进入同一账本，内部辅助读取不重复扣费。原规则监测保留原预算方式。宿主配置：

| 环境变量 | 默认值 |
|---|---:|
| RISK_QUERY_MAX_REQUESTS | 200次/任务 |
| RISK_QUERY_RATE_PER_MINUTE | 120次/任务/分钟 |
| RISK_QUERY_RUN_CONCURRENCY | 2 |
| RISK_QUERY_GLOBAL_CONCURRENCY | 8 |
| RISK_QUERY_TIMEOUT_SECONDS | 30秒/操作 |
| RISK_EXPORT_MAX_ROWS | 100000行/任务 |
| RISK_EXPORT_MAX_BYTES | 67108864字节/任务 |

SQLite使用WAL，短事务原子检查额度并占位，再在事务外查询。SQLite progress handler可在首次返回行前中断长SQL；MCP取消传到实际查询线程，等待退出后释放占位。应用启动和Pi进程树回收后，仅回收系统确认owner已死亡的占位，不按过期时间盲释放。PostgreSQL实现锁与statement_timeout接口，未实库验证；owner身份仅适用于同一宿主，不支持多主机共用账本。

已准入业务失败也计请求。FastMCP/JS schema在准入前拒绝的无效参数不产生业务查询，不扣数据库请求额度，但受JS次数/时间约束。导出每1000行计实际行数及已写Parquet、证据索引和schema字节，超限不发布manifest；可能超过阈值至当前批次，不是严格瞬时磁盘配额。复用仍计请求，不重复计物理导出量。新JS、Python重跑和任务重试不重置累计账本。

## 对照结果（2026-11-09）

以下是单次本机样例，不是性能基准，未调用真实模型。

| 测量 | 逐工具 | risk_js |
|---|---:|---:|
| 真实Pi固定响应流，计划阶段模型请求 | 2 | 1 |
| 固定业务fixture，宿主调用（含登记） | 3 | 3 |
| 固定业务fixture，回模型工具内容 | 292B | 140B |
| 固定业务fixture，本地耗时 | 6.08ms | 35.99ms |
| 真实MCP/SQLite，回模型内容（含计划） | 18258B | 728B |
| 真实MCP/SQLite，含连接启动的计划耗时 | 2071.55ms | 1669.30ms |

真实查询两路选出相同11家不良率排名触发行社，计划一致，导出528条历史观测逐条一致。交叉测试中JS两次查询+Python宿主导出耗尽3次任务额度，后续导出被拒。另用全82家8指标计划验证12054条导出及复用，这与11家筛选是不同样例。

固定流减少一次模型往返，但QuickJS增加本地开销；尚不能证明真实模型下总体更快，保持实验关闭。固定链条也可继续用普通业务函数。

## 验证范围

实际QuickJS/worker、Pi固定流、真实MCP/SQLite、计划/检查回执、跨进程额度、取消/SQL期限、零候选有对应检查。

**Docker引擎不可用，容器读取计划、pandas结果一致性和超过一万行的完整容器链路尚未验证。** Docker用例单列，不把缺失环境记为通过；真实模型、小Q与生产用户机构权限尚未接入，当前范围仅是测试任务范围。

```powershell
npm.cmd --prefix agent test
.\.venv\Scripts\python.exe -m pytest tests/test_query_budget.py tests/test_query_plans.py tests/test_risk_js_workflow.py -q
```
