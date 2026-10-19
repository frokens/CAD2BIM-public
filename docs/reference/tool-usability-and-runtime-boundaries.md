# 工具用法与运行边界

```text
status: implemented
scope: existing_Direct_tools_and_shared_runtime_boundaries
verification: focused_formal_ifc_cad_data_wheel_verified; native_passed_with_observations
```

本文记录现有工具用法的唯一来源、Data Python执行环境验证和已修正的读取/查询边界。
工具集合仍由Direct server实际注册决定；本文不定义另一份工具清单、Schema或业务状态。

## 工具描述的唯一来源

`src/CAD2BIM_mcp/tool_docs/`按稳定能力组织普通文本常量和可执行用法示例。
`direct/**/tools.py`引用工具描述，现有contracts/input DTO引用字段和对象帮助；Schema、enum、
预算及业务验证仍由原Provider/Domain合同拥有。`direct/server.py::_TOOL_REGISTRARS`保持唯一
注册顺序。两条保留的CAD描述import路径只作只读re-export，不拥有独立文本。

共享用法与Direct入口字段说明分开维护；`direct_session_alias`及受控本地source_path不成为
Backend或未来Runner合同。文本不携带阶段编号、migration操作指令、内部Agent消息协议或项目答案。
参数来源、分页、结果、硬限制、失败与下一步动作必须在当前实际Provider声明可达。

通用工具描述和Resource只承载调用方法、公开合同及调用者可处理的失败恢复。
项目的留出集纪律、交审/交接流程和本地辅助脚本问题留在项目SOP或故障记录中；
渲染器内部重试算法不作为调用说明。验证属性按具体模型声明选择，不从示例推广空心、
单Solid、完整帧、零重叠或全参数域证明要求。Family写入只保存草稿，Preview执行案例并检查proposal，
公开说明不要求调用者另建本地脚本重复后端验证。

公共reference/Scope恢复、说明编写和支持域位于`cad2bim://contracts/common/usage`；
有限案例、复用及发布位于`common/authoring`；相机、材料观察和结果解读位于`common/preview`。
完整说明只有一个归属，Geometry/Family具体请求放各自Preview tutorial，Write tutorial只教保存草稿。
工具和领域resource用短导航访问栏目，URI片段不构成额外resource或局部读取；必要字段说明和领域特有限制就近保留，
尤其Geometry可用temporary_interfaces而Family明确拒绝此字段（含null/空列表）。
共享类型由原DTO派生：`common/types`保存写作/程序共同类型，`common/preview_types`保存案例/渲染/结果类型，
每个定义只存一处、完整传递引用保持；具体操作仍从原资源外壳和根约束开始。
读取消费者必须显式取得并解析已读公共页，不能假定任意MCP客户端自动解析跨URI。
公开结果只含影响调用、结果判断、下一步及故障处理的字段；内部snapshot和完整性摘要不进入Preview/Operation公开结果。
业务参数中相同名称的键不据此删除，后台完整记录不变，源码修订CAS摘要继续保留。
R按实际读取正文计量，普通链接不自动加载；单工具自身、共享页、全目录去重总量分列。
公共正文唯一性、程序读取校验和原生Agent可用性分别记事实，静态计数不冒充实际实验账单或耗时收益。

最终工具input可带本工具内的`$defs/$ref`，完整展开后仍是同一输入合同；这不要求额外读取resource。
字段断言须沿本地引用解析，不能假设每个字段节点都内联。FastMCP默认解引用仍启用，最终列表投影
仅改返回声明副本；注册输入校验及短引用解析保持原路径。SDK声明往返已验证，原生模型接受性未验证，
后续LangChain模型适配会再展开的情况不计为当前收益。

代码类工具的完整示例同时经过当前Direct DTO解析和真实Runtime/Worker执行，覆盖Geometry
`build(ctx)`、Family `realize(ctx)`三终态、Data SQL/Python Transform与Query、Geometry和IFC Query。
其余分支沿既有Provider测试验证，不复制第二份Schema。示例文本不能仅以substring或hash测试
代替可执行性；描述变动同时核对去description后的机器声明，实际工具可用性另由原生调用验证。
详细工程规则见Provider Schema与描述规则。

## Data Python计算环境与恢复

