// ============================================================================
// floor-plan.js — Heating Controller floor-plan editor
// ============================================================================
// Self-contained Bootstrap modal that lets the user draw a multi-level floor
// plan (walls, openings, rooms, radiators, sensors, contacts) and bind real
// devices to those geometric features. On save, persists via two channels:
//
//   1. /api/heating/floor-plan       (the rich plan, kept verbatim)
//   2. /api/heating/controller/config (existing endpoint — circuits get the
//                                     projected dimensions/radiators/sensors
//                                     written by routes/floor_plan_routes.py
//                                     before save)
//
// Public API
// ----------
//   import { openFloorPlanEditor } from './floor-plan.js';
//   openFloorPlanEditor({ circuits, devices, sensors, contacts, onSave });
//
// Coordinate system
//   metres in, metres out. SVG viewBox is in metres. Zoom = scale of the <g>.
//   +x right, +y down for SVG (the canonical SVG convention). When projecting
//   to/from the model (which uses +y up), we flip y on read AND write so the
//   user's drawing matches reality.
// ============================================================================

const FP_VERSION = 1;
const DEFAULT_LEVEL_HEIGHT = 2.4;
const SNAP_M = 0.1;                  // snap-to-grid distance, metres
const PIXELS_PER_METRE_DEFAULT = 80;

// ─────────────────── module state (per-modal-open) ───────────────────

let _state = null;         // see resetState()
let _onSaveCallback = null;
let _availableDevices = { trvs: [], sensors: [], contacts: [] };
// Circuits passed in by the caller (heating-controller config). Used purely
// for display: showing which circuit a radiator's bound room belongs to,
// in the radiator props panel. Schema: [{ id, name, rooms: [{id, name}] }, ...]
let _availableCircuits = [];

function resetState(plan) {
    _state = {
        plan: plan || newEmptyPlan(),
        currentLevelId: null,
        tool: 'select',
        selection: null,           // {kind, id} or null
        drawBuffer: null,          // tool-specific scratchpad
        zoom: PIXELS_PER_METRE_DEFAULT,
        pan: { x: 0, y: 0 },       // pixel offset
        showGrid: true,
        showBackground: true,
        showSun: false,
        sunData: null,
        calibration: null,         // { p1: {x,y} } during 2-click calibrate
    };
    _state.currentLevelId = (_state.plan.levels[0] || {}).id || null;
}

function newEmptyPlan() {
    return {
        version: FP_VERSION,
        north_offset_deg: 0,
        scale_pixels_per_metre: PIXELS_PER_METRE_DEFAULT,
        circuits: [],
        levels: [{
            id: 'ground',
            name: 'Ground floor',
            index: 0,
            ceiling_height_m: DEFAULT_LEVEL_HEIGHT,
            floor_above_ground_m: 0,
            walls: [],
            openings: [],
            rooms: [],
            radiators: [],
            sensors: [],
            contacts: [],
        }],
    };
}

function genId(prefix) {
    return `${prefix}_${Math.random().toString(36).slice(2, 8)}`;
}

function currentLevel() {
    return _state.plan.levels.find(l => l.id === _state.currentLevelId) || _state.plan.levels[0];
}

// ──────────────────────────── public entry ────────────────────────────

export async function openFloorPlanEditor(opts = {}) {
    _availableDevices = {
        trvs: opts.devices?.thermostats || [],
        sensors: opts.sensors || [],
        contacts: opts.contacts || [],
        receivers: opts.devices?.receivers || opts.receivers || [],
    };
    _availableCircuits = opts.circuits || [];
    _onSaveCallback = opts.onSave || null;

    let initialPlan;
    try {
        const r = await fetch('/api/heating/floor-plan').then(r => r.json());
        initialPlan = (r && r.success && r.plan) ? r.plan : newEmptyPlan();
    } catch {
        initialPlan = newEmptyPlan();
    }

    // Heal orphan images: any level with no `background` block but an image
    // on disk gets a synthesised metadata block so the user sees their image
    // and can recalibrate it. This recovers state where the editor previously
    // failed to persist the metadata (now fixed in save(), but leaving the
    // recovery path in place as defence-in-depth).
    let orphansAdopted = 0;
    for (const lvl of (initialPlan.levels || [])) {
        if (lvl.background?.present) continue;
        const adopted = await tryAdoptOrphanImage(lvl);
        if (adopted) orphansAdopted += 1;
    }

    resetState(initialPlan);
    // Ensure plan always has a circuits array (backward compat with older saves)
    if (!Array.isArray(_state.plan.circuits)) _state.plan.circuits = [];

    ensureModal();
    const modalEl = document.getElementById('floorPlanModal');
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    modal.show();
    renderAll();

    if (orphansAdopted > 0) {
        const status = document.getElementById('fpSaveStatus');
        if (status) {
            status.innerHTML = `<span class="text-warning"><i class="fas fa-exclamation-triangle me-1"></i>` +
                `Recovered ${orphansAdopted} orphan image${orphansAdopted === 1 ? '' : 's'} — ` +
                `please run <strong>Calibrate</strong> for affected levels and Save.</span>`;
        }
    }
}

/**
 * For a level missing its `background` block, probe the image endpoint and
 * synthesise metadata if the image bytes exist on the server. Returns true
 * iff a block was synthesised.
 */
async function tryAdoptOrphanImage(lvl) {
    try {
        const url = `/api/heating/floor-plan/image/${encodeURIComponent(lvl.id)}`;
        const resp = await fetch(url, { method: 'GET' });
        if (!resp.ok) return false;
        const blob = await resp.blob();
        if (!blob || blob.size === 0) return false;
        const dims = await readImageDimensions(blob);
        const contentType = blob.type || 'image/png';
        lvl.background = {
            present: true,
            pixels_per_metre: 50.0,            // placeholder — user must calibrate
            image_width_px: dims.width,
            image_height_px: dims.height,
            origin_x_m: 0,
            origin_y_m: 0,
            rotation_deg: 0,
            opacity: 0.5,
            content_type: contentType,
            _cb: Date.now(),
        };
        return true;
    } catch {
        return false;
    }
}

// ─────────────────────────── modal scaffold ───────────────────────────

function ensureModal() {
    if (document.getElementById('floorPlanModal')) return;
    const html = `
    <div class="modal fade" id="floorPlanModal" tabindex="-1" aria-hidden="true">
      <div class="modal-dialog modal-fullscreen">
        <div class="modal-content">
          <div class="modal-header py-2">
            <h5 class="modal-title"><i class="fas fa-drafting-compass me-2"></i>Floor plan</h5>
            <div class="ms-3 small text-muted" id="fpStatus"></div>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body p-0 d-flex" style="overflow:hidden">
            <!-- Left: tools + levels -->
            <div id="fpSidebar" class="border-end" style="width:240px;min-width:240px;overflow:auto;padding:12px">
              <div class="mb-3">
                <div class="small text-muted text-uppercase mb-1">Levels</div>
                <div id="fpLevelList" class="list-group list-group-flush small"></div>
                <button class="btn btn-sm btn-outline-secondary w-100 mt-2" id="fpAddLevel"><i class="fas fa-plus me-1"></i>Add level</button>
              </div>
              <div class="mb-3" id="fpCircuitSection">
                <div class="d-flex justify-content-between align-items-center mb-1">
                  <div class="small text-muted text-uppercase">Circuits</div>
                  <button class="btn btn-sm btn-outline-success py-0 px-1" id="fpAddCircuit" title="Add circuit"><i class="fas fa-plus"></i></button>
                </div>
                <div id="fpCircuitList" class="mb-1"></div>
              </div>
              <div class="mb-3">
                <div class="small text-muted text-uppercase mb-1">Tools</div>
                <div class="btn-group-vertical w-100" role="group" id="fpToolbar">
                  <button class="btn btn-sm btn-outline-primary" data-tool="select"><i class="fas fa-mouse-pointer me-1"></i>Select</button>
                  <button class="btn btn-sm btn-outline-primary" data-tool="wall"><i class="fas fa-grip-lines-vertical me-1"></i>Wall</button>
                  <button class="btn btn-sm btn-outline-primary" data-tool="room"><i class="fas fa-vector-square me-1"></i>Room</button>
                  <button class="btn btn-sm btn-outline-primary" data-tool="window"><i class="fas fa-window-maximize me-1"></i>Window</button>
                  <button class="btn btn-sm btn-outline-primary" data-tool="door"><i class="fas fa-door-open me-1"></i>Door</button>
                  <button class="btn btn-sm btn-outline-primary" data-tool="radiator"><i class="fas fa-fire me-1"></i>Radiator</button>
                  <button class="btn btn-sm btn-outline-primary" data-tool="sensor"><i class="fas fa-thermometer-half me-1"></i>Sensor</button>
                  <button class="btn btn-sm btn-outline-primary" data-tool="contact"><i class="fas fa-link me-1"></i>Contact</button>
                  <button class="btn btn-sm btn-outline-warning" data-tool="calibrate"><i class="fas fa-ruler me-1"></i>Calibrate</button>
                </div>
              </div>
              <div class="mb-3">
                <div class="small text-muted text-uppercase mb-1">Background image</div>
                <input type="file" id="fpImageFile" class="form-control form-control-sm mb-2"
                       accept=".png,.jpg,.jpeg,.pdf,image/png,image/jpeg,application/pdf">
                <div class="d-flex gap-1 mb-2">
                  <button class="btn btn-sm btn-outline-danger flex-fill" id="fpRemoveImage" disabled><i class="fas fa-trash me-1"></i>Remove</button>
                </div>
                <label class="form-label small mb-0">Opacity</label>
                <input type="range" id="fpImageOpacity" class="form-range" min="0.05" max="1" step="0.05" value="0.5">
                <div class="form-check form-switch small mt-1">
                  <input class="form-check-input" type="checkbox" id="fpToggleBackground" checked>
                  <label class="form-check-label" for="fpToggleBackground">Show image</label>
                </div>
              </div>
              <div class="mb-3">
                <div class="small text-muted text-uppercase mb-1">View</div>
                <div class="form-check form-switch small">
                  <input class="form-check-input" type="checkbox" id="fpToggleGrid" checked>
                  <label class="form-check-label" for="fpToggleGrid">Grid</label>
                </div>
                <div class="form-check form-switch small">
                  <input class="form-check-input" type="checkbox" id="fpToggleSun">
                  <label class="form-check-label" for="fpToggleSun">Sun path (today)</label>
                </div>
                <div class="d-flex gap-1 mt-2">
                  <button class="btn btn-sm btn-outline-secondary flex-fill" id="fpZoomOut">−</button>
                  <button class="btn btn-sm btn-outline-secondary flex-fill" id="fpZoomFit">Fit</button>
                  <button class="btn btn-sm btn-outline-secondary flex-fill" id="fpZoomIn">+</button>
                </div>
              </div>
              <div class="mb-3">
                <div class="small text-muted text-uppercase mb-1">Compass (North)</div>
                <div id="fpCompass" class="position-relative" style="width:120px;height:120px;margin:0 auto"></div>
                <div class="small text-center mt-1">
                  <input type="number" id="fpNorthDeg" class="form-control form-control-sm text-center" step="1" style="display:inline-block;width:80px"> °
                </div>
              </div>
            </div>

            <!-- Centre: SVG canvas -->
            <div id="fpCanvasWrap" class="flex-grow-1 position-relative" style="overflow:hidden">
              <svg id="fpCanvas" style="width:100%;height:100%;display:block;cursor:crosshair">
                <defs>
                  <pattern id="fpGridMinor" width="40" height="40" patternUnits="userSpaceOnUse">
                    <path class="fp-grid-line-minor" d="M 40 0 L 0 0 0 40" fill="none" stroke-width="0.5"/>
                  </pattern>
                  <pattern id="fpGridMajor" width="200" height="200" patternUnits="userSpaceOnUse">
                    <rect width="200" height="200" fill="url(#fpGridMinor)"/>
                    <path class="fp-grid-line-major" d="M 200 0 L 0 0 0 200" fill="none" stroke-width="1"/>
                  </pattern>
                </defs>
                <g id="fpGrid"><rect id="fpGridRect" width="10000" height="10000" x="-5000" y="-5000" fill="url(#fpGridMajor)"/></g>
                <g id="fpScene"></g>
                <g id="fpOverlay"></g>
              </svg>
            </div>

            <!-- Right: properties pane -->
            <div id="fpProps" class="border-start" style="width:300px;min-width:300px;overflow:auto;padding:12px">
              <div class="text-muted small">Select something to edit its properties.</div>
            </div>
          </div>
          <div class="modal-footer py-2">
            <button type="button" class="btn btn-outline-warning btn-sm me-2" id="fpSwitchMode"
                    title="Switch the heating controller back to manual configuration">
              <i class="fas fa-list-ul me-1"></i>Switch to manual
            </button>
            <div id="fpSaveStatus" class="me-auto small text-muted"></div>
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
            <button type="button" class="btn btn-primary" id="fpSave"><i class="fas fa-save me-1"></i>Save plan</button>
          </div>
        </div>
      </div>
    </div>`;
    document.body.insertAdjacentHTML('beforeend', html);
    bindModalEvents();
}

