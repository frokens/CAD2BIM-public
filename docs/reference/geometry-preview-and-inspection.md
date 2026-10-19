# Geometry与Product Realization Preview、保存与观察

本文记录当前两个Preview入口的显示、显式保存及同结果观察合同。Geometry的正式资源、Family的exact版本与接口事实仍由各自owner管理；Preview不赋予IFC写入、外管修改或真实网络连接资格。Family数值求解、组件装配与实际接口见[自身几何合同](product-family-self-geometry.md)和[Authoring/Runtime合同](product-family-authoring-and-realization-runtime.md)。
当前草稿、案例与发布的共同语义见[生命周期合同](authoring-drafts-preview-and-publication.md)。工程实现与作者原生验收分开记录，作者实验仍暂停。

公开两个 Preview 都用 `mode=single|cases`，source 封闭选择 draft 或 published；Geometry 另有 `mode=inspection` 观察已保存结果。单例执行按 profile 返回实际图像，多案例通过 render_json 选择渲染，不默认给每例图片。`retain=false` 不新建 Artifact 或 Operation；`retain=true` 保存同次准确报告与成功实体，通过 operation_list/get 发现。公开字段是 retain，内部服务的 retain_for_inspection 不进入公开 Provider。

案例输入为 inline 命名项或 from_operation 与 add/replace/delete/reuse。from_operation继承输入和预期，清除原嵌套reuse；changes中的`{change_kind: reuse, case_key: ...}`明确选择该Operation保存结果，不必重传参数。未标记案例仍执行，不需要的案例可delete；变更输入或预期用完整replace。两种显式reuse均核对准确来源和执行输入，不匹配拒绝而不回退重建。来源快照、依赖与逐例执行事实由报告持有。技术中断可保存已完成部分和未运行项；FAILED Operation 仍能按 case_key 或 after_case_key/page_size 读取报告。成功实体的 inspection_ref 支持换视角、截面和 Query，不再求解或建模。单例与案例报告均不自动赋予发布或 IFC 资格。

Geometry显式reuse在本次参数拒绝处理之前核对保存案例；已完成的REJECTED可复用原PARAMETERS_INVALID事实并比较当前预期，不读取或生成模型，选择出图时为no_geometry。BUILT仍沿原保存资产校验与观察链；技术失败或未运行不作为业务结果复用。参数匹配保留JSON值类型，不将布尔值视为等值数字。

公开帮助分别说明`execution_status`（执行完成/技术错误/未运行）、领域`status`、`expectation_check`、`render_status`及`coverage_gaps`；`completion=interrupted`保留已有事实，不表示整批通过。`rendered_case_keys`按顺序对应实际返回图片。保存的single为`observation/not_checked`，可在cases对相同输入显式reuse并检查业务预期，不重新执行适用模型。完整字段与接续见`common/preview#preview_results`和`common/authoring`。

`checks_passed` 限于本批已请求的明确预期、实际执行、保存和跨案例检查。正确的全拒绝批可通过；不要求额外成功构造或发布代表。`coverage_gaps` 只说明未检查的观察或中断恢复缺口，未声明预期仍为 `not_checked`。渲染状态单独表达，数值检查通过不代表已成功出图或完整支持域已验收。

两个Preview的D均给出材料/连接核实的简短完成边界，`common/preview#geometry_inspection`承接完整方法；`#preview_profile`负责相机和显示。`interface_focus`范围容不下两个frame时扩大`vertical_extent_m`或省略该可选值自动取景；`preset_region/custom`的必填范围不能照此省略。已保存模型直接换视图，避免为了图片再次求解。

## 输入、来源与同次结果

`GeometryPreviewProfile`是共享Domain相机与叠加合同；Direct的`preview_profile_json`解析为该值。完整公开Schema与可解析示例由`src/CAD2BIM_mcp/direct/ifc/geometry/`、`src/CAD2BIM_mcp/direct/ifc/product_family/`及`src/CAD2BIM_mcp/tool_docs/preview.py`生成，不在本页复制第二份Schema。

