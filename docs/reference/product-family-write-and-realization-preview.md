# ProductFamily 草稿写入与 Product Realization Preview

Write 保存 Family 草稿，Preview 执行实际求解；正式版本仅由 ifc_authoring_publish 产生。共同生命周期、依赖闭包固定和恢复边界见[草稿、Preview 与发布](authoring-drafts-preview-and-publication.md)。本文记录当前入口合同。

外部目标的三种位置、方向与 profile 语义见[自身几何合同](product-family-self-geometry.md#产品原点与内外部约束)。当前阶段入口与验收状态由职责索引维护；未受影响证据保持其原输入和范围。

## Family Write

Direct 每次提供 direct_session_alias，只有 create/update 两种模式。

作者对需要检查的连接，应以 `model_interface` 引用对应 Geometry 同次构建的实际接口，并在
Family 定义中声明连接约束。适配外部管道或设备时，还需声明适用的 `external_constraint`，
并在 `self_geometry` 调用中提供对应外部条件。后台按这些声明检查；未声明的连接不会自动
推断或检查。接口坐标与轮廓检查不替代实体接触、通孔或真实安装检查。

外部条件以 `interface_key` 绑定 Family external 接口，再投影到同次 Geometry receipt。Family 的 `external_constraint.allowed_kinds` 声明支持 `finite_segment`、`fixed_point`、`infinite_axis` 的哪些类型；`required`、方向、profile、可选 roll 及最大容差保持原职责。public_parameters 承载 Family 声明的有类型计算与布局资料，须说明单位、来源与用途；external_constraints 独立断言实际输出接口的关系。三种位置均显式给 direction_relation，OD 均可选但受 Family 最低要求约束；给出的 OD 始终比较绑定输出接口的圆形截面。fixed_point 的 none 分支无 axis/reference_direction，有向分支要求 axis。准确字段与封闭分支以当前公开 Resource/Schema 为准。

| 模式 | 必填输入 | 声明 |
| --- | --- | --- |
| create + inline | display_name、purpose、usage_instructions、source | usage 使用 action=set/value；source 给 source_files 和 set-only definition_json |
| create + published | source | 给 exact family_version_alias；name、purpose、usage 省略时继承所选 bundle.version，显式 usage 覆盖只允许 action=set |
| update | draft_ref、expected_edit_token，至少一项改动 | 可改 metadata、source_changes_json、usage 或 definition_json；大字段内 set/edit，省略继承，显式 null 拒绝 |

definition_json 只编码一次，完整写入为 `{"action":"set","value":{"family_contract":...,"geometry_bindings":[...]}}`，update 同一字段允许 `action=edit`。正式 ProductFamilyContract 的业务声明保持原合同；每个 Geometry binding 通过 source_kind=draft,draft_ref 或 source_kind=published,resource_version_alias 选择来源，并给 binding_key、资源种类及用途。Write 不接受 validation_scenarios 或 nominal selector。

definition 的 edit 按业务条目更新：geometry_bindings 按 binding_key；family_contract 的 public_parameters/solved_parameters、components、geometry_fragments、interfaces、constraints 分别按 parameter_key、component_key、fragment_key、interface_key、constraint_key。每个集合用 set 提交现有业务 DTO 的完整条目，用 remove 提交已存在 key。相同 key 替换且保留原位置，新 key 按提交顺序追加；重复 key、同次 set/remove 冲突和删除不存在条目均拒绝。较深的 binding_options/parameter_sources 随完整 fragment 条目替换。standard_model 用 `{"action":"set","value":...}` 或 `{"action":"clear"}`；product_structure_kind 直接给新标量。没有自动级联改名。

全部修改在内存合并后一次检查完整参数引用、组件树、fragment/interface/constraint 和准确 Geometry 绑定。因此参数、绑定及映射可同次提交；悬空引用或准确资源种类不符时整次失败。最终 Family contract 保留 64 KiB 预算，完整定义保留 8 MiB UTF-8 预算；Direct JSON 动作外壳有额外 128 KiB 有界空间，不占用最终内容容量。

Family业务声明和Realization程序按当前业务结构处理。每个binding option的parameter_sources
使用default_source与overrides对象；Write按准确Geometry参数Schema展开、正规化并冻结完整overrides后，
才比较DRAFT_UNCHANGED或保存。显式override优先；default_source只引用指定public/solved命名空间中的
同名顶层参数，缺名拒绝。没有default_source时，仅合法Geometry默认值可展开为fixed。
源码或usage单独更新直接继承已保存的展开结果，不重新读取未改的Geometry静态依赖。
array.items和tagged_object.branches为匿名值合同；number_array是精确长度数值数组的简写，
类型、单位、范围及树预算继续由原Family normalizer核验。详细规则见[自身几何合同](product-family-self-geometry.md#合同与参数)。

源码成员为 `{source_path,source}`，完整 bundle 含 main.py/realize(ctx)。source_changes_json 是包含 changes[] 的封闭JSON对象，支持 literal_edit/add_member/replace_member/delete_member；expected_edit_token 已固定整个草稿 generation，替换或删除成员不再要求回填成员 SHA。准确父源码字节校验、目标存在性及 literal_edit 的唯一旧文本匹配仍保留。无需重复未改的定义或源码。

完整Family源码沿现有领域预算允许最多64个成员，草稿层不另设16成员限制；创建与更新后的完整内容都遵守成员及总字节预算。`expected_edit_token`是最新Write/Info的正整数并发令牌，不是正式版本或SHA。usage_instructions 完整写入用 action=set/value，update 局部修改用 action=edit/edits，元素复用 old_text/new_text。每个锚点相对同一修改前原文唯一匹配，未命中、重复命中或重叠拒绝并给出从1开始的编辑序号；删除 new_text 为空，添加在替换文本中保留锚点。最终说明必须非空、无 NUL，并遵守 8192 字符/32768 UTF-8 字节预算。所选发布版本没有有效说明时必须显式 set 补齐，不合成默认说明、不改读 latest。源码/接口/支持域改变后仍须核对说明准确性；写作指引由`common/usage#version_usage`按需提供。

模型说明只补充 Schema 和准确合同不能直接表达的使用语义，例如参数改变如何影响部件与装配、局部锚点或接口的实际含义、调用者需要知道的支持与证据边界；不重列参数类型、单位、required/default/bounds、绑定或通用工具步骤。公开创建示例的说明按这一职责书写。

成功返回 `draft_ref / edit_token / operation_alias / saved=true / runtime_checked=false`。保存不运行程序、不求解、不创建正式版本。共享 FamilyDraftWriteCommand 负责真实业务声明，CAS 冲突为 DRAFT_CHANGED。旧 repair_attempt/revise_version 和继承定义解释器已退出。

Runner 直接消费 typed Domain 的 definition set/edit 对象，成员路径为 path；Direct Session/source_path 不进入 Runner。转发保留嵌套字段省略，不能把继承字段补成 null。Direct 实际签名、Provider 完整 create/source_kind 分支及资料同步表达新输入；definition_json 资源分别提供 create_definition_input_schema/update_definition_input_schema，内嵌 JSON 短引用沿声明路径投影到 value.geometry_bindings 及 edit.geometry_bindings.set，不改写普通参数 JSON。旧裸 usage/definition 输入解释器退出。Info 继续返回完整正文/定义，target_kind=draft 返回当前草稿及 edit_token，Code 返回准确源码和成员摘要；正式版本 Info/Code 保留原资产职责。

合并、说明和业务引用校验错误由 Family Application 映射为 preflight 输入错误，保留字段/key/编辑序号定位。solved 数值声明缺少 minimum/maximum 时，原校验入口一次收集同类缺界路径；参数归一化一次列出同一对象内缺失的 required 字段。消息前置受影响总数及路径，沿既有 512-byte reason／1024-byte 总诊断裁剪：典型六项可同时显示，长路径列表不承诺完整。未知键仍先拒绝，合法默认值与正常归一化语义保持。失败不提交草稿 generation、指针或终态 Operation；Artifact 准备仍沿原流程在数据库短事务外进行。持久快照始终存完整内容，不保存 action 外壳或增量回放链。

## Product Realization Preview

source 选择 source_kind=draft,draft_ref 或 source_kind=published,family_version_alias，不跟随隐式 latest。

Preview的public参数按该Family公开参数的default/required规则处理；空对象不隐式继承定义中的standard_model发布参数。草稿准备发现已知参数语义错误时返回`PRODUCT_FAMILY_INPUT_INVALID`/`preflight`，包括缺失键及diagnostic中的从0开始item_index、field、reason、correction；它不是模型输出或资产完整性故障。未知完整性错误仍按原失败边界返回。

- mode=single：attachment_kind=self_geometry|standard_model、一次编码的 invocation_json，可给 preview_profile_json。invocation 有 public_parameters、product_origin、length_unit；self_geometry 可带实际外部条件，standard_model 使用自身基准实例。
- mode=cases：cases_json 每批最多 16 例，为 inline/items 或 from_operation/operation_alias/changes。每例包含 case_kind=family、case_key、invocation 与可选 expectation；负例必须给稳定 reason_keys。nominal_scenario_key 可选标记本报告已有的名义案例，不选择发布模型，也不要求追加案例。render_json 选择 none 或 selected 案例。

两分支均可选 retain 和显示参考 reference_geometries。retain=false 是临时执行；true 保存来源快照、G 依赖、原请求、实际输出及成功实体。from_operation继承输入与预期、清除旧嵌套reuse；changes支持add/replace/delete及`{change_kind: reuse, case_key: ...}`，后者直接选择该Operation保存案例，不必重抄invocation。未标记案例仍执行，不需要的案例可delete；更改输入或预期使用完整replace。显式reuse核对准确来源、输入与绑定依赖，并重新比较预期，不继承PASS。技术错误、未运行、SOLVED、AMBIGUOUS、INFEASIBLE和期望检查分别表达；拒绝结果可以复用，但没有实体供Query或出图。

保存single的`observation`未提供预期，因此为`not_checked`。需要预期检查时，可在cases保留准确invocation、声明预期并显式复用原`case`或`inspection`，不重新求解适用的原执行。必要标准构造与外部条件覆盖用于业务核收，不以单个SOLVED代表完整通过，也不作为Publish的供料门禁。完整可解析链见Family Preview的`tutorial`。标准连接条件是本次安装事实或设计范围；后台接口比较不自动查询、授权或修改外部对象，也不证明真实连接。

单例结果保留数值、组件、fragment、实际接口和 frame 检查。SOLVED 可返回同次 inline PNG；保留成功但 render 失败仍有事实、inspection_ref 和诊断。多案例报告返回 source_ref、依赖、逐例状态、coverage_gaps、跨案例诊断，实际图片与 rendered_case_keys 对应。期望只检查实际结果，不生成成功或失败。

`checks_passed` 只判断本批已请求案例的完成、明确预期比较、保存及跨案例检查；缺少 expectation 保持 `not_checked`，不表示构建失败。聚焦标准例或全负例批次可通过，不强制同时提供名义、平移、普通成功及外部负例。`coverage_gaps` 说明本批未检查的观察或中断恢复缺口；通过不证明完整支持域。已有可比较标准构造仍检查原点独立性，技术错误和渲染失败保留各自准确状态。

有限代表选择、同风险复用与停止条件由 Preview 共享指导维护；按真实分支、转换点、支持边界及重要失败路径选代表，不枚举全矩阵。SOLVED、frame PASS 或图片不证明实体材料、真实外部连接或 IFC 写入资格。已保存实体由 Geometry Preview mode=inspection 和 Geometry Query 观察量测，不重复求解；精确坐标、截面合同见[Preview 与 Inspection](geometry-preview-and-inspection.md)。

可选外部示意沿共享 preview profile 的 connection_context.mode=none／auto／pipes 选择，仍在产品求解之外。auto 从实际通过检查的端口向外显示短示意；pipes 只需 Agent 指定显示区域的两点与 OD，不要求真实外管或 CAD 数据，不吸附或裁切。新执行省略/null 不生成背景；已保存 whole/result 观察省略/null 继承保存背景，none／pipes／auto 只临时覆盖本次图片，不改原包或 Operation。示意复用独立 display_context，不成为产品 BREP、fragment、检查端口、Query 产品统计或标准发布资产。长度、跳过诊断及完整规则见[Preview／Inspection](geometry-preview-and-inspection.md#标准连接的独立示意层)。

## Publish、恢复与边界

Publish 输入 `root_kind=family / draft_ref / expected_edit_token`，可选 `preview_examples[]` 只关联原Operation和case_keys。按定义生成正式F版本并冻结全部G绑定；不要求先逐个手动发布G草稿，后续编辑也不改变旧F依赖。`standard_model.public_parameters`给出标准产品参数并复用合同默认值，在局部原点、METRE下构建正式标准BREP；没有标准能力时不要求此资产，不从Preview抽取模型。

准确published Geometry绑定继续固定；draft绑定随Family冻结并去重，未变已发布内容复用准确版本。新固定G按定义构建，新参数化G发布程序。全部声明绑定（包括本次未选中的binding_options）必须闭合，不回退旧版本、不部分发布。Preview保存结果和其检查范围保持独立，不因发布而改写。

正式标准模型可由Geometry Preview的`mode=family_standard_model`出图/截面，或Query的同名target量测，输入均为准确`family_version_alias`；直接读原标准资产，不求解、不复制Inspection，不依赖原作者私有报告。Info提供可访问的发布来源；未记录的历史构造参数保持未知。

`standard_model` 不接受调用方 external_constraints，也不依赖已删除的公开安装参数；内部名义目标可复用同一 solver。标准构造成功不代替 self_geometry 的有限范围成功、越界/方向/OD 失败与明确装不下的独立检查。

未知保存或发布回执、案例中断通过 operation_list/get 对账。案例报告可按 case_key 或 after_case_key/page_size 有界读取，FAILED 批次仍可读已保存部分。Write 回执不充当发布回执。

已保存case报告的`operation_get`默认返回案例状态、诊断数量、inspection引用和内容长度／定位，
不内联完整请求与结果。`page_size`默认1、最多4，实际公开JSON预算可减少返回案例数；
用`next_after_case_key`作下一页`after_case_key`，不跳过未返回案例。
完整内容由同一工具`mode=preview_report`读取，使用返回的`operation_ref`：
`content_kind=case_request/case_outcome`要求`case_key`；
`dependencies/coverage_gaps/cross_case_diagnostics/placement_input/observation_input`不带case键。
`start_character`与`character_count`按Unicode码点选片（默认0／4096、单次最多16384字符）；
返回总字符／UTF-8字节、实际片段、`next_character/complete`，拼接完整text后再解析JSON，null亦保留为原文本。
分片前先形成公开投影及准确引用，参考几何只含公开reference字段；最终公开响应含JSON转义受64KiB预算约束。
Direct与Backend/Runner共用原Operation／Artifact读取服务，HTTP返回前分页；不会重做Preview或生成新Package。
该读取与投影的限定验证见当前实施记录§12，不代表安装业务验收。

Family 仍为纯数值求解，Geometry 由受控 Runtime 构建，实际接口与最终结果由 Backend 独立检查。详见[Family Runtime](product-family-authoring-and-realization-runtime.md)与[自身几何合同](product-family-self-geometry.md)。当前 authoring/Preview 不代行未来 Apply、外管修改、网络关系或持久 IFC ports。

作者返回的fragments省略geometry_parameters；后台按当前绑定来源唯一组装。公开program_api Schema
表达作者返回，canonical执行结果及保存的Preview仍保留完整Geometry参数和同次receipt。
旧Family草稿、Operation业务详情、正式版本和保存报告没有当前使用资格；通用operation_list
只显示授权范围中的终态摘要及准确operation_ref，详情、Code与结果读取继续独立检查当前来源。
Geometry来源的保存形体和查询不受Family合同替换影响。

公开资料按需选择 Write 的 definition_json、source_changes_json、program_api、tutorial，以及 Preview 的 self_geometry、standard_model、cases_json、render_json、result、tutorial。`result`是可选的核心字段导读，不是完整输出Schema，也不是调用前置；通常直接读取本次返回。Family导读解释求解状态、有效参数、绑定/构建记录和接口事实；执行/预期/渲染状态、保存引用与图片顺序的共同含义只在`common/preview#preview_results`维护。共享流程见`common/authoring`，图片与结果解读见`common/preview`。Write `program_api.return_schema`仍是作者必须正确构造的Proposal接口，不能随工具结果资料一起简化掉。Provider DTO、共享 Domain、资料和错误路径保持一致，不另存人工 schema 版本。

替换前验收记录保留原轮事实，不证明新草稿/Publish 已完成原生作者验收。
