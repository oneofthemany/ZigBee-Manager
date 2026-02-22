/**
 * mesh.js
 * Enhanced mesh visualization with connection table and packet statistics
 */

// Module-level state
let dashboardMeshData = null;
let dashboardSimulation = null;
let dashboardSvg = null;
let dashboardG = null;
let dashboardZoom = null;
let meshInitialized = false;
let labelsVisible = true;
let statsInterval = null;

/**
 * Initialize mesh module
 */
export function initMesh() {
    console.log('Mesh module initialized');

    const tabEl = document.querySelector('button[data-bs-target="#topology"]');
    if (tabEl) {
        tabEl.addEventListener('shown.bs.tab', function (event) {
            console.log('Mesh tab activated');
            setTimeout(() => {
                const container = document.querySelector('.mesh-topology-container');
                if (container && (container.children.length === 0 || container.innerHTML.trim() === "")) {
                    loadMeshTopology();
                }
            }, 50);
        });
    }
}

/**
 * Load mesh topology visualization with connection table
 */
export async function loadMeshTopology() {
    const meshContainer = document.querySelector('.mesh-topology-container');
    if (!meshContainer) return;

    console.log('Loading mesh topology visualization...');

    meshContainer.innerHTML = `
        <div class="text-center py-4">
            <div class="spinner-border text-primary" role="status">
                <span class="visually-hidden">Loading...</span>
            </div>
            <p class="text-muted mt-2">Discovering mesh topology...</p>
        </div>
    `;

    try {
            const response = await fetch('/api/network/simple-mesh');
            const data = await response.json();
            dashboardMeshData = data;

            // Build the full UI
            const meshContainer = document.querySelector('.mesh-topology-container');
            meshContainer.innerHTML = buildMeshUI();

            // Initialize D3 and Tables
            initializeD3Visualization(data);
            populateConnectionTable(data.connection_table || []);
            populatePacketStats(data.nodes || [], data.stats_summary || {});

            // Setup Tab Event Listeners for Auto-Refresh
            const statsTabBtn = document.querySelector('button[data-bs-target="#meshPacketStats"]');
            const otherTabs = document.querySelectorAll('button[data-bs-target="#meshVisualization"], button[data-bs-target="#meshConnectionTable"]');

            if (statsTabBtn) {
                // When Packet Stats tab is shown, start polling every 2 seconds
                statsTabBtn.addEventListener('shown.bs.tab', () => {
                    refreshPacketStats(); // Immediate update
                    statsInterval = setInterval(refreshPacketStats, 2000);
                });

                // When leaving the tab, stop polling
                statsTabBtn.addEventListener('hidden.bs.tab', () => {
                    if (statsInterval) clearInterval(statsInterval);
                });
            }

    } catch (error) {
        console.error('Failed to load mesh topology:', error);
        meshContainer.innerHTML = `
            <div class="alert alert-danger m-3">
                <i class="fas fa-exclamation-triangle"></i>
                Failed to load mesh topology: ${error.message}
            </div>
        `;
    }
}

/**
 * Build the mesh UI with tabs
 */
