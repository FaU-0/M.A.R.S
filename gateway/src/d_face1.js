
/* ===== Face Mesh: ядро ===== */
const LM = {
  OVAL: [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109],
  REYE: [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246], LEYE: [263, 249, 390, 373, 374, 380, 381, 382, 362, 398, 384, 385, 386, 387, 388, 466],
  BROWS: [[70, 63, 105, 66, 107], [46, 53, 52, 65, 55], [300, 293, 334, 296, 336], [276, 283, 282, 295, 285]],
  LIPO: [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267, 0, 37, 39, 40, 185], LIPI: [78, 95, 88, 178, 87, 14, 317, 402, 318, 324, 308, 415, 310, 311, 312, 13, 82, 81, 80, 191],
  RIRIS: [469, 470, 471, 472], LIRIS: [474, 475, 476, 477],
  RC: 468, LC: 473, REI: 133, LEI: 362, REO: 33, LEO: 263, RM: 61, LM: 291, RB: 105, LB: 334, NOSE: 1, CHIN: 152, BRIDGE: 6, RALA: 129, LALA: 358, RCH: 234, LCH: 454, TOP: 10
};
const FACE_MAX = 2; /* 2 лица: чтобы выбрать наибольшее и предупредить о втором */
/* положительный yaw: пациент повернул голову к своему левому плечу (знак проверяется автоматически по положению носа) */
const POSE_YAW = { frontal: 0, left45: 45, right45: -45, left90: 90, right90: -90 };
const fitMap = (vw, vh, cw, ch, fit) => { const s = fit === 'cover' ? Math.max(cw / vw, ch / vh) : Math.min(cw / vw, ch / vh); return { s, ox: (cw - vw * s) / 2, oy: (ch - vh * s) / 2 }; };
const deg = r => r * 180 / Math.PI;

const FaceMesh = {
  role: null, consent: false, mode: 'off', coach: false, autocrop: false, secondary: false, tol: 5, poses: ['frontal', 'left45', 'right45', 'left90', 'right90'],
  trk: null, trk2: null, els: {}, cal: null, latest: {}, info: null, unavailable: '', busy: 0, subs: new Set(), lastAssess: 0, lastSend: 0, motion: [], timers: [], photos: [], dpr: 1, sizes: new Map(), accent: '#007AFF', accT: 0,
  Loader: {}, Overlay: {}, Pose: {}, Symmetry: {}, Mobility: {}, Capture: {}, Compare: {}, Calibrate: {}, Privacy: {}, Report: {}, Remote: {}
};

/* --- Remote: расчёт метрик в сервисе vision через очередь заданий gateway.
   Если сервис недоступен или не успел ответить, используется расчёт на устройстве. --- */
FaceMesh.Remote = {
  WAIT: 6000, down: false,
  /* компактный кадр: только нужные точки и коэффициенты */
  pack(cur, idx, names) {
    const lm = {}, bs = {};
    if (idx) for (const i of idx) { const p = cur.lm[i]; if (p) lm[i] = [p.x, p.y, p.z]; }
    if (cur.bs) for (const c of cur.bs) if (!names || names.includes(c.categoryName)) bs[c.categoryName] = c.score;
    return { lm, mat: cur.mat ? Array.from(cur.mat) : null, bs, vw: cur.vw, vh: cur.vh, multi: !!cur.multi };
  },
  key() { return crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2, 12); },
  /* результат vision или null; ошибка расчёта (например, мало кадров) пробрасывается */
  async run(kind, input, signal) {
    if (!Api.room) return null;
    let job;
    try { job = await Api.post('/rooms/' + Api.room + '/analyze', { kind, key: this.key(), input }, { signal }); }
    catch (e) { if (e && e.name === 'AbortError') throw e; if (!(e && e.status === 503)) console.warn('vision', e); return null; }
    const t0 = performance.now();
    while (job.status === 'queued' || job.status === 'running') {
      if (performance.now() - t0 > this.WAIT) return null;
      await new Promise(r => setTimeout(r, 400));
      job = await Api.get('/rooms/' + Api.room + '/jobs/' + job.id, { signal }).catch(e => { if (e && e.name === 'AbortError') throw e; return job; });
    }
    if (job.status === 'done') return job.result;
    throw new Error((job.error && job.error.message) || 'Ошибка анализа');
  },
  async pick(kind, input, local, signal, downMsg) {
    const r = await this.run(kind, input, signal);
    if (r) { this.down = false; r.source = 'vision'; return r; }
    if (!this.down) Toast.show(downMsg || 'Сервис анализа временно недоступен: расчёт выполнен на этом устройстве');
    this.down = true; local.source = 'device';
    return local;
  }
};

