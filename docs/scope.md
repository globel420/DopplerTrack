# Project scope

**Goal:** estimate where a video was recorded by matching aircraft Doppler curves in its audio against historical flight tracks, and say "undetermined" when the evidence doesn't support an answer.

## What's built

| Area | Status |
| --- | --- |
| Web interface: upload, link import, progress, results, report download | Working |
| Resumable uploads up to 4 GiB and one hour | Working; a full 4 GiB transfer hasn't been run in production |
| Link import through an egress proxy that blocks private addresses | Working |
| Quick audio check and closer curve measurement | Working on generated audio; see the [known weaknesses](optimizations.md) |
| Flight lookup through ADSB.aero, with consent | Query and parsing work against the live service; busy areas return an incomplete list, so the app abstains |
| Location search and abstain rules | Works on clean simulated flyovers in small search areas, and fails at larger ones; see the [test results](optimizations.md) |
| Login through Cloudflare Access, with origin token checks | Working; required unless development mode is set explicitly |
| Automatic deletion after 24 hours | Working |
| Docker deployment with reduced privileges | Working |

## What isn't built

- Analysis of the video picture
- Flight matching for clips over 10 minutes
- Calibrated accuracy or confidence areas
- A login option other than Cloudflare Access
- Saved results that survive a page reload

## What hasn't been verified

- **Accuracy on real recordings.** No real aircraft recording with a known location has been run through the full system.
- **A real end-to-end location result.** The live flight service has been queried once as a check, but not with a user's job.
- **False-location rate.** The rate at which it reports a wrong location on real audio is unknown.

## Stack

Python 3.13, FastAPI, Redis, NumPy, SciPy, FFmpeg and yt-dlp, with a plain HTML, CSS and JavaScript front end that has no build step.
