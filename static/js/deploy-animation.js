/**
 * deploy-animation.js
 * ZigBee Matter Manager — deploy animation
 * Bee with flames flies across the screen when triggered.
 */

(function () {

  /* ------------------------------------------------------------------ */
  /*  Inject overlay HTML once                                            */
  /* ------------------------------------------------------------------ */
  function ensureOverlay() {
    if (document.getElementById('bee-overlay')) return;

    const style = document.createElement('style');
    style.textContent = `
      #bee-overlay {
        position:fixed;top:0;left:0;width:100vw;height:100vh;
        pointer-events:none;z-index:99999;overflow:hidden;display:none;
      }
      #bee-flash {
        position:absolute;top:0;left:0;width:100%;height:100%;
        background:white;opacity:0;
      }
      #bee-msg {
        position:absolute;top:42%;left:50%;
        transform:translate(-50%,-50%) scale(0);
        font-family:'Courier New',monospace;font-size:1.8rem;font-weight:bold;
        color:#f5a623;white-space:nowrap;opacity:0;letter-spacing:3px;
        text-shadow:0 0 20px #ff8800;
      }
    `;
    document.head.appendChild(style);

    const div = document.createElement('div');
    div.id = 'bee-overlay';
    div.innerHTML = `
      <canvas id="bee-canvas"></canvas>
      <div id="bee-flash"></div>
      <div id="bee-msg">DEPLOYING... 88MPH</div>
    `;
    document.body.appendChild(div);
  }

  /* ------------------------------------------------------------------ */
  /*  Particle pool                                                       */
  /* ------------------------------------------------------------------ */
  const particles = [];

  function spawnParticles(beeX, beeY) {
    for (let i = 0; i < 5; i++) {
      particles.push({
        x:     beeX - 30,
        y:     beeY + 12 + (Math.random() - 0.5) * 10,
        vx:    -(2 + Math.random() * 4),
        vy:    (Math.random() - 0.5) * 1.5,
        life:  1,
        decay: 0.025 + Math.random() * 0.03,
        size:  4 + Math.random() * 8,
        type:  Math.random() < 0.7 ? 'flame' : 'spark'
      });
    }
  }

  function drawFlameParticles(ctx) {
    for (let i = particles.length - 1; i >= 0; i--) {
      const p = particles[i];
      p.x  += p.vx;
      p.y  += p.vy;
      p.vy += (Math.random() - 0.52) * 0.3;
      p.life -= p.decay;
      if (p.life <= 0) { particles.splice(i, 1); continue; }

      const a = p.life;
      if (p.type === 'spark') {
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.size * 0.25 * a, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(255,240,100,${a})`;
        ctx.fill();
      } else {
        const sz = p.size * (0.4 + 0.6 * a);
        const g  = p.life > 0.6 ? Math.floor(180 * a) : Math.floor(80 * a);
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate(Math.PI * 0.5 + (Math.random() - 0.5) * 0.3);
        ctx.beginPath();
        ctx.moveTo(0, -sz);
        ctx.bezierCurveTo( sz*0.6, -sz*0.3,  sz*0.5, sz*0.4, 0,  sz*0.5);
        ctx.bezierCurveTo(-sz*0.5,  sz*0.4, -sz*0.6, -sz*0.3, 0, -sz);
        ctx.closePath();
        ctx.fillStyle = `rgba(255,${g},0,${a * 0.85})`;
        ctx.fill();
        ctx.restore();
      }
    }
  }

  /* ------------------------------------------------------------------ */
  /*  Core flame tongues                                                  */
  /* ------------------------------------------------------------------ */
  function drawCoreFire(ctx, beeX, beeY, t, W) {
    const ox      = beeX - 32;
    const oy      = beeY + 12;
    const maxLen  = Math.min(ox, W * 0.85);
    if (maxLen < 10) return;

    const tongues = [
      { spread:   0, len: maxLen,       color0: [255,240, 80], color1: [255, 80,  0], w: 18, alpha: 0.9 },
      { spread:  -8, len: maxLen * 0.75, color0: [255,200,  0], color1: [220, 30,  0], w: 11, alpha: 0.8 },
      { spread:   8, len: maxLen * 0.75, color0: [255,200,  0], color1: [220, 30,  0], w: 11, alpha: 0.8 },
      { spread: -16, len: maxLen * 0.45, color0: [255,140,  0], color1: [180, 10,  0], w:  7, alpha: 0.7 },
      { spread:  16, len: maxLen * 0.45, color0: [255,140,  0], color1: [180, 10,  0], w:  7, alpha: 0.7 },
    ];

    tongues.forEach(({ spread, len, color0, color1, w, alpha }) => {
      const tipX  = ox - len;
      const tipY  = oy + spread;
      const wave  = Math.sin(t * 0.008 + spread * 0.2) * 12;
      const midY  = oy + spread * 0.5 + wave;
      const steps = 40;

      const grad = ctx.createLinearGradient(ox, oy, tipX, tipY);
      grad.addColorStop(0,    `rgba(${color0},${alpha})`);
      grad.addColorStop(0.35, `rgba(${color1},${alpha * 0.9})`);
      grad.addColorStop(0.7,  `rgba(${color1},${alpha * 0.5})`);
      grad.addColorStop(1,    `rgba(${color1},0)`);

      ctx.beginPath();
      for (let s = 0; s <= steps; s++) {
        const frac   = s / steps;
        const px     = ox - frac * len;
        const py     = midY + (oy - midY) * (1 - frac) + Math.sin(frac * Math.PI * 3 + t * 0.01 + spread * 0.1) * 4 * (1 - frac);
        const halfW  = (w * 0.5) * (1 - frac) * (1 - frac * 0.3);
        if (s === 0) ctx.moveTo(px, py - halfW);
        else         ctx.lineTo(px, py - halfW);
      }
      for (let s = steps; s >= 0; s--) {
        const frac   = s / steps;
        const px     = ox - frac * len;
        const py     = midY + (oy - midY) * (1 - frac) + Math.sin(frac * Math.PI * 3 + t * 0.01 + spread * 0.1) * 4 * (1 - frac);
        const halfW  = (w * 0.5) * (1 - frac) * (1 - frac * 0.3);
        ctx.lineTo(px, py + halfW);
      }
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
    });

    /* white-hot core */
    const coreGrad = ctx.createLinearGradient(ox, oy, ox - maxLen * 0.2, oy);
    coreGrad.addColorStop(0,   'rgba(255,255,220,0.95)');
    coreGrad.addColorStop(0.5, 'rgba(255,240,100,0.6)');
    coreGrad.addColorStop(1,   'rgba(255,200,0,0)');
    ctx.beginPath();
    ctx.ellipse(ox - maxLen * 0.08, oy, maxLen * 0.15, 5, 0, 0, Math.PI * 2);
    ctx.fillStyle = coreGrad;
    ctx.fill();
  }

  /* ------------------------------------------------------------------ */
  /*  Bee drawing                                                         */
  /* ------------------------------------------------------------------ */
  function drawBee(ctx, cx, cy, t) {
    const wingBeat = Math.sin(t * 0.25) * 0.3;

    ctx.save();
    ctx.translate(cx, cy);

    /* top wing */
    ctx.save();
    ctx.rotate(-0.2 + wingBeat);
    ctx.beginPath();
    ctx.moveTo(0, -10);
    ctx.bezierCurveTo(-10, -55, -65, -60, -80, -25);
    ctx.bezierCurveTo(-75, -5, -30, 0, 0, -10);
    ctx.closePath();
    ctx.fillStyle   = 'rgba(230,220,200,0.55)';
    ctx.strokeStyle = 'rgba(160,120,80,0.5)';
    ctx.lineWidth   = 0.8;
    ctx.fill(); ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(-5,-12);  ctx.bezierCurveTo(-25,-40,-55,-48,-75,-28);
    ctx.moveTo(-5,-14);  ctx.bezierCurveTo(-20,-30,-45,-32,-70,-20);
    ctx.moveTo(-15,-22); ctx.lineTo(-55,-38);
    ctx.moveTo(-30,-28); ctx.lineTo(-60,-28);
    ctx.strokeStyle = 'rgba(140,100,60,0.35)';
    ctx.lineWidth   = 0.6;
    ctx.stroke();
    ctx.restore();

    /* bottom wing */
    ctx.save();
    ctx.rotate(0.15 + wingBeat * 0.7);
    ctx.beginPath();
    ctx.moveTo(-5, 10);
    ctx.bezierCurveTo(-15,-20,-60,-22,-68, 5);
    ctx.bezierCurveTo(-62, 22,-25, 20,-5, 10);
    ctx.closePath();
    ctx.fillStyle   = 'rgba(220,210,185,0.45)';
    ctx.strokeStyle = 'rgba(150,110,70,0.4)';
    ctx.lineWidth   = 0.7;
    ctx.fill(); ctx.stroke();
    ctx.restore();

    /* abdomen */
    ctx.beginPath();
    ctx.ellipse(-28, 14, 38, 20, -0.15, 0, Math.PI * 2);
    ctx.fillStyle = '#e8920a';
    ctx.fill();

    const stripeClip = new Path2D();
    stripeClip.ellipse(-28, 14, 38, 20, -0.15, 0, Math.PI * 2);
    ctx.save();
    ctx.clip(stripeClip);
    [[-66,12],[-50,12],[-34,12],[-18,12],[-2,12]].forEach(([sx], i) => {
      ctx.beginPath();
      ctx.rect(sx, 2, 10, 24);
      ctx.fillStyle = i % 2 === 0 ? '#1a1005' : '#e8920a';
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
    ctx.lineWidth   = 1;
    ctx.stroke();

    /* stinger */
    ctx.beginPath();
    ctx.moveTo(-64, 14); ctx.lineTo(-76, 12); ctx.lineTo(-66, 20);
    ctx.closePath();
    ctx.fillStyle = '#2a1800';
    ctx.fill();

    /* thorax */
    ctx.beginPath();
    ctx.ellipse(8, 4, 22, 18, 0, 0, Math.PI * 2);
    ctx.fillStyle = '#c07808';
    ctx.fill();
    for (let f = 0; f < 10; f++) {
      const fx = 8 + Math.cos(f * 0.63) * 14;
      const fy = 4 + Math.sin(f * 0.63) * 11;
      ctx.beginPath();
      ctx.moveTo(fx, fy);
      ctx.lineTo(fx + Math.cos(f * 0.63) * 4, fy + Math.sin(f * 0.63) * 4);
      ctx.strokeStyle = 'rgba(240,160,20,0.4)';
      ctx.lineWidth   = 1;
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.ellipse(8, 4, 22, 18, 0, 0, Math.PI * 2);
    ctx.strokeStyle = '#3a2000';
    ctx.lineWidth   = 0.8;
    ctx.stroke();

    /* head */
    ctx.beginPath();
    ctx.ellipse(28, -2, 17, 15, 0, 0, Math.PI * 2);
    ctx.fillStyle = '#d08010';
    ctx.fill();
    ctx.strokeStyle = '#3a2000';
    ctx.lineWidth   = 0.8;
    ctx.stroke();

    /* compound eye */
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

    /* antennae */
    ctx.beginPath();
    ctx.moveTo(34,-14); ctx.bezierCurveTo(38,-30,50,-35,52,-28);
    ctx.strokeStyle = '#1a0a00'; ctx.lineWidth = 1.5; ctx.stroke();
    ctx.beginPath(); ctx.arc(52,-27,3,0,Math.PI*2);
    ctx.fillStyle = '#1a0a00'; ctx.fill();

    ctx.beginPath();
    ctx.moveTo(30,-16); ctx.bezierCurveTo(32,-28,40,-30,38,-22);
    ctx.strokeStyle = '#1a0a00'; ctx.lineWidth = 1.5; ctx.stroke();
    ctx.beginPath(); ctx.arc(38,-21,3,0,Math.PI*2);
    ctx.fillStyle = '#1a0a00'; ctx.fill();

    /* legs */
    [[10,16,20,30],[0,18,8,32],[-10,16,-4,28]].forEach(([lx,ly,ex,ey]) => {
      ctx.beginPath(); ctx.moveTo(lx,ly); ctx.lineTo(ex,ey);
      ctx.strokeStyle = '#1a0a00'; ctx.lineWidth = 1.2; ctx.stroke();
    });

    ctx.restore();

    /* amber speed lines */
    [0,1,2,3].forEach(i => {
      const ly  = cy - 10 + i * 8;
      const len = 40 + i * 20;
      const gr  = ctx.createLinearGradient(cx-len-20, ly, cx-20, ly);
      gr.addColorStop(0, 'rgba(245,166,35,0)');
      gr.addColorStop(1, `rgba(245,166,35,${0.35 - i * 0.07})`);
      ctx.beginPath();
      ctx.strokeStyle = gr;
      ctx.lineWidth   = 1.2 - i * 0.2;
      ctx.moveTo(cx-len-20, ly);
      ctx.lineTo(cx-20, ly);
      ctx.stroke();
    });
  }

  /* ------------------------------------------------------------------ */
  /*  Main animation loop                                                 */
  /* ------------------------------------------------------------------ */
  window.showDeloreanAnimation = function () {
    ensureOverlay();

    const overlay = document.getElementById('bee-overlay');
    const canvas  = document.getElementById('bee-canvas');
    const flash   = document.getElementById('bee-flash');
    const msg     = document.getElementById('bee-msg');
    const ctx     = canvas.getContext('2d');

    const W = canvas.width  = window.innerWidth;
    const H = canvas.height = window.innerHeight;
    overlay.style.display   = 'block';
    particles.length        = 0;

    let startTime = null;
    const duration = 3600;

    function animate(ts) {
      if (!startTime) startTime = ts;
      const elapsed  = ts - startTime;
      const progress = Math.min(elapsed / duration, 1);

      ctx.clearRect(0, 0, W, H);

      const beeX = -80 + progress * (W + 180);
      const beeY = H * 0.48 + Math.sin(elapsed * 0.003) * 12;

      /* blue time-trail streaks */
      if (progress > 0.2 && progress < 0.88) {
        const sa = Math.sin(progress * Math.PI) * 0.45;
        for (let i = 0; i < 6; i++) {
          const sy = H * 0.48 + i * 7 - 18;
          const gr = ctx.createLinearGradient(0, sy, beeX-40, sy);
          gr.addColorStop(0,   `rgba(100,180,255,0)`);
          gr.addColorStop(0.6, `rgba(100,180,255,${sa * 0.3})`);
          gr.addColorStop(1,   `rgba(100,180,255,${sa})`);
          ctx.beginPath();
          ctx.strokeStyle = gr;
          ctx.lineWidth   = i < 2 ? 1.5 : 0.8;
          ctx.moveTo(0, sy); ctx.lineTo(beeX-40, sy);
          ctx.stroke();
        }
      }

      /* flames (drawn before bee so bee sits on top) */
      drawCoreFire(ctx, beeX, beeY, elapsed, W);
      spawnParticles(beeX, beeY);
      drawFlameParticles(ctx);

      drawBee(ctx, beeX, beeY, elapsed);

      /* flash */
      if (progress > 0.68 && progress < 0.75) {
        flash.style.opacity = ((0.75 - progress) / 0.07 * 0.85).toFixed(3);
      } else {
        flash.style.opacity = 0;
      }

      /* message */
      if (progress > 0.7 && progress < 0.93) {
        const mp = (progress - 0.7) / 0.23;
        const sc = mp < 0.2 ? mp / 0.2 : mp > 0.8 ? 1 - (mp - 0.8) / 0.2 : 1;
        msg.style.opacity   = sc;
        msg.style.transform = `translate(-50%,-50%) scale(${0.4 + sc * 0.6})`;
      } else {
        msg.style.opacity = 0;
      }

      if (progress < 1) {
        requestAnimationFrame(animate);
      } else {
        overlay.style.display = 'none';
      }
    }

    requestAnimationFrame(animate);
  };

})();