### 标准连接的独立示意层

共享 GeometryPreviewProfile.connection_context 使用封闭的 mode=none／auto／pipes；省略/null 也合法。pipes.items 每项只有 start_m、end_m、outer_diameter_m，最多 64 项，复用原 straight-profile recipe 预算。显示输入位于求解输入外层，复用 display_context、straight-profile builder 与保存/观察路径，不另注册 Geometry 或复制产品。坐标属于结果坐标系、单位米，不再次按 invocation.length_unit 换算。

auto 只读取同次或已保存 Family external 端口标注、对应标准条件和 evaluation.satisfied；缺失或失败的检查跳过并给诊断。没有 Family 事实的普通 Geometry 显式 auto 返回不支持的渲染诊断，不猜安装目标。每项必须在条件中提供 OD；即使实际端口带 profile，缺少条件 OD 也不回填或猜管径。reference_direction 与实际 frame 的意义保持原检查合同。

- finite_segment／fixed_point／infinite_axis 统一：从实际端口 P 沿其向外 Z 轴 n 显示，终点为 `P + 3 × 条件 OD × n`。不按 same／opposed 再次翻转；direction_relation=none 时也使用实际向外轴，不额外要求与有限段共线。
- 有限段只规定接入点求解范围，自动示意不延伸到其端点，也不被其端点裁短。三倍 OD 只承担显示职责，即使超出有限段也不改变求解范围，不表示真实管长、裁管或安装。
- 无条件 OD、缺实际端口、缺检查或失败检查时跳过并返回 connection_context_diagnostics。pipes 按 Agent 给出的两点和 OD 原样显示区域，不吸附、不裁切；不需要 CAD 来源、内径、材料或完整外管数据。手动示意不参与或覆盖标准条件检查。

新执行省略/null 时不构造背景；已保存 whole/result 观察省略/null 时继承保存的背景字节。none 清除本次场景及背景，新的 pipes／auto 临时替换本次显示；这些观察不修改原 inspection 资产、不创建 Operation，也不重求解产品。retain 保存的 display_context 至多一个，字节与场景身份闭合，和产品／fragment 资产分开。

示意只在 whole/result 显示，fragment/component 观察不带背景；section 与 Query 只读取产品。不计入产品 BREP、组件/fragment、产品体积和 solid 数、端口检查或正式标准模型。安装检查仍来自标准输入及同次 Geometry receipt。渲染元数据返回本次实际生效的 pipes 与 connection_context_diagnostics，内部 recipe／存储摘要由 MCP 投影隐藏；渲染失败保持显示失败，不能改写产品执行或业务状态。显示增量验证见实施记录，标准端口及已完成的库替换范围见验收清单。

| 路径 | 几何和接口来源 | 当前显示行为 |
| --- | --- | --- |
| mapped Geometry | 已授权的exact GeometryResourceVersion canonical资产与manifest接口 | 用该固定资产形成同次scene；已保存的固定资源不因Preview改变 |
| modeling_function显式sample | 本次参数正规化、受控构建和实际sample接口 | 使用同次动态BREP渲染，按retain选择保存；本次响应回显正规化参数 |
| 已注册资源的预存PNG | exact资源保存的图片，没有本次sample几何 | 仅展示对应固定预览，不借它构造动态sample、其他视角或实体Query |
| Family solve | 本次完整数值Proposal、后端建模及唯一finalizer形成的world BREP、frame和接口 | 只投影同次已核事实并渲染；不可行或歧义结果不伪造图片 |
| geometry_inspection观察 | 已保存的whole/local sample或instance BREP、接口、来源和坐标关系 | 同引用整体/组件/片段观察、截面或再次渲染，不调用求解器/作者构建，不新建固化Operation |

