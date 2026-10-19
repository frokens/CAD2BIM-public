# I0-B3.04W IFC Revision Runtime as-built合同

```text
status: authoritative_as_built
implementation_status: committed
implementation_commit: 12fd9b1a1bf2f3168d74ae1a2ef4180addae00c3
verification: verified_current
provider_surface: unchanged_35_direct_tools_16_ifc_prefixed_tools_1_resource_template
database_head: 20260904_0072_unchanged
```

## 1. 完成边界

I0-B3.04W把四个现有IFC写入口的重型IfcOpenShell工作迁入受监督的one-shot子进程：

- `ifc_model_initialize`；
- `ifc_model_configure_geometry_basis`；
- `ifc_project_update`；
- `ifc_spatial_structure_apply`。

迁移范围包括revision 1构造、私有副本修改、candidate生成及保存重开inspection、已选候选恢复时的
独立inspection和提交后的exact inspection。正常新候选私有Storage发布时验证实际字节并复用同次
compiler inspection，不重复解析相同IFC。Backend主进程继续唯一拥有：

- Invocation Scope、RLS、Task/Run和Direct Session解析；
- Critical Operation及其幂等身份；
- Artifact/ArtifactVersion和Storage发布；
- IfcModel、immutable revision、CURRENT和`write_epoch` CAS；
- response-loss reconcile、freeze和恢复；
- Provider结果投影。

本切片没有新增公开MCP工具、Provider字段、ResourceTemplate、数据库表、migration、Repository、
Storage实现、Job或candidate Artifact。Alembic head保持`20260904_0072`。Geometry Revision Runtime
只是计算边界，不是第二套durable IFC kernel或第二事实源。

## 2. 唯一调用链

```text
Direct/未来Runner入口
→ 既有Scope解析、RLS和Run授权
→ 既有Repository持久化PREPARED Operation并固定CURRENT/CAS frontier
→ IfcWriteCoordinator按Run排序并按逻辑Operation做进程内single-flight
→ IfcRevisionRuntimePort
   → 父进程流式stage exact source并校验size/SHA
   → one-shot受监督child执行唯一IfcOpenShell codec/compiler
   → 返回私有candidate capability、inspection和必要typed effect
→ IfcRevisionWriter流式发布candidate到既有Storage
→ 正常新候选：实际读取私有存储字节并核对size/SHA，复用compiler保存重开inspection
  已选候选恢复：Worker从私有存储重新独立inspection
→ 既有PostgreSQL事务原子写入Artifact/ArtifactVersion/revision/CURRENT/Operation
→ response-loss reconcile；正常同次revision提交验证正式exact字节并复用已保存重开的inspection
  已选候选恢复或缺少同次inspection时仍由Worker独立回读；初始化保留原独立回读
→ 原Provider响应
```

`ifc_model_initialize`没有source；其余写入固定prepare时的exact base。CAS loser继续返回既有
`IFC_CURRENT_CONFLICT`，不会从新CURRENT自动重算。Storage发布成功而数据库未提交时，保存对象仍是
不可见orphan；后续由既有幂等Operation完成提交或由维护策略回收。

## 3. 封闭Port合同

`IfcRevisionRuntimePort`只接受六类typed请求：

1. `InitializeIfcRevisionRequest`；
2. `ConfigureIfcGeometryBasisRevisionRequest`；
3. `UpdateIfcProjectRevisionRequest`；
4. `ApplyIfcSpatialStructureRevisionRequest`；
5. `InspectExactIfcRevisionRequest`。
6. `CompileIfcProductsRevisionRequest`：一次Family合同和异步的有界Geometry编译块。

请求只包含执行当前IFC算法所需的Domain命令、IFC GlobalId、初始化时间和exact字节能力。协议不包含
owner/tenant/task/run、Operation、ArtifactVersion、revision/CURRENT、Storage key、Provider alias、
credential或writer身份。

source是repeatable `IfcRevisionChunkSource`：每次迭代都从byte zero开始，并声明exact size与SHA-256。
candidate是进程私有、短生命周期、repeatable `IfcRevisionCandidateHandle`；共享写任务是其唯一owner，
任务终止时必须调用`aclose()`。

成功结果只有两种：

- `GeneratedIfcRevision`：candidate、独立inspection及可选typed effect；
- `InspectedIfcRevision`：只读exact inspection。

