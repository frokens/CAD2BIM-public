# Code发现、Data Query Attempt与Data入口收敛as-built合同

```text
status: committed
verification: automated_release_gates_and_native_governance_verified_current
implemented_slice: I0-B3.04C
implementation_commits:
  code_discovery: b960492dc87d8e606208b10996a6d45c0ffc2cdc
  data_query_attempt: 9607069d15ae7e20f9cf9c4bb2a246fc71b6c4a9
  data_transform_entry: e6b5552a323c72230b57ee7c6715a167436ab475
  geometry_preview_cache: 2003076f071ff21416b82bc313a0de7181d8bdc2
  provider_and_worker_governance: 073779dd421f1569e77960a0451090254b3bbebb
slice_database_head: 20260904_0072
slice_direct_public_tools: 35
slice_ifc_prefixed_tools: 16
slice_preview_resource_templates: 1
slice_provider_projection_sha256: 092aa3d4501b1b0f735c73ac3c04f72b708217f66c0218b52cb6e4c3ea7ed7c9
data_query_attempt_stage_seed_business_closure: excluded_confirmed
extended_by_i0b3_06: product_family_authoring_attempt_and_realization_program
```

本文记录I0-B3.04C已经提交并验证的统一Code读取投影、Run级DataQueryAttempt、Data Transform
单入口、Geometry Preview render前缓存，以及Data Python Worker能力命名治理。真实promoted
foundation已完成完整性校验和两个隔离副本物化；自动门禁与重启后的独立原生Codex治理实验均已
完成，因此`.04C`当前状态为`committed / verified_current`。
上方工具数量与声明摘要是该切片提交快照；后续扩展见
[工具用法与执行边界治理](tool-usability-and-runtime-boundaries.md)，本文下方Data Worker章节
记录其现行计算环境与诊断合同。第3节已按B3.11实现与核收更新；其工程及两模型证据独立记录，
不将上方历史切片误写为本轮重新执行。当前证据边界见
实施接续。

## 1. 单一权威与统一Code读取

Data Transform 的 SQL/Python 源码统一上限为 131,072 UTF-8 字节（128 KiB），由
`domain/data_transform.py::MAX_TRANSFORM_CODE_BYTES` 维护；当前创建 Provider、Domain、
Worker 请求、exact 保存源码回读以及修复后的完整源码均使用该值。字符 Schema 同设 131,072，
实际容量仍以 UTF-8 字节核验。Query、CAD measurement、Geometry、JSON Schema、Markdown
容量及 CPU、墙钟、内存、输出、Worker 控制帧总量预算不随此变化。

统一 `code_read` 的 full 视图仍最多 65,536 UTF-8 字节；较大 Transform 源码使用现有
lines/search 分段读取，不截断源码。修复仍限制 1..16 个 literal edits、每段 16,384 字节、
合计 32,768 字节，只将修复后完整 Data Transform 源码上限扩为 128 KiB。共享 patch 默认
64 KiB 不变，Data Transform 调用显式传入其领域预算。

`code_list`和`code_read`是Application层只读投影，不是新Code产品平台：

```text
Provider code_list / code_read
→ Direct Session与InvocationScope
→ CodeDiscoveryService按code_kind路由
→ 原领域owner重新授权并解析exact绑定
→ ExactArtifactReader与TextSourceView
→ Provider-safe Card或full | lines | search结果
```

代码仍由实际拥有它的领域事实保存：

- Data Transform和CAD Measurement由`CriticalOperation`及其exact code Artifact拥有；
- SQL/Python Data Query由`DataQueryAttempt`拥有；
- 当前Geometry/Family源码由各自可编辑draft的准确内容快照拥有；写`CriticalOperation`仅保留本次保存快照的只读记录，不能作为修复候选；
- 已发布Geometry程序由`GeometryResourceVersion`拥有；
- Geometry和IFC调查源码分别由现有QueryAttempt拥有。

