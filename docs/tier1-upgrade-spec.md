# 第一梯队三项体验升级详细设计说明书

> **覆盖范围**：本设计文档细化第一梯队全部 3 项改造的业务口径、前后端接口契约、并发安全防护、UI 交互与验收标准，供评审确认后交由本地 Codex（GPT）落地实现与测试闭环。
> 1. **模块一**：「报告草稿」统一更名为「风险报告」+ 全辖扫描后**后台自动为重点高风险行社生成风险报告**（实时进度，零人工点击）；
> 2. **模块二**：正式公文精修与发文闭环（**公文数值统一两位小数** + **在线修订/审核定稿** + **一键导出标准公文 Word `.docx`**）；
> 3. **模块三**：执行轨迹**时间轴自动折叠空闲间隙** + 左侧**历史任务列表辨识度增强与安全清理**。

---

## 模块一：「风险报告」更名 + 扫描后后台自动生成闭环

### 1.1 全局文案统一（不动既有 API 路径与数据库表名，保证零破坏兼容）

| 位置 | 原文案 | 新文案 |
| --- | --- | --- |
| 顶栏主 Tab | `报告草稿` | **`风险报告`** |
| 总览/详情操作按钮 | `生成提示函草稿` / `查看草稿` | **`生成风险报告`** / **`查看风险报告`** |
| 风险报告页头操作 | `草稿 vN` / `刷新草稿` | **`版本 vN`** / **`刷新报告`** |
| 执行轨迹阶段标题 | `06 提示函撰写` | **`06 风险报告撰写`** |
| 轨迹来源下拉选项 | `提示函撰写 · {行社}` | **`风险报告 · {行社}`** |

### 1.2 端到端全自动流程架构

```mermaid
flowchart LR
    S1["01~04 取数与源表口径校验"] --> S5["05 行社归并（82家）"]
    S5 -->|"run.status = completed<br/>立刻展示总览与82家列表"| UI["前端工作台立即可读"]
    S5 -->|"自动触发后台队列"| PICK["重点高风险筛选器<br/>按 P0 底线数降序选 Top N"]
    PICK --> POOL["后台线程池（SQLite 2并发 / PG 4并发）<br/>带 per-(run,org) 互斥锁防重跑"]
    POOL -->|"每家走 v3.2 多步 MCP 工具链"| L6["06 风险报告撰写<br/>实时落盘 Letter + letter_drafts + auto_reports 进度"]
    L6 -->|"前端 4s 静默轮询进度"| UI
```

### 1.3 重点高风险行社自动筛选规则（确定性、可配置）

在 `05 行社归并` 落盘后，后端调用 `select_auto_report_targets(cases)` 自动选出本次需要自动撰写《风险报告》的行社清单：
1. **风险排序依据**：对本次扫描所有命中风险事项的行社，复用既有 `evaluate_letter_rules(case)` 计算：
   - `p0_count = len(mandatory_p0_clues)`（触发 P0 监管硬底线数量）；
   - `signal_count = len(scan_signals)`（有效扫描信号数）；
   - 按 `(-p0_count, -signal_count, org_id)` 稳定排序。
2. **默认入选口径**（与实测 82 家分布精准匹配：82 家中 11 家命中 ≥3 项 P0）：
   - 优先选取所有 **`p0_count >= 3`** 的重度高风险行社，上限默认 **`8` 家**（环境变量 `RISK_AUTO_REPORT_MAX`，默认 `8`，设为 `0` 关闭自动生成）；
   - 若某期次全辖无 `p0_count >= 3` 的行社，则自动降级选取 `p0_count >= 1` 中最靠前的前 **5** 家；若全辖均无 P0，则取 `signal_count > 0` 的前 **3** 家。
3. **非入选行社**：在总览与「风险报告」页仍保留 **「生成风险报告」** 按钮，随时可按需补生成。

### 1.4 实时进度状态结构（存 `run.selection["risk_scan_result"]["auto_reports"]`，无需改表结构）