Project effect只携带响应需要的`IfcProjectSummary`。Product effect携带本次product/component实际GlobalId映射、
源/映射/三角形/块计数、实际单位和线性离散设置、阶段wall观测，不携带默认几何完整摘要。
Spatial effect只携带本次created binding、排序去重的
affected GlobalId和对应空间摘要，不返回完整空间Catalog。Application Core在提交前验证effect与命令一致，
防止协议或实现错误先推进CURRENT。

## 4. 父进程、child与字节边界

父进程按1 MiB chunk将repeatable source写入0700私有scratch，并同时核验声明的size/SHA。source路径名在
child启动前unlink；child只通过显式继承的只读文件描述符读取。candidate通过独立文件描述符写入scratch，
父进程重新计算size/SHA并与result metadata和inspection交叉核对，然后只把私有只读capability交给writer。

child使用当前唯一的`IfcOpenShellModelCodec`及geometry/project/spatial compiler，不复制领域编译算法，
也没有数据库、Storage根、MCP或网络能力。result frame携带candidate metadata、inspection与typed effect；
terminal frame只携带终态、命令类型和source SHA attestation。

`compile_products`保存回读复用已存在的私有`candidate.ifc`：codec通过原只读candidate描述符逐块比较
实际保存字节与compiler输出，并确认路径为普通文件且与该描述符具有相同设备/文件身份，随后使用
IfcOpenShell原生文件reader重新打开。SPF、严格UTF-8、体积、完整inspection及`verify_saved`仍保留；
错配或reader错误直接失败，不转回其他parser。该入口不接受公开路径，不改变candidate生命周期，
也不改变source提前unlink和正式exact inspection的只读FD边界。

当前实现诚实边界如下：

- Backend父进程的写链不再把231 MiB source/candidate整体装入Python bytes；
- child仍将source读入bytes，再经历IFC文本/IfcOpenShell对象图；产品candidate保存回读逐块比较字节后
  直接通过原生文件reader重开，其他写命令仍按既有bytes回读；
- 因此本切片隔离并限制native crash与内存峰值，但没有消除child内部whole-file峰值；
- 普通IFC只读工具保留既有Reader、权限与exact持久化权威；CURRENT重解析通过下述Runtime
  固定投影，自由Query仍保留自身Query Worker。

## 5. 排序、single-flight和取消

`IfcWriteCoordinator`以`(owner, tenant, task, run)`串行化同一Run写入，并以Run加server-only逻辑
Operation identity共享同进程内的一次执行。它不持久化状态、不访问Repository/Storage，也不代替PostgreSQL
的幂等与CURRENT CAS。

claim具有`accepting → retiring`生命周期：最后一个等待者取消时，在同一把claim锁内停止接纳；此时到达的
重放等待旧任务完成candidate清理后再重新竞争，绝不会加入一个已经确定取消的共享任务。正常完成或取消退役后，
claim与无消费者的Run lock都会被回收。

取消分为两个明确阶段：

- Storage发布完成前取消：不进入数据库commit，Operation保持可恢复的PREPARED状态；
- 数据库commit开始后取消：独立settlement task不随调用方取消，继续完成response-loss reconcile和exact回读；
  调用方最终仍收到自身的取消，不会收到伪成功。settlement无法确认时，Backend使用既有
  `freeze_indeterminate`使Operation/IfcModel fail closed。

单个等待者取消不会影响其他等待者。candidate由共享任务关闭，不由任一外层等待者关闭。

## 6. 错误、重试与恢复

child将错误分为：

- Domain business rejection：确定性、不可技术重试，既有Operation进入FAILED；
- 输入或协议验证错误：确定性、不可技术重试；
- Worker启动、超时、资源、协议损坏或native crash：技术错误，按runtime的retryable与原有限重试合同处理；
- 容量准入拒绝：保留整次调用及当前阶段执行事实，直接返回原Runtime诊断，不在同次调用内重复准入，
  不把机器容量当成IFC完整性破坏或据此冻结模型。

`IfcInvocationSupervisor`实际消费runtime的`retryable`分类。技术重试复用已经成功生成并验证的candidate，
不回退到主进程IfcOpenShell写路径。数据库提交后响应丢失时，writer按预分配的exact revision读取、核验并重放
原结果；提交后证据异常时，成功Operation保持SUCCEEDED而IfcModel冻结，交由Backend reconcile。

## 7. 当前强制预算