/* --- Loader: локальные файлы MediaPipe с /vendor --- */
FaceMesh.Loader = {
  mod: null, fileset: null, conn: null,
  async load() {
    if (this.mod) return this.mod;
    if (!Cfg.mediapipe) throw new Error('Файлы MediaPipe не найдены на сервере');
    this.mod = await import('/vendor/vision_bundle.mjs');
    this.fileset = await this.mod.FilesetResolver.forVisionTasks('/vendor/wasm');
    this.conn = this.mod.FaceLandmarker.FACE_LANDMARKS_TESSELATION || null;
    return this.mod;
  },
  async create(running) {
    const m = await this.load();
    const mk = delegate => m.FaceLandmarker.createFromOptions(this.fileset, {
      baseOptions: { modelAssetPath: '/vendor/face_landmarker.task', delegate },
      runningMode: running || 'VIDEO', numFaces: FACE_MAX, outputFaceBlendshapes: true, outputFacialTransformationMatrixes: true
    });
    try { return await mk('GPU'); }
    catch (e) { console.warn('GPU недоступен, использую CPU', e); return await mk('CPU'); }
  }
};

/* --- Трекер: цикл detectForVideo --- */
class FMTracker {
  constructor(video, opt) {
    this.v = video; this.fps = opt.fps || 24; this.maxFps = opt.fps || 24; this.minFps = 10; this.ema = 0; this.last = 0; this.lastTs = 0;
    this.run = false; this.pending = false; this.cur = null; this.lastSeen = 0; this.faces = 0; this.errors = 0; this.onFrame = opt.onFrame || null; this.l = null; this.h = 0;
  }
  async start() {
    if (this.run) return;
    this.run = true;
    try { this.l = await FaceMesh.Loader.create('VIDEO'); } catch (e) { this.run = false; throw e; }
    if (!this.run) { try { this.l.close(); } catch (e) { /* ignore */ } return; }
    this.kick();
  }
  kick() {
    if (!this.run || this.pending) return;
    this.pending = true;
    const cb = () => { this.pending = false; this.tick(); };
    if (this.v.requestVideoFrameCallback) this.h = this.v.requestVideoFrameCallback(cb); else this.h = requestAnimationFrame(cb);
  }
  lost() { return !this.cur || performance.now() - this.lastSeen > 1500; }
  tick() {
    if (!this.run) return;
    const v = this.v;
    if (document.hidden || v.readyState < 2 || v.paused || !v.videoWidth) { setTimeout(() => this.kick(), 250); return; }
    const now = performance.now();
    if (now - this.last < 1000 / this.fps - 2) { this.kick(); return; }
    this.last = now;
    try {
      const ts = Math.max(now, this.lastTs + 1); this.lastTs = ts;
      const r = this.l.detectForVideo(v, ts);
      const dt = performance.now() - now;
      this.ema = this.ema * 0.8 + dt * 0.2;
      if (this.ema > 1000 / this.fps * 0.8 && this.fps > this.minFps) this.fps = Math.max(this.minFps, this.fps - 2);
      else if (this.ema < 1000 / this.fps * 0.35 && this.fps < this.maxFps) this.fps = Math.min(this.maxFps, this.fps + 1);
      this.consume(r, now);
      this.errors = 0;
    } catch (e) {
      console.error('Face Mesh', e);
      if (++this.errors > 5) { this.stop(); FaceMesh.fail('Модуль Face Mesh остановлен из-за ошибки обработки видео'); return; }
    }
    this.kick();
  }
  consume(r, now) {
    const faces = r.faceLandmarks || []; this.faces = faces.length;
    if (!faces.length) { this.cur = null; return; }
    let idx = 0, best = -1;
    faces.forEach((f, i) => { let a = 1, b = 0, c = 1, d = 0; for (const p of f) { if (p.x < a) a = p.x; if (p.x > b) b = p.x; if (p.y < c) c = p.y; if (p.y > d) d = p.y; } const ar = (b - a) * (d - c); if (ar > best) { best = ar; idx = i; } });
    const mats = r.facialTransformationMatrixes, bs = r.faceBlendshapes;
    this.cur = { lm: faces[idx], bs: bs && bs[idx] ? bs[idx].categories : null, mat: mats && mats[idx] ? mats[idx].data : null, t: now, vw: this.v.videoWidth, vh: this.v.videoHeight, multi: faces.length > 1 };
    this.lastSeen = now;
    if (this.onFrame) this.onFrame(this.cur, this);
  }
  stop() {
    this.run = false;
    try { if (this.v.cancelVideoFrameCallback && this.h) this.v.cancelVideoFrameCallback(this.h); } catch (e) { /* ignore */ }
    try { if (this.l) this.l.close(); } catch (e) { /* ignore */ }
    this.l = null; this.cur = null;
  }
}

