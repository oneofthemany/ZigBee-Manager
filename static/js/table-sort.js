/**
 * Table Sorting Module
 * Based on ZHA patterns and modern JavaScript best practices
 * Provides sortable table functionality with multi-type column support
 */

/**
 * Sort state management
 */
const sortState = {
    column: null,
    direction: 'asc', // 'asc' or 'desc'
    type: 'string'    // 'string', 'number', 'boolean', 'date'
};

/**
 * Type-specific comparison functions
 */
const comparators = {
    /**
     * String comparison (case-insensitive, natural sort)
     */
    string: (a, b, direction) => {
        const valA = String(a || '').toLowerCase();
        const valB = String(b || '').toLowerCase();

        // Natural sort for strings containing numbers
        const result = valA.localeCompare(valB, undefined, {
            numeric: true,
            sensitivity: 'base'
        });

        return direction === 'asc' ? result : -result;
    },

    /**
     * Numeric comparison
     */
    number: (a, b, direction) => {
        const numA = parseFloat(a);
        const numB = parseFloat(b);

        // Handle NaN values - push to end
        if (isNaN(numA) && isNaN(numB)) return 0;
        if (isNaN(numA)) return 1;
        if (isNaN(numB)) return -1;

        const result = numA - numB;
        return direction === 'asc' ? result : -result;
    },

    /**
     * Boolean comparison (true > false)
     */
    boolean: (a, b, direction) => {
        const boolA = Boolean(a);
        const boolB = Boolean(b);

        if (boolA === boolB) return 0;
        const result = boolA ? 1 : -1;
        return direction === 'asc' ? result : -result;
    },

    /**
     * Date/timestamp comparison
     */
    date: (a, b, direction) => {
        const dateA = new Date(a).getTime();
        const dateB = new Date(b).getTime();

        // Handle invalid dates
        if (isNaN(dateA) && isNaN(dateB)) return 0;
        if (isNaN(dateA)) return 1;
        if (isNaN(dateB)) return -1;

        const result = dateA - dateB;
        return direction === 'asc' ? result : -result;
    }
};

/**
 * Extract sortable value from device object based on column name
 */
function extractValue(device, column) {
    switch(column) {
        case 'type':
            return device.type || '';
        case 'friendly_name':
            return device.friendly_name || '';
        case 'ieee':
            return device.ieee || '';
        case 'manufacturer':
            return device.manufacturer || '';
        case 'model':
            return device.model || '';
        case 'lqi':
            // Handle LQI which might be undefined
            return device.lqi !== undefined ? device.lqi : -1;
        case 'last_seen_ts':
            return device.last_seen_ts || 0;
        case 'available':
            return device.available !== false; // Default to true if undefined
        default:
            return '';
    }
}

/**
 * Sort devices array by specified column and direction
 */
export function sortDevices(devices, column, type, direction) {
    if (!devices || devices.length === 0) return devices;

    const comparator = comparators[type] || comparators.string;

    return [...devices].sort((a, b) => {
        const valueA = extractValue(a, column);
        const valueB = extractValue(b, column);
        return comparator(valueA, valueB, direction);
    });
}

/**
 * Update sort indicators in table headers
 */
function updateSortIndicators(clickedHeader) {
    // Remove all sort classes from headers
    document.querySelectorAll('.sortable-header').forEach(header => {
        header.classList.remove('sort-asc', 'sort-desc');
    });

    // Add appropriate class to clicked header
    if (sortState.direction === 'asc') {
        clickedHeader.classList.add('sort-asc');
    } else {
        clickedHeader.classList.add('sort-desc');
    }
}

/**
 * Initialize table sorting
 * Attaches click handlers to sortable headers
 */
export function initTableSort(onSortCallback) {
    console.log("Initialising table sort functionality...");

    const headers = document.querySelectorAll('.sortable-header');

    headers.forEach(header => {
        header.addEventListener('click', () => {
            const column = header.getAttribute('data-column');
            const type = header.getAttribute('data-type') || 'string';

            // Toggle direction if clicking same column, otherwise reset to ascending
            if (sortState.column === column) {
                sortState.direction = sortState.direction === 'asc' ? 'desc' : 'asc';
            } else {
                sortState.column = column;
                sortState.type = type;
                sortState.direction = 'asc';
            }

            console.log(`Sorting by ${column} (${type}) in ${sortState.direction} order`);

            // Update visual indicators
            updateSortIndicators(header);

            // Trigger callback to re-render table with sorted data
            if (onSortCallback) {
                onSortCallback(column, type, sortState.direction);
            }
        });
    });

    console.log(`Table sort initialised for ${headers.length} columns`);
}

/**
 * Get current sort state
 */
export function getSortState() {
    return { ...sortState };
}

/**
 * Reset sort state to default
 */
export function resetSortState() {
    sortState.column = null;
    sortState.direction = 'asc';
    sortState.type = 'string';

    // Remove all sort classes
    document.querySelectorAll('.sortable-header').forEach(header => {
        header.classList.remove('sort-asc', 'sort-desc');
    });
}

/**
 * Apply sorting with current state
 * Useful for re-applying sort after data update
 */
export function applySortState(devices) {
    if (!sortState.column) {
        return devices; // No sorting applied
    }

    return sortDevices(devices, sortState.column, sortState.type, sortState.direction);
}