Worker 仅按固定命令加载实际消费者：Product/Project/Spatial compiler 不在只读
`inspect_exact` 启动时统一导入，Geometry Basis 仍执行必要单位与 Context 检查。
基础 Codec 的创建 API 仅在创建 revision 时加载；TFS 的 `ifcopenshell.geom` 与
产品放置 API 在对应执行分支加载。compiler仍独立重开实际保存的候选。正常同次revision提交
在发布前后校验实际存储字节，并在提交后重新核对exact身份、size与SHA；字节一致时复用本次
候选的inspection，不再次解析全文。恢复没有本次生成结果时仍独立检查已选候选及正式exact，
初始化也保留原独立回读；存储身份、Scope、幂等及CAS不因此放宽。正式字节损坏仍冻结模型，
不能以数据库内的inspection代替实际存储校验。
参数 Schema 库在实际参数规则消费者处初始化；Product child 的规则准备仍在原只读
审计安装前完成，未把初始化移入不受控的作者执行阶段。

| 资源 | enforced ceiling |
|---|---:|
| control frame | 8 MiB |
| exact source / candidate | 各512 MiB |
| result/effect frame | 128 MiB |
| product block frame / canonical bytes | 各64 MiB，每块最多1024件；编码帧也须满足64 MiB |
| product stream cumulative encoded bytes | 512 MiB |
| terminal/status frame | 32 KiB |
| wall time | 150 s |
| CPU time | 150 s |
| RSS | 8 GiB |
| scratch | 1,056 MiB |
| native thread growth allowance | 4，相对于监督器记录的基线 |
| open file descriptors | 64 |
| stdout / stderr | 各16 KiB |
| cleanup | 1 s |
| capacity queue | 600 s，共用invocation有限额度 |
| host concurrency | 1 |

单次Revision执行链H为170秒，Q/T/C为600/15/5秒，总计790秒；IFC完整写invocation的
H为510秒、总计1130秒，详见[统一执行预算](tool-usability-and-runtime-boundaries.md)。
单child清理上限与调用清理保留不是同一个计时器。
`InspectExactIfcRevisionRequest`还支持封闭的model/contexts/spatial/host_facts/body/products投影，复用原Domain
Catalog/Page、Compiler与HostFactExtractor；Model info、Context/Spatial list、CURRENT Host事实提取与Query exact read通过同一Revision
Runtime处理重解析。Reader仍核对固定Artifact长度、SHA与Project身份，Writer继续拥有唯一
exact持久化和回读链，未新增读取Runtime、Repository或持久缓存。
同次inspection及context/spatial/host/body/products投影复用已经打开的私有模型，geometry basis检查不再自行
二次解析全文；保存后的实际字节仍独立重开一次并完成inspection，不以编译前对象代替保存回读。
`products`承接[固定产品读取](ifc-products-read.md)的默认基础事实及请求的七组当前语义，
在同次解析中按需提取，保留原预算与隔离；原Apply报告读取不启动该投影。

8 MiB control预算由当前最大合法256项Spatial command表征锁定；128 MiB result预算覆盖当前最大合法
affected-summary包络。超出Domain上限在Operation prepare前确定性拒绝；真正的Worker资源超限走技术故障语义。

## 8. Direct组合根和实现位置

Direct唯一composition root构造一个`IfcOpenShellRevisionRuntime`、一个`IfcWriteCoordinator`、一个
`IfcRevisionReader`和一个`IfcRevisionWriter`，四个写Service共享它们。Direct启动只检查必要的
解释器环境、数据库和存储，不调用各Runtime的真实样例readiness；该方法仍可供能力验证使用。
实际业务调用继续沿既有子进程监督、容量、预算及输入／产物校验执行，真实执行故障由对应调用返回。
close先关闭runtime及其残留candidate，再关闭其他Worker和数据库资源。

- Runtime Port：`src/CAD2BIM_backend/ports/ifc_revision_runtime.py`
- 父adapter与child：`src/CAD2BIM_backend/infrastructure/ifc_revision_runtime/`
- 产品表示编译：`src/CAD2BIM_backend/infrastructure/ifc/product_compilation.py`
- 排序/single-flight：`src/CAD2BIM_backend/application/ifc_write_coordinator.py`
- durable writer/reader：`src/CAD2BIM_backend/application/ifc_revision_writer.py`、
  `ifc_revision_reader.py`
- 四个Application Service：`ifc_model_service.py`、`ifc_geometry_basis_service.py`、
  `ifc_project_service.py`、`ifc_spatial_structure_service.py`
- Direct composition：`src/CAD2BIM_mcp/direct/bootstrap.py`

