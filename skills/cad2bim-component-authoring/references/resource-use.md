# 资源与结果读取

本页用于资源创作中的准确引用、保存模型和返回读取。观察 Geometry 已保存的执行模型使用 inspection；适配已有产品族读取其准确合同和绑定。模型安装、当前 IFC 调查、Data 派生和产品维护使用独立的 `cad2bim-installation-maintenance` Skill。

任务允许复用已有资源时，Geometry 按需要读取 Info、参数合同、使用说明和源码，Family 读取定义、支持域与绑定；从零创作时遵守任务提供的参考范围。只读当前任务相关且获准的资源。资源名、图片和旧会话别名不能替代当前准确引用；跨 Scope 必须使用当前身份可读的引用。

原样复用已成功读取合同的准确资源引用，不手工拼接、截短或改写。引用读取失败时，先依据发现结果和诊断纠正并读到合同，再继续依赖它的写入。

草稿引用、正式版本和保存模型引用职责不同：草稿用于继续编辑及执行；正式版本固定业务内容；inspection 对应某次准确执行的实体。当前草稿变更不会更新旧保存体。

已有 inspection_ref 直接交 Geometry Preview 或 Query。丢失时沿 operation_list／已知 operation_ref → operation_get（按需 case_key）取回该次 inspection_ref，再观察同一保存体。不存在保存结果时不能凭操作名假造模型，也不为找回而新建相同操作。

逐项保留完整返回，先检查业务 MCP 的 `isError` 或协议 `error`，让原诊断可见，再访问成功字段。缺少 `structuredContent` 不等于失败：按该入口声明处理 `content`，保留文本、图片等块的顺序；不把整个 content 强行解为一个 JSON，也不用 `structuredContent ?? content` 冒充语义解码。资料正文不以 Object.keys 或摘要代替。

区分宿主资源发现返回、业务 MCP 返回和普通文本。对象不再 JSON.parse；字符串只有通道明确为 JSON 时才解析一次。资源目录可能是含 `resources` 数组的对象，先确认实际形态，再取声明的集合；不对未知返回直接 filter。Component 仍按其实际工具通道读取，Codex 的 store/text 是客户端能力，不是所有 Agent 的必需接口。

独立读取可并行，各分支分别保留返回和错误；一处分支的本地解析或保存失败不应遮住其它结果。只展示当前需要的片段，保留完整原返回供后续读取；显示截断、解析或保存失败时先复用原返回，不重发其它成功请求。`retryable=false` 表示同输入盲重试没有依据，纠正有明确诊断的输入后可以再调用；后台完整性故障则保留对象引用及原错误并报告，不以原样重读当修复。客户端脚本保持短小，完整资料仍适用时按需复用。

Direct 专有身份字段不带入 Runner。Skill 不更改权限、Scope、草稿并发条件、业务资源版本或发布机制。

## 按问题读取工具 R

以实际入口的资源目录为准。Component 先用 `state_resource_list` 发现，再按需用 `state_resource_read` 读取；工具描述已经给出的准确 URI 可直接读取。下列共享合同沿当前入口读取，不凭另一环境的 URI 假定可用。

| 当前疑问 | 按需资料 |
|---|---|
| Geometry build 程序允许怎样写、有哪些 ctx 能力 | `cad2bim://contracts/ifc_geometry_resource_write/program_api` |
| Family 定义及 proposal 必须满足什么结构 | `cad2bim://contracts/ifc_product_family_write/definition_json`、`program_api` 同工具资料页 |
| Write／Preview／Publish、保存与准确复用的实际语义 | `cad2bim://contracts/common/authoring` |
| 图像、截面、标注与结果状态怎样解释 | `cad2bim://contracts/common/preview` |
| 如何写资源使用说明、区分支持与验证范围 | `cad2bim://contracts/common/usage` |

方法页帮助决定做什么；R 回答具体字段、调用能力和执行语义。实际调用所需输入必须完整了解，但不需要预读全部输出 Schema 或递归加载所有链接。已有完整且仍适用的资料可以复用。
