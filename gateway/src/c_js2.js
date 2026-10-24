
/* ===== Auth ===== */
const Auth = {
  doctor: null,
  async init() {
    const me = await Api.get('/auth/me', { token: false });
    Cfg.center = me.center; Cfg.auth = me.auth_required; this.doctor = me.doctor;
    return me.doctor;
  },
  showLogin(msg) {
    State.set({ role: null, view: 'login' });
    Header.update();
    const user = Ui.input('text'), pw = Ui.input('password');
    user.autocomplete = 'username'; pw.autocomplete = 'current-password';
    const err = h('p', { class: 'sm', style: 'color:var(--badt);margin:0;min-height:20px', role: 'alert', text: msg || '' });
    const go = async e => {
      e.preventDefault(); err.textContent = '';
      try {
        const r = await Api.post('/auth/login', { login: user.value.trim(), password: pw.value }, { token: false });
        this.doctor = r.doctor; App.afterLogin();
      } catch (ex) { err.textContent = ex.status === 429 ? 'Слишком много попыток. Подождите несколько минут.' : ex.message; }
    };
    $('#main').replaceChildren(h('div', { class: 'center-col' },
      h('h1', { text: 'Вход для врача' }), h('p', { class: 'mut', text: Cfg.center }),
      h('form', { class: 'stack', style: 'width:min(360px,100%)', onsubmit: go },
        Ui.field('Логин', user), Ui.field('Пароль', pw), err,
        h('button', { class: 'btn pri lg', type: 'submit', text: 'Войти' })),
      h('button', { class: 'btn txt', onclick: () => Session.askCode(), text: 'Я пациент: ввести код' })));
    user.focus();
  },
  async logout() {
    try { await Api.post('/auth/logout', {}, { token: false }); } catch (e) { /* уже вышли */ }
    this.doctor = null; App.teardown(); history.replaceState(null, '', location.pathname); this.showLogin();
  }
};

