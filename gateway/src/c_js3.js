/* ===== Состояние сервисов ===== */
const Services = {
  st: null, timer: null,
  down() { return this.st && this.st.services ? Object.keys(this.st.services).filter(k => !this.st.services[k].up) : []; },
  async poll() {
    try { const r = await fetch('/status', { cache: 'no-store' }); this.st = r.ok ? await r.json() : null; } catch (e) { this.st = null; }
    Header.update();
  },
  start() { this.poll(); this.timer = setInterval(() => this.poll(), 15000); }
};


/* ===== Шапка ===== */
const Header = {
  el: null,
  update() {
    if (!this.el) return;
    const v = State.v, inRoom = !!Api.room && v.view === 'call';
    const kids = [h('div', { class: 'brand' }, Cfg.center, ' ', h('span', { text: 'M.A.R.S.' })), h('div', { class: 'sp' })];
    const down = Services.down();
    if (down.length) kids.push(h('div', { class: 'net', role: 'status', title: T('Недоступны: ') + down.join(', ') }, h('span', { class: 'dot bad' }), h('span', { class: 'only-wide', text: T('Сервисы: ') + down.length })));
    kids.push(h('div', { class: 'net', role: 'status' }, h('span', { class: 'dot ' + (v.net ? 'ok' : 'bad') }), h('span', { class: 'only-wide', text: v.net ? T('В сети') : T('Нет связи') })));
    if (inRoom) kids.push(h('div', { class: 'codebox' }, h('span', { class: 'mut', text: T('Код') }), h('span', { class: 'mono', text: Api.room }),
      h('button', { class: 'ibtn', style: 'width:32px;height:32px', 'aria-label': T('Скопировать код сессии'), title: T('Скопировать код'), onclick: () => Ui.copy(Api.room, T('Код скопирован')) }, icon('copy'))));
    const big = Ui.isBig();
    kids.push(h('button', { class: 'ibtn', 'aria-label': T('Крупно и контрастно'), 'aria-pressed': String(big), title: big ? T('Обычный вид') : T('Крупно и контрастно'), onclick: () => { Ui.big(); this.update(); } }, icon('text')));
    kids.push(h('button', { class: 'ibtn', 'aria-label': T('Меню'), title: T('Меню'), onclick: () => this.menu() }, icon('menu')));
    this.el.replaceChildren(...kids);
  },
  menu() {
    const v = State.v, inRoom = !!Api.room && v.view === 'call', doc = v.role === 'doctor';
    const s = Ui.sheet({ title: T('Меню') });
    const item = (ic, text, fn, danger) => h('button', { class: 'li', style: 'width:100%;text-align:left' + (danger ? ';color:var(--badt)' : ''), onclick: async () => { s.close(); await fn(); } }, icon(ic), h('span', { class: 'grow', text }));
    const list = h('div', { class: 'list' });
    if (doc || (Auth.doctor && v.view !== 'join')) list.append(item('calendar', T('Приёмы'), () => App.toAppointments()));
    if (Auth.doctor && Auth.doctor.admin) list.append(item('list', T('Журнал аудита'), () => App.auditSheet()));
    list.append(item('text', Ui.isBig() ? T('Обычный вид') : T('Крупно и контрастно'), () => { Ui.big(); this.update(); }));
    list.append(item('text', Ui.langName(Ui.otherLang()), () => Ui.setLang(Ui.otherLang())));
    if (inRoom && doc) {
      list.append(item('user', T('Освободить место пациента'), async () => { try { await Api.ev('control', { action: 'reset_patient' }); Toast.show(T('Пациент сможет войти заново по ссылке')); } catch (e) { Toast.err(e); } }));
      list.append(item('alert', T('Экстренное завершение'), () => App.emergency(), true));
    }
    if (Auth.doctor && Cfg.auth) list.append(item('logout', T('Выйти'), () => Auth.logout()));
    s.el.append(list);
  }
};

