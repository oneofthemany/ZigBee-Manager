// api-docs.js — ZigBee & Matter Manager API Explorer
// =====================================================================
// Data sources:
//   /api/routes     — flat list of routes, grouped by tag/prefix (sidebar)
//   /openapi.json   — full OpenAPI spec from FastAPI (detail pane content)
//
// Hand-curated overrides in `routeMetadata` are still honoured if present —
// they win over OpenAPI for description/returns. Mostly you should never
// need to add entries: write a docstring on the route handler instead, and
// FastAPI will surface it via OpenAPI automatically.
console.log('ZMM API Docs JS loaded');

// ============================================================================
// OPTIONAL HAND-CURATED OVERRIDES
// ----------------------------------------------------------------------------
// Only needed for routes you can't or don't want to docstring (e.g. generic
// helpers like /routes itself). Leave empty if you want a fully OpenAPI-driven
// page.
// ============================================================================
const routeMetadata = {
    '/routes': {
        description: 'Plain HTML route listing — handy for quick eyeballing.',
        returns: 'HTML page',
    },
    '/api/routes': {
        description: 'JSON list of every documented route grouped by tag/prefix.',
        returns: '{ groupName: [{method, path}, ...] }',
    },
};

// Cache: { "GET /api/foo": { operation, components } }
const openApiCache = {};
let openApiLoaded = false;

let currentRoute = null;

// ============================================================================
// LOAD ROUTES + OPENAPI SPEC
// ============================================================================

async function loadRoutes() {
    try {
        const [routesRes, openapiRes] = await Promise.all([
            fetch('/api/routes'),
            fetch('/openapi.json'),
        ]);

        if (!routesRes.ok) throw new Error(`/api/routes HTTP ${routesRes.status}`);
        const groups = await routesRes.json();

        if (openapiRes.ok) {
            const spec = await openapiRes.json();
            indexOpenApi(spec);
            openApiLoaded = true;
            console.log(`OpenAPI loaded — ${Object.keys(openApiCache).length} operations indexed`);
        } else {
            console.warn('OpenAPI spec not available — falling back to hand-curated metadata only');
        }

        renderRoutes(groups);
    } catch (error) {
        console.error('Error loading routes:', error);
        const list = document.getElementById('routesList');
        if (list) {
            list.innerHTML = `
                <div style="padding: 20px; color: #f87171;">
                    <strong>Error:</strong> ${error.message}
                </div>
            `;
        }
    }
}

// Index spec.paths into a flat lookup keyed by "METHOD /path"
function indexOpenApi(spec) {
    const paths = (spec && spec.paths) || {};
    for (const [path, ops] of Object.entries(paths)) {
        for (const [method, op] of Object.entries(ops)) {
            if (!['get', 'post', 'put', 'patch', 'delete', 'head', 'options'].includes(method)) continue;
            const key = `${method.toUpperCase()} ${path}`;
            openApiCache[key] = { operation: op, components: spec.components || {} };
        }
    }
}

// ============================================================================
// RENDER SIDEBAR
// ============================================================================

function renderRoutes(groups) {
    const container = document.getElementById('routesList');
    if (!container) return;
    container.innerHTML = '';

    let totalRoutes = 0;
    Object.entries(groups).forEach(([groupName, routes]) => {
        totalRoutes += routes.length;

        const groupDiv = document.createElement('div');
        groupDiv.className = 'route-group';

        const header = document.createElement('div');
        header.className = 'group-header collapsed';
        header.innerHTML = `
            <span>${groupName.replace(/_/g, ' ').toUpperCase()} (${routes.length})</span>
            <span class="arrow">▼</span>
        `;

        const routeList = document.createElement('div');
        routeList.className = 'route-list collapsed';

        routes.forEach(route => {
            const item = document.createElement('div');
            item.className = 'route-item';
            item.innerHTML = `
                <span class="method-badge method-${route.method}">${route.method}</span>
                <span class="route-path" title="${route.path}">${route.path}</span>
            `;
            item.addEventListener('click', (ev) => {
                showEndpoint(route.method, route.path, ev.currentTarget);
            });
            routeList.appendChild(item);
        });

        header.addEventListener('click', () => {
            header.classList.toggle('collapsed');
            routeList.classList.toggle('collapsed');
        });

        groupDiv.appendChild(header);
        groupDiv.appendChild(routeList);
        container.appendChild(groupDiv);
    });

    console.log(`Rendered ${totalRoutes} total routes in ${Object.keys(groups).length} groups`);
}

