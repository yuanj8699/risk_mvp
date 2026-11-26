# 代码核验层架构与算子规格说明书（v1.1）

> 适用范围：`risk_agent_mvp` 后端确定性数据计算与口径核验层。  
> 定位：承接“取数 + 代码核验”两步法中的**第二步**——建立 **L1 基础原语层（`backend/verification_primitives.py`）+ L2 复合分析算子层（`backend/verification_layer.py`）** 两层确定性计算体系，将问数引擎（小 Q / QuickBI）或本地指标快照返回的原子观测事实，经标准化入账、口径体检、标准算子计算与后置恒等核验，输出**高精度、可追溯、防漂移**的结构化证据包供大模型解读与拟函引用。

---

## 一、总体架构与设计边界

### 1.1 四阶段流水线

```text
[原子取数: LocalSnapshotFetcher / QuickBiOlapFetcher / SmartQ明细]
        │  只取最小原子观测，不让自然语言问数引擎做复杂跨期/复合计算
        ▼
[Stage 1 · 标准化入账 Normalize]
  统一输入契约、Decimal 解析、保留 evidence_id / quality_status / unit / data_date
        ▼
[Stage 2 · 口径体检与硬护栏 Pre-Check]
  复用 backend/metric_validator.py：缺数不补零、单位与期次对齐、指标类型判定、裁剪范围阻断
        ▼
[Stage 3 · 确定性计算层 Compute]
  主轨（80% 高频）：7 类标准分析算子（纯 Python 参数化函数 + 单元测试）
  辅轨（20% 长尾）：隔离沙箱自定义脚本（沿用现有 Docker/Python 沙箱与 manifest 校验）
        ▼
[Stage 4 · 后置恒等复核与证据封装 Post-Verify]
  数学恒等校验 + evidence_id 溯源校验 + 统一 Result Contract JSON 输出
```

### 1.2 核心架构约束（必须遵守）
1. **复用现有契约，不重复造轮子**：
   - 输入事实直接兼容 [data_fetcher.py](../backend/data_fetcher.py) 返回的 `fetched` 结构（`organizations`、`indicators`、`latest`、`history`、`missing`、`missing_source`、`source_totals`、`visibility`）。
   - 口径前置体检直接复用 [metric_validator.py](../backend/metric_validator.py) 的 `validate_fetched_metrics()`，不在算子内重复实现一套单位/加权率校验逻辑。
   - 指标定义直接读取 [risk_knowledge.py](../backend/risk_knowledge.py) 的 `INDICATORS`（含 `metric_kind`、`numerator_id`、`denominator_id`、`unit`、`unit_definition`），同时支持调用方传入临时补充定义。
2. **高精度十进制计算**：所有数值运算统一采用 `Decimal`，展示格式化沿用 `services.number()`，禁止用 Python `float` 做金额加总、占比与偏离运算。
3. **事实不造假、缺数不补零**：任何 `value is None`、`usable is False`、`quality_status != "valid"` 或单位冲突的记录，一律剔除并记录进 `coverage.excluded_orgs`（附原因），严禁补零。
4. **实测与推演严格隔离**：前 6 类基于观测事实派生的算子标记 `result_type = "observed_derived"`；第 7 类压力测算强制标记 `result_type = "simulated"` 并附带 `assumptions`，防止下游误作历史实测值写入正式风险提示函。
5. **不破坏既有接口与测试**：保持现有 API、MCP 既有 4+ 个工具签名及全部既有测试完全兼容；核验层作为共享模块 `backend/verification_layer.py` 实现，同时提供清晰的服务入口供 API / MCP / 沙箱脚本调用。

---

## 二、统一输入与输出契约（Result Contract）

### 2.1 统一入口签名

```python
def run_verified_operator(
    fetched: dict,
    metric_definitions: dict[str, dict],
    operator: str,
    params: dict,
    *,
    validation: dict | None = None,
) -> dict:
    ...
```

- `fetched`：符合 `DataFetcher.fetch()` 协议的标准取数字典。
- `metric_definitions`：指标定义字典（如 `risk_knowledge.INDICATORS` 或其子集/扩展）。
- `operator`：7 个注册算子名之一（见第三章）；未知算子返回 `status="blocked"`、`error.code="UNKNOWN_OPERATOR"`。
- `params`：对应算子的强类型参数字典（非法参数返回 `status="blocked"`、`error.code="INVALID_PARAMS"`）。
- `validation`：可选，若调用方已运行 `validate_fetched_metrics(fetched, metric_definitions)` 可直接透传复用，否则内部自动执行一次（且不修改调用方原始 `fetched` 引用，需做好深/浅拷贝隔离）。

### 2.2 统一返回 Schema

```json
{
  "status": "ok | blocked | insufficient_data",
  "result_type": "observed_derived | simulated",
  "operator": "consecutive_trend",
  "params": {},
  "as_of": "2026-09-30",
  "coverage": {
    "total_orgs": 82,
    "valid_orgs": 80,
    "excluded_orgs": [
      {"org_id": "ORG_01", "org_name": "某农商行", "reason": "缺失 2026-06-30 期次，连续性中断"}
    ]
  },
  "summary": {},
  "rows": [],
  "checks_passed": ["unit_aligned", "no_null_as_zero", "evidence_traceable"],
  "caveats": [],
  "assumptions": [],
  "error": null,
  "evidence_ids": ["obs_1001", "obs_1002"]
}
```

字段约束：
- `status == "blocked"` 时，`error` 必须为 `{"code": "<稳定错误码>", "message": "<中文业务说明>"}`，`rows` 为空列表。
- `status == "insufficient_data"` 时，表示输入无违规但可用数据不足以得出结论（如历史期次不足 N 期、有效机构为 0），`caveats` 与 `summary` 须说明原因。
- `evidence_ids`：去重、稳定排序后的原始观测 ID 列表，**每一项必须真实存在于输入 `fetched["latest"]` 或 `fetched["history"]` 中**，由 Stage 4 后置核验器强制断言。
- 所有数值输出字段统一为可 JSON 序列化的十进制字符串（经 `number()` 规范化）或整数字段（计数/排名/期数）。

