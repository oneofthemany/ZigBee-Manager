/**
 * Automation Tab — State Machine with Recursive Action Sequences
 *
 * Step types: command, delay, wait_for, condition (gate),
 *             if_then_else (branching), parallel (concurrent)
 *
 * Prerequisites support NOT (negate) flag.
 * Inline conditions in if_then_else support AND/OR logic.
 */

import { state } from '../state.js';

let cachedActuators = [], cachedAttributes = [], cachedAllDevices = [];
let currentSourceIeee = null, editingRuleId = null;
let condRows = [], condIdC = 0, prereqRows = [], prereqIdC = 0;
// Step trees stored in memory — rendered to DOM
let thenTree = [], elseTree = [], stepIdC = 0;

const OP = {'eq':'=','neq':'≠','gt':'>','lt':'<','gte':'>=','lte':'<=','in':'∈','nin':'∉'};

const OPT = {'eq':'equals','neq':'not equal','gt':'greater than','lt':'less than','gte':'greater or equal','lte':'less or equal','in':'in list','nin':'not in list'};

function _opOpts(sel) {
    return Object.entries(OP).map(([k,v]) =>
        `<option value="${k}" ${k===sel?'selected':''}>${v} ${OPT[k]}</option>`
    ).join('');
}
const SICON = {command:'fa-bolt',delay:'fa-clock',wait_for:'fa-hourglass-half',condition:'fa-filter',if_then_else:'fa-code-branch',parallel:'fa-columns'};

const SLBL = {command:'Command',delay:'Delay',wait_for:'Wait For',condition:'Gate',if_then_else:'If / Then / Else',parallel:'Parallel'};

// ── Group optgroup builder ──
function _devOpts(list, selectedIeee, extraAttrs='') {
    const devs = list.filter(d => !d._is_group);
    const grps = list.filter(d => d._is_group);
    let h = devs.map(d =>
        `<option value="${d.ieee}" ${extraAttrs ? extraAttrs.replace('$IEEE', d.ieee).replace('$SEL', d.ieee===selectedIeee?'selected':'') : (d.ieee===selectedIeee?'selected':'')}>${d.friendly_name}</option>`
    ).join('');
    if (grps.length) {
        h += `<optgroup label="── Groups ──">` + grps.map(d =>
            `<option value="${d.ieee}" ${extraAttrs ? extraAttrs.replace('$IEEE', d.ieee).replace('$SEL', d.ieee===selectedIeee?'selected':'') : (d.ieee===selectedIeee?'selected':'')}>${d.friendly_name}</option>`
        ).join('') + '</optgroup>';
    }
    return h;
}

function _uid() { return stepIdC++; }

// ============================================================================
// RENDER
// ============================================================================

export function renderAutomationTab(device) {
    currentSourceIeee = device.ieee;
    return `<div id="automation-tab-content">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <span class="text-muted small">State-machine triggers with action sequences.</span>
            <div>
                <button class="btn btn-sm btn-outline-secondary me-1" onclick="window._aTrace()"><i class="fas fa-search"></i> Trace</button>
                <button class="btn btn-sm btn-success" onclick="window._aShowForm()"><i class="fas fa-plus"></i> Add Rule</button>
            </div>
        </div>
        <div id="a-form" class="card mb-3" style="display:none"></div>
        <div id="a-trace" class="card mb-3" style="display:none">
            <div class="card-header bg-dark text-white d-flex justify-content-between py-1">
                <strong><i class="fas fa-search"></i> Trace</strong>
                <div class="d-flex gap-2 align-items-center">
                    <select class="form-select form-select-sm bg-dark text-white border-secondary" id="tf" style="width:auto;max-width:220px;font-size:.75rem" onchange="window._aRefTrace()"><option value="">All</option></select>
                    <button class="btn btn-sm btn-outline-light" onclick="window._aRefTrace()"><i class="fas fa-sync-alt"></i></button>
                    <button class="btn btn-sm btn-outline-light" onclick="document.getElementById('a-trace').style.display='none'"><i class="fas fa-times"></i></button>
                </div>
            </div>
            <div class="card-body p-0" style="max-height:400px;overflow-y:auto"><div id="a-trace-c" class="font-monospace small p-2"></div></div>
        </div>
        <div id="a-rules"><div class="text-center text-muted py-3"><i class="fas fa-spinner fa-spin"></i></div></div>
    </div>`;
}

export async function initAutomationTab(ieee) {
    currentSourceIeee = ieee;
    try {
        const [rR,aR,actR,dR] = await Promise.all([
            fetch(`/api/automations?source_ieee=${encodeURIComponent(ieee)}`),
            fetch(`/api/automations/device/${encodeURIComponent(ieee)}/attributes`),
            fetch('/api/automations/actuators'), fetch('/api/automations/devices'),
        ]);
        cachedAttributes = await aR.json(); cachedActuators = await actR.json();
        cachedAllDevices = await dR.json(); _renderRules(await rR.json());
    } catch(e) { const el=document.getElementById('a-rules'); if(el)el.innerHTML=`<div class="alert alert-danger">${e.message}</div>`; }
}

// ============================================================================
// RULES LIST
// ============================================================================

function _renderRules(rules) {
    const el = document.getElementById('a-rules'); if (!el) return;
    if (!rules?.length) { el.innerHTML = `<div class="text-center text-muted py-4"><i class="fas fa-robot fa-2x mb-2 d-block opacity-50"></i>No rules.</div>`; return; }
    let h = '';
    rules.forEach(rule => {
        const en = rule.enabled !== false;
        const nm = rule.name ? `<strong>${rule.name}</strong> ` : '';
        const st = rule._state||'unknown';
        const stB = st==='matched'?'<span class="badge bg-success ms-1">matched</span>':st==='unmatched'?'<span class="badge bg-secondary ms-1">unmatched</span>':'<span class="badge bg-dark ms-1">init</span>';
        const run = rule._running?'<span class="badge bg-warning text-dark ms-1">⏳</span>':'';

        let cH = '';
        (rule.conditions||[]).forEach((c,i) => {
            const p = i===0?'<strong class="text-primary">IF</strong>':'<strong class="text-warning">AND</strong>';
            let cDesc;
            if (c.type === 'time_window') {
                const DAY_NAMES = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
                const dayStr = (!c.days || c.days.length === 7) ? 'Every day' : c.days.map(d => DAY_NAMES[d]).join(', ');
                const neg = c.negate ? '<span class="badge bg-danger ms-1">NOT</span>' : '';
                cDesc = `${neg} Time <code>${c.time_from} → ${c.time_to}</code> <span class="text-muted">${dayStr}</span>`;
            } else {
                const sus = c.sustain?`<span class="badge bg-info text-dark ms-1">⏱${c.sustain}s</span>`:'';
                const dispVal = Array.isArray(c.value) ? c.value.join(', ') : c.value;
                cDesc = `<code>${c.attribute}</code> ${OP[c.operator]||c.operator} <code>${dispVal}</code>${sus}`;
            }
            cH += `<div class="small">${p} ${cDesc}</div>`;
        });
        (rule.prerequisites||[]).forEach(p => {
            const neg = p.negate?'<span class="badge bg-danger ms-1">NOT</span>':'';
            let pDesc;
            if (p.type === 'time_window') {
                const DAY_NAMES = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
                const dayStr = (!p.days || p.days.length === 7) ? 'Every day' : p.days.map(d => DAY_NAMES[d]).join(', ');
                pDesc = `<code>${p.time_from} → ${p.time_to}</code> <span class="text-muted">${dayStr}</span>`;
            } else {
                pDesc = `${p.device_name||p.ieee} <code>${p.attribute}</code> ${OP[p.operator]||p.operator} <code>${p.value}</code>`;
            }
            cH += `<div class="small"><strong class="text-info">CHECK</strong>${neg} ${pDesc}</div>`;
        });
        const tH = _seqSummary(rule.then_sequence||[], 'THEN', 'success');
        const eH = _seqSummary(rule.else_sequence||[], 'ELSE', 'danger');

        h += `<div class="card mb-2 ${en?'':'opacity-50'}"><div class="card-body py-2 px-3">
            <div class="d-flex justify-content-between align-items-start">
                <div class="flex-grow-1"><div class="mb-1">${nm}<code class="text-muted small">${rule.id}</code>${stB}${run}</div>${cH}${tH}${eH}</div>
                <div class="d-flex gap-1 ms-2">
                    <span class="badge bg-secondary">${rule.cooldown||5}s</span>
                    <button class="btn btn-sm btn-outline-secondary" onclick="window._aTraceR('${rule.id}')"><i class="fas fa-search"></i></button>
                    <button class="btn btn-sm btn-outline-primary" onclick="window._aEdit('${rule.id}')"><i class="fas fa-edit"></i></button>
                    <button class="btn btn-sm ${en?'btn-outline-success':'btn-outline-secondary'}" onclick="window._aToggle('${rule.id}')"><i class="fas fa-${en?'toggle-on':'toggle-off'}"></i></button>
                    <button class="btn btn-sm btn-outline-danger" onclick="window._aDel('${rule.id}')"><i class="fas fa-trash"></i></button>
                    <button class="btn btn-sm btn-outline-info" onclick="window._aDownloadJson('${rule.id}')" title="Download JSON"><i class="fas fa-download"></i></button>
                </div>
            </div>
        </div></div>`;
    });
    el.innerHTML = h;
}