/* ===== Приёмы (врач) ===== */
const Appointments = {
  timer: null,
  dayStart() { const d = new Date(); d.setHours(0, 0, 0, 0); return Math.floor(d / 1000); },
  async show() {
    State.set({ view: 'appointments' }); Header.update();
    clearInterval(this.timer);
    const list = h('div', { class: 'stack' }, h('div', { class: 'sk', style: 'height:64px' }), h('div', { class: 'sk', style: 'height:64px' }));
    $('#main').replaceChildren(h('div', { class: 'page wide' }, h('div', { class: 'rowb', style: 'margin-bottom:16px' }, h('h1', { text: 'Приёмы' }),
      h('button', { class: 'btn pri', onclick: () => this.newSheet() }, icon('plus'), 'Новый приём')), list));
    const load = async () => {
      if (State.v.view !== 'appointments') { clearInterval(this.timer); return; }
      try {
        const from = this.dayStart(), to = from + 14 * 86400;
        const r = await Api.get('/appointments?from=' + from + '&to=' + to);
        this.render(list, r.appointments);
      } catch (e) { if (e.status === 401) { Auth.showLogin('Сессия истекла'); } else list.replaceChildren(h('p', { class: 'mut', text: e.message })); }
    };
    await load(); this.timer = setInterval(load, 15000);
  },
  render(box, items) {
    if (!items.length) { box.replaceChildren(h('p', { class: 'mut', text: 'На ближайшие дни приёмов нет. Нажмите «Новый приём».' })); return; }
    const days = new Map();
    for (const a of items) { const k = fmtDate(a.scheduled); if (!days.has(k)) days.set(k, []); days.get(k).push(a); }
    const today = fmtDate(Math.floor(Date.now() / 1000));
    box.replaceChildren(...[...days].map(([k, arr]) => h('div', { class: 'stack' },
      h('h3', { text: k === today ? 'Сегодня, ' + k : k }),
      h('div', { class: 'list' }, arr.map(a => h('div', { class: 'li' },
        h('span', { class: 'mono', text: fmtTime(a.scheduled) }),
        h('div', { class: 'grow' }, h('div', { class: 't', text: a.patient_name || 'Без имени' }), h('div', { class: 'sm mut' }, 'Код ', h('span', { class: 'mono', text: a.code }))),
        Ui.statusChip(a.status),
        h('button', { class: 'ibtn', 'aria-label': 'Скопировать ссылку', title: 'Скопировать ссылку-приглашение', onclick: () => Ui.copy(a.link, 'Ссылка скопирована') }, icon('copy')),
        a.status === 'completed' ? null : h('button', { class: 'btn pri', onclick: () => Doctor.open(a.id) }, 'Открыть')))))));
  },
  async newSheet() {
    const name = Ui.input('text', 'Фамилия Имя Отчество'); name.setAttribute('list', 'ptlist'); name.autocomplete = 'off';
    const dl = h('datalist', { id: 'ptlist' });
    const map = new Map();
    const when = Ui.input('datetime-local'); const d = new Date(Date.now() + 3600e3); d.setMinutes(0, 0, 0);
    when.value = d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + 'T' + pad2(d.getHours()) + ':00';
    const email = Ui.input('email', 'необязательно');
    const err = h('p', { class: 'sm', style: 'color:var(--badt);margin:0', role: 'alert' });
    let t = null;
    name.addEventListener('input', () => {
      clearTimeout(t); t = setTimeout(async () => {
        if (name.value.trim().length < 2) return;
        try { const r = await Api.get('/patients?q=' + encodeURIComponent(name.value.trim())); map.clear(); dl.replaceChildren(); for (const p of r.patients) { map.set(p.full_name, p.id); dl.append(h('option', { value: p.full_name })); } } catch (e) { /* подсказки необязательны */ }
      }, 250);
    });
    const s = Ui.sheet({
      title: 'Новый приём', actions: [{ label: 'Отмена', kind: 'sec' }, {
        label: 'Создать', kind: 'pri', keep: true, onClick: async api => {
          err.textContent = '';
          if (!name.value.trim()) { err.textContent = 'Укажите ФИО пациента'; return false; }
          try {
            const body = { scheduled: Math.floor(new Date(when.value).getTime() / 1000), email: email.value.trim() };
            if (map.has(name.value.trim())) body.patient_id = map.get(name.value.trim()); else body.full_name = name.value.trim();
            const r = await Api.post('/appointments', body);
            api.close(); this.created(r); this.show();
          } catch (e) { err.textContent = e.message; return false; }
          return false;
        }
      }]
    });
    s.el.append(Ui.field('ФИО (начните вводить, чтобы выбрать существующего пациента)', name), dl, Ui.field('Дата и время', when), Ui.field('Email для приглашения', email), err);
  },
  created(r) {
    const s = Ui.sheet({ title: 'Приём создан', actions: [{ label: 'Готово', kind: 'pri' }] });
    s.el.append(h('p', { class: 'mut', style: 'margin:0', text: 'Передайте пациенту ссылку или код. Регистрация пациенту не нужна.' }),
      h('div', { class: 'list' },
        h('div', { class: 'li' }, h('div', { class: 'grow' }, h('div', { class: 'sm mut', text: 'Ссылка-приглашение' }), h('div', { class: 't mono sm', text: r.link })), h('button', { class: 'ibtn', 'aria-label': 'Скопировать ссылку', onclick: () => Ui.copy(r.link) }, icon('copy'))),
        h('div', { class: 'li' }, h('div', { class: 'grow' }, h('div', { class: 'sm mut', text: 'Код' }), h('div', { class: 'mono', style: 'font-size:22px', text: r.code })), h('button', { class: 'ibtn', 'aria-label': 'Скопировать код', onclick: () => Ui.copy(r.code) }, icon('copy')))),
      h('p', { class: 'sm mut', style: 'margin:0', text: r.email_sent ? 'Письмо с приглашением отправлено на email пациента.' : 'Автоматическая отправка писем не настроена (SMTP не задан).' }));
  }
};

