# ProductFamily Catalog与持久内核

本文记录当前ProductFamily读侧领域内核、PostgreSQL持久关系、Scope/RLS、opaque reference
和Direct Catalog入口。可执行目录只发现完整的当前合同版本；旧协议原字节保留但不解释执行。
Authoring、Code/Operation和source-first原子发布的现态见
[`ProductFamily Authoring与Product Realization Runtime`](product-family-authoring-and-realization-runtime.md)。
公开写入与组合预览见
[`ProductFamily写入与Product Realization预览`](product-family-write-and-realization-preview.md)。
原B3.05文本及历史门禁保存在原文归档，
当前工程与原生验收状态见实施记录。

## 1. 当前公开能力

本轮已实现、原生能力验收待完成的版本使用说明增量见系统基线§4.5：
原Family版本owner保存usage_instructions并纳入内容身份，exact family_version info返回完整正文，
历史缺失返回null；list和root版本card不展开正文。purpose仍为短用途，不能代替完整说明。
实现及持久化/Provider/不可变性验收前，以下as-built字段与历史证据不表示该增量已经具备。

### 1.1 `ifc_product_family_list`

该READ工具发现当前Direct Session内SYSTEM、同TENANT和同TASK可见、当前合同有效的ready
ProductFamily root。合同拒绝诊断与续页语义见第4节。首页支持：

- `owner_scopes`的OR过滤；
- `product_structure_kinds`的OR过滤；
- `purpose_contains`的有界子串过滤；
- `page_size` 1—100；
- server-issued opaque cursor续页。

每张card返回scope-bound `family_alias`和读取当时计算的exact
`latest_family_version_alias`。`latest` 只是发现便利；它没有数据库指针、不是
CURRENT，也不能由后续执行入口从root alias隐式推导。

### 1.2 `ifc_product_family_info`

已发布资源使用以下两个封闭target分支：

- `family_root`：返回稳定root和一页不可变version card；
- `family_version`：返回一个exact ready version的完整当前合同、usage正文、exact geometry
  bindings、version-asset metadata和正式发布／标准模型导航。

未发布草稿的`draft` target沿共享Authoring Info合同处理，见
[`共享创作草稿`](authoring-drafts-preview-and-publication.md)。父引用的身份读取不放宽当前exact合同及资产校验。

工具没有`target=latest`分支。Agent必须从list或root info复制服务器签发的exact
version alias。返回不含数据库ID、ArtifactVersion ID、Storage路径、writer身份或内部
Scope字段。

## 2. 唯一领域身份

### 2.1 中性owner Scope

[`resource_scope.py`](../../src/CAD2BIM_backend/domain/resource_scope.py)定义一个封闭的
`ResourceOwnerScopeKind`/`ResourceOwnerScope`，同时被Geometry Resource和ProductFamily使用。
原Geometry名称仅保留稳定导入alias，没有第二套Scope事实。

Scope是严格的三分联合：

```text
SYSTEM -> tenant_id=null, task_id=null
TENANT -> tenant_id!=null, task_id=null
TASK   -> tenant_id!=null, task_id!=null
```

### 2.2 稳定root与不可变version

- `ProductFamilyRecord`只保存稳定`family_id`、owner Scope和`created_at`；
- `ProductFamilyVersionRecord`保存一次已发布合同的精确快照；
- `ProductFamilyGeometryBindingRecord`用family-local `binding_key`绑定exact
  `(geometry_resource_id, geometry_resource_version_id, content_digest, kind)`；
- `ProductFamilyVersionAssetRecord`保存该version拥有的exact byte asset metadata；
- `ProductFamilyVersionBundle`是Repository只读重组单元，必须同时包含至少一个
  geometry binding和恰好一个`realization_source_bundle`。

root上没有display name、purpose、CURRENT、default/latest version字段或可变发布状态。

### 2.3 digest与完整性

`contract_digest` 是canonical family contract的SHA-256。`content_digest`由一套领域函数
确定性计算，覆盖：

