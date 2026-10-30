# Issue #1 实际验证

日期：2026-10-30。基线：master@8589f6e；分支：feat/issue-1-business-mcp。
环境：Windows、Python 3.14.4、Node 24.14.0、SQLite、Pi 1.4.0、FastMCP 4.1.4、MCP TypeScript SDK 1.32.2。

## 已验证的业务链路

先验证单行社，再执行批量。使用独立临时SQLite，模型响应为确定性替身；Pi循环、stdio MCP连接和数据库查询均真实执行，没有使用输入中预装的辅助数组。

- 合成行社LS082：五级不良贷款率3.15%，排名82/82触发规则。
- Pi依次调用 `check_risks → query_metrics(history=true) → get_letter_template → submit_analysis`。
- 实际取得8条去重观测，包括逾期率4.04%、历史不良率3.07%和3.11%；分析引用这次查询的稳定证据ID。
- 函件草稿列出触发事实、补查值、实际日期和证据ID。
- 82家行社批量：38家风险行社、49项触发、358项未触发、3项数据不足、82项占比待配置。
- 浏览器实测：查看检查结果、主动补查证据及引用，再生成草稿。单行社监测范围正确显示为1家。

可复核材料：

- [单行社完整上下文、工具轨迹与证据](docs/examples/issue-1-single-society.json)
- [对应函件草稿](docs/examples/issue-1-letter.txt)
- [批量结果](docs/examples/issue-1-batch.json)

## 回归与必要边界

- Python检查共37项已有通过记录。一次全量运行先得到35通过、1项旧断言失败（122秒）；旧断言禁止任何陈旧辅助指标出现，与本轮明确的历史查询语义冲突，现改为断言实际历史日期、历史标识、不可冒充本期变化及current_issue。最终动态MCP工具schema构建后的4项针对性检查通过（8.47秒），其中包含该修正和新增加的真实Pi/MCP错误纠正路径。未通过跳过或削弱业务约束制造通过。
- Node既有/针对性契约检查12项通过；前端TypeScript检查、Agent构建、前端构建通过。
- 模型等待时独立连接可读运行和证据，并可编辑规则、导入新快照；本任务规则版本及历史快照集合保持冻结。
- 首次并发和失败重试通过条件更新争取唯一执行权；相同任务不会重复执行模型。失败记录和触发证据保留，不可生成草稿，显式重试后可完成。
- 截至日查询涵盖最新日频与上期季度值；季度重复观测去重，跨来源/未来/新导入快照不会混入；冻结历史快照不可读时明确失败。
- 真实Pi流先调用未知指标，接收MCP错误，再修正参数查询逾期率；轨迹包含 `query_metrics:error → query_metrics:ok`，结果正确引用4.04%的查询证据。
- Python实际子进程等待期限等于TS预算加10秒退出清理余量；Pi取消/超时受限，结束时关闭MCP。完成联调后未发现残留Python MCP进程。

## 使用边界

- 未验证真实模型推理质量，也未连接小Q、TBase/PostgreSQL真实指标源或正式下发系统。
- MCP当前读取应用内已导入/合成快照。RISK_SOURCE_DATABASE_URL仍用于显式只读导入脚本，不进入应用建表或种子流程。
- 没有持久worker。宿主异常崩溃留下running记录的自动恢复尚未实现；正常执行异常/超时则保存failed并允许显式重试。

复现：安装requirements-dev.txt后运行 `python -m pytest tests -q`；Agent运行 `npm --prefix agent test`。单家运行通过POST /api/runs指定org_scope=["LS082"]，失败时对相同输入传retry=true。
