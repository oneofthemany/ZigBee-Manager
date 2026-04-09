/* ============================================================
   ZigBee Matter Manager — Device Status Enhancements
   ============================================================ */

(function () {
    'use strict';

    // ----------------------------------------------------------
    // 1. SIGNAL STRENGTH BARS (replaces LQI badge)
    // ----------------------------------------------------------

    function renderSignalBars(lqi) {
        var val = parseInt(lqi);
        if (isNaN(val) || val < 0) return '';

        var level, className;
        if (val >= 200) { level = 4; className = 'excellent'; }
        else if (val >= 100) { level = 3; className = 'good'; }
        else if (val >= 50) { level = 2; className = 'fair'; }
        else { level = 1; className = 'poor'; }

        var bars = '';
        var heights = [4, 7, 10, 14];
        for (var i = 0; i < 4; i++) {
            var active = i < level ? 'active ' + className : 'inactive';
            bars += '<div class="zbm-signal-bar ' + active + '" style="height:' + heights[i] + 'px"></div>';
        }

        return '<span class="zbm-signal" title="LQI: ' + val + '">' + bars + '</span>' +
               '<span class="small text-muted ms-1" style="font-size:0.7rem">' + val + '</span>';
    }

    // ----------------------------------------------------------
    // 2. BATTERY INDICATOR
    // ----------------------------------------------------------

    function renderBattery(percentage) {
        if (percentage === undefined || percentage === null) return '';
        var val = parseInt(percentage);
        if (isNaN(val)) return '';

        var icon, className;
        if (val > 75) { icon = 'fa-battery-full'; className = 'good'; }
        else if (val > 40) { icon = 'fa-battery-three-quarters'; className = 'good'; }
        else if (val > 20) { icon = 'fa-battery-half'; className = 'medium'; }
        else if (val > 5) { icon = 'fa-battery-quarter'; className = 'low'; }
        else { icon = 'fa-battery-empty'; className = 'critical'; }

        return '<span class="zbm-battery ' + className + '" title="Battery: ' + val + '%">' +
               '<i class="fas ' + icon + '"></i> ' + val + '%</span>';
    }

    // ----------------------------------------------------------
    // 3. HEATING INDICATOR
    // ----------------------------------------------------------

    function renderHeatingStatus(device) {
        var s = device.state || {};
        var runningState = s.running_state || 0;
        var isHeating = (runningState & 0x0001) || String(runningState).includes('heat');

        if (isHeating) {
            return '<span class="zbm-heating-indicator" title="Actively heating">' +
                   '<i class="fas fa-fire"></i> HEAT</span>';
        }
        return '';
    }

    // ----------------------------------------------------------
    // 4. TEMPERATURE MINI DISPLAY
    // ----------------------------------------------------------

    function renderTempMini(device) {
        var s = device.state || {};
        var tempKeys = ['internal_temperature', 'temperature', 'local_temperature'];
        var temp = null;

        for (var i = 0; i < tempKeys.length; i++) {
            var v = s[tempKeys[i]];
            if (v !== undefined && v !== null && Number(v) !== 0) {
                temp = Number(v);
                break;
            }
        }

        if (temp === null) return '';

        var className = '';
        if (temp >= 25) className = 'hot';
        else if (temp >= 20) className = 'warm';
        else if (temp < 15) className = 'cold';

        return '<span class="zbm-temp-mini ' + className + '" title="Temperature: ' + temp.toFixed(1) + '°C">' +
               '<i class="fas fa-thermometer-half"></i> ' + temp.toFixed(1) + '°</span>';
    }

    // ----------------------------------------------------------
    // 5. CHECK IF DEVICE IS A THERMOSTAT
    // ----------------------------------------------------------

    function isThermostat(device) {
        if (!device.capabilities || !Array.isArray(device.capabilities)) return false;
        return device.capabilities.some(function(ep) {
            return (ep.inputs || []).some(function(c) { return c.id === 0x0201; }) ||
                   (ep.outputs || []).some(function(c) { return c.id === 0x0201; });
        });
    }



    // ----------------------------------------------------------
    // 5b. ON/OFF STATUS BADGES (multi-EP aware)
    // ----------------------------------------------------------

    function getOnOffEndpoints(device) {
        if (!device.capabilities || !Array.isArray(device.capabilities)) return [];

        var capList = device.capability_list || [];
        // Skip pure sensor devices
        if (capList.indexOf('contact_sensor') !== -1 && capList.indexOf('switch') === -1 && capList.indexOf('light') === -1) return [];
        if (capList.indexOf('motion_sensor') !== -1 && capList.indexOf('switch') === -1 && capList.indexOf('light') === -1) return [];

        var eps = [];
        device.capabilities.forEach(function(ep) {
            // Must have OnOff (0x0006) in INPUT clusters
            var hasOnOffInput = (ep.inputs || []).some(function(c) { return c.id === 0x0006; });
            if (!hasOnOffInput) return;

            // Skip sensor-typed endpoints
            if (ep.component_type === 'sensor') return;

            eps.push(ep.id);
        });

        return eps;
    }

    function renderOnOffStatus(device) {
        // Skip thermostats — they have their own indicators
        if (isThermostat(device)) return '';

        var eps = getOnOffEndpoints(device);
        if (eps.length === 0) return '';

        var s = device.state || {};
        var html = '';
        var multiEp = eps.length > 1;

        eps.forEach(function(epId) {
            // Resolve on state: try ep-specific key first, then global for EP1
            var isOn;
            if (s['on_' + epId] !== undefined) {
                isOn = !!s['on_' + epId];
            } else if (epId === 1 && s.on !== undefined) {
                isOn = !!s.on;
            } else {
                // Check string state as fallback
                var stateKey = s['state_' + epId] || (epId === 1 ? s.state : undefined);
                if (stateKey !== undefined) {
                    isOn = String(stateKey).toUpperCase() === 'ON';
                } else {
                    return; // No state data for this EP
                }
            }

            var cls = isOn ? 'on' : 'off';
            var icon = isOn ? 'fa-toggle-on' : 'fa-toggle-off';
            var label = isOn ? 'ON' : 'OFF';
            var epLabel = multiEp ? 'EP' + epId + ' ' : '';
            var title = epLabel + (isOn ? 'On' : 'Off');

            html += '<span class="zbm-onoff-badge ' + cls + '" title="' + title + '">' +
                    '<i class="fas ' + icon + '"></i> ' + epLabel + label + '</span> ';
        });

        return html;
    }

    // ----------------------------------------------------------
    // 6. ENHANCE TABLE ROWS
    // ----------------------------------------------------------

    function enhanceDeviceTable() {
        // Access the global state which devices.js populates
        if (!window.state || !window.state.deviceCache) return;

        var tbody = document.getElementById('deviceTableBody');
        if (!tbody) return;

        var rows = tbody.querySelectorAll('tr');

        rows.forEach(function(tr) {
            // Skip already-enhanced rows
            if (tr.dataset.enhanced === 'true') return;

            // Use data-ieee attribute (works for both Zigbee and Matter)
            var ieee = tr.dataset.ieee;
            if (!ieee) return;

            var device = window.state.deviceCache[ieee];
            if (!device) return;

            tr.dataset.enhanced = 'true';

            // --- Enhance LQI column (6th column) ---
            var lqiCell = tr.querySelector('td.device-lqi');
            if (lqiCell && device.lqi !== undefined) {
                lqiCell.innerHTML = renderSignalBars(device.lqi);
            }

            // --- Enhance Status column (8th column) ---
            var statusCell = tr.querySelector('td.device-status-badges');
            if (statusCell) {
                var s = device.state || {};
                var isOnline = device.available !== false;
                var extras = '';

                // Battery
                var battery = s.battery || s.battery_percentage;
                if (battery !== undefined) {
                    extras += ' ' + renderBattery(battery);
                }

                // Thermostat heating status
                if (isThermostat(device)) {
                    extras += ' ' + renderHeatingStatus(device);
                    extras += ' ' + renderTempMini(device);
                }

                // Temperature sensors (not thermostats) — show temp
                if (!isThermostat(device) && (s.temperature !== undefined || s.local_temperature !== undefined)) {
                    extras += ' ' + renderTempMini(device);
                }

                // On/Off status (multi-EP aware)
                var onOffHtml = renderOnOffStatus(device);
                if (onOffHtml) {
                    extras += ' ' + onOffHtml;
                }

                // Contact sensor status
                var capList = device.capability_list || [];
                if (capList.indexOf('contact_sensor') !== -1) {
                    var isClosed = s.contact === true || s.is_open === false;
                    var contactLabel = isClosed ? 'CLOSED' : 'OPEN';
                    var contactClass = isClosed ? 'zbm-contact-closed' : 'zbm-contact-open';
                    extras += ' <span class="' + contactClass + '" title="Contact: ' + contactLabel + '">' +
                              '<i class="fas ' + (isClosed ? 'fa-door-closed' : 'fa-door-open') + '"></i> ' +
                              contactLabel + '</span>';
                }

                // Motion sensor status
                if (capList.indexOf('motion_sensor') !== -1 || capList.indexOf('occupancy_sensing') !== -1) {
                    var motion = s.occupancy === true || s.motion === true || s.presence === true;
                    var motionLabel = motion ? 'MOTION' : 'CLEAR';
                    var motionClass = motion ? 'zbm-motion-active' : 'zbm-motion-clear';
                    extras += ' <span class="' + motionClass + '" title="Motion: ' + motionLabel + '">' +
                              '<i class="fas ' + (motion ? 'fa-running' : 'fa-shield-alt') + '"></i> ' +
                              motionLabel + '</span>';
                }

                // Replace the simple Online/Offline badge with dot + extras
                var dotClass = isOnline ? 'online' : 'offline';
                if (isThermostat(device)) {
                    var rs = s.running_state || 0;
                    if ((rs & 0x0001) || String(rs).includes('heat')) {
                        dotClass = 'heating';
                    }
                }

                var statusLabel = isOnline ? 'Online' : 'Offline';
                var protocolBadge = '';
                if (device.protocol === 'matter') {
                    protocolBadge = '<span class="badge bg-info me-1" style="font-size:0.65rem">Matter</span>';
                }

                statusCell.innerHTML = '<div class="zbm-status-cell">' +
                    '<span class="zbm-status-dot ' + dotClass + '" title="' + statusLabel + '"></span>' +
                    '<span style="font-size:0.7rem;color:var(--text-muted, #6b7280)">' + statusLabel + '</span>' +
                    protocolBadge + extras +
                    '</div>';
            }
        });
    }

    window._enhanceDeviceTable = enhanceDeviceTable;

    // ----------------------------------------------------------
    // 7. OBSERVE TABLE CHANGES
    // ----------------------------------------------------------

    function init() {
        var tbody = document.getElementById('deviceTableBody');
        if (!tbody) {
            // Table not ready yet, retry
            setTimeout(init, 500);
            return;
        }

        // Enhance existing rows
        enhanceDeviceTable();

        // Watch for table re-renders (devices.js calls renderDeviceTable)
        var observer = new MutationObserver(function () {
            // Small delay to let devices.js finish rendering
            setTimeout(enhanceDeviceTable, 50);
        });

        observer.observe(tbody, { childList: true, subtree: false });
    }

    // ----------------------------------------------------------
    // 8. START
    // ----------------------------------------------------------

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            setTimeout(init, 300);
        });
    } else {
        setTimeout(init, 300);
    }

})();