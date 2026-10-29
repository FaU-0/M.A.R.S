
/* ===== Face Mesh: стандартизованная съёмка (пациент) ===== */
FaceMesh.Capture = {
  TEXT: {
    frontal: 'Смотрите прямо в камеру',
    left45: 'Поверните голову к левому плечу на 45 градусов',
    right45: 'Поверните голову к правому плечу на 45 градусов',
    left90: 'Профиль: поверните голову к левому плечу на 90 градусов',
    right90: 'Профиль: поверните голову к правому плечу на 90 градусов'
  },
  /* кодирование JPEG не больше лимита сервера (5 МБ) */
  async encode(cv) {
    const toBlob = (c, q) => new Promise(r => c.toBlob(r, 'image/jpeg', q));
    let c = cv, q = 0.92;
    for (let i = 0; i < 12; i++) {
      const b = await toBlob(c, q);
      if (b && b.size <= 4.6 * 1024 * 1024) return b;
      if (q > 0.6) q -= 0.1;
      else { const n = document.createElement('canvas'); n.width = Math.round(c.width * 0.8); n.height = Math.round(c.height * 0.8); n.getContext('2d').drawImage(c, 0, 0, n.width, n.height); c = n; }
    }
    throw new Error('Не удалось уменьшить снимок до допустимого размера');
  },
  /* снимок текущего кадра в полном разрешении видеоэлемента, без зеркала */
  grab(video, cur, hide, mesh) {
    const vw = video.videoWidth, vh = video.videoHeight;
    if (!vw || !vh) throw new Error('Камера не готова');
    const cv = document.createElement('canvas'); cv.width = vw; cv.height = vh;
    const ctx = cv.getContext('2d'); ctx.drawImage(video, 0, 0, vw, vh);
    const fresh = cur && performance.now() - cur.t < 700 && cur.vw === vw && cur.vh === vh;
    if (hide) {
      if (!fresh) throw new Error('Лицо не найдено в кадре: скрыть его перед сохранением невозможно. Снимок не сделан.');
      FaceMesh.Privacy.hide(ctx, cur, vw, vh);
    } else if (mesh && fresh) {
      const k = Math.max(1, vw / 640);
      ctx.save(); ctx.scale(k, k); FaceMesh.Overlay.draw(ctx, cur, vw, vh, 'contour', { s: 1 / k, ox: 0, oy: 0 }, '#34C759'); ctx.restore();
    }
    return cv;
  },
  async open(poses) {
    const F = FaceMesh;
    if (!(await F.ensureConsent())) return;
    if (!Media.stream || !Media.cam) { Toast.err('Для съёмки нужна включённая камера'); return; }
    const list = (poses && poses.length ? poses : ['frontal']).slice();
    F.busy++; await F.sync();
    if (!F.trk) { F.busy = Math.max(0, F.busy - 1); return; }
    let closed = false, idx = 0, stepToken = 0, hl = null;
    const vid = h('video', { autoplay: true, playsinline: true, class: 'mirror', style: 'position:absolute;inset:0;width:100%;height:100%;object-fit:cover' }); vid.muted = true; vid.srcObject = Media.stream;
    const ov = h('canvas', { style: 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none' });
    const count = h('div', { style: 'position:absolute;inset:0;display:grid;place-items:center;font-size:96px;font-weight:700;color:#fff;text-shadow:0 0 8px rgba(0,0,0,.7);pointer-events:none' });
    const stage = h('div', { style: 'position:relative;width:100%;aspect-ratio:3/4;max-height:62vh;background:#000;border-radius:14px;overflow:hidden;margin:0 auto' }, vid, ov, count);
    const title = h('div', { class: 'big-hint', role: 'status', 'aria-live': 'polite' });
    const sub = h('div', { class: 'note', style: 'text-align:center', 'aria-live': 'polite' });
    const prog = h('progress', { max: 1, value: 0, style: 'width:100%;height:12px' });
    const btns = h('div', { class: 'row wrap', style: 'justify-content:center' });
    const opts = h('div', { class: 'stack' },
      h('label', { class: 'row', style: 'min-height:44px' }, h('input', { type: 'checkbox', style: 'width:22px;height:22px', checked: F.hideFace || null, onchange: e => { F.hideFace = e.target.checked; mesh.disabled = F.hideFace; } }), ' Скрыть лицо (пикселизация до отправки)'));
    const mesh = h('input', { type: 'checkbox', style: 'width:22px;height:22px', checked: F.meshOnShot || null, disabled: F.hideFace || null, onchange: e => { F.meshOnShot = e.target.checked; } });
    opts.append(h('label', { class: 'row', style: 'min-height:44px' }, mesh, ' Наложить контуры на снимок'));
    const s = Ui.sheet({
      title: 'Фотосъёмка по ракурсам', wide: true, onClose: async () => {
        if (closed) return; closed = true; stepToken++; if (hl) Loop.del(hl); F.subs.delete(onCur);
        F.busy = Math.max(0, F.busy - 1); await Media.restore720(); F.sync();
      }
    });
    s.el.append(stage, title, sub, prog, btns, opts);
    let state = null; /* состояние шага */
    const pose = () => list[idx];
    /* подсказка направления и допуск */
    const evalPose = cur => {
      const p = pose(), tgt = POSE_YAW[p], e = F.Pose.euler(cur), tol = F.tol;
      const ey = e.yaw - tgt;
      const ok = Math.abs(ey) <= tol && Math.abs(e.pitch) <= tol && Math.abs(e.roll) <= tol;
      let hint = '';
      if (Math.abs(ey) > tol) hint = ey < 0 ? 'Поверните голову ещё левее (к левому плечу)' : 'Поверните голову ещё правее (к правому плечу)';
      else if (Math.abs(e.pitch) > tol) hint = e.pitch > 0 ? 'Опустите подбородок' : 'Приподнимите подбородок';
      else if (Math.abs(e.roll) > tol) hint = 'Не наклоняйте голову к плечу';
      return { ok, hint, e, ey };
    };
    const manualOnly = () => Math.abs(POSE_YAW[pose()]) > 60;
    const onCur = cur => {
      if (!state || state.busy || closed) return;
      if (manualOnly()) return;
      const r = evalPose(cur); state.last = r;
      const info = F.info;
      const good = r.ok && info && info.level !== 'bad' && !F.trk.lost() && !(cur.multi && false);
      const now = performance.now();
      if (good) {
        if (!state.since) state.since = now;
        const held = now - state.since;
        if (held >= 1000 && !state.counting) countdown(true);
        else if (!state.counting) { title.textContent = 'Держите позу'; prog.value = Math.min(1, held / 1000); }
      } else {
        state.since = 0; prog.value = 0;
        if (state.counting) { state.cancel = true; }
        if (!state.counting) title.textContent = info && info.hint && (!r.hint) ? info.hint : (r.hint || 'Выровняйте голову');
      }
    };
    F.subs.add(onCur);
    const countdown = async auto => {
      if (state.counting) return; state.counting = true; state.cancel = false; const tok = stepToken;
      for (let n = 3; n >= 1; n--) {
        count.textContent = String(n); title.textContent = 'Не двигайтесь';
        for (let t = 0; t < 10; t++) { await sleep(100); if (closed || tok !== stepToken) { count.textContent = ''; return; } if (auto && state.cancel) { state.counting = false; state.since = 0; count.textContent = ''; return; } }
      }
      count.textContent = '';
      if (closed || tok !== stepToken) return;
      state.counting = false; shoot();
    };
    const drawGuide = () => {
      if (closed) return;
      const w = ov.clientWidth, hh = ov.clientHeight; if (!w) return;
      const dpr = window.devicePixelRatio || 1; if (ov.width !== Math.round(w * dpr)) { ov.width = Math.round(w * dpr); ov.height = Math.round(hh * dpr); }
      const c = ov.getContext('2d'); c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, hh);
      const p = pose(), r = state && state.last, ok = r && r.ok;
      c.lineWidth = 3; c.strokeStyle = ok ? '#34C759' : 'rgba(255,255,255,.75)'; c.setLineDash(ok ? [] : [10, 8]);
      const prof = Math.abs(POSE_YAW[p]) > 60, narrow = Math.abs(POSE_YAW[p]) > 30 ? 0.26 : 0.34;
      c.beginPath(); c.ellipse(w / 2, hh * 0.47, w * (prof ? 0.2 : narrow), hh * 0.34, 0, 0, Math.PI * 2); c.stroke();
      c.setLineDash([]); c.lineWidth = 1.5; c.strokeStyle = 'rgba(255,255,255,.5)';
      c.beginPath(); c.moveTo(w / 2, hh * 0.1); c.lineTo(w / 2, hh * 0.84); c.moveTo(w * 0.25, hh * 0.4); c.lineTo(w * 0.75, hh * 0.4); c.stroke();
      if (r && !prof) { /* шкала отклонения по yaw: маркер уходит в ту же сторону, куда смещено лицо на зеркальном экране */
        const bw = w * 0.6, bx = (w - bw) / 2, by = hh * 0.93, off = clamp(r.ey / 30, -1, 1);
        c.strokeStyle = 'rgba(255,255,255,.6)'; c.lineWidth = 2; c.beginPath(); c.moveTo(bx, by); c.lineTo(bx + bw, by); c.stroke();
        c.fillStyle = ok ? '#34C759' : '#FF9500'; c.beginPath(); c.arc(w / 2 - off * bw / 2, by, 7, 0, Math.PI * 2); c.fill();
        c.fillStyle = '#fff'; c.fillRect(w / 2 - 1.5, by - 10, 3, 20);
      }
    };
    hl = Loop.add(drawGuide);
    const showStep = () => {
      stepToken++; state = { since: 0, counting: false, cancel: false, busy: false, last: null };
      const p = pose(); prog.value = 0; count.textContent = '';
      title.textContent = F.Capture.TEXT[p];
      sub.textContent = 'Ракурс ' + (idx + 1) + ' из ' + list.length + ': ' + POSE_NAMES[p] + (manualOnly() ? '. Профиль 90 градусов система не отслеживает: снимок делается вручную.' : '. Снимок сделается сам после удержания позы в допуске ' + F.tol + ' градусов.');
      btns.replaceChildren(
        h('button', { class: 'btn pri lg', onclick: () => { if (!state.busy) { state.since = 0; countdown(false); } } }, icon('camera'), 'Снять сейчас'),
        h('button', { class: 'btn sec', onclick: () => next() }, 'Пропустить ракурс'));
    };
    const next = () => { idx++; if (idx >= list.length) { title.textContent = 'Съёмка завершена'; sub.textContent = ''; prog.value = 1; btns.replaceChildren(h('button', { class: 'btn pri lg', onclick: () => s.close() }, 'Готово')); state = null; stepToken++; return; } showStep(); };
    const shoot = async () => {
      if (!state || state.busy) return; state.busy = true; const tok = stepToken;
      try {
        const cur = F.trk && F.trk.cur;
        const e = cur ? F.Pose.euler(cur) : null, tgt = POSE_YAW[pose()];
        const cv = this.grab(vid, cur, F.hideFace, F.meshOnShot);
        const blob = await this.encode(cv);
        if (closed || tok !== stepToken) return;
        const url = URL.createObjectURL(blob);
        const warn = (!manualOnly() && e && !(Math.abs(e.yaw - tgt) <= F.tol && Math.abs(e.pitch) <= F.tol && Math.abs(e.roll) <= F.tol)) ? 'Поза в момент снимка вне допуска. Лучше переснять.' : '';
        title.textContent = 'Проверьте снимок'; sub.textContent = warn || (F.hideFace ? 'Лицо скрыто пикселизацией: такой снимок нельзя использовать для сравнения.' : 'Нажмите «Отправить», если всё в порядке.');
        const img = h('img', { src: url, alt: 'Снимок', style: 'position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000' });
        stage.append(img);
        const done = () => { img.remove(); URL.revokeObjectURL(url); };
        btns.replaceChildren(
          h('button', { class: 'btn pri lg', onclick: async ev => {
            ev.target.disabled = true;
            try {
              await Api.req('POST', '/rooms/' + Api.room + '/frames', { raw: blob, headers: { 'Content-Type': 'image/jpeg', 'X-Pose': pose() } });
              Toast.show('Снимок отправлен врачу'); done(); next();
            } catch (er) { Toast.err(er); ev.target.disabled = false; }
          } }, 'Отправить'),
          h('button', { class: 'btn sec', onclick: () => { done(); showStep(); } }, 'Переснять'),
          h('button', { class: 'btn txt', onclick: () => { done(); next(); } }, 'Пропустить'));
        state.busy = true;
      } catch (er) { Toast.err(er); if (state) { state.busy = false; state.since = 0; state.counting = false; } }
    };
    Media.applyMax().then(() => { if (!closed) { title.textContent = F.Capture.TEXT[pose()]; } }).catch(() => { });
    showStep();
  }
};

/* ===== Face Mesh: сравнение до и после ===== */
FaceMesh.Compare = {
  l: null,
  RIGID: [33, 133, 362, 263, 6, 1, 168, 10],
  POINTS: [[105, 'Правая бровь'], [334, 'Левая бровь'], [33, 'Правый внешний угол глаза'], [263, 'Левый внешний угол глаза'], [1, 'Кончик носа'], [61, 'Правый угол рта'], [291, 'Левый угол рта'], [152, 'Подбородок']],
  async load(photo) {
    const url = await FaceMesh.thumb(photo.id);
    const img = new Image(); img.src = url; await img.decode();
    return img;
  },
  detect(img) {
    const r = this.l.detect(img);
    if (!r.faceLandmarks || !r.faceLandmarks.length) return null;
    const mats = r.facialTransformationMatrixes, bs = r.faceBlendshapes;
    let idx = 0, best = -1;
    r.faceLandmarks.forEach((f, i) => { let a = 1, b = 0, c = 1, d = 0; for (const p of f) { a = Math.min(a, p.x); b = Math.max(b, p.x); c = Math.min(c, p.y); d = Math.max(d, p.y); } if ((b - a) * (d - c) > best) { best = (b - a) * (d - c); idx = i; } });
    return { lm: r.faceLandmarks[idx], mat: mats && mats[idx] ? mats[idx].data : null, bs: bs && bs[idx] ? bs[idx].categories : null, vw: img.naturalWidth, vh: img.naturalHeight, t: performance.now(), multi: r.faceLandmarks.length > 1 };
  },
  /* подобие после -> до по жёстким точкам (метод наименьших квадратов, комплексная форма) */
  procrustes(A, B) {
    const pa = this.RIGID.map(i => ({ x: A.lm[i].x * A.vw, y: A.lm[i].y * A.vh })), pb = this.RIGID.map(i => ({ x: B.lm[i].x * B.vw, y: B.lm[i].y * B.vh }));
    const n = pa.length, ca = { x: pa.reduce((s, p) => s + p.x, 0) / n, y: pa.reduce((s, p) => s + p.y, 0) / n }, cb = { x: pb.reduce((s, p) => s + p.x, 0) / n, y: pb.reduce((s, p) => s + p.y, 0) / n };
    let re = 0, im = 0, den = 0;
    for (let i = 0; i < n; i++) { const ax = pa[i].x - ca.x, ay = pa[i].y - ca.y, bx = pb[i].x - cb.x, by = pb[i].y - cb.y; re += bx * ax + by * ay; im += bx * ay - by * ax; den += bx * bx + by * by; }
    const c = re / den, d = im / den;
    return { c, d, tx: ca.x - (c * cb.x - d * cb.y), ty: ca.y - (d * cb.x + c * cb.y), scale: Math.hypot(c, d), angle: deg(Math.atan2(d, c)) };
  },
  apply(T, x, y) { return { x: T.c * x - T.d * y + T.tx, y: T.d * x + T.c * y + T.ty }; },
  async open(before, after, pose) {
    const F = FaceMesh, self = this;
    if (F.unavailable) { Toast.show(F.unavailable); return; }
    let closed = false;
    const body = h('div', { class: 'stack' }, h('div', { class: 'sk', style: 'height:120px' }), h('p', { class: 'note', style: 'margin:0', text: 'Обработка снимков на вашем устройстве...' }));
    const s = Ui.sheet({ title: 'Сравнение: ' + (POSE_NAMES[pose] || pose), wide: true, onClose: () => { closed = true; if (self.l) { try { self.l.close(); } catch (e) { /* ignore */ } self.l = null; } } });
    s.el.append(body);
    try {
      const [ia, ib] = await Promise.all([this.load(before), this.load(after)]);
      if (closed) return;
      if (this.l) { try { this.l.close(); } catch (e) { /* ignore */ } }
      this.l = await F.Loader.create('IMAGE');
      const A = this.detect(ia), B = this.detect(ib);
      try { this.l.close(); } catch (e) { /* ignore */ } this.l = null;
      if (closed) return;
      if (!A || !B) { body.replaceChildren(h('p', { text: 'Лицо не найдено на ' + (!A && !B ? 'обоих снимках' : (!A ? 'снимке «до»' : 'снимке «после»')) + '. Если снимок был сохранён со скрытым лицом, сравнить его нельзя.' })); return; }
      const loc = this.calc(A, B, pose, F.tol, F.cal), side = (X, p) => ({ id: p.id, ts: p.ts, frame: F.Remote.pack(X, this.IDX, []) });
      const R = await F.Remote.pick('compare', { before: side(A, before), after: side(B, after), pose, tolerance: F.tol, calibration: F.cal }, loc, null,
        'Анализ временно недоступен, снимок сохранён и будет обработан позже. Сейчас показан расчёт на этом устройстве');
      if (closed) return;
      this.show(body, s, ia, ib, R, before, after, pose);
    } catch (e) { console.error(e); if (!closed) body.replaceChildren(h('p', { text: 'Не удалось выполнить сравнение: ' + ((e && e.message) || 'ошибка') })); }
  },
  IDX: [33, 133, 362, 263, 6, 1, 168, 10, 105, 334, 61, 291, 152, 468, 473],
  /* расчёт сравнения по точкам двух снимков; тот же результат отдаёт сервис vision (/v1/compare) */
  calc(A, B, pose, tol, cal) {
    const F = FaceMesh, tgt = POSE_YAW[pose], mm = pose === 'frontal' && cal && cal.ipd_mm ? cal : null;
    /* с учётом направления поворота: снимок «влево 45°», снятый вправо, не проходит */
    const chk = X => { const e = F.Pose.eulerStill(X); if (!e) return { ok: false, e: null }; const dy = Math.abs(e.yaw - tgt); return { ok: dy <= tol && Math.abs(e.pitch) <= tol && Math.abs(e.roll) <= tol, e }; };
    const ca = chk(A), cb = chk(B), poseOk = ca.ok && cb.ok && !!A.mat && !!B.mat;
    const T = this.procrustes(A, B);
    /* нормировка: по МЗР для анфаса, по высоте лица для остальных ракурсов */
    const dist = (X, i, j) => Math.hypot((X.lm[i].x - X.lm[j].x) * X.vw, (X.lm[i].y - X.lm[j].y) * X.vh);
    const normName = pose === 'frontal' ? 'ipd' : 'face_height', norm = normName === 'ipd' ? dist(A, 468, 473) : dist(A, 10, 152);
    const rows = this.POINTS.map(([i, label]) => {
      const pa = { x: A.lm[i].x * A.vw, y: A.lm[i].y * A.vh }, pb = this.apply(T, B.lm[i].x * B.vw, B.lm[i].y * B.vh);
      const dx = (pb.x - pa.x) / norm, dy = (pb.y - pa.y) / norm;
      const o = { id: i, label, dx, dy, px: pa.x, py: pa.y, qx: pb.x, qy: pb.y };
      if (mm) { o.dx_mm = dx * mm.ipd_mm; o.dy_mm = dy * mm.ipd_mm; }
      return o;
    });
    return { kind: 'compare', pose, tolerance: tol, T, rows, ca, cb, pose_ok: poseOk, norm: normName, mm, multi: [!!A.multi, !!B.multi] };
  },
  show(body, sheet, ia, ib, R, before, after, pose) {
    const F = FaceMesh, tol = R.tolerance, mm = R.mm, T = R.T, rows = R.rows, ca = R.ca, cb = R.cb, poseOk = R.pose_ok, normName = R.norm;
    const W = ia.naturalWidth, H = ia.naturalHeight;
    const cv = document.createElement('canvas'); cv.width = W; cv.height = H; cv.setAttribute('aria-label', 'Сравнение снимков до и после');
    const ctx = cv.getContext('2d');
    const st = { mode: 'wipe', pos: 50, arrows: true };
    const render = () => {
      ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, W, H); ctx.globalAlpha = 1;
      ctx.drawImage(ia, 0, 0, W, H);
      const drawAfter = () => { ctx.setTransform(T.c, T.d, -T.d, T.c, T.tx, T.ty); ctx.drawImage(ib, 0, 0); ctx.setTransform(1, 0, 0, 1, 0, 0); };
      if (st.mode === 'blend') { ctx.globalAlpha = st.pos / 100; drawAfter(); ctx.globalAlpha = 1; }
      else {
        const x = W * st.pos / 100; ctx.save(); ctx.beginPath(); ctx.rect(x, 0, W - x, H); ctx.clip(); ctx.fillStyle = '#000'; ctx.fillRect(0, 0, W, H); drawAfter(); ctx.restore();
        ctx.strokeStyle = '#fff'; ctx.lineWidth = Math.max(2, W / 400); ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
      }
      if (st.arrows) {
        const lw = Math.max(2, W / 500); ctx.lineWidth = lw; ctx.strokeStyle = '#FF9500'; ctx.fillStyle = '#FF9500';
        for (const r of rows) {
          const len = Math.hypot(r.qx - r.px, r.qy - r.py); ctx.beginPath(); ctx.arc(r.px, r.py, lw * 1.8, 0, Math.PI * 2); ctx.fill();
          if (len < 3) continue;
          ctx.beginPath(); ctx.moveTo(r.px, r.py); ctx.lineTo(r.qx, r.qy); ctx.stroke();
          const a = Math.atan2(r.qy - r.py, r.qx - r.px), hs = lw * 5;
          ctx.beginPath(); ctx.moveTo(r.qx, r.qy); ctx.lineTo(r.qx - hs * Math.cos(a - 0.4), r.qy - hs * Math.sin(a - 0.4)); ctx.lineTo(r.qx - hs * Math.cos(a + 0.4), r.qy - hs * Math.sin(a + 0.4)); ctx.closePath(); ctx.fill();
        }
      }
    };
    const slider = h('input', { type: 'range', min: 0, max: 100, value: 50, style: 'width:100%', 'aria-label': 'Положение шторки или прозрачность', oninput: e => { st.pos = +e.target.value; render(); } });
    const unit = normName === 'ipd' ? (mm ? 'мм' : 'доли МЗР') : 'доли высоты лица';
    const f = v => v == null ? '-' : (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(mm ? 1 : 3);
    const tbl = h('table', { class: 'tb' }, h('tr', null, h('th', { text: 'Точка (после относительно до, ' + unit + ')' }), h('th', { class: 'n', text: 'По горизонтали' }), h('th', { class: 'n', text: 'По вертикали' })),
      rows.map(r => h('tr', null, h('td', { text: r.label }), h('td', { class: 'n', text: f(mm ? r.dx_mm : r.dx) }), h('td', { class: 'n', text: f(mm ? r.dy_mm : r.dy) }))));
    const warn = [];
    if (!poseOk) warn.push('Поза на одном или обоих снимках вне допуска ' + tol + ' градусов от цели (до: ' + (ca.e ? 'yaw ' + ca.e.yaw.toFixed(0) + ', pitch ' + ca.e.pitch.toFixed(0) + ', roll ' + ca.e.roll.toFixed(0) : 'нет данных') + '; после: ' + (cb.e ? 'yaw ' + cb.e.yaw.toFixed(0) + ', pitch ' + cb.e.pitch.toFixed(0) + ', roll ' + cb.e.roll.toFixed(0) : 'нет данных') + '). Результат ориентировочный и в протокол не сохраняется.');
    if (R.multi && (R.multi[0] || R.multi[1])) warn.push('На снимке найдено более одного лица: использовано самое крупное.');
    if (pose === 'frontal' && !mm) warn.push('Калибровки нет: значения даны в долях межзрачкового расстояния, не в миллиметрах.');
    if (mm && mm.approx) warn.push('Калибровка по радужке приблизительная: миллиметры ориентировочные. Для точности используйте калибровку по карте.');
    if (pose !== 'frontal') warn.push('Для ракурса, отличного от анфаса, миллиметры не показываются: расстояния искажены поворотом.');
    const msg = h('div', { class: 'stack' }, warn.map(w => h('div', { class: 'banner warn', style: 'border-radius:10px', text: w })));
    const saveBtn = h('button', { class: 'btn pri', disabled: !poseOk || null, onclick: async e => {
      e.target.disabled = true;
      const payload = { kind: 'compare', pose_ok: true, source: R.source, job_id: R.job_id, pose, before_id: before.id, after_id: after.id, before_ts: before.ts, after_ts: after.ts, norm: normName, scale: T.scale, rotation_deg: T.angle, tolerance: tol, rows: rows.map(r => { const o = { id: r.id, label: r.label, dx: r.dx, dy: r.dy }; if (mm) { o.dx_mm = r.dx_mm; o.dy_mm = r.dy_mm; } return o; }), ts: Math.floor(Date.now() / 1000) };
      if (mm) payload.calibration = mm;
      try { await Api.post('/rooms/' + Api.room + '/measurements', payload); F.hist = null; Toast.show('Сравнение сохранено в протокол'); } catch (er) { Toast.err(er); e.target.disabled = false; }
    } }, 'Сохранить в протокол');
    const dl = h('button', { class: 'btn sec', onclick: () => cv.toBlob(b => { const a = h('a', { href: URL.createObjectURL(b), download: 'сравнение.png' }); document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500); }, 'image/png') }, icon('download'), 'Скачать изображение');
    body.replaceChildren(
      Ui.segmented([['wipe', 'Шторка'], ['blend', 'Наложение']], st.mode, m => { st.mode = m; render(); }, 'Режим сравнения'),
      h('div', { class: 'cmp' }, cv), slider,
      h('p', { class: 'note', style: 'margin:0', text: 'До: ' + fmtDT(before.ts) + '. После: ' + fmtDT(after.ts) + '. Снимок «после» совмещён со снимком «до» по жёстким точкам (масштаб ' + T.scale.toFixed(3) + ', поворот ' + T.angle.toFixed(1) + ' градусов).' }),
      h('label', { class: 'row', style: 'min-height:44px' }, h('input', { type: 'checkbox', checked: true, style: 'width:22px;height:22px', onchange: e => { st.arrows = e.target.checked; render(); } }), ' Стрелки смещения ключевых точек'),
      msg, tbl, h('p', { class: 'note', style: 'margin:0;font-weight:600', text: SIGN_SYM }),
      h('div', { class: 'row wrap' }, saveBtn, dl));
    render();
  }
};

