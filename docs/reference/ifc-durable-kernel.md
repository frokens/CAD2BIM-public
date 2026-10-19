# I0-A / I0-B0 / I0-B1 / I0-B2 durable IFC模型运行时as-built合同

```text
status: I0-A_I0-B0_I0-B1_I0-B2_committed
verification: verified_current
alembic_revision: 20260830_0064
ifc_schema: IFC4X3_ADD2
public_ifc_mcp_tools: ifc_model_initialize, ifc_model_info, ifc_model_configure_geometry_basis, ifc_representation_context_list, ifc_project_update, ifc_spatial_structure_list, ifc_spatial_structure_apply
direct_tool_count: 28
post_i0b3_04w_execution_boundary: committed_at_12fd9b1a1bf2f3168d74ae1a2ef4180addae00c3 / verified_current
```

本文记录已实现的I0-A、I0-B0、I0-B1与I0-B2精确合同。I0-B2已于
`0ab30255b8714836340ecaace81247270dcc2a18`提交。这些能力已在fresh migration后的隔离PostgreSQL、Artifact存储和
当前Provider投影上重新验证。

## I0-B0/I0-B1已实现增量

当前I0-B0 as-built增量为：

- `ifc_model_initialize`必填LLM提供的`project_name`，写入`IfcProject.Name`并纳入request digest和安全Operation输入投影；
- initialize/info共享必需的嵌套`project {global_id, name}`摘要；`ifc_model_info`未初始化时返回
  `IFC_MODEL_NOT_INITIALIZED`，不再返回`initialized=false`成功对象；
- `geometry_basis_ready`改名为`geometry_basis_configured`，`product_count`改名为`element_count`；
- `ifc_model_info`增加从exact IFC回读的nullable `geometry_basis`，固定用途数组已经移除；完整Context事实由
  `ifc_representation_context_list`返回；
- 新增调用监督、有界内部重试、model-less Run系统暂停与reconcile能力；
- Direct已注册四个公开IFC工具，Provider Schema对嵌套、判别联合结构保持封闭且无`unknown`；
- initialize成功Operation的Agent安全投影包含Project Name、GlobalId、schema和`model_initialized`结果，
  不包含Scope、幂等键、IfcModel/revision/ArtifactVersion或storage内部信息。

详细Provider合同和I0-B1精确边界见
`../agent-rebuild/cad-mcp-rebuild/15-ifc-model-entry-and-geometry-context-contract.md`。

## 1. 单一权威资源

- 每个Run最多绑定一个`IfcModel`。
- `IfcModel.current_revision_id`是CURRENT的唯一机器权威；没有`current_revision_no`、文件CURRENT、Graph State
  CURRENT或`ifc_session_alias`。
- 每个`IfcModelRevision`不可变，使用model-local正整数`revision_no`单调递增，并精确绑定一个
  `ArtifactVersion`。
- 每个revision使用独立Artifact及该Artifact的version 1。`Artifact.current_version_id`只证明该单版本
  Artifact的exact version，不承担IfcModel CURRENT。
- size、SHA-256、filename、media type和storage key的权威位于`ArtifactVersion`；revision行只保存exact
  Artifact/ArtifactVersion引用。
- 成功IFC Operation的result revision由`IfcModelRevision.operation_id`唯一反向定位，
  `critical_operations`不保存第二个result-revision列。

## 2. revision 1

`InitializeIfcModelCommand`是必填`project_name`的create-only命令。规范化后的名称长度为1至255字符，
控制字符在Provider/Domain校验阶段拒绝，不创建Operation。当前首次成功创建：

- 一个精确`IFC4X3_ADD2` IFC-SPF文件；
- 恰好一个`IfcProject`，其GlobalId由Run-bound server identity稳定生成，Name等于规范化后的用户业务名称；
- `IfcModel`、revision 1、CURRENT、Artifact/ArtifactVersion和成功Operation/Catalog binding。

revision 1不包含Site、Units、Geometric Representation Context/SubContext、Product或几何基础。
`ifc_model_configure_geometry_basis`已经按“LLM提交高层合法值、服务器编译为IFC实体”实现：

- Units、precision、WCS/TrueNorth和CRS/MapConversion在已有placement/几何后仍可修改；工具只直接修改对应IFC实体/属性并
  重新解释已有数值，不遍历换算已有placement、geometry、quantity或property，也永久不提供保持原物理模型的迁移模式；
