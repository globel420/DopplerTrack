from __future__ import annotations

import base64
import json
import math
import os
import struct
import subprocess
import time
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
from scipy.signal import find_peaks, stft

from app import settings
from app.geolocation import match_recording


class Cancelled(Exception):
    pass


class AnalysisError(Exception):
    pass


def _check(cancelled: Callable[[], bool], started: float) -> None:
    if cancelled():
        raise Cancelled
    if time.monotonic() - started > settings.ANALYSIS_TIMEOUT_SECONDS:
        raise AnalysisError("Analysis timed out. Try a shorter recording.")


def probe_media(path: Path) -> dict:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,codec_name,duration,width,height,sample_rate,channels",
                "-of", "json", str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        metadata = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        raise AnalysisError("This file could not be read as a supported video.") from exc

    streams = metadata.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not video or not video.get("width") or not video.get("height"):
        raise AnalysisError("The uploaded file does not contain a readable video stream.")
    try:
        duration = float(metadata.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    if not math.isfinite(duration) or duration <= 0:
        raise AnalysisError("The video duration could not be determined.")
    if duration < 1.0:
        raise AnalysisError("The video is shorter than the one-second minimum.")
    if duration > settings.MAX_DURATION_SECONDS:
        raise AnalysisError(f"The video exceeds the {settings.MAX_DURATION_SECONDS // 60}-minute duration limit.")
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    return {
        "duration_seconds": round(duration, 3),
        "has_audio": audio is not None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "sample_rate": int(audio.get("sample_rate", 0)) if audio and audio.get("sample_rate") else None,
        "channels": int(audio.get("channels", 0)) if audio and audio.get("channels") else None,
        "video_codec": video.get("codec_name"),
    }


def decode_audio(
    path: Path,
    cancelled: Callable[[], bool],
    started: float,
    sample_rate: int = 22050,
) -> np.ndarray:
    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-filter_threads", "1", "-threads", "1",
        "-i", str(path), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "f32le", "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            _check(cancelled, started)
            try:
                raw, _ = process.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode != 0:
            raise AnalysisError("Audio decoding failed for this video.")
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        raise
    if not raw:
        raise AnalysisError("No decodable audio was found.")
    audio = np.frombuffer(raw[: len(raw) - (len(raw) % 4)], dtype="<f4").copy()
    if audio.size < sample_rate:
        raise AnalysisError("The audio track is shorter than one second.")
    return np.nan_to_num(audio, copy=False, nan=0.0, posinf=0.0, neginf=0.0)


def _spectrum(
    audio: np.ndarray,
    sample_rate: int = 22050,
    nperseg: int = 4096,
    overlap_fraction: float = 0.75,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    noverlap = int(nperseg * overlap_fraction)
    frequencies, times, transformed = stft(
        audio,
        fs=sample_rate,
        nperseg=nperseg,
        noverlap=noverlap,
        boundary=None,
        padded=False,
    )
    magnitude = np.abs(transformed)
    del transformed
    np.maximum(magnitude, 1e-8, out=magnitude)
    np.log10(magnitude, out=magnitude)
    magnitude *= 20
    return frequencies, times, magnitude


def _decode_pcm_file(
    path: Path,
    pcm_path: Path,
    sample_rate: int,
    cancelled: Callable[[], bool],
    started: float,
) -> int:
    """Decode mono float PCM to disk so hour-long clips do not fill worker RAM."""
    pcm_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-filter_threads", "1", "-threads", "1",
        "-i", str(path), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "f32le", "pipe:1",
    ]
    process: subprocess.Popen | None = None
    try:
        descriptor = os.open(pcm_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.DEVNULL)
            while process.poll() is None:
                _check(cancelled, started)
                time.sleep(0.25)
        if process.returncode != 0:
            raise AnalysisError("Audio decoding failed for this video.")
        size = pcm_path.stat().st_size
        if size % 4:
            raise AnalysisError("Audio decoding returned incomplete sample data.")
        sample_count = size // 4
        if sample_count < sample_rate:
            raise AnalysisError("The audio track is shorter than one second.")
        return sample_count
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        pcm_path.unlink(missing_ok=True)
        raise


def _spectrogram_blocks(
    path: Path,
    pcm_path: Path,
    sample_rate: int,
    nperseg: int,
    overlap_fraction: float,
    cancelled: Callable[[], bool],
    started: float,
) -> Iterator[tuple[int, int, np.ndarray, np.ndarray, np.ndarray, np.ndarray, int, int]]:
    """Yield at most one minute of audio/spectrum at a time, with global times."""
    sample_count = _decode_pcm_file(path, pcm_path, sample_rate, cancelled, started)
    hop = nperseg - int(nperseg * overlap_fraction)
    core_frames = max(1, int(60 * sample_rate // hop))
    core_samples = core_frames * hop
    duration = sample_count / sample_rate
    mapped: np.memmap | None = None
    try:
        mapped = np.memmap(pcm_path, dtype="<f4", mode="r", shape=(sample_count,))
        total_blocks = math.ceil(sample_count / core_samples)
        for block_index, start_sample in enumerate(range(0, sample_count, core_samples)):
            _check(cancelled, started)
            end_sample = min(sample_count, start_sample + core_samples + nperseg - hop)
            audio = np.array(mapped[start_sample:end_sample], dtype=np.float32, copy=True)
            frequencies, local_times, db = _spectrum(
                audio, sample_rate=sample_rate, nperseg=nperseg,
                overlap_fraction=overlap_fraction,
            )
            offset = start_sample / sample_rate
            global_times = local_times + offset
            core_end = min(duration, (start_sample + core_samples) / sample_rate)
            keep = global_times <= core_end + 1e-9
            global_times = global_times[keep]
            db = db[:, keep]
            core_count = min(core_samples, sample_count - start_sample)
            yield block_index, total_blocks, audio, frequencies, global_times, db, start_sample, core_count
            del audio, db, global_times
    finally:
        if mapped is not None:
            del mapped
        pcm_path.unlink(missing_ok=True)


def _preview_parts(
    frequencies: np.ndarray,
    times: np.ndarray,
    db: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mask = (frequencies >= 100) & (frequencies <= 5000)
    selected_frequencies = frequencies[mask]
    rows = db[mask]
    if rows.size == 0:
        return selected_frequencies, times[:0], rows
    row_step = max(1, math.ceil(rows.shape[0] / 260))
    rows = rows[::row_step]
    column_step = max(1, math.ceil(rows.shape[1] / 24))
    return selected_frequencies[::row_step], times[::column_step], rows[:, ::column_step].astype(np.float32)


def _robust_line(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    design = np.column_stack((x, np.ones_like(x)))
    coefficients = np.linalg.lstsq(design, y, rcond=None)[0]
    for _ in range(5):
        residuals = y - design @ coefficients
        center = np.median(residuals)
        scale = 1.4826 * np.median(np.abs(residuals - center))
        if not math.isfinite(scale) or scale < 1e-8:
            break
        normalized = np.abs(residuals - center) / (1.345 * scale)
        weights = np.ones_like(normalized)
        outliers = normalized > 1
        weights[outliers] = 1 / normalized[outliers]
        root_weights = np.sqrt(weights)
        coefficients = np.linalg.lstsq(
            design * root_weights[:, None], y * root_weights, rcond=None
        )[0]
    return float(coefficients[0]), float(coefficients[1])


def _ridge_candidates(
    frequencies: np.ndarray,
    times: np.ndarray,
    db: np.ndarray,
    min_frequency: float = 150.0,
    max_frequency: float = 5000.0,
    prominence_db: float = 7.0,
    max_tracks: int = 48,
    include_measurements: bool = False,
) -> list[dict]:
    allowed = np.flatnonzero((frequencies >= min_frequency) & (frequencies <= max_frequency))
    tracks: list[dict] = []
    hop_dt = float(np.median(np.diff(times))) if len(times) > 1 else 0.1
    for frame_index, timestamp in enumerate(times):
        frame = db[allowed, frame_index]
        if frame.size < 3:
            continue
        peaks, properties = find_peaks(frame, prominence=prominence_db, distance=2)
        if not len(peaks):
            continue
        order = np.argsort(properties["prominences"])[::-1][:10]
        peaks = peaks[order]
        prominences = properties["prominences"][order]
        current = [(float(frequencies[allowed[index]]), float(prom)) for index, prom in zip(peaks, prominences)]
        active = [
            track for track in tracks
            if timestamp - track["times"][-1] <= max(0.6, 4 * hop_dt)
        ]
        for frequency, prominence in current:
            choices = [
                (abs(frequency - track["frequencies"][-1]), track)
                for track in active
                if abs(frequency - track["frequencies"][-1]) <= max(25.0, track["frequencies"][-1] * 0.025)
                and track["times"][-1] < timestamp
            ]
            if choices:
                _, track = min(choices, key=lambda item: item[0])
                if track.get("last_frame") == frame_index:
                    continue
                track["times"].append(float(timestamp))
                track["frequencies"].append(frequency)
                track["prominences"].append(prominence)
                track["last_frame"] = frame_index
            elif len(tracks) < max_tracks:
                tracks.append({
                    "times": [float(timestamp)],
                    "frequencies": [frequency],
                    "prominences": [prominence],
                    "last_frame": frame_index,
                })

    output: list[dict] = []
    for track in tracks:
        if len(track["times"]) < 12:
            continue
        x = np.asarray(track["times"], dtype=float)
        y = np.asarray(track["frequencies"], dtype=float)
        duration = float(x[-1] - x[0])
        if duration < 1.0:
            continue
        slope, intercept = _robust_line(x, y)
        fitted = slope * x + intercept
        ss_res = float(np.sum((y - fitted) ** 2))
        ss_tot = float(np.sum((y - np.mean(y)) ** 2))
        r_squared = 1.0 - ss_res / ss_tot if ss_tot > 1e-9 else 0.0
        summary = {
            "start_seconds": round(float(x[0]), 2),
            "end_seconds": round(float(x[-1]), 2),
            "duration_seconds": round(duration, 2),
            "start_hz": round(float(y[0]), 1),
            "end_hz": round(float(y[-1]), 1),
            "slope_hz_per_second": round(float(slope), 3),
            "fit_r_squared": round(float(r_squared), 3),
            "median_prominence_db": round(float(np.median(track["prominences"])), 1),
            "observations": len(x),
        }
        if include_measurements:
            indices = np.linspace(0, len(x) - 1, min(96, len(x)), dtype=int)
            summary["measurements"] = [
                [round(float(x[index]), 3), round(float(y[index]), 2)] for index in indices
            ]
        output.append(summary)
    output.sort(key=lambda item: (item["duration_seconds"], item["median_prominence_db"]), reverse=True)
    return output[:12]


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


# Sampled from a perceptually ordered dark-to-bright ramp. Display only.
_PLOT_RAMP = np.array([
    (0, 0, 4), (40, 11, 84), (101, 21, 110), (159, 42, 99),
    (212, 72, 66), (245, 125, 21), (250, 193, 39), (252, 255, 164),
], dtype=float)


def _spectrogram_svg(frequencies: np.ndarray, times: np.ndarray, db: np.ndarray) -> str:
    """Render a log-frequency plot; aircraft tones sit low, where a linear axis hides them."""
    frequency_mask = (frequencies >= 100) & (frequencies <= 5000)
    rows = db[frequency_mask]
    if rows.size == 0:
        return "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 800 200\"></svg>"
    max_width, max_height = 900, 260
    selected = frequencies[frequency_mask]
    targets = np.geomspace(100.0, 5000.0, max_height)
    row_indices = np.clip(np.searchsorted(selected, targets), 0, len(selected) - 1)
    col_step = max(1, math.ceil(rows.shape[1] / max_width))
    reduced = rows[row_indices][:, ::col_step]
    # Scale to the recording's own peak so quiet clips stay legible; true silence stays dark.
    top = max(float(np.percentile(reduced, 99.7)), -60.0)
    norm = np.clip((reduced - (top - 60.0)) / 60.0, 0.0, 1.0)
    height, width = reduced.shape
    stops = np.linspace(0.0, 1.0, len(_PLOT_RAMP))
    rgb = np.stack(
        [np.interp(norm, stops, _PLOT_RAMP[:, channel]) for channel in range(3)], axis=-1
    ).astype(np.uint8)[::-1]
    scanlines = b"".join(b"\x00" + rgb[row].tobytes() for row in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(scanlines, level=6))
        + _png_chunk(b"IEND", b"")
    )
    png_data = base64.b64encode(png).decode("ascii")
    duration = float(times[-1]) if len(times) else 0.0
    if not math.isfinite(duration):
        duration = 0.0
    total_height = max_height + 38
    span = math.log(5000.0 / 100.0)
    ticks = "".join(
        f'<line x1="0" x2="{max_width}" y1="{y:.1f}" y2="{y:.1f}" stroke="#ffffff" stroke-opacity=".16"/>'
        f'<text x="8" y="{max(y - 5, 13):.1f}" fill="#ffffff" font-size="12" paint-order="stroke" '
        f'stroke="#000000" stroke-width="3">{label}</text>'
        for label, y in (
            (name, max_height * (1 - math.log(hz / 100.0) / span))
            for name, hz in (("200 Hz", 200), ("500 Hz", 500), ("1 kHz", 1000), ("2 kHz", 2000), ("5 kHz", 5000))
        )
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {max_width} {total_height}" font-family="sans-serif" role="img" '
        f'aria-label="Audio spectrogram, 100 to 5000 hertz over {duration:.1f} seconds">'
        '<rect width="100%" height="100%" fill="#0b0e14"/>'
        f'<image x="0" y="0" width="{max_width}" height="{max_height}" preserveAspectRatio="none" '
        f'href="data:image/png;base64,{png_data}"/>'
        f'{ticks}'
        f'<text x="8" y="{max_height + 24}" fill="#c9d1d9" font-size="12">0 s</text>'
        f'<text x="{max_width / 2}" y="{max_height + 24}" fill="#c9d1d9" font-size="12" text-anchor="middle">time</text>'
        f'<text x="{max_width - 8}" y="{max_height + 24}" fill="#c9d1d9" font-size="12" text-anchor="end">{duration:.0f} s</text>'
        '</svg>'
    )


def _write_svg(path: Path, svg: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")


def _independent_curve_candidates(candidates: list[dict]) -> list[dict]:
    """Collapse detected harmonic groups so one pass cannot count twice."""
    adjacency = {index: set() for index in range(len(candidates))}
    for index, item in enumerate(candidates):
        for partner in item.get("harmonic_partner_indexes", []):
            if isinstance(partner, int) and 0 <= partner < len(candidates):
                adjacency[index].add(partner)
                adjacency[partner].add(index)
    selected = []
    visited: set[int] = set()
    for start in range(len(candidates)):
        if start in visited:
            continue
        group = {start}
        pending = [start]
        visited.add(start)
        while pending:
            current = pending.pop()
            for partner in adjacency[current] - visited:
                visited.add(partner)
                group.add(partner)
                pending.append(partner)
        chosen = max(
            group,
            key=lambda candidate_index: (
                candidates[candidate_index]["median_prominence_db"],
                candidates[candidate_index]["duration_seconds"],
            ),
        )
        selected.append(candidates[chosen])
    return selected


def _annotate_harmonic_candidates(candidates: list[dict]) -> list[dict]:
    """Mark possible harmonics and return one representative per tonal family."""
    for item in candidates:
        item["harmonic_relation"] = "no consistent harmonic partner detected"
    for i, first in enumerate(candidates):
        for j in range(i + 1, len(candidates)):
            second = candidates[j]
            overlap_start = max(first["start_seconds"], second["start_seconds"])
            overlap_end = min(first["end_seconds"], second["end_seconds"])
            if overlap_end - overlap_start < 1.0:
                continue

            def fitted(track: dict, at: float) -> float:
                return track["start_hz"] + track["slope_hz_per_second"] * (at - track["start_seconds"])

            ratios = []
            for at in (overlap_start, overlap_end):
                f1 = fitted(first, at)
                f2 = fitted(second, at)
                ratios.append(max(f1, f2) / max(min(f1, f2), 1e-6))
            harmonic_numbers = [round(ratio) for ratio in ratios]
            if (
                harmonic_numbers[0] in {2, 3, 4}
                and harmonic_numbers[0] == harmonic_numbers[1]
                and all(abs(ratio - harmonic_numbers[0]) <= 0.06 for ratio in ratios)
            ):
                note = f"possible {harmonic_numbers[0]}:1 ratio across overlap"
                first["harmonic_relation"] = note
                second["harmonic_relation"] = note
                first.setdefault("harmonic_partner_indexes", []).append(j)
                second.setdefault("harmonic_partner_indexes", []).append(i)
    for index, item in enumerate(candidates):
        item["candidate_index"] = index
    return _independent_curve_candidates(candidates)


def _run_quick_windowed(
    path: Path,
    metadata: dict,
    report_dir: Path,
    cancelled: Callable[[], bool],
    started: float,
    progress: Callable[[int], None] | None,
) -> dict:
    sample_rate = 22050
    sample_total = 0
    squared_total = 0.0
    clipped_total = 0
    tracks: list[dict] = []
    energy_parts: list[np.ndarray] = []
    preview_freqs: np.ndarray | None = None
    preview_times: list[np.ndarray] = []
    preview_db: list[np.ndarray] = []
    raw_path = report_dir / ".quick-audio.f32"
    for index, total, audio, frequencies, times, db, _start, core_count in _spectrogram_blocks(
        path, raw_path, sample_rate, 2048, 0.5, cancelled, started
    ):
        core_audio = audio[:core_count]
        sample_total += core_count
        squared_total += float(np.sum(core_audio.astype(np.float64) ** 2))
        clipped_total += int(np.count_nonzero(np.abs(core_audio) >= 0.999))
        tracks.extend(_ridge_candidates(frequencies, times, db, prominence_db=9.0))
        band_mask = (frequencies >= 150) & (frequencies <= 5000)
        if np.any(band_mask) and times.size:
            band_energy = np.mean(np.exp2(db[band_mask] / 6.0206), axis=0)
            energy_parts.append(band_energy)
        freq_part, time_part, db_part = _preview_parts(frequencies, times, db)
        if db_part.size:
            if preview_freqs is None:
                preview_freqs = freq_part
            preview_times.append(time_part)
            preview_db.append(db_part)
        if progress:
            progress(20 + round(55 * (index + 1) / total))
    if not sample_total:
        raise AnalysisError("No decodable audio was found.")

    rms = math.sqrt(squared_total / sample_total)
    rms_dbfs = 20 * math.log10(max(rms, 1e-8))
    clipped_fraction = clipped_total / sample_total
    moving = [
        item for item in tracks
        if item["duration_seconds"] >= 3.0
        and abs(item["end_hz"] - item["start_hz"]) >= 15.0
        and item["fit_r_squared"] >= 0.72
    ]
    moving.sort(key=lambda item: (item["duration_seconds"], item["median_prominence_db"]), reverse=True)
    energy = np.concatenate(energy_parts) if energy_parts else np.array([])
    active_fraction = float(np.mean(energy > np.percentile(energy, 25))) if energy.size else 0.0
    quality_score = max(0, min(100, round(100 + rms_dbfs * 1.4 - clipped_fraction * 800 - max(0, 0.35 - active_fraction) * 80)))
    if rms_dbfs < -68:
        quality_label = "Very quiet"
    elif clipped_fraction > 0.02:
        quality_label = "Clipped / distorted"
    elif quality_score >= 65:
        quality_label = "Fair for screening"
    else:
        quality_label = "Weak or noisy"

    reasons = ["Audio track detected."]
    if moving:
        reasons.append("One or more smooth, shifting tonal ridges are present; these can also come from sirens, engines, or other sources.")
    else:
        reasons.append("No clear smooth, shifting tonal ridge met the conservative screening threshold.")
    if clipped_fraction > 0.02:
        reasons.append("Clipping may obscure frequency measurements.")
    if rms_dbfs < -68:
        reasons.append("The audio level is very low; the recording may not contain enough usable detail.")
    if preview_freqs is None or not preview_times:
        raise AnalysisError("No usable audio frames were found.")
    plot = _spectrogram_svg(preview_freqs, np.concatenate(preview_times), np.concatenate(preview_db, axis=1))
    _write_svg(report_dir / "quick.svg", plot)
    if progress:
        progress(90)
    return {
        "qualification": "likely_suitable" if moving and quality_score >= 45 and clipped_fraction <= 0.08 else "inconclusive",
        "audio_present": True,
        "signal_quality": {
            "label": quality_label,
            "score": quality_score,
            "rms_dbfs": round(rms_dbfs, 1),
            "clipped_fraction": round(clipped_fraction, 5),
            "frequency_resolution_hz": round(sample_rate / 2048, 2),
        },
        "candidate_features": moving[:5],
        "reasons": reasons,
        "limitations": [
            "This heuristic screens audio only; it does not identify an aircraft or prove a Doppler shift.",
            "Road vehicles, sirens, machinery, speech, wind, and compression artifacts can create similar features.",
            "This long recording was screened in one-minute windows; a ridge crossing a window boundary may appear as separate candidates.",
        ],
        "duration_seconds": metadata.get("duration_seconds"),
        "plot": "quick.svg",
    }


def run_quick(
    path: Path,
    metadata: dict,
    report_dir: Path,
    cancelled: Callable[[], bool],
    started: float,
    progress: Callable[[int], None] | None = None,
) -> dict:
    if not metadata.get("has_audio"):
        return {
            "qualification": "unsuitable",
            "audio_present": False,
            "signal_quality": {"label": "No audio track", "rms_dbfs": None, "clipped_fraction": None},
            "candidate_features": [],
            "reasons": ["No audio stream was found in the video."],
            "limitations": ["No aircraft or location conclusion can be made without usable audio."],
            "duration_seconds": metadata.get("duration_seconds"),
        }
    if float(metadata.get("duration_seconds") or 0) > 600:
        return _run_quick_windowed(path, metadata, report_dir, cancelled, started, progress)
    _check(cancelled, started)
    if progress:
        progress(20)
    audio = decode_audio(path, cancelled, started)
    if progress:
        progress(48)
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    rms_dbfs = 20 * math.log10(max(rms, 1e-8))
    clipped_fraction = float(np.mean(np.abs(audio) >= 0.999))
    frequencies, times, db = _spectrum(audio, sample_rate=22050, nperseg=2048, overlap_fraction=0.5)
    _check(cancelled, started)
    if progress:
        progress(72)
    tracks = _ridge_candidates(frequencies, times, db, prominence_db=9.0)
    if progress:
        progress(90)
    moving = [
        item for item in tracks
        if item["duration_seconds"] >= 3.0
        and abs(item["end_hz"] - item["start_hz"]) >= 15.0
        and item["fit_r_squared"] >= 0.72
    ]
    band_mask = (frequencies >= 150) & (frequencies <= 5000)
    band_energy = np.mean(np.exp2(db[band_mask] / 6.0206), axis=0) if np.any(band_mask) else np.array([])
    active_fraction = float(np.mean(band_energy > np.percentile(band_energy, 25))) if band_energy.size else 0.0
    quality_score = max(0, min(100, round(100 + rms_dbfs * 1.4 - clipped_fraction * 800 - max(0, 0.35 - active_fraction) * 80)))
    if rms_dbfs < -68:
        quality_label = "Very quiet"
    elif clipped_fraction > 0.02:
        quality_label = "Clipped / distorted"
    elif quality_score >= 65:
        quality_label = "Fair for screening"
    else:
        quality_label = "Weak or noisy"

    reasons: list[str] = ["Audio track detected."]
    if moving:
        reasons.append("One or more smooth, shifting tonal ridges are present; these can also come from sirens, engines, or other sources.")
    else:
        reasons.append("No clear smooth, shifting tonal ridge met the conservative screening threshold.")
    if clipped_fraction > 0.02:
        reasons.append("Clipping may obscure frequency measurements.")
    if rms_dbfs < -68:
        reasons.append("The audio level is very low; the recording may not contain enough usable detail.")

    qualification = "likely_suitable" if moving and quality_score >= 45 and clipped_fraction <= 0.08 else "inconclusive"
    svg = _spectrogram_svg(frequencies, times, db)
    _write_svg(report_dir / "quick.svg", svg)
    return {
        "qualification": qualification,
        "audio_present": True,
        "signal_quality": {
            "label": quality_label,
            "score": quality_score,
            "rms_dbfs": round(rms_dbfs, 1),
            "clipped_fraction": round(clipped_fraction, 5),
            "frequency_resolution_hz": round(22050 / 2048, 2),
        },
        "candidate_features": moving[:5],
        "reasons": reasons,
        "limitations": [
            "This heuristic screens audio only; it does not identify an aircraft or prove a Doppler shift.",
            "Road vehicles, sirens, machinery, speech, wind, and compression artifacts can create similar features.",
        ],
        "duration_seconds": metadata.get("duration_seconds"),
        "plot": "quick.svg",
    }


def _run_deep_windowed(
    path: Path,
    metadata: dict,
    report_dir: Path,
    recording_datetime: str | None,
    search_area: dict | None,
    time_uncertainty_seconds: int,
    flight_lookup_consent: bool,
    cancelled: Callable[[], bool],
    started: float,
    progress: Callable[[int], None] | None,
    request_permit: Callable[[], bool] | None,
) -> dict:
    native_rate = int(metadata.get("sample_rate") or 22050)
    sample_rate = min(max(native_rate, 8000), 22050)
    sample_total = 0
    squared_total = 0.0
    clipped_total = 0
    candidates: list[dict] = []
    preview_freqs: np.ndarray | None = None
    preview_times: list[np.ndarray] = []
    preview_db: list[np.ndarray] = []
    raw_path = report_dir / ".deep-audio.f32"
    for index, total, audio, frequencies, times, db, _start, core_count in _spectrogram_blocks(
        path, raw_path, sample_rate, 4096, 0.5, cancelled, started
    ):
        core_audio = audio[:core_count]
        sample_total += core_count
        squared_total += float(np.sum(core_audio.astype(np.float64) ** 2))
        clipped_total += int(np.count_nonzero(np.abs(core_audio) >= 0.999))
        tracks = _ridge_candidates(
            frequencies, times, db, prominence_db=7.0, max_tracks=64, include_measurements=True
        )
        candidates.extend(
            item for item in tracks
            if item["duration_seconds"] >= 2.0
            and abs(item["end_hz"] - item["start_hz"]) >= 4.0
            and item["fit_r_squared"] >= 0.55
        )
        freq_part, time_part, db_part = _preview_parts(frequencies, times, db)
        if db_part.size:
            if preview_freqs is None:
                preview_freqs = freq_part
            preview_times.append(time_part)
            preview_db.append(db_part)
        if progress:
            progress(18 + round(60 * (index + 1) / total))
    if not sample_total:
        raise AnalysisError("No decodable audio was found.")

    candidates.sort(key=lambda item: (item["duration_seconds"], item["median_prominence_db"]), reverse=True)
    candidates = candidates[:8]
    match_candidates = _annotate_harmonic_candidates(candidates)
    if preview_freqs is None or not preview_times:
        raise AnalysisError("No usable audio frames were found.")
    plot = _spectrogram_svg(preview_freqs, np.concatenate(preview_times), np.concatenate(preview_db, axis=1))
    _write_svg(report_dir / "deep.svg", plot)
    rms = math.sqrt(squared_total / sample_total)
    clipped = clipped_total / sample_total
    if progress:
        progress(84)
    flight_matching = match_recording(
        match_candidates,
        recording_datetime,
        search_area,
        float(metadata.get("duration_seconds") or 0),
        time_uncertainty_seconds,
        flight_lookup_consent,
        settings.ADSB_HISTORY_ENABLED,
        request_permit,
    )
    _check(cancelled, started)
    if progress:
        progress(96)
    return {
        "analysis": "experimental acoustic measurements",
        "duration_seconds": metadata.get("duration_seconds"),
        "recording_datetime": recording_datetime,
        "time_uncertainty_seconds": time_uncertainty_seconds,
        "search_area": search_area,
        "audio": {
            "sample_rate_hz": sample_rate,
            "channels_analyzed": 1,
            "rms_dbfs": round(20 * math.log10(max(rms, 1e-8)), 1),
            "clipped_fraction": round(clipped, 5),
            "stft_window_samples": 4096,
            "frequency_resolution_hz": round(sample_rate / 4096, 2),
            "hop_seconds": round(4096 / 2 / sample_rate, 4),
            "analysis_window_seconds": 60,
        },
        "candidate_tracks": candidates,
        "evidence_summary": (
            f"{len(candidates)} shifting tonal ridge candidate(s) met the stated thresholds across the full recording. "
            "A candidate is a measured spectral pattern, not a confirmed Doppler curve."
        ),
        "flight_matching": flight_matching,
        "assumptions": [
            "The uploaded file's audio stream is representative of the recording.",
            "Recording time, clock uncertainty, and search area are user-supplied. Upload time and upload location are never substituted.",
            "The one-hour recording was analyzed in consecutive 60-second windows at up to 22.05 kHz mono; measurements retain offsets from the clip start.",
        ],
        "uncertainty": [
            "Spectral ridges can be produced by sirens, traffic, machinery, wind, voices, and codec artifacts.",
            "A ridge crossing a 60-second window boundary can be reported as separate candidates.",
            "Historical flight matching currently supports recording windows of up to 10 minutes; no flight query is sent for longer videos.",
            "The analysis does not correct for microphone response, unknown recorder clock error, wind, or propagation conditions.",
        ],
        "plot": "deep.svg",
    }


def run_deep(
    path: Path,
    metadata: dict,
    report_dir: Path,
    recording_datetime: str | None,
    search_area: dict | None,
    time_uncertainty_seconds: int,
    flight_lookup_consent: bool,
    cancelled: Callable[[], bool],
    started: float,
    progress: Callable[[int], None] | None = None,
    request_permit: Callable[[], bool] | None = None,
) -> dict:
    _check(cancelled, started)
    if float(metadata.get("duration_seconds") or 0) > 600:
        return _run_deep_windowed(
            path, metadata, report_dir, recording_datetime, search_area,
            time_uncertainty_seconds, flight_lookup_consent, cancelled,
            started, progress, request_permit,
        )
    native_rate = int(metadata.get("sample_rate") or 22050)
    sample_rate = min(max(native_rate, 8000), 22050)
    if progress:
        progress(18)
    audio = decode_audio(path, cancelled, started, sample_rate=sample_rate)
    _check(cancelled, started)
    if progress:
        progress(48)
    frequencies, times, db = _spectrum(audio, sample_rate=sample_rate, nperseg=4096, overlap_fraction=0.5)
    tracks = _ridge_candidates(
        frequencies, times, db, prominence_db=7.0, max_tracks=64, include_measurements=True
    )
    if progress:
        progress(78)
    candidates = [
        item for item in tracks
        if item["duration_seconds"] >= 2.0
        and abs(item["end_hz"] - item["start_hz"]) >= 4.0
        and item["fit_r_squared"] >= 0.55
    ][:8]

    match_candidates = _annotate_harmonic_candidates(candidates)

    _check(cancelled, started)
    _write_svg(report_dir / "deep.svg", _spectrogram_svg(frequencies, times, db))
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    clipped = float(np.mean(np.abs(audio) >= 0.999))
    if progress:
        progress(84)
    flight_matching = match_recording(
        match_candidates,
        recording_datetime,
        search_area,
        float(metadata.get("duration_seconds") or 0),
        time_uncertainty_seconds,
        flight_lookup_consent,
        settings.ADSB_HISTORY_ENABLED,
        request_permit,
    )
    _check(cancelled, started)
    if progress:
        progress(96)
    return {
        "analysis": "experimental acoustic measurements",
        "duration_seconds": metadata.get("duration_seconds"),
        "recording_datetime": recording_datetime,
        "time_uncertainty_seconds": time_uncertainty_seconds,
        "search_area": search_area,
        "audio": {
            "sample_rate_hz": sample_rate,
            "channels_analyzed": 1,
            "rms_dbfs": round(20 * math.log10(max(rms, 1e-8)), 1),
            "clipped_fraction": round(clipped, 5),
            "stft_window_samples": 4096,
            "frequency_resolution_hz": round(sample_rate / 4096, 2),
            "hop_seconds": round(4096 / 2 / sample_rate, 4),
        },
        "candidate_tracks": candidates,
        "evidence_summary": (
            f"{len(candidates)} shifting tonal ridge candidate(s) met the stated thresholds. "
            "A candidate is a measured spectral pattern, not a confirmed Doppler curve."
        ),
        "flight_matching": flight_matching,
        "assumptions": [
            "The uploaded file's audio stream is representative of the recording.",
            "Recording time, clock uncertainty, and search area are user-supplied. Upload time and upload location are never substituted.",
            "Frequency tracks use mono audio at the source sample rate up to 48 kHz; higher-rate audio is resampled. The optional location stage applies a simplified sound-travel-time and aircraft-trajectory model.",
        ],
        "uncertainty": [
            "Spectral ridges can be produced by sirens, traffic, machinery, wind, voices, and codec artifacts.",
            "The analysis does not correct for microphone response, unknown recorder clock error, wind, or propagation conditions.",
            "The audio ridge detector does not confirm an aircraft. When enabled, the location search uses an approximate retarded-time Doppler model; it has not been validated on independent known-location recordings.",
        ],
        "plot": "deep.svg",
    }


def report_filename(job_id: str) -> str:
    return f"dopplertrack-report-{job_id[:8]}.json"


def safe_report_json(payload: dict) -> str:
    # Ensure there is no non-finite numeric value in an exported report.
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)