function buildMeshUI() {
    return `
        <div class="mesh-wrapper">
            <!-- Controls -->
            <div class="mesh-controls p-2 bg-light border-bottom d-flex justify-content-between align-items-center">
                <div class="btn-group btn-group-sm">
                    <button class="btn btn-outline-primary" onclick="dashboardMeshRefresh()" title="Refresh topology">
                        <i class="fas fa-sync-alt"></i> Scan
                    </button>
                    <button class="btn btn-outline-secondary" onclick="dashboardMeshReset()" title="Reset view">
                        <i class="fas fa-expand"></i> Reset
                    </button>
                    <button class="btn btn-outline-secondary" onclick="dashboardMeshCenter()" title="Center view">
                        <i class="fas fa-crosshairs"></i> Center
                    </button>
                    <button class="btn btn-outline-secondary" onclick="toggleMeshLabels()" title="Toggle labels">
                        <i class="fas fa-tags"></i> Labels
                    </button>
                </div>
                <div class="mesh-legend small">
                    <span class="me-2"><i class="fas fa-square text-primary"></i> Coordinator</span>
                    <span class="me-2"><i class="fas fa-circle text-success"></i> Router</span>
                    <span class="me-2"><i class="fas fa-circle text-secondary"></i> End Device</span>
                    <span class="text-muted">|</span>
                    <span class="ms-2 signal-excellent">● &gt;200</span>
                    <span class="signal-good">● 150-200</span>
                    <span class="signal-fair">● 100-150</span>
                    <span class="signal-poor">● &lt;100</span>
                </div>
            </div>

            <!-- Sub-tabs for different views -->
            <ul class="nav nav-tabs px-2 pt-2" role="tablist">
                <li class="nav-item">
                    <button class="nav-link active" data-bs-toggle="tab" data-bs-target="#meshVisualization">
                        <i class="fas fa-project-diagram"></i> Visualisation
                    </button>
                </li>
                <li class="nav-item">
                    <button class="nav-link" data-bs-toggle="tab" data-bs-target="#meshConnectionTable">
                        <i class="fas fa-table"></i> Connection Table
                    </button>
                </li>
                <li class="nav-item">
                    <button class="nav-link" data-bs-toggle="tab" data-bs-target="#meshPacketStats">
                        <i class="fas fa-chart-bar"></i> Packet Statistics
                    </button>
                </li>
            </ul>

            <!-- Tab content -->
            <div class="tab-content">
                <!-- Visualisation Tab -->
                <div class="tab-pane fade show active" id="meshVisualization">
                    <div class="mesh-visualisation-wrapper" style="height: 1000px; position: relative; border: 1px solid #dee2e6; border-radius: 0 0 4px 4px; overflow: hidden;">
                        <svg class="mesh-svg" id="dashboard-mesh-svg" style="width: 100%; height: 100%;"></svg>
                    </div>
                </div>

                <!-- Connection Table Tab -->
                <div class="tab-pane fade" id="meshConnectionTable">
                    <div class="p-3" style="max-height: 1000px; overflow-y: auto;">
                        <div class="table-responsive">
                            <table class="table table-sm table-striped table-hover" id="connectionTable">
                                <thead class="table-dark sticky-top">
                                    <tr>
                                        <th>Source Device</th>
                                        <th>Role</th>
                                        <th>Connections</th>
                                    </tr>
                                </thead>
                                <tbody id="connectionTableBody"></tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <!-- Packet Statistics Tab -->
                <div class="tab-pane fade" id="meshPacketStats">
                    <div class="p-3">
                        <div class="row g-2 mb-3" id="packetStatsSummary"></div>

                        <div class="table-responsive" style="max-height: 1000px; overflow-y: auto;">
                            <table class="table table-sm table-striped table-hover" id="packetStatsTable">
                                <thead class="table-dark sticky-top">
                                    <tr>
                                        <th>Device</th>
                                        <th class="text-end">RX Packets</th>
                                        <th class="text-end">TX Packets</th>
                                        <th class="text-end">Total</th>
                                        <th class="text-end">RX/min</th>
                                        <th class="text-end">TX/min</th>
                                        <th class="text-end">Errors</th>
                                        <th class="text-end">Error %</th>
                                        <th>Load</th>
                                    </tr>
                                </thead>
                                <tbody id="packetStatsBody"></tbody>
                            </table>
                        </div>
                    </div>
                </div>

            </div>
        </div>
    `;
}

/**
 * Initialize D3 visualization
 */
