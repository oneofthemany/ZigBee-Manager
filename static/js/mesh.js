/**
 * mesh.js
 * Enhanced mesh visualisation with connection table and packet statistics
 */

// Module-level state
let dashboardMeshData = null;
let dashboardSimulation = null;
let dashboardSvg = null;
let dashboardG = null;
let dashboardZoom = null;
let meshInitialized = false;
let labelsVisible = true;

/**
 * Initialise mesh module
 */
export function initMesh() {
    console.log('Mesh module initialized');

    // Listen for mesh tab activation (Bootstrap 5 event)
    const tabEl = document.querySelector('button[data-bs-target="#topology"]');
    if (tabEl) {
        tabEl.addEventListener('shown.bs.tab', function (event) {
            console.log('Mesh tab activated');
            // Small delay to ensure container has dimensions
            setTimeout(() => {
                const container = document.querySelector('.mesh-topology-container');
                // Load if it's empty or just has the loading text
                if (container && (container.children.length === 0 || container.innerHTML.trim() === "")) {
                    loadMeshTopology();
                }
            }, 50);
        });
    }
}

/**
 * Load mesh topology visualisation with connection table
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
        const meshHTML = `
            <div class="mesh-visualisation-wrapper" style="height: 600px; position: relative; border: 1px solid #dee2e6; border-radius: 4px; overflow: hidden;">
                <svg class="mesh-svg" id="dashboard-mesh-svg" style="width: 100%; height: 100%; background: #f8f9fa;"></svg>

                <div class="mesh-controls" style="position: absolute; top: 10px; right: 10px; background: rgba(255,255,255,0.9); padding: 5px; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                    <button class="btn btn-sm btn-light border" onclick="dashboardMeshRefresh()" title="Refresh & Scan"><i class="fas fa-sync"></i></button>
                    <button class="btn btn-sm btn-light border" onclick="dashboardMeshReset()" title="Reset Zoom"><i class="fas fa-compress-arrows-alt"></i></button>
                    <button class="btn btn-sm btn-light border" onclick="toggleMeshLabels()" title="Toggle Labels"><i class="fas fa-tag"></i></button>
                </div>

                <div class="mesh-legend" style="position: absolute; bottom: 10px; left: 10px; background: rgba(255,255,255,0.9); padding: 8px; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); font-size: 0.75rem;">
                    <div class="mb-1"><i class="fas fa-circle text-primary"></i> Coordinator</div>
                    <div class="mb-1"><i class="fas fa-circle text-success"></i> Router</div>
                    <div><i class="fas fa-circle text-warning"></i> End Device</div>
                </div>
            </div>
        `;

        meshContainer.innerHTML = meshHTML;

        // Initialize D3
        if (typeof d3 === 'undefined') {
            throw new Error("D3.js library not loaded");
        }

        await initDashboardMesh();

    } catch (error) {
        console.error('Error loading mesh:', error);
        meshContainer.innerHTML = `<div class="alert alert-danger m-3">Failed to load mesh: ${error.message}</div>`;
    }
}

async function initDashboardMesh() {
    const svg = d3.select("#dashboard-mesh-svg");
    const container = svg.node().parentElement;
    const width = container.clientWidth;
    const height = container.clientHeight;

    dashboardSvg = svg.attr("viewBox", `0 0 ${width} ${height}`);

    dashboardZoom = d3.zoom()
        .scaleExtent([0.1, 4])
        .on("zoom", (event) => {
            if (dashboardG) dashboardG.attr("transform", event.transform);
        });

    dashboardSvg.call(dashboardZoom);
    dashboardG = dashboardSvg.append("g");

    meshInitialized = true;
    await loadDashboardMeshData();
}

async function loadDashboardMeshData() {
    try {
        const res = await fetch('/api/network/simple-mesh');
        const data = await res.json();

        if (!data.success) throw new Error(data.error);

        dashboardMeshData = data;
        renderDashboardMesh();
    } catch (error) {
        console.error("Failed to fetch mesh data", error);
    }
}

function renderDashboardMesh() {
    if (!dashboardMeshData || !dashboardMeshData.nodes) return;

    dashboardG.selectAll("*").remove();

    const width = dashboardSvg.node().parentElement.clientWidth;
    const height = dashboardSvg.node().parentElement.clientHeight;

    const nodes = dashboardMeshData.nodes.map(d => ({...d}));
    const links = dashboardMeshData.connections.map(d => ({...d}));

    dashboardSimulation = d3.forceSimulation(nodes)
        .force("link", d3.forceLink(links).id(d => d.ieee_address).distance(100))
        .force("charge", d3.forceManyBody().strength(-300))
        .force("center", d3.forceCenter(width / 2, height / 2))
        .force("collide", d3.forceCollide(30));

    const link = dashboardG.append("g")
        .selectAll("line")
        .data(links)
        .enter().append("line")
        .attr("stroke", "#999")
        .attr("stroke-width", d => Math.max(1, (d.lqi || 0) / 50));

    const node = dashboardG.append("g")
        .selectAll("g")
        .data(nodes)
        .enter().append("g")
        .call(d3.drag()
            .on("start", dragstarted)
            .on("drag", dragged)
            .on("end", dragended));

    node.append("circle")
        .attr("r", d => d.role === 'Coordinator' ? 15 : (d.role === 'Router' ? 10 : 7))
        .attr("fill", d => {
            if (d.role === 'Coordinator') return '#0d6efd'; // Primary
            if (d.role === 'Router') return '#198754';      // Success
            return '#ffc107';                               // Warning (EndDevice)
        })
        .attr("stroke", "#fff")
        .attr("stroke-width", 1.5);

    const labels = node.append("text")
        .text(d => d.friendly_name || d.ieee_address)
        .attr("x", 12)
        .attr("y", 4)
        .style("font-size", "10px")
        .style("pointer-events", "none")
        .style("opacity", labelsVisible ? 1 : 0)
        .attr("class", "node-label");

    node.append("title")
        .text(d => `${d.friendly_name}\n${d.ieee_address}\nLQI: ${d.lqi}\nRole: ${d.role}`);

    dashboardSimulation.on("tick", () => {
        link
            .attr("x1", d => d.source.x)
            .attr("y1", d => d.source.y)
            .attr("x2", d => d.target.x)
            .attr("y2", d => d.target.y);

        node
            .attr("transform", d => `translate(${d.x},${d.y})`);
    });

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
}

// --- CONTROLS ---

export async function dashboardMeshRefresh() {
    const btn = document.querySelector('.mesh-controls button i.fa-sync')?.parentElement;
    if(btn) {
        const originalIcon = btn.innerHTML;
        btn.innerHTML = '<i class="fas fa-spin fa-spinner"></i>';
        btn.disabled = true;
    }
    
    try {
        // Trigger the scan
        await fetch('/api/network/scan', { method: 'POST' });
        
        // Wait a moment for results to start populating (2s)
        await new Promise(r => setTimeout(r, 2000));
        
        // Reload the visualisation
        await loadDashboardMeshData();
    } catch (e) {
        console.error("Scan failed", e);
    } finally {
        if(btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-sync"></i>';
        }
    }
}

export function dashboardMeshReset() {
    if (dashboardSvg && dashboardZoom) {
        dashboardSvg.transition().duration(750).call(dashboardZoom.transform, d3.zoomIdentity);
    }
}

export function dashboardMeshCenter() {
    // Reset handles centering via identity transform usually
    dashboardMeshReset();
}

export function toggleMeshLabels() {
    labelsVisible = !labelsVisible;
    if (dashboardG) {
        dashboardG.selectAll('.node-label').style('opacity', labelsVisible ? 1 : 0);
    }
}
