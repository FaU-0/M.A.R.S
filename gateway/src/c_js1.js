'use strict';
/* ===== Вспомогательные функции ===== */
const $ = (s, r) => (r || document).querySelector(s);
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) for (const k in attrs) {
    const v = attrs[k];
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'style') el.style.cssText = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
}
const ICONS = {
  mic: 'M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z|M5 11a7 7 0 0 0 14 0|M12 18v3',
  micoff: 'M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z|M5 11a7 7 0 0 0 14 0|M12 18v3|M4 4l16 16',
  video: 'M3 7h11a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H3z|M16 11l5-3v8l-5-3',
  videooff: 'M3 7h11a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H3z|M16 11l5-3v8l-5-3|M4 4l16 16',
  screen: 'M3 5h18v11H3z|M8 20h8|M12 16v4',
  mesh: 'M12 3c4 0 6 3 6 8s-2 10-6 10-6-5-6-10 2-8 6-8z|M6 11h12|M12 3v18|M8 7h8|M8 15h8',
  more: 'M5 12h1|M11.5 12h1|M18 12h1',
  end: 'M3 15c5-5 13-5 18 0l-2 3-4-2v-2c-2-1-4-1-6 0v2l-4 2z',
  chat: 'M4 5h16v11H9l-5 4z',
  file: 'M6 3h8l4 4v14H6z|M14 3v4h4',
  doc: 'M6 3h8l4 4v14H6z|M14 3v4h4|M9 12h6|M9 16h6',
  face: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z|M9 10h.01|M15 10h.01|M9 15c1.5 1.5 4.5 1.5 6 0',
  send: 'M4 12l16-8-6 16-2-7z',
  plus: 'M12 5v14|M5 12h14',
  close: 'M6 6l12 12|M18 6L6 18',
  copy: 'M8 8h11v12H8z|M5 16V4h11',
  user: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z|M4 21c0-4 4-6 8-6s8 2 8 6',
  calendar: 'M4 6h16v14H4z|M4 10h16|M8 3v4|M16 3v4',
  check: 'M5 12l5 5 9-10',
  full: 'M4 9V4h5|M20 9V4h-5|M4 15v5h5|M20 15v5h-5',
  menu: 'M4 7h16|M4 12h16|M4 17h16',
  alert: 'M12 4l9 16H3z|M12 10v4|M12 17h.01',
  text: 'M3 19l5-13 5 13|M5 14h6|M15 19v-6a2 2 0 0 1 4 0v6|M15 16h4',
  download: 'M12 4v11|M7 11l5 5 5-5|M5 20h14',
  upload: 'M12 16V5|M7 9l5-5 5 5|M5 20h14',
  camera: 'M4 8h4l2-3h4l2 3h4v11H4z|M12 17a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z',
  logout: 'M10 4H4v16h6|M15 8l5 4-5 4|M9 12h11',
  audio: 'M5 14v-3a7 7 0 0 1 14 0v3|M5 14h3v5H5z|M16 14h3v5h-3z',
  list: 'M8 6h12|M8 12h12|M8 18h12|M4 6h.01|M4 12h.01|M4 18h.01',
  video2: 'M4 5h7v14H4z|M13 5h7v14h-7z',
  card: 'M4 5h16v14H4z|M4 10h16|M7 15h4'
};
function icon(name) {
  const NS = 'http://www.w3.org/2000/svg';
  const s = document.createElementNS(NS, 'svg');
  s.setAttribute('viewBox', '0 0 24 24');
  s.setAttribute('class', 'ic');
  s.setAttribute('aria-hidden', 'true');
  for (const d of (ICONS[name] || '').split('|')) {
    const p = document.createElementNS(NS, 'path');
    p.setAttribute('d', d);
    s.appendChild(p);
  }
  return s;
}
const pad2 = n => String(n).padStart(2, '0');
const fmtTime = ts => { const d = new Date(ts * 1000); return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); };
const fmtDate = ts => { const d = new Date(ts * 1000); return pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + '.' + d.getFullYear(); };
const fmtDT = ts => fmtDate(ts) + ' ' + fmtTime(ts);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const median = arr => { if (!arr.length) return null; const s = arr.slice().sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const POSE_NAMES = { frontal: 'Анфас', left45: 'Поворот влево 45', right45: 'Поворот вправо 45', left90: 'Левый профиль 90', right90: 'Правый профиль 90', free: 'Свободный' };

/* ===== Единое состояние с подпиской ===== */
const State = {
  v: { role: null, view: '', status: 'planned', rtc: 'idle', quality: null, net: true, faceConsent: false, faceMode: 'off', peerHere: false },
  subs: [],
  set(p) {
    const old = Object.assign({}, this.v);
    Object.assign(this.v, p);
    for (const f of this.subs) { try { f(this.v, p, old); } catch (e) { console.error(e); } }
  },
  sub(f) { this.subs.push(f); }
};
const Cfg = { center: 'Медицинский центр', mediapipe: false, auth: true, secure: false };

/* ===== Уведомления ===== */
const Toast = {
  show(text, kind, ms) {
    const box = $('#toasts');
    const t = h('div', { class: 'toast' + (kind === 'err' ? ' err' : ''), role: 'status', text });
    box.append(t);
    requestAnimationFrame(() => t.classList.add('on'));
    setTimeout(() => { t.classList.remove('on'); setTimeout(() => t.remove(), 250); }, ms || 3500);
  },
  err(e) { this.show(typeof e === 'string' ? e : (e && e.message) || 'Ошибка', 'err', 5000); }
};

/* ===== Общий rAF-цикл ===== */
const Loop = {
  fns: new Set(), running: false,
  add(f) { this.fns.add(f); this.start(); return f; },
  del(f) { this.fns.delete(f); },
  start() {
    if (this.running) return;
    this.running = true;
    const tick = t => {
      if (!this.running) return;
      for (const f of this.fns) { try { f(t); } catch (e) { console.error(e); } }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  },
  stop() { this.running = false; this.fns.clear(); }
};

/* ===== Ui ===== */
const Ui = {
  sheets: [],
  sheet(opt) {
    const root = $('#sheets');
    const scrim = h('div', { class: 'scrim' });
    const body = h('div', { class: 'stack' });
    const sheet = h('div', { class: 'sheet' + (opt.wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-label': opt.title || 'Диалог' });
    const api = { el: body, close: () => { Ui.sheets = Ui.sheets.filter(s => s !== api); scrim.classList.remove('on'); sheet.classList.remove('on'); setTimeout(() => { scrim.remove(); sheet.remove(); if (!Ui.sheets.length) root.style.pointerEvents = 'none'; }, 220); if (opt.onClose) opt.onClose(); } };
    if (opt.title) sheet.append(h('div', { class: 'rowb', style: 'margin-bottom:12px' }, h('h2', { text: opt.title }),
      opt.noClose ? null : h('button', { class: 'ibtn', 'aria-label': 'Закрыть', onclick: api.close }, icon('close'))));
    sheet.append(body);
    if (opt.actions && opt.actions.length) {
      sheet.append(h('div', { class: 'row wrap', style: 'margin-top:16px;justify-content:flex-end' },
        opt.actions.map(a => h('button', { class: 'btn ' + (a.kind || 'sec'), onclick: async () => { if (a.onClick) { const r = await a.onClick(api); if (r === false) return; } if (!a.keep) api.close(); } }, a.label))));
    }
    if (!opt.noClose) scrim.addEventListener('click', api.close);
    root.style.pointerEvents = 'auto';
    root.append(scrim, sheet);
    requestAnimationFrame(() => { scrim.classList.add('on'); sheet.classList.add('on'); const f = sheet.querySelector('input,select,textarea,button.pri,button'); if (f) f.focus(); });
    this.sheets.push(api);
    return api;
  },
  closeAll() { for (const s of this.sheets.slice()) s.close(); },
  confirm(title, text, okLabel, danger) {
    return new Promise(res => {
      let done = false;
      const fin = v => { if (!done) { done = true; res(v); } };
      const s = this.sheet({
        title, onClose: () => fin(false),
        actions: [{ label: 'Отмена', kind: 'sec', onClick: () => fin(false) }, { label: okLabel || 'Подтвердить', kind: danger ? 'dng' : 'pri', onClick: () => fin(true) }]
      });
      s.el.append(h('p', { text, style: 'margin:0' }));
    });
  },
  field(label, input, id) {
    id = id || 'f' + Math.random().toString(36).slice(2, 8);
    input.id = id;
    return h('div', { class: 'field' }, h('label', { for: id, text: label }), input);
  },
  input(type, ph, val) { const i = h('input', { class: 'inp', type: type || 'text', placeholder: ph || '' }); if (val != null) i.value = val; return i; },
  statusChip(s) {
    const m = { planned: ['Запланирован', 'chip'], waiting: ['Пациент ожидает', 'chip o'], in_progress: ['Идёт', 'chip g'], completed: ['Завершён', 'chip'] };
    const x = m[s] || m.planned;
    return h('span', { class: x[1], text: x[0] });
  },
  async copy(text, msg) {
    try { await navigator.clipboard.writeText(text); Toast.show(msg || 'Скопировано'); }
    catch (e) {
      const t = h('textarea', { style: 'position:fixed;opacity:0' }); t.value = text; document.body.append(t); t.select();
      try { document.execCommand('copy'); Toast.show(msg || 'Скопировано'); } catch (e2) { Toast.err('Не удалось скопировать'); }
      t.remove();
    }
  },
  segmented(items, cur, onChange, label) {
    const box = h('div', { class: 'seg', role: 'group', 'aria-label': label || '' });
    const btns = items.map(([val, text]) => h('button', { 'aria-pressed': String(val === cur), onclick: () => { cur = val; btns.forEach((b, i) => b.setAttribute('aria-pressed', String(items[i][0] === cur))); onChange(val); } }, text));
    box.append(...btns);
    return box;
  },
  download(name, text, mime) {
    const a = h('a', { href: URL.createObjectURL(new Blob([text], { type: mime || 'application/json' })), download: name });
    document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  },
  /* «Крупно и контрастно» для слабовидящих: крупнее текст и элементы управления (big), темнее текст и рамки (hc).
     Выбор запоминается на этом устройстве */
  big() { const on = !this.isBig(); document.documentElement.classList.toggle('big', on); document.documentElement.classList.toggle('hc', on); try { localStorage.setItem('mars_big', on ? '1' : '0'); } catch (e) { /* без хранилища */ } },
  isBig() { return document.documentElement.classList.contains('big'); },
  /* заметная кнопка режима на первом экране пациента: до согласия и проверки камеры */
  bigToggle() {
    const b = h('button', { class: 'btn sec bigtoggle', 'aria-pressed': String(this.isBig()) }, icon('text'), h('span'));
    const paint = () => { b.setAttribute('aria-pressed', String(this.isBig())); b.lastChild.textContent = this.isBig() ? 'Обычный вид' : 'Крупно и контрастно'; };
    b.onclick = () => { this.big(); paint(); Header.update(); };
    paint();
    return b;
  },
  sound() {
    try {
      const AC = window.AudioContext || window.webkitAudioContext; if (!AC) return;
      Ui.ac = Ui.ac || new AC();
      const o = Ui.ac.createOscillator(), g = Ui.ac.createGain();
      o.type = 'sine'; o.frequency.value = 660; g.gain.value = 0.04;
      o.connect(g); g.connect(Ui.ac.destination); o.start(); o.stop(Ui.ac.currentTime + 0.18);
    } catch (e) { /* звук необязателен */ }
  },
  closeAudio() { try { if (Ui.ac) Ui.ac.close(); } catch (e) { /* ignore */ } Ui.ac = null; }
};

/* ===== Api ===== */
class ApiErr extends Error { constructor(status, code, msg) { super(msg); this.status = status; this.code = code; } }
const Api = {
  room: null, token: null,
  async req(method, path, opt) {
    opt = opt || {};
    const hd = Object.assign({}, opt.headers || {});
    let body = opt.raw;
    if (opt.json !== undefined) { hd['Content-Type'] = 'application/json'; body = JSON.stringify(opt.json); }
    const tok = opt.token === false ? null : (opt.token || this.token);
    if (tok && path.startsWith('/rooms')) hd['Authorization'] = 'Bearer ' + tok;
    let res;
    try { res = await fetch(path, { method, headers: hd, body, signal: opt.signal, credentials: 'same-origin', keepalive: !!opt.keepalive }); }
    catch (e) { if (e && e.name === 'AbortError') throw e; throw new ApiErr(0, 'network', 'Нет связи с сервером'); }
    if (opt.blob && res.ok) return res.blob();
    let data = null; try { data = await res.json(); } catch (e) { /* не JSON */ }
    if (!res.ok) throw new ApiErr(res.status, (data && data.error) || 'error', (data && data.message) || ('Ошибка ' + res.status));
    return data;
  },
  get(p, o) { return this.req('GET', p, o); },
  post(p, json, o) { return this.req('POST', p, Object.assign({}, o, { json })); },
  ev(type, data) { return this.post('/rooms/' + this.room + '/events', { type, data: data || {} }); },
  /* событие при закрытии вкладки */
  bye() { if (this.room && this.token) { try { this.req('POST', '/rooms/' + this.room + '/events', { json: { type: 'presence', data: { state: 'left' } }, keepalive: true }).catch(() => { }); } catch (e) { /* ignore */ } } }
};