已发布Family程序通过`product_family_realization_program`发现。按卡片`owner_alias`匹配
准确Family版本，再将返回的`code_locator`与`members[].source_path`原样交给`code_read`；
Family或Operation alias不能代替源码locator。每页先扫描有界候选；当前Family合同结构
不合规的记录进入`rejections`，不发源码locator。诊断与Family list共用相同业务投影，
公开字段为`family_version_alias/version_no/code/field_path/reason`，不含完整合同或输入值。
`returned_count`只计合法Code卡片，`next_cursor`按扫描末行推进，合法卡片为空也可继续。
数据库和Storage等技术故障继续报错，不能被转换为目录拒绝。准确源码读取仍重新授权并
校验当前合同和资产，历史定位诊断不构成旧源码的读取入口。

当前闭合`code_kind`为：

```text
data_transform_attempt
cad_measurement_implementation
data_query_sql_attempt
data_query_python_attempt
geometry_authoring_attempt
geometry_resource_program
geometry_query_attempt
ifc_query_attempt
product_family_authoring_attempt
product_family_realization_program
geometry_draft
product_family_draft
```

`product_family_authoring_attempt`从原Write保存的来源读取源码：草稿JSON快照委托原
FamilyAuthoringService，当前SYSTEM copy源码ZIP经ExactArtifactReader与Source Bundle检查读取；
两者按Operation实际`detail_kind`分派，保留原Scope与字节完整性检查（Write不执行作者代码）。
`product_family_realization_program`从exact ProductFamilyVersion asset回读已发布Program。
它们继续使用原领域owner和同一`code_list/code_read`，没有Code表、Repository或通用写工具。

`geometry_draft`和`product_family_draft`只接收Write/Info返回的`draft_ref`，返回当前源码成员目录，
不分页。`code_locator`冻结读取时的snapshot；草稿修改后旧locator返回`CODE_DRAFT_CHANGED`，
重新`code_list`后再读。固定资产派生且没有源码的Geometry草稿返回空目录。
Info的draft分支提供当前业务定义和更新所需generation，源码正文仍沿`code_read`的full/lines/search读取。
案例输入、预期和执行报告属于Preview保存Operation，不进入Code。

首次目录调用使用`mode=first`及一个由`code_kind`判别的完整`query`；可分页Code类别的
`page_size`均默认20，只接受1至50的整数。成功空页由成功的结构化结果
`returned_count=0, codes=[]`表示；失败结果或缺少结构化结果不能解释为空目录。
`next_cursor`非空时，后续调用改用`mode=cursor`，只提交同一`direct_session_alias`和服务端原样
返回的`cursor`，不再提交首次`query`。

每个目录Card同时返回`owner_alias`和单独签发的`code_locator`：前者标识实际拥有源码的
Attempt、Operation、ResourceVersion或ProductFamilyVersion，后者才是`code_read`接受的读取
引用，两者不能互换。调用`code_read`时从同一Card复制`code_locator`和`source_form`；
`single_text`不提交`source_path`，`bundle`只提交该Card成员目录中`text_available=true`的
`source_path`。

当前Direct目录使用显式`DirectCodeCard/DirectCodeListResult`投影，复用原业务`CodeMember`。
Card保留源码形式、whole-source/bundle SHA、成员路径/SHA、owner/locator、终态诊断、repair资格、
结果引用和分页；不公开后台创建/结束时间与sequence。普通读取保留单源码SHA或bundle/member SHA，
不重复返回`selected_source_sha256`。省略该重复值前，Direct将TextSourceView的selected SHA与对应
权威source/member SHA比较；不一致报`CODE_INTEGRITY_ERROR`。此检查不改变原领域读取、Scope、
存储或完整性权威，投影错误捕获不包含上游服务调用。公开投影和既有真实DuckDB→MCP→Code消费者
已自动验证；B4五组中的实际Code/Operation、Data发布及repair消费者已独立接收，
范围和原始记录见本轮审计，不外推为所有未调用分支通过。

已知多个成员时，`code_read`还接受完整批次分支：

