# Optimizations and test results

Improvements based on tests run on 2026-11-04 and the [research review](research.md). **Items 1, 3, 6, 8, 10, 11, 12 and 21 are done**, as marked below. The rest are proposals, listed so we can decide what to do.

Effort: **S** is a few hours, **M** is a day or two, and **L** is several days. Source: **Tested** means measured here, **Code** means found by reading the code, and **Research** means another project does it.

## The short version

The physics works, but the system still hasn't been scored on a real recording. The quick fixes removed the worst detection bugs. The main problems left are:

1. **The fit threshold barely filters anything.** Unrelated tones "matched" flights in every trial. Only a separate ambiguity check stopped a wrong answer.
2. **The search grid is too coarse.** It finds the right flight pair in about half of clean simulated cases at 10 km, and rarely at 25 km or more.
3. **Clean simulations still give some wrong answers.** About 1 in 13 coordinates was over 1 km off, and the worst was about 11 km off, even with the true conditions supplied.
4. **The method's reference recording can't run.** Its two flyovers are about 15 minutes apart, while flight matching stops at 10 minutes. The live flight service also hit its 50-flight limit for that area.

## Results of the quick fixes

| Test | Before | After |
| --- | --- | --- |
| Tone starting 2 s or later into noisy audio | **Missed completely** | Found for its full length, about 240 s |
| 90 Hz engine tone, 240 s pass | Missed | Found for 240 s |
| 134 Hz engine tone, 240 s pass | 96 s found | 240 s found |
| Frequency error at 300 Hz | 1.35 Hz | 0.07 Hz |
| Moving tones reported in pure noise (quick check, 20 clips) | 1.7 per clip on average, up to 5 | **0** |
| Longest false curve in pure noise | 16.7 s | 3.4 s |

Search time also fell by about 40%, because repeated hypotheses are now skipped: 49 flights and 6 curves went from 139 s to 83 s, and repeated work went from 51% to none.

**A side effect, and how it's handled:** removing the track budget alone let noise form more short false curves. Requiring a curve to be visible at least 55% of the time removed them. False curves were tracked 34–49% of the time, and true curves 93–100% down to −18 dB. This threshold comes from simulated noise, so it needs checking on real audio. A very faint tone (around −24 dB) can now be rejected too.

**Stating temperature and ground height** (30 clean simulated flyovers per condition, 10 km radius):

| Site | Search told the true values? | Coordinates given | Right flight pair | Median error | Over 1 km off |
| --- | --- | --- | --- | --- | --- |
| Standard (15 °C, sea level) | Not needed | 15 | 14 | 0.49 km | 3 |
| Hot and high (35 °C, 1600 m) | No | 19 | 16 | 0.66 km | 6 |
| Hot and high (35 °C, 1600 m) | Yes | 13 | 13 | 0.49 km | 1 |
| Cold and high (−5 °C, 1600 m) | No | 11 | 9 | 0.96 km | 5 |
| Cold and high (−5 °C, 1600 m) | Yes | 13 | 12 | 0.49 km | 2 |

So the benefit is **fewer bad answers rather than more answers**. An earlier version of this page said these errors cut recall sharply, from 5 of 8 to 2 of 8. That was too small a sample, and the larger test doesn't support it.

## Recommended order

| Phase | Items | Why |
| --- | --- | --- |
| **1. Quick fixes** | Done: 1, 3, 6, 8, 10, 11, 12, 21 | Small changes with a measured or clear benefit |
| **2. Measure** | 18, then 7, 13, 16 | Get a real score, and stop trusting weak fits |
| **3. Better detection and search** | 2, 4, 9 | The largest expected accuracy gains |
| **4. Scale** | 5, 14, 15, 17 | Long recordings, busy areas and steadier answers |

## Audio detection

| # | Change | Evidence | Effort | Source |
| --- | --- | --- | --- | --- |
| 1 | ✅ **Done.** Stop noise using up the tracker budget. Retire finished tracks, and count only active ones toward the limit | Tone starting at 0–1 s: found for about 239 s. Starting at 2 s or later: **not found at all**. With the budget removed: found every time | S | Tested |
| 2 | **Use a longer analysis window**, about 1–2 s on 4 kHz audio | At −24 dB, the current 0.19 s window kept 5 s of a 240 s curve, and a 1 s window kept 200 s. At −30 dB: 0 s and 24 s | M | Tested, Research |
| 3 | ✅ **Done, now 50 Hz.** Lower the frequency floor from 150 Hz | Replications report tones of 50–300 Hz. A 90 Hz pass yielded only 3 s, and a 134 Hz pass lost its lower part (96 s kept of 240 s) | S | Tested, Research |
| 4 | **Replace greedy peak linking** with a Viterbi tracker that can say "no tone", plus per-frequency whitening | Used in some form by DOPPLERGEIST, the hermes plugin and doppler-geo | M–L | Research |
| 5 | **Remove the 60-second window split and 10-minute match limit**, using overlapping chunks that merge | Reference flyovers are at about 2156 s and 3068 s, so the clip can't be matched today. A 60-second window also cuts up a long pass | L | Code, Research |
| 6 | ✅ **Done.** Measure frequency between bins (peak interpolation) | Bins are 5.38 Hz wide, giving a median error of 1.35 Hz at 300 Hz. Rounding alone lowered recall from 5 of 8 to 4 of 8. Item 2 also helps | S | Tested |
| 7 | **Test for a real Doppler shape.** Compare each curve with a flat tone, and require a physically plausible speed and distance | Today's check fits a straight line, though a real pass is an S-curve (R² of 0.80–0.92 on ideal passes) | M | Tested, Research |
| 8 | ✅ **Done.** Recognize more harmonic ratios, including 3:2 and 4:3 | Only 2:1, 3:1 and 4:1 are grouped, so one engine could count as two "independent" passes | S | Code, Research |

