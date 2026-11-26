# 行社《风险提示函》放权与可扩展工具化架构规范（v3.2）

> **定稿说明**：本规范融合既有取数层（`data_fetcher.py` / `mcp_server.py`）与代码核验层（`verification_layer.py` 七大确定性算子），作为 `06 提示函撰写`（`draft_risk_letter`）重构的唯一业务与工程契约。

---

## 一、核心架构原则

1. **零重复造轮子**：不另建私有趋势/对标统计脚本，直接复用已验收的 `backend/verification_layer.py`（`run_verified_operator` 七大高精度 `Decimal` 算子 + 前置口径体检 + 后置恒等复核 + 实测/推演严格隔离）与 `backend/mcp_server.py` 只读 MCP 总线。
2. **配置驱动规则、零代码扩展新维度**：将监管硬底线（`letter_regulatory_floors`）与良性防误报规则（`letter_caution_guards`）收口到 `backend/risk_knowledge_data.json`，彻底移除 `backend/risk_letter.py` 中写死的 8 专题 `if/elif` 与 `agent/src/risk-letter.ts` 中写死 8 专题且未知即抛错的 `responseFor()`。
3. **放权给模型自主研判，宿主严守底线护栏**：
   - 起步仅向模型提供极简任务简报（`< 2KB`），模型通过专职只读 MCP 工具主动取数、核查底线、运行确定性算子，最后自主聚类成 **`1～4` 段风险**与 **`1～5` 条定制整改建议**提交；
   - 宿主与工具双端严守 5 条铁律：**先查后引（Read-Before-Cite）**、**P0 底线零漏报**、**实测与推演隔离**、**良性防误报约束**、**公文洁净度红线**。
4. **打通本地快照与 QuickBI 归档回放**：提供 `fetched_from_scan_archive(db, run)`，从当次扫描已归档的 82 家 `RiskCase.evidence` 重建标准 `fetched` 结构，使本地测试库与内网 QuickBI 扫描任务都能一致复用 7 大核验算子。

---

## 二、声明式配置扩展（`backend/risk_knowledge_data.json`）

在 `backend/risk_knowledge_data.json` 新增两段纯 JSON 配置，并在 `backend/risk_knowledge.py` 暴露读取接口：

1. **`letter_regulatory_floors`（P0 硬底线规则，支持后续只改 JSON 扩充）**：
   - 拨备覆盖率（`TEST_METRIC_097`）`< 150%`；
   - 资本充足率（`TEST_METRIC_013`）`< 10.5%`（或 `< 11.0%` 且排全辖后 10 名或较年初下降）；
   - 一级资本充足率（`TEST_METRIC_015`）`< 8.5%`；核心一级资本充足率（`TEST_METRIC_017`）`< 7.5%`；
   - 不良贷款“双升”（`TEST_METRIC_024 > 0` 且 `TEST_METRIC_028 > 0`），或不良贷款率（`npl_ratio`）`>= 2.0%` 且较年初上升或排全辖后 25 名；
   - 存在有效资金业务不良余额（`funds_npl_balance > 0`）；
   - 净息差（`TEST_METRIC_095`）`< 1.8%`；
   - 净利润同比负增长（`TEST_METRIC_091 < 0`）。
2. **`letter_caution_guards`（良性防误报提示）**：
   - 当 `TEST_METRIC_073 < 35` 且无有效资金业务不良（`funds_npl_balance <= 0` 或缺失）时：提示该行资金业务低占比且无资金不良，增速排后不构成资金业务扩张风险，切勿误写；
   - 当 `TEST_METRIC_091 > 0` 时：提示净利润同比为正增长，不得表述为“净利润下滑”；若排名全辖尾部仅可客观提示“盈利增速居全辖末位”。

---

## 三、工具链契约（只读 MCP 总线 + 弹性终稿提交）

### 3.1 扩展只读 MCP 工具（`backend/mcp_server.py`，纳入 `READ_ONLY` 与 `READ_OPERATIONS` 预算隔离，兼容既有 4 工具及全部已有测试）

所有只读工具严格由宿主环境变量 `RISK_TASK_RUN_ID` + `RISK_TASK_ORG_ID` 绑定任务与当前行社范围，不接受模型跨行社或跨任务篡改：

1. **`fetch_org_metrics(dimension_id?: str, indicator_ids?: list[str], keyword?: str, include_history: bool = False)`**
   - 返回当前行社清洗后（去除 `【…】` 内部审校标记）的有效实测指标列表，每项含 `indicator_id`, `indicator_name`, `dimension_id`, `dimension_title`, `aspect`, `meaning`, `value`, `value_display`, `unit`, `data_date`, `rank`, `population`, `evidence_id`；`include_history=True` 时一并返回该指标近多期月末历史序列。
   - 自动覆盖 `risk_knowledge` 中任意新增维度与指标。
2. **`check_risk_rules()`**
   - 基于声明式配置 `letter_regulatory_floors`、`letter_caution_guards` 以及本次 05 扫描归档的 `case.analysis["signals"]` / `issues` 计算并返回：
     - `mandatory_p0_clues`：命中的 P0 硬底线列表（含 `rule_id`, `label`, `indicator_id`, `indicator_name`, `value_display`, `evidence_id`, `related_metrics`）；
     - `scan_signals`：本次扫描命中的异常信号（自动剔除被 `letter_caution_guards` 判为良性的伪信号，自动保留任意新维度信号）；
     - `cautions`：良性防误报口径提醒列表。