```json
{
  "status": "running",
  "rule": "重点高风险行社（≥3项P0底线，上限8家）",
  "total": 8,
  "completed": 3,
  "failed": 0,
  "items": [
    {"org_id": "TEST_LS_046", "org_name": "新昌", "p0_count": 5, "status": "completed", "revision": 1, "error": null},
    {"org_id": "TEST_LS_018", "org_name": "鹿城", "p0_count": 4, "status": "running", "revision": null, "error": null},
    {"org_id": "TEST_LS_061", "org_name": "柯城", "p0_count": 4, "status": "queued", "revision": null, "error": null}
  ],
  "started_at": "2026-11-26T09:15:00+00:00",
  "updated_at": "2026-11-26T09:17:12+00:00"
}
```
- 状态枚举：`idle`（未启用或无目标） / `running`（自动生成中） / `completed`（全部成功） / `partial`（部分成功、部分失败，支持前端一键重试失败项）。

### 1.5 关键工程防坑与并发护栏（必须落实）

1. **进程内互斥锁（防后台队列与手动点击撞车）**：按 `(run_id, org_id)` 维护轻量锁，同一行社正在后台自动生成时，若用户在前端手动点击同一家生成，返回 `409 正在自动生成该行社风险报告，请稍候`，不重复烧大模型 Token。
2. **SQLite 并发写保护**：SQLite 下后台撰函并发度限定为 **`2`**（PostgreSQL 下为 **`4`**），开启 SQLite `PRAGMA journal_mode=WAL` 与 `busy_timeout=15000`，写入 `Letter` / `run.selection` 采用独立短事务，避免 `database is locked`。
3. **MCP 累计读取配额适配**：单个行社多步工具链撰写平均消耗 `6～12` 次只读 MCP 调用，自动生成 8 家 + 后续人工抽查会接近默认 `200` 次配额；将 `risk_scan` 任务的默认 `RISK_QUERY_MAX_REQUESTS` 下限提升至 **`600`**（允许环境变量显式覆盖），防止自动生成中途触发 `QUERY_BUDGET_EXCEEDED`。
4. **单测隔离防后台泄漏**：`ScanInput` 新增可选字段 `auto_generate_reports: bool = True`；且后台工作线程捕获 `SessionLocal` 引用并在 `engine` 已释放或任务已删除时静默退出，确保 `pytest` 临时库销毁零异常。
5. **补跑/重试接口**：新增 **`POST /api/risk-scans/{run_id}/auto-reports`**（可选 `retry_failed_only: bool = True`），支持对失败项一键重试，也支持对昨天已跑完的旧扫描一键触发重点高风险报告批量自动生成。

### 1.6 前端交互呈现

- **总览页顶部轻量进度条**（仅当 `auto_reports.total > 0` 时展示）：
  - 进行中：蓝色呼吸状态点 + `风险报告自动生成中 · 3 / 8 家（正在撰写：鹿城、柯城）` + 细进度条；前端每 **4 秒** 静默拉取 `GET /api/risk-scans/{run_id}`，完成后自动停表；
  - 完成后：浅绿提示 `已自动生成 8 份重点高风险行社风险报告`；若 `status === 'partial'`，显示失败家数及 **「重试失败项」** 按钮。
- **总览行社表格**：每行行社名旁用极简小点/文案标识报告状态（已生成显示浅蓝 `已有报告`、正在生成显示 `生成中…`），点选该行切到「风险报告」页直接可读。

---

## 模块二：正式公文精修与发文闭环（两位小数 + 在线修订/定稿 + Word `.docx` 导出）

### 2.1 公文数值精度双轨规范（底层全精度，公文两位小数）

**原则**：绝不修改数据库原值、绝不降低 `verification_layer.py` 的 `Decimal` 运算精度；只在**对外公文展示层**统一保留符合银行业行文习惯的 **2 位小数**。

| 指标类型 | 原始实测示例 | 正式公文与附表展示 | 特殊边界规则 |
| --- | --- | --- | --- |
| 比率/百分比（`%`） | `133.7127%` / `226.03305%` | **`133.71%`** / **`226.03%`** | `ROUND_HALF_UP` 四舍五入保留 2 位小数 |
| 百分点变化（`百分点`/`个百分点`） | `0.0633个百分点` / `92.32035个百分点` | **`0.06个百分点`** / **`92.32个百分点`** | 若原值非零但绝对值 `< 0.005`（四舍五入后成 `0.00`），保留 4 位防丢失真实变动 |
| 金额（`万元`/`元`） | `105,915.40万元` | **`105,915.40万元`** | 统一千分位 + 2 位小数 |
| 整数/排名/期次 | `第 77 / 82 名` / `2026年9月末` | 原样保持 | 绝不误改日期、编号、排名与计数 |

