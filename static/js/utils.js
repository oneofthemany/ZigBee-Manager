/**
 * Utility Functions
 * General-purpose helpers used across the application
 */

/**
 * Get icon for device type
 */
export function getTypeIcon(type) {
    if (type === 'Coordinator') return '<i class="fas fa-network-wired text-primary" title="Coordinator"></i>';
    if (type === 'Matter') return '<i class="fas fa-atom text-info"></i>';
    // Changed fa-wifi to fa-plug to better represent mains-powered devices
    if (type === 'Router') return '<i class="fas fa-plug text-success" title="Router (Mains)"></i>';
    return '<i class="fas fa-battery-three-quarters text-warning" title="End Device (Battery)"></i>';
}

/**
 * Get colored badge for LQI value
 */
export function getLqiBadge(lqi) {
    let color = 'bg-secondary';
    if (lqi > 150) color = 'bg-success';
    else if (lqi > 80) color = 'bg-warning text-dark';
    else if (lqi > 0) color = 'bg-danger';
    return `<span class="badge ${color}">${lqi}</span>`;
}

/**
 * Format timestamp as relative time
 */
export function timeAgo(ts) {
    if (!ts) return "Never";
    const seconds = Math.floor((Date.now() - ts) / 1000);
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    return `${Math.floor(seconds / 3600)}h ago`;
}

/**
 * Format timestamp as a readable date string
 */
export function formatTime(ts) {
    if (!ts) return "Never";
    return new Date(ts).toLocaleString();
}

/**
 * Update all last-seen time displays
 */
export function updateLastSeenTimes() {
    document.querySelectorAll('.last-seen').forEach(cell => {
        const ts = parseInt(cell.getAttribute('data-ts'));
        if (ts) {
            cell.innerText = timeAgo(ts);
        }
    });
}

/**
 * Get formatted current timestamp string [HH:MM:SS.mmm]
 */
export function getTimestamp() {
    const now = new Date();
    const pad = (n, width = 2) => n.toString().padStart(width, '0');
    return `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}.${pad(now.getMilliseconds(), 3)}`;
}


/**
 * Show a toast notification
 * @param {string} message - The message to display
 * @param {string} type - 'success', 'danger', 'info', 'warning' (default: info)
 */
export function showToast(message, type = 'info') {
    // Create toast container if it doesn't exist
    let toastContainer = document.getElementById('toast-container');
    if (!toastContainer) {
        toastContainer = document.createElement('div');
        toastContainer.id = 'toast-container';
        toastContainer.className = 'toast-container position-fixed bottom-0 end-0 p-3';
        toastContainer.style.zIndex = '1055';
        document.body.appendChild(toastContainer);
    }

    // Map types to Bootstrap colors if needed (e.g. 'error' -> 'danger')
    const typeMap = {
        'error': 'danger',
        'success': 'success',
        'warning': 'warning',
        'info': 'info'
    };
    const bsType = typeMap[type] || type;

    // Create unique ID
    const toastId = 'toast-' + Date.now();

    // Create toast HTML
    const toastHtml = `
        <div id="${toastId}" class="toast align-items-center text-white bg-${bsType} border-0" role="alert" aria-live="assertive" aria-atomic="true">
            <div class="d-flex">
                <div class="toast-body">
                    ${message}
                </div>
                <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
            </div>
        </div>
    `;

    // Append to container
    const wrapper = document.createElement('div');
    wrapper.innerHTML = toastHtml;
    const toastEl = wrapper.firstElementChild;
    toastContainer.appendChild(toastEl);

    // Initialize and show using Bootstrap API
    // (Assuming bootstrap is loaded globally via <script> tag)
    if (window.bootstrap) {
        const toast = new window.bootstrap.Toast(toastEl, { delay: 3000 });
        toast.show();

        // Remove from DOM after hidden
        toastEl.addEventListener('hidden.bs.toast', () => {
            toastEl.remove();
        });
    } else {
        // Fallback if Bootstrap JS isn't loaded
        toastEl.style.display = 'block';
        setTimeout(() => {
            toastEl.remove();
        }, 3000);
    }
}