---

## 三、7 大标准分析算子规格

### 算子 1：`consecutive_trend`（连续 N 期恶化/改善判断）

- **业务场景**：识别“连续 3 个季度不良率上升”“连续 4 个月净息差下滑”的行社。
- **入参 `params`**：
  - `indicator_id: str`（必填）
  - `n_periods: int`（必填，`>= 2`，表示连续期数，例如 3 表示连续 3 个期次，即至少 2 次相邻跃迁）
  - `direction: Literal["up", "down"]`（必填，`"up"` 表示连续上升，`"down"` 表示连续下降）
  - `strict: bool = True`（`True` 要求相邻每期严格 `>` / `<`；`False` 允许 `>=` / `<=` 且首尾总体必须严格变化）
  - `org_ids: list[str] | None = None`（可选，限定分析行社子集）
  - `as_of: str | None = None`（可选，ISO 日期，截断不晚于该日的数据）
- **核心口径与防坑规则**：
  1. 数据源取自 `fetched["history"]`（合并 `latest` 中同指标有效记录，按 `(org_id, data_date)` 去重并优先 `latest`）。
  2. **严禁跳期拼接**：
     - 先提取本次有效数据中所有机构在 `<= as_of` 范围内出现的唯一 `data_date` 升序序列，作为全辖观察期次轴 `period_axis`；若指标元数据声明 `frequency` 为 `"季"`/`"月"`/`"日"`，额外校验最近 `n_periods` 个锚点期次在日历上属于相邻周期（季频相邻差 1 季，月频相邻差 1 月，日频允许交易日/工作日连续但如出现跨多月断层需记录 caveat）；
     - 若 `len(period_axis) < n_periods`，返回 `status="insufficient_data"`，说明实际仅有几期。
     - 对每家行社，只检查 `target_periods = period_axis[-n_periods:]` 这 `n_periods` 个固定期次；只要该行社在 `target_periods` 中**任意一期缺失或不可用**，直接记入 `coverage.excluded_orgs`（原因：`缺失 <date> 期次，连续性中断`），**绝不跳过缺期拿更早期次拼凑**。
  3. 对完整覆盖 `target_periods` 的行社逐相邻对比较，全部满足方向条件则命中。
- **输出**：
  - `summary`：`{"indicator_id", "target_periods": [...], "direction", "strict", "matched_count"}`
  - `rows`：仅返回命中行社，按总变化幅度降序（`up`）/ 升序（`down`），每行含 `org_id`, `org_name`, `region_name`, `series`（每期 `{data_date, value, evidence_id}`）, `step_changes`（相邻期差值列表）, `total_change`, `evidence_ids`。
- **后置校验**：`checks_passed` 包含 `"period_contiguous"`；每条命中记录 `len(series) == n_periods` 且相邻差值符号全部符合 `direction` 与 `strict`。

---

### 算子 2：`benchmark_deviation`（对标均值/中位数/基准偏离）

- **业务场景**：筛查“不良率高于全省平均”“净息差低于全辖中位数超过 0.2 个百分点”的行社。
- **入参 `params`**：
  - `indicator_id: str`（必填）
  - `baseline: Literal["auto", "source_province_total", "province_weighted", "median", "arithmetic_mean", "custom"] = "auto"`
  - `custom_value: str | int | float | None = None`（仅 `baseline="custom"` 时必填）
  - `direction: Literal["above", "below", "both"] = "both"`
  - `min_deviation: str | int | float | None = None`（可选，筛选 `abs(deviation) >= min_deviation`）
  - `allow_rate_arithmetic_mean: bool = False`（默认 `False`）
- **核心口径与防坑规则**：
  1. **裁剪范围护栏**：当 `visibility.clipped == True` 且 `baseline` 依赖全辖基准（`source_province_total`、`province_weighted`，或 `auto` 需要全辖基准且无显式 `custom`）时，返回 `status="blocked"`, `error.code="CLIPPED_SCOPE"`。
  2. **率类指标禁用简单平均**：
     - 若 `metric_kind == "rate"` 且调用方显式指定 `baseline="arithmetic_mean"` 且 `allow_rate_arithmetic_mean is False`，直接返回 `status="blocked"`, `error.code="INVALID_METHOD_FOR_RATE"`（说明率类指标禁止简单平均，请改用全辖加权值、源表合计或中位数）。
     - 若 `metric_kind == "rate"` 且 `baseline="auto"`，按以下顺序回退选择基准（复用 `validation["accounting_details"]` 的核验结果）：
       1. `source_province_total`（源表全辖合计行）；
       2. `province_weighted`（`weighted_value`，完整同日同单位分子分母重算）；
       3. `median`（全辖有效行社中位数），并在 `caveats` 中强制注明：“缺少源表全辖合计及完整分子分母，已改用行社中位数对标，未使用简单平均”。
     - 若 `metric_kind in {"amount", "delta"}` 且 `baseline="auto"`，默认取全辖有效行社 `arithmetic_mean`（同时在 `summary` 附带 `median` 参考）。
  3. 精确计算 `deviation = value - baseline_value`；若 `baseline_value != 0` 且 `metric_kind == "amount"`，额外给出 `deviation_pct = deviation / abs(baseline_value) * 100`。
- **输出**：
  - `summary`：`{"indicator_id", "unit", "baseline_method", "baseline_value", "matched_count"}`
  - `rows`：命中行社按偏离绝对值降序排列，含 `org_id`, `org_name`, `region_name`, `value`, `baseline_value`, `deviation`, `side` (`"above"|"below"|"equal"`), `data_date`, `evidence_id`。
- **后置校验**：`checks_passed` 包含 `"rate_no_simple_average"`（针对率类）与 `"deviation_exact"`（每行满足 `Decimal(value) - Decimal(baseline_value) == Decimal(deviation)`）。

---

### 算子 3：`bucket_distribution`（区间分桶分布）

