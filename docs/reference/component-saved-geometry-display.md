# Component 保存实体的 PNG 与 3D 显示

本合同描述当前实现。P4 的代表性原生、浏览器和跨 Task 验收状态及原始失败，见
实施记录与显示验证。

## 唯一数据源与入口

显示实体只能来自已获权的准确保存结果：

| source_kind | source_ref 指向 | 实体来源 |
| --- | --- | --- |
| geometry_resource_version | 准确 Geometry 正式版本 | mapped_model 的 canonical BREP；定义型函数本身没有可直接显示的保存实体 |
| family_standard_model | 准确 Family 正式版本 | 该版本保存的 standard model BREP；无 standard model 时明确拒绝 |
| geometry_inspection | 准确 inspection | 保存的完整实体及其原坐标，可来自草稿或正式资源的 Preview |

`GET /tasks/{task_id}/runs/{run_id}/display` 读取已有配对结果；`POST` 在缺失时生成。
两者均使用 `source_kind` 和 `source_ref`，每次包括命中已有结果时都重新授权准确源。
没有直接从宿主路径、任意 URL 或聊天图片推导实体的入口。

## 授权与跨对话

入口先验证用户对具体 Component Task/Run 的访问，再由既有
`checkpoint_observation_context` 读取该保存 Run 的可信 RUNNER 身份；核对 tenant、principal、
Task、Run、thread、module 和 execution mode 后，仅用于解析本显示源。
浏览器不获得 RUNNER 凭据，不提供任意运行身份切换。

`source_ref`使用工具实际返回的短引用：Geometry版本`gv_`、Family版本`fv_`、inspection
`gi_`。入口在上述可信RUNNER身份中复用`OpaqueReferenceService`及原索引展开，再交给
原display服务。短引用不改变业务权限、准确源或manifest幂等身份；同一个准确源的短引用
和内部认证引用命中同一已保存结果，不重复生成PNG/STL。错误类型、拼写或Scope不可见返回404。
显示API返回的`source.source_ref`也在同Scope投影为短引用；磁盘manifest仍保存领域准确引用，
仅返回对象中的此字段被投影，资产、几何摘要与幂等身份不变。

同 TENANT 的新 Task 沿 Catalog 重新发现正式版本，获得自己范围内的准确引用，然后生成
自己的显示 ArtifactVersion。旧 Task 的 opaque 引用、私有 Operation、inspection 和显示资产
不因此跨 Task 公开。正式资源的共享规则仍由 Catalog、Repository、RLS 和 Storage 共同执行。

## 投影与持久化

`SavedGeometryDisplayService` 从原 Catalog / Inspection / Asset owner 读取 CanonicalGeometry。
PNG 调用既有 GeometryRuntime render；STL 通过既有受监督 Codec 的
`read_geometry_payload → mesh_for_render` 导出。两者使用同一 BREP 和 geometry_sha256；
不调用 build_fixed、realize_geometry，不运行作者 Query 代码，也不建立另一份可编辑几何权威。

PNG、STL 和最后写入的 manifest 沿原 Artifact/Storage 保存。幂等键绑定具体 Task/Run、准确
来源和格式；失败重试可复用已经写入的同源 PNG，未完成配对时没有可用 manifest。
读取已有 manifest 同时校验源、实体摘要及两个 ArtifactVersion 的 Task/Run、kind 和元数据。
错误不会被占位图片或模型替代。

输出只包含经过授权的 ArtifactVersion 内容 URL、字节摘要、准确来源及显示警告，不暴露
StorageKey 或本地路径。数据保持 METRE 和保存实体原坐标；前端旋转、缩放和相机 fit 不改变
下载模型的工程坐标。STL 是三角网格近似，不能代替精确 Query、IFC 安装或物理连接证明。

## 前端与失败边界

