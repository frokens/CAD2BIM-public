# 模型接口、固定版本标注与Family坐标检查

本文记录同次模型接口、固定版本补标注与当前Family的唯一变换/检查职责。Family参数来源、
产品原点和外部point/line/free约束的精确合同见[自身几何](product-family-self-geometry.md)。
显示与exact回读见[共享Preview](geometry-preview-and-inspection.md)，当前工程/原生验收状态见
实施记录。接口注释与frame检查不证明实际端面或流道。
此前含旧Program接口和Host语义的原文保存在
历史归档，不再作为当前Family执行合同。

当前P0职责迁移
已在源码中落实Family纯数值解、Backend完整预检后执行Geometry及唯一finalizer检查；Family
作者不能实例化或读取实际模型接口。函数Geometry的独立参数接口入口及Backend唯一变换链保持原owner。
R1源码切片已接收；R3b2已把新Geometry写入的注册样例与正式资源分离。本页不代替0087数据库
迁移执行、工程资产或原生Agent验收证据。

接口独立求值已沿现有Runtime扩展；工程检查和原生业务核收仍分别记录。
Apply整批预检与严格提交以产品添加合同为准。

## 同一准确参数的接口权威

已实现、原生能力验收待完成的版本使用说明
解释接口身份、坐标/方向和参数变化；固定版本从info读其说明，函数准确接口取独立evaluate。
说明不替代define_interface或receipt。阳极主体基准、引线出口、末端与业务接入点按真实模型
区分；调压器说明实际两侧身份与向外轴关系，不把datum或同坐标点当网络端口。
固定接口与说明通过当前create/update草稿及按定义发布产生新版本，不能覆盖旧模型接口或旧证据；旧annotate_interfaces入口已退出。

固定入口为`main.py:build(ctx)`，返回一个shape；函数另有`interfaces.py:evaluate(ctx)`，返回None。
函数build不具有define_interface能力，接口入口及可达helper不能导入main或CadQuery/OCP。
Geometry Build和Family Runtime直接使用
同一个`infrastructure/geometry_runtime/build_context.py:GeometryBuildContext`，各自Worker监督、
依赖、预算和失败语义独立。`ctx.param(name)`读取本次参数，函数evaluate或固定创作build声明：

```python
ctx.define_interface(
    key="outlet",
    origin_m=(0.1, 0.0, 0.0),
    axis=(1.0, 0.0, 0.0),
    reference_direction=(0.0, 0.0, 1.0),
    profile={"kind": "circular", "diameter_m": 0.05},
)
```

`define_interface`的`key`、`origin_m`、`axis`、`reference_direction`和`profile`均为keyword-only；
位置参数调用会失败。完整关键字形状同时出现在Provider工具说明中。

接口frame的平移与旋转使用唯一`domain/geometry_frame.py:RigidTransform3D`，原ProductRealization
路径仍导出同一类型和转换/组合函数。axis为局部Z，reference_direction为局部X；两者必须有限、
单位且垂直，沿原有`1e-9`刚体合法性阈值验证，不自动正交化。该数值不是几何忠实度、IFC Precision
或制造精度。内部pair容差来自Family约束，外部容差来自显式条件且不得弱化Family上限。

模型接口Domain值为`GeometryModelInterface(interface_key, local_frame, profile)`，没有internal/external、
IFC Port、Scope或独立数据库身份。模型profile是局部轮廓，封闭分支为：

工程TEE的显式`shape.mode=near_zero`是一个有界资源语义：同次build的三个模型接口frame保留
原source连接datum的位置、向外轴和OD；主管实体沿0°直轴，实际端面中心、法向与圆环尺寸由
Geometry独立量测并受该资源的近零预算限制。因此该模式的`local_frame`不是测得的端面法向，
`frame_check_status=PASS`也只表示所声明连接frame关系通过，不能升级为实体端面贴合、流通或
外部安装证明。消费者可从`proposal.internal_parameters.shape.mode`识别这一模式（工程
场景摘要投影为`proposal_facts.internal_parameters.shape.mode`）；普通TEE直线/曲线接口的
原含义不因这个窄例外改变。具体准入与量测见TEE资源`usage.md`；Domain接口类型及全局
frame容差不变，也不增设第二套端口事实。

| kind | 字段及限制 |
| --- | --- |
| circular | `diameter_m`：有限正数，沿现有连接径预算不超过1000米 |
| rectangle | `width_m`、`height_m`：有限正数，各不超过1000米 |
| polygon | `points_m`：接口局部XY中的3—64个不同点，隐式闭合、简单非零面积外轮廓；不平移作者原点 |

