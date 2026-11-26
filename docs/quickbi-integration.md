# QuickBI 内网取数配置

全辖风险扫描默认使用 `RISK_DATA_FETCHER=local`，从 `data/risk-testdata.db` 读取 82 家行社的 62 项本地指标。启用 `quickbi` 后，工作台按业务日期向内网长表或宽表取数。扫描前可在 `GET /api/bootstrap` 的 `meta.data_fetcher` 确认当前模式。

## 启动和挂载

在启动后端的同一环境设置以下变量。示例地址和文件路径均需替换为部署值。AK、SK 和会话凭据由服务管理器的环境变量或门户身份钩子提供。

```powershell
$env:RISK_DATA_FETCHER = 'quickbi'
$env:QB_HOST = 'https://quickbi.example.internal'
$env:RISK_QUICKBI_DICT_FILE = 'C:\QuickBI\metrics\指标字典.json'
$env:RISK_QUICKBI_ORG_FILE = 'C:\QuickBI\机构归属.json'
$env:IDENTITY_PROVIDER = 'smartq_ticket'
$env:SMARTQ_SVC_URL = 'https://smartq.example.internal'
$env:EMBED_WORKS_ID = '<免登目标 worksId>'
$env:QB_REPORT_ID = '<已存在的看板 reportId>'
$env:RISK_PI_MODE = 'demo'
.\.venv\Scripts\python.exe run.py
```

| 变量 | 配置说明 |
| --- | --- |
| `QB_HOST` | 内网 QuickBI 服务的协议、主机和可选端口 |
| `QB_ACCESS_ID`、`QB_ACCESS_SECRET` | OpenAPI AK/SK，用于数据集元数据、小 Q 查询和旧版 `ticket` 模式的签名出票 |
| `SMARTQ_SVC_URL` | SmartQ 出票服务的 HTTP(S) 基地址，兼容 `QB_SMARTQ_TICKET_URL`。也可直接填写以 `/api/report/createTicket` 结尾的接口地址 |
| `EMBED_WORKS_ID` | 出票和免登所用的作品 ID，依次回退到 `QB_WORKS_ID`、`QB_REPORT_ID` |
| `QB_REPORT_ID` | OLAP 请求所用的真实看板 ID，填入 URL、表单和两个 Referer。数据集 ID 单独填在字典的 `cubeId` |
| `QB_COMPONENT_ID` | 明细表组件 ID，默认 `00000000-0000-0000-0000-000000000001` |
| `QB_SESSION_TTL_SECONDS` | 单个客户端内存中的会话缓存秒数，默认 300。设为 0 时每次调用重新获取会话 |
| `RISK_QUICKBI_DICT_FILE` | 挂载精简包的 UTF-8 指标字典。69 条目录中，以名称和同义词匹配内置口径，使用可绑定且有数据源的条目 |
| `RISK_QUICKBI_ORG_FILE` | UTF-8 机构归属文件，包含 `999999` 全省、11 个地区和 82 家行社 |
| `RISK_DATABASE_URL` | 可选，覆盖应用归档数据库。QuickBI 模式默认使用 `data/risk-quickbi.db`。归档包含运行结果和当时可见的业务数据，应按使用身份隔离实例和归档库 |
| `RISK_PI_MODE` | 示例使用 `demo` 完成取数联调，Pi 归并分析使用离线模型替身。模型模式沿用项目的 Pi 配置 |

以下为长表指标字典和机构归属示例。机构的上级填写机构目录中的名称或代码。字典的 `indicatorName` 对应长表“指标名称”列中的值，`unit` 填实际计量单位。源表“指标单位”为代码 `1` 时，适配器使用该字典单位。宽表指标可按下文示例绑定度量列。

```json
{"metrics":{"五级不良贷款率":{"cubeId":"<数据集 ID>","indicatorName":"五级不良贷款率","kind":"rate","unit":"%","synonyms":["不良贷款率"],"status":"available"}}}
```

```json
{"全省合计":{"代码":"999999","层级":"01","上级":null},"示例地区":{"代码":"999001","层级":"02","上级":"全省合计"},"示例行社":{"代码":"801000","层级":"03","上级":"示例地区"}}
```

## 身份模式

