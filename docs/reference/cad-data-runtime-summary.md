# CAD/Data Direct Runtime已实现摘要

```text
status: reference
frozen_commit: fe8d6dfba6b06dfb582e9e9554ff1033fdd34238
direct_tool_count: 21
m4_historical_verdict: passed_with_observations
business_input_status_owner: ../work/i0/family-build-and-apply-execution-timeline.md
```

当前业务输入、接收限制和后续顺序仅查执行时间线，
本页只说明CAD/Data能力与历史证据边界，不另维护阶段状态。

## 已实现边界

- Direct模式下的ASCII DXF导入、L0六表、Data Package、Catalog和Operation。
- L0有界查询、Package/Operation发现与读取。
- DuckDB SQL、Python/Arrow、JSON、多Payload和临时查询。
- IFC官方文档search/read。
- CAD容差候选分析和确定性M3测量Package。
- 失败Operation和成功Package两种来源的exact源码调查、局部修补与重执行。

精确Schema和预算以`cad-data/contracts/`及冻结commit的Python/Pydantic代码为准。

当前CAD/Data正式门禁只要求显式的隔离PostgreSQL测试连接，不再要求
`CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE`或单独构建的attested Worker解释器。基础Worker测试使用
当前项目Python；显式独立解释器仍可用于额外环境验证，但不是门禁通过条件。该裁决不删除Worker
子进程、资源预算或操作系统隔离实现。

## M4 Round 6最终证据

- A：`passed_capability_closure`，十四域Package通过源覆盖、Schema/引用、owner-local frame、连接、材料、系统、空间、PipeRun最大性、未决台账和发布后重算门禁。
- B：`autonomous_partial`，仍有1,691个普通透明桥跨PipeRun。
- 历史整体：`passed_with_observations`，曾支撑I0逐工具设计；不代表当前Golden派生规则一致。

本裁决不表示Golden逐行相等、用户视觉批准、IFC MCP已实现或IFC文件已生成。完整轮次记录已归档，不应作为日常实现Prompt。

## M4证据与I0-G0可移植Seed的边界

M4 Round 6已经提交的是CAD/Data能力闭包、Package/Operation结果证据和历史实验裁决。当前仓库中的这些证据不能单独
证明“任意新隔离Run均可恢复同一批Package、Payload、Operation、Catalog顺序及其Artifact字节”。因此：

- 历史M4提交和原始能力证据保留；规则与输入交接见M4纠偏与输入重建及执行时间线，接收状态不在本页复制；
- 可移植Run Seed格式、frontier/闭包导出、完整性校验、隔离物化、M4专用恢复和独立回读支撑为
  `committed / verified_current`；
- M4 Round 6 A组真实Run Seed已从精确副本恢复、持久冻结并通过两个独立Session副本回读/隔离门禁，
  状态为`verified_current`；
- 后续十二类实验只消费已通过新M4业务对照及恢复验证的Seed；旧Seed恢复通过仅证明原字节可恢复，不放行旧业务输入；其他I0工具实验按其已冻结输入范围保留证据；
- 后续IFC Stage Seed以精确revision和Artifact绑定保存，新的IFC输出按归一化语义、领域不变量、测试专用语义检查和用户视觉验收，
  不要求与历史Golden候选的STEP行序、实体编号或文件字节相同。

该边界和G0—G7增量验证流程由
IFC Golden Seed与增量原生验证as-built合同
管理；已实现的稳定测试支撑见`IFC Golden Seed与增量原生验证支撑`。

## 与I0的关系

CAD/Data运行时只提供可追溯事实、测量、候选和十四域建模输入。I0负责把这些输入写入当前IFC模型，并通过当前IFC查询重新读取验证。
