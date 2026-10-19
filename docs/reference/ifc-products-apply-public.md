# 产品批量添加公开合同

本文记录当前 `ifc_products_apply` 的 Direct、Runner 和 Operation 查询合同。
批量编译、报告事务与准确 IFC 验证由[产品添加 Core](ifc-product-apply-core.md)负责；
动作身份、claim、监督与恢复遵循[共享原动作恢复合同](shared-operation-recovery.md)。
本文不复制 Family 参数或外部条件 Schema；原生 Agent 使用结果、性能与限制见
公开产品添加交付。

## 入口与职责

`ifc_products_apply` 将一个准确已发布 Family 与一个完整 Parquet payload 中的全部产品
添加到当前授权 Run 的 IFC。调用同步返回已确认结果；工具不建立后台 Job 或轮询协议。
Direct 和 Runner 使用相同公开业务分支，并进入同一个 `IfcProductsApplyService` 和
`IfcRevisionWriter`。

- Direct 在请求中提供 `direct_session_alias`，由服务器解析 Scope 和本次调用身份。
- Runner 从受保护的执行凭证取得 Task、Run、主体和 action 身份；Provider 输入没有
  Direct session、本地路径或可由调用方选择的 Run。
- Family 和 Package 公开引用由服务器解析成当前授权范围内的准确不可变资源。
  原 Operation 保存 request、准确 input binding 和内部原动作身份。
- 公开输入不接受幂等键、摘要、claim、Worker 预算、候选位置、部分提交开关或表示选择。

## 两个封闭输入分支

`mode` 必填，各分支禁止混用字段。引用必须复制工具返回值，不根据字符串形式构造。

| 字段 | `mode=apply` | `mode=recover` |
| --- | --- | --- |
| `family_version_alias` | 必填；Family list/info 返回的准确已发布版本 | 不接受 |
| `package_alias` | 必填；当前 Scope 可见的输入 Package | 不接受 |
| `payload_name` | 必填；包内一个完整 Parquet 产品表 | 不接受 |
| `operation_ref` | 不接受 | 必填；原 Apply Operation 的准确引用 |
| `direct_session_alias` | 仅 Direct 必填 | 仅 Direct 必填 |

Direct 添加示例：

```json
{
  "direct_session_alias": "direct_001",
  "mode": "apply",
  "family_version_alias": "<复制准确 Family 版本引用>",
  "package_alias": "data_001",
  "payload_name": "products"
}
```

Direct 恢复示例：

```json
{
  "direct_session_alias": "direct_001",
  "mode": "recover",
  "operation_ref": "<复制原 Operation 引用>"
}
```

Runner 使用同一业务对象并省略 `direct_session_alias`。恢复请求不携带可修改的 Family、
Package 或 payload；服务器从原 Operation 加载原请求，使用原幂等键调用共享 Core。
本次 recover 的新传输身份不会替换原动作身份。

## 输入准备与 IFC 前置条件

按需输入资源为 `cad2bim://contracts/ifc_products_apply/input`。先通过
`data_describe_package` 和有界 `data_query_rows` 检查来源；符合要求的 Package 可直接使用，
需要转换时用 `data_transform` 发布新 Package。应保留来源键和来源列，不能通过删去失败或
未处理产品使一次完整添加看似通过。

