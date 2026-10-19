# ProductFamily Authoring与Product Realization Runtime

本文记录当前Authoring、Runtime和持久化职责。几何、参数、坐标与约束的唯一详细合同见
[Family自身几何](product-family-self-geometry.md)，公开调用见
[Family写入与预览](product-family-write-and-realization-preview.md)。工程与原生验收状态见
实施记录，不能由本文的实现说明推断通过。

作者生命周期见[草稿、Preview与发布](authoring-drafts-preview-and-publication.md)，本轮核收见实施记录。本页保留 Family 数值 ABI、Runtime、SYSTEM 准确复制与资产职责。业务构造规则不得写入通用 Runtime；历史实验记录不改写。

## 1. 唯一事实与责任边界

ProductFamily 保持 stable root、一个可编辑草稿和不可变正式版本。草稿 generation 仅用于并发编辑，正式版本号只在发布时产生。Preview 可使用授权草稿或符合当前合同的准确已发布版本，不要求其等于同根最新版本；Geometry 依赖同样按准确声明解析。

Realization Program 属于草稿快照或正式版本的源码资产，不另建 Program root、Code 表或结果库。编辑不改旧正式版本；公开 `ifc_products_apply` 选择已发布 Family 的准确版本。Component 作者沿同一核心保存 TENANT 资源，Direct / 普通 Workflow 保存 TASK 资源，权限与跨 Task 快照规则见 [TENANT 作者资源持久化](tenant-authoring-persistence.md)。

| 层 | 当前责任 |
| --- | --- |
| Domain | 结构、参数/来源、数值解预检、真实模型接口、约束与唯一业务finalizer |
| Application | Scope与exact依赖重授权、草稿保存、Preview执行、证据与原子发布/对账 |
| Runtime | 独立受监督进程执行同源包入口，实例化exact Geometry，返回有界receipt与几何 |
| Repository | 唯一事务内发布root/version/bindings/assets、Operation终态与Catalog alias |
| Direct | 封闭Provider DTO、JSON运输投影、公开错误及Code/Operation读取 |

Geometry参数、Family参数和Worker IPC各自保持原权威；入口JSON不是另一份业务事实。
Runtime不持有Scope、Run、Operation、Artifact、Storage、数据库或IFC CURRENT。

## 2. 当前执行合同

Family定义、作者数值输出及后台结果只使用当前严格DTO，不再携带人工schema/ABI代号或
固定runtime/budget profile。Runtime使用实际执行配置与预算；资源content digest绑定合同、
源码、exact Geometry依赖及资产身份，不绑定无人消费的固定标签。不存在旧合同解释器。

当前Family支持single_product或semantic_assembly、唯一root和无环parent闭包、完整fragment
结构、typed public/solved参数与fixed/public/solved来源。每个fragment声明有限的
`binding_options`，每项指向一个exact Geometry绑定；Program的SOLVED proposal为每个
fragment提交所选`binding_key`，Backend只构造该项。所有Family接口均引用同次Geometry
receipt的model_interface；不要求installation_anchor，内部约束支持显式frame relation的接口重合。
Program不自报权威接口，不存在attachment、Host edit、adapter binding或标准成员子集。

solved中的数值（含`number_array`元素和嵌套数值）必须显式声明minimum与maximum；
number_array的元素上下界直接放在该数组定义上。缺少边界时错误指出具体solved参数路径，
闭合参数集合中的未知键错误指出实际键/嵌套路径，不输出参数值。

Family定义可复用的构造、组件结构和接口约束规则。来源支持的实例直径、长度、角度、路径和
定位变化由同一exact version的public或有界solved参数表达；固定来源需要明确的规则依据。
名义场景不是Family的唯一尺寸。规则、源码或合同变化先编辑同根草稿，再次发布才追加不可变 version；明显不同的结构规则才建立另一 root。

`semantic_assembly`的根可采用当前闭合IFC类枚举中的实体产品类并带子产品。实体根必须拥有
geometry；`IfcElementAssembly`可作为无geometry的语义根。每个非根构件必须拥有至少一个
geometry fragment，由 `owner_component_key` 指定。bindings只管理依赖；同一绑定的多次
实例化可以分别属于不同子构件。唯一根、parent引用闭合和无环规则不变。该树描述产品
分解及几何归属，不能代替实际接口连接或IFC写入。

