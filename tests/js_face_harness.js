// Считает метрики кодом браузера (gateway/src/d_face1.js, d_face3.js) для tests/test_vision.py.
// Вход: JSON в stdin, выход: JSON в stdout.
const fs = require('fs'), path = require('path'), vm = require('vm');
const src = path.join(__dirname, '..', 'gateway', 'src');
const c1 = fs.readFileSync(path.join(src, 'c_js1.js'), 'utf8');
const helpers = ['const clamp', 'const median'].map(k => c1.split('\n').find(l => l.startsWith(k))).join('\n');
const ctx = { console, performance, Math, JSON, Object, Array, Date, Promise, Set, Map, setTimeout, clearTimeout, setInterval, clearInterval };
vm.createContext(ctx);
vm.runInContext(helpers + '\n' + fs.readFileSync(path.join(src, 'd_face1.js'), 'utf8') + '\n' + fs.readFileSync(path.join(src, 'd_face3.js'), 'utf8') + '\nthis.FaceMesh = FaceMesh; this.LM = LM;', ctx);
const F = ctx.FaceMesh;

const inp = JSON.parse(fs.readFileSync(0, 'utf8'));
const cur = f => ({ lm: f.lm.map(p => ({ x: p[0], y: p[1], z: p[2] })), mat: f.mat, vw: f.vw, vh: f.vh, multi: !!f.multi,
  bs: Object.entries(f.bs || {}).map(([categoryName, score]) => ({ categoryName, score })) });
const out = { sym: [], mob: [], cmp: [], iris: [], card: [] };

for (const c of inp.sym) {
  F.tol = c.tol;
  const S = [];
  for (const f of c.frames.map(cur)) { if (!F.Pose.inTol(F.Pose.euler(f), c.tol)) continue; const v = F.Symmetry.one(f); if (v) S.push(v); }
  out.sym.push(S.length >= 12 ? F.Symmetry.build(S, c.cal) : null);
}
for (const c of inp.mob) {
  // тот же цикл, что в Mobility.runTask, без таймера
  const task = F.Mobility.TASKS.find(t => t.id === c.task), tol = Math.max(c.tol * 2, 10);
  let mr = 0, ml = 0, ms = 0, n = 0;
  for (const f of c.frames.map(cur)) {
    if (!F.Pose.inTol(F.Pose.euler(f), tol)) continue;
    n++;
    if (task.single) ms = Math.max(ms, F.Mobility.val(f, task.single)); else { mr = Math.max(mr, F.Mobility.val(f, task.r)); ml = Math.max(ml, F.Mobility.val(f, task.l)); }
  }
  if (n < 10) { out.mob.push(null); continue; }
  const o = { id: task.id, label: task.label };
  if (task.single) o.single = ms; else { o.right = mr; o.left = ml; o.ratio = Math.max(mr, ml) > 0.05 ? Math.min(mr, ml) / Math.max(mr, ml) : null; }
  out.mob.push(o);
}
for (const c of inp.cmp) { F.tol = c.tol; out.cmp.push(F.Compare.calc(cur(c.A), cur(c.B), c.pose, c.tol, c.cal)); }
for (const fr of inp.iris) {
  // как калибровка в браузере (d_face2.js): только кадры с позой в допуске 5 градусов
  const S = fr.map(cur).filter(f => F.Pose.inTol(F.Pose.euler(f), 5)).map(f => ({ iris: F.Calibrate.irisPx(f), ipd: F.Calibrate.ipdPx(f) })).filter(s => s.iris && s.ipd);
  out.iris.push(S.length >= 12 ? F.Calibrate.fromIris(S) : null);
}
for (const c of inp.card) out.card.push(F.Calibrate.fromCard(c.pts, c.ipd));
process.stdout.write(JSON.stringify(out));