function _seqSummary(steps, label, color) {
    if (!steps.length) return '';
    const parts = steps.map(s => {
        if (s.type==='command') return `<span class="badge bg-info text-dark">${s.command}${s.value!=null?' ='+s.value:''}</span> <small class="text-muted">${s.target_name||s.target_ieee||'?'}</small>`;
        if (s.type==='delay') return `<span class="badge bg-warning text-dark">⏱${s.seconds}s</span>`;
        if (s.type==='wait_for') return `<span class="badge bg-secondary">⏳ ${s.device_name||s.ieee||'?'} ${s.attribute}</span>`;
        if (s.type==='condition') return `<span class="badge bg-dark">🔒 ${s.device_name||s.ieee||'?'} ${s.attribute}</span>`;
        if (s.type==='if_then_else') return `<span class="badge bg-purple" style="background:#6f42c1">IF/THEN/ELSE</span>`;
        if (s.type==='parallel') return `<span class="badge bg-dark">⚡ PARALLEL(${(s.branches||[]).length})</span>`;
        return '';
    }).join(' <i class="fas fa-arrow-right text-muted small"></i> ');
    return `<div class="small mt-1"><strong class="text-${color}">${label}</strong> ${parts}</div>`;
}

// ============================================================================
// FORM
// ============================================================================

function _showForm(rule) {
    const isE = !!rule; editingRuleId = isE ? rule.id : null;
    const el = document.getElementById('a-form'); if (!el) return;
    el.innerHTML = `
    <div class="card-header bg-light d-flex justify-content-between"><strong><i class="fas fa-${isE?'edit':'bolt'}"></i> ${isE?'Edit':'New'} Automation</strong>
        <button class="btn btn-sm btn-outline-secondary" onclick="window._aHideForm()"><i class="fas fa-times"></i></button></div>
    <div class="card-body">
        <div class="mb-3"><label class="form-label small text-muted mb-0">Rule Name</label>
            <input type="text" class="form-control form-control-sm" id="a-name" value="${isE?(rule.name||''):''}"></div>
        <div class="mb-3"><div class="d-flex justify-content-between mb-1"><label class="form-label fw-bold small mb-0">Trigger Conditions</label>
            <button class="btn btn-sm btn-outline-primary" onclick="window._aAddCond()"><i class="fas fa-plus"></i></button></div><div id="cb"></div></div>
        <div class="mb-3"><div class="d-flex justify-content-between mb-1"><label class="form-label fw-bold small mb-0">Prerequisites <span class="text-muted fw-normal">(optional, supports NOT)</span></label>
            <button class="btn btn-sm btn-outline-info" onclick="window._aAddPrereq()"><i class="fas fa-plus"></i></button></div><div id="pb"></div></div>
        <div class="mb-3"><label class="form-label fw-bold small text-success">THEN sequence <span class="fw-normal text-muted">(conditions become true)</span></label>
            <div id="then-b"></div>${_addBtns('then')}</div>
        <div class="mb-3"><label class="form-label fw-bold small text-danger">ELSE sequence <span class="fw-normal text-muted">(conditions become false)</span></label>
            <div id="else-b"></div>${_addBtns('else')}</div>
        <div class="row g-2 mb-3">
            <div class="col-md-4"><label class="form-label small text-muted mb-0">Cooldown (s)</label><input type="number" class="form-control form-control-sm" id="a-cd" value="${isE?(rule.cooldown||5):5}" min="0"></div>
            <div class="col-md-4 d-flex align-items-end"><button class="btn btn-primary btn-sm w-100" onclick="window._aSave()"><i class="fas fa-save"></i> ${isE?'Update':'Save'}</button></div>
        </div>
    </div>`;
    el.style.display = 'block';

    // Conditions
    condRows=[]; condIdC=0;
    if(isE && rule.conditions?.length) rule.conditions.forEach(()=>condRows.push(condIdC++));
    else condRows.push(condIdC++);
    _refConds();
    if(isE && rule.conditions) setTimeout(()=>rule.conditions.forEach((c,i)=>{if(condRows[i]!==undefined)_setC(condRows[i],c);}),50);

    // Prerequisites
    prereqRows=[]; prereqIdC=0;
    if(isE && rule.prerequisites?.length) rule.prerequisites.forEach(()=>prereqRows.push(prereqIdC++));
    _refPrereqs();
    if(isE && rule.prerequisites) setTimeout(()=>rule.prerequisites.forEach((p,i)=>{if(prereqRows[i]!==undefined)_setP(prereqRows[i],p);}),100);

    // Step trees
    stepIdC = 0;
    thenTree = isE ? _cloneSteps(rule.then_sequence||[]) : [];
    elseTree = isE ? _cloneSteps(rule.else_sequence||[]) : [];
    _renderStepTree('then');
    _renderStepTree('else');
}

function _cloneSteps(steps) {
    return steps.map(s => {
        const c = {...s, _id: _uid()};
        if(c.then_steps) c.then_steps = _cloneSteps(c.then_steps);
        if(c.else_steps) c.else_steps = _cloneSteps(c.else_steps);
        if(c.branches) c.branches = c.branches.map(b=>_cloneSteps(b));
        if(c.inline_conditions) c.inline_conditions = c.inline_conditions.map(ic=>({...ic, _id:_uid()}));
        return c;
    });
}

function _addBtns(path) {
    return `<div class="mt-1 btn-group btn-group-sm">
        <button class="btn btn-outline-success" onclick="window._aAddStep('${path}','command')"><i class="fas fa-bolt"></i> Cmd</button>
        <button class="btn btn-outline-warning" onclick="window._aAddStep('${path}','delay')"><i class="fas fa-clock"></i> Delay</button>
        <button class="btn btn-outline-secondary" onclick="window._aAddStep('${path}','wait_for')"><i class="fas fa-hourglass-half"></i> Wait</button>
        <button class="btn btn-outline-dark" onclick="window._aAddStep('${path}','condition')"><i class="fas fa-filter"></i> Gate</button>
        <button class="btn btn-outline-primary" onclick="window._aAddStep('${path}','if_then_else')"><i class="fas fa-code-branch"></i> If/Then/Else</button>
        <button class="btn btn-outline-info" onclick="window._aAddStep('${path}','parallel')"><i class="fas fa-columns"></i> Parallel</button>
    </div>`;
}

// ============================================================================
// VALUE INPUT
// ============================================================================

// Add 'idAttr' parameter to handle both data-sid and data-icid
function _vI(cls, id, opts, cur, idAttr = 'data-id') {
    if(opts?.length) {
        // pass the specific class (s-vl or ic-vl) and the ID attribute
        let h=`<select class="form-select form-select-sm ${cls}" ${idAttr}="${id}">`;
        opts.forEach(v=>{
            h+=`<option value="${v}" ${cur!==undefined&&String(cur).toLowerCase()===String(v).toLowerCase()?'selected':''}>${v}</option>`;
        });
        return h+'</select>';
    }
    return `<input type="text" class="form-control form-control-sm ${cls}" ${idAttr}="${id}" placeholder="Value" value="${cur!==undefined?cur:''}">`;
}