// Color mode toggle
window.showColorMode = function(ieee, epId, mode) {
    const tempPanel = document.getElementById(`colorTempPanel_${epId}`);
    const colorPanel = document.getElementById(`colorPickerPanel_${epId}`);
    if (mode === 'temp') {
        if (tempPanel) tempPanel.style.display = '';
        if (colorPanel) colorPanel.style.display = 'none';
    } else {
        if (tempPanel) tempPanel.style.display = 'none';
        if (colorPanel) colorPanel.style.display = '';
    }
};

// Convert HSL to hex for color picker
window.hslToHex = function(h, s, l) {
    s /= 100;
    l /= 100;
    const a = s * Math.min(l, 1 - l);
    const f = n => {
        const k = (n + h / 30) % 12;
        const color = l - a * Math.max(Math.min(k - 3, 9 - k, 1), -1);
        return Math.round(255 * color).toString(16).padStart(2, '0');
    };
    return `#${f(0)}${f(8)}${f(4)}`;
};

// Convert hex to HS
window.hexToHS = function(hex) {
    const r = parseInt(hex.slice(1, 3), 16) / 255;
    const g = parseInt(hex.slice(3, 5), 16) / 255;
    const b = parseInt(hex.slice(5, 7), 16) / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    let h, s;
    const d = max - min;
    s = max === 0 ? 0 : d / max;
    if (max === min) {
        h = 0;
    } else {
        switch (max) {
            case r: h = (g - b) / d + (g < b ? 6 : 0); break;
            case g: h = (b - r) / d + 2; break;
            case b: h = (r - g) / d + 4; break;
        }
        h /= 6;
    }
    return { h: Math.round(h * 360), s: Math.round(s * 100) };
};

// Interaction debounce for color controls (prevents modal refresh during use)
let colorInteractionTimeout = null;
function setColorInteractionActive() {
    // Use the shared state if available, otherwise create local flag
    if (window.state) {
        window.state.controlInteractionActive = true;
    }
    if (colorInteractionTimeout) clearTimeout(colorInteractionTimeout);
    colorInteractionTimeout = setTimeout(() => {
        if (window.state) {
            window.state.controlInteractionActive = false;
        }
    }, 1000); // Longer timeout for color picking
}

// Send color from picker (converts hex to HS)
window.sendColorFromPicker = function(ieee, hexColor, epId) {
    setColorInteractionActive();

    const hs = window.hexToHS(hexColor);

    // Optimistic UI update - update saturation slider to match picked color
    const satSlider = document.getElementById(`satSlider_${ieee}_${epId}`);
    if (satSlider) {
        satSlider.value = hs.s;
    }

    // Send command
    window.sendCommand(ieee, 'hs_color', [hs.h, hs.s], epId);
};

// Send HS color command
window.sendHSColor = function(ieee, hue, sat, epId) {
    setColorInteractionActive();

    // Get current values if one is null
    if (hue === null || hue === undefined) {
        const picker = document.getElementById(`colorPicker_${ieee}_${epId}`);
        if (picker) {
            const hs = window.hexToHS(picker.value);
            hue = hs.h;
        } else {
            hue = 0;
        }
    }
    if (sat === null || sat === undefined) {
        const slider = document.getElementById(`satSlider_${ieee}_${epId}`);
        if (slider) {
            sat = parseInt(slider.value);
        } else {
            sat = 100;
        }
    }

    // Optimistic UI update - update color picker to reflect new HS values
    const picker = document.getElementById(`colorPicker_${ieee}_${epId}`);
    if (picker) {
        const newHex = window.hslToHex(hue, sat, 50);
        picker.value = newHex;
    }

    // Send via REST API
    window.sendCommand(ieee, 'hs_color', [hue, sat], epId);
};