部件Geometry与Family相对组装的作者职责按
已批准设计执行。
当前Runtime支持多绑定、多实例、子件归属和接口约束位姿；业务资产是否合理拆分仍需源码
审查。现有单资源part分支版本不能仅凭结构合法就宣称符合新的资源/组装职责要求。

旧 typed 合同及旧 Write repair/revise 执行模式退出当前入口。已有完整且符合当前正式合同的版本继续准确读取和执行，不因作者流程变化而全库重注册。

## 3. 保存草稿、Preview与发布

Write 仅核 Scope、CAS、静态源码入口、合同及绑定，保存 Artifact 快照并更新草稿指针；不要求样例、不执行 Runtime、不发布。update 只传变化并继承准确草稿中的未变内容。

Preview 冻结来源与 Geometry 依赖，再沿现有 Runtime 执行单例或有限代表。固定几何草稿在受理/重放检查之后构建。预期由 Application 同实际结果比较，不注入作者程序。`retain=false` 不创建持久报告或 Operation；`retain=true` 保存报告和成功案例的同次形体。保留结果能直接 Query、截面或换视角，不再求解。失败前的完整结果保留，未执行项不算通过。

Publish 只读适用的保存证据和形体，准备准确 Geometry 闭包及 Family 资产，在原 Repository 根锁、CAS与事务中提交。没有第二套验证器。旧 repair/revise 方法退出；保存失败或验证失败后继续编辑当前草稿。精确公开参数见[Write与Preview](product-family-write-and-realization-preview.md)。

### 标准模型与发布证据

标准能力由 `standard_model` 声明；`nominal_scenario_key` 位于 Preview 报告，标准产品参数按当前声明提供。参数正规化继续复用现有 InputService。同一 canonical source bundle 提供 `main.py:realize(ctx)` 与 `main.py:standard_model(ctx)`，均返回原数值 Proposal；Backend 以 `T(product_origin)` 放置世界根。标准构造不接收调用方外部要求；内部名义连接目标可复用同一 solver，不依赖已删除的公开安装参数。标准连接改造的实现与验收状态见当前目标。

发布验证至少包含：METRE零原点的名义标准SOLVED、同参数非零原点的标准SOLVED、
正常self_geometry SOLVED。声明必需external_constraint时，至少有一组带外部条件的self_geometry
INFEASIBLE与同public参数的标准SOLVED对照。其他内在构造无解场景不要求同参数标准SOLVED；
是否携带可选外部条件不改变这一点，也不能据此推断实际失败原因。预期状态仍须由真实执行结果
验证；非零self不能代替标准入口的非零原点对照。场景通过不证明连续支持域全覆盖。
这些场景执行同一份声明与Program输出合同：fixed来源等于声明值，public来源等于正规化公开
参数，solved来源等于`internal_parameters`中的同名值；fixed位姿等于声明，program位姿由proposal
完整返回。同一正规化名义参数的两个standard_model场景必须保留相同Family-local内部构造，
安装原点差异只由Backend的世界根平移产生。

新版本资产为realization_source_bundle、standard_model_geometry与standard_model_manifest。
manifest只保存当前合同、源码、绑定、正式几何SHA与
summary；案例参数、selector、Proposal与逐fragment receipt由原Preview报告保存，不在正式manifest复制第二份事实。AssetService按exact身份、
metadata、size和SHA回读并交叉核对。

### Backend SYSTEM exact复制

`SystemFamilyPublicationService.publish(scope, SystemFamilyPublicationCommand)`是后台管理入口，
不新增MCP工具或Direct/Runner DTO。Command固定原`request_id`、source Family root/version/
content digest及SYSTEM target owner，不接受DirectSession、alias、源码或skip_validation。
普通作者沿草稿→Preview→Publish；复制从已成功的当前TASK正式版本及真实资产建立新
SYSTEM Family root/v1，parent/base为空，不改源，不复制源作者Operation或历史版本。

来源按现行Domain/合同和完整真实资产验证；不保存或读取布尔发布资格标记。
`CatalogSystemFamilySource`以源LOGIN/identity独立读取；
`SelectedLibrarySystemFamilySource`从明确选定的现行8张领域表和payload闭包投影原Domain。
selector只选择exact IDs/content digest和闭包；asset_files只定位文件；实际大小/SHA与
标准manifest按领域asset和原AssetService核验。不解释旧合同、补造源Operation，或要求
整库迁移。历史路径、环境和usage等重复副本不成为资格凭证。