/* ===== Media ===== */
const Media = {
  stream: null, cam: null, screen: null, micOn: true, camOn: true, audioOnly: false, sharing: false, sel: { a: '', v: '' }, ac: null,
  explain(e) {
    const n = e && e.name;
    if (!window.isSecureContext) return 'Камера и микрофон работают только на защищённом соединении (https://) или на localhost. Откройте страницу по https-адресу центра.';
    if (n === 'NotAllowedError' || n === 'SecurityError') return 'Доступ к камере или микрофону запрещён. Разрешите его в настройках браузера (значок замка рядом с адресом) и нажмите «Повторить».';
    if (n === 'NotFoundError' || n === 'OverconstrainedError') return 'Камера или микрофон не найдены. Подключите устройство или выберите другое.';
    if (n === 'NotReadableError') return 'Устройство занято другим приложением. Закройте его и нажмите «Повторить».';
    return 'Не удалось получить доступ к устройствам: ' + ((e && e.message) || n || 'неизвестная ошибка');
  },
  async start(opt) {
    opt = opt || {};
    this.stop();
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw Object.assign(new Error('API'), { name: 'SecurityError' });
    const audio = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
    if (this.sel.a) audio.deviceId = { exact: this.sel.a };
    const video = { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } };
    if (this.sel.v) video.deviceId = { exact: this.sel.v }; else video.facingMode = 'user';
    let s = null, vErr = null;
    if (!opt.audioOnly) {
      try { s = await navigator.mediaDevices.getUserMedia({ audio, video }); }
      catch (e) { vErr = e; if (e.name === 'NotAllowedError' || e.name === 'SecurityError') throw e; }
    }
    if (!s) {
      s = await navigator.mediaDevices.getUserMedia({ audio });
      this.audioOnly = true; this.camOn = false;
      if (vErr) Toast.show('Камера недоступна, включён режим только аудио', null, 5000);
    } else { this.audioOnly = false; this.camOn = true; }
    this.stream = s; this.cam = s.getVideoTracks()[0] || null; this.micOn = true;
    return s;
  },
  stop() {
    for (const st of [this.stream, this.screen]) if (st) st.getTracks().forEach(t => t.stop());
    this.stream = null; this.cam = null; this.screen = null; this.sharing = false;
    if (this.ac) { try { this.ac.close(); } catch (e) { /* ignore */ } this.ac = null; }
  },
  async devices() {
    try { const d = await navigator.mediaDevices.enumerateDevices(); return { a: d.filter(x => x.kind === 'audioinput'), v: d.filter(x => x.kind === 'videoinput') }; }
    catch (e) { return { a: [], v: [] }; }
  },
  async switchDevice(kind, id) {
    this.sel[kind] = id;
    const keepMic = this.micOn, keepCam = this.camOn;
    const old = this.stream;
    const ns = await this.start({ audioOnly: this.audioOnly });
    this.micOn = keepMic; ns.getAudioTracks().forEach(t => t.enabled = keepMic);
    if (this.cam) this.cam.enabled = keepCam, this.camOn = keepCam;
    if (Rtc.pc) { for (const t of ns.getTracks()) { const s = Rtc.senders[t.kind]; if (s && !(t.kind === 'video' && this.sharing)) await s.replaceTrack(t); } }
    CallUi.setLocal(ns); if (old) old.getTracks().forEach(t => t.stop());
  },
  toggleMic() { this.micOn = !this.micOn; if (this.stream) this.stream.getAudioTracks().forEach(t => t.enabled = this.micOn); return this.micOn; },
  toggleCam() { if (!this.cam) { Toast.show('Камера недоступна'); return false; } this.camOn = !this.camOn; this.cam.enabled = this.camOn; return this.camOn; },
  setAudioOnly(on) { if (!this.cam) return; this.cam.enabled = !on; this.camOn = !on; this.audioOnly = on; },
  async shareScreen() {
    if (!navigator.mediaDevices.getDisplayMedia) { Toast.err('Демонстрация экрана не поддерживается этим браузером'); return false; }
    try {
      const s = await navigator.mediaDevices.getDisplayMedia({ video: true });
      const t = s.getVideoTracks()[0];
      this.screen = s; this.sharing = true;
      t.onended = () => this.stopShare();
      if (Rtc.senders.video) await Rtc.senders.video.replaceTrack(t);
      CallUi.shareChanged(true); return true;
    } catch (e) { if (e.name !== 'NotAllowedError') Toast.err('Не удалось начать демонстрацию экрана'); return false; }
  },
  async stopShare() {
    if (!this.sharing) return;
    this.sharing = false;
    if (this.screen) this.screen.getTracks().forEach(t => t.stop());
    this.screen = null;
    if (Rtc.senders.video && this.cam) { try { await Rtc.senders.video.replaceTrack(this.cam); } catch (e) { /* ignore */ } }
    CallUi.shareChanged(false);
  },
  /* максимальное разрешение для съёмки лица: отдельное применение ограничений */
  async applyMax() {
    const t = this.cam; if (!t || !t.applyConstraints) return null;
    try {
      const c = t.getCapabilities ? t.getCapabilities() : {};
      await t.applyConstraints({ width: { ideal: (c.width && c.width.max) || 3840 }, height: { ideal: (c.height && c.height.max) || 2160 } });
    } catch (e) { /* остаёмся на текущем */ }
    return t.getSettings();
  },
  async restore720() { const t = this.cam; if (t && t.applyConstraints) { try { await t.applyConstraints({ width: { ideal: 1280 }, height: { ideal: 720 } }); } catch (e) { /* ignore */ } } }
};

