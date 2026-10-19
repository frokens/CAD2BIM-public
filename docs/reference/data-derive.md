# data_derive：统一数据派生与 IFC 提取

状态：已实现并通过 B3.11 主控核收。本文是当前唯一公开入口、来源／事务、只读执行及恢复合同。
工程、大文件资源边界与两模型原生业务证据分别核收；具体新增、复用和未覆盖范围见第10—12节。
可复用的准备、原生执行及审计脚本见脚本说明。
本文的`tmp/`原始证据链接属于受控本地记录，不随源码提交；交付范围与证据保留说明见
源码交付记录。

## 1. 职责与公开入口

`data_derive` 是唯一数据派生发布工具，沿原 create／repair 链处理 SQL、Python、IFC 提取及联查。
它从准确输入生成新的不可变 Data Package，不覆盖输入 Package，不修改 IFC 或推进 CURRENT。
Direct 和 Runner 公开入口映射同一 Application Core；Runner 不接收 Direct Session 字段。
不另注册 `data_transform`、`data_repair_transform` 或 `ifc_extract_to_package` 的同义执行入口。

内部 `DataTransformService`、Domain／Worker 类型及程序入口 `transform(ctx)` 保留原职责。
共享 `CriticalOperationKind.DATA_TRANSFORM` 和 `detail_kind=data_transform` 不随公开名机械改写。
该 Operation 类别还有其他实际消费者；新派生操作的生产工具为 `data_derive`。
历史不可变 Operation 保留真实生产者事实，不因此获得旧在线合同或 repair 资格。

Package 输入与表格计算、IFC 当前调查和后续模型写入分别承担各自职责：

| 需求 | 当前入口及边界 |
| --- | --- |
| 小量产品事实、属性／材料／关系及原 Apply 匹配 | `ifc_products_read`；不发布 Package |
| 自定义 IFC 调查并返回有界 JSON | `ifc_query_python`；保存 Attempt，不发布 Package |
| 可复用表格／JSON、多来源联查与持久选择集合 | `data_derive`；声明输出并原子发布 |
| 产品修改、删除或关系写入 | 相应写工具；本页输出不签发修改权限 |

Apply 原四表报告已保存历史输入、诊断和实际提交映射；派生无需重新猜测 product_key→GUID。
历史报告不能替代当前 IFC 的属性、材料、空间／系统或端口连接事实。

## 2. create 的完整来源分支

`mode=create` 使用完整、封闭的来源／引擎分支，不通过全部字段可选来隐藏约束。

| 来源 | `execution.engine` | Package `inputs` | IFC 声明 |
| --- | --- | --- | --- |
| Package-only SQL | `duckdb_sql` | 1—16 个绑定 | 不接受 |
| Package-only Python | `python_arrow` | 1—16 个绑定 | 不声明 |
| IFC-only Python | `python_arrow` | 空数组或省略后的空默认 | `ifc_source=current` |
| IFC＋Package Python | `python_arrow` | 1—16 个绑定 | `ifc_source=current` |

每个 Package 绑定包含 `input_name`、当前 Scope 可消费的 `package_alias` 和准确 `payload_name`。
服务器解析 immutable Package／member；input_name 必须唯一，同一准确 Payload 不重复绑定。
`parameters` 是既有有类型参数；`inline_tables` 是请求内有类型小表，不独立替代必需来源。
SQL 不接受 IFC；未声明 `ifc_source` 的 Python 不获得模型。没有 Package 或 IFC 来源的调用拒绝。

create 提交完整 `execution`、`outputs` set 与 `output_package`；输出数量为 1—16。
每个输出声明准确名称、格式、说明和逻辑键；JSON 还使用当前 JSON Schema 合同。
包说明使用现有 Markdown set 合同；公开 Schema 和共享 Resource 是字段与限制的机器依据。
Agent 不提交路径、内部 model／revision ID、解释器、安装命令、预算开关或固定版本标签。

## 3. 程序与声明输出