3. **`run_verified_operator(operator: str, params: dict, focus_current_org: bool = True)`**
   - 直接调用 `backend/verification_layer.run_verified_operator(fetched, INDICATORS, operator, params, validation=...)`；
   - 其中 `fetched` 通过 `fetched_from_scan_archive(db, run)` 从本次扫描已归档的 82 家观测重建（本地快照与 QuickBI 归档统一可用）；
   - 完整保留 `status`, `result_type`, `operator`, `summary`, `checks_passed`, `caveats`, `assumptions`, `error`, `coverage`；
   - 当 `focus_current_org=True` 时，`rows` 优先聚焦当前行社对应记录（以及 `bucket_distribution` / `concentration_cr` 的聚合分桶/汇总行），`evidence_ids` 仅保留当前行社相关观测 ID 与必要溯源信息，避免 82 行全量撑爆上下文或泄露无关行社明细。

### 3.2 Pi Agent 终稿提交工具（`submit_risk_letter`）

- **弹性结构**：
  - `title`：`关于{行社全称}{期次}经营与风险情况的风险提示函`
  - `recipient`：`{行社全称}：`
  - `opening`：直击核心痛点的导语（1 段）
  - `overview`：基本经营盘定调（资产/贷款/存款及增幅，缺数时可为空）
  - `risk_sections`：**`1～4` 段**（模型自主聚类、排序、动态拟题，每项含 `heading`, `body`, `evidence_ids`）
  - `suggestions`：**`1～5` 条**（紧扣上文痛点的定制整改举措，每项含 `heading`, `body`，**不强制与 `risk_sections` 数量 1:1 相等**）
  - `closing_requirement`：主要负责人牵头建账、15 个工作日内书面报送风险管理部的闭环要求
  - `appendix_evidence_ids`：**`1～18` 项**真实实测观测 ID（由宿主渲染为干净 Markdown 附表）

---

## 四、5 条不可妥协护栏与离线 `demo` 去硬编码

1. **先查后引（Read-Before-Cite）**：`risk_sections[*].evidence_ids` 与 `appendix_evidence_ids` 中的每一项必须同时满足：① 属于本行社有效观测白名单；② 必须是本轮调用工具实际读取到的观测 ID（含起步简报中的 `business_overview`）。
2. **P0 底线零漏报**：`mandatory_p0_clues` 中每一项的 `evidence_id` 必须至少出现在 `risk_sections` 任意一段的 `evidence_ids` 中（不限制放在第几段、如何与其他风险合并）。
3. **实测与推演严格隔离**：`stress_test` 产出的 `result_type="simulated"` 数值绝不得进入 `appendix_evidence_ids`；正文若引用情景测算结果，必须显式说明假设前提，严禁冒充当期实测值。
4. **公文洁净度红线**：正文所有字段严禁包含 32 位十六进制哈希、`TEST_METRIC_*`、内部字段名、`（测试草稿）`、`成因分析：`、`核查建议：`、`证据：`、`原始指标记录：`、`待核】`、换行符或 `|<>` 控制字符。
5. **离线 `demo` 去硬编码**：彻底删除 `responseFor()` 中的 8 专题 `switch` 和 `throw Error('未识别的风险专题')`。离线 `demo` 模式同样按真实工具顺序调用 `check_risk_rules` → `fetch_org_metrics` → `run_verified_operator` → `submit_risk_letter`，并基于返回的维度名、线索说明、指标实测值及知识库 `check_suggestion` 动态拼装风险段与建议，确保新增任意未知风险维度均不崩溃。

---

## 五、运行预算、前端交互与验收要求

- **运行预算**：单行社撰函默认 `maxRequests: 10`、`maxTools: 16`、`timeoutMs: 120000`（受环境变量覆盖）、思考模式下 `maxTokens: 8000`（受模型上限约束）。
- **执行轨迹**：`06 提示函撰写 · {行社名}` 完整记录每一轮思考链及 `check_risk_rules` / `fetch_org_metrics` / `run_verified_operator` / `submit_risk_letter` 工具调用参数、耗时与引用证据。
- **前端快捷跳转**：在「报告草稿」页头（草稿存在且有撰函轨迹时）提供 **「查看撰函轨迹」** 按钮，点击直接切到「执行轨迹」并选中 `letter:{org_id}`。
- **验收测试**：
  1. 弹性段数自由成立（验证 `risk_sections` 与 `suggestions` 数量可以不同）；
  2. 未查先引被拦截、P0 底线漏报被拦截、禁用词/哈希被拦截；
  3. 模拟新增未知风险维度（如 `liquidity_risk` / `rural_micro_positioning`）在 `demo` 与 `model` 契约下均正常运行不抛错；
  4. MCP 工具 `run_verified_operator` 完整复用 `verification_layer.py` 且不破坏既有 `test_services.py` 等全部回归测试。