function bindModalEvents() {
    document.getElementById('fpSave').addEventListener('click', save);
    document.getElementById('fpSwitchMode')?.addEventListener('click', switchToManual);
    document.getElementById('fpAddLevel').addEventListener('click', addLevel);
    document.getElementById('fpAddCircuit').addEventListener('click', addCircuit);
    document.querySelectorAll('#fpToolbar [data-tool]').forEach(b => {
        b.addEventListener('click', () => setTool(b.dataset.tool));
    });
    document.getElementById('fpToggleGrid').addEventListener('change', e => {
        _state.showGrid = e.target.checked;
        document.getElementById('fpGrid').style.display = _state.showGrid ? '' : 'none';
    });
    document.getElementById('fpToggleSun').addEventListener('change', async e => {
        _state.showSun = e.target.checked;
        if (_state.showSun) await loadSunData();
        renderOverlay();
    });
    document.getElementById('fpZoomIn').addEventListener('click', () => zoomBy(1.25));
    document.getElementById('fpZoomOut').addEventListener('click', () => zoomBy(0.8));
    document.getElementById('fpZoomFit').addEventListener('click', zoomFit);
    document.getElementById('fpNorthDeg').addEventListener('change', e => {
        const v = parseFloat(e.target.value);
        if (Number.isFinite(v)) {
            _state.plan.north_offset_deg = ((v % 360) + 360) % 360;
            renderCompass();
            renderProps();
        }
    });

    document.getElementById('fpImageFile').addEventListener('change', onImageFileChosen);
    document.getElementById('fpRemoveImage').addEventListener('click', removeBackgroundImage);
    document.getElementById('fpImageOpacity').addEventListener('input', e => {
        const lvl = currentLevel();
        if (!lvl.background?.present) return;
        lvl.background.opacity = parseFloat(e.target.value);
        renderScene();
    });
    document.getElementById('fpToggleBackground').addEventListener('change', e => {
        _state.showBackground = e.target.checked;
        renderScene();
    });

    const svg = document.getElementById('fpCanvas');
    svg.addEventListener('mousedown', onCanvasMouseDown);
    svg.addEventListener('mousemove', onCanvasMouseMove);
    svg.addEventListener('mouseup', onCanvasMouseUp);
    svg.addEventListener('wheel', onCanvasWheel, { passive: false });
    // Right-click finishes a wall or room chain (or just suppresses the
    // browser context menu when nothing is in progress).
    svg.addEventListener('contextmenu', e => {
        e.preventDefault();
        if (_state?.tool === 'wall' && _state.drawBuffer?.points?.length >= 2) {
            finishWallChain();
        } else if (_state?.tool === 'room' && _state.drawBuffer?.points?.length >= 3) {
            finishRoom();
        }
    });
    // Double-click also finishes chains. Useful when the last vertex isn't
    // close enough to the first for a single click to close.
    svg.addEventListener('dblclick', e => {
        if (_state?.tool === 'wall' && _state.drawBuffer?.points?.length >= 2) {
            e.preventDefault();
            finishWallChain();
        } else if (_state?.tool === 'room' && _state.drawBuffer?.points?.length >= 3) {
            e.preventDefault();
            finishRoom();
        }
    });

    // Keyboard: Enter / Esc handle in-progress chains across the whole modal
    // so the user doesn't have to keep focus on the canvas.
    const onKey = (e) => {
        if (!_state) return;
        // Ignore when typing in a form field
        const tag = (e.target?.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
        if (e.key === 'Enter') {
            if (_state.tool === 'wall' && _state.drawBuffer?.points?.length >= 2) {
                e.preventDefault(); finishWallChain();
            } else if (_state.tool === 'room' && _state.drawBuffer?.points?.length >= 3) {
                e.preventDefault(); finishRoom();
            }
        } else if (e.key === 'Escape') {
            if (_state.drawBuffer) { e.preventDefault(); cancelDrawing(); }
            else if (_state.calibration) { e.preventDefault(); _state.calibration = null; renderOverlay(); }
        } else if (e.key === 'Backspace' || e.key === 'Delete') {
            // Backspace removes the last placed vertex of the current chain
            if ((_state.tool === 'wall' || _state.tool === 'room')
                && _state.drawBuffer?.points?.length >= 1) {
                e.preventDefault();
                _state.drawBuffer.points.pop();
                if (_state.drawBuffer.points.length === 0) _state.drawBuffer = null;
                renderOverlay();
            }
        }
    };
    document.addEventListener('keydown', onKey);

    // Cleanup on hide
    document.getElementById('floorPlanModal').addEventListener('hidden.bs.modal', () => {
        document.removeEventListener('keydown', onKey);
        _state = null;
    });
}

// ──────────────────────────── render top-level ────────────────────────────

function renderAll() {
    renderLevelList();
    renderCircuitList();
    renderToolbar();
    renderCompass();
    renderScene();
    renderOverlay();
    renderProps();
    document.getElementById('fpNorthDeg').value = Math.round(_state.plan.north_offset_deg);
    syncBackgroundControls();
    requestAnimationFrame(zoomFit);
}

function syncBackgroundControls() {
    const lvl = currentLevel();
    const removeBtn = document.getElementById('fpRemoveImage');
    const opSlider = document.getElementById('fpImageOpacity');
    if (!removeBtn || !opSlider) return;
    if (lvl?.background?.present) {
        removeBtn.disabled = false;
        opSlider.value = lvl.background.opacity ?? 0.5;
    } else {
        removeBtn.disabled = true;
    }
}

function renderLevelList() {
    const wrap = document.getElementById('fpLevelList');
    wrap.innerHTML = _state.plan.levels
        .slice()
        .sort((a, b) => b.index - a.index)
        .map(l => {
            const active = l.id === _state.currentLevelId ? 'active' : '';
            return `
              <button type="button" class="list-group-item list-group-item-action py-1 ${active}" data-level-id="${l.id}">
                <div class="d-flex justify-content-between align-items-center">
                  <span>${escapeHtml(l.name)}</span>
                  <small class="text-muted">L${l.index}</small>
                </div>
              </button>`;
        }).join('');
    wrap.querySelectorAll('[data-level-id]').forEach(el => {
        el.addEventListener('click', () => {
            _state.currentLevelId = el.dataset.levelId;
            _state.selection = null;
            renderAll();
        });
    });
}

function renderToolbar() {
    document.querySelectorAll('#fpToolbar [data-tool]').forEach(b => {
        b.classList.toggle('active', b.dataset.tool === _state.tool);
    });
    const cursors = {
        select: 'default', wall: 'crosshair', room: 'crosshair',
        window: 'crosshair', door: 'crosshair', radiator: 'crosshair',
        sensor: 'crosshair', contact: 'crosshair', calibrate: 'crosshair',
    };
    document.getElementById('fpCanvas').style.cursor = cursors[_state.tool] || 'default';
}

function renderCompass() {
    const el = document.getElementById('fpCompass');
    if (!el) return;
    const deg = _state.plan.north_offset_deg;
    el.innerHTML = `
      <svg width="120" height="120" viewBox="-60 -60 120 120" style="cursor:grab">
        <circle class="fp-compass-ring" cx="0" cy="0" r="55" fill="none"/>
        <text class="fp-compass-label" x="0" y="-44" text-anchor="middle" font-size="10">N</text>
        <text class="fp-compass-label" x="44" y="3"   text-anchor="middle" font-size="10">E</text>
        <text class="fp-compass-label" x="0" y="50"   text-anchor="middle" font-size="10">S</text>
        <text class="fp-compass-label" x="-44" y="3"  text-anchor="middle" font-size="10">W</text>
        <g id="fpCompassNeedle" transform="rotate(${deg})">
          <polygon class="fp-compass-needle" points="0,-50 -8,0 0,-12 8,0"/>
          <polygon class="fp-compass-tail"   points="0,50 -8,0 0,12 8,0"/>
        </g>
      </svg>`;
    const svg = el.querySelector('svg');
    let dragging = false;
    const update = (cx, cy) => {
        const rect = svg.getBoundingClientRect();
        const x = cx - rect.left - rect.width / 2;
        const y = cy - rect.top  - rect.height / 2;
        const a = (Math.atan2(x, -y) * 180 / Math.PI + 360) % 360;
        _state.plan.north_offset_deg = Math.round(a);
        document.getElementById('fpCompassNeedle').setAttribute('transform', `rotate(${a})`);
        document.getElementById('fpNorthDeg').value = Math.round(a);
    };
    svg.addEventListener('mousedown', e => { dragging = true; update(e.clientX, e.clientY); });
    window.addEventListener('mousemove', e => { if (dragging) update(e.clientX, e.clientY); });
    window.addEventListener('mouseup', () => { dragging = false; });
}

// ──────────────────────────── coordinates ────────────────────────────

// SVG y-axis goes DOWN. Model y-axis goes UP. Convert at the boundary.
function modelToSvg(p)  { return { x: p.x,  y: -p.y }; }
function svgToModel(p)  { return { x: p.x,  y: -p.y }; }

function clientToSvg(evt) {
    const svg = document.getElementById('fpCanvas');
    const pt = svg.createSVGPoint();
    pt.x = evt.clientX; pt.y = evt.clientY;
    const ctm = document.getElementById('fpScene').getScreenCTM();
    if (!ctm) return { x: 0, y: 0 };
    const m = pt.matrixTransform(ctm.inverse());
    return { x: m.x, y: m.y };
}

function snap(v) { return Math.round(v / SNAP_M) * SNAP_M; }
function snapPt(p) { return { x: snap(p.x), y: snap(p.y) }; }

// ──────────────────────────── scene render ────────────────────────────

function renderScene() {
    const lvl = currentLevel();
    if (!lvl) return;
    const scene = document.getElementById('fpScene');
    const m2px = _state.zoom;
    scene.setAttribute('transform',
        `translate(${_state.pan.x}, ${_state.pan.y}) scale(${m2px}, ${m2px})`);

    const parts = [];

    // Background image (drawn first, under everything else)
    if (_state.showBackground && lvl.background?.present) {
        const bg = lvl.background;
        const wM = bg.image_width_px / bg.pixels_per_metre;
        const hM = bg.image_height_px / bg.pixels_per_metre;
        // Image origin sits at (origin_x_m, origin_y_m) in model space.
        // In SVG coords (+y down), the top-left of the image goes there.
        // Model uses +y up, so we place top-left at (origin_x, -(origin_y + hM)) → svg y = origin_y_m
        // Actually: SVG <image> draws downward from its (x,y). We want the
        // image's top-left in MODEL space at (origin_x_m, origin_y_m + hM)
        // (i.e. top edge in model = origin_y + hM; bottom = origin_y).
        // Top-left in SVG = modelToSvg({x: origin_x, y: origin_y + hM}) = (origin_x, -(origin_y + hM))
        const tlSvg = modelToSvg({ x: bg.origin_x_m, y: bg.origin_y_m + hM });
        parts.push(`
          <image href="/api/heating/floor-plan/image/${escapeAttr(lvl.id)}?t=${bg._cb || 0}"
                 x="${tlSvg.x}" y="${tlSvg.y}" width="${wM}" height="${hM}"
                 opacity="${bg.opacity}" preserveAspectRatio="none"
                 transform="rotate(${-(bg.rotation_deg || 0)} ${tlSvg.x + wM/2} ${tlSvg.y + hM/2})"
                 pointer-events="none"/>`);
    }

    // Rooms (under shapes but over the image)
    // Build circuit colour palette for rooms
    const CIRCUIT_COLOURS = [
        'rgba(59,130,246,0.18)',   // blue
        'rgba(16,185,129,0.18)',   // green
        'rgba(245,158,11,0.18)',   // amber
        'rgba(239,68,68,0.18)',    // red
        'rgba(139,92,246,0.18)',   // violet
        'rgba(236,72,153,0.18)',   // pink
        'rgba(20,184,166,0.18)',   // teal
        'rgba(249,115,22,0.18)',   // orange
    ];
    const circuitColourMap = {};
    (_state.plan.circuits || []).forEach((c, i) => {
        circuitColourMap[c.id] = CIRCUIT_COLOURS[i % CIRCUIT_COLOURS.length];
    });

    for (const r of lvl.rooms) {
        const sel = isSelected('room', r.id);
        const path = polygonToPath(r.polygon);
        const fillColour = r.circuit_id && circuitColourMap[r.circuit_id]
            ? circuitColourMap[r.circuit_id]
            : 'rgba(100,116,139,0.08)';
        parts.push(`
          <path class="fp-room ${sel ? 'fp-selected' : ''}" d="${path}"
                fill="${fillColour}"
                stroke-width="${sel ? 0.05 : 0.025}" stroke-dasharray="0.1 0.1"
                data-kind="room" data-id="${r.id}" pointer-events="visiblePainted"/>`);
        const c = polygonCentroid(r.polygon);
        const sc = modelToSvg(c);
        const circuitName = r.circuit_id
            ? ((_state.plan.circuits || []).find(x => x.id === r.circuit_id)?.name || r.circuit_id)
            : null;
        parts.push(`<text class="fp-room-label" x="${sc.x}" y="${sc.y}" font-size="0.18" text-anchor="middle"
                    pointer-events="none">${escapeHtml(r.name || r.id)}</text>`);
        if (circuitName) {
            parts.push(`<text class="fp-room-label" x="${sc.x}" y="${sc.y + 0.22}" font-size="0.13" text-anchor="middle"
                        fill="#64748b" pointer-events="none">${escapeHtml(circuitName)}</text>`);
        }
    }

    // Walls
    for (const w of lvl.walls) {
        const sel = isSelected('wall', w.id);
        const a = modelToSvg({ x: w.x1, y: w.y1 });
        const b = modelToSvg({ x: w.x2, y: w.y2 });
        const typ = w.type || 'unknown';
        parts.push(`
          <line class="fp-wall fp-wall-${typ} ${sel ? 'fp-selected' : ''}"
                x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"
                stroke-width="${sel ? 0.12 : 0.08}" stroke-linecap="square"
                data-kind="wall" data-id="${w.id}" style="cursor:pointer"/>`);
        // Endpoint drag handles only on the selected wall, and only when the
        // current tool is 'select' (otherwise drawing tools take precedence).
        if (sel && _state.tool === 'select') {
            parts.push(`
              <circle class="fp-wall-handle" cx="${a.x}" cy="${a.y}" r="0.16"
                      stroke-width="0.04" data-kind="wall-handle"
                      data-id="${w.id}" data-which="1"
                      style="cursor:grab"/>
              <circle class="fp-wall-handle" cx="${b.x}" cy="${b.y}" r="0.16"
                      stroke-width="0.04" data-kind="wall-handle"
                      data-id="${w.id}" data-which="2"
                      style="cursor:grab"/>`);
        }
    }

    // Openings — drawn ON the wall they belong to
    for (const o of lvl.openings) {
        const wall = lvl.walls.find(w => w.id === o.wall_id);
        if (!wall) continue;
        const sel = isSelected('opening', o.id);
        const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1);
        if (wlen < 1e-6) continue;
        const ux = (wall.x2 - wall.x1) / wlen;
        const uy = (wall.y2 - wall.y1) / wlen;
        const start = { x: wall.x1 + ux * o.offset_m, y: wall.y1 + uy * o.offset_m };
        const end   = { x: start.x + ux * o.width_m,  y: start.y + uy * o.width_m };
        const sa = modelToSvg(start), sb = modelToSvg(end);
        parts.push(`
          <line class="fp-opening fp-opening-${o.kind} ${sel ? 'fp-selected' : ''}"
                x1="${sa.x}" y1="${sa.y}" x2="${sb.x}" y2="${sb.y}"
                stroke-width="${sel ? 0.16 : 0.12}" stroke-linecap="butt"
                data-kind="opening" data-id="${o.id}" style="cursor:pointer"/>`);
        if (o.kind === 'door') {
            const mid = modelToSvg({ x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 });
            parts.push(`<circle class="fp-door-pivot" cx="${mid.x}" cy="${mid.y}" r="0.05" pointer-events="none"/>`);
        }
    }

    // Radiators — plan view. Two render modes:
    //   wall-mounted: drawn as a thin strip ALONG the host wall (fixed
    //     0.1 m depth in plan view — `height_m` is the radiator's PHYSICAL
    //     height, used for sizing/heat calcs, not its plan-view footprint).
    //     The strip is offset perpendicular toward the bound room centroid
    //     so it sits on the room-side face of the wall.
    //   freestanding: drawn as a length × 0.1 m axis-aligned strip at (x, y).
    //     Used for towel rails, columns, underfloor zones, or anywhere the
    //     user explicitly placed away from a wall.
    const RAD_PLAN_DEPTH_M = 0.10;   // fixed plan-view strip depth
    for (const r of lvl.radiators) {
        const sel = isSelected('radiator', r.id);
        const len = r.length_m || 0.6;
        const hgt = RAD_PLAN_DEPTH_M;   // plan-view depth — NOT r.height_m
        const wall = r.wall_id ? lvl.walls.find(w => w.id === r.wall_id) : null;

        if (wall) {
            // Wall-mounted geometry
            const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) || 1;
            const ux = (wall.x2 - wall.x1) / wlen, uy = (wall.y2 - wall.y1) / wlen;
            // Clamp offset so the radiator's CENTRE stays on the wall (length
            // can overhang slightly if wall is shorter than radiator)
            const t0 = Math.max(0, Math.min(wlen - len, (r.offset_m ?? wlen / 2)));
            // Perpendicular direction: rotate (ux, uy) 90° → (-uy, ux). We
            // pick the side facing the bound room's centroid. If no room
            // bound, default to the +90 side.
            let nx = -uy, ny = ux;
            const room = r.room_id ? (lvl.rooms || []).find(rm => rm.id === r.room_id) : null;
            if (room) {
                const c = polygonCentroid(room.polygon);
                const wallMidX = (wall.x1 + wall.x2) / 2;
                const wallMidY = (wall.y1 + wall.y2) / 2;
                // Dot product of (centroid - wallMid) with (nx, ny). Positive
                // means the chosen normal already points toward the room.
                const dot = (c.x - wallMidX) * nx + (c.y - wallMidY) * ny;
                if (dot < 0) { nx = -nx; ny = -ny; }
            }
            // Four corners of the radiator rectangle, in MODEL coords.
            const ax = wall.x1 + ux * t0,         ay = wall.y1 + uy * t0;
            const bx = ax + ux * len,             by = ay + uy * len;
            const cx = bx + nx * hgt,             cy = by + ny * hgt;
            const dx = ax + nx * hgt,             dy = ay + ny * hgt;
            // Convert to SVG coords (flip y)
            const A = modelToSvg({ x: ax, y: ay });
            const B = modelToSvg({ x: bx, y: by });
            const C = modelToSvg({ x: cx, y: cy });
            const D = modelToSvg({ x: dx, y: dy });
            // Label centre = average of midpoints of AB and CD
            const labelMx = (A.x + B.x + C.x + D.x) / 4;
            const labelMy = (A.y + B.y + C.y + D.y) / 4;
            parts.push(`
              <g data-kind="radiator" data-id="${r.id}" style="cursor:pointer">
                <polygon class="fp-radiator ${sel ? 'fp-selected' : ''}"
                         points="${A.x},${A.y} ${B.x},${B.y} ${C.x},${C.y} ${D.x},${D.y}"
                         stroke-width="${sel ? 0.04 : 0.02}"/>
                <text class="fp-radiator-label" x="${labelMx}" y="${labelMy + 0.05}"
                      font-size="0.14" text-anchor="middle">${Math.round(r.watts_at_dt50 || 0)}W</text>
              </g>`);
            // Slide handle: midpoint of the wall-side edge (A→B), only when
            // selected with the Select tool. Drag to slide along the wall.
            if (sel && _state.tool === 'select') {
                const handleMx = (A.x + B.x) / 2;
                const handleMy = (A.y + B.y) / 2;
                parts.push(`
                  <circle class="fp-rad-handle" cx="${handleMx}" cy="${handleMy}" r="0.18"
                          stroke-width="0.04" data-kind="rad-handle" data-id="${r.id}"
                          style="cursor:ew-resize"/>`);
            }
        } else {
            // Freestanding geometry — axis-aligned strip at (x, y)
            const p = modelToSvg({ x: r.x ?? 0, y: r.y ?? 0 });
            parts.push(`
              <g data-kind="radiator" data-id="${r.id}" style="cursor:pointer">
                <rect class="fp-radiator ${sel ? 'fp-selected' : ''}"
                      x="${p.x - len / 2}" y="${p.y - hgt / 2}" width="${len}" height="${hgt}"
                      stroke-width="${sel ? 0.04 : 0.02}"/>
                <text class="fp-radiator-label" x="${p.x}" y="${p.y + hgt / 2 + 0.22}"
                      font-size="0.14" text-anchor="middle">${Math.round(r.watts_at_dt50 || 0)}W</text>
              </g>`);
        }
    }

    // Sensors
    for (const s of lvl.sensors) {
        const sel = isSelected('sensor', s.id);
        const p = modelToSvg({ x: s.x ?? 0, y: s.y ?? 0 });
        parts.push(`
          <g data-kind="sensor" data-id="${s.id}" style="cursor:pointer">
            <circle class="fp-sensor ${sel ? 'fp-selected' : ''}"
                    cx="${p.x}" cy="${p.y}" r="0.13" stroke-width="${sel ? 0.04 : 0.02}"/>
            ${s.primary ? `<circle class="fp-sensor-primary-dot" cx="${p.x}" cy="${p.y}" r="0.06"/>` : ''}
          </g>`);
    }

    // Contact sensors — drawn near their opening's centre, slightly offset
    for (const c of lvl.contacts) {
        const op = lvl.openings.find(o => o.id === c.opening_id);
        if (!op) continue;
        const wall = lvl.walls.find(w => w.id === op.wall_id);
        if (!wall) continue;
        const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) || 1;
        const ux = (wall.x2 - wall.x1) / wlen, uy = (wall.y2 - wall.y1) / wlen;
        const cx = wall.x1 + ux * (op.offset_m + op.width_m / 2);
        const cy = wall.y1 + uy * (op.offset_m + op.width_m / 2);
        const p = modelToSvg({ x: cx + (-uy) * 0.12, y: cy + ux * 0.12 });
        const sel = isSelected('contact', c.id);
        parts.push(`
          <g data-kind="contact" data-id="${c.id}" style="cursor:pointer">
            <rect class="fp-contact ${sel ? 'fp-selected' : ''}"
                  x="${p.x - 0.07}" y="${p.y - 0.07}" width="0.14" height="0.14"
                  stroke-width="${sel ? 0.04 : 0.02}"/>
          </g>`);
    }

    scene.innerHTML = parts.join('');

    // Click bindings.
    // Wall-handle elements get special treatment: mousedown starts a drag,
    // not a selection (the wall is already selected when handles are visible).
    scene.querySelectorAll('[data-kind="wall-handle"]').forEach(el => {
        el.addEventListener('mousedown', e => {
            if (_state.tool !== 'select') return;
            e.stopPropagation();
            const wall = currentLevel().walls.find(w => w.id === el.dataset.id);
            if (!wall) return;
            _wallDrag = { wallId: wall.id, which: parseInt(el.dataset.which, 10) };
        });
    });

    // Radiator slide handle — drag along the host wall to change offset_m.
    scene.querySelectorAll('[data-kind="rad-handle"]').forEach(el => {
        el.addEventListener('mousedown', e => {
            if (_state.tool !== 'select') return;
            e.stopPropagation();
            const rad = currentLevel().radiators.find(r => r.id === el.dataset.id);
            if (!rad || !rad.wall_id) return;
            _radDrag = { radId: rad.id };
        });
    });

    // Regular selection on every other interactive element.
    scene.querySelectorAll('[data-kind][data-id]').forEach(el => {
        if (el.dataset.kind === 'wall-handle') return;
        if (el.dataset.kind === 'rad-handle') return;
        el.addEventListener('mousedown', e => {
            if (_state.tool !== 'select') return;
            e.stopPropagation();
            _state.selection = { kind: el.dataset.kind, id: el.dataset.id };
            renderScene(); renderProps();
        });
    });
}

