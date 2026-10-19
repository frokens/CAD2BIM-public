(() => {
  // Playback pacing only. These chunks are not provider token or timing records.
  const textFrames = (text) => {
    const chars = Array.from(text);
    const frames = [];
    const width = Math.max(2, Math.ceil(chars.length / 100));
    for (let end = width; end < chars.length; end += width) frames.push(chars.slice(0, end).join(""));
    frames.push(text);
    return frames;
  };

  // One cancellable clock owns text, tool completion and result publication.
  const createClock = (schedule, cancel) => {
    let timer = null, jobs = [], cursor = 0, done = null, paused = false, generation = 0;
    const clear = () => { if (timer !== null) cancel(timer); timer = null; };
    const tick = () => {
      timer = null;
      if (paused) return;
      if (cursor >= jobs.length) { const finish = done; done = null; finish?.(); return; }
      const current = generation;
      const job = jobs[cursor++];
      job.run();
      if (current === generation && !paused) timer = schedule(tick, job.delay ?? 0);
    };
    return {
      play(next, finish) { clear(); generation += 1; jobs = next; cursor = 0; done = finish; paused = false; tick(); },
      pause() { paused = true; clear(); },
      resume() { if (paused) { paused = false; tick(); } },
      stop() { clear(); generation += 1; jobs = []; cursor = 0; done = null; paused = false; },
    };
  };

  const structuredSteps = (role, state) => {
    if (role === "Requirement Agent" || role === "CAD Agent") return [
      { field: "content", label: role === "Requirement Agent" ? "需求列表" : "CAD 整理结果", value: state?.content },
      { field: "clarifications", label: "澄清列表", value: state?.clarifications },
    ];
    if (role === "Global Planner") return [
      { field: "ifc_task_tree", label: "任务树", value: state?.kind === "task_tree" ? state.ifc_task_tree : undefined },
      { field: "clarifications", label: "澄清列表", value: state?.clarifications },
    ];
    if (["Stage Planner", "Modeler", "Reviewer"].includes(role)) return [
      { field: "ifc_task_tree", label: role === "Stage Planner" ? "阶段任务与检查条件" : role === "Modeler" ? "任务提交" : "任务检查结果", value: state?.ifc_task_tree },
      ...(role === "Stage Planner" ? [{ field: "clarifications", label: "澄清列表", value: state?.clarifications }] : []),
    ].filter((step) => state?.updated_fields?.includes(step.field));
    return [];
  };
  window.CAD2BIM_REPLAY = { textFrames, createClock, structuredSteps };
})();