function initializeD3Visualization(data) {
    const svg = d3.select('#dashboard-mesh-svg');
    const container = svg.node().parentElement;
    const width = container.clientWidth;
    const height = container.clientHeight;

    svg.selectAll('*').remove();

    // Set up zoom
    dashboardZoom = d3.zoom()
        .scaleExtent([0.1, 4])
        .on('zoom', (event) => {
            dashboardG.attr('transform', event.transform);
        });

    svg.call(dashboardZoom);

    dashboardG = svg.append('g');
    dashboardSvg = svg;

    // Arrow marker for directed links
    svg.append('defs').append('marker')
        .attr('id', 'arrowhead')
        .attr('viewBox', '-0 -5 10 10')
        .attr('refX', 20)
        .attr('refY', 0)
        .attr('orient', 'auto')
        .attr('markerWidth', 6)
        .attr('markerHeight', 6)
        .append('path')
        .attr('d', 'M 0,-5 L 10,0 L 0,5')
        .attr('fill', '#999');

    // Process nodes and links
    const nodes = data.nodes || [];
    const links = data.links || [];

    // Color scale for LQI
    const getLinkColor = (lqi) => {
        if (lqi >= 200) return '#00b894';
        if (lqi >= 150) return '#fdcb6e';
        if (lqi >= 100) return '#e17055';
        return '#d63031';
    };

    // Create simulation
    dashboardSimulation = d3.forceSimulation(nodes)
        .force('link', d3.forceLink(links).id(d => d.id).distance(100))
        .force('charge', d3.forceManyBody().strength(-300))
        .force('center', d3.forceCenter(width / 2, height / 2))
        .force('collision', d3.forceCollide().radius(40));

    // Draw links
    const link = dashboardG.append('g')
        .selectAll('line')
        .data(links)
        .join('line')
        .attr('stroke', d => getLinkColor(d.lqi))
        .attr('stroke-opacity', 0.6)
        .attr('stroke-width', d => Math.max(1, d.lqi / 50))
        .attr('marker-end', 'url(#arrowhead)');

    // Draw nodes
    const node = dashboardG.append('g')
        .selectAll('g')
        .data(nodes)
        .join('g')
        .call(d3.drag()
            .on('start', dragstarted)
            .on('drag', dragged)
            .on('end', dragended));

    // Node shapes based on role
    node.each(function(d) {
        const el = d3.select(this);
        if (d.role === 'Coordinator') {
            el.append('rect')
                .attr('width', 24)
                .attr('height', 24)
                .attr('x', -12)
                .attr('y', -12)
                .attr('fill', '#0d6efd')
                .attr('rx', 4);
        } else {
            el.append('circle')
                .attr('r', 10)
                .attr('fill', d.role === 'Router' ? '#198754' : '#6c757d')
                .attr('stroke', d.online ? '#fff' : '#dc3545')
                .attr('stroke-width', 2);
        }
    });

    // Labels
    node.append('text')
        .attr('class', 'mesh-label')
        .attr('dx', 15)
        .attr('dy', 4)
        .attr('font-size', '11px')
        .attr('fill', '#333')
        .text(d => d.friendly_name || d.id.slice(-8));

    // Tooltips
    node.append('title')
        .text(d => {
            const stats = d.packet_stats || {};
            return `${d.friendly_name || d.id}
Role: ${d.role}
LQI: ${d.lqi}
NWK: ${d.network_address}
Online: ${d.online ? 'Yes' : 'No'}
RX: ${stats.rx_packets || 0} | TX: ${stats.tx_packets || 0}
Rate: ${stats.rx_rate || 0}/min`;
        });

    // Simulation tick
    dashboardSimulation.on('tick', () => {
        link
            .attr('x1', d => d.source.x)
            .attr('y1', d => d.source.y)
            .attr('x2', d => d.target.x)
            .attr('y2', d => d.target.y);

        node.attr('transform', d => `translate(${d.x},${d.y})`);
    });

    // Drag functions
    function dragstarted(event, d) {
        if (!event.active) dashboardSimulation.alphaTarget(0.3).restart();
        d.fx = d.x;
        d.fy = d.y;
    }

    function dragged(event, d) {
        d.fx = event.x;
        d.fy = event.y;
    }

    function dragended(event, d) {
        if (!event.active) dashboardSimulation.alphaTarget(0);
        d.fx = null;
        d.fy = null;
    }

    meshInitialized = true;
}