function renderOverlay() {
    const ov = document.getElementById('fpOverlay');
    let html = '';

    // Drawing preview
    if (_state.drawBuffer) {
        const m2px = _state.zoom;
        ov.setAttribute('transform',
            `translate(${_state.pan.x}, ${_state.pan.y}) scale(${m2px}, ${m2px})`);
        const db = _state.drawBuffer;

        // Window/door: single-segment drag (start → cur) along a host wall
        if ((_state.tool === 'window' || _state.tool === 'door') && db.start && db.cur) {
            const a = modelToSvg(db.start);
            const b = modelToSvg(db.cur);
            html += `<line class="fp-preview fp-preview-${_state.tool}"
                          x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"
                          stroke-width="0.08" stroke-dasharray="0.1 0.05"/>`;
            const dx = db.cur.x - db.start.x, dy = db.cur.y - db.start.y;
            const len = Math.hypot(dx, dy);
            const mid = modelToSvg({ x: (db.start.x + db.cur.x) / 2, y: (db.start.y + db.cur.y) / 2 });
            html += `<text class="fp-preview-label" x="${mid.x}" y="${mid.y - 0.15}" font-size="0.15" text-anchor="middle">${len.toFixed(2)} m</text>`;
        }

        // Wall (chain mode) and Room (polygon) share the same preview
        // shape: a polyline through committed vertices plus a rubber-band
        // segment to the cursor, with dots at every vertex.
        else if ((_state.tool === 'wall' || _state.tool === 'room') && db.points) {
            const ptsSvg = db.points.map(modelToSvg);
            const mouseSvg = db.cur ? modelToSvg(db.cur) : null;
            const polyPts = ptsSvg.map(p => `${p.x},${p.y}`).join(' ')
                + (mouseSvg ? ` ${mouseSvg.x},${mouseSvg.y}` : '');
            const cls = _state.tool === 'room' ? 'fp-preview-room' : 'fp-preview fp-preview-wall';
            html += `<polyline class="${cls}"
                              points="${polyPts}"
                              stroke-width="0.06" stroke-dasharray="0.1 0.05"
                              fill="none"/>`;
            // Vertex dots — committed vertices are solid; first one slightly
            // bigger to invite "click here to close"
            for (let i = 0; i < ptsSvg.length; i++) {
                const sp = ptsSvg[i];
                const r = (i === 0 && _state.tool === 'wall' && db.points.length >= 2) ? 0.12 : 0.07;
                html += `<circle class="fp-preview-vertex" cx="${sp.x}" cy="${sp.y}" r="${r}"/>`;
            }
            // Live length label on the rubber-band segment for walls
            if (_state.tool === 'wall' && mouseSvg && db.points.length > 0) {
                const last = db.points[db.points.length - 1];
                const dx = db.cur.x - last.x, dy = db.cur.y - last.y;
                const len = Math.hypot(dx, dy);
                const mid = modelToSvg({ x: (last.x + db.cur.x) / 2, y: (last.y + db.cur.y) / 2 });
                html += `<text class="fp-preview-label" x="${mid.x}" y="${mid.y - 0.15}"
                               font-size="0.18" text-anchor="middle">${len.toFixed(2)} m</text>`;
            }
        }
    }

    // Calibration: show first picked point and rubber-band to mouse
    if (_state.calibration && _state.calibration.p1) {
        const m2px = _state.zoom;
        ov.setAttribute('transform',
            `translate(${_state.pan.x}, ${_state.pan.y}) scale(${m2px}, ${m2px})`);
        const cal = _state.calibration;
        const p1svg = modelToSvg(cal.p1);
        // Crosshair-style marker: ring + centre dot for clear targeting.
        html += `<circle class="fp-calibration-marker" cx="${p1svg.x}" cy="${p1svg.y}"
                         r="0.25" stroke-width="0.06" fill="none"/>`;
        html += `<circle class="fp-calibration-marker" cx="${p1svg.x}" cy="${p1svg.y}"
                         r="0.06" stroke-width="0"/>`;
        html += `<text class="fp-preview-label" x="${p1svg.x}" y="${p1svg.y - 0.35}"
                       font-size="0.18" text-anchor="middle">A</text>`;

        // Live rubber-band to cursor (or to the locked p2 if click 2 just landed)
        const target = cal.p2 || cal.cur;
        if (target) {
            const p2svg = modelToSvg(target);
            html += `<line class="fp-preview fp-preview-wall"
                          x1="${p1svg.x}" y1="${p1svg.y}"
                          x2="${p2svg.x}" y2="${p2svg.y}"
                          stroke-width="0.06" stroke-dasharray="0.15 0.08"/>`;
            const dx = target.x - cal.p1.x, dy = target.y - cal.p1.y;
            const d  = Math.hypot(dx, dy);
            const mid = modelToSvg({ x: (cal.p1.x + target.x) / 2, y: (cal.p1.y + target.y) / 2 });
            html += `<text class="fp-preview-label" x="${mid.x}" y="${mid.y - 0.2}"
                           font-size="0.18" text-anchor="middle">${d.toFixed(2)} m drawn</text>`;
            // Second marker at current/locked endpoint
            html += `<circle class="fp-calibration-marker" cx="${p2svg.x}" cy="${p2svg.y}"
                             r="0.25" stroke-width="0.06" fill="none"/>`;
            html += `<circle class="fp-calibration-marker" cx="${p2svg.x}" cy="${p2svg.y}"
                             r="0.06" stroke-width="0"/>`;
            html += `<text class="fp-preview-label" x="${p2svg.x}" y="${p2svg.y - 0.35}"
                           font-size="0.18" text-anchor="middle">B</text>`;
        }
    }

    // Sun overlay
    if (_state.showSun && _state.sunData && _state.sunData.points) {
        const r = 5; // metres radius
        const path = [];
        for (const pt of _state.sunData.points) {
            if (pt.el <= 0) continue;
            const planAz = (pt.az + _state.plan.north_offset_deg) % 360;
            const ang = (planAz * Math.PI) / 180;
            const x = Math.sin(ang) * r * Math.cos(pt.el * Math.PI / 180);
            const y = Math.cos(ang) * r * Math.cos(pt.el * Math.PI / 180);
            const s = modelToSvg({ x, y });
            path.push(`${s.x},${s.y}`);
        }
        if (path.length > 1) {
            html += `<polyline class="fp-sun-path" points="${path.join(' ')}"
                              fill="none" stroke-width="0.05" stroke-dasharray="0.15 0.08"/>`;
        }
    }

    ov.innerHTML = html;
}