**双端落实机制**：
1. **工具返回层**（`fetch_org_metrics` & `run_verified_operator`）：在保留原始 `value` / `value_display` 供核验的同时，新增 `formal_display` 字段（如 `"133.71%"`），并在 `agent/src/risk-letter.ts` 的 `systemPrompt` 明确指示模型在公文正文统一使用 2 位小数正式口径；
2. **宿主渲染层**（`backend/risk_letter.py` 的 `_render()`）：
   - 附表「本期实测」列统一调用 `format_formal_display(row)` 输出 2 位小数；
   - 对正文中的超长小数百分比/百分点（如模型偶发写出 `226.03305%` 或 `92.32035个百分点`）做精准正则归一化到 2 位小数（仅匹配紧邻 `%` 或 `个?百分点` 的 `3+` 位小数，确保绝不误伤任何非百分比数字）。

### 2.2 在线微调修订 + 审核定稿（100% 复用既有后端 API）

后端 `backend/app.py` 已经完整实现了带乐观锁与审计日志的函件生命周期接口，前端直接接入即可：
- **`PATCH /api/letters/{letter_id}`**（已存在）：传入 `{content, expected_revision}`，保存人工微调后的正文、版本号递增至 `v(N+1)`、写入 `LetterHistory`、原审批状态按合规规则自动失效退回待审；
- **`POST /api/letters/{letter_id}/approve`**（已存在）：传入 `{expected_revision}`，审核定稿（`status = "approved"`），同时锁定防止被「重新生成」意外覆盖。

**前端「风险报告」页交互设计（保持 TDesign 极简风格）**：
- 页头元信息：`{行社全称} · 版本 v{revision} · {草稿待审 | 已审核定稿}`；
- 操作区按钮（按状态克制呈现，不堆砌）：
  - **浏览态**：`查看撰函轨迹`（有轨迹时） | `编辑修订` | `重新生成`（仅草稿态启用） | `审核定稿`（仅草稿态启用） | **`导出 Word`**（主按钮） | `导出 Markdown`（次级下拉/文字链）；
  - **编辑态**：将 `.rd-document` 切为等宽阅读区 `<textarea>`（预填完整正文，保留公文段落与附表），页头切换为 `保存新版本`（调用 `PATCH /api/letters/{id}`）与 `取消编辑`；若遇并发版本冲突（409），清晰提示刷新读取最新版。

### 2.3 一键导出标准银行公文 Word（`.docx`）

- **技术选型（零外部依赖，内网离线无忧）**：
  - 当前 `.venv` 未安装 `python-docx`；为避免内网离线部署新增外部依赖包，新建 **`backend/docx_export.py`**，使用 Python 标准库 **`zipfile` + `xml.etree.ElementTree`** 直接构建符合 ECMA-376 OpenXML 规范的标准 `.docx` 字节流（约 150～200 行纯 Python，Microsoft Word 与 WPS 均可无警告打开）。
- **新增接口**：`GET /api/letters/{letter_id}/download.docx`
  - 返回 `application/vnd.openxmlformats-officedocument.wordprocessingml.document`，文件名 UTF-8 URL 编码为 `{行社名}风险报告-v{revision}.docx`。
- **公文版式规范（对照 GB/T 9704 与省联社正式函件样式）**：
  - **纸张与页边距**：A4 纵向（`210mm × 297mm`），上 `37mm`、下 `35mm`、左 `28mm`、右 `26mm`；
  - **主标题**：居中、二号（22pt）加粗、方正小标宋简体（回退黑体/微软雅黑），标题段后带深蓝/暗红细分隔线；
  - **主送行**（`XX农商银行：`）：左顶格、三号（16pt）仿宋加粗；
  - **一级标题**（`一、……`、`二、……`、`附表：……`）：首行缩进 2 字符、三号黑体；
  - **正文段落**：首行缩进 2 字符（`firstLineChars="200"`）、三号仿宋_GB2312（回退仿宋）、固定行距 `28pt`；段落开头的 `（一）小标题。` 自动加粗，正文常规；
  - **落款**：`特此函告。` 首行缩进 2 字符；`风险管理部` 与日期右对齐；
  - **附表**：标准实线边框表格，表头浅灰底色（`#F1F5F9`）加粗居中，数值与排名列右对齐/居中，指标名称列左对齐，单元格带舒适内边距，避免跨页断行（设置 `cantSplit` 与表头重复 `tblHeader`）。

