# IFC源码查询与exact回读as-built合同

```text
status: committed
implemented_slice: I0-B3.04
verification: unit_contract_real_postgres_ifc_cad_data_seed_wheel_static_compose_and_native_provider_passed_with_observations
native_provider_experiment: passed_with_observations
provider_surface_convergence: I0-B3.04R_committed_verified_current
code_read_convergence: I0-B3.04C_committed_verified_current
database_head: 20260904_0072
public_tools: ifc_query_python; code_list/code_read(code_kind=ifc_query_attempt)
business_effect: read_only_ifc; durable_query_audit_only
```

本文记录I0-B3.04已经形成的源码先持久化、server-only exact IFC固定、隔离IfcOpenShell查询、
源码repair、结果分页与响应丢失对账能力。它是后续产品族和产品工具调查CURRENT IFC的共享READ范式，
不创建IFC revision、不推进CURRENT、不创建关键IFC Operation，也不发布Data Package。

## 1. 公开工具

| 工具 | 调度 | 当前职责 |
| --- | --- | --- |
| `ifc_query_python` | READ | `fresh`固定当前Run的exact CURRENT并执行新源码；`repair`修补父attempt源码且继续读取父exact IFC/输入；`cursor`只读同一持久结果的后续页 |
| `code_list` / `code_read` | READ | 以`ifc_query_attempt`发现可见Attempt，再对其exact源码执行`full`、`lines`或区分大小写的literal `search` |

I0-B3.04提交时两个新工具追加在既有39个工具之后；I0-B3.04R收敛后为37个工具。I0-B3.04C进一步以
`code_list/code_read`统一代码发现和回读；该切片提交时Direct Provider共35个工具，其中16个以`ifc_`开头。Provider只接收
`direct_session_alias`、查询源码、安全attempt
alias、literal edits、分页参数以及可选的Package/Payload alias；它不接收或返回IfcModel、revision、
ArtifactVersion、Storage路径、数据库身份或writer身份。

`ifc_query_python`的三种封闭输入分支是：

```text
fresh  = code + zero-to-16 exact Package/Payload bindings + page_size
repair = parent_attempt_alias + one-to-16 literal edits + page_size
cursor = unchanged opaque cursor
```

成功返回安全`attempt_alias`、可选父alias、exact源码SHA、固定`succeeded`状态、有界摘要、RFC 8785
canonical `result_json`与可选`next_cursor`。源码或执行失败返回稳定ToolError；一旦源码已持久化，安全
diagnostic同时包含可供源码回读和repair使用的`attempt_alias`。

## 2. 单一exact读取链

fresh调用按以下顺序执行：

```text
Direct Session → InvocationScope
→ IfcRevisionReader.pin_current固定一个server-only exact revision
→ 解析零到16个当前Scope可见的exact Package/Payload
→ 保存exact查询源码ArtifactVersion
→ 以PREPARED创建IfcQueryAttempt并绑定exact IFC、源码和输入
→ 从Artifact/Storage重新打开exact源码、IFC字节和Arrow输入
→ 隔离Worker在私有IfcOpenShell模型上执行query(ctx)
→ 保存canonical结果ArtifactVersion
→ PREPARED原位终结为SUCCEEDED或FAILED
→ 投影有界Provider响应
```

`IfcRevisionReader`从原`IfcRevisionWriter`中抽离为中性READ能力。Direct组合根只构造一个reader实例，
并把同一实例注入writer和query service；因此IFC写工具和IFC Query共享同一套revision/ArtifactVersion/
Storage字节一致性校验，没有平行exact reader或第二套CURRENT事实源。

## 3. QueryAttempt与Artifact事实

Alembic `20260903_0069`新增：

- `ifc_query_attempts`：绑定owner/tenant/task/run、exact IfcModel revision、exact源码ArtifactVersion、
  可选repair父attempt、request identity/digest、输入数、server预算、终态、结果ArtifactVersion和安全诊断；