/* ===== Экран звонка ===== */
const CallUi = {
  r: {}, t0: 0, timerFn: null, panes: {}, tabBtn: {}, curTab: 'chat', mq: null,
  build() {
    const isDoc = State.v.role === 'doctor', R = this.r = {};
    this.panes = {}; this.tabBtn = {}; this.t0 = 0;
    R.remote = h('video', { autoplay: true, playsinline: true, 'aria-label': T('Видео собеседника') });
    R.ovR = h('canvas', { class: 'ov' });
    R.stage = h('div', { class: 'vstage' }, R.remote, R.ovR);
    R.local = h('video', { autoplay: true, playsinline: true, class: 'mirror', 'aria-label': T('Ваше видео') }); R.local.muted = true;
    R.ovL = h('canvas', { class: 'ov mirror' });
    R.self = h('div', { class: 'selfview', 'aria-label': T('Самовидение, можно перетаскивать') }, R.local, R.ovL);
    R.qdot = h('span', { class: 'dot' }); R.qtxt = h('span', { class: 'sm', text: '' }); R.timer = h('span', { class: 'mono sm', text: '00:00' });
    const peer = isDoc ? (App.proto.appointment && App.proto.appointment.patient_name) || T('Пациент') : T(App.proto.appointment && App.proto.appointment.doctor) || T('Врач');
    R.vtop = h('div', { class: 'vtop' }, h('span', { class: 'grow', text: peer }), R.qdot, R.qtxt, R.timer);
    R.vmsg = h('div', { class: 'vmsg' });
    R.hint = h('div', { class: 'hint', role: 'status', 'aria-live': 'polite' });
    R.pq = h('div', { class: 'pqual hidden' });
    /* подпись под каждой кнопкой (data-short): иконка без слов непонятна; на телефоне класс wide-only прячет лишнее */
    const cb = (name, label, fn, short, wideOnly) => h('button', { class: 'cb' + (wideOnly ? ' wide-only' : ''), 'aria-label': label, title: label, 'data-short': short, 'aria-pressed': 'false', onclick: fn }, icon(name));
    R.bMic = cb('mic', T('Микрофон (M)'), () => this.toggleMic(), T('Микрофон'));
    R.bCam = cb('video', T('Камера (V)'), () => this.toggleCam(), T('Камера'));
    R.bScr = cb('screen', T('Демонстрация экрана'), async e => { if (Media.sharing) await Media.stopShare(); else await Media.shareScreen(); }, T('Экран'), true);
    R.bMesh = cb('mesh', isDoc ? T('Сетка лица (F)') : T('Задания для лица'), () => { if (isDoc) FaceMesh.toggle(); else FaceMesh.openTasks(); }, isDoc ? T('Сетка') : T('Задания'), !isDoc);
    R.bMore = cb('more', T('Ещё'), () => this.moreSheet(), T('Ещё'));
    R.bEnd = cb('end', isDoc ? T('Завершить приём') : T('Выйти'), () => this.endClick(), isDoc ? T('Завершить') : T('Выйти')); R.bEnd.classList.add('end');
    const bar = [R.bMic, R.bCam, R.bScr, R.bMesh];
    if (!isDoc) { R.bSide = cb('chat', T('Чат и файлы'), () => { R.layout.classList.toggle('noside'); R.bSide.setAttribute('aria-pressed', String(!R.layout.classList.contains('noside'))); if (window.matchMedia('(max-width:700px)').matches) this.showTab('chat'); }, T('Чат'), true); bar.push(R.bSide); }
    bar.push(R.bMore, R.bEnd);
    R.bar = h('div', { class: 'callbar', role: 'toolbar', 'aria-label': T('Управление звонком') }, bar);
    R.wrap = h('div', { class: 'callwrap' }, R.stage, R.self, R.vtop, R.vmsg, R.pq, R.hint, R.bar);
    R.center = h('div', { class: 'center' }, R.wrap);
    /* панели */
    const tabs = isDoc ? [['card', T('Карточка'), 'user'], ['face', T('Лицо'), 'face'], ['chat', T('Чат'), 'chat'], ['files', T('Файлы'), 'file'], ['tr', T('Расшифровка'), 'text'], ['concl', T('Заключение'), 'doc']]
      : [['chat', T('Чат'), 'chat'], ['files', T('Файлы'), 'file'], ['face', T('Задания'), 'face']];
    const content = {
      chat: Chat.build(), files: Files.build(),
      face: isDoc ? FaceMesh.doctorPane() : FaceMesh.patientPane()
    };
    if (isDoc) { R.cardBody = Record.cardPane(); content.card = R.cardBody; content.concl = Record.conclPane(); content.tr = Transcript.build(); }
    for (const [k] of tabs) this.panes[k] = h('section', { class: 'pane', id: 'pane-' + k, role: 'tabpanel' }, k === 'card' ? null : content[k]);
    if (isDoc) this.panes.card.append(R.cardBody);
    R.tabs = h('div', { class: 'tabs', role: 'tablist' }, tabs.map(([k, label]) => { const b = h('button', { role: 'tab', 'aria-selected': 'false', onclick: () => this.showTab(k) }, label); this.tabBtn[k] = b; return b; }));
    R.rside = h('aside', { class: 'rside' }, R.tabs, tabs.map(([k]) => this.panes[k]));
    R.pbar = h('nav', { class: 'pbar', 'aria-label': T('Разделы') }, [h('button', { 'aria-selected': 'true', 'data-t': 'video', onclick: () => this.showTab('video') }, icon('video'), T('Видео'))].concat(tabs.map(([k, label, ic]) => h('button', { 'aria-selected': 'false', 'data-t': k, onclick: () => this.showTab(k) }, icon(ic), label))));
    R.lside = isDoc ? h('aside', { class: 'lside' }) : null;
    R.layout = h('div', { class: 'dlayout' + (isDoc ? '' : ' pat noside'), 'data-ptab': 'video' }, R.lside, R.center, R.rside, R.pbar);
    $('#main').replaceChildren(R.layout);
    if (isDoc) {
      this.mq = window.matchMedia('(min-width:1100px)');
      this.mq.onchange = () => this.place(); this.place();
    }
    this.showTab(isDoc && !this.mq.matches ? 'card' : 'chat');
    R.layout.dataset.ptab = 'video'; this.markPbar('video');
    if (Media.stream) this.setLocal(Media.stream);
    if (Rtc.remote) this.setRemote(Rtc.remote);
    this.makeDraggable(R.self, R.wrap);
    this.shareChanged(false); this.syncBtns();
    Speech.mount(R.vtop);
    FaceMesh.mount({ role: State.v.role, remote: R.remote, local: R.local, ovR: R.ovR, ovL: R.ovL, stage: R.stage, wrap: R.wrap, hint: R.hint, pq: R.pq });
    if (isDoc) { Files.history(); }
    if (this.timerFn) Loop.del(this.timerFn);
    let last = '';
    this.timerFn = Loop.add(() => {
      if (!this.t0) return;
      const s = Math.floor((Date.now() - this.t0) / 1000), txt = pad2(Math.floor(s / 60)) + ':' + pad2(s % 60);
      if (txt !== last) { last = txt; R.timer.textContent = txt; }
    });
    this.refresh();
  },
  place() {
    const R = this.r; if (!R.lside) return;
    if (this.mq.matches) { R.lside.append(R.cardBody); this.tabBtn.card.classList.add('hidden'); R.pbar.querySelector('[data-t=card]').classList.remove('hidden'); if (this.curTab === 'card') this.showTab('chat'); }
    else { this.panes.card.append(R.cardBody); this.tabBtn.card.classList.remove('hidden'); }
  },
  showTab(name) {
    const R = this.r;
    if (name === 'video') { R.layout.dataset.ptab = 'video'; this.markPbar('video'); return; }
    if (!this.panes[name]) return;
    this.curTab = name;
    for (const k in this.panes) { this.panes[k].classList.toggle('on', k === name); this.tabBtn[k].setAttribute('aria-selected', String(k === name)); }
    R.layout.dataset.ptab = name; this.markPbar(name);
    if (R.layout.classList.contains('pat')) { if (window.matchMedia('(max-width:700px)').matches === false) { R.layout.classList.remove('noside'); if (R.bSide) R.bSide.setAttribute('aria-pressed', 'true'); } }
    if (name === 'chat') Chat.focusEnd();
    if (name === 'tr') Transcript.load();
  },
  markPbar(n) { for (const b of this.r.pbar.querySelectorAll('button')) b.setAttribute('aria-selected', String(b.dataset.t === n)); },
  setRemote(stream) {
    const R = this.r; if (!R.remote) return;
    R.remote.srcObject = stream; const p = R.remote.play(); if (p && p.catch) p.catch(() => { });
    this.refresh();
  },
  setLocal(stream) { const R = this.r; if (!R.local) return; R.local.srcObject = stream; const p = R.local.play(); if (p && p.catch) p.catch(() => { }); this.syncBtns(); },
  syncBtns() {
    const R = this.r; if (!R.bMic) return;
    R.bMic.replaceChildren(icon(Media.micOn ? 'mic' : 'micoff')); R.bMic.setAttribute('aria-pressed', String(!Media.micOn));
    R.bCam.replaceChildren(icon(Media.camOn ? 'video' : 'videooff')); R.bCam.setAttribute('aria-pressed', String(!Media.camOn));
  },
  toggleMic() { Media.toggleMic(); this.syncBtns(); },
  toggleCam() { Media.toggleCam(); this.syncBtns(); },
  shareChanged(on) { if (this.r.bScr) this.r.bScr.setAttribute('aria-pressed', String(!!on)); },
  callStarted() { if (!this.t0) { this.t0 = Date.now(); } Quality.start(); this.refresh(); },
  setStatus(s) { State.set({ status: s }); },
  refresh() {
    const R = this.r; if (!R.vmsg) return;
    const v = State.v, isDoc = v.role === 'doctor';
    let title = '', sub = '', btn = false, show = true;
    if (v.status === 'completed') { title = T('Консультация завершена'); sub = isDoc ? T('Заполните заключение и отправьте его пациенту.') : T('Спасибо. Документы от врача появятся в чате и в разделе «Файлы».'); }
    else if (v.status === 'in_progress') {
      if (v.rtc === 'connected') show = false;
      else { title = v.rtc === 'reconnecting' ? T('Восстанавливаю связь') : T('Подключение'); sub = isDoc ? (v.peerHere ? T('Устанавливаю соединение с пациентом') : T('Ждём подключения пациента')) : T('Ожидаем подключения врача'); }
    } else if (isDoc) { title = v.peerHere ? T('Пациент ожидает') : T('Ожидаем пациента'); sub = v.peerHere ? T('Укажите адрес и телефон пациента и начните приём') : T('Пациент ещё не вошёл в комнату'); btn = true; }
    else { title = T('Врач скоро подключится'); sub = T('Оставайтесь на этой странице'); }
    R.vmsg.classList.toggle('hidden', !show);
    /* replaceChildren(null) выводит слово «null»: пустые элементы отбрасываются */
    if (show) R.vmsg.replaceChildren(...[h('h2', { text: title }), h('p', { style: 'margin:0;opacity:.8', text: sub }),
      btn ? h('button', { class: 'btn pri lg', onclick: () => this.start() }, T('Начать приём')) : null].filter(Boolean));
    const q = v.quality, L = { good: ['ok', T('Связь: хорошая')], mid: ['mid', T('Связь: средняя')], bad: ['bad', T('Связь: плохая')] }[q ? q.level : ''];
    R.qdot.className = 'dot ' + (L ? L[0] : ''); R.qtxt.textContent = L ? L[1] : (v.rtc === 'reconnecting' ? T('Переподключение') : '');
  },
  async start() {
    if (!Record.locOk()) { Toast.show(T('Укажите адрес и телефон пациента')); this.showTab('card'); Record.focusLoc(); return; }
    try {
      await Record.saveLocation();
      await Api.ev('control', { action: 'start' });
      State.set({ status: 'in_progress' });
      await Rtc.startAsDoctor();
    } catch (e) { Toast.err(e); }
  },
  peerLeft() { Toast.show(T('Собеседник отключился'), null, 5000); State.set({ peerHere: false }); this.refresh(); },
  async endClick() {
    if (State.v.role === 'doctor') {
      if (State.v.status === 'completed') return;
      if (!(await Ui.confirm(T('Завершить приём?'), T('Видеосвязь будет закрыта. Затем можно заполнить заключение.'), T('Завершить'), true))) return;
      try { await Api.ev('control', { action: 'end' }); } catch (e) { Toast.err(e); return; }
      Events.ended(); this.showTab('concl');
      Ui.sheet({ title: T('Приём завершён'), actions: [{ label: T('Позже'), kind: 'sec' }, { label: T('Заполнить заключение'), kind: 'pri', onClick: () => { this.showTab('concl'); } }] }).el.append(h('p', { style: 'margin:0', text: T('Заполните заключение консультации и отправьте его пациенту.') }));
    } else {
      if (!(await Ui.confirm(T('Покинуть консультацию?'), T('Вы сможете вернуться по той же ссылке, пока врач не завершит приём.'), T('Выйти'), true))) return;
      try { await Api.ev('presence', { state: 'left' }); } catch (e) { /* ignore */ }
      App.teardown();
      $('#main').replaceChildren(h('div', { class: 'center-col' }, h('h1', { text: T('Вы вышли из консультации') }), h('p', { class: 'mut', text: T('Чтобы вернуться, откройте ссылку-приглашение ещё раз.') }),
        h('button', { class: 'btn pri lg', onclick: () => location.reload() }, T('Вернуться'))));
    }
  },
  async moreSheet() {
    const s = Ui.sheet({ title: T('Ещё') });
    const d = await Media.devices();
    const mk = (label, list, kind) => {
      const sel = h('select', { class: 'inp' }); list.forEach((x, i) => sel.append(h('option', { value: x.deviceId, text: x.label || (label + ' ' + (i + 1)) })));
      const cur = kind === 'v' ? Media.cam : (Media.stream && Media.stream.getAudioTracks()[0]); const id = cur && cur.getSettings().deviceId; if (id) sel.value = id;
      sel.onchange = async () => { try { await Media.switchDevice(kind, sel.value); } catch (e) { Toast.show(Media.explain(e), 'err', 6000); } };
      return Ui.field(label, sel);
    };
    s.el.append(mk(T('Камера'), d.v, 'v'), mk(T('Микрофон'), d.a, 'a'));
    const ao = h('button', { class: 'btn sec', onclick: () => { Media.setAudioOnly(Media.camOn); this.syncBtns(); ao.textContent = Media.camOn ? T('Включить режим «Только аудио»') : T('Выключить режим «Только аудио»'); } }, icon('audio'), Media.camOn ? T('Включить режим «Только аудио»') : T('Выключить режим «Только аудио»'));
    const fs = h('button', { class: 'btn sec', onclick: () => { s.close(); this.fullscreen(); } }, icon('full'), T('Полный экран'));
    const big = h('button', { class: 'btn sec', onclick: () => { Ui.big(); Header.update(); s.close(); } }, icon('text'), Ui.isBig() ? T('Обычный вид') : T('Крупно и контрастно'));
    const sp = State.v.role === 'doctor' ? h('button', { class: 'btn sec', onclick: () => { s.close(); Speech.request(); } }, icon('audio'), Speech.on ? T('Остановить расшифровку речи') : T('Включить расшифровку речи')) : null;
    s.el.append(h('div', { class: 'stack' }, ao, fs, big, sp));
  },
  fullscreen() {
    const el = this.r.wrap; if (!el) return;
    if (document.fullscreenElement) { document.exitFullscreen(); return; }
    const f = el.requestFullscreen || el.webkitRequestFullscreen; if (f) { const p = f.call(el); if (p && p.catch) p.catch(() => Toast.show(T('Полный экран недоступен'))); } else Toast.show(T('Полный экран недоступен в этом браузере'));
  },
  makeDraggable(el, box) {
    let sx = 0, sy = 0, ox = 0, oy = 0, drag = false;
    el.addEventListener('pointerdown', e => { drag = true; const b = box.getBoundingClientRect(), r = el.getBoundingClientRect(); ox = r.left - b.left; oy = r.top - b.top; sx = e.clientX; sy = e.clientY; el.setPointerCapture(e.pointerId); el.style.right = 'auto'; el.style.left = ox + 'px'; el.style.top = oy + 'px'; el.style.cursor = 'grabbing'; });
    el.addEventListener('pointermove', e => { if (!drag) return; const b = box.getBoundingClientRect(); el.style.left = clamp(ox + e.clientX - sx, 0, b.width - el.offsetWidth) + 'px'; el.style.top = clamp(oy + e.clientY - sy, 0, b.height - el.offsetHeight) + 'px'; });
    const up = () => { drag = false; el.style.cursor = 'grab'; };
    el.addEventListener('pointerup', up); el.addEventListener('pointercancel', up);
  }
};
State.sub((v, p) => { if ('rtc' in p || 'status' in p || 'quality' in p || 'peerHere' in p) CallUi.refresh(); if ('net' in p) Header.update(); });

