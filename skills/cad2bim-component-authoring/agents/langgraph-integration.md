# 标准包与现有读取入口

`SKILL.md` 使用原生 Codex frontmatter，通用规范分存于 references Markdown；具体产品实现独立提供，不进入 Skill。Codex 和 LangGraph 读取同一套包内正文，不另存指南副本或工具合同。

## Component 的标准 Skill 包

当前 Component composition 使用 `AgentSkills`：启动时由官方 Deep Agents `SkillsMiddleware` 扫描仓库 `skills/`，请求时只投影名称、描述和入口路径。将本标准包部署为 `skills/cad2bim-component-authoring/`，保留其他仍需使用的包，即可沿同一发现入口使用；无需手工登记 catalog、新增加载器或修改角色消息结构。

正文由现有官方 `read_file` 按需读取，例如 `/cad2bim-component-authoring/SKILL.md` 和 `/cad2bim-component-authoring/references/family-authoring.md`。相对链接不自动展开，不在角色入口预载全部正文。实际读取结果直接保存在原 `RoleMemory.messages` 的 ToolMessage 中，按既有 Checkpointer 恢复与压缩；不再建立 `loaded_skills` 正文快照。

虚拟根只包含部署的 Skill 包，不能挂入整个仓库或将指向包目录外的符号链接当作读取入口。正文中的工具合同 Resource 沿现有 `state_resource_list`／`state_resource_read` 发现和读取，权限仍来自当前角色工具集合与准确 checkpoint grant。Skill 读取不授予领域调用或宿主执行权限。

`cad2bim-installation-maintenance` 以同一 `skills/` 根下的独立标准包部署，由上述入口自动发现。两包各自按任务读取，不预载另一包正文，也不增加第二套 catalog 或加载器。

## Codex 与受限原生测试

常规 Codex 使用当前配置中已发现的标准包，按相对链接读取详细页；仓库部署不自动改变用户级 Skill 安装或配置。新会话应先确认实际发现的名称与描述。

受限作者测试若不允许宿主文件读取，不得为了读取 Skill 临时越过原工具白名单。工程准备方可在启动前沿既有材料投放入口将必需页面全文送达作者，并保存实际送达回执；这时准确称为 Skill 正文投放，不冒充已验证原生 skill 调用。若使用原生发现／读取路径，应先让原限制配置明确包含该读取能力，不在运行中修改监控或权限。

规范只读加载；不执行附件脚本。实际 Geometry／Family 创建、求解和出图仍由业务工具完成，保留本次真实回执。