两个入口可在本次构建渲染请求附加`reference_geometries`；预存PNG分支不能追加参考层。Family将其置于solve输入外层，不写入`invocation_json`。每项使用唯一`reference_key`、Catalog返回的exact `resource_version_alias`与显式`datum_transform`；最多8项、canonical源字节合计32MiB，仅接受mapped-model的`occt_brep_v1`或`indexed_triangle_mesh_v1`。参考资产先经Catalog/Scope授权、类型与大小预检，原始字节由Geometry资源/Storage持有。它们只作同次显示，不能改变主BREP、Family求解输入、receipt、主体测量、IFC资格或Query target。响应的参考列表返回安全exact Alias、格式和放置，不泄露内部ID或宿主路径。修改参考层需重新发起完整Preview。

Geometry mapped/function本次Preview可提供`datum_transform: RigidTransform3D`，把局部模型刚体放置到project-world，平移单位为米。Backend在受监督codec内变换并canonicalize同次BREP，实际接口沿同一变换投影；没有独立第二次接口求解。省略datum保持局部坐标。单例 sample_parameters_json 空对象合法；案例报告与已保存请求保留本次输入及执行事实，具体默认参数由资源合同正规化。修改datum或参数需重新发起Preview。

一次性Preview不返回可复用临时实例身份。Query可以针对固定mapped的`target_kind=geometry_resource_version`，也可用`target_kind=geometry_inspection`查询已保存的sample/Family实际结果；两者都走原GeometryQuery owner、Attempt、Code修复及预算。保存结果的私有计算副本可释放，原持久资产不删除。

所有渲染均走`GeometryRuntimePort.render → OcpCadQueryGeometryRuntime → render_mesh`。Host adapter把
原生OCP codec工作交给一个共享的私有`OcpGeometryCodecExecutor`；该执行器接受`normalize`、
`normalize_batch`、`reopen`、`render`和`build_display_context`封闭操作。`normalize`支持1—8项有序批处理，输出按
输入顺序和`payload_index`对应；其余操作保持各自固定的输入输出数量。每次操作启动一个新的受监督
child，不在Host线程内执行OCP。

