# Component 聊天运行活动投影

当前运行活动沿现有 `RunEvent` 日志发布。`RoleMemory.messages` 和 LangGraph checkpoint
仍是完整消息、工具提议和工具结果的权威；活动不保存模型私有推理或工具参数片段。

## 观察来源

- `AsyncPostgresGraphRuntime._invoke` 将当前已领取 Run 的 `run_id/task_id/thread_id`
  加入回调 metadata，保留原执行 metadata、claim context 与 `durability="sync"`。
- `MainTextDeltaCallback` 观察实际 `initialize_component` 节点和带当前角色标签的模型调用。
  标题、Main、Role Card 分别显示初始化、生成标题、模型处理、整理结果；前端将 `card`
  活动具体呈现为“正在生成本轮工作记录…”。它表示退出前的 summary 模型调用，
  不表示重新建模或加载三维成果；Card 保存后才开始本轮发布成果的展示加载。
- 模型 SDK 首次 `response.function_call_arguments.delta` 只发步骤信号，不发参数内容；
  同一模型尝试的后续参数片段不新增活动事件。
- `ChatCheckpointObserver` 复用已有 `tool.call.started/completed/failed` 投影已保存的提议和结果。
  已保存 AIMessage 与 ToolMessage 的来源及 `main_message_id` 关联保持原合同。
- 原 LangChain `on_tool_start/on_tool_end/on_tool_error` 观察真正执行、返回或中断。
  同一 tool item 依次显示 `queued`（提议已保存）、`running`（真正开始执行）、
  `result_received`（结果已返回，等待保存），最终结果仍由原 ToolMessage 发布为
  `succeeded/failed`。执行异常尚无 ToolMessage 时为 `interrupted`，拒绝提议为 `not_executed`。
  `status_sequence` 给出对应 RunEvent 顺序，不改变 executor 或 pending_calls。
- 压缩从真实 summary 模型请求开始；摘要改变时，活动结束等待原 checkpoint 发布成功。
  无收益压缩在实际节点返回时结束。未获得保存证据不宣称压缩已保存。

工具声明装配和模型 `bind_tools` 在服务启动建图时进行。当前 Run 不重复装配工具，
所以不会将每轮 Prompt 组装或初始化伪装为 `loading_tools`。

## 浏览器合同与刷新

快照增加 `activity`，SSE 使用 `event: activity`。两处同一公开结构为：
`kind="runtime"`、`activity_id`、`stage`、`phase`、`label`、`event_sequence`；
实际存在时附 `main_message_id`、`call_id`、`retry_attempt`。
`phase` 为 `start/end/error`，`event_sequence` 与原 RunEvent sequence、SSE id 一致。
活动事件在同一 Run 的运行活动 item 中按 `activity_id` 严格配对：开始后可更新阶段，
只允许一次结束或错误；缺失开始、重复终态和终态后再次开始都被拒绝。
实际工具回调每次执行使用独立 activity_id；`tool_item_id` 和 `call_id` 关联原已保存工具提议，
恢复执行不改变原工具请求或业务操作身份。
工具回调的单个 activity SSE 帧可附 `item`，内含同一原工具 item 的当前投影；前端先
处理 item 和 activity，再推进游标。一个 sequence 不拆成多帧，避免接收第一帧后断线
导致重连跳过同序号的工具更新。snapshot.activity 不附 item；原工具 checkpoint 仍只发 item 帧。

阶段包含 `initializing/title/model/tool_arguments/tools/tool_results/compression/card/retry`。
Main 依据已保存角色消息或摘要的实际历史区分首次处理与继续处理。
浏览器用 sequence 和 activity_id 配对，迟到的旧活动结束不结束新活动。
快照从日志重建活动，分页读旧消息不回退当前运行活动；Run 非 running 时 activity 为 null。
快照按原 `run.status.changed` 的非 running 边界清除未闭合活动；继续原 Run 后，
先前模型活动不会在新一轮活动结束后重新出现。此边界也读取最近窗口之前的事件，
不补写虚假的模型完成。租约回收的既有协调方法关闭执行活动，保留等待执行的工具提议，
其最终 ToolMessage 仍沿原工具 item 保存。
既有压缩增量提示仍为临时投影，不能覆盖更新的持久活动。

网络重试使用原 `message_id/call_id` 的 `text_delta phase="retry"`，清空失败尝试的
临时文字和序号，并附实际 `retry_attempt`。成功尝试不拼接失败尝试的输出。
重试活动仅发布步骤边界；PAUSED 的公开 Run.error 只显示运行方保存的安全模型原因短码，
恢复仍沿现有 Run/checkpoint/resume 路径。

## 网络中断与继续

浏览器收到 offline 后关闭旧订阅并停止轮询；online 后立即重新连接。普通断连按
1、2、4、8、16、30 秒退避，后续间隔保持 30 秒，直到连接恢复或用户切换任务/离开页面。
SSE 建连最多等待 15 秒；普通 GET 最多 30 秒，其余 HTTP 请求最多 120 秒。
初次任务列表或快照读失败也自动重读，认证失败/不可见资源不反复请求。
恢复使用原 Run 的 PUBLIC 事件游标，连接成功再 GET 当前 Run 状态；只读重连不创建、
继续或取消 Run，不自动重发业务写操作。原用户输入/控制请求重试继续使用原幂等身份。