/**
 * Populate the connection table with a Tree View
 */
function populateConnectionTable(connections) {
    const tbody = document.getElementById('connectionTableBody');
    if (!tbody) return;

    if (!connections || connections.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="3" class="text-center text-muted py-4">
                    <i class="fas fa-info-circle"></i> No connection data available.
                    Try running a topology scan.
                </td>
            </tr>
        `;
        return;
    }

    // Helpers (Internal to this scope as before)
    const getLqiClass = (lqi) => {
        if (lqi >= 200) return 'signal-excellent';
        if (lqi >= 150) return 'signal-good';
        if (lqi >= 100) return 'signal-fair';
        if (lqi >= 50) return 'signal-poor';
        return 'signal-weak';
    };

    const getSignalBars = (lqi) => {
        const bars = Math.ceil(lqi / 51);
        let html = '';
        for (let i = 0; i < 5; i++) {
            const active = i < bars;
            html += `<span style="display:inline-block; width:4px; height:${8 + i * 3}px;
                     background:${active ? '#198754' : '#dee2e6'}; margin-right:1px;"></span>`;
        }
        return html;
    };

    // 1. Group connections by Source IEEE Address
    const grouped = {};
    connections.forEach(conn => {
        const sourceId = conn.source_ieee;
        if (!grouped[sourceId]) {
            grouped[sourceId] = {
                name: conn.source_name,
                ieee: conn.source_ieee,
                role: conn.source_role,
                targets: []
            };
        }
        grouped[sourceId].targets.push(conn);
    });

    // 2. Build HTML
    let html = '';

    // Sort groups alphabetically by name
    const sortedGroups = Object.values(grouped).sort((a, b) =>
        (a.name || '').localeCompare(b.name || '')
    );

    sortedGroups.forEach((group, index) => {
        const collapseId = `conn-collapse-${index}`;

        // Parent Row (Source Device)
        html += `
            <tr style="cursor: pointer;">
                <td data-bs-toggle="collapse" data-bs-target="#${collapseId}" aria-expanded="false" class="d-flex align-items-center border-0">
                    <i class="fas fa-chevron-right me-3 transition-icon text-muted"></i>
                    <div>
                        <span class="fw-medium">${escapeHtml(group.name)}</span>
                        <small class="text-muted d-block">${group.ieee.slice(-8)}</small>
                    </div>
                </td>
                <td class="align-middle">
                    <span class="badge ${getRoleBadgeClass(group.role)}">${group.role}</span>
                </td>
                <td class="align-middle">
                    <span class="badge bg-light text-dark border">${group.targets.length} Neighbors</span>
                </td>
            </tr>
        `;

        // Child Row (Expanded Details)
        html += `
            <tr>
                <td colspan="3" class="child-row-cell">
                    <div class="collapse" id="${collapseId}">
                        <div class="p-3 bg-light border-bottom shadow-inset">
                            <table class="table table-sm table-bordered nested-connection-table mb-0">
                                <thead>
                                    <tr>
                                        <th>Target Device</th>
                                        <th>Role</th>
                                        <th>Relationship</th>
                                        <th>LQI</th>
                                        <th>Signal</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${group.targets.map(t => `
                                        <tr>
                                            <td>
                                                <span class="fw-medium">${escapeHtml(t.target_name)}</span>
                                                <small class="text-muted d-block">${t.target_ieee.slice(-8)}</small>
                                            </td>
                                            <td><span class="badge ${getRoleBadgeClass(t.target_role)}">${t.target_role}</span></td>
                                            <td><span class="badge bg-secondary">${t.relationship}</span></td>
                                            <td class="${getLqiClass(t.lqi)} fw-bold">${t.lqi}</td>
                                            <td>${getSignalBars(t.lqi)}</td>
                                        </tr>
                                    `).join('')}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </td>
            </tr>
        `;
    });

    tbody.innerHTML = html;
}

