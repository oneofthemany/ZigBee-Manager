/**
 * MQTT Explorer Module
 * Real-time MQTT message monitoring and exploration
 */

import { state } from './state.js';

let mqttExplorerState = {
    monitoring: false,
    messages: [],
    topics: [],
    stats: {},
    autoScroll: true,
    filters: {
        topic: '',
        search: ''
    },
    // Configurable buffer size
    maxMessages: 500
};

/**
 * Initialize MQTT Explorer
 */
export function initMQTTExplorer() {
    console.log('Initializing MQTT Explorer...');

    // Set up event listeners
    setupEventListeners();

    // Load initial stats
    updateStats();

    // Start periodic stats refresh
    setInterval(updateStats, 2000);
}

/**
 * Set up event listeners for MQTT Explorer controls
 */
function setupEventListeners() {
    // Start/Stop monitoring
    const startBtn = document.getElementById('mqttStartBtn');
    const stopBtn = document.getElementById('mqttStopBtn');

    if (startBtn) {
        startBtn.addEventListener('click', startMonitoring);
    }
    if (stopBtn) {
        stopBtn.addEventListener('click', stopMonitoring);
    }

    // Clear messages
    const clearBtn = document.getElementById('mqttClearBtn');
    if (clearBtn) {
        clearBtn.addEventListener('click', clearMessages);
    }

    // Refresh messages
    const refreshBtn = document.getElementById('mqttRefreshBtn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', refreshMessages);
    }

    // Auto-scroll toggle
    const autoScrollCheck = document.getElementById('mqttAutoScroll');
    if (autoScrollCheck) {
        autoScrollCheck.addEventListener('change', (e) => {
            mqttExplorerState.autoScroll = e.target.checked;
        });
    }

    // Filter inputs
    const topicFilterInput = document.getElementById('mqttTopicFilter');
    if (topicFilterInput) {
        topicFilterInput.addEventListener('input', debounce(() => {
            mqttExplorerState.filters.topic = topicFilterInput.value;
            // We refresh from server on filter change to get history
            refreshMessages();
        }, 300));
    }

    const searchInput = document.getElementById('mqttSearchFilter');
    if (searchInput) {
        searchInput.addEventListener('input', debounce(() => {
            mqttExplorerState.filters.search = searchInput.value;
            // We refresh from server on filter change to get history
            refreshMessages();
        }, 300));
    }

    // Publish form
    const publishBtn = document.getElementById('mqttPublishBtn');
    if (publishBtn) {
        publishBtn.addEventListener('click', publishMessage);
    }
}

/**
 * Start MQTT monitoring
 */
async function startMonitoring() {
    try {
        const response = await fetch('/api/mqtt_explorer/start', { method: 'POST' });
        const data = await response.json();

        if (data.success) {
            mqttExplorerState.monitoring = true;
            updateUI();
            refreshMessages();
            showToast('MQTT Explorer started', 'success');
        } else {
            showToast(data.message || 'Failed to start monitoring', 'warning');
        }
    } catch (error) {
        console.error('Failed to start MQTT Explorer:', error);
        showToast('Failed to start monitoring', 'danger');
    }
}

/**
 * Stop MQTT monitoring
 */
async function stopMonitoring() {
    try {
        const response = await fetch('/api/mqtt_explorer/stop', { method: 'POST' });
        const data = await response.json();

        if (data.success) {
            mqttExplorerState.monitoring = false;
            updateUI();
            showToast('MQTT Explorer stopped', 'info');
        }
    } catch (error) {
        console.error('Failed to stop MQTT Explorer:', error);
        showToast('Failed to stop monitoring', 'danger');
    }
}

/**
 * Clear all messages
 */
async function clearMessages() {
    if (!confirm('Clear all MQTT messages?')) return;

    try {
        const response = await fetch('/api/mqtt_explorer/clear', { method: 'POST' });
        const data = await response.json();

        if (data.success) {
            mqttExplorerState.messages = [];
            renderMessages();
            showToast('Messages cleared', 'success');
        }
    } catch (error) {
        console.error('Failed to clear messages:', error);
        showToast('Failed to clear messages', 'danger');
    }
}

/**
 * Refresh messages from server
 */
async function refreshMessages() {
    try {
        const params = new URLSearchParams();
        if (mqttExplorerState.filters.topic) {
            params.append('topic', mqttExplorerState.filters.topic);
        }
        if (mqttExplorerState.filters.search) {
            params.append('search', mqttExplorerState.filters.search);
        }
        params.append('limit', mqttExplorerState.maxMessages.toString());

        const response = await fetch(`/api/mqtt_explorer/messages?${params}`);
        const data = await response.json();

        if (data.messages) {
            mqttExplorerState.messages = data.messages;
            renderMessages();
        }
    } catch (error) {
        console.error('Failed to refresh messages:', error);
    }
}

