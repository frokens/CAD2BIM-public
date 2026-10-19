(() => {
  const routeWindow = document.documentElement.classList.contains('is-embedded') ? window : window.parent;
  const api = window.CAD2BIM_LIBRARY_API?.create();
  const live = Boolean(api && !api.staticCity);
  const resources = live ? [] : window.CAD2BIM_RESOURCE_LIBRARY || [];
  const tasks = live ? (api.context.task_id ? [{ id: api.context.task_id, name: '当前任务' }] : []) : window.CAD2BIM_LIBRARY_TASKS || [];
  const esc = (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
  const scopes = { task: '任务', tenant: '团队共享', system: '系统内置' };
  let selectedTask = tasks[0]?.id || '';
  const state = { family: { scope: 'all', search: '', scroll: 0 }, geometry: { scope: 'all', search: '', scroll: 0 } };
  const scroll = document.querySelector('.resource-scroll');
  const detail = document.getElementById('resourceDetail');
  const sourceDialog = document.getElementById('resourceSourceDialog');
  const deleteDialog = document.getElementById('resourceDeleteDialog');
  let current = null, disposeViewer = null, renderToken = 0;
  let activeItem = null, codeToken = 0;
  let sourceItem = null, sourceTrigger = null;
  let deleteItem = null, deleteTrigger = null, deleting = false, listNotice = '';
  const pages = { family: { loaded: false, cursor: null }, geometry: { loaded: false, cursor: null } };
  const details = new Map();
  const thumbnails = new Map();
  const detailScroll = new Map();
  const route = () => {
    const [kind, id = '', version = ''] = (routeWindow.location.hash || location.hash).slice(1).split('/').map((part) => {
      try { return decodeURIComponent(part); } catch { return part; }
    });
    return { kind: kind === 'geometry' ? 'geometry' : 'family', id, version };
  };
  const key = (value) => `${value.kind}/${value.id}/${value.version}`;
  const navigate = (kind, id = '', version = '') => { routeWindow.location.hash = [kind, id, version].filter(Boolean).map(encodeURIComponent).join('/'); };
  const saveScroll = () => {
    if (!current) return;
    if (current.id) detailScroll.set(key(current), scroll.scrollTop);
    else state[current.kind].scroll = scroll.scrollTop;
  };
  const table = (rows, headings) => `<div class="resource-table-wrap"><table><thead><tr>${headings.map((heading) => `<th scope="col">${esc(heading)}</th>`).join('')}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${esc(cell)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  const constraintLabels = { unit: '单位', default: '默认值', default_value: '默认值', minimum: '最小值', maximum: '最大值', exclusiveMinimum: '大于', exclusiveMaximum: '小于', multipleOf: '步长倍数', enum: '可选值', const: '固定值', required: '必填', nullable: '允许空值', min_items: '最少项数', max_items: '最多项数', minItems: '最少项数', maxItems: '最多项数', uniqueItems: '各项唯一', minLength: '最短长度', maxLength: '最长长度', pattern: '匹配规则', additionalProperties: '额外字段', description: '说明', title: '名称', schema: '允许输入' };
  const constraintValue = (value) => {
    if (Array.isArray(value) && value.every((entry) => entry === null || typeof entry !== 'object')) return value.length ? `<span class="constraint-options">${value.map((entry) => `<span>${esc(entry === null ? 'null' : entry)}</span>`).join('')}</span>` : '空列表';
    if (value !== null && typeof value === 'object') return `<details class="constraint-nested"><summary>展开规则</summary><pre>${esc(JSON.stringify(value, null, 2))}</pre></details>`;
    return esc(value === null ? 'null' : typeof value === 'boolean' ? (value ? '是' : '否') : value);
  };
  const parameterTable = (item) => `<div class="resource-table-wrap resource-parameter-table"><table><thead><tr><th scope="col">字段</th><th scope="col">类型</th><th scope="col">约束</th></tr></thead><tbody>${item.parameters.map(([name, type, fallback]) => {
    const rules = item.parameterConstraints?.[name];
    const body = rules ? (Object.keys(rules).length ? `<dl class="constraint-grid">${Object.entries(rules).map(([key, value]) => `<div class="constraint-entry${key === 'description' || (value !== null && typeof value === 'object' && !Array.isArray(value)) ? ' constraint-entry-wide' : ''}"><dt title="${esc(key)}">${esc(constraintLabels[key] || key)}</dt><dd>${constraintValue(value)}</dd></div>`).join('')}</dl>` : '<span class="constraint-empty">未声明</span>') : esc(fallback);
    return `<tr><td><code>${esc(name)}</code></td><td>${esc(type)}</td><td>${body}</td></tr>`;
  }).join('')}</tbody></table></div>`;
  const section = (title, body) => body ? `<section${title === '参数' ? ' class="resource-parameters"' : ''}><h3>${esc(title)}</h3>${body}</section>` : '';
  const displayMessage = (phase) => phase === 'preparing' ? '首次打开，正在准备三维预览…' : '正在读取保存的三维预览…';
  const preview = (item, selected) => {
    if (selected?.noPreview) return '<div class="resource-no-image">此版本无预览</div>';
    if (item.image) return `<img src="${esc(item.image)}" alt="${esc(item.name)}模型图片" loading="lazy" />`;
    if (item.remote && !item.displaySource && item.displayMessage) return `<div class="resource-no-image">${esc(item.displayMessage)}</div>`;
    if (item.thumbnailMessage) return `<div class="resource-no-image">${esc(item.thumbnailMessage)}</div>`;
    if (item.remote && item.displaySource) return `<div class="resource-no-image">${esc(item.modelError || displayMessage(item.displayPhase))}</div>`;
    const model = selected?.model || item.model || (item.versions ?? []).find((version) => version.model)?.model;
    return model ? '<div class="resource-no-image">点击查看三维模型</div>' : '<div class="resource-no-image">暂无模型图片</div>';
  };
  const subtypeNames = { single_product: '单产品', assembly: '产品组合', semantic_assembly: '产品组合', modeling_function: '参数化几何', mapped_model: '固定几何' };
  const tags = (item) => `<div class="resource-tags">${[subtypeNames[item.subtype] || item.subtype, scopes[item.scope], item.scope === 'task' ? item.taskName : ''].filter(Boolean).map((value) => `<span>${esc(value)}</span>`).join('')}</div>`;
  const itemVersions = (item) => item.versions || (item.version != null ? [{ number: item.version }] : []);
  const versionLabel = (version) => version?.number != null ? `版本 ${version.number}` : '';
  const bindingButton = (item, label, version) => `<button type="button" data-resource="${esc(item.id)}" data-target-kind="${esc(item.kind)}" ${version != null ? `data-version="${esc(version)}"` : ''}><span><strong>${esc(item.name)}</strong>${label ? `<small>${esc(label)}</small>` : ''}</span><span aria-hidden="true">→</span></button>`;

  const loadList = async (kind, more = false) => {
    if (pages[kind].loaded && !more) return;
    const result = await api.list(kind, more ? pages[kind].cursor : null);
    if (!more) for (let index = resources.length - 1; index >= 0; index -= 1) {
      if (resources[index].kind === kind) resources.splice(index, 1);
    }
    for (const entry of result.resources || result.families) {
      const item = window.CAD2BIM_LIBRARY_API.summary(kind, entry);
      if (item.scope === 'task') { item.taskId = api.context.task_id; item.taskName = '当前任务'; }
      const index = resources.findIndex((existing) => existing.kind === kind && existing.id === item.id);
      if (index < 0) resources.push(item); else resources[index] = item;
    }
    pages[kind] = { loaded: true, cursor: result.next_cursor };
  };
  const loadThumbnails = (visible, token) => {
    if (!live || !api.context.task_id || !api.context.run_id) return;
    for (const item of visible) {
      const reference = itemVersions(item)[0]?.ref;
      if (!reference) continue;
      const source = { source_kind: item.kind === 'family' ? 'family_standard_model' : 'geometry_resource_version', source_ref: reference };
      const cacheKey = `${item.kind}:${reference}`;
      if (!thumbnails.has(cacheKey)) thumbnails.set(cacheKey, api.thumbnail(source)
        .then((image) => ({ image })).catch(() => ({ error: true })));
      void thumbnails.get(cacheKey).then((result) => {
        if (itemVersions(item)[0]?.ref !== reference) return;
        if (result.image) item.image = result.image;
        else item.thumbnailMessage = result.error ? '模型图片暂不可用' : '暂无已保存模型图片';
        if (token !== renderToken || current.id || current.kind !== item.kind) return;
        for (const card of document.getElementById('resourceGrid').querySelectorAll('.resource-card')) {
          if (card.dataset.resource === item.id && card.dataset.targetKind === item.kind) {
            card.querySelector('.resource-card-preview').innerHTML = preview(item, itemVersions(item)[0]);
          }
        }
      });
    }
  };
  const loadDetail = async (selection) => {
    const cacheKey = key(selection);
    if (details.has(cacheKey)) return details.get(cacheKey);
    const { kind, id, version } = selection;
    let response = await api.info(kind, version || id);
    let root, exact;
    if (['resource_root', 'family_root'].includes(response.catalog.target_kind)) {
      root = window.CAD2BIM_LIBRARY_API.details(kind, response);
      response = await api.info(kind, root.versions[0].ref);
      exact = window.CAD2BIM_LIBRARY_API.details(kind, response);
    } else {
      exact = window.CAD2BIM_LIBRARY_API.details(kind, response);
      root = window.CAD2BIM_LIBRARY_API.details(kind, await api.info(kind, exact.id));
    }
    const versions = [...root.versions];
    if (!versions.some((entry) => entry.ref === exact.selectedRef)) versions.push({ number: exact.selectedNumber, ref: exact.selectedRef });
    versions.sort((left, right) => right.number - left.number);
    const item = { ...root, ...exact, versions, versionsComplete: root.versionsComplete, beforeVersion: root.beforeVersion };
    if (item.scope === 'task') { item.taskId = api.context.task_id; item.taskName = '当前任务'; }
    details.set(cacheKey, item);
    return item;
  };
  const sourceSection = (item) => {
    if (!live || item.kind !== 'geometry') return '';
    if (!api.context.task_id || !api.context.run_id) return section('源码', '<p>请从一个现有任务打开此资源，以查看源码和模型。</p>');
    if (!item.code) return section('源码', '<p>此版本没有可读取的源码。</p>');
    const members = (item.code.members || []).filter((member) => member.text_available);
    if (item.code.source_form === 'bundle' && !members.length) return section('源码', '<p>此版本没有可读取的文本文件。</p>');
    return section('源码', '<button type="button" class="resource-more" data-read-code>查看源码</button>');
  };
  const deleteControl = (item) => !live ? '' : `<button type="button" class="resource-delete" data-delete-resource ${item.scope === 'system' ? 'disabled aria-describedby="resourceDeleteRestriction"' : ''}>删除${item.kind === 'family' ? '产品族' : '几何'}</button>${item.scope === 'system' ? '<small id="resourceDeleteRestriction">系统内置资源不能删除。</small>' : ''}`;
  const status = (message = '', failed = false) => {
    const label = document.getElementById('resourceStatus');
    if (label) { label.textContent = message; label.hidden = !message; }
    const retry = document.getElementById('resourceRetry');
    if (retry) retry.hidden = !failed;
  };

  async function render() {
    saveScroll();
    const next = route();
    const changed = !current || key(current) !== key(next);
    if (changed && sourceDialog?.open) sourceDialog.close();
    if (changed && deleteDialog?.open && !deleting) deleteDialog.close();
    current = next;
    const token = ++renderToken;
    disposeViewer?.(); disposeViewer = null;
    const { kind, id, version } = current;
    document.querySelectorAll('[data-kind]').forEach((button) => {
      button.classList.toggle('is-active', button.dataset.kind === kind);
      button.setAttribute('aria-pressed', String(button.dataset.kind === kind));
    });
    let item;
    if (live) {
      activeItem = null; detail.hidden = true;
      document.getElementById('resourceList').hidden = Boolean(id);
      status(id ? '正在读取保存的版本…' : '正在读取资源库…');
      try {
        if (id) item = await loadDetail(current); else await loadList(kind);
      } catch (error) {
        if (token !== renderToken) return;
        document.getElementById('resourceGrid').replaceChildren();
        detail.replaceChildren(); detail.hidden = true;
        document.getElementById('resourceEmpty').hidden = true;
        document.getElementById('resourceList').hidden = Boolean(id);
        const back = document.getElementById('resourceBack');
        back.hidden = !id; back.dataset.back = kind; back.textContent = `← 返回${kind === 'family' ? '产品族' : '几何'}`;
        status(error.message, true);
        return;
      }
      if (token !== renderToken) return;
      status(id ? '' : listNotice); listNotice = '';
    } else item = resources.find((entry) => entry.kind === kind && entry.id === id);
    activeItem = item;
    const origin = document.getElementById('resourceOrigin');
    if (origin) origin.textContent = live ? (api.context.task_id ? '已连接真实资源库 · 当前任务可见资源' : '已连接真实资源库 · 当前租户可见资源') : '城市燃气管网转换 · 静态样例，仅对应当前城市任务';
    const filters = state[kind];
    if (item && item.scope === 'task' && item.taskId) { filters.scope = 'task'; selectedTask = item.taskId; }
    const back = document.getElementById('resourceBack');
    back.hidden = !item; back.dataset.back = kind;
    back.textContent = `← 返回${kind === 'family' ? '产品族' : '几何'}`;
    document.getElementById('resourceList').hidden = Boolean(item);
    detail.hidden = !item;
    if (item) {
      const versions = itemVersions(item);
      const selected = versions.find((entry) => (entry.ref && (entry.ref === item.selectedRef || entry.ref === version)) || String(entry.number) === version) || versions[0];
      const model = item.remote ? item.model : selected?.model || (!selected?.noPreview && (!version || selected === versions[0]) ? item.model : null);
      const modelCaption = item.displaySource?.source_kind === 'geometry_inspection'
        ? `已保存的默认参数案例预览：${item.displaySource.title || item.displaySource.case_key || '默认参数案例'}。` : model?.caption;
      const dependencies = (item.bindings || []).flatMap((binding) => {
        const target = resources.find((entry) => entry.kind === 'geometry' && entry.id === binding.id);
        return target ? [bindingButton(target, [binding.role, binding.version != null ? `版本 ${binding.version}` : ''].filter(Boolean).join(' · '), binding.version)] : item.remote ? [bindingButton({ kind: 'geometry', id: binding.id, name: binding.name || binding.role || '依赖几何' })] : [];
      });
      const consumers = (item.usedBy || []).flatMap((id) => {
        const target = resources.find((entry) => entry.id === id);
        return target ? [bindingButton(target, target.kind === 'family' ? '产品族' : '几何')] : [];
      });
      detail.innerHTML = `<header class="resource-detail-heading"><div>${tags(item)}<h2 tabindex="-1">${esc(item.name)}</h2>${item.purpose ? `<p>${esc(item.purpose)}</p>` : ''}</div><div class="resource-version-tools">${versions.length > 1 ? `<label class="version-picker">版本<select id="resourceVersion">${versions.map((entry) => `<option value="${esc(entry.ref || entry.number)}" ${entry === selected ? 'selected' : ''}>${esc(versionLabel(entry))}</option>`).join('')}</select></label>` : `<span class="resource-version-label">${esc(versionLabel(selected))}</span>`}${item.remote && !item.versionsComplete ? (item.beforeVersion ? '<button type="button" data-older-versions>更早版本</button>' : '<small>仅列出已载入的近期版本</small>') : ''}${deleteControl(item)}</div></header>
        <div class="resource-detail-grid"><div class="resource-preview"><div class="resource-model-fallback">${preview(item, selected)}</div><div class="resource-model-canvas"></div><div class="resource-model-toolbar"><span class="resource-model-status" role="status">${model ? '正在载入三维模型…' : esc(item.image && !selected?.noPreview ? '模型图片' : item.displayMessage || '暂无模型预览')}</span><button type="button" data-view-reset hidden>复位视角</button></div>${modelCaption ? `<p class="resource-model-caption">${esc(modelCaption)}</p>` : ''}</div>${item.structure?.length ? `<section class="resource-info"><h3>${kind === 'family' ? '产品结构' : '几何内容'}</h3><ul class="resource-structure">${item.structure.map((part) => `<li>${esc(part)}</li>`).join('')}</ul></section>` : ''}</div>
        ${item.warnings?.length ? `<p class="resource-display-warning">${item.warnings.map((warning) => esc(warning.message || warning.code)).join('；')}</p>` : ''}
        <div class="resource-detail-sections">${item.usage ? section('使用说明', `<p>${esc(item.usage)}</p>`) : ''}${section('参数', item.parameters?.length ? parameterTable(item) : `<p>${item.kind === 'geometry' && item.subtype === 'mapped_model' ? '固定几何没有可配置的函数参数。' : '此版本未保存参数声明。'}</p>`)}${section('接口', item.interfaces?.length ? table(item.interfaces, item.interfaceHeadings || ['接口', '所属产品', '类型']) + (item.interfaceRecords ? `<details class="resource-contract-details"><summary>查看保存的接口定义</summary><pre>${esc(JSON.stringify(item.interfaceRecords, null, 2))}</pre></details>` : '') : '<p>此版本未保存接口声明。</p>')}${item.savedSampleInterfaces?.length ? section('当前样例的输出接口', `<p>来自已保存的样例输出，坐标与断面仅对应当前样例。</p>${table(item.savedSampleInterfaces.map((entry) => [entry.interface_key, JSON.stringify(entry.local_frame.translation_m), JSON.stringify(entry.profile)]), ['接口', '局部位置 · m', '断面'])}<details class="resource-contract-details"><summary>查看样例接口定义</summary><pre>${esc(JSON.stringify(item.savedSampleInterfaces, null, 2))}</pre></details>`) : ''}${section('依赖几何', dependencies.length ? `<div class="binding-list">${dependencies.join('')}</div>` : '')}${section('使用此几何的资源', consumers.length ? `<div class="binding-list">${consumers.join('')}</div>` : '')}${sourceSection(item)}</div>`;
      document.getElementById('resourceVersion')?.addEventListener('change', (event) => navigate(kind, id, event.target.value));
      if (item.displayPending && !model) detail.querySelector('.resource-model-status').textContent = displayMessage(item.displayPhase);
      if (item.remote && item.displaySource && api.context.task_id && api.context.run_id && !item.model && !item.displayPending && !item.modelError) {
        item.displayPending = true;
        void api.display(item.displaySource, { onPhase(phase) {
          item.displayPhase = phase;
          if (activeItem !== item) return;
          detail.querySelector('.resource-model-fallback').innerHTML = preview(item, selected);
          detail.querySelector('.resource-model-status').textContent = displayMessage(phase);
        } }).then((display) => {
          Object.assign(item, display);
          thumbnails.set(`${item.kind}:${item.selectedRef}`, Promise.resolve({ image: display.image }));
          if (activeItem === item) void render();
        }).catch((error) => {
          item.modelError = error.message;
          if (activeItem === item) {
            detail.querySelector('.resource-model-fallback').innerHTML = preview(item, selected);
            detail.querySelector('.resource-model-status').textContent = error.message;
            status(error.message, true);
          }
        }).finally(() => { item.displayPending = false; });
      } else if (item.remote && item.modelError) status(item.modelError, true);
      if (model?.url) {
        import('./library-viewer.js?v=20260912-flat-viewer').then(({ mountViewer }) => {
          if (token !== renderToken) return;
          disposeViewer = mountViewer(detail.querySelector('.resource-preview'), model, item.name);
        }).catch(() => {
          if (token === renderToken) detail.querySelector('.resource-model-status').textContent = item.image ? '三维视图暂不可用，显示模型图片' : '三维视图暂不可用';
        });
      }
      if (changed) { scroll.scrollTop = detailScroll.get(key(current)) || 0; detail.querySelector('h2').focus({ preventScroll: true }); }
      return;
    }
    detail.replaceChildren();
    document.getElementById('resourceKindTitle').textContent = kind === 'family' ? '产品族' : '几何';
    document.getElementById('resourceKindDescription').textContent = kind === 'family' ? '查看产品组成、参数、接口及其几何依赖。' : '查看产品族使用的形体、建模参数与接口。';
    document.getElementById('resourceSearch').value = filters.search;
    document.querySelectorAll('[data-scope]').forEach((button) => {
      button.classList.toggle('is-active', button.dataset.scope === filters.scope);
      button.setAttribute('aria-pressed', String(button.dataset.scope === filters.scope));
    });
    const taskLabel = document.getElementById('resourceTaskLabel');
    const taskSelect = document.getElementById('resourceTask');
    if (taskLabel && taskSelect) {
      taskLabel.hidden = filters.scope !== 'task' || tasks.length === 0;
      taskSelect.innerHTML = tasks.map((task) => `<option value="${esc(task.id)}"${task.id === selectedTask ? ' selected' : ''}>${esc(task.name)}</option>`).join('');
    }
    const visible = resources.filter((entry) => entry.kind === kind
      && (filters.scope === 'all' || (entry.scope === filters.scope && (filters.scope !== 'task' || entry.taskId === selectedTask)))
      && `${entry.name} ${entry.purpose || ''} ${entry.subtype || ''} ${entry.taskName || ''}`.toLowerCase().includes(filters.search.toLowerCase()));
    document.getElementById('resourceGrid').innerHTML = visible.map((entry) => `<button type="button" class="resource-card" data-resource="${esc(entry.id)}" data-target-kind="${kind}"><div class="resource-card-preview">${preview(entry, itemVersions(entry)[0])}</div><div class="resource-card-copy">${tags(entry)}<h3>${esc(entry.name)}</h3>${entry.purpose ? `<p>${esc(entry.purpose)}</p>` : ''}<footer><span>${esc(versionLabel(itemVersions(entry)[0]))}</span><span>查看详情 →</span></footer></div></button>`).join('');
    loadThumbnails(visible, token);
    document.getElementById('resourceEmpty').hidden = visible.length > 0;
    const more = document.getElementById('resourceMore');
    if (more) more.hidden = !live || !pages[kind].cursor;
    if (changed) scroll.scrollTop = filters.scroll;
  }
  document.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.kind) navigate(button.dataset.kind);
    if (button.dataset.scope) { state[current.kind].scope = button.dataset.scope; render(); }
    if (button.dataset.resource) navigate(button.dataset.targetKind, button.dataset.resource, button.dataset.version);
    if (button.dataset.back) navigate(button.dataset.back);
    if (button.id === 'resourceRetry') { pages[current.kind].loaded = false; details.delete(key(current)); void render(); }
    if (button.id === 'resourceMore') {
      button.disabled = true;
      void loadList(current.kind, true).then(render).catch((error) => status(error.message, true)).finally(() => { button.disabled = false; });
    }
    if ('olderVersions' in button.dataset) {
      const item = activeItem, token = renderToken;
      button.disabled = true;
      void api.info(current.kind, item.id, item.beforeVersion).then((response) => {
        if (token !== renderToken) return;
        const older = window.CAD2BIM_LIBRARY_API.details(current.kind, response);
        for (const version of older.versions) if (!item.versions.some((entry) => entry.ref === version.ref)) item.versions.push(version);
        item.versionsComplete = older.versionsComplete; item.beforeVersion = older.beforeVersion;
        void render();
      }).catch((error) => { if (token === renderToken) status(error.message, true); }).finally(() => { button.disabled = false; });
    }
    if ('sourceClose' in button.dataset) sourceDialog.close();
    if ('deleteResource' in button.dataset && live && activeItem?.scope !== 'system' && activeItem) {
      deleteItem = activeItem; deleteTrigger = button;
      const label = deleteItem.kind === 'family' ? '产品族' : '几何';
      document.getElementById('resourceDeleteTitle').textContent = `删除这个${label}？`;
      document.getElementById('resourceDeleteName').textContent = deleteItem.name;
      document.getElementById('resourceDeleteDescription').textContent = `删除后，该${label}及其全部版本将从资源库移除，无法再用于新的${deleteItem.kind === 'family' ? '构件' : '产品族'}。`;
      document.getElementById('resourceDeleteImpact').textContent = deleteItem.kind === 'family' ? '绑定的几何仍会保留。' : '如果任一产品族版本正在使用此几何，删除会被阻止；请先删除使用它的产品族。';
      document.getElementById('resourceDeleteConfirm').textContent = `删除${label}`;
      document.getElementById('resourceDeleteError').hidden = true;
      if (!deleteDialog.open) deleteDialog.showModal();
      deleteDialog.querySelector('.task-delete-cancel').focus();
    }
    if ('deleteCancel' in button.dataset && !deleting) deleteDialog.close();
    if ('deleteConfirm' in button.dataset) void confirmDelete();
    if ('readCode' in button.dataset && activeItem?.kind === 'geometry' && activeItem.code) {
      sourceItem = activeItem; sourceTrigger = button;
      document.getElementById('resourceSourceTitle').textContent = `${sourceItem.name} · 版本 ${sourceItem.selectedNumber}`;
      const members = (sourceItem.code.members || []).filter((member) => member.text_available);
      const select = document.getElementById('resourceSourcePath');
      select.innerHTML = members.map((member) => `<option value="${esc(member.source_path)}">${esc(member.source_path)}</option>`).join('');
      select.value = members[0]?.source_path || '';
      document.getElementById('resourceSourceFileLabel').hidden = members.length <= 1;
      document.getElementById('resourceSourceSingleFile').textContent = members.length === 1 ? members[0].source_path : members.length ? '' : '保存的源码';
      if (!sourceDialog.open) sourceDialog.showModal();
      void readCode(1);
    }
    if (button.dataset.sourcePage) {
      const text = document.getElementById('resourceSourceText');
      const start = button.dataset.sourcePage === 'next' ? Number(text.dataset.endLine) + 1
        : button.dataset.sourcePage === 'previous' ? Math.max(1, Number(text.dataset.startLine) - 200) : 1;
      void readCode(start);
    }
  });
  async function confirmDelete() {
    if (!deleteItem || deleting) return;
    const item = deleteItem;
    const failure = document.getElementById('resourceDeleteError');
    const confirm = document.getElementById('resourceDeleteConfirm');
    deleting = true; failure.hidden = true;
    deleteDialog.setAttribute('aria-busy', 'true');
    deleteDialog.querySelectorAll('button').forEach((button) => { button.disabled = true; });
    confirm.textContent = '正在删除…';
    try {
      await api.remove(item.kind, item.id);
      details.clear(); detailScroll.clear(); thumbnails.clear();
      for (const kind of ['family', 'geometry']) pages[kind] = { loaded: false, cursor: null };
      for (let index = resources.length - 1; index >= 0; index -= 1) {
        if (resources[index].kind === item.kind && resources[index].id === item.id) resources.splice(index, 1);
      }
      deleteDialog.close();
      listNotice = `${item.kind === 'family' ? '产品族' : '几何'}「${item.name}」已删除。`;
      navigate(item.kind);
    } catch (error) {
      failure.textContent = error.message || '删除未完成，请重试。'; failure.hidden = false;
    } finally {
      deleting = false;
      deleteDialog.setAttribute('aria-busy', 'false');
      deleteDialog.querySelectorAll('button').forEach((button) => { button.disabled = false; });
      confirm.textContent = `删除${item.kind === 'family' ? '产品族' : '几何'}`;
    }
  }
  deleteDialog?.addEventListener('cancel', (event) => { if (deleting) event.preventDefault(); });
  deleteDialog?.addEventListener('close', () => {
    deleteItem = null;
    if (deleteTrigger?.isConnected) deleteTrigger.focus();
    deleteTrigger = null;
  });
  deleteDialog?.addEventListener('click', (event) => {
    if (event.target !== deleteDialog || deleting) return;
    const box = deleteDialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) deleteDialog.close();
  });
  sourceDialog?.addEventListener('close', () => {
    ++codeToken;
    const item = sourceItem;
    sourceItem = null;
    if (sourceTrigger?.isConnected) sourceTrigger.focus();
    else if (activeItem === item) detail.querySelector('[data-read-code]')?.focus();
    sourceTrigger = null;
  });
  sourceDialog?.addEventListener('click', (event) => {
    if (event.target !== sourceDialog) return;
    const box = sourceDialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) sourceDialog.close();
  });
  document.getElementById('resourceSourcePath')?.addEventListener('change', () => { if (sourceDialog.open) void readCode(1); });
  async function readCode(startLine) {
    if (!sourceItem || !sourceDialog.open) return;
    const token = ++codeToken, item = sourceItem;
    const label = document.getElementById('resourceSourceStatus');
    const text = document.getElementById('resourceSourceText');
    const pagination = document.getElementById('resourceSourcePages');
    label.textContent = '正在读取保存的源码…'; text.hidden = true; pagination.hidden = true;
    try {
      const result = await api.code(item.code, document.getElementById('resourceSourcePath')?.value, startLine);
      if (token !== codeToken || !sourceDialog.open) return;
      const view = result.view;
      text.innerHTML = view.lines.map((line) => `<div class="resource-source-line"><span class="resource-source-number" aria-hidden="true">${esc(line.line_number)}</span><code>${esc(line.text.replace(/\r?\n$/, '').replace(/\r$/, ''))}</code></div>`).join('');
      text.dataset.startLine = String(view.start_line || 1); text.dataset.endLine = String(view.end_line || view.total_lines);
      text.hidden = false; label.textContent = '只读';
      pagination.hidden = false;
      document.getElementById('resourceSourceRange').textContent = `第 ${text.dataset.startLine}–${text.dataset.endLine} 行，共 ${view.total_lines} 行`;
      pagination.querySelector('[data-source-page="previous"]').disabled = Number(text.dataset.startLine) <= 1;
      pagination.querySelector('[data-source-page="next"]').disabled = Number(text.dataset.endLine) >= view.total_lines;
    } catch (error) { if (token === codeToken && sourceDialog.open) label.textContent = error.message; }
  }
  document.getElementById('resourceSearch').addEventListener('input', (event) => { state[current.kind].search = event.target.value; render(); });
  document.getElementById('resourceTask')?.addEventListener('change', (event) => { selectedTask = event.target.value; render(); });
  routeWindow.addEventListener('hashchange', render);
  window.addEventListener('pagehide', () => { ++renderToken; disposeViewer?.(); });
  render();
})();
