import numpy as np, time, math, json
from app.analysis import _spectrum, _ridge_candidates

SR = 22050
rng = np.random.default_rng(7)


def flyover(dur, tc, f0, v, d, c=343.0):
    t = np.arange(int(dur * SR)) / SR
    # received freq, ignoring retardation (shape test only)
    x = v * (t - tc)
    ur = v * x / np.sqrt(d * d + x * x)
    f = f0 * c / (c + ur)
    return np.cumsum(f) / SR * 2 * np.pi, f


def detect(a, **kw):
    fr, t, db = _spectrum(
        a.astype(np.float32),
        sample_rate=SR,
        nperseg=kw.pop("nperseg", 4096),
        overlap_fraction=0.5,
    )
    tr = _ridge_candidates(
        fr, t, db, prominence_db=7.0, max_tracks=64, include_measurements=True, **kw
    )
    keep = [
        x
        for x in tr
        if x["duration_seconds"] >= 2
        and abs(x["end_hz"] - x["start_hz"]) >= 4
        and x["fit_r_squared"] >= 0.55
    ]
    return tr, keep


# E1a: frequency floor. Clean-ish propeller pass at the published reference tone (~134 Hz), and same at 400 Hz.
for f0 in (90, 134, 145, 160, 400):
    ph, f = flyover(240, 120, f0, 100, 3500)
    a = 0.1 * np.sin(ph) + 0.01 * rng.standard_normal(ph.size)
    tr, keep = detect(a)
    print(
        f"E1a f0={f0}Hz received {f.max():.0f}->{f.min():.0f}Hz: raw_tracks={len(tr)} accepted={len(keep)} best_span={max([k['duration_seconds'] for k in keep], default=0)}s"
    )
# E1b: track budget exhaustion. noise first, pass later
dur = 420
ph, f = flyover(dur, 330, 400, 100, 3500)
tone = np.zeros(ph.size)
tone[int(240 * SR) :] = 0.05 * np.sin(ph[int(240 * SR) :])
noise = rng.standard_normal(ph.size) * 0.02
a = noise + tone
tr, keep = detect(a)
late = [k for k in keep if k["start_seconds"] >= 230]
print(
    "E1b noisy clip, pass at 240-420s: raw tracks",
    len(tr),
    "accepted",
    len(keep),
    "accepted after 230s",
    len(late),
)
fr, t, db = _spectrum(
    a[int(235 * SR) :].astype(np.float32),
    sample_rate=SR,
    nperseg=4096,
    overlap_fraction=0.5,
)
tr2 = _ridge_candidates(
    fr, t, db, prominence_db=7.0, max_tracks=64, include_measurements=True
)
k2 = [
    x
    for x in tr2
    if x["duration_seconds"] >= 2
    and abs(x["end_hz"] - x["start_hz"]) >= 4
    and x["fit_r_squared"] >= 0.55
]
print(
    "   same pass, audio trimmed to start at 235s: raw",
    len(tr2),
    "accepted",
    len(k2),
    "best_span",
    max([k["duration_seconds"] for k in k2], default=0),
)
# E1c quantisation error on measured ridge
ph, f = flyover(240, 120, 300, 100, 3500)
a = 0.1 * np.sin(ph) + 0.002 * rng.standard_normal(ph.size)
tr, keep = detect(a)
k = max(keep, key=lambda x: x["duration_seconds"])
m = np.array(k["measurements"])
truth = np.interp(m[:, 0], np.arange(f.size) / SR, f)
print(
    f"E1c f0=300 bin-quantised error: median |e|={np.median(np.abs(m[:, 1] - truth)):.2f}Hz  = {100 * np.median(np.abs(m[:, 1] - truth)) / 300:.2f}% ; span {k['duration_seconds']}s, R2={k['fit_r_squared']}"
)
# E1d: r2 of real sigmoid passes against linear filter
for v, d, T in [(70, 1500, 240), (100, 3500, 240), (100, 800, 300), (60, 600, 300)]:
    tt = np.linspace(-T / 2, T / 2, 400)
    x = v * tt
    ur = v * x / np.sqrt(d * d + x * x)
    ff = 300 * 343 / (343 + ur)
    A = np.polyfit(tt, ff, 1)
    r2 = 1 - np.sum((ff - np.polyval(A, tt)) ** 2) / np.sum((ff - ff.mean()) ** 2)
    print(f"E1d v={v} d={d} span={T}s linear-fit R2={r2:.3f}")
# E1e: sensitivity vs SNR with app window vs long window
for snr_db in (6, 0, -6, -12, -18, -24, -30):
    res = []
    for nper, name in ((4096, "app 0.19s"), (22050, "1.0s")):
        ph, f = flyover(240, 120, 300, 100, 3500)
        amp = 0.1
        sigma = amp / np.sqrt(2) / 10 ** (snr_db / 20)
        a = amp * np.sin(ph) + sigma * np.random.default_rng(3).standard_normal(ph.size)
        fr, t, db = _spectrum(
            a.astype(np.float32), sample_rate=SR, nperseg=nper, overlap_fraction=0.5
        )
        tr = _ridge_candidates(
            fr, t, db, prominence_db=7.0, max_tracks=64, include_measurements=True
        )
        best = 0
        for x in tr:
            m = np.array(x.get("measurements", []))
            if len(m) >= 12:
                tru = np.interp(m[:, 0], np.arange(f.size) / SR, f)
                if np.median(np.abs(m[:, 1] - tru)) < 6:
                    best = max(best, x["duration_seconds"])
        res.append(f"{name}: true-ridge span {best:.0f}s")
    print(f"E1e broadband SNR {snr_db:>4} dB  " + " | ".join(res))
