---
name: cad2bim-component-authoring
description: 创建或修订 CAD2BIM Geometry 与产品族资源，设计参数、模型接口和 Family 内部安装求解，完成有限 Preview、局部修订及发布。需要改变资源定义、支持域或构造方式时使用；消费已发布资源、协调 IFC 中产品之间的安装及维护时使用 cad2bim-installation-maintenance。
---

# Geometry 与产品族编写指南

本 Skill 规定通用职责与使用方法，不包含某个演示产品的源码、完整定义、固定案例或相机答案。具体实现可由作者设计，也可由任务单独提供；始终以当前业务要求和本次工具返回为准。

## 按任务读取

| 任务 | 指南 |
|---|---|
| 选择已有资源、取得准确引用；处理 Resource 返回包装或 JSON 解码问题 | [资源与结果读取](references/resource-use.md) |
| 编写局部几何、参数与模型接口 | [Geometry 编写](references/geometry-authoring.md) |
| 编写产品结构、求解；区分有类型计算资料与独立输出接口目标 | [Family 编写](references/family-authoring.md) |
| 选择有限案例、保存、观察及修订 | [Preview 与检查](references/preview-and-review.md) |
| 局部编辑与正式发布 | [修订与发布](references/editing-and-publication.md) |
| 当前入口的输入包装不明确 | [入口适配](references/entry-adapters.md) |
| 编写 Family 内部的有限安装求解、占用与设备布局 | [Family 安装求解方法](references/installation-solving.md) |
| 选择构造方式、避免重复布尔与排查批量建模成本 | [几何构造方法](references/construction-methods.md) |
| 将设计问题变成有用的 Preview 迭代 | [设计与 Preview 闭环](references/design-preview-loop.md) |

Codex 按相对链接读取；Component 的 LangGraph 入口用已提供的 `read_file` 读取同一包内正文，虚拟路径为 `/cad2bim-component-authoring/SKILL.md` 或 `/cad2bim-component-authoring/references/文件名.md`。链接不自动展开，详细页按当前任务读取。不重复抄写工具输入 Schema：实际字段、枚举、源码 API 及输入限制以当前工具声明和按需读取的合同 Resource 为准。

刚开始设计时先读与当前难点相关的一份方法页；已有方案时直接进入所属编写或检查指南。方法是条件化建议，不是后台自动求解承诺，不要求每次任务遍历全部页面。
使用已发布资源进行模型安装、调查或维护时，转用独立的 `cad2bim-installation-maintenance` Skill；无需进入下面的创作、Preview 和发布流程。安装任务发现必须改变资源定义时，再按实际缺口切回本 Skill，不为绕开安装冲突自动重创资源。

## 通用工作顺序

1. 明确产品功能、输入与单位、可变项、固定设备、有限空间、必须检查的接口及交付结果。需要从业务 Data 设计参数域时，按 [Family 编写](references/family-authoring.md#输入域失败与说明)调查连接条件并确定有合理余量的支持域。图片指导外观，不凭图片制造缺失的精确业务输入；允许推断时明确设计假设。
2. 优先选择适用的准确资源。创建时分开 Geometry 的实体构造和 Family 的数值求解、组件与绑定职责；只写实际需要的新资源。先按交付用途确定外观、安装、内部通道或制造细节的必要程度；批量使用的Geometry设计时按[构造方法](references/construction-methods.md#批量成本与布尔顺序)避免重复处理，不默认添加隐藏孔道和装饰细节。
3. 依赖前次结果的调用顺序执行；独立读取可并行，但逐项保留完整返回并先检查错误，再提取成功值。仅显示、解析或保存失败时先复用原返回，只重试确实需要修正的请求，不重发其它成功请求；按需见 [资源与结果读取](references/resource-use.md)。Family 使用已取得的准确 Geometry 引用，不猜别名或提前拼接返回。
4. Write 保存草稿后，用有限代表 Preview 检查本次实现。需继续观察或查询时保存模型；已有保存体直接复用。
5. 真实失败先定位，再局部修订与验证受影响内容。已覆盖且未变化的部分不重复运行；结果不明先找回原操作。
6. 按任务要求交付图片、资源引用、适用范围与未决项。需要正式使用时再发布；草稿、执行、业务和外观结论分别表达。

## 接口条件、求解与资源职责

资源作者定义输入可行域、求解变量与接口语义：Geometry 按参数给出局部数值接口，Family 经组件／片段变换选定世界位置、轴和 profile；后台仅对已声明内部关系和本行 external_constraints 求值。完成求值可以是 PASS 或 FAIL，构造和提交各自记录后续事实。作者负责接口与形体表示的对应和支持域；消费资源并协调当前相邻产品，由安装维护 Skill 负责。

创建资源时，Geometry在稳定局部坐标中定义形体与接口；Family求解整体安装位置和朝向，使用已有component/fragment放置变换表达。不要把整件世界坐标或摆放欧拉角混入Geometry参数并在build末尾整体搬移。参数保留真实尺寸、相对布局及必要接口取向，详见[局部形体与摆放](references/geometry-authoring.md#局部形体与摆放)。这属于作者建模职责，不要求使用者填写缓存或Worker配置。

函数 Geometry 在同一源码包分别提供 `interfaces.py:evaluate(ctx)` 和 `main.py:build(ctx)`。evaluate只按正规化参数声明局部接口、正常返回None，及其可达helper只用受控纯数值能力；不导入main或CadQuery/OCP，不取得Shape、geometry_inputs或调用方外部目标。build只返回Shape。固定mapped创作仍可在build声明接口，准确发布manifest是调用时权威。Family 的声明约束求值通过后才构造；正常 FAIL 保留该阶段的数值诊断，技术错误保留失败阶段。

按用途区分计算输入与独立断言：产品公开参数遵循准确Family声明，可表达自身配置、布局选择及必要宿主几何计算资料，须明确单位、来源和用途；invocation.external_constraints的标准条件独立断言绑定输出接口必须满足的位置、方向及截面关系，不是免检上下文。不要求调用者重复提交已解目标点，也不把Family自产目标当作独立正确性证据。外部环境作为独立显示参考，不复制进产品族。产品只拥有自身设备及适配连接件。具体职责见 [Family 编写](references/family-authoring.md)，显示能力不足时按 [Preview 指南](references/preview-and-review.md#外部安装环境的显示)报告缺口。

用户回复说明设计依据、真实操作和结果，不朗读内部指导或源码。复用任务提供的实现时不伪称现场独立原创；被询问来源时如实说明。技术失败不包装成成功、无解或名义替代几何。