```json
{
  "direct_session_alias": "direct_001",
  "mode": "batch",
  "reads": [
    {"source_form": "bundle", "code_locator": "<同一Card返回的code_locator>", "source_path": "main.py", "view": {"view_mode": "full"}},
    {"source_form": "bundle", "code_locator": "<同一Card返回的code_locator>", "source_path": "layout.py", "view": {"view_mode": "full"}}
  ]
}
```

占位locator必须替换为服务器返回值，成员必须来自该Card。批次有1–8个完整single_text或bundle
选择，每项独立指定view；不重复session alias，不混入顶层单项字段。返回`mode=batch`及按输入
顺序排列的`reads`。每项仍通过原`CodeDiscoveryService.read_code`重新授权原owner并校验exact
源码，没有批次缓存、合并owner或新事务。单项读取合同保持不变。

相同选择重复出现报`CODE_BATCH_INVALID`；批次序列化响应限262144 UTF-8字节，超过时报
`CODE_BATCH_BUDGET_EXCEEDED`，应减少成员或使用有界lines/search。所有项成功才返回完整批次，
任何错误不返回partial结果；每项原有源码与view限制仍适用。成员目录和SHA用于识别已经读过的
同一owner/version/member，避免无依据重复全文读取；相同SHA不会合并不同owner或Scope。

`TextSourceViewService`拒绝不合法或超预算视图时，`code_read`保留有界提示并返回
`CODE_SOURCE_READ_INVALID`。例如已保存的Transform源码超过64 KiB时，full拒绝并提示改用
lines/search；使用同一exact locator分段读取，不重新提交Transform。批次沿原read_code路径
附带失败项索引。未知owner异常继续返回`CODE_OWNER_READ_FAILED`，不得把未知失败当作预期超限。

locator只负责导航；每次读取都重新执行Scope/RLS、owner、engine、MIME、size和SHA校验。相同
SHA不会合并不同owner、Scope、Attempt、Operation或Version。当前没有Code表、Code Repository、
Code事务、通用Code写工具、`code_NNN`身份或第二Storage。

固定资源补标注扩展后，`geometry_authoring_attempt`通过原Operation查询的
`require_code_artifact`条件在keyset分页前要求Code三元组均非空；不依赖历史detail JSON字段，也不在
页后丢弃记录。补标注版本若复制了原SOURCE_BUNDLE，它仍可作为exact几何生成来源回读，但无法单独
重建后补接口；接口权威为该版本manifest，参见[模型接口合同](geometry-model-interfaces.md)。

本切片同时把Package目录精确改名为`data_package_list`，把Geometry Resource的exact source
入口精确改名为`ifc_geometry_resource_source_read`。旧Provider名称没有保留转发alias；历史
Package、Operation、Artifact和ResourceVersion本身没有回写。

### Direct Source、Catalog与Operation结果边界

用户上传经后台转换的L0正式文件名由包详情中的`package.source_filename`提供；后台source摘要只含
原始size_bytes、dxf_version和encoding。Source Ingestion Operation里的filename是独立的持久恢复
事实，继续保留。Package list与describe的JSON摘要不再重复固定`schema_available=true`；
调用方用exact payload name请求data_describe_package，读取真正的payload_schema。

describe使用显式Direct投影，仅缩减JSON摘要的固定字段，复用完整Package、Parquet摘要和
PayloadSchema业务类型及原Domain跨字段校验。完整schema_json逐字保留，内部的sha、digest等
业务属性不被筛除。四处既有MCP消费者按公开DTO解析，直接使用Backend服务的消费者维持原类型。

Operation公开详情不再序列化IFC Products内部request_json，也不公开Geometry worker/budget与
Family runtime/budget配置。原Domain事实保持，列表的sequence/时间/intent/终态、完整产品ledger、
Transform inline-table columns/rows、错误phase/message、bundle/member SHA和repair判断仍返回。
不能把不公开内部配置推广为删掉任意请求JSON、查询数据或完整恢复输入。