/* ===== WebRTC ===== */
const Rtc = {
  pc: null, ice: [], queue: [], makingOffer: false, ignoreOffer: false, polite: false, timer: null, senders: {}, remote: null,
  setState(s) { if (State.v.rtc !== s) State.set({ rtc: s }); },
  async init() {
    const c = await Api.get('/rooms/' + Api.room + '/ice-config');
    this.ice = c.iceServers; this.polite = State.v.role === 'patient';
    this.setState(Media.stream ? 'media_ready' : 'idle');
  },
  create() {
    this.close(true);
    const pc = new RTCPeerConnection({ iceServers: this.ice });
    this.pc = pc; this.queue = []; this.senders = {}; this.makingOffer = false; this.ignoreOffer = false;
    if (Media.stream) for (const t of Media.stream.getTracks()) this.senders[t.kind] = pc.addTrack(t, Media.stream);
    if (!Media.stream || !Media.cam) pc.addTransceiver('video', { direction: 'recvonly' });
    pc.onicecandidate = e => { if (e.candidate) Api.ev('ice', { candidate: e.candidate.toJSON() }).catch(() => { }); };
    pc.ontrack = e => { this.remote = e.streams[0] || new MediaStream([e.track]); CallUi.setRemote(this.remote); };
    pc.onnegotiationneeded = () => { if (State.v.role === 'doctor' || this.started) this.offer(false); };
    pc.oniceconnectionstatechange = () => this.onIce(pc);
    this.setState('signaling');
    return pc;
  },
  started: false,
  async startAsDoctor() { this.started = true; this.create(); },
  /* полный перезапуск (например, пациент перезагрузил страницу) */
  async hardRestart() {
    try { await Api.ev('control', { action: 'rtc_reset' }); } catch (e) { /* пациента нет */ }
    this.started = true; this.create();
  },
  async offer(restart) {
    const pc = this.pc; if (!pc) return;
    try {
      this.makingOffer = true;
      const o = await pc.createOffer(restart ? { iceRestart: true } : undefined);
      if (pc.signalingState !== 'stable') return;
      await pc.setLocalDescription(o);
      await Api.ev('offer', { sdp: { type: o.type, sdp: o.sdp } });
      this.setState(State.v.rtc === 'reconnecting' ? 'reconnecting' : 'connecting');
    } catch (e) { if (!(e instanceof ApiErr && e.status === 410)) { console.error(e); Toast.err('Не удалось установить соединение, пробую ещё раз'); } }
    finally { this.makingOffer = false; }
  },
  async flush() {
    const q = this.queue; this.queue = [];
    for (const c of q) { try { await this.pc.addIceCandidate(c); } catch (e) { console.warn('ice', e); } }
  },
  async handle(ev) {
    const d = ev.data || {};
    try {
      if (ev.type === 'offer') {
        if (!this.pc) { this.started = true; this.create(); }
        const pc = this.pc;
        const collision = this.makingOffer || pc.signalingState !== 'stable';
        this.ignoreOffer = !this.polite && collision;
        if (this.ignoreOffer) return;
        await pc.setRemoteDescription(d.sdp); await this.flush();
        const a = await pc.createAnswer(); await pc.setLocalDescription(a);
        await Api.ev('answer', { sdp: { type: a.type, sdp: a.sdp } });
        this.setState('connecting');
      } else if (ev.type === 'answer') {
        if (!this.pc || this.pc.signalingState !== 'have-local-offer') return;
        await this.pc.setRemoteDescription(d.sdp); await this.flush();
      } else if (ev.type === 'ice') {
        if (!this.pc || !this.pc.remoteDescription) { this.queue.push(d.candidate); return; }
        try { await this.pc.addIceCandidate(d.candidate); } catch (e) { if (!this.ignoreOffer) console.warn('ice', e); }
      }
    } catch (e) { console.error(e); Toast.err('Ошибка установки соединения'); }
  },
  onIce(pc) {
    if (pc !== this.pc) return;
    const s = pc.iceConnectionState;
    clearTimeout(this.timer);
    if (s === 'connected' || s === 'completed') { this.setState('connected'); CallUi.callStarted(); }
    else if (s === 'disconnected') { this.setState('reconnecting'); this.timer = setTimeout(() => { if (pc === this.pc && pc.iceConnectionState === 'disconnected') this.restartIce(); }, 3000); }
    else if (s === 'failed') { this.setState('reconnecting'); this.restartIce(); }
  },
  restartIce() {
    if (!this.pc) return;
    if (State.v.role === 'doctor') { try { this.pc.restartIce(); } catch (e) { /* старые браузеры */ } this.offer(true); }
    else Api.ev('control', { action: 'restart_ice' }).catch(() => { });
  },
  close(keep) {
    clearTimeout(this.timer);
    if (this.pc) { const pc = this.pc; this.pc = null; pc.onicecandidate = pc.ontrack = pc.onnegotiationneeded = pc.oniceconnectionstatechange = null; try { pc.close(); } catch (e) { /* ignore */ } }
    this.queue = []; this.senders = {}; this.remote = null;
    if (!keep) { this.started = false; this.setState('closed'); }
  }
};

