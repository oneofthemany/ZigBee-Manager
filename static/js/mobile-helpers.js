/* ============================================================
   ZigBee Matter Manager — Mobile Touch Helpers
   ============================================================
*/

(function () {
    'use strict';

    // Only activate on touch-capable devices
    if (!('ontouchstart' in window || navigator.maxTouchPoints > 0)) return;

    /* ----------------------------------------------------------
       1. PULL-TO-REFRESH PREVENTION ON CONTROLS
       ---------------------------------------------------------- */

    document.addEventListener('touchstart', function (e) {
        var el = e.target;
        if (el.matches('input[type="range"]') ||
            el.closest('.modal-body') ||
            el.closest('[class*="thermostat"]') ||
            el.closest('[class*="climate"]') ||
            el.closest('[class*="slider"]') ||
            el.closest('.color-picker')) {
            document.body.classList.add('controlling');
        }
    }, { passive: true });

    document.addEventListener('touchend', function () {
        document.body.classList.remove('controlling');
    }, { passive: true });

    document.addEventListener('touchcancel', function () {
        document.body.classList.remove('controlling');
    }, { passive: true });


    /* ----------------------------------------------------------
       2. RANGE SLIDER — prevent page scroll during drag
       ---------------------------------------------------------- */

    document.addEventListener('touchstart', function (e) {
        if (e.target.matches('input[type="range"]')) {
            var slider = e.target;

            function blockScroll(ev) {
                ev.preventDefault();
            }

            function cleanup() {
                slider.removeEventListener('touchmove', blockScroll);
                slider.removeEventListener('touchend', cleanup);
                slider.removeEventListener('touchcancel', cleanup);
            }

            slider.addEventListener('touchmove', blockScroll, { passive: false });
            slider.addEventListener('touchend', cleanup);
            slider.addEventListener('touchcancel', cleanup);
        }
    }, { passive: true });


    /* ----------------------------------------------------------
       3. MODAL OVERSCROLL FIX
       ---------------------------------------------------------- */

    function fixModalOverscroll(el) {
        if (el._overscrollFixed) return;
        el._overscrollFixed = true;

        el.addEventListener('touchmove', function () {
            if (el.scrollTop <= 0) {
                el.scrollTop = 1;
            }
            if (el.scrollTop + el.clientHeight >= el.scrollHeight) {
                el.scrollTop = el.scrollHeight - el.clientHeight - 1;
            }
        }, { passive: true });
    }

    // Fix existing modals
    document.querySelectorAll('.modal-body').forEach(fixModalOverscroll);

    // Fix dynamically created modals (device modal, group modal, etc.)
    var observer = new MutationObserver(function (mutations) {
        for (var i = 0; i < mutations.length; i++) {
            var added = mutations[i].addedNodes;
            for (var j = 0; j < added.length; j++) {
                var node = added[j];
                if (node.nodeType !== 1) continue;
                if (node.classList && node.classList.contains('modal-body')) {
                    fixModalOverscroll(node);
                }
                if (node.querySelectorAll) {
                    node.querySelectorAll('.modal-body').forEach(fixModalOverscroll);
                }
            }
        }
    });
    observer.observe(document.body, { childList: true, subtree: true });


    /* ----------------------------------------------------------
       4. VIEWPORT HEIGHT CSS VARIABLE
       ---------------------------------------------------------- */

    function setVH() {
        document.documentElement.style.setProperty(
            '--vh', (window.innerHeight * 0.01) + 'px'
        );
    }
    setVH();
    window.addEventListener('resize', setVH);
    window.addEventListener('orientationchange', function () {
        setTimeout(setVH, 150);
    });


    /* ----------------------------------------------------------
       5. TAB SCROLL FADE INDICATORS
       ---------------------------------------------------------- */

    function updateTabScrollHint(tabBar) {
        var canLeft = tabBar.scrollLeft > 5;
        var canRight = tabBar.scrollLeft < (tabBar.scrollWidth - tabBar.clientWidth - 5);

        var mask = 'none';
        if (canLeft && canRight) {
            mask = 'linear-gradient(to right, transparent, black 24px, black calc(100% - 24px), transparent)';
        } else if (canRight) {
            mask = 'linear-gradient(to right, black calc(100% - 24px), transparent)';
        } else if (canLeft) {
            mask = 'linear-gradient(to right, transparent, black 24px)';
        }
        tabBar.style.webkitMaskImage = mask;
        tabBar.style.maskImage = mask;
    }

    function initTabScrollHints() {
        ['#mainTabs', '#settingsSubNav', '#devTabs'].forEach(function (sel) {
            var el = document.querySelector(sel);
            if (!el || el._scrollHint) return;
            el._scrollHint = true;
            el.addEventListener('scroll', function () { updateTabScrollHint(el); }, { passive: true });
            setTimeout(function () { updateTabScrollHint(el); }, 300);
        });
    }

    initTabScrollHints();

    // Re-init when modals open (for #devTabs inside device modal)
    var tabObserver = new MutationObserver(function () {
        setTimeout(initTabScrollHints, 200);
    });
    tabObserver.observe(document.body, { childList: true, subtree: true });


    /* ----------------------------------------------------------
       6. PREVENT DOUBLE-TAP ZOOM ON CONTROLS
       ---------------------------------------------------------- */

    var lastTap = 0;
    document.addEventListener('touchend', function (e) {
        var now = Date.now();
        if (now - lastTap < 300) {
            if (e.target.closest('button, .btn, input, select, .nav-link, a')) {
                e.preventDefault();
            }
        }
        lastTap = now;
    }, { passive: false });

})();