每次接口求值最多64个唯一key，规范化接口JSON最多32768 UTF-8字节。polygon验证退化、自交、相触与
重叠边，用于可靠轮廓表达；不验证材料接触、内孔或流道。profile可省略，Geometry不调用
`define_interface`时得到空集合；Family引用的接口则必须实际存在于同次接口结果。
作者对需要连接或配合检查的几何，应在evaluate声明相应接口，坐标、方向与轮廓须对应其连接
几何；若使用名义连接datum，须说明其含义。空接口集合适用于无需接口检查的几何，例如纯装饰
细节；技术上允许省略，不表示承担连接职责的几何可以省略声明。

## 传递、持久化与实例

- 函数Geometry先在独立数值child输出`sample-NNN.interfaces.json`，再在Shape child
  输出`sample-NNN.brep`。数值child不加载CadQuery/OCP，不携带geometry_inputs或BREP；
  Host直接比较同一准确源码与正规化参数后关联两阶段结果。样例数按参数组计，Host核对
  成员闭包、canonical JSON、共享Domain预算与几何重开。evaluate技术失败不读重资产或build；
  fixed创作仍一次构建并收集build接口，其输出包含BREP和接口JSON。
- `GeometryFixedBuildResult.interfaces`绑定固定构建；每个`GeometryFunctionSampleExecution.interfaces`
  绑定其对应参数、几何和同次独立求值。临时manifest、Schema及摘要回显已退出运行返回。
- fixed非空接口由GeometryAuthoringService核对Runtime receipt后写入既有version manifest及对应
  MANIFEST asset；二者沿原manifest digest一致性链发布。旧版本无接口字段仍合法且不回写。
- function的接口规则权威为exact source bundle中的interfaces.py。验证样例接口在
  `GeometryFunctionValidationResult.sample_executions`中用于核验，不进入新runtime manifest、
  version summary中不复制第二套接口规则；后续显式参数Preview/Family预检复用相同入口。
- ProductRealizationInputService从已重新授权的exact mapped version manifest读取接口，经原私有
  Worker binding协议送达child；function则在独立数值Worker执行evaluate取得接口。
- Backend interface_evaluations以fragment_key绑定准确参数、binding与scale，不含BREP摘要或伪receipt；
  Family作者不能读取这些实际接口来生成Proposal。结果按下述唯一变换链解析。

同次构建接口没有新增表、migration、公开工具、ResourceTemplate、Repository、Operation种类或Interface资源。
源码Geometry写继续使用现有source-first Operation、Source SHA-CAS、Storage及原子版本发布。
已建模instance receipt另行绑定真实BREP；新函数版本同bundle保存两个入口、Schema和输入依赖。
旧immutable资产保全历史身份，不增加旧build接口的在线fallback。
当前Family只执行当前typed合同，不使用人工ABI代号选择执行分支；旧Family原字节保留，不增加旧解码求解路径。
两Worker的Seatbelt可信依赖包含共享frame/interface/context文件，并纳入发行包检查。

接口及输出bundle继续按原实际预算检查；已删除的临时manifest不再形成结果门槛。超限在Runtime adapter
投影为确定性`GeometryRuntimeValidationError`，Authoring保持一次尝试并保存
`GEOMETRY_AUTHORING_CODE_INVALID`及`BUILD_OUTPUT_BUDGET_EXCEEDED`诊断，不重复三次技术构建。
ProductRealization的接口输入/所有instance receipts继续占用既有control/result各1 MiB总预算；
单实例32 KiB不是可任意倍增的额外预算。没有新增独立接口总配额、扩大IPC或改变原失败类别。

## Family实例引用与唯一变换链

`ProductFamilyInterfaceContract.source`必须是
`{"source_kind":"model_interface","fragment_key":"body_shape","model_interface_key":"outlet"}`，
从该片段的同次instance receipt读取frame/profile。owner_component_key必须等于片段owner；
缺少实际模型key是结构失败。declared_frame、adapter_binding_key、program_frame及Program
proposal.interfaces字段均不属于当前合同，不能提供替代frame或旧空值字段。

Backend先校验整份数值Proposal，再按最终fragment建模；不存在Family作者instantiate或handle。
`scale=1`保留原canonical bytes/SHA；其他合法mapped尺度在同次实例生成中缩放shape，再
canonical序列化、重开和计算summary。receipt记录scale，默认1在receipt序列化中省略；
scale不会进入刚体矩阵，也不重复执行build来取得接口。

当前尺度分流合同：
`mapped_model`的系统尺度同时作用于固定几何、局部接口位置及profile长度，正统一尺度不改变
单位方向，再按刚体位姿旋转/平移。`modeling_function`不接受系统业务缩放；若Schema声明
尺度参数，函数须按相同造型逻辑生成接口，不能由后端假定所有接口都简单乘同一比例。
新proposal只对mapped开放可变`uniform_scale`，函数绑定固定1/省略；Preview派生结果和IFC
映射应消费同一尺度事实，IFC单位换算与后续apply验收另计。

