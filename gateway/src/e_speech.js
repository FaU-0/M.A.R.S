
/* ===== Расшифровка речи: каждая сторона записывает свой микрофон кусками по 10 с ===== */
/* Кусок записывается отдельным MediaRecorder: так каждый файл читается сам по себе, без заголовка первого куска.
   Разделение «врач и пациент» получается само: у каждого свой микрофон. */
const Speech = {
  CHUNK: 10000, MAXQ: 90, on: false, consent: false, rec: null, seq: 0, sid: '', q: [], sending: false, tm: null, chip: null, poll: null, st: null,
  mime() { for (const m of ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4']) if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m; return ''; },
  mount(vtop) {
    this.chip = h('button', { class: 'chip o hidden', style: 'pointer-events:auto;border:0;cursor:pointer', 'aria-live': 'polite', onclick: () => this.chipClick() });
    vtop.insertBefore(this.chip, vtop.children[1] || null);
    const sp = (App.proto && App.proto.speech) || {};
    this.consent = !!sp.consent;
    this.set(!!(sp.consent && sp.active) && State.v.status !== 'completed');
  },
  /* включить или выключить запись у себя */
  set(on) {
    if (on === this.on) return this.paint();
    this.on = on;
    if (on) {
      if (!this.mime() || !Media.stream || !Media.stream.getAudioTracks().length) { this.on = false; Toast.show(T('Запись речи недоступна в этом браузере')); return this.paint(); }
      this.sid = Remote_key(); this.seq = 0; this.t00 = performance.now(); this.cycle();
      if (State.v.role === 'doctor') { clearInterval(this.poll); this.poll = setInterval(() => this.refresh(), 10000); this.refresh(); }
    } else {
      clearTimeout(this.tm);
      if (this.rec && this.rec.state !== 'inactive') this.rec.stop();  // последний неполный кусок тоже уйдёт
      this.rec = null;
    }
    this.paint();
  },
  cycle() {
    if (!this.on) return;
    const tr = Media.stream && Media.stream.getAudioTracks()[0];
    if (!tr) { this.set(false); return; }
    let rec;
    try { rec = new MediaRecorder(new MediaStream([tr]), { mimeType: this.mime(), audioBitsPerSecond: 32000 }); }
    catch (e) { console.warn('MediaRecorder', e); this.on = false; this.paint(); Toast.show(T('Не удалось начать запись речи')); return; }
    const parts = [], t0 = performance.now(), t00 = this.t00, sid = this.sid, seq = this.seq++;
    rec.ondataavailable = e => { if (e.data && e.data.size) parts.push(e.data); };
    rec.onstop = () => {
      const dur = Math.round(performance.now() - t0);
      if (parts.length && dur > 500) this.push({ blob: new Blob(parts, { type: rec.mimeType || this.mime() }), dur, seq, off: Math.round(t0 - t00), key: sid + '-' + seq, tries: 0 });
      if (this.on && this.rec === rec) this.cycle();
    };
    this.rec = rec; rec.start();
    this.tm = setTimeout(() => { if (rec.state !== 'inactive') rec.stop(); }, this.CHUNK);
  },
  push(item) {
    this.q.push(item);
    if (this.q.length > this.MAXQ) this.q.shift();  // нет связи больше 15 минут: старые куски не держим в памяти
    this.send();
  },
  /* отправка по очереди; при сбое сети повтор с паузой. Сервер хранит кусок, даже если speech недоступен */
  async send() {
    if (this.sending || !this.q.length) return;
    this.sending = true;
    while (this.q.length) {
      const it = this.q[0];
      try {
        await Api.req('POST', '/rooms/' + Api.room + '/audio', { raw: it.blob, headers: { 'Content-Type': it.blob.type.split(';')[0], 'X-Chunk-Key': it.key, 'X-Seq': String(it.seq), 'X-Duration-Ms': String(it.dur), 'X-Offset-Ms': String(it.off) } });
        this.q.shift();
      } catch (e) {
        if (e && (e.status === 403 || e.status === 410 || e.status === 415 || e.status === 400)) { this.q.shift(); if (e.status === 403) this.set(false); continue; }
        it.tries++; await new Promise(r => setTimeout(r, Math.min(30000, 1000 * 2 ** Math.min(it.tries, 5))));
      }
    }
    this.sending = false;
    this.paint();
  },
  async refresh() {
    if (State.v.role !== 'doctor' || !Api.room) return;
    try { this.st = await Api.get('/rooms/' + Api.room + '/transcript/status'); } catch (e) { return; }
    if (!this.on && !this.st.queued) { clearInterval(this.poll); this.poll = null; }
    this.paint();
  },
  paint() {
    const c = this.chip; if (!c) return;
    const st = this.st, doc = State.v.role === 'doctor';
    let txt = '';
    if (this.on) txt = T('● Идёт расшифровка речи');
    else if (doc && st && st.queued) txt = T('Расшифровка: в очереди ') + st.queued;
    if (txt && doc && st && st.chunks) txt += ' · ' + st.done + T(' из ') + st.chunks + (st.service_up === false ? T(' · сервис недоступен, куски сохранены') : '');
    if (this.on && this.q.length > 1) txt += T(' · не отправлено ') + this.q.length;
    c.textContent = txt; c.classList.toggle('hidden', !txt);
    c.title = doc ? T('Остановить расшифровку') : T('Остановить расшифровку (отозвать согласие)');
    c.setAttribute('aria-label', txt + '. ' + c.title);
  },
  async chipClick() {
    if (!this.on) return;
    if (State.v.role === 'doctor') return this.stopByDoctor();
    if (!(await Ui.confirm(T('Остановить расшифровку?'), T('Запись вашей речи прекратится. Уже расшифрованный текст останется у врача.'), T('Остановить'), true))) return;
    this.answer(false);
  },
  /* врач: запросить согласие или остановить */
  async request() {
    if (this.on) return this.stopByDoctor();
    try { await Api.ev('control', { action: 'speech_request' }); Toast.show(T('Пациенту отправлен запрос на согласие')); } catch (e) { Toast.err(e); }
  },
  async stopByDoctor() { try { await Api.ev('control', { action: 'speech_stop' }); } catch (e) { Toast.err(e); } },
  /* пациент: лист согласия */
  ask() {
    const s = Ui.sheet({
      title: T('Согласие на расшифровку речи'),
      actions: [{ label: T('Не согласен(на)'), kind: 'sec', onClick: () => this.answer(false) }, { label: T('Согласен(на)'), kind: 'pri', onClick: () => this.answer(true) }]
    });
    s.el.append(h('p', { style: 'margin:0', text: T('Врач просит включить расшифровку консультации. Что происходит: ваш микрофон записывается кусками по 10 секунд и отправляется на сервер медицинского центра, где речь переводится в текст.') }),
      h('p', { style: 'margin:0', text: T('Аудиозапись удаляется сразу после расшифровки. Текст видит только врач; в отчёт он попадёт только без ваших личных данных.') }),
      h('p', { class: 'note', style: 'margin:0', text: T('Согласие можно отозвать в любой момент: нажмите на индикатор «Идёт расшифровка речи» вверху экрана.') }));
  },
  async answer(g) { try { await Api.ev('control', { action: 'speech_consent', granted: g }); } catch (e) { Toast.err(e); } },
  /* события control от сервера */
  control(d) {
    if (d.action === 'speech_request') { if (State.v.role === 'patient') this.ask(); return; }
    if (d.action === 'speech_consent') {
      this.consent = !!d.granted; this.set(!!d.granted);
      if (State.v.role === 'doctor') Toast.show(d.granted ? T('Пациент согласился на расшифровку речи') : T('Пациент отказался от расшифровки речи'));
      return;
    }
    if (d.action === 'speech_stop') { this.set(false); if (State.v.role === 'patient') Toast.show(T('Врач остановил расшифровку речи')); }
  },
  stop() { this.set(false); }
};
const Remote_key = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2, 12));