## Location search

Clean simulated flyovers before the quick fixes, 8 cases each, 10 flights per case. Treat these as a rough guide, since 8 cases is a small sample:

| Condition | Found the right flight pair | Coordinate error (km) |
| --- | --- | --- |
| Search radius 5 km | 4 of 8 | 0.09–0.40 |
| Search radius 10 km | 5 of 8 | 0.19–2.32 |
| Search radius 25 km | 1 of 8 | 0.46 |
| Search radius 50 km | **0 of 8** | none given |
| 10 km, clock wrong by 7 s | 4 of 8 | 0.19–0.61 |
| 10 km, frequency rounded to 5.38 Hz bins | 4 of 8 | 0.19–0.80 |

| # | Change | Evidence | Effort | Source |
| --- | --- | --- | --- | --- |
| 9 | **Use a fixed grid spacing**, such as 200–500 m, instead of 13 starting points at any radius | The table above: recall falls to 0 of 8 at 50 km, where starting points are 25 km apart. Others use 200 m or finer | M | Tested, Research |
| 10 | ✅ **Done.** Skip repeated work and check for cancellation during the search | 51% of hypotheses were recomputed. 49 flights and 6 curves took 139 s at ±30 s. Cancel and timeout aren't checked during this step | S | Tested, Code |
| 11 | ✅ **Done.** Derive sound speed from an optional temperature input and a standard lapse, instead of a fixed 343 m/s. Fetching weather automatically is still open | See the results above. Other projects use temperature | S | Tested, Research |
| 12 | ✅ **Input field done.** Use ground elevation. Terrain data is still open | See the results above | S–M | Tested, Research |
| 13 | **Replace the 1.5% gate** with a test on how well a flight explains the curve's shape, compared with a flat tone | Unrelated tones matched 2 or more flights in **14 of 14** trials, with about 0.1% error in the trial sampled. Only the ambiguity check stopped them | M | Tested |
| 14 | **Reduce the 60 km hearing margin and handle busy areas.** Decide between a smaller margin (DOPPLERGEIST defaults to 20 km), filtering jets, following the cursor, or using the ADSB.lol daily files | The first live query: 65 km radius, 50 flights plus a cursor, so the app abstains. Daily files are about 3 GB each | S to L | Tested, Research |
| 15 | **Use more of the flight data.** The response includes ground speed, track, vertical rate and altitude corrections that the code ignores | Field names seen in the live response. Their exact meaning still needs checking in the provider's docs | S–M | Tested |

## Trust and validation

| # | Change | Evidence | Effort | Source |
| --- | --- | --- | --- | --- |
| 16 | **Add a time-shift control.** Rerun with a wrong clock, and refuse the result if it scores as well | In simulations where closest approach could fall outside the curve, 3 coordinates were given and all were 14–35 km off. One blind case elsewhere was invalidated by this control | M | Tested, Research |
| 17 | **Add verdict tiers and a stability check.** Rate two passes "weak", three passes "ok", and test dropping each flyover | aircraft-doppler's approach | M | Research |
| 18 | **Build a real benchmark** from public recordings with known answers, and record each answer before checking it | The system has never been scored on a real recording. This is the most valuable long-term item | M | Research |
| 19 | **Add an "unheard" penalty** for close planes that should have been audible | Used by Colsto and others. Needs care, since flight data has gaps | M | Research |
| 20 | **Score the spectrogram directly** along predicted curves, as an alternative to tracing first | Could use tones too faint to trace | L | Research |

## Product and security

| # | Change | Evidence | Effort | Source |
| --- | --- | --- | --- | --- |
| 21 | ✅ **Done.** Make login fail closed. An unset `DOPPLERTRACK_ENV` now means production, and an unknown value stops startup | Previously, a deployment that forgot this setting had no login | S | Code |
| 22 | **Support other login systems** besides Cloudflare Access | Helps people run it on their own sites | M | Code |
| 23 | **Keep results across a reload**, by putting the job in the URL | Results stay on the server for 24 hours, but a refresh loses them | S | Code |
| 24 | **Allow another location try** with a different time or area after a closer scan | The API refuses a second scan, so it means uploading again | S | Code |
| 25 | **Suggest the file's creation time** as a hint for the user to confirm | Would save typing, though metadata can be wrong or stripped | S | Research |
| 26 | **Tidy small issues:** a score type mismatch in an unreachable branch of the search, and "one-hour" wording shown for all long clips | Neither affects results today | S | Code |

## Not recommended for now

- **Recovering recording time from mains hum.** DOPPLERGEIST found a persistent tone candidate in only 2 of 50 recordings, and no public North American reference data.
- **Worldwide search with no known area.** It needs a large flight index, and it raises the misuse risk considerably.

## How far to trust these tests

Every test used generated audio and tracks, with few cases per condition, so treat these as signs of direction and not precise rates. Simulated tones are cleaner and steadier than real aircraft. The flight service was queried once, for one area, so the 50-flight limit may not apply everywhere. Scripts are in [`experiments/`](../experiments/).

## Already fixed

These came up during the review and were fixed with the site refresh:

- The security policy blocked the browser's video-length check.
- The cancel button disappeared after an upload finished.
- A label claimed 48 kHz analysis, when the limit is 22.05 kHz.
- Spectrogram frequency labels sat on the time axis, and low frequencies were squashed.
- Time and area had to be entered before knowing whether the audio was usable.
