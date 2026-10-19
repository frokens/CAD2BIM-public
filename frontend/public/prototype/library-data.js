// Tables are deterministic projections of saved Provider-contract fields.
// Display names, short descriptions and web scope are presentation metadata.
(() => {
  const parse = (value) => typeof value === 'string' ? JSON.parse(value) : value;
  const valueText = (value) => typeof value === 'string' ? value : JSON.stringify(value);
  const constraints = (object, omitted) => Object.fromEntries(Object.entries(object).filter(([key]) => !omitted.includes(key)));
  const parameterFields = (rows) => ({
    parameters: rows.map(([path, type, rules]) => [path, type, typeof rules === 'object' ? Object.entries(rules).map(([key, value]) => `${key}: ${valueText(value)}`).join('; ') : rules]),
    parameterConstraints: Object.fromEntries(rows.map(([path, , rules]) => [path, typeof rules === 'object' ? rules : { schema: rules === 'true' }])),
  });
  const familyParameters = (definitions, prefix = '') => definitions.flatMap((definition) => {
    const path = prefix + definition.parameter_key;
    const rows = [[path, definition.parameter_kind, constraints(definition, ['parameter_key', 'parameter_kind', 'properties', 'items'])]];
    if (definition.properties) rows.push(...familyParameters(definition.properties, path + '.'));
    if (definition.items) rows.push(...familyParameters([{ ...definition.items, parameter_key: '[]' }], path));
    return rows;
  });
  const geometryParameters = (schema, path = '$', required) => {
    if (typeof schema === 'boolean') return [[path, 'schema', String(schema)]];
    if (!schema || typeof schema !== 'object') return [];
    const detail = { ...schema, ...(required === undefined ? {} : { required }) };
    const rows = [[path, Array.isArray(schema.type) ? schema.type.join(' | ') : schema.type ?? '未声明', constraints(detail, ['type', 'properties', 'items', 'prefixItems'])]];
    for (const [name, child] of Object.entries(schema.properties ?? {})) rows.push(...geometryParameters(child, path === '$' ? name : path + '.' + name, (schema.required ?? []).includes(name)));
    if (schema.items != null) rows.push(...geometryParameters(schema.items, path + '[]'));
    if (schema.prefixItems) schema.prefixItems.forEach((child, index) => rows.push(...geometryParameters(child, `${path}[${index}]`)));
    return rows;
  };
  window.readLibraryFields = (response) => {
    const info = response.info ?? response;
    if (info.target_kind === 'family_version') {
      const contract = parse(info.family_contract_json);
      return {
        ...parameterFields(familyParameters(contract.public_parameters ?? [])),
        interfaces: (contract.interfaces ?? []).map((item) => [item.interface_key, item.owner_component_key, item.interface_kind]),
        interfaceRecords: contract.interfaces ?? [],
        interfaceHeadings: ['接口', '所属产品', '类型'],
        structure: (contract.components ?? []).map((item) => `${item.component_key} · ${item.ifc_class}${item.parent_component_key ? ` · parent: ${item.parent_component_key}` : ''}`),
      };
    }
    const schema = parse(info.parameter_schema_json);
    return {
      ...parameterFields(schema == null ? [] : geometryParameters(schema)),
      interfaces: (info.interfaces ?? []).map((item) => [item.interface_key, valueText(item.local_frame), valueText(item.profile)]),
      interfaceRecords: info.interfaces ?? [],
      interfaceHeadings: ['接口', '局部坐标框架', '断面'],
      structure: [info.resource_kind, info.geometry_format, info.unit].filter(Boolean),
    };
  };
})();

window.CAD2BIM_RESOURCE_LIBRARY = [];
