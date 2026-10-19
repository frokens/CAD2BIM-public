# Geometry Resource Catalog与调查as-built合同

```text
status: committed
verification: verified_current
implemented_slice: I0-B3.02
database_head_at_slice: 20260902_0067
public_direct_tools_total_at_slice: 34
public_ifc_tools_total_at_slice: 13
public_read_tools_added: 6
preview_resource_templates_added: 1
database_head_at_previous_document_update: 20260906_0078
direct_tools_total_at_previous_document_update: 35
ifc_prefixed_tools_total_at_previous_document_update: 16
provider_surface_convergence: I0-B3.04R_committed_verified_current
```

本文记录I0-B3.02已实现的Geometry Resource发现、源码读取、预览、只读代码调查和受信
SYSTEM seed。它沿I0-B3.01的稳定资源根、不可变Version、exact VersionAsset、Scope/RLS和
Storage发布链形成一个纵向能力切片。它没有创建ProductFamily、TASK Geometry Authoring、
`ifc_query_python`或IFC revision写入能力。

## 1. 公开能力

本轮已实现、原生能力验收待完成的usage_instructions
由原write入口随版本manifest持久化，并在resource_version exact info返回完整正文，历史缺失
返回null；list/root版本card不展开。读取说明用于理解真实参数、局部接口和调用检查，不能
从源码注释临时拼出一份“已保存”正文。正式info描述须指导Agent继续读取exact合同和依赖。

SYSTEM seed保留受信manifest流程；后台精确资产导入见下文3.1。
Authoring资产绑定、function依赖和混合参考预览仍按
当前目标实施，
后台导入不代表这些后续能力已经贯通。

当前调查入口复用以下工具；Preview可显式保存结果，不能统一标为READ：

| 工具 | 精确职责 |
| --- | --- |
| `ifc_geometry_resource_list` | 有界列出当前Scope可见且已有成功Version的资源根；cursor保存原查询边界 |
| `ifc_geometry_resource_info` | 读取一个资源根或一个exact不可变Version的Provider安全投影 |
| `ifc_geometry_resource_source_read` | 列出exact Version的source bundle成员，并对选定UTF-8成员执行`full/lines/search` |
| `ifc_geometry_preview` | exact mapped预览、显式函数sample预览/保存、已保存inspection的model或section观察；PNG作为同次`ImageContent`返回 |
| `ifc_geometry_query_python` | 以`fresh/repair/cursor`调查exact mapped版本或已保存inspection的指定对象和坐标 |
| `code_list` / `code_read` | 以`geometry_query_attempt`发现并读取当前Task可见的exact查询源码 |

`source_read`与Catalog、Query及Preview共用Direct短引用投影。Catalog返回的`gv_` exact
版本引用可直接用于读取，不要求Agent取得或手工转换内部token。后台认证引用使用当前`cur.`前缀，
由同一解码路径核对类型与Scope，不保留按旧版本分支的读取路径。
正式middleware先按当前Scope和引用类型解析，再进入既有Source Application owner。
错误类型由Provider拒绝，不可见短引用由原引用索引拒绝；两者都不能到达来源字节读取。

exact info公开依赖的input_key和resource_version_alias，不再返回代表资产content_sha256或
依赖content_digest/canonical_geometry_sha256。原代表资产检查与冻结依赖exact digest比较仍在
Application中执行；删除公开指纹不改变后端完整性。source_files中的成员SHA及source_read的
selected-source SHA继续返回，尤其保留非文本mapped-model来源身份；它们不是whole-bundle修订
并发标记的替代物。接口、bbox/topology、参数Schema、验证和完整usage正文维持原业务含义。
按定义发布的`modeling_function`可不保存样例实体；此时exact Info仍返回源码目录和参数Schema，`geometry_format`、`bbox`、`topology`为空，实际形体须按显式参数Preview观察。

I0-B3.02提交时Direct有34个公开工具，其中IFC能力族13个；I0-B3.03提交时达到39/18，
I0-B3.04提交时达到41/20。I0-B3.04R收敛后为37/16，I0-B3.04C完成时总数为35个公开工具，其中16个以`ifc_`开头。几何工具统一位于
`src/CAD2BIM_mcp/direct/ifc/geometry/`；Provider只接收`direct_session_alias`和安全alias/cursor，
不会看到数据库ID、ArtifactVersion、Storage路径、其他Scope身份或Worker内部对象。

