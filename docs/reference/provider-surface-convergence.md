# I0-B3.04R Provider公开面与共享机制收敛as-built合同

```text
status: committed
verification: verified_current
implemented_slice: I0-B3.04R
baseline_commit: d30fec35c7da1f5077c6ae6a640af607ead7f363
database_head: 20260903_0070
direct_public_tools: 37
ifc_prefixed_tools: 16
preview_resource_templates: 1
provider_projection_sha256: 265932d9ef1329a4aa45b1d6e11c9daaad140c5e93eee1117d736579e4711659
superseded_current_read_projection: I0-B3.04C_code_list_code_read
current_direct_public_tools: 35
current_database_head: 20260904_0072
```

本切片在I0-B3.04与I0-B3.05之间收敛重复的Provider入口和已由多个真实消费者证明通用的
技术机制。它延续使用现有Scope/RLS、Artifact/ArtifactVersion、Storage、CriticalOperation、
Geometry Runtime、Geometry/IFC QueryAttempt、IFC revision reader/writer和CURRENT CAS，没有创建平行
Repository、影子DTO、第二事务所有者或第二事实源。

本文的37工具及`operation_source_read/query_attempt_source_read`是`.04R`提交时的真实快照。
当前公开源码发现/读取面已经由I0-B3.04C进一步收敛为`code_list/code_read`；`.04R`中的领域事实、
Repository和读取逻辑继续作为该投影的owner-backed实现，不保留旧Provider alias。

## 1. 公开面

### 当前公开投影补充

公共合同共享实现由`provider_schema`生成派生投影、`tool_support`发布、Direct组合根完成同次注册。
正式面为39工具、18静态resource和1个Preview模板；新增
`cad2bim://contracts/common/types`与`cad2bim://contracts/common/usage`。原工具及业务输入输出保持。
公共类型来自原DTO生成的完整Schema，按类型身份及传递定义图比较；生成模式产生的实际差异保持分离。
局部外壳/根约束与公共定义共同构成闭包，公共页内递归和discriminator引用保持；不处理业务default/examples数据。
内部`_reference_documents`继续使用原自包含Schema，短引用/Scope处理不依赖静态资源。
程序读取消费者读取公共页后使用仅含已读URI的Registry校验；Agent从工具导航和资源描述获取公共URI并复用
会话内仍适用的完整副本。该路径已提供进程内合同验证入口，原生Agent读取行为和模型效率未验证。
最终input声明通过FastMCP公开构造参数注册的外层middleware投影：先完成默认解引用，再在返回工具
副本的parameters内共享完整相同的Schema节点。`provider_schema.share_provider_input_nodes`是唯一
变换owner，包含描述、默认值及全部约束的完整比较；只接受计入定义名和引用成本后缩小的结果。
不修改注册工具、DTO、短引用路径、output或资源。已有引用/作用域、discriminator、未知对象/数组
关键字及旧式tuple items保持原合同；业务default/examples/enum/const和同名字段不作为Schema遍历。
最终MCP声明及SDK JSON往返保留工具内引用，39份输入展开与前一接收快照精确一致。
原生模型投影仍未观察；未来LangChain工具转换可能再次展开，当前静态减量不代表该链路或模型收益。
本段是实现说明，独立接收状态以active治理任务记录为准。下述16份资源描述是本次共享之前的快照。

本小节记录当前执行树的公开合同增量；下文`.04R`的工具数、迁移和测试仍属于原切片。
本批资源拆分后的静态库存为16份合同资源及1个Preview资源模板；工具数量不在本批重新计量。
Code、Source/Catalog、Operation和Geometry/Family均由Direct边界显式投影公开字段；
Backend Domain继续保存并校验内部摘要、运行预算、完整写入证据。业务JSON和inline table不递归删字段。