/* ===== Качество связи ===== */
const Quality = {
  timer: null, prev: null, bad: 0,
  start() { this.stop(); this.timer = setInterval(() => this.poll(), 2000); },
  stop() { clearInterval(this.timer); this.timer = null; this.prev = null; this.bad = 0; State.set({ quality: null }); },
  async poll() {
    const pc = Rtc.pc; if (!pc || State.v.rtc !== 'connected') return;
    let lost = 0, recv = 0, rtt = null;
    try {
      const st = await pc.getStats();
      st.forEach(r => {
        if (r.type === 'inbound-rtp' && !r.isRemote) { lost += r.packetsLost || 0; recv += r.packetsReceived || 0; }
        if (r.type === 'candidate-pair' && r.nominated && r.currentRoundTripTime != null) rtt = r.currentRoundTripTime;
      });
    } catch (e) { return; }
    const p = this.prev; this.prev = { lost, recv };
    let loss = 0; if (p) { const dl = lost - p.lost, dr = recv - p.recv; loss = dl + dr > 0 ? Math.max(0, dl) / (dl + dr) : 0; }
    let level = 'good';
    if (loss > 0.06 || (rtt != null && rtt > 0.5)) level = 'bad'; else if (loss > 0.02 || (rtt != null && rtt > 0.25)) level = 'mid';
    State.set({ quality: { level, loss, rtt } });
    if (level === 'bad') { if (++this.bad === 2) Toast.show('Связь плохая. Включите режим «Только аудио» через кнопку «Ещё».', null, 7000); } else this.bad = 0;
  }
};