// ──────────────────────────── interactions ────────────────────────────

function setTool(tool) {
    _state.tool = tool;
    _state.drawBuffer = null;
    _state.calibration = null;
    _state.selection = null;
    renderToolbar(); renderProps(); renderOverlay();
}

let _isPanning = false;
let _panStart = null;

// Wall vertex drag state — set when the user grabs an endpoint handle on
// a selected wall. `which` is 1 or 2 (which endpoint of the wall). The
// drag updates `wall.x{which}/y{which}` live; mouse-up commits with snap.
let _wallDrag = null;

// Radiator drag state — set when the user grabs the slide handle on a
// selected wall-mounted radiator. Drag projects the cursor onto the host
// wall and updates `radiator.offset_m` live, clamped so the radiator stays
// on the wall.
let _radDrag = null;

function onCanvasMouseDown(e) {
    if (e.button === 1 || (e.button === 0 && e.shiftKey)) {
        _isPanning = true;
        _panStart = { x: e.clientX - _state.pan.x, y: e.clientY - _state.pan.y };
        document.getElementById('fpCanvas').style.cursor = 'grabbing';
        return;
    }
    if (e.button !== 0) return;
    const m = snapPt(clientToSvgModel(e));
    const lvl = currentLevel();

    if (_state.tool === 'wall') {
        // Chain mode: each click drops a vertex; consecutive vertices form a
        // wall. Click the first vertex again (or press Enter / Esc / right-
        // click) to finish the chain.
        const snapped = snapToExistingEndpoint(lvl, m) || m;
        if (!_state.drawBuffer || !_state.drawBuffer.points) {
            _state.drawBuffer = { points: [snapped], cur: snapped };
        } else {
            const pts = _state.drawBuffer.points;
            const first = pts[0];
            const last = pts[pts.length - 1];
            // Click on first point closes the chain
            if (pts.length >= 2 && Math.hypot(snapped.x - first.x, snapped.y - first.y) < 0.2) {
                pts.push(first);
                finishWallChain();
                return;
            }
            // Reject zero-length segment
            if (Math.hypot(snapped.x - last.x, snapped.y - last.y) < 0.2) {
                toast('warn', 'Too short', 'Move further before clicking.');
                return;
            }
            pts.push(snapped);
        }
        renderOverlay();
    } else if (_state.tool === 'window' || _state.tool === 'door') {
        // Find nearest wall to start point; constrain to it
        const w = nearestWall(lvl, m);
        if (!w) return;
        const proj = projectPointOntoSegment(m, w);
        _state.drawBuffer = { wall: w, start: proj.point, startT: proj.t, cur: proj.point };
    } else if (_state.tool === 'room') {
        if (!_state.drawBuffer) {
            _state.drawBuffer = { points: [m], cur: m };
        } else {
            // Click near the first point closes the polygon
            const first = _state.drawBuffer.points[0];
            if (Math.hypot(m.x - first.x, m.y - first.y) < 0.3 && _state.drawBuffer.points.length >= 3) {
                finishRoom();
            } else {
                _state.drawBuffer.points.push(m);
            }
        }
        renderOverlay();
    } else if (_state.tool === 'radiator' || _state.tool === 'sensor') {
        addPointFeature(_state.tool, m);
    } else if (_state.tool === 'contact') {
        // If the click is near a window/door, auto-bind to that opening.
        // Otherwise place an unbound contact and let the user pick the
        // opening from a dropdown in the property panel.
        const op = nearestOpening(lvl, m);
        addContact(op || null);
        if (!op) {
            toast('info', 'No opening', 'Contact placed; choose a window/door in the panel.');
        }
    } else if (_state.tool === 'calibrate') {
        if (!_state.calibration) {
            _state.calibration = { p1: m, cur: m };
            toast('info', 'Calibration', 'Click the second known point.');
            renderOverlay();
        } else {
            const p1 = _state.calibration.p1;
            const p2 = m;
            const drawnDist = Math.hypot(p2.x - p1.x, p2.y - p1.y);
            // Show BOTH points before the prompt, so the user sees their
            // second click registered. The prompt blocks the event loop, so
            // we render synchronously first, then yield to let the browser
            // paint, then open the prompt.
            _state.calibration = { p1, p2, cur: p2 };
            renderOverlay();
            // requestAnimationFrame to ensure the second marker is painted
            // before the modal prompt blocks rendering on some browsers.
            requestAnimationFrame(() => {
                _state.calibration = null;
                promptCalibrationDistance(p1, p2, drawnDist);
                renderOverlay();
            });
        }
    } else if (_state.tool === 'select') {
        // Empty-canvas click clears selection
        _state.selection = null;
        renderScene(); renderProps();
    }
}

function onCanvasMouseMove(e) {
    if (_isPanning && _panStart) {
        _state.pan.x = e.clientX - _panStart.x;
        _state.pan.y = e.clientY - _panStart.y;
        renderScene(); renderOverlay();
        return;
    }
    // Wall-handle drag: live update the endpoint as the user moves. Snap to
    // grid by default; if a nearby OTHER wall's endpoint is in range, prefer
    // that (so dragging an endpoint can reattach to a neighbour cleanly).
    if (_wallDrag) {
        const lvl = currentLevel();
        const w = lvl.walls.find(x => x.id === _wallDrag.wallId);
        if (w) {
            const m = snapPt(clientToSvgModel(e));
            const snapped = snapToOtherWallEndpoint(lvl, w.id, m) || m;
            if (_wallDrag.which === 1) { w.x1 = snapped.x; w.y1 = snapped.y; }
            else                       { w.x2 = snapped.x; w.y2 = snapped.y; }
            renderScene(); renderProps();
        }
        return;
    }
    // Radiator slide drag: project cursor onto the host wall, compute the
    // offset that keeps the radiator's visual centre under the cursor, clamp
    // so it stays on the wall, write back to offset_m.
    if (_radDrag) {
        const lvl = currentLevel();
        const r = lvl.radiators.find(x => x.id === _radDrag.radId);
        const w = r?.wall_id ? lvl.walls.find(wl => wl.id === r.wall_id) : null;
        if (r && w) {
            const m = clientToSvgModel(e);   // no grid-snap; smooth drag
            const proj = projectPointOntoSegment(m, w);
            const wlen = Math.hypot(w.x2 - w.x1, w.y2 - w.y1) || 1;
            const len = r.length_m || 0.6;
            // proj.t is the cursor's position along the wall (in metres from
            // wall.start). We want the radiator CENTRE under the cursor:
            //   offset_m = proj.t - len/2
            let offset = proj.t - len / 2;
            // Clamp so the radiator stays on the wall. If wall is shorter
            // than the radiator, pin offset to 0 — render code is forgiving.
            const maxOffset = Math.max(0, wlen - len);
            offset = Math.max(0, Math.min(maxOffset, offset));
            r.offset_m = Math.round(offset * 1000) / 1000;
            renderScene(); renderProps();
        }
        return;
    }
    // Calibration tool tracks cursor between the two clicks for live feedback.
    if (_state.tool === 'calibrate' && _state.calibration && _state.calibration.p1) {
        _state.calibration.cur = snapPt(clientToSvgModel(e));
        renderOverlay();
        return;
    }
    if (!_state.drawBuffer) return;
    const m = snapPt(clientToSvgModel(e));
    if (_state.tool === 'wall') {
        // Snap to nearby existing endpoints during chain drawing
        const lvl = currentLevel();
        _state.drawBuffer.cur = snapToExistingEndpoint(lvl, m) || m;
    } else if (_state.tool === 'window' || _state.tool === 'door') {
        const proj = projectPointOntoSegment(m, _state.drawBuffer.wall);
        _state.drawBuffer.cur = proj.point;
        _state.drawBuffer.curT = proj.t;
    } else if (_state.tool === 'room') {
        _state.drawBuffer.cur = m;
    }
    renderOverlay();
}

function onCanvasMouseUp(e) {
    if (_isPanning) { _isPanning = false; _panStart = null; renderToolbar(); return; }
    if (_wallDrag) { _wallDrag = null; renderScene(); renderProps(); return; }
    if (_radDrag)  { _radDrag = null;  renderScene(); renderProps(); return; }
    if (!_state.drawBuffer) return;
    const lvl = currentLevel();

    if (_state.tool === 'wall') {
        // Chain mode commits walls in onCanvasMouseDown, not on mouse-up.
        // Nothing to do here. (Kept as a no-op so the mouseup still fires
        // for clean event handling.)
        return;
    } else if (_state.tool === 'window' || _state.tool === 'door') {
        const wall = _state.drawBuffer.wall;
        const t1 = _state.drawBuffer.startT;
        const t2 = _state.drawBuffer.curT;
        const offset_m = Math.min(t1, t2);
        const width_m  = Math.abs(t2 - t1);
        if (width_m >= 0.2) {
            const id = genId(_state.tool === 'window' ? 'win' : 'dr');
            const op = {
                id, wall_id: wall.id, kind: _state.tool,
                offset_m, width_m,
                height_m: _state.tool === 'window' ? 1.2 : 2.0,
            };
            if (_state.tool === 'window') op.glazing = 'double';
            else op.door_type = 'internal';
            lvl.openings.push(op);
            _state.selection = { kind: 'opening', id };
        }
        _state.drawBuffer = null;
        renderScene(); renderOverlay(); renderProps();
    }
}

function onCanvasWheel(e) {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
    zoomBy(factor, e.clientX, e.clientY);
}

function clientToSvgModel(evt) {
    const svgPt = clientToSvg(evt);
    return svgToModel(svgPt);
}

function isSelected(kind, id) {
    return _state.selection && _state.selection.kind === kind && _state.selection.id === id;
}

// ─────────────────────── geometry helpers (frontend) ───────────────────────

function projectPointOntoSegment(p, w) {
    const dx = w.x2 - w.x1, dy = w.y2 - w.y1;
    const L = Math.hypot(dx, dy);
    if (L < 1e-9) return { point: { x: w.x1, y: w.y1 }, t: 0 };
    const t = ((p.x - w.x1) * dx + (p.y - w.y1) * dy) / (L * L) * L;
    const tClamped = Math.max(0, Math.min(L, t));
    return {
        t: tClamped,
        point: { x: w.x1 + (dx / L) * tClamped, y: w.y1 + (dy / L) * tClamped },
    };
}

function nearestWall(lvl, p) {
    let best = null, bestD = Infinity;
    for (const w of lvl.walls) {
        const proj = projectPointOntoSegment(p, w);
        const d = Math.hypot(p.x - proj.point.x, p.y - proj.point.y);
        if (d < bestD) { bestD = d; best = w; }
    }
    return bestD < 0.5 ? best : null;
}

function nearestOpening(lvl, p) {
    let best = null, bestD = Infinity;
    for (const o of lvl.openings) {
        const w = lvl.walls.find(x => x.id === o.wall_id);
        if (!w) continue;
        const wlen = Math.hypot(w.x2 - w.x1, w.y2 - w.y1) || 1;
        const ux = (w.x2 - w.x1) / wlen, uy = (w.y2 - w.y1) / wlen;
        const cx = w.x1 + ux * (o.offset_m + o.width_m / 2);
        const cy = w.y1 + uy * (o.offset_m + o.width_m / 2);
        const d = Math.hypot(p.x - cx, p.y - cy);
        if (d < bestD) { bestD = d; best = o; }
    }
    return bestD < 0.6 ? best : null;
}

/**
 * If `p` is within ~0.25 m of an existing wall endpoint (or an in-progress
 * chain vertex), return that endpoint snapped exactly. Otherwise null. This
 * makes chains close cleanly and adjacent walls share endpoints exactly.
 */
function snapToExistingEndpoint(lvl, p) {
    const SNAP_R = 0.25;
    let best = null, bestD = SNAP_R;
    for (const w of (lvl.walls || [])) {
        for (const ep of [{ x: w.x1, y: w.y1 }, { x: w.x2, y: w.y2 }]) {
            const d = Math.hypot(p.x - ep.x, p.y - ep.y);
            if (d < bestD) { bestD = d; best = ep; }
        }
    }
    // Also snap to vertices already placed in the current chain
    if (_state?.drawBuffer?.points) {
        for (const ep of _state.drawBuffer.points) {
            const d = Math.hypot(p.x - ep.x, p.y - ep.y);
            if (d < bestD) { bestD = d; best = ep; }
        }
    }
    return best ? { x: best.x, y: best.y } : null;
}

/**
 * Snap-to-endpoint variant for drag operations: like snapToExistingEndpoint
 * but excludes the wall being dragged (so an endpoint can't snap to itself
 * or to its other end on the same wall).
 */
function snapToOtherWallEndpoint(lvl, excludeWallId, p) {
    const SNAP_R = 0.25;
    let best = null, bestD = SNAP_R;
    for (const w of (lvl.walls || [])) {
        if (w.id === excludeWallId) continue;
        for (const ep of [{ x: w.x1, y: w.y1 }, { x: w.x2, y: w.y2 }]) {
            const d = Math.hypot(p.x - ep.x, p.y - ep.y);
            if (d < bestD) { bestD = d; best = ep; }
        }
    }
    return best ? { x: best.x, y: best.y } : null;
}

function polygonCentroid(poly) {
    if (!poly || poly.length < 3) {
        return poly && poly[0] ? { x: poly[0][0], y: poly[0][1] } : { x: 0, y: 0 };
    }
    let cx = 0, cy = 0, a = 0;
    for (let i = 0; i < poly.length; i++) {
        const [x1, y1] = poly[i];
        const [x2, y2] = poly[(i + 1) % poly.length];
        const cross = x1 * y2 - x2 * y1;
        a += cross;
        cx += (x1 + x2) * cross;
        cy += (y1 + y2) * cross;
    }
    a *= 0.5;
    if (Math.abs(a) < 1e-9) {
        return { x: poly[0][0], y: poly[0][1] };
    }
    return { x: cx / (6 * a), y: cy / (6 * a) };
}

function polygonToPath(poly) {
    if (!poly || poly.length < 2) return '';
    return poly.map((p, i) => {
        const sp = modelToSvg({ x: p[0], y: p[1] });
        return `${i === 0 ? 'M' : 'L'} ${sp.x} ${sp.y}`;
    }).join(' ') + ' Z';
}