`normalize_batch`是现有执行器的内部有界跨产品/fragment入口，不新增公开MCP工具。
一次最多32项，每帧最多8项；整批输入含帧最多32 MiB、输出含帧最多64 MiB，使用一个输入FD、
一个输出FD和原control/terminal通道。多帧在同一个受监督Codec进程处理，重型依赖首次导入后复用；
帧顺序、全局item index、输入SHA与每项原canonical metadata/输出SHA一起校验。
只有原纯Python `parse_mesh` 的输入验证失败可返回逐项`MESH_INPUT_INVALID`并继续；
进入原生几何后的异常、协议损坏或进程失败均终止批次。没有完整可信终态时不返回部分批次，
调用方保持未确认项为未完成；Host也不把部分解码结果写入lease验证缓存。
这个内部入口不改变旧`normalize_many`的一帧上限、单操作失败语义、预算或已有消费者。
Product Realization已有受控批量入口，具体对象所有权、输出需求及部分交付规则见
[Family Runtime](product-family-authoring-and-realization-runtime.md#5-runtime与实际接口检查)。
当前每次`normalize_batch`仍启动一个Codec进程；Product跨组处理不使Codec自动跨调用常驻。
两者的有界批量能力不等于工程全量吞吐或阶段业务验收已完成。

Product与Codec沿原supervisor可记录独立的有界FD计时：启动时间由Host记录，bootstrap时点早于
应用模块导入，child另记依赖导入、输入/源码准备、作者entry、实际造型、BREP输出、Codec打开/验证/摘要。
Host仍记录槽位等待、输入暂存、输出读取、收尾及采样资源峰值。父跨度与子跨度可能重叠，不能直接相加；
进程启动和初始化次数与逐项实际处理数分别报告。计时只作观测，不改变业务终态，缺失/损坏不补猜。

render child通过唯一codec读取本次或固定资源的canonical字节并校验payload SHA与content type。BREP不重写，
不比较重新计算的完整浮点摘要，也不以历史triangle count作为准入条件。摘要只计算原生bounds、
topology、面积与体积；含自由低维子项的混合BREP体积为null，纯实体组合才报告实体体积汇总。
BREP的triangle_count为null，旧资产中的显示计数允许保留，但不参与完整性
或几何等价判定。indexed mesh仍返回其固有三角数量。

只有显示或显式vertices()/triangles()/display_diagnostics()调查才生成网格。主几何和display context各生成一次，
多个panel复用结果。OCP面内三角提取保持location、反向面绕序和全局顶点offset，以
NbTriangles()/Triangle(i)访问索引；不把面间重复顶点当作源几何拓扑。当前显示仍使用
相对偏差0.001、角度0.1的原OCCT默认网格过程；第一参数不是1 mm绝对精度。
只在私有显示Copy调用`BRepTools.RemoveInternals_s(copy, False)`，保真实孔与保存BREP。
原生面通过Copy history及原方向准确映射到显示副本，不按面积/bbox猜匹配。标准IncrementalMesh
上下文保留同次有序pcurve，中间`CleanModel=False`仅保留诊断资料；代表几何的顶点及有向
三角索引与原CadQuery过程一致。whole按保存whole显示，fragment/component沿同一个
`InspectionGeometry.display_objects()`逐fragment显示；截面objects语义保持独立。
codec逐面比较三角面积与原生曲面面积；差异超过5%时，仅复制该面并清除其显示网格，
以相同相对挠度和0.5角度参数重试一次。5%仅为显示质量启发式，不是几何、Family或业务
验收阈值。初始及重试网格保留至选择完成：在安全候选中选择有限相对面积差较小者，
同值保留初始；重试仍超差或重试失败但初始安全时，面积差本身仍可返回诊断PNG。
普通Preview只对这些质量异常候选补共享边界检查；确认原生检查异常或选用离散边界真正穿越
则沿原render失败路径拒绝成图，保留已有BUILT/SOLVED和inspection_ref，不改业务INFEASIBLE。
诊断与处置分离，Query仍可返回当前失败事实；不存在自动heal、精度扫描或另一套网格器。
所有触发重试的面均在同次后端`render_metadata.display_quality_warnings`记录canonical SHA、
从零开始的面序号、原生及两次网格面积（平方米）、相对差、网格SHA、选择与重试错误；
外层exact version/scene保持原有权威。PNG同时显示质量警告，不能视为display PASS。
Direct公开`presentation.display_quality_warnings`按原顺序保留每条`code/message`，包括同值条目，
message给准确当前对象/selection、面号、曲面类型及实际初始/重试/选用摘要；不公开canonical SHA。
源/参考/背景归属在render request owner形成，Provider只透传安全message。参考对象须调查其
独立exact目标，背景没有合法独立target时明确限制，不用主模型引用冒充。
公开`presentation.panels`保留panel key、裁切状态及visible/hidden overlay IDs；实际overlay继续
使用完整业务类型，不复制一套删减的接口或约束结构。
面积更接近本身不能证明曲面正确，不应仅为消除此警告改写有效实体拓扑；端口、管腔和
壁面连续性仍须独立验证。无安全网格、非法坐标/索引、渲染失败和资源限制保持硬错误。
确认边界失败的下一步是相同保存目标/selection/coordinates调用Query `ctx.display_diagnostics()`，
结合本人源码调查有依据的构造修订，再用同业务输入复核；不建议原样重画。正常薄壁、窄面
或小面积不构成统一拒绝条件，原生no_issue_reported也不是严格正确证明。
正常面保持原有三角索引和绕序，无警告的旧结果序列化不新增空诊断字段。
重试只产生私有显示数据，不修改源BREP或共享面边拓扑，不在Authoring源码中预网格。
显示近似不改变BREP、接口或工程约束测量的精度。

`display_diagnostics()`复用当前Query内原保存体的同次选用网格、pcurve与检查缓存，不读取
private_geometry修改后的计算副本。显式调用检查所有受支持selected面，不依赖面积告警；当前
深查支持非周期Plane，其他曲面/周期域准确列为unsupported，资料/映射不完整为inconclusive。
返回对象与面覆盖、未检查原因、原生检查及限制、穿越线段对数量和当前坐标米的区域bbox；不
返回整套UV/三角数组或内部SHA。整数计数只在完整检查后给出，端点接触/相切/重合不冒充真正
穿越，未支持范围不记为零。查询诊断的当前网格不承诺等于历史PNG；完整API和最小例子唯一
归`cad2bim://contracts/ifc_geometry_query_python/program_api`。vertices/triangles在同次深入检查
确认失败后拒绝当前网格，缓存/原生资源在close统一释放，不新增磁盘缓存或模型存储。

注册产生的正式几何资产由原owner保存、重开并验证实际形体有效；一次性Preview的派生几何
只在同次执行中使用，显式保存结果由GeometryInspection owner管理，不因再观察反复上传。正式资源仍由Storage、Artifact、exact Version、
Scope/RLS与CAS负责持久身份；源文件完整性、真实几何有效性和业务连接条件分别核验。
固定Geometry版本的Query使用私有计算副本，源事实API仍读取原输入，不以前后网格指纹作身份。

当前显式保存沿既有`geometry_inspection` Operation与Artifact，不新增独立结果目录或公开发现工具；普通观察不记成新的固化。Geometry build/query、Product Realization和codec操作共用组合根
注入的一个`HostWorkerCapacity(max_concurrent_workers=1)`；同一执行链的Product child与codec
沿原Runtime端口复用一次lease，阶段之间不释放后重新争抢。codec墙钟使用当前调用剩余deadline，
同次后续操作不重置时间；独立codec调用仍受自身45秒执行边界限制。每次操作保留30秒CPU和
2 GiB RSS；整次调用按[统一Q/H/T/C合同](tool-usability-and-runtime-boundaries.md)分配预算。已开始的codec
操作触发超时或资源上限时返回终态资源限制失败，不建议以同一输入自动重试；调用取消会终止child，
每次调用退出都会清理文件描述符、capacity和私有scratch。Runtime关闭后拒绝新codec调用。Host上的
`asyncio.to_thread`只用于删除scratch，不再承载原生OCP计算。
BRep的per-face mesh连接关系不能视为原indexed mesh的共享顶点关系。

## 相机与预算

简式`view/width_px/height_px`输入保留，默认isometric、640×480。现代`presentation`包含1—4个唯一
panel及一个overlay请求，并在序列化时省略中性的简式view。显式非默认简式view不能与presentation并存。
一张PNG总宽高各为64—2048，每panel宽高不少于64；最多四panel不扩大16 MiB PNG预算。
双panel为左右布局，三或四panel为2×2，三个panel时第四格留空。

camera有四个封闭分支：

- `preset_fit`：按实际几何投影包络、画布比例和边距适配。
- `preset_region`：标准view加明确target与vertical_extent_m。
- `custom`：明确target、单位且正交的direction/up以及vertical_extent_m。
- `interface_focus`：同scene中一至两个overlay ID，side_x/side_y/axial/oblique，extent可显式指定。

direction从camera指向target，screen-right=`direction × up`，近深度轴为`-direction`。

| view | direction | up |
| --- | --- | --- |
| front / back | +Y / −Y | +Z |
| right / left | −X / +X | +Z |
| top / bottom | −Z / +Z | +Y |
| isometric | (−1,−1,−1)/√3 | (−1,−1,2)/√6 |

坐标必须有限，extent必须有限且为正。无任意业务坐标范围上限；超出有限浮点运算能力时明确失败。

显式interface_focus视野无法同时容纳所选接口时，共享相机解析返回请求错误，提示扩大
`vertical_extent_m`或省略该值自动取景。Geometry入口投影为`GEOMETRY_PREVIEW_REQUEST_INVALID`，
Family solve为`PREVIEW_REQUEST_INVALID`，均为`input_validation`且不建议原样自动重试；
该情况不表示exact几何损坏。
interface_focus使用第一接口的实际frame基准，以一或两原点中点为target，参考各原点附近最多32个实际
网格顶点确定局部视窗；它是显示启发式，不是BRep端面识别。显式extent必须容纳两原点，否则拒绝。
固定物理extent可用于跨参数比较，不会分别fit掩盖尺度变化。

实际输出为resolved panel/camera，含像素区域、world target/direction/up/right、横/纵物理extent及
`geometry_clipped`。裁切由实际几何投影顶点是否超出panel判断，不修改canonical几何。
历史持久PNG保留原bytes；新显示请求不承诺与历史renderer的PNG字节一致。

## 表面与接口显示

表面逐像素z-buffer遮挡，使用稳定光照，不绘制三角outline。真实边界、超过30度的锐边及轮廓线
在深度/可见表面归属验证后显示；同一可见面不因边线与像素中心采样差异而错误遮住自身锐边。
不根据融合后的mesh猜测组件身份或分配业务颜色。

`GeometryRenderScene`绑定exact geometry SHA与coordinate_space；当前Preview仅投影actual或明确临时接口，
接口显示项最多128项，标识唯一。Host构造状态、预测保留段及display context不属于当前普通Preview结果。
Geometry坐标空间为`geometry_coordinates`；Family为`project_world`，直接使用finalizer world facts，
不新增Preview变换链。marker ID及角色为：

| ID前缀 | 来源与角色 |
| --- | --- |
| model: | exact模型接口 |
| family: | Family internal或external接口 |
| temporary: | 仅Geometry图片的临时候选 |

marker显示实际原点、局部Z轴、红色X参考、可选圆/矩形/多边形轮廓及标签。角色颜色与图例区分来源。
标记作为解释层覆盖在表面之上，不用实体遮挡隐藏待检查frame；图像不证明接口标注有效或材料接触。
`clean`不绘制marker；`interfaces`可选择全部或恰好两ID的pair，显式pair显示双方标记与连接虚线。
camera focus与overlay focus独立。resolved `visible_overlay_ids`表示被选择且原点在viewport内，
`hidden_overlay_ids`表示未选或原点在viewport外；这两个字段不表示BRep遮挡或整个轮廓都在图内。

内部`GeometryRenderScene.source_path_overlays`可附带中立的来源折线说明层：每项有唯一
`source:`显示ID、来源标签及同一scene坐标空间中的完整有序米坐标点列。单条2…256点、最多16条、
总计不超过1024点；非有限坐标与重复ID拒绝。默认空且旧scene序列化不增加字段；非空点列进入
scene序列化与身份摘要，参与相机拟合，以浅蓝虚线绘在表面之上并标明`source path`。
此线不构成BRep边界、Host、Family参数、接口、连接或IFC关系。它是内部渲染输入，
不增加公开Preview请求中的动态来源路径参数，也不改变原Host straight-profile语义。

Geometry临时接口复用模型接口64项/32 KiB预算，None或省略表示本次没有临时值；
空数组明确为空，非空数组作为本次完整temporary集合。它不改version manifest、不覆盖model接口、不进入Family求解。
Family Provider与Application拒绝任何显式temporary_interfaces字段，包括null和空数组。

## 两个普通Preview入口与返回

`ifc_geometry_preview`的single/cases接受草稿或准确发布Geometry；`ifc_product_realization_preview`的single/cases接受草稿或准确发布Family及self_geometry/standard_model输入。单例和多例共享原求解/构建能力，出图与是否保存独立选择，完整字段见[草稿生命周期](authoring-drafts-preview-and-publication.md)与两入口合同。成功案保留实际数值、组件、fragment、接口和检查结论；业务拒绝和未执行不伪造图片。实际frame检查和图片仍不证明IFC安装或业务整体通过。

后端Preview结果中的PNG bytes、MIME、大小及SHA在同次返回前相互核对，16 MiB为PNG上限。公开DTO只投影安全字段，不暴露私有路径或内部摘要。MCP `content`先返回公开DTO的`TextContent` JSON，有图时返回同一PNG的`ImageContent`；模型引用使用inspection_ref，不使用宿主路径或退役preview资源URI。机器结果校验与引用投影仍沿当前DTO/Provider派生；Geometry与Family的`/result` Resource仅提供核心概念、字段导航及下一动作，不再发布完整输出Schema。通常直接阅读实际返回，有疑问才查结果资料；公共执行/预期/渲染状态与保存引用语义只在`common/preview#preview_results`解释，不要求递归阅读输出类型。输入Schema、作者程序必须构造的返回接口保持完整。已有inspection重新显示时只读取保存BREP并渲染，不要求原求解输入重跑；渲染失败而保存已成功时返回引用及独立诊断，不毁掉保存结果。

`ifc_geometry_query_python`对已保存的动态sample、放置后结果及Family构建使用准确inspection_ref，Inspection manifest保留来源、正规化输入、whole与local实体、接口及实例变换；不从PNG推断实体事实。保存操作与逐案输入可从`operation_list/get`找回，也可由inspection_ref反查。多案例负例只有原结果/诊断，不保存占位实体。已有结果观察不新建保存Operation，Query保留自身Attempt；均不推进IFC CURRENT。

正式Family标准模型沿现有Geometry Preview的`mode=family_standard_model`或Query的同名target读取，输入为准确family_version_alias。它只读取该正式版本保存的局部米单位whole BREP，复用原渲染、截面与私有副本查询能力；不重运行standard_model、不创建Inspection副本、不需要原作者私有Preview。原标准资产无接口/片段或历史参数记录时不伪造这些事实。新正式构造输入来自Family定义的standard_model.public_parameters。

## 时间、错误与证据边界

Preview/Query的当前预算以各Application/Direct执行策略及[运行边界](tool-usability-and-runtime-boundaries.md)为准。一次性路径覆盖Scope、同次构建/渲染及清理，显式保存还覆盖保存与回执；不能排除真实持久化成本。超时/断连不证明已受理保存失败，先按原Operation对账；Query限制沿原Attempt处理。案例批次共用一次调用预算，不按案例数无限扩大。

同次调用顺序释放和取得 Worker lease 时，已使用的等待与执行时间累计保留；后续 lease 的执行额度取自身上限和调用剩余量的较小值。等待、执行截止时间使用该实际额度，调用总截止时间及终态、清理预留不重置。

renderer的网格与PNG是显示近似，不能单凭图片认定中空、接触、实际管道连接或IFC写入通过。模型接口由同次真实几何事实投影，`presentation.display_quality_warnings`只报告显示质量，不改变源BREP和业务检查。历史阶段2图片及工程证据保留原验收范围；本页的当前普通Preview合同不使历史结果自动重验或变成阶段3全量逐件图片。阶段3按抽查粒度消费后端已有结论，只在代表性抽查或具体诊断时显式保留模型图。

Domain renderer、Geometry/Family Application、Direct Provider及工具文案各由原owner验证。一次性路径核零新增持久结果及清理；显式保存核同次实际实体、Scope、Operation恢复、准确引用及后续出图/Query不重建，渲染失败与构建保存失败分开。退役preview targets不恢复；多案例/草稿目标另做受影响验证。本页纠正过时说明，不把文档更新当作新运行测试通过。