Python query的program_api资源保留Arrow程序返回ABI及完整API/示例/repair/失败边界，
`mcp_result_navigation`列出row_schema、rows、page、query_statistics的用途及分页终止条件。
客户端实际提供list_tools/outputSchema时可读取完整注册输出合同；未提供时沿资源中的字段指导
和同次实际结果消费，不要求不存在的Schema读取API或猜测工具。page.next_cursor原样续页，
complete=true对应remaining_count=0及next_cursor=null。资源不复制完整工具输出Schema；
程序内返回Arrow对象与工具返回分页JSON是两个不同的消费边界。B4 Data实际读取该指导并消费
只读Query行/分页结果，与SQL及repair发布结果核对；不以此声称客户端提供独立outputSchema读取API。

## 2. Run级DataQueryAttempt

SQL与Python保留两个不同的执行引擎和公开工具，但共用一个中性Attempt生命周期：

```text
Provider fresh | repair | cursor
→ 固定当前Run和exact Package/Payload输入
→ source ArtifactVersion先持久化
→ PREPARED DataQueryAttempt绑定engine、源码和输入
→ 原Data Query Worker执行
├── SUCCEEDED：绑定有界临时result snapshot metadata
└── FAILED：保存稳定诊断，源码仍可由Code投影发现和读取
```

`fresh`创建根Attempt；`repair`从不可变父Attempt exact源码和输入开始，执行有界literal patch；
调用者仅给父Alias和edits，后台继续校验父Artifact字节完整性，不要求回传源码SHA。
`cursor`只读取同一进程内的临时结果快照。结果快照到期不会删除Attempt、
exact源码、输入绑定或终态诊断。DataQueryAttempt不创建CriticalOperation、Data Package或持久结果
Artifact，也不冒充业务发布历史。

Alembic `20260904_0071_data_query_attempts.py`建立Run-bound Attempt、exact输入、guard、RLS和
角色权限。状态只允许`PREPARED → SUCCEEDED | FAILED`；source-first残留和响应丢失通过同一
request identity重新对账。进程内single-flight只防止同进程重复执行；数据库身份与终态仍是
跨重启权威。terminal publication保留15秒，另留5秒清理；执行与排队不能侵占终态预算，
精确Q/H/T/C见[统一运行边界](tool-usability-and-runtime-boundaries.md)。

`DataQueryAttempt`是调查/修复记录，不是G Stage恢复业务状态所需事实，因此已经确认排除在
Stage Seed业务闭包之外。目标空数据库证明、migration、RLS、源码恢复和原生治理实验仍覆盖它。

## 3. Data派生唯一公开入口

当前工程公开入口已替换为`data_derive`，统一承接Data派生和IFC提取，保留create／repair。
完整来源与只读边界见[阶段合同](data-derive.md)；
B3.11工程与必要原生验证已核收；新增／复用／未覆盖范围与已知问题见稳定合同及上述过程记录。

公开面只有一个`data_derive`，其Provider Schema明确表达create/repair及各engine/source完整、封闭分支：

1. `create + duckdb_sql`，至少一个Package，不接收IFC来源；
2. `create + python_arrow`，至少一个Package，可同时绑定`ifc_source=current`；
3. `create + python_arrow`，无Package时必须绑定`ifc_source=current`；
4. `repair + failed_operation`；
5. `repair + succeeded_package`。

Provider收敛没有合并两个Worker或复制Application Core。create继续调用现有`transform()`，repair
继续调用现有`repair_transform()`；两者复用同一个DataTransformService、Artifact/Package、
CriticalOperation、Storage、响应丢失对账和原子发布事务。

新Operation统一保存`tool_name=data_derive`；当前repair父项及transform_input读取仅接受这个统一入口。
共享Operation种类仍为data_transform，不新增类别。已有历史data_transform／data_repair_transform
不可变行不被改写，可由通用Operation读取，但不恢复为当前执行或repair父项合同。