// ============================================================================
// CONDITIONS + PREREQUISITES (same pattern as before)
// ============================================================================

function _renderCond(id, ctype) {
    ctype = ctype || 'attribute';
    const opts=cachedAttributes.map(a=>`<option value="${a.attribute}" data-type="${a.type}" data-operators='${JSON.stringify(a.operators)}' data-current="${a.current_value}" data-vo='${JSON.stringify(a.value_options||[])}'>${a.attribute} (${a.current_value})</option>`).join('');
    const idx=condRows.indexOf(id);
    const badge = `<span class="badge ${idx===0?'bg-primary':'bg-warning text-dark'} small">${idx===0?'IF':'AND'}</span>`;
    const rmBtn = idx>0 ? `<button class="btn btn-sm btn-outline-danger" onclick="window._aRmC(${id})"><i class="fas fa-times"></i></button>` : '<div style="width:31px"></div>';
    const DAYS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    const dayBoxes = DAYS.map((d,i) => `<label class="me-1 small"><input type="checkbox" class="ctd" data-id="${id}" data-day="${i}" checked> ${d}</label>`).join('');
    const attrRow = `
        <div class="col"><select class="form-select form-select-sm ca" data-id="${id}" onchange="window._aCa(${id},this)"><option value="">Attr...</option>${opts}</select></div>
        <div class="col-auto"><select class="form-select form-select-sm co" data-id="${id}" style="width:120px"><option value="">Op...</option></select></div>
        <div class="col" id="cv-${id}"><input type="text" class="form-control form-control-sm cv" data-id="${id}" placeholder="Value"></div>
        <div class="col-auto" style="width:65px"><input type="number" class="form-control form-control-sm cs" data-id="${id}" placeholder="⏱s" min="0"></div>`;
    const timeRow = `
        <div class="col-auto"><div class="form-check form-check-inline mb-0"><input class="form-check-input cn" type="checkbox" data-id="${id}" title="NOT (negate)"><label class="form-check-label small text-danger">NOT</label></div></div>
        <div class="col-auto"><label class="small text-muted mb-0 me-1">From</label><input type="time" class="form-control form-control-sm ct-from" data-id="${id}" style="width:110px" value="00:00"></div>
        <div class="col-auto"><label class="small text-muted mb-0 me-1">To</label><input type="time" class="form-control form-control-sm ct-to" data-id="${id}" style="width:110px" value="23:59"></div>
        <div class="col"><div class="d-flex flex-wrap gap-1 align-items-center pt-1">${dayBoxes}</div></div>`;
    return `<div class="row g-1 mb-1 align-items-center flex-wrap" id="c-${id}">
        <div class="col-auto">${badge}</div>
        <div class="col-auto"><select class="form-select form-select-sm ctype" data-id="${id}" style="width:90px" onchange="window._aCType(${id},this)"><option value="attribute" ${ctype==='attribute'?'selected':''}>Attr</option><option value="time_window" ${ctype==='time_window'?'selected':''}>Time/Day</option></select></div>
        <div style="display:contents">${ctype==='time_window'?timeRow:attrRow}</div>
        <div class="col-auto">${rmBtn}</div>
    </div>`;
}
function _refConds(){const el=document.getElementById('cb');if(el)el.innerHTML=condRows.map(id=>_renderCond(id)).join('');}
function _setC(id,c){
    const ctype = c.type || 'attribute';
    const row = document.getElementById(`c-${id}`);
    if (!row) return;
    row.outerHTML = _renderCond(id, ctype);
    const r2 = document.getElementById(`c-${id}`);
    if (!r2) return;
    if (ctype === 'time_window') {
        const neg = r2.querySelector('.cn'); if (neg) neg.checked = !!c.negate;
        const tf = r2.querySelector('.ct-from'); if (tf) tf.value = c.time_from || '00:00';
        const tt = r2.querySelector('.ct-to');   if (tt) tt.value = c.time_to   || '23:59';
        const days = c.days ?? [0,1,2,3,4,5,6];
        r2.querySelectorAll('.ctd').forEach(cb => { cb.checked = days.includes(parseInt(cb.dataset.day)); });
    } else {
        const s=r2.querySelector('.ca');if(!s)return;s.value=c.attribute;window._aCa(id,s);
        setTimeout(()=>{const o=r2.querySelector('.co');if(o){o.value=c.operator;if(o.onchange)o.onchange();}
            const v=r2.querySelector(`#cv-${id} .cv`);
            if(v){const dv=Array.isArray(c.value)?c.value.join(', '):String(c.value);v.value=dv;}
            const ss=r2.querySelector('.cs');if(ss&&c.sustain)ss.value=c.sustain;},20);
    }
}

function _renderPrereq(id, ptype) {
    ptype = ptype || 'device';
    const _filtP = cachedAllDevices.filter(d => d.ieee !== currentSourceIeee);
    const _devP  = _filtP.filter(d => !d._is_group);
    const _grpP  = _filtP.filter(d => d._is_group);
    let devs = _devP.map(d => `<option value="${d.ieee}">${d.friendly_name}</option>`).join('');
    if (_grpP.length) devs += `<optgroup label="── Groups ──">` + _grpP.map(d => `<option value="${d.ieee}">${d.friendly_name}</option>`).join('') + '</optgroup>';

    const DAYS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    const dayBoxes = DAYS.map((d,i) => `<label class="me-1 small"><input type="checkbox" class="ptd" data-id="${id}" data-day="${i}" checked> ${d}</label>`).join('');

    const deviceRow = `
        <div class="col"><select class="form-select form-select-sm pd" data-id="${id}" onchange="window._aPd(${id},this)"><option value="">Device...</option>${devs}</select></div>
        <div class="col"><select class="form-select form-select-sm pa" data-id="${id}" onchange="window._aPa(${id},this)"><option value="">Attr...</option></select></div>
        <div class="col-auto"><select class="form-select form-select-sm po" data-id="${id}" style="width:70px">${Object.entries(OP).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></div>
        <div class="col" id="pv-${id}"><input type="text" class="form-control form-control-sm pv" data-id="${id}" placeholder="Value"></div>`;

    const timeRow = `
        <div class="col-auto"><label class="small text-muted mb-0 me-1">From</label><input type="time" class="form-control form-control-sm pt-from" data-id="${id}" style="width:110px" value="00:00"></div>
        <div class="col-auto"><label class="small text-muted mb-0 me-1">To</label><input type="time" class="form-control form-control-sm pt-to" data-id="${id}" style="width:110px" value="23:59"></div>
        <div class="col"><div class="d-flex flex-wrap gap-1 align-items-center pt-1">${dayBoxes}</div></div>`;

    return `<div class="row g-1 mb-1 align-items-center flex-wrap" id="p-${id}">
        <div class="col-auto"><span class="badge bg-info text-dark small">CHECK</span></div>
        <div class="col-auto"><div class="form-check form-check-inline mb-0"><input class="form-check-input pn" type="checkbox" data-id="${id}" title="NOT (negate)"><label class="form-check-label small text-danger">NOT</label></div></div>
        <div class="col-auto">
            <select class="form-select form-select-sm ptype" data-id="${id}" style="width:90px" onchange="window._aPType(${id},this)">
                <option value="device" ${ptype==='device'?'selected':''}>Device</option>
                <option value="time_window" ${ptype==='time_window'?'selected':''}>Time/Day</option>
            </select>
        </div>
        <div class="prereq-body-${id}" style="display:contents">${ptype === 'time_window' ? timeRow : deviceRow}</div>
        <div class="col-auto"><button class="btn btn-sm btn-outline-danger" onclick="window._aRmP(${id})"><i class="fas fa-times"></i></button></div>
    </div>`;
}

function _refPrereqs() {
    const el = document.getElementById('pb');
    if (el) el.innerHTML = prereqRows.map(id => _renderPrereq(id)).join('');
}

