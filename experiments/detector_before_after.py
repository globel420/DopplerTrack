import numpy as np, inspect
from app import analysis as an

SR = 22050


def pass_tone(n, onset, f0, v=100, d=3500, span=240, c=343.0):
    t = np.arange(n) / SR
    x = v * (t - (onset + span / 2))
    f = f0 * c / (c + v * x / np.sqrt(d * d + x * x))
    ph = np.cumsum(f) / SR * 2 * np.pi
    return t, f, ph


def deep(a):
    fr, t, db = an._spectrum(
        a.astype(np.float32), sample_rate=SR, nperseg=4096, overlap_fraction=0.5
    )
    tr = an._ridge_candidates(
        fr, t, db, prominence_db=7.0, max_tracks=64, include_measurements=True
    )
    keep = [
        x
        for x in tr
        if x["duration_seconds"] >= 2
        and abs(x["end_hz"] - x["start_hz"]) >= 4
        and x["fit_r_squared"] >= 0.55
    ]
    return tr, keep


def quick(a):
    fr, t, db = an._spectrum(
        a.astype(np.float32), sample_rate=SR, nperseg=2048, overlap_fraction=0.5
    )
    tr = an._ridge_candidates(fr, t, db, prominence_db=9.0)
    return [
        x
        for x in tr
        if x["duration_seconds"] >= 3
        and abs(x["end_hz"] - x["start_hz"]) >= 15
        and x["fit_r_squared"] >= 0.72
    ]


def span_true(tr, t, f):
    b = 0
    e = []
    for k in tr:
        m = np.array(k.get("measurements", []))
        if len(m) < 12:
            continue
        tru = np.interp(m[:, 0], t, f)
        if np.median(np.abs(m[:, 1] - tru)) < 6:
            if k["duration_seconds"] > b:
                b = k["duration_seconds"]
                e = np.abs(m[:, 1] - tru)
    return b, (float(np.median(e)) if len(e) else None)


print("A. Tone starting partway into noisy audio (true pass is 240 s)")
for onset in (0, 2, 30, 120):
    rng = np.random.default_rng(5)
    n = int((onset + 240) * SR)
    t, f, ph = pass_tone(n, onset, 400)
    tone = 0.05 * np.sin(ph)
    tone[t < onset] = 0
    tr, _ = deep(tone + rng.standard_normal(n) * 0.02)
    print(f"  onset {onset:>3}s: true-curve span {span_true(tr, t, f)[0]:6.1f}s")
print("B. Low engine tones (true pass is 240 s)")
for f0 in (70, 90, 134, 160, 400):
    rng = np.random.default_rng(6)
    n = 240 * SR
    t, f, ph = pass_tone(n, 0, f0)
    a = 0.1 * np.sin(ph) + 0.01 * rng.standard_normal(n)
    tr, _ = deep(a)
    s, e = span_true(tr, t, f)
    print(
        f"  f0={f0:>3} Hz ({f.max():.0f}->{f.min():.0f}): span {s:6.1f}s, median error {e if e is None else round(e, 2)} Hz"
    )
print("C. Frequency precision at 300 Hz")
rng = np.random.default_rng(7)
n = 240 * SR
t, f, ph = pass_tone(n, 0, 300)
tr, _ = deep(0.1 * np.sin(ph) + 0.002 * rng.standard_normal(n))
print("  median error Hz", round(span_true(tr, t, f)[1], 3))
print("D. False curves in audio with no aircraft (5 x 120 s each)")


def report(label, gen):
    dq = []
    dd = []
    sp = []
    for seed in range(5):
        a = gen(np.random.default_rng(100 + seed))
        tr, keep = deep(a)
        dq.append(len(quick(a)))
        dd.append(len(keep))
        sp.append(max([k["duration_seconds"] for k in keep], default=0))
    print(
        f"  {label:<28} quick-check moving tones {dq}  deep curves {dd}  longest false curve {max(sp):.1f}s"
    )


N = 120 * SR
tt = np.arange(N) / SR
report("white noise", lambda r: r.standard_normal(N) * 0.02)


def pink(r):
    w = r.standard_normal(N)
    F = np.fft.rfft(w)
    fq = np.fft.rfftfreq(N, 1 / SR)
    F[1:] /= np.sqrt(fq[1:])
    a = np.fft.irfft(F, N)
    return a / np.std(a) * 0.02


report("pink noise", pink)
report(
    "wind-like low rumble",
    lambda r: (lambda a: a / np.std(a) * 0.05)(
        np.fft.irfft(
            np.fft.rfft(r.standard_normal(N))
            / (1 + np.fft.rfftfreq(N, 1 / SR) / 30) ** 2,
            N,
        )
    ),
)
report(
    "60 Hz hum + harmonics",
    lambda r: (
        sum(0.03 / k * np.sin(2 * np.pi * 60 * k * tt) for k in (1, 2, 3))
        + r.standard_normal(N) * 0.01
    ),
)