/**
 * Update statistics
 */
async function updateStats() {
    try {
        const response = await fetch('/api/mqtt_explorer/stats');
        const stats = await response.json();

        mqttExplorerState.stats = stats;
        mqttExplorerState.monitoring = stats.monitoring;

        updateStatsUI(stats);
        updateUI();
    } catch (error) {
        console.error('Failed to update stats:', error);
    }
}

/**
 * Update statistics UI
 */
function updateStatsUI(stats) {
    const statusBadge = document.getElementById('mqttStatusBadge');
    if (statusBadge) {
        if (stats.monitoring) {
            statusBadge.className = 'badge bg-success';
            statusBadge.textContent = 'Monitoring';
        } else {
            statusBadge.className = 'badge bg-secondary';
            statusBadge.textContent = 'Stopped';
        }
    }

    const totalEl = document.getElementById('mqttTotalMessages');
    if (totalEl) totalEl.textContent = stats.total_messages || 0;

    const rateEl = document.getElementById('mqttMessagesPerSec');
    if (rateEl) rateEl.textContent = stats.messages_per_second || '0.00';

    const uniqueEl = document.getElementById('mqttUniqueTopics');
    if (uniqueEl) uniqueEl.textContent = stats.unique_topics || 0;

    const bufferEl = document.getElementById('mqttBufferUsage');
    if (bufferEl) {
        // Display local buffer usage vs max
        bufferEl.textContent = `${mqttExplorerState.messages.length} / ${mqttExplorerState.maxMessages}`;
    }
}

/**
 * Update UI based on monitoring state
 */
function updateUI() {
    const startBtn = document.getElementById('mqttStartBtn');
    const stopBtn = document.getElementById('mqttStopBtn');

    if (mqttExplorerState.monitoring) {
        startBtn?.classList.add('d-none');
        stopBtn?.classList.remove('d-none');
    } else {
        startBtn?.classList.remove('d-none');
        stopBtn?.classList.add('d-none');
    }
}

/**
 * Helper: Check if topic matches MQTT filter pattern
 */
function topicMatches(topic, pattern) {
    if (!pattern || pattern === '#') return true;

    const topicParts = topic.split('/');
    const patternParts = pattern.split('/');

    // Multi-level wildcard at end
    if (patternParts[patternParts.length - 1] === '#') {
        if (topicParts.length < patternParts.length - 1) return false;
        for (let i = 0; i < patternParts.length - 1; i++) {
            if (patternParts[i] !== '+' && patternParts[i] !== topicParts[i]) return false;
        }
        return true;
    }

    // Exact length check for non-# patterns
    if (topicParts.length !== patternParts.length) return false;

    for (let i = 0; i < topicParts.length; i++) {
        if (patternParts[i] !== '+' && patternParts[i] !== topicParts[i]) return false;
    }

    return true;
}

/**
 * Render messages in the table
 */