// ─────────────────────── circuit management ───────────────────────────

const CIRCUIT_BADGE_COLOURS = [
    '#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899','#14b8a6','#f97316'
];

function renderCircuitList() {
    const wrap = document.getElementById('fpCircuitList');
    if (!wrap) return;
    const circuits = _state.plan.circuits || [];
    if (!circuits.length) {
        wrap.innerHTML = '<div class="text-muted small fst-italic">No circuits yet.</div>';
        return;
    }
    // Count rooms per circuit across all levels
    const roomCounts = {};
    (_state.plan.levels || []).forEach(lvl => {
        (lvl.rooms || []).forEach(r => {
            if (r.circuit_id) roomCounts[r.circuit_id] = (roomCounts[r.circuit_id] || 0) + 1;
        });
    });
    wrap.innerHTML = circuits.map((c, i) => {
        const colour = CIRCUIT_BADGE_COLOURS[i % CIRCUIT_BADGE_COLOURS.length];
        const cnt = roomCounts[c.id] || 0;
        return `<div class="d-flex align-items-center gap-1 mb-1" data-circuit-id="${c.id}">
          <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${colour};flex-shrink:0"></span>
          <span class="small flex-grow-1 text-truncate" title="${escapeHtml(c.id)}">${escapeHtml(c.name)}<span class="text-muted ms-1">(${cnt})</span></span>
          <button class="btn btn-sm btn-link p-0 text-primary" data-action="edit-circuit" data-circuit-id="${c.id}" title="Edit"><i class="fas fa-pencil-alt fa-xs"></i></button>
          <button class="btn btn-sm btn-link p-0 text-danger" data-action="delete-circuit" data-circuit-id="${c.id}" title="Delete"><i class="fas fa-trash fa-xs"></i></button>
        </div>`;
    }).join('');

    wrap.querySelectorAll('[data-action="edit-circuit"]').forEach(btn => {
        btn.addEventListener('click', () => editCircuit(btn.dataset.circuitId));
    });
    wrap.querySelectorAll('[data-action="delete-circuit"]').forEach(btn => {
        btn.addEventListener('click', () => deleteCircuit(btn.dataset.circuitId));
    });
}

function addCircuit() {
    const name = prompt('Circuit name (e.g. "Living"):');
    if (!name || !name.trim()) return;
    const id = 'circuit_' + Math.random().toString(36).slice(2, 8);
    _state.plan.circuits = _state.plan.circuits || [];
    _state.plan.circuits.push({ id, name: name.trim(), receiver_command: 'thermostat' });
    renderCircuitList();
    renderScene();
    editCircuit(id);
}

function editCircuit(circuitId) {
    const c = (_state.plan.circuits || []).find(x => x.id === circuitId);
    if (!c) return;

    // Filter out receivers already assigned to *other* circuits
    const usedRecv = new Set((_state.plan.circuits || [])
        .filter(x => x.id !== circuitId && x.receiver_ieee)
        .map(x => x.receiver_ieee));

    const receivers = _availableDevices.receivers || [];
    const receiverOpts = ['<option value="">— No receiver —</option>']
        .concat(receivers
            .filter(r => !usedRecv.has(r.ieee) || r.ieee === c.receiver_ieee)
            .map(r => {
                const modeStr = r.system_mode ? ` [${r.system_mode}]` : '';
                return `<option value="${escapeAttr(r.ieee)}" ${c.receiver_ieee === r.ieee ? 'selected' : ''}>
                    ${escapeHtml(r.name)}${modeStr} (${escapeHtml(r.ieee.slice(-8))})
                </option>`;
            })
        ).join('');

    const propsDiv = document.getElementById('fpProps');
    propsDiv.innerHTML = `
      <div class="text-muted small text-uppercase mb-2">Circuit</div>
      <div class="mb-2"><label class="form-label small">Name</label>
        <input class="form-control form-control-sm" id="fpCircuitName" value="${escapeAttr(c.name)}"/></div>
      <div class="mb-2"><label class="form-label small">Boiler receiver</label>
        <select class="form-select form-select-sm" id="fpCircuitReceiver">${receiverOpts}</select>
        ${receivers.length === 0 ? '<div class="form-text small text-warning"><i class="fas fa-info-circle me-1"></i>No receivers found. Configure them in the Heating Controller, then reopen the floor plan editor.</div>' : ''}
      </div>
      <div class="mb-2"><label class="form-label small">Receiver command</label>
        <select class="form-select form-select-sm" id="fpCircuitCmd">
          <option value="thermostat" ${c.receiver_command === 'thermostat' ? 'selected' : ''}>thermostat</option>
          <option value="switch" ${c.receiver_command === 'switch' ? 'selected' : ''}>switch</option>
        </select></div>
      <div class="small text-muted mb-2">ID: <code>${escapeHtml(c.id)}</code></div>
      <div class="small text-info"><i class="fas fa-info-circle me-1"></i>Assign rooms to this circuit via the room properties panel.</div>`;

    const update = () => {
        c.name = document.getElementById('fpCircuitName').value.trim() || c.name;
        const recVal = document.getElementById('fpCircuitReceiver').value;
        if (recVal) c.receiver_ieee = recVal; else delete c.receiver_ieee;
        c.receiver_command = document.getElementById('fpCircuitCmd').value;
        renderCircuitList();
        renderScene();
    };
    propsDiv.querySelectorAll('input, select').forEach(el => el.addEventListener('change', update));
}

function deleteCircuit(circuitId) {
    const c = (_state.plan.circuits || []).find(x => x.id === circuitId);
    if (!c) return;
    const assigned = (_state.plan.levels || []).reduce((n, lvl) =>
        n + (lvl.rooms || []).filter(r => r.circuit_id === circuitId).length, 0);
    const msg = assigned > 0
        ? `Delete circuit "${c.name}"?\n\n${assigned} room(s) will become unassigned.`
        : `Delete circuit "${c.name}"?`;
    if (!confirm(msg)) return;
    _state.plan.circuits = (_state.plan.circuits || []).filter(x => x.id !== circuitId);
    // Unassign rooms across all levels
    (_state.plan.levels || []).forEach(lvl => {
        (lvl.rooms || []).forEach(r => { if (r.circuit_id === circuitId) delete r.circuit_id; });
    });
    renderCircuitList();
    renderScene();
}

// ─────────────────────────── feature creators ───────────────────────────


function finishRoom() {
    const lvl = currentLevel();
    const points = _state.drawBuffer.points;
    if (points.length < 3) {
        _state.drawBuffer = null; renderOverlay(); return;
    }
    const id = genId('room');
    const r = {
        id, name: `Room ${lvl.rooms.length + 1}`,
        polygon: points.map(p => [p.x, p.y]),
    };
    lvl.rooms.push(r);
    _state.drawBuffer = null;
    _state.selection = { kind: 'room', id };
    renderScene(); renderOverlay(); renderProps();
}

/**
 * Commit the in-progress wall chain. Each consecutive pair of vertices
 * becomes one wall with type `unknown` (the user classifies in the props
 * panel afterwards — supports the "draw layout first, classify later"
 * workflow). Segments shorter than 0.2 m are skipped.
 */
function finishWallChain() {
    const lvl = currentLevel();
    const pts = _state.drawBuffer?.points || [];
    if (pts.length < 2) {
        _state.drawBuffer = null; renderOverlay(); return;
    }
    const newIds = [];
    for (let i = 0; i < pts.length - 1; i++) {
        const a = pts[i], b = pts[i + 1];
        if (Math.hypot(b.x - a.x, b.y - a.y) < 0.2) continue;
        const id = genId('w');
        lvl.walls.push({ id, x1: a.x, y1: a.y, x2: b.x, y2: b.y, type: 'unknown' });
        newIds.push(id);
    }
    _state.drawBuffer = null;
    if (newIds.length > 0) {
        // Select the first new wall so the user can classify it immediately.
        _state.selection = { kind: 'wall', id: newIds[0] };
        toast('success', 'Walls added',
            `${newIds.length} wall${newIds.length === 1 ? '' : 's'} drawn — classify in the panel.`);
    }
    renderScene(); renderOverlay(); renderProps();
}

/** Cancel an in-progress chain without committing anything. */
function cancelDrawing() {
    if (!_state.drawBuffer) return;
    _state.drawBuffer = null;
    renderOverlay();
}

function addPointFeature(tool, m) {
    const lvl = currentLevel();
    // If the click lands inside a room polygon, auto-bind to that room as a
    // convenience. Otherwise leave room_id empty — the user binds via the
    // property panel. This mirrors the manual flow where a radiator can be
    // assigned to a room without a drawn floor-plan polygon yet.
    const room = (lvl.rooms || []).find(r => pointInPolygon(m, r.polygon));
    const room_id = room ? room.id : '';
    if (tool === 'radiator') {
        // Wall-snap behaviour: if the click is close to a wall (within 0.5 m
        // perpendicular distance), create a wall-mounted radiator. Otherwise
        // create a freestanding one at the click position. The user can
        // convert between modes later via the property panel.
        const RAD_WALL_SNAP_M = 0.5;
        const snap = nearestWallWithProjection(lvl, m, RAD_WALL_SNAP_M);
        const id = genId('rad');
        if (snap) {
            // Wall-mounted: store wall_id + offset_m. No x/y — render computes
            // the on-wall point from (wall, offset_m, room centroid for side).
            lvl.radiators.push({
                id, room_id,
                wall_id: snap.wall.id,
                offset_m: snap.offset_m,
                watts_at_dt50: 1000, length_m: 0.6, height_m: 0.6,
            });
        } else {
            // Freestanding: store x/y as before.
            lvl.radiators.push({
                id, room_id, x: m.x, y: m.y,
                watts_at_dt50: 1000, length_m: 0.6, height_m: 0.6,
            });
        }
        _state.selection = { kind: 'radiator', id };
    } else if (tool === 'sensor') {
        const id = genId('sens');
        lvl.sensors.push({
            id, room_id, ieee: '', kind: 'temp_sensor',
            x: m.x, y: m.y, height_m: 1.5, primary: false,
        });
        _state.selection = { kind: 'sensor', id };
    }
    if (!room) {
        toast('info', 'No room', 'Marker placed; pick a room in the panel on the right.');
    }
    renderScene(); renderProps();
}

/**
 * Find the wall whose projected distance from `p` is smallest, returning the
 * projection details if within `maxDist` metres. Returns null otherwise.
 * { wall, offset_m, projected: {x,y}, perpDist }
 */
function nearestWallWithProjection(lvl, p, maxDist) {
    let best = null, bestD = maxDist;
    for (const w of (lvl.walls || [])) {
        const proj = projectPointOntoSegment(p, w);
        const d = Math.hypot(p.x - proj.point.x, p.y - proj.point.y);
        if (d < bestD) {
            bestD = d;
            best = { wall: w, offset_m: proj.t, projected: proj.point, perpDist: d };
        }
    }
    return best;
}

function addContact(opening) {
    const lvl = currentLevel();
    const id = genId('con');
    lvl.contacts.push({
        id,
        opening_id: opening ? opening.id : '',
        ieee: '',
        debounce_open_seconds: 30,
        require_temp_drop_c: 0.5,
        max_close_minutes: 60,
        enabled: true,
    });
    _state.selection = { kind: 'contact', id };
    renderScene(); renderProps();
}

function pointInPolygon(p, poly) {
    if (!poly || poly.length < 3) return false;
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
        const [xi, yi] = poly[i], [xj, yj] = poly[j];
        const intersect = (yi > p.y) !== (yj > p.y) &&
                          p.x < ((xj - xi) * (p.y - yi)) / (yj - yi + 1e-12) + xi;
        if (intersect) inside = !inside;
    }
    return inside;
}

// ─────────────────────────── properties pane ───────────────────────────

function renderProps() {
    const el = document.getElementById('fpProps');
    if (!_state.selection) {
        el.innerHTML = renderLevelProps(currentLevel());
        bindLevelProps();
        return;
    }
    const { kind, id } = _state.selection;
    const lvl = currentLevel();
    let html = '';
    switch (kind) {
        case 'wall':     html = renderWallProps(lvl.walls.find(w => w.id === id)); break;
        case 'opening':  html = renderOpeningProps(lvl.openings.find(o => o.id === id)); break;
        case 'room':     html = renderRoomProps(lvl.rooms.find(r => r.id === id)); break;
        case 'radiator': html = renderRadiatorProps(lvl.radiators.find(r => r.id === id)); break;
        case 'sensor':   html = renderSensorProps(lvl.sensors.find(s => s.id === id)); break;
        case 'contact':  html = renderContactProps(lvl.contacts.find(c => c.id === id)); break;
    }
    el.innerHTML = html;
    bindPropsHandlers();
}

function renderLevelProps(lvl) {
    if (!lvl) return '<div class="text-muted small">No level selected.</div>';
    return `
      <div class="text-muted small text-uppercase mb-2">Level properties</div>
      <div class="mb-2"><label class="form-label small">Name</label>
        <input class="form-control form-control-sm" data-prop="level.name" value="${escapeAttr(lvl.name || '')}"/></div>
      <div class="row g-2 mb-2">
        <div class="col-6"><label class="form-label small">Index</label>
          <input type="number" class="form-control form-control-sm" data-prop="level.index" value="${lvl.index}"/></div>
        <div class="col-6"><label class="form-label small">Height (m)</label>
          <input type="number" step="0.05" class="form-control form-control-sm" data-prop="level.ceiling_height_m" value="${lvl.ceiling_height_m}"/></div>
      </div>
      <div class="mb-2"><label class="form-label small">Floor above ground (m)</label>
        <input type="number" step="0.1" class="form-control form-control-sm" data-prop="level.floor_above_ground_m" value="${lvl.floor_above_ground_m || 0}"/></div>
      ${_state.plan.levels.length > 1 ? `<button class="btn btn-sm btn-outline-danger w-100 mt-2" id="fpDeleteLevel"><i class="fas fa-trash me-1"></i>Delete level</button>` : ''}
      <hr/>
      <div class="text-muted small">
        <div><strong>Pan/zoom:</strong> Shift+drag (or middle-mouse) to pan, wheel to zoom.</div>
        <div class="mt-1"><strong>Walls:</strong> click to start a chain, click again to add each vertex. Press <kbd>Enter</kbd> or right-click or double-click to finish, <kbd>Esc</kbd> to cancel, <kbd>Backspace</kbd> to undo last vertex. Click on the first vertex to close back into it.</div>
        <div class="mt-1"><strong>Rooms:</strong> click to drop polygon vertices; close by clicking the first one (need 3+).</div>
        <div class="mt-1"><strong>Radiator/Sensor:</strong> place anywhere; pick the room from the panel. <strong>Contact:</strong> place near a window/door (or anywhere) and pick the opening from the panel.</div>
      </div>`;
}