/* ===== Face Mesh: отчёт ===== */
FaceMesh.Report = {
  async collect() {
    const F = FaceMesh, a = App.proto.appointment || {};
    const hist = await F.loadHistory().catch(() => []);
    const mine = k => { const l = hist.filter(m => m.kind === k && m.appt_id === a.id); return l.length ? l[l.length - 1].data : null; };
    const sym = F.latest.symmetry || mine('symmetry'), mob = F.latest.mobility || mine('mobility');
    const cmpList = hist.filter(m => m.kind === 'compare' && m.appt_id === a.id).map(m => m.data);
    const rep = { kind: 'report', center: a.center || Cfg.center, patient: a.patient_name || '', doctor: a.doctor || '', appointment_id: a.id, date: fmtDT(Math.floor(Date.now() / 1000)), ts: Math.floor(Date.now() / 1000), symmetry: sym || null, mobility: mob || null, compare: cmpList, photo_ids: F.photos.map(p => ({ id: p.id, pose: p.pose, ts: p.ts })) };
    const cal = (sym && sym.calibration) || (cmpList.find(c => c.calibration) || {}).calibration || F.cal;
    if (cal) rep.calibration = cal;
    /* ход консультации: только обезличенный текст, проверенный врачом */
    try {
      const tr = await Api.get('/rooms/' + Api.room + '/transcript');
      if (tr.deid && tr.deid.status === 'done' && tr.deid.confirmed) rep.transcript = { segments: tr.deid.segments.map(g => ({ role: g.role, t: g.t, text: g.text })) };
      else if (tr.deid && tr.deid.status !== 'none') rep.transcript_note = 'Расшифровка не включена: обезличенный текст ещё не проверен врачом.';
    } catch (e) { /* расшифровки нет или сервис недоступен: раздел не выводится */ }
    return { rep, hist };
  },
  async make(out) {
    const F = FaceMesh;
    out.replaceChildren(h('div', { class: 'sk', style: 'height:60px' }));
    const { rep, hist } = await this.collect();
    const trends = F.drawTrends(null, hist, true);
    F.lastReport = { rep, trends };
    this.show(out, F.lastReport);
  },
  node(rp) {
    const F = FaceMesh, rep = rp.rep;
    const kids = [h('h1', { text: rep.center }), h('h2', { text: 'Документирование лица: отчёт' }),
      h('table', null, h('tr', null, h('th', { text: 'Пациент' }), h('td', { text: rep.patient })), h('tr', null, h('th', { text: 'Дата' }), h('td', { text: rep.date })), h('tr', null, h('th', { text: 'Врач' }), h('td', { text: rep.doctor })),
        h('tr', null, h('th', { text: 'Калибровка размеров' }), h('td', { text: rep.calibration ? rep.calibration.label + '; межзрачковое расстояние ' + rep.calibration.ipd_mm.toFixed(1) + ' мм' : 'нет (значения в миллиметрах не приводятся)' })))];
    if (rep.symmetry) kids.push(h('h2', { text: 'Симметрия' }), F.symTable(rep.symmetry, true));
    if (rep.mobility) kids.push(h('h2', { text: 'Подвижность лица' }), F.mobTable(rep.mobility));
    if (rp.trends && rp.trends.length) { kids.push(h('h2', { text: 'Динамика между приёмами' })); for (const t of rp.trends) kids.push(h('img', { src: t.url, alt: t.title, style: 'max-height:60mm;display:block' })); }
    if (rep.compare && rep.compare.length) for (const c of rep.compare) {
      const mm = !!c.calibration, f = v => v == null ? '-' : (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(mm ? 1 : 3);
      kids.push(h('h2', { text: 'Сравнение до и после: ' + (POSE_NAMES[c.pose] || c.pose) }), h('p', { text: 'До: ' + fmtDT(c.before_ts) + '. После: ' + fmtDT(c.after_ts) + '. Единицы: ' + (c.norm === 'ipd' ? (mm ? 'мм' : 'доли МЗР') : 'доли высоты лица') + '.' }),
        h('table', { class: 'tb' }, h('tr', null, h('th', { text: 'Точка' }), h('th', { class: 'n', text: 'По горизонтали' }), h('th', { class: 'n', text: 'По вертикали' })), c.rows.map(r => h('tr', null, h('td', { text: r.label }), h('td', { class: 'n', text: f(mm ? r.dx_mm : r.dx) }), h('td', { class: 'n', text: f(mm ? r.dy_mm : r.dy) })))));
    }
    if (rep.transcript && rep.transcript.segments.length) {
      const t0 = rep.transcript.segments[0].t;
      kids.push(h('h2', { text: 'Ход консультации' }), h('p', { class: 'sm', text: 'Обезличенная расшифровка речи, проверена врачом. Метки в квадратных скобках скрывают личные данные.' }),
        h('table', { class: 'tb' }, rep.transcript.segments.map(g => { const s = Math.max(0, Math.round((g.t - t0) / 1000)); return h('tr', null, h('td', { class: 'n', text: pad2(Math.floor(s / 60)) + ':' + pad2(s % 60) }), h('td', { text: g.role === 'doctor' ? 'Врач' : 'Пациент' }), h('td', { text: g.text })); })));
    }
    if (rep.photo_ids.length) { kids.push(h('h2', { text: 'Снимки приёма' }), h('div', { class: 'thumbs' }, rep.photo_ids.map(p => F.thumbEl(p)))); }
    kids.push(h('p', { class: 'sm', text: 'Документ содержит числовые наблюдения для документирования. Он не является диагнозом и не заменяет клинической оценки врача. ' + SIGN_SYM + '. ' + SIGN_MOB + '.' }),
      h('div', { class: 'sign', text: 'Подпись врача ______________________ / ' + rep.doctor + ' /' }));
    return h('div', null, kids);
  },
  show(out, rp) {
    const F = FaceMesh, rep = rp.rep;
    const none = !rep.symmetry && !rep.mobility && !(rep.compare && rep.compare.length) && !rep.photo_ids.length && !rep.transcript;
    out.replaceChildren(
      h('p', { class: 'note', style: 'margin:0', text: 'Отчёт сформирован ' + rep.date + '. Симметрия: ' + (rep.symmetry ? 'есть' : 'нет') + '; подвижность: ' + (rep.mobility ? 'есть' : 'нет') + '; сравнений: ' + (rep.compare || []).length + '; снимков: ' + rep.photo_ids.length + '; графиков: ' + (rp.trends || []).length + '; расшифровка: ' + (rep.transcript ? 'есть (обезличена)' : rep.transcript_note ? 'не проверена' : 'нет') + '.' }),
      ...(none ? [h('div', { class: 'banner warn', style: 'border-radius:10px', text: 'В этом приёме пока нет данных для отчёта.' })] : []),
      h('div', { class: 'row wrap' },
        h('button', { class: 'btn pri', onclick: async () => {
          const node = this.node(rp);
          const imgs = [...node.querySelectorAll('img')];
          await Promise.all(imgs.map(i => i.decode ? i.decode().catch(() => { }) : Promise.resolve()));
          Print.go(node);
        } }, 'Печать или PDF'),
        h('button', { class: 'btn sec', onclick: () => Ui.download('отчёт-лицо-' + (rep.appointment_id || 'приём') + '.json', JSON.stringify(rep, null, 2)) }, icon('download'), 'Скачать JSON'),
        h('button', { class: 'btn sec', onclick: async e => { e.target.disabled = true; try { await Api.post('/rooms/' + Api.room + '/measurements', rep); Toast.show('Отчёт сохранён в протокол'); } catch (er) { Toast.err(er); } e.target.disabled = false; } }, 'Сохранить в протокол')));
  }
};