function renderMessages() {
    const tbody = document.getElementById('mqttMessagesBody');
    if (!tbody) return;

    // Filter messages for display
    const displayMessages = mqttExplorerState.messages.filter(msg => {
        // Topic filter
        if (mqttExplorerState.filters.topic && !topicMatches(msg.topic, mqttExplorerState.filters.topic)) {
            return false;
        }
        // Search filter
        if (mqttExplorerState.filters.search) {
            const searchLower = mqttExplorerState.filters.search.toLowerCase();
            const topicMatch = msg.topic.toLowerCase().includes(searchLower);
            const payloadMatch = (msg.payload_raw || "").toLowerCase().includes(searchLower);
            if (!topicMatch && !payloadMatch) return false;
        }
        return true;
    });

    if (displayMessages.length === 0) {
        // Show different empty state depending on whether we have hidden data or no data
        const hasHiddenData = mqttExplorerState.messages.length > 0;

        tbody.innerHTML = `
            <tr>
                <td colspan="5" class="text-center text-muted">
                    ${hasHiddenData
                        ? 'No messages match your filters'
                        : (mqttExplorerState.monitoring ? 'Waiting for messages...' : 'Start monitoring to see messages')}
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = '';

    displayMessages.forEach((msg) => {
        const tr = document.createElement('tr');
        tr.className = 'mqtt-message-row';

        // Format timestamp
        const time = new Date(msg.timestamp * 1000);
        const timeStr = time.toLocaleTimeString('en-GB', {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            fractionalSecondDigits: 3
        });

        // Truncate payload for display
        const payloadDisplay = msg.payload_raw.length > 100
            ? msg.payload_raw.substring(0, 100) + '...'
            : msg.payload_raw;

        // Format size
        const sizeStr = msg.size < 1024
            ? `${msg.size}B`
            : `${(msg.size / 1024).toFixed(2)}KB`;

        tr.innerHTML = `
            <td class="font-monospace small">${timeStr}</td>
            <td class="font-monospace small">
                <span class="badge bg-info" title="${msg.topic}">${msg.topic}</span>
            </td>
            <td class="font-monospace small text-truncate" style="max-width: 300px;">
                ${escapeHtml(payloadDisplay)}
            </td>
            <td class="text-center small">
                <span class="badge bg-${msg.qos === 0 ? 'secondary' : msg.qos === 1 ? 'primary' : 'warning'}">
                    QoS ${msg.qos}
                </span>
                ${msg.retain ? '<span class="badge bg-warning ms-1">R</span>' : ''}
            </td>
            <td class="text-end small">${sizeStr}</td>
        `;

        // Make row clickable to show details
        tr.addEventListener('click', () => showMessageDetails(msg));
        tr.style.cursor = 'pointer';

        tbody.appendChild(tr);
    });

    // Auto-scroll if enabled
    if (mqttExplorerState.autoScroll) {
        const container = document.getElementById('mqttMessagesContainer');
        if (container) {
            container.scrollTop = 0;
        }
    }
}

/**
 * Show message details in modal
 */
function showMessageDetails(msg) {
    const modal = new bootstrap.Modal(document.getElementById('mqttMessageModal'));

    // Set modal title
    document.getElementById('mqttMessageModalTitle').textContent = msg.topic;

    // Set message details
    const detailsDiv = document.getElementById('mqttMessageDetails');

    const time = new Date(msg.timestamp * 1000);

    let formattedPayload = msg.payload_raw;
    if (msg.payload_parsed) {
        formattedPayload = JSON.stringify(msg.payload_parsed, null, 2);
    }

    detailsDiv.innerHTML = `
        <div class="mb-3">
            <h6>Timestamp</h6>
            <div class="font-monospace">${time.toISOString()}</div>
        </div>
        <div class="mb-3">
            <h6>Topic</h6>
            <div class="font-monospace">${escapeHtml(msg.topic)}</div>
        </div>
        <div class="mb-3">
            <h6>Properties</h6>
            <div>
                <span class="badge bg-${msg.qos === 0 ? 'secondary' : msg.qos === 1 ? 'primary' : 'warning'}">
                    QoS ${msg.qos}
                </span>
                ${msg.retain ? '<span class="badge bg-warning ms-1">Retained</span>' : '<span class="badge bg-secondary ms-1">Not Retained</span>'}
                <span class="badge bg-info ms-1">${msg.size} bytes</span>
            </div>
        </div>
        <div class="mb-3">
            <h6>Payload</h6>
            <pre class="bg-light p-2 rounded" style="max-height: 400px; overflow-y: auto;"><code>${escapeHtml(formattedPayload)}</code></pre>
        </div>
    `;

    modal.show();
}

/**
 * Publish a test message
 */
async function publishMessage() {
    const topic = document.getElementById('mqttPublishTopic').value;
    const payload = document.getElementById('mqttPublishPayload').value;
    const qos = parseInt(document.getElementById('mqttPublishQos').value);
    const retain = document.getElementById('mqttPublishRetain').checked;

    if (!topic) {
        showToast('Topic required', 'warning');
        return;
    }

    try {
        const response = await fetch('/api/mqtt_explorer/publish', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ topic, payload, qos, retain })
        });

        const data = await response.json();

        if (data.success) {
            showToast('Message published', 'success');
            // Clear the form
            document.getElementById('mqttPublishPayload').value = '';
        } else {
            showToast(data.message || 'Publish failed', 'danger');
        }
    } catch (error) {
        console.error('Failed to publish message:', error);
        showToast('Failed to publish message', 'danger');
    }
}

/**
 * Handle incoming WebSocket MQTT messages
 */
export function handleMQTTMessage(message) {
    // Rotating Buffer: Add to beginning (newest first)
    mqttExplorerState.messages.unshift(message);

    // Rotating Buffer
    if (mqttExplorerState.messages.length > mqttExplorerState.maxMessages) {
        mqttExplorerState.messages = mqttExplorerState.messages.slice(0, mqttExplorerState.maxMessages);
    }

    renderMessages();
}

/**
 * Utility: Debounce function
 */
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

/**
 * Utility: Escape HTML
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Utility: Show toast notification
 */
function showToast(message, type = 'info') {
    // Simple console log fallback if no toast system
    console.log(`[${type}] ${message}`);
}

// Export for use in main.js
export { mqttExplorerState };