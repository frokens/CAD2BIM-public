# 作者草稿、Preview 与正式发布

本文描述当前实现的职责。验证范围与尚未完成事项见发布与导航实施核收。设计入口为草稿、Preview 与发布。

## 1. 唯一事实与执行顺序

| 操作 | 保存什么 | 是否执行作者代码 | 是否产生正式版本 |
| --- | --- | --- | --- |
| Geometry / Family Write | 当前根的一个可编辑草稿；完整内容由 Artifact 保存不可变快照 | 否，只检查结构、源码语法、入口和准确依赖 | 否 |
| Preview single / cases，`retain=false` | 不保存新报告、模型或 Operation | 对需要执行的输入运行；观察已有 inspection 不重建 | 否 |
| Preview single / cases，`retain=true` | 一个报告 Operation、逐案结果、成功案例的同次模型引用 | 沿现有 Runtime 运行需要执行的案例 | 否 |
| Geometry Preview inspection / family_standard_model / Query | 读取保存的准确实例或正式标准模型 | 只观察、测量或查询，不重新求解或注册 | 否 |
| `ifc_authoring_publish` | 当前定义及完整 Geometry 依赖闭包的正式版本和发布回执 | 固定 Geometry 与声明的 Family 标准模型沿现有 Runtime 构建；函数 Geometry 发布源码和参数合同 | 是；未变内容复用原版本 |

发布不是 Preview 或 Query 的前置条件。草稿即能 Preview；选择保留后即可凭 `inspection_ref` 查询、截面或换视角。公开 `ifc_products_apply` 使用已发布 Family 的准确版本，沿当前 Core、Operation 和 Writer 执行批量添加与原操作恢复。

## 2. 草稿保存和局部修改

Geometry / Family 根各增加 `draft_artifact_version_id`、`draft_generation`。没有独立草稿表、候选版本表或第二套资源目录。Write 创建或更新完整 Artifact JSON，现有仓储在一个短事务内比较编辑令牌、更新根指针并记录终态 Write Operation。Artifact 上传不代表数据库事务已经提交。

公开 Write 只有 `create`、`update`。创建可使用完整作者内容，也可从有权读取的准确发布版本初始化；SYSTEM / 其他所有者的正式资源初始化到当前可信入口选择的作者所有权，不修改原资产。Direct 和普通 Workflow 为 TASK，服务器已固定的 Component Run 为同 tenant 的 TENANT；所有权不是工具输入。准确发布来源允许省略元数据继承；缺失有效说明时须显式补齐，不自动生成说明。更新传 `draft_ref` 和 `expected_edit_token`；省略的源码、合同、绑定、说明继承当前草稿。准确权限和跨 Task 边界见 [TENANT 作者资源持久化](tenant-authoring-persistence.md)。

Family 草稿保存 Geometry root 或准确 version 身份；公开 `draft_ref` / version alias 由当前调用 Scope 投影，不能把另一 Task 的旧引用作为持久绑定。TENANT 草稿只能绑定同所有权草稿或同 tenant / SYSTEM 正式依赖；当前准确草稿快照由原根、CAS 和 RLS 授权。跨 Task 的未变发布闭包可复用正式资源，每个新请求记录自己的 Operation，不泄露另一 Task 的私有回执。

说明与定义/参数合同在原字段中用 `action=set` 完整写入，更新还可用 `action=edit` 局部修改。说明使用唯一原文片段；Geometry按顶层参数，Family按绑定、参数、组件、片段、接口、约束的业务key增改删。名称、用途等短字段直接替换。源码沿原有界成员机制更新，不引入任意JSON路径修改语言。先合并本次所有关联修改，再校验完整内容；错误不部分提交根指针或编辑令牌。动作包装只属于写入输入，Artifact和读取结果继续保留完整内容，无需迁移或重注册已有版本。具体格式分别见Geometry与Family写入合同。

Write 不接收验证案例、验证场景或标准案例键。保存成功只表示草稿已保存。语法或结构检查不能证明作者代码可以执行。版本号不随草稿更新增长；已发布版本不可变，后续编辑仍发生在同根的草稿中，重新发布才产生新正式版本。原 Write 的重放返回原快照对应的编辑令牌，不能用已前进的当前草稿替换历史输出。