/* ===== События ===== */
const Events = {
  handle(ev) {
    const t = ev.type, d = ev.data || {};
    if (t === 'kill') return App.killed();
    if (t === 'offer' || t === 'answer' || t === 'ice') return Rtc.handle(ev);
    if (t === 'chat') return Chat.add(ev);
    if (t === 'file') return Files.add(ev);
    if (t === 'document') return Chat.addDoc(ev);
    if (t === 'presence') return Events.presence(d);
    if (t === 'control') return Events.control(d);
    if (t === 'face') return FaceMesh.onFace(d);
    if (t === 'measure') return FaceMesh.onMeasure(d);
  },
  presence(d) {
    const doc = State.v.role === 'doctor';
    if (d.state === 'waiting') { const was = State.v.peerHere; if (doc) this.patientWaiting(was); else State.set({ peerHere: true }); }
    else if (d.state === 'online') { State.set({ peerHere: true }); if (doc && State.v.status === 'in_progress' && !(Rtc.pc && State.v.rtc === 'connected')) { Rtc.hardRestart(); Toast.show(T('Пациент подключился заново')); } }
    else if (d.state === 'left') { CallUi.peerLeft(); }
  },
  patientWaiting(silent) {
    const first = !State.v.peerHere;
    State.set({ peerHere: true, status: State.v.status === 'in_progress' ? 'in_progress' : 'waiting' });
    if (!silent && first) { Ui.sound(); Toast.show(T('Пациент ожидает в комнате'), null, 6000); }
  },
  control(d) {
    const doc = State.v.role === 'doctor';
    switch (d.action) {
      case 'start': if (!doc) { State.set({ status: 'in_progress' }); } break;
      case 'end': this.ended(); break;
      case 'rtc_reset': if (!doc) Rtc.close(true); break;
      case 'restart_ice': if (doc) { Rtc.offer(true); } break;
      case 'reset_patient': break;
      case 'face_consent': if (doc) FaceMesh.setConsent(!!d.granted); break;
      case 'speech_request': case 'speech_consent': case 'speech_stop': Speech.control(d); break;
      default: if (!doc) FaceMesh.patientRequest(d); break;
    }
  },
  ended() {
    Speech.stop(); Rtc.close(); Quality.stop(); FaceMesh.stopAll(); Media.stop(); clearInterval(Session.hb);
    State.set({ status: 'completed', rtc: 'closed' });
    if (CallUi.r.remote) { CallUi.r.remote.srcObject = null; CallUi.r.local.srcObject = null; }
    CallUi.refresh();
  }
};

