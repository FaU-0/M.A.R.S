
/* ===== Face Mesh: управление, панели, съёмка, сравнение, отчёт ===== */
const SIGN_SYM = 'Числа для документирования, не диагноз';
const SIGN_MOB = 'Наблюдение для врача, не оценка по клинической шкале';
const fx = (v, n) => (v == null || !isFinite(v)) ? '-' : (v >= 0 ? '' : '−') + Math.abs(v).toFixed(n == null ? 3 : n);

Object.assign(FaceMesh, {
  blobs: new Map(), hist: null, remotePose: null, frameEl: null, cs: { s: 1, x: 0, y: 0 }, tg: { s: 1, x: 0, y: 0 }, starting: false, pp: null, dp: null, dTab: 'metrics', cmp: { pose: 'frontal', before: null, after: null }, ctl: null,
  trackerOf(which) { const doc = this.role === 'doctor'; return which === 'remote' ? (doc ? this.trk : this.trk2) : (doc ? this.trk2 : this.trk); },
  mainVideo() { return this.role === 'doctor' ? this.els.remote : this.els.local; },
  otherVideo() { return this.role === 'doctor' ? this.els.local : this.els.remote; },
  fail(msg) {
    this.unavailable = msg || 'Модуль Face Mesh недоступен';
    this.stopTrackers(); this.mode = 'off';
    Toast.show(this.unavailable, 'err', 7000);
    if (CallUi.r.bMesh) CallUi.r.bMesh.classList.add('hidden');
    this.refreshPanes();
  },
  mount(els) {
    this.stopAll();
    this.els = els; this.role = els.role; this.mode = 'off'; this.photos = []; this.latest = {}; this.info = null; this.cal = null; this.consent = false; this.coach = false; this.hist = null; this.remotePose = null;
    this.unavailable = Cfg.mediapipe ? '' : 'Модуль Face Mesh отключён: на сервере нет файлов MediaPipe. Видеозвонок работает без него.';
    if (this.unavailable && CallUi.r.bMesh) CallUi.r.bMesh.classList.add('hidden');
    const fp = App.proto.face || {}; this.tol = fp.tolerance || 5; this.poses = (fp.poses && fp.poses.length) ? fp.poses : ['frontal', 'left45', 'right45', 'left90', 'right90'];
    this.frameEl = h('div', { class: 'frame-ok hidden' }); els.wrap.append(this.frameEl);
    this.Overlay.init();
    this.frameFn = Loop.add(() => { this.Overlay.frame(); this.autoFrame(); });
    if (this.role === 'patient') {
      try { this.consent = sessionStorage.getItem('mars_fc_' + Api.room) === '1'; } catch (e) { this.consent = false; }
      this.sendConsent(); this.timers.push(setInterval(() => this.sendConsent(), 10000));
    }
    this.timers.push(setInterval(() => this.statusTick(), 500));
    this.vis = () => { if (!document.hidden) { for (const t of [this.trk, this.trk2]) if (t) t.kick(); } };
    document.addEventListener('visibilitychange', this.vis);
    if (this.role === 'doctor') this.loadPhotos().catch(() => { });
    this.refreshPanes();
  },
  stopTrackers() { for (const k of ['trk', 'trk2']) { if (this[k]) { this[k].stop(); this[k] = null; } } this.motion = []; if (this.els && this.els.ovR) { for (const c of [this.els.ovR, this.els.ovL]) if (c) c.getContext('2d').clearRect(0, 0, c.width, c.height); } },
  stopAll() {
    this.stopTrackers();
    this.timers.forEach(clearInterval); this.timers = [];
    if (this.frameFn) { Loop.del(this.frameFn); this.frameFn = null; }
    if (this.vis) { document.removeEventListener('visibilitychange', this.vis); this.vis = null; }
    this.subs.clear(); this.busy = 0; this.coach = false; this.autocrop = false; this.mode = 'off';
    if (this.Overlay.ro) { this.Overlay.ro.disconnect(); this.Overlay.ro = null; }
    for (const u of this.blobs.values()) URL.revokeObjectURL(u); this.blobs.clear();
    if (this.Compare.l) { try { this.Compare.l.close(); } catch (e) { /* ignore */ } this.Compare.l = null; }
    if (this.ctl) { try { this.ctl.abort(); } catch (e) { /* ignore */ } this.ctl = null; }
    this.cs = { s: 1, x: 0, y: 0 }; this.tg = { s: 1, x: 0, y: 0 };
    if (this.els && this.els.stage) this.els.stage.style.transform = '';
    if (this.frameEl) { this.frameEl.remove(); this.frameEl = null; }
  },
  /* --- согласие --- */
  setConsent(g) {
    if (this.role !== 'doctor' || this.consent === g) return;
    this.consent = g; State.set({ faceConsent: g });
    if (!g) { this.stopTrackers(); this.mode = 'off'; this.coach = false; this.autocrop = false; }
    this.refreshPanes();
  },
  sendConsent() { if (this.role === 'patient' && Api.room) Api.ev('face', { kind: 'consent', granted: this.consent }).catch(() => { }); },
  ensureConsent() {
    if (this.unavailable) { Toast.show(this.unavailable, null, 6000); return Promise.resolve(false); }
    if (this.consent) return Promise.resolve(true);
    return new Promise(res => {
      let done = false; const fin = v => { if (!done) { done = true; res(v); } };
      const s = Ui.sheet({
        title: 'Согласие на анализ лица', onClose: () => fin(false),
        actions: [{ label: 'Не согласен(на)', kind: 'sec', onClick: () => fin(false) }, {
          label: 'Согласен(на)', kind: 'pri', onClick: async () => {
            this.consent = true;
            try { sessionStorage.setItem('mars_fc_' + Api.room, '1'); } catch (e) { /* ignore */ }
            try { await Api.ev('control', { action: 'face_consent', granted: true }); } catch (e) { Toast.err(e); }
            this.sendConsent(); this.refreshPanes(); fin(true);
          }
        }]
      });
      s.el.append(h('p', { style: 'margin:0', text: 'Что анализируется: изображение с вашей камеры обрабатывается локально, на вашем устройстве.' }),
        h('p', { style: 'margin:0', text: 'Что передаётся врачу: числовые результаты и снимки, которые вы подтверждаете сами.' }),
        h('p', { style: 'margin:0', text: 'Что не сохраняется: поток видео. Он не записывается и не отправляется для анализа на сервер.' }),
        h('p', { class: 'note', style: 'margin:0', text: 'Согласие можно отозвать в любой момент в разделе «Задания».' }));
    });
  },
  async revoke() {
    this.consent = false; this.stopTrackers(); this.mode = 'off'; this.coach = false;
    try { sessionStorage.removeItem('mars_fc_' + Api.room); } catch (e) { /* ignore */ }
    try { await Api.ev('control', { action: 'face_consent', granted: false }); } catch (e) { Toast.err(e); }
    this.sendConsent(); this.els.hint.textContent = ''; this.els.pq.classList.add('hidden'); this.refreshPanes();
  },
  /* --- режимы --- */
  async setMode(m) {
    if (this.unavailable) { Toast.show(this.unavailable); return; }
    if (m !== 'off' && !this.consent) {
      if (this.role === 'doctor') { Toast.show('Пациент не дал согласие на анализ лица'); this.refreshPanes(); return; }
      if (!(await this.ensureConsent())) { this.refreshPanes(); return; }
    }
    this.mode = m; this.refreshPanes(); await this.sync();
  },
  toggle() { this.setMode(this.mode === 'off' ? 'grid' : 'off'); },
  cycleMode() { this.setMode({ off: 'grid', grid: 'contour', contour: 'off' }[this.mode]); },
  async sync() {
    if (this.unavailable) return;
    if (this.starting) { this.again = true; return; }
    const need = this.consent && (this.mode !== 'off' || this.coach || this.autocrop || this.busy > 0);
    const need2 = this.consent && this.secondary && this.mode !== 'off';
    this.starting = true;
    try {
      if (need && !this.trk) { this.trk = new FMTracker(this.mainVideo(), { fps: 24, onFrame: c => this.onFrame(c) }); await this.trk.start(); }
      else if (!need && this.trk) { this.trk.stop(); this.trk = null; this.info = null; this.showInfo(); }
      if (need2 && !this.trk2 && this.trk) { this.trk2 = new FMTracker(this.otherVideo(), { fps: 10 }); await this.trk2.start(); }
      else if (!need2 && this.trk2) { this.trk2.stop(); this.trk2 = null; }
    } catch (e) { console.error(e); this.trk = null; this.trk2 = null; this.starting = false; this.fail('Не удалось запустить Face Mesh: ' + ((e && e.message) || 'ошибка инициализации') + '. Видеозвонок продолжает работать.'); return; }
    this.starting = false;
    if (this.again) { this.again = false; return this.sync(); }
  },
  onFrame(cur) {
    const now = performance.now(), n = cur.lm[LM.NOSE];
    this.motion.push({ t: now, x: n.x, y: n.y }); while (this.motion.length && now - this.motion[0].t > 600) this.motion.shift();
    if (now - this.lastAssess > 250) {
      this.lastAssess = now;
      try { this.info = this.Pose.assess(cur, this.mainVideo(), this.tol); this.info.multi = cur.multi; this.info.lost = false; this.showInfo(); this.sendInfo(now); } catch (e) { console.error(e); }
    }
    for (const f of this.subs) f(cur);
  },
  statusTick() {
    const t = this.trk; if (!t || !this.info) return;
    const lost = t.lost();
    if (lost && !this.info.lost) { this.info = Object.assign({}, this.info, { lost: true, level: 'bad', hint: 'Лицо не найдено' }); this.showInfo(); this.sendInfo(performance.now(), true); }
  },
  showInfo() {
    const i = this.info, pq = this.els.pq, hint = this.els.hint; if (!pq) return;
    if (!i || !this.trk) { pq.classList.add('hidden'); hint.textContent = ''; if (this.frameEl) this.frameEl.classList.add('hidden'); return; }
    const lv = i.lost ? 'bad' : i.level;
    pq.classList.remove('hidden');
    pq.replaceChildren(h('span', { class: 'dot ' + { good: 'ok', medium: 'mid', bad: 'bad' }[lv] }), h('span', { text: (this.role === 'doctor' ? 'Поза по сжатому видео: ' : 'Поза: ') + { good: 'хорошая', medium: 'средняя', bad: 'плохая' }[lv] }));
    let txt = i.lost ? 'Лицо не найдено' : (i.multi ? 'В кадре несколько лиц: используется самое крупное. ' : '') + i.hint;
    if (this.role === 'patient' && (this.coach || this.busy)) hint.textContent = txt; else hint.textContent = '';
    if (this.frameEl) this.frameEl.classList.toggle('hidden', !(lv === 'good' && (this.coach || this.busy)));
  },
  sendInfo(now, force) {
    if (this.role !== 'patient' || !(this.coach || this.busy) || !this.info) return;
    if (!force && now - this.lastSend < 500) return; this.lastSend = now;
    const i = this.info;
    Api.ev('face', { kind: 'pose', level: i.lost ? 'bad' : i.level, yaw: Math.round(i.yaw || 0), pitch: Math.round(i.pitch || 0), roll: Math.round(i.roll || 0), hint: i.lost ? 'Лицо не найдено' : i.hint, lost: !!i.lost, multi: !!i.multi, tol: this.tol }).catch(() => { });
  },
  autoFrame() {
    if (this.role !== 'doctor' || !this.els.stage) return;
    const st = this.els.stage, cw = st.clientWidth, ch = st.clientHeight;
    if (!this.autocrop || !this.trk || this.trk.lost() || !this.trk.cur) { this.tg = { s: 1, x: 0, y: 0 }; }
    else {
      const cur = this.trk.cur, g = this.Pose.geom(cur), m = fitMap(cur.vw, cur.vh, cw, ch, 'contain');
      const fx0 = m.ox + g.cx * cur.vw * m.s, fy0 = m.oy + g.cy * cur.vh * m.s, fh = g.size * cur.vh * m.s;
      const s = clamp(ch * 0.5 / Math.max(fh, 1), 1, 2.2);
      const tx = clamp(-s * (fx0 - cw / 2), -(s - 1) * cw / 2, (s - 1) * cw / 2), ty = clamp(-s * (fy0 - ch / 2), -(s - 1) * ch / 2, (s - 1) * ch / 2);
      if (Math.abs(s - this.tg.s) > 0.06 || Math.abs(tx - this.tg.x) > cw * 0.03 || Math.abs(ty - this.tg.y) > ch * 0.03) this.tg = { s, x: tx, y: ty };
    }
    const c = this.cs, k = 0.08;
    const ns = c.s + (this.tg.s - c.s) * k, nx = c.x + (this.tg.x - c.x) * k, ny = c.y + (this.tg.y - c.y) * k;
    if (Math.abs(ns - c.s) > 0.0005 || Math.abs(nx - c.x) > 0.05 || Math.abs(ny - c.y) > 0.05) { this.cs = { s: ns, x: nx, y: ny }; st.style.transform = 'translate(' + nx.toFixed(2) + 'px,' + ny.toFixed(2) + 'px) scale(' + ns.toFixed(4) + ')'; }
  },
  /* --- события --- */
  onFace(d) {
    if (this.role !== 'doctor') return;
    if (d.kind === 'consent') { this.setConsent(!!d.granted); }
    else if (d.kind === 'pose') { this.remotePose = d; this.updatePoseRow(); }
    else if (d.kind === 'photo') { if (!this.photos.find(p => p.id === d.id)) { this.photos.push({ id: d.id, pose: d.pose, ts: d.ts }); Toast.show('Получен снимок: ' + (POSE_NAMES[d.pose] || d.pose)); this.refreshHistory(); if (this.dTab === 'capture') this.renderDoctor(); } }
  },
  onMeasure(d) {
    if (this.role !== 'doctor') return;
    if (d.kind === 'calibration') { this.cal = d.calibration; Toast.show('Получена калибровка размеров'); }
    else if (d.kind === 'symmetry' || d.kind === 'mobility') { this.latest[d.kind] = d; Toast.show(d.kind === 'symmetry' ? 'Получены метрики симметрии' : 'Получены результаты подвижности'); this.hist = null; }
    this.renderDoctor();
  },
  patientRequest(d) {
    if (d.action === 'set_face') { if (d.poses && d.poses.length) this.poses = d.poses; if (d.tolerance) this.tol = d.tolerance; this.refreshPanes(); return; }
    const kinds = { symmetry: 'измерение симметрии лица', mobility: 'задания на подвижность лица', capture: 'стандартную фотосъёмку лица', calibrate: 'калибровку размеров' };
    let what = '', run = null;
    if (d.action === 'request_measure' && kinds[d.kind]) { what = kinds[d.kind]; run = () => this.startKind(d.kind); }
    else if (d.action === 'request_retake') { what = 'повторить снимок: ' + (POSE_NAMES[d.pose] || ''); run = () => this.Capture.open([d.pose]); }
    if (!run) return;
    Ui.sound();
    const s = Ui.sheet({ title: 'Просьба врача', actions: [{ label: 'Позже', kind: 'sec' }, { label: 'Начать', kind: 'pri', onClick: () => { run(); } }] });
    s.el.append(h('p', { class: 'big-hint', text: 'Врач просит выполнить: ' + what }), h('p', { class: 'note', style: 'text-align:center;margin:0', text: 'Вы подтверждаете каждый результат перед отправкой.' }));
  },
  startKind(k) { if (k === 'symmetry') this.measureSym(); else if (k === 'mobility') this.measureMob(); else if (k === 'capture') this.Capture.open(this.poses); else if (k === 'calibrate') this.calibrateSheet(); },
  loadProto(p) {
    const f = p.face || {}; if (f.tolerance) this.tol = f.tolerance; if (f.poses && f.poses.length) this.poses = f.poses;
    this.refreshPanes();
  },
  /* --- вспомогательное: таблицы --- */
  symTable(p, forPrint) {
    const k = p.calibration && p.calibration.ipd_mm, rows = p.rows || [];
    const unit = k ? ' (МЗР / мм)' : ' (доли МЗР)';
    const src = (k ? ' Источник масштаба: ' + p.calibration.label + '.' : ' Калибровки нет: значения в мм не показываются.') + (p.source === 'device' ? ' Рассчитано на устройстве.' : p.source === 'vision' ? ' Рассчитано сервисом анализа.' : '');
    return h('div', { class: 'stack' },
      h('table', { class: 'tb' }, h('tr', null, h('th', { text: 'Метрика' + unit }), h('th', { class: 'n', text: 'Правая' }), h('th', { class: 'n', text: 'Левая' }), h('th', { class: 'n', text: 'Разность' })),
        rows.map(r => h('tr', null, h('td', { text: r.label }), h('td', { class: 'n', text: fx(r.right) + (k ? ' / ' + fx(r.right_mm, 1) : '') }), h('td', { class: 'n', text: fx(r.left) + (k ? ' / ' + fx(r.left_mm, 1) : '') }), h('td', { class: 'n', text: fx(r.diff) + (k ? ' / ' + fx(r.diff_mm, 1) : '') })))),
      h('table', { class: 'tb' }, (p.singles || []).map(s => h('tr', null, h('td', { text: s.label }), h('td', { class: 'n', text: s.unit === 'deg' ? fx(s.value, 1) + '°' : fx(s.value) + (k && s.value_mm != null ? ' / ' + fx(s.value_mm, 1) + ' мм' : '') })))),
      h('p', { class: 'note', style: 'margin:0', text: 'МЗР: межзрачковое расстояние. Положительное значение смещения: в сторону левой половины лица пациента. Кадров в выборке: ' + (p.samples || 0) + '.' + src }),
      h('p', { class: 'note', style: 'margin:0;font-weight:600', text: SIGN_SYM }));
  },
  /* крупные значения по сторонам: правая и левая своим цветом, как дозировка на упаковке */
  doseCard(title, right, left, unit, foot) {
    const sd = (cls, name, v) => h('div', { class: 'sd ' + cls }, h('span', { class: 'k', text: name }), h('span', { class: 'n', text: v }), unit ? h('span', { class: 'u', text: unit }) : null);
    return h('div', { class: 'dose', role: 'group', 'aria-label': title },
      h('h4', { text: title }), h('div', { class: 'sides' }, sd('r', 'Правая', right), sd('l', 'Левая', left)),
      foot ? h('div', { class: 'df', text: foot }) : null);
  },
  symDoses(p) {
    const k = p.calibration && p.calibration.ipd_mm, n = k ? 1 : 3, f = v => fx(v, n).replace('.', ',');
    const hint = k ? null : h('p', { class: 'note', style: 'margin:0', text: 'Без калибровки значения даны в долях межзрачкового расстояния (МЗР): 0,100 — одна десятая расстояния между зрачками этого пациента. Так значения сравнимы между приёмами, даже если пациент сидел дальше или ближе к камере. Чтобы видеть миллиметры, запросите калибровку.' });
    return h('div', { class: 'stack' }, hint, h('div', { class: 'doses' }, (p.rows || []).map(r => this.doseCard(r.label,
      f(k ? r.right_mm : r.right), f(k ? r.left_mm : r.left), k ? 'мм' : 'МЗР',
      'Разность (правая − левая): ' + f(k ? r.diff_mm : r.diff) + (k ? ' мм' : '')))));
  },
  mobDoses(p) {
    const f = v => fx(v, 2).replace('.', ',');
    return h('div', { class: 'doses' }, (p.tasks || []).map(t => t.single != null
      ? h('div', { class: 'dose', role: 'group', 'aria-label': t.label }, h('h4', { text: t.label }), h('div', { class: 'df', text: 'Обе стороны вместе (модель не разделяет): ' + f(t.single) }))
      : this.doseCard(t.label, f(t.right), f(t.left), '', 'Соотношение сторон: ' + (t.ratio == null ? '-' : f(t.ratio)) + ' (1 — одинаково)')));
  },
  fold(title, ...kids) { return h('details', { class: 'fold' }, h('summary', { text: title }), h('div', { class: 'stack' }, ...kids)); },
  mobTable(p) {
    return h('div', { class: 'stack' },
      h('table', { class: 'tb' }, h('tr', null, h('th', { text: 'Задание (максимум коэффициента 0-1)' }), h('th', { class: 'n', text: 'Правая' }), h('th', { class: 'n', text: 'Левая' }), h('th', { class: 'n', text: 'Соотношение' })),
        (p.tasks || []).map(t => t.single != null ? h('tr', null, h('td', { text: t.label + ' (обе стороны, модель не разделяет)' }), h('td', { class: 'n', text: fx(t.single, 2) }), h('td', { class: 'n', text: '-' }), h('td', { class: 'n', text: '-' }))
          : h('tr', null, h('td', { text: t.label }), h('td', { class: 'n', text: fx(t.right, 2) }), h('td', { class: 'n', text: fx(t.left, 2) }), h('td', { class: 'n', text: t.ratio == null ? '-' : fx(t.ratio, 2) })))),
      h('p', { class: 'note', style: 'margin:0;font-weight:600', text: SIGN_MOB }));
  },
  async sendResult(payload) {
    try { await Api.post('/rooms/' + Api.room + '/measurements', payload); await Api.ev('measure', payload); Toast.show('Результат отправлен врачу'); return true; }
    catch (e) { Toast.err(e); return false; }
  },
  /* лист с превью камеры для заданий пациента */
  taskSheet(title) {
    const vid = h('video', { autoplay: true, playsinline: true, class: 'mirror', style: 'width:100%;aspect-ratio:4/3;object-fit:cover;background:#000;border-radius:var(--r2)' }); vid.muted = true; vid.srcObject = Media.stream;
    const hint = h('div', { class: 'big-hint', role: 'status', 'aria-live': 'polite', text: '' });
    const prog = h('progress', { max: 1, value: 0, style: 'width:100%;height:12px' });
    const body = h('div', { class: 'stack' });
    const ctl = new AbortController();
    let closed = false;
    const s = Ui.sheet({ title, onClose: () => { if (closed) return; closed = true; ctl.abort(); this.busy = Math.max(0, this.busy - 1); this.sync(); } });
    s.el.append(vid, hint, prog, body);
    return { s, hint, prog, body, signal: ctl.signal, isClosed: () => closed };
  },
  async measureSym() {
    if (!(await this.ensureConsent())) return;
    this.busy++; await this.sync(); if (!this.trk) { this.busy--; return; }
    const T = this.taskSheet('Измерение симметрии');
    const go = async () => {
      T.body.replaceChildren();
      try {
        const loc = await this.Symmetry.run((p, msg) => { T.prog.value = p; T.hint.textContent = (this.trk && this.trk.lost()) ? 'Лицо не найдено' : msg; }, T.signal);
        if (T.isClosed()) return;
        T.hint.textContent = 'Расчёт...';
        const res = await this.Remote.pick('symmetry', { frames: loc._frames, tolerance: this.tol, calibration: this.cal }, loc, T.signal);
        if (T.isClosed()) return;
        T.hint.textContent = 'Готово'; T.prog.value = 1;
        T.body.replaceChildren(this.symTable(res), h('div', { class: 'row wrap' },
          h('button', { class: 'btn pri', onclick: async e => { e.target.disabled = true; if (await this.sendResult(res)) T.s.close(); else e.target.disabled = false; } }, 'Отправить врачу'),
          h('button', { class: 'btn sec', onclick: go }, 'Повторить')));
      } catch (e) { if (T.isClosed()) return; T.hint.textContent = ''; T.body.replaceChildren(h('p', { text: e.message }), h('button', { class: 'btn pri', onclick: go }, 'Повторить')); }
    };
    go();
  },
  async measureMob() {
    if (!(await this.ensureConsent())) return;
    this.busy++; await this.sync(); if (!this.trk) { this.busy--; return; }
    const T = this.taskSheet('Подвижность лица'), out = [];
    const finish = async () => {
      const loc = { kind: 'mobility', pose_ok: true, tolerance: Math.max(this.tol * 2, 10), tasks: out.slice(), ts: Math.floor(Date.now() / 1000) };
      T.hint.textContent = 'Расчёт...'; T.body.replaceChildren();
      let res;
      try { res = await this.Remote.pick('mobility', { tasks: out.map(t => ({ id: t.id, frames: t._frames })), tolerance: this.tol }, loc, T.signal); }
      catch (e) { if (T.isClosed()) return; T.hint.textContent = ''; T.body.replaceChildren(h('p', { text: e.message }), h('button', { class: 'btn pri', onclick: () => { out.length = 0; next(0); } }, 'Пройти заново')); return; }
      if (T.isClosed()) return;
      T.hint.textContent = 'Готово'; T.prog.value = 1;
      T.body.replaceChildren(this.mobTable(res), h('div', { class: 'row wrap' },
        h('button', { class: 'btn pri', onclick: async e => { e.target.disabled = true; if (await this.sendResult(res)) T.s.close(); else e.target.disabled = false; } }, 'Отправить врачу'),
        h('button', { class: 'btn sec', onclick: () => { out.length = 0; next(0); } }, 'Пройти заново')));
    };
    const next = i => {
      if (T.isClosed()) return;
      if (i >= this.Mobility.TASKS.length) return finish();
      const task = this.Mobility.TASKS[i];
      T.prog.value = 0; T.hint.textContent = task.label + '. Нажмите «Начать»';
      T.body.replaceChildren(h('button', { class: 'btn pri lg', onclick: async () => {
        T.body.replaceChildren(); T.hint.textContent = task.cmd;
        try { const r = await this.Mobility.runTask(task, (p, m) => { T.prog.value = Math.min(1, p); T.hint.textContent = m; }, T.signal); out.push(r); next(i + 1); }
        catch (e) { if (T.isClosed()) return; T.body.replaceChildren(h('p', { text: e.message }), h('button', { class: 'btn pri', onclick: () => next(i) }, 'Повторить задание')); }
      } }, 'Начать'), h('span', { class: 'note', text: 'Задание ' + (i + 1) + ' из ' + this.Mobility.TASKS.length }));
    };
    next(0);
  },
  async calibrateSheet() {
    if (!(await this.ensureConsent())) return;
    this.busy++; await this.sync(); if (!this.trk) { this.busy--; return; }
    const T = this.taskSheet('Калибровка размеров'), C = this.Calibrate;
    const saveCal = async cal => {
      this.cal = cal;
      if (await this.sendResult({ kind: 'calibration', calibration: cal, ts: cal.ts })) T.s.close();
    };
    const menu = () => {
      T.hint.textContent = ''; T.prog.value = 0;
      T.body.replaceChildren(
        h('p', { class: 'note', style: 'margin:0', text: 'Без калибровки показываются только безразмерные индексы. Калибровка нужна для значений в миллиметрах и действует только в анфасе.' }),
        h('button', { class: 'btn pri', onclick: card }, 'По банковской карте (85,6 мм)'),
        h('button', { class: 'btn sec', onclick: iris }, 'По радужке (приблизительно)'));
    };
    const iris = async () => {
      T.body.replaceChildren(); T.hint.textContent = 'Смотрите прямо в камеру';
      const S = []; const t0 = performance.now();
      const fn = cur => { const e = this.Pose.euler(cur); if (!this.Pose.inTol(e, this.tol)) { T.hint.textContent = 'Смотрите прямо в камеру'; return; } const ir = C.irisPx(cur), ip = C.ipdPx(cur); if (ir && ip) S.push({ iris: ir, ipd: ip }); T.prog.value = Math.min(1, S.length / 30); };
      this.subs.add(fn);
      const wait = setInterval(() => {
        if (T.isClosed() || S.length >= 30 || performance.now() - t0 > 15000) {
          clearInterval(wait); this.subs.delete(fn); if (T.isClosed()) return;
          const cal = S.length >= 12 ? C.fromIris(S) : null;
          if (!cal) { T.body.replaceChildren(h('p', { text: 'Не удалось получить устойчивый кадр в анфасе. Повторите.' }), h('button', { class: 'btn pri', onclick: iris }, 'Повторить')); return; }
          T.hint.textContent = 'Приблизительно, погрешность выше';
          T.body.replaceChildren(h('p', { style: 'margin:0', text: 'Межзрачковое расстояние: ' + cal.ipd_mm.toFixed(1).replace('.', ',') + ' мм (' + cal.label + ').' }),
            h('div', { class: 'row wrap' }, h('button', { class: 'btn pri', onclick: () => saveCal(cal) }, 'Сохранить'), h('button', { class: 'btn sec', onclick: menu }, 'Назад')));
        }
      }, 200);
    };
    const card = () => {
      T.hint.textContent = 'Держите карту у лба или щеки, лицо анфас';
      T.body.replaceChildren(h('p', { class: 'note', style: 'margin:0', text: 'Карту нужно держать в одной плоскости с лицом, ровно, лицом к камере. Нажмите «Сделать кадр», затем отметьте четыре угла карты.' }),
        h('button', { class: 'btn pri', onclick: shot }, 'Сделать кадр'), h('button', { class: 'btn sec', onclick: menu }, 'Назад'));
    };
    const shot = () => {
      const trk = this.trk, cur = trk && trk.cur, e = cur && this.Pose.euler(cur);
      if (!cur || trk.lost() || !this.Pose.inTol(e, this.tol)) { Toast.show('Нужен анфас в допуске: ' + (this.info && this.info.hint || 'смотрите прямо в камеру')); return; }
      const v = this.els.local, cv = document.createElement('canvas'); cv.width = v.videoWidth; cv.height = v.videoHeight; cv.getContext('2d').drawImage(v, 0, 0);
      cv.style.cssText = 'width:100%;border-radius:var(--r2);touch-action:none;cursor:crosshair'; const pts = [];
      const names = ['левый верхний', 'правый верхний', 'правый нижний', 'левый нижний'];
      T.hint.textContent = 'Отметьте угол карты: ' + names[0];
      const ctx = cv.getContext('2d');
      cv.addEventListener('pointerdown', ev => {
        const r = cv.getBoundingClientRect(); const p = { x: (ev.clientX - r.left) * cv.width / r.width, y: (ev.clientY - r.top) * cv.height / r.height };
        if (pts.length >= 4) return; pts.push(p);
        ctx.fillStyle = '#FF9500'; ctx.beginPath(); ctx.arc(p.x, p.y, Math.max(5, cv.width / 200), 0, 7); ctx.fill();
        if (pts.length > 1) { ctx.strokeStyle = '#FF9500'; ctx.lineWidth = Math.max(2, cv.width / 500); ctx.beginPath(); ctx.moveTo(pts[pts.length - 2].x, pts[pts.length - 2].y); ctx.lineTo(p.x, p.y); ctx.stroke(); }
        if (pts.length < 4) { T.hint.textContent = 'Отметьте угол карты: ' + names[pts.length]; return; }
        const cal = C.fromCard(pts, C.ipdPx(cur));
        if (!cal) { T.hint.textContent = 'Углы отмечены неверно'; T.body.replaceChildren(h('button', { class: 'btn pri', onclick: card }, 'Повторить')); return; }
        T.hint.textContent = '';
        T.body.append(h('p', { style: 'margin:8px 0 0', text: 'Межзрачковое расстояние: ' + cal.ipd_mm.toFixed(1).replace('.', ',') + ' мм; масштаб ' + cal.mm_per_px.toFixed(4) + ' мм/пиксель.' + (cal.skew > 0.08 ? ' Пропорции карты искажены (' + (cal.skew * 100).toFixed(0) + '%): карта наклонена, точность снижена.' : '') }),
          h('div', { class: 'row wrap' }, h('button', { class: 'btn pri', onclick: () => saveCal(cal) }, 'Сохранить'), h('button', { class: 'btn sec', onclick: card }, 'Заново')));
      });
      T.body.replaceChildren(cv);
    };
    menu();
  },
  /* --- загрузка данных (врач) --- */
  async pid() {
    let p = App.proto.appointment && App.proto.appointment.patient_id;
    if (!p) { try { App.proto = Object.assign(App.proto, await Api.get('/rooms/' + Api.room + '/protocol')); p = App.proto.appointment.patient_id; } catch (e) { /* нет */ } }
    return p;
  },
  async loadPhotos() {
    const pid = await this.pid(); if (!pid) return [];
    const r = await Api.get('/patients/' + pid + '/photos'); this.allPhotos = r.photos;
    const aid = App.proto.appointment && App.proto.appointment.id;
    this.photos = r.photos.filter(p => p.appt_id === aid).map(p => ({ id: p.id, pose: p.pose, ts: p.ts }));
    this.refreshHistory(); return r.photos;
  },
  async loadHistory() {
    const pid = await this.pid(); if (!pid) return [];
    if (this.hist) return this.hist;
    const r = await Api.get('/patients/' + pid + '/measurements'); this.hist = r.measurements;
    const cals = this.hist.filter(m => m.kind === 'calibration'); if (cals.length && !this.cal) this.cal = cals[cals.length - 1].data.calibration;
    return this.hist;
  },
  async thumb(id) {
    if (this.blobs.has(id)) return this.blobs.get(id);
    const b = await Api.req('GET', '/photos/' + id, { blob: true }); const u = URL.createObjectURL(b); this.blobs.set(id, u); return u;
  },
  thumbEl(p, onclick) {
    const img = h('img', { alt: POSE_NAMES[p.pose] || p.pose, src: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=' });
    this.thumb(p.id).then(u => { img.src = u; }).catch(() => { });
    const fig = h('figure', null, onclick ? h('button', { style: 'display:block;width:100%', onclick, 'aria-label': 'Выбрать снимок ' + fmtDT(p.ts) }, img) : img, h('figcaption', { text: (POSE_NAMES[p.pose] || p.pose) + ', ' + fmtDT(p.ts) }));
    return fig;
  },
  refreshHistory() {
    const box = Files.hist; if (!box) return;
    box.replaceChildren(h('h3', { text: 'Снимки этого приёма' }), this.photos.length ? h('div', { class: 'thumbs' }, this.photos.map(p => this.thumbEl(p))) : h('p', { class: 'note', style: 'margin:0', text: 'Снимков пока нет.' }));
  },
  updatePoseRow() {
    const el = this.poseRow; if (!el) return; const d = this.remotePose;
    if (!d) { el.textContent = 'Поза пациента: нет данных'; return; }
    el.replaceChildren(h('span', { class: 'dot ' + { good: 'ok', medium: 'mid', bad: 'bad' }[d.level] }), ' Поза на устройстве пациента: ' + ({ good: 'хорошая', medium: 'средняя', bad: 'плохая' }[d.level]) + '. yaw ' + d.yaw + '°, pitch ' + d.pitch + '°, roll ' + d.roll + '°' + (d.hint ? '. ' + d.hint : ''));
  },
  refreshPanes() {
    if (CallUi.r.bMesh) CallUi.r.bMesh.setAttribute('aria-pressed', String(this.mode !== 'off'));
    if (this.role === 'patient') this.renderPatient(); else if (this.role === 'doctor') this.renderDoctor();
  },
  /* --- панель пациента --- */
  openTasks() { CallUi.showTab('face'); this.refreshPanes(); },
  patientPane() { this.pp = h('div', { class: 'stack' }); this.role = 'patient'; return this.pp; },
  renderPatient() {
    const box = this.pp; if (!box) return;
    if (this.unavailable) { box.replaceChildren(h('h3', { text: 'Задания для лица' }), h('p', { text: this.unavailable })); return; }
    if (!this.consent) {
      box.replaceChildren(h('h3', { text: 'Задания для лица' }), h('p', { text: 'Врач может попросить вас выполнить несколько простых заданий перед камерой: фотосъёмку и проверку подвижности лица. Для этого нужно отдельное согласие на анализ лица.' }),
        h('button', { class: 'btn pri lg', onclick: () => this.ensureConsent() }, 'Прочитать и дать согласие'));
      return;
    }
    const big = (label, fn, pressed) => h('button', { class: 'btn sec lg', style: 'justify-content:flex-start', 'aria-pressed': pressed == null ? null : String(pressed), onclick: fn }, label);
    const opts = h('div', { class: 'stack' },
      h('label', { class: 'row', style: 'min-height:44px' }, h('input', { type: 'checkbox', style: 'width:22px;height:22px', checked: this.hideFace || null, onchange: e => { this.hideFace = e.target.checked; this.refreshPanes(); } }), ' Скрыть лицо при сохранении'),
      h('label', { class: 'row', style: 'min-height:44px' }, h('input', { type: 'checkbox', style: 'width:22px;height:22px', checked: this.meshOnShot || null, disabled: this.hideFace || null, onchange: e => { this.meshOnShot = e.target.checked; } }), ' Накладывать сетку на снимок'));
    box.replaceChildren(h('h3', { text: 'Задания для лица' }), h('p', { class: 'note', style: 'margin:0', text: 'Видео обрабатывается на вашем устройстве. Врачу уходят только числа и снимки, которые вы подтвердили.' }),
      big(this.coach ? 'Выключить подсказки позы' : 'Подсказки позы', () => { this.coach = !this.coach; this.refreshPanes(); this.sync().then(() => { if (!this.coach) { this.showInfo(); } }); }, this.coach),
      big('Измерение симметрии', () => this.measureSym()), big('Подвижность лица', () => this.measureMob()),
      big('Фотосъёмка по ракурсам', () => this.Capture.open(this.poses)), big('Калибровка размеров', () => this.calibrateSheet()),
      h('div', { class: 'stack' }, h('span', { class: 'note', text: 'Показать сетку на видео' }), Ui.segmented([['off', 'Выкл'], ['grid', 'Сетка'], ['contour', 'Контуры']], this.mode, m => this.setMode(m), 'Сетка на видео')),
      opts, h('button', { class: 'btn txt', onclick: () => this.revoke() }, 'Отозвать согласие'));
  },
  /* --- панель врача --- */
  doctorPane() {
    this.role = 'doctor';
    this.dp = h('div', { class: 'stack' });
    return this.dp;
  },
  renderDoctor() {
    const box = this.dp; if (!box) return;
    if (this.unavailable) { box.replaceChildren(h('h3', { text: 'Лицо' }), h('p', { text: this.unavailable })); return; }
    const cons = this.consent ? h('span', { class: 'chip g', text: 'Пациент дал согласие' }) : h('span', { class: 'chip o', text: 'Пациент не дал согласие' });
    this.poseRow = h('div', { class: 'sm', role: 'status', 'aria-live': 'polite' }); this.updatePoseRow();
    const head = h('div', { class: 'stack' }, h('div', { class: 'rowb' }, h('h3', { text: 'Лицо' }), cons), this.poseRow,
      this.fold('Сетка на видео и кадрирование',
      Ui.segmented([['off', 'Выкл'], ['grid', 'Сетка'], ['contour', 'Контуры']], this.mode, m => this.setMode(m), 'Режим сетки (F)'),
      h('label', { class: 'row', style: 'min-height:44px' }, h('input', { type: 'checkbox', style: 'width:22px;height:22px', checked: this.autocrop || null, disabled: !this.consent || null, onchange: e => { this.autocrop = e.target.checked; this.sync(); } }), ' Автокадрирование по лицу (только у вас)'),
      h('label', { class: 'row', style: 'min-height:44px' }, h('input', { type: 'checkbox', style: 'width:22px;height:22px', checked: this.secondary || null, onchange: e => { this.secondary = e.target.checked; this.sync(); } }), ' Сетка и на самовидении'),
      h('p', { class: 'note', style: 'margin:0', text: 'Сетка и поза по входящему видео построены по сжатому потоку и ориентировочны. Точные измерения выполняются на устройстве пациента.' })));
    const tabs = Ui.segmented([['metrics', 'Метрики'], ['capture', 'Съёмка'], ['compare', 'Сравнение'], ['report', 'Отчёт']], this.dTab, t => { this.dTab = t; this.renderDoctor(); }, 'Разделы');
    const body = h('div', { class: 'stack' });
    box.replaceChildren(head, tabs, body);
    ({ metrics: () => this.tabMetrics(body), capture: () => this.tabCapture(body), compare: () => this.tabCompare(body), report: () => this.tabReport(body) })[this.dTab]();
  },
  req(kind) {
    if (!this.consent) { Toast.show('Пациент не дал согласие на анализ лица'); return; }
    Api.ev('control', { action: 'request_measure', kind }).then(() => Toast.show('Запрос отправлен пациенту')).catch(Toast.err);
  },
  tabMetrics(body) {
    const L = this.latest;
    body.append(h('button', { class: 'btn pri', disabled: !this.consent || null, onclick: () => this.req('symmetry') }, 'Измерить симметрию'),
      h('div', { class: 'row wrap' }, h('button', { class: 'btn sec', disabled: !this.consent || null, onclick: () => this.req('mobility') }, 'Подвижность'), h('button', { class: 'btn sec', disabled: !this.consent || null, onclick: () => this.req('calibrate') }, 'Калибровка в мм')));
    if (!this.consent) body.append(h('p', { class: 'note', style: 'margin:0', text: 'Измерения станут доступны, когда пациент даст согласие на анализ лица.' }));
    if (this.cal || !L.symmetry) body.append(h('p', { class: 'note', style: 'margin:0', text: this.cal ? 'Калибровка: ' + this.cal.label + '; межзрачковое расстояние ' + this.cal.ipd_mm.toFixed(1).replace('.', ',') + ' мм.' : 'Калибровки нет: значения в миллиметрах не показываются.' }));
    if (!L.symmetry && !L.mobility) body.append(this.firstSteps());
    if (L.symmetry) body.append(h('h3', { text: 'Симметрия' }), this.symDoses(L.symmetry), this.fold('Все значения симметрии', this.symTable(L.symmetry)));
    else body.append(h('p', { class: 'note', style: 'margin:0', text: 'Метрик симметрии в этом приёме пока нет.' }));
    if (L.mobility) body.append(h('h3', { text: 'Подвижность' }), this.mobDoses(L.mobility), this.fold('Все значения подвижности', this.mobTable(L.mobility)));
    const chartBox = h('div', { class: 'stack' }); body.append(h('h3', { text: 'Динамика между приёмами' }), chartBox);
    this.loadHistory().then(hist => this.drawTrends(chartBox, hist)).catch(e => chartBox.replaceChildren(h('p', { class: 'note', text: e.message })));
  },
  /* порядок документирования лица для врача, который делает это впервые */
  firstSteps() {
    const steps = [
      ['Пациент даёт согласие на анализ лица', this.consent],
      ['«Измерить симметрию»: пациент держит голову прямо, числа приходят сюда', false],
      ['«Калибровка в мм» — если нужны миллиметры, а не доли МЗР', !!this.cal],
      ['«Подвижность» и снимки по ракурсам (раздел «Съёмка») — по необходимости', false],
      ['Раздел «Отчёт»: печать или PDF с числами, снимками и динамикой', false]];
    return h('div', { class: 'steps' }, h('h3', { text: 'Порядок документирования' }),
      h('ol', null, steps.map(([t, done]) => h('li', { class: done ? 'done' : null }, t, done ? h('span', { class: 'chip g', text: 'готово' }) : null))));
  },
  /* ряды по приёмам: последнее измерение каждого приёма */
  series(hist, kind, pick) {
    const byAppt = new Map();
    for (const m of hist) if (m.kind === kind) byAppt.set(m.appt_id, m);
    const out = []; for (const m of byAppt.values()) { const v = pick(m.data); if (v != null && isFinite(v)) out.push({ x: m.ts, y: v }); }
    return out.sort((a, b) => a.x - b.x);
  },
  drawTrends(box, hist, forPrint) {
    const cvs = [];
    const symDefs = [['half', 'Ширина половин лица: разность'], ['brow', 'Высота бровей: разность'], ['mcor', 'Уголки рта: разность высот'], ['mouth', 'Уголки рта до средней линии: разность']];
    for (const [id, title] of symDefs) {
      const s = this.series(hist, 'symmetry', d => { const r = (d.rows || []).find(x => x.id === id); return r ? r.diff : null; });
      cvs.push([title + ' (доли МЗР)', [{ name: 'правая минус левая', points: s }], [-0.1, 0.1]]);
    }
    const s1 = this.series(hist, 'symmetry', d => { const r = (d.singles || []).find(x => x.id === 'tilt'); return r ? r.value : null; }); cvs.push(['Наклон линии глаз (градусы)', [{ name: 'наклон', points: s1 }], [-5, 5]]);
    const mob = id => this.series(hist, 'mobility', d => { const t = (d.tasks || []).find(x => x.id === id); return t && t.ratio != null ? t.ratio : null; });
    cvs.push(['Подвижность: соотношение сторон', [{ name: 'улыбка', points: mob('smile') }, { name: 'брови', points: mob('brow') }, { name: 'глаза', points: mob('eye') }, { name: 'губы', points: mob('lips') }], [0, 1]]);
    const nodes = []; const urls = [];
    for (const [title, ser, fixed] of cvs) {
      if (!ser.some(s => s.points.length)) continue;
      const cv = h('canvas', { style: 'width:100%;height:auto', 'aria-label': title }); this.Chart.line(cv, ser, { title, print: forPrint, fixed });
      nodes.push(cv); urls.push({ title, url: cv.toDataURL('image/png') });
    }
    if (box) box.replaceChildren(...(nodes.length ? nodes : [h('p', { class: 'note', style: 'margin:0', text: 'Данных по предыдущим приёмам пока нет. Графики появятся после нескольких измерений.' })]));
    return urls;
  },
  tabCapture(body) {
    const names = ['frontal', 'left45', 'right45', 'left90', 'right90'];
    const boxes = names.map(n => h('input', { type: 'checkbox', style: 'width:22px;height:22px', checked: this.poses.includes(n) || null, 'data-p': n }));
    const tol = Ui.input('number', '5', this.tol); tol.min = 1; tol.max = 15; tol.step = 0.5;
    const save = async () => {
      const poses = boxes.filter(b => b.checked).map(b => b.dataset.p); if (!poses.length) { Toast.err('Выберите хотя бы один ракурс'); return; }
      const t = clamp(parseFloat(tol.value) || 5, 1, 15);
      try { await Api.post('/rooms/' + Api.room + '/protocol', { face: { poses, tolerance: t } }); this.poses = poses; this.tol = t; await Api.ev('control', { action: 'set_face', poses, tolerance: t }); Toast.show('Набор ракурсов сохранён и отправлен пациенту'); } catch (e) { Toast.err(e); }
    };
    body.append(h('h3', { text: 'Набор ракурсов' }), h('div', { class: 'list' }, names.map((n, i) => h('label', { class: 'li' }, boxes[i], h('span', { class: 'grow', text: POSE_NAMES[n] })))),
      Ui.field('Допуск по углам, градусы (анфас: 5)', tol), h('button', { class: 'btn sec', onclick: save }, 'Сохранить набор'),
      h('button', { class: 'btn pri', onclick: () => this.req('capture') }, icon('camera'), 'Запросить съёмку у пациента'),
      h('h3', { text: 'Снимки этого приёма' }));
    if (!this.photos.length) body.append(h('p', { class: 'note', style: 'margin:0', text: 'Снимков пока нет.' }));
    else body.append(h('div', { class: 'thumbs' }, this.photos.map(p => { const f = this.thumbEl(p); f.append(h('button', { class: 'btn txt', style: 'min-height:32px;padding:0', onclick: () => { Api.ev('control', { action: 'request_retake', pose: p.pose }).then(() => Toast.show('Запрос на повторный снимок отправлен')).catch(Toast.err); } }, 'Повторить')); return f; })));
  },
  tabCompare(body) {
    const wrap = h('div', { class: 'stack' }); body.append(wrap, h('div', { class: 'sk', style: 'height:48px' }));
    this.loadPhotos().then(all => {
      body.replaceChildren(wrap);
      const poses = [...new Set(all.map(p => p.pose))].filter(p => all.filter(x => x.pose === p).length >= 2);
      if (!poses.length) { wrap.replaceChildren(h('h3', { text: 'Сравнение до и после' }), h('p', { class: 'note', style: 'margin:0', text: 'Нужно минимум два снимка одного ракурса одного пациента.' })); return; }
      if (!poses.includes(this.cmp.pose)) this.cmp = { pose: poses[0], before: null, after: null };
      const list = all.filter(p => p.pose === this.cmp.pose).sort((a, b) => a.ts - b.ts);
      const sel = (p) => { if (!this.cmp.before || (this.cmp.before && this.cmp.after)) { this.cmp.before = p; this.cmp.after = null; } else this.cmp.after = p; this.renderDoctor(); };
      wrap.replaceChildren(h('h3', { text: 'Сравнение до и после' }), Ui.segmented(poses.map(p => [p, POSE_NAMES[p] || p]), this.cmp.pose, p => { this.cmp = { pose: p, before: null, after: null }; this.renderDoctor(); }, 'Ракурс'),
        h('p', { class: 'note', style: 'margin:0', text: 'Нажмите снимок «до», затем снимок «после».' + (this.cmp.before ? ' До: ' + fmtDT(this.cmp.before.ts) + '.' : '') + (this.cmp.after ? ' После: ' + fmtDT(this.cmp.after.ts) + '.' : '') }),
        h('div', { class: 'thumbs' }, list.map(p => { const f = this.thumbEl(p, () => sel(p)); if ((this.cmp.before && this.cmp.before.id === p.id) || (this.cmp.after && this.cmp.after.id === p.id)) f.firstChild.classList.add('sel'); return f; })),
        h('button', { class: 'btn pri', disabled: !(this.cmp.before && this.cmp.after) || null, onclick: () => this.Compare.open(this.cmp.before, this.cmp.after, this.cmp.pose) }, icon('video2'), 'Сравнить'));
    }).catch(e => { body.replaceChildren(h('p', { text: e.message })); });
  },
  tabReport(body) {
    const out = h('div', { class: 'stack' });
    body.append(h('button', { class: 'btn pri', onclick: async e => { e.target.disabled = true; try { await this.Report.make(out); } catch (x) { Toast.err(x); } e.target.disabled = false; } }, icon('doc'), 'Сформировать отчёт'), out);
    if (this.lastReport) this.Report.show(out, this.lastReport);
  }
});

/* --- Chart: простой линейный график на canvas --- */
FaceMesh.Chart = {
  line(cv, series, o) {
    const W = cv.width = 560, H = cv.height = 240, ctx = cv.getContext('2d'), pr = o && o.print;
    const fg = pr ? '#000' : (getComputedStyle(document.body).color || '#000'), grid = pr ? '#bbb' : 'rgba(128,128,128,.35)';
    ctx.clearRect(0, 0, W, H); if (pr) { ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, W, H); }
    ctx.font = '13px sans-serif'; ctx.fillStyle = fg; ctx.textBaseline = 'top'; ctx.fillText(o.title || '', 8, 6);
    const pts = series.flatMap(s => s.points); if (!pts.length) return;
    let x0 = Math.min(...pts.map(p => p.x)), x1 = Math.max(...pts.map(p => p.x)), y0 = Math.min(...pts.map(p => p.y)), y1 = Math.max(...pts.map(p => p.y));
    if (x1 === x0) { x0 -= 86400; x1 += 86400; }
    /* фиксированная шкала одна для всех пациентов; расширяется, только если значение за её пределами */
    if (o && o.fixed) { y0 = Math.min(o.fixed[0], y0); y1 = Math.max(o.fixed[1], y1); }
    else { if (y1 === y0) { y0 -= 0.05; y1 += 0.05; } const pad = (y1 - y0) * 0.1; y0 -= pad; y1 += pad; }
    const L = 52, R = 12, T = 30, B = 44, pw = W - L - R, ph = H - T - B;
    const X = x => L + (x - x0) / (x1 - x0) * pw, Y = y => T + ph - (y - y0) / (y1 - y0) * ph;
    ctx.strokeStyle = grid; ctx.lineWidth = 1; ctx.fillStyle = fg; ctx.textBaseline = 'middle';
    for (let i = 0; i <= 4; i++) { const yv = y0 + (y1 - y0) * i / 4, yy = Y(yv); ctx.beginPath(); ctx.moveTo(L, yy); ctx.lineTo(W - R, yy); ctx.stroke(); ctx.textAlign = 'right'; ctx.fillText(yv.toFixed(Math.abs(y1 - y0) < 1 ? 2 : 1), L - 6, yy); }
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    const uniq = [...new Set(pts.map(p => p.x))].sort((a, b) => a - b); const step = Math.ceil(uniq.length / 5);
    uniq.forEach((x, i) => { if (i % step === 0) ctx.fillText(fmtDate(x), clamp(X(x), L + 20, W - R - 20), H - B + 8); });
    const css = getComputedStyle(document.documentElement), tok = (n, d) => (pr ? d : css.getPropertyValue(n).trim() || d);
    const cols = [tok('--side-l', '#0B5563'), tok('--side-r', '#B4530A'), tok('--ser3', '#5E7A1F'), tok('--ser4', '#7A4E9C')];
    series.forEach((s, k) => {
      if (!s.points.length) return; ctx.strokeStyle = ctx.fillStyle = cols[k % cols.length]; ctx.lineWidth = 2; ctx.beginPath();
      s.points.forEach((p, i) => { if (i) ctx.lineTo(X(p.x), Y(p.y)); else ctx.moveTo(X(p.x), Y(p.y)); }); ctx.stroke();
      s.points.forEach(p => { ctx.beginPath(); ctx.arc(X(p.x), Y(p.y), 3.5, 0, 7); ctx.fill(); });
      if (series.length > 1) { ctx.textAlign = 'left'; ctx.textBaseline = 'top'; ctx.fillText(s.name, L + k * 90, H - 16); }
    });
  }
};