```text
contract digest
+ fixed program entry
+ sorted exact geometry bindings and geometry content digests
+ sorted immutable version-asset metadata and SHA-256
```

version号、root/latest投影、opaque alias、Operation alias和preview缓存不进入内容摘要。

`family_contract`是封闭typed Domain对象，当前详细语义由
[自身几何合同](product-family-self-geometry.md)拥有：

- 单一当前typed合同和封闭的结构种类，不含人工schema/ABI代号；
- 有界component数组、首版允许的IFC product class、唯一root、parent引用闭包和无环约束；
- 有界geometry fragment数组，每个fragment显式归属component并引用family-local
  `binding_key`；
- 有界interface数组和必需installation_anchor，全部来自fragment的同次model_interface；
- 完整typed参数、fixed/public/solved来源、fixed或interface_constrained局部pose、scale来源
  及显式内部/外部约束；具体覆盖与独立检查见[自身几何合同](product-family-self-geometry.md)；
- component、fragment、interface和binding key唯一，所有引用必须闭合，
  未使用或用途不兼容的exact binding不能组成ready version。
- family contract与validation summary分别在Domain发布/回读边界限制为最多65,536个
  canonical JSON UTF-8字节；ready bundle最多320个exact geometry bindings和4个封闭角色的
  version assets。Domain可接受的ready version因此总能进入唯一Provider读取投影，预算不在
  Direct输出末端另设第二来源。

Pydantic解析先补齐明确默认值，`contract_digest`再对该规范化typed对象计算，
因此原始输入形式不会成为第二套摘要事实。读取也使用同一严格DTO；已退出字段不被忽略、
自动转换或经旧解释器读取。固定运行/预算标签不进入资源摘要；真实执行预算由Runtime配置负责。

## 3. PostgreSQL关系与约束

`20260904_0073_product_family_catalog.py`从0072创建恰好四张领域表：

```text
product_families
product_family_versions
product_family_geometry_bindings
product_family_version_assets
```

关键机器约束：

- `(family_id, version_no)`唯一；
- `(family_id, family_version_id)`提供same-family parent composite FK；
- parent FK为`DEFERRABLE INITIALLY DEFERRED`且`ON DELETE RESTRICT`；
- geometry binding必须指向exact GeometryResourceVersion pair；
- binding trigger校验geometry kind、content digest和owner-scope containment；
- deferred completeness trigger在事务提交时校验至少一个binding与恰好一个
  realization source bundle；
- 声明 `standard_model` 的版本还必须包含 validation report、standard model geometry 和
  manifest；这两个新增角色分别保存实际名义 OCCT BREP 和 exact 构建清单。`0080` 扩展
  原资产角色约束和完整性触发器。历史不可变字节继续保留，但不保证旧typed合同可执行读取；
