// Library presentation over the existing Catalog, Code and saved-display owners.
(() => {
  const prefix = '/api/component';
  const segment = (value) => encodeURIComponent(value);
  const assetPath = /^\/tasks\/[A-Za-z0-9_-]+\/artifacts\/[A-Za-z0-9_-]+\/versions\/[A-Za-z0-9_-]+\/content$/;
  const create = ({ search = window.location.search, fetch: fetchRequest = window.fetch.bind(window), displayTimers = window } = {}) => {
    const params = new URLSearchParams(search);
    const context = { task_id: params.get('task_id') || '', run_id: params.get('run_id') || '' };
    const staticCity = params.get('source') === 'city';
    const request = async (path, query = {}, payload, scopedQuery = true, method = payload === undefined ? 'GET' : 'POST', signal) => {
      const values = new URLSearchParams();
      for (const [key, value] of Object.entries({ ...(scopedQuery ? context : {}), ...query })) {
        if (value !== undefined && value !== null && value !== '') values.set(key, String(value));
      }
      const response = await fetchRequest(`${prefix}${path}${values.size ? '?' + values : ''}`, {
        method, credentials: 'same-origin', cache: 'no-store',
        headers: { accept: 'application/json', ...(payload === undefined ? {} : { 'content-type': 'application/json' }) },
        ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
        ...(signal ? { signal } : {}),
      });
      if (response.status === 204) return;
      const result = await response.json();
      if (!response.ok) {
        const error = new Error(typeof result.detail === 'string' ? result.detail : result.detail?.message || '暂时无法读取资源，请重试。');
        error.status = response.status;
        throw error;
      }
      return result;
    };
    const requireRun = () => {
      if (!context.task_id || !context.run_id) throw new Error('请从一个现有任务打开资源，以查看源码和模型。');
    };
    const assetUrl = (asset) => {
      if (!asset || !assetPath.test(asset.content_url || '')) throw new Error('模型资源地址不可用。');
      return prefix + asset.content_url;
    };
    const displayPath = () => `/tasks/${segment(context.task_id)}/runs/${segment(context.run_id)}/display`;
    const displayIdentity = (source) => ({ source_kind: source.source_kind, source_ref: source.source_ref });
    return {
      context, staticCity,
      list: (kind, cursor) => request(`/library/${kind === 'geometry' ? 'geometry' : 'families'}`, { page_size: 20, cursor }),
      remove: (kind, reference) => request(`/library/${kind === 'geometry' ? 'geometry' : 'families'}`, { reference }, undefined, true, 'DELETE'),
      info: (kind, reference, beforeVersion) => request(`/library/${kind === 'geometry' ? 'geometry' : 'families'}/info`, {
        reference, ...(kind === 'family' ? { page_size: 20, before_version_no: beforeVersion } : {}),
      }),
      code: (card, sourcePath, startLine = 1) => {
        requireRun();
        return request('/library/code', {}, { code_locator: card.code_locator,
          ...(sourcePath ? { source_path: sourcePath } : {}),
          view: { view_mode: 'lines', start_line: startLine, line_count: 200 } });
      },
      thumbnail: async (source) => {
        requireRun();
        try {
          const result = await request(displayPath(), displayIdentity(source), undefined, false);
          return assetUrl(result.png);
        } catch (error) {
          if (error.status === 404 || error.status === 422) return null;
          throw error;
        }
      },
      display: async (source, { onPhase = () => {} } = {}) => {
        requireRun();
        const path = displayPath();
        const identity = displayIdentity(source);
        const displayRequest = async (phase, payload) => {
          onPhase(phase);
          const abort = new AbortController();
          let timer;
          try {
            return await Promise.race([
              request(path, payload === undefined ? identity : {}, payload, false, payload === undefined ? 'GET' : 'POST', abort.signal),
              new Promise((_, reject) => {
                timer = displayTimers.setTimeout(() => {
                  reject(new Error(phase === 'reading' ? '读取三维预览超时，请点击“重新读取”重试。' : '准备三维预览超时，请点击“重新读取”检查已生成结果。'));
                  abort.abort();
                }, phase === 'reading' ? 30_000 : 120_000);
              }),
            ]);
          } finally { displayTimers.clearTimeout(timer); }
        };
        let result;
        try { result = await displayRequest('reading'); }
        catch (error) { if (error.status !== 404) throw error; result = await displayRequest('preparing', identity); }
        return { image: assetUrl(result.png), model: { url: assetUrl(result.stl), format: 'stl' }, warnings: result.display_quality_warnings || [] };
      },
    };
  };
  const version = (kind, item) => ({ number: item.version_no,
    ref: kind === 'geometry' ? item.resource_version_ref : item.family_version_ref });
  const summary = (kind, item) => {
    const latest = item.latest_version;
    return { remote: true, kind,
      id: kind === 'geometry' ? item.resource_root_ref : item.family_root_ref,
      name: kind === 'geometry' ? item.display_name : latest.display_name,
      scope: String(item.owner_scope).toLowerCase(),
      subtype: kind === 'geometry' ? item.resource_kind : latest.product_structure_kind,
      purpose: kind === 'family' ? latest.purpose : '', versions: [version(kind, latest)] };
  };
  const details = (kind, result) => {
    const info = result.catalog;
    if (info.target_kind === 'resource_root' || info.target_kind === 'family_root') {
      return { ...summary(kind, info.resource || info.family),
        versions: (info.recent_versions || info.versions).map((item) => version(kind, item)),
        versionsComplete: info.recent_versions_complete ?? info.versions_complete,
        beforeVersion: info.next_before_version_no };
    }
    const manifest = info.manifest || {};
    const fields = kind === 'family' ? window.readLibraryFields({ target_kind: 'family_version', family_contract_json: info.family_contract })
      : window.readLibraryFields({ resource_kind: info.resource_kind, geometry_format: manifest.geometry_format,
        unit: manifest.unit, parameter_schema_json: manifest.parameter_schema, interfaces: manifest.interfaces || [] });
    return { remote: true, kind, ...fields,
      id: info.resource_root_ref || info.family_root_ref, name: info.display_name,
      scope: String(info.owner_scope).toLowerCase(), subtype: info.resource_kind || info.product_structure_kind,
      purpose: info.purpose || '', usage: info.usage_instructions || manifest.usage_instructions || '',
      selectedRef: kind === 'geometry' ? info.version.resource_version_ref : info.family_version_ref,
      selectedNumber: kind === 'geometry' ? info.version.version_no : info.version_no,
      bindings: kind === 'family' ? (info.geometry_bindings || []).map((binding) => ({
        id: binding.geometry_resource_version_ref, role: binding.binding_key, name: binding.binding_key,
      })) : (manifest.geometry_inputs || []).map((input) => ({
        id: input.resource_version_ref || input.geometry_resource_version_ref, role: input.input_key, name: input.input_key,
      })).filter((input) => input.id),
      code: result.code, displaySource: result.display_source, displayMessage: result.display_message,
    };
  };
  window.CAD2BIM_LIBRARY_API = { create, summary, details };
})();
