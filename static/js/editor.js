/**
 * Code Editor Module
 * static/js/editor.js
 *
 * Monaco Editor-based IDE for editing backend code from the web UI.
 * Monaco is loaded from CDN on first use (no build step needed).
 */

let monacoReady = false;
let editorInstance = null;
let currentFile = null;
let unsavedChanges = false;
let fileTree = [];

const MONACO_CDN = 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.45.0/min';

// ============================================================================
// INITIALISATION
// ============================================================================

export async function initEditor() {
    const container = document.getElementById('editorContainer');
    if (!container) return;

    container.innerHTML = buildEditorHTML();

    await loadMonaco();
    await loadFileTree();

    // Keyboard shortcuts
    document.addEventListener('keydown', e => {
        if ((e.ctrlKey || e.metaKey) && e.key === 's' && currentFile) {
            e.preventDefault();
            saveCurrentFile();
        }
    });

    // Check for pending test on init
        await checkPendingTest();
}

async function checkPendingTest() {
    try {
        const res = await fetch('/api/editor/test-status');
        const data = await res.json();
        if (data.pending) {
            showTestRecoveryBanner('pending', data.remaining);
        }
    } catch (e) {
        // Service might be restarting — ignore
    }
}

export function getEditorInstance() {
    return editorInstance;
}

function buildEditorHTML() {
    return `
    <div class="editor-layout d-flex" style="height: calc(100vh - 180px); min-height: 500px; max-height: calc(100vh - 180px); overflow: hidden;">
        <!-- Sidebar -->
        <div class="editor-sidebar border-end" style="width: 260px; min-width: 200px; overflow-y: auto; background: #1e1e1e;">
            <!-- Search -->
            <div class="p-2 border-bottom" style="background: #252526;">
                <div class="input-group input-group-sm">
                    <input type="text" class="form-control form-control-sm bg-dark text-light border-secondary"
                           id="editorSearch" placeholder="Search files..."
                           style="font-size: 12px;"
                           onkeydown="if(event.key==='Enter') window.editorSearchFiles(this.value)">
                    <button class="btn btn-outline-secondary btn-sm" onclick="window.editorSearchFiles(document.getElementById('editorSearch').value)">
                        <i class="fas fa-search"></i>
                    </button>
                </div>
            </div>
            <!-- File tree -->
            <div id="editorFileTree" class="p-1" style="font-size: 12px;"></div>
        </div>

        <!-- Main editor area -->
        <div class="flex-grow-1 d-flex flex-column" style="min-width: 0;">
            <!-- Tab bar -->
            <div class="editor-tabs d-flex align-items-center border-bottom px-2"
                 style="height: 36px; background: #252526; overflow-x: auto; white-space: nowrap;">
                <div id="editorTabBar" class="d-flex align-items-center gap-1"></div>
                <div class="ms-auto d-flex gap-1">
                    <button class="btn btn-sm btn-outline-success py-0 px-2" style="font-size: 11px;"
                            onclick="window.editorValidate()" title="Validate syntax" id="editorValidateBtn" disabled>
                        <i class="fas fa-check-circle"></i>
                    </button>
                    <button class="btn btn-sm btn-outline-light py-0 px-2" style="font-size: 11px;"
                            onclick="window.editorSave()" title="Save (Ctrl+S)" id="editorSaveBtn" disabled>
                        <i class="fas fa-save"></i>
                    </button>
                    <button class="btn btn-sm btn-outline-danger py-0 px-2" style="font-size: 11px;"
                            onclick="window.editorTestDeploy()" title="Test Deploy (with rollback)" id="editorTestBtn" disabled>
                        <i class="fas fa-flask"></i> Test
                    </button>
                    <button class="btn btn-sm btn-outline-warning py-0 px-2" style="font-size: 11px;"
                            onclick="window.editorShowBackups()" title="Backups">
                        <i class="fas fa-history"></i>
                    </button>
                    <button class="btn btn-sm btn-outline-info py-0 px-2" style="font-size: 11px;"
                            onclick="window.editorGlobalSearch()" title="Search in files">
                        <i class="fas fa-search"></i>
                    </button>
                </div>
            </div>

            <!-- Monaco container -->
            <div id="monacoContainer" class="flex-grow-1 position-relative" style="background: #1e1e1e;">
                <div class="d-flex align-items-center justify-content-center h-100 text-muted"
                     id="editorPlaceholder">
                    <div class="text-center">
                        <i class="fas fa-code fa-3x mb-3 opacity-25"></i>
                        <div style="font-size: 14px;">Select a file from the sidebar to begin editing</div>
                        <div class="mt-2" style="font-size: 11px; color: #666;">
                            Ctrl+S to save &bull; All saves create automatic backups
                        </div>
                    </div>
                </div>
            </div>

            <!-- Status bar -->
            <div class="editor-status d-flex align-items-center justify-content-between px-3"
                 style="height: 24px; background: #007acc; color: #fff; font-size: 11px;">
                <div id="editorStatusLeft">
                    <span id="editorFileName">No file open</span>
                </div>
                <div id="editorStatusRight" class="d-flex gap-3">
                    <span id="editorValidationStatus"></span>
                    <span id="editorCursorPos">Ln 1, Col 1</span>
                    <span id="editorLanguage">-</span>
                    <span id="editorFileSize">-</span>
                </div>
            </div>
        </div>
    </div>

    <!-- Search results modal -->
    <div class="modal fade" id="editorSearchModal" tabindex="-1">
        <div class="modal-dialog modal-lg">
            <div class="modal-content bg-dark text-light">
                <div class="modal-header border-secondary py-2">
                    <h6 class="modal-title"><i class="fas fa-search me-1"></i> Search Results</h6>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body" id="editorSearchResults" style="max-height: 400px; overflow-y: auto; font-size: 12px;">
                </div>
            </div>
        </div>
    </div>

    <!-- Backups modal -->
    <div class="modal fade" id="editorBackupsModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content bg-dark text-light">
                <div class="modal-header border-secondary py-2">
                    <h6 class="modal-title"><i class="fas fa-history me-1"></i> File Backups</h6>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body" id="editorBackupsList" style="max-height: 400px; overflow-y: auto; font-size: 12px;">
                </div>
            </div>
        </div>
    </div>
    `;
}