聊天成果区仅从成功 `ifc_authoring_publish` 返回的 `geometry_versions[*].resource_version_alias`
和 `family_version_alias` 选择显示源，不递归扫描其他返回引用。Info、Catalog、Operation
读取历史成果时不会自动将它们贴成本轮交付。已有 manifest 优先读取，缺失时调用投影；
资源列表不批量为整个 TENANT 建网格。
这是前端的结果展示规则，并非 Agent 在最终回复里指定插入卡片。
成功发布时仅记住该工具条目的准确显示源与事件序号；收到该轮成功保存的 Role Card 后，
才读取构件信息和请求展示投影。最终 Main 回复、工作记录在前，三维成果卡在后。
Main 的 `final_answer`、暂停、Run 状态或模型收尾活动本身不触发展示加载。
实时消息、快照与历史分页均使用已保存 Card 的序号区分结束轮次，上一轮 Card 不释放下一轮成果。
重复消息不会重复加载成功或失败的展示，失败后可单独重试原显示源。
每张卡片显示构件名称；信息尚未返回时显示资源类型。加载过程分别说明读取构件信息、
读取已有三维展示或从保存模型准备展示。展示失败保留名称、实际失败阶段与本轮回复。
Preview 工具只显示该次返回的图片块及原工具详情：不扫描其 inspection 或依赖版本引用，
不为保存案例额外请求 PNG/STL，不生成“保存的模型”图库。后端 inspection 读取能力保留，
但聊天前端不自动把 inspection 投影成三维卡片。

对话复用原城市任务的 `agent-turn / role-run / role-final` 和工作卡组件：真实 Card 作为
本轮结束边界，最终 Main 正文置于折叠过程之外；尚未结束的 Main 文字与生成状态仍实时可见。
空的纯工具调用 AI 消息不生成空头像行，但工具交换、摘要和中间文字仍可在过程里查看。
Main 提示词要求实质工作前说明目的、结果后说明发现/问题/下一步。正文来自真实保存消息，
不由前端补造；保存的 `commentary` 留在工作过程，`final_answer` 进入最终回复区域。
`main_message_id` 来自原请求事件的已保存 Main 身份；READ/WRITE 来自注册 binding 在
AIMessage/ToolMessage 中保存的可信 metadata。同一 Main 的连续 READ 由城市回放既有
`renderActivityGroup` 合组；WRITE、缺失关联/效果、不同 Main、正文和压缩事件形成边界。
工具 start/result 沿原 call_id 更新同一行，不能把恢复后的结果计成第二次调用。
活跃角色首次创建默认展开；后续消息不覆盖用户已选择的折叠状态。正在思考/压缩的状态
跟随原 Main/summary 事件，计数请求不产生压缩进度，也不创建第二个角色框。
本次 Preview 返回的全部图片归入对应工具过程，沿原 Preview 小图入口打开。
Geometry / Family 成果卡跟随回复展示，标签为“几何”“产品族”，直接挂载三维视图；
与城市任务复用 `renderModelViewer`、`family-delivery` 样式和原 STL viewer，支持拖动、
缩放与复位，刷新回读原 manifest 和 ArtifactVersion。卡片不再提供“查看三维与下载”
折叠入口、原始 STL 下载链接或来源 JSON；“在几何与产品族中查看”携带当前 Task/Run 与
准确 `gv_` / `fv_` 进入原库页详情，切换任务时释放 viewer。
这只改变现有消息和资产的展示，不改变保存内容、工具返回或压缩机制。

消息订阅断线时保留已加载消息并显示连接提示；重连成功后移除提示。重连仅恢复 SSE 读取，
不启动、继续或重做 Run。页面有历史内容不构成后台当前在线的证明。
窄屏通过顶栏任务导航开关显示原 sidebar，复用同一新建入口与任务列表；选择任务或新建
后收起，遮罩、关闭按钮及 Escape 均可关闭，不维护另一份移动端任务状态。

无身份返回401，明确访问拒绝403，不可见源或错误 Task/Run 返回404，完整性冲突409，
不支持的源或实际渲染/网格失败422。失败只影响该显示投影，不改变原正式版本或保存实体。
通过 inspection 再次 Preview 是观察同一实体，不承诺重现历史图片字节。

## 几何与产品族库、源码

