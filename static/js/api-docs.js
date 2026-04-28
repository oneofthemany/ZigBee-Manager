// api-docs.js — ZigBee & Matter Manager API Explorer
// Adapted from the pyCroft version. Routes come from FastAPI introspection
// at /api/routes; route metadata is best-effort (hand-curated below for
// commonly used endpoints, generic try-it form for everything else).
console.log('ZMM API Docs JS loaded');

// ============================================================================
// OPTIONAL HAND-CURATED METADATA
// Add entries here as you wish. The page works fine without them.
// Format mirrors pyCroft: { description, params: [], returns, example }
// ============================================================================
const routeMetadata = {
    '/api/routes': {
        description: 'JSON list of every documented route grouped by tag/prefix.',
        params: [],
        returns: 'Object: { groupName: [{method, path}, ...] }',
        example: null
    },
    '/routes': {
        description: 'Plain HTML route listing — handy for quick eyeballing.',
        params: [],
        returns: 'HTML page',
        example: null
    },
    '/api/setup/status': {
        description: 'Check if the dongle setup wizard should be shown.',
        params: [],
        returns: '{ needs_setup: bool, reason: string }',
        example: null
    },
    '/api/system/health': {
        description: 'Lightweight health check — used by the restore polling loop.',
        params: [],
        returns: '{ status: "ok", ... }',
        example: null
    },
    '/api/upgrade/status': {
        description: 'Combined upgrade settings + live host status.',
        params: [],
        returns: 'Object describing current/previous version, last check, build state.',
        example: null
    },
    // Add more as you go — anything not listed gets a generic try-it form.
};

let currentRoute = null;

// ============================================================================
// LOAD ROUTES
// ============================================================================

async function loadRoutes() {
    console.log('Loading routes from /api/routes...');
    try {
        const response = await fetch('/api/routes');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data = await response.json();
        console.log('Routes data received, group count:', Object.keys(data).length);
        renderRoutes(data);
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

// ============================================================================
// RENDER SIDEBAR
// ============================================================================

function renderRoutes(groups) {
    const container = document.getElementById('routesList');
    if (!container) {
        console.error('routesList container not found!');
        return;
    }
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
// SHOW ENDPOINT
// ============================================================================

function showEndpoint(method, path, clickedItem) {
    currentRoute = { method, path };

    document.querySelectorAll('.route-item').forEach(item => item.classList.remove('active'));
    if (clickedItem) clickedItem.classList.add('active');

    const metadata = routeMetadata[path] || {
        description: 'No hand-curated documentation for this endpoint yet — but you can still try it below.',
        params: [],
        returns: 'See response when you call it.',
        example: null
    };

    const content = document.querySelector('.content');
    const isBodyMethod = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method);

    content.innerHTML = `
        <div class="endpoint-header">
            <div style="display:flex;align-items:center;gap:15px;flex-wrap:wrap;">
                <span class="method-badge method-${method}" style="font-size:14px;padding:8px 16px;">${method}</span>
                <h1 class="endpoint-title">${path}</h1>
            </div>
            <p class="endpoint-description">${metadata.description}</p>
        </div>

        ${metadata.params && metadata.params.length > 0 ? `
        <div class="section">
            <h2 class="section-title">📋 Parameters</h2>
            <table class="param-table">
                <thead>
                    <tr><th>Name</th><th>Type</th><th>Required</th><th>Description</th></tr>
                </thead>
                <tbody>
                    ${metadata.params.map(p => `
                        <tr>
                            <td><span class="param-name">${p.name}</span></td>
                            <td><code>${p.type}</code></td>
                            <td><span class="param-${p.required ? 'required' : 'optional'}">${p.required ? 'REQUIRED' : 'OPTIONAL'}</span></td>
                            <td>${p.description || ''}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        </div>` : ''}

        <div class="section">
            <h2 class="section-title">📤 Returns</h2>
            <p style="color:var(--text-secondary);">${metadata.returns}</p>
        </div>

        <div class="section">
            <h2 class="section-title">🧪 Try It Out</h2>
            <div class="try-it">
                <form id="apiForm">
                    ${method === 'GET' ? `
                        <div class="form-group">
                            <label>Query parameters (optional)</label>
                            <input type="text" id="queryParams"
                                   placeholder="?param1=value1&amp;param2=value2"
                                   value="${typeof metadata.example === 'string' ? metadata.example : ''}" />
                            <small style="color:var(--text-secondary);display:block;margin-top:5px;">
                                Format: <code>?param1=value1&amp;param2=value2</code>
                            </small>
                        </div>
                    ` : isBodyMethod ? `
                        <div class="form-group">
                            <label>Request body (JSON)</label>
                            <textarea id="requestBody" placeholder='{}'>${
                                metadata.example && typeof metadata.example === 'object'
                                    ? JSON.stringify(metadata.example, null, 2)
                                    : '{}'
                            }</textarea>
                        </div>
                    ` : '<p>No parameters needed — click <em>Send Request</em> to test.</p>'}

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

// ============================================================================
// TEST ENDPOINT
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
        const options = { method };

        if (method === 'GET') {
            const qp = document.getElementById('queryParams');
            if (qp && qp.value.trim()) {
                let extra = qp.value.trim();
                if (!extra.startsWith('?')) extra = '?' + extra;
                url += extra;
            }
        } else if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
            const body = document.getElementById('requestBody');
            if (body && body.value.trim() && body.value.trim() !== '{}') {
                options.headers = { 'Content-Type': 'application/json' };
                options.body = body.value.trim();
            }
        }

        const t0 = performance.now();
        const response = await fetch(url, options);
        const dur = (performance.now() - t0).toFixed(0);

        let data;
        const ct = response.headers.get('content-type') || '';
        if (ct.includes('application/json')) {
            data = await response.json();
        } else {
            data = await response.text();
        }

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