// ============================================================================
// MONACO LOADER
// ============================================================================

async function loadMonaco() {
    if (monacoReady) return;

    return new Promise((resolve, reject) => {
        const loaderScript = document.createElement('script');
        loaderScript.src = `${MONACO_CDN}/vs/loader.min.js`;
        loaderScript.onload = () => {
            window.require.config({ paths: { vs: `${MONACO_CDN}/vs` } });
            window.require(['vs/editor/editor.main'], () => {
                monacoReady = true;
                resolve();
            });
        };
        loaderScript.onerror = () => reject(new Error('Failed to load Monaco Editor'));
        document.head.appendChild(loaderScript);
    });
}

function createEditor(container, content, language) {
    if (editorInstance) {
        editorInstance.dispose();
    }

    const placeholder = document.getElementById('editorPlaceholder');
    if (placeholder) placeholder.style.display = 'none';

    let wrapper = document.getElementById('monacoWrapper');
    if (!wrapper) {
        wrapper = document.createElement('div');
        wrapper.id = 'monacoWrapper';
        wrapper.style.cssText = 'position:absolute;top:0;left:0;right:0;bottom:0;';
        container.appendChild(wrapper);
    }

    editorInstance = monaco.editor.create(wrapper, {
        value: content,
        language: language,
        theme: 'vs-dark',
        fontSize: 13,
        fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Consolas', monospace",
        minimap: { enabled: true, scale: 2 },
        scrollBeyondLastLine: false,
        wordWrap: 'off',
        lineNumbers: 'on',
        renderWhitespace: 'selection',
        bracketPairColorization: { enabled: true },
        automaticLayout: true,
        tabSize: 4,
        insertSpaces: true,
        smoothScrolling: true,
        cursorBlinking: 'smooth',
        cursorSmoothCaretAnimation: 'on',
        padding: { top: 8 },
    });

    // Track cursor position
    editorInstance.onDidChangeCursorPosition(e => {
        const pos = e.position;
        const el = document.getElementById('editorCursorPos');
        if (el) el.textContent = `Ln ${pos.lineNumber}, Col ${pos.column}`;
    });

    // Track unsaved changes + auto-validate on pause
    let validateTimer = null;
    editorInstance.onDidChangeModelContent(() => {
        if (!unsavedChanges) {
            unsavedChanges = true;
            updateTabDirtyState();
        }
        // Auto-validate 1.5s after stop typing
        if (validateTimer) clearTimeout(validateTimer);
        validateTimer = setTimeout(() => {
            validateCurrentFile();
        }, 1500);
    });

    // Ctrl+S
    editorInstance.addAction({
        id: 'save-file',
        label: 'Save File',
        keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS],
        run: () => saveCurrentFile(),
    });

    // Force Monaco to recalculate dimensions
    setTimeout(() => {
        if (editorInstance) editorInstance.layout();
    }, 100);

    return editorInstance;
}

