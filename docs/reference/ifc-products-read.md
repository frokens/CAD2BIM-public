# IFC 产品读取合同

本页记录当前 `ifc_products_read` 的已实现公开边界与共享调用链。工具用于读取现状和准确
历史关联，服务后续产品及语义／关系维护；不实现这些写操作。B3.10已完成主控独立核收，
精确接收、工程证据复用、两模型必要闭环及后续边界见[第6节](#6-阶段核收证据与后续边界)。

## 1. 职责

公开 READ 工具 `ifc_products_read` 使 Agent 不编写查询程序即可定位产品，按需
读取当前属性、材料、空间／系统分配和实际连接，并沿指定原 Apply 找到准确输入及结果。
当前读取也为后续产品修改、删除及语义／关系维护提供准确对象和现状，不能仅以
添加报告查询说明工具价值；外部导入产品也能读取当前事实，原 Apply 不是必需来源。
不增加产品来源表、结果存储、独立 Reader／Repository／Worker、注册资格或实现指纹。

| 能力 | 本期分工 |
| --- | --- |
| `ifc_products_read` | 小批产品键或 GUID 的定位、准确关联、历史诊断／提交映射，以及后续操作所需的按需当前语义 |
| `operation_list/get` | 原动作发现、状态、原请求绑定、提交元数据与恢复导航 |
| Data 读取与查询 | 原输入／报告枚举、分页、参数／solution／历史接口／checks 明细及显式 Join；不代替当前 IFC 语义 |
| `ifc_query_python` | 自定义 CURRENT IFC 调查、完整关系遍历与显式绑定数据的只读联查 |
| B3.11 `data_derive` | 查询结果与选择集合的不可变 Package 发布；[已实现并核收，范围见稳定合同](data-derive.md) |
| 后续模型修改／关系工具 | B3.12有限编辑/删除设计承接Family替换、整体位姿及安全删除；属性/材料、分配/连接留后续。本READ合同仅读取，不表示这些写操作已实现 |

READ 不运行 Family 求解或 Geometry 构建，不恢复 Apply，不创建写 Operation、Package、
QueryAttempt 或 IFC revision，不推进 CURRENT。历史 Family 接口不证明真实端口；实际端口、
连接和系统归属各按当前 IFC 关系读取，连接存在也不能直接证明运行流向或网络可达性。

## 2. 已核实现与复用映射

业务事实分别由下列现有 owner 持有，新增投影不能成为另一份可编辑权威。

| 新职责 | 现有权威与处理方式 | 实际消费者及必要增补 |
| --- | --- | --- |
| 原 Apply 身份、状态、输入与提交绑定 | `DataRuntimeService.get_operation_by_locator`、现有 Repository 与 Operation 引用；直接复用 | 读取 Service 授权原操作；不通过别名猜原动作 |
| 原报告与输入字节 | `ExactPayloadResolver.payload_from_exact_package/materialize_members`、Artifact／Storage；直接复用 | 对 Operation 已授权的准确 Package/member 做有界表投影 |
| 报告键与 GUID | `product_apply_report.py` 的四表及原 Operation；直接消费 | 按准确键匹配，不再发布一份产品映射 |
| CURRENT 固定与完整性 | `IfcRevisionReader.pin_current/open_exact` 与现有 `IfcRevisionWriter.read_projection` 读链；直接复用 | 当前读先固定一次 exact，再交受控 Runtime |
| 当前产品投影 | `InspectExactIfcRevisionRequest`／Revision Runtime inspection；沿正式扩展点新增 `products` 投影 | 同一次模型解析中完成既有 inspection 和所选基础／语义组提取；具体关系读取留在既有 IFC infrastructure |
| 输入组合与结果语义 | Application层入口中性的 `IfcProductsReadService` | 组合上述权威，不持有发布事务或保存第二份结果 |
| Direct／Runner | 现有 products DTO／入口、`internal_ifc_api`、Runner IFC 注册、引用投影与 READ 调度 | 两入口确定性映射同一业务请求；Runner 无 Direct 字段 |
| 装配 | `ifc_application_resources.py`、`data_application_resources.py` 及现有 Direct／Backend 组合根 | 注入既有实例，不在 Service 内隐藏构造 Repository／Data Runtime |

现有自由 IFC Query 先 `read_exact` inspection，再进入 Query Worker。固定产品读取不包装
自由 Query，不继承其双进程解析，也不借此修改自由 Query 的安全合同。products与既有
model／contexts／spatial／host_facts／body inspection在相同Runtime单次解析中投影。现有位姿、
空间关系与单位机制按职责复用：位姿 helper 的模型单位须在公开投影中统一换米；空间移动
闭包不能直接当产品删除规则。属性、材料、系统和连接可由现有自由 Query 调查，但不因此
视为已有固定公开投影。直接扩展现有读链，不另做 Reader 归属重构或通用投影框架。

## 3. 公开输入与批量边界

两个完整、封闭的 literal 分支，`mode` 必填，字段不能混用：

| 分支 | 必填 | 可选与默认 | 读取对象 |
| --- | --- | --- | --- |
| `from_apply` | `operation_ref`、非空 `product_keys` | `view=applied`，可选 `current` | 原报告中的产品；current 对整批成功提交的所选根对象再读当前基础事实与请求语义 |
| `from_ifc` | 非空 `global_ids` | `operation_ref` | 当前 IFC 所选对象的基础事实与请求语义；给定原操作时仅在其准确报告内反查 |

### 3.1 按需选择与组内名称发现

当前读取采用“默认基础事实＋按需语义组”，业务内容见第4节。时间视角 `view` 与读取内容
是不同维度：`applied` 不接受当前语义选择，也不会把历史参数或接口转换成当前事实。
当前请求表达“目标产品＋所需信息组＋组内必要筛选”；Agent在公开业务选项中选择，不要求
自行编写 IFC 查询源码或知道内部属性路径。一次可组合多个乃至全部受支持的信息组，由同一次
固定快照与模型解析完成；不提供无界递归的“所有信息”开关，也不强制先摘要再逐组调用。

不知道模型内实际属性名称时，同一工具须支持选中产品的组内名称发现／简要目录，再按返回的
实际名称读取值。以属性组为例，提供实际 Pset／QTO 名称、属性／数量名及实例／Type 来源所需
定位信息；材料成员、端口等同样使用实际返回的名称或身份，不让Agent猜测。已知名称时直接
取值，不先重复发现。名称可能重复，须结合所属集合及来源区分，不能把显示名称当唯一写入ID。
名称发现只服务选中产品和请求组，不是全模型目录、原报告目录或新的Catalog，不增加cursor。
集合超限沿第3.2节处理，不能以只给前若干名称冒充完整目录。

Domain和公开DTO采用如下封闭类型。
`groups`只属于`from_ifc`与`from_apply/view=current`；默认省略全部组。

| `groups`字段 | 合法选择及默认 | 精确取值条件 |
| --- | --- | --- |
| `structure` | `{"mode":"direct"}` | 仅所选对象的直接构成／依赖 |
| `properties` | `{"mode":"names","sources":["instance","type"],"set_kinds":["pset","qto"]}` | `mode=values`带`selectors`；每项为`source/set_kind/set_name/property_names` |
| `materials` | `{"mode":"names","sources":["instance","type"]}` | `mode=values`带`selectors`；每项为`source/set_name/member_names` |
| `spatial` | `{"mode":"direct"}` | 实际包含／引用与必要空间上级 |
| `systems` | `{"mode":"direct"}` | 实际组／系统分配与直接服务关系 |
| `ports` | `{"mode":"names"}` | `mode=values`带实际端口`global_ids`，限所选产品直接拥有的端口 |
| `representations` | `{"mode":"summary"}` | 表示及Context摘要，不输出几何实体 |

属性和材料的`names`可缩小上述来源；属性另可缩小Pset／QTO种类。`values`各selector均完整
指定来源与真实集合名称，名称区分大小写；省略的来源／属性不提取。名称可能不唯一，返回
歧义不得任意选第一项。`selector_name`保留完整实际名称，可供下次读取；展示文本另有明确
截断标志，不能复制截断文本当selector。未命名或不支持结构明确报告，必要时转既有自由Query。
组内“名称／摘要”和“按名称取值”按真实差异建明确分支，保留来源与单位约束；不按语义组
另建工具，不用任意JSON、通用IFC路径语言或可执行表达式替代合同。

Direct 另带 `direct_session_alias`；Runner 的 Scope 来自受保护凭证。引用复制正式返回值，
不要求 Agent 提交内部 model/revision/member ID、摘要、预算或预计算 GUID。

Runner业务请求示意；引用和键须复制实际结果，示意占位符不可直接执行：

```json
{"mode":"from_apply","operation_ref":"<原操作引用>","product_keys":["<产品键>"],"view":"applied"}
```

```json
{"mode":"from_ifc","global_ids":["<实际产品或子件GUID>"],"operation_ref":"<可选的原操作引用>"}
```

### 3.2 集合、展开与读取成本边界

工具仅作有界选择，不新增报告目录或 cursor 分支；原输入／报告枚举及稳定分页复用 Data。选择键必须
非空且无重复；GUID语法沿当前类型约束。DTO现固定每次1—32个产品选择、各返回集合至多
128项、完整typed响应至多256 KiB；selectors及名称／端口选择数组同样有128项边界。
这是独立限额，不保证32个任意复杂产品请求七组一定装得下响应；产品选择上限与Operation
恢复发现的分页独立。返回在成员／组构建期间累计检查，并在Runtime、Core组合
和最终公开操作导航后校验准确序列化字节，超限不返回部分集合。展示文本至多2048字符，
带truncated／original_length；精确名称与合法原product_key不套用此展示上限。
需要完整展开时沿既有IFC Query，持久选择由B3.11接续；不静默返回半套关系冒充完整。
不为此新增全局 budget profile，不静默截断结果或要求 Agent 填写后台预算。
选择同一对象的全部受支持信息组仍受各组边界约束，不自动读取整棵子件树、系统的其他成员、
同材料的全部产品或对端的后续网络。需要更多子件时先取得结构中的准确身份，再明确选择；
完整大子树及全网任意遍历使用既有 IFC Query。

小选择／小返回不代表原报告小。读取调用须有统一期限；报告member物化和表扫描沿现有
`ExactPayloadResolver`／Data读取owner的预算检查，明确传递适用的`check_budget`及输入字节边界，
不能因`max_total_bytes`可省略就留下无界物化。当前报告所需member合计限256 MiB、累计扫描
至多5,000,000行、物化／扫描期限30秒，调用总期限沿既有`IFC_REVISION_EXECUTION`；按需
选择列，一批复用同表读取。代表测量及负例见工程与验收归档，预算在所属层覆盖，不逐项重开整链。

## 4. 返回及准确匹配

返回分为所选 view／请求项、原操作关联、逐项结果。复用已有公开类型中确实相同的
Family 引用、提交摘要和业务值；不得直接返回后台 Operation／Runtime／第三方对象。

| 内容 | 公开用途与边界 |
| --- | --- |
| 原操作关联 | 保留 `operation_ref`、真实原状态、准确 Family 引用及适用的原 `committed_revision`；不重复复制整个 Operation 包络与摘要 |
| 输入定位 | payload、`logical_key_fields`、准确产品键；Package alias 仅在确认当前 Scope 的同一准确包可消费时提供 |
| 报告导航 | products／components／interfaces／checks 的既有 Data 读取入口及可用性；不在每次结果中展开全部 JSON |
| `from_apply` 每项 | 请求产品键、报告行匹配状态／数量、唯一匹配时的 `input_row_index`、solve/build/commit 状态；仅原操作整批确认成功时返回持久根 GUID，报告行命中不等于已提交映射 |
| `from_ifc` 每项 | 请求 GUID、定位状态和当前基础事实；原操作整批成功且匹配时给产品键、根、component key／parent key 和角色 |
| 默认当前基础事实 | 本次固定快照中的存在、实际 IFC class、适用的 Name、米制 project-world placement；保留直接 aggregate 父及歧义状态，`direct_child_count`为aggregate关系中的直接子件计数；nesting及其他依赖请求structure，不将此计数当完整子树大小 |

IFC产品关联的是共享关系图，Type、材料、系统和端口对端不构成产品独占的递归子树。
返回范围由选中产品、请求信息组及其规定的关系展开共同确定，默认不收集全部关联对象。
基础事实不为了列出各组“有没有”而提前执行全部组提取；可选能力由Schema与描述发现，
模型内实际名称／身份由明确请求的组内发现取得。

按需当前语义具有以下消费者，未请求的组不计算或展开。当前沿固定投影实现这些读取，
对应写工具按各自合同开发。

| 语义组 | 必要读取信息 | 后续操作的用途与边界 |
| --- | --- | --- |
| 构成与依赖 | 当前 aggregation、nesting 的直接父子身份／计数；placement 依赖及业务引用的分类摘要；关系自身的可定位信息 | 区分整产品与子件，调查移动／替换／删除的影响；不展开无界子树，不凭摘要返回“可安全删除”结论 |
| 直接属性、Type、Pset／QTO | 适用的 Description、Tag、ObjectType、PredefinedType；实际 Type 关联；所选属性／数量的值、类型、单位及 occurrence／Type 来源 | 修改具体字段、维护类型与实例属性；Type 不是 CAD2BIM Family，不将实例值与继承值合成丢失来源的字典，不为缺失数量自动重算几何 |
| 材料 | 当前实际材料关联、直接／继承来源、单材料／层／型材／组分集合种类、所选成员与适用尺寸／单位 | 材料分配与替换前核现状及共享关系；材料不具有 GlobalId，不伪造身份或将颜色当材料；样式／显示层需要时沿既有 Query 调查 |
| 场地与空间分配 | 直接 containment、额外 spatial reference、容器 GUID／类别／名称及解释所需空间层级 | 变更 Site／Building／Storey／Space 等归属前确认实际关系；包含、引用、聚合和几何上位于空间内不能互相推导 |
| 系统与分组 | 实际 System／Group／适用 Circuit 身份、类别、名称、成员关联及适用服务关系 | 分配／迁移系统前核现状；多系统可能合法，系统归属不证明端口连接、压力值或上下游 |
| 真实端口与连接 | 当前端口身份、所属产品、位姿、实际 FlowDirection 等属性；连接关系、对端端口及所属产品 | 创建／断开／重连前确定准确端点与现有连接；无端口与未查询分开，保留未知流向，不从历史 Family 接口或几何接触补造关系 |
| 几何表示摘要 | 当前是否有 Representation、必要的表示标识／类别／Context 及 Body 状态 | 几何修改／替换前了解已有表示；不默认输出网格、完整表示树或计算全形体等价；合法装配根无独立 Body 不等于失败 |

Agent按任务选择信息，例如查位置只需基础事实；查压力字段与材料选择属性和材料；迁移楼层
选择空间并在必要时补结构；替换产品且保留关系时组合结构、属性、材料、空间、系统与连接。
这些是可迁移的调用组织，不把特定实例、字段答案或固定调用顺序写入通用工具说明。

当前公开投影的结构支持范围如下，不能将固定摘要扩写成任意IFC结构解释器：

| 对象 | 当前支持与单位／身份边界 |
| --- | --- |
| 属性值／数量 | `IfcPropertySingleValue`、枚举值、列表值及`IfcPhysicalSimpleQuantity`；标量为bool／int／float／str，保留实际值类型和解析到的属性单位。复杂属性结构明确unsupported，不展平为假标量；保存数量不从几何重算 |
| 材料集合 | single、layer_set／layer_set_usage、profile_set／profile_set_usage、constituent_set、list；层厚和引用线偏移换算米，适用时保留Fraction、Profile局部身份和DirectionSense；tapering usage等未支持结构明确诊断，不补造普通profile set |
| 位姿 | 可解析的无环`IfcLocalPlacement`刚性链；project-world translation为米、rotation为无量纲矩阵。缺失／歧义长度单位、非支持placement或循环链分别保留对象事实及诊断，不猜单位矩阵 |
| IFC身份 | 有GlobalId的产品、关系、Type、容器、系统、端口及对端保留实际GUID；无GlobalId的属性、材料、placement、表示／Context仅给同次结果内的`step_id/ifc_class`，不能跨调用用STEP号选取或作为未来写入许可 |
| 名称与值 | `selector_name`是完整、区分大小写的实际选择名称；展示文本及长字符串属性值至多2048字符并标明截断，不能将显示截断文本当准确selector。已知名称可直取；未命中、同名歧义及不支持结构按实际状态解释 |

米制统一适用于位姿及显式`*_m`尺寸字段；材料成员的`unit`记录项目源长度单位元数据，
不改变`thickness_m`的米制含义。属性／数量值保留保存值及其实际单位，不统一改写为米。
对`IfcSIUnit`，`unit.name`保留IFC基础Name，`unit.symbol`包含适用SI前缀；须联合解释，
基础名称与含前缀符号并列本身不构成单位冲突。缺失或不支持的单位不授权猜测尺度。

公开结果由`view/items/operation`组成；每项分别具有`apply_match`与可选`current`。
`current`包含GUID定位状态、匹配数、基础事实及请求的语义组；组内统一保留status、必要
diagnostic和适用count。`operation`仅在指定原操作时存在，承接真实outcome、Family引用、
适用committed_revision及原input／四表report导航；不重复整个Operation包络。
产品选择数、原报告行数与原批次提交数是不同量：READ所选一项不能被解释成原批次只提交一项，
完整批次汇总由原`operation_get`提供。实际IFC class也不能替代原Family／产品业务身份。

给定原操作时，`apply_match`仅判断所选对象与该`operation_ref`的关系；省略时为
`not_checked`，当前对象存在也不证明它匹配某个原批次。比较结论应保留同一对象GUID与
目标操作范围，不能以命中另一操作替代当前问题。可直接消费本工具的准确匹配，或复用
原报告完整GUID集合／等价精确查询；部分清单或跨包同名业务键的未命中不足以证明对象不在其中。

返回的每个嵌套类型须交代用途、投影 owner 与正式消费者。大文本、全部 Pset、完整解算、
表示树和模型库对象不默认公开。原输入／报告明细沿 Data；当前语义明细由本工具所请求组或
既有 IFC Query 提供，不能将 Data 中的历史四表当作当前 Pset、材料、分配或连接的读取入口。
没有 GlobalId 的材料／属性子对象保留其真实定位语义，不把 STEP 行号当跨 revision 稳定身份，
也不提前发明供未来写工具使用的资格 token 或可变对象引用合同。
每组分别表达未请求、已查但未赋值／无关系、不支持、歧义、超限或技术失败；未查不能返回
“没有”，超限须说明结果不完整及现有下一步，不能吞掉技术错误换成空数组。
直接aggregate父不唯一时明确关系歧义，不任选一个；完整关系遍历仍用IFC Query。
构成关系读取实际关系集合，不复用写侧按首项选择语义placement父的策略，不将PlacementRelTo
或历史parent_component_key当成当前aggregate关系。
nesting、placement 依赖、空间包含、系统成员和端口连接也分别按实际关系解释。
当前定位、历史匹配和明细可用性分别表达，互不推导；当前找不到对象也可以有准确历史关联。

后续调用按“读取准确对象和相关现状→对应写工具在自己的当前模型／事务内复核并修改→读取
受影响字段确认结果”组织。READ 不锁住模型，不签发修改／删除许可；跨工具调用不自动共用
exact，后续写仍承担自己的对象范围、共享引用、级联／保留规则和 CURRENT CAS。

准确键及歧义规则：

- 输入原行由 exact Package/member/payload 与 `product_key` 共同定位；包内键不是全局来源。
- products 报告唯一键是 `input_row_index`，不是 `product_key`；它也不是输入物理行接口。
  失败报告允许缺失／重复产品键。重复时保留完整原报告，主返回明确歧义与匹配数量，不任选
  首行或给出唯一 GUID；全部诊断行沿 Data 有界读取。无效键行仍可通过原报告读取。
- components 按 `(product_key, component_key)`，interfaces 按 `(product_key, interface_key)`，
  checks 按 `check_index` 定位。根也可能在 components，同一 GUID 的根／组件角色合并，
  不当成两个实体或来源歧义。
- IFC 定位不能只信 `by_guid()` 的首个结果；选中 GUID 的多实体冲突、非产品对象及未知 GUID
  分别给出可行动状态，不把任意 IfcRoot 当产品 occurrence。
- 未提供来源范围为“未查询来源”；指定 Operation 无匹配为“该操作内无匹配”；多个不一致
  候选为歧义。均不能升级为“全局没有来源”。不扫描全部 Package 或按名字／坐标猜匹配。
- 输入关联只证明对应的 Apply 输入。CAD 行级来源依赖原输入保留的来源键，允许零到多来源、
  一源多产品及设计新增；Package 血缘不自动证明逐行唯一 CAD 来源。

## 5. 历史、CURRENT、Scope 与失败

`applied` 仅读原 Operation／报告和原提交元数据，不解析历史或当前 IFC。它回答原操作保存的
诊断，以及整批成功时的准确提交映射，不证明实体此刻仍在。原接口、solution、正规化参数和
报告世界变换保持历史快照，不是当前属性、材料、分配、真实端口或连接的事实源。

Apply 严格整批提交：所有合法行先做数值预检；普通输入、求解或接口检查任一失败，整批拒绝，
几何实例化不执行、提交数为零。预检全通过后才构建与提交；已确认失败同样不保留本次部分
新增产品。失败报告可保留逐产品已求解／已构建事实，但 commit 均为 NOT_COMMITTED，全部
根／组件提交 GUID 为空；这些执行状态只用于诊断。准则及实现见
[Apply Core](ifc-product-apply-core.md#整批数值屏障与实例化)和
`product_apply_report.py::report_for_commit`。查询同一批“哪些提交成功、哪些提交失败”
不构成本工具的价值场景，原 Operation 已提供整批结论。

例如 valve_02 数值预检失败时，报告可记录 valve_01／valve_03 预检通过，但三个产品均未
构建、未提交、无持久根 GUID。Agent 应解释整批未添加，并通过 checks 定位失败原因；
修正输入后的新 Apply 仍应包含原批全部目标，不能只重试失败行来遗漏其他未提交产品。
成功批次的产品键↔GUID定位、当前语义调查及后续操作准备才是主要使用场景。

`current` 与 `from_ifc` 固定当前调用 Run 的 CURRENT；整批只使用一个 exact。若还绑定原
Apply，须校验原提交模型与当前模型一致；不同模型不能仅凭相同 GUID 混接，须明确拒绝
当前关联并保留原历史来源。CURRENT 后续推进不改变本次已固定事实。内部 exact 身份留在
后台；原提交的公开 `revision_no/size_bytes` 复用现行 Apply 合同，不接受任意历史版本选择。

保留既有同 owner/tenant/Task 下的跨 Run Operation 读取权限及 Catalog frontier 约束，
不将恢复写的 same-Run 条件泛化为 READ 禁令。已核实的实际缺口是：历史返回的 `data_NNN`
属于原 Run；现有引用 middleware 不转换 Data alias，Data 工具仍按调用 Run 解析。

当前按 exact Package 身份重新确认当前 Catalog 的可消费 alias；实际可见时返回
当前 alias，可能与原 Run 的数字不同。不可见时明确 `not_in_current_catalog` 类明细访问状态，
不返回看似可在当前 Run 消费的旧 alias，不隐式挂包、创建 Catalog 或降低权限。原 Operation
授权下的 applied 核心读取仍可完成；不能把明细不可消费说成原操作／原输入不存在。
跨 Run 全面 Data 联查所需的新来源分支及 Attempt guard 修改不混入本期；若实际验收任务需要
此能力，先报告具体消费者及影响，按范围变化裁决，不能用空结果伪装闭合。

原操作未决时返回真实 prepared／indeterminate 及结果可用性，不推断零提交或自动 recover。
失败可有原报告，也可因发布失败无报告；不得补造空报告、GUID 或 committed revision。
`from_apply.current` 遇到无已确认提交、无根GUID或重复产品键时，保留原状态并明确当前对象
不可确认，不进入GUID定位，不把缺少提交元数据误报为跨模型冲突或对象已删除。
`from_ifc`附此类原操作时仍可读取所选当前对象。确认失败的原操作没有本次持久 GUID 映射，
其已提交映射匹配数为零；原报告诊断行匹配数可以非零，二者不能混用。prepared／indeterminate
的提交结果及已提交匹配数保持未知，不能写成零；不得把异常或响应丢失当作确认失败，已提交
后的回读技术问题仍须保留已提交事实。原操作确认成功后才按已知原提交模型检查当前关联。
成功报告、输入 binding 或原提交元数据错配沿原 owner 报完整性错误，不降级猜测。

当前 GUID 未找到不推断删除原因。对象存在但 placement 无法解释时保留已确认对象事实并标明
该字段不支持／异常，不返回虚构单位矩阵。合法装配根无独立 Body 不自动判产品失败。
字段级不可解释、逐项未匹配与模型完整性／Runtime 技术失败分别表达；错误码在所属 owner
确定并同步 Provider，不能吞技术错误返回空列表成功。


## 6. 阶段核收、证据与后续边界

B3.10于2026-10-16完成主控独立核收。接收的是本合同的共享读取切片、Direct／Runner公开面、
功能验证，以及实际Agent使用中两项剩余问题的闭环；不是未来修改／删除或关系写入的验收。
工程执行使用`gpt-6.1-sol/max`，正式被测使用Agent与工程、审查角色分开。

唯一工程及原始证据入口为工程证据索引。
验收设计归档和
工程与Agent过程归档
保留原任务、原失败、根因、退修及各时点状态，不替代当前公开合同。

| 接收项 | 证据及准确范围 |
| --- | --- |
| 工程功能 | kernel的1,924个必验节点与Direct的378个唯一节点已接收，由未受影响证据复用及聚焦复验组成；本轮不重跑全量，不把累计结果描述成一次fresh运行 |
| 本轮公开面 | 既有DTO字段补单位和值语义说明，工具描述、共享usage及现有Skill同步；实际Direct／Runner注册和Provider对比17项通过，业务字段、类型、默认值及后台匹配／单位算法不变 |
| 聚焦验证 | 公开合同／描述／Resource／Skill owner检查27项；原生Skill／环境入口39项前置检查，随后隔离修复2项、合法标题至EOF读取修复3项；各自原日志保留，不重复相加为新增全量 |
| 模型两项目标 | `gpt-6.1-sol/high`和`gpt-6-luna/high`均正确保持同一对象GUID与指定其他原批次的范围，并正确联合解释保存数值、SI基础Name与含前缀symbol，区别显式米制placement |
| 实际资料使用 | 两模型确实读取了同份冻结现有Skill的相关内容及共享`cad2bim://contracts/ifc_products_read/usage`；正常短引用、独立fresh对话、无人工答案反馈。该结果不单独证明某一说明层的因果收益 |
| 只读观察 | 实际非superuser、非bypassrls角色下，两隔离环境的Operation、Package、IFC model／revision、Direct session、Task及Run七张业务表完整可见行前后一致；该观察不扩大为物理字节全库审计 |

两模型的本轮原始目录为
Sol scope-units-read-01
及Luna scope-units-read-02，
各自保留原生rollout定位、transport原始字节及索引、实际Skill读取、最终回答和只读观测。
Sol耗时133.201秒；Luna成功轮105.517秒，另保留首次20.779秒的监督器误停成本。
这些是两项局部任务实测，不作同配置旧轮性能比较或全任务效率收益推断。

本轮修复针对公开语义缺口：关联结论保留对象GUID与指定操作范围，允许本工具直接匹配、
准确报告GUID集合或等价精确查询；单位说明保留实际保存值，基础Name与含前缀symbol联合解释。
没有因此增加业务字段、数值转换或新读取链。Luna成功轮曾用错误大小写状态过滤得到两次空行，
后续读取真实值并自主纠正，最终没有误报；该过程保留，不另扩大为必选补验。
首次Luna在业务调用前因监督器误判合法Skill的只读sed而停止，原中断保留，修正原入口规则及
必要负例后只重跑受影响Luna；不将中断计为业务通过或模型失败。

B3.11的结果提取／Package发布，以及后续产品修改／删除、属性／材料维护、空间／系统分配、
端口与连接写入按各自合同另行实施。对应工具复用本读取及现有领域机制，在实际写入时校验
对象与关系；READ结果不构成未来写入许可。当前跨Run明细可消费限制、支持结构及展开边界
继续以本合同为准。达到本期出口后停止增轮；只有实际新改动、新失败或具体证据失效才重新
打开受影响范围，不自动恢复旧实验或开始下一阶段。

### 6.1 源码交付

B3.10源码及已接收的消费者适配已随提交`278a7113ba64bc54d8442dab1588989d20d80813`推送至
`origin/codex/unified-mcp-component`。原生运行入口的必要源码现已纳入Git：
`scripts/experiments/native_authoring/`中的`launch_native.py`、`observed_entry.py`、
`record_observed.py`、`collect_author_delivery.py`和`stdio_recorder.py`。
启动器从自身源码目录定位stdio recorder，
观察器使用同目录collector，原外部recorder不再构成该入口的源码依赖。

源码交付的受影响owner验证记录为40项通过，包含本地真实stdio字节转发及RPC索引校验；准确记录见
上述工程证据索引的`source_publication`。该次交付复用原有功能及两模型业务证据，未重跑模型、
数据库链或全量门禁。原实验数据、数据库、私有配置与历史原始记录仍按本地证据入口保全；
五文件源码闭包不表示全部实验环境已打包或部署。
