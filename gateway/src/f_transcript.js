
/* ===== Вкладка «Расшифровка» (только врач): обезличенный текст, правки замен, подтверждение для отчёта ===== */
const Transcript = {
  el: null, d: null, st: null, timer: null, busy: false,
  ROLE: { doctor: T('Врач'), patient: T('Пациент') },
  TYPES: [['NAME', T('Имя')], ['ORG', T('Организация')], ['CITY', T('Место')], ['SCHOOL', T('Учебное заведение')], ['OTHER', T('Другое')]],
  build() { this.el = h('div', { class: 'stack' }); this.render(); return this.el; },
  async load() {
    if (!Api.room || State.v.role !== 'doctor') return;
    try { const r = await Api.get('/rooms/' + Api.room + '/transcript'); this.d = r.deid; this.st = r.status; }
    catch (e) { if (e.status !== 403) Toast.err(e); return; }
    this.render();
    clearTimeout(this.timer);
    if (this.d && this.d.status === 'queued' && CallUi.curTab === 'tr') this.timer = setTimeout(() => this.load(), 4000);
  },
  async edit(body) {
    if (this.busy) return; this.busy = true;
    try { const r = await Api.post('/rooms/' + Api.room + '/transcript/edit', body); this.d = r.deid; this.render(); }
    catch (e) { Toast.err(e); }
    this.busy = false;
  },
  async redo() {
    if (this.d && this.d.status === 'done' && !(await Ui.confirm(T('Обезличить повторно?'), T('Расшифровка будет обезличена заново. Ваши ручные правки и подтверждение будут сброшены.'), T('Обезличить')))) return;
    try { const r = await Api.post('/rooms/' + Api.room + '/transcript/deidentify', {}); this.d = r.deid; this.render(); this.load(); }
    catch (e) { Toast.err(e); }
  },
  async original() {
    if (!(await Ui.confirm(T('Показать оригинал?'), T('Исходная расшифровка содержит личные данные пациента. Просмотр записывается в журнал аудита.'), T('Показать')))) return;
    let r; try { r = await Api.get('/rooms/' + Api.room + '/transcript/original'); } catch (e) { Toast.err(e); return; }
    const s = Ui.sheet({ title: T('Исходная расшифровка'), wide: true });
    s.el.append(h('div', { class: 'banner warn', style: 'border-radius:var(--r1)', text: T('Содержит личные данные. Не копируйте этот текст в отчёт.') }), this.list(r.segments, false));
  },
  addSheet() {
    const sel = (window.getSelection && String(window.getSelection())).trim();
    const inp = h('input', { class: 'inp', value: sel, maxlength: 200 });
    const typ = h('select', { class: 'inp' }, this.TYPES.map(([v, l]) => h('option', { value: v, text: l })));
    const s = Ui.sheet({ title: T('Добавить замену'), actions: [{ label: T('Отмена'), kind: 'sec' }, { label: T('Заменить везде'), kind: 'pri', onClick: () => { if (inp.value.trim().length < 2) { Toast.show(T('Введите фрагмент')); return false; } this.edit({ op: 'add', text: inp.value.trim(), type: typ.value }); } }] });
    s.el.append(h('p', { class: 'note', style: 'margin:0', text: T('Фрагмент будет скрыт меткой во всей расшифровке. Можно выделить его в тексте до нажатия кнопки.') }), Ui.field(T('Фрагмент'), inp), Ui.field(T('Что это'), typ));
  },
  /* текст с подсвеченными метками; клик по метке предлагает отменить замену */
  textNode(text, editable) {
    const out = [], rx = /\[[A-ZА-ЯЁ_0-9]+\]/g; let pos = 0, m;
    while ((m = rx.exec(text))) {
      if (m.index > pos) out.push(text.slice(pos, m.index));
      const lab = m[0];
      out.push(editable ? h('button', { class: 'chip b', style: 'border:0;cursor:pointer;margin:0 1px', title: T('Отменить замену'), onclick: async () => { if (await Ui.confirm(T('Отменить замену ') + lab + '?', T('Под этой меткой во всей расшифровке будет показан исходный текст. Делайте это, только если это не личные данные (например, название лекарства).'), T('Отменить замену'))) this.edit({ op: 'undo', label: lab }); } }, lab)
        : h('span', { class: 'chip b', style: 'margin:0 1px' }, lab));
      pos = m.index + lab.length;
    }
    if (pos < text.length) out.push(text.slice(pos));
    return out;
  },
  list(segs, editable) {
    const t0 = segs.length ? segs[0].t : 0;
    return h('div', { class: 'stack', style: 'gap:8px' }, segs.map(g => {
      const s = Math.max(0, Math.round((g.t - t0) / 1000));
      return h('div', null, h('div', { class: 'sm mut' }, (this.ROLE[g.role] || g.role) + ' · ' + pad2(Math.floor(s / 60)) + ':' + pad2(s % 60)),
        h('p', { style: 'margin:0;line-height:1.6' }, this.textNode(g.text, editable)));
    }));
  },
  render() {
    const el = this.el; if (!el) return;
    const d = this.d, st = this.st, kids = [];
    if (!d) { el.replaceChildren(h('p', { class: 'mut', text: T('Загрузка...') })); return; }
    const live = st && st.chunks ? T('Кусков записи: ') + st.chunks + T(', расшифровано: ') + st.done + (st.queued ? T(', в очереди: ') + st.queued : '') + '.' : '';
    if (d.status === 'none') kids.push(h('p', { style: 'margin:0', text: T('Обезличенной расшифровки пока нет. Она появится после завершения приёма, если пациент дал согласие на расшифровку речи.') }), live ? h('p', { class: 'note', style: 'margin:0', text: live }) : null,
      Speech.on ? null : h('button', { class: 'btn pri', style: 'align-self:flex-start', onclick: () => Speech.request() }, icon('audio'), T('Запросить согласие на расшифровку')));
    if (d.status === 'queued') kids.push(h('div', { class: 'banner', style: 'border-radius:var(--r1)', role: 'status', text: d.waiting ? T('Сервис обезличивания временно недоступен. Расшифровка сохранена и будет обезличена, когда сервис заработает.') : T('Идёт обезличивание расшифровки. На процессоре это может занять несколько минут.') }));
    if (d.status === 'error') kids.push(h('div', { class: 'banner bad', style: 'border-radius:var(--r1)', text: T('Не удалось обезличить: ') + ((d.error && d.error.message) || T('ошибка')) + '.' }));
    if (st && (st.error || st.cancelled)) kids.push(h('div', { class: 'banner warn', style: 'border-radius:var(--r1)', text: T('Расшифровка неполная: ') + (st.error ? T('не удалось расшифровать кусков записи — ') + st.error : '') + (st.error && st.cancelled ? '; ' : '') + (st.cancelled ? T('удалено после отзыва согласия — ') + st.cancelled : '') + '.' }));
    const btns = [];
    if (d.status === 'done') {
      kids.push(h('div', { class: 'row wrap', style: 'gap:8px' }, d.confirmed ? h('span', { class: 'chip g', text: T('Проверено врачом, идёт в отчёт') }) : h('span', { class: 'chip o', text: T('Не проверено: в отчёт не попадает') }),
        h('span', { class: 'sm mut', text: T('Замен: ') + (d.replacements || []).length + (d.model ? ' · ' + d.model : '') })));
      kids.push(h('p', { class: 'note', style: 'margin:0', text: T('Проверьте текст: нейросеть может пропустить имя или название. Синие метки скрывают личные данные; нажмите на метку, чтобы отменить замену.') }));
      if (d.editable) btns.push(h('button', { class: 'btn sec', onclick: () => this.addSheet() }, T('Добавить замену')));
      btns.push(h('button', { class: 'btn ' + (d.confirmed ? 'sec' : 'pri'), onclick: () => this.edit({ op: d.confirmed ? 'unconfirm' : 'confirm' }) }, d.confirmed ? T('Снять подтверждение') : T('Подтвердить для отчёта')));
    }
    if (d.status !== 'none' || (st && st.chunks)) btns.push(h('button', { class: 'btn sec', onclick: () => this.original() }, T('Показать оригинал')));
    if (d.status === 'done' || d.status === 'error' || (d.status === 'none' && st && st.chunks)) btns.push(h('button', { class: 'btn sec', onclick: () => this.redo() }, d.status === 'none' ? T('Обезличить сейчас') : T('Обезличить повторно')));
    if (btns.length) kids.push(h('div', { class: 'row wrap' }, btns));
    if (d.segments && d.segments.length) kids.push(this.list(d.segments, d.status === 'done' && d.editable));
    else if (d.status === 'done') kids.push(h('p', { class: 'mut', text: T('В записи не найдено речи.') }));
    el.replaceChildren(...kids.filter(Boolean));
  }
};
