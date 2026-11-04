import numpy as np
from app import analysis as an

SR = 22050


def tracks(a, nper=4096, prom=7.0):
    fr, t, db = an._spectrum(
        a.astype(np.float32), sample_rate=SR, nperseg=nper, overlap_fraction=0.5
    )
    return an._ridge_candidates(
        fr, t, db, prominence_db=prom, max_tracks=64, include_measurements=True
    ), float(np.median(np.diff(t)))


def keep(tr):
    return [
        x
        for x in tr
        if x["duration_seconds"] >= 2
        and abs(x["end_hz"] - x["start_hz"]) >= 4
        and x["fit_r_squared"] >= 0.55
    ]


def sup(x, hop):
    return x["observations"] / max(1, x["duration_seconds"] / hop)


N = 120 * SR
gens = {
    "white": lambda r: r.standard_normal(N) * 0.02,
    "pink": lambda r: (
        lambda w: (
            lambda F, fq: (
                F.__setitem__(slice(1, None), F[1:] / np.sqrt(fq[1:])),
                np.fft.irfft(F, N),
            )[1]
        )(np.fft.rfft(w), np.fft.rfftfreq(N, 1 / SR))
    )(r.standard_normal(N)),
    "rumble": lambda r: np.fft.irfft(
        np.fft.rfft(r.standard_normal(N)) / (1 + np.fft.rfftfreq(N, 1 / SR) / 30) ** 2,
        N,
    ),
}
false = []
for name, g in gens.items():
    for seed in range(8):
        a = g(np.random.default_rng(200 + seed))
        a = a / np.std(a) * 0.02
        tr, hop = tracks(a)
        for x in keep(tr):
            false.append((sup(x, hop), x["duration_seconds"], name))
s = np.array([f[0] for f in false])
d = np.array([f[1] for f in false])
print(
    "false curves",
    len(false),
    "support pct 10/50/90/max",
    np.round(np.percentile(s, [10, 50, 90, 100]), 2),
    "dur 50/90/max",
    np.round(np.percentile(d, [50, 90, 100]), 1),
)
for th in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
    print(f"  support>={th}: false curves kept {int((s >= th).sum())} of {len(s)}")
for th_s, th_d in ((0.5, 5), (0.5, 8), (0.6, 5), (0.6, 8), (0.7, 5)):
    print(
        f"  support>={th_s} and dur>={th_d}: kept {int(((s >= th_s) & (d >= th_d)).sum())}"
    )
print("true curves in noise (240 s pass, f0=300)")
for snr in (6, 0, -6, -12, -15, -18, -21, -24):
    amp = 0.1
    sig = amp / np.sqrt(2) / 10 ** (snr / 20)
    n = 240 * SR
    t = np.arange(n) / SR
    x = 100 * (t - 120)
    f = 300 * 343 / (343 + 100 * x / np.hypot(x, 3500))
    a = amp * np.sin(np.cumsum(f) / SR * 2 * np.pi) + sig * np.random.default_rng(
        3
    ).standard_normal(n)
    tr, hop = tracks(a)
    rows = []
    for k in tr:
        m = np.array(k["measurements"])
        if np.median(np.abs(m[:, 1] - np.interp(m[:, 0], t, f))) < 6:
            rows.append((round(k["duration_seconds"], 0), round(sup(k, hop), 2)))
    rows.sort(reverse=True)
    print(f"  SNR {snr:>4}: top true segments (span s, support) {rows[:4]}")
