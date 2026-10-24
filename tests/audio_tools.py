# -*- coding: utf-8 -*-
"""Подготовка тестового звука: синтез русской речи голосом macOS (say -v Milena) и нарезка
на куски webm/opus по 10 секунд, как их записывает браузер (MediaRecorder).
Нужен PyAV: запускается интерпретатором speech/.venv.

    speech/.venv/bin/python tests/audio_tools.py <текст> <папка>
"""
import fractions
import subprocess
import sys
from pathlib import Path

import av
import numpy as np

CHUNK_S = 10
RATE = 48000


def synth(text, path):
    subprocess.run(["say", "-v", "Milena", "-o", str(path), text], check=True)


def read_mono(path):
    with av.open(str(path)) as f:
        rs = av.AudioResampler(format="flt", layout="mono", rate=RATE)
        out = []
        for fr in f.decode(audio=0):
            for r in rs.resample(fr):
                out.append(r.to_ndarray()[0])
        for r in rs.resample(None):
            out.append(r.to_ndarray()[0])
    return np.concatenate(out)


def write_webm(samples, path):
    with av.open(str(path), "w", format="webm") as f:
        st = f.add_stream("libopus", rate=RATE, layout="mono")
        pts = 0
        for i in range(0, len(samples), 960):
            part = samples[i:i + 960]
            if len(part) < 960:
                part = np.pad(part, (0, 960 - len(part)))
            fr = av.AudioFrame.from_ndarray(part.reshape(1, -1).astype(np.float32), format="flt", layout="mono")
            fr.sample_rate, fr.pts, fr.time_base = RATE, pts, fractions.Fraction(1, RATE)
            pts += 960
            for pk in st.encode(fr):
                f.mux(pk)
        for pk in st.encode(None):
            f.mux(pk)


def chunks(text, out_dir, pause_s=0.0):
    """Синтезирует текст и режет его на куски по 10 с. Возвращает [(путь, длительность, с)]."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    aiff = out_dir / "speech.aiff"
    synth(text, aiff)
    s = read_mono(aiff)
    if pause_s:
        s = np.concatenate([s, np.zeros(int(pause_s * RATE), np.float32)])
    res = []
    n = CHUNK_S * RATE
    for k, i in enumerate(range(0, len(s), n)):
        p = out_dir / ("chunk%02d.webm" % k)
        write_webm(s[i:i + n], p)
        res.append((str(p), round(len(s[i:i + n]) / RATE, 2)))
    return res


def silence(path, secs=10):
    write_webm(np.zeros(int(secs * RATE), np.float32), path)


if __name__ == "__main__":
    for p, d in chunks(sys.argv[1], sys.argv[2]):
        print(p, d)