权限由真实LOGIN与管理Scope共同约束：LOGIN非super、非BYPASSRLS，继承
`cad2bim_system_curator`，并拒绝额外API/MCP/Runner runtime membership。
`CAD2BIM_SYSTEM_CURATOR_POSTGRES_PASSWORD`对应固定`cad2bim_system_curator_login`，
只授curator NOLOGIN权限。账号初始化支持`role_names=("mcp", "system-curator")`，
管理CLI为`cad2bim-db-runtime-roles --roles mcp system-curator`；显式选择时只读取、
校验和轮换所选账号的密码，每个所选账号的密码均必需。省略选择时初始化API、Runner和
MCP账号，并在提供curator密码时一并初始化curator。
独立产品库按业务迁移、所选账号初始化、发布的顺序准备。Checkpointer的建表和Runner表
授权由`cad2bim-checkpointer-setup`负责，在部署Runner时显式执行。
普通LOGIN声明curator actor仍被数据库角色检查拒绝；bootstrap owner只迁移/provision。

管理身份由原identity directory解析`CAD2BIM_SYSTEM_PUBLICATION_SUBJECT`；原TaskRun
Repository以publication_name打开持久Task/Run，module=`system_library`、
stage_key=`publication`。复用现有非Graph `DIRECT_MCP`枚举，不建立DirectSession binding。
curator owner边界只读取已解析users并核tenant，不写users；Task/Run/Artifact/Operation
仍在管理tenant/task下，资源owner另为SYSTEM。

0097仅在原Family Operation/detail、原授权/事务上增量准入system_copy。curator可读取
管理身份、Task/Run、source Artifact、Operation/Catalog并创建该管理链；Artifact更新、
Task/Run status与Operation终态/claim列保持受限。现有资产/Operation Catalog RLS的只读
可见性guard获得必要EXECUTE，角色和RLS不放宽。Family四领域表仅SYSTEM INSERT；
Geometry复用既有SYSTEM发布权限。Data输入表无curator SELECT/INSERT，统一Operation
投影只在DATA_TRANSFORM读取其实际输入。0100增量迁移接入草稿与发布；SYSTEM复制管理链继续复用。

编排沿完整依赖拓扑准备SYSTEM Geometry，重写geometry_inputs中的exact引用及当前
manifest/content digest；固定BREP和其余实际字节保留。同管理Run内同source Geometry
version共享同一目标ID，不逐Family克隆。Family bindings只引用SYSTEM exact版本，
使用其实际target digest。Family ZIP/标准BREP字节保持；typed标准manifest只重定向
family_version_id与binding digest。业务binding digest复用三事实：binding_key、kind、
实际Geometry content digest，不以新ID或环境指纹判失效。

不可变资产由原Storage准备后，原Family Authoring保存完整copy plan（源exact/owner、
所有目标G bundles和Family目标assets），再逐G调用原publish_candidate，最后在原Family
Repository短事务里核完整G闭包并原子提交F/Operation/Catalog。复制不分配Runtime、
不求解scenarios或重建BREP，管理组合根沿现有ifc_application_resources接线。
缺少/损坏/未授权源在受理前拒绝；技术连接/数据库故障保留原语义，不伪装源无效或FAILED。

原请求先于来源读取：同Run/request_id派生原Operation/Family目标ID，参数变化按原
`PRODUCT_FAMILY_IDEMPOTENCY_CONFLICT`拒绝。中断保留已完成G及同Operation的完整计划；
有claim的PREPARED中断转INDETERMINATE，不暴露F。重建服务使用原请求恢复，不重读源、
不换CURRENT/alias或建立第二账本；真实提交后响应丢失沿原成功对账与claim fence返回
同Operation/F/G。成功重放继续验证保存的资产。copy Operation不能转为普通TASK repair。

CLI复用原Settings ENV_FILE、DATABASE_URL、STORAGE_ROOT和上述subject：

