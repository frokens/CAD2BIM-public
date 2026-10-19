# Family 自身 OCCT 几何与显式约束

本合同记录 B3.07 的职责与几何语义。唯一当前合同入口已实现，工程验收状态以
清理记录为准；验收状态和原始证据见
实施记录。它实现自身几何的构建、
放置、独立约束检查与只读 Preview；不赋予 IFC 安装或外部连接资格。目标设计仍见
设计文档。

当前源码采用product_origin、单位与程序计算位姿合同。
Family调用不再要求installation_anchor、datum_transform或到anchor的完整位姿覆盖，保留实际内外部
接口检查。历史资产与证据不改写；Geometry独立预览的datum保持其原合同。这里的代码合同不表示
数据库迁移、工程资产重建或原生验收已经完成；R1限定切片及退修01已独立接收。
当前作者生命周期采用草稿写入、Preview与独立发布；标准能力由standard_model声明，
名义参数在当前声明中提供。当前入口只接受符合当前typed结构的准确Family闭包，不携带人工合同/ABI代号。
实现、数据库验证与原生作者验收分别按当前阶段回执记载，历史证据不换写。
当前外部安装输入已使用 finite_segment、fixed_point、infinite_axis，安装事实与产品配置分开。
标准 DTO、运行接线、Provider 与 Preview 机制的受影响检查已通过；十二族定义、源码与原代表数值已适配，
每族一条原 self_geometry 代表已通过真实 supervised Geometry Runtime、独立 codec 与实际 fragment／端口检查，
frame_check_status 均 PASS、external 检查全部 satisfied；本次未做十二族整体 world compose／render。
五族标准产品配置已补齐并通过标准入口与对应程序验证，十二份 definition 均通过当前 ProductFamilyContract
及真实 normalize_standard_definition。正式发布／资源替换尚未完成，既有 0100/v4 库不兼容当前合同。
新资源与七项验收的未完成范围见当前清单，不将构建或标准程序通过写成新库已发布。
有限段两端点构成整段允许求解范围，实际接口不要求位于中点。
不以固定点或无限轴替代有限段失败。Geometry接口随实体参数一致变化由作者及其必要
回归负责，不新增自动BREP端口识别机制。阳极表面接入与鞍座宿主含义不改成轴心切管口；全量剩余长度
核算属于阶段结果检查，非当前Family输出Package能力。

Family程序返回完整参数/位姿，Backend对整份数值解预检后按fragment建模，再由唯一finalizer
检查资源模式声明的连接接口。Geometry构建和实际Shape读取由Backend执行；receipt保存
同次几何与接口证据。

当前工程TEE新增的显式`shape.mode=near_zero`由Family数值选择、Geometry同次build量测
实体验收：该分支接口frame表示原source逻辑连接位置/方向/OD，物理主管为0°直轴，实际
圆环端面的中心、法向和口径另行检查，超界即拒绝。模式保留于原
`proposal.internal_parameters.shape.mode`（工程摘要为
`proposal_facts.internal_parameters.shape.mode`）；当前receipt不另存成功端面的偏差数值，
需要数值时应从同一保存BREP独立回读并记录。消费者须识别上述模式；不得把该接口轴称为
实测端面法向，也不能从Runtime的frame PASS推断
两者零偏差。该资源局部近零预算不改变通用`GeometryModelInterface`、外部约束阈值或
普通构件接口合同；详细数字与适用范围见TEE Family/Geometry `usage.md`。

## 合同与参数

当前定义、程序输入/输出及保存资产使用一套严格DTO，不提供schema/ABI代号或版本选择分支。
历史 Artifact、源码、Preview 和实验记录在离线存储中保留原字节；旧Family不再提供在线
发现、info、Code/read、完整bundle读取、执行、repair或replay。
需要继续使用的资产须提交完整当前定义，生成新 immutable Version 并重新验证。
当前 Preview 只接受 `self_geometry`、`standard_model` 和 `inspect`。