- **业务场景**：统计“不良率在 1% 以下、1%～1.5%、1.5%～2%、2% 以上各有多少家，分别占比多少”。
- **入参 `params`**：
  - `indicator_id: str`（必填）
  - `bins: list[str | int | float]`（必填，严格递增的切分点列表，长度 `>= 1`）
  - `labels: list[str] | None = None`（可选，长度须等于 `len(bins) + 1`）
  - `right_closed: bool = False`（默认 `False`：左闭右开 `[a, b)`；`True`：左开右闭 `(a, b]`）
  - `include_org_ids: bool = True`
- **核心口径与防坑规则**：
  1. 校验 `bins` 全部可解析为 `Decimal` 且严格递增，否则返回 `status="blocked"`, `error.code="INVALID_PARAMS"`。
  2. 生成 `len(bins) + 1` 个连续互斥区间，显式输出每个桶的数学区间表达式（如 `(-∞, 1.0)`、`[1.0, 1.5)`、`[1.5, 2.0)`、`[2.0, +∞)`）。
  3. 缺失/不可用/单位异常行社**绝不落入任何数值桶**，单独归入 `summary.missing_bucket`（含 `count` 和 `excluded_orgs`）。
- **输出**：
  - `summary`：`{"indicator_id", "unit", "right_closed", "valid_count", "missing_count", "total_count"}`
  - `rows`：各数值分桶列表，含 `label`, `range_expr`, `count`, `ratio_of_valid`（占有效家数 %，保留 2 位小数）, `ratio_of_total`（占总行社家数 %）, `org_ids`（可选）, `evidence_ids`。
- **后置校验**：强校验 `sum(row["count"] for row in rows) == coverage["valid_orgs"]` 且 `coverage["valid_orgs"] + len(coverage["excluded_orgs"]) == coverage["total_orgs"]`，记录 `"bucket_sum_matches_total"`。

---

### 算子 4：`concentration_cr`（头部集中度 CRn）

- **业务场景**：计算“前 5 家、前 10 家行社的不良贷款余额合计占全辖比重（CR5 / CR10）”。
- **入参 `params`**：
  - `indicator_id: str`（必填）
  - `top_n: int | list[int]`（必填，所有 `n >= 1`，支持单值如 `5` 或多值如 `[5, 10]`）
  - `allow_partial_denominator: bool = False`（默认 `False`，要求全辖完整有效覆盖）
- **核心口径与防坑规则**：
  1. **指标类型护栏**：仅允许 `metric_kind == "amount"`；对 `rate` / `delta` 返回 `status="blocked"`, `error.code="INVALID_METRIC_KIND"`。
  2. **范围与完整性护栏**：`visibility.clipped == True` 一律返回 `CLIPPED_SCOPE`；若存在缺数/多日期混杂导致有效覆盖不完整，默认返回 `INCOMPLETE_COVERAGE`（仅当 `allow_partial_denominator=True` 且有效行社同日期时允许按可见有效合计计算并强制写入 caveat）。
  3. **负值与非正合计护栏**：若任一有效行社数值 `< 0`，返回 `status="blocked"`, `error.code="NEGATIVE_VALUE_NOT_ALLOWED"`；若总和 `<= 0`，返回 `NON_POSITIVE_TOTAL`。
  4. **排序与并列声明**：按 `(-Decimal(value), org_id)` 稳定排序；若第 `n` 名与第 `n+1` 名数值相等，必须在 `caveats` 注明边界存在并列。
  5. 若请求的 `n > valid_orgs`，截断至 `valid_orgs` 并在 `caveats` 说明。
- **输出**：
  - `summary`：`{"indicator_id", "unit", "total_value", "cr_values": {"CR5": "42.35", "CR10": "63.10"}}`
  - `rows`：按升序 `n` 输出每个 CRn 结果对象，含 `n`, `effective_n`, `top_sum`, `total_value`, `cr_pct`, `has_boundary_tie`, `top_orgs`（每家含 `rank`, `org_id`, `org_name`, `value`, `share_pct`, `evidence_id`）。
- **后置校验**：验证每个 `0 <= cr_pct <= 100`、多个 `n` 时 `cr_pct` 单调不减、且 `sum(top_orgs.value) == top_sum`，记录 `"concentration_bounds_valid"`。

---

### 算子 5：`incremental_contribution`（增量贡献度/拉动率）

- **业务场景**：计算“前 5 家行社不良增量占全辖总增量的比例”“各行社对全辖贷款增量的贡献度”。
- **入参 `params`**：
  - 两种取增量模式（二选一）：
    - 模式 A（直接增量列）：`indicator_id: str`（指向已有 `delta` 类增量指标，如“五级不良贷款余额比年初增量”）
    - 模式 B（两期作差）：`base_indicator_id: str`（须为 `amount` 类）+ `from_date: str` + `to_date: str`（从 `history`/`latest` 取同机构两期有效值计算 `to_value - from_value`）
  - `top_n: int = 5`
  - `mode: Literal["auto", "net", "positive_only"] = "auto"`
  - `offset_ratio_threshold: str = "0.2"`（净增量绝对值低于总变动绝对值之和 `pos_total + abs(neg_total)` 的 20% 时判定为“显著正负抵消”）
- **核心口径与防坑规则**：
  1. **范围护栏**：`visibility.clipped == True` 一律返回 `CLIPPED_SCOPE`。
  2. **正负抵消护栏**：
     - 计算 `net_total = sum(delta)`、`positive_total = sum(d for d in deltas if d > 0)`、`negative_total = sum(d for d in deltas if d < 0)`、`gross_abs_total = positive_total + abs(negative_total)`。
     - 若 `gross_abs_total == 0`（全辖全部无变动），返回 `status="insufficient_data"`。
     - 判定显著抵消：`significant_offset = (net_total == 0) or (abs(net_total) < Decimal(offset_ratio_threshold) * gross_abs_total)`。
     - `mode == "net"`：若 `net_total == 0` 返回 `status="blocked"`, `error.code="ZERO_NET_TOTAL"`；否则按 `delta / net_total * 100` 计算，若 `significant_offset` 为真则强制添加 caveat 警示正负抵消导致占比失真。
     - `mode == "auto"`：无显著抵消时采用 `effective_mode = "net"`；有显著抵消时自动切换 `effective_mode = "positive_only"`（若 `positive_total > 0`，计算正增量行社 `delta / positive_total * 100`，并附 caveat 说明切换原因；若无正增量则按负向降幅说明）。
     - `mode == "positive_only"`：仅对 `delta > 0` 的行社计算占 `positive_total` 的比例；若 `positive_total == 0` 返回 `status="insufficient_data"`。
