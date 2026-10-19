# Direct 与 Runner 调用入口

源码、定义和案例的业务内容只有一份，可由当前任务提供或作者设计。下表说明 Codex Direct 与当前 Component 的公开 Runner 输入；独立 Runner 以自身实际工具 Schema 为准，不凭框架名称套用包装。不创建另一套源码、定义或执行器。

| 内容 | Codex Direct | Component 的 Runner MCP |
|---|---|---|
| 工具外层 | 平铺当前输入字段；会话身份字段以该入口 Schema 为准 | `{ "request": BODY }`；身份来自 Runner 凭据，不提交 Direct 身份字段 |
| 源码成员 | 成员路径字段名以该入口 Schema 为准，提交 main.py 与完整源码 | `{ "path": "main.py", "source": 同一完整源码 }` |
| Geometry 参数定义 | `source.parameter_schema_json = JSON.stringify({action:"set", value:P})` | `request.source.parameter_schema_json` 使用同一 JSON 字符串 |
| Family 定义 | `source.definition_json = JSON.stringify({action:"set", value:D})` | `request.source.definition_json` 使用同一 JSON 字符串 |
| Family Preview 案例 | `cases_json = JSON.stringify(C)` | `request.cases_json` 使用同一 JSON 字符串 |
| Family Preview 出图选择 | `render_json = JSON.stringify(R)` | `request.render_json` 使用同一 JSON 字符串 |
| 保存体 observation | 原生 observation 对象 | 同一个原生 observation 对象 |

`P` 是实际参数 Schema，`D` 是含 family_contract 和 geometry_bindings 的完整定义，`C` 是当前 mode/items 案例对象，`R` 为本次所选渲染设置。不得遗漏 Write 的 action/value 包装，不将 observation 再编码。源码必须包含真实换行，不递归解码或将 JSON 字符串本身当程序。

其余业务字段保留：创建的 mode、名称、用途、usage_instructions 动作；source_kind；准确绑定；Preview 的 retain、source、逐例 invocation 和 expectation。资源短引用来自当前身份对应工具的实际返回，不把 Direct 旧环境别名或内部数据库 ID 带入另一运行环境。

任务材料中如有 `{{...}}`，它们只是说明占位符，提交前全部替换为明确内容。它们不是后台模板语言，后台不会替你执行字符串插值。