// ============================================================================
// OPENAPI HELPERS
// ============================================================================

function resolveRef(ref, components) {
    if (!ref || !ref.startsWith('#/components/')) return null;
    const parts = ref.replace('#/components/', '').split('/');
    let cur = components;
    for (const p of parts) {
        if (!cur) return null;
        cur = cur[p];
    }
    return cur || null;
}

// Build a JSON skeleton from a JSON Schema (for body placeholders)
function buildSkeleton(schema, components, depth = 0) {
    if (!schema || depth > 6) return null;
    if (schema.$ref) return buildSkeleton(resolveRef(schema.$ref, components), components, depth + 1);
    if (schema.example !== undefined) return schema.example;
    if (schema.default !== undefined) return schema.default;

    if (schema.anyOf) {
        const concrete = schema.anyOf.find(s => s.type !== 'null') || schema.anyOf[0];
        return buildSkeleton(concrete, components, depth + 1);
    }

    const type = schema.type || (schema.properties ? 'object' : null);

    switch (type) {
        case 'string':  return schema.enum ? schema.enum[0] : '';
        case 'integer':
        case 'number':  return 0;
        case 'boolean': return false;
        case 'array':   return [buildSkeleton(schema.items, components, depth + 1)].filter(v => v !== null);
        case 'object': {
            const out = {};
            const props = schema.properties || {};
            for (const [k, v] of Object.entries(props)) {
                out[k] = buildSkeleton(v, components, depth + 1);
            }
            return out;
        }
        default: return null;
    }
}

function getRequestBodySchema(operation, components) {
    const rb = operation.requestBody;
    if (!rb || !rb.content) return null;
    const json = rb.content['application/json'];
    if (!json || !json.schema) return null;
    if (json.schema.$ref) return resolveRef(json.schema.$ref, components);
    return json.schema;
}

function typeLabel(schema, components) {
    if (!schema) return 'any';
    if (schema.$ref) {
        const resolved = resolveRef(schema.$ref, components);
        return resolved && resolved.title ? resolved.title : 'object';
    }
    if (schema.anyOf) {
        return schema.anyOf
            .map(s => typeLabel(s, components))
            .filter(t => t !== 'null')
            .join(' | ') || 'any';
    }
    if (schema.enum) return `enum(${schema.enum.map(v => JSON.stringify(v)).join(', ')})`;
    if (schema.type === 'array') return `array<${typeLabel(schema.items, components)}>`;
    return schema.type || 'any';
}

function describeResponses(operation, components) {
    const responses = operation.responses || {};
    const rows = [];
    for (const [code, resp] of Object.entries(responses)) {
        let typeText = '';
        const json = resp.content && resp.content['application/json'];
        if (json && json.schema) typeText = typeLabel(json.schema, components);
        rows.push({ code, desc: resp.description || '', typeText });
    }
    return rows;
}

// ============================================================================
// SHOW ENDPOINT
// ============================================================================