| `IDENTITY_PROVIDER` | 配置与调用方式 |
| --- | --- |
| `smartq_ticket` | 调用配置的 SmartQ 出票服务，再使用纯 HTTP 在 QuickBI 源下完成免登。自动保留会话 Cookie、CSRF 和票据 |
| `cookie`（默认） | 设置目标用户的 `QB_COOKIE` 与 `QB_CSRF_TOKEN`。嵌入 Python 服务时也可给 `QuickBiClient(session_provider=...)` 注入返回 `Cookie`、`X-Csrf-Token` 两项的回调。用 `check_alive()` 检查会话 |
| `ticket` | 已由门户完成换票时，提供同一会话的 `QB_ACCESS_TICKET`、`QB_COOKIE`、`QB_CSRF_TOKEN`。需要运行时出票时，用 `QuickBiClient(session_exchange=exchange)` 构造客户端，再传入 `QuickBiOlapFetcher(client)`。回调 `exchange(ticket, login_url)` 在目标用户的隔离会话完成免登，返回上述两个会话头。配置 `EMBED_WORKS_ID`，绑定账号时同时设置 `EMBED_ACCOUNT_NAME` 与 `EMBED_ACCOUNT_TYPE`，也可用 `QB_USER_ID`。后续登录探测和取数请求均携带该票据 |
| `dataService` | `DATA_SERVICE_SID` 为预留参数。选择此模式会提示“数据服务适配器尚未发布/启用” |

SmartQ 模式先发 `GET /api/report/createTicket?worksId=…`，出票请求不带 Cookie 或 AK/SK 签名头。客户端使用出票响应的免登地址，缺省时构造 `/token3rd/dashboard/view/pc.htm?pageId=…&accessTicket=…&locale=zh-CN`。初始目标和每一跳都须与 `QB_HOST` 同源。客户端逐跳更新 Cookie，从最终 Cookie 中提取 CSRF。整条链路使用 HTTP，无需浏览器或 CDP。

`client.login()` 与 `client.get_session()` 共用按 worksId 区分的当前会话缓存。`login(works_id=…, force=True)` 强制重换，`clear_session_cache()` 清空缓存。`whoami()` 返回当前用户信息，`check_alive()` 可用于登录探测。票据会话在探测和 OLAP 请求上附带 `accessTicket`。SmartQ 和注入了 `session_exchange` 的票据模式遇到 `AUTHX_01002` 时重新出票、换会话并重发原请求一次。静态凭据过期时将错误返回调用方。联调日志中应省略会话对象及含票据的 URL。

## OLAP 请求和指标绑定

取数请求为 `POST /api/v2/olap/query`，query 带 `componentId`、`reportId`、`componentType=73`，票据模式另带 `accessTicket`。请求头包括会话的 `Cookie`、`X-Csrf-Token`、`X-Requested-With: XMLHttpRequest` 以及 `Origin: <QB_HOST>`。`Referer` 与 `X-GW-Referer` 均为该源下的 `/dashboard/view/pc.htm?pageId=<reportId>`。

`Content-Type` 为 `application/x-www-form-urlencoded; charset=UTF-8`。表单只有一个字段 `olapQueryParam`，值为 UTF-8 编码的紧凑 JSON，经表单 URL 编码发送。JSON 包含组件 ID、组件名、`dataType="general"`、看板 ID 和有序的 `configs`。每段包含 `type`、`cubeId`、`config`。

| 段 | config 内容 |
| --- | --- |
| `field` | `fields` 按 fid 动态选列，guid 默认同 fid，geographicInfoModel 默认 0。维度用 row，聚合查询的度量用 column 并默认 sum |
| `beforeAggregateCondition` | `logicalOperator="AND"`。每个 `in` 条件的 `args` 只放一个值，例如 `[{"valueType":"string","value":"20260228"}]` |
| `paging` | limit 默认 5000，offset 默认 0，pagedByAllDim 为 true |
| `queryConfig` | needCount、queryCount、queryDetail 默认均为 false。queryDetail 为 true 时字段不得带 aggregate |

长表的“指标名称”和“指标值”分别用于过滤和取值，字典的 `indicatorName` 填源表指标名。宽表中每项指标占一个度量列，字典可显式指定列名，例如 `{"indicatorName":"五级不良贷款率","measureCaption":"五级不良率（%）","cubeId":"<宽表 ID>","unit":"%"}`。适配器从 infoNew 的 measureList 动态解析 fid，支持用指标名及 `synonyms` 匹配度量，按机构名称、机构号和业务日期查询。两类表都按日期候选逐个试探，并缓存成功的日期字段。