/* ===== Поллинг событий ===== */
const Poller = {
  last: 0, run: false, ctl: null, fail: 0, seen: new Set(), handler: null,
  async start(handler) {
    this.stop(); this.run = true; this.handler = handler; this.ctl = new AbortController(); this.fail = 0; this.seen = new Set();
    try {
      const r = await Api.get('/rooms/' + Api.room + '/events?after=0&history=1', { signal: this.ctl.signal });
      this.last = r.last || 0; for (const ev of r.events) this.dispatch(ev);
    } catch (e) { if (this.fatal(e)) return; }
    this.loop();
  },
  fatal(e) {
    if (e && (e.status === 410 || e.status === 401 || e.status === 404)) { this.run = false; App.roomGone(e); return true; }
    return false;
  },
  async loop() {
    while (this.run) {
      try {
        const r = await Api.get('/rooms/' + Api.room + '/events?after=' + this.last + '&wait=15', { signal: this.ctl.signal });
        if (this.fail) { this.fail = 0; App.setOffline(false); }
        for (const ev of r.events) { this.last = Math.max(this.last, ev.id); this.dispatch(ev); }
        if (!r.events.length && r.last > this.last) this.last = r.last;
      } catch (e) {
        if (!this.run || (e && e.name === 'AbortError')) return;
        if (this.fatal(e)) return;
        this.fail++; if (this.fail >= 2) App.setOffline(true);
        await sleep(Math.min(10000, 1000 * Math.pow(2, this.fail)));
      }
    }
  },
  dispatch(ev) {
    if (this.seen.has(ev.id)) return;
    this.seen.add(ev.id); if (this.seen.size > 2000) this.seen = new Set([...this.seen].slice(-1000));
    this.last = Math.max(this.last, ev.id);
    try { this.handler(ev); } catch (e) { console.error(e); }
  },
  stop() { this.run = false; if (this.ctl) { try { this.ctl.abort(); } catch (e) { /* ignore */ } } this.ctl = null; }
};