// ============================================================================
// FILE TREE
// ============================================================================

async function loadFileTree() {
    try {
        const res = await fetch('/api/editor/tree');
        const data = await res.json();
        if (!data.success) return;

        fileTree = data.tree;
        renderFileTree(data.tree);
    } catch (e) {
        console.error('Failed to load file tree:', e);
    }
}

function renderFileTree(tree) {
    const container = document.getElementById('editorFileTree');
    if (!container) return;

    container.innerHTML = tree.map(dir => {
        const children = (dir.children || []).map(item => {
            if (item.is_dir) {
                return `<div class="tree-folder ms-2 mt-1">
                    <div class="tree-label text-muted" style="cursor:pointer;" onclick="window.editorToggleFolder(this)">
                        <i class="fas fa-folder fa-fw me-1" style="color:#dcb67a;"></i>${item.name}
                    </div>
                    <div class="tree-children ms-2" style="display:none;"></div>
                </div>`;
            }
            const icon = getFileIcon(item.extension);
            const active = currentFile === item.path ? 'background:#37373d;' : '';
            return `<div class="tree-file py-1 px-2 rounded" style="cursor:pointer;${active}"
                         onclick="window.editorOpenFile('${item.path}')"
                         onmouseover="this.style.background='#2a2d2e'"
                         onmouseout="this.style.background='${currentFile === item.path ? '#37373d' : ''}'">
                <i class="${icon} fa-fw me-1"></i>
                <span class="text-light">${item.name}</span>
                <span class="text-muted ms-1" style="font-size:10px;">${formatSize(item.size)}</span>
            </div>`;
        }).join('');

        const dirLabel = dir.path === '.' ? 'PROJECT ROOT' : dir.name.toUpperCase();
        return `<div class="tree-section mb-2">
            <div class="text-uppercase px-2 py-1" style="font-size:10px;letter-spacing:1px;color:#888;cursor:pointer;"
                 onclick="this.nextElementSibling.style.display = this.nextElementSibling.style.display === 'none' ? '' : 'none'">
                <i class="fas fa-chevron-down fa-xs me-1"></i>${dirLabel}
            </div>
            <div>${children}</div>
        </div>`;
    }).join('');
}

// ============================================================================
// FILE OPERATIONS
// ============================================================================

window.editorOpenFile = async function(path) {
    if (unsavedChanges && !confirm('You have unsaved changes. Discard?')) return;

    try {
        const res = await fetch(`/api/editor/file?path=${encodeURIComponent(path)}`);
        const data = await res.json();
        if (!data.success) {
            alert('Error: ' + data.error);
            return;
        }

        currentFile = path;
        unsavedChanges = false;

        const container = document.getElementById('monacoContainer');
        createEditor(container, data.content, data.language);

        // Update status bar
        document.getElementById('editorFileName').textContent = path;
        document.getElementById('editorLanguage').textContent = data.language;
        document.getElementById('editorFileSize').textContent = formatSize(data.size);
        document.getElementById('editorSaveBtn').disabled = false;
        document.getElementById('editorValidateBtn').disabled = false;
        document.getElementById('editorTestBtn').disabled = false;

        // Clear previous validation status
        const valStatus = document.getElementById('editorValidationStatus');
        if (valStatus) valStatus.innerHTML = '';

        // Update tab
        updateTabBar();

        // Re-render tree to highlight active file
        renderFileTree(fileTree);

        // Run initial validation
        setTimeout(() => validateCurrentFile(), 500);

    } catch (e) {
        alert('Failed to open file: ' + e.message);
    }
};