拆分前B4快照中，普通工具的38份独立`outputSchema`按既有Provider路径生成；两个多模态Preview没有独立
`outputSchema`，其公开结果Schema分别在既有`/result`静态资源中，以局部`$defs/$ref`
保留完整引用闭包。计量中这两份正文只计入Resource，不补造独立Output数值。
相对原14个URI，新增资源为`cad2bim://contracts/ifc_product_family_write/tutorial`和
`cad2bim://contracts/ifc_product_family_write/repair_validation_scenarios_json`；其他URI保留。
这是B4当时的资源清单；当前`repair_attempt`与`create`、`revise_version`共用完整
`definition_json`，专用`repair_validation_scenarios_json`资源已从现行注册表移除。
Data Query程序资料使用真实MCP结果字段导航，避免复制整个工具结果Schema。

当前结果资料已替换上述历史输出Schema形态：Geometry/Family Preview保留既有`/result` URI，正文改为核心概念与关键字段导读，共同语义引用`common/preview`。不再向Agent展开完整结果`$defs`；共享类型收集仅从仍需精确构造的输入和作者程序合同出发，结果专用类型随之退出公共类型页。内部DTO、结果校验、引用投影及实际响应不变，测试需要机器Schema时从权威DTO生成，不把人读资料当校验器。

精确合同见[Code/Data投影](code-discovery-data-query-and-transform.md)、
[Preview](geometry-preview-and-inspection.md)和[Family写入](product-family-write-and-realization-preview.md)。
拆分前15份资源的B4实际注册证据位于
B4注册快照；
当前第16份资源是上述repair专用固定URI，不把旧快照表述为拆分后的注册证据。
节点3及启动冻结已接收，五组fresh Luna/max完成约定消费者并获独立接收；逐工具裁决、
15资源的静态/读取/实际分支覆盖、过程限制及提交状态见本轮审计。
B4静态40工具不等于40工具全部原生运行；standard_model Preview资源及暂停产品候选不计为该轮业务通过。

Geometry Authoring执行历史由现有Operation族统一承担：

- `operation_list`增加可选精确`operation_kind`过滤；过滤由现有Repository在keyset分页前执行；
- `operation_get`对`geometry_resource_write`返回父Operation、bundle SHA、源码成员、有界诊断、repair条件
  和成功资源安全alias；
- `operation_source_read`在当前Scope内对Geometry写Operation的exact UTF-8源码成员执行
  `full | lines | search`，同时返回whole-bundle SHA和member SHA。

本切片将Geometry写入统一为`ifc_geometry_resource_write`，当时Provider有三个完整、封闭的叶分支：

1. `mode=create + resource_kind=mapped_model`；
2. `mode=create + resource_kind=modeling_function`；
3. `mode=repair`，提交父Operation alias、expected whole-bundle SHA和有界exact edits。

三个分支确定性投影到原`GeometryAuthoringService`及其唯一Runtime/Artifact/Operation/Version事务链，
没有重写create、repair、技术重试、响应丢失对账或immutable Version追加逻辑。