`python_worker_environment.py`保留8个child源码文件的Seatbelt读取白名单。launcher统一
加载Host当前授权源码，复用既有Worker环境中的第三方计算依赖。源码解析检查准确路径、
普通文件和唯一可信package root，沙箱按白名单限制实际读取。

attestation记录批准库、实际第三方distribution闭包与解释器身份；Direct启动核验必要的
解释器环境、数据库和存储，不前置运行各Worker／Runtime真实样例。OS沙箱及真实child readiness
由独立setup/check验证；实际业务调用保留既有监督、容量、预算和输入／产物校验。
Host源码、预算和项目版本更新沿当前部署生效；环境维护
由计算依赖或解释器的实际变化触发。setup从`uv.lock`解析依赖并沿原sdist流程安装，
check比较实际依赖与解释器，原环境记录的额外字段按非合同元数据处理。恢复命令保留venv
launcher路径并正确shell quoting；installed wheel找不到可信checkout时不臆造setup路径。

启动故障分别归为source、dependency、interpreter、environment、sandbox或child-readiness类别，
Direct数据库readiness另用`DATABASE_READINESS_FAILED`。各Worker真实执行故障在对应业务调用中返回；
不建立lazy readiness、能力profile平台或新的Worker监督栈。诊断不泄露连接凭据或未经处理的底层异常。
精确配置、命令和隔离边界见[Code与Data运行合同](code-discovery-data-query-and-transform.md)。

## exact资产的一次完整性读取

原`StoragePort.read_verified`返回同一次读取的stored metadata、bytes和verified SHA。
LocalStorage从固定root文件描述符逐级打开目录，拒绝越界和symlink，检查metadata/payload为regular
file，并在一次payload materialization中计算摘要。打开前后及路径身份检查用于检测读取中的替换；
FIFO以非阻塞方式打开后按文件类型拒绝。此策略不承诺对有权任意修改宿主文件系统的攻击者提供
超出文件描述符和身份核对的隔离。

Geometry与Family exact消费者在原Scope/Repository授权之后，核验各自asset role、key、MIME、
size与SHA绑定；不再单独重复扫描同一小资产或在Application重复计算整份SHA。
大IFC的既有stat/iter_chunks语义不变。精确角色与发布/重放职责分别见
[Geometry资源内核](geometry-resource-catalog-kernel.md)和[Family资源内核](product-family-catalog-kernel.md)。

## IFC Query与视野请求的错误边界

IFC Query允许局部dict/list的索引赋值、累加、切片和删除。模型Attribute写入、private/low-level
访问、事务控制、反射、I/O及有副作用的模型方法限制保留。实体index修改仍受私有模型事务过程
记录与前后摘要审计；修改后还原不能仅因最终SHA相同而通过。不受IfcOpenShell支持的实体index
删除按真实执行失败返回，不伪造同一种mutation诊断。精确合同见[IFC Query](ifc-query.md)。

interface_focus范围不足属于视图输入错误：Geometry返回`GEOMETRY_PREVIEW_REQUEST_INVALID`，
Family返回`PREVIEW_REQUEST_INVALID`，阶段为input_validation；扩大或省略vertical_extent_m可以
修复。该错误不表示资产损坏。完整相机、同次PNG和exact inspect合同见
[共享Preview与inspect](geometry-preview-and-inspection.md)。

## 统一执行准入与调用期限

本节描述当前实现；统一准入与诊断已通过正式 CAD/Data、IFC、wheel、聚焦回归及真实 stdio 并发验证。
精确冻结快照、补充修复前后的验证范围和资源观测见
实施审计。
这些证据不等于 fresh Agent 易用性重验或完整 Golden 业务验收。

`domain/runtime_execution.py`拥有调用预算；各 Worker 原有 policy 继续拥有单个 child 的
CPU、RSS、scratch、输出、线程、FD 和 wall 上限。调用预算分为排队 Q、整条执行链 H、
持久终态/发布 T 和清理 C，单位均为秒；总期限为 Q+H+T+C。排队不能消耗保留的 H/T/C。
H 是多阶段的共用上限，并不扩大单个 child 的资源额度；阶段之间不重新取得完整预算。