- **输出**：
  - `summary`：`{"effective_mode", "significant_offset", "net_total", "positive_total", "negative_total", "top_n", "top_n_contribution_pct", "unit"}`
  - `rows`：按增量降序返回 Top N（或按模式排序）行社明细，含 `org_id`, `org_name`, `region_name`, `delta_value`, `contribution_pct`, `evidence_ids`（模式 B 包含期初期末两条 ID）。
- **后置校验**：`effective_mode == "net"` 时全部有效行社贡献度之和在 `100 ± 0.05%` 内；`effective_mode == "positive_only"` 时全部正增量行社贡献度之和在 `100 ± 0.05%` 内，记录 `"contribution_sum_reconciled"`。

---

### 算子 6：`metric_divergence`（跨指标背离/剪刀差筛查）

- **业务场景**：
  - 分位背离：“贷款增速排前 20%，但拨备覆盖率排后 20%”；
  - 组合红线背离：“不良率＜1.5%，但关注类贷款率＞3%”；
  - 剪刀差：“普惠贷款增速 - 各项贷款增速＜0”。
- **入参 `params`**：
  - `indicator_a: str`（必填）
  - `indicator_b: str`（必填，不能与 `indicator_a` 相同）
  - `rule_type: Literal["quantile_opposite", "threshold_combo", "spread"]`（必填）
  - `allow_date_mismatch: bool = False`
  - 规则专属参数：
    - `quantile_opposite`：`a_side` (`"top"|"bottom"`), `a_pct` (`0 < pct <= 100`), `b_side` (`"top"|"bottom"`), `b_pct` (`0 < pct <= 100`)
    - `threshold_combo`：`a_op`, `a_value`, `b_op`, `b_value`（操作符支持 `>`, `>=`, `<`, `<=`, `==`, `!=`）
    - `spread`：`spread_op`, `spread_value`（计算 `spread = value_a - value_b`）
- **核心口径与防坑规则**：
  1. 仅对同时拥有 `indicator_a` 和 `indicator_b` 有效观测的行社做内连接；任一缺失记入 `coverage.excluded_orgs`。
  2. 期次对齐校验：默认要求同机构两指标 `data_date` 一致；不一致的行社剔除并说明，仅当 `allow_date_mismatch=True` 时放行并在 `caveats` 注明。
  3. `spread` 模式强制要求两指标**单位兼容**（如均为 `%` 或均为同金额单位），单位不同返回 `status="blocked"`, `error.code="UNIT_MISMATCH_FOR_SPREAD"`。
  4. `quantile_opposite` 模式中，若 `visibility.clipped == True` 需返回 `CLIPPED_SCOPE`（局部视图不能算全辖分位）；分位边界采用 `ceil(valid_count * pct / 100)` 确定前/后 K 家，若边界存在同值并列，须在 `caveats` 说明并采用“并列同入”口径避免任意裁切。
- **输出**：
  - `summary`：`{"indicator_a", "indicator_b", "rule_type", "matched_count", "paired_org_count"}`
  - `rows`：命中行社清单，含 `org_id`, `org_name`, `region_name`, `value_a`, `unit_a`, `date_a`, `value_b`, `unit_b`, `date_b`, `spread`（如适用）, `rank_a`/`rank_b`（如适用）, `evidence_ids`（两条）。
- **后置校验**：所有命中行社均同时具备 A、B 两条真实有效观测且满足给定规则表达式，记录 `"divergence_pair_verified"`。

---

### 算子 7：`stress_test`（情景假设与压力测算）

- **业务场景**：
  - 信用冲击：“若各行社不良贷款余额增加 20%（或增加 X 亿元），拨备覆盖率将降至多少？哪些行社将跌破 150% 监管稳健线？”
  - 利率冲击：“若债券收益率曲线平行上移 10bp，各行社预计公允价值变动损失是多少？”
- **入参 `params`**：
  - `formula_id: Literal["npl_shock_provision_coverage", "rate_shock_bond_loss"]`（必填，严格白名单，严禁 `eval`）
  - `org_ids: list[str] | None = None`
  - 公式专属参数：
    - `npl_shock_provision_coverage`：
      - `npl_balance_indicator_id: str`（不良贷款余额指标 ID）
      - `provision_coverage_indicator_id: str`（拨备覆盖率指标 ID，单位 `%`）
      - `provision_balance_indicator_id: str | None = None`（可选，若提供则直接用拨备余额；否则由 `不良余额 * 拨备覆盖率 / 100` 反推当前拨备余额并在 `assumptions` 声明）
      - `shock_type: Literal["pct", "amount"]` + `shock_value: str | int | float`（要求 `> 0`）
      - `warning_line: str = "150"`（监管稳健参考线 %）
      - 计算公式：
        - `shocked_npl = npl_balance * (1 + shock_value / 100)`（`pct`）或 `npl_balance + shock_value`（`amount`）
        - `shocked_coverage = provision_balance / shocked_npl * 100`（前提：拨备余额不变、原 `npl_balance > 0`）
        - `coverage_drop_pp = shocked_coverage - current_coverage`
        - `breaches_warning_line = shocked_coverage < warning_line`
    - `rate_shock_bond_loss`：
      - `bond_balance_indicator_id: str`（本币债券余额/市值指标 ID，金额类）
      - `duration_indicator_id: str`（加权平均修正久期指标 ID，非负数值）
      - `shift_bp: str | int | float`（收益率曲线平行变动基点数，如 `5`、`10`、`20`）
      - 计算公式（一阶修正久期近似）：
        - `estimated_pnl = -bond_balance * duration * (shift_bp / 10000)`
        - `estimated_loss = max(Decimal("0"), -estimated_pnl)`