后续固定模型接口能力在同一工具增加第四个`annotate_interfaces`分支，继续复用Operation与版本发布，
但不运行Runtime或生成Code；当前精确合同见[模型接口](geometry-model-interfaces.md)。
函数合同演进使用同一工具的`revise_function`封闭分支，复用exact parent/source、函数参数验证、
Operation及版本发布；参数Schema和样例可以更新，几何依赖与root继承不变，见
[Geometry Authoring](geometry-resource-authoring.md#41-同root函数合同修订)。

Geometry和IFC QueryAttempt源码统一由`query_attempt_source_read`入口路由：

- `target.kind=geometry_resource_query`解析GeometryQueryAttempt；
- `target.kind=ifc_model_query`解析IfcQueryAttempt。

入口只统一Provider的source-view语汇和路由。两类QueryAttempt继续使用各自的Domain、表、Repository、
RLS、exact资源绑定、Worker权限、cursor和reconcile语义。

退出的7个旧公开名称不再注册，也没有保留转发alias：

```text
ifc_geometry_authoring_list
ifc_geometry_authoring_info
ifc_geometry_authoring_source_read
ifc_geometry_resource_create
ifc_geometry_resource_repair
ifc_geometry_query_source_read
ifc_query_source_read
```

## 2. Operation历史与migration

Alembic `20260903_0070_geometry_resource_write_tool_name.py`只替换现有
`critical_operations` detail CHECK：

- 新Geometry写Operation如实保存`tool_name=ifc_geometry_resource_write`；
- 新行仍使用现有typed detail和`repair_parent_operation_id`区分create/repair；
- 旧`ifc_geometry_resource_create`/`ifc_geometry_resource_repair`行保持原值；
- migration不执行历史数据`UPDATE`，不新建表、Repository、RLS或事务边界；
- 新旧三种`tool_name`均投影为同一`geometry_resource_write`业务种类；
- 旧成功/失败Operation继续可由`operation_get/source_read`读取，并可作为新repair的父Operation；
- 存在新名Operation时，downgrade在改变约束前fail closed。

## 3. 共享机制与保留的领域边界

本轮新增的中性模块只解决已证实的技术重复：

- `src/CAD2BIM_mcp/direct/tool_support.py`：FastMCP Provider注册、Schema和验证错误投影；
- `src/CAD2BIM_mcp/direct/source_view.py`：Provider source-view到现有Domain/Application text-source view的唯一转换；
- `src/CAD2BIM_backend/application/exact_artifact_reader.py`：领域先解析exact绑定后，统一复核
  owner/Task/Run、Artifact/Version、MIME、size、metadata SHA和实际字节SHA；
- `src/CAD2BIM_mcp/direct/directory_budget.py`：Package和Operation目录共享128 KiB响应预算和
  keyset continuation投影。

下列候选没有抽取：QueryAttempt source/result Artifact持久化、canonical JSON pager、cursor claims、
terminal reconcile。它们在Geometry Query和IFC Query中的资源绑定、预算、Worker权限和恢复语义不同，
继续领域内实现可以避免超级Query Service和第二事实源。

## 4. 不变量

- IFC revision、IfcModel CURRENT和writer CAS没有改动；
- GeometryResource/immutable Version/exact VersionAsset仍是唯一几何资源权威；
- CriticalOperation仍是Geometry写入执行历史和响应丢失对账权威；
- GeometryQueryAttempt和IfcQueryAttempt仍是各自查询源码、exact输入、结果、cursor和repair的权威；
- Geometry Preview仍只渲染一次、保存一份Preview Artifact，同份PNG投影为内联ImageContent和ResourceLink；
- Provider不获得ArtifactVersion ID、Storage路径、数据库ID、writer、内部Scope或revision选择权。

## 5. 验证证据

当前收口门禁覆盖：

- Provider递归Schema、37工具唯一注册顺序、投影摘要和1个preview ResourceTemplate；
- mapped/function create、失败诊断、新旧父Operation repair、whole-bundle CAS、幂等重放和响应丢失对账；
- `operation_list` kind过滤、keyset分页、详情投影和exact源码`full/lines/search`；
- Geometry/IFC两类QueryAttempt的统一source route、exact SHA、Scope/RLS和repair基线；
- exact Artifact reader的字节完整性、MIME、size、Scope和跨Task拒绝；
- 真实PostgreSQL fresh migration、`0069 ↔ 0070`往返、新旧CHECK、RLS、重启回读和已有新行降级fail closed；
- 当前默认允许测试`967 passed, 55 deselected`，以及Ruff、wheel和架构/as-built一致性审计。

此切片没有运行Docker，没有恢复退休Agent/IFC smoke，没有扩展ProductFamily、产品实例、Host edit或
`ifc_extract_to_package`。I0-B3.04C已经在本合同之上完成统一Code读取投影、DataQueryAttempt、
Data Transform入口和Preview render前缓存；当前as-built见
[`code-discovery-data-query-and-transform.md`](code-discovery-data-query-and-transform.md)。本文件继续记录
B3.04R提交时的37工具与`0070`事实，不将后续状态倒写为该切片原始结果。