---

## 模块三：执行轨迹时间轴折叠空闲间隙 + 左侧历史列表增强与安全清理

### 3.1 时间轴空闲间隙自动折叠算法（纯前端确定性计算，`frontend/src/trace-panel.ts`）

解决 `时长` 视图下“昨天 15:39 扫描 + 17:21 撰函拉出 `0 – 6,123s` 大段空白断点”的问题：

1. **活跃区间归并**：
   - 提取全部有有效 `started_at` 的事件区间 `[start_i, end_i]`（`end_i = start_i + max(0, duration_ms)`），按 `start_i` 排序并合并重叠或相邻间隔 **`<= 15,000 ms`（15 秒）** 的区间，得到若干互不相交的活跃段 `S_1, S_2, ..., S_k`；
2. **分段压缩映射（当 `k >= 2` 且启用「折叠空闲」时）**：
   - 计算总活跃时长 `activeSpan = sum(S_j.end - S_j.start)`；
   - 每个空闲间隙 `gap_j = S_{j+1}.start - S_j.end` 分配固定视觉宽度 `gapWidth = min(4%, 24% / (k - 1))`，剩余 `100% - (k - 1) * gapWidth` 按各活跃段真实耗时比例线性分配；
   - 每个事件在所属活跃段内严格按真实时间比例映射 `left` 与 `width`，**彻底避免几十秒的模型调用被 90 分钟闲置压成看不清的细丝**；
3. **视觉与交互呈现**：
   - 在每个被折叠的空闲槽位渲染一条简洁的浅灰斜虚线分隔标（`.trace-gap`），hover 提示具体空闲时长（如 `已折叠空闲 1 小时 25 分`）；
   - 时间轴元信息清晰标出：`活跃耗时 3 分 12 秒 · 已折叠 2 处空闲（总跨度 1 小时 42 分）`；
   - 工具栏增加轻量切换按钮 **`折叠空闲`**（默认开启；点击可关闭切回「真实挂钟跨度」以便审计核对原始时间间距）。

### 3.2 左侧历史列表辨识度升级 + 安全清理

1. **历史列表项信息密度升级（`frontend/src/main.ts` & `styles.css`）**：
   - 每条历史任务保留主标题（如 `2026-09-30 · 82 家行社扫描`），下方新增一行 `11px` 浅灰紧凑副信息：
     - 发起时间 `MM-DD HH:mm`（立刻区分同日多条扫描）；
     - 模型/模式简标（`DeepSeek` / `OpenAI` / `演示`）；
     - 报告状态（如 `8 份报告` 或 `生成中 3/8`；失败任务继续显示橙色 `失败` 角标）。
2. **安全删除接口与防误删保护**：
   - 新增后端接口：
     - **`DELETE /api/risk-scans/{run_id}`**（同时兼容 `DELETE /api/runs/{run_id}` 与 `DELETE /api/code-runs/{run_id}` 以便清理所有历史脏任务）；
     - **`POST /api/risk-scans/cleanup-failed`**（一键清理所有 `status == "failed"` 的历史失败测试记录，不影响任何已完成任务）；
   - **三条硬安全红线（后端强制校验）**：
     1. 正在运行中的任务（`run.status == "running"` 或 `auto_reports.status == "running"`）禁止删除（返回 `409`）；
     2. 包含已审核定稿或已模拟下发风险报告（`Letter.status in {"approved", "simulated_sent"}`）的任务禁止删除（返回 `409 存在已定稿或已下发的风险报告，禁止删除`），保护合规审计留痕；
     3. 删除时在单事务内按外键顺序干净级联清理：`LetterHistory` → `Letter` → `RiskCase` → `ConversationTurn` → `QueryBudgetLedger` → `AnalysisRun`。
   - **前端交互**：
     - 历史项 hover/聚焦时右侧露出低调的删除图标（仅对可删任务展示），点击弹轻量二次确认，删除成功后自动切到下一条最新任务；
     - 左侧历史栏标题区增加轻量 **「清理失败」** 文字按钮（仅当存在失败任务时显示），一键清掉早期调试残留的失败记录。

