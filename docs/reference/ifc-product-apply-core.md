# 单产品族批量添加 Core

本文记录当前代码的 Application Core 合同。入口是
`IfcProductsApplyService.apply(scope, command, idempotency_key=...)`，Direct 与 Backend
组合根复用同一能力。公开 `ifc_products_apply` 的 Direct、Runner 入口已接入本 Core；
输入、结果和原操作恢复见[公开合同](ifc-products-apply-public.md)。Operation
发现与读取投影原持久结果，不另建批量执行或恢复机制。

## 输入与准确来源

`ApplyIfcProductsCommand` 指定一个准确已发布 `family_version_id`、当前 Scope 可见的
`package_id` 和一个 `payload_name`。服务器将其解析为准确不可变 Parquet member，
持久记录 `package_id/member_id/payload_name/logical_key_fields`，并处理该 payload 全部行。
逻辑键必须是 `product_key`。六列合同如下；其他来源列可由输入 Package 保存，Core 只读取六列。

| 列 | Parquet 物理类型 | 当前含义 |
| --- | --- | --- |
| `product_key` | string | 本次完整 payload 中非空且唯一的产品键 |
| `origin_x_m` | float64 | 产品原点的 X 米制坐标 |
| `origin_y_m` | float64 | 产品原点的 Y 米制坐标 |
| `origin_z_m` | float64 | 产品原点的 Z 米制坐标 |
| `parameters_json` | string | JSON object，按准确 Family 的公开参数合同正规化 |
| `external_constraints_json` | string | JSON array，按当前外部条件合同解析；空条件使用 `[]` |

每行形成完整 `ProductRealizationInvocation`。坐标必须有限；参数、条件与支持域由现有
Family 输入服务验证。重复键涉及的每一行均记录输入错误。Core 不以来源业务状态筛行，
也不推导 CAD 布局、裁剪父管或补全缺失的来源条件。来源修订应发布新的准确输入 Package。

## 整批数值屏障与实例化

执行先固定原 Operation、准确模型 base 和原执行 claim，检查当前 IFC 中已有的 Body
子上下文，再准备一次准确 Family 程序与 Geometry 数值接口闭包。全部合法行先完成
数值求解、提案验证、组件世界变换与接口检查。普通输入错误、Family 正常非 SOLVED
结果、提案拒绝和接口 FAIL 逐行收集；任一普通失败使整批 `rejected`，提交数量为零。
这一屏障成立前不会 hydrate 重型 Geometry 资产或实例化任一产品。

屏障通过后，现有 Product Realization Runtime 使用同一数值提案、完整 typed invocation、
policy 和准确 Family 身份进行实例化。Runtime 检查二阶段绑定，实例化不会再次调用求解程序。
Numeric Host已完整验证的预检结果由Apply/Preview保留；Geometry Child检查跨进程收到的
准确请求关联、组件/fragment闭包及接口求值，不再重复执行不变提案的Schema准备和完整正规化。
实例化响应返回Host时，比较同一预检的全部业务字段及顺序，只允许
execution stage和新增receipt变化；新增receipt继续检查绑定、参数、尺度、summary及
原接口一致性。不再次完整正规化不变的提案，不增加信任标签或跳过实际结果检查。
函数 Geometry 的 `interfaces.py` 保持纯数值职责；`main.py` 仅在实际构造阶段运行。
独立接口合同见 [Geometry 模型接口](geometry-model-interfaces.md)。

技术异常停止受影响执行链。报告保留已经确认的前缀事实；Worker 仅给出观测范围时，
该范围为 `UNCONFIRMED`，后续未执行行保持 `NOT_EXECUTED`。缺失、额外或不合合同的
Worker 结果均属于技术错误。Core 不将技术异常改写为正常不可行，也不跳过异常行提交其余产品。

## 原生 IFC 表示与有界复用

