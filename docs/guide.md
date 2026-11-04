# How a passing plane can give away where a video was filmed

The idea is simple enough to explain in a minute. Making it reliable is hard, and nobody has fully managed it yet. This guide covers both, and shows how to try it yourself.

## The idea

Propeller planes give off a steady hum, often somewhere around 50 to 300 Hz. It can be faint enough that you'd never notice it, but microphones still record it.

As a plane passes, that hum slides downward, like the siren of a passing ambulance. This is the **Doppler effect**. Faster planes fall further in pitch, and closer planes fall more suddenly.

Public flight-tracking data records where planes were at each moment. So if you know roughly when and where a video was made, you can ask: *at which spot on the map would those real flights have sounded exactly like this?*

## The steps

1. **Pull out the sound.** The picture isn't used. Only the audio track is decoded.
2. **Draw a picture of the sound.** A spectrogram shows pitch over time. A steady hum makes a line, and a passing plane makes a line that bends down.
3. **Trace the curves.** Software follows those lines and records pitch at each moment. Sirens, engines and traffic can look similar, so a curve is only a candidate.
4. **Get the flight history.** Using the recording's time and a rough area, fetch the tracks of nearby flights.
5. **Test spots on a map.** For each spot and flight, predict the curve a microphone there would have heard, allowing for the seconds that sound takes to arrive.
6. **Compare and rank.** The engine's true tone is unknown, so it's fitted for each curve. All curves must share one recorder clock.
7. **Answer, or refuse to.** If too few planes fit, or another spot fits nearly as well, the honest answer is "undetermined".

### The formula

Sound heard at time `t` left the plane earlier, at time `τ`. What matters is the plane's motion at that earlier moment.

```text
t = τ + r(τ) / c                 sound arrives after travelling distance r
f(t) = f₀ × c / (c + u(τ))       heard pitch; u is the speed moving away from you
```

`f₀` is the engine's true tone and `c` is the speed of sound.

## Why one plane isn't enough

A curve tells you how far away a plane passed, but not on which side. One straight pass leaves two equally good answers, one on each side of the flight path. A second plane on a different heading picks between them.

Geometry matters as much as sound quality. Planes passing **low and off to one side** pin down a location sharply. Planes passing high and **almost directly overhead** barely change as you move the microphone, which can leave kilometres of doubt. Flights all following one runway line don't resolve the two-sided problem either.

### What makes a recording usable

- **Length:** long recordings catch more flyovers. Published demonstrations mostly use around an hour or more.
- **Aircraft type:** propeller planes work best. Jets mostly help only near airports.
- **Known time:** the start time, ideally to within seconds.
- **Untouched sound:** heavy noise suppression can erase the hum.
- **Coverage:** the plane has to be in the flight data, and many small aircraft aren't.

## Does it really work?

