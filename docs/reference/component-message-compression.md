# Component 消息、压缩与标准 Skills

本合同描述当前 Component 角色的实现；真实 P4 纵向接收状态另见
实施记录。消息与业务资源分别沿既有权威保存。

当前 Main 与压缩请求容量检查已接入项目 API Key 官方计数优先、本地估算回退；实际推理仍使用 SIWC。
本合同说明当前代码接线，真实圆环任务与受影响运行链路由主控另行核收，不在此声明运行验收完成。
其他当前目标与接收边界见总计划。

## 消息进入与执行

Component 全局图的首节点 `initialize_component` 先沿既有反馈入口读取已保存的首次输入，
以原生用户文字/图片调用同一配置模型生成任务标题，再进入角色子图。命名调用使用 `title`
purpose，不绑定业务工具，不作为 Main 回复或推理片段显示，也不把标题 AIMessage 写入
`RoleMemory.messages`；原始用户输入、批次与水位完整交给 Main。
标题沿原 Task 保存，与 `metadata.component_title_generated` 及 `task.title.updated` 事件
同事务写入。已有生成记录则不再命名，覆盖命名提交后但节点 checkpoint 尚未保存的恢复情况。
外部创建 Task 不得提交该服务端标记。SSE 的 `task` 事件和 snapshot.task 从同一 Task 投影，
同步最近任务与页头。命名不成功时不伪造标题或吞掉错误。

部署可通过 `component_initial_reference_text` 将一份完整参考传给原图。仅当已保存的首次
HumanMessage（`input_kind=initial_input`）正文含 `过滤阀组` 时，初始化节点在命名后追加
一条 `source=initial_reference` 的 SystemMessage。消息 ID 由原 Run 与首发消息 ID 确定，
`trigger_message_id` 指回原首发；随同一 RoleMemory/checkpoint 保存，不新增注入状态或数据库。
首次输入识别独立于标题是否已经生成，标题提交后重入仍能加入材料。续聊、工具结果和其他
SystemMessage 中的关键词不触发；原用户文字、图片、反馈身份保持。标题不包含参考材料。

本地 Component 组合根从交接输入包的明确清单加载 `injection-guidance.md`、`demo-guidance.md`、
`geometry-writes.json`、`family-write.json`、`preview-cases.json`、`observations.json`、
`publish.json`；不扫描任意目录、不自动执行内容、不把来源审计或概念图注入消息。
详细安装与验收指导独立于简短用户正文；通用Skill只注入官方发现目录，正文和references仍由
Agent按需调用`read_file`加载。概念图由用户沿现有附件入口上传，不按关键词自动附图。
参考代码与 JSON 完整计入现有模型请求和官方计数。材料来自已提供实现，不能称为模型无参考
独立原创；只有本次真实返回可用作成果。System 参考不投影成用户气泡或工具输出。

`RoleMemory.messages` 保存 LangChain 原生消息：用户文字/图片、Main 的响应与原生推理内容、
工具调用、配对 ToolMessage、资源读取和 Skill 附属文件。角色子图以同一 checkpointer 保存；
父图在角色返回时整体替换对应记忆槽，不叠加旧 messages 前缀。

Main 之前先由反馈节点收取已受理输入，再组合当前角色指令、Skill 目录、双摘要和 messages，
计入实际绑定的 Provider 工具定义并检查输入预算。Main 完整响应保存后才执行其已保存的工具
提案；工具结果与原 tool_call_id 配对，写入未知时保留 pending 与原 action，不产生新写身份。

用户输入的“已保存/已投递”不等于 Main 已处理。最后一条已保存 Main AIMessage 之后的全部
HumanMessage 均受保护；Main 失败不会越过该边界。Main 因容量返回压缩节点时保存刚收取的
全部输入，压缩后重新进入 Main 准备流程，再收取压缩期间到达的反馈。

## 压缩请求与提交

1. 后台按原消息 ID 选择完整历史区段，保护初始输入及通过 `trigger_message_id` 关联的完整
   initial_reference System 消息、尚未处理的用户输入、未完成工具交换及
   必要的最近交换。最近历史的软保留目标最多为输入容量的10%，并扣除前缀、工具定义、旧
   摘要和首发固定材料在90%提前线内已占的空间；不能把这部分当成窗口外另送的额度。即使软
   目标为零，初始输入、未处理反馈和最新完整工具交换仍受保护。调用与返回不拆分，短反馈
   连同理解背景进入临时压缩材料。
2. 临时请求包含旧 `conversation_summary` / `tool_chain_summary` 和原顺序原生消息。
   连续区段用 SystemMessage 文字明确标注 `selected_to_compress` 或 `retained_for_context`。
   图片保持图片块；opaque reasoning 保持原字段，不人为转成可读推理。标记不写回 messages。