function _setP(id, p) {
    const ptype = p.type || 'device';
    // Rebuild row with correct type first
    const row = document.getElementById(`p-${id}`);
    if (!row) return;
    row.outerHTML = _renderPrereq(id, ptype);
    const r2 = document.getElementById(`p-${id}`);
    if (!r2) return;

    const neg = r2.querySelector(`.pn`); if (neg) neg.checked = !!p.negate;

    if (ptype === 'time_window') {
        const tf = r2.querySelector('.pt-from'); if (tf) tf.value = p.time_from || '00:00';
        const tt = r2.querySelector('.pt-to');   if (tt) tt.value = p.time_to   || '23:59';
        const days = p.days ?? [0,1,2,3,4,5,6];
        r2.querySelectorAll('.ptd').forEach(cb => {
            cb.checked = days.includes(parseInt(cb.dataset.day));
        });
    } else {
        const d = r2.querySelector('.pd'); if (d) { d.value = p.ieee; window._aPd(id, d); }
        setTimeout(() => {
            const a = r2.querySelector('.pa'); if (a) { a.value = p.attribute; }
            const o = r2.querySelector('.po'); if (o) { o.value = p.operator; if (o.onchange) o.onchange(); }
            const v = r2.querySelector(`#pv-${id} .pv`); if (v) v.value = Array.isArray(p.value) ? p.value.join(', ') : String(p.value);
        }, 50);
    }
}

// ============================================================================
// STEP TREE RENDERER (recursive)
// ============================================================================

function _renderStepTree(path) {
    const el = document.getElementById(`${path}-b`); if(!el) return;
    const tree = path==='then'?thenTree:elseTree;
    if(!tree.length){el.innerHTML='<div class="text-muted small fst-italic">No steps.</div>';return;}
    el.innerHTML = tree.map((s,i)=>_renderStep(s, path, i, tree.length)).join('');
    // Init dynamic selects after DOM render
    requestAnimationFrame(()=>_initStepSelects(tree, path));
}

function _renderStep(step, path, idx, total) {
    const sid = step._id;
    const ic = SICON[step.type]||'fa-cog';
    const lb = SLBL[step.type]||step.type;
    let body = '';

    if(step.type==='command') {
            const _devActs=cachedActuators.filter(d=>!d._is_group);
            const _grpActs=cachedActuators.filter(d=>d._is_group);
            let acts=_devActs.map(d=>`<option value="${d.ieee}" data-cmds='${JSON.stringify(d.commands)}' ${step.target_ieee===d.ieee?'selected':''}>${d.friendly_name}</option>`).join('');
            if(_grpActs.length){acts+=`<optgroup label="── Groups ──">`+_grpActs.map(d=>`<option value="${d.ieee}" data-cmds='${JSON.stringify(d.commands)}' ${step.target_ieee===d.ieee?'selected':''}>${d.friendly_name}</option>`).join('')+'</optgroup>';}
        body=`<div class="row g-1"><div class="col-md-5"><select class="form-select form-select-sm s-tgt" data-sid="${sid}" onchange="window._aSTC(${sid},this)"><option value="">Target...</option>${acts}</select></div>
            <div class="col-md-4"><select class="form-select form-select-sm s-cmd" data-sid="${sid}"><option value="">Cmd...</option></select></div>
            <div class="col-md-3"><input type="text" class="form-control form-control-sm s-val" data-sid="${sid}" placeholder="Value" value="${step.value!=null?step.value:''}"></div></div>
            <input type="hidden" class="s-ep" data-sid="${sid}" value="${step.endpoint_id||''}">`;
    } else if(step.type==='delay') {
        body=`<div class="d-flex gap-1 align-items-center"><input type="number" class="form-control form-control-sm s-sec" data-sid="${sid}" value="${step.seconds||5}" min="1" style="width:80px"><span class="small">seconds</span></div>`;
    } else if(step.type==='wait_for'||step.type==='condition') {
            const _devW=cachedAllDevices.filter(d=>!d._is_group);
            const _grpW=cachedAllDevices.filter(d=>d._is_group);
            let devs=_devW.map(d=>`<option value="${d.ieee}" ${step.ieee===d.ieee?'selected':''}>${d.friendly_name}</option>`).join('');
            if(_grpW.length){devs+=`<optgroup label="── Groups ──">`+_grpW.map(d=>`<option value="${d.ieee}" ${step.ieee===d.ieee?'selected':''}>${d.friendly_name}</option>`).join('')+'</optgroup>';}
        const neg = step.type==='condition'||step.type==='wait_for'?`<div class="form-check form-check-inline mb-0"><input class="form-check-input s-neg" type="checkbox" data-sid="${sid}" ${step.negate?'checked':''}><label class="small text-danger">NOT</label></div>`:'';
        const tout = step.type==='wait_for'?`<input type="number" class="form-control form-control-sm s-tout" data-sid="${sid}" value="${step.timeout||300}" min="1" style="width:65px" title="Timeout(s)">`:'';
        body=`<div class="row g-1 align-items-center"><div class="col-auto">${neg}</div><div class="col"><select class="form-select form-select-sm s-ieee" data-sid="${sid}" onchange="window._aSDC(${sid},this)"><option value="">Device...</option>${devs}</select></div>
            <div class="col"><select class="form-select form-select-sm s-attr" data-sid="${sid}"><option value="">Attr...</option></select></div>
            <div class="col-auto"><select class="form-select form-select-sm s-op" data-sid="${sid}" style="width:120px">${_opOpts(step.operator||'')}</select></div>
            <div class="col" id="sv-${sid}"><input type="text" class="form-control form-control-sm s-vl" data-sid="${sid}" placeholder="Value" value="${step.value!=null?step.value:''}"></div>
            ${tout?`<div class="col-auto">${tout}</div>`:''}</div>`;
    } else if(step.type==='if_then_else') {
        const logic = step.condition_logic||'and';
        const ics = step.inline_conditions||[];
        let icH = ics.map((ic,j)=>_renderInlineCond(ic,j,sid,ics.length)).join('');
        const showLogic = ics.length > 1;
        body=`<div class="mb-2"><div class="d-flex gap-2 align-items-center mb-1">
            <span class="small fw-bold">IF</span>
            <select class="form-select form-select-sm s-logic" data-sid="${sid}" style="width:70px${showLogic?'':';display:none'}"><option value="and" ${logic==='and'?'selected':''}>AND</option><option value="or" ${logic==='or'?'selected':''}>OR</option></select>
            <button class="btn btn-sm btn-outline-primary py-0" onclick="window._aAddIC(${sid})"><i class="fas fa-plus"></i></button></div>
            <div id="ic-${sid}">${icH}</div></div>
        <div class="border-start border-success border-3 ps-2 mb-2"><div class="small fw-bold text-success mb-1">THEN</div><div id="ite-then-${sid}">${(step.then_steps||[]).map((s,i)=>_renderStep(s,`ite-then-${sid}`,i,(step.then_steps||[]).length)).join('')}</div>
            ${_addBtns(`ite-then-${sid}`)}</div>
        <div class="border-start border-danger border-3 ps-2"><div class="small fw-bold text-danger mb-1">ELSE</div><div id="ite-else-${sid}">${(step.else_steps||[]).map((s,i)=>_renderStep(s,`ite-else-${sid}`,i,(step.else_steps||[]).length)).join('')}</div>
            ${_addBtns(`ite-else-${sid}`)}</div>`;
    } else if(step.type==='parallel') {
        const branches = step.branches||[[], []];
        body = branches.map((br,bi)=>`<div class="border-start border-info border-3 ps-2 mb-2"><div class="small fw-bold text-info mb-1">Branch ${bi+1}</div>
            <div id="par-${sid}-${bi}">${br.map((s,i)=>_renderStep(s,`par-${sid}-${bi}`,i,br.length)).join('')}</div>
            ${_addBtns(`par-${sid}-${bi}`)}</div>`).join('');
        body += `<button class="btn btn-sm btn-outline-info" onclick="window._aAddBranch(${sid})"><i class="fas fa-plus"></i> Branch</button>`;
    }

    return `<div class="card card-body p-2 mb-1" style="background:#f8f9fa" id="step-${sid}">
        <div class="d-flex justify-content-between align-items-center mb-1">
            <span class="badge bg-dark"><i class="fas ${ic}"></i> ${lb} <small>${idx+1}/${total}</small></span>
            <button class="btn btn-sm btn-outline-danger py-0 px-1" onclick="window._aRmStep(${sid},'${path}')"><i class="fas fa-times"></i></button>
        </div>${body}</div>`;
}