/* --- Pose: углы, качество позы, тренер --- */
FaceMesh.Pose = {
  flip: 1, agree: 0, disagree: 0, cv1: null, cv2: null,
  rot(mat) {
    const sx = Math.hypot(mat[0], mat[1], mat[2]) || 1, sy = Math.hypot(mat[4], mat[5], mat[6]) || 1, sz = Math.hypot(mat[8], mat[9], mat[10]) || 1;
    return [[mat[0] / sx, mat[4] / sy, mat[8] / sz], [mat[1] / sx, mat[5] / sy, mat[9] / sz], [mat[2] / sx, mat[6] / sy, mat[10] / sz]];
  },
  euler(cur) {
    if (!cur || !cur.mat) return null;
    const R = this.rot(cur.mat);
    let yaw = deg(Math.atan2(R[0][2], R[2][2])), pitch = deg(Math.asin(clamp(-R[1][2], -1, 1))), roll = deg(Math.atan2(R[1][0], R[1][1]));
    /* самопроверка знака yaw по положению кончика носа между внешними уголками глаз */
    const lm = cur.lm, wid = lm[LM.LEO].x - lm[LM.REO].x;
    if (Math.abs(wid) > 1e-3) {
      const yl = (lm[LM.NOSE].x - (lm[LM.REO].x + lm[LM.LEO].x) / 2) / wid;
      if (Math.abs(yl) > 0.12 && Math.abs(yaw) > 10) { if (Math.sign(yl) === Math.sign(yaw)) this.agree++; else this.disagree++; if (this.agree + this.disagree >= 8) this.flip = this.disagree > this.agree ? -1 : 1; if (this.agree + this.disagree > 200) { this.agree = this.agree >> 1; this.disagree = this.disagree >> 1; } }
    }
    return { yaw: yaw * this.flip, pitch, roll };
  },
  geom(cur) {
    const lm = cur.lm; let a = 1, b = 0, c = 1, d = 0;
    for (const i of LM.OVAL) { const p = lm[i]; if (p.x < a) a = p.x; if (p.x > b) b = p.x; if (p.y < c) c = p.y; if (p.y > d) d = p.y; }
    return { x0: a, x1: b, y0: c, y1: d, size: Math.abs(lm[LM.CHIN].y - lm[LM.TOP].y), cx: (a + b) / 2, cy: (c + d) / 2 };
  },
  stats(video, g) {
    const W = video.videoWidth, H = video.videoHeight; if (!W) return null;
    if (!this.cv2) { this.cv2 = document.createElement('canvas'); this.cv2.width = this.cv2.height = 96; this.cv1 = document.createElement('canvas'); }
    const x2 = this.cv2.getContext('2d', { willReadFrequently: true });
    const sx = clamp(g.x0 * W, 0, W - 2), sy = clamp(g.y0 * H, 0, H - 2), sw = clamp((g.x1 - g.x0) * W, 2, W - sx), sh = clamp((g.y1 - g.y0) * H, 2, H - sy);
    x2.drawImage(video, sx, sy, sw, sh, 0, 0, 96, 96);
    const d = x2.getImageData(0, 0, 96, 96).data, gr = new Float32Array(9216);
    let sum = 0, sq = 0, over = 0;
    for (let i = 0, j = 0; i < d.length; i += 4, j++) { const y = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]; gr[j] = y; sum += y; sq += y * y; if (y > 245) over++; }
    const n = 9216, mean = sum / n, std = Math.sqrt(Math.max(0, sq / n - mean * mean));
    let ls = 0, lq = 0, ln = 0;
    for (let y = 1; y < 95; y++) for (let x = 1; x < 95; x++) { const i = y * 96 + x, l = -4 * gr[i] + gr[i - 1] + gr[i + 1] + gr[i - 96] + gr[i + 96]; ls += l; lq += l * l; ln++; }
    const sharp = lq / ln - (ls / ln) * (ls / ln);
    const x1 = this.cv1.getContext('2d', { willReadFrequently: true }); const w1 = 80, h1 = Math.max(8, Math.round(80 * H / W)); this.cv1.width = w1; this.cv1.height = h1;
    x1.drawImage(video, 0, 0, w1, h1); const e = x1.getImageData(0, 0, w1, h1).data; let bs = 0, bn = 0; const band = Math.floor(w1 * 0.12);
    for (let y = 0; y < h1; y++) for (let x = 0; x < w1; x++) { if (x < band || x >= w1 - band) { const i = (y * w1 + x) * 4; bs += 0.299 * e[i] + 0.587 * e[i + 1] + 0.114 * e[i + 2]; bn++; } }
    return { mean, std, over: over / n, sharp, bg: bn ? bs / bn : mean };
  },
  moving() {
    const m = FaceMesh.motion; if (m.length < 4) return false;
    let a = 1, b = 0, c = 1, d = 0; for (const p of m) { if (p.x < a) a = p.x; if (p.x > b) b = p.x; if (p.y < c) c = p.y; if (p.y > d) d = p.y; }
    return Math.max(b - a, d - c) > 0.02;
  },
  assess(cur, video, tol) {
    const e = this.euler(cur), g = this.geom(cur), st = this.stats(video, g);
    if (!e) return null;
    const aMax = Math.max(Math.abs(e.yaw), Math.abs(e.pitch), Math.abs(e.roll));
    let hint = '', level = 'good';
    if (aMax > tol * 2.5) { hint = 'Повернитесь к камере'; level = 'bad'; }
    else if (g.size < 0.2) { hint = 'Подвиньтесь ближе'; level = 'bad'; }
    else if (g.size < 0.3) { hint = 'Подвиньтесь ближе'; level = 'medium'; }
    else if (g.size > 0.8) { hint = 'Отодвиньтесь от камеры'; level = 'medium'; }
    else if (Math.abs(g.cx - 0.5) > 0.18 || Math.abs(g.cy - 0.5) > 0.18) { hint = 'Расположитесь по центру кадра'; level = 'medium'; }
    else if (st && st.mean < 70) { hint = 'Добавьте света спереди'; level = 'medium'; }
    else if (st && st.bg - st.mean > 45) { hint = 'Уберите свет за спиной'; level = 'medium'; }
    else if (st && st.over > 0.08) { hint = 'Слишком яркий свет на лице'; level = 'medium'; }
    else if (this.moving()) { hint = 'Держите голову неподвижно'; level = 'medium'; }
    else if (st && st.sharp < 12) { hint = 'Изображение размыто, держите голову неподвижно'; level = 'medium'; }
    else if (aMax > tol) { hint = 'Выровняйте голову'; level = 'medium'; }
    return Object.assign({ level, hint, tol, size: g.size, cx: g.cx, cy: g.cy, t: performance.now() }, e, st ? { mean: st.mean, over: st.over, sharp: st.sharp, bg: st.bg } : {});
  },
  inTol(e, tol) { return !!e && Math.abs(e.yaw) <= tol && Math.abs(e.pitch) <= tol && Math.abs(e.roll) <= tol; }
};