目标格式为 `IFC4X3_ADD2`。Body 必须唯一，`ContextIdentifier=Body`、
`ContextType=Model`、`TargetView=MODEL_VIEW`，父 Context 为 Model/3D。
Body 缺失或不兼容在求解前返回 `rejected`，诊断指向现有
`ifc_model_configure_geometry_basis`。Core 不自动建立 Context。
Body早检复用同一准确revision已保存的实际Context与单位检查事实；读取前仍核对准确base及
实际存储字节。未保存事实时沿现有只读检查取得，不将内部派生事实缺失变成执行资格门禁；
事实存在但与准确ArtifactVersion错配则拒绝。详见[IFC Revision Runtime](ifc-revision-runtime.md#11-原生产品表示compiler)。

同一次Apply内，后台按准确Geometry、真实固定依赖、完整正规化建模参数、尺度和表示路线
索引源。产品键、fragment/binding别名和独立placement不作为几何身份；不舍入尺寸，不要求
Agent提供缓存字段。固定/函数、SYSTEM/Agent资源共用此机制。普通函数的全部参数均参与，
不能从参数名字猜测哪些无关。每产品仍独立求解、接口检查、生成当前receipt及IFC身份。

首次收到的源先核传输字节；同批后续引用可避免再次构造和传输，但只在独立Codec核验成功后
才能形成已检查结果。不同handle的相同已核字节只做一次Codec计算，再逐handle关联结果。
技术失败不成为成功源，不取消整次提交原子性。源缓存与IFC暂存共用本操作私有文件，
128项/16MiB内存缓存仅加速读取；淘汰后仍可从本操作spool取得。不同实例的构造记录不共享
可变嵌套数据。缓存不跨Apply，也不建立常驻缓存服务或第二套资源资格。

已核准确SYSTEM `curved_two_port`另外允许刚性主体复用：第一份真实参数仍调用原Geometry
源码构造；后续相同完整尺寸标量且实际端点/轴/径向匹配时，记录源到当前fragment的刚性变换。
当前接口不被首源接口替换；变换与原fragment局部frame组合，缩放后平移按相同尺度处理。
不满足匹配的输入使用完整准确参数构造，不切换表示路线。未知/Agent Geometry不启用该
专用投影，不新增一套BEND建模公式。

Geometry 与 Codec 完成后，Core 将准确规范 BREP 保存在本次私有有界 spool；已选参数
路线的同次构造记录以独立私有文件保存，实际字节与 BREP 一起计入 spool 和 IFC 块预算。释放上游
Worker 槽位，再把产品块送入同一个 IFC Worker 的同一个私有模型。数值分组与几何分组
当前均为每窗口最多1024条；Numeric和Modeling各自在同一准确闭包的串行进程内跨窗口执行，
窗口边界握手并保留逐项/整次预算与取消语义，不增加并行Worker。Modeling结束后才运行Codec。
IFC流块独立为32条或64MiB；输入16MiB和输出传输256MiB仍分别约束每窗口。
完整数值结果、输入 materialization
及私有规范几何 spool 各自受现有 256 MiB 上限约束；IFC source/candidate 与各 Worker
继续使用其原有预算。分组不改变整批失败语义和原执行 deadline。

Codec 沿现有受监督文件协议发送 frame header 与原不可变 payload 引用，直接写入私有
scratch；不先拼出整批输入 bytes。成功终态后从有界 regular output descriptor 逐 frame
解码，header 和 payload 分配前限制初始文件剩余字节，保留顺序、身份、SHA、长度、终态、
EOF 和读取前后 fstat 检查；全部核验后才返回或缓存，没有 partial 结果。
批量入口在完整输入预算校验后可读取既有调用域的已验证缓存，只有整批命中，且每次出现的
输出payload加各帧最大header及长度前缀的保守上界不超原输出预算，才直接返回；上界不满足
则交原Worker按实际输出判断，不提前拒绝可能有效的请求。
缓存仍限本调用的16项和原字节上限，不保留失败或跨调用结果。Codec child 在原8项帧内
复用完全相同请求的成功计算，键覆盖准确字节、格式、单位、变换、summary及raw需求。
每次出现仍检查传输字节、保持原序号并计入完整输出预算；帧结束释放复用结果。
部分命中不压缩 Host 请求列表，原技术失败索引及整批失效语义保持。
无 summary、无 transform 的 BREP 仍从实际字节检查非空和 BRepCheck 拓扑，复用
`user_geometry.py` 的唯一底层能力，避免加载该路径不需要的 CadQuery wrapper。
summary、显示网格和 transform 在实际消费函数加载各自依赖，必要 OCP 检查仍执行。
实际建模的 Product Worker 在安装只读审计前显式准备自身 CadQuery 依赖，覆盖函数
Geometry 与 mapped template 的建模消费；纯数值分支不加载它，不借 Codec 模块导入
隐式初始化建模环境。Geometry build/query Worker 继续各自已有的准备顺序。
按需导入可改变子阶段归属，不能以导入时段单项下降替代完整 Codec/Geometry 流程测量。

后台按准确 Geometry 版本 ID 为当前 17 个 SYSTEM 资源选择整项路线，命中时核对
SYSTEM 所有权；所有权不一致属于技术错误。已有资源加载器继续负责准确绑定、可见性和
资产字节，复制 lineage 仅用于离线核实本轮来源。未知版本及 Agent 创建的固定、函数资源
统一使用 TFS。同一资源的支持域内不按参数分支换路线，也不在编码异常后自动切换表示。
当前政策为 12 项 Parametric（包括 SERVICE_SADDLE）、REGULATOR/VALVE 两项固定资源 Advanced BREP、
GAS_METER 固定资源及 TEE/CROSS 三项 TFS。当前准确 ID 由
`system_geometry_ifc_routes.py`拥有；原选择与质量证据见表示实现记录，
当前源码的正式发布/绑定与实际全量范围见最新核收记录。
没有注册期自动检测、资格凭证或后台实现指纹门禁。
SERVICE_SADDLE当前准确资源统一沿其实际圆柱、多边形回转、布尔及缩放构造记录编码；
不另写族公式，不按角度分支换路。路线切换只改变IFC表示，完整Geometry、Codec和接口
职责保留；必要边界、小批保存回读及性能限制见优化过程记录。

Parametric 消费唯一 `main.py` 在现有受控 ModelingArena 中实际构造时生成的
`GeometryConstruction`：操作依赖、实际参数、Workplane 平面和必要形体快照经根剪枝后
脱离 live handle。纯数值阶段和 TFS/Advanced 路线不生成该记录。编码器从同次实际构造
投影受支持的 profile、拉伸、回转、扫掠、基本体、布尔与刚体变换，映射源为
`Body/SolidModel`；不按 Family 名称另写建模公式。未支持操作明确停止。
形体快照仅供实际读取者：保留box/fillet/chamfer结果、Workplane准确pending profiles及
回转/扫掠/loft的native shape参数引用；不额外序列化无人读取的最终root、拉伸/回转结果、
Boolean中间体或重复profile BREP。完整canonical BREP仍独立输出并由Codec检查，
构造图、资产完整性、盒体/截面及保存回读检查不因减少私有数据而取消。
完整最终BREP不是参数实体encoder的几何输入；它仍由当前作者形体查询、返回值合同、
独立Codec及准确source身份消费。输入正规化、求解与命名接口检查不依赖它。当前保存回读
检查IFC结构、关系、位置和表示，不逐产品重新生成native实体，不能用它替代实体有效性检查。
声明允许的原生属性getter与方法共用受控native调用及construction记录边界；getter产生的
Vector等handle具有同次调用内的准确producer，原生异常仍不可由作者捕获后继续执行。
构造记录由唯一父 DTO 接收原始节点并验证有限 JSON 值和图闭包，不先逐节点重复建 DTO。
私有记录通过实际 UTF-8 JSON 出入站：有限值、非法字符、重复节点、缺失引用和环仍被拒绝。
不为校验生成无人消费的 JCS 文本，也不添加 JavaScript 安全整数范围限制；实际整数按
当前 Python/JSON 合同准确传递。此处不改变资产身份或传输字节完整性检查。
回转实体直接表达同次实际角度，按项目 `PLANEANGLEUNIT` 的 RADIAN 及 SI 前缀换算；
原全周回转后双半空间裁切的小角度分支已退出。当前 Body 预检要求合法的 RADIAN 单位。
正交盒体从实际面、角点和 Location 识别为 `IfcBlock`，保留拓扑及几何判定；
面顶点、法向和 BoundingBox 只在同次编码中复用，不改变源几何。
参数编码的子拓扑枚举使用 OCCT 的同一有序去重索引表，以 `FindKey` 按索引取值，
避免其 Python 隐式迭代开销；边的退化过滤、内环排除规则和有向线环遍历语义保留。
这不减少盒体合法性、角点、法向或面积检查，也不改变构造图及 IFC 实体表达。
参数编码器直接使用 OCP/TopoDS 读取准确构造观察与投影 IFC，不加载 CadQuery 的
Sketch、Assembly、导入导出或 VTK Python 模块。原剖面闭合/共面检查、ShapeFix
方向修复、面法线、包围盒及截面拼线容差均保留；Geometry 建模器仍是唯一构造权威。
可选 Worker 观测将 `geometry_native_dependencies` 与 `ifc_parametric_dependencies`
分别记录为既有几何/IFC 阶段内的依赖加载子阶段，统计时不重复累加。

Advanced 从原准确规范 BREP 投影每个 Solid 的实际解析面、支持曲线、拓扑和 Location。
每个 Solid 在同一候选模型内建立 `AdvancedBrep` 内层 map，外层复用已有源 map。
内部坐标、长度及位置向项目单位换算，方向、参数和权重保持各自含义；内层映射 scale 为 1。
当前拒绝有理或周期 spline、退化边、多 shell Solid 和 Compound 中非 Solid 叶子，
不跳过不支持的边、面或子体。

TFS 路线调用 IfcOpenShell 原生 `geom.tesselate`，线性 deflection 为米制 0.001。
原生 `SurfaceModel/IfcFaceBasedSurfaceModel` 只作为内部三角网格输入，最终映射源为
`Body/Tessellation/IfcTriangulatedFaceSet`。三种路线的产品组件 Body 均通过
`MappedRepresentation/IfcMappedItem` 引用源，不保存最终 FBSM 或异常 fallback。
source_key 包含准确 Geometry 版本、路线和实际规范形体身份；同一源在该 IFC Worker 内
只编码一次，RepresentationMap 跨产品及流块复用。同键换路线或换字节明确拒绝。

每个原生 `IfcConnectedFaceSet` 独立编码为一份 TFS。索引按原点的 wrapped 实体身份
建立，从 1 开始，不依赖尚未入文件的 STEP 编号；同坐标不同实体不焊接，多个 shell
不合并。原 FaceBound 方向、三角形顺序、重复和退化三角形均保留，不简化网格或补三角。
`Closed`、`Normals` 与 `PnIndex` 保持未设置；TFS 表面不自动证明闭合实体或精确碰撞。
非三角 loop、多 bound、空 shell、非三维或非有限点及换算溢出明确报技术错误，沿原
首错停止和零提交链传播。全部原生 shell 检查后才向候选模型建立 TFS。

固定 Geometry 复用原规范 BREP，receipt 的 uniform scale 在映射中应用一次；函数
Geometry 已按实际参数生成规范字节，映射 scale 为 1。米制坐标向项目单位换算、组件
位姿和 fragment 相对位姿各按所属层应用一次。保存后重新打开 candidate，核对实际
产品/组件 GUID、Body、映射引用、缩放、组件世界位姿及聚合父子关系；各映射源还核实际
所选路线的源表示类型与 items、实际映射来源及内外层位姿/scale 是否保持；TFS 另核
原生 shell 数、点表和三角索引。保存结构检查不替代精确曲面的形体质量验证。
集合 root 不重复附加其子组件 Body。

`representation_map_build` 是包含所选编码、单位换算与 map 创建的累计时长。
路线分别记录 `ifc_native_tesselate`、`ifc_parametric_encode`、`ifc_advanced_brep_encode`；
装配时长包含 map 构建，不能将嵌套
时段相加作为总耗时。读取器输出三角数可与原始实体三角数不同；保存网格保持及读取后
形体一致分别由同源的精确结构与世界坐标有向三角形比较证明，不能只比数量。
`triangle_count` 只计实际 TFS 三角形，不能解释为 Parametric/Advanced 的全模型 reader
三角数。性能记录中 CPU 为各受监督子进程终态累计 CPU 之和，RSS 为单个子进程峰值的
最大值；阶段 wall 区间、整机资源和并发总内存分别解释。小批及全量预测的准确边界见本轮记录。

工程集合按原通过证据复用、原失败修复与未执行补验完成；工程验证与实际全量测量分别保留准确证据。
质量核验分开保存结构、原生网格、完整有向解析边界和实体集合：闭合曲面完整面域及修剪
边界对应用于已复现的近重合布尔比较器异常，保留原 Cut/Common 失败；不能只以有效性、
体积、包围盒或少量采样判几何等价。含重叠 Solid 的 Compound 直接切差不等于材料集合差，
本轮 GAS 同序六实体及两侧 union 单独核对，未删去任一子体。
源拓扑 tolerance 导致的 BoundingBox 计算包络另行说明，不扩大为几何误差容限。

## 四表报告与唯一提交

报告是新建 Data Package，含 manifest、description 和四张 Parquet 表：

| Payload | 逻辑键 | 事实 |
| --- | --- | --- |
| `products` | `input_row_index` | 每一输入行、产品键、求解/构造/提交状态、实际 root GUID、正规化参数、solution 和错误阶段 |
| `components` | `product_key, component_key` | 已验证组件结构、IFC class、世界变换、实际 component GUID 和提交状态 |
| `interfaces` | `product_key, interface_key` | 同次求解的命名接口来源、组件/fragment、世界原点/方向、profile、提交状态及 `component_global_id` |
| `checks` | `check_index` | 输入、求解、接口、执行覆盖及提交诊断，含残差、容差、错误来源与下一动作 |

`interfaces.component_global_id` 从本次 Compiler 的 `(product_key, component_key)` 绑定取得，
只有确认提交时才填写；失败或 CAS 冲突报告为 null。接口的数值观察可以保留，但不会因此
取得当前实体端面或外部连接已验证的含义。后续编辑不改变原报告的坐标和组件身份。

`ProductApplySummary` 将完整输入分母、确认求解、未确认求解、已求解、完成接口约束求值的产品数、
确认构造、未确认构造、已构造、已提交及未执行数量分开。成功必须全部输入已构造且
已提交；任何失败结果的 `committed_count=0`。实际阶段 elapsed 值单独记录，不能以
嵌套计时求和替代总耗时或进程 CPU。

成功报告字节、失败/CAS 报告字节和 IFC candidate 在提交前写入私有 Storage，并完成
实际字节校验。唯一 `IfcRevisionWriter` 沿原 IFC Repository 事务提交：IFC revision、
ArtifactVersion、CURRENT CAS、Operation 终态和报告 Package/Catalog 同时可见。
报告注册复用同一数据库事务，不再调用另一发布事务。正常失败与确定 CAS 失败只发布
原 Operation 的失败报告，全部 GUID 为 NULL、提交状态为 `NOT_COMMITTED`。

候选容量超过实际限制时，既有 Runtime terminal 返回
`IFC_REVISION_CANDIDATE_TOO_LARGE`、`candidate_save` 和
`actual_bytes/limit_bytes`，Core 原样保留至失败 checks。该技术容量错误不可自动重试。

报告暂存或登记本身失败时，不宣称已经存在可读 Package。无法确定 commit settlement
时冻结原 Operation 并返回技术错误，保留原 identity 供对账；不得创建替代 Operation。
Scope、鉴权及 RLS 复用当前 Data/IFC 权威边界。

## 重放、恢复与读取

`idempotency_key` 与原请求摘要用于原操作身份。原准确输入 member 已在 Operation detail
固定。重复调用先恢复该 Operation；已选 candidate 使用原准确 candidate 与报告，
无须重新求解、构造或另写候选。响应丢失、进程重启和 CURRENT 后续变化均不改变原结果来源。

正常同次提交后的检查与成功重放均实际核对原exact IFC bytes。正常同次提交已由compiler
独立重开候选且正式字节一致时，Writer复用该inspection；恢复或成功重放缺少同次事实时，
仍按现有只读路径独立解析。Scope、CAS、exact身份及真实字节检查保持不变。回读损坏或不可读
时，Writer 冻结该原操作并抛出 `IfcCommittedVerificationError`，错误码为
`IFC_COMMITTED_READBACK_FAILED`，携带原 Operation 与 revision。已完成的 SUCCEEDED
记录与报告保持原事实，不将回读错误改写为 FAILED 或用 metadata 恢复掩盖。恢复准确
owned 字节后可沿同一原 identity 重新核验。

现有 Direct/Runner Operation 投影共用当前 DTO。读取时校验原输入 Package、member、
payload 和逻辑键，返回本 Scope 可见的准确 Family 引用、输入引用、成功/失败报告及
原 summary。原 `operation_ref` 指向原提交来源。现有 IFC Query 的 `CURRENT` 仍按
当前模型固定，不提供历史 revision 查询。缺少当前 request/input binding 的退役
Apply detail 不进入当前发现与读取链。

## 实现与验证所有者

- Domain：`domain/product_apply.py`、`domain/ifc_product_compilation.py`、
  `domain/ifc_geometry_construction.py` 和现有
  `domain/data_runtime.py` 的 Apply Operation/report 合同。
- Application：`application/product_apply_service.py`、`application/product_apply_report.py`、
  `application/system_geometry_ifc_routes.py`、现有 `product_realization_input_service.py`
  与 `ifc_revision_writer.py`；共享组合根为
  `ifc_application_resources.py`。
- Infrastructure：原 Product/Geometry/Codec Runtime、IFC Worker 内
  `infrastructure/ifc/product_compilation.py` 及其 Parametric/Advanced 编码 helper、
  原 PostgreSQL Repository/ORM/Storage。
- 输入/屏障/二阶段绑定/技术覆盖：`tests/unit/backend/test_product_apply_service.py`、
  `test_geometry_interfaces_runtime.py` 及现有 Product Realization owner 测试。
- 原生转换/保存核验：`tests/unit/backend/test_ifc_product_compilation.py` 与现有
  `test_ifc_revision_runtime.py`，含点实体身份与 detached 编号、面方向、多个原生 shell、
  重复/退化三角形、非法网格拒绝，以及中空形体、open-face、multi-solid、单位/尺度/旋转
  与保存网格等价；实际产品块 required-null、同 source 无字节复用与技术首错沿现 owner 验证。
- Codec 传输/字节校验/依赖消费者：现有 `test_geometry_codec_execution.py` 与
  `test_geometry_runtime.py`，覆盖有界多帧、文件变化、完整核验前不缓存、共享拓扑检查、
  无 summary 的实际依赖，以及正常 summary/render/transform 消费路径。
- 报告原子可见、rollback、CAS、RLS、重启/响应丢失与回读退修：原 Writer owner
  `test_ifc_revision_writer.py`、IFC PostgreSQL 集成 owner 和 Core 真实代表。

本合同证明产品自身添加、保存与报告读取能力。具体工程输入的来源完整性、串联布局、
Host 编辑、持久网络连接及整体安装由各自任务与验收证据确定。
