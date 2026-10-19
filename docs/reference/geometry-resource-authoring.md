# Geometry Resource Authoring as-built 合同

当前 `ifc_geometry_resource_write` 保存可信入口选择的 TASK 或 TENANT 草稿：Direct / 普通 Workflow 为 TASK，已固定的 Component Run 为 TENANT；调用方不提交所有权字段。`ifc_geometry_preview` 执行观察和代表案例，`ifc_authoring_publish` 按自身定义准备并发布正式资产。共同身份、CAS、快照、依赖固定和恢复语义见[草稿、Preview 与发布](authoring-drafts-preview-and-publication.md)，跨 Task 权限见 [TENANT 作者资源持久化](tenant-authoring-persistence.md)。工程验证不等于原生作者业务验收。

## Write 输入与结果

Direct 每个分支必带 `direct_session_alias`；Runner 使用共享 Domain 命令，源码成员路径是 `path`，不接受 Direct Session 或 `source_path`。

| 模式 | 必填字段 | 内容来源 |
| --- | --- | --- |
| `create`，作者源码 | `display_name / usage_instructions(action=set) / source` | `source_kind=mapped_model`：source_files，可带 geometry_inputs、interfaces；`source_kind=modeling_function`：source_files、parameter_schema_json(action=set)，可带 geometry_inputs |
| `create`，已发布来源 | `source(source_kind=published,resource_version_alias)` | name 省略时取所选 bundle.resource.display_name；usage 省略时取准确 bundle.version.manifest.usage_instructions，可逐字段覆盖，usage 覆盖使用 action=set |
| `update` | `draft_ref / expected_edit_token`，至少一项改动 | 可改 display_name、usage_instructions、source_changes、geometry_inputs、parameter_schema_json、interfaces |

省略字段继承当前草稿，显式 null 拒绝。source_changes 使用封闭 literal_edit / add_member / replace_member / delete_member 分支，整次修改原子完成。expected_edit_token 已固定整个草稿 generation，替换/删除不再要求调用方回填成员 SHA；准确父源码字节、目标存在性和 literal_edit 的唯一旧文本匹配仍由后台检查。interfaces 只用于 mapped_model，函数参数合同只用于 modeling_function；Geometry 输入目前只绑定已发布 exact Geometry。

mapped_model 的声明 interfaces 是固定模型的接口注释。Geometry Preview、Family 草稿的延后固定依赖构建及正式发布共用同一选择政策：声明非空时保留声明，声明为空时采用 build 输出；两侧均非空且不一致时拒绝该构建结果。build 未重复输出接口不使合法声明丢失；这一政策不替代 Family 对实际引用模型接口和全帧关系的校验。

modeling_function 的同一准确源码包必须包含 `main.py:build(ctx)` 和 `interfaces.py:evaluate(ctx)`。后者只读同一正规化参数，通过 `ctx.define_interface` 声明局部接口并返回 None；即使没有接口也保留该入口。可达 helper 只用受控纯数值能力，不导入 main、CadQuery/OCP，不读 Shape、geometry_inputs、文件或网络。函数 build 只返回 Shape，不能声明接口；fixed mapped 的创作 build 仍可声明接口，发布后的准确 manifest 才是固定接口权威。Write 与按定义 Publish 静态核接口入口，不因此新增全域试算或发布资格。

函数Preview和Query样例复用同一两阶段Runtime：独立数值Worker先执行evaluate，成功后才从同一
准确依赖引用读取重资产并启动Shape Worker。两阶段直接比较冻结源码与正规化参数，
函数build不重新求接口。接口程序技术失败没有实体或图像，普通build故障仍是技术失败；
Family正常接口关系FAIL则保存无几何数值报告，可以读取与复用，不进入build。

`usage_instructions` 的完整写入为 `{"action":"set","value":"完整说明"}`；update 可用 `{"action":"edit","edits":[{"old_text":"准确且唯一的原文","new_text":"替换文"}]}`。原文按同一份当前正文匹配，重复、未命中或重叠失败会给出字段、原因及从 1 开始的编辑序号。草稿与 Info 内仍是完整说明字符串；动作外壳不进入资源内容。最终说明继续受 8192 Unicode 字符、32768 UTF-8 字节、非空白和无 NUL 的原合同约束。发布来源缺失有效说明时，省略不能生成说明，必须显式 set；不读取 latest，也不为继承补造默认正文。

