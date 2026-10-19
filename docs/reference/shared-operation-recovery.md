# 共享 Application 原动作恢复合同

本合同说明当前 Data Transform、Geometry/Family 草稿保存与发布、显式 Geometry Inspection 保存
与 IFC 写入的持久化恢复边界。
Direct 与 Runner 复用同一 Application Core、Domain DTO、Repository 和 Worker 能力；
入口协议不建立第二套业务结果或事务所有者。这里的恢复是已受理原动作的继续与回读，
不是新修补、新修订或后台 Job 轮询。

## 1. 唯一业务权威与实现所有者

关键发布、Authoring 和 IFC 写入使用 `CriticalOperationRecord`；Operation 保存原请求、
exact 输入与输出引用、状态和有界失败事实。Package、资源版本、Artifact 和 IFC revision
仍由各自已有表与 Storage 保存。Operation 的结果引用、已选候选和业务结果投影服务于
同一动作的确定性恢复，不构成独立可编辑事实源。

| 能力 | Application 所有者 | 持久化所有者 |
|---|---|---|
| Data Transform/repair | [DataTransformService](../../src/CAD2BIM_backend/application/data_transform_service.py) | [Data Runtime Repository](../../src/CAD2BIM_backend/infrastructure/postgres/repository_data_runtime.py) |
| Geometry/Family 草稿 | 原两 AuthoringService → [AuthoringDraftService](../../src/CAD2BIM_backend/application/authoring_draft_service.py) | [Draft Repository](../../src/CAD2BIM_backend/infrastructure/postgres/repository_authoring_draft.py) 与现 Artifact |
| Geometry/Family 发布 | [AuthoringPublicationService](../../src/CAD2BIM_backend/application/authoring_publication_service.py) | 原 Geometry/Family Repository 与 Data Runtime Operation |
| 同次 Geometry/Family 结果保存 | [GeometryInspectionService](../../src/CAD2BIM_backend/application/geometry_inspection_service.py) | 现 [Data Runtime Repository](../../src/CAD2BIM_backend/infrastructure/postgres/repository_data_runtime.py) 保存原Operation；现 [ArtifactService](../../src/CAD2BIM_backend/application/artifact_service.py) 保存准确请求和完整结果bundle |
| IFC 写入 | [IfcRevisionWriter](../../src/CAD2BIM_backend/application/ifc_revision_writer.py) | [IFC Repository](../../src/CAD2BIM_backend/infrastructure/postgres/repository_ifc_model.py) |

普通查询不创建关键 Operation；Data/IFC Query 的原 attempt 与 exact 输入恢复沿各自查询合同。
组合根由 [Data resources](../../src/CAD2BIM_backend/data_application_resources.py) 和
[IFC resources](../../src/CAD2BIM_backend/ifc_application_resources.py) 提供共享服务。

草稿 Write 卡片返回 `authoring_draft_saved`、准确 draft_ref 和原 edit_token；Publish 卡片返回 `authoring_published` 与正式 G/F 引用。回读旧保存动作不读取当前草稿来冒充原结果。完整生命周期见[草稿、Preview 与发布](authoring-drafts-preview-and-publication.md)。

Preview case_report 的列表只读有界 metadata；get 按 case_key 或 after_case_key/page_size 读取原案例输入及实际输出。FAILED 批次允许读取已保存 partial 报告，明确 completed / technical_error / not_run；未运行不能记 PASS，未保存报告不能伪造结果。

List可按operation_kind、tool_name和outcome在分页前筛选，案例卡给出小型索引及保存模型可用性，不读取BREP。Get的`mode=operation`使用当前Run的operation_alias；`mode=operation_reference`使用已授权的operation_ref回查同Task原操作；`mode=inspection`从inspection_ref找回原案例且不接收分页字段。不同Run的数字alias不会混用，不能通过来源链接扩大Scope。

G/F准确版本Info返回可访问的发布来源，草稿Info返回原保存来源。Publish详情返回正式版本和可选Preview示例关联；它们是回查入口，不是正式资产来源。未记录或无权读取的来源明确不可用，不猜测历史输入。正式标准模型观察/Query直接读取Family版本资产，不需要原作者私有操作可见。

Preview/Operation公开结果提供源引用、依赖引用、case输入、求解与形体/接口检查、可处理诊断；不暴露内部snapshot_id、运行配置和完整性摘要。内部Domain与Artifact保留完整事实，Direct/Runner使用同一公开投影；任意业务参数不按名称删键。源码修订所需CAS摘要不受影响。