- `ifc_query_inputs`：按顺序绑定输入名、exact Data Package、exact Payload member和可选exact schema member；
- RLS与数据库guard：API只读，MCP/Runner可插入和执行唯一终态更新；跨Tenant/Task/Run不可见；无公开删除；
  PREPARED只能一次性变为SUCCEEDED或FAILED，身份和exact绑定不可改写；
- QueryAttempt的source/result外键同时绑定owner、Task、Run与exact Artifact，数据库拒绝跨Run借用查询
  Artifact；`ifc_query_source`和`ifc_query_result`各自只有一个不可变ArtifactVersion，API/Runner/MCP均
  无法原地改写已经发布的查询字节或摘要；
- Package输入必须在本Run的Data Catalog中可见并处于可选frontier内。Package可以来源于先前Run，
  但当前Run必须拥有显式Catalog可见性；Repository和数据库guard共同复核该事实。

查询源码和成功结果进入既有Artifact/ArtifactVersion与Storage链；IFC本体继续由既有immutable IFC
revision绑定的exact ArtifactVersion保存。QueryAttempt是查询审计、repair和cursor的机器权威，关键IFC
Operation仍只记录业务IFC写入。普通查询不会用Operation、Artifact current或另一个revision字段表达相同事实。

## 4. Worker与`query(ctx)`

受控源码必须定义唯一同步入口：

```python
def query(ctx):
    model = ctx.model()
    table = ctx.table("optional_parquet_input")
    value = ctx.json("optional_json_input")
    ctx.log("bounded diagnostic")
    return {"projects": len(model.by_type("IfcProject"))}
```

稳定Context表面只有：

- `ctx.model()`：后台从exact bytes重建的真实、进程私有`ifcopenshell.file`；须按只读合同使用，
  不是类型层不可变对象，也不是只模拟几个读取方法的代理；
- `ctx.host_sections(product_global_id)`：同一私有模型中实际直管 Body 的有界读取，复用
  既有Body事实提取器；返回原Axis、米制站位区间、截面及`sections[].body_section_ref`。
  支持域见下文。当前Family不接收此引用作为Host输入，它也不授权修改IFC；
  不支持/歧义错误的 diagnostic 保留 `PRODUCT_REALIZATION_HOST_*` 原因码；
- `ctx.table(name)`与`ctx.batches(name)`：显式绑定的exact Parquet Payload；
- `ctx.json(name)`：显式绑定的exact JSON Payload；
- `ctx.log(message)`：最多8 KiB的受控诊断。

允许的import root是`collections`、`decimal`、`fractions`、`functools`、`ifcopenshell`、`itertools`、
`json`、`math`、`operator`、`pyarrow`、`re`和`statistics`。运行时不提供Repository、Storage、durable
writer、CURRENT、Operation、Package publisher、宿主路径、网络或子进程权限。

`query(ctx)`返回JSON标量、list或字符串键dict；浮点数必须有限，不直接接受tuple、原生IFC
实体或Arrow表。`get_info(recursive=True)`仍可能保留tuple聚合，不能视为结果已经JSON安全。
调用方应显式投影所需基本值及list，或对本来可序列化的结构作适当转换，不以`default=str`
掩盖原生对象。结构校验拒绝时，实际ToolError及持久Attempt保留有界`json_path`、`value_type`
和`reason`，不返回原值；安全`attempt_alias`仍可用于准确源码读取与repair。补充诊断不改变
既有接受类型，也不回写旧失败记录。

读取有效材料等继承信息时，实例的`HasAssociations`不会枚举其所赋Type上的关联。
应依任务和实际IFC语义区分实例与Type来源及适用覆盖关系，不能把没有实例直接关联当作
没有有效材料；这与只读接口是否允许查询是不同职责。

exact IFC和Arrow输入由Host写入受控staging，以只读FD打开后立即unlink路径，再把FD继承给child。
因此用户源码既拿不到源文件路径，也无法通过`ifcopenshell.open()`重新打开它；AST策略同时拒绝直接和
alias形式的文件opener。