create的description_markdown用set，outputs用set完整列表，每个JSON schema_json字符串用set action承载
完整object或boolean Schema。repair可省略代码edits，仅改包名称、description_markdown set/edit或
outputs set/edit。outputs.edit按payload_name添加、替换和删除完整小声明，同名现存JSON项可用
schema_json action=edit修改保存的Schema正文；新项及outputs.set须schema set。最终重新验证完整Data
Schema方言、records array/items及无远程引用约束，不使用Geometry参数Schema编辑算法。

DataTransformCommand、Worker和持久resolved command继续保存完整值；输入动作壳只表达待合并请求。
说明变化建立新Artifact，未变才复用父Artifact。所有repair均重新执行新Operation，成功生成新Package；
inputs、engine、parameters、inline_tables和预算继承，原Package保持不变，至少一项有效变化。
IFC来源同样继承准确model／revision，不暴露repair换源字段；新repair接纳及发布时核对CURRENT，
变化则不发布新Package。create执行固定来源，原成功动作的恢复先对账原结果，不受之后CURRENT
推进影响。Package-only不引入IFC版本检查。
共享SourcePatchPort由Data传原源码128KiB与说明/Schema64KiB上限；公开输入不再索取不可变父来源SHA。

Data Transform的默认`operation_get`返回导航，不展开原参数值或内联表行。
`detail.inputs/outputs/parameters/inline_tables`分别是原元数据页，包含`total_count`、`start_index`、
`items`、`returned_count`、`complete`及`next_index`。输入项给原Package、payload和真实逻辑键；
输出项给原声明；参数项只给名称、类型和值数量；内联表项只给名称、行列数量。
代码位置由现有CodeDiscovery签发`code_locator`，可直接交给`code_read`；不根据Operation引用拼接定位符。

原值继续由同一`operation_get`的`mode=transform_input`读取，必须指定原`operation_alias`。
读取选择为封闭分支：

| input_kind | 请求选择与返回 |
| --- | --- |
| `metadata` | 指定`section=inputs/outputs/parameters/inline_tables`、`start_index`和`item_count`；按原序续读元数据页 |
| `inline_schema` | 指定`input_name`、`start_column/column_count`；读取真实列定义，返回`next_column` |
| `inline_rows` | 指定`input_name`、零起点`start_row`、`row_count`及可选`select`列；按原物理顺序返回原行、总行数及`next_row` |
| `inline_text` | 指定`input_name`、`row_index`、`column_name`、`start_character/character_count`；按Unicode字符区间续读原字符串单元格 |
| `parameter` | 用`parameter_name`读取一个原始带类型参数值 |
| `package_description` | 读取原说明，`view`复用`full/lines/search` |
| `output_schema` | 用`payload_name`读取原Schema文本，`view`复用`full/lines/search` |

元数据与原值均使用既有64KiB响应边界，实际编码过大时页可以少于请求数量，必须按返回的续读位置继续。
单行过大时选择更少列；单个字符串过大时用`inline_text`，不能因超限丢掉该行或把输出表当原输入。
`complete`描述该次选择的读取是否结束，不表示Operation成功或业务验收完成。
Direct、Runner及internal route共享DataRuntimeService原不可变输入读取owner；成功、失败且没有输出Package的
Operation均可调查。读取不创建Package或Operation，也不改写已保存原值；repair仍在后台继承原完整命令，
不把公开导航重新拼装为可执行输入。普通输出业务行继续通过Data读取。

Alembic `20260904_0072_unify_data_transform_entry.py`只扩展现有CriticalOperation detail CHECK。
它不回写历史行；存在新统一repair事实时降级fail closed。原CAD Measurement Operation允许的
description Artifact合同保持不变。

## 4. Geometry Preview render前缓存

Geometry Preview仍由现有`GeometryPreviewService`、唯一Geometry Runtime、ArtifactService、
opaque capability和Resource handler承担。请求摘要在render之前计算，至少绑定：