function showEndpoint(method, path, clickedItem) {
    currentRoute = { method, path };

    document.querySelectorAll('.route-item').forEach(item => item.classList.remove('active'));
    if (clickedItem) clickedItem.classList.add('active');

    const cached = openApiCache[`${method} ${path}`];
    const op = cached ? cached.operation : null;
    const components = cached ? cached.components : {};
    const override = routeMetadata[path] || {};

    const description =
        override.description
        || (op && (op.description || op.summary))
        || (openApiLoaded
            ? '<em style="color:#94a3b8;">No description — add a docstring to the route handler.</em>'
            : 'OpenAPI spec unavailable.');

    const params = (op && op.parameters ? op.parameters : []).map(p => ({
        name: p.name,
        in: p.in,
        required: !!p.required,
        type: typeLabel(p.schema, components),
        description: p.description || (p.schema && p.schema.description) || '',
        default: p.schema ? p.schema.default : undefined,
    }));

    const bodySchema = op ? getRequestBodySchema(op, components) : null;
    const bodySkeleton = bodySchema ? buildSkeleton(bodySchema, components) : null;
    const responseRows = op ? describeResponses(op, components) : [];

    const content = document.querySelector('.content');

    content.innerHTML = `
        <div class="endpoint-header">
            <div style="display:flex;align-items:center;gap:15px;flex-wrap:wrap;">
                <span class="method-badge method-${method}" style="font-size:14px;padding:8px 16px;">${method}</span>
                <h1 class="endpoint-title">${path}</h1>
            </div>
            <p class="endpoint-description">${description}</p>
        </div>

        ${params.length > 0 ? renderParamsSection(params) : ''}
        ${bodySchema ? renderBodySchemaSection(bodySchema, components) : ''}
        ${responseRows.length > 0 ? renderResponsesSection(responseRows) : `
            <div class="section">
                <h2 class="section-title">📤 Returns</h2>
                <p style="color:var(--text-secondary);">${override.returns || 'See response when you call it.'}</p>
            </div>`}

        <div class="section">
            <h2 class="section-title">🧪 Try It Out</h2>
            <div class="try-it">
                <form id="apiForm">
                    ${renderTryItForm(method, params, bodySkeleton)}
                    <div style="margin-top:20px;">
                        <button type="submit" class="btn btn-primary">
                            <span id="submitText">Send Request</span>
                            <span id="submitLoading" style="display:none;" class="loading"></span>
                        </button>
                        <button type="button" class="btn btn-secondary" id="clearBtn">Clear</button>
                    </div>
                </form>

                <div id="responseContainer" class="response-container">
                    <div class="response-header">
                        <span>Response</span>
                        <span id="statusBadge" class="status-badge"></span>
                    </div>
                    <div class="response-body">
                        <pre id="responseBody"></pre>
                    </div>
                </div>
            </div>
        </div>
    `;

    document.getElementById('apiForm').addEventListener('submit', testEndpoint);
    document.getElementById('clearBtn').addEventListener('click', clearResponse);
}