- 父Context/SubContext由Agent在该工具的封闭操作中显式创建或修改；服务器和产品工具不预建、补齐或推断任何Context；
- 专用READ工具`ifc_representation_context_list`返回Context Catalog；产品工具使用模型内全局唯一的
  `ContextIdentifier`（例如`Body`）引用已有SubContext，并在创建任何产品事实前完成全量存在、唯一性和兼容性校验；
- 重复Context create报已存在；缺失、重复/歧义或不兼容都使对应写入零修改失败。

封闭Provider分支覆盖项目长度/平面角SI单位、local/projected CRS、父Context和SubContext的显式create/update、
WCS、TrueNorth、precision、TargetView/TargetScale及可选用户视图。Response固定为configure返回
`operation_alias + model`，info返回`geometry_basis_configured + nullable geometry_basis`；Context list稳定按
`ContextIdentifier`排序并返回每个Context的有界业务摘要。
每个source的坐标归一化属于CAD/Data变换链。

I0-B2已注册`ifc_project_update`、`ifc_spatial_structure_list`和`ifc_spatial_structure_apply`：

- Project工具使用`patch + clear_fields`修改Name/Description/ObjectType/LongName/Phase，保持Project GlobalId、Schema、Units、
  Context和OwnerHistory不变；
- Spatial apply覆盖Site/Building/BuildingStorey/Space的create、update、reparent-preserve-world、
  set-project-world-placement和delete-if-empty；已存对象使用GlobalId，同批create使用请求内`client_ref`；
- 所有公开位置都是project-world事实，Backend以唯一领域父子规则和私有IfcOpenShell副本编译相对
  `IfcLocalPlacement`；默认完整语义子树刚体随动，显式布尔值可保持后代project-world不变；
- list首页可不带筛选调查全部空间，也可按class/GlobalId/direct-parent使用AND语义筛选；续页opaque Cursor
  固定首页的server-only exact revision，进程重启和CURRENT推进都不改变该快照；
- list和delete共用同一关系/blocker分类器；placement闭包在修改前完成实体与work-unit有界预检，
  超限以`IFC_SPATIAL_RESOURCE_LIMIT`零修改失败；
- 三个工具都不暴露数字revision、ArtifactVersion、IfcModel ID、writer或STEP ID。Spatial list是READ，不创建Operation；
  两个WRITE均复用现有PREPARED→Artifact→revision/CURRENT/Operation原子提交链。

当前增量测试支撑继续复用
`IFC Golden Seed与增量原生验证as-built合同`：
可移植M4 Run Seed、隔离物化、确定性回读和使用当前四个IFC工具的原生G0半盲实验均已实现并验证；用户已批准
G0内容；I0-F0已将正式`foundation` Seed固化为可重复恢复的G0测试基线。I0-G0不改变本as-built运行时语义。

## 3. 写入与事务顺序

```text
server-derived Run scope
→ 持久化 PREPARED Operation
→ 固定exact base并流式stage到私有Worker scratch
→ one-shot受监督IfcOpenShell child生成并检查candidate
→ IfcRevisionWriter将私有candidate流式发布到Storage
→ 正常新候选：流式核对私有Storage实际字节的size/SHA，复用compiler保存重开后的inspection
  恢复已选候选：从私有Storage经Worker重新独立inspection
→ 单个 PostgreSQL transaction：
   Artifact + ArtifactVersion + revision + CURRENT + Operation终态 + Catalog
→ repository exact身份 + 实际正式字节检查
  正常同次revision提交复用已保存重开的inspection；无同次事实的恢复仍由Worker独立回读
→ 返回成功
```

Storage和PostgreSQL不是跨系统事务。Storage已发布而数据库未提交时，字节是不可见orphan候选；
同一server-only Operation identity重试会重用相同不可变对象并完成数据库提交。数据库已提交而响应丢失时，
service按exact revision对账并重放原成功结果，不生成revision 2。

正常新候选不再为相同字节在私有Storage重复启动一次IFC解析。复用只限该次compiler实际保存并
重开检查的候选，且Writer在选择候选前重新读取私有Storage实际字节、核对size/SHA；不是用元数据
代替实际读取。正式提交后再次核对准确revision身份及实际字节size/SHA；正常同次revision提交
可复用该候选的inspection，不再解析相同全文。已选候选恢复没有该次生成结果时，仍独立检查
私有候选与正式exact；初始化保留原独立回读。正式字节损坏依旧冻结，唯一数据库事务/CURRENT
CAS不变。内部可复用Body事实未保存时沿当前真实检查读取，不将缺少派生事实作为执行资格门禁。