- cache合同与renderer profile版本；
- owner、Task和Run；
- exact GeometryResourceVersion、内容摘要和资源路径kind；
- sample parameters是否显式出现及其canonical值；
-固定Render Profile。

命中缓存时，服务先在当前身份和RLS下重新解析exact Geometry version，再校验Artifact/metadata/
实际PNG三方SHA、MIME和size，最后为当前调用者重新签发capability。缓存共享边界是同一owner/
Task/Run；capability继续绑定具体principal/actor/tenant/task/owner/run。因此同一Run的另一个合法
principal可以复用PNG，但不能复用旧调用者的能力令牌。

一次Preview仍只有一份`geometry_preview` Artifact。内联`ImageContent`和`ResourceLink`读取同一
份已保存PNG；缓存命中不再次render、不建立第二Preview事实源。`mapped_model`拒绝任何显式
sample parameters，包括空对象；`modeling_function`继续使用exact显式参数做私有构建。缓存损坏
或完整性不一致时fail closed，不用重新render掩盖持久事实损坏。

## 5. Data Python Worker能力命名

受控Python执行环境当前统一称为`Data Python Worker`。正式配置、脚本、attestation和本地运行目录
分别是：

```text
CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE
CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE
scripts/setup/setup_data_python_worker_env.py
.cad2bim-data-python-worker.json
.venv/data-python-worker
```

I0-B3.04C只替换当时运行面中的阶段编号命名。历史migration文件名、持久fingerprint字面量、旧Operation
名称和历史实验记录原样保留，因为它们是既有数据库或证据身份。Data Python Worker继续拥有自己的依赖、
协议、预算、attestation和失败语义；后续IFC、Geometry或Realization执行环境只能复用真正中性的监督、
IPC和OS隔离机制，不能与Data Python Worker合并成一个通用任意代码进程。

`python_worker_environment.py`中的8个child源码路径定义Seatbelt文件读取白名单。
launcher从Host当前授权的源码根启动child，第三方计算依赖由既有Worker解释器环境提供。
源码路径必须准确、唯一且指向普通文件；缺失、越界或多package root歧义在启动边界拒绝。
计算环境attestation记录批准库、实际第三方distribution闭包与解释器身份；Direct启动只校验
必要的解释器环境、数据库和存储，不前置各Worker／Runtime真实样例。批准读取根、沙箱及真实child
readiness沿独立setup/check验证；实际业务调用仍保留监督、容量、预算及输入／产物校验。
当前运行源码、预算和项目版本按Host部署生效。
已有计算环境可随Host源码更新继续使用，计算依赖或解释器需求改变时才调整环境。
setup沿现有sdist安装流程构建环境，从`uv.lock`解析计算依赖闭包；环境校验比较实际
第三方依赖与解释器。旧环境记录的额外字段按非合同元数据处理，现有环境记录可直接读取。

setup、check和启动使用同一`DataPythonWorkerEnvironmentError`及稳定类别：
`DATA_PYTHON_WORKER_SOURCE_MISMATCH`、`DEPENDENCY_MISMATCH`、`INTERPRETER_MISMATCH`、
`SANDBOX_FAILURE`、`CHILD_READINESS_FAILED`、`ENVIRONMENT_INVALID`（后五项也有相同前缀）。
setup/check的child readiness实际复用Darwin Seatbelt命令；沙箱构造/启动错误与child协议/输出失败
分开。`SOURCE_MISMATCH`表示可信child源码路径解析失败，环境依赖差异使用`DEPENDENCY_MISMATCH`。
恢复命令保留实际Host venv解释器路径，绑定目标env及可信checkout的uv.lock；安装包找不到
源码checkout时明确说明，不生成不存在的绝对路径。沙箱失败要求恢复sandbox-exec后再check，
不误导重建Worker。Direct数据库readiness独立返回`DATABASE_READINESS_FAILED`且不暴露连接凭据。

## 6. 实现位置