## 9. verified_current证据

2026-09-04在当前工作树、非Docker、隔离PostgreSQL上完成：

- IFC正式门禁：46个kernel文件和15个Direct入口文件通过，最终运行102.245秒；
- CAD/Data正式门禁：56个聚焦文件通过，175.142秒；
- Golden Seed门禁：13个节点通过，12.411秒；
- 默认活跃测试集：1072 passed、65 deselected、4个既有warning，134.26秒；
- wheel实际构建/安装与31项结构、部署、runtime边界测试通过；
- 真实Worker×PostgreSQL/RLS纵向测试依次完成initialize、geometry basis、Project和Spatial四写，
  验证四个Operation/ArtifactVersion/immutable revision、CURRENT/write epoch 4、exact回读和跨Task不可见；
- 同一纵向测试强杀真实Worker，确认三次技术重试后零新revision、CURRENT不变、Backend仍存活且模型fail closed；
- 231 MiB Golden Candidate opt-in表征通过，source SHA保持
  `02aade531690f7a4624f64ed0f10ba47b416f052f223160c33c9c7dbb2a210a7`，candidate可重复读取两次；
  当前机器观测为117.79秒、CPU 116.293308416秒、峰值RSS 7,469,121,536 bytes、峰值scratch
  241,858,421 bytes、1线程和9个打开文件描述符。

大文件观测是当前机器的表征证据，不是跨环境性能承诺。最初60秒/5 GiB配置无法覆盖真实Golden峰值；根因修复为
按实测工作负载将IFC专属预算校准到150秒/8 GiB，并继续保留one-shot隔离和硬上限。

公开Provider没有变化，因此本切片不新增独立原生LLM工具理解实验；真实stdio Provider合同仍由既有Direct门禁冻结，
本切片新增风险由真实child、真实PostgreSQL/RLS纵向门禁和大文件表征直接验证。

## 10. 后续边界

I0-B3.04W已经完成并作为已批准I0-B3.05 ProductFamily Catalog合同的前置底座。后续IFC写工具必须复用
同一Runtime Port、Coordinator、writer、Artifact/revision/CURRENT/CAS与恢复链；不得重新引入主进程写fallback、
第二Repository、第二candidate持久化或第二CURRENT。

本文one-shot描述现有执行机制，不构成后续批量工具必须逐产品启动进程的要求。
新产品批量添加须遵守apply批次内Worker与模型复用目标：
同一有界批次在所属Worker内处理多产品，复用初始化与候选模型，保留本合同提交、隔离与恢复
owner。当前产品compiler切片见下节；整条apply的持久化和业务接收仍由对应Application/Writer证据确认，
不据此改写以上历史as-built证据。

## 11. 原生产品表示compiler

`CompileIfcProductsCommand`复用唯一`ProductFamilyContract`，每个编译条目直接消费已经检查的
`ProductRealizationResult`，附`product_key/input_row_index`和fragment准确源引用，不复制proposal业务规则。
每源首次提交`CanonicalGeometry`；已选Parametric还提交同次私有`GeometryConstruction`，
后续同次引用可省略已缓存的字节与构造记录。source_key绑定准确Geometry版本、预选路线及实际
形状身份，同键不同字节或路线拒绝；不同准确资产不因Family名称或近似尺寸合并。

求解前先固定并校验准确base字节，再检查该revision的实际Body事实，要求Body/Model/MODEL_VIEW
及Model/3D父Context，父标识不限定为Model。当前Codec从实际IFC提取Body子Context及父Context
属性和项目长度单位换算，与完整inspection一起由既有ArtifactVersion元数据保存。现有Repository
按准确revision读取，并核对ArtifactVersion的size、SHA和Project身份；事实存在但错配时拒绝。
若未保存这些可复用事实，Writer沿当前`inspect_exact`读取实际字节取得事实，首次成本保留；
缺少内部派生事实不使原本合法的revision失去执行资格，不修改已有不可变记录。`None`表示事实
未记录，空集合表示实际检查未发现Body，两者不得混同。缺少/不兼容Body仍在Numeric前返回原
Context错误并指引`ifc_model_configure_geometry_basis`，不自动创建Context。整个编译只生成Body。
已选候选恢复时，原inspection未记录派生事实则仍比较全部原有持久事实，实际读取所得新inspection
随publication保存；双方均有派生事实时一并比较，已记录的错误事实不能被忽略。

