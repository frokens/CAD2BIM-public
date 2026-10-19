# 产品编辑

已有物理产品用 `ifc_products_edit`。先按需读 `cad2bim://contracts/ifc_products_edit/input`，沿 Data Package 的完整 Parquet payload 组织 product_key、target_global_id 与 action。GUID 来自实际读取，不按名称、坐标或 STEP 数字 ID 定位；不要求旧 Family 登记或原 Apply 来源。选中内部组件只影响其实际所属子树，不自动扩大到父根。

## 动作与业务依据

- replace：每行选准确已发布 Family，提供完整公开参数、外部条件及米制安装原点。保留目标根 GUID，新 Family 决定类别和内部结构；旧子件退出，新子件有新 GUID，同名 component_key 不表示身份沿用。
- placement：设置绝对 project-world 位姿，origin 为米、axis 为局部 Z、ref_direction 为局部 X。实际所属子树保持内部相对位姿随动；仅有 placement 依赖的外部对象保持世界位姿。相对移动先计算为明确绝对目标。
- delete：删除准确目标与已核定归属的子树，摘除其实际关系，保护父／兄弟、共享定义及连接对端；不自动重连。

选择动作前按[安装管理](installation-management.md)确认跨对象关联和保留范围。删除、缩短或拆分必须有业务依据，不能靠移除失败目标伪装交付。新增残段用合法新输入添加，保留原源到各段的映射；不要将技术失败包装成符号形体替代。

replace 的声明约束求值处理本次接口条件，placement 改变所选对象及其所属子树的保存位姿。随后按实际变化复核对端及受影响关系；原报告保持历史坐标，尤其在 placement 后从当前保存对象重新取得坐标，不能沿用旧接口位置判断衔接。

多 Family 输入按每个业务动作映射目标 Family，不能由目标原 IFC 类别推断替换族。原样复用已成功读取合同的准确引用；引用失败先纠正，不继续生成依赖包。核对必要目标、action 和 Family 映射与给定设计；已实际读取的列可复用，缺少时沿 Data 有界读取。普通 placement/delete 不增加 Family 核查，也不重做后台参数或求解验证。

## 模型依据与影响范围

优先使用输入 Package 的系统 IFC 来源；不能唯一确定目标模型时，原样传当前读取的 source_revision.revision_ref 作 base_revision_ref，两者同时存在须一致。revision_no 只说明模型局部修订，历史 Apply 摘要不是当前编辑依据。冲突后重读受影响事实与依赖，不只换 ref 盲重试；未读 IFC 的历史视图可以没有 source_revision。

同批重复目标、祖先／后代或闭包重叠整批拒绝，没有隐式执行顺序。replace/placement 遇跨子树真实连接会阻断；Family 几何外部条件不等于 IFC 网络。类型、材料、Pset、空间／系统分配及网络修改不由此入口开放。replace 解开旧 Type 可能改变继承元数据，保留的普通 Pset 不承诺重算，旧几何数量不能当新结果。

确定批次前，沿[持久证据与路径计划](installation-management.md#把调查结果接到可执行计划)核同一原路径上的全部关联及共享设备另一侧。未检查对象不能归为无需修改；已知站位冲突与关联歧义分别处置。编辑后的目标映射和检查结论接回原计划，只补受本次变化影响的事实。

## 结果与读回

成功 primary_result 含原报告、全批统计及原提交修订；失败可无报告，未决没有假 summary 或零删除。按需读 products 原行状态、changes 新旧身份及关系影响、checks 诊断；interfaces 仅在 replace 实际观察时存在。输入行数与新增／删除产品身份数分开，placement/delete 不伪造 Family 求解成功。diagnostics.complete 只表示记录展示完整，不表示全部输入已检查。

当前事实通过 `ifc_products_read from_ifc` 查实际 GUID，不将 Edit 引用传进 Apply 专属 from_apply。核查保存后的实际位姿、形体、受影响依赖与来源覆盖，深度服从任务与实际变化。中断及原操作恢复统一见[结果与恢复](results-and-recovery.md)。