### 实际直管Body读取

`host_sections`只读取IfcPipeSegment：唯一Axis/Curve3D由两点IfcPolyline或无segments的两点
IfcIndexedPolyCurve定义，唯一Body含1—64个共线IfcSweptDiskSolid。实际Body段必须位于原Axis
内、截面内外半径一致，段间不能重叠、重复或相接，不推断内部端口。单位与placement由同次
exact IFC读取；超出支持域、歧义或退化返回typed诊断。

section引用绑定IFC SHA、项目/产品、Axis/截面签名、Body digest和派生section key；该引用
不是Scope授权。现有内部选择器仅允许唯一未变段或保留所选原边界的唯一缩短子段重绑定，
边界消失、增长、截面/Axis/placement变化或映射歧义均要求重新发现。读取一致性容差1e-9米，
不作为制造或安装误差阈值。当前事实定义与提取仍由
[`product_realization_host.py`](../../src/CAD2BIM_backend/domain/product_realization_host.py)及其
[IFC提取实现](../../src/CAD2BIM_backend/infrastructure/ifc/product_realization_host.py)拥有；
Family Runtime不依赖该读取器，自动裁管/修改和写入衔接属于后续切片。

## 5. mutation拒绝

业务上只读不能仅依赖“私有副本最终看起来没变”。Worker执行以下检查：

1. 原始IFC字节SHA必须等于Application固定的exact revision SHA；
2. 执行前开启IfcOpenShell transaction，共用`PrivateModelAudit`独立计数create/delete/edit，
   包括修改后恢复及随后清空公开operation记录；
3. 私有模型执行前后两次序列化SHA必须相同，用于捕获header或低层状态变化。

AST仍拒绝属性写入/删除、模型mutation方法、低层wrapper、transaction/history/undo/redo和文件写入。
普通Python字典、list和Counter可以进行索引赋值、累加、切片及删除，用于局部聚合；不再把
Subscript Store/Del一律判为IFC写入。普通`set.add/list.remove`可用于局部整理；静态拒绝可识别的
模型同名写方法，私有模型自身与运行审计继续阻止实际修改，不开发通用Python类型推断。
IFC实体的索引赋值及`operator.setitem`由上述事务过程审计拒绝，赋值后还原也失败；IfcOpenShell
不支持的实体索引删除返回实际`IFC_QUERY_EXECUTION_FAILED`，不冒称发生了模型修改。
属性写入的`code_validation`诊断继续返回首个违规节点的源码行列、AST节点类型及`assign/delete`操作，
便于读取exact源码后repair。
Python动态调用不能仅靠AST穷尽判断；编译通过仍须完成运行审计。已检测的修改以
`IFC_QUERY_MUTATION_DETECTED`或稳定的源码错误失败，不交付查询结果。该机制不是对任意
native绕过、清除记录再还原或全部动态调用的完备检测证明。

正式模型的保护还来自隔离子进程、准确输入的私有对象、只读FD及没有durable Writer／提交权限；
私有模型即使被改动，也不改变持久源IFC、CURRENT、revision、关键Operation和Package。
作者代码不得打开／保存文件、调用模型write或任意导出。当前Python audit与静态规则拒绝已知
I/O；当前IFC adapter还显式关闭Seatbelt的scratch路径写入，只保留Host预建的声明输出FD。
普通与代表性native路径保存负例已有工程证据，这不构成对所有Python/native绕过的完备证明。
共用保护的本轮实现与聚焦证据见实施核收记录，
B3.11必要工程、两模型及诊断恢复已核收；当前派生合同见[data_derive](data-derive.md)，不改变Query有界返回或repair父快照语义。

## 6. repair、cursor和恢复

- fresh始终在任何源码保存或Worker执行前固定一次CURRENT；调用过程中其他writer推进CURRENT时，本次查询
  仍只读已固定的旧exact revision。