Python 源码定义 `transform(ctx)`，返回 `None`；它不返回 MCP 响应或 Package 对象。
Package 沿 `ctx.table(name)`、`ctx.batches(name)`、`ctx.json(name)` 读取，参数沿既有 Context 取得。
局部字典、列表与集合用于计算；正常局部 `set.add()`、`list.remove()` 不等于 IFC mutation。
声明 IFC 后，`ctx.model()` 返回后台加载的真实、进程私有 `ifcopenshell.file`。
可按当前导入／危险能力规则读取属性、Pset/QTO、材料、placement、空间／系统及端口连接。
读取方法示例不是逐函数名单，也不表示所有 native 或低层能力均开放。
作者代码仍使用现有受限 builtin 集合；`getattr/hasattr/eval/exec` 等不可用，完整限制见正式 Resource。
这不限制已加载 IfcOpenShell 正常读 API，也不改变运行期 mutation 审计。

Python 通过 `ctx.write_table`／`ctx.write_json` 提交声明的 Parquet／JSON，不能打开或保存文件。
受控输出经 Host 预建的 FD 传输；作者不选择输出路径或把 IFC 模型直接作为结果返回。
Package-only SQL 使用私有 DuckDB，最终输出是与声明同名的 TEMP TABLE／TEMP VIEW。
SQL 不接触 IFC；其结果仍走相同的 Data 输出验证和发布链。

Host 校验输出集合、名称、格式、Schema、逻辑键、行数、字节及资产；合法空表保留字段。
缺少、重复、未声明、错 Schema、逻辑键冲突或超限时，整批输出不发布。
一个输出失败不能留下可消费的部分 Package；Parquet 与 JSON 使用同一原子发布边界。
完整结果经 Data 工具分页或有界查询，不把全部数据展开到工具正文。

## 4. repair、准确来源与成功重放

`mode=repair` 选择失败 Operation 或成功 Package 作为唯一父来源。
源码变更使用既有有界 `old_text/new_text` edits；outputs 和包元数据沿现有 set／edit 合同修改。
省略的可修字段继承父项；输入、参数、引擎及 IFC 来源不得在 repair 中暗中换掉。
源码由 `code_list/code_read` 读取，不增加另一套源码读取入口。

IFC 来源是同一 Scope 下的准确 model／immutable revision 绑定，不以文件名或字节相同代替身份。
repair 父链继承原 create 实际读取的 IFC；公开 revision_no 只作解释，不是选择器。

| 时点 | 当前行为 |
| --- | --- |
| create 接纳 | 固定一次 CURRENT；执行期间 CURRENT 前进不改变本次来源 |
| 新 repair 接纳 | 与原来源的准确 model／revision 不同则拒绝，不启动该 Worker |
| repair 执行 | 使用固定来源；不在 Worker 执行期间持有数据库锁 |
| repair 发布 | 同一发布事务再次核对 CURRENT，执行中已变化则不发布 Package |
| 已成功原动作的响应丢失 | 先恢复原 Operation／Package；CURRENT 后来变化不推翻原成功 |

接纳与发布两次检查覆盖不同并发窗口，不以额外解析或整文件哈希判断版本是否相同。
未决动作沿原 claim、deadline、候选与对账恢复，不因结果未知另建操作或轮询平台。
过期来源应重新 create 获取当前快照；读取历史 Package 本身不要求 CURRENT 仍相同。
Package-only 分析或 repair 不因上游历史包含 IFC 就自动触发直接 IFC 来源 fence。
普通 `ifc_query_python` repair 仍读父 Attempt 的 exact 快照，与派生发布的两次 fence 不同。

## 5. 返回、来源与只读回查

成功响应包含 Operation alias、Package alias、包名及已发布输出摘要。
单输出分支使用 `output`；多输出分支使用 `outputs`，不发明主输出。
repair 分支另返回 `repair_parent_operation_alias`、`engine` 和准确 `code_sha256`；
公开导航沿当前注册 outputSchema，不因减少调用方重复 SHA 负担而删去真实源码摘要。
声明 IFC 的结果及 Operation detail 可有 `ifc_source.revision_no`，表示该动作的历史来源。
此摘要不能用于选择历史模型、证明当前新鲜度或绕过授权。

内部 `IfcReadSourceRef` 保存准确 model_id、revision_id 和 revision_no，供后台关系及 fence 使用。
这些内部身份保持 server-only；Agent 不能用业务 JSON 同名字段覆盖系统来源。
Host 在既有 manifest 的 `transformation` 系统区域记录准确 IFC 来源及实际输入／源码／输出事实。
不向每行 Parquet 或业务 JSON 注入系统字段，不额外维护来源索引表或可编辑来源副本。
Operation 是准确动作绑定权威，manifest 是唯一生成器写出的不可变发布记录。