function renderWallProps(w) {
    if (!w) return '';
    return `
      <div class="text-muted small text-uppercase mb-2">Wall</div>
      <div class="mb-2"><label class="form-label small">Type</label>
        <select class="form-select form-select-sm" data-prop="wall.type">
          ${['external','party','internal','unknown'].map(t =>
            `<option value="${t}" ${w.type === t ? 'selected' : ''}>${t}</option>`).join('')}
        </select></div>
      <div class="small text-muted mb-2">Length: ${Math.hypot(w.x2-w.x1, w.y2-w.y1).toFixed(2)} m</div>
      <button class="btn btn-sm btn-outline-danger w-100" data-action="delete-wall"><i class="fas fa-trash me-1"></i>Delete wall</button>`;
}

function renderOpeningProps(o) {
    if (!o) return '';
    const lvl = currentLevel();
    const roomOpts = lvl.rooms.map(r =>
        `<option value="${r.id}" ${o.room_id === r.id ? 'selected' : ''}>${escapeHtml(r.name || r.id)}</option>`).join('');
    return `
      <div class="text-muted small text-uppercase mb-2">${o.kind === 'window' ? 'Window' : 'Door'}</div>
      <div class="row g-2 mb-2">
        <div class="col-6"><label class="form-label small">Width (m)</label>
          <input type="number" step="0.05" class="form-control form-control-sm" data-prop="opening.width_m" value="${o.width_m}"/></div>
        <div class="col-6"><label class="form-label small">Height (m)</label>
          <input type="number" step="0.05" class="form-control form-control-sm" data-prop="opening.height_m" value="${o.height_m}"/></div>
      </div>
      <div class="mb-2"><label class="form-label small">Belongs to room</label>
        <select class="form-select form-select-sm" data-prop="opening.room_id">
          <option value="">— unassigned —</option>${roomOpts}
        </select></div>
      ${o.kind === 'window' ? `
      <div class="mb-2"><label class="form-label small">Glazing</label>
        <select class="form-select form-select-sm" data-prop="opening.glazing">
          ${['single','double','triple'].map(g => `<option value="${g}" ${o.glazing === g ? 'selected' : ''}>${g}</option>`).join('')}
        </select></div>` : `
      <div class="mb-2"><label class="form-label small">Door type</label>
        <select class="form-select form-select-sm" data-prop="opening.door_type">
          ${['external','internal'].map(d => `<option value="${d}" ${o.door_type === d ? 'selected' : ''}>${d}</option>`).join('')}
        </select></div>`}
      <button class="btn btn-sm btn-outline-danger w-100" data-action="delete-opening"><i class="fas fa-trash me-1"></i>Delete</button>`;
}

function renderFpScheduleSlots(slots) {
    if (!slots || !slots.length) return '<div class="text-muted small fst-italic mb-1">No slots — room uses operating hours defaults.</div>';
    const days = ['mon','tue','wed','thu','fri','sat','sun'];
    return slots.map((slot, i) => `
      <div class="fp-slot border rounded p-1 mb-1" data-slot-idx="${i}">
        <div class="d-flex flex-wrap gap-1 mb-1">
          ${days.map(d => `<label class="fp-day-label"><input type="checkbox" data-slot-day="${d}" data-slot-idx="${i}" ${(slot.days||[]).includes(d) ? 'checked' : ''}><span>${d.charAt(0).toUpperCase()}</span></label>`).join('')}
        </div>
        <div class="row g-1 align-items-center">
          <div class="col-auto"><input type="time" class="form-control form-control-sm fp-slot-start" data-slot-idx="${i}" value="${slot.start||'07:00'}"/></div>
          <div class="col-auto text-muted small">→</div>
          <div class="col-auto"><input type="time" class="form-control form-control-sm fp-slot-end" data-slot-idx="${i}" value="${slot.end||'22:00'}"/></div>
          <div class="col"><input type="number" step="0.5" min="5" max="32" class="form-control form-control-sm fp-slot-temp" data-slot-idx="${i}" value="${slot.temp??20}" placeholder="°C"/></div>
          <div class="col-auto"><button class="btn btn-sm btn-outline-danger fp-del-slot" data-slot-idx="${i}" title="Delete slot"><i class="fas fa-times"></i></button></div>
        </div>
      </div>`).join('');
}

function renderRoomProps(r) {
    if (!r) return '';
    const planCircuits = _state.plan.circuits || [];
    const circuitOpts = ['<option value="">— Unassigned —</option>']
        .concat(planCircuits.map(c =>
            `<option value="${c.id}" ${r.circuit_id === c.id ? 'selected' : ''}>${escapeHtml(c.name)}</option>`
        )).join('');
    const circuitSection = planCircuits.length > 0
        ? `<div class="mb-2"><label class="form-label small">Circuit</label>
             <select class="form-select form-select-sm" data-prop="room.circuit_id">${circuitOpts}</select></div>`
        : `<div class="mb-2 small text-muted fst-italic"><i class="fas fa-info-circle me-1"></i>Add a circuit in the sidebar to assign this room.</div>`;
    const oohAction = r.out_of_hours_action || 'setback';
    return `
      <div class="text-muted small text-uppercase mb-2">Room</div>
      <div class="mb-2"><label class="form-label small">Name</label>
        <input class="form-control form-control-sm" data-prop="room.name" value="${escapeAttr(r.name || '')}"/></div>
      ${circuitSection}
      <div class="mb-2"><label class="form-label small">Temperature targets</label>
        <div class="row g-1">
          <div class="col-4">
            <label class="form-label small text-muted mb-0">Target (°C)</label>
            <input type="number" step="0.5" min="5" max="32" class="form-control form-control-sm" data-prop="room.target_temp" value="${r.target_temp??20}"/>
          </div>
          <div class="col-4">
            <label class="form-label small text-muted mb-0">Setback (°C)</label>
            <input type="number" step="0.5" min="5" max="32" class="form-control form-control-sm" data-prop="room.night_setback" value="${r.night_setback??17}"/>
          </div>
          <div class="col-4">
            <label class="form-label small text-muted mb-0">Min (°C)</label>
            <input type="number" step="0.5" min="5" max="32" class="form-control form-control-sm" data-prop="room.min_temp" value="${r.min_temp??16}"/>
          </div>
        </div>
      </div>
      <div class="mb-2">
        <label class="form-label small">Schedule <span class="badge bg-secondary">${(r.schedule||[]).length}</span></label>
        <div class="fp-schedule-slots" data-room-id="${r.id}">${renderFpScheduleSlots(r.schedule||[])}</div>
        <button class="btn btn-sm btn-outline-secondary w-100 mt-1 fp-add-slot"><i class="fas fa-plus me-1"></i>Add time slot</button>
      </div>
      <div class="mb-2"><label class="form-label small">Outside scheduled hours</label>
        <div class="row g-1">
          <div class="col-7">
            <select class="form-select form-select-sm" data-prop="room.out_of_hours_action">
              <option value="setback" ${oohAction==='setback'?'selected':''}>Setback (lower target)</option>
              <option value="min_only" ${oohAction==='min_only'?'selected':''}>Frost-protect only</option>
              <option value="off" ${oohAction==='off'?'selected':''}>Off (no heat call)</option>
            </select>
          </div>
          <div class="col-5">
            <input type="number" step="0.5" min="-10" max="0" class="form-control form-control-sm"
                   placeholder="Offset °C" data-prop="room.night_setback_offset_c"
                   value="${r.night_setback_offset_c??-3}"/>
          </div>
        </div>
      </div>
      <div class="mb-2"><label class="form-label small">Floor type</label>
        <select class="form-select form-select-sm" data-prop="room.floor_type">
          <option value="">—</option>
          ${['solid','suspended','carpet_over_concrete','tile_over_concrete','wooden','carpet_over_wooden','unknown']
            .map(f => `<option value="${f}" ${r.floor_type === f ? 'selected' : ''}>${f}</option>`).join('')}
        </select></div>
      <div class="mb-2"><label class="form-label small">Ceiling type</label>
        <select class="form-select form-select-sm" data-prop="room.ceiling_type">
          <option value="">—</option>
          ${['insulated','uninsulated','flat_roof','unknown']
            .map(ct => `<option value="${ct}" ${r.ceiling_type === ct ? 'selected' : ''}>${ct}</option>`).join('')}
        </select></div>
      <button class="btn btn-sm btn-outline-danger w-100" data-action="delete-room"><i class="fas fa-trash me-1"></i>Delete room</button>`;
}

function renderRadiatorProps(r) {
    if (!r) return '';
    const lvl = currentLevel();
    const trvUsed = new Set(lvl.radiators.filter(x => x.id !== r.id && x.trv_ieee).map(x => x.trv_ieee));
    const trvOpts = ['<option value="">— No TRV (fixed valve) —</option>']
      .concat(_availableDevices.trvs.map(t =>
        `<option value="${t.ieee}" ${r.trv_ieee === t.ieee ? 'selected' : ''} ${trvUsed.has(t.ieee) ? 'disabled' : ''}>${escapeHtml(t.name || t.ieee)}${trvUsed.has(t.ieee) ? ' (used)' : ''}</option>`)).join('');
    const roomOpts = ['<option value="">— Select room —</option>']
      .concat(lvl.rooms.map(rm =>
        `<option value="${rm.id}" ${r.room_id === rm.id ? 'selected' : ''}>${escapeHtml(rm.name || rm.id)}</option>`)).join('');

    // Circuit context — derived from `room_id` via the caller-provided
    // circuits list. Read-only: circuit membership is set in the heating-
    // controller config, not here, because it's a behavioural binding (which
    // receiver fires for this room), not a geometric one. We just show it
    // so the user knows what controller behaviour this radiator triggers.
    const circuit = r.room_id ? findCircuitForRoom(r.room_id) : null;
    const circuitInfo = circuit
        ? `<div class="mb-2 small text-muted">
             <i class="fas fa-stream me-1"></i>Heats on circuit
             <strong>${escapeHtml(circuit.name || circuit.id)}</strong>
             ${circuit.receiver_ieee
               ? `<span class="text-muted" title="${escapeHtml(circuit.receiver_ieee)}"> · receiver <code>${escapeHtml(circuit.receiver_ieee.slice(-8))}</code></span>`
               : '<span class="text-warning ms-1">(no receiver assigned)</span>'}
           </div>`
        : (_availableCircuits.length && r.room_id
            ? `<div class="mb-2 small text-warning">
                 <i class="fas fa-exclamation-triangle me-1"></i>
                 Room <code>${escapeHtml(r.room_id)}</code> isn't in any circuit yet.
                 Add it in the heating controller settings.
               </div>`
            : '');

    // Mounting section: tells the user whether this radiator is wall-mounted
    // or freestanding, and offers a single button to convert. Wall ID is
    // shown read-only because it's set geometrically (drag/place); changing
    // it through a dropdown without geometric feedback is error-prone.
    const wall = r.wall_id ? lvl.walls.find(w => w.id === r.wall_id) : null;
    const wallLen = wall ? Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) : 0;
    const mountSection = wall
        ? `<div class="mb-2 p-2 border rounded bg-light">
             <div class="small text-muted text-uppercase mb-1">Mounting</div>
             <div class="small mb-1">
               <i class="fas fa-link me-1"></i>
               Wall-mounted on <code>${escapeHtml(r.wall_id)}</code>
               <span class="text-muted">(${wallLen.toFixed(2)} m long)</span>
             </div>
             <div class="row g-2">
               <div class="col-12">
                 <label class="form-label small">Offset from wall start (m)</label>
                 <input type="number" step="0.05" min="0" max="${wallLen.toFixed(2)}"
                        class="form-control form-control-sm"
                        data-prop="radiator.offset_m"
                        value="${(r.offset_m ?? 0).toFixed(2)}"/>
               </div>
             </div>
             <button class="btn btn-sm btn-outline-secondary w-100 mt-2" data-action="rad-to-freestanding">
               <i class="fas fa-arrows-alt me-1"></i>Convert to freestanding
             </button>
           </div>`
        : `<div class="mb-2 p-2 border rounded bg-light">
             <div class="small text-muted text-uppercase mb-1">Mounting</div>
             <div class="small mb-2">
               <i class="fas fa-arrows-alt me-1"></i>Freestanding at
               <code>(${(r.x ?? 0).toFixed(2)}, ${(r.y ?? 0).toFixed(2)})</code>
             </div>
             ${(lvl.walls || []).length > 0 ? `
               <button class="btn btn-sm btn-outline-secondary w-100" data-action="rad-snap-to-wall">
                 <i class="fas fa-link me-1"></i>Snap to nearest wall
               </button>` : `
               <div class="form-text small text-muted">No walls on this level to snap to.</div>`}
           </div>`;

    return `
      <div class="text-muted small text-uppercase mb-2">Radiator</div>
      <div class="mb-2"><label class="form-label small">Room</label>
        <select class="form-select form-select-sm" data-prop="radiator.room_id">${roomOpts}</select>
        ${lvl.rooms.length === 0 ? '<div class="form-text small text-warning">No rooms drawn on this level — draw one with the Room tool, then return here.</div>' : ''}
      </div>
      ${circuitInfo}
      ${mountSection}
      <div class="row g-2 mb-2">
        <div class="col-6"><label class="form-label small">Watts @ ΔT50</label>
          <input type="number" class="form-control form-control-sm" data-prop="radiator.watts_at_dt50" value="${r.watts_at_dt50 || 0}"/></div>
        <div class="col-6"><label class="form-label small">Length (m)</label>
          <input type="number" step="0.1" min="0.1" max="10" class="form-control form-control-sm" data-prop="radiator.length_m" value="${r.length_m || 0.6}"/></div>
      </div>
      <div class="row g-2 mb-2">
        <div class="col-6"><label class="form-label small" title="Physical panel height — used for sizing calcs, not shown on plan view">Panel height (m)</label>
          <input type="number" step="0.05" min="0.05" max="3" class="form-control form-control-sm" data-prop="radiator.height_m" value="${r.height_m || 0.6}"/></div>
        <div class="col-6"><label class="form-label small">Approx. surface area</label>
          <input type="text" class="form-control form-control-sm" readonly
                 value="${((r.length_m || 0.6) * (r.height_m || 0.6)).toFixed(2)} m²"/></div>
      </div>
      <div class="mb-2"><label class="form-label small">Type</label>
        <select class="form-select form-select-sm" data-prop="radiator.type">
          <option value="">—</option>
          ${['single_panel','double_panel_single_conv','double_panel_double_conv','triple_panel','column','towel_rail','underfloor']
            .map(t => `<option value="${t}" ${r.type === t ? 'selected' : ''}>${t}</option>`).join('')}
        </select></div>
      <div class="mb-2"><label class="form-label small">Bound TRV</label>
        <select class="form-select form-select-sm" data-prop="radiator.trv_ieee">${trvOpts}</select></div>
      <div class="mb-2 form-check form-switch small">
        <input type="checkbox" class="form-check-input" id="fpRadRefl" data-prop="radiator.reflective_panel" ${r.reflective_panel ? 'checked' : ''}/>
        <label class="form-check-label" for="fpRadRefl">Reflective panel behind</label></div>
      <button class="btn btn-sm btn-outline-danger w-100" data-action="delete-radiator"><i class="fas fa-trash me-1"></i>Delete</button>`;
}