- **核心口径与防坑规则**：
  1. `result_type` 强制固定为 `"simulated"`，并在 `assumptions` 中写明全部简化假设（如“假设拨备余额维持不变”“采用一阶修正久期线性近似，未计入凸性影响”）。
  2. 任一依赖指标缺失、单位不匹配、或出现非法值（如原不良余额 `<= 0`、久期 `< 0`）的行社，记入 `coverage.excluded_orgs`，绝不补零估算。
  3. 严禁修改输入 `fetched` 或数据库状态。
- **输出**：
  - `summary`：冲击参数、有效测算家数、汇总指标（如 `newly_breached_count` / `total_breached_count` 或 `total_estimated_loss`）。
  - `rows`：按风险冲击严重程度排序的行社明细（含基准观测值、测算后值、变动值、是否跌破参考线、`evidence_ids`）。
- **后置校验**：强制断言 `result_type == "simulated"`、`len(assumptions) >= 1`，并逐行复算验证公式结果与输出一致，记录 `"simulation_isolated_and_verified"`。

---

## 四、系统接入点设计

为保持架构简洁、避免过度封装，接入点限定为以下两处（均为只读）：

1. **共享业务核心模块**：`backend/verification_layer.py`
   - 导出 `OPERATORS` 注册表、`run_verified_operator(...)`、`verify_operator_result(...)` 以及 7 个算子函数；
   - 直接可供后端服务、沙箱/分析链路及单元测试复用。
2. **只读业务入口（按最小侵入原则接入）**：
   - **HTTP 只读接口**：在后端提供只读核验路由 `POST /api/runs/{run_id}/verify`（请求体包含 `operator: str`, `params: dict`, `indicator_ids: list[str] | None = None`），按 `run_id` 加载该任务已冻结的 `TaskContext`（复用 `services.load_task_context` 与 `LocalSnapshotFetcher` / 对应快照范围），执行 `run_verified_operator` 并返回统一 Result Contract JSON。
   - **MCP 只读工具（可选增强，须保证兼容）**：若接入 `backend/mcp_server.py`，需遵守 `READ_ONLY` 注解、预算中间件 `READ_OPERATIONS` 以及任务范围隔离（`RISK_TASK_RUN_ID` / `RISK_TASK_ORG_ID`），且**不得破坏既有测试对 MCP 工具列表或权限边界的断言**。

---

## 五、交付物与验收标准（Codex CLI / `gpt-6.2-astra` 实现与测试闭环，主控代理复核验收）

### 5.1 交付物清单
1. `risk_agent_mvp/backend/verification_layer.py`：四阶段核验流水线、7 大标准分析算子、后置恒等核验器。
2. 只读 API 路由接入（`POST /api/runs/{run_id}/verify`，可放在合适路由模块并挂载到 `backend/app.py`），严格遵守任务冻结上下文与只读边界；如同时接入 `backend/mcp_server.py`，确保完全兼容既有测试。
3. 针对性自动化测试 `risk_agent_mvp/tests/test_verification_layer.py`：聚焦拦住真实业务错误，不写机械凑数测试。

### 5.2 逐项验收标准（Codex CLI 须自测并逐项回报测试结果，由主控代理最终复核）
- [ ] **AC-1 契约与精度**：所有 7 个算子统一经 `run_verified_operator` 调用，返回完整 Result Contract 字段；数值计算全部使用 `Decimal`；`evidence_ids` 100% 溯源到真实输入观测且无伪造 ID。
- [ ] **AC-2 复用前置体检**：正确复用 `metric_validator.validate_fetched_metrics()`；缺数/质量不合格/单位异常观测一律进入 `excluded_orgs`，绝不补零；输入 `fetched` 字典不被原地破坏。
- [ ] **AC-3 连续 N 期防跳期**：验证 `consecutive_trend` 在正常连续上升/下降、期次不足、以及**中间断期缺数**场景下能准确剔除断期行社，不把跳期数据误判为连续趋势。
- [ ] **AC-4 率值防简单平均与裁剪阻断**：验证 `benchmark_deviation` 对率类指标拒绝 `arithmetic_mean`（`INVALID_METHOD_FOR_RATE`），`auto` 模式按“源表合计 → 分子分母重算 → 中位数+caveat”顺序回退；`visibility.clipped=True` 时正确阻断全辖基准对标。
- [ ] **AC-5 分桶恒等式**：验证 `bucket_distribution` 开闭区间边界、`missing_bucket` 独立统计，且 `有效分桶之和 + 缺失数 == 总机构数`。
- [ ] **AC-6 集中度护栏**：验证 `concentration_cr` 正常计算 CR5/CR10 且单调不减，拦截 `rate`/`delta` 类型、拦截负值与裁剪视图，并在边界并列时给出 caveat。
- [ ] **AC-7 增量贡献度正负抵消护栏**：验证 `incremental_contribution` 支持直接增量列与两期作差；在正常同向场景下贡献度之和为 100%；在**显著正负抵消**场景下 `auto` 自动切换 `positive_only` 并附 caveat，`net` 模式在 `net_total == 0` 时返回 `ZERO_NET_TOTAL`。
- [ ] **AC-8 跨指标背离校验**：验证 `metric_divergence` 的 `quantile_opposite`、`threshold_combo`、`spread` 三种模式；验证跨期默认剔除、不同单位拦截 `spread`、裁剪视图拦截全辖分位。
- [ ] **AC-9 压力测算隔离**：验证 `stress_test` 两种白名单公式（不良冲击拨备覆盖率、利率上移久期损失）计算精确，拒绝未知公式，`result_type == "simulated"` 且必带 `assumptions`。
- [ ] **AC-10 接口集成与全量回归**：`POST /api/runs/{run_id}/verify` 走通真实冻结上下文测试；运行 `risk_agent_mvp` 全部 Python 单元测试（`pytest`）全部通过，零回归。


---

## 六、L1 基础原语算子层规格（v1.1 新增：`backend/verification_primitives.py`）

> 定位：面向单值/明细列表/多期序列的轻量级确定性原子积木块，既供 L2 复合算子（`backend/verification_layer.py`）复用，也可供 API、业务规则、沙箱脚本直接调用，彻底消除“缺数补零、率值简单平均、混单位求和、基期为零算增速”等口径漂移。

