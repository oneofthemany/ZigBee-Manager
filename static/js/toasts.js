/* ============================================================
   ZigBee Matter Manager — Toast Notification System
   ============================================================ */

(function () {
    'use strict';

    // ----------------------------------------------------------
    // 1. CREATE TOAST CONTAINER
    // ----------------------------------------------------------

    var container;

    function ensureContainer() {
        if (container && document.body.contains(container)) return container;
        container = document.createElement('div');
        container.id = 'zbm-toast-container';
        document.body.appendChild(container);
        return container;
    }

    // ----------------------------------------------------------
    // 2. ICON MAP
    // ----------------------------------------------------------

    var ICONS = {
        success: '<i class="fas fa-check-circle"></i>',
        error:   '<i class="fas fa-exclamation-circle"></i>',
        warning: '<i class="fas fa-exclamation-triangle"></i>',
        info:    '<i class="fas fa-info-circle"></i>'
    };

    var TITLES = {
        success: 'Success',
        error:   'Error',
        warning: 'Warning',
        info:    'Info'
    };

    // ----------------------------------------------------------
    // 3. CREATE TOAST ELEMENT
    // ----------------------------------------------------------

    function createToast(type, message, options) {
        options = options || {};
        var duration = options.duration || (type === 'error' ? 6000 : 4000);
        var title = options.title || TITLES[type];

        ensureContainer();

        var toast = document.createElement('div');
        toast.className = 'zbm-toast zbm-toast-' + type;

        // Handle multi-line messages (from alert() calls that use \n)
        var formattedMessage = String(message).replace(/\n/g, '<br>');

        toast.innerHTML =
            '<span class="zbm-toast-icon">' + ICONS[type] + '</span>' +
            '<div class="zbm-toast-body">' +
                '<div class="zbm-toast-title">' + title + '</div>' +
                '<div class="zbm-toast-message">' + formattedMessage + '</div>' +
            '</div>' +
            '<button class="zbm-toast-close" aria-label="Close">&times;</button>';

        // Click to dismiss
        toast.addEventListener('click', function () {
            dismissToast(toast);
        });

        // Close button
        toast.querySelector('.zbm-toast-close').addEventListener('click', function (e) {
            e.stopPropagation();
            dismissToast(toast);
        });

        container.appendChild(toast);

        // Auto-dismiss
        var timer = setTimeout(function () {
            dismissToast(toast);
        }, duration);

        // Pause timer on hover
        toast.addEventListener('mouseenter', function () {
            clearTimeout(timer);
        });

        toast.addEventListener('mouseleave', function () {
            timer = setTimeout(function () {
                dismissToast(toast);
            }, 2000);
        });

        // Limit max visible toasts
        var toasts = container.querySelectorAll('.zbm-toast:not(.zbm-toast-out)');
        if (toasts.length > 5) {
            dismissToast(toasts[0]);
        }

        return toast;
    }

    function dismissToast(toast) {
        if (!toast || toast.classList.contains('zbm-toast-out')) return;
        toast.classList.add('zbm-toast-out');
        setTimeout(function () {
            if (toast.parentNode) {
                toast.parentNode.removeChild(toast);
            }
        }, 300);
    }

    // ----------------------------------------------------------
    // 4. PUBLIC API
    // ----------------------------------------------------------

    window.toast = {
        success: function (msg, opts) { return createToast('success', msg, opts); },
        error:   function (msg, opts) { return createToast('error', msg, opts); },
        warning: function (msg, opts) { return createToast('warning', msg, opts); },
        info:    function (msg, opts) { return createToast('info', msg, opts); }
    };

    // ----------------------------------------------------------
    // 5. OVERRIDE window.alert()
    // ----------------------------------------------------------
    //
    // Heuristic: detect the type from the message content.
    // - Messages containing "error", "fail", "invalid" → error toast
    // - Messages containing "success", "done", "✓", "saved" → success toast
    // - Messages containing "warning", "caution", "banned" → warning toast
    // - Everything else → info toast
    //

    var _nativeAlert = window.alert.bind(window);

    window.alert = function (msg) {
        if (msg === undefined || msg === null) msg = '';
        var text = String(msg).toLowerCase();

        if (text.match(/error|fail|invalid|could not|unable|exception/)) {
            window.toast.error(msg);
        } else if (text.match(/success|done|✓|saved|complete|uploaded|applied|removed|enabled/)) {
            window.toast.success(msg);
        } else if (text.match(/warning|caution|banned|disconnect/)) {
            window.toast.warning(msg);
        } else {
            window.toast.info(msg);
        }
    };

    // Expose native alert in case it's ever needed
    window.nativeAlert = _nativeAlert;

})();
