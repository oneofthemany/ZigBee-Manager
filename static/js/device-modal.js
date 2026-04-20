/**
 * Device Modal Management
 * Orchestrates the device detail modal by importing tab renderers.
 */

import { state } from './state.js';
import { hasCluster } from './modal/config.js';
import { renderOverviewTab, saveConfig } from './modal/overview.js';
import { renderControlTab, updateControlValues, refreshHeatingManaged } from './modal/control.js';
import { renderBindingTab } from './modal/binding.js';
import { renderCapsTab } from './modal/clusters.js';
import { renderAutomationTab, initAutomationTab } from './modal/automation.js';
import { renderMappingsTab, initMappingsTab, hasGenericContent } from './modal/mappings.js';
import { bindScheduleEvents } from './modal/schedule.js';
import { renderOTATab, handleOTAProgress } from './modal/ota.js';
import { renderMatterClustersTab, initMatterClustersTab } from './modal/matter-clusters.js';
import { renderMatterEventsTab } from './modal/matter-events.js';
import { renderMatterEndpointsTab, initMatterEndpointsTab } from './modal/matter-endpoints.js';

// Re-export these functions so main.js (and others) can still import them from here
export { renderOverviewTab, renderControlTab, renderBindingTab, renderCapsTab, renderAutomationTab, renderMappingsTab, saveConfig, handleOTAProgress };
export async function openDeviceModal(d) {
    // Refresh heating-controller managed set so the Control tab can disable
    // direct heating controls for managed devices. Non-blocking failure.
    await refreshHeatingManaged().catch(() => {});
    const cachedDev = (d && d.ieee && state.deviceCache[d.ieee]) ? state.deviceCache[d.ieee] : d;
    const isZigbee = !cachedDev.protocol || cachedDev.protocol === 'zigbee';
    state.currentDeviceIeee = cachedDev.ieee;

    const modalBody = document.getElementById('capModalBody');
    if (!modalBody) return;

    let html = `
        <div class="mb-3 d-flex justify-content-between align-items-center">
            <div>
                <h5>${cachedDev.friendly_name}</h5>
                <div class="text-muted small font-monospace">${
                    cachedDev.protocol === 'matter'
                    ? (cachedDev.ip_addresses?.length
                        ? cachedDev.ip_addresses[0]
                        : `Node ${cachedDev.state?.node_id || '?'}`)
                        : cachedDev.ieee
                }</div>
            </div>
            <div>
                ${!isZigbee ? `<span class="badge bg-info me-1">${cachedDev.network_type === 'thread' ? 'Thread' : cachedDev.network_type === 'wifi' ? 'WiFi' : 'Matter'}</span>` : ''}
                <span class="badge bg-secondary">${cachedDev.manufacturer}</span>
                <span class="badge bg-secondary">${cachedDev.model}</span>
            </div>
        </div>

        <ul class="nav nav-tabs mb-3" id="devTabs">
            <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#tab-overview">Overview</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-control">Control</button></li>
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-ota"></i>OTA</button></li>
            ${isZigbee ? '<li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-binding">Binding</button></li>' : ''}
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-caps">Clusters</button></li>
            ${!isZigbee ? '<li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-endpoints">Endpoints</button></li>' : ''}
            <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-automation">Automation</button></li>
            ${isZigbee ? '<li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-mappings">Mappings</button></li>' : ''}
        </ul>

        <div class="tab-content">
            <div class="tab-pane fade show active" id="tab-overview">
                ${renderOverviewTab(cachedDev)}
            </div>
            <div class="tab-pane fade" id="tab-control">
                ${!isZigbee ? renderMatterEventsTab(cachedDev) : ''}
                ${renderControlTab(cachedDev)}
            </div>
            <div class="tab-pane fade" id="tab-ota">
                ${renderOTATab(cachedDev)}
            </div>
            ${isZigbee ? `
            <div class="tab-pane fade" id="tab-binding">
                ${renderBindingTab(cachedDev)}
            </div>
            ` : ''}
            <div class="tab-pane fade" id="tab-caps">
                ${isZigbee ? renderCapsTab(cachedDev) : renderMatterClustersTab(cachedDev)}
            </div>
            ${!isZigbee ? `
            <div class="tab-pane fade" id="tab-endpoints">
                ${renderMatterEndpointsTab(cachedDev)}
            </div>
            ` : ''}
            <div class="tab-pane fade" id="tab-automation">
                ${renderAutomationTab(cachedDev)}
            </div>
            ${isZigbee ? `
            <div class="tab-pane fade" id="tab-mappings">
                ${renderMappingsTab(cachedDev)}
            </div>
            ` : ''}
        </div>
    `;

    modalBody.innerHTML = html;

    if (!isZigbee) {
        const capsTab = modalBody.querySelector('[data-bs-target="#tab-caps"]');
        if (capsTab) {
            capsTab.addEventListener('shown.bs.tab', () => {
                initMatterClustersTab(cachedDev.state?.node_id);
            });
        }
    }

    if (!isZigbee) {
        const epTab = modalBody.querySelector('[data-bs-target="#tab-endpoints"]');
        if (epTab) {
            epTab.addEventListener('shown.bs.tab', () => {
                initMatterEndpointsTab(cachedDev.state?.node_id);
            });
        }
    }

    // Bind schedule calendar events for thermostat devices
    if (hasCluster(cachedDev, 0x0201)) {
        bindScheduleEvents(cachedDev.ieee);
    }

    // Hydrate automation tab when clicked (lazy load API data)
    const autoTab = modalBody.querySelector('[data-bs-target="#tab-automation"]');
    if (autoTab) {
        autoTab.addEventListener('shown.bs.tab', () => {
            initAutomationTab(cachedDev.ieee);
        });
    }

    const modalEl = document.getElementById('capModal');
    if (modalEl) new bootstrap.Modal(modalEl).show();

    if (isZigbee) {
        const mapTab = modalBody.querySelector('[data-bs-target="#tab-mappings"]');
        if (mapTab) {
            mapTab.addEventListener('shown.bs.tab', () => {
                initMappingsTab(cachedDev.ieee);
            });
        }
    }
}

export function refreshModalState(device) {
    if (!device) return;

    const isZigbee = !device.protocol || device.protocol === 'zigbee';

    // Overview tab — always full re-render
    const overviewTab = document.getElementById('tab-overview');
    if (overviewTab) {
        overviewTab.innerHTML = renderOverviewTab(device);
    }

    // Control tab — only skip the full re-render if a slider is being
    // actively dragged (otherwise we'd stomp the user's drag).
    const controlTab = document.getElementById('tab-control');
    if (controlTab) {
        const sliderActive =
            state.controlInteractionActive &&
            document.querySelector(
                '#tab-control input[type="range"]:active, ' +
                '#tab-control input[type="range"]:focus'
            );

        if (sliderActive) {
            updateControlValues(device);
        } else {
            controlTab.innerHTML =
                (!isZigbee ? renderMatterEventsTab(device) : '') +
                renderControlTab(device);
            if (hasCluster(device, 0x0201)) {
                bindScheduleEvents(device.ieee);
            }
        }
    }

    // Binding tab — always full re-render
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