- Code读投影：`src/CAD2BIM_backend/application/code_discovery_service.py`、
  `src/CAD2BIM_mcp/direct/code/`；
- DataQueryAttempt：`src/CAD2BIM_backend/domain/data_query_attempt.py`、
  `src/CAD2BIM_backend/application/data_query_attempt_service.py`、
  `src/CAD2BIM_backend/ports/data_query_attempts.py`、
  `src/CAD2BIM_backend/infrastructure/postgres/repository_data_query_attempt.py`；
- SQL/Python领域入口：`src/CAD2BIM_backend/application/data_query_sql_service.py`、
  `data_query_python_service.py`和`src/CAD2BIM_mcp/direct/query/`；
- Data Transform单入口：`src/CAD2BIM_mcp/direct/transform/`、
  `src/CAD2BIM_backend/application/data_transform_service.py`和既有Data Runtime Repository；
- Preview缓存：`src/CAD2BIM_backend/application/geometry_preview_service.py`；
- migration：`src/CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260904_0071_data_query_attempts.py`
  与`20260904_0072_unify_data_transform_entry.py`；
- 唯一注册顺序：`src/CAD2BIM_mcp/direct/server.py`。

## 7. 既有切片验证证据

以下证据属于本文件开头标识的既有切片及治理轮次，保留其原范围，不表示B3.11重新运行或通过。
B3.11当前新增、复用与未执行范围见实施接续及其工程证据索引。

既有轮次已验证：

- CAD/Data正式门禁：56个门禁节点，内部585项测试通过；
- IFC Model正式门禁：43个kernel节点与14个Direct-entry节点通过；
- IFC Golden Seed正式门禁：13个节点通过；
- 默认测试全集：1044项通过、64项按marker排除；
- Ruff、compileall和离线wheel构建/安装通过；wheel包含352个成员、4个CLI入口，Geometry Worker探针通过；
- promoted foundation根摘要完整性通过，并已物化A/B两个隔离数据库与Storage副本；
- 原生SQL/Python Data Query都完成失败Attempt、`code_list/code_read`重新发现、SHA保护repair和结果游标读取；
- 原生历史Transform链可由新`code_list/code_read`读取，并通过唯一
  `data_transform(mode=repair)`创建新统一Operation和Package；
- 重启后的最终原生探针从`op_015/data_012`读取exact源码SHA
  `233ca0ddc7551a01bfff8493d7891b75173a347070373c1af9bbfecd14d0182b`，随后成功返回
  `op_016/data_013`，且只使用`code_list → code_read → data_transform`；
- A副本在治理实验后具有4个DataQueryAttempt、16个Operation、13个Package、1个IfcModel和2个IFC
  revision；B副本继续保持0/13/10/1/2，二者IFC revision 1/2 SHA分别保持
  `8f0e390a7a4e7098a5543a03dda1a23b8c447e9107c7ccb03a5fdeb7e86507ac`与
  `dbf8720c8f8b1e1ab6413a2529260bf352e92a40fb9c7be1ffba368a0321327e`；
- 该历史轮次的`data_repair_transform`行保持原值；当时统一repair保存`tool_name=data_transform`并指向exact直接父项。当前入口与父项资格已由第3节替换；
- 该切片提交时Provider冻结为35个Direct工具、16个`ifc_`工具和1个Preview ResourceTemplate；递归Schema中没有
  `unknown`。该历史轮次repair公开SHA用法已被原字段局部编辑合同替换；当前读取仍标识exact全文，但repair无需回传不可变父来源摘要。

原生第一次重跑曾因只在父Codex进程设置环境变量、未把必需变量写入MCP server的显式`env`配置而在
握手期关闭。直接启动同一stdio入口证明Backend就绪；将六个必需变量显式绑定到该MCP server后握手和
读写闭环通过。一次写调用的最终终端输出在Desktop重启时丢失，但数据库已经证明它只提交一个成功
Operation/Package；为避免把数据库终态误当Agent可见终态，随后又执行了上述完整、可观察的原生闭环。