`resolve_product_realization_world_facts`是Worker几何放置、Backend重算和typed结果的唯一组件/
接口变换核心。接口使用`T_world_component * T_component_fragment * F_scaled`，world事实返回
source及component-local frame。世界根为`T(product_origin)`，组件相对parent、片段相对owner；
局部mapped尺度不再次放大这条链上的平移。

## Frame关系、残差与失败候选

当前internal coincidence必须声明`InterfaceFrameRelation`：
必填`direction_relation=same|opposed`、`orientation_requirement=axis_only|full_frame`；可选
`profile_requirement=none|equal`和`expected_b_from_a`。外部要求使用独立
`InterfaceExternalCondition`表达位置、方向、roll、profile及容差，不能由Program改变。

目标为`T_world_B_target=T_world_A*expected_b_from_a`。缺省same用identity，opposed用
`diag(1,-1,-1)`；显式相对变换的Z须符合方向要求。axis_only忽略绕轴角，full_frame比较完整目标
姿态。内部容差由原constraint给出；外部条件按[自身几何合同](product-family-self-geometry.md)
独立计算，free位置不自动取消方向、roll或profile要求。当前没有缺省旧None关系解释分支。

中性evaluator返回原点距离、沿A的Z方向的有符号轴向残差、横向残差、方向角、可选完整姿态角、
可选profile比较和satisfied。残差均相对于上述目标，不叫实际端面间隙。equal时圆/矩形比较对应
长度，polygon允许同一轮廓循环起点及反向点序；没有运行实体接触、流道或标注正确性判断。
`profile_requirement`省略或为`none`时，Backend计算`profile_satisfied=None`，compact公开结果省略
该字段，Provider输出Schema也将其声明为可选。要求`equal`时返回Backend计算的布尔比较结果。
`profile_satisfied`只属于Backend诊断，Program不能以自报值覆盖它。

Backend从最终frame重算全部必需pair，按合同顺序生成observations，不信任Program伪报PASS或漏报。
结构合法且几何可渲染的新关系失败仍可返回`SOLVED`候选，同时`frame_check_status=FAIL`并保存
逐项诊断。Preview公开结果仅在FAIL输出`diagnostic_only=true`、`eligible_for_product_write=false`；
PASS不声称具备未来写入的全部条件。Preview期待检查与按定义发布分开，FAIL样例不构成额外发布资格；准确关联及按定义构建规则见[作者生命周期](authoring-drafts-preview-and-publication.md)。
结构非法、参数来源/receipt不闭合继续拒绝；旧合同在准入时拒绝。该链不修改IFC或CURRENT。
Worker业务finalizer生成检查结论；当前Runtime接收及部分Preview消费者仍通过integrity入口
核对请求、准确绑定和fragment/receipt关联。这里不再使用旧solve_digest名称描述当前Result。
本轮静态审计发现同次proposal正规化及部分integrity调用重复，待按
清理计划合并；
当前尚未完成去重，不能写成已经只有一次校验。历史snapshot原字节不改写，也不构成旧协议兼容承诺。

## 固定模型接口修订

当前旧annotate_interfaces分支及其专用Operation链已退出，不能继续按旧样例调用。
固定Geometry使用现有create/update草稿合同：从可见准确published版本创建草稿，
或编辑当前draft，在一次完整更新中修改interfaces，之后按定义发布新版本。
interfaces省略表示继承，显式空集合表示清空；函数Geometry不接受这份固定接口列表。

发布资产生成、源资产复用、准确依赖和失败原子性以[Geometry Authoring](geometry-resource-authoring.md)
及[作者生命周期](authoring-drafts-preview-and-publication.md)为准，不再承诺旧annotation路径的
“无构建复制所有资产”流程。旧不可变版本/Family绑定不被原地改写，需使用新接口时更新准确绑定。
原标注历史记录保留历史身份，不构成在线兼容资格；不新增Interface资源或第二发布事务。

## Provider与证据

Geometry与Family当前文案由`src/CAD2BIM_mcp/tool_docs/ifc_geometry.py`和
`ifc_product_family.py`拥有，机器Schema由实际Provider注册生成。
`tests/unit/mcp/test_direct_profile.py`检查当前注册集合与顺序，Provider合同测试直接验证公开Schema及使用规则。

测试入口：`test_geometry_interface.py`、`test_geometry_runtime_authoring.py`、
`test_product_realization_runtime.py`、两Authoring Service单测、现有Geometry Authoring PostgreSQL
纵向链、Provider与架构/wheel门禁。聚焦结果、失败修正和实际数据库执行记录见
当前实施证据。
本地Runtime/FastMCP/Schema测试不计作独立原生实验，历史技术证据不替代当前Golden业务验收。

R3b2的数据库迁移和原生验收均为pending；既有轮次证据不替代本批验收。