/**
 * Silently refresh packet statistics without redrawing the whole UI
 */
async function refreshPacketStats() {
    // specific check to ensure we don't run if the tab isn't actually visible
    const statsTab = document.getElementById('meshPacketStats');
    if (!statsTab || !statsTab.classList.contains('active')) return;

    try {
        const response = await fetch('/api/network/simple-mesh');
        const data = await response.json();

        // Update global data ref
        dashboardMeshData = data;

        // Only update the stats table and summary
        populatePacketStats(data.nodes || [], data.stats_summary || {});
    } catch (error) {
        console.error("Silent stats refresh failed:", error);
    }
}

/**
 * Populate packet statistics
 */
function populatePacketStats(nodes, summary) {
    // --- CONFIGURATION ---
    // Define the "Red Line" for network traffic.
    // 5 Packets Per Second (300/min) is generally considered high for a single ZigBee device.
    const PPS_THRESHOLD = 5;
    const PPM_THRESHOLD = PPS_THRESHOLD * 60; // Convert to Per Minute for calculation
    // ---------------------

    // Summary cards
    const summaryContainer = document.getElementById('packetStatsSummary');
    if (summaryContainer) {
        summaryContainer.innerHTML = `
            <div class="col-md-2">
                <div class="card text-center">
                    <div class="card-body py-2">
                        <div class="small text-muted">Devices</div>
                        <div class="h5 mb-0">${summary.total_devices || nodes.length}</div>
                    </div>
                </div>
            </div>
            <div class="col-md-2">
                <div class="card text-center">
                    <div class="card-body py-2">
                        <div class="small text-muted">Total RX</div>
                        <div class="h5 mb-0">${formatNumber(summary.total_rx_packets || 0)}</div>
                    </div>
                </div>
            </div>
            <div class="col-md-2">
                <div class="card text-center">
                    <div class="card-body py-2">
                        <div class="small text-muted">Total TX</div>
                        <div class="h5 mb-0">${formatNumber(summary.total_tx_packets || 0)}</div>
                    </div>
                </div>
            </div>
            <div class="col-md-2">
                <div class="card text-center">
                    <div class="card-body py-2">
                        <div class="small text-muted">Errors</div>
                        <div class="h5 mb-0 ${summary.total_errors > 0 ? 'text-danger' : ''}">${summary.total_errors || 0}</div>
                    </div>
                </div>
            </div>
            <div class="col-md-2">
                <div class="card text-center">
                    <div class="card-body py-2">
                        <div class="small text-muted">Avg/Device</div>
                        <div class="h5 mb-0">${summary.avg_packets_per_device || 0}</div>
                    </div>
                </div>
            </div>
            <div class="col-md-2">
                <div class="card text-center">
                    <div class="card-body py-2">
                        <div class="small text-muted">Uptime</div>
                        <div class="h5 mb-0">${formatUptime(summary.uptime_seconds || 0)}</div>
                    </div>
                </div>
            </div>
        `;
    }

    // Device stats table
    const tbody = document.getElementById('packetStatsBody');
    if (!tbody) return;

    // Sort by Total Rate (Activity) instead of historical volume
    const sortedNodes = [...nodes].sort((a, b) => {
        const aRate = (a.packet_stats?.rx_rate || 0) + (a.packet_stats?.tx_rate || 0);
        const bRate = (b.packet_stats?.rx_rate || 0) + (b.packet_stats?.tx_rate || 0);
        return bRate - aRate; // Busiest right now at the top
    });

    tbody.innerHTML = sortedNodes.map(node => {
        const stats = node.packet_stats || {};

        // Calculate current activity: RX Rate + TX Rate (Packets Per Minute)
        const currentPpm = (stats.rx_rate || 0) + (stats.tx_rate || 0);

        // Calculate % Load against our defined Threshold
        // If currentPpm = 300 and Threshold = 300, we are at 100% load
        const loadPercent = Math.round((currentPpm / PPM_THRESHOLD) * 100);

        // Cap visual bar at 100% so it doesn't overflow, but allow logic to see higher
        const visualPercent = Math.min(loadPercent, 100);

        return `
            <tr>
                <td>
                    <span class="fw-medium">${escapeHtml(node.friendly_name)}</span>
                    <small class="text-muted d-block">${node.ieee_address.slice(-8)}</small>
                </td>
                <td class="text-end">${formatNumber(stats.rx_packets || 0)}</td>
                <td class="text-end">${formatNumber(stats.tx_packets || 0)}</td>
                <td class="text-end fw-bold">${formatNumber(stats.total_packets || 0)}</td>
                <td class="text-end">${stats.rx_rate || 0}</td>
                <td class="text-end">${stats.tx_rate || 0}</td>
                <td class="text-end ${stats.errors > 0 ? 'text-danger' : ''}">${stats.errors || 0}</td>
                <td class="text-end ${stats.error_rate > 5 ? 'text-danger' : ''}">${stats.error_rate || 0}%</td>
                <td style="width: 100px; vertical-align: middle;">
                    <div class="progress" style="height: 8px;" title="Current: ${currentPpm} PPM / Threshold: ${PPM_THRESHOLD} PPM">
                        <div class="progress-bar ${getLoadBarClass(loadPercent)}"
                             style="width: ${visualPercent}%"></div>
                    </div>
                </td>
            </tr>
        `;
    }).join('');
}