3. 两个摘要调用按各自职责重写对应摘要；要求保留有效用户要求、决策、成功/失败/未知状态和
   准确业务引用。对话摘要不重复工具明细与引用串；工具摘要保留继续工作和回读所需引用，
   不复制完整源码、JSON或已被正式资源取代的草稿细节。保留区帮助理解，不因进入摘要材料
   就获得删除资格。
4. 后台检查新摘要加保留区和请求前缀/工具定义能否容纳，并比较压前/压后占用。两个摘要都
   成功、容量通过且计数占用减少，才一次提交新双摘要和选中原 ID 的 RemoveMessage。
   无收益时保留原文与旧摘要，返回原目标节点；超过容量或生成失败仍报原错误并保全状态。
5. 第二次、第三次仍以旧双摘要加新选择历史重写相同两个字段；没有摘要代次列表。

摘要模型只负责压缩。触发、容量检查、原消息选择和删除均由后台负责。

## 预算与已知限制

默认总窗口为 256000，输出预留 8192，可用输入 247808；提前线为可用输入的 90%，即
223027。提前检查后，Main 收取新反馈仍有最终容量保护。当前前缀、双摘要、messages 和实际
Provider 工具定义均计入，标准 Skills 目录也计入。资源/Skill 的实际正文以消息成本计入。
8192 是本地输入规划的预留，不是已发送的服务端输出硬上限；当前 SIWC 入口不接受
`max_output_tokens`。压后检查保证候选满足输入硬容量并有计数收益，不保证每次回到提前线以下。

计数沿 [`count_request_input_tokens`](../../src/CAD2BIM_backend/infrastructure/input_token_count.py)
解开实际模型的绑定参数，再调用 [`ChatGPTPlanModel.acount_input_tokens`](../../src/CAD2BIM_backend/infrastructure/chatgpt_model.py)。
它复用推理的 `_get_request_payload`，保留实际 model、输入、reasoning 和绑定工具的最终投影，
仅筛出计数 SDK 接受的字段；不另拼消息、不解密 reasoning、不裁掉图片或 opaque 材料。
[`role_graph`](../../src/CAD2BIM_backend/infrastructure/langgraph/role_graph.py) 在 Main 收取反馈后检查完整请求；
[`role_compression`](../../src/CAD2BIM_backend/infrastructure/langgraph/role_compression.py) 对压前请求、
两份完整摘要输入及压后候选分别优先调用同一计数入口。本地估算仍用于选择历史区段的软保留目标。

[`Settings.load`](../../src/CAD2BIM_backend/config.py) 只在显式设置 `CAD2BIM_ENV_FILE` 且文件存在时
读取该 dotenv 文件，再由进程环境覆盖；`OPENAI_API_KEY` 保存为不参与 repr 的配置字段。
[`component_demo`](../../src/CAD2BIM_backend/infrastructure/component_demo.py) 有该 Key 时建立独立
`AsyncOpenAI` 计数 client，超时 10 秒、SDK 不自动重试；模型仍由原 `ChatGPTAuth` 取得 SIWC token。
计数凭据不进入推理认证，也不因计数失败切换推理路线。

成功结果记录 `source=official`；Key 缺失、模型没有计数入口或计数服务异常时回退到
`source=local_estimate`，同时记录 `fallback_reason`。日志只记录安全原因类别及可用的 HTTP 状态，
不记录凭据、请求正文或 SDK 错误正文。推理请求投影的合同错误仍在计数调用前抛出，取消也不转成估算成功。
`official` 表示官方计数端点的返回值，不保证与 SIWC 的历史加密推理计入口径逐项相等。
本次真实圆环前四次 Main 中，首请求一致，后三次 SIWC input usage 比 count 多 145/262/525；
原始 reasoning item 和 encrypted_content 均完整保留，差额与此前 reasoning usage 相关。
该观察不是固定补偿公式，不加“每条 +2”等未经服务端合同确认的常数；保留现有余量，
容量保证仍受两种服务端口径及本地回退精度限制。准确样本见
历史加密推理计数调查。
本地估算对紧凑 Provider 工具 JSON 使用 tiktoken `o200k_base`，消息使用 LangChain 近似计数；
它不宣称准确的服务端 tokenizer、图片成本或加密 reasoning 占用，不能将回退结果写成官方计数。
双摘要、默认阈值和反馈保护不因计数来源改变。必须保留的材料或压缩模型完整输入过大时，
容量错误保全状态并停止，不通过静默裁图、丢消息或自动摘要重试掩盖。历史计数探测与样本限制见
计数接口调查。