function _renderInlineCond(ic, idx, parentSid, total) {
    const _devIC=cachedAllDevices.filter(d=>!d._is_group);
    const _grpIC=cachedAllDevices.filter(d=>d._is_group);
    let devs=_devIC.map(d=>`<option value="${d.ieee}" ${ic.ieee===d.ieee?'selected':''}>${d.friendly_name}</option>`).join('');
    if(_grpIC.length){devs+=`<optgroup label="── Groups ──">`+_grpIC.map(d=>`<option value="${d.ieee}" ${ic.ieee===d.ieee?'selected':''}>${d.friendly_name}</option>`).join('')+'</optgroup>';}
    const icId = ic._id;
    const rmBtn = total > 1 ? `<button class="btn btn-sm btn-outline-danger py-0" onclick="window._aRmIC(${parentSid},${icId})"><i class="fas fa-times"></i></button>` : '';
    return `<div class="row g-1 mb-1 align-items-center" id="ic-row-${icId}">
        <div class="col-auto"><div class="form-check form-check-inline mb-0"><input class="form-check-input ic-neg" type="checkbox" data-icid="${icId}" ${ic.negate?'checked':''}><label class="small text-danger">NOT</label></div></div>
        <div class="col"><select class="form-select form-select-sm ic-ieee" data-icid="${icId}" onchange="window._aICDev(${icId},this)"><option value="">Device...</option>${devs}</select></div>
        <div class="col"><select class="form-select form-select-sm ic-attr" data-icid="${icId}"><option value="">Attr...</option></select></div>
        <div class="col-auto"><select class="form-select form-select-sm ic-op" data-icid="${icId}" style="width:120px">${_opOpts(ic.operator||'')}</select></div>
        <div class="col" id="icv-${icId}"><input type="text" class="form-control form-control-sm ic-vl" data-icid="${icId}" placeholder="Value" value="${ic.value!=null?ic.value:''}"></div>
        <div class="col-auto">${rmBtn}</div>
    </div>`;
}

function _initStepSelects(steps, path) {
    steps.forEach(s => {
        if(s.type==='command' && s.target_ieee) {
            const sel=document.querySelector(`.s-tgt[data-sid="${s._id}"]`);
            if(sel){const o=sel.options[sel.selectedIndex];if(o?.dataset?.cmds)_popCmds(s._id,JSON.parse(o.dataset.cmds),s.command,s.endpoint_id);}
        } else if((s.type==='wait_for'||s.type==='condition')&&s.ieee) {
            _loadAttrs(s._id,s.ieee,s.attribute,s.value);
        } else if(s.type==='if_then_else') {
            (s.inline_conditions||[]).forEach(ic=>{if(ic.ieee)_loadICAttrs(ic._id,ic.ieee,ic.attribute,ic.value);});
            _initStepSelects(s.then_steps||[],'ite-then-'+s._id);
            _initStepSelects(s.else_steps||[],'ite-else-'+s._id);
        } else if(s.type==='parallel') {
            (s.branches||[]).forEach((br,bi)=>_initStepSelects(br,`par-${s._id}-${bi}`));
        }
    });
}

// ============================================================================
// SELECT HELPERS
// ============================================================================

function _popCmds(sid,cmds,selCmd,selEp) {
    const sel=document.querySelector(`.s-cmd[data-sid="${sid}"]`);if(!sel)return;
    sel.innerHTML='<option value="">Cmd...</option>';
    (cmds||[]).forEach(c=>{const o=document.createElement('option');o.value=c.command;o.dataset.ep=c.endpoint_id||'';o.textContent=`${c.label||c.command}${c.endpoint_id?' (EP'+c.endpoint_id+')':''}`;if(selCmd===c.command&&(!selEp||selEp==c.endpoint_id))o.selected=true;sel.appendChild(o);});
    sel.onchange=()=>{const o=sel.options[sel.selectedIndex];const ep=document.querySelector(`.s-ep[data-sid="${sid}"]`);if(ep&&o)ep.value=o.dataset.ep||'';};
    if(selEp){const ep=document.querySelector(`.s-ep[data-sid="${sid}"]`);if(ep)ep.value=selEp;}
}

async function _loadAttrs(sid,ieee,selAttr,selVal) {
    const aS=document.querySelector(`.s-attr[data-sid="${sid}"]`);if(!aS)return;
    try{const d=await(await fetch(`/api/automations/device/${encodeURIComponent(ieee)}/state`)).json();
        aS.innerHTML='<option value="">Attr...</option>';
        (d.attributes||[]).forEach(a=>{const o=document.createElement('option');o.value=a.attribute;o.dataset.vo=JSON.stringify(a.value_options||[]);o.dataset.current=a.current_value;o.textContent=`${a.attribute} (${a.current_value})`;if(selAttr===a.attribute)o.selected=true;aS.appendChild(o);});

        aS.onchange=()=>{
            const o=aS.options[aS.selectedIndex];
            if(!o)return;
            const vo=JSON.parse(o.dataset.vo||'[]');
            const w=document.getElementById(`sv-${sid}`);
            // PASS 'data-sid' as the ID attribute name
            if(w)w.innerHTML=_vI('s-vl', sid, vo, '', 'data-sid');
        };
        if(selAttr){
            const o=aS.options[aS.selectedIndex];
            if(o){
                const vo=JSON.parse(o.dataset.vo||'[]');
                const w=document.getElementById(`sv-${sid}`);
                // PASS 'data-sid' as the ID attribute name
                if(w)w.innerHTML=_vI('s-vl', sid, vo, selVal!=null?selVal:'', 'data-sid');
            }
        }
    }catch(e){}
}

async function _loadICAttrs(icId,ieee,selAttr,selVal) {
    const aS=document.querySelector(`.ic-attr[data-icid="${icId}"]`);if(!aS)return;
    try{const d=await(await fetch(`/api/automations/device/${encodeURIComponent(ieee)}/state`)).json();
        aS.innerHTML='<option value="">Attr...</option>';
        (d.attributes||[]).forEach(a=>{const o=document.createElement('option');o.value=a.attribute;o.dataset.vo=JSON.stringify(a.value_options||[]);o.textContent=`${a.attribute} (${a.current_value})`;if(selAttr===a.attribute)o.selected=true;aS.appendChild(o);});

        aS.onchange=()=>{
            const o=aS.options[aS.selectedIndex];
            if(!o)return;
            const vo=JSON.parse(o.dataset.vo||'[]');
            const w=document.getElementById(`icv-${icId}`);
            // PASS 'data-icid' as the ID attribute name
            if(w)w.innerHTML=_vI('ic-vl', icId, vo, '', 'data-icid');
        };
        if(selAttr){
            const o=aS.options[aS.selectedIndex];
            if(o){
                const vo=JSON.parse(o.dataset.vo||'[]');
                const w=document.getElementById(`icv-${icId}`);
                // PASS 'data-icid' as the ID attribute name
                if(w)w.innerHTML=_vI('ic-vl', icId, vo, selVal!=null?selVal:'', 'data-icid');
            }
        }
    }catch(e){}
}

// ============================================================================
// STEP TREE MANIPULATION
// ============================================================================