// ============================================================================
// VALIDATION
// ============================================================================

async function validateCurrentFile() {
    if (!currentFile || !editorInstance) return null;

    const content = editorInstance.getValue();
    const model = editorInstance.getModel();
    if (!model) return null;

    try {
        const res = await fetch('/api/editor/validate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                content,
                path: currentFile,
            })
        });
        const data = await res.json();

        if (!data.success) return null;

        // Map errors to Monaco markers
        const markers = (data.errors || []).map(err => ({
            severity: {
                'error': monaco.MarkerSeverity.Error,
                'warning': monaco.MarkerSeverity.Warning,
                'info': monaco.MarkerSeverity.Info,
            }[err.severity] || monaco.MarkerSeverity.Info,
            startLineNumber: err.line || 1,
            startColumn: err.column || 1,
            endLineNumber: err.endLine || err.line || 1,
            endColumn: err.endColumn || (err.column ? err.column + 10 : 100),
            message: err.message,
            source: 'Zigbee Manager',
        }));

        // Set markers on the model
        monaco.editor.setModelMarkers(model, 'validator', markers);

        // Update status bar
        updateValidationStatus(data);

        return data;
    } catch (e) {
        console.error('Validation failed:', e);
        return null;
    }
}

function updateValidationStatus(result) {
    const el = document.getElementById('editorValidationStatus');
    if (!el) return;

    if (!result || !result.errors) {
        el.innerHTML = '';
        return;
    }

    const errorCount = result.errors.filter(e => e.severity === 'error').length;
    const warnCount = result.errors.filter(e => e.severity === 'warning').length;
    const infoCount = result.errors.filter(e => e.severity === 'info').length;

    if (errorCount === 0 && warnCount === 0) {
        el.innerHTML = `<span style="color:#4ec9b0;">✓ Valid</span>`;
    } else {
        let parts = [];
        if (errorCount) parts.push(`<span style="color:#f44747;">${errorCount} error${errorCount > 1 ? 's' : ''}</span>`);
        if (warnCount) parts.push(`<span style="color:#cca700;">${warnCount} warning${warnCount > 1 ? 's' : ''}</span>`);
        if (infoCount) parts.push(`<span style="color:#75beff;">${infoCount} info</span>`);
        el.innerHTML = parts.join(' · ');
    }
}

window.editorValidate = validateCurrentFile;

// ============================================================================
// SAVE
// ============================================================================

async function saveCurrentFile() {
    if (!currentFile || !editorInstance) return;

    // Validate first
    const validation = await validateCurrentFile();

    // Block save on syntax errors (with override option)
    if (validation && !validation.valid) {
        const errorCount = validation.errors.filter(e => e.severity === 'error').length;
        if (errorCount > 0) {
            const proceed = confirm(
                `${errorCount} syntax error${errorCount > 1 ? 's' : ''} found.\n\n` +
                `Saving broken code may cause the application to fail on restart.\n\n` +
                `Save anyway?`
            );
            if (!proceed) return;
        }
    }

    const content = editorInstance.getValue();
    const btn = document.getElementById('editorSaveBtn');

    try {
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; }

        const res = await fetch('/api/editor/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: currentFile, content, create_backup: true })
        });
        const data = await res.json();

        if (data.success) {
            unsavedChanges = false;
            updateTabDirtyState();
            document.getElementById('editorFileSize').textContent = formatSize(data.size);
            const statusBar = document.querySelector('.editor-status');
            if (statusBar) {
                statusBar.style.background = '#16825d';
                setTimeout(() => statusBar.style.background = '#007acc', 1500);
            }
        } else {
            alert('Save failed: ' + data.error);
        }
    } catch (e) {
        alert('Save error: ' + e.message);
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-save"></i>'; }
    }
}

window.editorSave = saveCurrentFile;


// ---- TEST DEPLOY ----