Geometry 的唯一参数权威仍是 exact version 的 schema。原 Normalizer 支持闭合 object、
同质 array 及原标量类型；树深度最多4、节点最多256、每层对象最多64字段、数组最多128项，
schema 和正规化参数各保留16 KiB UTF-8预算。拒绝 `$ref` 和无界结构。默认值在原权威处
递归正规化，BuildContext 向程序提供独立副本；程序内修改嵌套值不会改变原输入或receipt。

Family 继续使用自己的 typed 参数合同，支持 array/object，不把 Geometry JSON Schema
变为 Family 的第二权威。节点类型、单位、范围、required/default 在原 Normalizer 中校验；
Family 调用正规化值保留64 KiB预算。映射到某个 Geometry 后仍须满足它的16 KiB和schema。
这两个预算对应不同对象，不能互相替代。

`tagged_object`只表达带显式标签的封闭对象分支：`discriminator_key`指明标签，
`branches`包含2—32个匿名完整object值合同，不携带parameter_key、required或default。
各分支含同名required choice标签，choices仅一个值且无default，各标签值唯一。
选中分支沿原对象正规化；缺字段、其他分支字段和未知标签拒绝，未选中分支的默认值不加入结果。
公开调用值仍使用现有`object`包装，不新增同义DTO。全部分支计入节点预算，分支选择不增加实例深度。
对应Geometry采用安全Schema的带标签`anyOf`子集，各分支同样是完整封闭对象；其独立入口仍完整验证。
Family与Geometry声明相同字段时，默认值须一致；成功proposal必须提供所选分支的完整参数。
该能力不改变SOLVED的完整组件/片段集合；选择Geometry绑定由fragment的有限
`binding_options`与SOLVED proposal的`binding_key`表达，不能用参数标签暗中切换资源。

array.items同样是匿名值合同，不携带parameter_key、required或default；object.properties中的
具名字段继续声明自身key、required及有效default。number_array用length声明0—128的精确长度，
unit/minimum/maximum约束每个元素。正规化复用原array+number规则，布尔及非有限数拒绝，
长度、单位换算、路径和树预算均与完整写法一致；持久声明保留number_array简写，不另存展开合同。

Family可按本族用途声明number/integer/boolean/choice/number_array/array/object/tagged_object参数；未知key被拒绝，
没有任意Python对象或任意表达式合同。公开参数是调用输入，solved参数是程序求得值；
两组是独立命名空间，各组内顶层key唯一，跨组允许同名且取值可以不同；Geometry绑定通过
`source_kind`指定读取哪一组，不合并或互相覆盖。声明参数不等于Backend自动执行优化或证明
任意代数关系。Family的interfaces允许为空，
合法组件/几何结构仍必需，不能把每个位置参考强制解释成外部GAS端口。

每个 fragment 至少声明一项`binding_options`；每项的`binding_key`必须出现在定义顶层
`geometry_bindings`，所有选项均须在同次请求中精确授权，SOLVED proposal为每个fragment
选一项。每项的 `parameter_sources` 使用 `{default_source,overrides}`：default_source可为
public_parameter或solved_parameter，overrides的来源为fixed、public_parameter或solved_parameter。
Write在准确Geometry参数Schema处按每个参数key展开：显式override优先，其余按default_source
引用该命名空间的同名顶层参数，缺名拒绝且不改用另一命名空间或默认值。没有default_source时，
省略且有合法Geometry默认值的参数展开为正规化fixed，其他缺项拒绝。未知键或引用拒绝。
展开后的完整overrides在比较及保存之前冻结，保存体不保留default_source；源码或说明单独修改
继承原展开结果。求解变量沿用
typed 参数，数值叶子必须有显式上下界。顶层solved参数可设`required=false`且无默认值，
供未选分支直接省略；选中项引用该值时必须实际提交。公开参数与所有嵌套可选字段仍须有
有效默认值。每项的`uniform_scale_source`单独校验，默认固定为1，可变值要求有上下界、
为正且无量纲。
后台按fixed声明、正规化public参数及internal_parameters的solved值唯一组装完整Geometry参数。
作者proposal不得包含geometry_parameters；canonical结果保留完整参数，Host/finalizer重算并逐项
比较，不为canonical结果填漏或改值。同次receipt及场景执行继续核验对应关系。

