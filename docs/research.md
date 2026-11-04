# Research review

A review of how others approach aircraft Doppler geolocation, carried out on 2026-11-04. These are mostly the projects' own reports, not peer-reviewed results. Check the linked sources before relying on a figure.

## Where this came from

[Colsto's video](https://www.youtube.com/watch?v=UytYrhYLOKI) brought the method to wide attention, and [Simon Willison's post](https://simonwillison.net/2026/Nov/3/a-sound-that-leaks-your-exact-location/) summarizes it. Colsto's code isn't public. Independent projects have since rebuilt the method from the video.

An older line of academic work established the physics: [estimating a propeller aircraft's speed and altitude from one microphone](https://researchers.mq.edu.au/en/publications/application-of-the-short-time-fourier-transform-and-the-wigner-vi/) using the Doppler shift of its blade-rate tone.

## Comparison

| | DopplerTrack | [DOPPLERGEIST](https://github.com/1337isnot1337/DOPPLERGEIST) | [aircraft-doppler](https://github.com/Espaye/aircraft-doppler) | [mjshiggins/dopplertrack](https://github.com/mjshiggins/dopplertrack) | [hermes plugin](https://github.com/mojomast/hermes-aircraft-doppler-geolocation) | [doppler-geo](https://github.com/teamchong/doppler-geo) |
| --- | --- | --- | --- | --- | --- | --- |
| **Analysis audio rate** | up to 22.05 kHz | 4 kHz | not checked | not checked | 4 kHz | 4 kHz |
| **Spectrum window** | 0.19 s | 2 s | not checked | not checked | 1 s | about 1 s |
| **Lowest frequency** | 150 Hz | 40 Hz | not checked | 40 Hz | 60 Hz (script option) | 80 Hz |
| **Curve tracing** | Greedy peak linking | Bounded peak linking, plus Viterbi in its replay method | Peak linking, constant-ratio harmonic groups | Detector with whitening | Viterbi with an "off" state | Dynamic programming |
| **Sound speed** | Fixed 343 m/s | Constant, tested as a sensitivity | From temperature, refined ±5 °C | not checked | From temperature data | 340 m/s in its 1D model |
| **Ground height** | Sea level | Terrain data plus geoid | Flat setting or USGS terrain | not checked | Fitted scale | not stated |
| **Search grid** | About 35 points | 200 m | Dense grid, then least squares | 200 m, then 50 m | 1000, 200, then 50 m | Grid, then Nelder-Mead |
| **Needs known area** | Yes, up to 50 km radius | US, DE or ES profile, or bounds | Yes | Search box | Yes, up to 60 km | No |
| **Needs known time** | Yes, within 10 min | Yes | Clock prior | Day | Date and tolerance | No |
| **Clip length for matching** | Up to 10 min | No such limit found | No such limit found | 1 h and 12 h tested | No such limit found | No such limit found |
| **Flight data** | ADSB.aero (50 flights) | ADSB.lol archive | ADSB.lol archive, ADSB.aero fallback | ADSB.lol | ADSB.lol | ADSB.lol index |
| **License** | MIT | PolyForm Noncommercial | MIT | MIT | MIT | MIT |

"Not checked" means this review didn't confirm it, not that the project lacks it.

## Reported results

| Project | Result | Conditions |
| --- | --- | --- |
| Colsto | About 100 m, as reported by coverage | Secondary reports; one recording |
| mjshiggins/dopplertrack | 43 m on Colsto's clip; 592 m on an eagle cam | Known answers, searched without using them; margin over the runner-up only 4.9% and 2.5% |
| aircraft-doppler | 17 m and 114 m | Blind and hash-stamped, one site, two recordings |
| DOPPLERGEIST | 476 m, automatic; 43.86 m, configured | Inferred or public references; neither accepted as a location |
| hermes plugin | 2 of 18 held-out runs under 3 km | "Mostly fails on unseen clips" |
| colsto-leak-and-mask | Colsto's town with hand-measured events; 137 km off when fully automatic | No blind webcam case located |
| doppler-experiments | Physics checks out; correct flight pair in 6 of 6 worlds | Synthetic only |

The pattern is that a favorable recording with careful setup can land within tens of metres, while unseen recordings mostly fail. Nobody has published a validated false-location rate.

## DOPPLERGEIST in detail

[DOPPLERGEIST](https://github.com/1337isnot1337/DOPPLERGEIST) by Cinna Davis is the most thoroughly documented open attempt, and its negative results are as useful as its positive ones.

**License:** it's source-available under [PolyForm Noncommercial 1.0.0](https://github.com/1337isnot1337/DOPPLERGEIST/blob/main/LICENSE), not open source. Its code can't be copied into this MIT project. This review describes its ideas in our own words, with links, and DopplerTrack contains none of its code. Anyone reusing its code must follow its terms.

### Its pipeline

1. **Audio discovery** ([docs](https://github.com/1337isnot1337/DOPPLERGEIST/blob/main/docs/AUDIO_DISCOVERY.md)). Scans the whole recording in 120 s chunks with 60 s overlap, using a 2 s window at 0.5 s steps over 40 to 1600 Hz. It suppresses steady tones, links peaks with limits on motion, and merges overlapping chunks so a curve isn't split.
2. **Harmonics.** Integer and rational ratios, such as measured 2f and 3f with a missing fundamental, count as one event. A single ridge remains eligible.
3. **Flight data** ([docs](https://github.com/1337isnot1337/DOPPLERGEIST/blob/main/docs/DATA_AND_SCALE.md)). Uses verified ADSB.lol archives. All aircraft types stay eligible, track gaps are split, and nothing is extrapolated. Heights use GPS altitude, plus terrain data with a geoid correction.
4. **Matching** ([docs](https://github.com/1337isnot1337/DOPPLERGEIST/blob/main/docs/MATCHING.md)). Sweeps one shared recording start across all events. It uses a robust residual in Hz, and allows a small bounded engine drift (2% by default). Results report error, outliers and trajectory support for each event.
5. **Decision** ([docs](https://github.com/1337isnot1337/DOPPLERGEIST/blob/main/docs/CONFIDENCE.md)). Normally needs two independent flyovers, at most 3 Hz error per event, at most 25% outliers and at least 95% trajectory support. An alternative within 0.5 Hz per event counts as a near tie. Its authors say these thresholds aren't empirically calibrated.
6. **Next-step guidance** ([docs](https://github.com/1337isnot1337/DOPPLERGEIST/blob/main/docs/INCONCLUSIVE_GUIDANCE.md)). Explains an inconclusive result in terms of what's missing, without changing the result.

### Its results

- Automatic MP3 run: 466 aircraft and 105 ranked locations, with the leader 476 m from an inferred reference. The run stayed inconclusive.
- Configured two-flyover replay: 43.86 m from a public reference, using source-informed inputs, and not accepted as a location.
- 40 smartphone recordings: candidate sound groups in 36. These are proposals, not confirmed aircraft.
- Five public field recordings: three rankings with nothing within 1 km of the reported site, and two with no candidate.
- NASA and WSU recordings: mixed detection and no validated automatic location.
- Its status: "no independently accepted blind real-recording localization has yet been established."

### Time recovery from mains hum

Its [ENF note](https://github.com/1337isnot1337/DOPPLERGEIST/blob/main/docs/ENF_TIMESTAMPING.md) looks at recovering recording time from mains hum. Only 2 of 50 real recordings showed a persistent tone candidate, and it found no public North American reference data. So this isn't practical yet, though [forensic ENF work](https://doaj.org/article/dc6cd16fbf334eb7a0616f4e772303f3) shows why the idea is attractive.

### What DopplerTrack could adopt

The [optimization list](optimizations.md) marks items drawn from this review. The ideas that best address our measured weaknesses are the long analysis window, whole-recording discovery without window splits, thresholds in Hz, terrain and GPS heights, and the separation between a ranked guess and an accepted location.

## Useful ideas from other projects

- **Time-shift control** (aircraft-doppler, colsto-leak-and-mask): rerun the search with the wrong clock. If it scores as well, the result is noise. One blind case was invalidated this way.
- **"Unheard" penalty** (doppler-geo, hermes, Colsto): mark a spot down if a nearby propeller plane should have been audible but wasn't. Use with care, since flight data has gaps.
- **Scoring against the spectrogram** (Colsto, mjshiggins): instead of tracing a curve first, score spectrogram brightness along each predicted curve. This can use faint tones that a tracer would miss.
- **Bootstrap and leave-one-out** (aircraft-doppler): measure how much the answer depends on one flyover.
- **Verdict tiers** (aircraft-doppler): "ok" needs three explained passes, so two passes rate only "weak".
- **Hash-stamped predictions** (aircraft-doppler): record the answer before checking it, which makes blind tests credible.
- **Geometry** (mjshiggins, colsto-leak-and-mask): low passes to one side pin a location well, and nearly overhead passes don't.

## Protecting recordings

- [doppler-experiments](https://github.com/cbonitz/doppler-experiments) found in synthetic tests that a linear low-cut filter has no effect after per-frequency normalization, and that phone-band compression only partly helps.
- [colsto-leak-and-mask](https://github.com/Bodila51/colsto-leak-and-mask) offers a "safe copy" that notches tones and strips metadata. No effectiveness figure is published.

## Sources

Project links are above. Flight data sources: [ADSB.lol archive](https://www.adsb.lol/docs/open-data/historical), [ADSB.aero guide](https://adsb.aero/llms.txt), and the [OpenSky Trino interface via pyopensky](https://pypi.org/project/pyopensky/), which is free for academic and governmental research.