### 6.1 统一异常与基本约束
- 异常类：`PrimitiveError(ValueError)`，携带稳定 `code: str` 与中文 `message: str`（如 `INVALID_NUMBER`、`UNIT_CONVERSION_ERROR`、`AMBIGUOUS_ROW`、`NON_ADDITIVE_KIND`、`INVALID_METHOD_FOR_RATE`、`DIVISION_BY_ZERO`、`INVALID_GROWTH_BASELINE`、`UNIT_MISMATCH`、`INVALID_PARAMS`）。
- 精度约束：全部数值计算基于 `Decimal`（内部运算上下文 `prec = 64`），禁止 `float` 参与金额/比率/偏离计算。
- 不可变约束：所有原语函数均为纯函数，不得原地修改调用方传入的 `rows` 列表或行字典。
- 有效行判定约定：当行字典包含 `usable` 或 `quality_status` 字段时，仅 `usable is not False`、`quality_status in (None, "valid")` 且目标值非空的记录视为有效观测；缺数/不可用记录一律不得静默补零。

### 6.2 七大类 L1 基础原语清单（共 33 个函数）

#### A. 数值解析、单位换算与格式化（4 个）
| Python 函数 | 对应概念名 | 签名与核心规则 |
| :--- | :--- | :--- |
| `num` | `num` | `num(value) -> Decimal`：严格解析有限十进制数；拒绝 `None`、空串、`bool`、`NaN`/`Inf`、非数、极端指数（`abs(adjusted) > 1000` 或有效位 `> 1000`），抛 `INVALID_NUMBER`。不做隐式单位缩放。 |
| `convert_unit` | `scale` / 换算 | `convert_unit(value, from_unit: str, to_unit: str) -> Decimal`：显式高精度换算，支持金额类（`元` / `万元` / `亿元`）互转、比率类（`小数` / `%` / `bp`）互转；同单位直返；跨类别或未知单位抛 `UNIT_CONVERSION_ERROR`。另提供 `scale(value, from_unit="元", to_unit="亿元")` 作为便捷别名。 |
| `pct` | `pct` | `pct(value, *, from_ratio: bool = False, digits: int = 2) -> str`：`from_ratio=True` 时先乘 100，按 `ROUND_HALF_UP` 保留指定小数位并带 `%`；拒绝空/非数。 |
| `format_value` | `format` | `format_value(value, unit: str = "", *, digits: int = 2, target_unit: str | None = None, signed: bool = False) -> str`：`value is None` 时显式返回 `"缺失"`（绝不输出 `"0"`）；指定 `target_unit` 时先调用 `convert_unit`；按单位规范化输出（如 `12.35亿元`、`1.65%`、`+0.25个百分点`）。 |

#### B. 取数、对齐与时间序列（6 个）
| Python 函数 | 对应概念名 | 签名与核心规则 |
| :--- | :--- | :--- |
| `pick_row` | `pickRow` | `pick_row(rows, **criteria) -> dict | None`：按精确键值匹配行；0 条命中返回 `None`；超过 1 条命中抛 `AMBIGUOUS_ROW`（防止静默取首条掩盖重复数据）。 |
| `pick_value` | `pickValue` | `pick_value(row, field: str = "value", *, allow_zero_default: bool = False, default_if_missing=None) -> Decimal | None`：行不存在、不可用或字段为空时**默认返回 `None`（严禁静默补 0）**；仅当调用方显式传 `allow_zero_default=True` 时返回 `num(default_if_missing if default_if_missing is not None else 0)`。 |
| `join_by_org` | `joinByOrg` | `join_by_org(rows_a, rows_b, *, same_date: bool = True, org_key: str = "org_id") -> dict`：按机构内连接两组有效观测，返回 `{"paired": [(a, b), ...], "excluded": [{"org_id", "reason"}, ...]}`；同组重复机构抛 `AMBIGUOUS_ROW`。 |
| `align_periods` | `alignPeriods` | `align_periods(rows, *, indicator_id: str | None = None, frequency: str | None = None, n_periods: int | None = None, as_of: str | None = None) -> dict`：提取全辖有效观测升序期次轴，校验 `季`/`月`/`日` 日历连续性，返回 `{"periods": [...], "gaps": [...], "is_contiguous": bool}`。 |
| `pair_period` | `pairPeriod` | `pair_period(rows, *, indicator_id: str | None = None, mode: Literal["yoy", "mom", "ytd_start", "prev_quarter"], as_of: str | None = None) -> dict`：按目标期次与基期日历规则（`yoy` 上年同月/季、`mom` 上月末、`ytd_start` 上年 12 月末、`prev_quarter` 上季末）为各机构配对 `(prev_row, curr_row)`，返回 `{"curr_date", "prev_date", "paired": [...], "excluded": [...]}`。 |
| `series_of` | `seriesOf` | `series_of(rows, *, org_id: str, indicator_id: str | None = None, periods: list[str]) -> list[dict]`：按固定 `periods` 轴输出该机构有序走势点，缺期显式输出 `{"data_date": d, "value": None, "evidence_id": None, "missing": True}`，不跳期压缩。 |

#### C. 条件筛选与异常识别（3 个）
| Python 函数 | 对应概念名 | 签名与核心规则 |
| :--- | :--- | :--- |
| `filter_where` | `filterWhere` | `filter_where(rows, conditions: list[dict], *, logic: Literal["and", "or"] = "and") -> dict`：每条条件为 `{"field": "value", "op": ">"|">="|"<"|"<="|"=="|"!="|"between"|"in"|"is_null"|"not_null", "value": ...}`（`between` 接收 `(low, high)` 且支持 `inclusive: bool = True`）。数值比较自动排除不可用/缺值行并写入 `excluded`，返回 `{"matched": [...], "excluded": [...]}`。 |
| `find_missing` | `findMissing` | `find_missing(rows, expected_orgs: list[str], *, indicator_id: str | None = None) -> list[dict]`：返回预期机构清单中缺失记录、值为空、`usable=False` 或 `quality_status != "valid"` 的机构与原因列表。 |
| `detect_sign_flip` | `detectSignFlip` | `detect_sign_flip(prev_rows, curr_rows, *, kind: Literal["any", "pos_to_neg", "neg_to_pos"] = "any") -> dict`：按机构配对前后两期有效值，仅识别严格异号翻转（`prev > 0 and curr < 0` 或 `prev < 0 and curr > 0`，零值不视为翻转），返回命中行（含起止值、方向、`evidence_ids`）及 `excluded`。 |