实时 Component 的库页沿同一代理访问 `/library/geometry`、`/library/families` 及各自
`/info`。后台直接复用 GeometryCatalog、ProductFamilyCatalog、CodeDiscovery 和引用索引，
没有第二个资源 Repository 或前端发布库存。列表的权限、分页、版本及 Geometry 依赖均来自
当前 Catalog；源不存在、无权限或空列表不退回静态样例。原城市回放明确使用 `source=city`
的既有展示资料；实时路径为 `source=live`，不混合两种数据。

当前 Task/Run 范围下，Info 返回原 Catalog、已发布源码 CodeCard 和可显示的准确保存源。
详情自动读取所选版本模型，缺失时复用原 display 投影入口。列表只 GET 当前已加载页面的
现有 PNG，不因打开列表批量 POST 生成模型。版本切换重新读取准确源，不复用另一个版本图。
参数化 Geometry 定义没有 canonical 实体。现有 Info 沿该版本的 publication、准确草稿快照、
Geometry Preview 报告选择一个参数严格等于已发布合同默认值的保存 inspection，返回
`display_source` 及案例说明；同一快照的报告按页读取，命中即停。页面标明“默认参数的保存预览”，
库导航仍指向原发布版本。不使用 Family 整体代替 Geometry，也不重新执行作者源码。
没有准确保存案例时返回空源与中文说明，页面不发起无意义的显示重试；损坏资产仍按完整性错误处理。
无 Task/Run 时可按已认证 USER 查询可见 Catalog；源码与模型需要进入现有 Component 任务
取得其可信范围，不临时创建 Task/Run 或注入 Runner 凭据。

库页仅为 Geometry 提供源码入口；Family 详情不展示源码区或源码按钮。Family 的保存源码与
既有 CodeDiscovery 能力仍用于执行和修订，不因移除页面入口而删除。源码沿 `/library/code`
使用原 CodeReadSelection 与 CodeDiscovery 有界读取，只允许已发布 Geometry/Family 程序。
文件列表来自该 CodeCard 的真实 `members`，支持多文件，非文本成员
不作为源码读取。居中弹窗显示资源名、版本、文件切换、独立行号和分页；源码转义为文本，
不执行、编辑、预载整个源码包。Family 版本沿原游标分页；Geometry 保持当前最近版本窗口，
不把未取得的历史版本伪装成完整列表。

详情的显示读取区分 GET 读取已有结果与 GET 404 后 POST 首次生成：分别显示“正在读取保存的模型”
和“首次打开，正在准备三维预览”。显示 GET 最多等待 30 秒、POST 最多等待 120 秒；失败退出
加载状态并提供“重新读取”。手动重试仍先 GET 已保存结果，随后才按原幂等投影路径处理缺失，
不自动反复提交生成。返回模型后沿同一查看器显示，不改变原几何或重新运行作者程序。

## 资源库删除

`DELETE /library/{geometry|families}?reference=<root>` 沿原认证、Task/Run 范围解析、引用索引、
Catalog 和 Repository 操作。接受资源根引用，删除的是整个资源及其全部版本的在线可用性，
不是只移除前端卡片或删除单个版本；成功返回 204。详情使用应用内确认弹窗，成功后清除
页面缓存并重新读取目录，失败保留详情与后台返回的原因。

- SYSTEM 资源始终不可删除，后端返回 403，前端禁用入口。
- 产品族可以单独删除，不连带删除它依赖的几何。
- 几何被任何未删除产品族的任一已发布版本引用时返回 409；已有 Geometry input 引用也受保护。
- 原资源仍有 prepared 发布操作时返回 409，先处理原操作，避免删除与发布交叉。
- 不可见、错误引用或已删除资源返回 404，不披露其他租户/任务的资源身份。

唯一删除状态是既有 `geometry_resources` / `product_families` 根记录的 `deleted_at`；不建立
第二份删除登记表。原版本、资产、Operation 历史保留，RLS 使已删除根及其版本退出正常读取。
删除与新增版本、Family binding、Geometry input 通过同一资源根的事务锁协调，防止并发新增引用
绕过删除前的依赖检查。正常角色只获得窄删除函数权限，不获得任意 UPDATE/DELETE 权限。