## 2. 原动作 K 与受理边界

K 是入口固定的动作身份，落到 `request_identity` 或写入的 `idempotency_key`。
恢复先查同 Scope 内的原 K，并核对保存的规范化请求 digest/fingerprint；同 K 改参数拒绝。
已有动作使用保存的 exact Package、Artifact、资源版本、父 Operation 或 IFC revision，
不重新解析已变化的 Alias、读取全局 latest，或以新 CURRENT 替换原动作输入。

新请求的 Provider 类型、Scope/权限、输入引用和领域基础合同验证失败，尚未受理时不建 Operation。
Data Transform 固定原 K、规范化请求、exact 输入、CODE/DESCRIPTION Artifact 和执行期限后，
先持久化 PREPARED，再 claim 并进入 Worker staging、源码验证与执行。
受理后的 staging、capacity 或执行超时属于该原 Operation 的执行失败；不发布 partial Package。
Geometry/Family 的源码与发布草案、IFC 的 before/after revision 与 CURRENT CAS 条件
也在各自 prepare 边界固定，后续执行不能悄悄换成新的请求。

同 K 的响应丢失重放只恢复或回读同一 Operation。终态失败不会因原 K 重放而再次执行。对已确认 FAILED 的 Authoring Publish，修复实际失败原因后，新 K 可使用与原请求完全相同的草稿、编辑令牌和可选 Preview 关联；改动这些字段不是取得新请求资格的条件。PREPARED 或结果未明时先恢复或对账原 Operation，不能仅换 K 并行重做。
修补或修订采用新 K、新 Operation，并明确引用 exact 父结果；同补丁使用新 K 也是新动作。
源码检查与修补的具体输入见 Data repair 合同。

## 3. claim、监督与发布

claim 保存执行 owner、generation 和凭证；发布与终态写入校验当前 claim，旧 owner 不能提交。
Data 的恢复还核对原执行身份与 lease；Geometry/Family 在原执行进程已停止且
owner/generation 未变时才能 reclaim，复用同一 Operation 和保存的发布草案。
这些机制复用 [Data Runtime claim 实现](../../src/CAD2BIM_backend/infrastructure/postgres/repository_data_runtime.py)，
不把某个领域的进程协议或预算推广为所有 Worker 的共同合同。

IFC claim 另保存固定 `deadline_at`，恢复只使用剩余期限，不重新获得完整预算。
Runtime 把 guardian PID 与进程出生时间登记到原 claim；回收必须核查原执行及 guardian，
避免旧子进程仍在运行时启动竞争写入。执行退出沿监督链回收并 retire 原 claim。
实现边界见 [IFC Runtime adapter](../../src/CAD2BIM_backend/infrastructure/ifc_revision_runtime/adapter.py)
与 [Worker supervisor](../../src/CAD2BIM_backend/infrastructure/worker_runtime/supervisor.py)。

完整候选保存后，恢复优先完成该候选的发布与回读。IFC selected candidate 固定
origin generation、upload ID、字节 SHA/size、inspection 及必要 Project/Spatial 结果，
不能由恢复者替换；提交同时校验 claim 与原 CURRENT CAS。
Project/Spatial Operation 保存的 summary/result 是原动作结果本身，用于确定性回读。
具体 revision、Storage 完整性与事务顺序见 [IFC durable kernel](ifc-durable-kernel.md)。

## 4. 终态与 unknown

SUCCEEDED 必须能回读原动作的 exact 结果；即使 CURRENT 或 Catalog 已推进，仍返回原结果。
FAILED 必须来自持久化终态，返回原错误码、阶段、Operation 引用及适用的失败 blockers。
终态不可改写，重复调用不创建第二份发布结果。

Geometry Inspection 在执行前固定同Scope的原K、准确来源版本和规范化sample/realization请求，
复用现PREPARED、执行claim和终态发布；一次性Preview及已有引用的观察不创建该Operation。
完整结果Artifact包含同次world/local片段、实际receipt、接口和检查事实。保存成功而render失败
仍返回同一结果与Operation引用。终态提交响应丢失时沿原Operation的状态、fingerprint、结果及
错误阶段回读；完整原件已保存且原owner可合法回收时才继续完成原发布，不重新构建来源。
没有完整原件的未决动作保持原身份并要求对账，不能把读取失败推断为可再执行。
原请求详情由现operation_get读取，列表仅投影准确结果引用。错误文字复用现Data Runtime的
2000 UTF-8字节规范，避免多字节错误使已受理动作停留在PREPARED。

