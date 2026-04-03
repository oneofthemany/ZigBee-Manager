/* ============================================================
   ZigBee Matter Manager — Device Search / Filter
   ============================================================ */

(function () {
    'use strict';

    var searchInput = null;
    var clearBtn = null;
    var resultCount = null;

    // ----------------------------------------------------------
    // 1. INJECT SEARCH BOX INTO DEVICE CARD HEADER
    // ----------------------------------------------------------

    function init() {
        var cardHeader = document.querySelector('#devices .card-header .row');
        if (!cardHeader) {
            setTimeout(init, 500);
            return;
        }

        // Don't inject twice
        if (document.getElementById('zbm-device-search')) return;

        // Create the search column
        var col = document.createElement('div');
        col.className = 'col-auto';
        col.style.cssText = 'flex: 1; min-width: 150px; max-width: 300px;';

        col.innerHTML =
            '<div class="input-group input-group-sm">' +
                '<span class="input-group-text" style="background:transparent;border-right:none;">' +
                    '<i class="fas fa-search text-muted" style="font-size:0.75rem"></i>' +
                '</span>' +
                '<input type="text" id="zbm-device-search" class="form-control form-control-sm" ' +
                    'placeholder="Search devices..." ' +
                    'style="border-left:none; font-size:0.8rem;" ' +
                    'autocomplete="off" spellcheck="false">' +
                '<button id="zbm-search-clear" class="btn btn-outline-secondary btn-sm" ' +
                    'style="display:none; font-size:0.7rem; padding:0.15rem 0.4rem;" ' +
                    'type="button" title="Clear search">' +
                    '<i class="fas fa-times"></i>' +
                '</button>' +
            '</div>' +
            '<div id="zbm-search-count" class="small text-muted mt-1" style="display:none; font-size:0.7rem;"></div>';

        // Insert after the tab filter col
        var tabFilterCol = cardHeader.querySelector('.col-auto:nth-child(2)');
        if (tabFilterCol && tabFilterCol.nextSibling) {
            cardHeader.insertBefore(col, tabFilterCol.nextSibling);
        } else {
            // Insert before the ms-auto button
            var autoCol = cardHeader.querySelector('.col-auto.ms-auto');
            if (autoCol) {
                cardHeader.insertBefore(col, autoCol);
            } else {
                cardHeader.appendChild(col);
            }
        }

        searchInput = document.getElementById('zbm-device-search');
        clearBtn = document.getElementById('zbm-search-clear');
        resultCount = document.getElementById('zbm-search-count');

        // ----------------------------------------------------------
        // 2. BIND EVENTS
        // ----------------------------------------------------------

        searchInput.addEventListener('input', debounce(filterDevices, 150));

        searchInput.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                clearSearch();
                searchInput.blur();
            }
        });

        clearBtn.addEventListener('click', function () {
            clearSearch();
            searchInput.focus();
        });

        // Global keyboard shortcut: Ctrl+F or / to focus search
        document.addEventListener('keydown', function (e) {
            // Only activate when devices tab is visible
            var devicesTab = document.getElementById('devices');
            if (!devicesTab || !devicesTab.classList.contains('active') && !devicesTab.classList.contains('show')) return;

            // Don't capture if already in an input
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

            if (e.key === '/' || (e.ctrlKey && e.key === 'f')) {
                e.preventDefault();
                searchInput.focus();
                searchInput.select();
            }
        });
    }

    // ----------------------------------------------------------
    // 3. FILTER LOGIC
    // ----------------------------------------------------------

    function filterDevices() {
        var query = searchInput.value.trim().toLowerCase();

        // Show/hide clear button
        clearBtn.style.display = query ? 'block' : 'none';

        var tbody = document.getElementById('deviceTableBody');
        if (!tbody) return;

        var rows = tbody.querySelectorAll('tr');
        var visibleCount = 0;
        var totalCount = 0;

        rows.forEach(function (row) {
            // Skip "no devices" placeholder rows
            if (row.querySelector('td[colspan]')) return;
            totalCount++;

            if (!query) {
                row.style.display = '';
                visibleCount++;
                // Remove highlights
                removeHighlights(row);
                return;
            }

            // Gather searchable text from each cell
            var cells = row.querySelectorAll('td');
            var searchableText = '';

            cells.forEach(function (cell) {
                searchableText += ' ' + cell.textContent;
            });

            searchableText = searchableText.toLowerCase();

            // Support multiple search terms (space-separated AND logic)
            var terms = query.split(/\s+/).filter(function(t) { return t.length > 0; });
            var match = terms.every(function (term) {
                return searchableText.indexOf(term) !== -1;
            });

            if (match) {
                row.style.display = '';
                visibleCount++;
                highlightMatches(row, terms);
            } else {
                row.style.display = 'none';
                removeHighlights(row);
            }
        });

        // Show result count when filtering
        if (query && totalCount > 0) {
            resultCount.style.display = 'block';
            resultCount.textContent = visibleCount + ' of ' + totalCount + ' devices';
            if (visibleCount === 0) {
                resultCount.innerHTML = '<span style="color:var(--bs-danger, #dc3545)">No matches found</span>';
            }
        } else {
            resultCount.style.display = 'none';
        }
    }

    function clearSearch() {
        searchInput.value = '';
        clearBtn.style.display = 'none';
        resultCount.style.display = 'none';

        var tbody = document.getElementById('deviceTableBody');
        if (tbody) {
            tbody.querySelectorAll('tr').forEach(function (row) {
                row.style.display = '';
                removeHighlights(row);
            });
        }
    }

    // ----------------------------------------------------------
    // 4. HIGHLIGHT MATCHES
    // ----------------------------------------------------------

    function highlightMatches(row, terms) {
        // Only highlight in the name, IEEE, vendor, model cells (2nd-5th columns)
        var cells = row.querySelectorAll('td');
        for (var i = 1; i < Math.min(cells.length, 6); i++) {
            var cell = cells[i];
            // Walk text nodes only
            walkTextNodes(cell, function (textNode) {
                var text = textNode.textContent;
                var lower = text.toLowerCase();
                var hasMatch = terms.some(function (t) { return lower.indexOf(t) !== -1; });

                if (hasMatch) {
                    var span = document.createElement('span');
                    span.innerHTML = highlightText(text, terms);
                    textNode.parentNode.replaceChild(span, textNode);
                }
            });
        }
    }

    function highlightText(text, terms) {
        // Build a regex that matches any of the terms
        var escaped = terms.map(function (t) {
            return t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        });
        var regex = new RegExp('(' + escaped.join('|') + ')', 'gi');
        return text.replace(regex, '<mark style="background:#fef08a;padding:0 1px;border-radius:2px;">$1</mark>');
    }

    function removeHighlights(row) {
        var marks = row.querySelectorAll('mark');
        marks.forEach(function (mark) {
            var parent = mark.parentNode;
            parent.replaceChild(document.createTextNode(mark.textContent), mark);
            parent.normalize();
        });
        // Also remove wrapper spans we may have inserted
        row.querySelectorAll('span:not([class])').forEach(function (span) {
            if (span.children.length === 0 && !span.dataset.epBadge) {
                // Only unwrap if it looks like our highlight wrapper
                var parent = span.parentNode;
                while (span.firstChild) {
                    parent.insertBefore(span.firstChild, span);
                }
                parent.removeChild(span);
                parent.normalize();
            }
        });
    }

    function walkTextNodes(element, callback) {
        var walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT, null, false);
        var nodes = [];
        while (walker.nextNode()) {
            if (walker.currentNode.textContent.trim()) {
                nodes.push(walker.currentNode);
            }
        }
        // Process in reverse to avoid issues with DOM mutation
        nodes.reverse().forEach(callback);
    }

    // ----------------------------------------------------------
    // 5. RE-APPLY FILTER AFTER TABLE RE-RENDERS
    // ----------------------------------------------------------

    function watchTableChanges() {
        var tbody = document.getElementById('deviceTableBody');
        if (!tbody) {
            setTimeout(watchTableChanges, 500);
            return;
        }

        var observer = new MutationObserver(function () {
            // Re-apply current filter after table re-renders
            if (searchInput && searchInput.value.trim()) {
                setTimeout(filterDevices, 100);
            }
        });

        observer.observe(tbody, { childList: true });
    }

    // ----------------------------------------------------------
    // 6. UTILITY
    // ----------------------------------------------------------

    function debounce(fn, delay) {
        var timer;
        return function () {
            clearTimeout(timer);
            timer = setTimeout(fn, delay);
        };
    }

    // ----------------------------------------------------------
    // 7. START
    // ----------------------------------------------------------

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            setTimeout(init, 400);
            setTimeout(watchTableChanges, 600);
        });
    } else {
        setTimeout(init, 400);
        setTimeout(watchTableChanges, 600);
    }

})();