// Helper functions
function getRoleBadgeClass(role) {
    switch (role) {
        case 'Coordinator': return 'bg-primary';
        case 'Router': return 'bg-success';
        default: return 'bg-secondary';
    }
}

function getLoadBarClass(percent) {
    if (percent >= 80) return 'bg-danger';
    if (percent >= 50) return 'bg-warning';
    return 'bg-success';
}

function formatNumber(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return n.toString();
}

function formatUptime(seconds) {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
    return `${Math.round(seconds / 86400)}d`;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Exported control functions
export async function dashboardMeshRefresh() {
    const meshContainer = document.querySelector('.mesh-topology-container');
    if (!meshContainer) return;

    // Show scanning state
    meshContainer.innerHTML = `
        <div class="text-center py-4">
            <div class="spinner-border text-warning" role="status">
                <span class="visually-hidden">Scanning...</span>
            </div>
            <p class="text-muted mt-2">Scanning mesh topology (LQI)... this may take 15-30s</p>
        </div>
    `;

    try {
        // Trigger the actual zigpy topology scan first
        const scanRes = await fetch('/api/network/scan', { method: 'POST' });
        const scanData = await scanRes.json();
        if (!scanData.success) {
            console.warn('Topology scan warning:', scanData.error);
        }
    } catch (e) {
        console.error('Scan failed:', e);
    }

    // Now reload the fresh data
    await loadMeshTopology();
}

export function dashboardMeshReset() {
    if (dashboardSvg && dashboardZoom) {
        dashboardSvg.transition().duration(750).call(
            dashboardZoom.transform,
            d3.zoomIdentity
        );
    }
}

export function dashboardMeshCenter() {
    if (dashboardSimulation && dashboardSvg && dashboardZoom) {
        const svg = dashboardSvg.node();
        const width = svg.clientWidth;
        const height = svg.clientHeight;

        dashboardSvg.transition().duration(750).call(
            dashboardZoom.transform,
            d3.zoomIdentity.translate(width / 2, height / 2).scale(1)
        );
    }
}

export function toggleMeshLabels() {
    labelsVisible = !labelsVisible;
    d3.selectAll('.mesh-label').style('display', labelsVisible ? 'block' : 'none');
}