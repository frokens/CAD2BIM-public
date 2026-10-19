# TENANT 作者资源持久化与发布权限

本文描述当前 Geometry / Family 作者链的 TENANT 持久化边界。通用草稿、Preview、正式发布和原请求恢复仍遵循[作者草稿与发布合同](authoring-drafts-preview-and-publication.md)。本次验证及未验边界见数据库验证记录。

## 1. 唯一事实与可信入口

Geometry / Family 的所有权保存在原资源根的 `owner_scope`、`owner_tenant_id`、`owner_task_id`。TENANT 根只有 tenant 身份，`owner_task_id` 为 NULL；草稿 Artifact、Run 和 Operation 保留原技术 Task、owner、principal 和请求身份。没有独立共享目录、草稿表、跨 Task Operation 服务或第二个事务 owner。

生产组合根 `ifc_application_resources.resolve_authoring_resource_owner` 通过原 `get_run_with_execution_identity` 读取服务器执行身份。GRAPH 调用的 owner、Task、tenant、principal 须与 Run 一致；Component Run 还须有 PINNED 执行协议，其 module 和 stage 与 Run 相符，才沿现有 `ResourceOwnerScope` 选择当前认证 tenant 的 TENANT 归属。Direct 与其他 TASK 作者入口继续各自的 TASK 归属；公开 Task metadata 不承担权限授权。

`AuthoringDraftService` 负责不可变 Artifact 快照与密封引用；Geometry / Family AuthoringService 校验各自内容；原 PostgreSQL Repository 负责根 CAS、发布锁、正式资源和 Operation 的一次原子提交。新 Task 从 Catalog 或原草稿读取入口发现资源，并为自己的当前 scope 签发引用。

## 2. 原数据库权限上的增量

迁移 `20261002_0107` 接续合并点 `20261002_0106`，调整现有 RLS 与 guard，不新增业务表。正常 runtime 写入须携带有效技术 Task；可写同 tenant TENANT 根或本人 TASK 根。SYSTEM 的 curator 路径沿原权限。

| 原存储职责 | TENANT 行为 |
| --- | --- |
| Geometry / Family 根 INSERT、草稿 UPDATE | MCP / Runner runtime 可写当前 tenant 的 TENANT 根；TASK 仍精确限制当前 Task |
| G/F 正式 version、asset、Family binding INSERT | 沿被授权原资源根检查 owner；不绕过正式资源不可变约束 |
| Geometry named input INSERT | target 必须是准确 `mapped_model` 版本，并存在 `canonical_geometry` / `occt_brep_v1` 资产；TENANT consumer 仅接 SYSTEM 或同 tenant TENANT target |
| Family → Geometry binding | 保留原 scope-width guard；TENANT Family 不能绑定 TASK Geometry |
| API runtime | 可读取当前 tenant 的 TENANT 根及准确正式资源；无上述正式表的 INSERT 权限，也无根的草稿 UPDATE 权限 |

更窄依赖在 Application 解析与数据库各自的原边界拒绝。TENANT G → TASK G input 被 RLS 拒绝；TENANT F → TASK G binding 被原 Family trigger 拒绝。跨 tenant 的根、版本和快照均不可见，不因字节相同或资源名称相同获得权限。

## 3. 准确草稿快照

同 tenant 的另一个 Task 仅能取得已授权 TENANT 根的当前准确草稿快照，或合法 PREPARED 发布闭包中该根冻结选择的准确快照。跨 Task 授权检查 Artifact kind / status、draft content type、version、artifact、sha256 与根或冻结选择的一致性。作者保存仍产生当前技术 Task 的新不可变 Artifact；草稿 root guard 保留该来源检查。

原 Artifact RLS 的 source lookup 增加这项有限授权，`artifact_versions` 的 restrictive policy 再约束到准确版本。仅有同 tenant Artifact 身份不足以读其他 Task 的 draft bytes；同一 Artifact 的其他非当前、非冻结版本也不会因共享根而全部开放。本人 Task 的原私有读取权限继续适用。