function _findStepList(path) {
    if(path==='then') return thenTree;
    if(path==='else') return elseTree;
    // Nested paths: "ite-then-{sid}" or "ite-else-{sid}" or "par-{sid}-{bi}"
    const iteM = path.match(/^ite-(then|else)-(\d+)$/);
    if(iteM) {
        const branch=iteM[1], parentId=parseInt(iteM[2]);
        const step = _findStepById(parentId);
        if(!step) return null;
        return branch==='then' ? (step.then_steps||(step.then_steps=[])) : (step.else_steps||(step.else_steps=[]));
    }
    const parM = path.match(/^par-(\d+)-(\d+)$/);
    if(parM) {
        const parentId=parseInt(parM[1]), bi=parseInt(parM[2]);
        const step = _findStepById(parentId);
        if(!step || !step.branches) return null;
        return step.branches[bi];
    }
    return null;
}

function _findStepById(id, list) {
    for(const tree of [thenTree, elseTree]) {
        const r = _findInTree(tree, id);
        if(r) return r;
    }
    return null;
}

function _findInTree(steps, id) {
    for(const s of steps) {
        if(s._id === id) return s;
        if(s.then_steps) { const r=_findInTree(s.then_steps,id); if(r) return r; }
        if(s.else_steps) { const r=_findInTree(s.else_steps,id); if(r) return r; }
        if(s.branches) { for(const b of s.branches) { const r=_findInTree(b,id); if(r) return r; } }
    }
    return null;
}

function _removeFromTree(steps, id) {
    const idx = steps.findIndex(s=>s._id===id);
    if(idx>=0) { steps.splice(idx,1); return true; }
    for(const s of steps) {
        if(s.then_steps && _removeFromTree(s.then_steps,id)) return true;
        if(s.else_steps && _removeFromTree(s.else_steps,id)) return true;
        if(s.branches) { for(const b of s.branches) { if(_removeFromTree(b,id)) return true; } }
    }
    return false;
}

// ============================================================================
// WINDOW HANDLERS
// ============================================================================

// Conditions
window._aCa=(id,sel)=>{const o=sel.options[sel.selectedIndex];if(!o?.value)return;const ops=JSON.parse(o.dataset.operators||'["eq","neq"]'),vo=JSON.parse(o.dataset.vo||'[]'),cur=o.dataset.current,typ=o.dataset.type;
    const os=document.querySelector(`#c-${id} .co`);if(os){os.innerHTML=ops.map(op=>`<option value="${op}">${OP[op]} ${OPT[op]}</option>`).join('');
        os.onchange=()=>{const opV=os.value;const w=document.getElementById(`cv-${id}`);if(w){
            if(opV==='in'||opV==='nin'){w.innerHTML=`<input type="text" class="form-control form-control-sm cv" data-id="${id}" placeholder="val1, val2, ...">`;}
            else{w.innerHTML=_vI('cv',id,vo,typ==='boolean'?String(cur).toLowerCase():'');}}};}
    const w=document.getElementById(`cv-${id}`);if(w)w.innerHTML=_vI('cv',id,vo,typ==='boolean'?String(cur).toLowerCase():'');};
window._aAddCond=()=>{if(condRows.length>=5)return;const nid=condIdC++;condRows.push(nid);const el=document.getElementById('cb');if(el)el.insertAdjacentHTML('beforeend',_renderCond(nid));};
window._aRmC=id=>{condRows=condRows.filter(r=>r!==id);const row=document.getElementById(`c-${id}`);if(row)row.remove();
    if(condRows.length>0){const first=document.getElementById(`c-${condRows[0]}`);if(first){const b=first.querySelector('.badge');if(b){b.className='badge bg-primary small';b.textContent='IF';}
        const rm=first.querySelector('.btn-outline-danger');if(rm)rm.closest('.col-auto').innerHTML='<div style="width:31px"></div>';}};};
window._aCType = (id, sel) => {
    const ctype = sel.value;
    const row = document.getElementById(`c-${id}`);
    if (!row) return;
    const neg = row.querySelector('.cn')?.checked || false;
    row.outerHTML = _renderCond(id, ctype);
    const newRow = document.getElementById(`c-${id}`);
    if (newRow && ctype !== 'time_window') { /* attr row needs no restore */ }
    if (newRow && ctype === 'time_window') { const n = newRow.querySelector('.cn'); if (n) n.checked = neg; }
};
// Prerequisites
window._aPd=async(id,sel)=>{const ieee=sel.value;const aS=document.querySelector(`#p-${id} .pa`);if(!aS||!ieee)return;
    aS.innerHTML='<option>Loading...</option>';
    try{const d=await(await fetch(`/api/automations/device/${encodeURIComponent(ieee)}/state`)).json();
        aS.innerHTML='<option value="">Attr...</option>';(d.attributes||[]).forEach(a=>{const o=document.createElement('option');o.value=a.attribute;o.dataset.vo=JSON.stringify(a.value_options||[]);o.dataset.current=a.current_value;o.dataset.type=a.type;o.textContent=`${a.attribute} (${a.current_value})`;aS.appendChild(o);});
        aS.onchange=()=>window._aPa(id,aS);
    }catch(e){}};
window._aPa=(id,sel)=>{const o=sel.options[sel.selectedIndex];if(!o)return;const vo=JSON.parse(o.dataset?.vo||'[]');const w=document.getElementById(`pv-${id}`);if(w)w.innerHTML=_vI('pv',id,vo,o.dataset?.type==='boolean'?String(o.dataset.current).toLowerCase():'');};
window._aAddPrereq=()=>{if(prereqRows.length>=8)return;const nid=prereqIdC++;prereqRows.push(nid);const el=document.getElementById('pb');if(el)el.insertAdjacentHTML('beforeend',_renderPrereq(nid));};
window._aRmP=id=>{prereqRows=prereqRows.filter(r=>r!==id);const row=document.getElementById(`p-${id}`);if(row)row.remove();};

window._aPType = (id, sel) => {
    const ptype = sel.value;
    const row = document.getElementById(`p-${id}`);
    if (!row) return;
    const body = row.querySelector(`.prereq-body-${id}`);  // won't work with div.d-contents trick
    // Rebuild the full row keeping NOT state and new type
    const neg = row.querySelector('.pn')?.checked || false;
    const newHtml = _renderPrereq(id, ptype);
    row.outerHTML = newHtml;
    const newRow = document.getElementById(`p-${id}`);
    if (newRow) { const pn = newRow.querySelector('.pn'); if (pn) pn.checked = neg; }
};


// Steps
window._aAddStep = (path, type) => {
    const list = _findStepList(path);
    if (!list || list.length >= 15) return;

    // FIX: Sync current UI values to the tree before adding a new step
    _syncTreeFromDOM(thenTree);
    _syncTreeFromDOM(elseTree);

    const s = { _id: _uid(), type };
    if (type === 'delay') s.seconds = 5;
    if (type === 'wait_for') s.timeout = 300;
    if (type === 'if_then_else') {
        s.inline_conditions = [{ _id: _uid(), ieee: '', attribute: '', operator: 'eq', value: '' }];
        s.condition_logic = 'and';
        s.then_steps = [];
        s.else_steps = [];
    }
    if (type === 'parallel') s.branches = [[], []];

    list.push(s);

    // Re-render sequences
    _renderStepTree('then');
    _renderStepTree('else');
};

window._aRmStep=(sid,path)=>{_syncTreeFromDOM(thenTree);_syncTreeFromDOM(elseTree);_removeFromTree(thenTree,sid);_removeFromTree(elseTree,sid);_renderStepTree('then');_renderStepTree('else');};
window._aSTC=(sid,sel)=>{const o=sel.options[sel.selectedIndex];if(!o?.value)return;_popCmds(sid,JSON.parse(o.dataset.cmds||'[]'));};
window._aSDC=(sid,sel)=>{if(sel.value)_loadAttrs(sid,sel.value);};
window._aICDev=(icId,sel)=>{if(sel.value)_loadICAttrs(icId,sel.value);};

window._aAddIC = sid => {
    const s = _findStepById(sid);
    if (!s) return;

    // FIX: Sync current UI values
    _syncTreeFromDOM(thenTree);
    _syncTreeFromDOM(elseTree);

    if (!s.inline_conditions) s.inline_conditions = [];
    s.inline_conditions.push({ _id: _uid(), ieee: '', attribute: '', operator: 'eq', value: '' });

    _renderStepTree('then');
    _renderStepTree('else');
};