---

## 改动文件规划与验收标准（审批后交由 GPT / Codex 落地，我亲自复核验收）

### 拟涉及改动文件
- **后端**：
  - `backend/risk_scans.py`：重点高风险自动筛选、后台自动撰函线程池 + `(run_id, org_id)` 互斥锁 + `auto_reports` 进度维护、`POST /api/risk-scans/{run_id}/auto-reports`、`DELETE /api/risk-scans/{run_id}`、`POST /api/risk-scans/cleanup-failed`；
  - `backend/risk_letter.py`：两位小数正式展示格式化 `format_formal_display`、正文超长小数百分比/百分点安全归一化、附表格式化；
  - `backend/docx_export.py`（新建）& `backend/app.py`：纯标准库 OOXML `.docx` 生成器 + `GET /api/letters/{letter_id}/download.docx` + 通用任务安全删除路由 + SQLite WAL/`busy_timeout`；
  - `backend/query_budget.py`：`risk_scan` 任务默认读取配额提升至 `600`（仍可环境变量覆写）；
- **Pi Agent**：
  - `agent/src/risk-letter.ts`：System Prompt 增加两位小数正式公文展示约束 + `formal_display` 字段说明，重建 `agent/dist/`；
- **前端**：
  - `frontend/src/main.ts`、`frontend/src/detail-panel.ts`、`frontend/src/detail.css`、`frontend/src/overview-panel.ts`、`frontend/src/overview.css`、`frontend/src/trace-panel.ts`、`frontend/src/trace-panel.css`、`frontend/src/styles.css`、`frontend/src/scan-types.ts`、`frontend/src/workbench-tasks.ts`：
    - 全量更名「风险报告」；
    - 后台自动生成进度条 + 4 秒静默轮询 + 行社列表报告角标；
    - 风险报告在线编辑修订 / 审核定稿 / 导出 Word `.docx` + Markdown；
    - 时间轴空闲间隙折叠算法 + 折叠开关；
    - 左侧历史项时间/模型/报告数副标签 + 单条删除与一键清理失败任务；
    - 重建 `frontend/dist/`。

### 逐项验收标准
- [ ] **AC-1 全自动闭环**：发起全辖扫描完成后无需点击，后台自动按 `≥3 项 P0`（上限 8 家）依次生成重点高风险行社风险报告；前端实时轮询更新进度并在总览与轨迹中立即可见；同 `(run_id, org_id)` 并发手动点击被互斥拦截；
- [ ] **AC-2 两位小数精度**：底层观测与 `verification_layer.py` 全精度不受影响；正式风险报告正文与附表的比率/百分点统一保留 2 位小数（不误伤日期/编号/极小非零值）；
- [ ] **AC-3 在线修订与定稿**：在「风险报告」页可在线编辑保存 `v(N+1)`（写入 `LetterHistory`），可点「审核定稿」锁定版本并阻止覆盖；
- [ ] **AC-4 标准 Word 导出**：`GET /api/letters/{id}/download.docx` 返回合法 OOXML ZIP（含标题、主送、缩进正文、右对齐落款及真实 Word 附表 XML），用 Python `zipfile` + XML 解析验真无错；
- [ ] **AC-5 时间轴折叠断点**：多段跨小时轨迹在默认「时长 + 折叠空闲」下不再出现大片空白挤压，活跃段清晰可读，且支持切换回「真实跨度」；
- [ ] **AC-6 历史辨识度与安全清理**：左侧清晰区分同日多条扫描；一键清理失败任务与单条删除生效，且严格拦截运行中或含已定稿/已下发报告的任务（409）；
- [ ] **AC-7 零回归 + 线上实测**：Agent 全部 Node 测试、前端 `typecheck/build`、Python `.venv` 全量回归全部通过；实机用线上 DeepSeek V4.1 Flash 验证自动队列生成、两位小数成函、`.docx` 导出与浏览器 UI 验收。