## 4. CURRENT CAS与fail-closed

普通revision提交在prepare时固定`base_ifc_revision_id + expected_ifc_write_epoch`。提交仅在两者仍与
IfcModel一致时推进CURRENT和`write_epoch`。两个写入从同一base出发时只有一个winner；loser以
Operation持久态为：

```text
PREPARED → SUCCEEDED | FAILED | INDETERMINATE
```

PREPARED和INDETERMINATE不进入普通Catalog。已提交exact Artifact缺失、摘要不一致或无法解析时，
IfcModel进入`frozen`；原成功Operation保持`SUCCEEDED`，后续普通读写fail closed，由Backend reconcile处理。

## 5. 读取和入口边界

- `IfcModelService.info()`只按Run读取current，返回有界`IfcModelInfo`；不暴露model/revision/Artifact或writer身份。
- `get_current_exact()`和`get_exact_info()`是server-only恢复/门禁边界，frozen模型在Application Core中fail closed。
- Direct runtime在同一composition root中复用已有Direct Session、PostgreSQL Repository、LocalStorage和同一个
  `IfcOpenShellModelCodec`，构造`IfcModelService`、`IfcRevisionWriter`、`IfcGeometryBasisService`、
  `IfcProjectService`和`IfcSpatialStructureService`；本合同顶部的7个IFC工具/28个Direct工具是I0-B2提交快照。
  I0-B3.04进一步抽离唯一`IfcRevisionReader`并让writer与IFC Query共享；B3.04R收敛时为37个公开入口，
  B3.04C完成时Direct Provider共35个工具，其中16个以`ifc_`开头。B3.04原生Provider实验为`passed_with_observations`，最终提交状态见
  [`ifc-query.md`](ifc-query.md)。
  I0-B3.04W进一步让四个IFC写Service共享一个`IfcRevisionRuntimePort`与进程内`IfcWriteCoordinator`；
  durable身份、事务、CAS和恢复仍由本合同的Repository/writer唯一负责。精确执行边界见
  [`ifc-revision-runtime.md`](ifc-revision-runtime.md)。
- Runner后续将以service credential、Run claim/writer generation和exact checkpoint closure解析同一
  `IfcRunIdentity`，调用同一Application Core。Runner entry adapter和具体Graph接线不属于I0-A。
- generic revision writer与Repository接受server-trusted tool name/intent/idempotency identity。所有Direct非initialize IFC写入共用
  `server_ifc_invocation_key()`：对FastMCP `session_id + request_id`做带版本的server-only稳定摘要。原始request ID只在
  一个MCP session内唯一，不单独成为durable幂等事实；跨会话的相同原始编号保持隔离。Runner以后注入
  Graph exact tool-call identity。Operation保存完整Provider-safe业务请求与最小结果，不暴露Scope、revision、Artifact、
  storage、digest、CAS、MCP session/request或writer身份。

## 6. 实现位置

- 领域合同：`src/CAD2BIM_backend/domain/ifc_model.py`
- Operation泛化：`src/CAD2BIM_backend/domain/data_runtime.py`
- ports：`src/CAD2BIM_backend/ports/ifc_model.py`、
  `src/CAD2BIM_backend/ports/ifc_revision_reader.py`、`src/CAD2BIM_backend/ports/ifc_revision_runtime.py`
- geometry basis领域与port：`src/CAD2BIM_backend/domain/ifc_geometry_basis.py`、
  `src/CAD2BIM_backend/ports/ifc_geometry_basis.py`
- Project/Spatial领域与port：`src/CAD2BIM_backend/domain/ifc_project.py`、
  `src/CAD2BIM_backend/domain/ifc_spatial_structure.py`、`src/CAD2BIM_backend/ports/ifc_project.py`、
  `src/CAD2BIM_backend/ports/ifc_spatial_structure.py`
- IfcOpenShell codec与geometry compiler：`src/CAD2BIM_backend/infrastructure/ifc/codec.py`、
  `src/CAD2BIM_backend/infrastructure/ifc/geometry_basis.py`
- Project/Spatial compiler与placement：`src/CAD2BIM_backend/infrastructure/ifc/project.py`、
  `src/CAD2BIM_backend/infrastructure/ifc/spatial_structure.py`、`src/CAD2BIM_backend/infrastructure/ifc/placement.py`
- PostgreSQL模型和Repository：`src/CAD2BIM_backend/infrastructure/postgres/models.py`、
  `repository_ifc_model.py`
