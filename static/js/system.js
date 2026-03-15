/**
 * System & Configuration Management
 * Handles config.yaml editing and system restart
 */

import { state } from './state.js';

/**
 * Load configuration YAML into editor
 */
export async function loadConfigYaml() {
    const editor = document.getElementById('configEditor');
    if (!editor) return;

    try {
        const res = await fetch('/api/config');
        const data = await res.json();
        if (data.success) editor.value = data.content;
    } catch (e) {
        // Silent fail
    }
}

/**
 * Save configuration YAML
 */
export async function saveConfigYaml() {
    const editor = document.getElementById('configEditor');
    if (!editor || !confirm("Save?")) return;

    await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: editor.value })
    });
    alert("Saved");
}

/**
 * Restart the Zigbee service
 */
export async function restartSystem() {
    if (!confirm("Restart?")) return;

    state.isRestarting = true;
    await fetch('/api/system/restart', { method: 'POST' });
    setTimeout(() => location.reload(), 15000);
}

/**
 * SSL Status
 */

export async function loadSSLStatus() {
    const r = await fetch('/api/ssl/status');
    const d = await r.json();
    document.getElementById('sslToggle').checked = d.enabled;
}

export async function toggleSSL(enabled) {
    const r = await fetch('/api/ssl/toggle', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ enabled })
    });
    const d = await r.json();
    if (!d.success) {
        alert('SSL toggle failed: ' + d.error);
        // Revert toggle
        document.getElementById('sslToggle').checked = !enabled;
    } else {
        alert(`HTTPS ${enabled ? 'enabled' : 'disabled'}. Restart the service to apply.`);
    }
}