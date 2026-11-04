#!/usr/bin/env python3
"""The core idea of Doppler geolocation in about 100 lines. Needs only NumPy.

It invents a hidden microphone position and two straight flight paths, works out
the pitch curve the microphone would hear, then searches a map grid for the spot
that explains those curves. No audio, network, or real flight data is involved.

    python examples/minimal_doppler_match.py

This shows the geometry, not real-world accuracy: the curves come from the same
model that the search uses, so a good answer here proves very little.
"""
from __future__ import annotations

import numpy as np

SOUND_SPEED = 340.0  # metres per second


def aircraft_position(track: dict, times: np.ndarray) -> np.ndarray:
    """Position (east, north, up) in metres of a straight, level flight."""
    elapsed = (times - track["t0"])[:, None]
    return np.asarray(track["start"], float) + elapsed * np.asarray(track["velocity"], float)


def received_ratio(track: dict, microphone: np.ndarray, heard_at: np.ndarray) -> np.ndarray:
    """How much the pitch is shifted when sound reaches the microphone at `heard_at`.

    Sound heard now left the aircraft earlier, so we first find the emission time
    by repeating: emission = heard - distance / speed of sound.
    """
    emitted_at = heard_at.copy()
    for _ in range(6):
        distance = np.linalg.norm(aircraft_position(track, emitted_at) - microphone, axis=1)
        emitted_at = heard_at - distance / SOUND_SPEED
    offset = aircraft_position(track, emitted_at) - microphone
    distance = np.linalg.norm(offset, axis=1)
    receding_speed = (offset @ np.asarray(track["velocity"], float)) / distance
    return SOUND_SPEED / (SOUND_SPEED + receding_speed)


def misfit(track: dict, microphone: np.ndarray, heard_at: np.ndarray, measured_hz: np.ndarray) -> float:
    """Typical pitch error in Hz at this spot, after choosing the best engine tone."""
    ratio = received_ratio(track, microphone, heard_at)
    # The engine's true tone is unknown, so take the one that fits this spot best.
    source_hz = np.median(measured_hz / ratio)
    return float(np.median(np.abs(measured_hz - source_hz * ratio)))


def search(tracks: list[dict], curves: list[np.ndarray], heard_at: np.ndarray, half_width_m: float, step_m: float):
    """Score every grid point. Lower is better."""
    axis = np.arange(-half_width_m, half_width_m + step_m, step_m)
    scores = np.zeros((axis.size, axis.size))
    for north_index, north in enumerate(axis):
        for east_index, east in enumerate(axis):
            spot = np.array([east, north, 0.0])
            scores[north_index, east_index] = np.mean(
                [misfit(track, spot, heard_at, curve) for track, curve in zip(tracks, curves)]
            )
    return axis, scores


def best_spot(axis: np.ndarray, scores: np.ndarray) -> tuple[np.ndarray, float]:
    north_index, east_index = np.unravel_index(np.argmin(scores), scores.shape)
    return np.array([axis[east_index], axis[north_index], 0.0]), float(scores[north_index, east_index])


def main() -> None:
    rng = np.random.default_rng(4)
    hidden_microphone = np.array([1800.0, -2600.0, 0.0])
    heard_at = np.linspace(0.0, 150.0, 120)

    # Two planes on different headings, each passing the microphone to one side.
    first = {"start": (-4500.0, -300.0, 1200.0), "velocity": (90.0, 0.0, 0.0), "t0": 0.0}
    second = {"start": (4400.0, -8500.0, 900.0), "velocity": (0.0, 75.0, 0.0), "t0": 0.0}
    curves = [
        tone * received_ratio(track, hidden_microphone, heard_at) + rng.normal(0.0, 0.3, heard_at.size)
        for track, tone in ((first, 134.0), (second, 180.0))
    ]

    print(f"Hidden microphone: east {hidden_microphone[0]:.0f} m, north {hidden_microphone[1]:.0f} m\n")

    axis, one_plane = search([first], curves[:1], heard_at, 6000.0, 200.0)
    spot, error = best_spot(axis, one_plane)
    mirror = np.array([spot[0], 2 * (-300.0) - spot[1], 0.0])
    mirror_error = misfit(first, mirror, heard_at, curves[0])
    print("One plane:")
    print(f"  best spot east {spot[0]:.0f} m, north {spot[1]:.0f} m, error {error:.2f} Hz")
    print(f"  mirror spot across the flight path, north {mirror[1]:.0f} m, error {mirror_error:.2f} Hz")
    print("  Both sides of the path fit about equally well, so one plane cannot choose.\n")

    axis, two_planes = search([first, second], curves, heard_at, 6000.0, 200.0)
    spot, error = best_spot(axis, two_planes)
    miss = float(np.linalg.norm(spot - hidden_microphone))
    print("Two planes on different headings:")
    print(f"  best spot east {spot[0]:.0f} m, north {spot[1]:.0f} m, error {error:.2f} Hz")
    print(f"  {miss:.0f} m from the hidden microphone (grid spacing is 200 m)")


if __name__ == "__main__":
    main()
