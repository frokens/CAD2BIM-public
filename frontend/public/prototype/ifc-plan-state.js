(() => {
  // Presentation state for the documented planner contract; not an Agent Runtime.
  const copy = (value) => JSON.parse(JSON.stringify(value));
  const closed = (value, keys) => {
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).length !== keys.length || keys.some((key) => !Object.hasOwn(value, key))) throw new Error("Invalid planner fields");
  };
  const text = (value) => typeof value === "string";
  const unique = (value, ids) => {
    if (!text(value) || !value.trim() || ids.has(value)) throw new Error("Invalid or duplicate planner id");
    ids.add(value);
  };
  const validateTree = (tree) => {
    closed(tree, ["stages"]);
    if (!Array.isArray(tree.stages)) throw new Error("Invalid stages");
    const stages = new Set(), tasks = new Set();
    for (const stage of tree.stages) {
      closed(stage, ["stage_id", "title", "goal", "content", "tasks"]);
      unique(stage.stage_id, stages);
      if (![stage.title, stage.goal, stage.content].every(text) || !Array.isArray(stage.tasks)) throw new Error("Invalid Stage plan");
      for (const task of stage.tasks) {
        closed(task, ["task_id", "title", "description", "content", "status", "checks", "submission", "review"]);
        unique(task.task_id, tasks);
        if (![task.title, task.description, task.content].every(text) || !["planned", "in_progress", "submitted", "passed", "needs_repair"].includes(task.status) || !Array.isArray(task.checks)) throw new Error("Invalid Task plan");
        const checks = new Set();
        for (const check of task.checks) {
          closed(check, ["check_id", "description", "assessment"]);
          unique(check.check_id, checks);
          if (!text(check.description)) throw new Error("Invalid Check plan");
        }
      }
    }
    return copy(tree);
  };
  const applyGlobalPlan = (state, tree) => {
    const next = validateTree(tree);
    const previous = new Map((state.ifc_task_tree?.stages ?? []).flatMap((stage) => stage.tasks.map((task) => [task.task_id, task])));
    for (const stage of next.stages) for (const task of stage.tasks) {
      const old = previous.get(task.task_id);
      if (old) {
        for (const field of ["status", "checks", "submission", "review"]) task[field] = copy(old[field]);
      } else if (task.status !== "planned" || task.checks.length || task.submission !== null || task.review !== null) throw new Error("Global Planner cannot initialize execution or review results");
    }
    return { ...copy(state), ifc_task_tree: next };
  };
  const applyStagePlan = (state, stageId, plan) => {
    if (plan.stage_id !== stageId) throw new Error("Stage Planner cannot edit another Stage");
    const old = state.ifc_task_tree.stages.find((stage) => stage.stage_id === stageId);
    if (!old) throw new Error("Unknown Stage");
    const next = validateTree({ stages: state.ifc_task_tree.stages.map((stage) => stage.stage_id === stageId ? plan : stage) });
    const target = next.stages.find((stage) => stage.stage_id === stageId);
    if (JSON.stringify(old) === JSON.stringify(target)) return copy(state);
    if (old.tasks.some((task) => task.status !== "planned") && JSON.stringify(old) !== JSON.stringify(target)) throw new Error("Replanning executed work needs an explicit impact decision");
    for (const task of target.tasks) {
      if (task.status !== "planned" || task.submission !== null || task.review !== null || task.checks.some((check) => check.assessment !== null)) throw new Error("Stage Planner cannot create execution evidence");
    }
    return { ...copy(state), ifc_task_tree: next };
  };
  const validateComponentPlan = (plan) => {
    closed(plan, ["needs_component", "reason", "items"]);
    if (typeof plan.needs_component !== "boolean" || !text(plan.reason) || !plan.reason.trim() || !Array.isArray(plan.items)) throw new Error("Invalid Component plan");
    for (const item of plan.items) {
      closed(item, ["name", "requirements", "environment", "approach", "usage"]);
      if (!Object.values(item).every(text)) throw new Error("Invalid Component preparation item");
    }
    if (plan.needs_component && !plan.items.length) throw new Error("Component preparation needs a concrete requirement");
    return copy(plan);
  };
  const routeStage = (state, stageId, boundary) => {
    const stage = state.ifc_task_tree?.stages.find((item) => item.stage_id === stageId);
    if (!stage?.tasks.length) return { next: "Stage Planner", reason: "阶段计划尚未形成" };
    if (boundary === "plan_confirmed") {
      if (state.clarifications == null || state.clarifications.length) return { next: "Stage Planner", reason: "继续评估并落实阶段澄清" };
      try { validateComponentPlan(state.component_plan); } catch { return { next: "Stage Planner", reason: "构件准备决定尚未形成" }; }
      if (!state.stage_plan_md || stage.tasks.some((task) => !task.checks.length)) return { next: "Stage Planner", reason: "阶段计划与检查条件尚需补齐" };
      return { next: state.component_plan.needs_component ? "Component Agent" : "Modeler", reason: state.component_plan.reason };
    }
    if (boundary === "modeler_exit") return { next: "Reviewer", reason: "独立检查当前任务提交" };
    if (boundary === "reviewer_exit") return { next: stage.tasks.every((task) => task.status === "passed") ? "用户确认" : "Modeler", reason: stage.tasks.every((task) => task.status === "passed") ? "本阶段任务均通过，等待用户确认结果" : "仍有任务未通过，按当前检查意见继续处理" };
    if (boundary === "result_confirmed" && stage.tasks.every((task) => task.status === "passed")) return { next: "Stage Card", reason: "记录用户已确认的阶段成果，再按依赖选择后继阶段" };
    return { next: "Modeler", reason: "阶段结果尚不能确认，继续处理未完成工作" };
  };
  const taskChanges = (before, after, stageId) => {
    const old = before.ifc_task_tree.stages.find((stage) => stage.stage_id === stageId);
    const current = after.ifc_task_tree.stages.find((stage) => stage.stage_id === stageId);
    const differs = (a, b) => JSON.stringify(a) !== JSON.stringify(b);
    const fields = {};
    for (const key of ["title", "goal", "content"]) if (differs(old[key], current[key])) fields[key] = current[key];
    const tasks = current.tasks.flatMap((task) => {
      const previous = old.tasks.find((item) => item.task_id === task.task_id);
      if (!previous) return [{ task_id: task.task_id, title: task.title, added: copy(task), fields: {}, checks: [] }];
      const changed = {};
      for (const key of ["title", "description", "content", "status", "submission", "review"]) {
        if (!differs(previous[key], task[key])) continue;
        if (["submission", "review"].includes(key) && previous[key] && task[key]) changed[key] = Object.fromEntries(Object.entries(task[key]).filter(([field, value]) => differs(previous[key][field], value)));
        else changed[key] = copy(task[key]);
      }
      const checks = task.checks.flatMap((check) => {
        const prior = previous.checks.find((item) => item.check_id === check.check_id);
        if (!prior) return [{ check_id: check.check_id, added: copy(check) }];
        const change = Object.fromEntries(["description", "assessment"].filter((key) => differs(prior[key], check[key])).map((key) => [key, copy(check[key])]));
        return Object.keys(change).length ? [{ check_id: check.check_id, ...change }] : [];
      });
      for (const check of previous.checks) if (!task.checks.some((item) => item.check_id === check.check_id)) checks.push({ check_id: check.check_id, removed: true });
      return Object.keys(changed).length || checks.length ? [{ task_id: task.task_id, title: task.title, before_status: previous.status, fields: changed, checks }] : [];
    });
    for (const task of old.tasks) if (!current.tasks.some((item) => item.task_id === task.task_id)) tasks.push({ task_id: task.task_id, title: task.title, removed: true, fields: {}, checks: [] });
    return { stage_id: stageId, fields, tasks };
  };
  // Bounded presentation reducers for future owner events. The live Runner must
  // still bind revisions and validate real operations against its own authority.
  const ownerBatch = (state, stageId, updates, apply) => {
    const next = copy(state), tree = validateTree(next.ifc_task_tree);
    const stage = tree.stages.find((item) => item.stage_id === stageId);
    if (!stage || !Array.isArray(updates) || !updates.length) throw new Error("Invalid owner update scope");
    const seen = new Set();
    for (const update of updates) {
      unique(update.task_id, seen);
      const task = stage.tasks.find((item) => item.task_id === update.task_id);
      if (!task) throw new Error("Task is not in current Stage");
      apply(task, update);
    }
    return { ...next, ifc_task_tree: tree };
  };
  const applyModelerUpdates = (state, stageId, updates, returnedOperations = []) => ownerBatch(state, stageId, updates, (task, update) => {
    closed(update, ["task_id", "status", "summary", "operation_aliases", "remaining_work"]);
    if (!["in_progress", "submitted"].includes(update.status) || ![update.summary, update.remaining_work].every(text) || !Array.isArray(update.operation_aliases)) throw new Error("Invalid Modeler output");
    const operations = new Set(returnedOperations);
    if (new Set(update.operation_aliases).size !== update.operation_aliases.length || update.operation_aliases.some((alias) => !text(alias) || !operations.has(alias))) throw new Error("Operation must come from actual returned references");
    task.status = update.status;
    task.submission = { summary: update.summary, operation_aliases: copy(update.operation_aliases), remaining_work: update.remaining_work };
    task.review = null;
    // No affected-check binding exists in this prototype: conservatively clear
    // this Task's old assessments, without claiming cross-Stage invalidation.
    task.checks.forEach((check) => { check.assessment = null; });
  });
  const applyReviewerUpdates = (state, stageId, updates) => ownerBatch(state, stageId, updates, (task, update) => {
    closed(update, ["task_id", "status", "checks", "reason", "repair_instructions"]);
    if (!["passed", "needs_repair"].includes(update.status) || ![update.reason, update.repair_instructions].every(text) || !Array.isArray(update.checks)) throw new Error("Invalid Reviewer output");
    if (!task.submission || !["submitted", "needs_repair", "passed"].includes(task.status)) throw new Error("Review requires current submitted work");
    const seen = new Set();
    for (const result of update.checks) {
      closed(result, ["check_id", "verdict", "finding"]); unique(result.check_id, seen);
      const check = task.checks.find((item) => item.check_id === result.check_id);
      if (!check || !["pass", "fail", "unable_to_assess"].includes(result.verdict) || !text(result.finding)) throw new Error("Invalid existing Check assessment");
      check.assessment = { verdict: result.verdict, finding: result.finding };
    }
    if (update.status === "passed" && (!task.checks.length || task.checks.some((check) => check.assessment?.verdict !== "pass"))) throw new Error("Required current Checks have not passed");
    task.status = update.status;
    task.review = { reason: update.reason, repair_instructions: update.repair_instructions };
  });
  const taskPresentation = (status) => ({
    planned: { label: "待执行", mark: "○" }, in_progress: { label: "进行中", mark: "◐" },
    submitted: { label: "待检查", mark: "↗" }, needs_repair: { label: "需修订", mark: "↺" }, passed: { label: "检查通过", mark: "✓" },
  }[status] ?? { label: "状态未知", mark: "?" });
  const stagePresentation = (stage) => {
    const tasks = stage.tasks, passed = tasks.filter((task) => task.status === "passed").length;
    const label = tasks.length && passed === tasks.length ? "等待用户确认" : tasks.some((task) => task.status === "needs_repair") ? "有任务需修订" : tasks.some((task) => task.status === "in_progress") ? "建模进行中" : tasks.some((task) => task.status === "submitted") ? "等待检查" : passed ? "阶段进行中" : "尚未执行";
    return { label, passed, total: tasks.length };
  };
  const validateDependencies = (tree, edges) => {
    if (!Array.isArray(edges)) throw new Error("Dependencies have not been assessed");
    const ids = new Set(tree?.stages.map((stage) => stage.stage_id) ?? []), seen = new Set(), next = new Map();
    for (const edge of edges) {
      closed(edge, ["from_stage_id", "to_stage_id", "reason"]);
      if (!ids.has(edge.from_stage_id) || !ids.has(edge.to_stage_id) || edge.from_stage_id === edge.to_stage_id || !text(edge.reason) || !edge.reason.trim()) throw new Error("Invalid dependency endpoint or reason");
      const key = JSON.stringify([edge.from_stage_id, edge.to_stage_id]);
      if (seen.has(key)) throw new Error("Duplicate dependency");
      seen.add(key);
      next.set(edge.from_stage_id, [...(next.get(edge.from_stage_id) ?? []), edge.to_stage_id]);
    }
    const visiting = new Set(), visited = new Set();
    const visit = (id) => {
      if (visiting.has(id)) throw new Error("Cyclic stage dependencies");
      if (visited.has(id)) return;
      visiting.add(id); (next.get(id) ?? []).forEach(visit); visiting.delete(id); visited.add(id);
    };
    ids.forEach(visit);
    return copy(edges);
  };
  const routeResponse = (state, response) => {
    if (response.kind === "feedback") return { action: String(response.text ?? "").trim() ? "reenter" : "wait_for_input", nextAgent: "Global Planner" };
    if (!state?.complete || state.clarifications == null) return { action: "assess_clarifications", nextAgent: "Global Planner" };
    if (state.clarifications.length) {
      const defaults = state.clarifications.map(({ id, defaultAnswer }) => ({ id, answer: defaultAnswer }));
      return defaults.some((item) => !item.answer.trim()) ? { action: "prepare_defaults", nextAgent: "Global Planner" } : { action: "adopt_defaults", defaults, nextAgent: "Global Planner" };
    }
    try {
      validateTree(state.ifc_task_tree);
      if (!state.ifc_task_tree.stages.length || state.ifc_task_tree.stages.some((stage) => !stage.tasks.length)) throw new Error("Empty plan");
      validateDependencies(state.ifc_task_tree, state.stage_dependencies);
    } catch { return { action: "complete_plan", nextAgent: "Global Planner" }; }
    return { action: "continue", nextAgent: "Stage Planner" };
  };
  const dag = (tree, edges) => {
    validateDependencies(tree, edges);
    const parents = new Map(tree.stages.map((stage) => [stage.stage_id, edges.filter((edge) => edge.to_stage_id === stage.stage_id).map((edge) => edge.from_stage_id)]));
    const levels = new Map();
    const level = (id) => {
      if (!levels.has(id)) levels.set(id, parents.get(id).length ? 1 + Math.max(...parents.get(id).map(level)) : 0);
      return levels.get(id);
    };
    return { nodes: tree.stages.map((stage, index) => ({ id: stage.stage_id, title: stage.title, code: String(index + 1).padStart(2, "0"), level: level(stage.stage_id), dependsOn: parents.get(stage.stage_id) })) };
  };
  const markdown = (tree, edges, clarifications) => {
    const cell = (value) => String(value).replaceAll("|", "\\|").replaceAll("\n", "<br>");
    const names = new Map(tree.stages.map((stage) => [stage.stage_id, stage.title]));
    let value = "# IFC 整体建模计划\n\n计划待确认，各阶段尚未执行。详细检查由 Stage Planner 继续细化；提交与验收结论尚未产生。\n\n";
    for (const [index, stage] of tree.stages.entries()) {
      value += `## ${String(index + 1).padStart(2, "0")} ${stage.title}\n\n### 整体目标\n\n${stage.goal}\n\n### 具体内容\n\n${stage.content}\n\n### 主要任务\n\n`;
      for (const task of stage.tasks) {
        value += `#### ${task.title}\n\n${task.description}\n\n${task.content}\n\n状态：${taskPresentation(task.status).label}。\n\n`;
        if (task.submission) value += `提交：${task.submission.summary}\n\n关键操作：${task.submission.operation_aliases.join("、") || "未引用关键操作"}。\n\n剩余工作：${task.submission.remaining_work || "未声明剩余工作"}。\n\n`;
        if (task.checks.length) value += task.checks.map((check) => `- ${check.description}：${check.assessment ? check.assessment.verdict + "；" + check.assessment.finding : "尚未评估"}`).join("\n") + "\n\n";
        if (task.review) value += `检查决定：${task.review.reason}\n\n修订要求：${task.review.repair_instructions || "无"}。\n\n`;
      }
    }
    value += "## 阶段依赖\n\n";
    value += edges == null ? "尚未规划。\n\n" : edges.length ? "| 前序 | 后继 | 依赖原因 |\n| --- | --- | --- |\n" + edges.map((edge) => `| ${[names.get(edge.from_stage_id), names.get(edge.to_stage_id), edge.reason].map(cell).join(" | ")} |`).join("\n") + "\n\n" : "已评估，无依赖边。\n\n";
    value += "## 本轮澄清\n\n";
    value += clarifications == null ? "本轮尚未评估。\n" : clarifications.length ? clarifications.map((item) => `### ${item.topic}\n\n${item.question}\n\n默认方案：${item.defaultAnswer || "暂无合理默认方案，需要补充回答。"}\n\n${item.notes}\n`).join("\n") : "本轮已评估，没有新增需要立即回答的问题。缺失工程资料、实际接口和燃气表具体方案保留在后续任务中。\n";
    return value;
  };
  window.IFC_PLAN = {
    validateTree, applyGlobalPlan, applyStagePlan, validateComponentPlan, routeStage, taskChanges, applyModelerUpdates, applyReviewerUpdates, taskPresentation, stagePresentation, validateDependencies, routeResponse, dag, markdown,
    beginEntry: (state, feedback) => window.beginRoleEntry(state, feedback),
    applyClarifications: (state, update) => window.applyClarificationsUpdate(state, update),
  };
})();