- repair按父attempt的exact IfcModel revision、exact Package/Payload和exact源码继续；它不重新选择最新
  CURRENT。调用者仅给父attempt alias与edits，后台读取父源码时核验已保存Artifact的SHA/字节完整性；
  完整性不匹配在创建新源码、attempt或Worker之前失败，不要求调用者重复提交父源码SHA。
- repair使用既有Textum literal patch语义，经中性的`SourcePatchPort`/`TextSourcePatchAdapter`复用，
  IFC Application不直接依赖Infrastructure，也不复制patch算法。
- cursor绑定Scope摘要、attempt、exact结果ArtifactVersion及SHA、offset、page size和固定15分钟到期时间；
  continuation重新做RLS和exact Artifact校验，只读取持久结果，不重新执行源码，也不延长到期时间。
- 顶层list按记录分页；dict和标量作为一个结果项。工具的 `result_json` 始终编码当前页的 JSON
  数组：返回 dict 得到 `[dict]`，返回标量得到 `[scalar]`，返回顶层 list 时按其元素分页。解码后
  先读取数组中的结果项，再读取对象字段。一次完整结果最多256 KiB，单页canonical JSON最多
  65,536 UTF-8 bytes，`page_size`最多200。
- 同一request identity重放SUCCEEDED或FAILED终态时直接回读持久事实。终态数据库提交后的响应丢失通过
  reread对账；状态仍为PREPARED且结果未知时返回`IFC_QUERY_RECONCILIATION_REQUIRED`，不会再次运行Worker
  或把潜在成功伪造成FAILED。

## 7. 服务器预算

当前Worker固定预算为：45秒wall、45秒CPU、5 GiB RSS、128 MiB scratch额度、4 threads、64 FDs，
stdout/stderr各16 KiB。exact输入最多16个、合计5,000,000行和256 MiB Arrow；源码最多65,536
UTF-8 bytes。scratch资源额度不授予作者代码路径写入权限；当前IFC执行关闭scratch路径写入，
实际权限与只读审计见第5节。

整次Application调用包含受监督的exact IFC inspection，Q/H/T/C为600/230/15/5秒，
总期限850秒，Direct外层860秒。inspection和Query child使用同一执行lease，既有单child
资源上限不变。取消与容量诊断沿Attempt保存，详见[统一运行边界](tool-usability-and-runtime-boundaries.md)。

预算来自对当前代表性241,857,160-byte Golden Candidate的实际characterization：1 GiB RSS配置在
0.82秒内正确早停；临时6 GiB配置成功测得峰值约3,242,426,368 bytes RSS、21.42 CPU秒；最终5 GiB/
45秒生产配置以约21.69秒完成Project、Site、Product和Context调查。该测试用于固定资源边界，不把
Golden Candidate晋级为用户批准模型。