window.editorTestDeploy = async function() {
    if (!currentFile || !editorInstance) return;

    // Validate first
    const validation = await validateCurrentFile();
    if (validation && !validation.valid) {
        const errorCount = validation.errors.filter(e => e.severity === 'error').length;
        if (errorCount > 0) {
            alert(`Cannot test-deploy: ${errorCount} syntax error(s) found.\nFix errors before testing.`);
            return;
        }
    }

    const ext = currentFile.split('.').pop().toLowerCase();
    const isPython = ['py', 'yaml', 'yml'].includes(ext);
    const actionDesc = isPython ? 'restart the service' : 'reload the page';

    if (!confirm(
        `Test Deploy: ${currentFile}\n\n` +
        `This will:\n` +
        `• Create a backup of the current file\n` +
        `• Save your changes\n` +
        `• ${isPython ? 'Restart the service' : 'Reload frontend assets'}\n\n` +
        `You will have 120 seconds to confirm the changes work.\n` +
        `If you don\'t confirm (or the service fails), changes are automatically rolled back.\n\n` +
        `Proceed?`
    )) return;

    try {
        const content = editorInstance.getValue();
        const res = await fetch('/api/editor/test-deploy', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: currentFile, content })
        });
        const data = await res.json();

        if (!data.success) {
            alert('Test deploy failed: ' + data.error);
            return;
        }

        if (data.action === 'restart') {
            // Python file — trigger restart, page will reload
            showTestRecoveryBanner('restarting', data.timeout);
            await fetch('/api/editor/test-restart', { method: 'POST' });
            // Service will restart — page will disconnect and reconnect
        } else {
            // Frontend file — show confirmation banner, trigger reload
            showTestRecoveryBanner('pending', data.timeout);
            // Reload after brief delay to show the banner
            setTimeout(() => {
                window.location.reload();
            }, 1500);
        }

    } catch (e) {
        alert('Test deploy error: ' + e.message);
    }
};


// ---- CONFIRM / ROLLBACK ----

window.editorTestConfirm = async function() {
    try {
        const res = await fetch('/api/editor/test-confirm', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            hideTestRecoveryBanner();
            // Brief success indicator
            const banner = document.getElementById('testRecoveryBanner');
            if (banner) {
                banner.innerHTML = `
                    <div class="d-flex align-items-center justify-content-center gap-2 py-2"
                         style="background:#16825d;color:#fff;font-size:13px;">
                        <i class="fas fa-check-circle"></i> Changes confirmed and kept!
                    </div>
                `;
                setTimeout(() => banner.remove(), 3000);
            }
        } else {
            alert('Confirm failed: ' + data.error);
        }
    } catch (e) {
        alert('Confirm error: ' + e.message);
    }
};

window.editorTestRollback = async function() {
    if (!confirm('Rollback to the previous version?\n\nIf this was a Python file, the service will restart again.')) return;

    try {
        const res = await fetch('/api/editor/test-rollback', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            hideTestRecoveryBanner();
            alert('Rolled back: ' + data.message);
            if (data.message && data.message.includes('restart')) {
                // Service restarting — wait for reconnect
            } else {
                window.location.reload();
            }
        } else {
            alert('Rollback failed: ' + data.error);
        }
    } catch (e) {
        alert('Rollback error: ' + e.message);
    }
};


// ---- BANNER UI ----

function showTestRecoveryBanner(status, timeout) {
    let banner = document.getElementById('testRecoveryBanner');
    if (!banner) {
        banner = document.createElement('div');
        banner.id = 'testRecoveryBanner';
        banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;';
        document.body.prepend(banner);
    }

    if (status === 'restarting') {
        banner.innerHTML = `
            <div class="d-flex align-items-center justify-content-center gap-2 py-2"
                 style="background:#dc3545;color:#fff;font-size:13px;">
                <i class="fas fa-spinner fa-spin"></i>
                Service restarting... Page will reconnect automatically.
            </div>
        `;
        return;
    }

    // Pending confirmation
    let remaining = timeout || 120;
    banner.innerHTML = buildBannerHTML(remaining);

    // Countdown
    const interval = setInterval(() => {
        remaining--;
        if (remaining <= 0) {
            clearInterval(interval);
            banner.innerHTML = `
                <div class="d-flex align-items-center justify-content-center gap-2 py-2"
                     style="background:#dc3545;color:#fff;font-size:13px;">
                    <i class="fas fa-undo"></i> Timeout — rolling back automatically...
                </div>
            `;
            return;
        }
        const countdownEl = document.getElementById('testCountdown');
        if (countdownEl) countdownEl.textContent = remaining;

        // Flash red in last 30 seconds
        const bar = document.getElementById('testProgressBar');
        if (bar) {
            const pct = (remaining / (timeout || 120)) * 100;
            bar.style.width = pct + '%';
            if (remaining <= 30) bar.classList.add('bg-danger');
        }
    }, 1000);

    banner._interval = interval;
}

