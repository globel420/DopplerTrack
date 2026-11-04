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
    tc=None,
):
    ts = np.arange(t0, t1, dt)
    hx, hy = math.sin(hdg), math.cos(hdg)
    tc = EP + rng.uniform(30, 500) if tc is None else tc
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


# Fair flyovers: closest approach is inside the measured curve, 1-4 km to the side, 1.5 km up, crossing headings.
def case(rng, R_km, offs=0.0, noise=0.3, c=None, recv_alt=0.0, quant=None, nf=10):
    d = rng.uniform(0, R_km * 1000 * 0.9)
    th = rng.uniform(0, 2 * math.pi)
    e, n = d * math.sin(th), d * math.cos(th)
    lat = 40.0 + n / 111320
    lon = -100.0 + e / (111320 * math.cos(math.radians(40)))
    recv = g.geodetic_to_ecef(lat, lon, recv_alt)
    fl = rnd_flights(rng, nf - 2)
    h = rng.uniform(0, 6.28)
    curves = []
    tr = []
    for j in range(2):
        st = rng.uniform(0, 60) + j * 130
        hd = h + j * math.pi / 2 + rng.uniform(-0.3, 0.3)
        off = rng.uniform(1000, 4000) * rng.choice([-1, 1])
        cx = e + off * math.cos(hd)
        cy = n - off * math.sin(hd)
        t = track(
            rng,
            cx,
            cy,
            1500 / 0.3048,
            rng.uniform(70, 110),
            hd,
            f"T{j + 1}",
            tc=EP + st + 60,
        )
        tr.append(t)
        cu = forward(
            t,
            recv,
            rng.uniform(170, 400),
            st,
            120,
            c=c,
            offs=offs,
            noise=noise,
            rng=rng,
            quant=quant,
        )
        cu["candidate_index"] = j
        curves.append(cu)
    return (lat, lon), fl + tr, curves


def run(label, N=8, **kw):
    rng = np.random.default_rng(21)
    errs = []
    st = {}
    got = 0
    R = kw.pop("R_km", 10)
    t0 = time.time()
    truth_err = []
    for _ in range(N):
        tr, fl, cu = case(rng, R, **kw)
        r = g.rank_location_hypotheses(
            cu, fl, START, g.SearchArea(40.0, -100.0, float(R)), 30
        )
        st[r["status"]] = st.get(r["status"], 0) + 1
        got += {"T1", "T2"} <= {m["flight_id"] for m in r["matches"]}
        if r.get("location"):
            errs.append(
                g._distance_km(
                    tr, (r["location"]["latitude"], r["location"]["longitude"])
                )
            )
    print(
        f"E2c {label:<40} {st} right-pair={got}/{N} coord_errors_km={[round(x, 2) for x in errs]} ({(time.time() - t0) / N:.0f}s/case)",
        flush=True,
    )


run("clean flyovers, r=5km", R_km=5)
run("clean flyovers, r=10km", R_km=10)
run("clean flyovers, r=25km", R_km=25)
run("clean flyovers, r=50km", R_km=50)
run("mic at 1600 m elevation, r=10km", R_km=10, recv_alt=1600.0)
run("clock wrong by 7 s, r=10km", R_km=10, offs=7.0)
run("5.38 Hz bin rounding, r=10km", R_km=10, quant=5.38)
