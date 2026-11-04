import numpy as np, time, math
from datetime import datetime, UTC
import app.geolocation as g

START = "2026-06-01T12:00:00+00:00"
EP = datetime.fromisoformat(START).timestamp()


def track(
    rng,
    cx,
    cy,
    alt,
    spd,
    hdg,
    fid,
    t0=EP - 600,
    t1=EP + 1200,
    dt=5,
    lat0=40.0,
    lon0=-100.0,
):
    ts = np.arange(t0, t1, dt)
    hx, hy = math.sin(hdg), math.cos(hdg)
    tc = EP + rng.uniform(30, 500)
    pts = []
    for t in ts:
        e = cx + spd * (t - tc) * hx
        n = cy + spd * (t - tc) * hy
        lat = lat0 + n / 111320
        lon = lon0 + e / (111320 * math.cos(math.radians(lat0)))
        pts.append((float(t), lat, lon, alt))
    return {
        "flight_id": fid,
        "callsign": fid,
        "icao_type": "X",
        "emitter_category": "A1",
        "segments": [pts],
    }


def rnd_flights(rng, n, R=60000):
    return [
        track(
            rng,
            rng.uniform(-R, R),
            rng.uniform(-R, R),
            rng.uniform(2000, 12000),
            rng.uniform(60, 120),
            rng.uniform(0, 2 * math.pi),
            f"F{i}",
        )
        for i in range(n)
    ]


def forward(
    fl, recv, f0, start, span, c=None, offs=0.0, noise=0.0, rng=None, quant=None
):
    segs = g.prepare_segments([fl])
    s = segs[0]
    ts = np.linspace(start, start + span, 96)
    if c:
        keep = g.SOUND_SPEED_MPS
        g.SOUND_SPEED_MPS = c
    ratio, valid = g.predict_doppler_ratio(recv, EP + offs + ts, s)
    if c:
        g.SOUND_SPEED_MPS = keep
    f = f0 * ratio
    if noise:
        f = f + rng.normal(0, noise, f.size)
    if quant:
        f = np.round(f / quant) * quant
    return {
        "measurements": [[float(a), float(b)] for a, b, v in zip(ts, f, valid) if v],
        "candidate_index": 0,
    }


# --- E2a: FALSE POSITIVES: ridges that have nothing to do with any aircraft
rng = np.random.default_rng(1)
T = []
N = 14
fp = 0
two = 0
statuses = {}
for trial in range(N):
    fl = rnd_flights(rng, 10)
    curves = []
    for i in range(3):
        f0 = rng.uniform(160, 1500)
        slope = rng.uniform(-0.02, 0.02)  # gentle drift, like road/machinery tones
        st = rng.uniform(0, 200)
        sp = rng.uniform(10, 60)
        ts = np.linspace(st, st + sp, 96)
        fs = f0 * (1 + slope * (ts - st) / sp) + rng.normal(0, 1.0, 96)
        curves.append(
            {
                "measurements": [[float(a), float(b)] for a, b in zip(ts, fs)],
                "candidate_index": i,
            }
        )
    a = g.SearchArea(40.0, -100.0, 10.0)
    t = time.time()
    r = g.rank_location_hypotheses(curves, fl, START, a, 30)
    T.append(time.time() - t)
    statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    if len(r["matches"]) >= 2:
        two += 1
    if r["status"] == "experimental_candidate":
        fp += 1
print(
    f"E2a unrelated ridges, 10 flights, r=10km, ±30s: trials={N} statuses={statuses} two+flight 'matches'={two} false candidates={fp} mean_time={np.mean(T):.1f}s"
)
print(
    "    reason sample:",
    r["reason"][:90],
    "median err",
    r.get("best_median_relative_error"),
)


# --- E2b: true positives; ideal synthetic, with radius sweep
def truth_case(
    rng,
    R_km,
    nflights=10,
    c=None,
    recv_alt=0.0,
    offs=0.0,
    noise=0.3,
    quant=None,
    side=None,
):
    d = rng.uniform(0, R_km * 1000 * 0.9)
    th = rng.uniform(0, 2 * math.pi)
    e, n = d * math.sin(th), d * math.cos(th)
    lat = 40.0 + n / 111320
    lon = -100.0 + e / (111320 * math.cos(math.radians(40)))
    recv = g.geodetic_to_ecef(lat, lon, recv_alt)
    fl = rnd_flights(rng, nflights - 2)
    a1 = track(
        rng,
        e + rng.uniform(1000, 4000),
        n,
        1500,
        rng.uniform(60, 110),
        rng.uniform(0, 6.28),
        "T1",
    )
    a2 = track(
        rng,
        e,
        n + rng.uniform(1000, 4000),
        1500,
        rng.uniform(60, 110),
        rng.uniform(0, 6.28),
        "T2",
    )
    curves = []
    for j, tr_ in enumerate((a1, a2)):
        c_ = forward(
            tr_,
            recv,
            rng.uniform(170, 400),
            rng.uniform(0, 60),
            120,
            c=c,
            offs=offs,
            noise=noise,
            rng=rng,
            quant=quant,
        )
        c_["candidate_index"] = j
        curves.append(c_)
    return (lat, lon), fl + [a1, a2], curves


def run(label, N=6, **kw):
    rng = np.random.default_rng(11)
    errs = []
    st = {}
    gotT = 0
    R = kw.pop("R_km", 10)
    t = time.time()
    for _ in range(N):
        tr, fl, cu = truth_case(rng, R, **kw)
        r = g.rank_location_hypotheses(
            cu, fl, START, g.SearchArea(40.0, -100.0, float(R)), 30
        )
        st[r["status"]] = st.get(r["status"], 0) + 1
        ids = {m["flight_id"] for m in r["matches"]}
        gotT += {"T1", "T2"} <= ids
        if r.get("location"):
            errs.append(
                g._distance_km(
                    tr, (r["location"]["latitude"], r["location"]["longitude"])
                )
            )
    e = (
        f"median_err={np.median(errs):.2f}km max={np.max(errs):.2f}km"
        if errs
        else "no coordinates"
    )
    print(
        f"E2b {label:<46} {st} right-pair={gotT}/{N} {e} ({(time.time() - t) / N:.1f}s/case)"
    )


run("ideal, r=10km", R_km=10)
run("ideal, r=25km", R_km=25)
run("ideal, r=50km", R_km=50)
run("true c=320 m/s (cold air path), r=10km", R_km=10, c=320.0)
run("receiver at 1600 m elevation, r=10km", R_km=10, recv_alt=1600.0)
run("recorder clock off by 7 s, r=10km", R_km=10, offs=7.0)
run("5.38 Hz bin quantisation, r=10km", R_km=10, quant=5.38)