function renderSensorProps(s) {
    if (!s) return '';
    const lvl = currentLevel();
    const roomOpts = ['<option value="">— Select room —</option>']
      .concat(lvl.rooms.map(rm =>
        `<option value="${rm.id}" ${s.room_id === rm.id ? 'selected' : ''}>${escapeHtml(rm.name || rm.id)}</option>`)).join('');
    const ieeeUsed = new Set(lvl.sensors.filter(x => x.id !== s.id && x.ieee).map(x => x.ieee));
    const sensorOpts = ['<option value="">— Select device —</option>']
      .concat(_availableDevices.sensors.map(d =>
        `<option value="${d.ieee}" ${s.ieee === d.ieee ? 'selected' : ''} ${ieeeUsed.has(d.ieee) ? 'disabled' : ''}>${escapeHtml(d.name || d.ieee)}${d.temperature != null ? ` (${Number(d.temperature).toFixed(1)}°C)` : ''}</option>`)).join('');
    return `
      <div class="text-muted small text-uppercase mb-2">Temperature sensor</div>
      <div class="mb-2"><label class="form-label small">Room</label>
        <select class="form-select form-select-sm" data-prop="sensor.room_id">${roomOpts}</select>
        ${lvl.rooms.length === 0 ? '<div class="form-text small text-warning">No rooms drawn on this level — draw one with the Room tool, then return here.</div>' : ''}
      </div>
      <div class="mb-2"><label class="form-label small">Device</label>
        <select class="form-select form-select-sm" data-prop="sensor.ieee">${sensorOpts}</select></div>
      <div class="mb-2"><label class="form-label small">Kind</label>
        <select class="form-select form-select-sm" data-prop="sensor.kind">
          ${['temp_sensor','thermostat','room_stat'].map(k => `<option value="${k}" ${s.kind === k ? 'selected' : ''}>${k}</option>`).join('')}
        </select></div>
      <div class="mb-2"><label class="form-label small">Mounting height (m)</label>
        <input type="number" step="0.05" min="0" max="5" class="form-control form-control-sm" data-prop="sensor.height_m" value="${s.height_m ?? 1.5}"/>
        <div class="form-text small">Used to correct for warm-air stratification when reading the room.</div></div>
      <div class="mb-2 form-check form-switch small">
        <input type="checkbox" class="form-check-input" id="fpSensPrim" data-prop="sensor.primary" ${s.primary ? 'checked' : ''}/>
        <label class="form-check-label" for="fpSensPrim">Primary sensor for this room</label></div>
      <button class="btn btn-sm btn-outline-danger w-100" data-action="delete-sensor"><i class="fas fa-trash me-1"></i>Delete</button>`;
}

function renderContactProps(c) {
    if (!c) return '';
    const lvl = currentLevel();
    const ieeeUsed = new Set(lvl.contacts.filter(x => x.id !== c.id && x.ieee).map(x => x.ieee));
    const contactOpts = ['<option value="">— Select device —</option>']
      .concat(_availableDevices.contacts.map(d =>
        `<option value="${d.ieee}" ${c.ieee === d.ieee ? 'selected' : ''} ${ieeeUsed.has(d.ieee) ? 'disabled' : ''}>${escapeHtml(d.name || d.ieee)}</option>`)).join('');

    // Build the opening picker. Each entry shows kind + room (if known) so
    // similarly-named openings can be told apart.
    const openingUsed = new Set(lvl.contacts.filter(x => x.id !== c.id && x.opening_id).map(x => x.opening_id));
    const roomById = new Map((lvl.rooms || []).map(r => [r.id, r]));
    const openingOpts = ['<option value="">— Select opening —</option>']
      .concat((lvl.openings || []).map(o => {
        const room = o.room_id ? roomById.get(o.room_id) : null;
        const roomLabel = room ? ` · ${escapeHtml(room.name || room.id)}` : '';
        const used = openingUsed.has(o.id) ? ' (used)' : '';
        const w = (o.width_m || 0).toFixed(2);
        return `<option value="${o.id}" ${c.opening_id === o.id ? 'selected' : ''} ${openingUsed.has(o.id) ? 'disabled' : ''}>${o.kind} (${w} m)${roomLabel}${used}</option>`;
      })).join('');

    return `
      <div class="text-muted small text-uppercase mb-2">Contact sensor</div>
      <div class="mb-2"><label class="form-label small">Bound to opening</label>
        <select class="form-select form-select-sm" data-prop="contact.opening_id">${openingOpts}</select>
        ${(lvl.openings || []).length === 0 ? '<div class="form-text small text-warning">No windows or doors on this level yet — draw some first.</div>' : ''}
      </div>
      <div class="mb-2"><label class="form-label small">Device</label>
        <select class="form-select form-select-sm" data-prop="contact.ieee">${contactOpts}</select></div>
      <div class="row g-2 mb-2">
        <div class="col-6"><label class="form-label small">Debounce (s)</label>
          <input type="number" class="form-control form-control-sm" data-prop="contact.debounce_open_seconds" value="${c.debounce_open_seconds}"/></div>
        <div class="col-6"><label class="form-label small">Drop (°C)</label>
          <input type="number" step="0.1" class="form-control form-control-sm" data-prop="contact.require_temp_drop_c" value="${c.require_temp_drop_c}"/></div>
      </div>
      <div class="mb-2"><label class="form-label small">Max close-suppress (min)</label>
        <input type="number" class="form-control form-control-sm" data-prop="contact.max_close_minutes" value="${c.max_close_minutes}"/></div>
      <div class="mb-2 form-check form-switch small">
        <input type="checkbox" class="form-check-input" id="fpConEn" data-prop="contact.enabled" ${c.enabled ? 'checked' : ''}/>
        <label class="form-check-label" for="fpConEn">Enabled</label></div>
      <button class="btn btn-sm btn-outline-danger w-100" data-action="delete-contact"><i class="fas fa-trash me-1"></i>Delete</button>`;
}

function bindLevelProps() {
    const root = document.getElementById('fpProps');
    root.querySelectorAll('[data-prop]').forEach(el => {
        el.addEventListener('change', () => {
            const lvl = currentLevel();
            const key = el.dataset.prop.split('.')[1];
            const val = el.type === 'number' ? parseFloat(el.value) : el.value;
            if (key === 'index') lvl.index = parseInt(el.value, 10) || 0;
            else lvl[key] = val;
            renderLevelList(); renderScene();
        });
    });
    document.getElementById('fpDeleteLevel')?.addEventListener('click', () => {
        if (!confirm(`Delete level "${currentLevel().name}"? This removes everything on it.`)) return;
        _state.plan.levels = _state.plan.levels.filter(l => l.id !== _state.currentLevelId);
        _state.currentLevelId = _state.plan.levels[0].id;
        renderAll();
    });
}

function bindPropsHandlers() {
    const lvl = currentLevel();
    const sel = _state.selection;
    if (!sel) return;
    const root = document.getElementById('fpProps');

    root.querySelectorAll('[data-prop]').forEach(el => {
        el.addEventListener('change', () => {
            const [scope, key] = el.dataset.prop.split('.');
            let target;
            if (scope === 'wall')      target = lvl.walls.find(w => w.id === sel.id);
            else if (scope === 'opening')  target = lvl.openings.find(o => o.id === sel.id);
            else if (scope === 'room')     target = lvl.rooms.find(r => r.id === sel.id);
            else if (scope === 'radiator') target = lvl.radiators.find(r => r.id === sel.id);
            else if (scope === 'sensor')   target = lvl.sensors.find(s => s.id === sel.id);
            else if (scope === 'contact')  target = lvl.contacts.find(c => c.id === sel.id);
            if (!target) return;

            let val;
            if (el.type === 'checkbox') val = el.checked;
            else if (el.type === 'number') val = parseFloat(el.value);
            else val = el.value;
            if (val === '' || (typeof val === 'number' && Number.isNaN(val))) val = undefined;

            if (val === undefined) delete target[key]; else target[key] = val;

            // Primary sensor: enforce one-per-room
            if (scope === 'sensor' && key === 'primary' && val) {
                lvl.sensors.forEach(s2 => { if (s2.id !== sel.id && s2.room_id === target.room_id) s2.primary = false; });
            }
            // Circuit assignment: refresh sidebar room count
            if (scope === 'room' && key === 'circuit_id') {
                renderCircuitList();
            }
            renderScene();
        });
    });

    // Schedule slot handlers (room props panel)
    const fpAddSlot = root.querySelector('.fp-add-slot');
    if (fpAddSlot) {
        fpAddSlot.addEventListener('click', () => {
            const lvl = currentLevel();
            const room = lvl.rooms.find(r => r.id === sel.id);
            if (!room) return;
            if (!Array.isArray(room.schedule)) room.schedule = [];
            room.schedule.push({ days: ['mon','tue','wed','thu','fri'], start: '07:00', end: '22:00', temp: room.target_temp || 20 });
            renderProps();
        });
    }
    root.querySelectorAll('.fp-del-slot').forEach(btn => {
        btn.addEventListener('click', () => {
            const lvl = currentLevel();
            const room = lvl.rooms.find(r => r.id === sel.id);
            if (!room || !Array.isArray(room.schedule)) return;
            const idx = parseInt(btn.dataset.slotIdx, 10);
            room.schedule.splice(idx, 1);
            renderProps();
        });
    });
    root.querySelectorAll('[data-slot-day]').forEach(cb => {
        cb.addEventListener('change', () => {
            const lvl = currentLevel();
            const room = lvl.rooms.find(r => r.id === sel.id);
            if (!room || !Array.isArray(room.schedule)) return;
            const idx = parseInt(cb.dataset.slotIdx, 10);
            const slot = room.schedule[idx]; if (!slot) return;
            const day = cb.dataset.slotDay;
            if (cb.checked) { if (!slot.days.includes(day)) slot.days.push(day); }
            else { slot.days = slot.days.filter(d => d !== day); }
        });
    });
    root.querySelectorAll('.fp-slot-start, .fp-slot-end, .fp-slot-temp').forEach(inp => {
        inp.addEventListener('change', () => {
            const lvl = currentLevel();
            const room = lvl.rooms.find(r => r.id === sel.id);
            if (!room || !Array.isArray(room.schedule)) return;
            const idx = parseInt(inp.dataset.slotIdx, 10);
            const slot = room.schedule[idx]; if (!slot) return;
            if (inp.classList.contains('fp-slot-start')) slot.start = inp.value;
            else if (inp.classList.contains('fp-slot-end')) slot.end = inp.value;
            else slot.temp = parseFloat(inp.value) || slot.temp;
        });
    });

    root.querySelector('[data-action="delete-wall"]')?.addEventListener('click', () => deleteSelected('walls'));
    root.querySelector('[data-action="delete-opening"]')?.addEventListener('click', () => deleteSelected('openings'));
    root.querySelector('[data-action="delete-room"]')?.addEventListener('click', () => deleteSelected('rooms'));
    root.querySelector('[data-action="delete-radiator"]')?.addEventListener('click', () => deleteSelected('radiators'));
    root.querySelector('[data-action="delete-sensor"]')?.addEventListener('click', () => deleteSelected('sensors'));
    root.querySelector('[data-action="delete-contact"]')?.addEventListener('click', () => deleteSelected('contacts'));

    // Radiator mounting-mode conversions
    root.querySelector('[data-action="rad-to-freestanding"]')?.addEventListener('click', () => {
        const lvl = currentLevel();
        const r = lvl.radiators.find(x => x.id === sel.id);
        if (!r || !r.wall_id) return;
        const wall = lvl.walls.find(w => w.id === r.wall_id);
        // Compute the radiator's current ON-WALL centre point and write it
        // as x/y so the freestanding render lands in the same place.
        if (wall) {
            const wlen = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1) || 1;
            const ux = (wall.x2 - wall.x1) / wlen, uy = (wall.y2 - wall.y1) / wlen;
            const len = r.length_m || 0.6;
            const t = (r.offset_m ?? wlen / 2) + len / 2;
            r.x = wall.x1 + ux * t;
            r.y = wall.y1 + uy * t;
        } else if (r.x == null || r.y == null) {
            r.x = 0; r.y = 0;
        }
        delete r.wall_id;
        delete r.offset_m;
        renderScene(); renderProps();
    });
    root.querySelector('[data-action="rad-snap-to-wall"]')?.addEventListener('click', () => {
        const lvl = currentLevel();
        const r = lvl.radiators.find(x => x.id === sel.id);
        if (!r) return;
        const p = { x: r.x ?? 0, y: r.y ?? 0 };
        const snap = nearestWallWithProjection(lvl, p, 100);  // any distance — this is explicit user intent
        if (!snap) {
            toast('warn', 'No walls', 'No walls available on this level.');
            return;
        }
        r.wall_id = snap.wall.id;
        r.offset_m = snap.offset_m;
        delete r.x; delete r.y;
        renderScene(); renderProps();
    });
}

