# Geometry Resource Catalog Kernel as-built合同

```text
status: committed_and_verified_current
implemented_slice: I0-B3.01
database_head: 20260902_0066
public_mcp_tools_added: 0
product_family_tables_added: 0
```

本文记录I0-B3.01已实现的几何资源持久化内核。它建立可被后续SYSTEM seed、Catalog、
TASK Authoring和ProductFamily引用的机器身份，不注册Provider DTO、MCP工具、Worker或ProductFamily表。

## 1. 唯一权威与责任分工

本轮版本使用说明增量已实现、原生能力验收待完成：原version.manifest保存usage_instructions，纳入既有
manifest/content digest；mapped manifest asset仍只是确定性投影。exact info完整读取，历史
缺失为null，旧manifest/asset/digest不回写。该增量已实现且原生能力验收待完成；完整写入、继承、限额和验收只在
系统基线§4.5维护，
不新增说明Repository或事实源，也不把以下历史内核通过追溯为该增量已通过。

一个可复用几何资源由三层机器身份表达：

```text
GeometryResource                         稳定根身份
└── GeometryResourceVersion              不可变精确版本
    └── GeometryResourceVersionAsset[]   版本必需字节的精确绑定
```

- PostgreSQL是resource root、version、version-asset metadata、Scope、lineage和digest的唯一机器权威；
- 现有`StoragePort`/`LocalStorage`是不可变asset bytes的唯一存储边界；
- `ResourceStorageKey`由Scope、exact resource version和asset role确定，数据库不保存可漂移的物理路径；
- `Artifact/ArtifactVersion`继续承担Task/Run产物，没有被扩张为SYSTEM/TENANT资源库；
- Geometry Resource没有CURRENT指针，消费者精确引用`resource_version_id`；
- `content_digest`绑定resource kind、canonical manifest digest和排序后的exact asset元数据/摘要，
  origin、lineage、validation summary和timestamp不冒充内容身份。
- `version.manifest`是manifest语义的数据库权威；`mapped_model`的`manifest` asset是其RFC 8785
  canonical bytes。Bundle强制asset SHA-256等于`manifest_digest`且size等于canonical bytes长度，
  因此两种投影无法分叉。

实现入口：

- Domain：`src/CAD2BIM_backend/domain/geometry_resource.py`；
- Repository Port：`src/CAD2BIM_backend/ports/geometry_resources.py`；
- Application完整性边界：`src/CAD2BIM_backend/application/geometry_resource_service.py`；
- PostgreSQL adapter：`src/CAD2BIM_backend/infrastructure/postgres/repository_geometry_resource.py`；
- 唯一组合根：`PostgresControlPlaneRepository`继续组合新mixin，没有第二个Repository实例体系；
- Storage扩展：`src/CAD2BIM_backend/ports/storage.py`与现有`LocalStorage`。
- `GeometryResourceService`由现有Direct `bootstrap.py`使用同一Repository和Storage实例组装为内部
  runtime resource；本切片没有把它注册成Provider工具。

## 2. 资源类型与必需asset

当前只有两种消费语义：

- `mapped_model`：必须绑定`canonical_geometry`和`manifest`；
- `modeling_function`：必须绑定`source_bundle`和`parameter_schema`。

当前正式资源不保留`validation_report`或注册样例资格清单；发布和读取复用同一正式资产
约束。执行时按实际输入、准确资产和结果检查，不要求`runtime_manifest`或后台源码指纹。

`preview`、`inspection`和按kind允许的其他asset是精确version的可重建投影。它们继续绑定
exact version，但不单独定义resource身份。`generated_model`不是第三种kind：一次代码生成后固定的
几何归入`mapped_model`，可重复接受参数并生成几何的能力归入`modeling_function`。

## 3. Scope与RLS

resource root显式保存`SYSTEM | TENANT | TASK`所有权；version和asset通过外键归属该root。

运行时读取可见集固定为：

```text
TASK caller   → own TASK + own TENANT + SYSTEM
other Task    → its own TASK + same TENANT + SYSTEM
other Tenant  → its own TASK/TENANT + SYSTEM
```