```bash
CAD2BIM_ENV_FILE=/path/to/private/curator.env .venv/bin/python -m CAD2BIM_backend.infrastructure.system_library.cli \
  --publication-name library-publication --request-prefix library \
  --selector /path/to/source-selector.json --library /path/to/selected-library.json \
  --family-key pipe
CAD2BIM_ENV_FILE=/path/to/private/curator.env .venv/bin/python -m CAD2BIM_backend.infrastructure.system_library.cli \
  --publication-name library-publication --resume-request-id library:pipe
```

省略family-key将处理明确selection内所有命令；resume只从同publication_name管理Run上的
原K保存事实恢复，可不带源文件。返回request/Operation/F/G exact映射，不输出凭据。
工程证据与当前范围见执行回执，
原代表包含真实角色、原K恢复、跨租户读取与一次真实build；实际库批量发布与模型业务
接纳以阶段回执为准，不由该后台能力自动完成。

## 4. 源码与Operation/Version资产

草稿源码与定义保存为不可变 Artifact 快照，根仅维护当前指针和编辑令牌。原 Write/Preview Operation 固定当时输入；恢复不能改读最新草稿。正式版本拥有完整正式资产，执行不回退到创建 Operation。

Code 可读取授权草稿的冻结源码和准确正式源，重新核 Scope、Artifact、MIME与完整性。Preview 报告保存案例与形体来源，正式 manifest 保存发布身份及后续执行信息，两者不复制可变权威。

## 5. Runtime与实际接口检查

请求固定完整当前合同、source bytes/SHA、exact Geometry bindings/assets、正规化public参数、
product_origin、external_constraints及policy。InputService仅按显式IfcLengthUnit换算原点，
Runtime只接受METRE；已有`_m`字段、方向、角度和无量纲尺度不递归换算。
作者API为contract、param/parameters、product_origin、external_constraints与policy，读取返回
隔离快照。整个Family bundle仅有math与数值能力，不能经main或helper导入cadquery/OCP，
也不能访问instantiate、Shape、实际接口、Query或Host内部状态。Geometry独立保持受控建模能力。

external_constraints 的 condition.kind 已封闭为 finite_segment／fixed_point／infinite_axis。
public_parameters 承载 Family 定义的有类型计算与布局资料，包括确有计算职责且注明单位、来源和用途的宿主事实。
external_constraints 独立断言绑定输出接口的关系，不能作为免检上下文，也不能以本族算出的点自证正确。三种位置均显式给
direction_relation；fixed_point 的 none 分支不允许 axis/reference_direction，有向分支要求非零归一化轴。
finite_segment/infinite_axis 在 none 下保留位置定义轴。OD 对三种类型均可选；Family 最低要求可使其必填，任何已给 OD 都比较实际输出接口圆形截面。Family
external_constraint 用 allowed_kinds 替换 allowed_positions，保留 required、方向、profile、roll
与最大容差职责。interface_key 沿原 Family 到 Geometry receipt 链完成实际位置、方向、OD 与
可选 roll 检查，不另建求解或验证平台。标准 DTO、运行接线与 Provider 的受影响检查已通过；
十二族定义／源码／原代表数值已适配，每族一条原 self_geometry 代表已通过真实 supervised
Runtime、独立 codec 与同次 fragment receipt 检查，frame 与全部外部条件通过；本次不做整体 world compose／render。
五族标准配置缺项已修复并通过标准入口与对应程序验证；十二份 definition 已全部通过当前
ProductFamilyContract 与真实 normalize_standard_definition。正式发布／资源替换的剩余验收按
当前清单记录，不将程序或实际片段构建通过写成正式版本已发布。

Backend执行Family绑定的Geometry以及Geometry Preview单次/批量执行共用受控编译、能力和建模入口。
批次只共享编译结果、Host固定几何模板和exact依赖闭包；每次新建作者模块、默认参数、
Context、arena、几何副本与receipt，实际Geometry函数逐次执行。调用结束撤销能力持有的
Host/native引用，包括作者全局容器和循环闭包间接保留的能力。原生异常或越界使整个
Worker终止；未发生能力违规或使调用失效的原生异常时，Program合法返回的INFEASIBLE可
继续后续实例，已发生fatal错误不得转为INFEASIBLE。已验证边界不构成
任意Python/native程序的形式安全证明；历史exact报告与几何事实不因迁移而改写。

