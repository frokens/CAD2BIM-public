# 结果与恢复

## 报告与覆盖

先用 operation.primary_result.report_tables 的实际 payload 名称、行数和逻辑键选择所需表；字段结构不明时再读 Data Schema。detail.diagnostics.items[].check_index 定位原 checks，保留原行、业务键和实际测得的 metric／residual／tolerance／unit；空值不表示测量通过。detail.diagnostics.complete 只说明已记录错误／警告是否全部展示，false 时沿原 checks 有界读取，不重复写入来取得诊断。

Apply 的 products 给根 ID、逐行求解／构建／提交状态；components 给构件 GUID 与父子关系；interfaces 给本次数值命名接口的位置、轴和 profile；checks 给声明约束的实际值、残差及结果。接口求值、构造和提交各自表达已完成的工作，跨对象关系按[安装管理](installation-management.md)结合当前保存对象比较。报告接口坐标保持原执行快照。

interfaces.component_global_id 直接关联本次已提交的实际组件；未提交观察为 null。Apply 按 product_key／component_key，Edit 再保留 input_row_index 关联。replace 的所选根可保留 GUID，新子件使用本次实际新 GUID；不凭 component_key 沿用旧子件身份。placement／delete 没有新的替换接口观察时不造行，也不把该列当作 IfcPort 或端面测量结果。

Apply 摘要的 interface_constraints_evaluated_product_count 按返回 SOLVED 且完成约束求值的产品行计数，包含约束 FAIL；它既不是通过数量，也不是接口或端口数量。原严格失败仍为本批零提交。Edit 按自身 products、changes、checks 和实际观察到的 interfaces 解释，placement／delete 沿原动作范围记录。

先看摘要，再沿 Data 做覆盖聚合与异常筛选。首次或结构不清时读报告 Schema，已掌握且未改变的表可复用查询范式，不逐包重复 describe。原始输入逻辑键、Operation、准确报告包和源映射保持关联；根可能也在 components 中，实际实体数按非空 GUID 去重，不把产品数与构件行数相加。

Apply products 可按 solve_status／build_status／commit_status 分组，Edit products 按 precheck_status／solve_status／execution_status／commit_status 分组；Edit 没有 build_status。按 phase/status/error_code 分组 checks，再取有界异常键、field_path、residual 与 tolerance。缺失／重复业务键用 input_row_index 诊断关联。必要参数比较遵循准确 Family 的类型、单位、默认值和容差，不用 JSON 字符串或 json_contains 充当数值等价；0 与 0.0 可以等值。未比较、未执行和不适用分别表达，不造恒零异常结果。

输入完整覆盖不等于结果完整，结果行数相等也不证明来源一致。以原源目标追查当前保留、变更、拆分／退出及未决对象，按[安装管理](installation-management.md)说明依据和多对象关系；失败／未决不能借“已处置”标签变成完成。合法新增身份、删除缺席和未变对象保护按实际保存事实核查。

## 保存事实与核查范围

成功 operation.primary_result.committed_revision 的 revision_no／size_bytes 是该原 Operation 的准确已提交版本和字节数；apply、operation_get、recover 保持原事实。后续写入后它不表示最新 CURRENT，revision_no 不是跨模型全局键；最新模型读 ifc_model_info，不为逐批大小序列化 IFC。

复用后台单产品验证，另行核查任务所需跨对象适配。Body／Placement 非空只证明存在，不等于实际位置／方向、端面或衔接已核对。设计条件、报告接口、当前形体和真实网络拓扑分别说明。受后续操作影响的事实必须重验；未受影响且证据适用的内容可复用。

依赖允许时合并累积写入后的必要 GUID、类别、位姿、形体与层级检查；遇实际依赖或异常再提前针对性调查，不机械逐族全模读取，也不因合并而删掉必需断言。检查深度和代表样本须对应实际目标、修改与风险，不把局部读回称为全量实体或碰撞验证。真实模型的只读与 Data 派生边界见[模型与来源调查](model-investigation.md)。

## 最终交付与业务缺口

最终答复须主动向用户说明交付范围内的业务缺口，不能只写入 Package、内部日志或交接。按任务复杂度用简短段落或表格交代：受影响的源对象／关系及业务影响，已完成与未完成部分，实际尝试和证据，未完成原因及其确定程度，当前保留／保存状态，以及下一步具体最小方案或需要用户决定的设计条件。相关的范围外对端或未选入来源关系另作边界说明，不据此扩大交付目标或宣称已安装。模型、Operation 和 Package 引用应能接回已有结果与原失败，不要求另一套固定输出 Schema。

区分当前方案失败、支持域／硬约束冲突与调查或证据不足，不把有限尝试失败说成全域无解，也不以部分成功代替完整交付。已接收的设计例外仍逐项列出，说明过程交付状态和该关系尚未接合的实际结果；未验证事项保持未验证。

下一步沿已有授权提出可执行选项，不为每个未决重复索取同项授权。确需改变设计条件或超出授权时，明确指出具体决定及影响；规划和例外处理见[安装管理](installation-management.md#设计受限与交付例外)。

## 中断与原操作

超时、断开、报告不可用或 retryable=false 都不能证明零提交，不能立即同输入新发 Apply／Edit。

- 已有 operation_ref：先 operation_get，明确成功则复用原报告，明确失败则保留原诊断。
- 引用丢失：用 operation_list 的 ifc_products_recovery 分支，指定原 tool_name、分页并匹配时间及准确输入；不是最新一条或同输入就一定是原操作。
- 原操作未终结、需接续／对账：用同一工具 recover，仅传原 operation_ref。后台保留原请求、claim、期限和候选，不换输入或重置预算。
- 已提交后回读／投影失败：保留提交事实及引用，报告技术问题，不新写来尝试修复。
- 无法确认原身份或状态：报告不确定性，不猜成功、失败或零提交。

恢复不是撤销，不保证从某实例续算；输入修正使用新 Package／新操作。严格失败为原批零提交，修正或有依据地重组后须覆盖全部尚未完成目标，不只重发容易成功的子集。正常成功无需例行 recover。

若任务明确要求恢复验收，固定本次成功工具名与原 operation_ref，走同一工具 recover 核对原操作、报告及修订。不能恢复历史 Apply 代替本次 Edit；依赖允许时安排在最终合并 IFC 核查前，保留不重复写入、身份与持久化断言，避免重复相同全模查询。