| 现有能力 | 资源池 | Q | H | T | C | 调用总期限 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 独立 Geometry codec | Python | 600 | 45 | 15 | 5 | 665 |
| Geometry Authoring / 接口补标 | Python | 600 | 120 | 15 | 5 | 740 |
| Geometry Preview / inspect | Python | 600 | 150 | 15 | 5 | 770 |
| Geometry Query | Python | 600 | 270 | 15 | 5 | 890 |
| 独立 Product Realization（含批量） | Python | 600 | 900 | 15 | 5 | 1520 |
| Family Preview / inspect | Python | 600 | 240 | 15 | 5 | 860 |
| Family current_ifc Preview（含Host提取） | Python | 600 | 410 | 15 | 5 | 1030 |
| Family Authoring | Python | 600 | 540 | 15 | 5 | 1160 |
| IFC Query（含 exact IFC inspection） | Python | 600 | 230 | 15 | 5 | 850 |
| 单次 IFC Revision / Model info / Context、Spatial list | Python | 600 | 170 | 15 | 5 | 790 |
| IFC 写入完整 invocation | Python | 600 | 510 | 15 | 5 | 1130 |
| Data Python Query / Transform | Python | 600 | 110 | 15 | 5 | 730 |
| Data SQL Query / Transform | SQL | 120 | 60 | 15 | 5 | 200 |
| CAD tolerance analysis | CAD | 360 | 110 | 15 | 5 | 490 |
| CAD measurement compute | CAD | 360 | 170 | 15 | 5 | 550 |

有显式 Direct 外层 guard 的调用从同一 policy 派生期限并另留 10 秒响应余量；
项目原生 MCP 客户端 timeout 为 1200 秒。普通有界读取、JSON读取、cursor 续页和官方文档
仍使用原来的 10/15 秒合同；目录和 Code/Operation 读取不进入重计算队列。
Family Authoring 的 H 覆盖一次 readiness 与最多16个、每个30秒的验证场景及有界准备；
IFC 写入 H 覆盖最多三个170秒的原有计算/验证链。数值是有限准入策略，不能被写成
所有合法最大输入均已达到性能目标的实测结论。

Direct 组合根显式共享 Python 单槽、SQL 双槽和 CAD 单槽，默认 Host 路径跨 MCP 进程相同。
三个池保持独立资源边界，复用唯一 `HostWorkerCapacity`。每池最多32个等待请求，使用
Host FIFO ticket 与固定槽/等待文件描述符；取消或死亡请求失去描述符后由下一次准入清理。
队列记录只是短期资源元数据，不是 Job、Operation、Package 或第二套数据库事实源。
目录/锁配置矛盾及不安全文件类型 fail closed，不替换仍被持有的锁 inode。

同一 asyncio Task 的重计算链持有一次 lease，Geometry/Product/codec 连续阶段共享它；
并行子 Task 不能继承并同时使用同一 lease。发布前释放执行槽，后续独立的必要验证重新
入队，但仍消耗同一次 invocation 的剩余 Q/H。持久发布、CAS、Run 写顺序和 IFC
幂等仍由原 Application/Repository/Writer 决定。IFC 同一逻辑写的多等待者共用既有
协调任务；单个等待者超时不取消该任务，也不无限延长原有限 invocation。

## 缓存、取消与失败事实

codec 仅在当前调用拥有的 lease 范围内记忆已完整验证的 exact canonical bytes，最多16项、
输入/输出字节引用合计64 MiB；离开调用即清除。control、原payload identity和同次typed receipt绑定
验证保留，不以跨重写SHA或重算浮点摘要相等作为门槛，跨调用不依赖内存缓存。已渲染 PNG 的授权回读只验证原 Scope、exact Artifact、
资产角色、MIME、长度/SHA、谱系与快照；无需在释放槽后重新解析几何。缓存容量不足不能
缩小合法 BREP 支持范围。inspect 重新渲染读取精确保存字节并生成显示网格，不重写BREP。

所有现有受监督 child 启动点沿 `WorkerProcessSupervisor` 接入 parent-liveness pipe 与
同进程组 guardian。实际 child 与 guardian 持有借用的槽 FD，父进程退出后 guardian
终止并回收进程组，容量不在旧 child 存活时提前出售。正常/重复取消也等待必要清理，
先保留真实 PID、执行阶段和资源观测，再传播取消。该受控本地机制不是远程多租户隔离。