输入沿用 [Core 六列合同](ifc-product-apply-core.md#输入与准确来源)：

| 列 | Parquet 类型 | 含义 |
| --- | --- | --- |
| `product_key` | string | 完整 payload 内非空、唯一的产品键 |
| `origin_x_m`、`origin_y_m`、`origin_z_m` | 各为 float64 | 有限的产品世界坐标，单位为米 |
| `parameters_json` | string | 一个 JSON object，按准确 Family 的公开参数合同解释 |
| `external_constraints_json` | string | 一个 JSON array；无所需条件时为 `[]` |

`logical_key_fields` 必须精确为 `["product_key"]`。其他来源列可保留在输入 Package，
Core 只读取六列。`product_key` 是 payload 内的键，不是全模型去重身份。

通过 `ifc_product_family_info` 的准确版本查询读取 `family_contract_json`：参数名称、类型、
必填项、默认值和单位来自 `public_parameters`；接口要求来自准确 Family 的
`interfaces[].external_constraint` 及适用的 `connection_profile`。
外部条件复用 `cad2bim://contracts/ifc_product_realization_preview/self_geometry` 的
`external_constraints` 定义；表中只编码该数组，不编码整个 invocation。
Apply 固定位置和条件点为世界米制坐标，方向无量纲；Family 参数继续使用各自声明单位。
完整条件规则见[产品族自身几何条件](product-family-self-geometry.md)。

添加前读取 `ifc_model_info`。目标 Run 没有模型时才初始化；同一目标的多次 Family 添加
累积到同一模型。完整几何基准要求项目长度单位、平面角单位、唯一三维 `Model` 主 Context，
以及该父 Context 下唯一的 `Body` 子 Context，后者 `ContextType=Model`、
`TargetView=MODEL_VIEW`。通过 `geometry_basis_configured`、`geometry_basis` 和必要时的
`ifc_representation_context_list` 核实，以 `ifc_model_configure_geometry_basis` 补齐缺项。
已有合法单位和 Context 应复用。Body 正确但 `geometry_basis` 为空时，仍可能缺项目单位或
完整主 Context；这类提示不要求重复创建 Body。项目长度单位不改变输入三列坐标的米制约定。

`ifc_model_info.size_bytes` 是该次准确持久 IFC 的已核验字节数，沿已有 revision inspection
投影，不再通过 Query 的 `to_string()` 估算。读取仍执行既有准确模型检查，不宣称为常数时间
或免解析读取。多族累计时，元数据读取成本也受当前完整 IFC 大小影响。仅需记录某次 Apply
提交的版本和大小时，直接使用其返回的 `committed_revision`，无需再读取整个模型。

## 终态结果与报告

正常终态包络包含 `operation`、`detail`；已确认失败另有 `error`。对应 Apply 的公开类型
复用既有 Operation 投影，不返回后台 request、存储路径或私有 revision 选择器。

| 路径 | 含义 |
| --- | --- |
| `operation.operation_ref` | 原 Operation 引用，用于查询和必要恢复 |
| `operation.operation_alias`、`sequence` | 可见终态 Catalog 的真实 alias 与序号 |
| `operation.outcome` | 原持久状态 `succeeded` 或 `failed` |
| `operation.primary_result.report_package` | 报告 Package 的公开引用，用于 Data 读取 |
| `operation.primary_result.report_tables[]` | 原报告实际 Parquet 成员的 `payload_name`、`format`、`row_count`、`logical_key_fields`；只列已存在的表 |
| `operation.primary_result.summary` | 原 `ProductApplySummary`，不在顶层重复保存 |
| `operation.primary_result.committed_revision` | 仅成功分支必有；原操作提交的 `revision_no` 与准确持久文件 `size_bytes` |
| `detail.family_version_ref` | 原准确 Family 的可见引用 |
| `detail.input_binding` | 原 `package_alias`、`payload_name`、`logical_key_fields` |
| `detail.diagnostics` | 同次已记录 FAIL/WARN 的有界投影，含原 `check_index`、原行与产品键、阶段、原因和已有数值；完整记录仍读原 `checks` |
| `error` | 原失败 code、phase、message 及适用的已有诊断 |

summary 的 `outcome` 为 `committed`、`rejected` 或 `technical_failure`；分别记录完整输入数、
确认/未确认求解数、已求解数、完成接口约束求值的产品数、确认/未确认构造数、已构造数、提交数、未执行数，
并保存 `timings_seconds`。确认失败的严格整批操作提交数为零；未知 settlement 不能据此
推断为失败或零提交。成功必须有原报告和 committed 摘要。报告发布失败时，失败结果可以没有
primary result；不能补造空报告或完整计数。

`committed_revision` 从原 Operation 的 Run 和 `result_ifc_revision_id`，经已有 IFC
Repository 读取准确不可变 revision/Artifact 元数据；不解析 IFC、不重新序列化，也不查询
最新 CURRENT。版本号在原操作所属模型内有意义，`operation_ref` 绑定其准确来源。
首次 Apply、`operation_get` 和 recover 使用同一投影；较早操作在后续添加后仍返回原版本和
原大小。缺失或错配的准确提交元数据明确拒绝，失败与未决结果不填造该字段。

报告通过 Data 工具有界读取，不在工具返回中展开所有 GUID。四表及唯一事务的完整定义见
[Core 报告合同](ifc-product-apply-core.md#四表报告与唯一提交)。

| Payload | 定位与读取重点 |
| --- | --- |
| `products` | 以 `input_row_index` 定位每一原输入行；保存 `product_key`、求解/构造/提交状态及实际 root GUID |
| `components` | 按 `product_key` 与 `component_key` 读取组件、IFC class、世界变换和实际 component GUID |
| `interfaces` | 按 `product_key` 与 `interface_key` 读取同次求解的命名接口；用 `component_key` 关联组件，`component_global_id` 定位本次真实提交的组件 |
| `checks` | 结合 phase、error_code、field_path、残差/容差、recommended_action 与适用的 diagnostic_json 定位问题 |

components 可以包含产品根，其 GUID 与 products.root_global_id 相同；统计实际 IFC 构件时
按非空 GUID 去重，不能将产品数与组件行数相加。标准报告 Schema 已知后复用有界聚合，
字段有疑问时再 describe；整模型核收按需要合并，必要的完整性、异常及受影响结果检查仍须保留。
常规消费优先核完整输入键覆盖、提交状态和异常聚合，不把 Family 合同另写成重复 SQL
验证器。正规化数字按类型与单位语义比较，不用 JSON 文本或包含关系区分 `0` 与 `0.0`；
未执行的检查标为未比较，不用恒零表示通过。需要正式恢复验收时，先恢复，再合并执行一次
最终 IFC 完整核查；普通成功调用不例行 recover。

`products.normalized_parameters_json` 保存正规化公开参数；SOLVED 行的 `solution_json`
保存 Family 提交的 `internal_parameters`。失败报告可保留数值阶段已有结果，但只有实际提交行
提供持久 GUID。缺失或重复 `product_key` 也应留在 products 报告，因此不能以该列替代
`input_row_index` 作为所有错误行的唯一键。报告行号不是输入 Package 的物理行查询接口；
回查来源应结合原 Package、产品键及保留的来源列。

`interfaces.component_global_id` 只从本次 Compiler 的真实绑定填写；未提交时为 null。
接口原点采用项目世界米制坐标，方向采用世界单位向量，截面尺寸按其声明字段单位解释。
这仍是该次求解的命名接口观察，不是对当前管道端面或跨产品连接的测量，也不创建 IfcPort。
后续 replace 或 placement 不会刷新历史报告；需要判断后续模型时读取当前对象并显式比较。

`detail.diagnostics` 的 `complete` 只表示正文是否包含全部已记录错误和警告，不表示业务检查已全覆盖。
每项可含原 `check_index`、`input_row_index`、`product_key`、`phase`、`code`、`severity`、
`field_path`、`message`、`recommended_action` 及原检查已测得的 `metric/residual/tolerance/unit`。
错误优先于警告，省略时 `complete=false`；用原报告 Package 的 `checks` 继续有界读取。
首次结果、原 Operation 读取和恢复共用该投影，不另作检查或更改原保存状态。

已有 Worker 源码诊断通过结构化 `diagnostic` 进入 `checks.diagnostic_json`，保留原来源文件、
行列和适用的 binding 信息；文本遵循原有有界截断规则并携带截断标记。字段仅在相应检查分支
有事实时填写，空字段本身不代表缺陷，也不能据技术错误猜测新的根因类别。

## 原 Operation 发现与读取

已知 `operation_ref` 时直接调用 `operation_get` 的
`mode=operation_reference` 分支。只有原引用丢失时，使用 `operation_list` 的
`mode=ifc_apply_recovery` 查找当前授权 Run 的原 Apply 记录。

恢复发现输入为可选 `cursor` 和 `page_size`（默认 20，上限 50），Direct 另有
`direct_session_alias`；此分支不接受 `before_sequence`、Catalog 筛选或业务写入字段。
返回 `operations`、`complete`、`next_cursor`；下一页原样提交返回的 cursor。
服务端按原记录 `(created_at, operation_id)` 倒序分页，游标绑定 Scope/Run。

每项包含原 `operation_ref`、可空 `operation_alias`、`tool_name`、四种真实 `outcome`
（`prepared`、`indeterminate`、`succeeded`、`failed`）、`started_at`、可空 `finished_at`
和可空 `error_code`。未终态没有 Catalog alias 或 sequence，不能为其合成目录身份。
`finished_at` 只复制原记录；不能仅凭其存在判断已成功或失败。

`operation_get` 对终态返回既有卡片及准确 Apply detail；对未终态返回原引用、状态、原时间和
同一 detail，没有虚构报告、GUID 或序号。普通终态 Catalog 列表保持原语义。
这两个查询只读，不创建 Operation、不运行 Worker、不接管 claim、不解冻。

查找时结合调用时间、原 Family 和 `input_binding` 确认操作；相同输入可以对应不同合法动作，
不能只选择最新一条。无法唯一确认、查询失败或暂时找不到记录都不能证明未受理或零提交。
同 Task 跨 Run 的读取权限也不授予跨 Run 恢复写权限。

## 恢复决策与错误边界

| 已查明事实 | 行为 |
| --- | --- |
| 原记录 `succeeded` | 读取原报告及原准确 revision；不再次求解、构造或提交 |
| 原记录 `failed` | 返回原错误及可用报告；原因修复后以新操作添加全部应处理输入 |
| `prepared` 或 `indeterminate` 需要接续 | 对准确原引用调用 recover，由既有 Writer 核提交证据、claim、guardian、原期限和候选 |
| 后台仍活跃或活跃性不明 | 不接管原 claim；保留明确诊断并继续按原 Operation 核实 |
| `IFC_COMMITTED_READBACK_FAILED` | 保留已提交状态和原引用，按既有损坏冻结处理；不得重新添加 |
| 无法读取可靠终态 | 错误使用既有 `operation_status=unknown`；不新增持久状态或假失败报告 |

recover 只允许当前合法可写原 Run 的 `ifc_products_apply` Operation。原已选 candidate 和报告
继续复用；不会刷新 deadline、改变原请求或绕开 CURRENT CAS。旧成功操作的正常恢复读取其原
准确 revision，不覆盖随后 revision。当前 IFC Query 仍按 CURRENT 查询，`operation_ref`
不提供历史 IFC 查询选择能力。

错误包络保留原 code、phase、message、已知 operation_ref/alias 和已查明的状态。
`ProductApplyCallError` 的 input 阶段映射为公开 preflight。受理后异常携带原 Operation 身份；
共享恢复适配开始处理原引用后，即使后续失败仍保留该引用。Core 已完成后再发生读取或投影错误，不改写其已确认
成功/失败状态。超时、断连、`retryable=false` 和报告不可用都不能单独证明零提交。

Direct 的新 MCP 请求通常产生新动作身份，Runner 新 action 也如此；相同 Family/Package/
payload 不会自动合并为原动作。因此未知结局应先查原 Operation，不能用新 Apply 猜测恢复。

## 传输等待与实际执行预算

Runner 的 HTTP 和外层 MCP 等待使用相应既有 Application policy，并保留响应传输余量：
Apply 使用 IFC revision invocation；model info、representation context list 和 spatial structure
list 使用 IFC revision read；Query 使用自身的 Query policy。显式传输覆盖仍生效，普通短读取
维持原默认值。这里包含排队与收尾时间，不把整个外层等待时间当作 Worker 的计算额度。
本次接线没有提高 IFC Revision Worker 的 150 秒预算或 IFC Query Worker 的 45 秒预算。

Codex code-mode 使用测试在 Apply 工具启用时附加等待指引：初次等待约 30 秒；返回运行中
cell 时在同一 cell 上按 30–60 秒等待并及时收取完成结果，不反复请求一秒短等待或新建 Job
轮询。完整返回保存在会话变量后再投影简短摘要，发现错误先处理，不隐藏失败。该指引只改变
结果接收组织，不改变同步工具、执行预算、写入顺序或任何必要检查；它不证明已获得某个
Agent 用时百分比，实际使用效果须由对应原生调用证据单列。

只读 Query 超时不改变此前 Apply 的提交事实。应保留原 Attempt、利用已有失败修复入口调整
查询；文件大小使用上述专用字段，不能通过重新添加或删减产品绕过读取问题。

## 实现与验证所有者

公开请求与适配由 [products contracts/application](../../src/CAD2BIM_mcp/direct/ifc/products/contracts.py)
负责；按需资料由 [ifc_products tool docs](../../src/CAD2BIM_mcp/tool_docs/ifc_products.py) 提供。
Operation 发现和投影由 [Operation application](../../src/CAD2BIM_mcp/direct/operation/application.py)
负责；Runner 经[内部 IFC 授权入口](../../src/CAD2BIM_backend/internal_ifc_api.py)复用同一适配。
业务执行、原动作恢复与事务仍由既有 Core、Data Runtime、Writer 和 PostgreSQL Repository 各自负责。

公开封闭分支、Provider 投影和引用保真由
[Direct Provider owner 测试](../../tests/unit/mcp/test_direct_ifc_products_provider.py)覆盖；
原请求/身份、范围拒绝与报告诊断由
[Apply owner 测试](../../tests/unit/backend/test_product_apply_service.py)覆盖；
准确重放和原候选复用由
[Writer owner 测试](../../tests/unit/backend/test_ifc_revision_writer.py)覆盖；
持久化、四状态分页与 RLS 由
[durable kernel 集成测试](../../tests/integration/backend/test_ifc_model_durable_kernel.py)覆盖。
这些文件定义验证责任，不代替执行证据。已执行的工程、原生 Agent 累积添加、独立核收及
保留的失败统一由交付与证据入口承接。