/* ===== Пациент: код, согласие, проверка оборудования, ожидание ===== */
const CONSENT_TEXT = 'Я даю согласие на проведение консультации в дистанционной форме (видеосвязь) и на обработку моих персональных данных в объёме, необходимом для оказания медицинской помощи. Я понимаю, что дистанционная консультация не заменяет очный осмотр. Текст согласия задаётся центром.'; // шаблон заменяется юристом центра
const Session = {
  code: null, hb: null,
  key() { return 'mars_pt_' + this.code; },
  askCode(msg) {
    State.set({ role: 'patient', view: 'code' }); Header.update();
    const inp = Ui.input('text', 'Код из приглашения'); inp.maxLength = 8; inp.className += ' mono'; inp.style.cssText = 'text-align:center;font-size:22px;text-transform:uppercase';
    const err = h('p', { class: 'sm', style: 'color:var(--badt);margin:0;min-height:20px', role: 'alert', text: msg || '' });
    const go = e => { e.preventDefault(); const c = inp.value.toUpperCase().replace(/[^A-Z0-9]/g, ''); if (c.length < 4) { err.textContent = 'Введите код из приглашения'; return; } this.start(c); };
    $('#main').replaceChildren(h('div', { class: 'center-col' }, h('h1', { text: 'Консультация' }), h('p', { class: 'mut', text: Cfg.center }),
      h('form', { class: 'stack', style: 'width:min(340px,100%)', onsubmit: go }, Ui.field('Код консультации', inp), err, h('button', { class: 'btn pri lg', type: 'submit', text: 'Продолжить' })),
      Auth.doctor || Cfg.auth ? h('button', { class: 'btn txt', onclick: () => Auth.showLogin(), text: 'Вход для врача' }) : null));
    inp.focus();
  },
  async start(code) {
    this.code = code.toUpperCase().replace(/[^A-Z0-9]/g, '');
    State.set({ role: 'patient', view: 'join' }); Header.update();
    let info;
    try { info = await Api.get('/rooms/' + this.code + '/info', { token: false }); }
    catch (e) { this.askCode(e.status === 404 ? 'Комната не найдена. Проверьте код.' : e.status === 410 ? 'Эта консультация уже завершена.' : e.message); return; }
    let saved = null; try { saved = sessionStorage.getItem(this.key()); } catch (e) { /* нет хранилища */ }
    if (saved) { Api.room = this.code; Api.token = saved; return this.join(); }
    this.showConsent(info);
  },
  showConsent(info) {
    const cb = h('input', { type: 'checkbox', id: 'consent', style: 'width:24px;height:24px;flex:none' });
    const btn = h('button', { class: 'btn pri lg', disabled: true, onclick: () => this.join(true) }, 'Продолжить');
    cb.addEventListener('change', () => { btn.disabled = !cb.checked; });
    $('#main').replaceChildren(h('div', { class: 'page' }, h('div', { class: 'stack' },
      h('h1', { text: info.center }),
      h('div', { class: 'list' },
        h('div', { class: 'li' }, icon('user'), h('div', { class: 'grow' }, h('div', { class: 'sm mut', text: 'Врач' }), h('div', { text: info.doctor || 'Врач центра' }))),
        h('div', { class: 'li' }, icon('calendar'), h('div', { class: 'grow' }, h('div', { class: 'sm mut', text: 'Время' }), h('div', { text: fmtDT(info.scheduled) })))),
      h('h3', { text: 'Согласие' }),
      h('p', { style: 'margin:0', text: CONSENT_TEXT }),
      h('label', { class: 'row', for: 'consent', style: 'min-height:44px;gap:12px' }, cb, h('span', { text: 'Я согласен(на) на дистанционную консультацию и обработку персональных данных' })),
      btn)));
  },
  async join(withConsent) {
    try {
      const r = await Api.post('/rooms/' + this.code + '/join', { consent: true }, { token: Api.token || undefined });
      Api.room = this.code; Api.token = r.token;
      try { sessionStorage.setItem(this.key(), r.token); } catch (e) { /* ignore */ }
    } catch (e) {
      Api.token = null;
      if (e.status === 409) this.askCode('В комнате уже есть пациент. Если это вы, обратитесь в центр.');
      else this.askCode(e.status === 410 ? 'Эта консультация уже завершена.' : e.status === 404 ? 'Комната не найдена.' : e.message);
      return;
    }
    this.precheck();
  },
  async precheck() {
    State.set({ view: 'precheck' }); Header.update();
    const vid = h('video', { autoplay: true, muted: true, playsinline: true, class: 'mirror', style: 'width:100%;aspect-ratio:4/3;object-fit:cover;background:#000;border-radius:14px' });
    const msg = h('div', { class: 'banner warn hidden', role: 'alert' });
    const meter = h('div', { class: 'meter', role: 'progressbar', 'aria-label': 'Уровень микрофона' }, h('i'));
    const selV = h('select', { class: 'inp' }), selA = h('select', { class: 'inp' });
    const go = h('button', { class: 'btn pri lg', disabled: true }, 'Войти в комнату ожидания');
    const retry = h('button', { class: 'btn sec hidden' }, 'Повторить');
    const noVid = h('button', { class: 'btn sec hidden' }, 'Продолжить без видео');
    let raf = null, an = null;
    const stopMeter = () => { if (raf) Loop.del(raf); raf = null; };
    const startMeter = stream => {
      stopMeter();
      try {
        const AC = window.AudioContext || window.webkitAudioContext; Media.ac = new AC();
        const src = Media.ac.createMediaStreamSource(stream); an = Media.ac.createAnalyser(); an.fftSize = 512; src.connect(an);
        const buf = new Uint8Array(an.fftSize);
        raf = Loop.add(() => { an.getByteTimeDomainData(buf); let m = 0; for (const b of buf) m = Math.max(m, Math.abs(b - 128)); meter.firstChild.style.width = Math.min(100, m / 64 * 100) + '%'; });
      } catch (e) { /* без индикатора */ }
    };
    const fillDevices = async () => {
      const d = await Media.devices();
      for (const [sel, list, k, nm] of [[selV, d.v, 'v', 'Камера'], [selA, d.a, 'a', 'Микрофон']]) {
        sel.replaceChildren(); list.forEach((x, i) => sel.append(h('option', { value: x.deviceId, text: x.label || (nm + ' ' + (i + 1)) })));
        const cur = (k === 'v' ? Media.cam : Media.stream && Media.stream.getAudioTracks()[0]); const id = cur && cur.getSettings().deviceId; if (id) sel.value = id;
      }
    };
    const open = async opt => {
      msg.classList.add('hidden'); retry.classList.add('hidden'); noVid.classList.add('hidden');
      try {
        const s = await Media.start(opt); vid.srcObject = s; startMeter(s); await fillDevices(); go.disabled = false;
        if (Media.audioOnly) { msg.textContent = 'Камера недоступна: консультация будет только по аудио.'; msg.classList.remove('hidden'); }
      } catch (e) {
        msg.textContent = Media.explain(e); msg.classList.remove('hidden'); retry.classList.remove('hidden'); if (window.isSecureContext) noVid.classList.remove('hidden');
      }
    };
    retry.onclick = () => open(); noVid.onclick = () => open({ audioOnly: true });
    selV.onchange = async () => { Media.sel.v = selV.value; await open(); };
    selA.onchange = async () => { Media.sel.a = selA.value; await open(); };
    go.onclick = () => { stopMeter(); this.enterWaiting(); };
    $('#main').replaceChildren(h('div', { class: 'page' }, h('div', { class: 'stack' }, h('h1', { text: 'Проверка оборудования' }),
      !window.isSecureContext ? h('div', { class: 'banner bad', role: 'alert', text: 'Страница открыта без защищённого соединения: камера и микрофон будут недоступны. Откройте ссылку по https://.' }) : null,
      vid, msg, Ui.field('Камера', selV), Ui.field('Микрофон', selA), h('div', { class: 'field' }, h('label', { text: 'Уровень микрофона: скажите что-нибудь' }), meter),
      h('div', { class: 'row wrap' }, retry, noVid), go)));
    await open();
  },
  async enterWaiting() {
    try { await Rtc.init(); } catch (e) { Toast.err(e); }
    let proto = null; try { proto = await Api.get('/rooms/' + Api.room + '/protocol'); } catch (e) { if (Poller.fatal(e)) return; }
    State.set({ view: 'call', status: proto ? proto.status : 'planned', peerHere: false });
    App.proto = proto || {};
    CallUi.build();
    Poller.start(Events.handle);
    const beat = () => { if (State.v.status !== 'in_progress' || State.v.rtc !== 'connected') Api.ev('presence', { state: State.v.status === 'in_progress' ? 'online' : 'waiting' }).catch(() => { }); };
    beat(); clearInterval(this.hb); this.hb = setInterval(beat, 10000);
  }
};