Geometry draft Preview 复用原 inspection 创建 owner：Repository 的 `_require_inspection_source` 与数据库 inspection guard 均接受原 owner / Task 或该准确 TENANT 快照授权。检查仍固定 tenant、artifact、version、sha256、kind。inspection request、报告和 case-report 没有因此取得跨 Task 共享资格。

## 4. 有限发布查询

四个 PostgreSQL 函数只为原 repository 提供受控事实查询。函数使用固定 schema 的 search path 和 SECURITY DEFINER，禁止 PUBLIC 调用；不返回另一 Task 的 Operation 本体、alias、Run 或作者草稿定义。

| 函数 | 消费者与返回 |
| --- | --- |
| `cad2bim_authoring_tenant_snapshot_visible(version_id text)` | Artifact RLS、inspection owner 和 guard；返回该准确快照是否可见。MCP / Runner / API / curator 可调用 |
| `cad2bim_authoring_publication_fence(root_kind text, root_id text)` | 原发布 repository 与 guard；返回任意 PREPARED 闭包是否冻结该已授权 TENANT 根。仅 MCP / Runner 可调用 |
| `cad2bim_authoring_publication_result(root_kind text, root_id text, expected_drafts jsonb)` | 原发布 repository；当前全部选择仍匹配根的 generation 与准确快照，且完整闭包与已成功发布选择相同时，仅返回 G version IDs 与可选 `family_id` / `family_version_id`。仅 MCP / Runner 可调用 |
| `cad2bim_authoring_geometry_draft_result(root_id text, generation bigint, snapshot jsonb)` | 原 G repository；准确当前 G 草稿已经作为 G 或 Family 闭包发布时，仅返回 `resource_id` / `resource_version_id`。仅 MCP / Runner 可调用 |

原 Operation RLS 保持 Task 隔离。跨 Task 的新请求在自身 Task / Run 下创建原合同的 no-op 回执，并从有权读取的正式 Family bundle 与准确 G bindings 重建公开结果。原 Task 原请求重放则返回自己的原 Operation 与原准确结果；不会改成另一 Task 的操作，也不会增加重复正式版本。

## 5. 发布锁与恢复

Application 按固定顺序取得原根锁后，对完整 closure 的每个选定根检查 PREPARED fence。数据库 publication guard 同样按固定 root 顺序锁定并检查，防止绕过 repository 的直接 INSERT。Family 发布中冻结的 G 会挡住另一 Task 对该 G 的发布。

fence 函数使用 VOLATILE 查询，使 advisory-lock 等待后的调用看到已提交的 PREPARED 事实。返回只有 boolean；跨 Task fence 错误不附带旧 Operation alias。尚未解决的原请求沿原身份恢复或对账，不能用新 Task 或新请求绕过。

publication guard 接受已授权 TENANT 根来自另一技术 Task 的快照，但仍核对当前根 generation、准确快照身份和内容类型。Worker 执行与资产准备在正式提交事务外；G/F version、asset、bindings 与发布 Operation 沿原 Repository 原子提交。确定的提交拒绝不得留下部分正式 G/F 版本。

## 6. 迁移与回退

必须真实运行 Alembic upgrade，保留两个分支的原 revision 身份，经 `20261002_0106` 汇合后进入 `20261002_0107`；不能通过 stamp 代替缺失 migration。`Database.EXPECTED_SCHEMA_REVISION` 仅承担真实数据库 schema readiness 检查。

0107 不支持反向迁移共享作者根；`downgrade` 明确拒绝。需退回时恢复升级前数据库备份，不能修改已执行 migration 或把既有共享根伪装回 TASK。全 head 的 offline SQL 导出还受既有动态数据库函数查验限制；本合同的数据库证据是隔离 PostgreSQL 上的真实在线升级。