/* ===== Чат ===== */
const Chat = {
  seen: new Set(), list: null, input: null,
  build() {
    this.seen = new Set();
    this.list = h('div', { class: 'msgs', role: 'log', 'aria-live': 'polite', 'aria-label': T('Сообщения') });
    this.input = h('textarea', { class: 'inp', rows: 1, placeholder: T('Сообщение'), 'aria-label': T('Текст сообщения'), style: 'min-height:44px;height:44px;resize:none' });
    this.input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.send(); } });
    return h('div', { class: 'stack', style: 'flex:1;min-height:0' }, this.list,
      h('div', { class: 'sheetrow' }, this.input, h('button', { class: 'ibtn', style: 'background:var(--acc);color:var(--onacc)', 'aria-label': T('Отправить'), onclick: () => this.send() }, icon('send'))));
  },
  async send() {
    const text = this.input.value.trim(); if (!text) return;
    this.input.value = '';
    try { await Api.ev('chat', { text }); } catch (e) { this.input.value = text; Toast.err(e); }
  },
  push(ev, body) {
    if (!this.list) return;
    const mine = ev.from === State.v.role;
    const m = h('div', { class: 'msg' + (mine ? ' me' : '') }, body, h('span', { class: 'tm', text: fmtTime(ev.ts) }));
    this.list.append(m); this.focusEnd();
    if (!mine && CallUi.curTab !== 'chat' && State.v.view === 'call') Toast.show(T('Новое сообщение'));
    return m;
  },
  focusEnd() { if (this.list) this.list.scrollTop = this.list.scrollHeight; },
  add(ev) {
    const d = ev.data || {}; const k = 'c' + d.mid; if (this.seen.has(k)) return; this.seen.add(k);
    this.push(ev, h('span', { text: d.text || '' }));
  },
  addFile(ev, node) { this.push(ev, node); },
  addDoc(ev) {
    const d = ev.data || {}; const k = 'd' + d.mid; if (this.seen.has(k)) return; this.seen.add(k);
    this.push(ev, h('div', { class: 'stack', style: 'gap:6px' }, h('strong', { text: T('Заключение консультации') }), h('span', { class: 'sm', text: d.doctor ? T('Врач: ') + d.doctor : '' }),
      h('div', { class: 'row wrap' }, h('button', { class: 'btn sec', onclick: () => Print.conclusion(d) }, icon('doc'), T('Открыть и распечатать')),
        h('button', { class: 'btn sec', onclick: () => Ui.exportFile('conclusion_json', 'zaklyuchenie.json', JSON.stringify(d, null, 2)) }, icon('download'), 'JSON'))));
    Files.addDocRow(ev);
  }
};

