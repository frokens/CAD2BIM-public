(() => {
  const fixture = window.GOLDEN_RUN_FIXTURE;
  if (!fixture) throw new Error("CAD2BIM conversation is not available");

  const esc = (value = "") => String(value)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  // Cards (Role Card and Stage Card) are written in Markdown: render GFM (bold, italic, lists,
  // tables, line breaks) with the same renderer policy as the compiled process documents — raw
  // HTML is escaped, links are limited to safe targets and images fall back to their alt text.
  const cardMarkdown = (() => {
    const api = (typeof globalThis !== "undefined" ? globalThis.marked : null) ?? window.marked;
    if (!api?.Marked) return null;
    return new api.Marked({
      gfm: true,
      breaks: true,
      renderer: {
        html({ text }) { return esc(text); },
        link({ href, tokens }) {
          const label = this.parser.parseInline(tokens);
          if (!/^(https?:\/\/|#|\.\/content\/)/.test(href)) return label;
          return `<a href="${esc(href)}"${href.startsWith("http") ? ' target="_blank" rel="noopener noreferrer"' : ""}>${label}</a>`;
        },
        image({ text }) { return esc(text); },
      },
    });
  })();
  // Chinese prose writes `**强调。**下一句` without a following space, which CommonMark's
  // right-flanking rule rejects. Protect the explicit `**…**` pairs before parsing and restore
  // them as <strong> afterwards, so only deliberate bold markers are affected (single `*`
  // multiplication stays untouched).
  const STRONG_OPEN = "@@CARD_STRONG_OPEN@@";
  const STRONG_CLOSE = "@@CARD_STRONG_CLOSE@@";
  const protectStrong = (source) => String(source)
    .replace(/\*\*([^*\n]+)\*\*/g, (match, inner) => `${STRONG_OPEN}${inner}${STRONG_CLOSE}`)
    // A remaining single `*` between two word characters is arithmetic (OD*3, 4*0.45), not
    // emphasis: escape it so Markdown keeps it literal.
    .replace(/(?<=[\w\u4e00-\u9fff])\*(?=[\w\u4e00-\u9fff])/g, "\\*");
  const restoreStrong = (html) => String(html).replaceAll(STRONG_OPEN, "<strong>").replaceAll(STRONG_CLOSE, "</strong>");
  const cardText = (value = "") => {
    const source = String(value ?? "");
    if (!cardMarkdown) return esc(source).replace(/\n/g, "<br>");
    return restoreStrong(cardMarkdown.parse(protectStrong(source)));
  };
  const cardTextInline = (value = "") => {
    const source = String(value ?? "");
    if (!cardMarkdown) return esc(source).replace(/\n/g, "<br>");
    return restoreStrong(cardMarkdown.parseInline(protectStrong(source)));
  };
  // Rendering happens once for the finished card; replay streams the same text by re-rendering
  // the accumulated Markdown on every frame (the parser is incremental-friendly: unterminated
  // constructs simply stay literal until their closing characters arrive).
  const markdownSources = new Map();
  let markdownSequence = 0;
  const markdownBlock = (value) => {
    const id = `md-${++markdownSequence}`;
    markdownSources.set(id, String(value ?? ""));
    return `<div class="role-card-markdown" data-md-source="${id}">${cardText(value)}</div>`;
  };
  const slug = (value = "") => String(value).replace(/[^a-zA-Z0-9_-]+/g, "-").replace(/^-|-$/g, "").toLowerCase();
  const list = (value) => Array.isArray(value) ? value : value == null ? [] : [value];
  const unique = (values) => [...new Set(values.filter(Boolean))];
  const visibleStatus = (value) => value === "已完成" ? "" : value ?? "";
  const displayFileTitle = (value = "") => String(value).replace(/\.(md|json)$/i, "");
  const roleIconSvg = (key = "default") => {
    const paths = {
      requirements: '<path d="M7 3.5h7l3 3v14H7z"/><path d="M14 3.5v4h4M10 12h5M10 16h3"/>',
      cad: '<path d="M4 18V6m0 12h15"/><path d="m7 15 3-5 4 2 4-6"/><circle cx="10" cy="10" r="1"/><circle cx="14" cy="12" r="1"/>',
      plan: '<circle cx="5" cy="6" r="2"/><circle cx="19" cy="6" r="2"/><circle cx="12" cy="18" r="2"/><path d="M7 6h10M6.5 7.5l4.3 8.6M17.5 7.5l-4.3 8.6"/>',
      stage: '<path d="M5 20V4m0 2h9l-2 3 2 3H5"/><path d="M9 16h9"/>',
      model: '<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9"/>',
      review: '<circle cx="11" cy="11" r="7"/><path d="m8 11 2 2 4-5M16.5 16.5 21 21"/>',
      component: '<path d="m12 3 4 2.3v4.6l4 2.3v4.6L16 19l-4-2.3L8 19l-4-2.2v-4.6l4-2.3V5.3z"/><path d="M8 9.9 12 12l4-2.1M12 12v4.7"/>',
      visual: '<path d="M3 12s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6Z"/><circle cx="12" cy="12" r="2.5"/>',
      general: '<path d="M5 5h14M5 12h14M5 19h14"/><circle cx="9" cy="5" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="10" cy="19" r="2"/>',
      summary: '<path d="M6 4h12v16H6z"/><path d="m9 9 1.5 1.5L14 7m-5 8h6"/>',
      default: '<circle cx="12" cy="12" r="4"/>',
    };
    return `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths[key] ?? paths.default}</svg>`;
    document.getElementById("inspectorClose").setAttribute("aria-label", "关闭右侧栏");
  };
  const renderRoleIcon = (actor) => `<span class="agent-icon ${actor.tone === "general" ? "is-general" : ""}" title="${esc(actor.name)}">${roleIconSvg(actor.icon)}</span>`;
  const phaseStrip = document.getElementById("phaseStrip");
  // Replay playback default (the demo's 1× pacing was too slow); user selectable at runtime.
  const REPLAY_SPEED_DEFAULT = 16;
  const completedView = document.getElementById("conversationStart");
  const inspectorContent = document.getElementById("inspectorContent");
  const documents = fixture.documents ?? {};
  const eventTargets = new Map();
  const roleRuns = [];
  const savedCards = [];
  const operations = [];
  const detailEntries = new Map();
  const stateUpdates = new Map();
  const replayEvents = new Map();
  const replayGroups = new Map();
  const replayRuns = new Map();
  const publishedDocuments = new Map();
  const visiblePackageAliases = new Set();
  let replayActive = false;
  let replayFollow = true;
  const sectionIndex = new Map();
  let eventSequence = 0;
  let roleSequence = 0;
  let cardSequence = 0;

  const clock = () => "";

  const contextForRun = (ctx, events) => {
    if (ctx.id === "requirement") return null;
    const seed = events.find((event) => event.contextData);
    const selected = seed?.contextData?.selected ?? (events[0]?.context ? ctx.workContext?.selected : []);
    return selected?.length ? { selected } : null;
  };

  const registerTarget = (ctxId, title, id) => { if (title) eventTargets.set(`${ctxId}:${title}`, id); };
  const nextEventMeta = (ctx, event, index) => {
    eventSequence += 1;
    const id = `event-${slug(ctx.id)}-${index + 1}-${eventSequence}`;
    if (event.stateUpdate) stateUpdates.set(id, { state: event.stateUpdate, actor: event.actor?.name ?? ctx.title });
    replayEvents.set(id, event);
    registerTarget(ctx.id, event.title, id);
    return { id, time: clock(eventSequence) };
  };

  const registerDetail = (id, title, kind, body) => {
    detailEntries.set(id, { title, kind, body });
    return id;
  };
  const processDocument = (id) => window.PROCESS_CONTENT?.documents[window.PROCESS_CONTENT?.aliases[id] ?? id];
  const processDocumentBody = (doc) => `<a class="inline-link" href="${esc(doc.path)}" download>下载 Markdown</a><article class="process-markdown">${doc.html}</article>`;
  const renderResourceButton = (resource, id) => {
    const doc = processDocument(resource.id ?? resource.title);
    const body = Object.hasOwn(resource, "content") ? `<p class="detail-note">${esc(resource.source ?? "")}</p>${typeof resource.content === "string" ? `<pre class="markdown-source">${esc(resource.content)}</pre>` : resource.kind === "Stage Card" && resource.content && typeof resource.content === "object" ? renderStageCardDoc(resource.content) : resource.kind === "Role Card" && resource.content && typeof resource.content === "object" ? renderRoleCard(resource.content) : `<pre class="tool-json"><code>${esc(JSON.stringify(resource.content, null, 2))}</code></pre>`}` : doc ? processDocumentBody(doc) : `<p class="detail-note">本次资料读取记录；当前只读取了相关片段。</p>${resource.summary ? `<p>${esc(resource.summary)}</p>` : ""}${resource.parts?.length ? `<h3>读取范围</h3><ul>${resource.parts.map((part) => `<li>${esc(part)}</li>`).join("")}</ul>` : ""}`;
    registerDetail(id, resource.title, resource.kind ?? "资料", body);
    return `<button class="activity-item resource-load trace-target" id="${esc(id)}" type="button" data-action="open-detail-panel" data-detail="${esc(id)}"><span class="activity-mark" aria-hidden="true">▤</span><span class="activity-name">${esc(resource.title)}</span><span class="activity-status">${esc(resource.kind ?? "资料")} ↗</span></button>`;
  };
  const renderEntryResources = (resources = []) => resources.length ? `<details class="entry-injection"><summary><span class="activity-chevron" aria-hidden="true">›</span>进入时载入的资料</summary><div class="activity-list">${resources.map((resource) => {
    const id = `entry-resource-${detailEntries.size + 1}`;
    const body = resource.id === "ifc-task-tree-entry" && resource.content == null ? '<p class="detail-note">首次进入，当前完整任务树尚未形成。</p>' : typeof resource.content === "string"
      ? `<pre class="markdown-source">${esc(resource.content)}</pre>`
      : resource.kind === "Role Card" ? renderRoleCard(resource.content)
      : resource.kind === "Stage Card" ? renderStageCardDoc(resource.content)
      : `<pre class="tool-json"><code>${esc(JSON.stringify(resource.content, null, 2))}</code></pre>`;
    registerDetail(id, resource.title, resource.kind, `<p class="detail-note">来源：${esc(resource.source)}</p>${body}`);
    return `<button class="activity-item resource-load" type="button" data-replay-entry-resource="true" data-action="open-detail-panel" data-detail="${id}"><span class="activity-mark" aria-hidden="true">▤</span><span class="activity-name">${esc(resource.title)}<small>${esc(resource.source)}</small></span><span class="activity-status">${esc(resource.kind)} ↗</span></button>`;
  }).join("")}</div></details>` : "";
  const renderActivityGroup = (items, toolCount = 0, resourceCount = 0, status = "", updateId = "", replayId = "") => {
    const label = [resourceCount ? `读取 ${resourceCount} 份资料` : "", toolCount ? `调用 ${toolCount} 个工具` : ""].filter(Boolean).join(" · ");
    return `<details class="activity-group replay-item"${replayId ? ` data-replay-group="${replayId}"` : ""}${updateId ? ` data-state-update="${esc(updateId)}"` : ""}><summary><span class="activity-summary-row"><span class="activity-chevron" aria-hidden="true">›</span><span class="activity-label">${label || "处理步骤"}</span><small>${esc(status)}</small></span></summary><div class="activity-list">${items.join("")}</div></details>`;
  };
  const renderContext = (data) => {
    const selected = list(data?.selected);
    if (!selected.length) return "";
    return renderActivityGroup(selected.map((title) => renderResourceButton({ title, kind: "Context" }, `context-${detailEntries.size + 1}`)), 0, selected.length);
  };
  const renderResourceLoad = (event, meta) => renderResourceButton(event.resource, meta.id);
  const finalRequirement = fixture.sections.find((section) => section.id === "requirement")?.events.findLast((event) => event.stateUpdate?.kind === "requirement")?.stateUpdate.content;
  const requirementMarkdown = finalRequirement ? window.renderRequirementMarkdown(finalRequirement) : "";
  let displayedRequirement = finalRequirement;
  let requirementComplete = true;
  let displayedClarifications = [];
  const requirementCheckpoints = new Map();
  const renderRequirementRows = (content) => `<div class="requirement-row-head" aria-hidden="true"><span>目的</span><span>对应的 IFC 结构</span><span>建模方式</span></div><div class="requirement-rows">${content.requirements.map((row) => `<details class="requirement-row"><summary><strong>${esc(row.purpose)}</strong><span class="requirement-preview">${esc(row.ifc_structure)}</span><span class="requirement-preview">${esc(row.modeling_method)}</span><span class="requirement-expand" aria-hidden="true">＋</span></summary><dl class="detail-fields"><div><dt>对应的 IFC 结构</dt><dd>${esc(row.ifc_structure)}</dd></div><div><dt>建模方式</dt><dd>${esc(row.modeling_method)}</dd></div>${row.tools?.length ? `<div><dt>工具</dt><dd class="requirement-tools">${row.tools.map((name) => `<code>${esc(name)}</code>`).join("")}</dd></div>` : ""}</dl></details>`).join("")}</div>`;
  const renderClarifications = (questions, round) => questions == null ? `<p class="detail-note">本轮问题尚未完成评估。</p>` : questions.length ? `<details class="requirement-clarifications" aria-label="待确认问题" open><summary class="clarification-group-heading"><span><strong>需要你确认</strong><small>${round ? `第 ${round} 轮 · ` : ""}${questions.length} 项待确认</small></span><span class="disclosure-chevron" aria-hidden="true">⌄</span></summary><div class="clarification-group-body"><div class="clarification-items">${questions.map((item, index) => `<details class="clarification-item" data-question="${esc(item.id)}"><summary><span class="clarification-index" aria-hidden="true">${String(index + 1).padStart(2, '0')}</span><span class="clarification-preview"><strong>${esc(item.topic)}</strong></span><span class="disclosure-chevron" aria-hidden="true">⌄</span></summary><div class="clarification-content"><div class="clarification-section"><h4>情况与问题</h4><p>${esc(item.question)}</p></div><div class="clarification-default"><h4>默认检验方案</h4><p>${esc(item.defaultAnswer)}</p></div>${item.notes ? `<div class="clarification-section"><h4>说明与补充</h4><p>${esc(item.notes)}</p></div>` : ""}</div></details>`).join('')}</div></div></details>` : "";
  const renderLiveRequirement = (content) => content ? `<p class="requirement-live-status">${content.requirements.length} 条要求 · ${displayedClarifications == null ? "问题待评估" : displayedClarifications.length ? `${displayedClarifications.length} 项待确认` : requirementComplete ? "已整理" : "提取中"}</p>${renderRequirementRows(content)}${renderClarifications(displayedClarifications)}` : `<p class="detail-note">尚未提取需求。</p>`;
  const finalCad = fixture.sections.find((section) => section.id === "cad")?.events.findLast((event) => event.stateUpdate?.kind === "cad")?.stateUpdate.content;
  let displayedCad = finalCad;
  let cadComplete = true;
  const finalTaskTree = fixture.stages.findLast((stage) => stage.finalState)?.finalState ?? fixture.sections.find((section) => section.id === "planning")?.events.findLast((event) => event.stateUpdate?.kind === "task_tree")?.stateUpdate;
  let displayedTaskTree = finalTaskTree?.ifc_task_tree ?? null;
  const acceptedStageIds = new Set(fixture.stages.filter((stage) => stage.stageCard).map((stage) => stage.id));
  let displayedPlanningClarifications = finalTaskTree?.clarifications ?? null;
  let displayedStageDependencies = finalTaskTree?.stage_dependencies ?? null;
  const planningCheckpoints = new Map();
  const stageCheckpoints = new Map();
  let taskTreeComplete = true;
  let displayedCadClarifications = [];
  const cadCheckpoints = new Map();
  const cadDocumentButton = () => replayActive && !publishedDocuments.has("cad-result") ? '' : '<button class="document-chip" type="button" data-action="open-document" data-document="cad-result"><span aria-hidden="true">▤</span> CAD 调查.md <span aria-hidden="true">↗</span></button>';
  const renderCadRows = (content) => '<div class="cad-row-head"><span>输入来源</span><span>数据解释</span><span>数据成果</span></div><div class="cad-rows">' + content.rows.map((row, index) => '<details class="cad-row" data-cad-row="' + index + '"><summary><strong>' + esc(row.sources.map((source) => source.source_ref).join("、") || "本轮整理") + '</strong><span class="cad-preview">' + esc(row.interpretation) + '</span><span class="cad-dataset-preview">' + esc(row.datasets.map((dataset) => dataset.name).join("、") || "尚无已发布成果") + '</span><span class="cad-expand" aria-hidden="true">＋</span></summary><div class="cad-row-content"><dl class="detail-fields"><div><dt>输入来源</dt><dd>' + row.sources.map((source) => '<p><strong>' + esc(source.source_ref) + '</strong> · ' + esc(source.description) + '</p>').join("") + '</dd></div><div><dt>数据解释</dt><dd>' + esc(row.interpretation) + '</dd></div><div><dt>数据成果与用法</dt><dd>' + (row.datasets.length ? row.datasets.map((dataset) => '<section class="cad-dataset"><strong>' + esc(dataset.name) + '</strong><p><code>' + esc(dataset.package_alias) + '</code></p><p class="cad-payloads">' + dataset.payload_names.map((name) => '<code>' + esc(name) + '</code>').join(" ") + '</p><p>' + esc(dataset.usage) + '</p></section>').join("") : '本项尚无已发布的数据成果。') + '</dd></div></dl></div></details>').join("") + '</div>';
  const renderLiveCad = (content) => content ? '<header class="cad-live-header"><span>' + (displayedCadClarifications == null ? "问题待评估" : displayedCadClarifications.length ? displayedCadClarifications.length + " 项待确认" : cadComplete ? "调查结果" : "调查中") + '</span>' + cadDocumentButton() + '</header>' + renderCadRows(content) + renderClarifications(displayedCadClarifications) : '<p class="detail-note">等待 CAD 图纸调查。</p>';
  const renderCadFinal = (event, meta) => {
    const state = event?.cadState;
    const content = state?.content ?? finalCad;
    if (!content) return "";
    const questions = state?.clarifications ?? null;
    const id = state?.business_entry === 2 && Array.isArray(questions) && !questions.length ? "cad-final" : meta.id;
    const detailId = id + "-markdown";
    const markdown = state?.cad_md ?? window.renderCadMarkdown(content);
    const view = window.PROCESS_CONTENT.cadViews.find((entry) => entry.markdown === markdown);
    registerDetail(detailId, "CAD 调查 · 第 " + (event.cadRound ?? state?.business_entry ?? 1) + " 轮.md", "CAD 成果", '<article class="process-markdown">' + (view?.html ?? '<pre class="markdown-source">' + esc(markdown) + '</pre>') + '</article>');
    if (state) cadCheckpoints.set(id, { ...state, roleCard: event.roleCard ?? state.roleCard });
    return '<section class="requirement-final cad-final trace-target replay-item" id="' + id + '"' + (state ? ' data-cad-checkpoint="' + id + '"' : '') + '><header><div><h2>CAD 调查结果</h2><p class="requirement-round-label">第 ' + (event.cadRound ?? 1) + ' 轮 · ' + (questions == null ? '待评估' : questions.length ? '待澄清' : '待确认') + '</p></div><button class="document-chip" type="button" data-action="open-detail-panel" data-detail="' + detailId + '">▤ CAD 调查.md ↗</button></header><details class="result-table"><summary><span>' + content.rows.length + ' 项调查结果</span><span class="result-table-toggle">查看表格</span></summary>' + renderCadRows(content) + '</details>' + renderClarifications(questions, event.cadRound) + renderUserActionHint(event) + '</section>';
  };
  const renderAgentState = (update) => {
    if (!update?.state) return '<p class="detail-note">等待当前 Agent 的状态更新。</p>';
    const { state, actor } = update;
    const content = state.kind === "requirement" ? renderLiveRequirement(state.content)
      : `${state.summary ? `<p class="detail-note">${esc(state.summary)}</p>` : ""}${list(state.groups).map((group) => `<section><h3>${esc(group.title)}</h3><dl class="detail-fields">${list(group.items).map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl></section>`).join("")}`;
    return `<h3 class="live-state-agent">${esc(actor)}</h3>${content}`;
  };
  const renderRequirementFinal = (event, meta) => {
    const state = event.requirementState;
    const content = state?.content ?? finalRequirement;
    const questions = state?.clarifications ?? null;
    const isLast = Array.isArray(questions) && !questions.length && !event.dynamic;
    const id = event.dynamic ? meta.id : isLast ? "requirement-final" : meta.id;
    const detailId = isLast ? "requirement-markdown" : `${id}-markdown`;
    const markdown = state?.requirement_md ?? window.renderRequirementMarkdown(content);
    const view = window.PROCESS_CONTENT.requirementViews.find((entry) => entry.markdown === markdown);
    registerDetail(detailId, `需求提取${event.requirementRound ? ` · 第 ${event.requirementRound} 轮` : ""}.md`, "需求成果", `${isLast ? '<a class="inline-link" href="./requirement.md" download="需求提取.md">下载 Markdown</a>' : ''}<article class="process-markdown">${view?.html ?? `<pre class="markdown-source">${esc(markdown)}</pre>`}</article>`);
    if (state) requirementCheckpoints.set(id, { ...state, roleCard: event.roleCard ?? state.roleCard });
    return `<section class="requirement-final trace-target replay-item" id="${id}"${state ? ` data-requirement-checkpoint="${id}"` : ""}><header><div><h2>需求提取结果</h2>${event.requirementRound ? `<p class="requirement-round-label">第 ${event.requirementRound} 轮 · ${questions == null ? "待评估" : questions.length ? "待澄清" : "待确认"}</p>` : ''}</div><button class="document-chip" type="button" data-action="open-detail-panel" data-detail="${detailId}"><span aria-hidden="true">▤</span> 需求提取.md <span aria-hidden="true">↗</span></button></header><details class="result-table"><summary><span>${content.requirements.length} 条建模要求</span><span class="result-table-toggle">查看表格</span></summary>${renderRequirementRows(content)}</details>${renderClarifications(questions, event.requirementRound)}${renderUserActionHint(event)}</section>`;
  };

  const renderPreview = (preview, toolId) => preview ? (preview.items ?? [preview]).map((preview) => `<button class="tool-preview"${toolId ? ` data-preview-for="${esc(toolId)}"` : ""}${preview.mode === "image" ? ' data-preview-mode="image"' : ""} type="button" data-action="open-detail" data-title="${esc(preview.title)}" data-src="${esc(preview.image)}"><img src="${esc(preview.image)}" alt="${esc(preview.title)}" loading="lazy"/><span>${esc(preview.title)} ↗</span></button>`).join("") : "";
  const renderModelViewer = ({ url, name, status = "正在载入三维模型…" } = {}) => `<div class="family-delivery-viewer"${url ? ` data-live-model-url="${esc(url)}" data-live-model-name="${esc(name)}"` : ""}><div class="resource-model-fallback" hidden></div><div class="resource-model-canvas"></div><div class="resource-model-toolbar"><span class="resource-model-status" role="status">${esc(status)}</span><button type="button" data-view-reset hidden>复位视角</button></div></div>`;
  const familyDeliveryModel = (resourceId, version) => {
    const resource = list(window.CAD2BIM_RESOURCE_LIBRARY).find((item) => item.id === resourceId);
    if (!resource) return null;
    const versions = list(resource.versions);
    const exact = versions.find((entry) => String(entry.number) === String(version));
    if (exact) return exact.noPreview ? null : exact.model || null;
    return resource.model || (versions[0]?.noPreview ? null : versions[0]?.model) || null;
  };
  const renderFamilyDelivery = (event) => {
    const delivery = event?.familyDelivery;
    if (!delivery) return "";
    const model = familyDeliveryModel(delivery.resource_id, delivery.version);
    const hash = ["family", delivery.resource_id, delivery.version].filter((part) => part != null && part !== "").join("/");
    return `<article class="family-delivery replay-item" data-family-delivery="${esc(delivery.resource_id)}" data-delivery-version="${esc(delivery.version ?? "")}"><header><p class="eyebrow">产品族交付</p><h4>${esc(delivery.name || delivery.resource_id)}${delivery.version != null ? ` · v${esc(delivery.version)}` : ""}</h4></header>${renderModelViewer({ status: model ? "正在载入三维模型…" : "模型资源未导出" })}<footer><button type="button" class="primary-link" data-action="open-library" data-resource="${esc(hash)}">在几何与产品族中查看 ↗</button><span>打开该产品族的参数、接口与依赖几何</span></footer></article>`;
  };
  const renderTool = (event, meta, ctx, roleRunId) => {
    const facts = list(event.result?.facts);
    const operationAlias = facts.find(([key]) => String(key).toLowerCase() === "operation")?.[1] ?? "";
    operations.push({
      id: `operation-${operations.length + 1}`, targetId: meta.id, stageId: ctx.id,
      stageTitle: ctx.shortTitle ?? ctx.navLabel ?? ctx.title, role: event.actor?.name ?? "任务处理",
      title: event.technicalName ?? event.title, isTool: Boolean(event.technicalName), effect: event.effect ?? "READ",
      persistence: event.effect === "WRITE" ? "操作" : "查询记录",
      status: event.status ?? "已完成", time: meta.time, roleRunId, operationAlias,
    });
    const parameters = list(event.parameters);
    const technical = event.technicalName ?? event.title;
    const changesData = event.effect === "WRITE";
    const effectDescription = changesData ? "会创建或修改数据" : "只读查询";
    const writeIcon = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="9" cy="5.5" rx="5.5" ry="2.5"/><path d="M3.5 5.5v11c0 1.4 2.5 2.5 5.5 2.5M3.5 11c0 1.4 2.5 2.5 5.5 2.5M14.5 5.5v3M13 16l6-6 2 2-6 6-3 1z"/></svg>`;
    const json = (value) => `<pre class="tool-json"><code>${esc(JSON.stringify(value, null, 2))}</code></pre>`;
    const batchSummary = () => event.batchExecution ? `<section><h5>完整批量建模</h5><p>${esc(event.batchExecution.input_package.package_alias)}/${esc(event.batchExecution.input_package.payload_name)} 共 ${event.batchExecution.input_package.row_count.toLocaleString()} 行；${event.batchExecution.batch_count} 批全部完成，已提交 ${event.batchExecution.committed_item_count.toLocaleString()} 项，失败 ${event.batchExecution.failed_item_count} 项。</p><details><summary>全部 ${event.batchExecution.batch_count} 个批次回执</summary>${json(event.batchExecution.batches)}</details></section>` : "";
    const sampleSummary = () => event.sampleDisplay ? `<section><h5>Package 数据预览</h5><p>完整 Package 共 ${event.sampleDisplay.total_rows.toLocaleString()} 行，下面仅展示 ${event.sampleDisplay.sample_rows_count} 行。</p>${json(event.sampleDisplay.sample_rows)}</section>` : "";
    const body = () => event.call
      ? `${batchSummary()}<section><h5>${event.batchExecution ? '第 1 批输入' : '输入'}</h5>${json(event.call.input)}</section><section><h5>${event.batchExecution ? '第 1 批返回' : '返回'}</h5>${event.call.toolError ? `<pre class="tool-json">${esc(event.call.toolError)}</pre>` : json(event.call.output)}</section>${sampleSummary()}`
      : `<section><h5>输入摘要</h5>${event.inputBindings ? `<h6>${technical === "data_query_sql" ? "query.inputs" : "inputs"} · 只读输入绑定</h6>${json(event.inputBindings)}` : ""}${parameters.length ? `<dl>${parameters.map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>` : `<p>使用当前任务范围</p>`}${event.callPattern ? `<p class="tool-pattern">${esc(event.callPattern)}</p>` : ""}</section><section><h5>返回摘要</h5><p class="tool-headline">${esc(event.result?.headline ?? "调用完成")}</p>${facts.length ? `<dl>${facts.map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>` : ""}</section>`;
    const libraryResource = event.libraryResourceId ? window.CAD2BIM_RESOURCE_LIBRARY.find((item) => item.id === event.libraryResourceId) : null;
    registerDetail(meta.id, technical, "MCP 工具", () => `<div class="tool-detail">${body()}${libraryResource ? `<h5>资源合同</h5>${json(libraryResource.savedInfo)}<button class="document-chip" type="button" data-action="open-library" data-resource="${esc(libraryResource.id)}">在几何与产品族中查看 ${esc(libraryResource.name)} ↗</button>` : ""}${renderPreview(event.preview)}</div>`);
    return `<button class="activity-item tool-call trace-target ${changesData ? "tool-mutating" : ""} ${/失败/.test(event.status ?? "") ? "tool-failed" : ""}" id="${meta.id}" type="button" data-action="open-detail-panel" data-detail="${meta.id}" aria-label="${esc(technical)}，${effectDescription}"><span class="activity-mark" aria-hidden="true">${changesData ? writeIcon : event.technicalName ? "◇" : "○"}</span><code class="activity-name">${esc(technical)}</code><span class="activity-status">${esc(event.technicalName ? visibleStatus(event.status) : "待接入")} ↗</span>${changesData ? `<span class="tool-effect-tooltip" aria-hidden="true">${effectDescription}</span>` : ""}</button>${renderPreview(event.preview, meta.id)}`;
  };

  const summaryForRun = (actorName, events, ctx, runNumber) => {
    const card = events.findLast((event) => event.roleCard)?.roleCard;
    if (!card) throw new Error("Missing exit Role Card: " + ctx.id + " / " + actorName);
    return card;
  };
  // Role Cards preview the first paragraph of "本轮工作与结果"; Stage Cards keep their last-paragraph preview.
  // Expanding
  // shows the whole card. A card whose work-and-result is one unbroken block — a long bullet list
  // written without blank lines — would otherwise sit thousands of pixels tall while collapsed, so
  // a block past CARD_PREVIEW_MAX falls back to its last line.
  const CARD_PREVIEW_MAX = 1200;
  const cardPreview = (value, firstParagraph = false) => {
    const paragraphs = String(value ?? "").split(/\n{2,}/).map((part) => part.trim()).filter(Boolean);
    if (firstParagraph) return paragraphs[0] ?? "";
    const last = paragraphs.at(-1) ?? "";
    if (last.length <= CARD_PREVIEW_MAX) return last;
    const lines = last.split("\n").map((line) => line.trim()).filter(Boolean);
    return lines.at(-1) ?? last;
  };
  const renderWorkCard = ({ id, role, preview, body }) => `<details class="role-card role-card-collapsible" data-role-card-id="${esc(id)}"><summary><span class="role-card-head"><strong>本轮工作与交接</strong><small>${esc(role)}</small></span><span class="role-card-preview">${cardTextInline(preview)}</span><i class="disclosure-chevron" aria-hidden="true">⌄</i></summary><div class="role-card-body">${body}</div></details>`;
  const renderRoleCard = (card) => renderWorkCard({ id: card.id, role: `${card.role} · 第 ${card.round} 轮`, preview: cardPreview(card.outcome, true), body: [["本轮接收", card.input], ["本轮工作与结果", card.outcome], ["后续交接", card.handoff]].map(([title, text]) => `<section class="role-card-section"><h4>${title}</h4>${markdownBlock(text)}</section>`).join("") });
  const stageCardSection = (title, text) => text ? `<section class="role-card-section"><h4>${esc(title)}</h4>${markdownBlock(text)}</section>` : "";
  const stageCardList = (title, items) => list(items).length ? `<section class="role-card-section"><h4>${esc(title)}</h4><ul class="stage-card-list">${list(items).map((item) => `<li>${cardTextInline(item)}</li>`).join("")}</ul></section>` : "";
  const stageCardBody = (card) => `<header><h3>${esc(card.title)}</h3><p>阶段摘要 · 用户已确认</p></header>${stageCardSection("阶段依据", card.input)}${stageCardSection("完成与有效结果", card.outcome)}${stageCardList("关键结果与引用", card.results)}${stageCardList("来源与对象变化", card.changes)}${stageCardList("未决与适用边界", card.unresolved)}${stageCardSection("后续使用条件", card.handoff)}`;
  // The stage summary is the summary LLM's own output: it streams while replaying and stays
  // collapsible afterwards. `replayId` is passed only for the in-conversation occurrence; the
  // same body text is reused unchanged for the detail panel and the process-file list.
  const renderStageCardDoc = (card, replayId = "") => card ? `<details class="stage-card-doc llm-output${replayId ? " replay-item trace-target" : ""}"${replayId ? ` id="${esc(replayId)}"` : ""} data-stage-card-id="${esc(card.id)}"><summary><span class="llm-head"><span class="llm-avatar" aria-hidden="true">✧</span><span class="llm-who"><strong>阶段总结 LLM</strong></span><span class="llm-title">${esc(card.title)}</span><span class="llm-state" data-stream-state>已生成</span><i class="disclosure-chevron" aria-hidden="true">⌄</i></span><span class="role-card-preview">${cardTextInline(cardPreview(card.outcome))}</span></summary><div class="llm-body">${stageCardBody(card)}</div></details>` : "";
  const renderUserActionHint = (event) => {
    const hint = window.userActionHint(event);
    return hint ? `<p class="user-action-hint">${esc(hint)}</p>` : "";
  };

  const documentForEntry = (plan) => {
    if (!plan?.documentId) return null;
    const doc = processDocument(plan.documentId);
    const markdown = (plan.actor === "Stage Planner" ? plan.state?.stage_plan_md : null) ?? plan.state?.requirement_md ?? plan.state?.cad_md ?? plan.state?.ifc_plan_md ?? doc?.markdown;
    if (!markdown) return null;
    return {
      id: plan.documentId,
      title: displayFileTitle(doc?.title ?? documents[plan.documentId]?.title ?? (plan.actor === "CAD Agent" ? "CAD 调查" : "需求提取")) + ".md",
      markdown,
      action: plan.documentAction ?? ((plan.runNumber ?? plan.state?.business_entry ?? 1) > 1 ? "edit" : "create"),
    };
  };
  const renderDocumentChange = (plan, detailId) => {
    const record = documentForEntry(plan);
    if (!record) return "";
    const action = record.action === "create" ? "已创建" : "已编辑";
    const view = [...(window.PROCESS_CONTENT.requirementViews ?? []), ...(window.PROCESS_CONTENT.cadViews ?? [])].find((view) => view.markdown === record.markdown);
    const doc = processDocument(record.id);
    registerDetail(detailId, record.title, action + " · 本轮文档", '<article class="process-markdown">' + (view?.html ?? (doc?.markdown === record.markdown ? doc.html : null) ?? '<pre class="markdown-source">' + esc(record.markdown) + '</pre>') + '</article>');
    return `<div class="role-document-change" data-document-action="${record.action}"><span>${action}</span><button type="button" data-action="open-detail-panel" data-detail="${detailId}">▤ ${esc(record.title)} ↗</button></div>`;
  };
  const renderRoleWorkSummary = (actor, summary, stats, statAttributes = "") => `${renderRoleIcon(actor)}<span class="role-run-title"><strong>${esc(actor.name)}</strong><small>${esc(summary)}</small></span><span class="role-run-stats"${statAttributes}>${esc(stats)}</span><i></i>`;
  const renderRoleWork = ({ id, actor, summary, stats, statAttributes = "", context = "", body = "" }) => `<details class="role-run replay-item" id="${esc(id)}"><summary>${renderRoleWorkSummary(actor, summary, stats, statAttributes)}</summary><div class="role-run-body">${context}<div class="role-event-stream">${body}</div></div></details>`;
  const renderRoleRun = (events, ctx, startIndex, runNumber) => {
    const first = events[0];
    const actor = first.actor ?? { name: "任务处理", icon: "·", tone: "default" };
    const runId = `role-run-${++roleSequence}`;
    const contextData = contextForRun(ctx, events, actor.name, runNumber);
    const entryResources = first.entryResources ?? [];
    const toolCount = events.filter((event) => event.kind === "tool").length;
    const decision = events.filter((event) => event.kind === "decision").at(-1);
    const stream = [];
    let activity = [];
    const flushActivity = () => {
      if (!activity.length) return;
      const failed = activity.some(({ event }) => /失败/.test(event.status ?? ""));
      const pending = activity.some(({ event }) => event.kind === "tool" && !event.technicalName);
      const updateId = activity.findLast(({ event }) => event.stateUpdate)?.updateId;
      const replayId = `batch-${replayGroups.size + 1}`;
      replayGroups.set(replayId, { actor: actor.name, items: activity.map(({ event, updateId }) => ({ event, id: updateId })) });
      stream.push(renderActivityGroup(activity.map(({ html }) => html), activity.filter(({ event }) => event.kind === "tool" && event.technicalName).length, activity.filter(({ event }) => event.kind === "resource_load").length, failed ? "含失败调用" : pending ? "待接入" : "", updateId, replayId));
      activity = [];
    };
    events.forEach((event, offset) => {
      const meta = nextEventMeta(ctx, event, startIndex + offset);
      if (["tool", "resource_load", "capability_simulation"].includes(event.kind)) {
        const previous = activity.at(-1)?.event;
        const activityIteration = activity.findLast(({ event: item }) => item.iteration != null)?.event.iteration;
        if (previous && (previous.effect === "WRITE" || event.effect === "WRITE" || (event.iteration != null && activityIteration != null && event.iteration !== activityIteration))) flushActivity();
        activity.push({ event, updateId: meta.id, html: event.kind === "tool" ? renderTool(event, meta, ctx, runId) : renderResourceLoad(event, meta) });
        return;
      }
      flushActivity();
      if (event.kind === "assistant" && event.presentation === "role_card_exit") stream.push(`<span class="role-exit-marker trace-target replay-item" id="${meta.id}" data-role-card-exit="${esc(event.roleCardId)}"${event.stateUpdate ? ` data-state-update="${meta.id}"` : ""}></span>`);
      if (event.kind === "assistant" && event.presentation !== "role_card_exit") stream.push(`${offset > 0 && event.contextData ? renderContext(event.contextData) : ""}<div class="public-commentary trace-target replay-item" id="${meta.id}"${event.stateUpdate ? ` data-state-update="${meta.id}"` : ""}><div>${list(event.text).map((paragraph) => `<p>${esc(paragraph)}</p>`).join("")}</div></div>`);
      if (event.kind === "context_compaction") {
        registerDetail(meta.id, "上下文已压缩", "上下文管理", `<p>${esc(event.summary ?? "已保留本轮目标、已读取资料的引用、当前成果和未决项。")}</p>`);
        stream.push(`<button class="context-compaction trace-target replay-item" id="${meta.id}" type="button" data-action="open-detail-panel" data-detail="${meta.id}"><span aria-hidden="true">⇥</span><span>上下文已压缩</span><span aria-hidden="true">↗</span></button>`);
      }
      if (event.kind === "user") stream.push(`<article class="message user-message inline-user trace-target replay-item" id="${meta.id}"><div class="user-bubble">${esc(event.text)}${event.state ? `<small>${esc(event.state)}</small>` : ""}</div></article>`);
      if (event.kind === "decision") {
        const repair = event.outcome === "需要修订";
        stream.push(`<div class="review-decision ${repair ? "needs-repair" : "is-accepted"} trace-target replay-item" id="${meta.id}"${event.stateUpdate ? ` data-state-update="${meta.id}"` : ""}><span>${repair ? "↺" : "✓"}</span><div><strong>${esc(event.outcome)}</strong><p>${esc(event.text)}</p></div></div>`);
      }
    });
    flushActivity();
    const rendered = stream.join("");
    const cardId = `work-card-${++cardSequence}`;
    const cardSummary = summaryForRun(actor.name, events, ctx, runNumber);
    registerDetail(cardId, `${actor.name} · 第 ${runNumber} 轮工作与交接`, "Role Card", renderRoleCard(cardSummary));
    savedCards.push({ id: cardId, targetId: cardId, kind: "角色工作摘要", stageId: ctx.id, stageTitle: ctx.shortTitle ?? ctx.navLabel ?? ctx.title, role: actor.name, runNumber, time: clock(eventSequence), summary: cardSummary.outcome });
    const state = events.findLast((event) => ["requirement", "cad", "task_tree"].includes(event.stateUpdate?.kind))?.stateUpdate;
    const documentId = ctx.id === "requirement" ? "doc-requirement" : ctx.id === "cad" ? "cad-result" : ctx.id === "planning" ? "doc-ifc-plan" : actor.name === "Stage Planner" ? ctx.documentId : null;
    const documentPlan = { actor: actor.name, state, entryResources, documentId, runNumber, stageId: ctx.isStage ? ctx.id : null, documentAction: runNumber > 1 ? "edit" : "create" };
    replayRuns.set(runId, documentPlan);
    roleRuns.push({ id: runId, stageId: ctx.id, stageTitle: ctx.shortTitle ?? ctx.title, role: actor.name, runNumber, toolCount, targetId: runId });
    const loadCount = entryResources.length + list(contextData?.selected).length + events.filter((event) => event.kind === "resource_load").length;
    const work = renderRoleWork({ id: runId, actor, summary: `${runNumber > 1 ? `第 ${runNumber} 次进入 · ` : ""}${decision?.outcome ?? "本次工作完成"}`, stats: `资料 ${loadCount} · 工具活动 ${toolCount}`, statAttributes: ` data-final-load-count="${loadCount}" data-final-tool-count="${toolCount}" data-replay-load-count="0" data-replay-tool-count="0"`, context: `${renderEntryResources(entryResources)}${renderContext(contextData)}`, body: rendered });
    return `<div class="agent-turn">${work}<article class="role-final trace-target replay-item" id="${cardId}" data-completes-run="${runId}">${renderRoleCard(cardSummary)}${renderPreview(events.findLast((event) => event.workSummary)?.preview)}${renderFamilyDelivery(events.findLast((event) => event.familyDelivery))}${renderDocumentChange(documentPlan, `${cardId}-document`)}</article></div>`;
  };

  const isRoleEvent = (event) => ["assistant", "tool", "capability_simulation", "decision", "resource_load", "context_compaction"].includes(event.kind);
  const isInlineSteer = (event) => event.kind === "user" && event.state === "已用于本轮处理";
  const groupEvents = (events) => {
    const blocks = [];
    let index = 0;
    while (index < events.length) {
      const event = events[index];
      if (!isRoleEvent(event)) {
        blocks.push({ type: "single", events: [event], startIndex: index });
        index += 1;
        continue;
      }
      const actorName = event.actor?.name;
      const run = [event];
      let cursor = index + 1;
      while (cursor < events.length) {
        const next = events[cursor];
        if (isRoleEvent(next) && next.actor?.name === actorName) {
          run.push(next);
          cursor += 1;
          continue;
        }
        const afterSteer = events[cursor + 1];
        if (isInlineSteer(next) && isRoleEvent(afterSteer) && afterSteer.actor?.name === actorName) {
          run.push(next, afterSteer);
          cursor += 2;
          continue;
        }
        break;
      }
      blocks.push({ type: "role", events: run, startIndex: index });
      index = cursor;
    }
    const runCounts = new Map();
    for (const block of blocks) {
      if (block.type !== "role") continue;
      const actorName = block.events[0].actor?.name ?? "任务处理";
      const runNumber = (runCounts.get(actorName) ?? 0) + 1;
      runCounts.set(actorName, runNumber);
      block.runNumber = runNumber;
    }
    return blocks;
  };

  const renderMetrics = (metrics = []) => metrics.length ? `<div class="metric-row">${metrics.map(([value, label]) => `<div><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`).join("")}</div>` : "";

  const renderPlanningDag = (dag) => {
    if (!dag?.nodes?.length) return "";
    const levels = new Map();
    for (const node of dag.nodes) {
      if (!levels.has(node.level)) levels.set(node.level, []);
      levels.get(node.level).push(node);
    }
    const positions = new Map();
    const width = 720, nodeWidth = 124, nodeHeight = 42, rowGap = 78;
    [...levels.entries()].sort(([a], [b]) => a - b).forEach(([level, nodes]) => {
      const gap = width / nodes.length;
      nodes.forEach((node, index) => positions.set(node.id, { x: gap * (index + .5) - nodeWidth / 2, y: 12 + level * rowGap }));
    });
    const byId = new Map(dag.nodes.map((node) => [node.id, node]));
    const dependsTransitively = (start, target, seen = new Set()) => {
      if (start === target) return true;
      if (seen.has(start)) return false;
      seen.add(start);
      return (byId.get(start)?.dependsOn || []).some((id) => dependsTransitively(id, target, seen));
    };
    // The diagram omits redundant transitive edges; the planning data keeps every prerequisite.
    const edges = dag.nodes.flatMap((node) => node.dependsOn.filter((id) => !node.dependsOn.some((other) => other !== id && dependsTransitively(other, id))).map((id) => {
      const from = positions.get(id), to = positions.get(node.id);
      const x1 = from.x + nodeWidth / 2, y1 = from.y + nodeHeight, x2 = to.x + nodeWidth / 2, y2 = to.y;
      const mid = (y1 + y2) / 2;
      return '<path data-from="' + esc(id) + '" data-to="' + esc(node.id) + '" d="M ' + x1 + ' ' + y1 + ' V ' + mid + ' H ' + x2 + ' V ' + (y2 - 4) + '" marker-end="url(#plan-arrow)" />';
    })).join("");
    const nodes = dag.nodes.map((node) => {
      const { x, y } = positions.get(node.id);
      return '<a class="dag-node" href="#stage-' + esc(node.id) + '" data-target="stage-' + esc(node.id) + '" aria-label="' + esc(node.code + ' ' + node.title) + '"><rect x="' + x + '" y="' + y + '" width="' + nodeWidth + '" height="' + nodeHeight + '" rx="6" /><text x="' + (x + nodeWidth / 2) + '" y="' + (y + nodeHeight / 2) + '" text-anchor="middle" dominant-baseline="central">' + esc(node.code + ' ' + node.title) + '</text></a>';
    }).join("");
    return '<section class="planning-dag"><h3>阶段依赖</h3><svg viewBox="0 0 720 ' + (levels.size * rowGap) + '" role="group" aria-label="建模阶段依赖图"><defs><marker id="plan-arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M 0 0 L 6 3 L 0 6" fill="currentColor" /></marker></defs><g class="dag-edges">' + edges + '</g>' + nodes + '</svg></section>';
  };

  const renderPlannedStages = (plan) => plan.length ? '<section class="planned-stage-list" id="planned-stage-details"><h3>任务树</h3>' + plan.map(renderStage).join("") + '</section>' : "";

  const renderResult = (event, meta, ctx) => {
    if (ctx.id === "requirement") return renderRequirementFinal(event, meta);
    if (ctx.id === "cad") return renderCadFinal(event, meta);
    if (ctx.id === "planning") {
      const state = event.planningState;
      planningCheckpoints.set(meta.id, state);
      const tree = state.ifc_task_tree, edges = state.stage_dependencies;
      const names = new Map(tree.stages.map((stage) => [stage.stage_id, stage.title]));
      const dependencies = edges == null ? '<p class="detail-note">阶段依赖尚未规划。</p>' : renderPlanningDag(window.IFC_PLAN.dag(tree, edges)) + '<details class="result-table"><summary>查看依赖原因</summary><dl class="detail-fields">' + edges.map((edge) => '<div><dt>' + esc(names.get(edge.from_stage_id) + ' → ' + names.get(edge.to_stage_id)) + '</dt><dd>' + esc(edge.reason) + '</dd></div>').join('') + '</dl></details>';
      return '<section class="requirement-final planning-final trace-target replay-item" id="' + meta.id + '" data-planning-checkpoint="' + meta.id + '"><header><h2>整体建模计划</h2><button class="document-chip" type="button" data-action="open-document" data-document="doc-ifc-plan">▤ IFC 整体建模计划.md ↗</button></header>' + dependencies + renderPlannedStages(window.projectIfcPlan(tree, edges)) + renderClarifications(state.clarifications, state.business_entry) + renderUserActionHint(event) + '</section>';
    }
    const keyValues = list(event.keyValues);
    const domains = list(event.domains);
    const plan = list(event.plan);
    const groups = list(event.groups);
    const cardId = `stage-card-${++cardSequence}`;
    const resultSummary = event.metrics?.map(([value, label]) => `${label} ${value}`).join("；") || keyValues.map(([key, value]) => `${key}：${value}`).join("；") || ctx.summary;
    const hasTaskChecks = ctx.status === "已完成" && Boolean(ctx.tasks?.length);
    const completionKind = hasTaskChecks ? "阶段完成摘要"
      : ctx.id === "requirement" ? "需求确认摘要"
        : ctx.id === "cad" ? "CAD 数据准备摘要"
          : ctx.id === "planning" ? "建模计划摘要" : "工作结果";
    savedCards.push({ id: cardId, targetId: hasTaskChecks || event.finalActions ? cardId : meta.id, kind: completionKind, stageId: ctx.id, stageTitle: ctx.shortTitle ?? ctx.navLabel ?? ctx.title, role: hasTaskChecks ? "阶段完成" : "结果", iteration: 1, time: meta.time, summary: resultSummary });
    if (event.finalActions) return `<div class="final-actions trace-target replay-item" id="${meta.id}"><button type="button" data-action="open-model" data-version="after"><span>◇</span><strong>查看最终 IFC 模型</strong><small>整网、端口、系统与 PipeRun</small></button><button type="button" data-action="open-document" data-document="${esc(event.documentId ?? "doc-final")}"><span>▤</span><strong>查看最终修订记录</strong><small>反馈范围与回归检查</small></button></div><details class="saved-summary stage-saved-summary trace-target" id="${cardId}"><summary><span>任务完成摘要</span><small>最终版本</small></summary><dl><div><dt>交付</dt><dd>最终 IFC 模型与修订记录</dd></div><div><dt>完成结果</dt><dd>${esc(resultSummary)}</dd></div><div><dt>状态</dt><dd>用户确认后的最终版本</dd></div></dl></details>`;
    const savedResult = hasTaskChecks
      ? `<details class="saved-summary stage-saved-summary trace-target" id="${cardId}"><summary><span>阶段完成摘要</span><small>${meta.time}</small></summary><dl><div><dt>阶段</dt><dd>${esc(ctx.title)}</dd></div><div><dt>本次结果</dt><dd>${esc(resultSummary)}</dd></div><div><dt>任务状态</dt><dd>${ctx.tasks.length} 个任务、${ctx.tasks.flatMap((taskItem) => taskItem.checks).length} 个检查项已由 Reviewer 更新</dd></div></dl></details>`
      : "";
    const groupedContent = groups.length ? `<div class="result-groups">${groups.map((group) => `<section><h4>${esc(group.title)}</h4>${group.description ? `<p>${esc(group.description)}</p>` : ""}<dl>${list(group.items).map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl></section>`).join("")}</div>` : "";
    return `<div class="structured-result trace-target replay-item" id="${meta.id}"><header><span>${esc(event.title)}</span><small>${esc(visibleStatus(event.status))}</small></header>${renderMetrics(list(event.metrics))}${keyValues.length ? `<dl class="key-grid">${keyValues.map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>` : ""}${groupedContent}${domains.length ? `<div class="domain-list">${domains.map((domain) => `<span>${esc(domain)}</span>`).join("")}</div>` : ""}${event.callout ? `<p class="result-callout">${esc(event.callout)}</p>` : ""}${renderPlanningDag(event.dag)}${renderPlannedStages(plan)}${event.documentId ? `<button class="inline-link" type="button" data-action="open-document" data-document="${esc(event.documentId)}">▤ ${esc(displayFileTitle(documents[event.documentId]?.title))}.md ↗</button>` : ""}</div>${savedResult}`;
  };

  const renderArtifact = (event, meta) => {
    const thumbnails = list(event.thumbnails);
    if (event.kindName === "model") return `<article class="model-result trace-target replay-item" id="${meta.id}"><button class="model-image" type="button" data-action="open-model" data-version="${esc(event.version ?? "after")}"><img src="${esc(event.image)}" alt="${esc(event.title)}" /></button><div class="model-copy"><p class="eyebrow">模型成果</p><h3>${esc(event.title)}</h3><p>${esc(event.summary)}</p>${thumbnails.length ? `<div class="model-thumbnails">${thumbnails.map((thumb) => `<button type="button" data-action="open-detail" data-title="${esc(thumb.title)}" data-src="${esc(thumb.src)}"><img src="${esc(thumb.src)}" alt="${esc(thumb.title)}" /></button>`).join("")}</div>` : ""}<button class="primary-link" type="button" data-action="open-model" data-version="${esc(event.version ?? "after")}">打开 IFC 模型</button></div></article>`;
    return `<article class="component-result trace-target replay-item" id="${meta.id}"><button class="component-preview" type="button" data-action="${esc(event.action ?? "open-detail")}" data-title="${esc(event.titleForViewer ?? event.title)}" data-src="${esc(event.image)}"><span class="component-collage"><img src="${esc(event.image)}" alt="${esc(event.title)}" /></span><span class="preview-label">点击查看预览</span></button><div><p class="eyebrow">${event.kindName === "component" ? "构件成果" : "局部模型"}</p><h4>${esc(event.title)}</h4><p>${esc(event.summary)}</p><button class="primary-link" type="button" data-action="${esc(event.action ?? "open-detail")}" data-title="${esc(event.titleForViewer ?? event.title)}" data-src="${esc(event.image)}">${event.kindName === "component" ? "查看构件与接口" : "打开局部模型"}</button></div></article>`;
  };

  const renderSingleEvent = (event, ctx, index) => {
    const meta = nextEventMeta(ctx, event, index);
    if (event.kind === "stage_checkpoint") return renderStageCheckpoint(event, meta, ctx);
    // The stage summary node still exists as a replay step; it no longer prints a public note.
    if (event.kind === "stage_card_generation") return '<span class="stage-control replay-item" hidden id="' + meta.id + '"></span>';
    if (event.kind === "stage_complete") return renderStageCardDoc(event.stageCard, meta.id);
    if (event.kind === "stage_route") return '<span class="stage-control replay-item" hidden id="' + meta.id + '"></span>';
    // The end of the flow is a section of its own: the completion notice plus the final IFC the
    // user can download, and nothing else — it is not a Stage result and does not repeat the cards.
    if (event.kind === "workflow_complete") return '<section class="workflow-complete replay-item" id="' + meta.id + '"><h2>' + esc(event.title) + '</h2>' + (event.ifcFile ? '<p class="stage-model-result"><a class="document-chip" href="' + esc(event.ifcFile.url) + '" download="' + esc(event.ifcFile.name) + '">↓ 下载 ' + esc(event.ifcFile.name) + '</a></p>' : '') + '</section>';
    if (event.kind === "user") return `<article class="message user-message replay-item trace-target" id="${meta.id}"><div class="user-bubble">${esc(event.text)}${event.attachment ? `<span class="attachment">▱ ${esc(event.attachment)}</span>` : ""}${event.state ? `<small>${esc(event.state)}</small>` : ""}</div></article>`;
    if (event.kind === "note") return `<p class="flow-note replay-item trace-target" id="${meta.id}"><span>✓</span>${esc(event.text)}</p>`;
    if (event.kind === "result") return renderResult(event, meta, ctx);
    if (event.kind === "artifact") return renderArtifact(event, meta);
    return "";
  };

  const taskMark = (status) => status === "completed" ? "✓" : "○";
  const taskLabel = (status) => status === "completed" ? "已完成" : "待展开";
  const renderTaskPanel = (ctx) => {
    const tasks = list(ctx.tasks);
    if (!tasks.length) return "";
    const checks = tasks.flatMap((item) => item.checks);
    const completed = tasks.filter((item) => item.status === "completed").length;
    return `<details class="stage-tasks"><summary><span>阶段任务与检查项</span><small>${completed} / ${tasks.length} 个任务完成 · ${checks.length} 个检查项</small></summary><div class="task-tree-inline">${tasks.map((item) => `<details class="task-node trace-target" id="task-${esc(item.id)}"><summary><i>${taskMark(item.status)}</i><span><strong>${esc(item.title)}</strong><small>${esc(item.owner)} · ${taskLabel(item.status)}</small></span></summary>${item.checks.length ? `<ul>${item.checks.map((check) => `<li class="trace-target" id="check-${esc(check.id)}"><i>${taskMark(check.status)}</i><span>${esc(check.title)}</span><small>${taskLabel(check.status)}</small></li>`).join("")}</ul>` : '<p class="panel-help">进入本阶段时细化任务与完成条件。</p>'}</details>`).join("")}</div></details>`;
  };

  const renderStageWorkflow = (stage) => stage.workflow?.length ? `<section class="stage-workflow"><h3>阶段执行流程</h3><p>计划确认后按以下流程推进，有问题时继续当前阶段。</p><ol>${stage.workflow.map((step) => `<li class="${step.conditional ? "is-conditional" : ""}"><span>${esc(step.role)}${step.conditional ? " · 按需" : ""}</span><div><strong>${esc(step.title)}</strong><p>${esc(step.text)}</p></div></li>`).join("")}</ol></section>` : "";

  const renderEvents = (ctx) => groupEvents(ctx.events ?? []).map((block) => block.type === "role" ? renderRoleRun(block.events, ctx, block.startIndex, block.runNumber) : renderSingleEvent(block.events[0], ctx, block.startIndex)).join("");
  const renderSectionHeading = (ctx) => ["requirement", "cad", "planning"].includes(ctx.id) ? "" : `<header class="conversation-section-heading"><div><h2>${esc(ctx.title)}</h2><p>${esc(ctx.summary)}</p></div>${ctx.documentId ? `<button class="text-button" type="button" data-action="open-document" data-document="${esc(ctx.documentId)}">▤ ${esc(displayFileTitle(documents[ctx.documentId]?.title))}.md ↗</button>` : ""}</header>`;

  const renderStageGoals = (stage, className = 'stage-goal') => `<section class="${className}"><h4>整体目标</h4><p>${esc(stage.goal)}</p><h4>具体内容</h4><p>${esc(stage.content)}</p></section>`;
  const renderTaskSteps = (content) => {
    const lines = String(content).split("\n").filter((line) => line.trim());
    return lines.length && lines.every((line) => /^\d+\. /.test(line))
      ? '<ol class="task-steps">' + lines.map((line) => '<li>' + esc(line.replace(/^\d+\. /, '')) + '</li>').join('') + '</ol>'
      : lines.map((line) => '<p>' + esc(line) + '</p>').join('');
  };
  const renderTask = (task, location) => {
    const state = window.IFC_PLAN.taskPresentation(task.status);
    const submission = task.submission, review = task.review;
    const verdicts = { pass: "通过", fail: "不通过", unable_to_assess: "无法评估" };
    const checks = task.checks.length ? '<ul class="task-checks">' + task.checks.map((check) => '<li data-check="' + esc(check.check_id) + '"><div><strong>' + esc(check.description) + '</strong><span class="assessment-status" data-verdict="' + esc(check.assessment?.verdict ?? 'pending') + '">' + esc(check.assessment ? verdicts[check.assessment.verdict] : '尚未评估') + '</span></div>' + (check.assessment ? '<p>' + esc(check.assessment.finding) + '</p>' : '') + '</li>').join('') + '</ul>' : '<p class="task-empty">阶段规划时补充检查条件。</p>';
    return '<details class="tree-task task-detail" data-tree-task="' + esc(task.task_id) + '" data-task-status="' + esc(task.status) + '"' + (location === 'plan' ? ' id="task-' + esc(task.task_id) + '"' : '') + '><summary><i aria-hidden="true">' + state.mark + '</i><span><strong>' + esc(task.title) + '</strong><span class="task-status-label">' + esc(state.label) + '</span><p>' + esc(task.description) + '</p></span><b class="task-disclosure" aria-hidden="true">›</b></summary><div class="task-detail-body"><section><h5>建模步骤</h5>' + renderTaskSteps(task.content) + '</section><section><h5>建模提交</h5>' + (submission ? '<p>' + esc(submission.summary) + '</p><dl class="task-submission"><dt>关键操作</dt><dd>' + (submission.operation_aliases.length ? submission.operation_aliases.map((alias) => '<code>' + esc(alias) + '</code>').join(' ') : '未引用关键操作') + '</dd><dt>剩余工作</dt><dd>' + esc(submission.remaining_work || '未声明剩余工作') + '</dd></dl>' : '<p class="task-empty">尚未提交工作。</p>') + '</section><section><h5>检查与修订</h5>' + checks + (review ? '<p class="task-review-reason">' + esc(review.reason) + '</p>' + (review.repair_instructions ? '<div class="task-repair"><strong>修订要求</strong><p>' + esc(review.repair_instructions) + '</p></div>' : '') : task.checks.length ? '<p class="task-empty">尚无检查决定。</p>' : '') + '</section></div></details>';
  };
  const renderStageStatus = (stage) => {
    const state = window.IFC_PLAN.stagePresentation(stage);
    if (state.total && state.passed === state.total && acceptedStageIds.has(stage.stage_id ?? stage.id)) state.label = "已确认";
    return esc(state.label) + ' · ' + state.total + ' 项任务' + (state.passed ? ' · ' + state.passed + ' 项检查通过' : '');
  };
  const renderStageCheckpoint = (event, meta, ctx) => {
    stageCheckpoints.set(meta.id, event);
    const stage = event.state.ifc_task_tree.stages.find((stage) => stage.stage_id === ctx.id);
    const plan = event.boundary === 'plan', component = event.state.component_plan;
    const next = fixture.stages.find((item) => item.id === ctx.nextStageId);
    const modelLink = !plan && event.ifcFile ? '<p class="stage-model-result"><a class="document-chip" href="' + esc(event.ifcFile.url) + '" download="' + esc(event.ifcFile.name) + '">↓ 下载 ' + esc(event.ifcFile.name) + '</a></p>' : '';
    return '<section class="requirement-final stage-final replay-item" id="' + meta.id + '" data-stage-checkpoint="' + meta.id + '"><header><div><h2>' + esc(event.title) + '</h2><p class="requirement-round-label">' + (plan ? '阶段计划待确认' : '检查通过 · 等待用户确认') + '</p></div>' + (plan ? '<button class="document-chip" type="button" data-action="open-document" data-document="' + ctx.documentId + '">▤ ' + esc(ctx.shortTitle) + '阶段计划.md ↗</button>' : '') + '</header>' + modelLink + '<details class="stage-result-tasks"><summary>' + stage.tasks.length + ' 项任务 · ' + stage.tasks.flatMap((task) => task.checks).length + ' 项检查条件<span>查看任务</span></summary>' + stage.tasks.map((task) => renderTask(task, 'checkpoint')).join('') + '</details>' + (plan ? '<section class="component-decision"><h3>构件准备</h3><p>' + esc(component.reason) + '</p>' + (component.items.length ? '<details><summary>准备目标与使用方式 · ' + component.items.length + ' 项</summary>' + component.items.map((item) => '<section><h4>' + esc(item.name) + '</h4><dl class="detail-fields">' + [['准备要求', item.requirements], ['安装环境', item.environment], ['准备方式', item.approach], ['后续用法', item.usage]].map(([key, value]) => '<div><dt>' + key + '</dt><dd>' + esc(value) + '</dd></div>').join('') + '</dl></section>').join('') + '</details>' : '') + '</section>' + renderClarifications(event.state.clarifications) : '') + renderUserActionHint(event) + '</section>';
  };
  const stagePhases = [['Stage Planner', '阶段规划'], ['Modeler', '建模'], ['Reviewer', '检查'], ['用户确认', '结果确认']];
  const renderStageExecution = (stage) => {
    sectionIndex.set(stage.id, stage);
    if (!stage.events.length) return '<details class="stage-placeholder replay-item" id="stage-' + esc(stage.id) + '-flow" data-stage-placeholder="' + esc(stage.id) + '"><summary><span class="stage-number">' + esc(stage.code) + '</span><span><strong>' + esc(stage.shortTitle) + '</strong><small>' + (stage.id === 'pipes' ? '前序已确认 · 待细化' : '等待前序阶段') + '</small></span><i>⌄</i></summary><div class="empty-stage-body"><p>' + (stage.dependsOn.length ? '前序：' + stage.dependsOn.map((id) => esc(fixture.stages.find((entry) => entry.id === id).shortTitle)).join('、') : '整体计划确认后进入') + '</p><div class="empty-stage-roles">' + stagePhases.slice(0, 3).map(([role]) => '<section><strong>' + role + '</strong><span>尚未进入</span></section>').join('') + '</div></div></details>';
    // The completion notice belongs to the flow, not to this Stage: it is rendered after the stage
    // sections (see renderSection) instead of inside the stage it happens to be recorded on.
    const body = renderEvents({ ...stage, events: (stage.events ?? []).filter((event) => event.kind !== "workflow_complete") });
    return '<details class="stage-execution replay-item" id="stage-' + stage.id + '-flow" data-stage-execution="' + stage.id + '" open><summary class="stage-execution-header"><span class="stage-number">' + stage.code + '</span><div><h2>' + esc(stage.shortTitle) + '</h2><p>' + esc(stage.title) + '</p></div><span class="stage-live-label" data-stage-live="' + stage.id + '">已确认</span><span class="stage-disclosure" aria-hidden="true">›</span></summary><div class="stage-conversation">' + body + '</div></details>';
  };
  const renderStage = (stage) => {
    sectionIndex.set(stage.id, stage);
    return '<details class="stage-card planned-stage" id="stage-' + esc(stage.id) + '"><summary><i>' + esc(stage.code) + '</i><span><strong>' + esc(stage.shortTitle) + '</strong><small>' + renderStageStatus(stage) + '</small></span><em>＋</em></summary><div class="stage-body">' + renderStageGoals(stage) + '<h4 class="stage-task-heading">主要任务</h4><div class="stage-task-list">' + stage.tasks.map((task) => renderTask(task, 'plan')).join('') + '</div></div></details>';
  };

  const phaseForSection = (id) => id === "requirement" ? "requirement" : id === "cad" ? "cad" : ["planning", "modeling"].includes(id) ? "modeling" : "summary";
  const renderSection = (section) => {
    sectionIndex.set(section.id, section);
    if (section.stages) {
      const completions = section.stages.flatMap((stage) => (stage.events ?? [])
        .filter((event) => event.kind === "workflow_complete")
        .map((event) => renderSingleEvent(event, stage, stage.events.indexOf(event))));
      return '<section class="conversation-section stage-flow-section replay-item" id="section-modeling" data-section="modeling"><article class="message user-message replay-item" data-recorded-plan-confirmation><div class="user-bubble">确认并继续</div></article>' + section.stages.map(renderStageExecution).join('') + completions.join('') + '</section>';
    }
    if (["requirement", "finalRevision"].includes(section.id) && section.events?.[0]?.kind === "user") {
      const [feedback, ...revisionEvents] = section.events;
      return `<section class="conversation-section replay-item" id="section-${esc(section.id)}" data-section="${esc(phaseForSection(section.id))}">${renderSingleEvent(feedback, section, 0)}${renderSectionHeading(section)}<div class="section-events">${renderEvents({ ...section, events: revisionEvents })}</div>${renderTaskPanel(section)}</section>`;
    }
    return `<section class="conversation-section replay-item" id="section-${esc(section.id)}" data-section="${esc(phaseForSection(section.id))}">${renderSectionHeading(section)}<div class="section-events">${renderEvents(section)}</div>${renderTaskPanel(section)}</section>`;
  };

  const deliveryMounts = [];
  const disposeDeliveryMounts = () => { while (deliveryMounts.length) deliveryMounts.pop()(); };
  const mountFamilyDeliveries = (root) => {
    const nodes = root ? [...root.querySelectorAll("[data-family-delivery]")] : [];
    if (!nodes.length) return;
    const attach = (node) => {
      if (node.dataset.mounted === "true") return;
      const model = familyDeliveryModel(node.dataset.familyDelivery, node.dataset.deliveryVersion);
      if (!model) return;
      node.dataset.mounted = "true";
      import("./library-viewer.js?v=20260912-flat-viewer").then(({ mountViewer }) => {
        deliveryMounts.push(mountViewer(node.querySelector(".family-delivery-viewer"), model, node.dataset.familyDelivery));
      }).catch(() => {
        const status = node.querySelector(".resource-model-status");
        if (status) status.textContent = "三维视图暂不可用";
      });
    };
    // Mount eagerly: the replay runtime toggles visibility instead of
    // re-rendering, and an observer created for detached nodes would never
    // fire. Each delivery block owns one small STL viewer.
    nodes.forEach(attach);
  };
  // Flow card (right rail): before the IFC plan/DAG is confirmed it only shows the three
  // pre-planning stages plus an ellipsis; once the plan is confirmed the eight IFC stages replace
  // the ellipsis. During replay nodes appear as the playback reaches them, the active node shows
  // the current agent and round, and finished units are ticked off.
  const FLOW_PRE = [
    { id: "requirement", label: "需求", target: "section-requirement", kind: "pre" },
    { id: "cad", label: "图纸解析", target: "section-cad", kind: "pre" },
    { id: "planning", label: "IFC 全局规划", target: "section-planning", kind: "pre" },
  ];
  // Jump to the stage's own agent execution, not to the Global Planner's planned-stage card.
  const FLOW_STAGES = fixture.stages.map((stage) => ({ id: stage.id, label: stage.shortTitle, target: "stage-" + stage.id + "-flow", kind: "stage", code: stage.code }));
  const flow = { replay: false, dagConfirmed: true, reached: new Set([...FLOW_PRE, ...FLOW_STAGES].map((unit) => unit.id)), current: null };
  const flowRendered = new Set();
  const flowAllUnits = () => [...FLOW_PRE, ...FLOW_STAGES];
  const flowRank = (id) => flowAllUnits().findIndex((unit) => unit.id === id);
  // The frontier is the newest unit the playback has reached: it is still in progress, everything
  // before it is finished. A stage only counts as finished once its result was accepted.
  const flowFrontier = () => {
    const reached = flowAllUnits().filter((unit) => flow.reached.has(unit.id) || flow.current?.id === unit.id);
    return reached.at(-1)?.id ?? null;
  };
  const flowUnitState = (unit) => {
    if (unit.kind === "stage" && acceptedStageIds.has(unit.id)) return "done";
    if (flow.current?.id === unit.id) return "active";
    if (!flow.replay) return "done";
    const frontier = flowFrontier();
    if (!frontier) return flow.reached.has(unit.id) ? "active" : "pending";
    return flowRank(unit.id) < flowRank(frontier) ? "done" : flow.reached.has(unit.id) ? "active" : "pending";
  };
  const renderPhaseStrip = () => {
    const units = [...FLOW_PRE, ...(flow.dagConfirmed ? FLOW_STAGES : [])];
    // The strip is the plan, not a bar that grows with playback: the three pre-planning units are
    // listed from the first frame, and the eight stages all appear the moment the user confirms the
    // DAG. Progress is only expressed by the state dots (✓ done / ● active / ○ pending).
    const visible = units;
    const items = visible.map((unit) => {
      const state = flowUnitState(unit);
      const done = state === "done";
      const active = state === "active";
      const animated = flow.replay && !flowRendered.has(unit.id) ? " is-animated" : "";
      flowRendered.add(unit.id);
      const mark = done ? "✓" : active ? "●" : "○";
      const className = active ? "is-active-flow" : done ? "is-done" : "is-pending";
      return `<button class="stage-link ${className}${animated}" type="button" data-target="${esc(unit.target)}" aria-label="${esc((unit.code ? unit.code + " " : "") + unit.label)}${done ? " · 已完成" : active ? " · 进行中" : ""}"><i>${mark}</i><span>${esc((unit.code ? unit.code + " " : "") + unit.label)}</span></button>`;
    }).join("");
    const ellipsis = flow.dagConfirmed ? "" : '<span class="stage-ellipsis">…</span>';
    const finished = FLOW_STAGES.filter((unit) => acceptedStageIds.has(unit.id)).length;
    const head = `<div class="stage-strip-head"><strong>流程</strong><small>${flow.dagConfirmed ? `${finished}/${FLOW_STAGES.length} 个阶段已完成` : "IFC 流程待确认"}</small></div>`;
    const now = flow.current
      ? `<p class="stage-strip-now">${esc(flow.current.label)}<small>${esc([flow.current.actor, flow.current.round ? `第 ${flow.current.round} 轮` : ""].filter(Boolean).join(" · ") || "进行中")}</small></p>`
      : "";
    phaseStrip.innerHTML = `${head}<div class="stage-strip-list">${items}${ellipsis}</div>${now}`;
  };
  const renderFixture = () => {
    renderPhaseStrip();
    completedView.innerHTML = `${fixture.sections.map(renderSection).join("")}<div class="conversation-end" aria-hidden="true"></div>`;
    disposeDeliveryMounts();
    mountFamilyDeliveries(completedView);
  };

  renderFixture();
  document.querySelectorAll("[data-role-icon]").forEach((host) => { host.innerHTML = roleIconSvg(host.dataset.roleIcon); });

  const resolveProductTarget = (product) => eventTargets.get(`${product.stageId}:${product.targetEventTitle}`) || (fixture.stages.some((stage) => stage.id === product.stageId) ? `stage-${product.stageId}` : `section-${product.stageId}`);
  const renderTaskTreeBody = (content, complete = false) => {
    const stages = content ? window.projectIfcPlan(content, displayedStageDependencies) : [];
    const questions = renderClarifications(displayedPlanningClarifications);
    if (!stages.length) return '<p class="detail-note">' + (content ? '正在整理阶段与任务。' : '等待整体规划。') + '</p>' + questions;
    const taskCount = stages.reduce((count, stage) => count + stage.tasks.length, 0);
    const executing = stages.some((stage) => stage.tasks.some((task) => task.status !== 'planned'));
    return '<p class="task-tree-status">' + stages.length + ' 个阶段 · ' + taskCount + ' 项任务 · ' + (executing ? '阶段执行' : complete ? '待确认' : '规划中') + '</p><div class="inspector-task-tree">' + stages.map((stage) =>
      '<details class="tree-stage" data-tree-stage="' + esc(stage.id) + '"><summary><i>›</i><span><strong>' + esc(stage.code + ' ' + stage.shortTitle) + '</strong><small>' + renderStageStatus(stage) + '</small></span></summary><div><p class="tree-stage-description">' + esc(stage.summary) + '</p><details class="tree-stage-context" data-tree-context="' + esc(stage.id + '-context') + '"><summary>阶段目标与安排</summary>' + renderStageGoals(stage, 'tree-stage-goal') + '</details><h4 class="tree-task-heading">主要任务</h4>' + stage.tasks.map((task) => renderTask(task, 'inspector')).join('') + '</div></details>').join('') + '</div>' + questions;
  };
  const renderInspectorTaskTree = () => '<section class="inspector-panel" data-inspector-panel="tasks" hidden><div id="liveTaskTreeContent">' + renderTaskTreeBody(displayedTaskTree, taskTreeComplete) + '</div></section>';
  const setTaskTreeState = (state) => {
    const previousIds = new Set((displayedTaskTree?.stages ?? []).map((stage) => stage.stage_id));
    if (!state || Object.hasOwn(state, 'ifc_task_tree')) displayedTaskTree = state?.ifc_task_tree ?? null;
    if (!state || Object.hasOwn(state, 'clarifications')) displayedPlanningClarifications = state?.clarifications ?? null;
    if (!state || Object.hasOwn(state, 'stage_dependencies')) displayedStageDependencies = state?.stage_dependencies ?? null;
    if (!state || Object.hasOwn(state, 'complete')) taskTreeComplete = Boolean(state?.complete);
    // The IFC flow only replaces the "…" placeholder after the plan/DAG is confirmed.
    if (taskTreeComplete && !flow.dagConfirmed) { flow.dagConfirmed = true; renderPhaseStrip(); }
    const host = document.getElementById("liveTaskTreeContent");
    if (!host) return;
    const expanded = new Set([...host.querySelectorAll(".tree-stage[open], .tree-task[open], .tree-stage-context[open]")].map((row) => row.dataset.treeStage ?? row.dataset.treeTask ?? row.dataset.treeContext));
    const scrollTop = inspectorContent.scrollTop;
    host.innerHTML = renderTaskTreeBody(displayedTaskTree, taskTreeComplete);
    host.querySelectorAll(".tree-stage, .tree-task, .tree-stage-context").forEach((row) => {
      const id = row.dataset.treeStage ?? row.dataset.treeTask ?? row.dataset.treeContext;
      row.open = expanded.has(id) || (Boolean(row.dataset.treeStage) && !previousIds.has(id));
      if (row.dataset.treeTask) row.classList.toggle("is-task-updated", (state?.changed_task_ids ?? []).includes(row.dataset.treeTask));
    });
    inspectorContent.scrollTop = scrollTop;
  };

  const stageProducts = fixture.stages.flatMap((stage) => stage.events
    .filter((event) => event.ifcFile)
    .map((event) => ({ ...event.ifcFile, stageId: stage.id, final: event.kind === "workflow_complete" })));
  const visibleIfcFiles = new Set();
  const renderIfcFile = (file) => `<div class="resource-detail" data-ifc-file="${esc(file.url)}"><a class="document-chip" href="${esc(file.url)}" download="${esc(file.name)}">↓ ${esc(file.name)}</a></div>`;
  const syncReplayIfcFiles = () => {
    const files = [...inspectorContent.querySelectorAll("[data-ifc-file]")];
    files.forEach((item) => { item.hidden = replayActive && !visibleIfcFiles.has(item.dataset.ifcFile); });
    const group = inspectorContent.querySelector("[data-ifc-files]");
    if (group) group.hidden = files.every((item) => item.hidden);
  };

  const renderProduct = (product) => {
    const target = resolveProductTarget(product);
    const icon = product.kind === "数据包" ? "▦" : product.kind === "构件版本" ? "◇" : "◫";
    const openAction = product.kind === "数据包"
      ? `<button type="button" data-action="open-data-product" data-product="${esc(product.id)}">查看数据内容</button>`
      : product.kind === "构件版本"
        ? `<button type="button" data-action="open-component">查看构件与接口</button>`
        : `<button type="button" data-action="open-model" data-stage="${esc(product.stageId)}" data-title="${esc(product.title)}" data-preview="${esc(product.preview)}">查看阶段模型</button>`;
    const title = product.kind === "数据包" ? product.title.replace(/（[^（）]*）|\([^()]*\)/g, "").replace(/\s*[·・]?\s*(?:历史版本|历史参考|已修订)/g, "").trim() : product.title;
    return `<details class="resource-detail"${product.kind === "数据包" ? ` data-package-alias="${esc(product.alias)}"` : ""}><summary><span class="resource-icon">${icon}</span><span><strong>${esc(title)}</strong><small>${esc(product.kind)}${product.alias ? ` · ${esc(product.alias)}` : ""}${product.kind === "数据包" ? "" : ` · ${esc(product.status)}`}</small></span><em>›</em></summary><div>${product.kind === "数据包" ? "" : `<p>${esc(product.summary)}</p>`}${renderMetrics(list(product.metrics))}${product.contents?.length ? `<ul>${product.contents.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}<div class="resource-actions">${openAction}<button type="button" data-target="${esc(target)}">定位到生成过程</button></div></div></details>`;
  };

  const renderInspectorProducts = () => `<section class="inspector-panel" data-inspector-panel="products" hidden><div class="resource-group"><h3>数据包</h3>${fixture.dataProducts.filter((item) => item.kind === "数据包").map(renderProduct).join("")}</div>${fixture.dataProducts.some((item) => item.kind === "构件版本") ? `<div class="resource-group"><h3>构件版本</h3>${fixture.dataProducts.filter((item) => item.kind === "构件版本").map(renderProduct).join("")}</div>` : ""}${stageProducts.length ? `<div class="resource-group" data-ifc-files><h3>IFC 文件</h3>${stageProducts.map(renderIfcFile).join("")}</div>` : ""}</section>`;

  const groupByStage = (items) => items.reduce((groups, item) => {
    if (!groups.has(item.stageId)) groups.set(item.stageId, []);
    groups.get(item.stageId).push(item);
    return groups;
  }, new Map());

  const mcpOperations = operations.filter((operation) => operation.isTool);
  const operationCounts = (items) => ({ all: items.length, write: items.filter((item) => item.effect === "WRITE").length, read: items.filter((item) => item.effect !== "WRITE").length });
  const stageName = (id) => sectionIndex.get(id)?.shortTitle ?? sectionIndex.get(id)?.navLabel ?? sectionIndex.get(id)?.title ?? id;
  const operationSummary = (counts, filter = "all") => filter === "all" ? `${counts.all} 次 · 写入 ${counts.write} · 查询 ${counts.read}` : `${counts[filter]} 次${filter === "write" ? "写入" : "查询"}`;
  // Conversation order: 需求 → 图纸解析 → 建模规划 → 八个阶段. Both inspector panels use it, and
  // during replay a unit only appears once the playback has actually reached it.
  const unitOrder = [...fixture.sections.map((section) => section.id), ...fixture.stages.map((stage) => stage.id)];
  const unitRank = (id) => { const index = unitOrder.indexOf(id); return index < 0 ? unitOrder.length : index; };
  const revealedUnits = new Set();
  const revealedOperations = () => replayActive ? mcpOperations.filter((operation) => revealedUnits.has(operation.stageId)) : mcpOperations;
  let currentOperationFilter = "all";
  const renderInspectorOperations = () => {
    const counts = operationCounts(revealedOperations());
    return `<section class="inspector-panel" data-inspector-panel="operations" hidden><div class="operation-filters" role="group" aria-label="MCP 操作记录筛选"><button class="is-active" type="button" data-operation-filter="all">全部 ${counts.all}</button><button type="button" data-operation-filter="write">写入 ${counts.write}</button><button type="button" data-operation-filter="read">查询 ${counts.read}</button></div><div class="operation-groups">${[...groupByStage(mcpOperations).entries()].map(([stageId, items]) => {
      const totals = operationCounts(items);
      return `<details class="history-stage operation-stage" data-operation-stage="${esc(stageId)}" data-total="${totals.all}" data-write="${totals.write}" data-read="${totals.read}"><summary><span>${esc(stageName(stageId))}</span><small data-operation-count>${operationSummary(totals)}</small></summary><div>${items.map((operation) => `<button class="operation-row" type="button" data-operation-kind="${operation.effect === "WRITE" ? "write" : "read"}" data-action="open-detail-panel" data-detail="${esc(operation.targetId)}"><span class="operation-effect">${operation.effect === "WRITE" ? "↗" : "◇"}</span><span><strong>${esc(operation.title)}</strong><small>${esc(operation.role)} · ${operation.effect === "WRITE" ? "写入" : "查询"}</small></span><em>›</em></button>`).join("")}</div></details>`;
    }).join("")}</div><p class="operation-empty" hidden>此筛选下没有 MCP 记录。</p></section>`;
  };

  const filterMcpOperations = (filter) => {
    currentOperationFilter = filter;
    inspectorContent.querySelectorAll("[data-operation-filter]").forEach((button) => button.classList.toggle("is-active", button.dataset.operationFilter === filter));
    inspectorContent.querySelectorAll("[data-operation-kind]").forEach((row) => { row.hidden = filter !== "all" && row.dataset.operationKind !== filter; });
    let visibleGroups = 0;
    inspectorContent.querySelectorAll("[data-operation-stage]").forEach((group) => {
      const counts = { all: Number(group.dataset.total), write: Number(group.dataset.write), read: Number(group.dataset.read) };
      group.hidden = counts[filter] === 0 || group.dataset.unitHidden === "true";
      group.querySelector("[data-operation-count]").textContent = operationSummary(counts, filter);
      if (!group.hidden) visibleGroups += 1;
    });
    inspectorContent.querySelector(".operation-empty").hidden = visibleGroups > 0;
  };
  // Hide not-yet-reached units and keep the filter totals in step with what is visible.
  const refreshUnitVisibility = () => {
    const counts = operationCounts(revealedOperations());
    inspectorContent.querySelectorAll("[data-operation-filter]").forEach((button) => {
      const filter = button.dataset.operationFilter;
      button.textContent = filter === "all" ? `全部 ${counts.all}` : `${filter === "write" ? "写入" : "查询"} ${counts[filter]}`;
    });
    inspectorContent.querySelectorAll("[data-operation-stage]").forEach((group) => {
      group.dataset.unitHidden = String(replayActive && !revealedUnits.has(group.dataset.operationStage));
    });
    inspectorContent.querySelectorAll("[data-file-stage]").forEach((group) => {
      group.hidden = replayActive && !revealedUnits.has(group.dataset.fileStage);
    });
    filterMcpOperations(currentOperationFilter);
  };
  const revealUnit = (id) => {
    if (!id || revealedUnits.has(id)) return;
    revealedUnits.add(id);
    flow.reached.add(id);
    renderPhaseStrip();
    refreshUnitVisibility();
  };

  const fileGroups = new Map();
  const stageCards = new Map();
  // Stage Cards are listed as stage summaries, not as stage documents: they are produced from the
  // finished stage. The row keeps the ✧ marker as the only sign of how it was produced; the
  // process files never spell out model/streaming wording.
  const addStageCardFile = (stageId, card) => {
    if (!card) return;
    stageCards.set(card.id, card);
    if (!fileGroups.has(stageId)) fileGroups.set(stageId, new Map());
    fileGroups.get(stageId).set(card.id, { id: card.id, title: card.title, kind: "阶段总结", llm: true });
  };
  for (const stage of fixture.stages) addStageCardFile(stage.id, stage.stageCard);
  const roleCards = new Map();
  // Role Cards are listed as ordinary hand-off records (no LLM/streaming wording) so every unit's
  // process files stay complete and openable in the detail panel.
  const addRoleCardFile = (unitId, card) => {
    if (!card) return;
    roleCards.set(card.id, card);
    if (!fileGroups.has(unitId)) fileGroups.set(unitId, new Map());
    fileGroups.get(unitId).set(card.id, { id: card.id, title: `${card.role} · 第 ${card.round} 轮工作与交接`, kind: "角色交接" });
  };
  for (const unit of [...fixture.sections, ...fixture.stages]) {
    for (const event of unit.events ?? []) if (event.roleCard) addRoleCardFile(unit.id, event.roleCard);
  }
  const addFile = (stageId, rawId) => {
    if (!rawId) return;
    const full = processDocument(rawId);
    const id = full?.id ?? rawId;
    const doc = full ?? documents[id];
    if (!doc || /^(Skill|Context|Guide|Role Card)$/i.test(full?.kind ?? "")) return;
    if (!fileGroups.has(stageId)) fileGroups.set(stageId, new Map());
    fileGroups.get(stageId).set(id, { id, title: doc.title, kind: full?.kind ?? "阶段文档" });
  };
  for (const section of [...fixture.sections, ...fixture.stages]) {
    addFile(section.id, section.documentId);
    for (const event of section.events ?? []) {
      if (event.kind === "resource_load") addFile(section.id, event.resource.id ?? event.resource.title);
      if (event.kind === "context_compaction") addFile(section.id, event.documentId);
      for (const title of event.contextData?.selected ?? []) addFile(section.id, title);
    }
  }
  const renderInspectorFiles = () => `<section class="inspector-panel" data-inspector-panel="files" hidden><div class="file-groups">${[...fileGroups.entries()].sort((a, b) => unitRank(a[0]) - unitRank(b[0])).map(([stageId, docs]) => `<details class="history-stage file-stage" data-file-stage="${esc(stageId)}"><summary><span>${esc(stageName(stageId))}</span><small>${docs.size} 份</small></summary><div class="file-list">${[...docs.values()].map((doc) => `<article class="file-row${doc.llm ? " is-llm-output" : ""}"><button type="button" data-action="open-document" data-document="${esc(doc.id)}"><span class="resource-icon${doc.llm ? " llm" : ""}" aria-hidden="true">${doc.llm ? "✧" : "▤"}</span><span><strong>${esc(displayFileTitle(doc.title))}</strong><small>${esc(doc.kind)}</small></span></button></article>`).join("")}</div></details>`).join("")}</div></section>`;
  inspectorContent.innerHTML = `<nav class="inspector-tabs" aria-label="任务内容"><button class="is-active" type="button" data-inspector-tab="requirement">需求</button><button type="button" data-inspector-tab="cad">CAD</button><button type="button" data-inspector-tab="tasks">任务树</button><button type="button" data-inspector-tab="products">数据成果</button><button type="button" data-inspector-tab="operations">MCP 操作记录</button><button type="button" data-inspector-tab="files">过程文件</button></nav>${renderInspectorTaskTree()}<section class="inspector-panel requirement-live is-active" data-inspector-panel="requirement"><div id="liveRequirementContent">${renderLiveRequirement(displayedRequirement)}</div></section><section class="inspector-panel cad-live" data-inspector-panel="cad" hidden><div id="liveCadContent">${renderLiveCad(displayedCad)}</div></section>${renderInspectorProducts()}${renderInspectorOperations()}${renderInspectorFiles()}`;

  const shell = document.getElementById("appShell");
  const scrollArea = document.getElementById("conversationScroll");
  const newTaskView = document.getElementById("newTaskView");
  const input = document.getElementById("messageInput");
  const hint = document.getElementById("composerHint");
  const toast = document.getElementById("toast");
  const inspector = document.getElementById("inspector");
  const modelDialog = document.getElementById("modelDialog");
  const dataDialog = document.getElementById("dataDialog");
  const replayButton = document.querySelector('[data-action="replay"]');
  const replayJump = document.getElementById("replayJump");
  const replaySpeedControl = document.getElementById("replaySpeed");
  replaySpeedControl?.querySelectorAll?.("[data-replay-speed]").forEach((button) => {
    button.addEventListener("click", () => {
      replaySpeed = Number(button.dataset.replaySpeed) || REPLAY_SPEED_DEFAULT;
      replaySpeedControl.querySelectorAll("[data-replay-speed]").forEach((item) => item.classList.toggle("is-active", item === button));
    });
  });
  const replayExitButton = document.querySelector('[data-action="exit-replay"]');
  const inspectorButton = document.querySelector('.topbar [data-action="toggle-inspector"]');
  const submitButton = document.querySelector('[data-action="submit-feedback"]');
  const confirmButton = document.querySelector('[data-action="confirm"]');
  const topbarTitle = document.querySelector(".topbar h1");
  const composer = document.querySelector(".composer");
  const taskTypePicker = document.getElementById("taskTypePicker");
  const composerFile = document.getElementById("composerFile");
  const composerAttachments = document.getElementById("composerAttachments");
  const attachmentButton = document.getElementById("attachmentButton");
  const taskPauseButton = document.getElementById("taskPauseButton");
  const tasks = new Map();
  let conversationEnd = completedView.querySelector(".conversation-end");
  let currentTaskMode = "completed";
  let currentTaskId = "";
  let chatConnection = null;
  let toastTimer = 0;
  // Replay speed is user selectable (1× / 16× / 64×; 16× by default) and scales every scheduled delay.
  let replaySpeed = REPLAY_SPEED_DEFAULT;
  const playback = window.CAD2BIM_REPLAY.createClock(
    (fn, delay) => window.setTimeout(fn, Math.max(1, Math.round((delay ?? 0) / replaySpeed))),
    (timer) => window.clearTimeout(timer));
  const replayText = new Map();
  let replayBusy = false;
  let replayIndex = 0;
  let replayPaused = false;
  let pendingRequirement = null;
  let resumeAfterRequirement = null;
  let pendingCad = null;
  let pendingPlanning = null;
  let pendingStage = null;
  let resumeAfterPlanning = null;
  let resumeAfterStage = null;
  let resumeAfterCad = null;
  let viewerScale = 1;
  let viewerRotation = 0;
  let activeSection = "requirement";
  let activeDataProduct = null;

  const showToast = (message) => {
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.add("is-visible");
    toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 1800);
  };

  const setPrimaryNavigation = (destination) => {
    document.querySelectorAll(".primary-nav .nav-item").forEach((button) => {
      const selected = destination === "library" ? button.dataset.action === "open-library" : button.dataset.action === "open-workspace";
      button.classList.toggle("is-active", selected);
    });
  };

  const finishReplayActivities = () => {
    completedView.querySelectorAll(".activity-group.is-replay-active").forEach((group) => {
      group.classList.remove("is-replay-active");
      const status = group.querySelector("summary small");
      status.textContent = status.dataset.completedLabel;
      group.querySelectorAll(".tool-call").forEach((tool) => { tool.disabled = false; const label = tool.querySelector(".activity-status"); if (label.dataset.completedLabel != null) label.textContent = label.dataset.completedLabel; });
    });
  };
  const setRoleReplayStatus = (run, processing) => {
    const status = run?.querySelector(".role-run-title small");
    if (!status) return;
    status.dataset.completedLabel ??= status.textContent;
    status.textContent = processing ? "正在处理" : status.dataset.completedLabel;
  };
  const stopReplay = (message = "") => {
    playback.stop();
    replayBusy = false;
    replayActive = false;
    cancelReplayFollow();
    scrollArea.classList.remove("is-replaying");
    replayExitButton.hidden = true;
    if (replaySpeedControl) replaySpeedControl.hidden = true;
    clearReplayStatus();
    replayText.forEach((text, node) => { node.textContent = text; });
    document.querySelectorAll("[data-md-source]").forEach((block) => {
      block.innerHTML = cardText(markdownSources.get(block.dataset.mdSource) ?? "");
    });
    completedView.querySelectorAll(".is-streaming").forEach((item) => item.classList.remove("is-streaming"));
    revealedUnits.clear();
    refreshUnitVisibility();
    flow.replay = false;
    flow.dagConfirmed = true;
    flow.current = null;
    flow.reached = new Set([...FLOW_PRE, ...FLOW_STAGES].map((unit) => unit.id));
    renderPhaseStrip();
    syncReplayDocuments();
    syncReplayPackages();
    syncReplayIfcFiles();
    replayPaused = false;
    replayIndex = 0;
    pendingRequirement = null;
    resumeAfterRequirement = null;
    pendingCad = null;
    pendingPlanning = null;
    pendingStage = null;
    resumeAfterPlanning = null;
    resumeAfterStage = null;
    resumeAfterCad = null;
    confirmButton.hidden = true;
    confirmButton.disabled = false;
    document.querySelectorAll(".requirement-live-branch, .cad-live-branch, .planning-live-branch, .stage-live-branch").forEach((item) => item.remove());
    finishReplayActivities();
    completedView.querySelectorAll(".role-run").forEach((run) => setRoleReplayStatus(run, false));
    setRequirementState({ content: finalRequirement, complete: true, clarifications: [] });
    setCadState({ content: finalCad, complete: true, clarifications: [] });
    fixture.stages.filter((stage) => stage.stageCard).forEach((stage) => acceptedStageIds.add(stage.id));
    setTaskTreeState(finalTaskTree);
    fixture.stages.filter((stage) => stage.stageCard).forEach((stage) => setStageFlowPosition(stage.id, "已确认", "用户确认"));

    document.querySelectorAll(".replay-item").forEach((item) => item.classList.remove("is-replay-hidden"));
    completedView.querySelectorAll("[data-preview-for]").forEach((preview) => { preview.hidden = false; });
    restoreRoleReplayStats();
    replayFollow = true;
    document.querySelector(".replay-progress")?.remove();
    setReplayJumpVisibility();
    replayButton.textContent = "↻ 回放";
    replayButton.dataset.playing = "false";
    if (message) showToast(message);
  };

  const closeInspector = () => {
    shell.classList.remove("inspector-open");
    inspector.setAttribute("aria-hidden", "true");
  };

  const setTaskNavigation = (open, restoreFocus = false) => {
    shell.classList.toggle("task-navigation-open", open);
    const button = document.querySelector('[data-action="toggle-task-navigation"]');
    button.setAttribute("aria-expanded", String(open));
    button.setAttribute("aria-label", open ? "关闭任务导航" : "打开任务导航");
    if (open) document.querySelector('.sidebar [data-action="close-task-navigation"]').focus();
    else if (restoreFocus) button.focus();
  };

  const populateNewTaskView = (taskItem) => {
    const end = document.createElement("div");
    end.className = "conversation-end";
    end.setAttribute("aria-hidden", "true");
    const history = document.createElement(taskItem.historyError ? "button" : "p");
    history.className = "text-button task-history";
    if (taskItem.historyError) {
      history.type = "button";
      history.dataset.action = "load-task-history";
      history.dataset.taskId = currentTaskId;
    } else history.setAttribute("role", "status");
    history.textContent = taskItem.historyError ? "更早的消息读取失败，点击重试" : "正在自动加载更早的消息…";
    history.hidden = taskItem.nextBeforeSequence == null;
    const connection = document.createElement("p");
    connection.className = "user-action-hint";
    connection.setAttribute("role", "status");
    connection.hidden = !taskItem.connectionError && !taskItem.snapshotPending;
    connection.textContent = taskItem.connectionError ?? (taskItem.snapshotPending ? "正在读取对话，内容将自动显示…" : "");
    newTaskView.replaceChildren(history, connection, ...liveConversationEntries(taskItem), end);
  };

  const refreshTaskComposer = () => {
    const task = tasks.get(currentTaskId);
    const isNew = currentTaskMode === "new" && Boolean(task);
    submitButton.classList.toggle("composer-icon-action", isNew);
    attachmentButton.hidden = !isNew;
    shell.classList.toggle("is-empty-task", isNew && task.messages.length === 0);
    if (!isNew) {
      composerAttachments.hidden = true;
      composerAttachments.innerHTML = "";
      taskPauseButton.hidden = true;
      submitButton.hidden = false;
      input.placeholder = "可以随时输入补充说明或修改意见…";
      submitButton.textContent = "提交修改意见";
      submitButton.setAttribute("aria-label", "提交修改意见");
      submitButton.title = "提交修改意见";
      return;
    }
    input.placeholder = task.taskType === "component" ? "描述构件，或添加参考图片 / CAD 文件…" : "描述建模要求，或添加图片 / CAD 文件…";
    submitButton.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5m-7 7 7-7 7 7"/></svg>';
    submitButton.setAttribute("aria-label", "发送消息");
    submitButton.title = "发送消息";
    const hasInput = Boolean(input.value.trim() || task.draft.attachments.length);
    submitButton.disabled = !hasInput;
    hint.textContent = "Enter 发送 · Shift + Enter 换行";
    composerAttachments.hidden = !task.draft.attachments.length;
    composerAttachments.innerHTML = task.draft.attachments.map((attachment) => `<div class="attachment-preview">${attachment.isImage ? `<img src="${esc(attachment.url)}" alt="${esc(attachment.file.name)}" />` : '<span aria-hidden="true">▤</span>'}<span>${esc(attachment.file.name)}</span><button type="button" data-action="remove-attachment" data-attachment-id="${esc(attachment.id)}" aria-label="移除 ${esc(attachment.file.name)}">×</button></div>`).join("");
    taskPauseButton.hidden = !chatConnection || !["running", "pause_requested", "paused"].includes(task.state);
    submitButton.hidden = !hasInput && !taskPauseButton.hidden;
    taskPauseButton.disabled = task.controlPending || task.state === "pause_requested";
    const controlLabel = task.controlPending ? (task.state === "paused" ? "正在继续…" : "正在暂停…") : task.state === "paused" ? "继续任务" : task.state === "pause_requested" ? "正在暂停…" : "暂停任务";
    taskPauseButton.innerHTML = task.state === "paused" ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 11 7-11 7z" fill="currentColor"/></svg>' : '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1" fill="currentColor"/></svg>';
    taskPauseButton.setAttribute("aria-label", controlLabel);
    taskPauseButton.title = controlLabel;
    taskPauseButton.dataset.action = task.state === "paused" ? "resume-task" : "pause-task";
  };

  const acceptAttachments = (files) => {
    const task = currentTaskMode === "new" ? tasks.get(currentTaskId) : null;
    if (!task) return;
    for (const file of files) {
      const isCad = /\.(dxf|dwg)$/i.test(file.name);
      const isImage = !isCad && (file.type.startsWith("image/") || /\.(png|jpe?g|webp|gif|bmp|tiff?|avif|heic|svg)$/i.test(file.name));
      if (!isImage && !isCad) {
        showToast("请选择图片、DXF 或 DWG 文件");
        continue;
      }
      task.draft.attachments.push({ id: crypto.randomUUID(), file, isImage, url: URL.createObjectURL(file) });
    }
    refreshTaskComposer();
  };

  const showInputFailure = (task, request, error) => {
    const entry = task.messages.find((message) => message.request?.requestId === request.requestId);
    if (!entry) return;
    entry.element.querySelector(".task-input-error")?.remove();
    const note = document.createElement("p");
    note.className = "task-input-error";
    note.textContent = `发送遇到问题：${error?.message || "请重试"}`;
    const retry = document.createElement("button");
    retry.type = "button";
    retry.dataset.action = "retry-task-input";
    retry.dataset.taskId = request.viewId;
    retry.textContent = "重试";
    note.appendChild(retry);
    entry.element.appendChild(note);
  };

  const connectTaskSender = (task) => {
    if (!chatConnection) return;
    task.sender.connect(async (request) => {
      // P3 owns Task creation, exact uploads, durable input and unknown-outcome recovery.
      const receipt = await chatConnection.sendInput(request);
      const entry = receiveLiveInputReceipt(request.viewId, request.requestId, receipt);
      entry?.element.querySelector(".task-input-error")?.remove();
    });
  };

  const submitTaskInput = () => {
    const task = tasks.get(currentTaskId);
    const text = input.value.trim();
    if (!task || (!text && !task.draft.attachments.length)) return input.focus();
    const attachments = task.draft.attachments;
    const request = { viewId: currentTaskId, taskType: task.taskType, requestId: crypto.randomUUID(), text, attachments: attachments.map((attachment) => attachment.file) };
    const article = document.createElement("article");
    article.className = "message user-message";
    article.dataset.requestId = request.requestId;
    article.innerHTML = `<div class="user-bubble">${esc(text)}${attachments.length ? `<div class="message-attachments">${attachments.map((attachment) => `<a href="${esc(attachment.url)}" download="${esc(attachment.file.name)}">${attachment.isImage ? `<img src="${esc(attachment.url)}" alt="${esc(attachment.file.name)}" />` : `▤ ${esc(attachment.file.name)}`}</a>`).join("")}</div>` : ""}</div>`;
    task.messages.push({ request, attachments, item: { kind: "user", client_request_id: request.requestId, text }, element: article });
    refreshLiveTaskLabel(currentTaskId);
    conversationEnd.before(article);
    task.draft = { text: "", attachments: [] };
    input.value = "";
    input.style.height = "auto";
    refreshTaskComposer();
    task.sender.submit(request);
    scrollArea.scrollTo({ top: scrollArea.scrollHeight, behavior: "smooth" });
    input.focus();
  };

  const requestTaskControl = async (action) => {
    const viewId = currentTaskId;
    const task = tasks.get(viewId);
    if (!task || !chatConnection || task.controlPending) return;
    task.controlPending = true;
    refreshTaskComposer();
    try {
      await chatConnection[action]({ viewId, taskType: task.taskType });
    } catch (error) {
      showToast(error?.message || "操作失败，请重试");
    } finally {
      task.controlPending = false;
      refreshTaskComposer();
    }
  };

  // These objects are display projections. Saved messages and tool results remain server-owned.
  // Requested demo sidebar dates only; never change task/run timestamps or new tasks.
  const demoSidebarDates = {
    task_043651e0f87b4d94935ed5f8d2d92929: "2026-10-01",
    task_86289b08b76446709c67cfed43d94374: "2026-09-29",
    task_8eadb42e7aa64178be635a08512f1164: "2026-09-27",
    task_dcd92bc5e518474099bc4ae123a77bfe: "2026-09-26",
    task_e40e0692633c40ecb37c3b6ad997d71b: "2026-09-25",
  };
  const sortSidebarTasks = () => {
    const section = document.querySelector(".sidebar-section");
    const rows = [...section.querySelectorAll(":scope > .task-row, :scope > .task-row-container")];
    rows.sort((a, b) => (Date.parse(b.dataset.taskDate) || 0) - (Date.parse(a.dataset.taskDate) || 0));
    for (const row of rows) section.appendChild(row);
  };
  const updateTaskDate = (task, serverTask) => {
    const demoDate = demoSidebarDates[serverTask.task_id];
    const value = demoDate ? `${demoDate}T12:00:00` : serverTask.updated_at;
    if (!value || !task.row) return;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return;
    const label = task.row.querySelector("small");
    label.textContent = `${date.getMonth() + 1}月${date.getDate()}号`;
    label.title = demoDate ? `演示日期 · 实际更新时间：${new Date(serverTask.updated_at).toLocaleString("zh-CN")}` : date.toLocaleString("zh-CN");
    if (task.container) task.container.dataset.taskDate = value;
    sortSidebarTasks();
  };
  const taskReadStorageKey = "cad2bim:read-tasks";
  const readTasks = (() => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(taskReadStorageKey) ?? "[]");
      return new Set(Array.isArray(saved) ? saved.filter((id) => typeof id === "string") : []);
    } catch { return new Set(); }
  })();
  const updateTaskRead = (row, key, read) => {
    if (!row) return;
    if (read !== undefined && readTasks.has(key) !== read) {
      if (read) readTasks.add(key);
      else readTasks.delete(key);
      try { window.localStorage.setItem(taskReadStorageKey, JSON.stringify([...readTasks])); } catch { /* Keep in-memory state when storage is unavailable. */ }
    }
    row.classList.toggle("is-read", readTasks.has(key));
  };
  updateTaskRead(document.querySelector('[data-action="open-completed-task"]'), "city-replay");
  const liveDetailIds = new Map();
  const liveModelMounts = new Map();
  const liveResourceKind = (item) => item.kind === "resource" ? item.resource_kind ?? "资料"
    : item.kind === "tool" && item.name === "read_file" ? "Skill"
    : item.kind === "tool" && item.name === "state_resource_read" ? "Resource" : null;
  const liveResourceName = (item) => {
    const args = item.arguments?.request ?? item.arguments ?? {};
    return args.file_path ?? args.uri ?? args.resource_uri ?? args.resource_id ?? item.name ?? "资料";
  };
  let liveTurnSequence = 0;
  const pausedRunMessage = (error) => ({
    model_network_interrupted: "网络连接中断，任务已暂停。可点击继续。",
    model_authentication_failed: "模型认证失败，任务已暂停。请检查模型连接后继续。",
    model_response_failed: "回复生成失败，任务已暂停。可点击继续。",
    model_protocol_error: "模型返回内容异常，任务已暂停。可点击继续。",
    model_call_cancelled: "模型请求中断，任务已暂停。可点击继续。",
    model_request_failed: "模型请求失败，任务已暂停。请检查连接后继续。",
  })[error] || "任务已暂停，可点击继续。";
  const createLiveActivity = (activity) => {
    const value = { ...activity, id: activity.activity_id };
    if (activity.stage === "card") value.label = "正在生成本轮工作记录…";
    if (activity.kind === "compression" || activity.stage === "compression") {
      const element = document.createElement("div");
      element.className = "context-compaction";
      element.setAttribute("role", "status");
      element.innerHTML = `<span aria-hidden="true">⇥</span><span>${esc(activity.label || "正在压缩…")}</span>`;
      value.element = element;
    }
    return value;
  };
  const disposeLiveModels = (root) => {
    for (const [node, mount] of liveModelMounts) if (!root || root.contains(node)) {
      mount.cancelled = true;
      mount.dispose?.();
      liveModelMounts.delete(node);
    }
  };
  const mountLiveModels = (root) => {
    for (const node of root.querySelectorAll("[data-live-model-url]")) {
      if (liveModelMounts.has(node) || node.closest?.("details:not([open])")) continue;
      const mount = { cancelled: false };
      liveModelMounts.set(node, mount);
      import("./library-viewer.js?v=20260912-flat-viewer").then(({ mountViewer }) => {
        if (mount.cancelled || !node.isConnected) return;
        mount.dispose = mountViewer(node, { format: "stl", url: node.dataset.liveModelUrl }, node.dataset.liveModelName);
      }).catch(() => {
        if (!mount.cancelled && node.isConnected) node.querySelector(".resource-model-status").textContent = "三维视图暂不可用";
      });
    }
  };
  const liveConversationEntries = (task) => {
    const pauseReason = task.state === "paused" && task.run?.error ? pausedRunMessage(task.run.error) : "";
    task.turns ??= new Map();
    task.activityGroups ??= new Map();
    const currentGroups = new Set();
    const entries = [];
    let messages = [];
    let anchor = null;
    const displaysFor = (entry) => [...(task.displays?.values() ?? [])].filter((display) => display.item_id === entry.item.item_id);
    const flush = (waitingForFirstMessage = false) => {
      if (!messages.length && !waitingForFirstMessage) return;
      const first = anchor ?? messages[0] ?? task;
      const card = messages.at(-1)?.item.kind === "card" && messages.at(-1).item.status === "succeeded" ? messages.at(-1) : null;
      const currentSegment = waitingForFirstMessage || messages.includes(task.messages.at(-1)) || anchor === task.messages.at(-1) || !task.messages.length;
      const visible = messages.filter((entry) => !entry.element.hidden && entry !== card);
      const last = visible.at(-1);
      // One saved Card closes one turn. Main calls within that turn share one actor
      // header, including calls that initially have no text and only produce tools.
      const replies = visible.filter(({ item }) => item.kind === "assistant" && item.role !== "system" && item.message_phase === "final_answer");
      if (!replies.length && last?.item.kind === "assistant" && last.item.role !== "system" && last.item.message_phase !== "commentary") replies.push(last);
      const actor = { name: task.taskType === "component" ? "Component Agent" : "任务处理", icon: "component" };
      let turn = task.turns.get(first);
      if (!turn) {
        const element = document.createElement("div");
        element.className = "agent-turn message agent-message live-agent-turn";
        element.innerHTML = `<header>${renderRoleIcon(actor)}<div><strong>${esc(actor.name)}</strong></div></header>${renderRoleWork({ id: `live-work-${++liveTurnSequence}`, actor, summary: "工作过程", stats: "" })}<div class="role-final"></div><div class="live-turn-status" role="status" aria-live="polite"></div>`;
        turn = { element, status: element.querySelector(".live-turn-status"), work: element.querySelector(".role-run"), body: element.querySelector(".role-event-stream"), final: element.querySelector(".role-final") };
        if ((currentSegment && !card && ["running", "queued", "pause_requested"].includes(task.state))
          || messages.some((entry) => entry.previewElement && !entry.previewElement.hidden)) turn.work.open = true;
        task.turns.set(first, turn);
        turn.work.addEventListener("toggle", () => { if (turn.work.open) mountLiveModels(turn.work); else disposeLiveModels(turn.work); });
      }
      const process = [];
      const results = [];
      let activity = [];
      const flushActivity = () => {
        if (!activity.length) return;
        const firstTool = activity[0];
        let group = task.activityGroups.get(firstTool);
        if (!group || currentGroups.has(group)) group = [...task.activityGroups.values()].find((candidate) => !currentGroups.has(candidate) && candidate.members.some((entry) => activity.includes(entry)));
        if (!group) {
          const host = document.createElement("div");
          host.innerHTML = renderActivityGroup([], activity.length);
          const element = host.querySelector(".activity-group");
          group = { element, list: element.querySelector(".activity-list"), label: element.querySelector(".activity-label"), status: element.querySelector("summary small") };
        }
        task.activityGroups.set(firstTool, group);
        group.members = [...activity];
        currentGroups.add(group);
        group.element.dataset.mainMessageId = firstTool.item.main_message_id ?? "";
        const reads = activity.filter(({ item }) => liveResourceKind(item)).length;
        group.label.textContent = [reads ? `读取 ${reads} 份资料` : "", activity.length > reads ? `调用 ${activity.length - reads} 个工具` : ""].filter(Boolean).join(" · ");
        const completed = activity.filter(({ item }) => ["succeeded", "failed", "error", "not_executed", "cancelled", "result_received"].includes(item.status)).length;
        group.status.textContent = `完成 ${completed}/${activity.length}${activity.some(({ item }) => ["failed", "error"].includes(item.status)) ? " · 含失败调用" : activity.some(({ item }) => item.status === "interrupted") ? " · 含中断调用" : activity.some(({ item }) => item.status === "running") ? " · 执行中，等待返回" : activity.some(({ item }) => item.status === "queued") ? " · 等待执行" : activity.some(({ item }) => item.status === "result_received") ? " · 正在保存结果" : ""}`;
        group.list.replaceChildren(...activity.map((entry) => entry.element));
        process.push(group.element);
        // Returned images stay visible immediately below their tool group, even
        // when the individual calls are collapsed. Reuse the same image nodes.
        process.push(...activity.filter((entry) => entry.previewElement && !entry.previewElement.hidden).map((entry) => entry.previewElement));
        activity = [];
      };
      for (const entry of messages) {
        if (entry.item.kind === "tool" && !entry.element.hidden) {
          const previous = activity.at(-1)?.item;
          if (previous && !(previous.effect === "READ" && entry.item.effect === "READ" && previous.main_message_id && previous.main_message_id === entry.item.main_message_id)) flushActivity();
          activity.push(entry);
        } else {
          flushActivity();
          if (!replies.includes(entry) && entry !== card && !entry.element.hidden) process.push(entry.element);
        }
        const displays = card ? displaysFor(entry) : [];
        results.push(...displays.map((display) => display.element));
      }
      flushActivity();
      const tools = messages.filter(({ item }) => item.kind === "tool" && !liveResourceKind(item)).length;
      const resources = messages.filter(({ item }) => liveResourceKind(item) && (item.kind === "resource" || item.status === "succeeded")).length;
      const pending = messages.some((entry) => entry.stream && !entry.stream.closed);
      const interrupted = messages.at(-1)?.stream?.failed || ["failed", "cancelled", "paused"].includes(task.state);
      const mainId = task.activity?.main_message_id;
      const activityInTurn = !mainId || messages.some(({ item }) => item.message_id === mainId || item.main_message_id === mainId)
        || !task.messages.some(({ item }) => item.message_id === mainId || item.main_message_id === mainId);
      const running = ["running", "queued", "pause_requested"].includes(task.state);
      const active = currentSegment && !card && !interrupted && running;
      const runtime = active && activityInTurn && task.activity?.kind === "runtime" ? task.activity : null;
      const compressing = active && activityInTurn && task.activity?.element;
      if (compressing) process.push(task.activity.element);
      turn.work.hidden = !process.length;
      const runningTools = messages.some(({ item }) => item.kind === "tool" && item.status === "running");
      const queuedTools = messages.some(({ item }) => item.kind === "tool" && item.status === "queued");
      const receivedTools = messages.some(({ item }) => item.kind === "tool" && item.status === "result_received");
      turn.status.textContent = currentSegment && !card && pauseReason ? pauseReason
        : currentSegment && !card && running && task.connectionError ? "连接已断开，等待网络恢复…"
        : currentSegment && !card && task.state === "queued" ? "已提交，等待执行"
        : runtime ? runtime.label : compressing ? "正在压缩…" : active && runningTools ? "正在调用工具，执行中，等待返回…"
        : active && queuedTools ? "工具提议已保存，等待执行…" : active && receivedTools ? "工具结果已返回，正在保存…"
        : active && pending ? "正在生成回复…" : active && ["running", "queued", "pause_requested"].includes(task.state) ? "正在处理…" : "";
      turn.status.hidden = !turn.status.textContent;
      turn.status.classList.toggle("is-paused", Boolean(currentSegment && !card && pauseReason));
      turn.work.querySelector("summary").innerHTML = `<span class="role-run-title"><strong>工作过程</strong></span><span class="role-run-stats">${esc(`资料 ${resources} · 工具活动 ${tools}`)}</span><i></i>`;
      turn.body.replaceChildren(...process);
      turn.final.replaceChildren(...replies.map((entry) => entry.element), ...(card ? [card.element] : []), ...results);
      turn.final.hidden = !replies.length && !results.length && !card;
      entries.push(turn.element);
      messages = [];
      anchor = null;
    };
    for (const entry of task.messages) {
      if (entry.item.kind === "user") { flush(); entries.push(entry.element); anchor = entry; }
      else {
        messages.push(entry);
        if (entry.item.kind === "card" && entry.item.status === "succeeded") flush();
      }
    }
    flush(Boolean((anchor || task.activity || pauseReason) && (["running", "queued", "pause_requested"].includes(task.state) || pauseReason)));
    for (const [firstTool, group] of task.activityGroups) if (!currentGroups.has(group) || firstTool !== group.members[0]) task.activityGroups.delete(firstTool);
    return entries;
  };
  const liveStatus = (status) => ({ queued: "等待执行", running: "执行中，等待返回", result_received: "结果已返回，正在保存", interrupted: "执行中断", generating: "正在生成回复…", not_executed: "未执行", failed: "返回失败", error: "返回失败", succeeded: "已返回" })[status] ?? status ?? "";
  const liveJson = (value) => `<pre class="tool-json"><code>${esc(JSON.stringify(value, null, 2) ?? "")}</code></pre>`;
  const liveContent = (blocks = []) => list(blocks).map((block) => {
    if (typeof block === "string") return cardText(block);
    if (block.type === "image" || block.type === "image_url") {
      const image = block.content_url;
      return image ? renderPreview({ title: block.filename ?? block.file_name ?? block.title ?? "图片", image, mode: "image" }) : "";
    }
    if (block.type === "text") return cardText(block.text ?? "");
    if (block.type === "resource") return `<p class="detail-note">${esc(block.resource?.uri ?? "")}</p>${block.resource?.text != null ? `<pre class="markdown-source">${esc(block.resource.text)}</pre>` : liveJson(block.resource)}`;
    if (block.type === "resource_link") return `<p class="detail-note">${esc(block.name ?? block.title ?? "资料")} · ${esc(block.uri ?? "")}</p>${block.content_url ? `<a class="inline-link" href="${esc(block.content_url)}">查看保存的资料 ↗</a>` : ""}`;
    if (block.type === "file") {
      const name = block.filename ?? block.file_name ?? block.name ?? "附件";
      if (!block.content_url) return "";
      return `<a class="attachment" href="${esc(block.content_url)}" download="${esc(name)}">▤ ${esc(name)}</a>${/\.stl$/i.test(name) ? `<div class="live-saved-model family-delivery-viewer" data-live-model-url="${esc(block.content_url)}" data-live-model-name="${esc(name)}"><div class="resource-model-fallback" hidden></div><div class="resource-model-canvas"></div><div class="resource-model-toolbar"><span class="resource-model-status" role="status">正在载入三维模型…</span><button type="button" data-view-reset hidden>复位视角</button></div></div>` : ""}`;
    }
    return liveJson(block);
  }).join("");
  const liveItemText = (item) => item.text ?? list(item.content).filter((block) => block?.type === "text").map((block) => block.text).join("\n");
  const refreshLiveTaskLabel = (viewId) => {
    const task = tasks.get(viewId);
    if (!task?.row) return;
    const initial = task.taskType === "component" && task.title === "新建 Component"
      ? task.messages.find(({ item }) => item.kind === "user") : null;
    const text = initial ? liveItemText(initial.item).replace(/\s+/g, " ").trim() : "";
    const characters = Array.from(text);
    const label = text ? characters.slice(0, 32).join("") + (characters.length > 32 ? "…" : "") : task.title;
    task.row.querySelector("strong").textContent = label;
    task.row.title = text ? `首条输入：${text}` : task.title;
    task.deleteButton?.setAttribute("aria-label", `删除任务：${label}`);
  };
  const liveCardPreview = (item) => {
    const text = liveItemText(item);
    const outcome = text.match(/(?:^|\n)##\s+本轮工作与结果[ \t]*\n([\s\S]*?)(?=\n##\s|$)/)?.[1];
    return cardPreview(outcome ?? text.replace(/^#{1,6}[^\n]*\n\s*/, ""), true);
  };
  const liveItemBody = (item) => item.kind === "tool"
    ? `<div class="tool-detail"><section><h5>输入</h5>${liveJson(item.arguments)}</section><section><h5>返回</h5>${item.result !== undefined ? liveJson(item.result) : `<p>${esc(liveStatus(item.status))}</p>`}${liveContent(item.content ?? item.result?.content)}</section>${item.text ? `<section><h5>说明</h5>${cardText(item.text)}</section>` : ""}</div>`
    : item.kind === "compression" ? `<section><h5>对话摘要</h5>${cardText(item.result?.conversation_summary ?? "")}</section><section><h5>工具链摘要</h5>${cardText(item.result?.tool_chain_summary ?? "")}</section>`
    : `${cardText(liveItemText(item))}${liveContent(list(item.content).filter((block) => block?.type !== "text"))}${item.result !== undefined ? liveJson(item.result) : ""}`;
  const liveDetailId = (viewId, item) => {
    const key = `${viewId}:${item.item_id}`;
    if (!liveDetailIds.has(key)) liveDetailIds.set(key, `live-detail-${liveDetailIds.size + 1}`);
    return liveDetailIds.get(key);
  };
  const setLiveItemContent = (viewId, entry, html) => {
    const article = entry.element;
    if (article.innerHTML === html) return;
    const focused = document.activeElement;
    if (viewId === currentTaskId && article.contains(focused) && focused?.dataset?.detail) tasks.get(viewId).restoreDetailFocus = focused.dataset.detail;
    const disclosures = [...article.querySelectorAll("details")].map((element) => element.open);
    disposeLiveModels(article);
    article.innerHTML = html;
    article.querySelectorAll("details").forEach((element, index) => { element.open = disclosures[index] ?? element.open; });
  };
  const renderLiveItem = (viewId, entry) => {
    const item = entry.item;
    const article = entry.element;
    article.dataset.itemId = item.item_id ?? "";
    article.dataset.messageId = item.message_id ?? "";
    article.hidden = false;
    if (item.kind === "user") {
      article.className = "message user-message";
      setLiveItemContent(viewId, entry, `<div class="user-bubble">${esc(liveItemText(item))}${liveContent(list(item.content).filter((block) => block?.type !== "text"))}</div>`);
      return;
    }
    if (item.kind === "assistant") {
      article.className = `${item.message_phase === "commentary" ? "public-commentary " : ""}message agent-message`;
      article.classList.toggle("is-streaming", Boolean(entry.stream && !entry.stream.closed));
      const error = entry.stream?.failed ? `<p class="task-generation-error" role="status">回复生成中断，以下内容尚未完成。${entry.stream.error ? `<br>${esc(entry.stream.error)}` : ""}</p>` : "";
      const text = !entry.stream?.failed && entry.stream && (entry.stream.waiting || !entry.stream.text) ? "" : cardText(liveItemText(item));
      const label = item.role === "system" ? "执行提示" : tasks.get(viewId)?.taskType === "component" ? "Component Agent" : item.role ?? "Component";
      const content = liveContent(list(item.content).filter((block) => block?.type !== "text"));
      article.hidden = !error && !text.trim() && !content;
      setLiveItemContent(viewId, entry, article.hidden ? "" : `${item.role === "system" ? `<header>${renderRoleIcon({ name: label, icon: "default" })}<div><strong>${esc(label)}</strong></div></header>` : ""}<div class="role-card-markdown">${error}${text}${content}</div>`);
      return;
    }
    const detailId = entry.detailId ?? liveDetailId(viewId, item);
    entry.detailId = detailId;
    liveDetailIds.set(`${viewId}:${item.item_id}`, detailId);
    const previous = detailEntries.get(detailId);
    registerDetail(detailId, item.name ?? (item.kind === "compression" ? "上下文管理" : "本轮工作与交接"), item.kind === "tool" ? "MCP 工具" : item.kind === "resource" ? item.resource_kind ?? "资料" : item.kind === "compression" ? "上下文管理" : "角色交接", () => liveItemBody(entry.item));
    Object.assign(detailEntries.get(detailId), { live: { viewId, itemId: item.item_id }, loaded: (previous?.live?.itemId === item.item_id && previous.loaded) || !item.detail_available });
    const detail = document.getElementById("inspectorDetail");
    if (viewId === currentTaskId && !detail.hidden && detail.dataset.detailId === detailId) {
      disposeLiveModels(detail);
      detail.innerHTML = liveItemBody(item);
      mountLiveModels(detail);
    }
    article.className = item.kind === "card" ? "" : "live-activity";
    const previews = list(item.content ?? item.result?.content).filter((block) => ["image", "image_url"].includes(block?.type));
    if (item.kind === "tool" && (previews.length || entry.previewElement)) {
      entry.previewElement ??= document.createElement("div");
      entry.previewElement.className = "live-tool-previews";
      entry.previewElement.dataset.callId = item.call_id ?? item.item_id;
      entry.previewElement.hidden = !previews.length;
      const previewHtml = `<span class="tool-preview-caption">${esc(item.name ?? "工具")} · 返回图片</span><div class="tool-preview-row" tabindex="0" aria-label="本次调用的预览图片">${liveContent(previews)}</div>`;
      if (entry.previewElement.innerHTML !== previewHtml) entry.previewElement.innerHTML = previewHtml;
    }
    const resourceKind = liveResourceKind(item);
    setLiveItemContent(viewId, entry, item.kind === "tool"
      ? `<button class="activity-item tool-call ${resourceKind ? "resource-load" : ""} ${["failed", "error", "interrupted"].includes(item.status) ? "tool-failed" : ""}" type="button" data-action="open-detail-panel" data-detail="${detailId}"><span class="activity-mark" aria-hidden="true">${resourceKind ? "▤" : "◇"}</span><span class="activity-name">${resourceKind ? `<span class="resource-kind">${resourceKind}</span> ${esc(liveResourceName(item))}<small>${esc(item.name)}</small>` : `<code>${esc(item.name ?? "工具")}</code>`}</span><span class="activity-status">${esc(liveStatus(item.status))} ↗</span></button>`
      : item.kind === "resource" ? renderActivityGroup([`<button class="activity-item resource-load" type="button" data-action="open-detail-panel" data-detail="${detailId}"><span class="activity-mark" aria-hidden="true">▤</span><span class="activity-name">${esc(item.name ?? "资料")}</span><span class="activity-status">${esc(item.resource_kind ?? "资料")} ↗</span></button>`], 0, 1)
      : item.kind === "card" ? renderWorkCard({ id: item.item_id, role: tasks.get(viewId)?.taskType === "component" ? "Component Agent" : item.role ?? "任务处理", preview: liveCardPreview(item), body: `<div class="role-card-markdown">${cardText(liveItemText(item))}</div><button class="document-chip" type="button" data-action="open-detail-panel" data-detail="${detailId}">查看保存的交接记录 ↗</button>` })
      : `<button class="context-compaction" type="button" data-action="open-detail-panel" data-detail="${detailId}"><span aria-hidden="true">⇥</span><span>${esc(item.status === "succeeded" ? "上下文已压缩" : "正在压缩上下文…")}</span><span aria-hidden="true">↗</span></button>`);
  };
  const sameLiveItem = (entry, item) => entry.item?.item_id === item.item_id ||
    (item.kind === "tool" && item.call_id && entry.item?.kind === "tool" && entry.item.call_id === item.call_id) ||
    (item.kind === "assistant" && item.message_id && entry.item?.message_id === item.message_id) ||
    (item.kind === "user" && entry.item?.kind === "user" && ((item.client_request_id && entry.item.client_request_id === item.client_request_id) || (item.feedback_id && entry.item.feedback_id === item.feedback_id)));
  const upsertLiveItem = (viewId, item) => {
    const task = tasks.get(viewId);
    if (!task || !item?.item_id) return;
    let entry = task.messages.find((candidate) => sameLiveItem(candidate, item));
    if (item.kind === "tool" && Number.isSafeInteger(item.status_sequence) && Number.isSafeInteger(entry?.item.status_sequence)
      && item.status_sequence < entry.item.status_sequence) return entry;
    if (!entry) {
      entry = { element: document.createElement("article"), item };
      task.messages.push(entry);
    }
    entry.item = { ...entry.item, ...item };
    if (item.kind === "assistant" && item.message_id) {
      const stream = task.streams?.get(item.message_id);
      if (stream) stream.closed = true;
      delete entry.item.transient;
      entry.stream = null;
    }
    renderLiveItem(viewId, entry);
    return entry;
  };
  const receiveLiveInputReceipt = (viewId, requestId, receipt) => {
    const task = tasks.get(viewId);
    const entry = task?.messages.find((message) => message.request?.requestId === requestId);
    if (!entry || !receipt?.feedback_id) return entry;
    entry.item.feedback_id = receipt.feedback_id;
    const duplicates = task.messages.filter((message) => message !== entry && message.item?.kind === "user" && message.item.feedback_id === receipt.feedback_id);
    if (duplicates.length) {
      const follow = viewId === currentTaskId && isNearConversationBottom();
      for (const duplicate of duplicates) {
        entry.item = { ...entry.item, ...duplicate.item };
        disposeLiveModels(duplicate.element);
        task.messages.splice(task.messages.indexOf(duplicate), 1);
      }
      renderLiveItem(viewId, entry);
      task.messages.sort((a, b) => (a.item.sequence ?? Infinity) - (b.item.sequence ?? Infinity));
      refreshLiveTask(viewId, follow);
    }
    return entry;
  };
  let demoInspectorContent = "";
  const refreshLiveInspector = (viewId) => {
    if (viewId !== currentTaskId || currentTaskMode !== "new") return;
    const items = tasks.get(viewId).messages.filter((entry) => ["tool", "resource", "compression", "card"].includes(entry.item?.kind));
    const rows = items.map(({ item }) => `<button class="operation-row" type="button" data-operation-kind="${item.effect === "WRITE" ? "write" : item.effect === "READ" ? "read" : ""}" data-action="open-detail-panel" data-detail="${liveDetailId(viewId, item)}"><span class="operation-effect">${item.kind === "tool" ? "◇" : "▤"}</span><span><strong>${esc(item.name ?? (item.kind === "compression" ? "上下文管理" : "本轮工作与交接"))}</strong><small>${esc(item.kind === "resource" ? item.resource_kind ?? "资料" : liveStatus(item.status))}</small></span><em>›</em></button>`).join("");
    const counts = { all: items.length, write: items.filter(({ item }) => item.effect === "WRITE").length, read: items.filter(({ item }) => item.effect === "READ").length };
    inspectorContent.innerHTML = `<section><div class="operation-filters" role="group" aria-label="操作记录筛选">${["all", "write", "read"].map((filter) => `<button type="button" data-operation-filter="${filter}">${filter === "all" ? "全部" : filter === "write" ? "写入" : "查询"} ${counts[filter]}</button>`).join("")}</div><div class="operation-groups"><details class="history-stage operation-stage" open data-operation-stage="live" data-total="${counts.all}" data-write="${counts.write}" data-read="${counts.read}"><summary><span>工具与资料</span><small data-operation-count></small></summary><div>${rows}</div></details></div><p class="operation-empty" hidden>此筛选下没有记录。</p></section>`;
    filterMcpOperations(currentOperationFilter);
    inspectorButton.hidden = false;
  };
  const refreshLiveTask = (viewId, follow = false) => {
    refreshLiveTaskLabel(viewId);
    if (viewId !== currentTaskId || currentTaskMode !== "new") return;
    const task = tasks.get(viewId);
    const focused = document.activeElement;
    const detailFocus = task.restoreDetailFocus ?? (newTaskView.contains(focused) ? focused?.dataset?.detail : null);
    const conversationFocus = newTaskView.contains(focused) ? focused : null;
    populateNewTaskView(task);
    delete task.restoreDetailFocus;
    if (detailFocus) newTaskView.querySelector(`[data-detail="${detailFocus}"]`)?.focus({ preventScroll: true });
    else if (conversationFocus?.isConnected) conversationFocus.focus({ preventScroll: true });
    mountLiveModels(newTaskView);
    conversationEnd = newTaskView.querySelector(".conversation-end");
    refreshTaskComposer();
    refreshLiveInspector(viewId);
    if (follow) scrollArea.scrollTo({ top: scrollArea.scrollHeight, behavior: "auto" });
    replayJump.hidden = follow || isNearConversationBottom();
  };
  const interruptLiveReply = (viewId, entry, error = "") => {
    Object.assign(entry.stream, { failed: true, closed: true, waiting: false, error });
    entry.item.status = "failed";
    renderLiveItem(viewId, entry);
  };
  const updateLiveTaskState = (viewId, run) => {
    const task = tasks.get(viewId);
    if (!task) return;
    task.run = typeof run === "object" ? run : task.run;
    const status = typeof run === "string" ? run : run?.status;
    const normalizedStatus = status?.toLowerCase();
    task.state = normalizedStatus === "paused" ? "paused" : run?.pause_requested && ["running", "waiting_user"].includes(normalizedStatus) ? "pause_requested" : normalizedStatus ?? task.state;
    if (["failed", "cancelled"].includes(normalizedStatus)) {
      for (const entry of task.messages) if (entry.item?.transient && entry.stream && !entry.stream.failed) interruptLiveReply(viewId, entry);
    }
    refreshLiveTask(viewId);
    if (viewId === currentTaskId) refreshTaskComposer();
  };
  const loadTaskHistory = async (viewId) => {
    const task = tasks.get(viewId);
    if (!task || task.historyPending || task.nextBeforeSequence == null || !chatConnection?.loadHistory || viewId !== currentTaskId) return;
    task.historyPending = true;
    task.historyError = null;
    refreshLiveTask(viewId);
    try {
      while (task.nextBeforeSequence != null && viewId === currentTaskId && !task.connectionError) {
        const before = task.nextBeforeSequence;
        const page = await chatConnection.loadHistory(viewId, before);
        const previousHeight = scrollArea.scrollHeight;
        const previousTop = scrollArea.scrollTop;
        for (const item of page.items ?? []) upsertLiveItem(viewId, item);
        task.messages.sort((a, b) => (a.item.sequence ?? Infinity) - (b.item.sequence ?? Infinity));
        task.nextBeforeSequence = page.next_before_sequence;
        if (task.nextBeforeSequence != null && task.nextBeforeSequence >= before) throw new Error("消息分页未前进，请重新连接");
        refreshLiveTask(viewId);
        if (viewId === currentTaskId) scrollArea.scrollTop = previousTop + scrollArea.scrollHeight - previousHeight;
      }
    } catch (error) {
      task.historyError = error?.message ?? "更早的消息读取失败，请重试";
    } finally {
      task.historyPending = false;
      refreshLiveTask(viewId);
    }
  };
  window.CAD2BIM_TASK_CHAT = {
    loadingTask(viewId, pending) {
      const task = tasks.get(viewId);
      if (!task) return;
      task.snapshotPending = pending;
      refreshLiveTask(viewId);
    },
    connectionError(error, viewId = currentTaskId) {
      const task = tasks.get(viewId);
      if (!task) { showToast(error?.message ?? "暂时无法读取对话，请稍后重试"); return; }
      task.connectionError = `消息连接已断开，正在重新读取。当前显示为已读取的记录。${error?.message ? ` ${error.message}` : ""}`;
      refreshLiveTask(viewId);
    },
    connectionRestored(viewId) {
      const task = tasks.get(viewId);
      if (!task) return;
      task.connectionError = null;
      refreshLiveTask(viewId);
      void loadTaskHistory(viewId);
    },
    connect(connection) {
      chatConnection = connection;
      tasks.forEach(connectTaskSender);
      refreshTaskComposer();
    },
    updateTaskState: updateLiveTaskState,
    updateTaskInfo(viewId, serverTask) {
      const task = tasks.get(viewId);
      if (!task) return;
      const wasRead = readTasks.has(task.serverTaskId ?? viewId);
      task.serverTaskId = serverTask.task_id;
      updateTaskDate(task, serverTask);
      updateTaskRead(task.row, task.serverTaskId ?? viewId, wasRead || (currentTaskMode === "new" && currentTaskId === viewId) ? true : undefined);
      if (!serverTask.title) return;
      task.title = serverTask.title;
      refreshLiveTaskLabel(viewId);
      if (currentTaskId === viewId) {
        if (shell.classList.contains("library-mode")) workspaceTitle = task.title;
        else topbarTitle.textContent = task.title;
      }
    },
    receiveInputReceipt: receiveLiveInputReceipt,
    receiveActivity(viewId, activity) {
      const task = tasks.get(viewId);
      if (!task || !["runtime", "compression"].includes(activity.kind) || !activity.activity_id || !["start", "end", "error"].includes(activity.phase)) return;
      if (activity.kind === "runtime") {
        if (!Number.isSafeInteger(activity.event_sequence) || activity.event_sequence <= (task.activitySequence ?? 0)) return;
        task.activitySequence = activity.event_sequence;
      } else if (task.activity?.kind === "runtime") return;
      const follow = viewId === currentTaskId && isNearConversationBottom();
      if (activity.phase === "start") {
        if (task.activity?.id === activity.activity_id) return;
        task.activity = createLiveActivity(activity);
      } else if (["end", "error"].includes(activity.phase) && task.activity?.id === activity.activity_id) task.activity = null;
      else return;
      refreshLiveTask(viewId, follow);
    },
    clearActivity(viewId) {
      const task = tasks.get(viewId);
      if (!task?.activity) return;
      task.activity = null;
      refreshLiveTask(viewId);
    },
    receiveItem(viewId, item) {
      const follow = viewId === currentTaskId && isNearConversationBottom();
      const task = tasks.get(viewId);
      const newReply = item.kind === "assistant" && !item.transient && !task?.messages.some((entry) => sameLiveItem(entry, item));
      const entry = upsertLiveItem(viewId, item);
      if (entry && newReply) updateTaskRead(task.row, task.serverTaskId ?? viewId, currentTaskMode === "new" && currentTaskId === viewId);
      if (entry) refreshLiveTask(viewId, follow);
    },
    receiveDisplay(viewId, value) {
      const task = tasks.get(viewId);
      if (!task) return;
      task.displays ??= new Map();
      let display = task.displays.get(value.key);
      if (!display) { display = { element: document.createElement("article") }; task.displays.set(value.key, display); }
      disposeLiveModels(display.element);
      Object.assign(display, value);
      const node = display.element;
      node.className = "family-delivery live-geometry-display";
      node.dataset.sourceKind = value.source.source_kind;
      node.dataset.sourceRef = value.source.source_ref;
      const sourceLabel = { geometry_resource_version: "几何", family_standard_model: "产品族" }[value.source.source_kind];
      const loadingHeader = `<header><p class="eyebrow">${esc(sourceLabel)} · 展示加载</p><h4>${esc(value.title || sourceLabel)}</h4></header>`;
      const resource = `${value.source.source_kind === "family_standard_model" ? "family" : "geometry"}/${encodeURIComponent(value.source.source_ref)}`;
      const libraryFooter = `<button type="button" class="primary-link" data-action="open-library" data-resource="${esc(resource)}">在几何与产品族中查看 ↗</button><span>${value.source.source_kind === "family_standard_model" ? "查看参数、接口与依赖几何" : "查看参数、接口与代码"}</span>`;
      if (value.status === "ready") {
        const saved = value.display;
        node.dataset.geometrySha256 = saved.geometry_sha256;
        node.innerHTML = `<header><p class="eyebrow">${esc(sourceLabel)}</p><h4>${esc(saved.title ?? "保存的构件")}</h4></header>${renderModelViewer({ url: saved.stl.content_url, name: saved.title ?? "保存的构件" })}<footer>${libraryFooter}${value.display_note ? `<span>${esc(value.display_note)}</span>` : ""}${saved.display_quality_warnings?.length ? `<span>显示网格提示：${saved.display_quality_warnings.map((warning) => esc(warning.message)).join("；")}</span>` : ""}</footer>`;
      } else if (value.status === "unavailable") {
        node.innerHTML = `<header><p class="eyebrow">${esc(sourceLabel)}</p><h4>${esc(value.title || "保存的构件")}</h4></header><p class="detail-note">${esc(value.message || "此资源暂无可展示的预览。")}</p><footer>${libraryFooter}</footer>`;
      } else if (value.status === "failed") {
        const stageLabel = { info: "构件信息读取", read: "三维展示读取", prepare: "三维展示准备" }[value.stage] || "展示读取";
        node.innerHTML = `${loadingHeader}<div class="display-load-state"><p class="task-input-error">${esc(stageLabel)}失败：${esc(value.error)}</p><p class="detail-note">展示未加载完成；已保存的模型与本轮回复仍会保留。</p><button class="text-button" type="button">重新加载此构件</button></div>`;
        node.querySelector("button").addEventListener("click", () => { void chatConnection?.retryDisplay(viewId, display); });
      } else node.innerHTML = `${loadingHeader}<div class="display-load-state" role="status" aria-live="polite"><p class="live-turn-status">${esc(value.message || "正在读取构件信息…")}</p><p class="detail-note">本轮回复已完成，正在加载此构件的三维展示。</p></div>`;
      refreshLiveTask(viewId, viewId === currentTaskId && isNearConversationBottom());
    },
    receiveSnapshot(viewId, snapshot) {
      const task = tasks.get(viewId);
      if (!task) return;
      task.activity = snapshot.activity?.phase === "start" ? createLiveActivity(snapshot.activity) : null;
      task.activitySequence = snapshot.event_sequence ?? snapshot.activity?.event_sequence ?? 0;
      const follow = viewId === currentTaskId && isNearConversationBottom();
      for (const entry of task.messages) if (entry.stream && !entry.stream.closed) {
        entry.stream.waiting = true;
        renderLiveItem(viewId, entry);
      }
      for (const item of snapshot.items ?? []) upsertLiveItem(viewId, item);
      task.messages.sort((a, b) => (a.item.sequence ?? Infinity) - (b.item.sequence ?? Infinity));
      task.eventSequence = snapshot.event_sequence;
      task.nextBeforeSequence = snapshot.next_before_sequence;
      updateLiveTaskState(viewId, snapshot.run);
      refreshLiveTask(viewId, follow);
      void loadTaskHistory(viewId);
    },
    receiveTextDelta(viewId, delta) {
      const task = tasks.get(viewId);
      if (!task || !delta?.message_id || !delta.call_id || !["start", "delta", "retry", "error"].includes(delta.phase)) return;
      if (delta.phase === "delta" && !Number.isInteger(delta.delta_index)) return;
      task.streams ??= new Map();
      let stream = task.streams.get(delta.message_id);
      if (task.messages.some(({ item }) => item?.kind === "assistant" && item.message_id === delta.message_id && !item.transient)) return;
      const created = !stream;
      if (!stream) {
        for (const previous of task.messages) if (previous.stream && !previous.stream.closed) {
          previous.stream.closed = true;
          previous.stream.waiting = true;
          renderLiveItem(viewId, previous);
        }
        stream = { callId: delta.call_id, lastIndex: -1, text: "", waiting: delta.phase === "delta" && delta.delta_index !== 0, closed: false };
        task.streams.set(delta.message_id, stream);
      }
      if ((stream.closed && delta.phase !== "retry") || stream.callId !== delta.call_id || (delta.phase === "start" && !created)) return;
      if (delta.phase === "retry") {
        if (!Number.isSafeInteger(delta.retry_attempt) || delta.retry_attempt <= (stream.retryAttempt ?? 0)) return;
        Object.assign(stream, { retryAttempt: delta.retry_attempt, lastIndex: -1, text: "", waiting: false, closed: false, failed: false, error: null });
      }
      if (delta.phase === "delta" && delta.delta_index <= stream.lastIndex) return;
      const follow = viewId === currentTaskId && isNearConversationBottom();
      if (delta.phase === "delta") {
        if (delta.incomplete || delta.delta_index !== stream.lastIndex + 1) stream.waiting = true;
        stream.lastIndex = delta.delta_index;
        if (!stream.waiting) stream.text += delta.text ?? "";
      }
      let entry = task.messages.find(({ item }) => item?.message_id === delta.message_id);
      if (!entry) {
        entry = { element: document.createElement("article"), item: { item_id: `text-${delta.message_id}`, kind: "assistant", message_id: delta.message_id, call_id: delta.call_id, transient: true } };
        task.messages.push(entry);
      }
      entry.stream = stream;
      entry.item.text = stream.text;
      if (delta.phase === "retry") entry.item.status = "generating";
      if (delta.phase === "error") interruptLiveReply(viewId, entry, delta.error ?? "");
      else renderLiveItem(viewId, entry);
      refreshLiveTask(viewId, follow);
    },
    restoreTask(serverTask, run) {
      const serverId = serverTask.task_id;
      if (!serverId) return;
      const viewId = [...tasks].find(([, task]) => task.serverTaskId === serverId)?.[0] ?? `server-task-${serverId}`;
      const task = tasks.get(viewId) ?? addChatTask(viewId, serverTask.title ?? "Component", serverTask.task_type ?? serverTask.metadata?.task_type ?? "component");
      window.CAD2BIM_TASK_CHAT.updateTaskInfo(viewId, serverTask);
      updateLiveTaskState(viewId, run ?? serverTask.current_run ?? serverTask.latest_run);
      return viewId;
    },
    selectTask(viewId) {
      if (tasks.has(viewId)) switchTaskView("new", viewId);
    },
  };

  let workspaceTitle = "";
  let workspaceScrollTop = 0;
  const openWorkspace = () => {
    if (!shell.classList.contains("library-mode")) return;
    shell.classList.remove("library-mode");
    topbarTitle.textContent = workspaceTitle;
    scrollArea.scrollTop = workspaceScrollTop;
    setPrimaryNavigation("workspace");
    if (currentTaskMode === "new") mountLiveModels(newTaskView);
  };
  const openLibrary = (resource = "") => {
    disposeLiveModels();
    if (!shell.classList.contains("library-mode")) {
      workspaceTitle = topbarTitle.textContent;
      workspaceScrollTop = scrollArea.scrollTop;
    }
    if (replayButton.dataset.playing === "true" && !replayPaused) toggleReplay();
    const frame = document.getElementById("libraryFrame");
    const url = new URL(frame.dataset.src, window.location.href);
    const task = currentTaskMode === "new" ? tasks.get(currentTaskId) : null;
    url.searchParams.set("source", task?.taskType === "component" ? "live" : "city");
    if (task?.taskType === "component" && task.serverTaskId && task.run?.run_id) {
      url.searchParams.set("task_id", task.serverTaskId);
      url.searchParams.set("run_id", task.run.run_id);
    }
    url.hash = resource;
    // Re-enter the requested target even when the iframe navigated to another
    // resource or list internally; its src attribute still has the old target.
    frame.src = url.href;
    shell.classList.add("library-mode");
    topbarTitle.textContent = "几何与产品族";
    setPrimaryNavigation("library");
  };

  const switchTaskView = (mode, taskId = "") => {
    const returningToCurrentTask = shell.classList.contains("library-mode") && currentTaskMode === mode &&
      (mode === "completed" || document.querySelector(".task-row.is-active")?.dataset.taskId === taskId);
    openWorkspace();
    if (returningToCurrentTask) return;
    disposeLiveModels();
    const previousTask = tasks.get(currentTaskId);
    if (currentTaskMode === "new" && previousTask) {
      previousTask.draft.text = input.value;
      previousTask.scrollTop = scrollArea.scrollTop;
    }
    if (replayButton.dataset.playing === "true") stopReplay();
    currentTaskMode = mode;
    currentTaskId = mode === "new" ? taskId : "";
    const isCompleted = mode === "completed";
    completedView.hidden = !isCompleted;
    newTaskView.hidden = isCompleted;
    shell.classList.toggle("new-task-mode", !isCompleted);
    replayButton.hidden = !isCompleted;
    inspectorButton.hidden = false;
    confirmButton.hidden = true;
    input.value = "";
    input.style.height = "auto";
    submitButton.disabled = true;
    hint.textContent = "⌘ / Ctrl + Enter 提交";
    if (isCompleted) {
      if (demoInspectorContent) inspectorContent.innerHTML = demoInspectorContent;
      showInspectorOverview();
      conversationEnd = completedView.querySelector(".conversation-end");
      topbarTitle.textContent = fixture.task.title;
    } else {
      const taskItem = tasks.get(taskId);
      if (!taskItem) return switchTaskView("completed");
      populateNewTaskView(taskItem);
      mountLiveModels(newTaskView);
      conversationEnd = newTaskView.querySelector(".conversation-end");
      topbarTitle.textContent = taskItem.title;
      input.value = taskItem.draft.text;
      if (!demoInspectorContent) demoInspectorContent = inspectorContent.innerHTML;
      showInspectorOverview();
      refreshLiveInspector(taskId);
      closeInspector();
    }
    refreshTaskComposer();
    document.querySelectorAll(".task-row").forEach((button) => {
      const selected = isCompleted ? button.dataset.action === "open-completed-task" : button.dataset.taskId === taskId;
      button.classList.toggle("is-active", selected);
      if (selected) updateTaskRead(button, isCompleted ? "city-replay" : tasks.get(taskId)?.serverTaskId ?? taskId, true);
    });
    setPrimaryNavigation("workspace");
    scrollArea.scrollTo({ top: isCompleted ? 0 : tasks.get(taskId)?.scrollTop ?? scrollArea.scrollHeight, behavior: "auto" });
    replayJump.hidden = true;
    window.dispatchEvent(new CustomEvent("cad2bim:task-selected", { detail: { viewId: currentTaskId } }));
    if (!isCompleted) void loadTaskHistory(taskId);
  };

  const openParentDetails = (element) => {
    let parent = element.parentElement;
    while (parent && parent !== scrollArea) {
      if (parent.tagName === "DETAILS") parent.open = true;
      parent = parent.parentElement;
    }
  };

  const highlightTarget = (target) => {
    document.querySelectorAll(".trace-highlight").forEach((item) => item.classList.remove("trace-highlight"));
    target.classList.add("trace-highlight");
    window.setTimeout(() => target.classList.remove("trace-highlight"), 1900);
  };

  const scrollToTarget = (targetId) => {
    if (currentTaskMode !== "completed") switchTaskView("completed");
    const target = document.getElementById(targetId);
    if (!target) return showToast("该内容尚未生成");
    openParentDetails(target);
    if (target.tagName === "DETAILS") target.open = true;
    const block = target.id.startsWith("section-") || target.classList.contains("stage-card") || target.dataset.stageExecution ? "start" : "center";
    target.scrollIntoView({ behavior: "smooth", block });
    highlightTarget(target);
    const section = target.closest("[data-section]")?.dataset.section;
    if (section) activeSection = section;
    document.querySelectorAll(".stage-link").forEach((button) => button.classList.toggle("is-current", button.dataset.target === `section-${section}`));
  };

  const appendUserMessage = (text, subtext = "") => {
    const article = document.createElement("article");
    article.className = "message user-message";
    const bubble = document.createElement("div");
    bubble.className = "user-bubble";
    bubble.textContent = text;
    if (subtext) {
      const small = document.createElement("small");
      small.textContent = subtext;
      bubble.appendChild(small);
    }
    article.appendChild(bubble);
    conversationEnd.before(article);
    scrollArea.scrollTo({ top: scrollArea.scrollHeight, behavior: "smooth" });
    return bubble;
  };

  const appendAgentMessage = (text) => {
    const article = document.createElement("article");
    article.className = "message agent-message";
    article.innerHTML = `<header>${renderRoleIcon({ name: "General Modeler", icon: "general", tone: "general" })}<div><strong>General Modeler</strong><time>刚刚</time></div></header><p></p>`;
    article.querySelector("p").textContent = text;
    conversationEnd.before(article);
    scrollArea.scrollTo({ top: scrollArea.scrollHeight, behavior: "smooth" });
  };

  const handleRequirementResponse = (response) => {
    if (!pendingRequirement) return false;
    if (typeof replayBusy !== "undefined" && replayBusy) {
      // Freeze the last visible checkpoint before accepting new feedback.
      playback.stop(); replayBusy = false; replayPaused = true; clearReplayStatus();
      pendingRequirement.state = { ...pendingRequirement.state, content: displayedRequirement, clarifications: displayedClarifications,
        requirement_md: publishedDocuments.get("doc-requirement") ?? null, complete: false };
    }
    const { state, anchor } = pendingRequirement;
    const route = window.routeRequirementResponse(state, response);
    if (route.action === "wait_for_input") return true;
    if (route.action === "assess_clarifications") {
      hint.textContent = "本轮问题尚未评估，暂不能进入 CAD";
      confirmButton.hidden = true;
      return true;
    }
    const branch = document.createElement("div");
    branch.className = "requirement-live-branch";
    const text = response.kind === "confirm" ? "确认并继续" : route.feedback;
    branch.innerHTML = `<article class="message user-message"><div class="user-bubble">${esc(text)}</div></article>`;
    anchor.after(branch);
    input.value = ""; input.style.height = "auto";
    submitButton.disabled = true; confirmButton.disabled = false;
    const previousCard = state.roleCard;
    if (previousCard && route.action !== "continue") branch.insertAdjacentHTML("beforeend", renderEntryResources(window.requirementEntryResources(state, previousCard)));
    const entered = route.action === "continue" ? null : window.beginRequirementEntry(state, { ...response, defaults: route.defaults ?? [] });
    if (route.action === "adopt_defaults") {
      pendingRequirement = { state: entered, anchor: branch };
      setRequirementState(entered);
      const result = window.requirementDemoUpdate(state, response);
      if (!result) {
        branch.insertAdjacentHTML("beforeend", '<p class="detail-note">已保留本次确认，等待 Requirement Agent 根据当前需求和上轮问题完成修订。</p>');
        confirmButton.hidden = true;
        hint.textContent = "等待本轮需求与问题评估结果；CAD 尚未开始";
        return true;
      }
      const animate = typeof replayActive !== "undefined" && replayActive;
      let next;
      try {
        next = window.applyRequirementsUpdate(entered, result.requirementsUpdate);
        pendingRequirement.state = next;
        if (!animate) setRequirementState(next);
        next = window.applyClarificationsUpdate(next, result.clarificationsUpdate);
        pendingRequirement.state = next;
        if (!animate) setRequirementState(next);
        next = window.completeRequirementEntry(next, result.markdown);
      } catch (error) {
        branch.insertAdjacentHTML("beforeend", '<p class="detail-note">本轮更新未通过校验，已保留有效成果和上轮问题，等待重新处理。</p>');
        confirmButton.hidden = true;
        hint.textContent = "需求修订尚未完成，CAD 尚未开始";
        return true;
      }
      const id = `requirement-live-${next.revision}`;
      next.roleCard = { id: `${id}-card`, role: "Requirement Agent", round: (previousCard?.round ?? 1) + 1, source: "Requirement Agent · 本页确认后的完成轮次", input: "上一轮需求与已展示的默认方案、本次用户确认", outcome: `已将 ${route.defaults.length} 项已展示的默认方案写入对应需求，并保留采用来源。\n\n` + route.defaults.map((item) => item.answer).join("\n\n") + "\n\n本轮完成需求修订，相关设计假设与工程未知继续按需求文档保留；CAD 调查、IFC 建模及检查仍属后续工作。", handoff: "请查看修订需求，确认后进入 CAD；如提交修改意见，仍由 Requirement Agent 修订受影响内容。" };
      const publish = () => {
        branch.insertAdjacentHTML("beforeend", `<article class="role-final"><header class="requirement-branch-role">${renderRoleIcon({ name: "Requirement Agent", icon: "requirements" })}<strong>Requirement Agent</strong></header>${renderRoleCard(next.roleCard)}${renderDocumentChange({ actor: "Requirement Agent", state: next, documentId: "doc-requirement" }, `${id}-document`)}</article>${renderRequirementFinal({ requirementState: next, dynamic: true }, { id })}`);
        setRequirementState(next);
        pendingRequirement = { state: next, anchor: branch };
        // Another gate: the recorded confirmation is filled in automatically, the button stays greyed.
        replayPaused = true;
        confirmButton.hidden = false;
        confirmButton.disabled = true;
        replayButton.textContent = "▶ 继续示例";
        hint.textContent = "默认方案已写入本页需求，确认修订结果后进入 CAD；回放中「确认并继续」已自动填入 · 点击「继续示例」继续";
      };
      if (animate) animateRequirementRevision(branch, next, publish);
      else publish();
    } else if (route.action === "continue") {
      if (state.roleCard) {
        for (const sectionId of ["cad", "planning"]) {
          const section = fixture.sections.find((item) => item.id === sectionId);
          const entry = section.events.find((event) => event.entryResources);
          if (sectionId === "cad") entry.entryResources = window.requirementEntryResources(state, state.roleCard);
          else entry.entryResources = entry.entryResources.map((resource) => resource.id === "requirement-md" ? { ...resource, source: state.roleCard.source, content: window.renderRequirementMarkdown(state.content) } : resource.id === "requirements" ? { ...resource, source: state.roleCard.source, content: JSON.parse(JSON.stringify(state.content)) } : resource);
          const run = roleRuns.find((item) => item.stageId === sectionId);
          const panel = document.getElementById(run.id)?.querySelector(".entry-injection");
          if (panel) panel.outerHTML = renderEntryResources(entry.entryResources);
        }
      }
      pendingRequirement = null; pendingCad = null; confirmButton.hidden = true;
      hint.textContent = "需求已确认，继续查看 CAD 阶段";
      resumeAfterRequirement?.();
    } else if (route.action === "review_feedback") {
      pendingRequirement = { state: entered, anchor: branch };
      setRequirementState(entered);
      // Free text is a new role input, never a keyword-driven or silently defaulted answer.
      branch.insertAdjacentHTML("beforeend", `<article class="role-final"><header class="requirement-branch-role">${renderRoleIcon({ name: "Requirement Agent", icon: "requirements" })}<strong>Requirement Agent · 下一轮</strong></header><p>已保留你的文字反馈、已有需求和上轮问题。本轮问题尚未评估，需要结合这段补充重新整理需求和问题。</p><p class="detail-note">不会把任意文字回复自动当成对默认方案的批准。</p></article>`);
      confirmButton.hidden = true;
      replayButton.dataset.playing = "false"; replayButton.textContent = "↻ 重新回放";
      hint.textContent = "反馈已作为下一轮需求输入保留；CAD 尚未开始";
    } else {
      pendingRequirement = { state: entered, anchor: branch };
      setRequirementState(entered);
      confirmButton.hidden = true;
      hint.textContent = "需要先补齐问题的默认方案，不能直接进入 CAD";
    }
    branch.scrollIntoView({ behavior: "smooth", block: "start" });
    return true;
  };

  const handleCadResponse = (response) => {
    if (!pendingCad) return false;
    const { state, anchor } = pendingCad;
    const route = window.routeCadResponse(state, response);
    if (route.action === "wait_for_input") return true;
    if (route.action === "assess_clarifications" || route.action === "prepare_defaults") {
      confirmButton.hidden = true;
      hint.textContent = "本轮 CAD 问题尚未完成评估，暂不能进入整体规划";
      return true;
    }
    const branch = document.createElement("div");
    branch.className = "cad-live-branch";
    branch.innerHTML = '<article class="message user-message"><div class="user-bubble">' + esc(response.kind === "confirm" ? "确认并继续" : route.feedback) + '</div></article>';
    anchor.after(branch);
    input.value = ""; input.style.height = "auto"; submitButton.disabled = true;
    if (route.action === "continue") {
      pendingCad = null; confirmButton.hidden = true;
      hint.textContent = "CAD 成果已确认，继续整体规划";
      resumeAfterCad?.("planning");
      return true;
    }
    const entered = window.beginCadEntry(state, { ...response, defaults: route.defaults ?? [] });
    pendingCad = { state: entered, anchor: branch };
    setCadState(entered);
    if (window.cadDemoCanContinue(state, response)) {
      pendingCad = null; confirmButton.hidden = true;
      hint.textContent = "按已展示方案进入 CAD 第二轮，继续整理与回读";
      resumeAfterCad?.("second", entered);
    } else {
      branch.insertAdjacentHTML("beforeend", '<article class="role-final"><header class="requirement-branch-role">' + renderRoleIcon({ name: "CAD Agent", icon: "cad" }) + '<strong>CAD Agent · 下一轮</strong></header><p>已保留你的反馈、当前 CAD 成果和上轮问题。本轮需要重新理解修改范围，再维护成果和澄清。</p><p class="detail-note">自定义反馈不会自动被当成对默认方案的批准。</p></article>');
      confirmButton.hidden = true;
      hint.textContent = "等待 CAD Agent 重新评估反馈；整体规划尚未开始";
    }
    return true;
  };

  const handlePlanningResponse = (response) => {
    if (!pendingPlanning) return false;
    const { state, anchor } = pendingPlanning;
    const route = window.IFC_PLAN.routeResponse(state, response);
    if (route.action === 'wait_for_input') return true;
    if (['assess_clarifications', 'prepare_defaults', 'complete_plan'].includes(route.action)) {
      confirmButton.hidden = true;
      hint.textContent = '整体规划尚需补充或重新评估，暂不能进入 Stage Planner';
      return true;
    }
    const branch = document.createElement('div');
    branch.className = 'planning-live-branch';
    branch.innerHTML = '<article class="message user-message"><div class="user-bubble">' + esc(response.kind === 'confirm' ? '确认并继续' : response.text) + '</div></article>';
    anchor.after(branch);
    input.value = ''; input.style.height = 'auto'; submitButton.disabled = true;
    confirmButton.hidden = true;
    if (route.action === 'continue') {
      pendingPlanning = null;
      branch.insertAdjacentHTML('beforeend', '<p class="detail-note">整体计划已确认，进入初始化阶段的流程。</p>');
      hint.textContent = '初始化 · Stage Planner';
      if (typeof resumeAfterPlanning === 'function') resumeAfterPlanning();
    } else {
      const entered = window.IFC_PLAN.beginEntry(state, { ...response, defaults: route.defaults ?? [] });
      pendingPlanning = { state: entered, anchor: branch };
      setTaskTreeState(entered);
      branch.insertAdjacentHTML('beforeend', '<article class="role-final"><header><strong>Global Planner · 下一轮</strong></header><p>已保留本次回复、完整任务树、已有依赖和上轮问题。本轮澄清尚未评估，需要结合回复重新调查并维护计划。</p><p class="detail-note">新的计划与 MD 尚未生成。</p></article>');
      hint.textContent = '反馈已交回 Global Planner；等待重新评估';
    }
    return true;
  };

  const handleStageResponse = (response) => {
    if (!pendingStage) return false;
    const checkpoint = pendingStage;
    if (checkpoint.blocked) { hint.textContent = '当前反馈需由新一轮 Agent 处理，后续保持暂停'; return true; }
    if (response.kind === 'confirm') {
      const route = window.IFC_PLAN.routeStage(checkpoint.state, checkpoint.stageId, checkpoint.boundary === 'plan' ? 'plan_confirmed' : 'result_confirmed');
      if (!['Modeler', 'Component Agent', 'Stage Card'].includes(route.next)) {
        hint.textContent = route.reason; confirmButton.hidden = true;
        return true;
      }
    }
    const branch = document.createElement('div');
    branch.className = 'stage-live-branch';
    branch.innerHTML = '<article class="message user-message"><div class="user-bubble">' + esc(response.kind === 'confirm' ? '确认并继续' : response.text) + '</div></article>';
    checkpoint.anchor.after(branch);
    input.value = ''; input.style.height = 'auto'; submitButton.disabled = true; confirmButton.hidden = true;
    if (response.kind === 'confirm') {
      pendingStage = null;
      const stage = fixture.stages.find((item) => item.id === checkpoint.stageId);
      hint.textContent = checkpoint.boundary === 'plan' ? stage.shortTitle + ' · ' + (checkpoint.state.component_plan.needs_component ? 'Component Agent' : 'Modeler') : stage.shortTitle + '已确认，继续阶段交接';
      resumeAfterStage?.();
    } else {
      const target = checkpoint.boundary === 'plan' ? 'Stage Planner' : 'Modeler';
      pendingStage = { ...checkpoint, blocked: true, anchor: branch };
      branch.insertAdjacentHTML('beforeend', '<p class="detail-note">修改意见已保留，应交回 ' + target + ' 继续处理。后续流程等待修订完成。</p>');
      hint.textContent = '反馈已保留 · 等待 ' + target;
      replayButton.textContent = '示例已暂停';
    }
    return true;
  };

  const submitFeedback = () => {
    if (currentTaskMode === "new") return submitTaskInput();
    const text = input.value.trim();
    if (!text) return input.focus();
    if (handleRequirementResponse({ kind: "feedback", text })) return;
    if (handleCadResponse({ kind: "feedback", text })) return;
    if (handlePlanningResponse({ kind: "feedback", text })) return;
    if (handleStageResponse({ kind: "feedback", text })) return;
    if (replayButton.dataset.playing === "true") stopReplay("已停止回放并加入新消息");
    appendUserMessage(text, "已加入本页反馈草稿");
    input.value = "";
    input.style.height = "auto";
    submitButton.disabled = true;
    confirmButton.hidden = true;
    hint.textContent = "反馈已保留在本页；等待本轮修改处理";

  };

  const confirm = () => {
    if (currentTaskMode !== "completed") return;
    if (input.value.trim()) {
      hint.textContent = "请先提交或清空当前修改意见";
      return input.focus();
    }
    if (handleRequirementResponse({ kind: "confirm" })) return;
    if (handleCadResponse({ kind: "confirm" })) return;
    if (handlePlanningResponse({ kind: "confirm" })) return;
    if (handleStageResponse({ kind: "confirm" })) return;
    if (replayButton.dataset.playing === "true") stopReplay("已停止回放并加入确认");
    appendUserMessage("确认并继续");
    confirmButton.hidden = true;
    hint.textContent = "已确认，仍可随时输入新的修改意见";
  };

  const requirementMarkdownBody = () => {
    if (!displayedRequirement) return '<p class="detail-note">尚未提取需求。</p>';
    const markdown = replayActive ? publishedDocuments.get("doc-requirement") : window.renderRequirementMarkdown(displayedRequirement);
    if (!markdown) return '<p class="detail-note">需求文档尚未形成；当前需求列表仍在整理。</p>';
    const rendered = window.PROCESS_CONTENT.requirementViews.find((view) => view.markdown === markdown);
    const body = rendered?.html ?? `<h1>建模需求</h1>${renderRequirementRows(displayedRequirement)}`;
    return `${markdown === requirementMarkdown ? '<a class="inline-link" href="./requirement.md" download="需求提取.md">下载 Markdown</a>' : '<p class="detail-note">当前已提取的内容</p>'}<article class="process-markdown">${body}</article>`;
  };
  const setRequirementState = (state) => {
    displayedRequirement = state?.content ?? null;
    requirementComplete = Boolean(state?.complete);
    displayedClarifications = state?.clarifications ?? null;
    const host = document.getElementById("liveRequirementContent");
    if (!host) return;
    const expanded = new Set([...host.querySelectorAll(".requirement-row[open]")].map((row) => row.querySelector("strong").textContent));
    const scrollTop = inspectorContent.scrollTop;
    host.innerHTML = renderLiveRequirement(displayedRequirement);
    host.querySelectorAll(".requirement-row").forEach((row) => { row.open = expanded.has(row.querySelector("strong").textContent); });
    inspectorContent.scrollTop = scrollTop;
    const detail = document.getElementById("inspectorDetail");
    if (detail.dataset.detailId === "requirement-markdown") detail.innerHTML = requirementMarkdownBody();
  };
  const cadMarkdownBody = () => {
    if (!displayedCad) return '<p class="detail-note">等待 CAD 图纸调查。</p>';
    const markdown = replayActive ? publishedDocuments.get("cad-result") : window.renderCadMarkdown(displayedCad);
    if (!markdown) return '<p class="detail-note">CAD 文档尚未形成；当前调查结果仍在整理。</p>';
    const view = window.PROCESS_CONTENT.cadViews.find((item) => item.markdown === markdown);
    const download = markdown === window.renderCadMarkdown(finalCad) ? '<a class="inline-link" href="./content/cad-result.md" download="CAD 调查.md">下载 Markdown</a>' : '';
    return download + '<article class="process-markdown">' + (view?.html ?? renderCadRows(displayedCad)) + '</article>';
  };
  const syncReplayPackages = () => {
    document.querySelectorAll("[data-package-alias]").forEach((item) => { item.hidden = replayActive && !visiblePackageAliases.has(item.dataset.packageAlias); });
  };
  const setCadState = (state) => {
    displayedCad = state?.content ?? null;
    cadComplete = Boolean(state?.complete);
    displayedCadClarifications = state?.clarifications ?? null;
    // Package publication is append-only and independent of this editable CAD result.
    syncReplayPackages();
    const host = document.getElementById("liveCadContent");
    if (!host) return;
    const expanded = new Set([...host.querySelectorAll(".cad-row[open]")].map((row) => row.dataset.cadRow));
    const scrollTop = inspectorContent.scrollTop;
    host.innerHTML = renderLiveCad(displayedCad);
    host.querySelectorAll(".cad-row").forEach((row) => { row.open = expanded.has(row.dataset.cadRow); });
    inspectorContent.scrollTop = scrollTop;
    const detail = document.getElementById("inspectorDetail");
    if (detail.dataset.detailId === "cad-markdown") detail.innerHTML = cadMarkdownBody();
  };
  const applyStructuredUpdate = (update) => {
    if (update?.state.kind === "requirement") setRequirementState(update.state);
    if (update?.state.kind === "cad") {
      const firstUpdate = !displayedCad;
      setCadState(update.state);
      if (firstUpdate) selectInspectorTab("cad");
    }
    if (update?.state.kind === "task_tree") {
      const firstUpdate = !displayedTaskTree;
      setTaskTreeState(update.state);
      if (firstUpdate) selectInspectorTab("tasks");
    }
  };
  const selectInspectorTab = (selected) => {
    inspectorContent.querySelectorAll("[data-inspector-tab]").forEach((button) => button.classList.toggle("is-active", button.dataset.inspectorTab === selected));
    inspectorContent.querySelectorAll("[data-inspector-panel]").forEach((panel) => {
      const active = panel.dataset.inspectorPanel === selected;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });
  };
  const showInspectorOverview = () => {
    if (currentTaskMode === "new") disposeLiveModels(document.getElementById("inspectorDetail"));
    document.getElementById("inspectorDetail").hidden = true;
    inspectorContent.hidden = false;
    document.getElementById("inspectorKind").textContent = "";
    document.getElementById("inspectorTitle").textContent = "";
    shell.classList.remove("detail-open");
  };
  const openDetailPanel = (detailId) => {
    if (detailId === "requirement-markdown") registerDetail(detailId, "需求提取.md", "需求成果", requirementMarkdownBody());
    if (detailId === "cad-markdown") registerDetail(detailId, "CAD 调查.md", "CAD 调查成果", cadMarkdownBody());
    const entry = detailEntries.get(detailId);
    if (!entry) return showToast("未找到这条记录");
    const detail = document.getElementById("inspectorDetail");
    const title = document.getElementById("inspectorTitle");
    title.textContent = entry.title;
    document.getElementById("inspectorClose").setAttribute("aria-label", "返回侧栏列表");
    document.getElementById("inspectorKind").textContent = entry.kind;
    if (entry.live) disposeLiveModels(detail);
    detail.innerHTML = typeof entry.body === "function" ? entry.body() : entry.body;
    detail.dataset.detailId = detailId;
    detail.hidden = false;
    detail.scrollTop = 0;
    inspectorContent.hidden = true;
    shell.classList.add("inspector-open", "detail-open");
    inspector.setAttribute("aria-hidden", "false");
    title.focus({ preventScroll: true });
    if (entry.live) mountLiveModels(detail);
    if (entry.live && !entry.loaded && chatConnection?.getItem) {
      entry.loaded = true;
      void chatConnection.getItem(entry.live.viewId, entry.live.itemId).then((item) => {
        if (!item) return;
        upsertLiveItem(entry.live.viewId, item);
        refreshLiveTask(entry.live.viewId);
        if (currentTaskId === entry.live.viewId && detail.dataset.detailId === detailId && shell.classList.contains("detail-open")) openDetailPanel(detailId);
      }).catch((error) => {
        entry.loaded = false;
        if (currentTaskId === entry.live.viewId && detail.dataset.detailId === detailId) detail.innerHTML = `<p class="task-input-error">${esc(error?.message ?? "详情读取失败，请重新打开")}</p>`;
      });
    }
  };
  const dismissInspector = () => {
    if (shell.classList.contains("detail-open")) return showInspectorOverview();
    closeInspector();
  };
  const toggleInspector = () => {
    if (currentTaskMode === "new") refreshLiveInspector(currentTaskId);
    if (shell.classList.contains("detail-open")) {
      showInspectorOverview();
      shell.classList.add("inspector-open");
      inspector.setAttribute("aria-hidden", "false");
      return;
    }
    const open = !shell.classList.contains("inspector-open");
    shell.classList.toggle("inspector-open", open);
    inspector.setAttribute("aria-hidden", String(!open));
  };

  const openDialog = (dialog) => {
    if (!dialog) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  };
  const closeDialog = (dialog) => {
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  };

  const setViewerContentMode = (mode, title) => {
    const imageMode = mode === "image";
    modelDialog.dataset.contentMode = imageMode ? "image" : "model";
    modelDialog.querySelector("header .eyebrow").textContent = imageMode ? "图片查看" : "模型查看";
    modelDialog.querySelector(".viewer-caption span").textContent = imageMode ? "图片" : "IFC 4.3 · 局部模型";
    modelDialog.querySelector('[data-action="close-dialog"]').setAttribute("aria-label", imageMode ? "关闭图片查看" : "关闭模型查看");
    const image = modelDialog.querySelector(".viewer-canvas > img");
    image.alt = imageMode ? title : "城市燃气管网模型视图";
    image.style.objectFit = imageMode ? "contain" : "";
    for (const selector of [".viewer-mode", ".port-layer", ".system-legend"]) {
      const node = modelDialog.querySelector(selector);
      node.hidden = imageMode;
      node.style.display = imageMode ? "none" : "";
    }
  };
  const resetViewer = () => {
    viewerScale = 1;
    viewerRotation = 0;
    modelDialog.querySelector(".viewer-canvas > img").style.transform = "scale(1) rotate(0deg)";
    modelDialog.classList.remove("show-ports", "show-systems");
    modelDialog.querySelectorAll("[data-view-mode]").forEach((button) => {
      const selected = button.dataset.viewMode === "model";
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  };

  const updateViewerTransform = () => {
    const image = modelDialog.querySelector(".viewer-canvas > img");
    image.style.transform = `scale(${viewerScale}) rotate(${viewerRotation}deg)`;
    image.style.transition = "transform .18s ease";
  };

  const renderDocumentSection = (section) => {
    if (Array.isArray(section)) return `<section><h3>${esc(section[0])}</h3><p>${esc(section[1])}</p></section>`;
    const paragraphs = list(section.paragraphs ?? section.body).map((paragraph) => `<p>${esc(paragraph)}</p>`).join("");
    const metrics = list(section.metrics).length ? `<dl class="document-metrics">${section.metrics.map(([value, label]) => `<div><dt>${esc(value)}</dt><dd>${esc(label)}</dd></div>`).join("")}</dl>` : "";
    const bullets = list(section.bullets).length ? `<ul class="document-list">${section.bullets.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : "";
    const table = section.table ? `<div class="document-table-wrap"><table><thead><tr>${section.table.columns.map((column) => `<th>${esc(column)}</th>`).join("")}</tr></thead><tbody>${section.table.rows.map((row) => `<tr>${row.map((cell) => `<td>${esc(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : "";
    const callout = section.callout ? `<aside class="document-callout">${esc(section.callout)}</aside>` : "";
    return `<section><h3>${esc(section.title)}</h3>${paragraphs}${metrics}${bullets}${table}${callout}</section>`;
  };

  const renderDocument = (documentId) => {
    if (documentId === "doc-requirement") return openDetailPanel("requirement-markdown");
    if (["doc-cad", "cad-result"].includes(documentId)) return openDetailPanel("cad-markdown");
    if (replayActive && documentId === "doc-ifc-plan" && !publishedDocuments.has(documentId)) return showToast("整体规划文档尚未形成");
    const stageCard = stageCards.get(documentId);
    if (stageCard) return openDetailPanel(registerDetail(`document-${documentId}`, stageCard.title, "阶段总结", renderStageCardDoc(stageCard)));
    const roleCard = roleCards.get(documentId);
    if (roleCard) return openDetailPanel(registerDetail(`document-${documentId}`, `${roleCard.role} · 第 ${roleCard.round} 轮工作与交接`, "角色交接", renderRoleCard(roleCard)));
    const full = processDocument(documentId);
    if (full) return openDetailPanel(registerDetail(`document-${documentId}`, full.title, full.kind, processDocumentBody(full)));
    const documentData = documents[documentId];
    if (!documentData) return showToast("过程文件不存在");
    const meta = list(documentData.meta).length ? `<dl class="document-meta">${documentData.meta.map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>` : "";
    const body = `${documentData.path ? `<a class="inline-link" href="${esc(documentData.path)}" download>下载 Markdown</a>` : ""}<p class="document-stage">${esc([documentData.stage, visibleStatus(documentData.status)].filter(Boolean).join(" · "))}</p>${documentData.summary ? `<p class="document-lead">${esc(documentData.summary)}</p>` : ""}${meta}${documentData.sections.map(renderDocumentSection).join("")}`;
    openDetailPanel(registerDetail(`document-${documentId}`, documentData.title, "文档", body));
  };

  const valueForCell = (value) => value == null ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value);
  const renderDataPayload = (payloadIndex = 0) => {
    if (!activeDataProduct) return;
    const payloads = list(activeDataProduct.payloads);
    const payload = payloads[payloadIndex] ?? payloads[0];
    const host = document.getElementById("dataDialogContent");
    if (!payload) {
      host.innerHTML = `<div class="data-empty"><p>该数据成果只保存了概要信息。</p></div>`;
      return;
    }
    const columns = list(payload.columns);
    const rows = list(payload.rows);
    const inventory = list(activeDataProduct.payloadInventory);
    const packageMeta = [activeDataProduct.alias, activeDataProduct.sequence ? `序号 ${activeDataProduct.sequence}` : "", activeDataProduct.operation].filter(Boolean).join(" · ");
    const inventoryTable = inventory.length ? `<section class="data-section"><h3>包内数据域</h3><div class="data-table-wrap"><table><thead><tr><th>数据域</th><th>行数</th></tr></thead><tbody>${inventory.map(([name, rowCount]) => `<tr><td><code>${esc(name)}</code></td><td>${esc(rowCount)}</td></tr>`).join("")}</tbody></table></div></section>` : "";
    const fieldBody = columns.length
      ? columns.map((column) => `<tr><td><code>${esc(column.name)}</code></td><td>${esc(column.type)}</td><td>${esc(column.description)}</td></tr>`).join("")
      : `<tr><td colspan="3">该数据域目前只提供目录信息，尚无可展示的字段与样例。</td></tr>`;
    const previewTable = rows.length && columns.length
      ? `<div class="data-table-wrap data-preview-table"><table><thead><tr>${columns.map((column) => `<th>${esc(column.name)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map((column) => `<td>${esc(valueForCell(row[column.name]))}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
      : `<p class="data-preview-empty">此处只展示目录行数；样例预览未载入对话。</p>`;
    host.innerHTML = `<header class="data-summary"><p>${esc(activeDataProduct.summary)}</p>${payload.evidenceNote ? `<p class="detail-note">${esc(payload.evidenceNote)}</p>` : ""}${packageMeta ? `<span>${esc(packageMeta)}</span>` : ""}<dl><div><dt>${esc(payload.rowCount ?? (rows.length || "未知"))}</dt><dd>当前数据域行数</dd></div><div><dt>${columns.length}</dt><dd>预览字段</dd></div><div><dt>${esc(payload.format ?? "Parquet")}</dt><dd>格式</dd></div></dl></header>${inventoryTable}<nav class="payload-tabs" aria-label="数据内容">${payloads.map((item, index) => `<button type="button" class="${index === payloadIndex ? "is-active" : ""}" data-payload-index="${index}"><strong>${esc(item.displayName ?? item.name)}</strong><small>${esc(item.rowCount ?? (list(item.rows).length || "未知"))} 行</small></button>`).join("")}</nav><section class="data-section"><h3>字段说明</h3><div class="data-table-wrap"><table><thead><tr><th>字段</th><th>类型</th><th>业务含义</th></tr></thead><tbody>${fieldBody}</tbody></table></div></section><section class="data-section"><div class="data-section-heading"><h3>样例行</h3><span>仅显示有界预览</span></div>${previewTable}</section>${activeDataProduct.lineage ? `<section class="data-section"><h3>生成记录</h3><p>${esc(activeDataProduct.lineage)}</p></section>` : ""}`;
  };

  const openDataProduct = (productId) => {
    activeDataProduct = fixture.dataProducts.find((product) => product.id === productId);
    if (!activeDataProduct) return showToast("数据成果不存在");
    document.getElementById("dataDialogTitle").textContent = activeDataProduct.title;
    renderDataPayload(0);
    openDialog(dataDialog);
  };

  const addChatTask = (id, title, taskType) => {
    const taskItem = { title, taskType, messages: [], draft: { text: "", attachments: [] }, state: "idle", controlPending: false };
    taskItem.sender = window.CAD2BIM_TASK_INPUT.createSender((request, error) => showInputFailure(taskItem, request, error));
    tasks.set(id, taskItem);
    connectTaskSender(taskItem);
    const row = document.createElement("button");
    row.className = "task-row";
    row.type = "button";
    row.dataset.action = "open-new-task";
    row.dataset.taskId = id;
    const createdDate = new Date();
    row.innerHTML = `<span class="task-dot"></span><span><strong></strong><small>${createdDate.getMonth() + 1}月${createdDate.getDate()}号</small></span>`;
    row.querySelector("strong").textContent = title;
    const container = document.createElement("div");
    container.className = "task-row-container";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "task-delete icon-button";
    remove.dataset.action = "delete-task";
    remove.dataset.taskId = id;
    remove.setAttribute("aria-label", `删除任务：${title}`);
    remove.title = "删除任务";
    remove.textContent = "×";
    container.appendChild(row);
    container.appendChild(remove);
    document.querySelector(".sidebar-section").appendChild(container);
    taskItem.row = row;
    updateTaskRead(row, id);
    taskItem.container = container;
    container.dataset.taskDate = createdDate.toISOString();
    sortSidebarTasks();
    taskItem.deleteButton = remove;
    return taskItem;
  };
  const taskDeleteDialog = document.getElementById("taskDeleteDialog");
  let deletingTaskId = null;
  const closeTaskDelete = () => {
    if (tasks.get(deletingTaskId)?.deleting) return;
    closeDialog(taskDeleteDialog);
    deletingTaskId = null;
  };
  taskDeleteDialog.addEventListener("cancel", (event) => {
    if (tasks.get(deletingTaskId)?.deleting) event.preventDefault();
  });
  const deleteChatTask = (viewId) => {
    const task = tasks.get(viewId);
    if (!task || task.deleting) return;
    if (task.sender.isSending) return showToast("输入仍在提交，请提交结束后再删除任务");
    deletingTaskId = viewId;
    document.getElementById("taskDeleteName").textContent = task.title;
    document.getElementById("taskDeleteError").hidden = true;
    openDialog(taskDeleteDialog);
    taskDeleteDialog.querySelector(".task-delete-cancel").focus();
  };
  const confirmTaskDelete = async () => {
    const viewId = deletingTaskId;
    const task = tasks.get(viewId);
    if (!task || task.deleting) return;
    const failure = document.getElementById("taskDeleteError");
    if (task.sender.isSending) {
      failure.textContent = "输入仍在提交，请提交结束后再删除任务。";
      failure.hidden = false;
      return;
    }
    task.deleting = true;
    task.deleteButton.disabled = true;
    taskDeleteDialog.setAttribute("aria-busy", "true");
    taskDeleteDialog.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    document.getElementById("taskDeleteConfirm").textContent = "正在删除…";
    failure.hidden = true;
    try {
      if (task.serverTaskId && !chatConnection) throw new Error("连接尚未就绪，请稍后重试");
      await chatConnection?.deleteTask(viewId);
      task.sender.dispose();
      for (const attachment of task.draft.attachments) URL.revokeObjectURL(attachment.url);
      for (const [key, detailId] of liveDetailIds) if (key.startsWith(`${viewId}:`)) {
        liveDetailIds.delete(key);
        detailEntries.delete(detailId);
      }
      tasks.delete(viewId);
      task.container.remove();
      closeDialog(taskDeleteDialog);
      deletingTaskId = null;
      if (currentTaskId === viewId) createTask("component");
      else if (tasks.get(currentTaskId)) tasks.get(currentTaskId).row.focus();
      showToast("任务已删除");
    } catch (error) {
      const message = {
        "Pause the running task and wait for it to stop before deleting it.": "请先暂停任务，等待运行停止后再删除。",
        "This task has an unresolved write. Reconcile its original Operation before deleting it.": "该任务还有结果未确认的写入，请先核实原操作结果，再删除任务。",
        "This task is changing. Wait for the current action to finish and try deleting it again.": "任务状态正在更新，请稍后重试删除。",
      }[error.message];
      failure.textContent = message || error.message || "删除未完成，请重试";
      failure.hidden = false;
    } finally {
      task.deleting = false;
      task.deleteButton.disabled = false;
      taskDeleteDialog.setAttribute("aria-busy", "false");
      taskDeleteDialog.querySelectorAll("button").forEach((button) => { button.disabled = false; });
      document.getElementById("taskDeleteConfirm").textContent = "删除任务";
    }
  };
  const createTask = (taskType = "cad2bim") => {
    const id = `task-${crypto.randomUUID()}`;
    const title = taskType === "component" ? "新建 Component" : "新建 CAD2BIM";
    addChatTask(id, title, taskType);
    switchTaskView("new", id);
    // P3 consumes this UI identity and creates the actual authorized backend Task.
    window.dispatchEvent(new CustomEvent("cad2bim:task-created", { detail: { viewId: id, taskType } }));
    input.focus();
  };


  let currentReplayStatus = null;
  const clearReplayStatus = () => { currentReplayStatus?.remove(); currentReplayStatus = null; };
  const replayScrollArea = () => typeof scrollArea !== "undefined" ? scrollArea : document.getElementById("conversationScroll");
  const replayJumpButton = () => typeof replayJump !== "undefined" ? replayJump : document.getElementById("replayJump");
  const isNearConversationBottom = (threshold = 96) => {
    const area = replayScrollArea();
    const metrics = [area?.scrollHeight, area?.scrollTop, area?.clientHeight].map(Number);
    if (metrics.some((value) => !Number.isFinite(value))) return true;
    return metrics[0] - metrics[1] - metrics[2] <= threshold;
  };
  const setReplayJumpVisibility = () => {
    const button = replayJumpButton();
    if (button) button.hidden = !(replayActive && !replayFollow);
  };
  let followFrame = null;
  let lastFollowTime = null;
  let touchY = null;
  const cancelReplayFollow = () => {
    if (followFrame !== null) window.cancelAnimationFrame(followFrame);
    followFrame = null;
    lastFollowTime = null;
    replayScrollArea()?.classList.remove("is-auto-following");
  };
  const detachReplayFollow = () => {
    if (!replayActive) return;
    replayFollow = false;
    cancelReplayFollow();
    setReplayJumpVisibility();
  };
  const scheduleReplayFollow = () => {
    const area = replayScrollArea();
    if (!replayActive || !replayFollow) return setReplayJumpVisibility();
    area.classList.add("is-auto-following");
    if (followFrame !== null) return;
    const advance = (time) => {
      followFrame = null;
      if (!replayActive || !replayFollow) return;
      const target = Math.max(0, area.scrollHeight - area.clientHeight);
      const delta = target - area.scrollTop;
      const elapsed = lastFollowTime === null ? 16 : Math.min(50, time - lastFollowTime);
      lastFollowTime = time;
      const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      const next = reduced || Math.abs(delta) < 1 ? target : area.scrollTop + delta * (1 - Math.exp(-elapsed / 75));
      area.scrollTo({ top: next, behavior: "instant" });
      if (Math.abs(target - area.scrollTop) >= 1) followFrame = window.requestAnimationFrame(advance);
      else lastFollowTime = null;
    };
    followFrame = window.requestAnimationFrame(advance);
    setReplayJumpVisibility();
  };
  replayScrollArea()?.addEventListener?.("wheel", (event) => {
    if (event.deltaY < 0) detachReplayFollow();
  }, { passive: true });
  replayScrollArea()?.addEventListener?.("touchstart", (event) => {
    touchY = event.touches[0]?.clientY ?? null;
  }, { passive: true });
  replayScrollArea()?.addEventListener?.("touchmove", (event) => {
    const y = event.touches[0]?.clientY;
    if (touchY !== null && y > touchY) detachReplayFollow();
    touchY = y ?? null;
  }, { passive: true });
  replayScrollArea()?.addEventListener?.("keydown", (event) => {
    if (event.target.closest?.("input, textarea, select, [contenteditable]")) return;
    if (["ArrowUp", "PageUp", "Home"].includes(event.key) || (event.key === " " && event.shiftKey)) detachReplayFollow();
  });
  replayScrollArea()?.addEventListener?.("pointerdown", (event) => {
    const area = replayScrollArea();
    const bounds = area.getBoundingClientRect();
    if (event.clientX >= bounds.right - (area.offsetWidth - area.clientWidth)) detachReplayFollow();
  });
  replayJumpButton()?.addEventListener?.("click", () => {
    if (currentTaskMode === "new") {
      scrollArea.scrollTo({ top: scrollArea.scrollHeight, behavior: "smooth" });
      replayJump.hidden = true;
      return;
    }
    replayFollow = true;
    scheduleReplayFollow();
    setReplayJumpVisibility();
  });

  const formatRoleReplayStats = (loadCount, toolCount) => `资料 ${loadCount} · 工具活动 ${toolCount}`;
  const roleStatsFor = (run) => run?.querySelector?.(".role-run-stats");
  const setRoleReplayStats = (stats, loadCount, toolCount) => {
    if (!stats) return;
    stats.dataset.replayLoadCount = String(loadCount);
    stats.dataset.replayToolCount = String(toolCount);
    stats.textContent = formatRoleReplayStats(loadCount, toolCount);
  };
  const updateRoleReplayStat = (run, kind) => {
    const stats = roleStatsFor(run);
    if (!stats) return;
    const key = kind === "tool" ? "replayToolCount" : "replayLoadCount";
    const finalKey = kind === "tool" ? "finalToolCount" : "finalLoadCount";
    const current = Number(stats.dataset[key]) || 0;
    const final = Number(stats.dataset[finalKey]);
    const next = Number.isFinite(final) ? Math.min(final, current + 1) : current + 1;
    const loadCount = kind === "tool" ? Number(stats.dataset.replayLoadCount) || 0 : next;
    const toolCount = kind === "tool" ? next : Number(stats.dataset.replayToolCount) || 0;
    setRoleReplayStats(stats, loadCount, toolCount);
  };
  const updateRoleReplayStatFor = (item, kind) => updateRoleReplayStat(item?.closest?.(".role-run"), kind);
  const resetRoleReplayStats = () => {
    completedView.querySelectorAll(".role-run-stats").forEach((stats) => setRoleReplayStats(stats, 0, 0));
  };
  const restoreRoleReplayStats = () => {
    completedView.querySelectorAll(".role-run-stats").forEach((stats) => setRoleReplayStats(stats, Number(stats.dataset.finalLoadCount) || 0, Number(stats.dataset.finalToolCount) || 0));
  };
  const showReplayStatus = (anchor, text, complete = false) => {
    if (!currentReplayStatus) {
      currentReplayStatus = document.createElement("div");
      currentReplayStatus.className = "react-maintenance";
      currentReplayStatus.setAttribute("role", "status");
      anchor.after(currentReplayStatus);
    }
    currentReplayStatus.classList.toggle("is-complete", complete);
    currentReplayStatus.textContent = text;
  };
  const syncReplayDocuments = () => {
    const aliases = { "doc-requirement": "doc-requirement", "doc-cad": "cad-result", "cad-result": "cad-result", "doc-ifc-plan": "doc-ifc-plan" };
    completedView.querySelectorAll("[data-document]").forEach((button) => {
      const key = aliases[button.dataset.document];
      if (key) button.disabled = replayActive && !publishedDocuments.has(key);
    });
    inspectorContent.querySelectorAll("[data-document]").forEach((button) => {
      const key = aliases[button.dataset.document];
      if (key) button.disabled = replayActive && !publishedDocuments.has(key);
    });
  };
  // Cards type at a steady, reply-like rate. Measured on the fixture, both earlier rules finished
  // every card in a fixed time whatever its size — <=100 frames per block means the characters per
  // frame grow with the block — so rates ranged from 1,290 c/s (3.5k card) to 6,171 c/s (17k card)
  // while a transcript reply sits near 107 c/s: late, big cards visibly raced.
  // Cards now hold a constant ~1.5x that reply rate, with the frame count bounded so a 12k section
  // cannot schedule tens of thousands of timers. Prefixes are built when the frame runs.
  const CARD_STREAM_RATE = 160;
  const CARD_STREAM_MIN_STEP = 3;
  const CARD_STREAM_MAX_FRAMES = 1200;
  const cardPlan = (source) => {
    const chars = Array.from(String(source ?? ""));
    const frames = Math.max(1, Math.min(CARD_STREAM_MAX_FRAMES, Math.ceil(chars.length / CARD_STREAM_MIN_STEP)));
    const step = Math.max(1, Math.ceil(chars.length / frames));
    return { chars, step, frames, delay: Math.max(10, Math.round(step / CARD_STREAM_RATE * 1000)) };
  };
  const pushCardFrames = (jobs, block, source) => {
    const { chars, step, delay } = cardPlan(source);
    for (let end = step; end < chars.length; end += step) {
      const cut = end;
      jobs.push({ run: () => { block.innerHTML = cardText(chars.slice(0, cut).join("")); scheduleReplayFollow(); }, delay });
    }
    jobs.push({ run: () => { block.innerHTML = cardText(source); scheduleReplayFollow(); }, delay });
  };
  // Collapsing is animated in two steps (measure -> fade/shrink -> close) so the finished card does
  // not snap back to its summary in a single frame.
  const COLLAPSE_MS = 320;
  const beginCollapse = (node, body) => {
    if (!body) { node.open = false; return; }
    body.style.maxHeight = body.scrollHeight + "px";
    node.classList.add("is-collapsing");
    return true;
  };
  const finishCollapse = (node, body) => {
    node.open = false;
    node.classList.remove("is-collapsing");
    if (body) body.style.maxHeight = "";
  };
  // A card streams section by section. Headings live outside the streamed Markdown block, so the
  // body and every section start hidden and are revealed together with the text they introduce.
  const hideCardBody = (body) => {
    if (!body) return [];
    body.classList.add("is-stream-pending");
    const children = [...(body.children ?? [])];
    children.forEach((child) => child.classList.add("is-stream-pending"));
    return children;
  };
  const revealUpTo = (body, children, node) => {
    body?.classList.remove("is-stream-pending");
    for (const child of children) {
      child.classList.remove("is-stream-pending");
      if (child === node || child.contains?.(node)) return;
    }
  };
  const textReplayJobs = (item) => {
    const blocks = [...item.querySelectorAll("[data-md-source]")];
    const nodes = [...item.querySelectorAll("p")].filter((node) => !node.closest?.("[data-md-source]"));
    const collapsibles = [...item.querySelectorAll("details.role-card-collapsible")];
    const cardBody = item.querySelector(".role-card-body");
    const sections = hideCardBody(cardBody);
    const jobs = [{ run: () => {
      item.classList.add("is-streaming");
      // Stream with the card open, then collapse back to the preview when the text has arrived.
      collapsibles.forEach((node) => { node.classList.remove("is-collapsing"); node.open = true; });
      blocks.forEach((block) => { block.innerHTML = ""; });
      nodes.forEach((node) => {
        if (!replayText.has(node)) replayText.set(node, node.textContent);
        node.textContent = "";
      });
    } }];
    // Markdown blocks stream by re-rendering the accumulated source with the parser.
    for (const block of blocks) {
      const source = markdownSources.get(block.dataset.mdSource) ?? "";
      jobs.push({ run: () => revealUpTo(cardBody, sections, block), delay: 0 });
      pushCardFrames(jobs, block, source);
    }
    for (const node of nodes) {
      const text = replayText.get(node) ?? node.textContent;
      for (const frame of window.CAD2BIM_REPLAY.textFrames(text)) jobs.push({ run: () => { node.textContent = frame; scheduleReplayFollow(); }, delay: 28 });
    }
    jobs.push({ run: () => {
      item.classList.remove("is-streaming");
      // Anything after the last streamed section (trailing lists) belongs to the finished card.
      sections.forEach((child) => child.classList.remove("is-stream-pending"));
      collapsibles.forEach((node) => beginCollapse(node, node.querySelector(".role-card-body")));
    }, delay: 420 });
    jobs.push({ run: () => { collapsibles.forEach((node) => { const body = node.querySelector(".role-card-body"); if (body) body.style.maxHeight = "0px"; }); }, delay: 40 });
    jobs.push({ run: () => { collapsibles.forEach((node) => finishCollapse(node, node.querySelector(".role-card-body"))); }, delay: COLLAPSE_MS });
    return jobs;
  };
  // The stage summary streams like an LLM answer: prose types out, list rows appear in order,
  // and the header state chip switches from "正在输出…" to "已生成".
  const stageCardReplayJobs = (item) => {
    const state = item.querySelector("[data-stream-state]");
    const blocks = [...item.querySelectorAll(".llm-body [data-md-source]")];
    const summaryBody = item.querySelector(".llm-body");
    const summarySections = hideCardBody(summaryBody);
    const jobs = [{ run: () => {
      item.classList.remove("is-collapsing");
      item.open = true;
      item.classList.add("is-streaming");
      if (state) state.textContent = "正在输出…";
      blocks.forEach((block) => { block.innerHTML = ""; });
      scheduleReplayFollow();
    } }];
    for (const block of blocks) {
      const source = markdownSources.get(block.dataset.mdSource) ?? "";
      jobs.push({ run: () => revealUpTo(summaryBody, summarySections, block), delay: 0 });
      pushCardFrames(jobs, block, source);
    }
    jobs.push({ run: () => {
      item.classList.remove("is-streaming");
      if (state) state.textContent = "已生成";
      summarySections.forEach((child) => child.classList.remove("is-stream-pending"));
      beginCollapse(item, item.querySelector(".llm-body"));
    }, delay: 420 });
    jobs.push({ run: () => { const body = item.querySelector(".llm-body"); if (body) body.style.maxHeight = "0px"; }, delay: 40 });
    jobs.push({ run: () => finishCollapse(item, item.querySelector(".llm-body")), delay: COLLAPSE_MS });
    return jobs;
  };
  const structuredReplayJobs = (anchor, actor, state) => {
    return window.CAD2BIM_REPLAY.structuredSteps(actor, state).flatMap(({ field, label, value }) => [
      { run: () => showReplayStatus(anchor, "正在结构化输出" + label + "…"), delay: 520 },
      { run: () => {
        const available = value !== undefined && value !== null;
        if (available) {
          if (actor === "Requirement Agent") setRequirementState({
            content: field === "content" ? value : displayedRequirement,
            clarifications: field === "clarifications" ? value : displayedClarifications, complete: false,
          });
          if (actor === "CAD Agent") {
            setCadState({ content: field === "content" ? value : displayedCad,
              clarifications: field === "clarifications" ? value : displayedCadClarifications, complete: false });
            selectInspectorTab("cad");
          }
          if (state?.kind === "task_tree") applyStructuredUpdate({ state: { kind: 'task_tree', [field]: value, complete: false, changed_task_ids: state.changed_task_ids ?? [] } });
        }
        showReplayStatus(anchor, available ? label + "已更新" : "本次未提交新的" + label, true);
      }, delay: 200 },
    ]);
  };
  const dependencyReplayJobs = (anchor, plan) => {
    if (plan?.actor !== 'Global Planner' || plan?.state?.kind !== 'task_tree') return [];
    return [
      { run: () => showReplayStatus(anchor, '正在结构化输出阶段依赖…'), delay: 520 },
      { run: () => {
        const edges = window.IFC_PLAN.validateDependencies(plan.state.ifc_task_tree, plan.state.stage_dependencies);
        setTaskTreeState({ stage_dependencies: edges, complete: false });
        showReplayStatus(anchor, '阶段依赖已更新', true);
      }, delay: 200 },
    ];
  };
  const componentReplayJobs = (anchor, plan) => {
    if (plan?.actor !== 'Stage Planner' || !plan.state?.component_plan) return [];
    return [
      { run: () => showReplayStatus(anchor, '正在结构化输出构件准备计划…'), delay: 520 },
      { run: () => { window.IFC_PLAN.validateComponentPlan(plan.state.component_plan); showReplayStatus(anchor, '构件准备计划已更新', true); }, delay: 200 },
    ];
  };
  const setStageFlowPosition = (stageId, label, actor = null, round = null) => {
    if (label === "待进入" && !actor) {
      // Replay reset: forget the unit again so the flow card starts from nothing.
      flow.current = null;
      flow.reached.delete(stageId);
      flowRendered.delete(stageId);
    } else if (flow.current?.id !== stageId || flow.current?.actor !== actor || flow.current?.round !== round) {
      flow.current = { id: stageId, label: stageLabel(stageId, label), actor, round };
      flow.reached.add(stageId);
    }
    renderPhaseStrip();
    const host = document.querySelector('[data-stage-execution="' + stageId + '"]');
    if (!host) return;
    const status = host.querySelector('[data-stage-live]');
    if (status) status.textContent = label;
  };
  const stageLabel = (stageId, fallback) => {
    const stage = fixture.stages.find((item) => item.id === stageId);
    return stage ? `${stage.code} ${stage.shortTitle}` : sectionIndex.get(stageId)?.shortTitle ?? sectionIndex.get(stageId)?.navLabel ?? fallback;
  };
  const documentReplayJobs = (anchor, plan) => {
    const record = documentForEntry(plan);
    if (!record) return [];
    const creating = record.action === "create";
    return [
      { run: () => showReplayStatus(anchor, (creating ? "正在创建 MD · " : "正在编辑 MD · ") + record.title + "…"), delay: 950 },
      { run: () => {
        publishedDocuments.set(record.id, record.markdown);
        if (plan.state) applyStructuredUpdate({ state: plan.state });
        syncReplayDocuments();
        showReplayStatus(anchor, (creating ? "已创建 " : "已编辑 ") + record.title, true);
      }, delay: 260 },
    ];
  };

  const animateRequirementRevision = (branch, state, publish) => {
    confirmButton.hidden = true;
    pendingRequirement.state = { ...pendingRequirement.state, complete: false };
    replayBusy = true; replayPaused = false;
    replayButton.textContent = "Ⅱ 暂停回放";
    const message = document.createElement("div");
    message.className = "public-commentary";
    message.innerHTML = "<p>我会按照你刚确认的方案修订需求，重新评估澄清事项，再编辑已有的需求文档。</p>";
    branch.appendChild(message);
    playback.play([
      ...textReplayJobs(message),
      ...structuredReplayJobs(message, "Requirement Agent", state),
      ...documentReplayJobs(message, { actor: "Requirement Agent", state, documentId: "doc-requirement" }),
    ], () => {
      clearReplayStatus();
      publish();
      // Stream the role summary as well; the result remains hidden until it is complete.
      const summary = branch.querySelector(".role-final");
      const result = branch.querySelector(".requirement-final");
      if (result) result.hidden = true;
      confirmButton.hidden = true;
      playback.play(textReplayJobs(summary), () => {
        if (result) result.hidden = false;
        replayBusy = false; replayPaused = true;
        confirmButton.hidden = false;
        // Gate: the recorded confirmation is filled in by 继续示例, so the confirm button stays greyed.
        confirmButton.disabled = true;
        hint.textContent = "需求修订已完成；回放中「确认并继续」已自动填入 · 点击「继续示例」继续";
        replayButton.textContent = "▶ 继续示例";
      });
    });
  };
  const replayItemJobs = (item) => {
    const event = replayEvents.get(item.id);
    const jobs = [{ run: () => {
      if (event?.kind === "assistant" || item.matches(".role-run")) clearReplayStatus();
      if (event?.kind === "assistant") document.querySelectorAll(".is-task-updated").forEach((row) => row.classList.remove("is-task-updated"));
      item.classList.remove("is-replay-hidden");
      if (event?.packageAlias) { visiblePackageAliases.add(event.packageAlias); syncReplayPackages(); }
      openParentDetails(item);
      if (item.matches(".stage-card, .role-run, .stage-execution")) item.open = true;
      if (item.matches(".role-run")) {
        setRoleReplayStatus(item, true);
        const plan = replayRuns.get(item.id);
        if (plan?.actor === "Requirement Agent") setRequirementState({ content: displayedRequirement, clarifications: null, complete: false });
        if (plan?.actor === "CAD Agent") { setCadState({ content: displayedCad, clarifications: null, complete: false }); selectInspectorTab("cad"); }
        if (plan?.actor === "Global Planner") { setTaskTreeState({ clarifications: null, complete: false }); selectInspectorTab("tasks"); }
        if (plan?.stageId) { setStageFlowPosition(plan.stageId, plan.actor + ' · 进行中', plan.actor, plan.runNumber ?? null); selectInspectorTab("tasks"); }
      }
      if (event?.kind === 'stage_route') setStageFlowPosition(event.stageId, event.nextStageId ? '已确认 · 交给' + event.to : event.to === '用户确认' ? '等待用户确认' : '交给 ' + event.to, event.nextStageId ? '用户确认' : event.to);
      if (event?.kind === 'stage_checkpoint') setStageFlowPosition(event.stageId, event.boundary === 'plan' ? '等待确认阶段计划' : '等待确认阶段结果', event.boundary === 'plan' ? 'Stage Planner' : '用户确认');
      if (event?.kind === 'stage_complete') {
        acceptedStageIds.add(event.stageId);
        stageProducts.filter((file) => file.stageId === event.stageId && !file.final).forEach((file) => visibleIfcFiles.add(file.url));
        syncReplayIfcFiles();
        renderPhaseStrip();
        setTaskTreeState({ complete: true });
        setStageFlowPosition(event.stageId, '已确认', '用户确认');
        const nextId = fixture.stages.find((stage) => stage.id === event.stageId)?.nextStageId;
        const next = nextId ? document.querySelector('[data-stage-placeholder="' + nextId + '"] small') : null;
        if (next) next.textContent = '前序已确认 · 待细化';
      }
      if (event?.kind === "workflow_complete" && event.ifcFile) {
        visibleIfcFiles.add(event.ifcFile.url);
        syncReplayIfcFiles();
      }
      // Inspector panels follow the playback: a unit's MCP records and process files appear only
      // after the conversation has actually reached that unit.
      revealUnit(event?.stageId ?? replayRuns.get(item.closest?.(".role-run")?.id)?.stageId
        ?? item.closest?.("[data-stage-execution]")?.dataset?.stageExecution
        ?? (typeof item.id === "string" && item.id.startsWith("section-") ? item.id.slice("section-".length) : null));
      scheduleReplayFollow();
    }, delay: 90 }];
    if (item.matches(".role-run")) {
      const entryResources = [...item.querySelectorAll("[data-replay-entry-resource]")];
      for (let index = 0; index < entryResources.length; index += 1) jobs.push({ run: () => updateRoleReplayStat(item, "resource"), delay: 120 });
    }
    if (item.matches(".activity-group")) {
      const group = replayGroups.get(item.dataset.replayGroup);
      const tools = [...item.querySelectorAll(".tool-call")];
      const resources = [...item.querySelectorAll(".resource-load")].filter((resource) => resource.matches?.(".resource-load") || resource.classList?.contains?.("resource-load"));
      const status = item.querySelector("summary small");
      jobs.push({ run: () => {
        status.dataset.completedLabel ??= status.textContent;
        status.textContent = tools.length ? "调用中" : resources.length ? "读取中" : "处理中";
        item.classList.add("is-replay-active");
        if (tools.length) item.open = true;
        tools.forEach((tool) => {
          const label = tool.querySelector(".activity-status");
          label.dataset.completedLabel ??= label.textContent;
          label.textContent = "等待返回"; tool.disabled = true;
        });
        resources.forEach((resource) => {
          const label = resource.querySelector(".activity-status");
          label.dataset.completedLabel ??= label.textContent;
          label.textContent = "读取中"; resource.disabled = true;
        });
        scheduleReplayFollow();
      }, delay: 220 });
      for (const resource of resources) jobs.push({ run: () => {
        const label = resource.querySelector(".activity-status");
        label.textContent = label.dataset.completedLabel || "已读取 ↗";
        resource.disabled = false;
        updateRoleReplayStatFor(resource, "resource");
        scheduleReplayFollow();
      }, delay: 140 });
      for (const tool of tools) jobs.push({ run: () => {
        const label = tool.querySelector(".activity-status");
        label.textContent = label.dataset.completedLabel?.trim() === "↗" ? "已返回 ↗" : label.dataset.completedLabel || "已返回 ↗";
        tool.disabled = false;
        updateRoleReplayStatFor(tool, "tool");
        item.querySelectorAll("[data-preview-for]").forEach((preview) => { if (preview.dataset.previewFor === tool.id) preview.hidden = false; });
        const completed = replayEvents.get(tool.id);
        const alias = completed?.publishedPackage?.package_alias ?? (completed?.effect === "WRITE" && !completed.call?.isError ? completed?.call?.output?.package_alias : null);
        if (alias) { visiblePackageAliases.add(alias); syncReplayPackages(); }
        scheduleReplayFollow();
      }, delay: 140 });
      jobs.push({ run: () => {
        status.textContent = status.dataset.completedLabel;
        item.classList.remove("is-replay-active");
      } });
      const state = group?.items.findLast(({ event: entry }) => entry.stateUpdate)?.event.stateUpdate;
      if (group && state) jobs.push(...structuredReplayJobs(item, group.actor, state));
    } else if (item.matches(".public-commentary, .review-decision, .role-exit-marker")) {
      if (!item.matches(".role-exit-marker")) {
        const textJobs = textReplayJobs(item);
        jobs.unshift(textJobs.shift()); // Clear text before revealing its container.
        jobs.push(...textJobs);
      }
      if (event?.stateUpdate) {
        if (event.stateUpdate.trigger === "ENTRY") jobs.push({ run: () => applyStructuredUpdate({ state: event.stateUpdate }) });
        else jobs.push(...structuredReplayJobs(item, event.actor?.name, event.stateUpdate));
      }
    } else if (item.matches(".stage-card-doc")) {
      const stream = stageCardReplayJobs(item);
      jobs.unshift(stream.shift()); // Clear the body before the container is revealed.
      jobs.push(...stream);
    } else if (item.matches(".context-compaction")) {
      jobs.push({ run: () => item.classList.add("is-replay-visible"), delay: 1200 });
    } else if (item.dataset.completesRun) {
      const run = document.getElementById(item.dataset.completesRun);
      const anchor = run.querySelector(".role-event-stream")?.lastElementChild ?? run;
      // The document owner completes before this entry's Role summary is emitted.
      jobs.unshift(...dependencyReplayJobs(anchor, replayRuns.get(run.id)), ...componentReplayJobs(anchor, replayRuns.get(run.id)), ...documentReplayJobs(anchor, replayRuns.get(run.id)));
      const textJobs = textReplayJobs(item);
      jobs.unshift(textJobs.shift());
      jobs.push(...textJobs);
      jobs.push({ run: () => { clearReplayStatus(); run.open = false; setRoleReplayStatus(run, false); } });
    }
    return jobs;
  };
  const startReplay = () => {
    switchTaskView("completed");
    stopReplay();
    replayActive = true;
    revealedUnits.clear();
    refreshUnitVisibility();
    flow.replay = true;
    flow.dagConfirmed = false;
    flow.reached.clear();
    flowRendered.clear();
    flow.current = null;
    renderPhaseStrip();
    replayFollow = true;
    scrollArea.classList.add("is-replaying");
    replayExitButton.hidden = false;
    if (replaySpeedControl) replaySpeedControl.hidden = false;
    publishedDocuments.clear();
    visiblePackageAliases.clear();
    visibleIfcFiles.clear();
    syncReplayIfcFiles();
    syncReplayPackages();
    syncReplayDocuments();
    setRequirementState(null);
    setCadState(null);
    acceptedStageIds.clear();
    setTaskTreeState(null);
    fixture.stages.forEach((stage) => setStageFlowPosition(stage.id, "待进入", null));
    document.querySelectorAll("[data-stage-placeholder] small").forEach((item) => { item.textContent = "等待前序阶段"; });

    showInspectorOverview();
    selectInspectorTab("requirement");
    shell.classList.add("inspector-open");
    inspector.setAttribute("aria-hidden", "false");
    completedView.querySelectorAll("[data-preview-for]").forEach((preview) => { preview.hidden = true; });
    const items = [...completedView.querySelectorAll(".replay-item")];
    resetRoleReplayStats();
    items.forEach((item) => item.classList.add("is-replay-hidden"));
    completedView.querySelectorAll(".stage-card, .role-run, .activity-group").forEach((details) => { details.open = false; });
    const progress = document.createElement("div");
    progress.className = "replay-progress";
    progress.innerHTML = "<i></i>";
    document.body.appendChild(progress);
    replayButton.textContent = "Ⅱ 暂停回放";
    replayButton.dataset.playing = "true";
    setReplayJumpVisibility();
    scrollArea.scrollTo({ top: 0, behavior: "instant" });
    replayIndex = 0;
    replayPaused = false;
    const step = () => {
      // A gate during replay: the user's own confirmation is recorded content, so it is filled in for
      // them. The confirm button stays visible but greyed out; the only action left is 继续示例.
      const gatePause = (detail) => {
        replayBusy = false;
        replayPaused = true;
        confirmButton.hidden = false;
        confirmButton.disabled = true;
        hint.textContent = detail + "；回放中「确认并继续」已自动填入 · 点击「继续示例」继续";
        replayButton.textContent = "▶ 继续示例";
      };
      if (replayPaused || replayBusy) return;
      if (replayIndex >= items.length) return stopReplay("回放完成");
      const item = items[replayIndex];
      replayBusy = true;
      playback.play(replayItemJobs(item), () => {
        replayBusy = false;
        replayIndex += 1;
        document.querySelector(".replay-progress i")?.style.setProperty("width", `${Math.round((replayIndex / items.length) * 100)}%`);
        if (item.dataset.planningCheckpoint) {
          const state = planningCheckpoints.get(item.dataset.planningCheckpoint);
          pendingPlanning = { state, anchor: item };
          gatePause(state.clarifications == null ? '本轮整体规划问题尚未评估' : state.clarifications.length ? '整体计划已按已展示的默认方案采用' : '整体计划已确认');
          return;
        }
        if (item.dataset.stageCheckpoint) {
          const checkpoint = stageCheckpoints.get(item.dataset.stageCheckpoint);
          pendingStage = { ...checkpoint, anchor: item };
          const stage = fixture.stages.find((entry) => entry.id === checkpoint.stageId);
          gatePause(checkpoint.boundary === 'plan'
            ? stage.shortTitle + '阶段计划已确认，下一步进入 ' + (checkpoint.state.component_plan.needs_component ? 'Component Agent' : 'Modeler')
            : stage.shortTitle + '阶段结果已确认，随后形成阶段摘要');
          return;
        }
        if (item.dataset.cadCheckpoint) {
          const state = cadCheckpoints.get(item.dataset.cadCheckpoint);
          pendingCad = { state, anchor: item };
          gatePause(state.clarifications == null ? "本轮 CAD 问题尚未评估" : state.clarifications.length ? "CAD 已按已展示方案采用，进入 CAD 第二轮" : "CAD 成果已确认，进入整体规划");
          return;
        }
        if (item.dataset.requirementCheckpoint) {
          const state = requirementCheckpoints.get(item.dataset.requirementCheckpoint);
          pendingRequirement = { state, anchor: item };
          gatePause(state.clarifications == null ? "本轮问题尚未评估，暂不能进入 CAD" : state.clarifications.length ? "已按已展示的默认方案写入需求" : "需求已确认，进入 CAD");
          return;
        }
        step();
      });
    };
    resumeAfterRequirement = () => {
      const next = items.findIndex((item) => item.id === "section-cad");
      if (next < 0) return;
      replayIndex = next; replayPaused = false;
      replayButton.textContent = "Ⅱ 暂停回放";
      step();
    };
    resumeAfterPlanning = () => {
      const next = items.findIndex((item) => item.id === 'stage-initialization-flow');
      if (next < 0) return;
      document.getElementById('section-modeling').classList.remove('is-replay-hidden');
      replayIndex = next; replayPaused = false; replayButton.textContent = 'Ⅱ 暂停回放';
      step();
    };
    resumeAfterStage = () => {
      while (replayIndex < items.length && replayEvents.get(items[replayIndex].id)?.confirmation) replayIndex += 1;
      replayPaused = false; replayButton.textContent = 'Ⅱ 暂停回放'; step();
    };
    resumeAfterCad = (destination, entered) => {
      const targetId = destination === "planning" ? "section-planning" : roleRuns.find((run) => run.stageId === "cad" && run.runNumber === 2)?.id;
      const next = items.findIndex((item) => item.id === targetId);
      if (next < 0) return;
      if (entered) {
        const event = fixture.sections.find((section) => section.id === "cad").events.find((event) => event.cadEntry === 2);
        if (event) {
          event.stateUpdate = entered;
          for (const update of stateUpdates.values()) if (update.state?.kind === "cad" && update.state.trigger === "ENTRY" && update.state.business_entry === 2) update.state = entered;
        }
      }
      replayIndex = next; replayPaused = false;
      replayButton.textContent = "Ⅱ 暂停回放";
      step();
    };
    window.runReplayStep = step;
    step();
  };

  const toggleReplay = () => {
    if (replayButton.dataset.playing !== "true") return startReplay();
    // Paused at a gate: pressing 继续示例 enters the recorded confirmation on the user's behalf (the
    // confirm button itself stays greyed out) and then continues the replay.
    if (pendingPlanning || pendingStage || pendingRequirement || pendingCad) {
      replayPaused = false;
      confirmButton.disabled = false;
      confirm();
      confirmButton.disabled = true;
      return;
    }
    pendingRequirement = null; pendingCad = null; confirmButton.hidden = true;
    replayPaused = !replayPaused;
    if (replayPaused) { playback.pause(); cancelReplayFollow(); }
    replayButton.textContent = replayPaused ? "▶ 继续回放" : "Ⅱ 暂停回放";
    if (!replayPaused) { if (replayBusy) playback.resume(); else window.runReplayStep?.(); }
  };

  document.addEventListener("click", (event) => {
    if (taskTypePicker.open && !event.target.closest("#taskTypePicker")) taskTypePicker.open = false;
    const inspectorTab = event.target.closest("[data-inspector-tab]");
    if (inspectorTab) {
      selectInspectorTab(inspectorTab.dataset.inspectorTab);
      return;
    }

    const operationFilter = event.target.closest("[data-operation-filter]");
    if (operationFilter) {
      filterMcpOperations(operationFilter.dataset.operationFilter);
      return;
    }

    const payloadTab = event.target.closest("[data-payload-index]");
    if (payloadTab) {
      renderDataPayload(Number(payloadTab.dataset.payloadIndex));
      return;
    }

    const targetButton = event.target.closest("[data-target]");
    if (targetButton) {
      scrollToTarget(targetButton.dataset.target);
      if (targetButton.closest(".inspector") && window.innerWidth < 820) toggleInspector();
      return;
    }

    const modeButton = event.target.closest("[data-view-mode]");
    if (modeButton) {
      modelDialog.querySelectorAll("[data-view-mode]").forEach((button) => {
        const selected = button === modeButton;
        button.classList.toggle("is-active", selected);
        button.setAttribute("aria-pressed", String(selected));
      });
      modelDialog.classList.toggle("show-ports", modeButton.dataset.viewMode === "ports");
      modelDialog.classList.toggle("show-systems", modeButton.dataset.viewMode === "systems");
      return;
    }

    const actionButton = event.target.closest("[data-action]");
    if (!actionButton) return;
    const action = actionButton.dataset.action;
    if (action === "toggle-task-navigation") setTaskNavigation(!shell.classList.contains("task-navigation-open"));
    if (action === "close-task-navigation") setTaskNavigation(false, true);
    if (["open-workspace", "open-library", "open-completed-task", "open-new-task", "new-task"].includes(action)) setTaskNavigation(false);
    if (action === "toggle-dialogue") {
      const expand = actionButton.textContent === "展开对话";
      completedView.querySelectorAll(".role-run, .stage-card").forEach((item) => { item.open = expand; });
      actionButton.textContent = expand ? "收起对话" : "展开对话";
    }
    if (action === "toggle-inspector") toggleInspector();
    if (action === "close-inspector") dismissInspector();
    if (action === "open-detail-panel") openDetailPanel(actionButton.dataset.detail);
    if (action === "replay") toggleReplay();
    if (action === "exit-replay") stopReplay("已退出回放");
    if (action === "submit-feedback") submitFeedback();
    if (action === "confirm") confirm();
    if (action === "open-workspace") openWorkspace();
    if (action === "open-completed-task") switchTaskView("completed");
    if (action === "open-new-task") switchTaskView("new", actionButton.dataset.taskId);
    if (action === "delete-task") void deleteChatTask(actionButton.dataset.taskId);
    if (action === "cancel-task-delete") closeTaskDelete();
    if (action === "confirm-task-delete") void confirmTaskDelete();
    if (action === "new-task") {
      taskTypePicker.open = false;
      createTask(actionButton.dataset.taskType);
    }
    if (action === "attach-file") composerFile.click();
    if (action === "remove-attachment") {
      const task = tasks.get(currentTaskId);
      const index = task?.draft.attachments.findIndex((attachment) => attachment.id === actionButton.dataset.attachmentId) ?? -1;
      if (index >= 0) URL.revokeObjectURL(task.draft.attachments.splice(index, 1)[0].url);
      refreshTaskComposer();
    }
    if (action === "retry-task-input") tasks.get(actionButton.dataset.taskId)?.sender.retry();
    if (action === "load-task-history") void loadTaskHistory(actionButton.dataset.taskId);
    if (action === "pause-task") void requestTaskControl("pause");
    if (action === "resume-task") void requestTaskControl("resume");
    if (action === "open-library") openLibrary(actionButton.dataset.resource ?? "");
    if (action === "open-document") renderDocument(actionButton.dataset.document);
    if (action === "open-data-product") openDataProduct(actionButton.dataset.product);
    if (action === "open-model") {
      const version = actionButton.dataset.title || (actionButton.dataset.version === "before" ? "第一版模型" : "最终修订模型");
      setViewerContentMode("model", version);
      document.getElementById("modelDialogTitle").textContent = `城市燃气管网 · ${version}`;
      modelDialog.querySelector(".viewer-caption strong").textContent = actionButton.dataset.stage ? version : "城市燃气管网";
      modelDialog.querySelector(".viewer-caption span").textContent = `IFC 4.3 · ${version}`;
      modelDialog.querySelector(".viewer-canvas > img").src = actionButton.dataset.preview || "./assets/model/network-overview.webp";
      resetViewer();
      openDialog(modelDialog);
    }
    if (action === "open-detail") {
      const imageMode = actionButton.dataset.previewMode === "image";
      setViewerContentMode(imageMode ? "image" : "model", actionButton.dataset.title);
      document.getElementById("modelDialogTitle").textContent = actionButton.dataset.title;
      modelDialog.querySelector(".viewer-caption strong").textContent = actionButton.dataset.title;
      modelDialog.querySelector(".viewer-canvas > img").src = actionButton.dataset.src;
      resetViewer();
      openDialog(modelDialog);
    }
    if (action === "open-component") openLibrary("family/family-meter/2");
    if (action === "close-dialog") closeDialog(actionButton.closest("dialog"));
  });

  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
    if (currentTaskMode === "new") {
      tasks.get(currentTaskId).draft.text = input.value;
      refreshTaskComposer();
      return;
    }
    submitButton.disabled = !input.value.trim();
    confirmButton.disabled = Boolean(input.value.trim());
    hint.textContent = "⌘ / Ctrl + Enter 提交";
    if (replayButton.dataset.playing === "true" && !pendingRequirement && !pendingCad && !pendingPlanning && !pendingStage) stopReplay("已停止回放，可以继续输入");
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing && ((currentTaskMode === "new" && !event.shiftKey) || event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      submitFeedback();
    }
  });

  taskTypePicker.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    taskTypePicker.open = false;
    taskTypePicker.querySelector("summary").focus();
  });

  document.addEventListener("keydown", (event) => {
    if (taskDeleteDialog.open) return;
    if (event.key === "Escape" && shell.classList.contains("task-navigation-open")) {
      event.preventDefault();
      setTaskNavigation(false, true);
    }
  });

  composerFile.addEventListener("change", () => {
    acceptAttachments(composerFile.files ?? []);
    composerFile.value = "";
  });
  input.addEventListener("paste", (event) => {
    if (currentTaskMode !== "new") return;
    const files = [...(event.clipboardData?.items ?? [])].filter((item) => item.kind === "file").map((item) => item.getAsFile()).filter(Boolean);
    if (!files.length) return;
    event.preventDefault();
    acceptAttachments(files);
  });
  ["dragenter", "dragover"].forEach((type) => composer.addEventListener(type, (event) => {
    if (currentTaskMode !== "new" || ![...(event.dataTransfer?.types ?? [])].includes("Files")) return;
    event.preventDefault();
    composer.classList.add("is-dragging");
  }));
  composer.addEventListener("dragleave", (event) => { if (!composer.contains(event.relatedTarget)) composer.classList.remove("is-dragging"); });
  composer.addEventListener("drop", (event) => {
    if (currentTaskMode !== "new") return;
    event.preventDefault();
    composer.classList.remove("is-dragging");
    acceptAttachments(event.dataTransfer?.files ?? []);
  });
  document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    if (dialog === taskDeleteDialog) {
      const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) closeTaskDelete();
    }
    else closeDialog(dialog);
  }));

  const viewerButtons = [...document.querySelectorAll(".viewer-tools button")];
  viewerButtons[0]?.addEventListener("click", () => { viewerScale = 1; viewerRotation = 0; updateViewerTransform(); });
  viewerButtons[1]?.addEventListener("click", () => { viewerRotation += 15; updateViewerTransform(); });
  viewerButtons[2]?.addEventListener("click", () => { viewerScale = Math.min(1.8, viewerScale + .15); updateViewerTransform(); });
  viewerButtons[3]?.addEventListener("click", () => { viewerScale = Math.max(.7, viewerScale - .15); updateViewerTransform(); });

  const resizer = document.getElementById("inspectorResizer");
  const setInspectorWidth = (width) => {
    const next = Math.max(320, Math.min(1200, width));
    document.documentElement.style.setProperty("--inspector-width", `${next}px`);
    resizer.setAttribute("aria-valuenow", String(next));
  };
  resizer.addEventListener("pointerdown", (event) => {
    if (!shell.classList.contains("inspector-open")) return;
    resizer.setPointerCapture(event.pointerId);
    const onMove = (moveEvent) => setInspectorWidth(window.innerWidth - moveEvent.clientX);
    const onUp = () => { resizer.removeEventListener("pointermove", onMove); resizer.removeEventListener("pointerup", onUp); };
    resizer.addEventListener("pointermove", onMove);
    resizer.addEventListener("pointerup", onUp);
  });
  resizer.addEventListener("keydown", (event) => {
    if (!shell.classList.contains("inspector-open") || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const current = Number.parseInt(getComputedStyle(document.documentElement).getPropertyValue("--inspector-width"), 10) || 720;
    setInspectorWidth(current + (event.key === "ArrowLeft" ? 20 : -20));
  });
  resizer.setAttribute("aria-valuemin", "320");
  resizer.setAttribute("aria-valuemax", "1200");
  resizer.setAttribute("aria-valuenow", "720");

  const observedSections = [...document.querySelectorAll("[data-section]")];
  const sectionObserver = new IntersectionObserver((entries) => {
    if (currentTaskMode !== "completed") return;
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    const id = visible.target.dataset.section;
    activeSection = id;
    document.querySelectorAll(".stage-link").forEach((button) => button.classList.toggle("is-current", button.dataset.target === `section-${id}`));
  }, { root: scrollArea, threshold: [.12, .35], rootMargin: "-10% 0px -58%" });
  observedSections.forEach((section) => sectionObserver.observe(section));
})();