function renderParamsSection(params) {
    return `
        <div class="section">
            <h2 class="section-title">📋 Parameters</h2>
            <table class="param-table">
                <thead>
                    <tr><th>Name</th><th>In</th><th>Type</th><th>Required</th><th>Description</th></tr>
                </thead>
                <tbody>
                    ${params.map(p => `
                        <tr>
                            <td><span class="param-name">${escapeHtml(p.name)}</span></td>
                            <td><code>${p.in}</code></td>
                            <td><code>${escapeHtml(p.type)}</code></td>
                            <td><span class="param-${p.required ? 'required' : 'optional'}">${p.required ? 'REQUIRED' : 'OPTIONAL'}</span></td>
                            <td>${escapeHtml(p.description)}${p.default !== undefined ? ` <em style="color:#94a3b8;">(default: ${escapeHtml(JSON.stringify(p.default))})</em>` : ''}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        </div>
    `;
}

function renderBodySchemaSection(schema, components) {
    const props = schema.properties || {};
    const required = new Set(schema.required || []);
    const rows = Object.entries(props).map(([name, sub]) => ({
        name,
        type: typeLabel(sub, components),
        required: required.has(name),
        description: sub.description || '',
        default: sub.default,
    }));

    if (rows.length === 0) return '';

    return `
        <div class="section">
            <h2 class="section-title">📦 Request Body</h2>
            <table class="param-table">
                <thead>
                    <tr><th>Field</th><th>Type</th><th>Required</th><th>Description</th></tr>
                </thead>
                <tbody>
                    ${rows.map(r => `
                        <tr>
                            <td><span class="param-name">${escapeHtml(r.name)}</span></td>
                            <td><code>${escapeHtml(r.type)}</code></td>
                            <td><span class="param-${r.required ? 'required' : 'optional'}">${r.required ? 'REQUIRED' : 'OPTIONAL'}</span></td>
                            <td>${escapeHtml(r.description)}${r.default !== undefined ? ` <em style="color:#94a3b8;">(default: ${escapeHtml(JSON.stringify(r.default))})</em>` : ''}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        </div>
    `;
}

function renderResponsesSection(rows) {
    return `
        <div class="section">
            <h2 class="section-title">📤 Responses</h2>
            <table class="param-table">
                <thead>
                    <tr><th>Status</th><th>Description</th><th>Type</th></tr>
                </thead>
                <tbody>
                    ${rows.map(r => `
                        <tr>
                            <td><code>${escapeHtml(r.code)}</code></td>
                            <td>${escapeHtml(r.desc)}</td>
                            <td>${r.typeText ? `<code>${escapeHtml(r.typeText)}</code>` : '—'}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        </div>
    `;
}

// ============================================================================
// TRY-IT FORM
// ============================================================================

function renderTryItForm(method, params, bodySkeleton) {
    const pathParams = params.filter(p => p.in === 'path');
    const queryParams = params.filter(p => p.in === 'query');
    const isBodyMethod = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method);

    let html = '';

    if (pathParams.length > 0) {
        html += '<div class="form-group"><label>Path parameters</label>';
        pathParams.forEach(p => {
            html += `
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
                    <span style="min-width:140px;color:var(--text-secondary);font-family:monospace;font-size:13px;">${escapeHtml(p.name)} *</span>
                    <input type="text" data-param-in="path" data-param-name="${escapeHtml(p.name)}"
                           placeholder="${escapeHtml(p.type)}" required style="flex:1;" />
                </div>
            `;
        });
        html += '</div>';
    }

    if (queryParams.length > 0) {
        html += '<div class="form-group"><label>Query parameters</label>';
        queryParams.forEach(p => {
            const def = p.default !== undefined ? p.default : '';
            html += `
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
                    <span style="min-width:140px;color:var(--text-secondary);font-family:monospace;font-size:13px;">
                        ${escapeHtml(p.name)}${p.required ? ' *' : ''}
                    </span>
                    <input type="text" data-param-in="query" data-param-name="${escapeHtml(p.name)}"
                           placeholder="${escapeHtml(p.type)}${p.default !== undefined ? ` (default: ${JSON.stringify(p.default)})` : ''}"
                           value="${escapeHtml(String(def))}" style="flex:1;" />
                </div>
            `;
        });
        html += '</div>';
    }

    if (isBodyMethod) {
        const placeholder = bodySkeleton ? JSON.stringify(bodySkeleton, null, 2) : '{}';
        html += `
            <div class="form-group">
                <label>Request body (JSON)</label>
                <textarea id="requestBody">${escapeHtml(placeholder)}</textarea>
            </div>
        `;
    }

    if (method === 'GET' && pathParams.length === 0 && queryParams.length === 0) {
        html += `
            <div class="form-group">
                <label>Query string (optional)</label>
                <input type="text" id="rawQuery" placeholder="?param=value" />
            </div>
        `;
    }

    if (!html) {
        html = '<p style="color:var(--text-secondary);">No parameters needed — click Send Request to test.</p>';
    }

    return html;
}

// ============================================================================
// SEND REQUEST
// ============================================================================

async function testEndpoint(event) {
    event.preventDefault();
    if (!currentRoute) return false;

    const { method, path } = currentRoute;
    const submitText = document.getElementById('submitText');
    const submitLoading = document.getElementById('submitLoading');
    const responseContainer = document.getElementById('responseContainer');
    const statusBadge = document.getElementById('statusBadge');
    const responseBody = document.getElementById('responseBody');

    submitText.style.display = 'none';
    submitLoading.style.display = 'inline-block';

    try {
        let url = path;

        // Substitute path parameters
        const pathInputs = document.querySelectorAll('input[data-param-in="path"]');
        pathInputs.forEach(inp => {
            const name = inp.dataset.paramName;
            const val = inp.value.trim();
            if (val === '') throw new Error(`Path parameter "${name}" is required`);
            url = url.replace(`{${name}}`, encodeURIComponent(val));
        });

        // Build query string from structured inputs
        const qsParts = [];
        document.querySelectorAll('input[data-param-in="query"]').forEach(inp => {
            const val = inp.value.trim();
            if (val !== '') {
                qsParts.push(`${encodeURIComponent(inp.dataset.paramName)}=${encodeURIComponent(val)}`);
            }
        });
        if (qsParts.length > 0) {
            url += (url.includes('?') ? '&' : '?') + qsParts.join('&');
        }

        // Free-form query fallback for parameter-less GETs
        const rawQuery = document.getElementById('rawQuery');
        if (rawQuery && rawQuery.value.trim()) {
            let extra = rawQuery.value.trim();
            if (!extra.startsWith('?')) extra = '?' + extra;
            url += extra;
        }

        const options = { method };
        if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
            const bodyEl = document.getElementById('requestBody');
            if (bodyEl && bodyEl.value.trim() && bodyEl.value.trim() !== '{}') {
                options.headers = { 'Content-Type': 'application/json' };
                options.body = bodyEl.value.trim();
            }
        }

        const t0 = performance.now();
        const response = await fetch(url, options);
        const dur = (performance.now() - t0).toFixed(0);

        const ct = response.headers.get('content-type') || '';
        const data = ct.includes('application/json') ? await response.json() : await response.text();

        responseContainer.style.display = 'block';
        statusBadge.textContent = `${response.status} ${response.statusText} (${dur}ms)`;
        statusBadge.className = `status-badge ${response.ok ? 'status-success' : 'status-error'}`;
        responseBody.textContent = (typeof data === 'object')
            ? JSON.stringify(data, null, 2)
            : data;

    } catch (error) {
        console.error('Error testing endpoint:', error);
        responseContainer.style.display = 'block';
        statusBadge.textContent = 'Error';
        statusBadge.className = 'status-badge status-error';
        responseBody.textContent = `Error: ${error.message}`;
    } finally {
        submitText.style.display = 'inline';
        submitLoading.style.display = 'none';
    }

    return false;
}

function clearResponse() {
    const c = document.getElementById('responseContainer');
    if (c) c.style.display = 'none';
}

// ============================================================================
// SEARCH
// ============================================================================

let searchTimeout;
function setupSearch() {
    const input = document.getElementById('searchInput');
    if (!input) return;
    input.addEventListener('input', (e) => {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => {
            const term = e.target.value.toLowerCase();
            document.querySelectorAll('.route-item').forEach(item => {
                const path = item.querySelector('.route-path').textContent.toLowerCase();
                const method = item.querySelector('.method-badge').textContent.toLowerCase();
                item.style.display = (path.includes(term) || method.includes(term)) ? 'flex' : 'none';
            });
            document.querySelectorAll('.route-group').forEach(group => {
                const visible = Array.from(group.querySelectorAll('.route-item'))
                    .some(item => item.style.display !== 'none');
                group.style.display = visible ? 'block' : 'none';
            });
        }, 150);
    });
}

// ============================================================================
// BULK EXPAND / COLLAPSE
// ============================================================================

function setupBulkToggles() {
    const expandBtn = document.getElementById('expandAllBtn');
    const collapseBtn = document.getElementById('collapseAllBtn');

    if (expandBtn) {
        expandBtn.addEventListener('click', () => {
            document.querySelectorAll('.group-header').forEach(h => h.classList.remove('collapsed'));
            document.querySelectorAll('.route-list').forEach(l => l.classList.remove('collapsed'));
        });
    }
    if (collapseBtn) {
        collapseBtn.addEventListener('click', () => {
            document.querySelectorAll('.group-header').forEach(h => h.classList.add('collapsed'));
            document.querySelectorAll('.route-list').forEach(l => l.classList.add('collapsed'));
        });
    }
}

// ============================================================================
// UTILITIES
// ============================================================================

function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// ============================================================================
// BOOT
// ============================================================================

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        loadRoutes();
        setupSearch();
        setupBulkToggles();
    });
} else {
    loadRoutes();
    setupSearch();
    setupBulkToggles();
}