/* ===== Файлы ===== */
const EXT_OK = ['.pdf', '.jpg', '.jpeg', '.png', '.csv', '.xlsx', '.xls'];
const Files = {
  box: null, seen: new Set(), hist: null,
  build() {
    this.seen = new Set();
    const inp = h('input', { type: 'file', class: 'hidden', accept: EXT_OK.join(','), onchange: () => { for (const f of inp.files) this.upload(f); inp.value = ''; } });
    this.box = h('div', { class: 'list' });
    const drop = h('div', { class: 'stack', style: 'flex:1;min-height:0;overflow:auto' },
      h('button', { class: 'btn sec', onclick: () => inp.click() }, icon('upload'), T('Прикрепить файл')),
      h('p', { class: 'note', style: 'margin:0', text: T('PDF, JPEG, PNG, CSV, XLSX, XLS, до 20 МБ.') }), inp, this.box);
    drop.addEventListener('dragover', e => e.preventDefault());
    drop.addEventListener('drop', e => { e.preventDefault(); for (const f of e.dataTransfer.files) this.upload(f); });
    return drop;
  },
  history() { /* история файлов и снимков для левой панели строится в Record.cardPane */ },
  upload(file) {
    const ext = (file.name.match(/\.[^.]+$/) || [''])[0].toLowerCase();
    if (!EXT_OK.includes(ext)) { Toast.err(T('Недопустимый тип файла. Разрешены: PDF, JPEG, PNG, CSV, XLSX, XLS')); return; }
    if (file.size > 20 * 1024 * 1024) { Toast.err(T('Файл больше 20 МБ')); return; }
    if (file.size === 0) { Toast.err(T('Файл пустой')); return; }
    const prog = h('progress', { max: 100, value: 0, style: 'width:100%' });
    const row = h('div', { class: 'li' }, icon('upload'), h('div', { class: 'grow' }, h('div', { class: 't', text: file.name }), prog));
    this.box.prepend(row);
    const fr = new FileReader();
    fr.onerror = () => { row.remove(); Toast.err(T('Не удалось прочитать файл')); };
    fr.onload = () => {
      const b64 = String(fr.result).split(',')[1] || '';
      const x = new XMLHttpRequest();
      x.open('POST', '/rooms/' + Api.room + '/events');
      x.setRequestHeader('Content-Type', 'application/json'); x.setRequestHeader('Authorization', 'Bearer ' + Api.token);
      x.upload.onprogress = e => { if (e.lengthComputable) prog.value = e.loaded / e.total * 100; };
      x.onerror = () => { row.remove(); Toast.err(T('Нет связи при отправке файла')); };
      x.onload = () => {
        row.remove();
        if (x.status !== 202) { let m = T('Файл не принят'); try { m = JSON.parse(x.responseText).message || m; } catch (e) { /* ignore */ } Toast.err(m); }
      };
      x.send(JSON.stringify({ type: 'file', data: { name: file.name, b64 } }));
    };
    fr.readAsDataURL(file);
  },
  sizeText(n) { return n > 1048576 ? (n / 1048576).toFixed(1) + T(' МБ') : Math.max(1, Math.round(n / 1024)) + T(' КБ'); },
  async download(m) {
    try {
      const b = await Api.req('GET', '/rooms/' + Api.room + '/files/' + m.id, { blob: true });
      const a = h('a', { href: URL.createObjectURL(b), download: m.name }); document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 800);
    } catch (e) { Toast.err(e); }
  },
  add(ev) {
    const m = ev.data || {}; if (this.seen.has('f' + m.id)) return; this.seen.add('f' + m.id);
    const dl = h('button', { class: 'ibtn', 'aria-label': T('Скачать ') + m.name, onclick: () => this.download(m) }, icon('download'));
    if (this.box) this.box.prepend(h('div', { class: 'li' }, icon('file'), h('div', { class: 'grow' }, h('div', { class: 't', text: m.name }), h('div', { class: 'sm mut', text: this.sizeText(m.size) + ', ' + fmtTime(ev.ts) })), dl));
    const chatNode = h('div', { class: 'stack', style: 'gap:4px' }, h('div', { class: 'row' }, icon('file'), h('span', { text: m.name })), h('span', { class: 'sm', text: this.sizeText(m.size) }),
      h('button', { class: 'btn sec', onclick: () => this.download(m) }, icon('download'), T('Скачать')));
    if (/^image\//.test(m.mime || '')) {
      const img = h('img', { class: 'pv', alt: T('Предпросмотр: ') + m.name }); chatNode.append(img);
      Api.req('GET', '/rooms/' + Api.room + '/files/' + m.id, { blob: true }).then(b => { img.src = URL.createObjectURL(b); }).catch(() => img.remove());
    }
    Chat.addFile(ev, chatNode);
  },
  addDocRow(ev) {
    const d = ev.data || {}; if (!this.box) return;
    this.box.prepend(h('div', { class: 'li' }, icon('doc'), h('div', { class: 'grow' }, h('div', { class: 't', text: T('Заключение консультации') }), h('div', { class: 'sm mut', text: fmtTime(ev.ts) })),
      h('button', { class: 'ibtn', 'aria-label': T('Открыть заключение'), onclick: () => Print.conclusion(d) }, icon('doc'))));
  }
};