预览另注册一个MCP ResourceTemplate：

```text
cad2bim://geometry-preview/{direct_session_alias}/{opaque_token}
```

I0-B3.02提交时工具返回`ResourceLink`；当前视觉Agent闭环修复还在同次ToolResult中返回相同PNG字节的
内联`ImageContent`。ResourceTemplate读取时重新解析Direct Scope，核对opaque capability、
exact GeometryResourceVersion、Task ArtifactVersion、content digest和PNG digest。URI不携带真实资源
身份、ArtifactVersion或物理路径。

## 2. Ready Catalog与安全引用

PostgreSQL仍是GeometryResource稳定根、不可变Version、VersionAsset metadata、Scope、lineage和
digest的唯一机器权威。现有`StoragePort`仍是exact bytes的唯一存储边界。Catalog没有别名表：
同一现有`CursorCodec`签发并验证资源根引用、exact Version引用、分页cursor、QueryAttempt alias和
preview capability。

Catalog只返回已有成功Version的ready资源。`list`使用有界keyset cursor；`info`的target是封闭的
`resource_root | resource_version`分支。每次list/info/source/preview/query都会通过当前
TenantExecutionContext和RLS重新解析资源可见性。`display_name`仅用于显示，不承担机器身份。

Provider中的`preview_available`表达公开preview工具当前能否成功提供预览：

- ready `mapped_model`拥有canonical geometry，因此为`true`；
- I0-B3.03成功创建的`modeling_function`同时绑定exact source bundle和parameter schema，因此可按
  Agent显式样例参数动态构建并预览；
- 它不等价于“Version是否已经持久化一个PREVIEW asset”。

## 3. Source bundle与源码读取

`source_bundle`固定为确定性ZIP。Bundle允许UTF-8源码和无法文字投影的二进制来源同时存在，成员
目录对每项返回标准化相对路径、大小、SHA-256和`text_available`。只有`text_available=true`的
指定成员能进入`full/lines/search`；二进制成员会返回稳定的“不可作为文本读取”错误，不被猜测解码。

当前安全边界是最多64个成员、单成员最多64 MiB、解压后总量最多64 MiB、ZIP最多65 MiB；路径
拒绝绝对路径、父目录逃逸、重复规范化名和symlink。`full/lines/search`投影继续有独立响应预算，
不会把大BRep、IFC或mesh字节塞入Provider响应。

### 3.1 后台精确资产导入

