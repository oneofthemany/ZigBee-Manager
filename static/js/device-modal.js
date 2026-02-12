/**
 * Device Modal Management
 * Orchestrates the device detail modal by importing tab renderers.
 */

import { state } from './state.js';
import { renderOverviewTab, saveConfig } from './modal/overview.js';
import { renderControlTab, updateControlValues } from './modal/control.js';
import { renderBindingTab } from './modal/binding.js';
import { renderCapsTab } from './modal/clusters.js';
import { renderAutomationTab, initAutomationTab } from './modal/automation.js';

// Re-export these functions so main.js (and others) can still import them from here
export { renderOverviewTab, renderControlTab, renderBindingTab, renderCapsTab, renderAutomationTab, saveConfig };

export function openDeviceModal(d) {
    const cachedDev = (d && d.ieee && state.deviceCache[d.ieee]) ? state.deviceCache[d.ieee] : d;
    state.currentDeviceIeee = cachedDev.ieee;

    const modalBody = document.getElementById('capModalBody');
    if (!modalBody) return;

    let html = `
        <div class="mb-3 d-flex justify-content-between align-items-center">
            <div>
                <h5>${cachedDev.friendly_name}</h5>
                <div class="text-muted small font-monospace">${cachedDev.ieee}</div>
            </div>
            <div>
                <span class="badge bg-secondary">${cachedDev.manufacturer}</span>
                <span class="badge bg-secondary">${cachedDev.model}</span>
            </div>
        </div>

        <ul class="nav nav-tabs mb-3" id="devTabs">
            <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#tab-overview">Overview</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-control">Control</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-binding">Binding</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-caps">Clusters</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-automation">Automation</button></li>
        </ul>

        <div class="tab-content">
            <div class="tab-pane fade show active" id="tab-overview">
                ${renderOverviewTab(cachedDev)}
            </div>
            <div class="tab-pane fade" id="tab-control">
                ${renderControlTab(cachedDev)}
            </div>
            <div class="tab-pane fade" id="tab-binding">
                ${renderBindingTab(cachedDev)}
            </div>
            <div class="tab-pane fade" id="tab-caps">
                ${renderCapsTab(cachedDev)}
            </div>
            <div class="tab-pane fade" id="tab-automation">
                ${renderAutomationTab(cachedDev)}
            </div>
        </div>
    `;

    modalBody.innerHTML = html;
    // Hydrate automation tab when clicked (lazy load API data)
    const autoTab = modalBody.querySelector('[data-bs-target="#tab-automation"]');
    if (autoTab) {
        autoTab.addEventListener('shown.bs.tab', () => {
            initAutomationTab(cachedDev.ieee);
        });
    }
    const modalEl = document.getElementById('capModal');
    if (modalEl) new bootstrap.Modal(modalEl).show();
}

export function refreshModalState(device) {
    console.log("4. Refreshing Modal Content for:", device.friendly_name);

    // Update Overview Tab if it exists
    const overviewTab = document.getElementById('tab-overview');
    if (overviewTab) {
        overviewTab.innerHTML = renderOverviewTab(device);
    }

    // Update Control Tab - using targeted updates
    const controlTab = document.getElementById('tab-control');
    if (controlTab) {
        // Check if user is currently interacting with controls
        if (state.controlInteractionActive) {
            // Only update non-interactive elements (badges, labels)
            updateControlValues(device);
        } else {
            // Full re-render if no active interaction
            controlTab.innerHTML = renderControlTab(device);
        }
    }

    // Update Binding Tab if it exists
    const bindingTab = document.getElementById('tab-binding');
    if (bindingTab) {
        bindingTab.innerHTML = renderBindingTab(device);
    }
}

export function getDeviceStateHtml(d) {
    if (!d.state) return '';
    const keys = Object.keys(d.state).filter(k =>
        !['last_seen', 'power_source', 'available', 'manufacturer', 'model'].includes(k) && !k.startsWith('dp_') && !k.includes('_raw')
    );
    return keys.map(k => `<span class="badge bg-light text-dark border m-1">${k}: ${d.state[k]}</span>`).join(" ");
}

// Global exposure
window.getDeviceStateHtml = getDeviceStateHtml;