重复提交与当前内容完全相同的 update 返回 `DRAFT_UNCHANGED`，不上传新快照、不增加编辑令牌，也不因此制造新发布版本。

## 3. Preview：执行、出图与保留分别选择

两类 Preview 保留原业务执行 owner。`single` 用于一个输入的观察，`cases` 执行有限代表案例并对比明确预期；二者共享构建能力。单次 Geometry 最多 8 案，Family 最多 16 案；完整代表集合可分批。案例可内联，也可从已有报告读取并有界增删改输入；继承案例输入不自动继承通过结果。

每次开始时冻结准确来源和 Geometry 依赖。Family 固定几何草稿的构建发生在保存型 Preview 的受理/重放检查之后，整批共用同一冻结输入和 Runtime。原操作重放或适用的保存证据复用不重复构建。

single保存为无预期的`observation/not_checked`，可立即用于Query或再次出图。cases声明同源同输入的业务预期，显式复用原`operation_alias/case_key`或`inspection_ref`，服务重新比较预期并保存新报告；适用的原执行不重跑。`from_operation`只继承案例定义，不代替explicit reuse。两类结果都不供给正式发布模型，也不构成发布门禁。公开完整示例分别位于Geometry/Family Preview的tutorial，通用流程只在common/authoring说明，不另建验证入口。

`render` 只控制需要哪些图片；`retain` 控制是否保存报告和成功模型。返回报告区分实际执行状态、实际业务结果、预期比较、覆盖缺口与图片诊断。预期业务拒绝必须匹配稳定原因，技术异常不能视为预期拒绝。

保存型批次只有一个 Operation。成功案例保存整件与必要局部 BREP，以及同次接口、坐标和来源；负例只保存真实结果，没有占位模型。失败或取消保留已经取得的部分证据并标为未完整执行，不冒充通过。渲染失败不删除已保存实体，后续仍可用原引用观察。

一次性 Preview 不创建新持久 Operation/报告/模型；正常结束和失败均释放本次临时实体。使用已有 inspection 时只释放加载副本，原持久资产保留。QueryAttempt 仍只负责查询尝试，不承担注册、发布或第二份形体保存。

## 4. 按定义发布

发布先读取当前草稿，检查编辑令牌，再固定全部 Geometry 绑定，包括当前名义案例未选中的绑定选项。草稿 Geometry 可在同一发布事务内形成正式版本；已经发布且草稿未变的 Geometry 复用准确版本，包括原先随其他 Family 闭包发布的 Geometry。既有发布 Geometry 仍按准确引用授权读取。

公开输入为 `root_kind / draft_ref / expected_edit_token` 及可选 `preview_examples[]`。每项示例以原 Operation 和可选 case_keys 关联匹配的执行实例，未提供、不完整或未通过预期检查均不构成发布门禁。关联核对准确来源及权限；不从报告提取 whole、sample 或 fragment 作为正式资产。案例检查结论与正式版本发布分别表达。

固定 Geometry 沿已有 Runtime 构建其正式局部 BREP；参数化 Geometry 发布程序、参数合同和准确依赖，不强制产生样例 BREP。Family 的 `standard_model.public_parameters` 指定正式标准输入并复用本定义默认值，缺少必填值明确拒绝；标准模型在局部原点以 METRE 构建，不取 Preview 的平移、缩放或名义案例。没有声明标准能力的 Family 不要求标准 BREP。

发布沿现有 Operation 受理、claim、失败/取消/超时及终态恢复。执行在数据库事务外；现有 PostgreSQL Repository 是唯一提交 owner：按固定次序锁草稿根、复核闭包 CAS、插入必要的 Geometry / Family 正式版本和资产、提交发布 Operation / Catalog。失败无部分正式版本。已确定的拒绝保留原失败；业务或闭包 CAS 冲突须按实际原因处理。后台约束或工具故障修复且原操作确为 FAILED 后，新请求身份可提交相同草稿、编辑令牌和可选 Preview 关联，不为取得重试资格任意修改输入；原请求身份仍回原失败。PREPARED 或提交结果未知时先对账原操作，不以新请求绕过未决动作或未修复根因；活执行不会被重复启动。