function deleteSelected(arrKey) {
    const lvl = currentLevel();
    const sel = _state.selection;
    if (!sel) return;
    lvl[arrKey] = lvl[arrKey].filter(x => x.id !== sel.id);
    // Cascade: removing a wall removes its openings; removing an opening removes its contacts
    if (arrKey === 'walls') {
        const removedOpenings = lvl.openings.filter(o => !lvl.walls.find(w => w.id === o.wall_id)).map(o => o.id);
        lvl.openings = lvl.openings.filter(o => !removedOpenings.includes(o.id));
        lvl.contacts = lvl.contacts.filter(c => !removedOpenings.includes(c.opening_id));
    } else if (arrKey === 'openings') {
        lvl.contacts = lvl.contacts.filter(c => c.opening_id !== sel.id);
    } else if (arrKey === 'rooms') {
        lvl.radiators = lvl.radiators.filter(r => r.room_id !== sel.id);
        lvl.sensors = lvl.sensors.filter(s => s.room_id !== sel.id);
    }
    _state.selection = null;
    renderScene(); renderProps();
}

// ──────────────────────────── levels ────────────────────────────

function addLevel() {
    const idx = (_state.plan.levels.reduce((m, l) => Math.max(m, l.index), -1)) + 1;
    const l = {
        id: genId('level'),
        name: `Level ${idx}`,
        index: idx,
        ceiling_height_m: DEFAULT_LEVEL_HEIGHT,
        floor_above_ground_m: idx * DEFAULT_LEVEL_HEIGHT,
        walls: [], openings: [], rooms: [],
        radiators: [], sensors: [], contacts: [],
    };
    _state.plan.levels.push(l);
    _state.currentLevelId = l.id;
    renderAll();
}

// ──────────────────────────── zoom/pan ────────────────────────────

function zoomBy(factor, cx, cy) {
    const oldZ = _state.zoom;
    const newZ = Math.max(10, Math.min(400, oldZ * factor));
    if (cx != null && cy != null) {
        const wrap = document.getElementById('fpCanvasWrap').getBoundingClientRect();
        const px = cx - wrap.left, py = cy - wrap.top;
        const before = { x: (px - _state.pan.x) / oldZ, y: (py - _state.pan.y) / oldZ };
        _state.zoom = newZ;
        _state.pan.x = px - before.x * newZ;
        _state.pan.y = py - before.y * newZ;
    } else {
        _state.zoom = newZ;
    }
    renderScene(); renderOverlay();
}

function zoomFit() {
    const lvl = currentLevel();
    const wrap = document.getElementById('fpCanvasWrap');
    if (!wrap) return;
    const rect = wrap.getBoundingClientRect();
    if (rect.width < 50 || rect.height < 50) return;

    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    const consider = p => {
        if (p.x < minX) minX = p.x; if (p.x > maxX) maxX = p.x;
        if (p.y < minY) minY = p.y; if (p.y > maxY) maxY = p.y;
    };
    for (const w of lvl.walls) { consider({x: w.x1, y: w.y1}); consider({x: w.x2, y: w.y2}); }
    for (const r of lvl.rooms) for (const p of r.polygon) consider({x: p[0], y: p[1]});

    if (!Number.isFinite(minX)) {
        // Empty level: centre on origin
        _state.zoom = PIXELS_PER_METRE_DEFAULT;
        _state.pan = { x: rect.width / 2, y: rect.height / 2 };
        renderScene(); renderOverlay();
        return;
    }
    const w = maxX - minX, h = maxY - minY;
    const pad = 1.0;
    const z = Math.min(rect.width / (w + pad * 2), rect.height / (h + pad * 2));
    _state.zoom = Math.max(20, Math.min(300, z));
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const svgC = modelToSvg({ x: cx, y: cy });
    _state.pan = {
        x: rect.width / 2 - svgC.x * _state.zoom,
        y: rect.height / 2 - svgC.y * _state.zoom,
    };
    renderScene(); renderOverlay();
}

// ──────────────────────────── sun overlay ────────────────────────────

async function loadSunData() {
    try {
        const r = await fetch('/api/sun/day?step_minutes=20').then(r => r.json());
        if (r && r.success) _state.sunData = r.data;
    } catch (e) { /* swallow */ }
}

// ──────────────────────────── save ────────────────────────────

async function save() {
    const btn = document.getElementById('fpSave');
    const status = document.getElementById('fpSaveStatus');
    btn.disabled = true;
    status.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Saving plan…`;
    try {
        // Strip drawBuffer / view-state — not part of the plan
        const payload = {
            version: FP_VERSION,
            north_offset_deg: _state.plan.north_offset_deg,
            scale_pixels_per_metre: _state.plan.scale_pixels_per_metre,
            // Plan-level circuit definitions — MUST be sent so the backend
            // can persist them and use plan-native projection mode.
            circuits: (_state.plan.circuits || []).map(c => ({
                id: c.id,
                name: c.name,
                ...(c.receiver_ieee   ? { receiver_ieee:     c.receiver_ieee }   : {}),
                ...(c.receiver_command? { receiver_command:  c.receiver_command } : {}),
                ...(c.receiver_endpoint != null ? { receiver_endpoint: c.receiver_endpoint } : {}),
            })),
            levels: _state.plan.levels.map(l => {
                const out = {
                    id: l.id, name: l.name, index: l.index,
                    ceiling_height_m: l.ceiling_height_m,
                    floor_above_ground_m: l.floor_above_ground_m,
                    walls: l.walls, openings: l.openings, rooms: l.rooms,
                    radiators: l.radiators, sensors: l.sensors, contacts: l.contacts,
                };
                if (l.background?.present) {
                    // Strip view-only fields (cache-buster) before sending
                    const { _cb, ...bg } = l.background;
                    out.background = bg;
                }
                return out;
            }),
        };
        const r = await fetch('/api/heating/floor-plan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        }).then(r => r.json());
        if (!r.success) throw new Error(r.error || 'Save failed');
        status.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>Saved.</span>`;
        if (typeof _onSaveCallback === 'function') {
            try { await _onSaveCallback(r); } catch (e) { console.error(e); }
        }
        setTimeout(() => {
            const modalEl = document.getElementById('floorPlanModal');
            if (modalEl) bootstrap.Modal.getOrCreateInstance(modalEl).hide();
        }, 600);
    } catch (e) {
        status.innerHTML = `<span class="text-danger"><i class="fas fa-times-circle me-1"></i>${escapeHtml(e.message)}</span>`;
    } finally {
        btn.disabled = false;
    }
}

/**
 * Switch the heating controller back to manual configuration. Posts the
 * mode change directly — the backend strips floor_plan_ref from rooms so
 * the manual UI is fully editable. The saved plan stays on disk as a
 * backup; the user can switch back later and the plan will be re-applied.
 */
async function switchToManual() {
    const confirmed = confirm(
        'Switch the heating controller to manual configuration?\n\n'
        + 'Your floor plan stays saved as a backup. The rooms will become '
        + 'freely editable in the manual UI. You can switch back later — '
        + 'the plan will be re-applied to the rooms.'
    );
    if (!confirmed) return;

    const status = document.getElementById('fpSaveStatus');
    status.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Switching…`;
    try {
        const r = await fetch('/api/heating/controller/config-mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: 'manual' }),
        }).then(r => r.json());
        if (!r.success) throw new Error(r.error || 'Switch failed');

        status.innerHTML = `<span class="text-success">Switched to manual.</span>`;
        // Trigger the editor's onSave callback so the parent page refreshes.
        if (typeof _onSaveCallback === 'function') {
            try { await _onSaveCallback(r); } catch (e) { console.error(e); }
        }
        setTimeout(() => {
            const modalEl = document.getElementById('floorPlanModal');
            if (modalEl) bootstrap.Modal.getOrCreateInstance(modalEl).hide();
        }, 600);
    } catch (e) {
        status.innerHTML = `<span class="text-danger"><i class="fas fa-times-circle me-1"></i>${escapeHtml(e.message)}</span>`;
    }
}

// ──────────────────────────── background image ────────────────────────────

const PDF_JS_URL = '/static/js/vendor/pdf.min.js';
const PDF_WORKER_URL = '/static/js/vendor/pdf.worker.min.js';

async function ensurePdfJs() {
    if (window.pdfjsLib) return window.pdfjsLib;
    await new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = PDF_JS_URL;
        s.onload = resolve;
        s.onerror = () => reject(new Error('pdf.js not found at ' + PDF_JS_URL));
        document.head.appendChild(s);
    });
    if (!window.pdfjsLib) throw new Error('pdf.js loaded but window.pdfjsLib missing');
    window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDF_WORKER_URL;
    return window.pdfjsLib;
}

async function pdfFirstPageToPngBlob(file, scale = 2.0) {
    const lib = await ensurePdfJs();
    const buf = await file.arrayBuffer();
    const pdf = await lib.getDocument({ data: buf }).promise;
    const page = await pdf.getPage(1);
    const viewport = page.getViewport({ scale });
    const canvas = document.createElement('canvas');
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
    return await new Promise(res => canvas.toBlob(res, 'image/png'));
}

async function onImageFileChosen(e) {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;

    const lvl = currentLevel();
    const status = document.getElementById('fpSaveStatus');
    status.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Importing image…`;

    try {
        let uploadBlob = file;
        let contentType = file.type || '';
        if (file.type === 'application/pdf' || /\.pdf$/i.test(file.name)) {
            uploadBlob = await pdfFirstPageToPngBlob(file, 2.0);
            contentType = 'image/png';
        } else if (!['image/png', 'image/jpeg'].includes(file.type)) {
            throw new Error('Unsupported file. Use PNG, JPEG, or PDF.');
        }

        // Read dimensions client-side so we can compute pixels-per-metre
        const dims = await readImageDimensions(uploadBlob);

        // POST to server
        const fd = new FormData();
        fd.append('file', uploadBlob, contentType === 'image/png' ? 'plan.png' : 'plan.jpg');
        const r = await fetch(`/api/heating/floor-plan/image/${encodeURIComponent(lvl.id)}`, {
            method: 'POST', body: fd,
        }).then(r => r.json());
        if (!r.success) throw new Error(r.error || 'upload failed');

        // Default calibration: 1 metre = 50 px (placeholder until user calibrates)
        const ppm = lvl.background?.pixels_per_metre || 50;
        lvl.background = {
            present: true,
            pixels_per_metre: ppm,
            image_width_px: dims.width,
            image_height_px: dims.height,
            origin_x_m: 0,
            origin_y_m: 0,
            rotation_deg: 0,
            opacity: lvl.background?.opacity ?? 0.5,
            content_type: contentType,
            _cb: Date.now(),    // cache-buster for SVG <image>
        };

        document.getElementById('fpRemoveImage').disabled = false;
        document.getElementById('fpImageOpacity').value = lvl.background.opacity;
        status.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>Image imported. Use the <strong>Calibrate</strong> tool to set scale.</span>`;
        renderScene();
        zoomFit();
    } catch (err) {
        status.innerHTML = `<span class="text-danger"><i class="fas fa-times-circle me-1"></i>${escapeHtml(err.message)}</span>`;
    }
}

function readImageDimensions(blob) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        const url = URL.createObjectURL(blob);
        img.onload = () => {
            URL.revokeObjectURL(url);
            resolve({ width: img.naturalWidth, height: img.naturalHeight });
        };
        img.onerror = () => {
            URL.revokeObjectURL(url);
            reject(new Error('could not read image dimensions'));
        };
        img.src = url;
    });
}

async function removeBackgroundImage() {
    const lvl = currentLevel();
    if (!lvl.background?.present) return;
    if (!confirm('Remove background image for this level?')) return;
    try {
        await fetch(`/api/heating/floor-plan/image/${encodeURIComponent(lvl.id)}`, { method: 'DELETE' });
    } catch { /* swallow */ }
    delete lvl.background;
    document.getElementById('fpRemoveImage').disabled = true;
    renderScene();
}

function promptCalibrationDistance(p1, p2, drawnDist) {
    const lvl = currentLevel();
    if (!lvl.background?.present) {
        toast('warn', 'No image', 'Import a background image before calibrating.');
        return;
    }
    if (drawnDist < 1e-6) {
        toast('warn', 'Too short', 'Pick two distinct points.');
        return;
    }
    const ans = prompt(
        `You drew a line measuring ${drawnDist.toFixed(2)} m at the current scale.\n\n` +
        `What is its real-world length (metres)?`,
        drawnDist.toFixed(2),
    );
    if (ans == null) return;
    const realM = parseFloat(ans);
    if (!Number.isFinite(realM) || realM <= 0) {
        toast('warn', 'Invalid', 'Enter a positive number of metres.');
        return;
    }

    // Re-scale the image so that drawnDist (currently in current model metres)
    // equals realM. New ppm = old_ppm * (drawnDist / realM).
    const oldPpm = lvl.background.pixels_per_metre;
    if (!Number.isFinite(oldPpm) || oldPpm <= 0) {
        // Stale/invalid state — recover with a sane default rather than
        // propagating NaN/0 through the calibration math.
        lvl.background.pixels_per_metre = 50.0;
        toast('warn', 'Recovered scale',
            'Image had no valid scale; reset to 50 px/m. Calibrating now…');
        renderScene();
        // Re-enter calibration with the recovered ppm
        promptCalibrationDistance(p1, p2, drawnDist);
        return;
    }
    const newPpm = oldPpm * (drawnDist / realM);
    if (!Number.isFinite(newPpm) || newPpm <= 0) {
        toast('warn', 'Invalid', 'Calibration produced an invalid scale.');
        return;
    }

    // Re-anchor: keep p1 at the same MODEL coordinate after rescale.
    // Image origin in model space shifts so that the pixel under p1 stays at p1.
    const bg = lvl.background;
    const wOldM = bg.image_width_px / oldPpm;
    const hOldM = bg.image_height_px / oldPpm;
    // Pixel position of p1 within the current image (top-left of image is at
    // (origin_x, origin_y + h) in model coords, with +y up):
    const u = (p1.x - bg.origin_x_m) / wOldM;       // 0..1 across width
    const v = (bg.origin_y_m + hOldM - p1.y) / hOldM; // 0..1 down from top
    bg.pixels_per_metre = newPpm;
    const wNewM = bg.image_width_px / newPpm;
    const hNewM = bg.image_height_px / newPpm;
    bg.origin_x_m = p1.x - u * wNewM;
    bg.origin_y_m = p1.y - (hNewM - v * hNewM);

    toast('success', 'Calibrated', `${realM.toFixed(2)} m = ${drawnDist.toFixed(3)} drawn — scale ${newPpm.toFixed(1)} px/m.`);
    renderScene();
    renderOverlay();
}

// ──────────────────────────── utils ────────────────────────────

function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }
function toast(level, title, body) {
    if (window.toast?.[level]) window.toast[level](title, body);
    else if (window.showToast) window.showToast(level, title, body);
    else console.log(`[${level}] ${title}: ${body}`);
}