Application按准确SYSTEM资源选择整项路线，未知版本与Agent资源为TFS；已知ID的SYSTEM
所有权异常明确停止。Product Worker仅为已选Parametric记录唯一建模源码的实际构造。
IFC Worker从该记录编码SolidModel；Advanced从原准确BREP在同一模型建立每Solid内层
AdvancedBrep map。两者的支持域与拒绝边界见[Apply Core](ifc-product-apply-core.md#原生-ifc-表示与有界复用)。
不在异常后改走另一表示，也不因参数分支换路线。
Parametric 回转使用实际角度与项目 RADIAN/SI 前缀，不再以全周实体加两半空间表达
小角度回转。轴对称截面裁剪显式关闭原生并行；该修复未提高线程增长或 wall/CPU/RSS 预算。

TFS调用本机IfcOpenShell 0.8.5的`tesselate("IFC4X3_ADD2", brep_string, 0.001)`，BREP坐标明确为米。
原生`SurfaceModel/IfcFaceBasedSurfaceModel`只是内部网格，最终映射源写为
`Body/Tessellation/IfcTriangulatedFaceSet`并接已有Context；组件Body仍为引用该源的
`MappedRepresentation`。每个原生ConnectedFaceSet独立建立1-based点表与三角索引，按
wrapped实体身份区分点，不用未入文件的STEP编号、不焊接同坐标不同点、不合并shell。
FaceBound方向、重复与退化三角形保留，Closed/Normals/PnIndex不设置。非三角、多bound、
空shell或不合坐标合同的原生网格明确停止，没有最终FBSM分支、重新三角化或表示fallback。
离散设置不是已量测最大外形误差；业务尺度会放大离散误差，解析曲面和建模历史不会保留。

同一源先建立RepresentationMap，后续MappedItem跨块共享。源表示坐标一次转到项目单位，
component/fragment平移各沿同一米制frame链转换一次；MappingTarget仅承担尚未物化的正统一业务尺度。
上游已物化尺度的canonical必须传mapping_scale=1。一个component含多fragment仍只有一个产品身份；
无直接fragment的assembly根不复制子件Body，组成关系为IfcRelAggregates，无外管、Type或网络端口写入。

Adapter复用现有AsyncWorkerChannel管道传输有界块，单Child保持一个候选模型和map索引，不在块末保存。
全部消费后一次序列化/保存，保存字节独立重开一次，核身份、项目世界位姿、aggregate树、Body与共享map。
共享map按所选路线核实际源类型、items及内外层映射引用、位姿和scale；TFS另逐原生shell
核点表与三角索引保持。保存结构检查不替代形体质量验证；TFS表面不自动证明闭合实体。
技术首错停止，不返回前缀candidate；取消关闭背压producer、上游iterator与私有目录。
确定性表示/源错误返回`IFC_PRODUCT_COMPILATION_FAILED`与产品键/行号，标为不可原样技术重试；
传输超预算返回`IFC_PRODUCT_INPUT_BUDGET_EXCEEDED`与块号。现有wall/CPU/RSS配置未因产品命令增加而放宽。

WorkerTiming沿现有监督观测器记录native tesselate、Parametric/Advanced编码、索引编码、装配、
块接收/解析/释放、序列化和保存重开检查。effect的`representation_map_build`包含所选编码、
单位换算和map创建；路线事件为`ifc_native_tesselate`、`ifc_parametric_encode`、
`ifc_advanced_brep_encode`。装配包含map构建，旧累计`tesselate`字段已退出当前输出。
释放事件带实际product/fragment数量；effect带实际unique source、map引用、TFS三角形及block数量。
监督器相对自身记录的基线限制Native线程增长，配置4不等于所有平台绝对只能有4线程。
本轮调用资源汇总的CPU取各child终态累计CPU之和，RSS取最大单child峰值，不是阶段CPU
或所有进程同时占用的峰值。阶段记录是wall区间；嵌套时长和聚合event count不视为产品吞吐或分摊CPU。
证据显示该切片可执行，不单独证明唯一Writer事务、CURRENT原子提交或SYSTEM全量业务验收。

当前 SYSTEM 小批与受影响工程范围、质量方法和原始失败保留在
固定路线记录。31 个不同 CAD 产品为
业务分母；重复 PIPE 对照及 source 级重编仅计入各自质量和性能观察。
Source 级同 canonical 大小不是完整 Family 或累计 IFC 大小；原多组件装配、受影响
单源/mm 与完整有向解析边界按准确变更范围组合验证，不反写原模型或重复 Full。