/* --- Overlay: сетка и контуры, HiDPI, зеркало --- */
FaceMesh.Overlay = {
  ro: null,
  init() {
    const F = FaceMesh; this.ro && this.ro.disconnect();
    this.ro = new ResizeObserver(es => { for (const en of es) this.fit(en.target); });
    for (const c of [F.els.ovR, F.els.ovL]) if (c) { this.ro.observe(c); this.fit(c); }
  },
  fit(c) { const dpr = window.devicePixelRatio || 1; const w = Math.max(1, Math.round(c.clientWidth * dpr)), h2 = Math.max(1, Math.round(c.clientHeight * dpr)); if (c.width !== w || c.height !== h2) { c.width = w; c.height = h2; } },
  poly(ctx, lm, idx, close, m, vw, vh) {
    ctx.beginPath();
    idx.forEach((i, k) => { const x = m.ox + lm[i].x * vw * m.s, y = m.oy + lm[i].y * vh * m.s; if (k) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    if (close) ctx.closePath(); ctx.stroke();
  },
  /* рисует сетку или контуры в контексте ctx; m: {s,ox,oy} - отображение кадра */
  draw(ctx, cur, vw, vh, mode, m, color) {
    const lm = cur.lm; ctx.lineWidth = 1; ctx.strokeStyle = color; ctx.lineJoin = 'round';
    if (mode === 'grid' && FaceMesh.Loader.conn) {
      ctx.globalAlpha = 0.45; ctx.beginPath();
      for (const c of FaceMesh.Loader.conn) { const a = lm[c.start], b = lm[c.end]; if (!a || !b) continue; ctx.moveTo(m.ox + a.x * vw * m.s, m.oy + a.y * vh * m.s); ctx.lineTo(m.ox + b.x * vw * m.s, m.oy + b.y * vh * m.s); }
      ctx.stroke(); ctx.globalAlpha = 1; return;
    }
    ctx.globalAlpha = 0.9;
    this.poly(ctx, lm, LM.OVAL, true, m, vw, vh); this.poly(ctx, lm, LM.REYE, true, m, vw, vh); this.poly(ctx, lm, LM.LEYE, true, m, vw, vh);
    this.poly(ctx, lm, LM.LIPO, true, m, vw, vh); this.poly(ctx, lm, LM.LIPI, true, m, vw, vh);
    for (const b of LM.BROWS) this.poly(ctx, lm, b, false, m, vw, vh);
    if (lm.length > 477) { this.poly(ctx, lm, LM.RIRIS, true, m, vw, vh); this.poly(ctx, lm, LM.LIRIS, true, m, vw, vh); }
    ctx.globalAlpha = 1;
  },
  frame() {
    const F = FaceMesh, t = performance.now();
    if (t - F.accT > 1500) { F.accT = t; F.accent = getComputedStyle(document.documentElement).getPropertyValue('--acc').trim() || '#007AFF'; }
    const dpr = window.devicePixelRatio || 1;
    const items = [[F.els.ovR, F.els.remote, 'remote', 'contain'], [F.els.ovL, F.els.local, 'local', 'cover']];
    for (const [c, v, which, fit] of items) {
      if (!c || !v) continue;
      const ctx = c.getContext('2d'); ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, c.width, c.height);
      if (F.mode === 'off') continue;
      const trk = F.trackerOf(which); if (!trk || trk.lost() || !trk.cur) continue;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const cur = trk.cur, m = fitMap(cur.vw, cur.vh, c.width / dpr, c.height / dpr, fit);
      this.draw(ctx, cur, cur.vw, cur.vh, F.mode, m, F.accent);
    }
  }
};

/* --- Privacy: скрытие лица пикселизацией до сохранения --- */
FaceMesh.Privacy = {
  hide(ctx, cur, vw, vh) {
    const lm = cur.lm; const cv = ctx.canvas;
    ctx.save(); ctx.beginPath();
    LM.OVAL.forEach((i, k) => { const x = lm[i].x * vw, y = lm[i].y * vh; if (k) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    ctx.closePath(); ctx.clip();
    let a = vw, b = 0; for (const i of LM.OVAL) { a = Math.min(a, lm[i].x * vw); b = Math.max(b, lm[i].x * vw); }
    const bs = Math.max(10, Math.round((b - a) / 10));
    const tmp = document.createElement('canvas'); tmp.width = Math.max(1, Math.round(vw / bs)); tmp.height = Math.max(1, Math.round(vh / bs));
    const tc = tmp.getContext('2d'); tc.drawImage(cv, 0, 0, tmp.width, tmp.height);
    ctx.imageSmoothingEnabled = false; ctx.drawImage(tmp, 0, 0, tmp.width, tmp.height, 0, 0, vw, vh); ctx.imageSmoothingEnabled = true;
    ctx.restore();
  }
};

/* --- Symmetry: метрики после приведения к анфасу --- */
FaceMesh.Symmetry = {
  IDX: [468, 473, 133, 362, 33, 263, 61, 291, 105, 334, 1, 152, 234, 454],
  derot(cur) {
    const R = FaceMesh.Pose.rot(cur.mat), A = [[R[0][0], -R[0][1], -R[0][2]], [-R[1][0], R[1][1], R[1][2]], [-R[2][0], R[2][1], R[2][2]]];
    const pts = this.IDX.map(i => ({ i, x: cur.lm[i].x * cur.vw, y: cur.lm[i].y * cur.vh, z: cur.lm[i].z * cur.vw }));
    const c = pts.reduce((o, p) => ({ x: o.x + p.x / pts.length, y: o.y + p.y / pts.length, z: o.z + p.z / pts.length }), { x: 0, y: 0, z: 0 });
    const P = {};
    for (const p of pts) { const dx = p.x - c.x, dy = p.y - c.y, dz = p.z - c.z; P[p.i] = { x: A[0][0] * dx + A[1][0] * dy + A[2][0] * dz, y: A[0][1] * dx + A[1][1] * dy + A[2][1] * dz }; }
    return P;
  },
  one(cur) {
    const P = this.derot(cur), R = P[468], L = P[473];
    const ex = L.x - R.x, ey = L.y - R.y, ipd = Math.hypot(ex, ey); if (ipd < 5) return null;
    const ux = ex / ipd, uy = ey / ipd, nx = -uy, ny = ux, Mx = (R.x + L.x) / 2, My = (R.y + L.y) / 2;
    const along = p => ((p.x - Mx) * ux + (p.y - My) * uy) / ipd, down = p => ((p.x - Mx) * nx + (p.y - My) * ny) / ipd;
    const v = { ipd, tilt: deg(Math.atan2(ey, ex)), nose: along(P[1]), chin: along(P[152]) };
    v.eoR = -along(P[33]); v.eoL = along(P[263]); v.eiR = -along(P[133]); v.eiL = along(P[362]);
    v.mR = -along(P[61]); v.mL = along(P[291]); v.bR = -down(P[105]); v.bL = -down(P[334]);
    v.cR = -down(P[61]); v.cL = -down(P[291]); v.wR = -along(P[234]); v.wL = along(P[454]);
    return v;
  },
  PAIRS: [['eo', 'Внешние уголки глаз: расстояние до средней линии', 'eoR', 'eoL'], ['ei', 'Внутренние уголки глаз: расстояние до средней линии', 'eiR', 'eiL'], ['mouth', 'Уголки рта: расстояние до средней линии', 'mR', 'mL'],
    ['brow', 'Высота бровей над линией глаз', 'bR', 'bL'], ['mcor', 'Высота уголков рта относительно линии глаз', 'cR', 'cL'], ['half', 'Ширина половин лица', 'wR', 'wL']],
  build(samples, cal) {
    const med = k => median(samples.map(s => s[k])), ipdPx = med('ipd');
    const k = cal && cal.ipd_mm ? cal.ipd_mm : null;
    const rows = this.PAIRS.map(([id, label, a, b]) => {
      const r = med(a), l = med(b), o = { id, label, right: r, left: l, diff: r - l };
      if (k) { o.right_mm = r * k; o.left_mm = l * k; o.diff_mm = (r - l) * k; }
      return o;
    });
    const singles = [{ id: 'tilt', label: 'Наклон линии глаз', value: med('tilt'), unit: 'deg' }, { id: 'nose', label: 'Смещение кончика носа от средней линии', value: med('nose'), unit: 'ipd' }, { id: 'chin', label: 'Смещение подбородка от средней линии', value: med('chin'), unit: 'ipd' }];
    if (k) for (const s of singles) if (s.unit === 'ipd') s.value_mm = s.value * k;
    const out = { kind: 'symmetry', pose_ok: true, samples: samples.length, ipd_px: ipdPx, tolerance: FaceMesh.tol, rows, singles, ts: Math.floor(Date.now() / 1000) };
    if (k) out.calibration = cal;
    return out;
  },
  /* окно 2.5 с: в выборку попадают только кадры с позой в допуске */
  run(onProgress, signal) {
    const F = FaceMesh, trk = F.trk, secs = 2.5, need = 12;
    return new Promise((res, rej) => {
      if (!trk) return rej(new Error('Модуль не запущен'));
      const S = [], FR = []; let t0 = 0, fail = null; const t00 = performance.now();
      const fn = cur => {
        if (signal && signal.aborted) { done(new Error('Отменено')); return; }
        const e = F.Pose.euler(cur);
        if (!F.Pose.inTol(e, F.tol) || cur.multi && false) { t0 = 0; S.length = 0; FR.length = 0; onProgress(0, 'Выровняйте голову: ' + (e ? 'yaw ' + e.yaw.toFixed(0) + ', pitch ' + e.pitch.toFixed(0) + ', roll ' + e.roll.toFixed(0) : '')); return; }
        if (!t0) t0 = performance.now();
        const v = this.one(cur); if (v) { S.push(v); FR.push(F.Remote.pack(cur, this.IDX, [])); }
        const p = Math.min(1, (performance.now() - t0) / (secs * 1000)); onProgress(p, 'Держите позу');
        if (p >= 1) { if (S.length >= need) done(null); else { t0 = 0; S.length = 0; FR.length = 0; } }
      };
      const tm = setInterval(() => { if (performance.now() - t00 > 25000) done(new Error('Не удалось получить устойчивую позу в допуске за отведённое время')); else if (trk.lost()) { t0 = 0; S.length = 0; FR.length = 0; onProgress(0, 'Лицо не найдено'); } }, 400);
      /* кадры для сервиса vision: неперечисляемое поле, в протокол не попадает */
      const done = err => { clearInterval(tm); F.subs.delete(fn); if (err) return rej(err); const out = this.build(S, F.cal); Object.defineProperty(out, '_frames', { value: FR }); res(out); };
      F.subs.add(fn);
    });
  }
};

/* --- Mobility: подвижность по blendshapes --- */
FaceMesh.Mobility = {
  TASKS: [
    { id: 'smile', label: 'Улыбка', cmd: 'Широко улыбнитесь и держите улыбку', r: 'mouthSmileRight', l: 'mouthSmileLeft' },
    { id: 'brow', label: 'Подъём бровей', cmd: 'Поднимите брови как можно выше', r: 'browOuterUpRight', l: 'browOuterUpLeft' },
    { id: 'eye', label: 'Закрытие глаз', cmd: 'Крепко зажмурьте глаза', r: 'eyeBlinkRight', l: 'eyeBlinkLeft' },
    { id: 'cheek', label: 'Надувание щёк', cmd: 'Надуйте щёки и держите', single: 'cheekPuff' },
    { id: 'mouth', label: 'Открытие рта', cmd: 'Широко откройте рот', single: 'jawOpen' },
    { id: 'lips', label: 'Поджатие губ', cmd: 'Плотно сожмите губы', r: 'mouthPressRight', l: 'mouthPressLeft' }
  ],
  val(cur, name) { if (!cur.bs) return 0; const c = cur.bs.find(x => x.categoryName === name); return c ? c.score : 0; },
  /* одно задание: максимум коэффициента за 4 с в кадрах с допустимой позой */
  runTask(task, onProgress, signal) {
    const F = FaceMesh, secs = 4, tol = Math.max(F.tol * 2, 10);
    return new Promise((res, rej) => {
      if (!F.trk) return rej(new Error('Модуль не запущен'));
      const t0 = performance.now(), FR = [], names = task.single ? [task.single] : [task.r, task.l]; let mr = 0, ml = 0, ms = 0, n = 0;
      const fn = cur => {
        if (signal && signal.aborted) { fin(new Error('Отменено')); return; }
        if (FR.length < 400) FR.push(F.Remote.pack(cur, [LM.REO, LM.LEO, LM.NOSE], names));
        const e = F.Pose.euler(cur);
        if (!F.Pose.inTol(e, tol)) { onProgress((performance.now() - t0) / (secs * 1000), 'Смотрите в камеру'); return; }
        n++;
        if (task.single) ms = Math.max(ms, this.val(cur, task.single)); else { mr = Math.max(mr, this.val(cur, task.r)); ml = Math.max(ml, this.val(cur, task.l)); }
        onProgress((performance.now() - t0) / (secs * 1000), task.cmd);
      };
      const tm = setInterval(() => { if (performance.now() - t0 >= secs * 1000) fin(null); }, 100);
      const fin = err => {
        clearInterval(tm); F.subs.delete(fn);
        if (err) return rej(err);
        if (n < 10) return rej(new Error('Недостаточно кадров с лицом в допустимой позе. Повторите задание.'));
        const o = { id: task.id, label: task.label };
        if (task.single) o.single = ms; else { o.right = mr; o.left = ml; o.ratio = Math.max(mr, ml) > 0.05 ? Math.min(mr, ml) / Math.max(mr, ml) : null; }
        Object.defineProperty(o, '_frames', { value: FR });
        res(o);
      };
      F.subs.add(fn);
    });
  }
};

/* --- Calibrate: масштаб в миллиметрах --- */
FaceMesh.Calibrate = {
  CARD_W: 85.6, CARD_H: 53.98, IRIS_MM: 11.7,
  ipdPx(cur) { const v = FaceMesh.Symmetry.one(cur); return v ? v.ipd : null; },
  irisPx(cur) {
    const P = []; const d = ids => { let best = 0; for (const a of ids) for (const b of ids) best = Math.max(best, Math.hypot((cur.lm[a].x - cur.lm[b].x) * cur.vw, (cur.lm[a].y - cur.lm[b].y) * cur.vh)); return best; };
    if (cur.lm.length < 478) return null; P.push(d(LM.RIRIS), d(LM.LIRIS)); return (P[0] + P[1]) / 2;
  },
  fromIris(samples) {
    const iris = median(samples.map(s => s.iris)), ipd = median(samples.map(s => s.ipd));
    if (!iris || !ipd) return null;
    const mmpp = this.IRIS_MM / iris;
    return { source: 'iris', label: 'по радужке (приблизительно, погрешность выше)', approx: true, mm_per_px: mmpp, ipd_mm: ipd * mmpp, ts: Math.floor(Date.now() / 1000) };
  },
  /* pts: 4 угла карты по порядку: левый верх, правый верх, правый низ, левый низ (в пикселях кадра) */
  fromCard(pts, ipdPx) {
    const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
    const w = (dist(pts[0], pts[1]) + dist(pts[3], pts[2])) / 2, hh = (dist(pts[0], pts[3]) + dist(pts[1], pts[2])) / 2;
    if (w < 20 || hh < 12) return null;
    const mw = this.CARD_W / w, mh = this.CARD_H / hh, mmpp = (mw + mh) / 2, dev = Math.abs(mw - mh) / mmpp;
    return { source: 'card', label: 'по банковской карте 85,6 мм', approx: false, mm_per_px: mmpp, ipd_mm: ipdPx * mmpp, skew: dev, ts: Math.floor(Date.now() / 1000) };
  }
};