Sometimes, under good conditions. The method drew wide attention through a [video by Colsto](https://www.youtube.com/watch?v=UytYrhYLOKI), whose code isn't public. Coverage reports a location within about 100 m. Independent projects have since tried it, with mixed results. These are the projects' own reports, not peer-reviewed studies.

| Project | Best reported | The catch |
| --- | --- | --- |
| [mjshiggins/dopplertrack](https://github.com/mjshiggins/dopplertrack) | 43 m, and 592 m | Two recordings with known answers |
| [Espaye/aircraft-doppler](https://github.com/Espaye/aircraft-doppler) | 17 m and 114 m, blind | One site, two recordings |
| [DOPPLERGEIST](https://github.com/1337isnot1337/DOPPLERGEIST) | 476 m, automatic | Inconclusive; inferred reference |
| [hermes plugin](https://github.com/mojomast/hermes-aircraft-doppler-geolocation) | 2 of 18 under 3 km | "Mostly fails on unseen clips" |
| [colsto-leak-and-mask](https://github.com/Bodila51/colsto-leak-and-mask) | Colsto's town matched | No blind webcam case located |

> **Where DopplerTrack stands:** it has only passed synthetic tests built from its own model. It has never located a real recording with a known answer, so treat any coordinate it gives as an untested lead.

### What DOPPLERGEIST adds

[DOPPLERGEIST](https://github.com/1337isnot1337/DOPPLERGEIST) by Cinna Davis is the most thoroughly documented independent attempt. Its reports are useful because they publish failures as well as successes. Some of its findings:

- **Automatic discovery:** found candidate sound groups in 36 of 40 real smartphone recordings. It labels these proposals, not confirmed aircraft.
- **Failures count:** of five public field recordings, three gave rankings with nothing within 1 km of the reported site, and two gave nothing.
- **A ranked guess isn't an accepted location:** it keeps the two apart, and can report a leading spot while still answering "inconclusive".
- **Refuse without a defensible time:** with no credible recording-time window, it asks for one instead of assuming a date.
- **Better measuring:** it uses a long 2-second analysis window on low-rate audio, and follows curves with bounded jumps.
- **Better physics inputs:** ground height from terrain data and GPS-based aircraft altitude, instead of assuming sea level.

Its project status says plainly: "no independently accepted blind real-recording localization has yet been established." It's [source-available for noncommercial use](https://github.com/1337isnot1337/DOPPLERGEIST/blob/main/LICENSE) (PolyForm Noncommercial 1.0.0), so read its terms before reusing its code. This project uses none of its code.

## Build your own

Pick the route that matches how far you want to go.

### Route 1: see the geometry in one minute

[This short script](../examples/minimal_doppler_match.py) hides a microphone, predicts what it would hear from two planes, then finds it again. It only needs NumPy.

```bash
pip install numpy
python examples/minimal_doppler_match.py
```

### Route 2: run DopplerTrack on your own machine

Follow [the README](../README.md#run-it-locally). You'll need Python 3.13, FFmpeg and Redis. Development mode has **no login**, so keep it on your own computer.

### Route 3: put it on your own site

The repository includes a Docker Compose setup, covered in [the README](../README.md#deploy-it). Before going public, you'll want:

- **A login in front of it.** DopplerTrack expects [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/) and checks its token on every request. To use something else, replace `app/access.py`.
- **A background worker and queue.** Analysis takes minutes, so never run it inside a web request.
- **Limits.** Cap file size, length, disk use and uploads per person.
- **Automatic deletion** of uploads and results.
- **A locked-down downloader** if you accept links, so nobody can make your server fetch its own private addresses.
- **Consent** before sending the time and area to any outside service.

### Route 4: write it from scratch

The heart of it is one function that predicts a pitch curve for one candidate spot:

```python
def received_ratio(track, microphone, heard_at):
    emitted_at = heard_at.copy()
    for _ in range(6):                     # sound left the plane earlier
        distance = norm(position(track, emitted_at) - microphone, axis=1)
        emitted_at = heard_at - distance / SOUND_SPEED
    offset = position(track, emitted_at) - microphone
    receding = (offset @ velocity(track)) / norm(offset, axis=1)
    return SOUND_SPEED / (SOUND_SPEED + receding)
```

Multiply that ratio by a fitted engine tone, compare it with your measured curve, and repeat across a map grid. Around that, you'll need:

| Piece | A good place to start |
| --- | --- |
| Audio | FFmpeg, mono, about 4 kHz |
| Spectrogram | About 1–2 s windows, 40–700 Hz |
| Curve tracing | Viterbi path, with a way to say "no tone" |
| Flight history | [ADSB.lol daily files](https://www.adsb.lol/docs/open-data/historical) (ODbL) or [ADSB.aero](https://adsb.aero/llms.txt) |
| Sound speed | From air temperature, not a fixed number |
| Heights | Terrain data for the ground, GPS altitude for the plane |
| Map search | Fine grid first (200 m), then refine |

### Test it so you can trust it

The most common mistake is believing a good fit. A search over many flights and spots will always find a best answer, even in pure noise. Before you trust any result:

- **Feed it noise.** Noise must never produce an accepted location.
- **Shift the time.** Run the search with the wrong clock, say 30 minutes off. If that scores as well as the real time, your result means nothing.
- **Drop one plane.** If removing one flyover moves the answer a long way, it isn't stable.
- **Write down your answer before checking it** against recordings whose true location you know.
- **Report the failures.** Your error rate is the number that matters.

## Using it responsibly, and protecting your own videos

This technique can reveal where someone lives or works from a video they thought was anonymous. Use it on recordings you have the right to analyze, for research, verification or your own footage. Don't use it to find or follow a person.

If you're worried about your own recordings:

- **Removing or replacing the audio** is the only certain fix for this particular leak.
- **An ordinary low-cut filter may not be enough.** One [synthetic study](https://github.com/cbonitz/doppler-experiments) found it has no effect once software normalizes each frequency, because the tone and the noise around it drop together.
- **Notching out the tones** is offered by [one tool](https://github.com/Bodila51/colsto-leak-and-mask), but its effectiveness hasn't been measured.
- **Strip file metadata** too, since it can hold GPS and the date.
- **Remember the picture:** signs and buildings can give away a place on their own.

## Credits

Method demonstrated by [Colsto](https://www.youtube.com/watch?v=UytYrhYLOKI). Written up by [Simon Willison](https://simonwillison.net/2026/Nov/3/a-sound-that-leaks-your-exact-location/). Flight data: [ADSB.lol contributors](https://www.adsb.lol/docs/open-data/historical), [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/). More sources are in the [research review](research.md).