Program返回SOLVED、AMBIGUOUS或INFEASIBLE。作者SOLVED proposal包含完整internal_parameters、
component placements及fragment placements；每个fragment提供binding_key、局部位姿与uniform_scale。
internal_parameters只承接声明的solved参数；public值用ctx.param读取，不能在其中重复public键。
不得返回geometry_parameters。binding选项与owner由静态合同限定，作者不输出root世界位姿、handle、
实际接口或约束结论。唯一Domain来源解析器按保存的完整parameter_sources.overrides、正规化public
参数和internal_parameters组装Function的完整geometry_parameters；mapped参数为空。整份数值解
通过key集合、来源、刚体位姿与尺度预检后才允许Geometry执行，非SOLVED不建模。
canonical执行结果继续包含完整geometry_parameters。Host/finalizer独立重算来源并比较完整字段，
不填补或覆盖canonical结果中的缺失或冲突；同次receipt按fragment_key关联并保留完整性复核。

组件与fragment的`local_transform_mode`默认`fixed`，局部位姿须等于`fixed_local_transform`
（缺省identity）。显式`program`要求程序提供完整局部位姿，不能同时声明fixed transform；
根组件保持fixed identity。组件位姿相对parent，fragment位姿相对owner；不再要求到安装anchor的
刚性覆盖图。mapped系统尺度必须等于声明的正有限来源，function系统尺度只能1；函数自有尺度
由其参数合同和build处理。mapped尺度只作用于局部几何/接口，不重复放大组件或fragment平移。
同次实际接口仍按原始内部约束和外部条件检查，错误位姿可产生SOLVED但frame FAIL，不能通过发布。

公开主示例复用原Provider DTO与Domain合同，展示public尺寸、solved长度、独立部件的可变
位姿和必需完整帧接合，并执行多组尺寸及非identity放置。示例是教学用构造规则，不能视作
任何机械产品的完整设计；真实截面、接合和通道仍需Body量测。

合法SOLVED候选可以伴随`frame_check_status=FAIL`，供诊断Preview与只读Query；这种结果不能
通过Authoring的预期SOLVED发布门。结构非法、来源或receipt不闭合仍拒绝。frame PASS不证明
任务特定实体接合、流道、体积或制造条件，需实际几何量测。

