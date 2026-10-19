# Direct 短引用索引

Direct 的 Geometry、ProductFamily、草稿、相关Code、保存的Inspection、Query及Operation引用由既有
Provider 边界投影为25字符类型句柄。后台认证引用和分页cursor使用当前`cur.`语义前缀，
不携带人工版本或阶段代号；引用类型、用途、Scope与分页事实由认证内容绑定。
原业务对象、exact版本、源码、资产和权限仍由各领域服务维护。

## 公开导航合同

| 前缀 | 引用对象 | 合法用途 |
| --- | --- | --- |
| `gr_` / `gv_` | Geometry root / exact version | root发现；exact版本写入依赖、读取、Preview和Query |
| `fr_` / `fv_` | ProductFamily root / exact version | root发现；exact版本读取、修订、实现预览 |
| `gd_` / `fd_` | Geometry / ProductFamily draft | 草稿发现、保存、Preview与发布；可变内容并发仍使用当前edit_token |
| `sc_` | Geometry/Family草稿、正式程序或Geometry Query的源码locator | `code_read`及batch选择 |
| `gi_` | 保存的Geometry Inspection | 已保存实体的查看、换视角、截面及Geometry Query；不以图片代替实体事实 |
| `gq_` | Geometry Query attempt | 既有源码修复/恢复与目录投影 |
| `or_` | 原Operation的准确引用 | `operation_get`、Apply原操作恢复及产品READ的指定来源；读取权限仍由原Operation owner判断 |

预览当前使用`inspection_ref`，不再返回旧`gp_`／`pp_`或预览ResourceTemplate。Operation引用
可携带原Run身份，按现有Scope及frontier授权读取；Data alias仍由当前Catalog解析，不能
因Operation可读就复用原Run的`data_NNN`。精确边界见[产品读取合同](ifc-products-read.md#5-历史currentscope-与失败)。

后缀为服务器生成的128位随机值的22字符base64url编码。调用者必须原样复制完整值，不能
根据前缀猜造对象、手工截短、修补错字或将root当成exact版本。格式、类型、索引不可见或
完整性错误返回`REFERENCE_INVALID`及当前Scope目录/写入结果的恢复方向；原领域验证失败
仍使用其原错误码。成功索引不表示业务对象仍可用，也不授予跨Scope权限。

## 唯一权威与持久化

`OpaqueReferenceService`复用既有CursorCodec认证，`PostgresControlPlaneRepository`的
reference-index mixin通过原session factory和tenant context读写`opaque_reference_index`。
该表只保存Scope、句柄、认证长引用及其SHA-256，不保存Geometry、Family合同、latest/CURRENT
或可独立变化的业务状态。它不是第二套资源仓储或业务事实源。

同一Tenant/Principal/Task/actor Scope内，同一认证引用通过唯一约束稳定映射到同一句柄；
并发插入只允许`ON CONFLICT DO NOTHING`后重读，不能重定向。不同exact版本得到不同引用。
进程重启后保留索引及原CursorCodec密钥即可继续解析；已返回的旧版本引用不随新版本移动。

索引表启用并强制RLS，MCP与Runner runtime拥有同Tenant/Principal/Task的SELECT和INSERT权限。
UPDATE无授权且有数据库不可变trigger。原引用恢复后仍进入原Scope/type/exact对象验证，
因此Task导航索引不会放宽原Run-bound Code、Query或Preview约束。Task/Principal删除时索引
按现有生命周期级联清理。索引由`20260907_0079`建立；Runner访问由`20261002_0108`扩展到
既有SELECT／INSERT策略，Tenant/Principal/Task谓词及不可变绑定不变。

索引创建发生在原业务调用返回后的导航投影阶段，不接管原发布事务、Operation或CURRENT。
若导航投影失败，原领域发布的终态仍由原Operation/资源记录裁决，应通过原目录重新发现；
不能据响应丢失推断发布回滚。

## Provider及组合根

Direct bootstrap沿同一repository和codec创建服务；entry注入Facade；server注册
`DirectReferenceMiddleware`。工具注册时从现有DTO Schema生成明确字段路径，在Provider
Schema允许对应短句柄，入口展开后再交给原TypeAdapter和Application合同。
Runner复用同一字段选择投影及索引权威，调用Scope仍来自受保护凭证。

投影仅遍历声明的object/array引用路径；不搜索任意Payload、源码、业务identifier或分页cursor。
Family `definition_json`复用现有完整定义DTO的Schema，仅展开其中精确Geometry binding引用；
原UTF-8大小和重复JSON key检查仍先执行。输出同时处理structuredContent、JSON TextContent和
声明的ResourceLink导航，ImageContent字节不变；保存的预览实体按`inspection_ref`消费。
其他Code种类保持各自locator协议。
公开说明的文字来源是`tool_docs/common.py`及原能力说明，不建立独立Guide或人工版本号。

## 验证范围

`test_opaque_reference_service.py`验证类型、历史引用、稳定映射、跨Scope、拼写及认证完整性；
`test_direct_reference_projection.py`验证实际FastMCP的Schema/入出参、定义JSON、源码隔离、
图片及ResourceTemplate；`test_opaque_reference_postgres.py`验证限权数据库并发、重启、RLS、
不可变绑定和真实Geometry/Family/Preview/Query/Code串联。原领域及Provider回归保持各自验收。

这些验证说明引用机制的正确性边界，不构成陌生Agent效率提升或连接业务全矩阵通过的证据。
原生能力与过程审计另记于当前`docs/work`实验记录。