超时、断连、取消、读取失败、claim 冲突或 `retryable=false` 本身不证明领域写入失败。
尚不能确认原终态时保留 K 与请求，按领域返回 reconciliation/unknown；不猜测 FAILED，
不创建新 K 重做，也不把私有候选当作已发布结果。调用方失联不撤销已受理领域动作。
终态投影只公开当前合同允许的信息，不泄露 claim token、guardian 或私有 Storage 路径。

Direct输入引用解析失败使用`REFERENCE_INVALID / preflight`。业务调用已返回后，短引用
输出转换失败使用`OUTPUT_REFERENCE_PROJECTION_FAILED / output_validation`，携带原结果已有的
`operation_alias`及`tool_result_returned=true`；调用者保留原请求，通过原Operation确认并读回结果。
业务结果与终态继续由原Operation持有。原业务错误的诊断引用转换失败时，保留原错误与原诊断。

Direct关闭先调用现有`IfcWriteCoordinator.close()`停止接纳写入，并有界等待、取消和收拢
已受理的owner，然后释放Worker和数据库。等待调用方取消时，已受理写入仍按原协调器和
Writer的settlement逻辑完成持久化收尾。协调器报告owner尚未退出时，关闭操作以错误结束，
IFC运行依赖保留，调用方应按原Operation核实结果。Backend的正常关闭与启动失败清理均
保证Runner数据库池释放异常时，API数据库池仍获得清理机会；清理错误继续向调用方传播。

## 5. Direct 与 Runner 入口

Direct 的 session Alias、受控 `source_path` 与服务器调用身份属于 Direct 适配器。
Runner 的持久 pending-call/command 身份进入共享 K；恢复不因重启或 generation 变化重新生成 K。
Runner Provider、State、Card 和 Context 不包含 Direct session/local-path 字段。
两种入口确定性投影到共享 Domain 请求；[内部领域授权入口](../../src/CAD2BIM_backend/internal_domain_api.py)
仍核对 Task/Run、tenant/principal、Runner claim 与写权限，State 读取授权不能替代领域写授权。

## 6. 当前合同与必要完整性检查

Artifact普通下载直接复用Storage一次`read_verified`返回的字节、元数据和已核摘要，将同次结果
与数据库ArtifactVersion的key、名称、类型、长度和摘要对应后交付；实际字节完整性由原Storage
实现负责。流式下载继续使用其已有`expected`元数据约束。

恢复使用当前Family严格DTO，不依赖人工schema/ABI代号；Data Transform保持
128 KiB 源码上限及当前 Schema/参数合同；不恢复退役的 IFC products apply DTO/Writer/Runtime 分支。
Product batch 保持 900 秒执行链预算、256 MiB 累计输出上限及完整已接收结果前缀保全：
完整帧先接受，经 Codec 验证后返回，随后传播原 Product 错误；半条结果不进入前缀，
取消不启动新的 Codec。逐项与排队预算仍独立，见 [Family Runtime 合同](product-family-authoring-and-realization-runtime.md)
及 [执行预算定义](../../src/CAD2BIM_backend/domain/runtime_execution.py)。

保留有消费者的 hash：exact 资产字节完整性、资源内容/绑定与版本寻址、原请求冲突检测、
源码修补 CAS、已选候选完整性及实际缓存键。后台源码树、依赖锁、工具声明或运行环境
整体指纹不决定已注册 Family/Geometry 的执行资格；无关实现或预算变化不要求全族重注册。
执行仍检查实际参数、权限、必要依赖、资源字节和结果，见 实施 Runbook。

Backend SYSTEM exact复制已经沿原Family Operation、claim、Artifact/Storage与Repository
接入，权限、完整依赖计划及管理CLI见[Family后台复制合同](product-family-authoring-and-realization-runtime.md#backend-system-exact复制)。
其顺序是先保存所有目标映射，再逐G发布，最后F原子提交；原K恢复保留已完成G，不重读
source或重跑Runtime。来源技术故障不判FAILED，同K改参数仍拒绝，保存的G/Family资产
必须重新核实。Data专属input记录只由DATA_TRANSFORM投影读取，其他Operation不借用
该领域表。正式库批量发布与业务接纳仍按当前阶段规划
另行核收，不由工程恢复证据自动实现或授权。