模型推理对连接错误、断流、缺失完成事件、HTTP 5xx 和明确瞬态服务端错误最多尝试 3 次，
中间等待 1、2 秒；每次独立收集完整响应，失败尝试的部分正文/工具意图不进入 checkpoint。
认证、无效请求、用量限制、非瞬态失败和取消不自动重试。仅模型边界标识的失败可交给原
Runner 暂停事务；工具传输错误仍保留原工具执行与未知结果恢复语义。
重试耗尽时原 Run 保存安全错误原因、释放租约并进入 PAUSED，执行循环继续服务其他任务。
页面显示原因和继续入口；继续沿现有 resume→原 Run→原 operation receipt/checkpoint 接续。

用户气泡在实时对话和城市回放共享淡蓝色样式。当前工具组显示逐项准备、执行、返回、保存
和中断状态及完成数；同次 Preview 多图横向排列。发送/暂停采用圆形箭头/方块按钮。

## 多任务提交与等待

每个 Component 任务拥有独立输入发送器和 Task/Run 身份；一个任务运行时可新建并提交其他任务。
本机 Component 服务默认创建 2 个既有 Runner 执行器，每个槽位拥有独立 runner_id 和领取锁，
沿同一 PostgreSQL claim/lease/checkpoint 机制并发处理不同 Run；同一个 Run 仍只允许一个有效租约。
`--runner-slots` 或本机 runtime.json 的 `runner_slots` 可配置 1–4，其他组合根消费者默认仍为 1。
本机服务可通过 `--model` 与 `--reasoning-effort` 配置 SIWC 推理模型和思考强度；后者接受
`low/medium/high/xhigh/max`，未指定时读取 runtime.json 的 `reasoning_effort`，再回退到既有 `max`。
配置传入最终 Responses 请求并保留 `context=all_turns`；不会切换到 API Key 推理或改变工具权限。
运行中不热切换配置；比较测试在前一 Run 结束后重启同一服务，并核对实际 Provider 请求。
工具执行预算和 Geometry Worker 限制不随槽数放宽。可恢复模型错误暂停原 Run，不阻塞另一槽；
未知执行错误向服务监督传播并收束全部槽，不能当作模型失败自动重放写入。
`queued` 的角色卡底部明确显示“已提交，等待执行”，不沿用思考或工具执行状态。
LLM 标题尚未生成时，侧栏可从该任务已有首条用户消息显示临时摘要；不改变后台标题，
正式标题返回后替换摘要。

当前会话保留一个 SSE 订阅。存在非当前的 queued/running 任务时，在线页面每 5 秒沿既有
Task 列表读取这些任务的标题与 Run 状态；不读取后台任务正文、不改变事件游标、不启动或
切换 Run。相关任务结束、页面离开或离线后停止刷新，在线恢复后继续。切回任务仍从原 Run
快照和 SSE 恢复其消息。列表认证失败或不可见错误停止自动重读。

演示侧栏中指定的五个样例任务按用户要求展示 2026-09-25 至 2026-10-01 的日期，
侧栏统一显示“10月2号”这样的具体日期；鼠标提示标明演示日期及真实更新时间。此投影不修改服务端时间，
也不影响之后创建的新任务。

侧栏另有六个用户指定的 CAD2BIM 项目展示项，日期为 2026-09-23 至 2026-10-02。
它们只有名称和日期，不创建 Task/Run、不绑定导航或内容页，与真实任务共用侧栏滚动区域。

聊天快照在单次请求内按准确的 thread_id/checkpoint_ns/checkpoint_id 复用已读取的检查点，
每条消息仍按自身索引解析。缓存不跨请求、不改变消息或检查点权威；下次读取重新取得保存状态。
该读取明确使用不重发观察通知的检查点路径；Runner 与恢复读取默认仍保留通知。
仓库在同一快照边界内批量取得本页消息的完整事件及所需活动，不逐条查询消息，也不逐页扫描
全部活动历史；原 Run 锁、权限检查和 PUBLIC 事件分页游标保持有效。

打开任务后先显示最近的消息页，前端自动以每页 100 个公开事件继续补齐历史，保持阅读位置。
正常分页只显示“正在自动加载更早的消息”，只有读取失败才出现重试入口；切换到其他任务后
停止继续请求该任务的历史页。回到已加载任务直接显示页面已有消息，再从该任务原事件游标
重连 SSE，不重新请求整页快照。页面刷新后仍重新从服务端读取；没有新增持久消息缓存。

## 验证与范围

聚焦 owner 测试覆盖真实 LangGraph 初始化回调、快照/SSE 同一投影、刷新与迟到结束、
工具请求/结果关联、压缩提交与无收益处理、重试文本清理和公开信息边界。
这些自动检查不构成新的真实模型业务验收，也不证明尚未加载新代码的在途进程已发出活动。