Runtime复用原WorkerProcessSupervisor、DarwinSeatbeltSandbox、共享capacity与中性codec；
不共享不同领域的协议、终态或权限。当前单次执行路径的one-process-per-call受30秒wall/CPU、2 GiB RSS、
256 MiB scratch及源包/IPC上限监督。Family源包最多64个成员、每成员65536 UTF-8 bytes、
总源码262144 bytes；Geometry源包另有自己的限制，不因Family预算扩大。
Application排队/执行/终态额度见[统一运行边界](tool-usability-and-runtime-boundaries.md)，
保存和精确读取规则见[共享Preview](geometry-preview-and-inspection.md)。
内部数值`realize_batch`与几何`precheck_batch`→`instantiate_batch`分别通过同一Request/Result和finalizer处理有界序列；InputService一次加载
同exact版本的1—1024项正规化请求。Product每个阶段页使用一个受监督进程，900秒（15分钟）执行链预算内
逐项仍有30秒wall/CPU和剩余总预算限制；封闭帧验证顺序、输入digest、每项output need、字节预算和终态。
同批返回消息和BREP累计输出上限为256 MiB，由`domain/runtime_execution.py`统一配置，
原Product协议导入该预算，发送端和Host接收端共同检查；预算调整与建模实现身份分别承担各自职责。
批量输入上限为16 MiB。2 GiB RSS约束进程运行内存；单份几何及每件结果仍分别按各自协议上限验证。
900秒墙钟预算覆盖本次Product批量处理及后续Codec接续；Product批量Worker另受900秒累计CPU预算监督，
CPU按进程user+system时间计量，多线程运行时可先于墙钟预算耗尽。排队另有600秒额度，逐项预算不随批量增长。
案例或进程CPU/墙钟超限时，Supervisor在失败发生处保留该层的实际增量、限额和scope；
Family Preview沿现有资源错误/逐案diagnostic返回安全摘要，不能用进程900秒代替案例30秒，
也不把线程累计CPU当成墙钟时间。批次中断后的未运行项仍为not_run；缩小批次不提高案例限额。
缺少可靠计量时不填零或猜测原现场，不公开子进程原始异常、私有路径或身份。
启动 ready 超时明确表示尚未派发案例；codec 中断则说明求解已返回、后台形体检查尚未完成，
在现有 output_validation 诊断中保留实际 normalize/render 操作与资源计量。未通过后续检查
的结果不作为已接收成功项；资源超时本身不证明几何无效或安装物理无解。
Product阶段从整链deadline中预留现有Codec执行预算45秒，最迟在整链第855秒结束。
每项派发前，Product阶段墙钟及累计CPU余额须覆盖该项30秒上限及1秒收尾余量；
余额不足时按实际完成数发送finish。Child、Host握手及终态均核对这一完成数。
1024是提交数量上限；实际分页同时依据已观测耗时、CPU和输出预算选择。
同需求调用继续传单个need，不同需求传等长序列，均在启动前正规化；need不是公开业务参数。
同exact源包的realize/standard_model入口在一份module编译中全部检查，入口执行仍分别新建
importer/module/ctx/arena，依赖和原Geometry生命周期共享，作者可变module状态不跨项复用。
Host仅将通过完整帧、字节完整性、结果关联及单件预算检查的记录加入已接收前缀。
Product退出或被监督终止后，完整前缀按原有界分组交Codec独立验证，验证成功才yield；
随后传播原Product错误。当前半条结果不进入前缀。Codec失败以Codec原因及组范围报告，
同时保留原Product异常链。取消直接回收，不启动新的Codec工作。
预算正常收尾在前缀yield后返回既有ResourceLimitError，诊断为
`phase=batch_dispatch`、`reason=batch_budget_exhausted`，附完成数、剩余数和资源种类，
`item_index=None`，表示下一件尚未派发。工程驱动仅在该明确边界且本次有进展时，
读取STOP并用原剩余请求开启下一批；技术失败与零进展停止。已yield产品沿现有账本保存与恢复。
单槽容量保持顺序接续。调用者提前停止消费必须关闭异步迭代器；
取消/异常路径沿共享Supervisor回收任务和进程。Family注册仍消费完整结果后沿原事务一次发布。

同exact生命周期内，Geometry Domain私有准备态深拷贝Schema，完整检查结构/default并准备
validator；注册样例、内嵌Geometry和批次复用同一规则，每件仍正规化和验证实际参数及来源，
嵌套默认值不共享。消费规则时核对canonical Schema，变化不得继续使用旧准备态。批次内部
准备一份完整验证且独立拥有的Request闭包，逐件恢复验证invocation、Family关联、顺序及
输出模式；不为逐件输入和结果再生成或回传自摘要。每件闭包副本互不污染。外部Request与公开参数正规化入口继续完整验证，
不存在公开的“已验证”跳过字段或跨Scope全局缓存。

数值Worker先完成Family求解和Proposal正规化，再执行所选函数Geometry的interfaces.py或读取
fixed准确manifest。唯一Domain业务finalizer使用不含BREP的interface_evaluations生成世界位姿、
命名接口数值事实与声明约束求值结论。正常FAIL保留execution_stage=interface_constraints_evaluated的SOLVED诊断，无实体；
缺失引用key、程序、协议或资源技术错误首错停止。实例化Worker直接消费同一Proposal和预检事实，
不再次求解或重算关系，真实receipt绑定实际局部BREP。Host在进程边界核对实际请求身份、
Result保存完整typed invocation与policy，以及Request已有exact family_contract_digest，直接比较
原点、单位、外部条件、realization_kind、参数和政策，防止把旧PASS移到不同输入或Family合同。
准确绑定与源码、Proposal正规化状态和fragment/receipt关联，使用纯integrity入口，
不再次运行完整业务finalizer，也不生成或重算整份结果的自载摘要。实际Geometry和
Artifact字节仍由各自不可变资产SHA校验；保存与恢复使用原Operation和准确Artifact引用。

