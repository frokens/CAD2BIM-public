(() => {
  const prefix = "/api/component";
  const selectionKey = "cad2bim:selected-task";
  const segment = (value) => encodeURIComponent(value);
  const runPath = (binding) => `/tasks/${segment(binding.task.task_id)}/runs/${segment(binding.run.run_id)}`;
  const contentPath = /^\/tasks\/[A-Za-z0-9_-]+\/(?:runs\/[A-Za-z0-9_-]+\/items\/[A-Za-z0-9_-]+\/content\/[0-9]+|artifacts\/[A-Za-z0-9_-]+\/versions\/[A-Za-z0-9_-]+\/content)$/;
  const publicContent = (value) => {
    if (Array.isArray(value)) return value.map(publicContent);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key,
      (key === "url" || key === "content_url") && typeof item === "string" && contentPath.test(item)
        ? `${prefix}${item}` : publicContent(item),
    ]));
  };

  const createConnection = ({ chat = window.CAD2BIM_TASK_CHAT, fetch: fetchRequest = window.fetch.bind(window), EventSource: EventStream = window.EventSource, storage = window.localStorage, setTimeout: later = window.setTimeout.bind(window), clearTimeout: cancelLater = window.clearTimeout.bind(window) } = {}) => {
    const bindings = new Map();
    const requests = new Map();
    let selectedViewId = null;
    let stream = null;
    let streamViewId = null;
    let reconnect = null;
    let connectDeadline = null;
    let retryAttempt = 0;
    let online = window.navigator?.onLine !== false;
    let restoreTimer = null;
    let restoreAttempt = 0;
    let restorePending = false;
    let selectionGeneration = 0;
    let disposed = false;
    let restoring = false;
    let taskRefreshTimer = null;
    let taskRefreshPending = false;
    let taskRefreshBlocked = false;

    const request = async (path, { method = "GET", payload, body } = {}) => {
      const headers = { accept: "application/json" };
      if (payload !== undefined) headers["content-type"] = "application/json";
      const abort = new window.AbortController();
      let deadline;
      try {
        return await Promise.race([
          (async () => {
            const response = await fetchRequest(`${prefix}${path}`, {
              method, credentials: "same-origin", headers, signal: abort.signal,
              body: payload === undefined ? body : JSON.stringify(payload),
            });
            const data = response.status === 204 ? null : await response.json();
            if (!response.ok) {
              const error = new Error(typeof data.detail === "string" ? data.detail : data.detail?.message ?? "请求未完成，请重试");
              error.status = response.status;
              throw error;
            }
            return data;
          })(),
          new Promise((_, reject) => {
            deadline = later(() => {
              reject(new Error("连接超时，请检查网络后重试"));
              abort.abort();
            }, method === "GET" ? 30000 : 120000);
          }),
        ]);
      } finally {
        cancelLater(deadline);
      }
    };
    const displaySources = (item) => {
      // Reading an existing resource or Operation is investigation, not a new
      // delivery. Preview keeps only the images returned by that call.
      if (item.kind !== "tool" || item.name !== "ifc_authoring_publish" || item.status !== "succeeded" || !item.result || item.result.isError) return [];
      const sources = new Map();
      const add = (source_kind, source_ref) => {
        if (typeof source_ref === "string" && source_ref) sources.set(`${source_kind}:${source_ref}`, { source_kind, source_ref });
      };
      const published = (value) => {
        if (!value || typeof value !== "object") return;
        for (const geometry of Array.isArray(value.geometry_versions) ? value.geometry_versions : []) add("geometry_resource_version", geometry?.resource_version_alias);
        if (value.standard_model_available !== false) add("family_standard_model", value.family_version_alias);
      };
      published(item.result.structuredContent);
      for (const block of item.content ?? item.result.content ?? []) if (block.type === "text") {
        try { published(JSON.parse(block.text)); } catch { /* Prose is not a publication result. */ }
      }
      return [...sources.values()];
    };
    const loadDisplay = async (viewId, binding, source, itemId, retry = false) => {
      if (!chat.receiveDisplay || disposed) return;
      const key = `${source.source_kind}:${source.source_ref}`;
      const delivery = binding.deliveries.get(key);
      if (!delivery || delivery.sequence >= binding.sealedThrough) return;
      if (!retry && binding.displays.has(key)) return;
      binding.displays.set(key, "loading");
      let title = delivery.title || (source.source_kind === "geometry_resource_version" ? "几何部件" : "产品族");
      let stage = "info";
      const loading = (message) => chat.receiveDisplay(viewId, { key, source, item_id: itemId, status: "loading", title, stage, message });
      loading("正在读取构件名称与保存模型信息…");
      try {
        let displaySource = source;
        let displayNote = null;
        const kind = source.source_kind === "geometry_resource_version" ? "geometry" : "families";
        const info = await request(`/library/${kind}/info?reference=${segment(source.source_ref)}&task_id=${segment(binding.task.task_id)}&run_id=${segment(binding.run.run_id)}`);
        if (disposed) return;
        title = info.catalog?.display_name || title;
        delivery.title = title;
        if (source.source_kind === "geometry_resource_version") {
          if (!info.display_source) {
            binding.displays.set(key, "unavailable");
            chat.receiveDisplay(viewId, { key, source, item_id: itemId, status: "unavailable",
              title, message: info.display_message || "该几何尚无可展示的保存模型。" });
            return;
          }
          displaySource = { source_kind: info.display_source.source_kind, source_ref: info.display_source.source_ref };
          if (displaySource.source_kind === "geometry_inspection") displayNote = "默认参数的保存预览";
        }
        const path = `${runPath(binding)}/display`;
        let display;
        stage = "read";
        loading("正在读取已保存的三维展示…");
        try { display = await request(`${path}?source_kind=${segment(displaySource.source_kind)}&source_ref=${segment(displaySource.source_ref)}`); }
        catch (error) {
          if (error.status !== 404) throw error;
          stage = "prepare";
          loading("正在从保存模型准备三维展示…");
          display = await request(path, { method: "POST", payload: displaySource });
        }
        if (disposed) return;
        binding.displays.set(key, "ready");
        chat.receiveDisplay(viewId, { key, source, item_id: itemId, status: "ready", display_note: displayNote, display: publicContent(display) });
      } catch (error) {
        if (disposed) return;
        binding.displays.set(key, "failed");
        chat.receiveDisplay(viewId, { key, source, item_id: itemId, status: "failed", title, stage, error: error.message });
      }
    };
    const loadItemDisplays = (viewId, binding, items) => {
      // A saved Role Card seals the Main replies for that role entry. Publication
      // alone, a final-answer delta, pause, or a Run status cannot seal the turn.
      for (const item of items) {
        if (!Number.isSafeInteger(item.sequence)) continue;
        if (item.kind === "card" && item.status === "succeeded") binding.sealedThrough = Math.max(binding.sealedThrough, item.sequence);
        for (const source of displaySources(item)) {
          const key = `${source.source_kind}:${source.source_ref}`;
          if (!binding.deliveries.has(key)) binding.deliveries.set(key, { source, item_id: item.item_id, sequence: item.sequence });
        }
      }
      for (const delivery of binding.deliveries.values()) {
        if (delivery.sequence < binding.sealedThrough) void loadDisplay(viewId, binding, delivery.source, delivery.item_id);
      }
    };
    const rememberSelection = (binding) => {
      if (!binding?.run) return;
      try { storage?.setItem(selectionKey, JSON.stringify({ task_id: binding.task.task_id, run_id: binding.run.run_id })); } catch { /* Storage may be disabled. */ }
    };
    const closeStream = () => {
      stream?.close();
      stream = null;
      if (streamViewId !== null) chat.clearActivity(streamViewId);
      streamViewId = null;
      if (reconnect !== null) cancelLater(reconnect);
      reconnect = null;
      if (connectDeadline !== null) cancelLater(connectDeadline);
      connectDeadline = null;
    };
    const scheduleReconnect = (viewId, binding, generation, error) => {
      if (disposed || generation !== selectionGeneration || viewId !== selectedViewId) return;
      if (reconnect !== null) cancelLater(reconnect);
      reconnect = null;
      // Authentication and missing resources need correction, not repeated traffic.
      if ([401, 403, 404].includes(error?.status)) {
        chat.connectionError?.(error, viewId);
        return;
      }
      const delay = Math.min(1000 * 2 ** Math.min(retryAttempt++, 5), 30000);
      chat.connectionError?.(new Error(online ? `连接中断，${delay / 1000} 秒后自动重连` : "网络已断开，恢复后将自动重连"), viewId);
      if (!online) return;
      reconnect = later(() => {
        reconnect = null;
        if (binding.loaded) subscribe(viewId, binding, generation);
        else void snapshot(viewId, binding, generation).catch(() => {});
      }, delay);
    };
    const state = (viewId, binding, run) => {
      if (run?.run_id === binding.run?.run_id && Date.parse(run?.updated_at) < Date.parse(binding.run?.updated_at)) return;
      if (run) binding.run = run;
      chat.updateTaskState(viewId, binding.run);
      scheduleTaskRefresh();
    };
    const taskInfo = (viewId, binding, task) => {
      if (!task || task.task_id !== binding.task.task_id) return;
      binding.task = { ...binding.task, ...task };
      chat.updateTaskInfo?.(viewId, binding.task);
    };
    const backgroundTasks = () => [...bindings].filter(([viewId, binding]) => viewId !== selectedViewId &&
      ["queued", "running"].includes(binding.run?.status?.toLowerCase()));
    const scheduleTaskRefresh = () => {
      if (!backgroundTasks().length) {
        if (taskRefreshTimer !== null) cancelLater(taskRefreshTimer);
        taskRefreshTimer = null;
        return;
      }
      if (disposed || !online || taskRefreshBlocked || taskRefreshPending || taskRefreshTimer !== null) return;
      taskRefreshTimer = later(() => { taskRefreshTimer = null; void refreshBackgroundTasks(); }, 5000);
    };
    const refreshBackgroundTasks = async () => {
      if (disposed || !online || taskRefreshPending) return;
      const pending = new Map(backgroundTasks().map(([viewId, binding]) => [binding.task.task_id, { viewId, binding }]));
      if (!pending.size) return;
      taskRefreshPending = true;
      try {
        let cursor = null;
        do {
          const page = await request(`/tasks?limit=50${cursor ? `&after_task_id=${segment(cursor)}` : ""}`);
          if (disposed || !online) return;
          for (const task of page.tasks) {
            const target = pending.get(task.task_id);
            if (!target) continue;
            pending.delete(task.task_id);
            const { viewId, binding } = target;
            if (bindings.get(viewId) !== binding || selectedViewId === viewId) continue;
            if (Date.parse(task.updated_at) < Date.parse(binding.task.updated_at)) continue;
            taskInfo(viewId, binding, task);
            // Refresh only the existing Run; never switch Runs or consume its SSE cursor.
            if (task.latest_run?.run_id === binding.run?.run_id) state(viewId, binding, task.latest_run);
          }
          cursor = page.next_after_task_id;
        } while (pending.size && cursor);
      } catch (error) {
        taskRefreshBlocked = [401, 403, 404].includes(error.status);
        // The active conversation owns its connection notice and reconnection.
      } finally {
        taskRefreshPending = false;
        scheduleTaskRefresh();
      }
    };
    const receiveItem = (viewId, binding, item) => {
      if (item.message_id && item.kind === "assistant") {
        binding.closedMessages.add(item.message_id);
        binding.deltas.delete(item.message_id);
      }
      chat.receiveItem(viewId, publicContent(item));
      loadItemDisplays(viewId, binding, [item]);
    };
    const receiveDelta = (viewId, binding, delta) => {
      if (!delta.message_id || binding.closedMessages.has(delta.message_id)) return;
      let current = binding.deltas.get(delta.message_id);
      if (!current) {
        current = { callId: delta.call_id, next: 0, incomplete: false, started: false };
        binding.deltas.set(delta.message_id, current);
      }
      if (current.callId !== delta.call_id) return;
      if (delta.phase === "retry") {
        const attempt = Number(delta.retry_attempt);
        if (!Number.isSafeInteger(attempt) || attempt <= (current.retryAttempt ?? 0)) return;
        Object.assign(current, { next: 0, incomplete: false, started: true, retryAttempt: attempt });
        chat.receiveTextDelta(viewId, delta);
        return;
      }
      // Lifecycle signals do not consume token indices; errors also close an incomplete prefix.
      if (delta.phase === "error") {
        binding.closedMessages.add(delta.message_id);
        binding.deltas.delete(delta.message_id);
        chat.receiveTextDelta(viewId, delta);
        return;
      }
      if (delta.phase === "start") {
        if (current.started) return;
        current.started = true;
        chat.receiveTextDelta(viewId, delta);
        return;
      }
      if (delta.phase !== "delta" || !Number.isSafeInteger(delta.delta_index) || delta.delta_index < current.next || current.incomplete) return;
      current.started = true;
      if (delta.delta_index !== current.next) {
        current.incomplete = true;
        chat.receiveTextDelta(viewId, { ...delta, text: "", incomplete: true });
        return;
      }
      current.next += 1;
      chat.receiveTextDelta(viewId, delta);
    };
    const subscribe = (viewId, binding, generation) => {
      if (disposed || generation !== selectionGeneration || viewId !== selectedViewId) return;
      if (!online) { scheduleReconnect(viewId, binding, generation); return; }
      const source = new EventStream(`${prefix}${runPath(binding)}/events?after_sequence=${binding.sequence}`);
      stream = source;
      streamViewId = viewId;
      const live = () => stream === source && generation === selectionGeneration && selectedViewId === viewId && !disposed;
      source.onopen = () => {
        if (!live()) return;
        if (connectDeadline !== null) cancelLater(connectDeadline);
        connectDeadline = null;
        retryAttempt = 0;
        chat.connectionRestored?.(viewId);
        // A reader reconnect never re-runs work. Refresh the durable status even
        // if the server had no new public events during the disconnected period.
        void request(runPath(binding)).then((run) => { if (live() && run?.run_id === binding.run?.run_id) state(viewId, binding, run); }).catch(() => {});
      };
      for (const name of ["item", "run", "task"]) source.addEventListener(name, (event) => {
        if (!live()) return;
        const sequence = Number(event.lastEventId);
        if (!Number.isSafeInteger(sequence) || sequence <= binding.sequence) return;
        const data = JSON.parse(event.data);
        // PUBLIC events have legitimate holes in the original RunEvent sequence.
        if (name === "item") receiveItem(viewId, binding, data);
        else if (name === "task") taskInfo(viewId, binding, data);
        else state(viewId, binding, data);
        binding.sequence = sequence;
      });
      source.addEventListener("text_delta", (event) => {
        if (live()) receiveDelta(viewId, binding, JSON.parse(event.data));
      });
      source.addEventListener("activity", (event) => {
        if (!live()) return;
        const data = JSON.parse(event.data);
        if (data.kind === "runtime") {
          const sequence = Number(event.lastEventId || data.event_sequence);
          if (!Number.isSafeInteger(sequence) || sequence <= binding.sequence) return;
          data.event_sequence = sequence;
          // A callback's activity and tool status share one durable event. Apply
          // both before advancing its cursor, including after a network switch.
          if (data.item) receiveItem(viewId, binding, data.item);
          chat.receiveActivity(viewId, data);
          binding.sequence = sequence;
          return;
        }
        chat.receiveActivity(viewId, data);
      });
      source.onerror = () => {
        if (!live()) return;
        source.close();
        stream = null;
        if (connectDeadline !== null) cancelLater(connectDeadline);
        connectDeadline = null;
        chat.clearActivity(viewId);
        streamViewId = null;
        // Reconnect only the reader. This never starts, resumes or cancels a Run.
        scheduleReconnect(viewId, binding, generation);
      };
      connectDeadline = later(() => { if (live()) source.onerror(); }, 15000);
    };
    const snapshot = async (viewId, binding, generation) => {
      let data;
      chat.loadingTask?.(viewId, true);
      try { data = await request(`${runPath(binding)}/snapshot`); } catch (error) {
        if (!disposed && generation === selectionGeneration && selectedViewId === viewId) {
          scheduleReconnect(viewId, binding, generation, error);
        }
        throw error;
      } finally {
        if (!disposed && generation === selectionGeneration) chat.loadingTask?.(viewId, false);
      }
      if (disposed || generation !== selectionGeneration || selectedViewId !== viewId) return;
      chat.clearActivity(viewId);
      binding.sequence = data.event_sequence;
      binding.loaded = true;
      binding.deltas.clear();
      for (const item of data.items) if (item.kind === "assistant" && item.message_id) binding.closedMessages.add(item.message_id);
      state(viewId, binding, data.run);
      taskInfo(viewId, binding, data.task);
      chat.receiveSnapshot(viewId, publicContent({ ...data, run: binding.run }));
      loadItemDisplays(viewId, binding, data.items);
      rememberSelection(binding);
      subscribe(viewId, binding, generation);
    };
    const bindingFor = (viewId) => {
      const binding = bindings.get(viewId);
      if (!binding?.run) throw new Error("任务尚未开始");
      return binding;
    };
    const addBinding = (viewId, task, run = null) => {
      let binding = bindings.get(viewId);
      if (!binding) {
        binding = { task, run, sequence: 0, deltas: new Map(), closedMessages: new Set(), controls: new Map(), displays: new Map(), deliveries: new Map(), sealedThrough: 0 };
        bindings.set(viewId, binding);
      } else {
        binding.task = task;
        if (run) binding.run = run;
      }
      taskInfo(viewId, binding, task);
      scheduleTaskRefresh();
      return binding;
    };
    const selectTask = async (viewId) => {
      selectedViewId = viewId || null;
      if (restoring) return;
      const generation = ++selectionGeneration;
      closeStream();
      retryAttempt = 0;
      scheduleTaskRefresh();
      const binding = bindings.get(viewId);
      if (binding?.run) {
        rememberSelection(binding);
        if (binding.loaded) subscribe(viewId, binding, generation);
        else await snapshot(viewId, binding, generation);
      }
      else try { storage?.removeItem(selectionKey); } catch { /* Storage may be disabled. */ }
    };
    const control = async (action, value) => {
      const viewId = typeof value === "string" ? value : value.viewId;
      const binding = bindingFor(viewId);
      let identity = binding.controls.get(action);
      if (!identity) { identity = window.crypto.randomUUID(); binding.controls.set(action, identity); }
      const run = await request(`${runPath(binding)}/${action}`, { method: "POST", payload: { client_request_id: identity } });
      binding.controls.delete(action);
      state(viewId, binding, run);
      return run;
    };
    const sendInput = async (input) => {
      // CAD2BIM keeps its original frontend behavior; this transport starts Component only.
      if (input.taskType !== "component") return;
      let record = requests.get(input.requestId);
      if (!record) { record = { uploads: [], receipt: null }; requests.set(input.requestId, record); }
      if (record.receipt) {
        chat.receiveInputReceipt(input.viewId, input.requestId, record.receipt);
        return record.receipt;
      }
      let binding = bindings.get(input.viewId);
      if (!binding) {
        const title = "新建 Component";
        const task = await request("/tasks", { method: "POST", payload: { title, metadata: { task_type: "component" }, idempotency_key: `${input.requestId}:task` } });
        binding = addBinding(input.viewId, task);
      }
      for (let index = 0; index < input.attachments.length; index += 1) {
        if (record.uploads[index]) continue;
        const file = input.attachments[index];
        const form = new window.FormData();
        form.append("file", file, file.name);
        form.append("client_request_id", `${input.requestId}:attachment:${index}`);
        record.uploads[index] = await request(`/tasks/${segment(binding.task.task_id)}/attachments`, { method: "POST", body: form });
      }
      const content = input.text ? [{ type: "text", text: input.text }] : [];
      for (let index = 0; index < record.uploads.length; index += 1) {
        const upload = record.uploads[index];
        content.push({ type: upload.content_type.startsWith("image/") ? "image" : "file", artifact_id: upload.artifact_id, version_id: upload.version_id });
      }
      if (!binding.run) {
        const run = await request(`/tasks/${segment(binding.task.task_id)}/runs`, { method: "POST", payload: { module: "component", stage_key: "component", content, idempotency_key: input.requestId } });
        binding.run = run;
        record.receipt = run.initial_feedback || run;
      } else {
        record.receipt = await request(`${runPath(binding)}/feedback`, { method: "POST", payload: { client_request_id: input.requestId, delivery_mode: "steer", content } });
      }
      // Associate the real feedback identity before a snapshot can publish that same user input.
      chat.receiveInputReceipt(input.viewId, input.requestId, record.receipt);
      state(input.viewId, binding, binding.run);
      if (selectedViewId === input.viewId || selectedViewId === null) {
        selectedViewId = input.viewId;
        rememberSelection(binding);
        if (!stream) await selectTask(input.viewId).catch(() => {});
      }
      return record.receipt;
    };
    const restoreSavedTasks = async () => {
      const restorationGeneration = selectionGeneration;
      let selected = null;
      try { selected = JSON.parse(storage?.getItem(selectionKey) || "null"); } catch { /* Ignore invalid or unavailable storage. */ }
      let selectedBinding = null;
      let afterTaskId = null;
      do {
        const page = await request(`/tasks?limit=50${afterTaskId ? `&after_task_id=${segment(afterTaskId)}` : ""}`);
        for (const task of page.tasks) {
          const run = task.latest_run || null;
          if (task.task_type !== "component") continue;
          const existing = [...bindings].find(([, binding]) => binding.task.task_id === task.task_id);
          const viewId = existing?.[0] ?? chat.restoreTask(task, run);
          const binding = addBinding(viewId, task, run);
          if (selected?.task_id === task.task_id) selectedBinding = { viewId, binding };
        }
        afterTaskId = page.next_after_task_id;
      } while (afterTaskId);
      if (selected && !selectedBinding && restorationGeneration === selectionGeneration) {
        try {
          const task = await request(`/tasks/${segment(selected.task_id)}`);
          const run = await request(`/tasks/${segment(selected.task_id)}/runs/${segment(selected.run_id)}`);
          const viewId = chat.restoreTask(task, run);
          selectedBinding = { viewId, binding: addBinding(viewId, task, run) };
        } catch (error) {
          if (error.status !== 404) throw error;
          storage?.removeItem(selectionKey);
        }
      }
      if (selectedBinding && selectedViewId === null && restorationGeneration === selectionGeneration) {
        const { viewId, binding } = selectedBinding;
        if (selected.run_id !== binding.run?.run_id) binding.run = await request(`/tasks/${segment(selected.task_id)}/runs/${segment(selected.run_id)}`);
        restoring = true;
        try { chat.selectTask(viewId); } finally { restoring = false; }
        await selectTask(viewId);
      }
    };
    const restore = async () => {
      if (disposed) return;
      if (restoreTimer !== null) cancelLater(restoreTimer);
      restoreTimer = null;
      try {
        await restoreSavedTasks();
        restorePending = false;
        restoreAttempt = 0;
      } catch (error) {
        if (disposed) return;
        restorePending = ![401, 403, 404].includes(error.status);
        chat.connectionError?.(error, selectedViewId);
        if (restorePending && online) restoreTimer = later(() => {
          restoreTimer = null;
          void restore().catch(() => {});
        }, Math.min(1000 * 2 ** Math.min(restoreAttempt++, 5), 30000));
        throw error;
      }
    };
    const onSelection = (event) => { void selectTask(event.detail.viewId).catch(() => {}); };
    const onCreation = (event) => { void selectTask(event.detail.viewId); };
    const onOffline = () => {
      online = false;
      closeStream();
      if (taskRefreshTimer !== null) cancelLater(taskRefreshTimer);
      taskRefreshTimer = null;
      if (restoreTimer !== null) cancelLater(restoreTimer);
      restoreTimer = null;
      const binding = bindings.get(selectedViewId);
      if (binding?.run) scheduleReconnect(selectedViewId, binding, selectionGeneration);
    };
    const onOnline = () => {
      online = true;
      taskRefreshBlocked = false;
      scheduleTaskRefresh();
      if (restorePending) void restore().catch(() => {});
      const binding = bindings.get(selectedViewId);
      if (!binding?.run || disposed) return;
      selectionGeneration += 1;
      closeStream();
      retryAttempt = 0;
      chat.connectionError?.(new Error("网络已恢复，正在重新连接并同步任务"), selectedViewId);
      if (binding.loaded) subscribe(selectedViewId, binding, selectionGeneration);
      else void snapshot(selectedViewId, binding, selectionGeneration).catch(() => {});
    };
    window.addEventListener("cad2bim:task-selected", onSelection);
    window.addEventListener("cad2bim:task-created", onCreation);
    window.addEventListener("offline", onOffline);
    window.addEventListener("online", onOnline);
    return {
      sendInput, selectTask, restore,
      pause: (value) => control("pause", value), resume: (value) => control("resume", value),
      deleteTask: async (viewId) => {
        const binding = bindings.get(viewId);
        if (binding) await request(`/tasks/${segment(binding.task.task_id)}`, { method: "DELETE" });
        if (selectedViewId === viewId) {
          selectionGeneration += 1;
          closeStream();
          selectedViewId = null;
          storage?.removeItem(selectionKey);
        }
        bindings.delete(viewId);
      },
      openTask: async (viewId, task, run) => { addBinding(viewId, task, run); await selectTask(viewId); },
      getItem: async (viewId, itemId) => publicContent(await request(`${runPath(bindingFor(viewId))}/items/${segment(itemId)}`)),
      loadHistory: async (viewId, beforeSequence) => {
        const binding = bindingFor(viewId);
        const data = await request(`${runPath(binding)}/snapshot?before_sequence=${segment(beforeSequence)}&limit=100`);
        loadItemDisplays(viewId, binding, data.items);
        return publicContent(data);
      },
      retryDisplay: (viewId, value) => loadDisplay(viewId, bindingFor(viewId), value.source, value.item_id, true),
      dispose() { disposed = true; selectionGeneration += 1; closeStream(); if (restoreTimer !== null) cancelLater(restoreTimer); if (taskRefreshTimer !== null) cancelLater(taskRefreshTimer); window.removeEventListener("cad2bim:task-selected", onSelection); window.removeEventListener("cad2bim:task-created", onCreation); window.removeEventListener("offline", onOffline); window.removeEventListener("online", onOnline); },
    };
  };
  const install = (chat = window.CAD2BIM_TASK_CHAT) => {
    const connection = createConnection({ chat });
    chat.connect(connection);
    void connection.restore().catch(() => {});
    return connection;
  };
  window.CAD2BIM_TASK_CONNECTION = { createConnection, install };
  if (window.CAD2BIM_TASK_CHAT?.receiveSnapshot) install();
})();