/* ===== Печатные формы ===== */
const Print = {
  go(node) {
    const pa = $('#printArea'); pa.replaceChildren(node);
    const after = () => { pa.replaceChildren(); window.removeEventListener('afterprint', after); };
    window.addEventListener('afterprint', after);
    setTimeout(() => window.print(), 80);
  },
  async conclusion(d) {
    // Сначала сервис reports; если он недоступен, печатаем формой из браузера
    try {
      const r = await Api.post('/rooms/' + Api.room + '/render-conclusion', Object.assign({}, d, { lang: LANG }));
      if (r && r.html) { this.frame(r.html); return; }
    } catch (e) { /* запасной путь ниже */ }
    this.conclusionLocal(d);
  },
  frame(html) {
    const f = h('iframe', { style: 'position:fixed;width:0;height:0;border:0', 'aria-hidden': 'true', srcdoc: html });
    f.onload = () => { try { f.contentWindow.focus(); f.contentWindow.print(); } catch (e) { /* ignore */ } setTimeout(() => f.remove(), 60000); };
    document.body.append(f);
  },
  conclusionLocal(d) {
    const c = d.conclusion || {}, row = (t, v) => [h('h2', { text: t }), h('p', { style: 'white-space:pre-wrap', text: v || '-' })];
    this.go(h('div', null, h('h1', { text: T(d.center) || Cfg.center }), h('h2', { text: T('Заключение консультации') }),
      h('table', null, h('tr', null, h('th', { text: T('Пациент') }), h('td', { text: d.patient || '' })), h('tr', null, h('th', { text: T('Дата') }), h('td', { text: d.date || '' })), h('tr', null, h('th', { text: T('Врач') }), h('td', { text: d.doctor || '' }))),
      row(T('Жалобы'), c.complaints), row(T('Анамнез'), c.anamnesis), row(T('Диагноз'), c.diagnosis), row(T('Код МКБ-10'), c.icd), row(T('Рекомендации'), c.recommendations),
      row(T('Повторный контакт'), c.followup),
      h('p', { class: 'sm', text: T('Документ сформирован по результатам дистанционной консультации. Это заключение консультации, а не электронный рецепт.') }),
      h('div', { class: 'sign', text: T('Подпись врача ______________________ / ') + (d.doctor || '') + ' /' })));
  }
};