window._aRmIC=(sid,icId)=>{_syncTreeFromDOM(thenTree);_syncTreeFromDOM(elseTree);const s=_findStepById(sid);if(!s||!s.inline_conditions)return;s.inline_conditions=s.inline_conditions.filter(c=>c._id!==icId);_renderStepTree('then');_renderStepTree('else');};

window._aAddBranch = sid => {
    const s = _findStepById(sid);
    if (!s || !s.branches) return;

    // FIX: Sync current UI values
    _syncTreeFromDOM(thenTree);
    _syncTreeFromDOM(elseTree);

    s.branches.push([]);
    _renderStepTree('then');
    _renderStepTree('else');
};

// Form
window._aShowForm=()=>_showForm(null);
window._aHideForm=()=>{document.getElementById('a-form').style.display='none';editingRuleId=null;};
window._aEdit=async id=>{try{const r=await(await fetch(`/api/automations/rule/${id}`)).json();_showForm(r);document.getElementById('a-form')?.scrollIntoView({behavior:'smooth'});}catch(e){alert(e.message);}};

// Trace
window._aTrace=async()=>{document.getElementById('a-trace').style.display='block';
    const f=document.getElementById('tf');if(f){const c=f.value;f.innerHTML='<option value="">All</option>';
        try{(await(await fetch(`/api/automations?source_ieee=${encodeURIComponent(currentSourceIeee)}`)).json()).forEach(r=>{f.innerHTML+=`<option value="${r.id}">${r.name||r.id}</option>`;});}catch(e){}f.innerHTML+='<option value="-">System</option>';f.value=c||'';}
    _loadTr();};
window._aRefTrace=_loadTr;
window._aTraceR=async id=>{await window._aTrace();const f=document.getElementById('tf');if(f)f.value=id;_loadTr();};

// Toggle/Delete
window._aToggle=async id=>{try{await fetch(`/api/automations/${id}/toggle`,{method:'PATCH'});await _ref();}catch(e){}};
window._aDel=async id=>{if(!confirm('Delete?'))return;try{await fetch(`/api/automations/${id}`,{method:'DELETE'});await _ref();}catch(e){}};

// ============================================================================
// SAVE (recursive gather)
// ============================================================================

