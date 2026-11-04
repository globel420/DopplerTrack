import numpy as np
from app.analysis import _spectrum, _ridge_candidates

SR = 22050


def run(onset, max_tracks=64, dur=240, snr_sigma=0.02, amp=0.05, seed=5):
    rng = np.random.default_rng(seed)
    n = int(dur * SR)
    t = np.arange(n) / SR
    v, d, f0, tc, c = 100, 3500, 400, onset + 90, 343.0
    x = v * (t - tc)
    f = f0 * c / (c + v * x / np.sqrt(d * d + x * x))
    ph = np.cumsum(f) / SR * 2 * np.pi
    tone = amp * np.sin(ph)
    tone[t < onset] = 0
    a = tone + rng.standard_normal(n) * snr_sigma
    fr, tt, db = _spectrum(
        a.astype(np.float32), sample_rate=SR, nperseg=4096, overlap_fraction=0.5
    )
    tr = _ridge_candidates(
        fr, tt, db, prominence_db=7.0, max_tracks=max_tracks, include_measurements=True
    )
    best = 0
    for k in tr:
        m = np.array(k["measurements"])
        tru = np.interp(m[:, 0], t, f)
        if np.median(np.abs(m[:, 1] - tru)) < 6 and m[0, 0] >= onset - 1:
            best = max(best, k["duration_seconds"])
    return best, len(tr)


print("true pass lasts until the clip ends; true-ridge span found (s), tracks kept")
for onset in (0, 0.25, 0.5, 1, 2, 5, 30, 120):
    b, n = run(onset)
    b2, n2 = run(onset, max_tracks=100000)
    print(
        f"onset {onset:>6}s  max_tracks=64: span {b:6.1f}s kept {n:2d} | budget removed: span {b2:6.1f}s kept {n2:2d}"
    )