#### D. 统计聚合与多维分组（9 个）
| Python 函数 | 对应概念名 | 签名与核心规则 |
| :--- | :--- | :--- |
| `sum_values` | `sum` | `sum_values(rows, *, kind: str, value_key: str = "value") -> Decimal`：仅允许 `kind in {"amount", "delta"}`，否则抛 `NON_ADDITIVE_KIND`；存在多单位混杂时抛 `UNIT_MISMATCH`；仅汇总有效行，无有效行抛 `INSUFFICIENT_DATA`。 |
| `weighted_avg` | `weightedAvg` | `weighted_avg(rows_num, rows_den, *, scale_100: bool = True, same_date: bool = True) -> dict`：按机构内连接同日同单位分子分母后加总重算加权值（分母合计 `== 0` 抛 `DIVISION_BY_ZERO`），返回 `{"value", "numerator_sum", "denominator_sum", "paired_count", "excluded", "evidence_ids"}`。 |
| `avg` | `avg` | `avg(rows, *, kind: str, allow_rate_arithmetic_mean: bool = False, value_key: str = "value") -> Decimal`：`kind == "rate"` 且未显式允许时抛 `INVALID_METHOD_FOR_RATE`；要求同单位。 |
| `median_value` | `median` | `median_value(rows, *, value_key: str = "value") -> Decimal`：有效值精确十进制中位数（偶数取中间两数 `Decimal` 均值）；无有效行抛 `INSUFFICIENT_DATA`。 |
| `count_where` | `countWhere` | `count_where(rows, conditions_or_predicate, *, logic: Literal["and", "or"] = "and") -> int`：支持 `filter_where` 条件列表或 Python 谓词函数；只对有效行计数（谓词接收有效行，如显式检查缺失用 `filter_where` 的 `is_null`）。 |
| `distinct_count` | `distinctCount` | `distinct_count(rows, *, key: str = "org_id", valid_only: bool = True) -> int`：按字段去重计数（自动忽略 `None`）。 |
| `group_by` | `groupBy` | `group_by(rows, *, group_key: str) -> dict[str, list[dict]]`：按维度稳定分组，保留原始顺序，缺失分组键归入 `None` 或跳过（默认归入 `"未分组"` 并保持确定性键序）。 |
| `aggregate_by` | `aggregateBy` | `aggregate_by(rows, *, group_key: str, kind: str, agg: Literal["sum", "avg", "median", "count", "min", "max"] = "sum", allow_rate_arithmetic_mean: bool = False) -> list[dict]`：按分组执行受护栏保护的聚合（`sum` 禁 `rate`、`avg` 默认禁 `rate`），返回每组 `{group, value, valid_count, excluded_count, unit, evidence_ids}`。 |
| `rollup` | `rollup` | `rollup(rows, *, levels: tuple[str, ...] = ("province", "region_name", "org_id"), kind: str = "amount") -> dict`：按多层级自底向上汇总，返回各层级汇总及 `reconciled: bool`（验证各层级合计一致）。 |
| `rolling_agg` | `rollingAgg` | `rolling_agg(rows, *, org_id: str, indicator_id: str | None = None, window: int | None = None, agg: Literal["sum", "avg"] = "sum", kind: str = "amount", mode: Literal["rolling", "ytd"] = "rolling", frequency: str | None = None, as_of: str | None = None) -> dict`：`rolling` 模式要求最近 `window` 个全辖连续期次无断期；`ytd` 模式聚合目标年份内 `<= as_of` 的全部连续有效观测；断期或不足返回 `status="insufficient_data"` 而不跳期拼凑。 |

#### E. 排序、极值、分位与分布（7 个）
| Python 函数 | 对应概念名 | 签名与核心规则 |
| :--- | :--- | :--- |
| `extremum` | `extremum` | `extremum(rows, *, mode: Literal["max", "min"] = "max", value_key: str = "value") -> dict`：返回 `{"mode", "value", "rows": [...], "evidence_ids": [...]}`，**并列极值全部返回**。 |
| `top_n` | `topN` | `top_n(rows, *, n: int, order: Literal["desc", "asc"] = "desc", include_ties: bool = False, value_key: str = "value") -> dict`：按数值 + `org_id` 稳定排序取前 N，`include_ties=True` 时边界并列全部纳入，返回 `{"rows", "has_boundary_tie", "effective_n", "evidence_ids"}`。 |
| `rank_rows` | `rank` | `rank_rows(rows, *, order: Literal["desc", "asc"] = "desc", value_key: str = "value") -> list[dict]`：标准竞赛排名（并列 `1, 1, 3`），每行附带 `rank`、`population`、`tied: bool`。 |
| `percentile` | `percentile` | `percentile(rows, p, *, value_key: str = "value") -> Decimal`：精确 `Decimal` 线性插值分位数（`0 <= p <= 100`，遵循 R-7 / NumPy 默认线性插值公式：`idx = (n - 1) * p / 100`）。 |
| `percentile_rank` | `percentileRank` | `percentile_rank(rows, *, org_id: str, order: Literal["desc", "asc"] = "desc") -> dict`：返回该机构在有效总体中的竞赛排名、百分位（`top_pct = rank / population * 100`）与总体数。 |
| `bucket` | `bucket` | `bucket(rows, *, bins: list, labels: list[str] | None = None, right_closed: bool = False, expected_orgs: list[str] | None = None) -> dict`：严格递增分桶原语，缺失单独归入 `missing_bucket`，保证分桶计数恒等。 |
| `dispersion` / `find_outliers` | `dispersion` / 离群 | `dispersion(rows) -> dict` 返回 `count, min, max, range, mean, median, std_dev`（总体标准差，精确 `Decimal.sqrt()`）；`find_outliers(rows, *, k=2) -> dict` 返回偏离均值超过 `k * std_dev` 的离群行及上下阈值（`count < 2` 或 `std_dev == 0` 时安全返回空列表并说明原因）。 |