共享Supervisor在事件循环内持续检查墙钟与CPU，昂贵进程属性采样由唯一在途线程执行；
采样使用独立psutil Process及oneshot，避免其临时缓存影响及时的CPU读数。
临时目录最多每0.5秒完整扫描一次，进程正常退出时再核最终目录大小；目录遍历使用
scandir且逐项响应停止标志。采样不阻塞协议收发；退出先停止Worker，再停止并回收采样任务。
资源异常在主监督任务中补充执行Scope诊断，收发子任务只报告异常事实。
Python线程不能强制中断正在进行的OS查询；目录遍历可协作停止，采样线程回收仍等待当前系统调用返回。

DataQuery 的有界 Arrow 扫描和 DXF 提取仍在 Host 线程中，取消通知原预算器/提取循环，
调用可及时返回，线程完成后才删除它仍持有的临时数据。线程没有被 OS 强杀的保证；
这些轻量路径不竞争几何槽，最大合法扫描及总 Host 并行资源另作表征。

准入诊断区分 `queue_full`、`queue_timeout`、`capacity_unavailable`，并保留资源池、
等待时长、本阶段是否开始和整次调用是否已有执行。其原领域异常通过 Application、
Attempt/Operation 和 Direct 传递；机器故障不伪装为几何/参数不合法。IFC 的纯准入
拒绝直接返回，不在同次调用内无变化重试，也不据此冻结有效模型；既有 Operation
保留用于对账或后续重放。已确定的 IFC 完整性破坏仍沿原冻结路径。

Geometry / Family Preview 区分输入、授权/完整性、Runtime 容量、资源、超时和执行错误；
运行开始后的同输入资源失败为不可自动重试终态。Geometry / IFC Query 已保存 Attempt
后遇到取消，沿原 `_persist_failure` 用有界 shield/drain 完成 FAILED 终态；若提交事实
不能确认，保留原对账语义，不能把潜在成功伪造成失败。重试必须针对最早可修复的原因。

Family/Geometry写入的当前响应保留底层资源诊断，并在附加终态Operation alias时继续传递；
不改变原有持久Operation DTO或发布裁决。已耗尽准入/执行额度和明确不可重试失败不做无变化
内部重试。原process观测记录包含失败诊断或成功时采样峰值；历史缺失的指标不追补为已测。
Operation list/get与其他已列入短引用合同的工具使用同一个Provider注册器和Schema派生投影，
不依赖普通FunctionTool上不存在的私有字段；原Operation查询、结果DTO与持久化职责保持。

普通 FunctionTool 的合法调用在现有 Direct 输入中间件中持有同一个 execution observation scope；
ProviderFunctionTool 继续使用原 `run` owner。两类工具均将该次规范输入摘要、工具名与 correlation
传到 queue/stage/process 观测，不为日志增加业务状态。旧 adapter 自有 monitor 未产生的细分
process phase 指标仍为未知，不能由其他计时替代。

Data Query Attempt 的 Run 级序号互斥使用 PostgreSQL `FOR NO KEY UPDATE`；该临界区不更新 Run
主键，仍串行化序号分配，同时允许不可变 Artifact 子记录的 Run 外键 `KEY SHARE` 检查。
这避免原 Task 锁/Run 锁的交叉外键循环；Artifact、Attempt 的原事务和 RLS 权威保持。

Provider 参数诊断继续以完整 JSON Schema 验证为唯一合法性权威；在多个 union 错误中，
只有请求 literal 判别字段唯一选中完整分支时，才投影该分支的具体字段约束。
无法唯一选中时保留 union 诊断，不隐藏 Domain 验证或另建宽松 Schema。

## 原生证据与能力边界

独立原生技术实验已完成自主Geometry/Family编写、自然Family失败的exact源码修复、模型新旧版本
保持、Family exact绑定、产品自由Query、失败Query修复和Family视野错误恢复。四张同次ImageContent
均有调用间视觉记录，并经父任务视检与新MCP进程exact字节回读。该证据不构成Golden模型忠实度、
全实例支持或制造商从零建模验收。

当前资源版本Geometry Query仍只接受mapped_model；参数化样本及Family结果需在Preview时显式保存，
再用`target_kind=geometry_inspection`和`inspection_ref`进入同一Query/Attempt链。已有结果重新出图
或截面沿Geometry Preview观察入口，不重跑原求解。历史`product_realization_preview` target和
旧capability用法已退出，不能继续作为调用示例。当前合同见[Preview显示与保存](geometry-preview-and-inspection.md)。
草稿及多案例报告扩展见新设计目标，尚未实施。

实现与失败/重跑证据见治理实施记录。