## 标准 Skills 的唯一接入

`infrastructure/agent_skills.py::AgentSkills` 是组合适配：部署时调用已安装
Deep Agents `SkillsMiddleware.abefore_agent` 发现标准 SKILL.md；请求前调用其
`modify_request` 注入目录。名称、描述和 frontmatter 解析由官方组件负责。目录是启动时
生成的部署元数据，不是角色正文快照或另一套状态存储。

当前按职责发现[资源创作](../../skills/cad2bim-component-authoring/SKILL.md)与
[模型安装维护](../../skills/cad2bim-installation-maintenance/SKILL.md)两个标准包。
前者负责Geometry/Family定义与发布，后者负责消费已有资源、安装关联、实例维护与保存后核查；
目录只给名称、描述和入口，正文按当前任务读取，不因有两个包而预载全部内容。
原component-engineering的独有引用和交接规则沿当前相关指南保留，重复包不恢复。具体演示源码、案例和图在独立输入包；
`agents/langgraph-catalog.json` 不是 Component 注册入口。部署更新后重新发现目录。

只暴露官方 `FilesystemMiddleware.tools` 中的 `read_file`，并以当前保存调用的 ToolRuntime
执行其正式读取实现。官方 FilesystemBackend 使用受控包目录及 `virtual_mode=True`；拒绝
越界与外部符号链接，未挂载工作树、聊天、用户附件或业务资产。仅可信部署过程维护目录。
不提供写文件、脚本执行或通用宿主文件浏览。该路径限制不被表述成进程级多租户沙箱。

Markdown、Python、JSON 以原格式读取；图片保持原图片块。读取结果直接进入现有 ToolMessage，
保存/恢复/压缩规则与其他 READ 一致。明确新读取取得当前部署文件，既有消息仍是当时读取的
内容。原 skill_load、自有 SkillLoader、LoadedSkill 和重复 loaded_skills 正文快照退出在线链。
部署新的合同不会改写旧实验记录，也不承诺旧快照合同可跨实现直接续跑。

不安装 FilesystemMiddleware 的大用户消息/工具结果转存钩子，并显式关闭两个 eviction
阈值。没有通用历史消息/图片读取工具及其后台服务、索引、缓存或恢复层。

业务细节缺失时使用原 Operation、Catalog、资源读取、Query、Preview 调查。摘要保留
operation_ref、inspection_ref、case_key 等准确引用；inspection Preview 是同一保存实体
重新出图，可换视角或截面，不保证历史 PNG 字节相同，不重新运行作者程序。

## 对话轮次与任务删除

前端收到用户输入回执即创建该轮唯一角色框，当前状态放在框底部。该轮 Main 调用、工具过程、
最终回复与交接 Card 复用这一框；内部 Main 再次调用不新建 Component Agent 标题。
真实压缩 activity.start 将状态改为“正在压缩…”，同一角色框内的工作过程复用城市任务的
`context-compaction` 样式。临时状态不是消息权威，只有摘要 checkpoint 提交后的已保存事件
才生成“上下文已压缩”历史行；断线和切换任务清理临时状态，快照沿原 RunEvent 恢复仍在进行
的活动。工具逐项状态、真实阶段与恢复规则见 [聊天活动合同](component-chat-activity.md)。
预算足够、未调用摘要模型时不产生压缩开始事件；模型已运行但因无收益返回空更新时，
压缩节点结束通知仅清理临时 activity，不生成已压缩历史行。包含摘要更新时仍等待原
checkpoint 提交；模型、checkpoint 或祖先执行失败按原 error 事件收尾。

`DELETE /tasks/{task_id}` 沿原 TaskRunService/Repository 将 Task 标为 ARCHIVED，204 表示
完成，重复删除同一已授权 Task 幂等返回204。任务列表、Task/Run/消息/私有附件读取、继续和
新建 Run 均拒绝该任务；原请求的幂等重放不能复活它。运行中（含仅请求暂停）和未决 PREPARED
写操作返回409，不擅自取消真实执行或换身份重发写入。非运行中的等待/暂停/排队 Run 沿原
取消逻辑关闭 gate 与反馈；Task 锁后获取 Run NOWAIT，避免和原执行事务的锁顺序死锁。
不物理级联删除已发布 TENANT 资源及其引用资产；发布库权限仍由原 Catalog/RLS 决定。

前端删除入口使用站内确认弹窗，显示准确任务名称，默认焦点位于取消，支持 Escape/关闭。
删除请求执行中禁用按钮；失败在弹窗内显示，保留任务；仅204后清理本地订阅与任务投影。