内网核对口径时，可调用 `fetcher.live_indicators(cube_id, limit=500)`。宽表返回源表度量列名，长表在当前会话下直接查询“指标名称”列，不限定业务日，并对结果去重排序。

## 扫描和核验

1. 在全辖扫描选择业务日，如 `2026-02-28`。适配器读取 `GET /openapi/v2/dataset/infoNew` 的字段布局，向 OLAP 下推日期，长表另加指标名过滤。目标日无行时反查可用日期，原期次继续标记缺数。
2. 查看结果中的可见行社数。扫描以当期响应的三级行社为范围。全省和地区源表合计按机构号保存；可比的金额显示成员和与源表的差额，率类采用同口径源表合计。权限裁剪时只呈现可见范围并标出合计行的来源。
3. 客户端已按项目外 `agent-simplified.md` 对齐合规票据纯 HTTP 免登和 `olapQueryParam` 表单协议。本地协议桩使用合成身份与数据。部署到内网后，用 `whoami()` 核对目标用户，并用一个已知指标和日期核对行社名称、精确值、单位、全省合计及受限账号的可见数。

只读调用范围包括数据集元数据、登录态探测、嵌入出票、OLAP 查询和小 Q 流式问数。小 Q 客户端供探索、无字典指标补查和口径参考，仅调用 `POST /openapi/v2/smartq/queryByQuestionStream`。收到有效 `olapResult` 后的收尾错误记入 `diagnostics.trailing_error`。

| 错误码 | 处理方式 |
| --- | --- |
| `AUTHX_01002` | 更新已过期的 Cookie 或票据会话 |
| `OE10010101` | 校准请求机时钟，将偏差控制在 3 分钟以内 |
| `OE10010106` | 核对接口路径与内网应用授权 |
| `AE0130010003` | 为目标报表开通三方嵌入 |
| `AE0510150001` | 核对 `QB_REPORT_ID` 是否对应已存在的看板 |
| `NOX5300008` | 明细查询时移除字段的 aggregate，聚合查询使用 queryDetail=false |

## 小 Q 追问自动改写补查

工作台先读取本次任务、本次追问范围内的归档证据。遇到字典外指标或归档中缺少的细分维度时，Pi 可将问题改写为包含明确日期、完整机构名称、指标及题型的问句。省级汇总使用“全省合计（含宁波）”，率类取相应口径的值。Python 宿主签名并调用流式接口，返回可引用的 `smartq:` 证据。QuickBI 凭据保留在 Python 宿主。

| 变量 | 配置说明 |
| --- | --- |
| `RISK_SMARTQ_FOLLOWUP` | 默认 `auto`，配置齐全时自动启用。`on` 要求启用，缺少配置时补查不可用。`off` 强制关闭 |
| `QB_HOST` 或 `QB_BASE_URL`、`QB_ACCESS_ID`、`QB_ACCESS_SECRET` | 小 Q 服务地址及 OpenAPI 签名凭据 |
| `QB_SMARTQ_USER_ID` | 小 Q 使用的 QuickBI 用户，未设置时读取 `QB_USER_ID` |
| `QB_SMARTQ_CUBE_ID` | 目标数据集 ID，优先使用显式配置。也可从挂载的 `RISK_QUICKBI_DICT_FILE` 选择数据集 |
| `QB_SMARTQ_INFERENCE_LLM`、`QB_SMARTQ_DATA_LLM` | 分别指定推理和数据模型，均默认 `nv1`。按内网服务要求填写，区分大小写 |

可通过 `GET /api/bootstrap` 的 `meta.smartq_followup_available` 检查配置可用性；实际连通性由补查结果确认。单次 `POST /api/agent/runs/{run_id}/conversation` 可传 `allow_smartq: false`，仅查归档。

绑定行社的追问限制在该行社内。宿主检查改写问句的目标机构，有机构字段的返回记录按当前行社筛选；明确请求的全省对比可保留全省合计基准。同一任务及行社范围内的后续补查可沿用最近一次成功的小 Q 会话。工作台展示改写问句、状态与范围过滤后的记录数，每次最多保存 20 条代表记录。已收到有效数据后出现尾帧错误时保留结果并提示；遇到超时或空结果时展示补查状态。**小 Q 补查结果仅供参考，主指标口径以 QuickBI 源表为准。**