/* ===== Карточка, заметки, заключение (врач) ===== */
const ICD = [
  ['A09', T('Инфекционный гастроэнтерит и колит')], ['B34.9', T('Вирусная инфекция неуточнённая')], ['E03.9', T('Гипотиреоз неуточнённый')], ['E11.9', T('Сахарный диабет 2 типа без осложнений')],
  ['E66.9', T('Ожирение неуточнённое')], ['E78.5', T('Гиперлипидемия неуточнённая')], ['F32.9', T('Депрессивный эпизод неуточнённый')], ['F41.2', T('Смешанное тревожное и депрессивное расстройство')],
  ['G43.9', T('Мигрень неуточнённая')], ['G47.0', T('Нарушения засыпания и поддержания сна')], ['I10', T('Эссенциальная гипертензия')], ['I20.9', T('Стенокардия неуточнённая')],
  ['I25.1', T('Атеросклеротическая болезнь сердца')], ['I50.9', T('Сердечная недостаточность неуточнённая')], ['I83.9', T('Варикозное расширение вен нижних конечностей')],
  ['J00', T('Острый назофарингит')], ['J02.9', T('Острый фарингит неуточнённый')], ['J03.9', T('Острый тонзиллит неуточнённый')], ['J06.9', T('Острая инфекция верхних дыхательных путей')],
  ['J18.9', T('Пневмония неуточнённая')], ['J20.9', T('Острый бронхит неуточнённый')], ['J30.4', T('Аллергический ринит неуточнённый')], ['J45.9', T('Астма неуточнённая')],
  ['K21.9', T('Гастроэзофагеальный рефлюкс без эзофагита')], ['K29.7', T('Гастрит неуточнённый')], ['K58.9', T('Синдром раздражённого кишечника')], ['K80.2', T('Камни жёлчного пузыря без холецистита')],
  ['L20.9', T('Атопический дерматит неуточнённый')], ['L70.0', T('Угри обыкновенные')], ['L81.9', T('Нарушение пигментации неуточнённое')], ['L90.5', T('Рубцовые состояния и фиброз кожи')],
  ['M25.5', T('Боль в суставе')], ['M54.2', T('Боль в области шеи')], ['M54.5', T('Боль внизу спины')], ['N39.0', T('Инфекция мочевыводящих путей')], ['N95.1', T('Менопауза')],
  ['R50.9', T('Лихорадка неуточнённая')], ['R51', T('Головная боль')], ['Z09', T('Последующее наблюдение после лечения')], ['Z71.9', T('Консультирование неуточнённое')]
];
const ICD_RE = /^[A-Z]\d{2}(\.\d{1,2})?$/;
const Record = {
  f: {}, loc: {}, notes: null, c: {}, timers: {}, lastConcl: null,
  deb(k, fn, ms) { clearTimeout(this.timers[k]); this.timers[k] = setTimeout(fn, ms || 800); },
  cardPane() {
    const mk = (key, label, el) => { this.f[key] = el; el.addEventListener('input', () => this.deb('card', () => this.saveCard())); return Ui.field(label, el); };
    /* в базе хранится исходное значение, на экране — перевод */
    const sel = (opts) => { const s = h('select', { class: 'inp' }); for (const o of opts) s.append(h('option', { value: o, text: o ? T(o) : T('Не указано') })); return s; };
    this.loc = { address: Ui.input('text', T('Город, улица, дом, квартира')), phone: Ui.input('tel', '+7 ...') };
    for (const k in this.loc) this.loc[k].addEventListener('input', () => this.deb('loc', () => this.saveLocation().catch(() => { })));
    this.notes = h('textarea', { class: 'inp', placeholder: T('Видны только врачу'), rows: 4 });
    this.notes.addEventListener('input', () => this.deb('notes', () => Api.post('/rooms/' + Api.room + '/protocol', { notes: this.notes.value }).catch(Toast.err)));
    const hist = Files.hist = h('div', { class: 'stack' });
    /* открыто то, что нужно на каждом приёме; анамнез и заметки свёрнуты, пока пустые */
    this.histFold = Ui.fold(T('Анамнез: кровь, аллергии, болезни, препараты'),
      mk('blood', T('Группа крови'), sel(['', 'O(I) Rh+', 'O(I) Rh-', 'A(II) Rh+', 'A(II) Rh-', 'B(III) Rh+', 'B(III) Rh-', 'AB(IV) Rh+', 'AB(IV) Rh-'])),
      mk('allergies', T('Аллергии'), h('textarea', { class: 'inp', rows: 2 })), mk('chronic', T('Хронические заболевания'), h('textarea', { class: 'inp', rows: 2 })),
      mk('meds', T('Принимаемые препараты'), h('textarea', { class: 'inp', rows: 2 })));
    this.notesFold = Ui.fold(T('Приватные заметки'), this.notes, h('p', { class: 'note', style: 'margin:0', text: T('Заметки и карточка пациенту не передаются.') }));
    return h('div', { class: 'stack card' },
      h('div', { class: 'rowb' }, h('h3', { text: T('Пациент') }), h('span', { class: 'chip', text: T('со слов пациента') })),
      mk('full_name', T('ФИО'), Ui.input('text')), mk('birth', T('Дата рождения'), Ui.input('date')),
      mk('sex', T('Пол'), sel(['', RU('Женский'), RU('Мужской')])),
      h('div', { class: 'locbox stack' }, h('h3', { text: T('Местонахождение пациента') }),
        Ui.field(T('Адрес (обязательно)'), this.loc.address), Ui.field(T('Телефон (обязательно)'), this.loc.phone),
        h('p', { class: 'note', style: 'margin:0', text: T('Экстренные службы: 103 / 112') })),
      mk('complaints', T('Жалобы'), h('textarea', { class: 'inp', rows: 3 })),
      this.histFold, this.notesFold, hist);
  },
  load(p) {
    const c = p.card || {};
    for (const k in this.f) this.f[k].value = c[k] || '';
    this.loc.address.value = (p.location && p.location.address) || ''; this.loc.phone.value = (p.location && p.location.phone) || '';
    this.notes.value = p.notes || '';
    this.histFold.open = ['blood', 'allergies', 'chronic', 'meds'].some(k => (this.f[k].value || '').trim());
    this.notesFold.open = !!this.notes.value.trim();
    this.fillConcl(p.conclusion || {});
    this.lastConcl = p.conclusion || null;
    FaceMesh.loadProto(p);
  },
  locOk() { return this.loc.address.value.trim().length > 2 && this.loc.phone.value.trim().length > 4; },
  focusLoc() { (this.loc.address.value.trim() ? this.loc.phone : this.loc.address).focus(); },
  saveLocation() { return Api.post('/rooms/' + Api.room + '/protocol', { location: { address: this.loc.address.value.trim(), phone: this.loc.phone.value.trim() } }); },
  collectCard() { const o = {}; for (const k in this.f) o[k] = this.f[k].value; return o; },
  async saveCard() { try { await Api.ev('ehr', { card: this.collectCard() }); } catch (e) { Toast.err(e); } },
  conclPane() {
    const mk = (k, label, el) => { this.c[k] = el; return Ui.field(label, el); };
    const icd = Ui.input('text', T('Например J06.9')); icd.setAttribute('list', 'icdlist'); icd.maxLength = 6; icd.style.textTransform = 'uppercase';
    const dl = h('datalist', { id: 'icdlist' }, ICD.map(([c, n]) => h('option', { value: c, label: n })));
    const hintEl = h('div', { class: 'note', 'aria-live': 'polite', text: '' });
    icd.addEventListener('input', () => {
      const v = icd.value.toUpperCase().trim(); const f = ICD.find(x => x[0] === v);
      hintEl.textContent = !v ? '' : f ? f[1] : (ICD_RE.test(v) ? T('Код корректного формата') : T('Формат кода: A00 или A00.0'));
      icd.setAttribute('aria-invalid', String(!!v && !ICD_RE.test(v)));
    });
    this.c.icd = icd;
    return h('div', { class: 'stack' }, h('h3', { text: T('Заключение консультации') }),
      mk('complaints', T('Жалобы'), h('textarea', { class: 'inp', rows: 3 })), mk('anamnesis', T('Анамнез'), h('textarea', { class: 'inp', rows: 3 })),
      mk('diagnosis', T('Диагноз'), h('textarea', { class: 'inp', rows: 2 })), Ui.field(T('Код МКБ-10'), icd), dl, hintEl,
      mk('recommendations', T('Рекомендации'), h('textarea', { class: 'inp', rows: 4 })), mk('followup', T('Дата повторного контакта'), Ui.input('date')),
      h('div', { class: 'row wrap' }, h('button', { class: 'btn pri', onclick: () => this.saveConcl() }, T('Сохранить')),
        h('button', { class: 'btn sec', onclick: () => this.printConcl() }, icon('doc'), T('Печать')),
        h('button', { class: 'btn sec', onclick: () => this.sendConcl() }, icon('send'), T('Отправить пациенту')),
        h('button', { class: 'btn sec', onclick: () => this.exportJson() }, icon('download'), T('JSON для МИС'))),
      h('p', { class: 'note', style: 'margin:0', text: T('Это заключение консультации, а не электронный рецепт.') }));
  },
  fillConcl(c) { for (const k of ['complaints', 'anamnesis', 'diagnosis', 'icd', 'recommendations', 'followup']) if (this.c[k]) this.c[k].value = c[k] || ''; },
  collectConcl() { const o = {}; for (const k of ['complaints', 'anamnesis', 'diagnosis', 'icd', 'recommendations', 'followup']) o[k] = (this.c[k].value || '').trim(); o.icd = o.icd.toUpperCase(); return o; },
  async saveConcl() {
    const c = this.collectConcl();
    if (c.icd && !ICD_RE.test(c.icd)) { Toast.err(T('Код МКБ-10 должен иметь вид A00 или A00.0')); this.c.icd.focus(); return false; }
    try { await Api.post('/rooms/' + Api.room + '/protocol', { conclusion: c }); this.lastConcl = c; Toast.show(T('Заключение сохранено')); return true; }
    catch (e) { Toast.err(e); return false; }
  },
  payload() {
    const a = App.proto.appointment || {};
    return { center: Cfg.center, doctor: (Auth.doctor && Auth.doctor.name) || a.doctor || '', patient: this.f.full_name.value || a.patient_name || '', date: new Date().toISOString().slice(0, 10), conclusion: this.collectConcl() };
  },
  printConcl() { Print.conclusion(this.payload()); },
  async sendConcl() {
    if (!(await this.saveConcl())) return;
    try { await Api.ev('document', {}); Toast.show(T('Заключение отправлено пациенту')); } catch (e) { Toast.err(e); }
  },
  exportJson() {
    const p = this.payload(); const a = App.proto.appointment || {};
    Ui.exportFile('consultation_json', 'konsultaciya-' + (a.code || 'mars') + '.json', JSON.stringify({ schema: 'mars-consultation/1', center: p.center, appointment: { id: a.id, code: a.code, scheduled: a.scheduled }, doctor: p.doctor, patient: this.collectCard(), location: { address: this.loc.address.value, phone: this.loc.phone.value }, conclusion: p.conclusion }, null, 2));
  }
};

