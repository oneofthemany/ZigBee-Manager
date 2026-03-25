/**
 * setup-jedi-bee.js
 * ZigBee Matter Manager - Jedi Bee startup animation
 *
 * Canvas-rendered bee (matching deploy-animation.js quality) with:
 *   - Jedi hood/robe
 *   - Green lightsaber with glow + flicker
 *   - Periodic Force wave mind-trick effect
 *   - Floating force/saber sparkle particles
 *   - Gentle hover float
 *   - Jedi-themed status text rotation
 *
 * Drop-in: static/js/setup-jedi-bee.js
 * Load in index.html BEFORE setup-wizard.js
 */

(function () {
    'use strict';

    window.getJediBeeHTML = function () {
        return '<div id="jediBeeContainer" style="position:relative;width:100%;height:200px;overflow:hidden;margin:-8px 0 4px;">'
            + '<canvas id="jediBeeCanvas" style="position:absolute;top:0;left:0;width:100%;height:100%;"></canvas>'
            + '<div id="jediBeeText" style="position:absolute;bottom:10px;left:50%;transform:translateX(-50%);'
            + 'font-size:12px;color:var(--bs-secondary-color,#6c757d);font-style:italic;'
            + 'white-space:nowrap;opacity:0;transition:opacity 0.6s;font-family:Georgia,serif;'
            + 'text-shadow:0 0 8px var(--bs-body-bg,#fff);"></div>'
            + '</div>';
    };

    var particles = [];

    function spawnForceParticles(cx, cy, count) {
        for (var i = 0; i < count; i++) {
            var angle = Math.random() * Math.PI * 2;
            var speed = 0.8 + Math.random() * 2.5;
            particles.push({
                x: cx + (Math.random() - 0.5) * 24,
                y: cy + (Math.random() - 0.5) * 20,
                vx: Math.cos(angle) * speed,
                vy: Math.sin(angle) * speed,
                life: 1,
                decay: 0.012 + Math.random() * 0.012,
                size: 2 + Math.random() * 4,
                type: Math.random() < 0.5 ? 'force' : 'saber'
            });
        }
    }

    function spawnAmbientParticle(W, H) {
        particles.push({
            x: W * 0.25 + Math.random() * W * 0.5,
            y: H * 0.15 + Math.random() * H * 0.7,
            vx: (Math.random() - 0.5) * 0.3,
            vy: -0.15 - Math.random() * 0.4,
            life: 1,
            decay: 0.006 + Math.random() * 0.004,
            size: 1 + Math.random() * 2.5,
            type: 'ambient'
        });
    }

    function updateAndDrawParticles(ctx) {
        for (var i = particles.length - 1; i >= 0; i--) {
            var p = particles[i];
            p.x += p.vx;
            p.y += p.vy;
            p.life -= p.decay;
            if (p.life <= 0) { particles.splice(i, 1); continue; }
            ctx.globalAlpha = p.life * 0.8;
            if (p.type === 'force') {
                ctx.fillStyle = 'rgba(140,180,255,' + p.life + ')';
                ctx.shadowColor = 'rgba(100,160,255,0.6)';
                ctx.shadowBlur = 8;
            } else if (p.type === 'saber') {
                ctx.fillStyle = 'rgba(80,255,80,' + p.life + ')';
                ctx.shadowColor = 'rgba(60,255,60,0.5)';
                ctx.shadowBlur = 6;
            } else {
                ctx.fillStyle = 'rgba(245,166,35,' + (p.life * 0.35) + ')';
                ctx.shadowColor = 'transparent';
                ctx.shadowBlur = 0;
            }
            ctx.beginPath();
            ctx.arc(p.x, p.y, p.size * p.life, 0, Math.PI * 2);
            ctx.fill();
        }
        ctx.globalAlpha = 1;
        ctx.shadowBlur = 0;
    }

    /* ================================================================ */
    /*  Bee - full canvas render matching deploy-animation.js             */
    /* ================================================================ */

    function drawJediBee(ctx, cx, cy, t, saberGlow) {
        var wingBeat = Math.sin(t * 0.25) * 0.3;
        ctx.save();
        ctx.translate(cx, cy);

        // Top wing
        ctx.save();
        ctx.rotate(-0.2 + wingBeat);
        ctx.beginPath();
        ctx.moveTo(0, -10);
        ctx.bezierCurveTo(-10, -55, -65, -60, -80, -25);
        ctx.bezierCurveTo(-75, -5, -30, 0, 0, -10);
        ctx.closePath();
        ctx.fillStyle = 'rgba(230,220,200,0.55)';
        ctx.strokeStyle = 'rgba(160,120,80,0.5)';
        ctx.lineWidth = 0.8;
        ctx.fill();
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(-5, -12);
        ctx.bezierCurveTo(-25, -40, -55, -48, -75, -28);
        ctx.moveTo(-5, -14);
        ctx.bezierCurveTo(-20, -30, -45, -32, -70, -20);
        ctx.moveTo(-15, -22);
        ctx.lineTo(-55, -38);
        ctx.moveTo(-30, -28);
        ctx.lineTo(-60, -28);
        ctx.strokeStyle = 'rgba(140,100,60,0.35)';
        ctx.lineWidth = 0.6;
        ctx.stroke();
        ctx.restore();

        // Bottom wing
        ctx.save();
        ctx.rotate(0.15 + wingBeat * 0.7);
        ctx.beginPath();
        ctx.moveTo(-5, 10);
        ctx.bezierCurveTo(-15, -20, -60, -22, -68, 5);
        ctx.bezierCurveTo(-62, 22, -25, 20, -5, 10);
        ctx.closePath();
        ctx.fillStyle = 'rgba(220,210,185,0.45)';
        ctx.strokeStyle = 'rgba(150,110,70,0.4)';
        ctx.lineWidth = 0.7;
        ctx.fill();
        ctx.stroke();
        ctx.restore();

        // Abdomen
        ctx.beginPath();
        ctx.ellipse(-28, 14, 38, 20, -0.15, 0, Math.PI * 2);
        ctx.fillStyle = '#e8920a';
        ctx.fill();
        ctx.save();
        var sc = new Path2D();
        sc.ellipse(-28, 14, 38, 20, -0.15, 0, Math.PI * 2);
        ctx.clip(sc);
        [[-66,12],[-50,12],[-34,12],[-18,12],[-2,12]].forEach(function(pos, idx) {
            ctx.beginPath();
            ctx.rect(pos[0], 2, 10, 24);
            ctx.fillStyle = idx % 2 === 0 ? '#1a1005' : '#e8920a';
            ctx.fill();
        });
        ctx.beginPath();
        ctx.ellipse(-28, 8, 30, 10, -0.15, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(255,200,80,0.12)';
        ctx.fill();
        ctx.restore();
        ctx.beginPath();
        ctx.ellipse(-28, 14, 38, 20, -0.15, 0, Math.PI * 2);
        ctx.strokeStyle = '#3a2000';
        ctx.lineWidth = 1;
        ctx.stroke();

        // Stinger
        ctx.beginPath();
        ctx.moveTo(-64, 14);
        ctx.lineTo(-76, 12);
        ctx.lineTo(-66, 20);
        ctx.closePath();
        ctx.fillStyle = '#2a1800';
        ctx.fill();

        // Thorax
        ctx.beginPath();
        ctx.ellipse(8, 4, 22, 18, 0, 0, Math.PI * 2);
        ctx.fillStyle = '#c07808';
        ctx.fill();
        for (var f = 0; f < 10; f++) {
            var fx = 8 + Math.cos(f * 0.63) * 14;
            var fy = 4 + Math.sin(f * 0.63) * 11;
            ctx.beginPath();
            ctx.moveTo(fx, fy);
            ctx.lineTo(fx + Math.cos(f * 0.63) * 4, fy + Math.sin(f * 0.63) * 4);
            ctx.strokeStyle = 'rgba(240,160,20,0.4)';
            ctx.lineWidth = 1;
            ctx.stroke();
        }
        ctx.beginPath();
        ctx.ellipse(8, 4, 22, 18, 0, 0, Math.PI * 2);
        ctx.strokeStyle = '#3a2000';
        ctx.lineWidth = 0.8;
        ctx.stroke();

        // Jedi robe drape
        ctx.save();
        ctx.globalAlpha = 0.55;
        ctx.beginPath();
        ctx.moveTo(-10, -12);
        ctx.bezierCurveTo(-5, -28, 15, -30, 30, -20);
        ctx.lineTo(34, 8);
        ctx.bezierCurveTo(20, 14, 0, 14, -14, 8);
        ctx.closePath();
        ctx.fillStyle = '#3b2a1a';
        ctx.fill();
        ctx.restore();

        // Hood
        ctx.beginPath();
        ctx.moveTo(18, -14);
        ctx.bezierCurveTo(20, -30, 32, -34, 42, -28);
        ctx.bezierCurveTo(48, -22, 46, -10, 42, -4);
        ctx.lineTo(18, -4);
        ctx.closePath();
        ctx.fillStyle = '#4a3520';
        ctx.fill();
        ctx.strokeStyle = '#2a1a0a';
        ctx.lineWidth = 0.8;
        ctx.stroke();

        // Head
        ctx.beginPath();
        ctx.ellipse(28, -2, 17, 15, 0, 0, Math.PI * 2);
        ctx.fillStyle = '#d08010';
        ctx.fill();
        ctx.strokeStyle = '#3a2000';
        ctx.lineWidth = 0.8;
        ctx.stroke();

        // Compound eye
        ctx.beginPath();
        ctx.ellipse(36, -4, 10, 12, 0.3, 0, Math.PI * 2);
        ctx.fillStyle = '#0a0a0a';
        ctx.fill();
        ctx.beginPath();
        ctx.ellipse(33, -8, 3, 4, 0.5, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(80,120,180,0.4)';
        ctx.fill();
        ctx.beginPath();
        ctx.arc(32, -9, 1.5, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(200,220,255,0.3)';
        ctx.fill();

        // Antennae
        ctx.beginPath();
        ctx.moveTo(34, -14);
        ctx.bezierCurveTo(38, -30, 50, -35, 52, -28);
        ctx.strokeStyle = '#1a0a00';
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(52, -27, 3, 0, Math.PI * 2);
        ctx.fillStyle = '#1a0a00';
        ctx.fill();
        ctx.beginPath();
        ctx.moveTo(30, -16);
        ctx.bezierCurveTo(32, -28, 40, -30, 38, -22);
        ctx.strokeStyle = '#1a0a00';
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(38, -21, 3, 0, Math.PI * 2);
        ctx.fillStyle = '#1a0a00';
        ctx.fill();

        // Legs
        [[10,16,20,30],[0,18,8,32],[-10,16,-4,28]].forEach(function(leg) {
            ctx.beginPath();
            ctx.moveTo(leg[0], leg[1]);
            ctx.lineTo(leg[2], leg[3]);
            ctx.strokeStyle = '#1a0a00';
            ctx.lineWidth = 1.2;
            ctx.stroke();
        });

        // Arm holding saber
        ctx.beginPath();
        ctx.moveTo(38, 6);
        ctx.bezierCurveTo(48, -2, 55, -8, 58, -14);
        ctx.strokeStyle = '#c07808';
        ctx.lineWidth = 3.5;
        ctx.lineCap = 'round';
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(58, -14, 3, 0, Math.PI * 2);
        ctx.fillStyle = '#d08010';
        ctx.fill();

        // Lightsaber hilt
        ctx.save();
        ctx.translate(58, -14);
        ctx.rotate(-0.4);
        ctx.fillStyle = '#888';
        ctx.fillRect(-2.5, -18, 5, 14);
        ctx.fillStyle = '#666';
        ctx.fillRect(-3.5, -18, 7, 3);
        ctx.fillStyle = '#aaa';
        ctx.fillRect(-2, -4, 4, 3);
        ctx.fillStyle = '#c0392b';
        ctx.fillRect(-1, -12, 2, 4);

        // Blade
        var bladeLen = 50;
        var flicker = 0.85 + Math.sin(t * 0.15) * 0.1 + Math.sin(t * 0.37) * 0.05;
        var gl = saberGlow * flicker;

        ctx.shadowColor = 'rgba(0,255,0,0.6)';
        ctx.shadowBlur = 18 * gl;
        ctx.strokeStyle = 'rgba(0,255,0,' + (0.12 * gl) + ')';
        ctx.lineWidth = 12;
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.moveTo(0, -20);
        ctx.lineTo(0, -20 - bladeLen);
        ctx.stroke();

        ctx.shadowBlur = 10 * gl;
        ctx.strokeStyle = 'rgba(50,255,50,' + (0.35 * gl) + ')';
        ctx.lineWidth = 6;
        ctx.beginPath();
        ctx.moveTo(0, -20);
        ctx.lineTo(0, -20 - bladeLen);
        ctx.stroke();

        ctx.shadowBlur = 4;
        ctx.strokeStyle = 'rgba(180,255,180,' + (0.95 * gl) + ')';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(0, -20);
        ctx.lineTo(0, -20 - bladeLen);
        ctx.stroke();

        ctx.shadowBlur = 12 * gl;
        ctx.beginPath();
        ctx.arc(0, -20 - bladeLen, 3, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(200,255,200,' + (0.6 * gl) + ')';
        ctx.fill();

        ctx.shadowBlur = 0;
        ctx.restore();
        ctx.restore();
    }

    /* ================================================================ */
    /*  Force waves                                                       */
    /* ================================================================ */

    function drawForceWaves(ctx, cx, cy, progress) {
        if (progress <= 0 || progress >= 1) return;
        for (var r = 0; r < 3; r++) {
            var delay = r * 0.15;
            var rp = Math.max(0, Math.min(1, (progress - delay) / (1 - delay)));
            if (rp <= 0) continue;
            var radius = rp * 80;
            var alpha = (1 - rp) * 0.5;
            ctx.beginPath();
            ctx.ellipse(cx, cy, radius, radius * 0.6, 0, 0, Math.PI * 2);
            ctx.strokeStyle = 'rgba(120,170,255,' + alpha + ')';
            ctx.lineWidth = 2 - r * 0.5;
            ctx.stroke();
            if (rp > 0.2 && rp < 0.8) {
                for (var s = 0; s < 8; s++) {
                    var angle = (s / 8) * Math.PI * 2 + progress * 3;
                    var sx = cx + Math.cos(angle) * radius;
                    var sy = cy + Math.sin(angle) * radius * 0.6;
                    ctx.beginPath();
                    ctx.arc(sx, sy, 1.5, 0, Math.PI * 2);
                    ctx.fillStyle = 'rgba(180,210,255,' + (alpha * 0.8) + ')';
                    ctx.fill();
                }
            }
        }
    }

    /* ================================================================ */
    /*  Main loop                                                         */
    /* ================================================================ */

    window.startJediBeeAnimation = function () {
        var canvas = document.getElementById('jediBeeCanvas');
        var textEl = document.getElementById('jediBeeText');
        if (!canvas) return function () {};

        var ctx = canvas.getContext('2d');
        var dpr = window.devicePixelRatio || 1;
        var logW, logH, animId;
        var alive = true;

        function resize() {
            var rect = canvas.parentElement.getBoundingClientRect();
            logW = rect.width;
            logH = rect.height;
            canvas.width = logW * dpr;
            canvas.height = logH * dpr;
        }
        resize();
        window.addEventListener('resize', resize);

        var startTime = null;
        var lastForceWave = -2000;
        var forceWaveStart = 0;
        var forceWaveDuration = 1200;

        var quotes = [
            'These are the droids you\u2019re looking for\u2026',
            'The Force is strong with this coordinator\u2026',
            'Do. Or do not. There is no try.',
            'Sensing the Zigbee network\u2026',
            'Reaching out through the Force\u2026',
            'A Jedi uses the Force for knowledge\u2026',
            'Patience, young Padawan\u2026',
            'The mesh is strong with this one\u2026',
            'I find your lack of Zigbee disturbing\u2026',
            'In my experience, there\u2019s no such thing as luck\u2026'
        ];
        var quoteIndex = 0;

        function showQuote() {
            if (!textEl || !alive) return;
            textEl.style.opacity = '0';
            setTimeout(function () {
                if (!alive) return;
                textEl.textContent = quotes[quoteIndex % quotes.length];
                textEl.style.opacity = '0.85';
                quoteIndex++;
            }, 400);
        }
        showQuote();
        var qi = setInterval(showQuote, 4000);

        function animate(ts) {
            if (!alive) return;
            if (!startTime) startTime = ts;
            var elapsed = ts - startTime;

            ctx.save();
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, logW, logH);

            if (Math.random() < 0.12) spawnAmbientParticle(logW, logH);

            var beeCx = logW / 2;
            var beeCy = logH / 2 - 12;
            var floatY = Math.sin(elapsed * 0.0015) * 6;
            var floatX = Math.sin(elapsed * 0.001) * 3;
            var saberGlow = 0.85 + Math.sin(elapsed * 0.004) * 0.15;

            var fwp = -1;
            if (elapsed - lastForceWave > 3500) {
                lastForceWave = elapsed;
                forceWaveStart = elapsed;
                spawnForceParticles(beeCx + floatX + 50, beeCy + floatY - 45, 12);
            }
            if (elapsed - forceWaveStart < forceWaveDuration) {
                fwp = (elapsed - forceWaveStart) / forceWaveDuration;
            }

            var ga = 0.04 + Math.sin(elapsed * 0.003) * 0.02;
            var grd = ctx.createRadialGradient(
                beeCx + floatX + 40, beeCy + floatY - 30, 0,
                beeCx + floatX + 40, beeCy + floatY - 30, 70
            );
            grd.addColorStop(0, 'rgba(0,255,0,' + (ga * saberGlow) + ')');
            grd.addColorStop(1, 'rgba(0,255,0,0)');
            ctx.fillStyle = grd;
            ctx.fillRect(0, 0, logW, logH);

            if (fwp >= 0) drawForceWaves(ctx, beeCx + floatX + 50, beeCy + floatY - 40, fwp);
            updateAndDrawParticles(ctx);
            drawJediBee(ctx, beeCx + floatX, beeCy + floatY, elapsed, saberGlow);

            ctx.restore();
            animId = requestAnimationFrame(animate);
        }

        animId = requestAnimationFrame(animate);

        return function cleanup() {
            alive = false;
            clearInterval(qi);
            if (animId) cancelAnimationFrame(animId);
            window.removeEventListener('resize', resize);
            particles.length = 0;
        };
    };

})();