# 模型与来源调查

## 准确资源与返回读取

按实际入口的工具声明和资源目录取得合同。Component 用 `state_resource_list/read`；工具已给准确 URI 时可直接读，不凭另一环境的 server 名或包装猜调用。Direct 的身份字段不进入 Runner。引用、资源名和旧 Scope 别名职责不同，只用当前身份可读的准确引用。

逐项保留完整返回，先检查业务 `isError`／协议 `error`，再提取成功字段。对象不再 JSON.parse；只有通道明确是 JSON 的字符串才解析一次。`structuredContent` 缺席不自动表示失败，按声明处理有序 content 块，不把全部 content 强行解成一个对象。资源发现可能返回含 resources 数组的对象，先确认形态再使用。显示、解析或本地保存失败时先复用原返回，不重发其他成功调用。`retryable=false` 不支持同输入盲重试；应按实际诊断修正输入，后台完整性故障保留引用与原错误报告。

| 当前疑问 | 按需合同 |
|---|---|
| 完整产品输入与报告 | `cad2bim://contracts/ifc_products_apply/input` |
| 编辑动作、准确模型依据及结果 | `cad2bim://contracts/ifc_products_edit/input` |
| 当前产品、原批次、组发现与取值 | `cad2bim://contracts/ifc_products_read/usage` |
| 自定义 IFC 调查、模型 API 与只读边界 | `cad2bim://contracts/ifc_query_python/program_api` |
| IFC／多包联查与声明输出 | `cad2bim://contracts/data_derive/python_arrow` |
| 普通 SQL 整理或原动作 repair | `cad2bim://contracts/data_derive/duckdb_sql`、`cad2bim://contracts/data_derive/repair` |

按问题读取实际所需字段与语义，不递归加载全部资料；已读且仍适用的准确合同可复用。

## 当前事实与原批次

`ifc_products_read` 按模式区分原 Apply 快照与同次固定 CURRENT。原 Operation 的输入说明可行域，原报告接口说明该次选定的数值位置、轴和 profile；当前视图读取现在保存的对象与位姿。沿来源、GUID 和接口职责建立关联后比较任务所需关系，尤其在 placement 后重新取得当前坐标，原报告不会随之更新。调查仅覆盖实际读取时刻与范围，不锁定或授权后续写入。

比较对象与指定原批次时，固定 GUID 和准确 `operation_ref`，保持二者对应。可用 `from_ifc` 加指定引用，或从该批 products 根 GUID／components 实体 GUID 精确查找；证据充分时不重复另一条路线。未查询来源不等于不属于批次，不能把指定批次换成对象自己的原批次。

`product_key` 只属于原 Payload，不是全模型身份；异包同名或无同名键都不能证明 GUID 归属。部分页、未覆盖匹配范围的筛选或不可读报告不能证明全批无匹配。对完整来源执行精确 GUID 条件并完整返回零行，可以支持负例，不必搬回全表。根也可能是 components 行；产品数、选定对象数和实体数分开。

沿 Operation 的 Family 与 input_binding 找准确输入，再用 product_key 与来源列回查。键缺失／重复时 input_row_index 可定位诊断关联，但不是原包的物理行读取 API。来源不足时按必要范围追查已有 Data，而不是凭名称、类别或最近坐标制造确定关联；跨对象处理见[安装管理](installation-management.md)。

## 属性、单位与关系

保留原 scalar／values 及实际单位。SI 的 name 是基础 Name，symbol 包含适用前缀，结合解释，不能仅因二者拼写不同判为冲突。明确米制的世界 placement 字段另行解释；单位未知或不支持时不猜倍率。

区分属性缺席、存在但显式 null、有效值，保留实例／Type 来源与实际单位。按任务继承优先级和实际值换算、筛选；不能把缺席等同 null、忽略 Type 候选，或先猜固定名称清单再用预期数量断言代替筛选。

命名 Family 接口、几何安装关系和实际 IfcPort／连接分开。真实连接不自动证明流向或全网可达；没有端口也不抹去已有几何证据。调查更复杂关系时使用下面的只读代码入口。

## IFC 只读代码与 Data 派生

`ifc_query_python` 的 `ctx.model()` 是后台按准确快照加载的真实私有 IfcOpenShell 对象，可用正常读取 API 调查属性、材料、位姿、空间／系统和连接。不得修改／创建／删除实体、控制事务、打开或保存文件、调用 model.write／任意导出。修改后还原仍非只读。按源码检查和运行期诊断纠正代码，不换写法绕过审计。普通局部 dict/list 聚合、set.add／list.remove 可用，具体导入及能力以所选 Resource 为准。

Query 返回有界 JSON 原语及列表，不返回模型、实体或整份序列化 IFC。fresh 固定一次 CURRENT，repair 继承父 Attempt 的准确 IFC 与 Package；需要最新模型重新 fresh。page_size 只限制结果，不减少读取初始化成本。避免为统计文件大小序列化全模型；Apply 原修订大小用提交元数据，当前模型事实用 ifc_model_info，报告统计走 Data。依赖同一修订的断言可合并，返回聚合及有界异常。

需要持久结果或安装输入时用 `data_derive`，沿现有 Package／Payload／Schema／逻辑键组织。普通 SQL／Python 显式绑定准确输入名和 Payload，多包可联查；Python 显式 `ifc_source="current"` 才可不带 Package 并读 `ctx.model()`。程序 `transform(ctx)` 只用 ctx.write_table/write_json 提交声明输出，不修改 IFC 或保存文件。来源键、一对多映射和必要变更依据保留在输出中，模型安装调整不覆写原始输入。

源关系、允许窗口、本次求解接口和当前 Body 观察分别保留来源与读取身份。把候选、已证、歧义和未检查分开保存，后续计划直接消费已有证据包；完整路径及共享设备的组织示例见[安装管理](installation-management.md#把调查结果接到可执行计划)。后续修改只触发受影响范围的补读，不把每次维护变成全网重新关联。

create 固定一次 CURRENT；repair 继承原 create 来源，新 repair 接纳／发布发现 CURRENT 改变时拒绝且不发布包，需重新 create 调查当前模型。已完成原动作重放恢复原 Operation／Package，不重复执行。仅分析历史 Package 而未直接声明 IFC 时不要求 CURRENT 相同。系统准确来源不由业务 JSON 覆盖，revision_no 是来源说明而非模型选择器。提取包只证明当时事实，后续写工具仍按自身合同核查当前对象、依赖和并发条件。