API、Runner和MCP runtime都是`NOSUPERUSER`/`NOBYPASSRLS`。Runner和MCP写入须携带有效技术
Task，可写同tenant的TENANT根及其version/asset，或本人当前TASK根；SYSTEM发布仍仅允许
受信`cad2bim_system_curator`。API runtime可读授权资源，没有这些正式表的INSERT权限。
准确的TENANT增量、root草稿UPDATE及scope-width约束见
[TENANT作者资源持久化](tenant-authoring-persistence.md#2-原数据库权限上的增量)。
正式version/asset保持不可变，不授予UPDATE或DELETE。公开所有权由可信入口选择，底层
TENANT授权不允许Agent任意提交owner。越权Scope的ID读取统一表现为不可见，不泄漏资源是否存在。

## 4. 发布、幂等与响应丢失对账

发布顺序固定为：

```text
入口绑定server-only TenantExecutionContext
→ Repository invocation gate核对active context、actor与resource owner Scope
→ 授权通过后，上游候选asset才能被Application从immutable Storage读取
→ GeometryResourceService按ResourceStorageKey调用StoragePort.read_verified
→ 校验key/filename/content-type/size及数据库SHA与verified bytes的绑定
→ 同一PostgreSQL事务创建或核对root
→ 追加一个严格单调的immutable version
→ 绑定全部version assets
→ 事务内重读并核对publication
→ 事务外exact DB重读和Storage再校验
```

Repository在resource identity上使用transaction-scoped PostgreSQL advisory lock，保证同一resource
版本号的顺序判定，同时保持runtime无UPDATE权限。第一版必须为1，后续只接受
`max(version_no) + 1`。同一`resource_version_id`和相同内容是exact replay；同一身份但内容
不同是幂等冲突。数据库异常后，Repository通过同一Scope下的exact read判断提交是否已成功；
只有完全一致的publication才能对账为成功。

Application在任何Storage读取之前调用Repository的同一invocation gate；Repository在进入
数据库事务时再执行该门禁。未绑定active invocation context、context不匹配或无权Scope
都在Storage I/O之前fail closed，防止候选字节的存在性或digest变成跨Scope信息通道。

`StoragePort.read_verified`返回`VerifiedStorageRead(stored, content, verified_sha256)`，由Storage
对同次读取字节完成校验，Application继续核对数据库拥有的exact key、角色、文件名、MIME、大小和SHA。
LocalStorage从初始化时记录的root device/inode开始逐段以目录FD及O_NOFOLLOW打开version目录；
metadata和payload都从同一version目录FD打开并要求regular file。metadata最多64 KiB，payload读取
与SHA计算共用同一FD；前后核对inode/size/mtime/ctime及version路径身份，拒绝损坏、截断、symlink、
替换和非普通文件。非阻塞打开确保FIFO无writer时也能受控拒绝。
普通`read`复用该verified路径；`stat`和`iter_chunks`保持既有合同，大IFC流式读取未改。
GeometrySource复用Resource exact读取的已验证字节，不再重复整包SHA；源码codec自己的canonical
格式和成员检查保留。真实资产回归中，两种exact reader的filesystem payload读取均从3N降为N，
只保留一次Storage SHA，不再额外计算Application SHA；这不等于物理磁盘读次数的测量。

Storage候选缺失、metadata不符或digest不符时，Application Service不进入数据库发布；因此不会
暴露partial version。当前Goal没有为几何资源创建Operation；`.03`的Authoring attempt和其安全诊断
是后续独立业务职责。

## 5. Migration与旧Product-Core退出

- `20260902_0065`对`run_component_views`、`component_catalog_events`、
  `component_publication_receipts`和`component_promotions`取`ACCESS EXCLUSIVE`锁；
- migration精确统计四表，任一非空就以SQLSTATE `23514` fail closed并报告每表行数；
- 只有全部为空才按依赖顺序删除四表；不猜测旧Variant/Receipt语义；
- `0065`不可降级；需要回退时恢复精确`0064`数据库备份；
- `20260902_0066`创建三张Geometry Resource表、约束、索引、强制RLS和最小权限；
- `0066`只允许在三张新表全空时降级，防止静默丢失新资源；
- 旧Alembic历史保留，活跃ORM映射和产品Repository消费者已移除。

## 6. 已验证能力与后续边界

当前聚焦门禁验证：

- fresh base→`0066`、空`0064`→`0066`、四张旧表任一非空时fail closed；
- 两种resource kind、Scope形状、必需asset、canonical manifest/content digest和不可变合同；
- 候选asset缺失或摘要错误时数据库零可见行；
- exact replay、身份冲突、严格版本号、不可变Storage冲突和重启后exact bytes回读；
- SYSTEM/TENANT/TASK可见矩阵，API/Runner/MCP的非超级用户RLS与跨Scope防泄漏；
- 现有IFC、CAD/Data和Golden Seed正式门禁回归；
- 静态审计确认没有第二Repository/Storage/事务事实源、ProductFamily表或新Provider工具。

I0-B3.02已从这个内核实现manifest-driven SYSTEM seed、ready Catalog、preview和source-first
几何调查，并复用本文的exact resource version、Scope/RLS、Storage和Repository。当前as-built见
[`Geometry Resource Catalog与调查合同`](geometry-resource-catalog-and-investigation.md)。当前下一切片
是I0-B3.03 TASK几何Authoring；它继续复用同一Geometry Runtime、Worker监督和source处理能力。