`operation_list/get` 查原动作和失败事实；`code_list` 的来源分类仍可为 `data_transform_attempt`。
Code 卡片的当前 repair 导航指向 `data_derive`。默认Operation详情提供分页元数据和真实代码读取位置，
不内联原参数值或全部表行；`transform_input`按需读取原说明、输出Schema、元数据、内联列定义、
物理行区间、命名参数或字符串单元格字符区间。原输入读取也适用于没有输出Package的失败操作。
精确分支、续读位置及64KiB响应边界见[Code与原输入读取合同](code-discovery-data-query-and-transform.md#3-data派生唯一公开入口)。
Package 描述、Schema 和数据沿现有 `data_describe_package`、查询及 JSON 读取入口取得。
Alias 按当前 Scope／Catalog 解释，不能用另一 Run 的同名 alias 替代准确资源。

## 6. 发布事务与唯一权威

沿既有 DataTransformService 接纳、执行、候选 staging、发布和恢复，不增加 DataDeriveService。
准确 Package 解析复用 ExactPayloadResolver，准确 IFC 复用 IfcRevisionReader 和现有资源图注入。
Data Runtime Repository 继续持有同一 `critical_operations` 行、claim、候选和终态。
IFC 来源只增加必要读绑定与配对约束，不借用 IFC 写 claim、write epoch 或写 CAS 字段。
PostgreSQL 沿原 `critical_operations` 增加 `source_ifc_model_id/source_ifc_revision_id`，
通过既有 Scope＋model＋revision 复合外键和配对 CHECK 保护，迁移为 `20261017_0111`。
准确绑定只存该关系权威，原 detail JSON 由 Repository 投影，不保存第二份可独立变化的来源。

来源在接纳时固定；不可变资产读取和受监督 Worker 执行位于事务外。
Host 输出验证和 manifest 构建完成后，沿既有 `publish_package_result` 原子提交。
该事务核 claim、准确候选及适用 repair fence，一次可见 Package、成员、Operation 和 Catalog。
发布与 IFC CURRENT 更新遵守既有 Run 锁序；不在独立事务先检查后宣称发布 fence 已成立。
Storage、Artifact／ArtifactVersion、Package、Catalog 和 Scope／RLS 保留各自唯一权威。

真实字节完整性、动作幂等、候选绑定及可变对象 CAS 保留，不新增人工 ABI/profile 或资格哈希。
IFC mutation 摘要与输入传输 SHA 保护不同错误，不能因都含 hash 就合并或删除。
未受影响的普通表格链保持原职责，不复制 Repository、事务所有者、DTO 或沙箱平台。

## 7. 共享 IFC 私有执行与文件权限

Query 与 IFC 派生复用 IFC 私有执行及保护 owner；结果出口分别为有界 JSON 和声明 Data 输出。
服务器核实准确 IFC 字节，把只读 FD／受控输入交给隔离子进程，不交付正式 IFC 路径。
子进程没有 durable Writer、数据库、Storage 凭据或正式 IFC 提交通路。
私有对象即使被修改也不能改变持久源 IFC；这与程序遵守只读合同是两个不同断言。

静态检查拒绝可识别的模型写入、创建／删除、事务控制、低层绕过和文件操作。
Python 动态能力不能由 AST 穷尽判定；编译通过仍须完成运行期保护。
执行期复用 transaction mutation 记录、事务完整性检查和私有模型前后序列化比较。
检测到修改或必要审计不能完成时，丢弃查询结果／全部派生候选并返回错误。
有记录的修改后还原仍是 mutation；不宣称已证明所有 native 绕过或清除记录再还原均不可行。

IFC adapter 显式关闭 Seatbelt 的 scratch 路径写权限，只保留可信运行与声明输出需要的 FD。
禁止作者调用 opener、模型 `write`、任意导出、普通或 native 路径保存、网络及子进程。
scratch 资源额度不授予作者路径写权限；Python audit 也不是 native 文件隔离的唯一保护。
此隔离属于受控本地执行，不宣称远程敌对多租户代码安全。

## 8. IFC 模式预算与依赖

当前 IFC 派生预算由 IFC adapter 基于 Query 预算替换 Data Python 的执行额度形成。
IFC派生独立使用 **6 GiB RSS**；Query仍为5 GiB，普通Data Python仍为4 GiB，不将这些额度相加。

| 项目 | IFC 派生当前值／语义 |
| --- | --- |
| 准确 IFC 输入 | 最大 512 MiB |
| Worker wall／CPU | 90 秒／180 秒 |
| RSS／scratch 资源额度 | 6 GiB／4 GiB；scratch 路径写仍禁用 |
| 线程／FD | 库配置 2 线程；可信初始化后最多新增 2 个 native 线程；64 FD |
| Package 输入 | 最多 16 绑定、400万行、256 MiB存储Parquet、512 MiB Arrow |
| 声明表格输出 | 合计200万行、512 MiB Arrow、256 MiB Parquet；单Parquet最大100 MiB |

线程增长额度不是进程总线程数；可信导入／初始化仍计入相同 wall、CPU、RSS 与 FD 限制。
IFC 解析、作者执行和 mutation 审计共享一个 Worker 预算，不分别领取完整额度。
外层 Data 调用还承担输出验证与发布；不能只增超时或以 Query JSON 中转大型 Package。
无 IFC 的 SQL／Python 继续使用各自原 Worker 和预算，Query 不自动取得派生模式的额外库。
含 IFC 的计算复用现有 IFC 解释器及获准 Data 计算库；不接受 Agent 路径／在线 pip。
依赖检查核实际解释器与必要库，普通源码变化不触发环境重建、全族重发或资格注册。

## 9. 错误、保存与恢复

输入拒绝、源码拒绝、执行／预算、输出验证、来源变化和发布失败沿原错误及 Operation 链区分。
接纳前错误不保证存在 Operation；已保存失败沿 ToolError 的准确 Operation 导航回读。
已有安全运行诊断经 Worker 错误交给 Core，原因纳入原有界 `message`，最多 2,000 UTF-8 字节。
安全原因可包含异常及源码定位；该裁剪不承诺任意长消息完整，不暴露宿主堆栈或私有路径。
公开响应不新增独立 `diagnostic` 输出字段、调试工具或第二套失败记录。
持久失败、首次响应、`operation_get` 和原请求重放消费同一保存的有界错误事实。
MCP 外层沿既有一行文本格式化压缩空白，不能将首次 RPC 文本与数据库原文作逐字相同承诺。
源码编辑读取准确 Code 引用；错误中的源码行用于定位，不替代完整源码。
失败执行可按原父项 repair；响应／客户端显示失败先找原动作，不能据超时推断零发布。
旧失败文本不可倒写为修后诊断；后续验证产生自己的准确 Operation 和证据。

## 10. 当前 Resource、消费者与证据边界

公开编程与请求资料沿同一 tool_docs owner：

- `cad2bim://contracts/data_derive/duckdb_sql`
- `cad2bim://contracts/data_derive/python_arrow`
- `cad2bim://contracts/data_derive/repair`

Direct 注册、Runner WRITE／非并行调度、BackendClient 和内部 Data API 使用同一新入口。
Core producer、Operation detail／CHECK、Code discovery／repair_entry、当前脚本与 Skill 同步消费。
实现依据见[Data Core](../../src/CAD2BIM_backend/application/data_transform_service.py)、
[Data Repository](../../src/CAD2BIM_backend/infrastructure/postgres/repository_data_runtime.py)和
[IFC adapter](../../src/CAD2BIM_backend/infrastructure/ifc_query/adapter.py)。

工程范围沿最终工程证据索引核定。
新增工程证据覆盖完整 Provider 分支及 Direct／Runner 投影、真实 IFC＋两包贯通、原子输出、
准确来源 FK／RLS、repair 两次 fence、成功恢复与子进程退出后候选恢复，以及共享只读／保存拒绝。
诊断缺口修复扩展原 owner：Core 3项、真实 Worker 2项通过；不累加重叠组为唯一测试数量。

| 原生验证 | 实际范围及结果 |
| --- | --- |
| GPT-6.1 Sol/high 独立 fresh | 四项交付及正式 Data 读回符合冻结 oracle，151.810秒 |
| GPT-6 Luna/high 独立 fresh | 四项最终交付符合 oracle，保留自然程序／输入错误及自主恢复，162.139秒 |
| 原 Luna 线程继续 | 原码新建复现得到具体 NameError，沿新失败 repair 并读回位置表／summary，61.760秒；不增加 fresh 组 |

四项交付为双包＋IFC的位置复核 Parquet／JSON、Package-only优先级统计、同源header repair、
过期来源header拒绝后重新create。真实毫米坐标换算为米，缺失实体仍保留请求行与null，缺失QTO不冒零。
业务后验核输出、准确来源与manifest；
状态后验逐项对应新增Operation／Package，
核失败零Package、原任务可见事实未改写、CURRENT保持原准确来源及资产完整。

诊断续验的独立后验
确认原代码／绑定／完整声明一致、具体异常及源码定位在公开错误与Operation可读，旧失败文本保持，
修后数据符合原位置oracle；没有Query或另三项重跑。首次错误实际是受限builtin的getattr不可用，
缺失GUID是后续另一程序路径的问题。JSON Schema属性顺序按完整解析值比较，首次RPC错误按既有
Provider投影比较，原始审计失败均保留。原请求幂等重放一致性由工程owner及持久化恢复证据验证；
同线程续验只做原码新建复现和repair，不冒称新原生幂等重放或独立诊断学习实验。

两模型实际使用Direct会话、限定正式工具／Resource和冻结Skill，后台为物理独立数据库／Storage，
保留相同冻结身份ID，fresh指模型会话。小模型只有一个Pipe；本轮原生覆盖单位与缺失事实，
不将其扩大为复杂父层级变换、全部材料／关系或所有恶意native绕过均已验证。
上述基础原生验证未覆盖弯头角度、特定场地、真实连接管道和截短量。后续两臂正式IFC／安装
Package环境及独立验收依据已核收，内部装载见可信测试基线。
真实大文件后台证据见第11节，新增业务原生验证及有反馈修复见第12节，不追写为上述基础
原生运行已经覆盖这些场景。
原始调用、错误、时间和访问边界见原生证据目录。
B3.10、既有Data与IFC证据只复用未受影响范围；未重跑全量、Docker、wheel或全部Runner图链路。

准备中另有已知 Seed exporter 数值编码缺陷：极大浮点边界经 JSONB 读回成为309位整数，
触发 RFC 8785 IntegerDomainError；本轮未修，不把替代 dump／restore 准备写成 Seed 导出通过。
原证据见Seed 导出失败。
设计、原失败、退修及授权过程归入实施过程归档。

## 11. 真实大文件补验

2026-10-17使用用户指定的`08-final.ifc`完成优化前5 GiB提取预算下的本地串行后台纵向验证：404,950,864字节
（386.19MiB）、IFC4X3_ADD2、2,749,365条STEP实体记录。测试在既有隔离环境的独立Scope中
进行，原文件和两套小场景保留。登记副本仅绑定本Scope初始化Project GlobalId，其他行按字节
复制；原Writer提交后由正式exact Reader确认与登记字节一致。准确来源及原操作重放见
装载证据。

| 正常调用 | 结果与业务核验 | 调用与子进程耗时 |
| --- | --- | --- |
| Query | 类型、空间祖先、系统成员及端口关系统计与原STEP独立解析一致；56个类型组有界返回50组并明确截断 | 整条调用50.743秒，其中前置inspection进程13.416秒、Query进程36.109秒 |
| IFC data_derive | 一个新Package含全部14,304根管段的空间归属、20,683条真实端口连接及端口owner、统计JSON；正式Data分别72／104页读回，完整GUID集合和对应字段逐项一致 | 发布调用39.393秒，派生进程37.971秒；含完整Data读回54.654秒 |

独立核验使用原STEP记录及schema继承元数据，不调用被测提取程序生成预期；唯一Project
GUID映射按登记绑定核验。具体结果见Query原始记录、
派生与正式Data读回和
完整独立核验。
名称STEP转义解码未纳入该独立比较；系统数量是各真实System的不同直接成员数，不是管段数。

上述优化前正常调用未出现自然超时或RSS失败，当时生产预算未调高。另在测试侧分别设置Query／derive
子进程wall为2秒，以及一次derive RSS为1GiB，得到两次`IFC_QUERY_TIMEOUT`和一次
`IFC_QUERY_RESOURCE_LIMIT`。原Failed Attempt／Operation保留；没有结果Artifact／Package，
CURRENT及原Package集合不变。实际进程结束、Host槽可重新获取、本地容量归还和无残留
子进程均核实。派生的两项故障发生在初始化阶段，不表示作者提取程序已完整运行。

随后恢复生产预算的新Query调用49.520秒、新derive调用39.981秒成功。它们是后续合法新动作，
没有把原失败改成成功，也不构成原Operation repair新验证。Query源码和结果一致；派生源码、
声明及准确来源未变，新Package通过正式Data读取行数与summary，完整逐行正确性复用前一
正常产物的证据。失败与后续调用的原始记录均在
本次运行目录。

优化前supervisor记录Query／derive的RSS采样峰值分别为4,531,568,640及5,279,956,992字节，
约4.220／4.917GiB。后者距5GiB额度仅约84.64MiB，采样值不是严格峰值上界，也不是稳定
可用余量。**本次只证明这个文件和这些程序在现有预算下通过，不保证全部≤512MiB输入成功。**
Query总调用还包含前置读取／inspection，不能把50.743秒总耗时误写为45秒子进程预算超限；
当前未分别观测Query／derive内部解析、业务计算和前后mutation审计耗时。

业务衔接的有界源字段发现
另确认14,304根管及8,889个管件均有直接材料关联，5个System的统计可复用；这不是新增材料
提取结果或有效工程材料分类验收。全量单值属性名称扫描中唯一`angle`子串命中为
`MeshTriangleCount`，它是三角面数量，不能用作设计转角；本次未识别可直接采用的设计转角
命名字段，不能据此断言其他属性／Type／几何表示没有角度。三个管段样例可读取局部Polyline
Axis长度，未验证全部轴线、世界坐标或Body长度。没有安装目标P／g／d，故不计算本文件的
截短量；角度阈值、实例／Type规则、正负截短量和偏轴仍由已准备的小场景提供明确预期。

上述大文件实际执行入口是共享Application／Worker及正式Reader／Data链路，不包含LLM或
大文件MCP transport实测。未改变的Provider与入口证据按适用范围复用，不能将后台补验写成
新增Agent业务通过。没有重跑全量门禁，未启动下一阶段。

用户随后授权减少内存开销、提升预算及两模型原生业务补验。当前共享child在准确校验／解析后
释放原始IFC bytes；前后模型摘要仍基于完整序列化结果，只改为分片UTF-8编码喂入原摘要，
避免同时保留整份编码bytes。Host拼接唯一payload后释放分块列表，再调用原Worker。
事务、改后还原检测和文件隔离保持原职责；这不构成流式IFC解析或序列化。IFC派生RSS
独立调整为第8节的6GiB，Query和普通Data额度不随之扩大。

优化后只补一条相同准确来源、相同完整command／声明的实际提取：

| 观测项 | 优化前 | 优化后 |
| --- | --- | --- |
| IFC派生RSS上限 | 5GiB | 6GiB |
| Worker supervisor RSS采样峰值 | 5,279,956,992字节，约4.917GiB | 4,988,600,320字节，约4.646GiB |
| Host 0.5秒RSS采样峰值 | 1,252,491,264字节 | 1,145,864,192字节 |
| 发布调用 | 39.393秒 | 39.232秒 |
| 含完整Data读回 | 54.654秒 | 54.980秒 |

新Package的全部14,304管段、20,683连接及summary与优化前正式读回逐项一致，准确来源和
声明未变；CURRENT不变，容量释放且无残留子进程。Worker采样减少291,356,672字节，
约277.86MiB（5.52%）；新额度距本次采样值约1.354GiB。采样不是严格峰值上界，实际减少
也不等于理论副本大小，Host采样变化不单独归因某一行代码；本次没有证明稳定速度收益。
原失败、原完整集合核验及未受影响工程证据继续复用，没有重跑大文件矩阵。直接受影响的
UTF-8摘要一致性、代表性真实只读负例及预算传递检查通过。准确记录见
优化前后对照
及本次正式读回。
两模型新增原生业务结果按实际执行后另行核收，不从本项性能与正确性对照推定通过。

## 12. 原生业务补验与问题闭环

2026-10-17，在两套已核收的独立数据库／Storage中，分别以`gpt-6.1-sol/high`和
`gpt-6-luna/high`完成唯一一次独立fresh业务运行。两者读取同一冻结任务及各自Scope引用，
使用正式revision 2和安装方案Package；只开放本次13项客户端工具白名单。工具资料、通用
Skill及模型实际读取的Resource均留存，控制侧oracle和另一组解法未进入模型输入。
首次均使用优化后的6GiB IFC派生预算；后续只在实际问题影响范围内处理。

| 实际段落 | 业务结论与最终交付 | 实际时长 |
| --- | --- | --- |
| Sol独立首轮 | `data_003`弯头／场地统计和`data_004`连接／截短全部核收；三次Query JSON投影拒绝后自行修正 | 253.279秒 |
| Luna独立首轮 | `data_006`弯头包漏读Type材料，材料问题清单及系统分组错误；`data_005`连接／截短正确。首轮失败保留 | 166.830秒 |
| Luna原会话有反馈续验 | 只修Task1，沿`data_006→data_007→data_008`修订；最终`data_008/op_010`通过，Task2继续精确复用`data_005` | 71.347秒 |

这不是两个独立首轮都成功。续验仍为原`gpt-6-luna/high`线程，反馈只指出通用Type材料来源
遗漏并重申严格>90按未舍入值判断，没有提供具体GUID、材料名、数量、oracle或Sol解法。
续验仅补读更新的Query Resource，未重做已通过的连接／截短，没有新增fresh模型轮次。

业务核收来自正式Data页面、实际保存源码、独立冻结预期及关系见证，结论包括：

- 设计转角严格>90°的全局集合3件，目标Site归属子集2件；90°排除、Type／实例来源和显式
  null正确，未知角度保留。containment祖先与额外spatial reference分开，同名空间不混淆。
- 场地材料×系统分组各1；两条Carbon steel系统组与一条材料／系统缺失组并存，组计数和3
  不替代不同构件总数2。Luna新包的Type材料由作者程序实际读取，不由验收器填入。
- 实际一跳端口连接为3根管，邻近且同系统的未连接管排除；关系、端口owner及准确GUID
  对应。长度按实际单位和父级placement读取，截短+0.22m、需加长差值−0.34m，偏轴0.04m
  的目标长度／差值保持null并解释原因。Sol读取Body见证；Luna使用Axis／端口，复用冻结
  模型中Axis／Body／端口一致的独立事实，不声称其程序另外检查了Body。
- 最终Package对应原准确IFC revision 2；CURRENT、Artifact和write_epoch未变化。输出只是
  分析数据，不构成未来修改或连接写入授权。

原始自动比较因字段名和summary嵌套差异报错，保留在
audit-01。
控制侧随后仅对真实值作明确的字段／布局和已有诊断映射，不用oracle补行、材料、数值或
原因；映射后Sol通过、Luna首轮真实材料错误仍保留。修复后只新读Luna Task1全部Payload，
旧Task2逐值复用。准确结果见首轮语义比较、
Luna修复正式Data及
修复语义比较。
这些机械比较的待人工见证状态，已由主控对保存页面、源码和关系的独立核读闭合。

两项通用改进也已闭环：Query保持原JSON类型规则，在原递归校验中定位有界
`json_path/value_type/reason`，由真实Provider错误和持久／重放检查证明透传；Resource澄清
递归`get_info`仍可能含tuple，以及实例关联不等于有效Type来源。原首轮只见旧说明，新说明
的消费留在Luna续验，不能追写首轮。旧错误没有运行时首坏字段，分析仅按保存源码、依赖
和STEP事实推断tuple／原生引用原因，没有用新诊断伪造历史定位。

完整调用、资料读取、等待区间、可观测token和干预边界见
过程审计。
单次时长不构成模型排名，未观测区间不等于思考时间；没有因诊断说明修改重跑Sol或大文件。
本次固定样本通过不外推全部临界角度／长度、所有材料组合或全部IFC几何表示。既有17项
比较器校准、原生基础能力、只读负例、事务及大文件证据按受影响范围复用，未全量回归。

**本轮B3.11必选业务与资源增量已由主控核收，无剩余必修项。** 当前总入口为
最终证据索引；
原场景协议与准备记录
归档保留。独立首轮失败、中间Package和原始审计不覆盖，完成后停止本阶段工作，不自动进入下一阶段。