- 四表全部`ENABLE ROW LEVEL SECURITY`和`FORCE ROW LEVEL SECURITY`；
- 当前MCP/Runner写入须有有效技术Task，可写同tenant的TENANT根或本人当前TASK根；
  API无这些正式表的INSERT权限。root草稿的受限UPDATE及依赖scope-width见
  [TENANT作者资源持久化](tenant-authoring-persistence.md#2-原数据库权限上的增量)；
- SYSTEM的curator写入沿受控发布路径执行；0097已为四表授予curator INSERT并增加
  SYSTEM-only策略，准确职责见[作者与运行时合同](product-family-authoring-and-realization-runtime.md)。
  正式version、asset、binding仍不可变；不把root草稿的受限列更新解释为全表UPDATE权限。

scope containment为：

```text
SYSTEM family -> SYSTEM geometry
TENANT family -> SYSTEM or same-TENANT geometry
TASK family   -> SYSTEM or same-TENANT or same-TASK geometry
```

上述当前权限继续使用同一组表、同一PostgreSQL组合Repository和原子发布事务。
公开所有权由可信入口解析；TENANT授权不允许Agent任意提交owner，普通runtime仍无SYSTEM写权。

downgrade先对四表取`ACCESS EXCLUSIVE`锁。任一表非空时以SQLSTATE `23514` fail
closed；只有四表全空时才按child-to-parent顺序删除。它不猜测数据谱系、不使用
CASCADE、不恢复旧Product-Core。

## 4. 读取路径与并发语义

```text
Direct Provider request
→ Direct session Scope resolver
→ TenantExecutionContext / PostgreSQL RLS
→ ProductFamilyCatalogService
→ existing PostgresControlPlaneRepository composition root
→ ready root + immutable version bundle
→ exact GeometryResourceVersion reauthorization
→ scope-bound opaque aliases
→ closed Provider projection
```

Repository先要求参数Scope与当前激活的`TenantExecutionContext`完全一致，然后安装
RLS context。ready root是“至少有一个完整version”的root，空root不会被发现。

Catalog和Code Discovery按Scope、资源身份和源码资产查询。当前exact Info、执行以及
合法发现项完整校验当前DTO；parent引用只查询同Scope、同root中的准确版本身份，
root历史页读取既有摘要元数据。父身份不依赖合同中的结构类型，两者均不加载历史完整
合同或读取历史资产。历史摘要与parent引用不授予旧记录执行资格。
不再用schema/ABI代号决定发现或执行资格。开发期旧实验库保持离线历史；需要继续使用的
资源须按当前定义生成合规版本，不将旧标签换名后继续执行。0105删除无消费者profile列及
代号判断，保留Scope、准确引用、事务完整性与Operation状态守卫；历史JSON和资产字节不改写。

family list在同一Repository session的有界查询中选定root和当次latest exact
version ID，随后按该exact ID重组不可变bundle。它不先过滤一个version又在另一
事务重新计算latest，因此不会返回与过滤条件不同的version。后续新version发布只会
影响下一次查询，已签发的exact alias仍指向原version。

Family list与Family程序Code list对本页实际扫描候选进行当前合同校验。仅当前DTO结构不合规
时返回`rejections`，逐项包含准确`family_version_alias`、`version_no`、
`PRODUCT_FAMILY_CONTRACT_INVALID`、`field_path`和不含输入值的`reason`；Backend内部字段为
`family_version_ref`。拒绝项不返回可执行卡片或`code_locator`，也不回退到该root旧版本。
数据库、Scope、Storage、资产缺失或损坏等技术错误仍按原调用失败传播，不转换成拒绝诊断。
分页边界取实际扫描的最后候选，合法项为空但`rejections`和`next_cursor`非空时仍可继续。
`returned_count`仅计合法项目；root历史版本页不受当前完整合同校验影响。

Catalog在签发合法发现卡片或读取当前exact version时，使用同一`StoragePort`从
`owner Scope + family version + asset role`推导唯一逻辑key，以`read_verified`重新读取该version
所有exact assets，并核对key、filename、MIME、size及数据库SHA与同次verified bytes的绑定。
共享FD、目录与完整性语义见[Geometry Resource内核](geometry-resource-catalog-kernel.md)，
Family不再重复全量扫描或对同次已验证字节重算SHA。
`realization_source_bundle`还必须是现有codec可解析的canonical ZIP，且包含UTF-8
`main.py`。元数据行存在但字节缺失、损坏或不匹配时，该读取fail closed，
不返回“ready”卡片或部分alias。

组合根只实例化一次现有`SourceTextGeometryAuthoringSource`，并同时注入Geometry Authoring与
ProductFamily asset完整性服务。Application层依赖现有source-bundle Port，不直接导入codec基础设施；
ProductFamily没有第二套ZIP规则或解析器。共享Storage返回的not-found、metadata损坏和immutable
payload冲突统一投影为稳定的ProductFamily完整性错误，同时保留内部异常链。

info回读exact version时，Application Core对每个geometry binding重新走现有Geometry
Resource Service/Catalog，校验root、exact version、kind和content digest。任一依赖在当前
Scope不可见或不一致时，返回完整性错误，不投影一份部分可用的family。

## 5. opaque reference与Provider边界

root ref、exact version ref和list cursor继续复用当前opaque cursor codec。签名claims包含封闭
domain/version/reference kind、内部定位与scope digest。解析时需要：

- token签名和claims key set合法；
- domain、claims version和reference kind精确匹配；
- token绑定的scope digest等于当前Tenant/Task/Run调用Scope；
- root/exact target不能互换；
- cursor保留首页过滤与keyset boundary，续页不允许重新提交或修改过滤。

Direct Provider DTO是封闭的Pydantic分支；Provider递归Schema没有`unknown`、开放对象、
`allOf/if/then/else/not`或隐式latest选择器。`family_contract`/validation summary以
canonical JSON text投影，避免把不确定嵌套对象扩展为Provider `unknown`。

## 6. 当前边界与后续能力

Authoring、Runtime、Code、Operation和公开Preview已经复用本内核实现。Family自身产品的
IFC写入衔接、外部对象统一修改、真实Port/Connection与跨Task共享仍是分别验收的后续职责。
Family CURRENT、default/latest执行指针及独立Program身份不属于设计。

`ProductFamilyVersionAsset` 当前固定了身份、格式、MIME、大小和SHA的持久关系。
它的逻辑Storage key由owner Scope、family version和asset role确定；现有唯一
`StoragePort`/`LocalStorage`已支持该封闭key和独立逻辑namespace，Catalog已执行实际字节完整性回读。
B3.06已新增source-first写入、Operation证据和原子发布流程，并继续使用同一Storage实现。
后续B3.07已提供TASK-only公开Provider publisher，仍以完整验证和原子发布阻止半成品可见。

TASK资产按数据库和Storage持久保存，不随Codex对话结束自动删除；它只在原业务Task范围内
可见。SYSTEM/TENANT读取已具备，不代表TASK资产自动共享或已有管理发布入口。
新Task必须经Catalog重新发现资源并取得本Scope的exact alias，不能复用旧Task token。
TASK→TENANT/SYSTEM的完整依赖复制、重验证与发布是
后续Backend目标，
当前尚未实现，不新增MCP发布工具，也不把测试中的直接插入种子算作该能力。

## 7. 验证证据

当前测试覆盖Scope/RLS、exact依赖和资产回读、原子发布/失败、重启恢复、旧合同明确拒绝、
混合旧/当前版本的SQL分页以及0083追加守卫与历史字节不变。历史0073切片结果保存在原文
归档。当前完整工程门禁、原生作者与Golden业务验收分别以实施记录为准，不混合历史数量。

## 8. 实现位置

- Domain：`src/CAD2BIM_backend/domain/resource_scope.py`、`product_family.py`和
  `product_family_catalog.py`；
- Application：`src/CAD2BIM_backend/application/catalog_scope.py`、
  `product_family_asset_service.py`和`product_family_catalog_service.py`；
- Port：`src/CAD2BIM_backend/ports/product_families.py`；
- Storage key：`src/CAD2BIM_backend/ports/storage.py`，物理实现继续使用
  `src/CAD2BIM_backend/infrastructure/storage/local.py`中的唯一`LocalStorage`；
- PostgreSQL：`src/CAD2BIM_backend/infrastructure/postgres/models.py`、
  `repository_product_family.py`和`migrations/versions/20260904_0073_product_family_catalog.py`；
- Direct：`src/CAD2BIM_mcp/direct/ifc/product_family/`；
- 组合根：现有`PostgresControlPlaneRepository`、Direct bootstrap/application/server；
- 测试：`tests/unit/backend/test_product_family_*`、
  `tests/unit/mcp/test_direct_ifc_product_family_provider.py`、
  `tests/integration/backend/test_product_family_*`和
  `tests/contract/architecture/test_product_family_boundaries.py`。
