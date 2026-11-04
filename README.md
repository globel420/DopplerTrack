# DopplerTrack

**Estimate where a video was recorded from the sound of passing aircraft.**

Propeller planes leave a faint hum in a video's soundtrack, and that hum falls in pitch as the plane passes. DopplerTrack measures those curves, fetches historical flight tracks, and searches a map for the spot where real flights would have sounded that way. When the evidence is weak, it answers "location undetermined".

> [!WARNING]
> **This is experimental, and its accuracy is unproven.** It has passed synthetic tests only. It has never located a real recording with a known answer, and [our own tests](docs/optimizations.md) found serious weaknesses. Treat any coordinate as an untested lead.

**[Read the plain-language guide](docs/guide.md)**, which explains how it works and how to build your own.

## Try the idea in one minute

This script hides a microphone, predicts what it would hear from two planes, then finds it again. It needs only NumPy and no audio or network.

```bash
pip install numpy
python examples/minimal_doppler_match.py
```

## What it does

| Stage | What happens | Code |
| --- | --- | --- |
| **Intake** | Resumable uploads up to 4 GiB and one hour, or a public video link fetched through a locked-down proxy | `app/main.py`, `app/url_import.py`, `app/egress_proxy.py` |
| **Quick check** | Spectrogram, audio quality, and a search for moving tones | `app/analysis.py` |
| **Closer scan** | Measures candidate curves and groups harmonics | `app/analysis.py` |
| **Flight lookup** | With consent, sends time and area (never media) to ADSB.aero | `app/geolocation.py` |
| **Location search** | Predicts each curve with sound travel time, across candidate spots and one shared recorder clock | `app/geolocation.py` |
| **Decision** | Requires two separate flights and no equally good alternative spot; otherwise abstains | `app/geolocation.py` |

The parts worth reading first are [`app/geolocation.py`](app/geolocation.py), which holds the physical model and search, and [`app/analysis.py`](app/analysis.py), which holds the audio measurement.

**Out of scope for now:** the video picture (it uses sound only), clips over 10 minutes for flight matching, and calibrated accuracy figures.

## How it fits together

```text
Browser ──> Web API (FastAPI) ──> Redis queue ──> Worker (FFmpeg, NumPy, SciPy)
                                                     │
                                                     └──> Egress proxy ──> Public video pages
                                                                      └──> ADSB.aero flight history
```

The worker has no direct internet route. Its only way out is the proxy, which rejects private and internal addresses, including after redirects.

## Run it locally

You'll need Python 3.13, FFmpeg and Redis.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
export REDIS_URL=redis://127.0.0.1:6379/0 DOPPLERTRACK_ENV=development
uvicorn app.main:app --host 127.0.0.1 --port 8765
```

In a second terminal, with the same environment, start the worker:

```bash
python -m app.worker
```

Open <http://127.0.0.1:8765>.

> [!CAUTION]
> Development mode has **no login**, and it's the default when `DOPPLERTRACK_ENV` is unset. Never expose it to a network.

## Run the tests

```bash
python -m pip install --require-hashes -r requirements-dev.lock
python -m unittest discover -s tests -v
```

The tests use generated tones and a mocked flight service. They check that the code behaves consistently, not that it locates real recordings.

## Deploy it

`compose.yaml` runs the web app, worker, Redis and egress proxy with read-only filesystems, dropped privileges and resource limits. It expects:

1. **Cloudflare Access** in front of your hostname. Copy `.env.example` to `.env`, then set `CF_ACCESS_TEAM_DOMAIN` and `CF_ACCESS_AUD`. The app verifies the Access token on every request and won't start in production without them. To use another login system, replace `app/access.py`.
2. **A reverse proxy** on a Docker network named `dopplertrack-proxy`, routing to `dopplertrack-web:8000` with a request body limit above 32 MiB:

   ```bash
   docker network create dopplertrack-proxy
   docker compose up -d --build
   curl -fsS http://127.0.0.1:8765/health
   ```

3. **A decision on flight lookups.** `ADSB_HISTORY_ENABLED` is off by default. ADSB.aero is a free, best-effort service with no published SLA, so read [its guidance](https://adsb.aero/llms.txt) first. Even when it's on, each lookup needs the user's consent.

Uploads, plots and job records are deleted after 24 hours.

## Documentation

- [Plain-language guide](docs/guide.md): what this is, how it works, and how to build your own
- [Project scope](docs/scope.md): what's built, what isn't, and what's unverified
- [Optimizations and test results](docs/optimizations.md): measured weaknesses and proposed fixes
- [Research review](docs/research.md): how other projects approach this, including DOPPLERGEIST
- [Geolocation method](docs/geolocation-method.md): the physical model in detail

## Use it responsibly

This technique can reveal where someone lives or works from a video they believed was anonymous. Use it on recordings you have the right to analyze, for research, verification or your own footage. Don't use it to find or follow a person, and don't rely on it as the only evidence for anything.

## Credits and licenses

The code is under the [MIT License](LICENSE).

- **Method:** demonstrated publicly by [Colsto](https://www.youtube.com/watch?v=UytYrhYLOKI). This is an independent implementation, and is not affiliated with Colsto or the projects below.
- **Flight data:** [ADSB.lol](https://www.adsb.lol/docs/open-data/historical) contributors, under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/), queried through [ADSB.aero](https://adsb.aero/llms.txt). This repository contains no flight data.
- **Related work:** the [research review](docs/research.md) discusses [DOPPLERGEIST](https://github.com/1337isnot1337/DOPPLERGEIST) by Cinna Davis and others. DOPPLERGEIST is source-available under PolyForm Noncommercial 1.0.0. DopplerTrack contains none of its code.
- **Name:** [mjshiggins/dopplertrack](https://github.com/mjshiggins/dopplertrack) is a separate, unrelated project with the same name.