当前尺度来源合同：
只有`mapped_model`允许可变的`uniform_scale_source`；`modeling_function`固定为1或省略。
函数要支持尺度变化，必须在exact Geometry的`parameter_schema`声明真实参数，经
所选项的`parameter_sources`提供并由函数实现。后端不在函数产物外再施加业务尺度，必要单位换算
不属于这项禁止。作者SOLVED必须提交完整求解变量、所选binding与位姿/尺度；完整Geometry参数
由保存的来源展开结果组装，运行时不用Schema默认值补漏解。

当前component/fragment 的 `local_transform_mode` 缺省为 `fixed`，此时局部pose等于
合同`fixed_local_transform`（缺省identity）；预检可补齐省略的fixed位姿。显式`program`要求
程序返回完整刚体位姿，不可同时声明fixed transform；根组件固定identity。组件相对parent、
fragment相对owner，Backend在任何Geometry调用前检查完整解的key、来源、scale与pose，
随后依据同次Geometry receipt检查实际关系。声明约束不意味着Backend自动替作者求解。
精确规则见[Runtime相对位姿检查](product-family-authoring-and-realization-runtime.md#5-runtime与实际接口检查)。
相同正规化名义参数下，standard_model的identity与非零product_origin场景必须返回相同的
Family-local参数、组件、fragment与位姿；Backend只在其外应用安装原点平移。
Write仅保存并预检当前草稿，不执行场景。各入口的字段诊断、实际运行及恢复边界分别见
[Write/Preview合同](product-family-write-and-realization-preview.md)及[Runtime合同](product-family-authoring-and-realization-runtime.md#5-runtime与实际接口检查)，不沿用旧三种注册模式的说明。
多点管道只是普通 `build(ctx)` 函数，沿原 Authoring 注册，没有产品专用
Backend 几何分支。其工程样例支持域见 `tests/fixtures/geometry_runtime/multipoint_pipe/README.md`。

## 构件结构与几何归属

Family 的 `components` 声明 IFC 类和父子关系，`geometry_bindings` 声明 exact 资源依赖，
`geometry_fragments.owner_component_key` 声明每个几何片段属于哪个构件。同一 Geometry 可
实例化多次并分别归属不同子件；依赖列表位于定义顶层不表示所有几何属于根。每个非根构件
必须拥有几何；物理根也须有几何，`IfcElementAssembly` 根可以没有直接片段。实体产品根
带子件同样合法，不强制每个 Family 都有空装配根。

同一component可以拥有多个fragment；Geometry实例及fragment不是额外的IFC产品类别。
通用合同没有主体/附属类型或按body名称分派的规则，根component也不自动等于主体。
IFC表示中的Body指形体表达，不是“主体部件”；具体配方的body只是业务命名。

现有机制支持部件局部几何加 Family 相对组装。业务资产应按
已批准职责要求
选择拆分：Geometry提供部件形状与接口，Family组织实例、归属和位姿。该作者设计要求
不等于当前所有资产已经采用此结构，也不是新增加的Schema自动语义判定。

## 产品原点与内外部约束

普通、标准和批量调用都显式提供有限三维`product_origin`与`IfcLengthUnit`。InputService只将
原点正规化为米，Runtime只接收METRE；既有`_m`字段、方向、角度与scale不换算。世界根唯一为
`T(product_origin)`，作者不能覆盖；该点是产品位置参考，不要求任何模型接口恰好位于原点。
组件与fragment旋转/平移沿唯一局部变换链组合，mapped尺度不重复作用于这些平移。

外部位置断言只由标准 condition 分支表达，不保留 free、point/line 的旧公开结构。组件与 fragment 的 fixed
位姿仍受静态合同约束；通用 Runtime 不接管具体产品的布局或数值求解。

当前接口全部来自实际模型接口；没有旧Host attachment roles。当前内部约束支持可独立
检查的 `interface_coincidence`。只有 Program 自报 observation 的 shape 约束不能作为
当前验收条件。standard_model 必须保留原始完整 component、fragment、interface和内部约束，
不能靠删减标准子集把冲突变成通过。

Family 在 external interface 上声明外部条件的最低要求：`allowed_kinds`、是否必需、
allowed_directions、roll/profile 要求以及最大容差。allowed_kinds 替换 allowed_positions；调用
必须符合这些要求，不能漏掉必需条件、放宽容差或替换成未经允许的类型。

仅有interface_kind=external不表示允许传条件；还须声明external_constraint。该声明的
required省略时为true；false允许不传，传入后仍检查。未声明要求的接口收到条件会被拒绝。
检查器支持某类型也不代表 Family 程序会读取它求解。按用途选择输入：public_parameters
承载 Family 明确声明类型、单位、来源和用途的计算与布局资料；external_constraints 对实际绑定输出接口作独立断言。
宿主 A/B、OD 可作为必要计算资料，但不自动成为输出接口的落轴或等径要求。由 Family 算出的点不同时充当自身正确性的独立证据。
standard_model 不接收调用方 external_constraints；内部名义目标可复用同一 solver，不能充当
self_geometry 条件通过。每项以 interface_key 绑定 Family 接口，再读取同次 Geometry receipt。
Backend 按以下量测语义复用原检查器，独立评价同次实际端口：

| 条件 | 独立量测语义 |
| --- | --- |
| finite_segment | start_m/end_m 构成整段允许求解范围；direction_relation 必填，OD 可选；原点到轴的垂距和沿轴 station 范围分别量测 |
| fixed_point | 接口原点到 point_m 的距离；direction_relation 必填，none 无 axis/reference_direction，same/opposed 要求 axis；OD 可选 |
| infinite_axis | point_m/axis 定义无限轴；direction_relation 必填，OD 可选；只量测到轴的垂距，不伪造有限范围 |
| same / opposed | 接口轴与指定轴同向/反向的角误差 |
| reference roll | 接口X轴与明确且垂直目标轴的参考方向比较 |
| circular OD | 比较实际圆形接口与目标外径；不新增外管内径匹配 |

finite_segment 的垂距和区间越界量分别与 position tolerance 比较，边界包含等号；这不是
“到有限线段的欧氏距离”合同。方向与roll不能由位置通过推定，profile相等也不能单独证明
孔道或真实外部接口存在。共用原位置、方向、profile 与可选 roll 容差，缺省为 1e-6；
reference_direction 仅在显式 same/opposed 下可选，须垂直目标轴。finite_segment 与 infinite_axis 的 none 仍检查位置轴。
两端点不能零长度，不提供额外 allowed subrange，也不要求真实管对象。
非管 fixed_point（如阳极附着或宿主 datum）不强制按圆形管道 OD 检查；不增加精细孔道、流体或制造门禁。
所有位置类型的 OD 仅在 Family 未声明 profile_required 且没有 connection_profile 时可省略。

external_constraints 同时是求解输入和实际绑定接口的断言。profile_required 是输入最低要求，
不会抹去本次已经提供的 OD；direction_relation=none 仅不检查方向。finite_segment 仍要求实际接口落轴，
只有已提供 OD 时再比较圆截面等径，不直接表达导体末端在主管外表面的 OD/2 关系。
自定义 interface_key 不会创建新条件类型或免检上下文。fixed_point 可表达无方向/截面要求的纯点检查，
该 none 分支没有轴字段。Provider 的封闭 literal 分支投影到同一 Domain 与几何评估器；不增加表面安装算法。
Preview 的 external_target_comparison 由已有实际条件检查派生：PASS／FAIL／NOT_COMPARED，不成为持久事实或新门禁。
空目标的 NOT_COMPARED 与内部 frame PASS 分开，均不单独证明实体接触或外部真实连接。

标准条件表达调用方安装事实或设计范围；source_kind 缺省 design_target，observed_interface 的
evidence_reference 仍仅记录来源说明，不自动解析或重新授权真实外部实体。
结果的 external_actual_connection_status 仍保持 NOT_CHECKED，接口比较不代行实际连接与 IFC 持久化。

## 同次结果与 exact Preview

Geometry实例、接口和receipt来自同次受监督构建。child与codec复用canonical serializer；
OCCT stream输出在缺少标准 `DBRep_DrawableShape` header时补齐，与filename writer的
canonical策略一致。接口或摘要相等不能代替实际canonical bytes核对。

原 Preview snapshot 增加可选 `ProductRealizationPreviewEvidence`：原合同摘要、正规化
invocation、policy、完整SOLVED结果和world geometry SHA。当前Family Preview要求该证据，
发布前校验绑定与几何摘要。exact resolver 逐次重新授权原Artifact，并校验capability、
snapshot与证据一致；不读取新CURRENT、不重求解、也不建立另一结果库。
SOLVED和INSPECTED结果都从逻辑字段`result.product_realization_preview_capability`返回可继续使用的
exact capability；调用方原样复制到inspect或Geometry Query。PNG内容与链接只用于查看，不能代替
该capability授权exact inspect/query。

Direct Geometry的datum放置、正规化sample参数回读与 `geometry_preview` Query target
见[共享Preview合同](geometry-preview-and-inspection.md)。同次world BREP由原codec实际
施加刚体变换；渲染、inspect和Query读取该exact几何。

## 持久化、入口与后续使用

本轮usage_instructions增量已实现、原生能力验收待完成，见系统基线§4.5。
正文由Family/Geometry作者通过原write随exact版本保存，info读取；解释真实参数、坐标、
接口身份/变化与调用检查，不替代以下机器Schema、同次receipt或独立finalizer。不把接口
位置写成脱离参数的另一事实，不把datum统一解释为GAS或外部接入点。

迁移20261001_0105删除固定ABI/runtime/budget列及合同代号枚举，保留业务归属、完整性和事务守卫。
旧不可变JSON和资产不换标签；旧实验环境离线保留，不增加第二套解释器、结果表或Repository。
需要继续使用的来源直接适配当前内容。Geometry仍核真实来源和字节格式，不因Family人工标签变化重注册。

Direct 保留有界JSON文本输入桥接；输出递归参数值投影为canonical `value_json`，
公开schema保持封闭。声明及Guide来自原DTO和 `tool_docs`，Provider压缩只改变表示。
具体预算和工具用法以本树实际注册声明为准。

Direct以静态MCP资源公开JSON文本内部的精确合同。`cad2bim://contracts/`下，
`ifc_product_family_write/definition_json`给出create/update定义动作Schema及示例；
`source_changes_json`给出原子源码修改的原DTO Schema，`program_api`给出纯数值程序
上下文说明及作者proposal Schema，canonical执行结果保持独立完整合同；
`ifc_product_realization_preview/self_geometry`和`standard_model`给出各自invocation_json Schema。
update仅提交变化并继承准确草稿未改内容；已完整读取且仍适用的Schema知识可复用，否则按需读取definition页。
`source_changes_json`仅在修改源码时读取和提交，不与场景文档合并。
单资源最多65,536字节，内容由现有模型及共享tool_docs生成，无需业务Session且不读取业务资产。
Schema不替代现有跨字段语义校验。

当前共用Provider注册器把固定同质坐标元组表示为带长度边界的typed items，防止客户端
丢弃prefixItems后把坐标误表示为字符串。Backend仍按原typed元组和长度合同校验。

B3.08/09后续接入应复用本轮正规化invocation、exact Preview证据、唯一Geometry serializer、
现有Operation/Writer/CURRENT/CAS和保存后读取链。写前一致性重建、产品持久化资格及失败
恢复尚须各自工程与业务门禁；不得从SOLVED、Preview或本合同文档推定已经实现。

实现和验收状态以当前阶段回执为准；既有Family执行证据不替代新的工程或原生作者验收。
