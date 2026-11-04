import numpy as np, time, json, math
from datetime import datetime, UTC
import app.geolocation as g

# Count repeated work and time a worst-case-ish job
calls = 0
pts = set()
dup = 0
orig = g._score_hypothesis


def wrap(c, s, e, p, o, *rest):
    global calls, dup
    calls += 1
    k = (p, o)
    if k in pts:
        dup += 1
    pts.add(k)
    return orig(c, s, e, p, o, *rest)


g._score_hypothesis = wrap
START = "2026-06-01T12:00:00+00:00"
EP = datetime.fromisoformat(START).timestamp()
rng = np.random.default_rng(2)


def tr(i, n=120):
    ts = np.arange(EP - 300, EP + 900, 1200 / n)
    h = rng.uniform(0, 6.28)
    cx, cy = rng.uniform(-50000, 50000, 2)
    s = rng.uniform(80, 220)
    return {
        "flight_id": f"F{i}",
        "segments": [
            [
                (
                    float(t),
                    40 + (cy + s * (t - EP) * math.cos(h)) / 111320,
                    -100 + (cx + s * (t - EP) * math.sin(h)) / 85000,
                    float(rng.uniform(5000, 30000)),
                )
                for t in ts
            ]
        ],
    }


for nf, nc, unc in [(10, 3, 30), (49, 6, 30)]:
    fl = [tr(i) for i in range(nf)]
    curves = [
        {
            "measurements": [
                [float(t), 300 + float(t) * 0.2] for t in np.linspace(0, 60, 96)
            ],
            "candidate_index": i,
        }
        for i in range(nc)
    ]
    calls = dup = 0
    pts = set()
    t = time.time()
    r = g.rank_location_hypotheses(curves, fl, START, g.SearchArea(40, -100, 10.0), unc)
    print(
        f"E3 flights={nf} curves={nc} ±{unc}s: {time.time() - t:.1f}s, hypotheses={calls}, repeated={dup} ({100 * dup / calls:.0f}%), unique receiver points={len({k[0] for k in pts})}"
    )
print(
    "coarse grid points (any radius):",
    {r: len(g._grid(40, -100, r, r / 2)) for r in (1, 10, 50)},
)
print(
    "offset samples:",
    {
        u: (len(g._offsets(u)), g._offsets(u)[1] - g._offsets(u)[0])
        for u in (30, 60, 180, 600)
    },
)