- migration：`src/CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260830_0064_durable_ifc_model_kernel.py`
- Application Core：`src/CAD2BIM_backend/application/ifc_model_service.py`、
  `ifc_revision_reader.py`、`ifc_revision_writer.py`、`ifc_geometry_basis_service.py`、`ifc_project_service.py`、
  `ifc_spatial_structure_service.py`、`ifc_write_coordinator.py`与共享`ifc_write_support.py`
- supervised revision runtime：`src/CAD2BIM_backend/infrastructure/ifc_revision_runtime/`
- Direct runtime composition：`src/CAD2BIM_mcp/direct/bootstrap.py`
- Direct IFC Provider DTO/投影：`src/CAD2BIM_mcp/direct/ifc/model/contracts.py`、
  `model/application.py`、`model/tools.py`、`ifc/project/`、`ifc/spatial/`、共享`ifc/application_support.py`、
  `ifc/invocation.py`与根`protocol.py`
- Direct注册/入口：`src/CAD2BIM_mcp/direct/server.py`与`entry.py`
- 聚焦门禁：`scripts/release/run_ifc_model_gates.py`；精确节点由
  `scripts/release/current_test_manifest.py`管理

## 7. verified_current门禁

2026-09-01在UTF-8、非Docker的fresh隔离PostgreSQL上完成当前复核：

- Alembic从空库升级到`20260830_0064`；
- 0064在存在durable IFC状态时正确拒绝破坏性降级；在独立空库上`0064 → 0063 → 0064`往返通过；
- 部署入口建立LangGraph checkpointer表，运行时角色预检验证了数据库完整性；
- `run_ifc_model_gates.py`先运行15个kernel节点，再运行10个不重复的Direct-entry节点；两组全部通过；
- I0-A覆盖稳定revision 1、原子数据库可见性、稳定重放、Storage-before-DB崩溃恢复、
  persist-then-response-loss对账、同base并发CAS、committed corruption技术重试/冻结、RLS隔离和真实新Python进程回读；
- I0-B0覆盖Provider Schema、工具描述、项目名校验/exact回读、幂等重放、重复初始化错误、
  Operation安全历史、三次技术尝试、model-less暂停/reconcile、RLS与重启回读；
- I0-B1覆盖两个非等价合法配置、单位/WCS/precision/local/projected CRS round-trip、显式Context create/update、
  Project unit与Projected CRS MapUnit独立、非等值CRS更新无实体膨胀、projected移除无孤儿MapUnit、
  缺失/重复/不兼容预检、无自动Context、no-change、Storage失败、响应丢失对账、同base真实并发CAS唯一winner、
  读重试固定首次exact revision、Operation安全投影、异Run隔离与进程重启独立回读。
- I0-B2覆盖Project patch/clear、四类空间与CompositionType、同批前向`client_ref`、GlobalId、循环/冲突、
  project-world↔relative placement、reparent保持世界位置、默认完整语义子树随动、显式后代补偿、
  共享/跨闭包placement、影响预算早停、delete blocker、opaque exact Cursor、typed Operation、FAILED alias、
  非initialize frozen reconcile、CAS/响应丢失、RLS、重启回读和Provider不泄露。
- G1-B维护场景将hash固定的231 MiB Golden Candidate作为fresh Run唯一revision 1，验证了非空删除
  零revision、空间移动revision 2、Run/Tenant隔离、重启exact回读和源SHA不变。该证据是兼容性测试，
  不会把Golden Candidate晋级为用户已批准模型。
- 首次G1-A原生能力运行验证了同批前向`client_ref`创建12个空间节点、跨新MCP会话继续写入、
  刚体子树移动与保持世界位置重挂。本轮在真实stdio路径发现并修复原始FastMCP request ID跨会话重用：
  durable identity现由共享helper绑定MCP session与request，身份内容冲突安全投影为`SYSTEM_PAUSED`，不误报
  `IFC_CURRENT_CONFLICT`。该运行是`verified_current`能力证据，不替代两次完整qualifying G1-A、fresh G1-B或Seed晋级。

本轮没有重建历史M2/M3/M4平台，也没有启动Docker、旧Agent/IFC smoke或原生Direct MCP进程。

I0-B3.04W在2026-09-04完成了写执行边界的后续验证；正式门禁、取消/响应丢失、真实Worker×PostgreSQL、
大文件预算及公开面冻结证据见[`IFC Revision Runtime as-built合同`](ifc-revision-runtime.md)。该后续切片
没有改变本文件记录的I0-A数据库模型、唯一CURRENT或Provider业务合同。
