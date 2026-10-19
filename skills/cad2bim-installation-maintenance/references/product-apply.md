# 产品添加

本页消费准确已发布 Family，不要求重新创作 Geometry／Family。字段和输出以当前工具 Schema 与 `cad2bim://contracts/ifc_products_apply/input` 为准，从实际入口读取。

## 准备与批量组织

1. 通过 Family list/info 选择准确已发布版本，读取相关参数、单位、支持域与命名接口合同。同族多个实例复用准确合同，不逐实例重复发现或 Preview。准确引用原样使用，不拼接、截短或改写；必要合同读取失败时先纠正，再生成依赖它的输入。
2. 检查已有 Package 说明、Payload Schema、逻辑键及必要样例。已符合合同的包直接使用；需要协调设计或整理时用 `data_derive` 新建包，保留原来源键与一对多关系，不覆盖原包。调查与派生见[模型与来源调查](model-investigation.md)。
3. 一个 Apply 使用一个 Family、一个 Package 内的完整 Parquet Payload。多族按族组织，同一 IFC 的写调用按实际依赖顺序执行。任务要求全部目标时，不为避开失败静默筛掉行；源覆盖和安装关系见[安装管理](installation-management.md)。
4. 先读 `ifc_model_info`，确认目标 IFC 的 `geometry_basis_configured` 与 `geometry_basis`；按需用 `ifc_representation_context_list` 查看 Context。Apply 需要项目长度／平面角单位、唯一三维 Model 父 Context，以及其下唯一 Body 子 Context（Model/MODEL_VIEW）。仅缺配置时才用 `ifc_model_configure_geometry_basis` 补齐，复用已合法的单位和 Context。Body 合法但 geometry_basis 为空时仍需检查项目单位与主 Context，不将缺单位误判为 Body 不合法。带 `_m` 的位置始终为米，其他参数及条件遵循各自合同。

先做能避免整批失败的便宜检查，例如键唯一、必需列类型、来源范围和明显缺失参数。后台负责参数、求解、接口和几何合同验证；不在 Agent 侧逐族重写完整 SQL 校验器，也不强制逐行 Preview 或 Apply。跨对象安装协调仍属任务职责，不能因复用后台检查而省略；有具体诊断时再查询对应字段。

依赖允许且已有成本依据时，可将较小增量先写、明显增大模型的批次后置，减少反复处理大基底的机会。不为排序额外构造、不改变目标集合或原子边界，实际效率收益须测量。

## 保存与后续关系

沿本次结果区分求解、声明约束求值、构造与提交，PASS／FAIL 见检查结果。Apply 的 interface_constraints_evaluated_product_count 按完成求值的产品行累计并包含 FAIL。成功终态表示请求范围内产品已提交；严格失败为本批零提交。结果不明沿原 Operation 恢复。

Apply 创建请求产品及构件的 Body／Placement。有限段给出可行域，命名接口给出选定数值站位；安装任务据来源关联比较受影响的当前保存对象，再按实际需要派生待写管道输入或编辑已有对象。保存后核查受影响关系，保持单位、世界坐标、方向、截面及原路径来源。报告、核查和恢复集中见[结果与恢复](results-and-recovery.md)。