`GeometryResourceService.import_mapped_asset`是受控Backend入口。调用者提供合法owner
Scope、request key、显示名、归一化请求、source bundle中的相对来源名、预先核对的来源
SHA-256及说明文件字节；不接收宿主文件路径，没有新增MCP上传工具。现有Repository
先执行现行SYSTEM/TENANT/TASK写权限检查：SYSTEM仅curator；正常runtime须有有效技术Task，
TENANT须同tenant、TASK须同tenant及当前Task，准确权限见
[TENANT作者资源持久化](tenant-authoring-persistence.md#2-原数据库权限上的增量)。
这是受控Backend入口，不增加公开owner参数或MCP上传工具。共享source bundle packer拥有
路径和字节预算；`prepare_mapped_geometry_assets`由intake与SYSTEM seed共同消费，
`store_candidate_assets`和`publish_candidate`分别复用同一Storage与原子Repository事务。

正式版本按当前合同、来源与完整资产验证，不保存或读取布尔发布资格标记。相同BREP字节的
保存摘要和当前测量可有表示差异；读取保留实际字节完整性，并使用当前重开测量，不以整个
GeometrySummary精确相等作为资格门槛。Preview使用真实datum transform与坐标空间，无变换自摘要；
图片字节的内容SHA仍验证实际PNG完整性。

同Scope的request key绑定一个新root及唯一初始版本。指纹覆盖来源、全部说明文件、
显示名和归一化请求；相同key的不同输入拒绝。原提交响应丢失后，相同输入通过exact
Repository及全部Storage字节核验恢复，不重复创建版本。发布前任一失败都不产生可见
partial版本；未引用的不可变预写字节允许同请求重试复用，不能在提交结果不确定时删除。

IFC到BREP由原受监督codec完成，要求一个有表示的IfcElement及一个明确项目长度单位，
拒绝将IfcTessellatedItem逆向当作native BREP。完整product-local表示按米归一，保留表示内
映射变换；可选canonical变换仍只接受刚体变换。manifest的`source_provenance`记录源文件
SHA、product实体、长度单位到米的比例、原product placement、canonical归一变换及表示项
实体对应。原IFC和修复/来源说明保存在同版本SOURCE_BUNDLE并可经既有source_read回读。
没有来源信息的旧canonical序列化不新增空字段，旧seed字节规则保持兼容。

codec检查格式、有效性、摘要与保存后重开一致性。BREP可能包含面集合而没有solid，或在
solid之外还包含自由面、shell、wire等低维子项；这两种情况的整对象`volume_m3`均为null。
仅由solid组成的Compound/嵌套Compound保留实体体积汇总。不能以“存在solid”推断混合
对象具有可报告体积，也不使用CadQuery按首个子项选择积分类型的混合对象`Volume()`结果。
`topology.boundary_edge_count`统计表面中仅使用一次的边：mesh按triangle边使用次数，
BREP按OCP原始edge→face使用次数且排除退化边，不去重同一face的重复使用；周期seam
在同一face中使用两次，不计为开放边。自由wire不属于面边界；零计数不证明wire闭合、
实体有效性、流腔连通或manifold。当前量测沿同一codec进入新摘要与Query，不更改旧记录。
旧immutable manifest中的摘要保留原记录；历史混合对象的非空体积不构成物理量证据，需
通过当前Query重新量测。codec重开在重测摘要与旧输入不一致时不缓存旧值，重复读取仍
返回当前量测。`volume_m3=null`不自动构成导入失败或业务通过；通道、距离、接合等仍由任务与Query验收。
原始说明不是可执行程序，后台导入的mapped_model不会进入Code程序目录。

## 4. Preview语义

当前`ifc_geometry_preview`输入为三个完整封闭分支：

- `mapped_model`：从exact `canonical_geometry`读取`occt_brep_v1`或
  `indexed_triangle_mesh_v1`，由共享Geometry Runtime重开并按profile渲染；不接收样例或保存开关。
- `modeling_function`：exact版本加一个`sample_parameters_json`对象，同次构建实际sample；
  `retain_for_inspection`缺省false。true沿既有关键Operation受理、幂等与终态，成功返回
  `operation_alias`和`inspection_ref`；不会将sample发布为新的Geometry Resource版本。
- `inspection`：只接受`inspection_ref`、`selection`、`coordinate_space`和`observation`。
  不接收替代source、sample、datum、Family或上传Shape。selection为whole/component/fragment；
  fragment_local必须选择同一fragment_key。model使用原共享camera/profile；section使用米制origin、
  无量纲非零normal、最多四个不同有限offset和可选显示范围。

result坐标以`context.source.result_coordinate_space`为准：无datum的Geometry sample为
geometry_local，带datum的sample和Family结果为project_world；fragment_local使用准确保存的
局部bytes，其中receipt scale已经施加。

普通Preview不创建Operation或持久派生资产，临时模型在调用结束、错误或取消后清理。
保存结果的唯一字节权威仍是Artifact/ArtifactVersion；同次结果与必须的fragment、reference及receipt
一并封装为不可变bundle。后续观察和Query不重新构建原source，也不创建新的保存Operation。
成功保存但render失败返回原引用和明确render诊断，不补造图片。无独立inspection发现工具；
通过`operation_list(operation_kind=geometry_inspection)`和`operation_get`找回准确引用与原请求。

读取准确保存结果时，已确认的Scope/版本缺失返回`INSPECTION_NOT_FOUND`，Storage完整性冲突或
bundle/manifest损坏返回`INSPECTION_INTEGRITY_FAILED`。暂时数据库或下载失败返回
`INSPECTION_READ_UNAVAILABLE`、`phase=target`、`retryable=true`；继续使用原引用和原动作身份，
不把读取失败解释为原保存失败，不重新执行来源程序。已发布Operation终态保持不变。

截面回执逐平面保留选择/坐标、实际平面和轴、对象归属、材料region/边界/解析曲线、面积及诊断；
显示裁剪不裁剪量测对象。面积总和是各对象材料region之和，不是Boolean union面积；非solid材料面积
为null，空截面、相切/退化和unsupported均不代表业务PASS。绘图失败仍保留已有量测并单独报告。
JSON TextContent在前，实际PNG的ImageContent按回执顺序随后；无PNG的合法诊断仅返回文本。

## 5. Durable只读几何调查

`ifc_geometry_query_python`的固定入口是`query(ctx)`。fresh target仅为
`geometry_resource_version`的exact mapped版本，或`geometry_inspection`的准确引用、selection和坐标。
Runtime为该不可变目标创建
私有OCP或indexed-mesh调查对象；代码可以返回有界JSON值，用于bbox、拓扑、面积、体积、截面等
调查。private_geometry()按需创建并复用一个与源对象分离的计算副本，允许局部派生、缓存更新和形体替换；
summary()/vertices()/triangles()/face_types()/edge_types()始终描述原输入。回执引用入口验证的原payload
SHA，不做前后完整网格指纹比较。越权import、文件系统/网络/子进程访问、超时、资源预算超限或
不可序列化输出会使attempt失败；原资源字节始终不变，没有几何发布路径。

显式`display_diagnostics()`沿当前codec唯一网格/重试/选用owner，复用同次显示网格及有序
pcurve观测；whole保存whole，fragment/component与Preview共享逐fragment顺序和面号归属。
返回完整/不完整覆盖、异常或未检查面、原生与离散检查的分列事实和当前坐标的区域；当前深入
穿越支持非周期Plane，其他曲面明确unsupported，不将未算完计数记为零。原生no_issue_reported
不是严格证明；端点接触/重合不算真正穿越。Query可返回已确认渲染失败事实，但vertices()/triangles()
在失败确认后拒绝当前网格。private_geometry()/private_fragment()变更不污染该原体诊断，同Query
重复调用复用缓存，close释放全部派生物。完整公开方法、限制及例子唯一归既有program_api资料。

每次fresh或repair创建一个Task级durable `GeometryQueryAttempt`：

```text
persist exact Python source ArtifactVersion
→ PREPARED attempt绑定exact Version或inspection Artifact、selection/coordinates与request identity
→ 隔离Runtime执行一次
├── SUCCEEDED：绑定exact结果ArtifactVersion
└── FAILED：保存有界diagnostic且没有结果ArtifactVersion
```

状态只能从`PREPARED`推进一次到`SUCCEEDED`或`FAILED`。fresh固定当前解析的exact目标；
repair从不可变父attempt的exact源码开始，执行有界literal patch，并继续绑定同一目标、选择及坐标；
父引用固定源码身份，后台校验其Artifact字节，不要求调用者回传expected SHA；
cursor只分页读取同一结果ArtifactVersion，不重新执行代码。各回执保留同一个inspection_ref。
需要调查资源的新Version时必须发起新的fresh调用。

`ctx.source/components/fragments/interfaces/checks()`返回既有事实的私有JSON副本。
`private_fragment(key, coordinate_space=...)`提供memoized detached计算对象；保存的局部bytes已施加
receipt uniform_scale，不再次缩放。`ctx.sections(plane, offsets_m=...)`复用Preview同一受信截面核心，
返回选择、坐标和完整量测JSON，不返回Shape或图片。私有计算修改不改变这些原始事实。
端口/接口位置、PNG外观和SOLVED都不自动证明材料贯通、接触、间隙或业务完成；异常局部应按业务风险
选择平面及邻近平面，证据不足时说明未决项。

`request_identity + request_digest`提供重放与响应丢失对账。服务器遇到提交结果不确定时重新读取
同一Task内的attempt：只有输入和终态完全一致才返回既有结果。GeometryQueryAttempt不是
CriticalOperation；它不修改GeometryResource、不创建IFC revision、不推进CURRENT，也不承担
资源内容权威。

## 6. 受信SYSTEM seed

`cad2bim-system-geometry-seed`是独立Backend CLI。它只读取发行包内固定的version-controlled manifest
和包内source，不接受用户路径或manifest参数，也不在Direct启动、MCP工具注册或普通bootstrap中
自动运行。当前生产manifest合法为空，因此CLI执行零资源写入。

非空manifest条目在任何Storage写入前完成source SHA、目标格式、共享Runtime归一化、重开语义摘要、
entry digest、既有publication冲突和全部候选的一致性预检。`seed_key + entry digest`确定资源Version
身份；相同条目精确重放，不同entry内容在稳定资源根下产生新的确定性Version身份。CLI只输出manifest digest和published/
replayed计数。条目只声明实际来源、格式、单位、局部坐标与预期内容身份；不维护人工validation、
serializer或runtime profile，也不将这些标签投影到资产manifest或lineage。

CLI数据库凭据必须继承`cad2bim_system_curator`，同时是`NOSUPERUSER`和`NOBYPASSRLS`。SYSTEM发布仍
经同一`PostgresControlPlaneRepository`、GeometryResourceService、StoragePort和RLS完成。I0-B3.02
受信SYSTEM seed仍只允许`mapped_model`；`modeling_function`的TASK Authoring构建合同已由I0-B3.03实现。
该privilege role仅额外拥有`alembic_version`的`SELECT`，用于复用统一
`Database.verify_ready()`精确核对当前Schema；它不拥有该表写权限，也不因此获得其他业务表权限。

## 7. 单一运行时与复用边界

I0-B3.02建立一个`GeometryRuntimePort`及其OCP/CadQuery adapter，提供normalize、reopen、render和
read-only query。Direct组合根只创建一个Runtime实例，并把它同时注入preview和query；启动时验证
ready，关闭时只关闭一次。

Data Transform和Geometry Runtime共享中性的source patch、Worker FD协议、容量控制、进程监督、
Darwin Seatbelt和Python user-code安全实现。两者保留独立领域入口与allowed imports；Geometry Runtime
不获得Data Package发布能力。全链只有一套Repository、Storage、Scope/RLS、Artifact Service、
CursorCodec和Worker监督实现，没有平行Catalog、影子DTO、第二事务边界或第二事实源。

## 8. 验证与后续边界

主要实现入口：

- Catalog/source/preview/query Application：`src/CAD2BIM_backend/application/geometry_*_service.py`；
- Geometry Runtime Port与adapter：`src/CAD2BIM_backend/ports/geometry_runtime.py`和
  `src/CAD2BIM_backend/infrastructure/geometry_runtime/`；
- 中性source/Worker机制：`src/CAD2BIM_backend/infrastructure/source_text/`和
  `src/CAD2BIM_backend/infrastructure/worker_runtime/`；
- durable query Repository与migration：`src/CAD2BIM_backend/infrastructure/postgres/repository_geometry_query.py`
  和`20260902_0067_geometry_query_attempts.py`、`20260929_0098_geometry_inspection_query.py`；
- 同次结果保存：`src/CAD2BIM_backend/application/geometry_inspection_service.py`、
  现Artifact/Data Runtime Repository及`20260929_0099_geometry_inspection_operation.py`；
- 受信seed：`src/CAD2BIM_backend/infrastructure/geometry_seed/`；
- Direct Provider：`src/CAD2BIM_mcp/direct/ifc/geometry/`。

验证按实际变更选择现owner入口：Domain/Application、canonical BRep/mesh归一化与重开、
Preview及私有计算副本隔离；Direct/Runner封闭Schema与资料投影；真实Worker截面与Query一致性；
准确Artifact/Operation、Query target/repair/分页、migration/RLS及重开回读。
SYSTEM seed、CAD/Data共享机制、IFC或wheel只在其职责和依赖受到实际影响时另选必要检查。
当前正式选择入口见`scripts/release/current_test_manifest.py`；运行记录与阶段接收由当前阶段入口承接。

Geometry与IFC QueryAttempt源码由`code_list/code_read`按原owner路由；显式保存和统一Code投影
不改变GeometryResourceVersion的几何权威。Family写入/Preview的当前能力由
[Family合同](product-family-write-and-realization-preview.md)说明，本页的几何调查证据不证明
正式IFC业务写入或更广产品验收。
