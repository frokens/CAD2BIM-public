# CAD2BIM

**面向 CAD 数据调查、参数化构件与 IFC 建模维护的工具和 Agent 系统。**

CAD2BIM combines immutable CAD/Data Packages, parametric Geometry and ProductFamily resources,
transactional IFC writes, and an agent workbench. This public repository contains the current
implementation and anonymized regression material; it does not contain a customer's drawing,
project database, model, conversation, or authentication session.

## 当前能力

- **CAD 与数据包**：导入 DXF，提取结构化数据，以不可变 Package 保存表、JSON 与来源；支持 SQL、Python 查询和数据派生。
- **Geometry 与产品族**：分离参数、接口约束求值和实体构造，支持源码调查、草稿编辑、Preview 与准确资源引用。
- **IFC 建模与维护**：批量 Apply，以及针对既有产品的 `replace`、`placement`、`delete`；共享 Operation、报告、Revision、CURRENT 和事务写入链。
- **可核查结果**：有界读取 Operation 输入输出、逐行报告、当前对象与 IFC 表示；失败及原操作恢复具有明确边界。
- **Agent 与界面**：Component Agent、共享角色图、事件与聊天 API，以及支持本地后端的网页工作台。
- **两份 Skill**：资源创作与模型安装维护分开组织，覆盖关系调查、输入协调、既有对象编辑、保存后检查及业务缺口交付。

产品求解成功表示该产品满足本次显式输入条件，**不表示相邻管道已被自动裁切或移动**。
Agent 必须调查当前模型、确定关联，显式修改需要维护的对象，并检查实际保存结果。
Family 命名接口、真实实体端面与 `IfcDistributionPort` 网络是不同的事实；当前工具不会自动创建完整管网拓扑。

## 源码结构

| 目录 | 内容 |
| --- | --- |
| `src/CAD2BIM_backend/` | Domain、Application、PostgreSQL、Data、Geometry、IFC、Worker 与 Agent 底座 |
| `src/CAD2BIM_mcp/` | Direct／Runner MCP 入口、Provider DTO、工具描述与资源 |
| `skills/` | 构件资源创作与安装维护 Skill |
| `frontend/` | 当前网页与 API 代理代码，默认不附带任何历史项目回放 |
| `tests/` | 合成测试、匿名局部几何回归与独立构件定义 |
| `scripts/setup/`、`scripts/release/` | 初始化和能力验证入口 |
| `docs/reference/` | 随源码提供的工具与运行合同 |

## 本地启动

需要 **Python 3.11、PostgreSQL 16、Node.js ≥22.13 和 uv**。
PostgreSQL 的 `initdb`、`pg_ctl` 应位于 PATH，或将 `CAD2BIM_POSTGRES_BIN` 指向其 `bin` 目录。

```bash
git clone https://github.com/frokens/CAD2BIM-public.git
cd CAD2BIM-public
uv sync --frozen --extra dev --python 3.11
```

若使用 ChatGPT 登录 helper，先构建它，再按实际 CLI 帮助登录自己的账户：

```bash
cd src/CAD2BIM_backend/infrastructure/chatgpt_auth
npm ci
npm run build
cd ../../../..
uv run cad2bim-chatgpt --help
```

准备一个新的本地运行目录并启动后台：

```bash
uv run cad2bim-component prepare --runtime-dir .local/component-runtime
uv run cad2bim-component serve --runtime-dir .local/component-runtime
```

另一个终端启动前端：

```bash
cd frontend
npm ci
set -a
source ../.local/component-runtime/frontend.env
set +a
npm run dev
```

打开终端显示的地址，选择“新建任务 → Component”。默认没有已完成任务、对话回放或已注册资源；
公开源码不等于已有业务数据库。使用自己的 CAD 文件与资源，并保管生成的运行目录和登录状态。
这是受控本地运行入口，不是远程多租户部署保证。

## MCP 入口

- `cad2bim-mcp-direct`：受控本地 Direct Session，适合明确授权的原生工具工作。
- `cad2bim-mcp-runner`：Runner 调用入口，与 Direct 共享 Application Core。
- `cad2bim-checkpointer-setup`、`cad2bim-db-runtime-roles`：底座初始化。
- `cad2bim-system-geometry-seed`：参考资源初始化入口；本公开快照不携带原项目注册库。

普通源码阅读和单元测试不需要启动 MCP，也不需要调用付费模型。

## 测试与公开范围

```bash
uv run pytest tests/unit/mcp/test_provider_output_compaction.py \
  tests/unit/mcp/test_direct_ifc_products_provider.py
```

数据库能力验证应使用隔离 PostgreSQL。现有 release 脚本用于各自能力边界，不能将少量单元测试、
Preview 或成功回执表述成完整业务通过。原真实图纸及原生 Agent 实验没有随库发布，也不会自动重跑。

参见 [公开测试材料说明](tests/README.md)、[工具与检查边界](docs/TOOLS.md)、
[架构说明](docs/ARCHITECTURE.md)及[隐私与脱敏说明](docs/PRIVACY.md)。

## 当前边界

源码包含有限产品编辑、查询、持久化和局部安装维护能力；完整 CAD 分类、全场景多角色建模、
全网实体净空、密封、通流与施工可行性不能由本仓库的存在推定完成。
符号／紧凑／节点表示允许保留其名义接口语义，但不能替代实体接合证据。

## 许可

本项目暂未指定项目级开源许可证。第三方代码保留各自随附的许可证与
[第三方声明](THIRD_PARTY_NOTICES.md)；本次公开发布未代替原作者授予额外许可。