/* ===== Приложение ===== */
const App = {
  proto: {}, dead: false, banner: null,
  shell() {
    Header.el = h('header', { class: 'top' });
    this.banner = h('div', { class: 'banner bad hidden', role: 'alert', text: T('Нет связи с сервером. Пробую восстановить подключение.') });
    $('#app').replaceChildren(Header.el, this.banner, h('main', { id: 'main' }));
    Header.update(); Services.start();
  },
  async init() {
    try { if (localStorage.getItem('mars_big') === '1') document.documentElement.classList.add('big', 'hc'); } catch (e) { /* ignore */ }
    this.shell();
    window.addEventListener('online', () => this.setOffline(false));
    window.addEventListener('offline', () => this.setOffline(true));
    window.addEventListener('pagehide', () => { if (State.v.view === 'call' && !this.dead) Api.bye(); });
    window.addEventListener('unhandledrejection', e => { e.preventDefault(); console.error(e.reason); if (e.reason && e.reason.name !== 'AbortError') Toast.err(e.reason instanceof ApiErr ? e.reason.message : T('Не удалось выполнить действие')); });
    document.addEventListener('keydown', e => this.key(e));
    try {
      const hl = await Api.get('/health', { token: false });
      Cfg.mediapipe = hl.mediapipe; Cfg.secure = hl.secure; Cfg.center = hl.center; Cfg.auth = hl.auth_required;
      const doc = await Auth.init();
      Header.update();
      const code = new URLSearchParams(location.search).get('code');
      if (code) { Session.start(code); return; }
      if (doc) this.afterLogin(); else Auth.showLogin();
    } catch (e) {
      $('#main').replaceChildren(h('div', { class: 'center-col' }, h('h2', { text: T('Сервер недоступен') }), h('p', { class: 'mut', text: e.message }), h('button', { class: 'btn pri', onclick: () => location.reload() }, T('Повторить'))));
    }
  },
  afterLogin() {
    Header.update();
    const m = location.hash.match(/appt=(\d+)/);
    if (m) Doctor.open(parseInt(m[1], 10)); else Appointments.show();
  },
  key(e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'Escape') { if (Ui.sheets.length) { Ui.sheets[Ui.sheets.length - 1].close(); return; } if (document.fullscreenElement) document.exitFullscreen(); return; }
    if (e.target.closest && e.target.closest('input,textarea,select,[contenteditable]')) return;
    if (State.v.view !== 'call' || this.dead) return;
    const k = e.key.toLowerCase();
    if (k === 'm' || k === 'ь') CallUi.toggleMic();
    else if (k === 'v' || k === 'м') CallUi.toggleCam();
    else if (k === 'f' || k === 'а') FaceMesh.cycleMode();
  },
  setOffline(off) { State.set({ net: !off }); this.banner.classList.toggle('hidden', !off); },
  toAppointments() {
    if (State.v.view === 'call' && State.v.role === 'doctor') { Api.ev('presence', { state: 'left' }).catch(() => { }); }
    this.teardown(); history.replaceState(null, '', location.pathname); Appointments.show();
  },
  teardown() {
    Poller.stop(); clearInterval(Session.hb); clearInterval(Appointments.timer);
    Speech.stop(); Rtc.close(); Quality.stop(); FaceMesh.stopAll(); Media.stop(); Ui.closeAudio();
    Api.room = null; Api.token = null; State.set({ view: '', rtc: 'idle', status: 'planned', peerHere: false, faceConsent: false });
    if (CallUi.timerFn) Loop.del(CallUi.timerFn);
    Header.update();
  },
  async emergency() {
    if (!(await Ui.confirm(T('Экстренное завершение'), T('Консультация будет немедленно прервана у обоих участников, комната закроется. Продолжить?'), T('Завершить немедленно'), true))) return;
    try { await Api.ev('kill', {}); } catch (e) { if (!(e instanceof ApiErr && e.status === 410)) { Toast.err(e); return; } }
    this.killed();
  },
  killed() {
    if (this.dead) return; this.dead = true;
    const doc = State.v.role === 'doctor';
    try { sessionStorage.removeItem('mars_pt_' + Session.code); } catch (e) { /* ignore */ }
    Poller.stop(); clearInterval(Session.hb); clearInterval(Appointments.timer);
    Speech.stop(); Rtc.close(); Quality.stop(); FaceMesh.stopAll(); Media.stop(); Ui.closeAudio(); Ui.closeAll(); Loop.stop();
    Api.room = null; Api.token = null; Chat.list = null; Files.box = null; CallUi.r = {};
    $('#printArea').replaceChildren();
    State.set({ view: 'killed', rtc: 'closed', status: 'completed', peerHere: false });
    Header.update();
    $('#main').replaceChildren(h('div', { class: 'center-col' }, h('h1', { text: T('Консультация прервана') }),
      h('p', { class: 'mut', text: doc ? T('Комната закрыта у обоих участников. Данные на экране очищены.') : T('Врач завершил сеанс. Данные на экране очищены.') }),
      doc ? h('button', { class: 'btn pri lg', onclick: () => { this.dead = false; history.replaceState(null, '', location.pathname); Appointments.show(); } }, T('К списку приёмов')) : null));
  },
  roomGone(e) {
    if (this.dead) return;
    if (e && e.status === 410) { this.killed(); return; }
    Poller.stop(); clearInterval(Session.hb); Speech.stop(); Rtc.close(); Quality.stop(); FaceMesh.stopAll(); Media.stop();
    $('#main').replaceChildren(h('div', { class: 'center-col' }, h('h2', { text: T('Сеанс недоступен') }),
      h('p', { class: 'mut', text: T('Комната не найдена или доступ к ней потерян (возможно, сервер перезапущен).') }),
      h('button', { class: 'btn pri', onclick: () => { try { sessionStorage.removeItem('mars_pt_' + Session.code); } catch (x) { /* ignore */ } location.reload(); } }, T('Начать заново'))));
  },
  async auditSheet() {
    const s = Ui.sheet({ title: T('Журнал аудита'), wide: true });
    s.el.append(h('div', { class: 'sk', style: 'height:80px' }));
    try {
      const r = await Api.get('/audit?limit=300');
      s.el.replaceChildren(h('div', { style: 'overflow:auto;max-height:60dvh' }, h('table', { class: 'tb' },
        h('tr', null, [T('Время'), T('Участник'), T('Действие'), T('Комната'), T('Деталь')].map(t => h('th', { text: t }))),
        r.items.map(i => h('tr', null, h('td', { text: fmtDT(i.ts) }), h('td', { text: i.actor }), h('td', { text: i.action }), h('td', { class: 'mono', text: i.room }), h('td', { text: i.detail }))))));
    } catch (e) { s.el.replaceChildren(h('p', { text: e.message })); }
  }
};
