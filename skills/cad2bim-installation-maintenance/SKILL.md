---
name: cad2bim-installation-maintenance
description: 使用已发布 CAD2BIM 产品族完成 IFC 模型安装、调查和维护，包括批量添加、产品与管道关联、必要的位置及裁切调整、未决冲突处理和保存后核查。用于交付或修改模型中的实际安装结果；需要改变 Geometry 或 Family 资源定义时转 cad2bim-component-authoring。
---

# IFC 模型安装与维护

先明确交付范围：只读调查、安装新增、已有对象维护，或它们的组合。一次产品执行中，输入条件限定可行域，Geometry／Family 产生并变换接口值，后台对声明条件独立求值；构造与提交记录请求范围内产品的执行事实。

安装任务按实际依赖消费这些事实：Agent 依据来源关联读取受影响的当前对象，比较任务要求的安装关系并显式协调；保存后复核受影响关系。

依据实际依赖安排工作。接口依赖确定后，可以派生尚未写入的管道输入，也可以 Edit 已保存对象；先比较再决定是否修改。只读任务保持只读。接口和保存事实的具体解释见[安装管理](references/installation-management.md)。

## 按任务读取

| 当前问题 | 指南 |
|---|---|
| 首次安装或维护的关联、窗口分配、占用、裁切、拆分和设计例外 | [安装管理](references/installation-management.md) |
| 使用准确已发布 Family 和完整输入包添加产品 | [产品添加](references/product-apply.md) |
| 读取当前 IFC、追查原批次和设计来源，或用 Query／Data 派生输入 | [模型与来源调查](references/model-investigation.md) |
| 替换已有产品、改变绝对位姿、删除所属子树 | [产品编辑](references/product-edit.md) |
| 解释报告、保存后核查、主动汇报业务缺口或找回中断写操作 | [结果与恢复](references/results-and-recovery.md) |

先读当前难点对应页面，其余按需读取。字段、模式、预算和失败边界以实际工具 Schema 与合同 Resource 为准；本 Skill 不复制 Schema，不改变 Scope、权限、工具原子性或资源发布机制。

Component／LangGraph 通过现有 `read_file` 按需读取 `/cad2bim-installation-maintenance/SKILL.md` 及本包 references。相对链接不自动展开。Codex 按实际发现的本包路径读取；Skill 不能授予额外宿主执行能力。

## 交付职责

保留原始设计来源和用户交付范围内的完整目标分母；必要时派生新的合法安装输入，不能覆写来源、静默丢行或把未决标成完成。对象 GUID、源键、合法拆分与退出应能相互追查。关联证据不足时缩小未决范围，继续已证独立且获准的工作；真正需要用户选择的设计冲突再提出明确问题。

本 Skill 消费事实并协调当前对象；资源创作 Skill 定义接口与形体表示的关系及支持域。命名接口是数值参照，Body 端面是实际材料边界，IfcPort／连接关系是拓扑。历史报告保持原执行快照，placement 后尤其需要重新取得当前坐标。后台的声明约束求值可复用，Agent 按任务比较当前跨对象关系。

只在已发布资源确实不支持所需行为、且任务允许改变资源时使用 `cad2bim-component-authoring`。安装冲突或执行失败本身不授权重创资源。最终交付及未闭合业务缺口按[结果与恢复](references/results-and-recovery.md#最终交付与业务缺口)主动向用户汇报；设计例外按[安装管理](references/installation-management.md#设计受限与交付例外)处理，不把 SOLVED、报告行数或工具终态替代业务交付。