内部输出需求为solve_only、interfaces、local_fragments或preview_world。solve_only只求解，
零Geometry执行、无receipt或接口结论；interfaces完成接口求值及关系检查，没有BREP/receipt。
Result显式execution_stage为solved、interface_constraints_evaluated或instantiated，保存读回按阶段校验，
不从无receipt或无保存体推断solve_only。local_fragments生成局部几何并复用同次接口检查，
不合成展示；preview_world在此基础上按需合成一次世界模型。Preview 根据保留形体、出图及案例预期选择内部输出需求，不要求作者重复声明内部输出模式。

`interface_constraints_evaluated`表示数值求值阶段完成，结果可为PASS或FAIL。`frame_check_status`
汇总Family声明的内部接口约束及该行显式外部条件，不表示外部IFC对象的安装关系已通过。
有限段条件约束求解接口的允许站位；其PASS不意味着当前管道终点已在该位置，也不量测BREP实体端面。
Apply的`interface_constraints_evaluated_product_count`按完成该阶段的SOLVED输入产品行计数，
包括已完成但约束FAIL的行；它不是接口数或通过数。后续构造仍按原PASS门禁放行。
命名接口报告属于同次执行；提交仅保存请求范围内产品及其子件，后续编辑不会刷新旧报告。
Agent依据当前模型建立跨对象关系并显式协调；真实IfcPort拓扑属于另一种保存事实。

Codec后续组失败不撤销已经验证并yield的较早组；调用者不能把部分yield视作整批成功。
Product提交Codec子批时，将BREP字节及现有协议控制帧一并计入32MiB输入预算；
以逐件完整控制帧作保守上界，达到边界即分组。单件封装超出子批预算时使用既有单件Codec入口，
先交付待处理的前序片段，保持原Product/fragment顺序。Product累计输出预算与Codec子批预算分别约束各自边界。
批量输入正规化失败仅在当前invocation可确定时携带页内`item_index`；exact资源或绑定准备
失败不归到某件。Product技术故障的可选`item_index`表示故障时正在执行的项，不证明该件
是唯一致错来源；无在途项时保持未知。Codec故障只给`phase=codec`、原页组起点、组内件数
及有界原因，保留底层诊断和异常链，不从文本猜单项序号。观测事件有界记录Codec片段到
页内项的映射；调用方只确认故障前已yield的组，故障组及后续项仍未确认。
Family Preview逐案先准备numeric-only请求并预检，正常FAIL只保存数值报告；PASS才按同一exact
闭包读取选中重资产并实例化同一Proposal。独立Geometry Preview与Query样例亦先执行
interfaces.py，再读取冻结输入依赖并build。固定mapped创作仍可一次build取得发布接口。
选中mapped资产的occt_brep_v1格式在重资产读取前由InputService核对，不检查未选中替代绑定。
Preview保存真实逐案结果；Publish独立按定义准备正式资产，可选关联实例只用于回查。当前批量能力及小样本测量不证明工程全量吞吐，后续优化仍沿
完整成本目标。

上述数值链复用同一Runtime owner，Core消费`precheck_batch`与`instantiate_batch`。
IFC事务与工程业务核收由当前apply切片承担，Runtime局部测试不代表IFC写入已验收。

Family程序异常使用其自身bundle定位源码。Backend执行绑定的Geometry程序时，
异常在该binding边界按Geometry bundle定位，返回实际出错成员的`code_sha256`、
`source_path`、`line`及`source_line`，并附`binding_key`和`source_bundle_sha256`。
同名`main.py`或辅助文件不会被外层Family源码替换。错误仍沿原
`REALIZATION_EXECUTION_FAILED`和Worker terminal传播；这不改变执行/发布结果或重试规则。

## 6. 持久化、Catalog与恢复

0100在既有Geometry/Family根关联一个草稿Artifact指针和generation；不新增候选资源表或验证数据库。Artifact保存准确内容；Operation关联保存、保留型Preview和发布结果，临时Preview不伪造恢复记录。

发布沿既有Repository，按稳定顺序锁定相关根，校验CAS和已发布依赖，在单事务中提交需要的新版本、绑定、资产关联和终态。未变Geometry可复用，包括首次随Family发布的成果；当前Run回执明确保留原证据Run，不能错用跨Run alias。响应丢失按原Operation对账。

已发布SYSTEM/Task资产保留。旧Write执行/repair合同退出在线链，历史记录原样保全。普通Task不能因SYSTEM公开根可见而读取管理草稿。工程验证与尚未执行的原生作者验收分别记录于实施核收。