window._aSave=async()=>{
    const conditions=[]; let valid=true;
    condRows.forEach(id=>{
        const row=document.getElementById(`c-${id}`);
        if(!row)return;
        const ctype=row.querySelector('.ctype')?.value||'attribute';
        if(ctype==='time_window'){
            const tf=row.querySelector('.ct-from')?.value;
            const tt=row.querySelector('.ct-to')?.value;
            if(!tf||!tt){valid=false;return;}
            const neg=row.querySelector('.cn')?.checked||false;
            const days=[];row.querySelectorAll('.ctd').forEach(cb=>{if(cb.checked)days.push(parseInt(cb.dataset.day));});
            conditions.push({type:'time_window',time_from:tf,time_to:tt,days,negate:neg});
        } else {
            const a=row.querySelector('.ca')?.value,o=row.querySelector('.co')?.value;
            const vE=row.querySelector(`#cv-${id} .cv`),r=vE?.value,s=row.querySelector('.cs')?.value;
            if(!a||!o||r===undefined||r===''){valid=false;return;}
            const ai=cachedAttributes.find(x=>x.attribute===a);
            let value;
            if(o==='in'||o==='nin'){value=String(r).split(',').map(v=>_ct(v.trim(),ai?.type));}
            else{value=_ct(r,ai?.type);}
            const c={type:'attribute',attribute:a,operator:o,value};if(s&&parseInt(s)>0)c.sustain=parseInt(s);conditions.push(c);
        }
    });
    if(!valid||!conditions.length)return alert('Fill all conditions.');

    const prerequisites = [];
    prereqRows.forEach(id => {
        const row = document.getElementById(`p-${id}`);
        if (!row) return;
        const ptype = row.querySelector('.ptype')?.value || 'device';
        const neg   = row.querySelector('.pn')?.checked || false;

        if (ptype === 'time_window') {
            const tf = row.querySelector('.pt-from')?.value;
            const tt = row.querySelector('.pt-to')?.value;
            if (!tf || !tt) return;
            const days = [];
            row.querySelectorAll('.ptd').forEach(cb => { if (cb.checked) days.push(parseInt(cb.dataset.day)); });
            prerequisites.push({ type: 'time_window', time_from: tf, time_to: tt, days, negate: neg });
        } else {
            const ieee = row.querySelector('.pd')?.value;
            const a    = row.querySelector('.pa')?.value;
            const o    = row.querySelector('.po')?.value;
            const vE   = row.querySelector(`#pv-${id} .pv`);
            const r    = vE?.value;
            if (!ieee || !a || !o || r === undefined || r === '') return;
            const pval = (o === 'in' || o === 'nin') ? String(r).split(',').map(v => _co(v.trim())) : _co(r);
            prerequisites.push({ type: 'device', ieee, attribute: a, operator: o, value: pval, negate: neg });
        }
    });

    // Gather step trees (reads current DOM values into the tree, then clean for API)
    _syncTreeFromDOM(thenTree);
    _syncTreeFromDOM(elseTree);
    const then_sequence = _cleanTree(thenTree);
    const else_sequence = _cleanTree(elseTree);
    if(!then_sequence.length&&!else_sequence.length)return alert('Add at least one step.');

    const body={name:document.getElementById('a-name')?.value||'',source_ieee:currentSourceIeee,
        conditions,prerequisites,then_sequence,else_sequence,
        cooldown:parseInt(document.getElementById('a-cd')?.value)||5,enabled:true};
    try{
        const res = editingRuleId
            ? await fetch(`/api/automations/${editingRuleId}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
            : await fetch('/api/automations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        const d=await res.json();
        if(res.ok&&d.success){window._aHideForm();await _ref();}
        else alert('Failed: '+(d.detail||d.error||'Unknown'));
    }catch(e){alert(e.message);}
};

function _syncTreeFromDOM(steps) {
    steps.forEach(s => {
        const sid = s._id;
        if(s.type==='command') {
            s.target_ieee=document.querySelector(`.s-tgt[data-sid="${sid}"]`)?.value||'';
            s.command=document.querySelector(`.s-cmd[data-sid="${sid}"]`)?.value||'';
            const v=document.querySelector(`.s-val[data-sid="${sid}"]`)?.value;
            s.value=(v!==undefined&&v!=='')?_co(v):null;
            const ep=document.querySelector(`.s-ep[data-sid="${sid}"]`)?.value;
            s.endpoint_id=ep?parseInt(ep):null;
        } else if(s.type==='delay') {
            s.seconds=parseInt(document.querySelector(`.s-sec[data-sid="${sid}"]`)?.value)||5;
        } else if(s.type==='wait_for'||s.type==='condition') {
            s.ieee=document.querySelector(`.s-ieee[data-sid="${sid}"]`)?.value||'';
            s.attribute=document.querySelector(`.s-attr[data-sid="${sid}"]`)?.value||'';
            s.operator=document.querySelector(`.s-op[data-sid="${sid}"]`)?.value||'eq';

            // Check if it's a select dropdown or a text input
            const valEl = document.querySelector(`.s-vl[data-sid="${sid}"]`);
            const rawVal = valEl?.value;
            s.value = _co(rawVal !== undefined ? rawVal : '');

            s.negate=document.querySelector(`.s-neg[data-sid="${sid}"]`)?.checked||false;
            if(s.type==='wait_for')s.timeout=parseInt(document.querySelector(`.s-tout[data-sid="${sid}"]`)?.value)||300;
        } else if(s.type==='if_then_else') {
            s.condition_logic=document.querySelector(`.s-logic[data-sid="${sid}"]`)?.value||'and';
            (s.inline_conditions||[]).forEach(ic=>{
                const icid = ic._id;
                ic.ieee=document.querySelector(`.ic-ieee[data-icid="${icid}"]`)?.value||'';
                ic.attribute=document.querySelector(`.ic-attr[data-icid="${icid}"]`)?.value||'';
                ic.operator=document.querySelector(`.ic-op[data-icid="${icid}"]`)?.value||'eq';

                // Support dropdown values for inline conditions
                const icValEl = document.querySelector(`.ic-vl[data-icid="${icid}"]`);
                const icRawVal = icValEl?.value;

                ic.value=(ic.operator==='in'||ic.operator==='nin')
                    ? String(icRawVal||'').split(',').map(x=>_co(x.trim()))
                    : _co(icRawVal||'');

                ic.negate=document.querySelector(`.ic-neg[data-icid="${icid}"]`)?.checked||false;
            });
            _syncTreeFromDOM(s.then_steps||[]);
            _syncTreeFromDOM(s.else_steps||[]);
        } else if(s.type==='parallel') {
            (s.branches||[]).forEach(br=>_syncTreeFromDOM(br));
        }
    });
}

function _cleanTree(steps) {
    return steps.map(s=>{
        const d={type:s.type};
        if(s.type==='command'){d.target_ieee=s.target_ieee;d.command=s.command;if(s.value!=null)d.value=s.value;if(s.endpoint_id)d.endpoint_id=s.endpoint_id;}
        else if(s.type==='delay'){d.seconds=s.seconds;}
        else if(s.type==='wait_for'||s.type==='condition'){d.ieee=s.ieee;d.attribute=s.attribute;d.operator=s.operator;d.value=s.value;if(s.negate)d.negate=true;if(s.type==='wait_for')d.timeout=s.timeout;}
        else if(s.type==='if_then_else'){d.inline_conditions=(s.inline_conditions||[]).map(ic=>({ieee:ic.ieee,attribute:ic.attribute,operator:ic.operator,value:ic.value,...(ic.negate?{negate:true}:{})}));d.condition_logic=s.condition_logic||'and';d.then_steps=_cleanTree(s.then_steps||[]);d.else_steps=_cleanTree(s.else_steps||[]);}
        else if(s.type==='parallel'){d.branches=(s.branches||[]).map(br=>_cleanTree(br));}
        return d;
    }).filter(d=>{
        if(d.type==='command')return d.target_ieee&&d.command;
        if(d.type==='delay')return d.seconds>0;
        if(d.type==='wait_for'||d.type==='condition')return d.ieee&&d.attribute;
        if(d.type==='if_then_else')return(d.inline_conditions||[]).length>0;
        if(d.type==='parallel')return(d.branches||[]).length>=2;
        return false;
    });
}

// ============================================================================
// HELPERS
// ============================================================================

function _co(v){if(typeof v!=='string')return v;const t=v.trim(),l=t.toLowerCase();if(l==='true')return true;if(l==='false')return false;if(!isNaN(t)&&t!=='')return parseFloat(t);return t;}
function _ct(v,typ){if(!typ)return _co(v);if(typ==='boolean')return _co(v);if(typ==='float'){const n=parseFloat(v);return isNaN(n)?v:n;}if(typ==='integer'){const n=parseInt(v,10);return isNaN(n)?_co(v):n;}return String(v).trim();}
async function _ref(){if(!currentSourceIeee)return;try{_renderRules(await(await fetch(`/api/automations?source_ieee=${encodeURIComponent(currentSourceIeee)}`)).json());}catch(e){}}

async function _loadTr() {
    const el=document.getElementById('a-trace-c');if(!el)return;
    const fv=document.getElementById('tf')?.value||'';
    try{const entries=await(await fetch(fv?`/api/automations/trace?rule_id=${encodeURIComponent(fv)}`:'/api/automations/trace')).json();
        if(!entries?.length){el.innerHTML='<div class="text-muted p-2">No trace.</div>';return;}
        let h='';[...entries].reverse().forEach(e=>{
            const ts=new Date(e.timestamp*1000).toLocaleTimeString(),r=e.result||'';
            let cl='text-muted';
            if(r==='SUCCESS'||r.includes('FIRING')||r==='COMPLETE'||r==='WAIT_MET'||r==='GATE_PASS'||r==='IF_TRUE'||r==='PARALLEL_DONE')cl='text-success';
            else if(r.includes('FAIL')||r.includes('ERROR')||r==='EXCEPTION'||r.includes('MISSING')||r==='CMD_FAIL')cl='text-danger';
            else if(r==='BLOCKED'||r==='SUSTAIN_WAIT'||r==='DELAY'||r==='WAITING')cl='text-warning';
            else if(r==='CANCELLED'||r==='WAIT_TIMEOUT'||r==='IF_FALSE')cl='text-info';
            h+=`<div class="border-bottom py-1 ${cl}"><span class="text-muted">${ts}</span> <span class="badge bg-dark">${e.phase||''}</span> <span class="badge bg-secondary">${r}</span> `;
            if(e.rule_id&&e.rule_id!=='-')h+=`<code>${e.rule_id}</code> `;
            h+=e.message||'';
            if(e.conditions?.length){h+='<div class="ms-3">';e.conditions.forEach(c=>{const cc=c.result==='PASS'?'text-success':c.result==='SUSTAIN_WAIT'?'text-warning':'text-danger';
                let cLine;
                if(c.type==='time_window'){
                    const DAY_NAMES=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
                    const dayStr=(!c.days||c.days.length===7)?'Every day':c.days.map(d=>DAY_NAMES[d]).join(',');
                    cLine=`#${c.index} Time${c.negate?' NOT':''} ${c.time_from}→${c.time_to} [${dayStr}] now=${c.now_time} weekday=${c.now_weekday}`;
                }else{
                    cLine=`#${c.index} ${c.attribute} ${c.operator||''} ${c.threshold_raw||c.threshold||'?'} → ${c.actual_raw||'?'} (${c.actual_type||''})`;
                    if(c.sustain_elapsed!=null)cLine+=` ⏱${c.sustain_elapsed}s`;if(c.value_source)cLine+=` ${c.value_source}`;
                }
                h+=`<div class="${cc}">${cLine} [${c.result}]`;if(c.reason)h+=` — ${c.reason}`;h+='</div>';});h+='</div>';}
            if(e.prerequisites?.length){h+='<div class="ms-3">';e.prerequisites.forEach(p=>{const pc=p.result==='PASS'?'text-success':'text-danger';
                const DAY_NAMES=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
                let pLine;
                if(p.type==='time_window'){
                    const dayStr=(!p.days||p.days.length===7)?'Every day':p.days.map(d=>DAY_NAMES[d]).join(',');
                    pLine=`CHECK${p.negate?' NOT':''} Time ${p.time_from}→${p.time_to} [${dayStr}] now=${p.now_time} weekday=${p.now_weekday}`;
                }else{
                    pLine=`CHECK${p.negate?' NOT':''} ${p.device_name||p.ieee} ${p.attribute} ${p.operator||''} ${p.threshold_raw||'?'} → ${p.actual_raw||'?'}`;
                }
                h+=`<div class="${pc}">${pLine} [${p.result}]`;
                if(p.reason)h+=` — ${p.reason}`;h+='</div>';});h+='</div>';}
            if(e.inline_conditions?.length){h+='<div class="ms-3">';e.inline_conditions.forEach(ic=>{const cc=ic.result==='PASS'?'text-success':'text-danger';
                h+=`<div class="${cc}">  ${ic.negate?'NOT ':''}${ic.device_name||''} ${ic.attribute} ${ic.operator||''} ${ic.threshold||''} → ${ic.actual||'?'} [${ic.result}]</div>`;});h+='</div>';}
            if(e.error)h+=`<div class="ms-3 text-danger">${e.error}</div>`;h+='</div>';});
        el.innerHTML=h;
    }catch(err){el.innerHTML=`<div class="text-danger">${err.message}</div>`;}
}




// ============================================================================
// DOWNLOAD AUTOMATION FLOW
// ============================================================================
window._aDownloadJson = async (id) => {
    try {
        const res = await fetch(`/api/automations/rule/${id}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const blob = new Blob([JSON.stringify(data, null, 4)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `automation_${data.name ? data.name.replace(/[^a-z0-9_-]/gi,'_') : id}.json`;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 100);
    } catch (e) {
        alert('Failed to download: ' + e.message);
    }
};