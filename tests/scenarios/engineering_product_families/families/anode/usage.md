# ANODE 五部件产品族

本族使用最终 Golden06 已选的五个固定尺寸工程目标 Geometry：`cast_body`、`core`、`exit_seal`、`lead_jacket` 和 `lead_conductor`；这些尺寸不是制造商证明。五个 fragment 分别属于五个组件，按一个刚体组装；不会为匹配宿主管外径缩放主体或拉长引线。导体的真实自由端在主体局部 `(0.337, 0, -0.105)m`，局部端面轴为 `-Z`。`business_attachment` 只引用同次 `lead_conductor.free_lead_tip`，全族没有 GAS 口。

## 输入与定位

`host_a_m`、`host_b_m` 是同一条真实有限宿主管段的 A、B 世界坐标，`host_outer_diameter_m` 是其外径。宿主管轴按输入 A→B 定向，`product_origin` 是独立产品根 O。

`attachment_station_m` 是从 A 沿输入 A→B 方向量取的米制附着距离，数值 0 表示 A 端。站位必须在有限段内；A=B、非正外径、越界站位和非有限输入均拒绝。

Family 将 A→B 归一化为 `u`，世界 Z 投影到 `u` 的法平面得到外向 `n`；竖管退化时用世界 X。目标点为 `Q=A+station·u+(OD/2)·n`。旋转矩阵将局部 X 映射到 `u`、局部 Z 映射到 `n`；全部五部件共用该旋转，局部组装平移保持原比例。主体平移为 `Q−R·(0.337,0,−0.105)−O`，后端仅将根 O 应用一次。因此真实末端到宿主管外表面，末端轴 `−n` 指向管体。

`standard_model` 与 `realize` 使用同一输入、同一五部件布局；前者保留标准模型局部构造，并由产品根处理 O。已批准的 `standard_nominal` 与 `standard_placed` 使用完全相同的公开参数，后者只改变 O。

## 验证与边界

从 exact Family 和五个 exact Geometry 读取合同、空参数 Schema、真实接口与源码身份。通过正式 `execute_family_program` 求完整 proposal；数值拒绝时不调用 Geometry。成功时由后端按五个空参数 binding 构建各自实体，独立重开并核五个有效单实体、相对装配、真实导体端面与 world `business_attachment`。若另有独立目标点和方向，可给 self_geometry 的 `business_attachment` 提供标准 `condition.kind=fixed_point` 条件，填写 `point_m`、`axis`、`direction_relation=same`，不要求 GAS 外径；实际宿主管轴线、外径和有限站位仍须与同次实际接口做几何关系核验。

本族只给出自身五部件及业务附着位置。点位、朝向或几何相触不证明电气连续、制造接触、材料性能或宿主管修改；IFC 安装与宿主管调整由后续正式写链决定。