Geometry 说明只补充 Schema 和准确合同不能直接表达的模型使用语义，如参数变化对形状与接口的影响、局部锚点的实际含义及支持与证据边界。不重列参数类型、单位、required/default/bounds、绑定或通用工具步骤；公开示例保留几何规则与语义，不把调用教程写入资源说明。

### 构造成本与表达用途

`program_api`的usage沿原tool_docs owner提供构造成本指导；详细方法在同一
[作者Skill](../../skills/cad2bim-component-authoring/references/construction-methods.md#批量成本与布尔顺序)。
作者先按用途选择外观、安装、内部通道或制造细节，优先直接拉伸/一次回转和避免重复处理
同一交界面。例如最终统一扣空域H时，可验证`((A-H)∪B)-H`改为`(A∪B)-H`，避免
预先开孔造成重复共面内边界；这不是保证所有模型更快的后台重写规则。
只能在任务允许时省略隐藏孔道或装饰细节；盲腔和真实贯通、名义接口和实际孔洞须明确区分，
资源usage声明近似用途与限制。必要净空、实体有效性及当前形体到IFC的转换保真保持。

批量用途先选少量代表输入比较实际可见诊断，区分每组初始化和逐实例构造；无细分计时
时报告缺口，不在作者程序引入禁用计时器或后台私有API。此说明不增加公开字段、注册
性能门禁、全域试算或发布资格，不改变Write/Preview/Publish的现有执行边界。

Direct `parameter_schema_json` 是序列化一次的动作对象：完整写入为 `{"action":"set","value":完整Schema}`，update 的局部修改为 `{"action":"edit","set":{"diameter_m":{"schema":{"type":"number","exclusiveMinimum":0},"required":true}},"remove":["obsolete_parameter"]}`。Runner 在原 `parameter_schema` 字段提交对应对象；从作者源码创建仅接受 set。

参数 edit 按顶层名称新增或替换整个 properties 条目，required 明确控制该参数的必填成员资格；remove 只能删除存在的参数，同时移除其 required 成员。未涉及的参数、required 成员及根 description/default 等保持。重复 remove、同名 set/remove、未知参数删除拒绝。嵌套对象、数组或判别分支作为一条完整参数提交；改名由删除旧名和增加新名表示，源码读取点由作者同次 source_changes 修改。

合并后继续使用 `validate_geometry_function_parameter_schema` 的受限闭合方言和最终 16 KiB 预算，不支持通用 `$ref/$defs`，不因 set/edit 增加方言或减小原正文容量。根 default 因删除参数或修改类型变得非法时拒绝，作者以 set 修订完整根结构。mapped_model 不能提交参数合同；modeling_function 的 interfaces 来自同一源码包的 `interfaces.py:evaluate(ctx)`，Write 不能提交该字段，显式空集合也拒绝。

Direct source_files 成员为 `{source_path,source}`，完整 bundle 含 main.py/build(ctx)。入口确定性转换为共享 GeometryDraftWriteCommand，服务验证完整源码、参数声明和依赖。Write 不执行作者 Python、不渲染、不创建正式版本。

创建和更新后的完整源码统一受现有Runtime预算约束：最多16个成员，每成员UTF-8 64KiB，合计256KiB；不是512KiB。`geometry_inputs`最多8个，准确固定BREP来源合计32MiB，省略继承，空集合明确清空。公开Schema、草稿保存及受控执行共用所属领域常量。

成功返回 `draft_ref / edit_token / operation_alias / saved=true / runtime_checked=false`。最终内容或编辑错误以 preflight 的 `GEOMETRY_DRAFT_INPUT_INVALID` 返回，并保留字段、参数 key 或编辑序号；不会先保存部分修改。过期 CAS 返回 `GEOMETRY_DRAFT_CONFLICT`；先读取当前草稿再修改。未知结果先查原 Operation。旧 repair/revise/annotation 运行链已退出。

## Preview 与 Publish

Preview source 为封闭 `source_kind=draft,draft_ref` 或 `source_kind=published,resource_version_alias`。

- `mode=single`：sample_parameters_json 默认 `{}`，可给 preview_profile_json，执行一次准确快照观察。
- `mode=cases`：cases_json 每批最多 8 例，是 inline 命名案例或 from_operation 加有界输入修改；每例有 case_key/parameters，可断言 BUILT 或 REJECTED(PARAMETERS_INVALID)。render_json 选择不渲染或指定案例。
- `mode=inspection`：消费已保存 inspection_ref、selection、coordinate_space、observation，换视角或量测截面，不重执行作者源码。
- `mode=family_standard_model`：准确 family_version_alias 与 observation，读取该正式版本保存的标准模型；局部米单位整体观察，不求解、不创建保存型 Operation。

前两分支可选 retain、刚体 datum_transform、显示用 reference_geometries。retain=true 保存同次报告和实际成功实体；false 为临时观察，不签发可跨调用使用的模型引用。技术中断保留准确部分结果，completion为interrupted（不是partial枚举），不伪造业务拒绝。报告返回准确 source_ref、依赖、逐例状态、检查缺口和实际渲染案例次序。

保存的single是`observation/expectation_check=not_checked`，可立即Query或再出图。需要比较预期时，在cases声明同源同输入的预期并显式设置`reuse_kind=inspection`或`reuse_kind=case`；适用的原执行不重复建模。`from_operation`只继承案例输入。案例比较与Publish相互独立，见`common/authoring`及原工具tutorial。

有限代表选择、同风险证据复用和停止条件由 Preview 共享指导维护。BUILT、PNG 或接口通过都不能独立证明材料或连接；复用 inspection 量测实际形体。显示和截面精确语义见[Preview 与 Inspection](geometry-preview-and-inspection.md)。

Publish 输入 `root_kind=geometry / draft_ref / expected_edit_token`，可选 `preview_examples[]` 关联原 Operation/case_keys。固定Geometry由自身源码与准确输入构建正式局部BREP；参数化Geometry保存程序与参数合同，不要求sample。示例只供来源导航，不供给模型、不作为PASS门禁。成功返回原Operation与正式版本；失败可从原Operation找回，不暴露半发布资源。

## 读取与唯一所有者

Info 的 `target={target_kind:draft,draft_ref}` 返回当前 edit_token、源码成员及完整草稿声明；Code 读取当前准确源码和成员 SHA。已发布 root/version Info 继续由原 Catalog 提供。编辑草稿不会把早先 Preview 的 snapshot 自动变成新草稿证据。

Direct、Runner 和 Backend 内部 API 复用 GeometryAuthoringService → AuthoringDraftService。说明文本编辑复用注入的 SourcePatchPort.apply_text，参数合并由原 Geometry 领域合同负责；完整正文仍只保存一份 Artifact 快照，更新受草稿 CAS 控制。执行由 GeometryPreviewService/GeometryInspectionService 持有；发布由 AuthoringPublicationService 与原 Geometry Repository 持有。没有第二套 Worker、Catalog 或事务所有者。

受控 Python/build(ctx)、单位、方法与 shape 返回合同以 `cad2bim://contracts/ifc_geometry_resource_write/program_api` 为机器资料入口；资源与 Query 边界见[Geometry 资源调查](geometry-resource-catalog-and-investigation.md)。资料按需读取，不要求全量预读。

当前Write保存草稿不自动测试IFC表示。发布准备对固定Geometry执行build/reopen并保存
canonical BREP；函数Geometry保存源码和参数合同，不执行全参数域建模。Agent注册的固定
与函数Geometry用于IFC时一律TFS，不自动检测Advanced，不新增注册选路字段或资格资产。
SYSTEM优化由后台绑定准确资源的整项唯一路线，沿唯一构造及现IFC编译链实现，见
已批准混合策略。
固定BREP有效、Preview成功或发布成功不使普通Agent几何取得SYSTEM高级路线。

替换前验收摘录保留旧轮真实记录，仅适用于当时接口，不代表当前草稿流程已完成原生验收。
