from __future__ import annotations

import os
import unittest

import numpy as np

os.environ.setdefault("DOPPLERTRACK_ENV", "development")

from app import analysis

SAMPLE_RATE = 22050


def _flyover(seconds: int, onset: float, tone_hz: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    times = np.arange(seconds * SAMPLE_RATE) / SAMPLE_RATE
    along = 100.0 * (times - (onset + (seconds - onset) / 2))
    frequency = tone_hz * 343.0 / (343.0 + 100.0 * along / np.hypot(along, 3500.0))
    waveform = np.sin(np.cumsum(frequency) / SAMPLE_RATE * 2 * np.pi)
    waveform[times < onset] = 0.0
    return times, frequency, waveform


def _tracks(audio: np.ndarray) -> list[dict]:
    frequencies, times, db = analysis._spectrum(
        audio.astype(np.float32), sample_rate=SAMPLE_RATE, nperseg=4096, overlap_fraction=0.5
    )
    return analysis._ridge_candidates(frequencies, times, db, max_tracks=64, include_measurements=True)


def _true_curve(tracks: list[dict], times: np.ndarray, frequency: np.ndarray) -> tuple[float, float | None]:
    span, error = 0.0, None
    for track in tracks:
        values = np.asarray(track["measurements"])
        difference = np.median(np.abs(values[:, 1] - np.interp(values[:, 0], times, frequency)))
        if difference < 6 and track["duration_seconds"] > span:
            span, error = track["duration_seconds"], float(difference)
    return span, error


class RidgeDetectionTests(unittest.TestCase):
    def test_noise_before_a_flyover_does_not_hide_it(self):
        noise = np.random.default_rng(5).standard_normal(100 * SAMPLE_RATE) * 0.02
        times, frequency, waveform = _flyover(100, 30.0, 400.0)
        span, _ = _true_curve(_tracks(0.05 * waveform + noise), times, frequency)
        self.assertGreater(span, 60)

    def test_engine_tone_below_the_old_150_hz_floor_is_tracked(self):
        noise = np.random.default_rng(6).standard_normal(120 * SAMPLE_RATE) * 0.01
        times, frequency, waveform = _flyover(120, 0.0, 90.0)
        self.assertLess(frequency.max(), 150)
        span, _ = _true_curve(_tracks(0.1 * waveform + noise), times, frequency)
        self.assertGreater(span, 90)

    def test_frequency_is_measured_between_bins(self):
        noise = np.random.default_rng(7).standard_normal(120 * SAMPLE_RATE) * 0.002
        times, frequency, waveform = _flyover(120, 0.0, 300.0)
        _, error = _true_curve(_tracks(0.1 * waveform + noise), times, frequency)
        bin_width = SAMPLE_RATE / 4096
        self.assertIsNotNone(error)
        self.assertLess(error, bin_width / 10)


class HarmonicGroupingTests(unittest.TestCase):
    @staticmethod
    def _candidate(multiplier: float, prominence: float = 10.0) -> dict:
        moments = np.linspace(0.0, 60.0, 61)
        base = 100.0 + 40.0 * np.tanh((30.0 - moments) / 10.0)
        return {
            "start_seconds": 0.0,
            "end_seconds": 60.0,
            "duration_seconds": 60.0,
            "median_prominence_db": prominence,
            "measurements": np.column_stack((moments, base * multiplier)).tolist(),
        }

    def test_rational_harmonics_count_as_one_pass(self):
        for low, high in ((2, 3), (3, 4), (1, 2), (1, 3)):
            with self.subTest(low=low, high=high):
                kept = analysis._annotate_harmonic_candidates([self._candidate(low), self._candidate(high)])
                self.assertEqual(len(kept), 1)

    def test_tones_with_a_changing_ratio_stay_separate(self):
        sliding = self._candidate(2.0)
        moments = np.asarray(sliding["measurements"])[:, 0]
        sliding["measurements"] = np.column_stack((moments, 240.0 - moments)).tolist()
        kept = analysis._annotate_harmonic_candidates([self._candidate(1.0), sliding])
        self.assertEqual(len(kept), 2)


if __name__ == "__main__":
    unittest.main()