同一原请求重放返回原终态。新的请求对未变闭包复用准确版本并保存自己的回执；跨 Run 不冒用旧 Run 的数字别名。原请求响应丢失后按原 Operation 恢复，不重新运行或改用最新草稿。可访问的来源用 `operation_ref` 回查；无权限或未记录不伪造来源。正式标准模型可直接由 Geometry Preview / Query 的 `family_standard_model` 分支读取，不依赖作者私有 Preview。

Geometry / Family Info 的 `publication` 导航复用 Operation 目录摘要，只读取原发布操作身份及必要 Alias，不加载完整发布结果。`availability=available` 表示当前 Scope 中存在可访问的来源操作及 Catalog 记录；`operation_get` 独立读取完整业务内容并严格按当前 DTO 校验。Scope、Run 与目录前沿约束继续由原 Repository 负责，来源导航不赋予已退出的业务合同在线使用资格。

Component 资源展示需要定位准确草稿的已保存默认参数案例时，先读取上述目录身份，再按该 Operation 的准确身份读取所需发布闭包，分页查找对应报告；不会为生成目录图片重新建模。

## 5. 当前模块分工与数据库升级

- `AuthoringDraftService` 复用 Artifact、密封引用和 Operation，负责草稿快照及原保存结果恢复；G/F 内容与依赖仍由各自 AuthoringService 校验。
- `GeometryPreviewService`、`ProductRealizationPreviewService` 负责各自执行和观察；`GeometryInspectionService` 保存报告及同次模型。标准构造、预期结果和覆盖规则从旧 Write 移入当前 Preview 的共享规则，没有第二套验证执行器。
- `AuthoringPublicationService` 按定义准备资产、组织准确发布闭包；`repository_authoring_draft` 复用现有仓储事务机制，不是新 Repository 或新的组合根。
- 原 Geometry / Family Repository、Storage、Artifact、Operation、Code / Info、Direct 和 Runner 继续承担原职责；两个入口确定性投影到同一应用合同。

迁移 `20260930_0100` 增加根草稿关联与 CAS/Scope 约束，扩展 Operation / 引用种类，并允许多个 Run 的回执指向同一正式 Family 版本；准确版本外键及正式资产不可变边界保留。没有数据库重建或通用迁移平台。

`20261001_0101` 调整原 Operation 的发布状态/claim 约束与来源引用类型，并在原 QueryAttempt 加入准确 Family 版本外键；无新资源表。旧供料式发布记录保持不可变，但退出当前发布发现/恢复合同，不提供双版在线解释器。

已有符合当前正式合同的 SYSTEM / Task Family 和 Geometry 不需全量重注册、重求解或改版本标签。其标准模型与准确绑定继续有效；未保存的历史构造输入不猜测补齐。旧客户端 Write 执行合同退出在线创建、修复及恢复链；当前脚本、示例和测试直接适配新输入。SYSTEM curator 继续原准确复制职责，不改变普通作者 Scope。

当前数据库单一 head 为 `20261014_0110`，合并 MCP 的 `20261013_0108` 和 Component 的 `20261002_0109` 两条历史。两支各自删除 checkpoint topology 重复字段的迁移仅对该已退休字段作有界顺序协调；不恢复协议资格列或 Geometry / Family 的 `validation_summary_json`。资源删除继续是原根的 `deleted_at` 生命周期变更，正式资产历史不改写。

## 6. 验证和后续实验边界

本轮核收关注保存不执行、按定义发布、失败原子性/恢复、模型复用和来源导航、Scope/RLS与Provider投影；实际完成项以集中清单为准。复用未受影响的历史资产读取测试，不因文案或无关后台变化使所有资源失效。

作者实验另行恢复。恢复时保留已有本人 Family / Geometry，以当前草稿为基础修订；先让作者读取当前 SYSTEM 的准确形体图片及合法输入依据。图片属于形体目标依据，不能替代尺寸、接口、支持域和材料要求；不把另一作者答案或主控解法交给作者。
