/* EyeQ mascot: cursor-following pupil + emotion lids.
 * Usage: Mascot.mount(document.getElementById('mascot')) -> { setEmotion, destroy }
 * Emotions: neutral | sleepy | focused | happy | stressed | sad | blink | expressionN
 * Expects EyeQ.svg ids: full_pupil (pupil+highlight), body, eyelids1
 * (lid_top1/lid_bottom1, hand-drawn), eyelids2 (lid_top2/lid_bottom2,
 * rounded ellipses), silhouette (body's outer contour, used as clip
 * geometry only -- its own fill is none).
 */
(function () {
  const NS = 'http://www.w3.org/2000/svg';
  // Hole centre + radius, from #body's own hole subpath. Only used for
  // pupil tracking's centre/clamp and the sclera backing circle -- the
  // eyelids no longer need this at all, since they're masked by the
  // silhouette clip instead of computed against the hole's geometry.
  const EYE = { x: 689, y: 485.4 };
  const HOLE_R = 344;
  const MAX = { x: 130, y: 130 };
  // #pupil's own baked `rotate(...)` transform rotates it about its own
  // local centre (789.72, 531.31) -- i.e. it's a self-rotation, not a
  // move to EYE. Pivoting scale/translation around EYE instead of this
  // point drags that ~110px offset along with the tracking delta,
  // biasing the pupil rightward and shrinking its leftward travel.
  // Pivoting around the pupil's actual rendered centre instead makes
  // it land exactly on EYE at rest and travel symmetrically from there.
  const PUPIL_CENTER = { x: 789.73, y: 531.31 };
  const VIEWBOX = '139 30 1060 912'; // tight crop of #body's outer bbox

  // [lid style ('round' = eyelids2, 'flat' = eyelids1), top growth,
  // bottom growth, top rotate deg, bottom rotate deg]. Growth is 0..1
  // against each lid's own natural drawn size (1 = exactly as drawn),
  // and can go slightly past 1 to close further for a firm blink.
  // Rotation signs are deliberately mixed, not all tilting the same way.
  const LIDS = {
    neutral: ['round', 0.22, 0.18, 0, 0],
    focused: ['round', 0.42, 0.18, 0, 0],
    sleepy:  ['round', 0.62, 0.5, 0, 0],
    happy:   ['round', 0.3, 0.62, 0, 0],
    stressed:['round', 0.55, 0.42, 4, 0],
    sad:     ['round', 0.5, 0.18, -6, 0],
    blink:   ['round', 1.0, 0.85, 0, 0],
    expression1: ['round', 0.42, 0.18, 14, 0],
    expression2: ['round', 0.46, 0.18, -14, 0],
    expression3: ['flat', 0.5, 0.4, -8, 8],
    expression4: ['flat', 0.55, 0.45, -10, -10],
  };

  // Pupil (full_pupil, pupil+highlight together) size per emotion, as
  // [x scale, y scale] of the exported unit.
  const PUPILS = {
    neutral: [0.90, 0.90],
    focused: [0.62, 0.72],
    sleepy:  [0.85, 0.55],
    happy:   [0.95, 0.70],
    stressed:[0.50, 0.55],
    sad:     [0.90, 0.90],
  };

  const STYLES = ['round', 'flat'];

  function el(name, attrs) {
    const e = document.createElementNS(NS, name);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  function prep(svg) {
    svg.setAttribute('viewBox', VIEWBOX);
    svg.removeAttribute('width'); svg.removeAttribute('height');

    const fullPupil = svg.querySelector('#full_pupil');
    const body = svg.querySelector('#body');
    const eyelids2 = svg.querySelector('#eyelids2');
    const eyelids1 = svg.querySelector('#eyelids1');
    const silhouettePath = svg.querySelector('#silhouette path');

    // White sclera behind the pupil so it reads against the page
    // background showing through #body's hole. #body is painted OVER
    // #full_pupil and has the hole cut out of its own fill, so it
    // already masks the pupil/highlight to exactly the hole's shape
    // for free -- no clip-path needed for those.
    const sclera = el('circle', { cx: EYE.x, cy: EYE.y, r: HOLE_R + 10, fill: '#fff' });
    fullPupil.before(sclera);

    // Eyelids are the same purple as #body's fill, so they blend
    // seamlessly wherever they overlap the ring -- the only thing that
    // needs clipping is keeping a grown lid from poking past the
    // character's OUTER edge into the page background (visible as
    // stray "ears"). #silhouette is exported specifically as that
    // outer-contour clip geometry, so share one clipPath built from it
    // across both lid styles.
    const lids = el('g', { id: 'lids' });
    if (silhouettePath) {
      let defs = svg.querySelector('defs');
      if (!defs) { defs = el('defs', {}); svg.prepend(defs); }
      const clip = el('clipPath', { id: 'eyelidClip', clipPathUnits: 'userSpaceOnUse' });
      clip.appendChild(silhouettePath.cloneNode(true));
      defs.appendChild(clip);
      lids.setAttribute('clip-path', 'url(#eyelidClip)');
    }
    if (eyelids2) lids.appendChild(eyelids2);
    if (eyelids1) lids.appendChild(eyelids1);
    body.after(lids);
    svg.querySelector('#silhouette')?.remove();

    return { fullPupil };
  }

  function mount(container, opts = {}) {
    const svg = container.querySelector('svg');
    if (!svg) throw new Error('mascot: no <svg> in container');
    const { fullPupil } = prep(svg);
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

    // Each lid is wrapped in two nested <g>s: an inner one that
    // stretches it vertically (fill-box, anchored at its OWN outward
    // edge -- the edge away from the hole -- so it grows from nothing
    // there down/up into the eye), and an outer one that rotates it
    // (view-box, anchored at the ring's own centre, since rotating an
    // edge-anchored shape around anything else sweeps its far end away
    // from the ring's curve and opens a visible gap).
    function rigLid(id, anchorPct) {
      const lid = svg.querySelector(id);
      if (!lid) return null;
      lid.style.transformBox = 'fill-box';
      lid.style.transformOrigin = anchorPct;
      const spin = document.createElementNS(NS, 'g');
      lid.parentNode.insertBefore(spin, lid);
      spin.appendChild(lid);
      spin.style.transformBox = 'view-box';
      spin.style.transformOrigin = `${EYE.x}px ${EYE.y}px`;
      return { lid, spin };
    }
    const RIG = {
      round: { top: rigLid('#lid_top2', '50% 0%'), bottom: rigLid('#lid_bottom2', '50% 100%') },
      flat: { top: rigLid('#lid_top1', '50% 0%'), bottom: rigLid('#lid_bottom1', '50% 100%') },
    };

    // 'none' is the resting state: eyes wide open, no lid drawn at all
    // (growth 0 at each lid's own outward-edge anchor -- genuinely
    // zero height). Lids only animate in once an actual expression is
    // set (setEmotion).
    let emotion = 'none', blinking = false;
    const zero = () => ({ topG: 0, botG: 0, topRot: 0, botRot: 0 });
    let cur = { round: zero(), flat: zero() };
    let tgt = { round: zero(), flat: zero() };
    function applyLids() {
      const hidden = !blinking && emotion === 'none';
      const [style, t, b, tr = 0, br = 0] = LIDS[blinking ? 'blink' : emotion] || LIDS.neutral;
      for (const s of STYLES) {
        const active = !hidden && s === style;
        tgt[s].topG = active ? t : 0;
        tgt[s].botG = active ? b : 0;
        tgt[s].topRot = active ? tr : 0;
        tgt[s].botRot = active ? br : 0;
      }
      svg.dataset.emotion = emotion;
    }
    applyLids();
    // land exactly on the initial (hidden) values instead of easing in
    // from 0 on first paint
    cur = { round: { ...tgt.round }, flat: { ...tgt.flat } };

    // --- pupil tracking ---
    let tx = 0, ty = 0, cx = 0, cy = 0, sx = 0.8, sy = 0.8, raf = 0, lastMove = performance.now();
    function aim(clientX, clientY) {
      const ctm = svg.getScreenCTM();
      if (!ctm) return;
      const p = new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse());
      let dx = p.x - EYE.x, dy = p.y - EYE.y;
      const k = Math.hypot(dx / MAX.x, dy / MAX.y);
      if (k > 1) { dx /= k; dy /= k; }
      tx = dx; ty = dy;
    }
    const onMove = (e) => { lastMove = performance.now(); aim(e.clientX, e.clientY); };
    addEventListener('pointermove', onMove, { passive: true });

    // Lerp rates below are time-constants (ms), not per-frame fractions:
    // a fixed per-frame fraction converges in fewer *milliseconds* on a
    // high-refresh-rate display, since requestAnimationFrame just fires
    // more often there. Elapsed-time-based decay keeps the same
    // wall-clock duration regardless of refresh rate.
    let lastFrameTime = performance.now();
    function applyRig(rig, g, rot) {
      if (!rig) return;
      rig.lid.style.transform = `scale(1, ${g.toFixed(3)})`;
      rig.spin.style.transform = `rotate(${rot.toFixed(2)}deg)`;
    }
    function frame(now) {
      const dt = Math.min(now - lastFrameTime, 100); // clamp after e.g. a backgrounded tab
      lastFrameTime = now;

      // idle wander when the pointer has been still (also covers touch devices)
      if (!reduced && now - lastMove > 6000 && Math.random() < 0.01) {
        tx = (Math.random() * 2 - 1) * MAX.x * 0.7;
        ty = (Math.random() * 2 - 1) * MAX.y * 0.7;
      }
      const s = reduced ? 1 : 1 - Math.exp(-dt / 100);
      cx += (tx - cx) * s; cy += (ty - cy) * s;
      const [psx, psy] = PUPILS[emotion] || PUPILS.neutral;
      sx += (psx - sx) * s; sy += (psy - sy) * s;
      fullPupil.setAttribute('transform',
        `translate(${(EYE.x + cx).toFixed(2)} ${(EYE.y + cy).toFixed(2)}) ` +
        `scale(${sx.toFixed(3)} ${sy.toFixed(3)}) translate(${-PUPIL_CENTER.x} ${-PUPIL_CENTER.y})`);

      // slower time-constant than the pupil, so a lid change reads as a
      // clear, deliberate animation rather than a quick follow
      const ls = reduced ? 1 : 1 - Math.exp(-dt / 90);
      for (const st of STYLES) {
        cur[st].topG += (tgt[st].topG - cur[st].topG) * ls;
        cur[st].botG += (tgt[st].botG - cur[st].botG) * ls;
        cur[st].topRot += (tgt[st].topRot - cur[st].topRot) * ls;
        cur[st].botRot += (tgt[st].botRot - cur[st].botRot) * ls;
      }
      applyRig(RIG.round.top, cur.round.topG, cur.round.topRot);
      applyRig(RIG.round.bottom, cur.round.botG, cur.round.botRot);
      applyRig(RIG.flat.top, cur.flat.topG, cur.flat.topRot);
      applyRig(RIG.flat.bottom, cur.flat.botG, cur.flat.botRot);

      raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);

    // --- blinking ---
    let blinkTimer = 0;
    function blinkLoop() {
      blinkTimer = setTimeout(() => {
        if (emotion !== 'sleepy') {
          blinking = true; applyLids();
          setTimeout(() => { blinking = false; applyLids(); }, 130);
        }
        blinkLoop();
      }, 2000 + Math.random() * 4000);
    }
    if (!reduced) blinkLoop();

    return {
      setEmotion(name) {
        if (name !== 'none' && (!LIDS[name] || name === 'blink')) return;
        if (name === emotion) return;
        emotion = name; applyLids();
      },
      destroy() {
        cancelAnimationFrame(raf); clearTimeout(blinkTimer);
        removeEventListener('pointermove', onMove);
      },
    };
  }

  // Idle repertoire for a self-running companion (see below).
  const RANDOM_EMOTIONS = ['neutral', 'happy', 'focused', 'sleepy', 'stressed',
    'sad', 'expression1', 'expression2', 'expression3', 'expression4'];

  /* A mascot that runs itself: loads the artwork into `host`, mounts it,
   * holds each flashed emotion for a few seconds, drifts through idle
   * expressions, and reacts to clicks. flash() is the one entry point
   * for every trigger (live events, the idle timer, clicks) and is
   * rate-limited there, so rapid-fire triggers can't retarget the lid
   * animation faster than it can settle -- which reads as flashing
   * instability rather than as a reaction.
   */
  function companion(host, src = '/assets/EyeQ.svg') {
    const COOLDOWN_MS = 900;
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
    let rig = null, holdTimer = 0, lastFlashAt = 0;
    fetch(src).then((r) => r.text()).then((svg) => {
      host.innerHTML = svg;
      rig = mount(host);  // resting state: no eyelid, wide open
    }).catch(console.error);

    const pick = () => RANDOM_EMOTIONS[Math.floor(Math.random() * RANDOM_EMOTIONS.length)];
    function flash(name, holdMs = 4000) {
      if (!rig) return;
      const now = Date.now();
      if (now - lastFlashAt < COOLDOWN_MS) return;
      lastFlashAt = now;
      rig.setEmotion(name);
      clearTimeout(holdTimer);
      holdTimer = setTimeout(() => rig.setEmotion('none'), holdMs);
    }
    function scheduleIdle() {
      setTimeout(() => { flash(pick()); scheduleIdle(); },
                 15000 + Math.random() * 25000);  // every 15-40s
    }
    if (!reduced) scheduleIdle();
    host.addEventListener('click', () => flash(pick()));
    return { flash };
  }

  window.Mascot = { mount, companion };
})();