#### F. 比较与目标缺口（6 个）
| Python 函数 | 对应概念名 | 签名与核心规则 |
| :--- | :--- | :--- |
| `diff` | `diff` | `diff(a, b) -> Decimal`：精确差值 `num(a) - num(b)`。 |
| `compare_text` | `compareText` | `compare_text(a, b, *, unit: str = "", mode: Literal["delta", "benchmark"] = "delta", digits: int = 2) -> str`：确定性中文比较描述（`delta` 模式：`增加/减少/持平`；`benchmark` 模式：`高于/低于/等于基准`；`%` 差值自动用“个百分点”而非“%”）。 |
| `ratio` | `ratio` | `ratio(numerator, denominator, *, as_pct: bool = True) -> Decimal`：非结构性比率 `A ÷ B`（`as_pct=True` 时乘 100）；分母为 0 抛 `DIVISION_BY_ZERO`。 |
| `growth` | `growth` | `growth(current, baseline, *, as_pct: bool = True) -> Decimal`：变化率 `(curr - base) / base`；**当 `base <= 0` 时抛 `INVALID_GROWTH_BASELINE`**（防止零基数除零或负基数符号反转误导结论）。 |
| `delta_points` | `deltaPoints` | `delta_points(current_rate, baseline_rate, *, unit: Literal["pp", "bp"] = "pp") -> Decimal`：率值变化点（`pp` 为百分点差，`bp` 为基点差 `(curr - base) * 100`）。 |
| `gap_to_target` | `gapToTarget` | `gap_to_target(value, target, *, direction: Literal["gte", "lte"] = "gte") -> dict`：计算 `gap = value - target`、`shortfall`（未达标缺口绝对值，达标则为 `0`）、`met: bool` 及 `completion_pct`（仅当 `direction == "gte"` 且 `target > 0` 时给出，否则为 `None`）。 |

#### G. 校验原语（2 个）
| Python 函数 | 对应概念名 | 签名与核心规则 |
| :--- | :--- | :--- |
| `validate_sum` | `validateSum` | `validate_sum(rows, *, expected_total, kind: str = "amount", expected_count: int | None = None, tolerance=Decimal("0.01")) -> dict`：执行可加总类型校验 + 覆盖率校验（有效数 vs 预期数）+ 分项和与 `expected_total` 容差双校验，返回 `{passed, kind_valid, coverage_ok, sum_matches, valid_count, expected_count, coverage_ratio, actual_sum, expected_total, diff, issues}`。 |
| `validate_relation` | `validateRelation` | `validate_relation(payload: dict, *, relation: Literal["sum_equals", "ratio_equals", "order", "bounds"], tolerance=Decimal("0.01")) -> dict`：支持 4 类金融逻辑核对：<br>• `sum_equals`：`sum(parts) ≈ total`（差值 `<= tolerance`）<br>• `ratio_equals`：`rate ≈ numerator / denominator * 100`（分母为 0 直接判定失败）<br>• `order`：`left <op> right`（如 `总资产 >= 各项贷款`、`总逾期 >= 逾期90天以上`）<br>• `bounds`：`min_val <= value <= max_val`（如占比 ∈ `[0, 100]`、余额 ≥ `0`）。返回 `{passed, relation, diff, message}`。 |

### 6.3 L1 与 L2 复用关系
- `backend/verification_layer.py` 在不破坏任何既有外部契约与测试的前提下，复用 `backend/verification_primitives.py` 中的基础数值解析与通用计算原语（如 `num`、中位数/竞赛排名/期次连续性校验等合理复用点），避免两套代码逻辑分叉。

### 6.4 v1.1 补充验收标准（AC-11～AC-16）
- [ ] **AC-11 数值与单位原语**：`num` 拒绝空/`bool`/`NaN`/非数且不做隐式缩放；`convert_unit`/`scale` 精确支持 `元↔万元↔亿元` 及 `小数↔%↔bp`，拒绝跨类转换；`format_value(None, ...)` 返回 `"缺失"`。
- [ ] **AC-12 取数与不补零护栏**：`pick_row` 区分 0 命中与多命中歧义；`pick_value` 空值默认返回 `None` 绝不静默补 0，仅显式 `allow_zero_default=True` 返回默认零；`join_by_org`、`align_periods`、`pair_period`（同比/环比/较年初/较上季）、`series_of` 正确处理缺期与跨期剔除。
- [ ] **AC-13 筛选、翻转与聚合护栏**：`filter_where` 支持单/多条件与 `between`/`and`/`or` 且缺数不当 0；`find_missing` 抓全缺数与异常；`detect_sign_flip` 严格区分零值与异号翻转；`sum_values`/`aggregate_by` 拒绝 `rate` 求和与混单位求和；`avg` 默认拒绝 `rate`；`weighted_avg`、`median_value`、`count_where`、`distinct_count`、`rollup`、`rolling_agg`（含 `rolling` 防断期与 `ytd`）计算精确。
- [ ] **AC-14 排序、分位与离群**：`extremum` 并列全返回；`top_n` 支持稳定截断与并列同入；`rank_rows` 输出标准竞赛排名 `1, 1, 3`；`percentile`/`percentile_rank`/`bucket`/`dispersion`/`find_outliers`（含零方差安全降级）计算精确。
- [ ] **AC-15 比较、缺口与双校验**：`diff`/`compare_text`（率差自动用百分点）/`ratio`（防除零）/`growth`（`baseline <= 0` 阻断）/`delta_points`（`pp`/`bp`）/`gap_to_target` 行为准确；`validate_sum` 完成可加总/覆盖率/容差双校验；`validate_relation` 覆盖 4 类金融逻辑核对。
- [ ] **AC-16 L1/L2 共存与零回归**：`tests/test_verification_primitives.py` 覆盖 AC-11～AC-15 全部关键口径，且 `tests/test_verification_layer.py` 与全量非 Docker 测试套件全部通过、零回归。