/* ===== Врач: открытие комнаты ===== */
const Doctor = {
  async open(id) {
    State.set({ role: 'doctor', view: 'loading' }); Header.update();
    $('#main').replaceChildren(h('div', { class: 'center-col' }, h('div', { class: 'sk', style: 'width:240px;height:24px' }), h('p', { class: 'mut', text: 'Открываю комнату' })));
    clearInterval(Appointments.timer);
    try {
      const r = await Api.post('/rooms', { appointment_id: id });
      Api.room = r.room; Api.token = r.token; this.apptId = id;
      history.replaceState(null, '', location.pathname + '#appt=' + id);
    } catch (e) { Toast.err(e); if (e.status === 401) Auth.showLogin('Сессия истекла'); else Appointments.show(); return; }
    let proto = {};
    try { proto = await Api.get('/rooms/' + Api.room + '/protocol'); } catch (e) { Toast.err(e); Appointments.show(); return; }
    App.proto = proto;
    try { await Media.start(); }
    catch (e) { try { await Media.start({ audioOnly: true }); Toast.show('Камера недоступна: аудио только'); } catch (e2) { Toast.show(Media.explain(e2), 'err', 8000); } }
    try { await Rtc.init(); } catch (e) { Toast.err(e); }
    State.set({ view: 'call', status: proto.status, peerHere: proto.status === 'waiting', faceConsent: false });
    CallUi.build(); Record.load(proto);
    Poller.start(Events.handle);
    if (proto.status === 'waiting') Events.patientWaiting(true);
    if (proto.status === 'in_progress') { CallUi.setStatus('in_progress'); Rtc.hardRestart(); }
  }
};