function buildBannerHTML(remaining) {
    return `
        <div style="background:#1e1e1e;border-bottom:2px solid #dc3545;padding:8px 16px;">
            <div class="d-flex align-items-center justify-content-between">
                <div class="d-flex align-items-center gap-2" style="color:#fff;font-size:13px;">
                    <i class="fas fa-flask" style="color:#ffc107;"></i>
                    <strong>Test Deploy Active</strong>
                    <span class="text-muted">—</span>
                    <span style="color:#ffc107;">
                        <span id="testCountdown">${remaining}</span>s remaining
                    </span>
                </div>
                <div class="d-flex gap-2">
                    <button class="btn btn-sm btn-success px-3" onclick="window.editorTestConfirm()">
                        <i class="fas fa-check me-1"></i> Confirm — Keep Changes
                    </button>
                    <button class="btn btn-sm btn-danger px-3" onclick="window.editorTestRollback()">
                        <i class="fas fa-undo me-1"></i> Rollback
                    </button>
                </div>
            </div>
            <div class="progress mt-1" style="height:3px;">
                <div id="testProgressBar" class="progress-bar bg-warning" style="width:100%;transition:width 1s linear;"></div>
            </div>
        </div>
    `;
}

export function hideTestRecoveryBanner() {
    const banner = document.getElementById('testRecoveryBanner');
    if (banner) {
        if (banner._interval) clearInterval(banner._interval);
        banner.remove();
    }
}

// ============================================================================
// SEARCH
// ============================================================================

window.editorSearchFiles = async function(query) {
    if (!query || query.length < 2) return;

    try {
        const res = await fetch(`/api/editor/search?query=${encodeURIComponent(query)}`);
        const data = await res.json();

        const resultsEl = document.getElementById('editorSearchResults');
        if (!data.success || !data.results.length) {
            resultsEl.innerHTML = '<div class="text-muted text-center py-3">No results found</div>';
        } else {
            resultsEl.innerHTML = data.results.map(r => `
                <div class="search-result px-2 py-1 rounded" style="cursor:pointer;"
                     onmouseover="this.style.background='#37373d'"
                     onmouseout="this.style.background=''"
                     onclick="window.editorOpenFileAtLine('${r.path}', ${r.line})">
                    <div>
                        <span class="text-info">${r.path}</span>
                        <span class="text-muted">:${r.line}</span>
                    </div>
                    <div class="text-muted" style="font-size: 11px; font-family: monospace;">
                        ${escapeHtml(r.text)}
                    </div>
                </div>
            `).join('');
            if (data.truncated) {
                resultsEl.innerHTML += '<div class="text-warning text-center py-2 small">Results truncated at 100 matches</div>';
            }
        }

        new bootstrap.Modal(document.getElementById('editorSearchModal')).show();
    } catch (e) {
        alert('Search failed: ' + e.message);
    }
};

window.editorGlobalSearch = function() {
    const query = prompt('Search across all project files:');
    if (query) window.editorSearchFiles(query);
};

window.editorOpenFileAtLine = async function(path, line) {
    bootstrap.Modal.getInstance(document.getElementById('editorSearchModal'))?.hide();
    await window.editorOpenFile(path);
    if (editorInstance) {
        editorInstance.revealLineInCenter(line);
        editorInstance.setPosition({ lineNumber: line, column: 1 });
        editorInstance.focus();
    }
};

// ============================================================================
// BACKUPS
// ============================================================================