用户指定的404,950,864字节`08-final.ifc`已在同一生产预算下完成后台Query：整条调用
50.743秒，包含13.416秒前置inspection进程及36.109秒Query进程；RSS采样峰值约4.220GiB。
完整事实核验、2秒测试侧wall失败、资源释放及后续合法调用证据见
[真实大文件补验](data-derive.md#11-真实大文件补验)。这不是大文件MCP transport或新增Agent
业务实测，也不以输入字节上限保证任意模型的计算与内存需求。

## 8. 实现位置

- Domain：`src/CAD2BIM_backend/domain/ifc_query.py`
- Application：`src/CAD2BIM_backend/application/ifc_query_service.py`、
  `src/CAD2BIM_backend/application/ifc_revision_reader.py`
- Ports：`src/CAD2BIM_backend/ports/ifc_queries.py`、`ifc_query_worker.py`、
  `ifc_revision_reader.py`、`source_patch.py`
- Infrastructure：`src/CAD2BIM_backend/infrastructure/ifc_query/`、
  `infrastructure/postgres/repository_ifc_query.py`、`infrastructure/source_text/source_patch_adapter.py`
- Migration：`src/CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260903_0069_ifc_query_attempts.py`
- Direct Provider：`src/CAD2BIM_mcp/direct/ifc/query/`和共享入口路由`src/CAD2BIM_mcp/direct/ifc/query_attempt/`
- Composition root：`src/CAD2BIM_mcp/direct/bootstrap.py`

## 9. 当前验证与边界

当前已通过：

- 当前默认单元/合同全集967项，55项按marker排除；
- 隔离PostgreSQL下新Query纵向链、RLS、migration和既有IFC durable回归；
- `0069 → 0068 → 0069`空库真实回退/升级通过；存在QueryAttempt或查询Artifact历史时，降级会
  在删除任何对象前原子拒绝；同Task不同Run、同租户不同Task与跨租户反例均由真实角色覆盖；
- IFC正式allowlist、CAD/Data正式allowlist与Golden Seed确定性门禁；
- Provider递归Schema、该切片工具冻结顺序、worker mutation/隔离/readiness、cursor、repair、响应丢失和重启回读；
- wheel真实构建/安装（333个成员、4个CLI入口、Geometry Worker探针）、测试收集策略和Compose静态检查；
- Ruff规则检查和`git diff --check`。

独立Codex原生Provider实验在I0-B3.04当时的公开名称
`runtime_open_direct_session`、`ifc_query_python`和`ifc_query_source_read`白名单下完成，状态为
`passed_with_observations`。当前as-built源码发现与回读已经替换为`code_list/code_read`，其Provider契约、
Application路由和exact回读已由自动化门禁覆盖：

- Agent只获得`direct_001`和调查目标，没有获得模型SHA、实体数量、GlobalId、预期答案、数据库或
  Artifact身份；没有使用shell、仓库、文件系统、Web或其他CAD/Data能力；
- 补充使用原创建名称调用`runtime_open_direct_session`时返回同一个`direct_001`并明确
  `resumed=true`，证明同名Direct入口恢复既有Task/Run绑定，没有创建第二个会话；
- 一个确定性`NameError`先形成FAILED attempt，再通过`source_read(full)`取得exact源码与SHA，最后由
  `repair`的一处literal edit在父attempt固定的exact IFC上成功；
- 综合调查第一次因临时字典/Counter的下标赋值在AST预检阶段零执行失败，随后读取265行exact源码，
  以10处不重叠exact edits改为纯函数式聚合并成功；该观察已转化为更明确的工具描述和带源码位置的
  mutation diagnostic；
- 成功结果包含5个顶层记录，Agent通过opaque cursor按`2 + 2 + 1`读取全部三页；continuation复用同一
  attempt、源码SHA和持久化结果，没有重跑Python；
- 陌生模型调查实际识别出IFC4X3、一个Project、4个Representation Context、3个Site组成的空间树、
  73,316个IfcProduct及6类RepresentationType分布。该结果只证明Provider可理解性与调查能力，不构成
  Golden Candidate业务批准；
- 实验后数据库仍只有1个IfcModel、revision 1、一个CURRENT和`write_epoch=1`；IFC数据库绑定SHA与实际
  存储字节SHA仍为`02aade531690f7a4624f64ed0f10ba47b416f052f223160c33c9c7dbb2a210a7`；关键
  Operation仍为1、Data Package仍为0。新增事实仅为2个FAILED、2个SUCCEEDED QueryAttempt、4份查询
  源码Artifact和2份成功结果Artifact。

B3.04/B3.04R/B3.04C/B3.04W当轮没有实现ProductFamily、Realization Program、产品实例化、Host split/trim、
产品READ或IFC→Package提取。B3.04C完成Code读取投影、DataQueryAttempt、Data Transform入口和
Preview缓存治理，B3.04W完成IFC Revision Worker迁移。后续实现状态沿当前导航
读取；B3.11提取发布现已由[data_derive](data-derive.md)实现并核收，其证据与本节历史验收分别记录。