window.editorShowBackups = async function() {
    const path = currentFile || null;
    try {
        const url = path ? `/api/editor/backups?path=${encodeURIComponent(path)}` : '/api/editor/backups';
        const res = await fetch(url);
        const data = await res.json();

        const el = document.getElementById('editorBackupsList');
        if (!data.success || !data.backups.length) {
            el.innerHTML = '<div class="text-muted text-center py-3">No backups found</div>';
        } else {
            el.innerHTML = data.backups.map(b => `
                <div class="d-flex justify-content-between align-items-center py-1 px-2 rounded"
                     onmouseover="this.style.background='#37373d'" onmouseout="this.style.background=''">
                    <div>
                        <div class="text-light" style="font-size: 12px;">${b.name}</div>
                        <div class="text-muted" style="font-size: 10px;">${formatSize(b.size)} &bull; ${new Date(b.created * 1000).toLocaleString()}</div>
                    </div>
                    ${currentFile ? `<button class="btn btn-sm btn-outline-warning py-0 px-2"
                             onclick="window.editorRestoreBackup('${b.name}', '${currentFile}')">
                        Restore
                    </button>` : ''}
                </div>
            `).join('');
        }

        new bootstrap.Modal(document.getElementById('editorBackupsModal')).show();
    } catch (e) {
        alert('Failed to load backups: ' + e.message);
    }
};

window.editorRestoreBackup = async function(backupName, targetPath) {
    if (!confirm(`Restore ${targetPath} from backup?\nA new backup of the current version will be created first.`)) return;

    if (editorInstance) {
        await fetch('/api/editor/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: targetPath, content: editorInstance.getValue(), create_backup: true })
        });
    }

    const res = await fetch('/api/editor/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backup: backupName, path: targetPath })
    });
    const data = await res.json();

    if (data.success) {
        bootstrap.Modal.getInstance(document.getElementById('editorBackupsModal'))?.hide();
        await window.editorOpenFile(targetPath);
    } else {
        alert('Restore failed: ' + data.error);
    }
};

// ============================================================================
// FOLDER TOGGLE
// ============================================================================

window.editorToggleFolder = async function(el) {
    const childrenDiv = el.nextElementSibling;
    if (!childrenDiv) return;

    if (childrenDiv.style.display === 'none') {
        if (!childrenDiv.innerHTML.trim()) {
            const folderPath = el.textContent.trim();
            for (const dir of fileTree) {
                if (dir.name === folderPath || dir.path === folderPath) {
                    break;
                }
            }
        }
        childrenDiv.style.display = '';
        el.querySelector('i').className = 'fas fa-folder-open fa-fw me-1';
        el.querySelector('i').style.color = '#dcb67a';
    } else {
        childrenDiv.style.display = 'none';
        el.querySelector('i').className = 'fas fa-folder fa-fw me-1';
    }
};

// ============================================================================
// UI HELPERS
// ============================================================================

function updateTabBar() {
    const bar = document.getElementById('editorTabBar');
    if (!bar || !currentFile) return;

    const name = currentFile.split('/').pop();
    const icon = getFileIcon('.' + name.split('.').pop());
    bar.innerHTML = `
        <div class="editor-tab d-flex align-items-center gap-1 px-2 py-1 rounded"
             style="background:#1e1e1e; color:#fff; font-size:12px;">
            <i class="${icon} fa-xs"></i>
            <span id="editorTabName">${name}</span>
            <span id="editorTabDirty" style="display:none; color:#e8e8e8;">●</span>
        </div>
    `;
}

function updateTabDirtyState() {
    const dot = document.getElementById('editorTabDirty');
    if (dot) dot.style.display = unsavedChanges ? 'inline' : 'none';
}

function getFileIcon(ext) {
    const icons = {
        '.py': 'fab fa-python', '.js': 'fab fa-js-square',
        '.css': 'fab fa-css3-alt', '.html': 'fab fa-html5',
        '.yaml': 'fas fa-cog', '.yml': 'fas fa-cog',
        '.json': 'fas fa-brackets-curly', '.md': 'fas fa-file-alt',
        '.sh': 'fas fa-terminal', '.txt': 'fas fa-file-alt',
    };
    return icons[ext] || 'fas fa-file';
}

function formatSize(bytes) {
    if (bytes < 1024) return bytes + 'B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + 'KB';
    return (bytes / (1024 * 1024)).toFixed(1) + 'MB';
}

function escapeHtml(str) {
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

window.checkPendingTest = checkPendingTest;