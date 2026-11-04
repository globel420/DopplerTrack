from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment

ADSB_AERO_QUERY_URL = "https://adsb.aero/api/v1/query"
ADSB_AERO_LIMIT = 50
MAX_PROVIDER_RESPONSE_BYTES = 8 * 1024 * 1024
SOUND_SPEED_MPS = 343.0
SLOWEST_SOUND_SPEED_MPS = 295.0
LAPSE_RATE_K_PER_M = 0.0065
TROPOPAUSE_K = 216.65
MAX_SOUND_RANGE_KM = 60.0
MIN_CURVE_SPAN_SECONDS = 3.0
MIN_CURVE_SAMPLES = 12
MAX_MATCH_RELATIVE_ERROR = 0.015

ATTRIBUTION = {
    "provider": "adsb.aero historical trajectory query API",
    "source": "ADSB.lol historical ADS-B archive",
    "license": "Open Database License (ODbL) 1.0",
    "attribution": "Historical ADS-B data from ADSB.lol, queried through adsb.aero; licensed under ODbL 1.0.",
    "source_url": "https://www.adsb.lol/docs/open-data/historical",
    "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
}


class FlightDataError(Exception):
    """A bounded historical-flight query could not be completed safely."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class SearchArea:
    center_lat: float
    center_lon: float
    radius_km: float


@dataclass(frozen=True)
class SiteConditions:
    """What the matcher assumes about the air and ground at the microphone."""

    ground_temp_c: float = 15.0
    ground_elevation_m: float = 0.0


DEFAULT_SITE = SiteConditions()


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def validate_search_area(center_lat: Any, center_lon: Any, radius_km: Any) -> SearchArea:
    lat = _finite_number(center_lat)
    lon = _finite_number(center_lon)
    radius = _finite_number(radius_km)
    if lat is None or not -90 <= lat <= 90:
        raise ValueError("Search center latitude must be between -90 and 90 degrees.")
    if lon is None or not -180 <= lon <= 180:
        raise ValueError("Search center longitude must be between -180 and 180 degrees.")
    if radius is None or not 0.5 <= radius <= 50:
        raise ValueError("Search radius must be between 0.5 and 50 km.")
    return SearchArea(lat, lon, radius)


def validate_site_conditions(ground_temp_c: Any = None, ground_elevation_m: Any = None) -> SiteConditions:
    """Blank values fall back to a standard 15 C day at sea level."""
    temperature = DEFAULT_SITE.ground_temp_c
    elevation = DEFAULT_SITE.ground_elevation_m
    if ground_temp_c not in (None, ""):
        temperature = _finite_number(ground_temp_c)
        if temperature is None or not -60 <= temperature <= 60:
            raise ValueError("Air temperature must be between -60 and 60 degrees C.")
    if ground_elevation_m not in (None, ""):
        elevation = _finite_number(ground_elevation_m)
        if elevation is None or not -500 <= elevation <= 6000:
            raise ValueError("Ground height must be between -500 and 6000 metres.")
    return SiteConditions(temperature, elevation)


def sound_speed_along_path(site: SiteConditions, altitude_m: np.ndarray) -> np.ndarray:
    """Average sound speed between the ground and the aircraft, for a standard temperature lapse."""
    ground_k = site.ground_temp_c + 273.15
    height_m = np.maximum(np.asarray(altitude_m, dtype=float) - site.ground_elevation_m, 0.0)
    aircraft_k = np.maximum(ground_k - LAPSE_RATE_K_PER_M * height_m, min(TROPOPAUSE_K, ground_k))
    return 20.05 * (math.sqrt(ground_k) + np.sqrt(aircraft_k)) / 2


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Recording time must include a UTC offset.")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_query_payload(
    recording_datetime: str,
    duration_seconds: float,
    area: SearchArea,
    time_uncertainty_seconds: int,
) -> dict[str, Any]:
    try:
        recording_start = datetime.fromisoformat(recording_datetime)
    except ValueError as exc:
        raise ValueError("Recording time must be ISO 8601 with a UTC offset.") from exc
    if recording_start.tzinfo is None or recording_start.utcoffset() is None:
        raise ValueError("Recording time must include a UTC offset.")
    if not math.isfinite(float(duration_seconds)) or duration_seconds <= 0 or duration_seconds > 600:
        raise ValueError("Recording duration is outside the supported range.")
    if not 1 <= int(time_uncertainty_seconds) <= 600:
        raise ValueError("Time uncertainty must be between 1 and 600 seconds.")

    start = recording_start.astimezone(UTC)
    query_from = start - timedelta(seconds=int(time_uncertainty_seconds) + 180)
    query_to = start + timedelta(seconds=float(duration_seconds) + int(time_uncertainty_seconds))
    flight_floor = query_from - timedelta(days=6)
    # Search a single broad circle. The extra radius admits flights whose sound
    # could reach the stated receiver area; candidate receivers remain inside
    # the user's smaller search circle.
    query_radius_m = round((area.radius_km + MAX_SOUND_RANGE_KM) * 1000)
    return {
        "match": {
            "trajectory_intersects": {
                "geometry": {
                    "type": "Circle",
                    "coordinates": [area.center_lon, area.center_lat],
                    "radius": query_radius_m,
                },
                "time_from": _iso_utc(query_from),
                "time_to": _iso_utc(query_to),
            }
        },
        "end_date": _iso_utc(query_to + timedelta(seconds=1)),
        "start_from": _iso_utc(flight_floor),
        "window_days": 7,
        "limit": ADSB_AERO_LIMIT,
        "include_path": True,
    }


def _read_json_limited(response) -> dict[str, Any]:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > MAX_PROVIDER_RESPONSE_BYTES:
                raise FlightDataError("Historical flight data response exceeded the size limit.")
        except ValueError:
            raise FlightDataError("Historical flight data response had an invalid size header.")
    raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
        raise FlightDataError("Historical flight data response exceeded the size limit.")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FlightDataError("Historical flight data response was not valid JSON.") from exc
    if not isinstance(body, dict):
        raise FlightDataError("Historical flight data response had an unexpected format.")
    return body


def query_historical_tracks(
    recording_datetime: str,
    duration_seconds: float,
    area: SearchArea,
    time_uncertainty_seconds: int,
) -> tuple[list[dict[str, Any]], bool]:
    payload = build_query_payload(recording_datetime, duration_seconds, area, time_uncertainty_seconds)
    request = urllib.request.Request(
        ADSB_AERO_QUERY_URL,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "DopplerTrack/0.1 (historical query; ADSB.lol ODbL attribution)",
        },
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=12) as response:
            body = _read_json_limited(response)
    except urllib.error.HTTPError as exc:
        # Avoid returning provider response text, which can contain query context.
        raise FlightDataError(f"Historical flight service returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FlightDataError("Historical flight service did not respond within the time limit.") from exc

    flights = body.get("flights")
    if not isinstance(flights, list):
        raise FlightDataError("Historical flight service returned an unexpected result.")
    parsed = []
    for flight in flights[:ADSB_AERO_LIMIT]:
        if not isinstance(flight, dict):
            continue
        track = parse_flight_track(flight)
        if track:
            parsed.append(track)
    # A non-null cursor means the returned set was truncated. We deliberately
    # do not paginate a broad query; incomplete coverage cannot support a location.
    return parsed, bool(body.get("cursor")) or len(flights) >= ADSB_AERO_LIMIT


def _split_segments(flight: dict[str, Any]) -> list[list[tuple[float, float, float, float]]]:
    path = flight.get("path")
    timestamps = flight.get("timestamps")
    if not isinstance(path, dict) or path.get("type") != "MultiLineString":
        return []
    coordinates = path.get("coordinates")
    if not isinstance(coordinates, list) or not isinstance(timestamps, list):
        return []
    segments = []
    for coords, times in zip(coordinates, timestamps):
        if not isinstance(coords, list) or not isinstance(times, list) or len(coords) != len(times):
            continue
        current: list[tuple[float, float, float, float]] = []
        for coordinate, timestamp in zip(coords, times):
            if not isinstance(coordinate, (list, tuple)) or len(coordinate) < 3:
                if len(current) >= 2:
                    segments.append(current)
                current = []
                continue
            lon, lat, altitude_ft = (_finite_number(item) for item in coordinate[:3])
            stamp = _finite_number(timestamp)
            if (
                lon is None or not -180 <= lon <= 180
                or lat is None or not -90 <= lat <= 90
                or altitude_ft is None or not -2000 <= altitude_ft <= 70000
                or stamp is None
            ):
                if len(current) >= 2:
                    segments.append(current)
                current = []
                continue
            point = (stamp, lat, lon, altitude_ft)
            if current and (stamp <= current[-1][0] or stamp - current[-1][0] > 60):
                if len(current) >= 2:
                    segments.append(current)
                current = []
            current.append(point)
        if len(current) >= 2:
            segments.append(current)
    return segments


def parse_flight_track(flight: dict[str, Any]) -> dict[str, Any] | None:
    segments = _split_segments(flight)
    if not segments:
        return None
    flight_id = str(flight.get("flight_id") or flight.get("icao24") or "")[:100]
    if not flight_id:
        return None
    return {
        "flight_id": flight_id,
        "callsign": str(flight.get("callsign") or "")[:24],
        "icao_type": str(flight.get("icao_type") or "")[:12],
        "emitter_category": str(flight.get("emitter_category") or "")[:8],
        "segments": segments,
    }


def geodetic_to_ecef(lat_deg: float, lon_deg: float, altitude_m: float) -> np.ndarray:
    # WGS84 ellipsoid; altitude is ellipsoidal here. ADS-B pressure altitude is
    # only approximate, so candidate outputs remain explicitly experimental.
    a = 6378137.0
    e2 = 6.69437999014e-3
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    normal = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
    return np.array([
        (normal + altitude_m) * cos_lat * math.cos(lon),
        (normal + altitude_m) * cos_lat * math.sin(lon),
        (normal * (1.0 - e2) + altitude_m) * sin_lat,
    ], dtype=float)


def prepare_segments(flights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared = []
    for flight in flights:
        for segment in flight.get("segments", []):
            if len(segment) < 2:
                continue
            stamps = np.asarray([item[0] for item in segment], dtype=float)
            positions = np.vstack([
                geodetic_to_ecef(item[1], item[2], item[3] * 0.3048)
                for item in segment
            ])
            if not np.all(np.diff(stamps) > 0):
                continue
            velocities = np.gradient(positions, stamps, axis=0, edge_order=1)
            if np.any(~np.isfinite(velocities)) or np.nanmax(np.linalg.norm(velocities, axis=1)) > 800:
                continue
            prepared.append({
                "flight_id": flight["flight_id"],
                "callsign": flight.get("callsign", ""),
                "icao_type": flight.get("icao_type", ""),
                "emitter_category": flight.get("emitter_category", ""),
                "times": stamps,
                "altitudes_m": np.asarray([item[3] * 0.3048 for item in segment], dtype=float),
                "positions": positions,
                "velocities": velocities,
            })
    return prepared


def predict_doppler_ratio(
    receiver: np.ndarray,
    arrival_times: np.ndarray,
    segment: dict[str, Any],
    site: SiteConditions = DEFAULT_SITE,
) -> tuple[np.ndarray, np.ndarray]:
    times = segment["times"]
    positions = segment["positions"]
    velocities = segment["velocities"]
    altitudes = segment["altitudes_m"]
    emission_times = np.asarray(arrival_times, dtype=float).copy()
    aircraft = np.empty((len(emission_times), 3), dtype=float)
    speed = np.empty_like(aircraft)
    for _ in range(4):
        for axis in range(3):
            aircraft[:, axis] = np.interp(emission_times, times, positions[:, axis])
        distance = np.linalg.norm(aircraft - receiver, axis=1)
        sound_speed = sound_speed_along_path(site, np.interp(emission_times, times, altitudes))
        emission_times = arrival_times - distance / sound_speed
    valid = (emission_times >= times[0]) & (emission_times <= times[-1])
    for axis in range(3):
        aircraft[:, axis] = np.interp(emission_times, times, positions[:, axis])
        speed[:, axis] = np.interp(emission_times, times, velocities[:, axis])
    sound_speed = sound_speed_along_path(site, np.interp(emission_times, times, altitudes))
    relative = aircraft - receiver
    distance = np.linalg.norm(relative, axis=1)
    radial_velocity = np.sum(speed * relative, axis=1) / np.maximum(distance, 1.0)
    valid &= np.isfinite(radial_velocity) & (np.abs(radial_velocity) < 450)
    ratio = sound_speed / (sound_speed + radial_velocity)
    valid &= np.isfinite(ratio) & (ratio > 0.2) & (ratio < 2.0)
    return ratio, valid


def score_curve_against_segment(
    curve: dict[str, Any],
    recording_start_epoch: float,
    receiver: np.ndarray,
    segment: dict[str, Any],
    clock_offset_seconds: float,
    site: SiteConditions = DEFAULT_SITE,
) -> dict[str, float] | None:
    measurements = curve.get("measurements")
    if not isinstance(measurements, list) or len(measurements) < MIN_CURVE_SAMPLES:
        return None
    values = np.asarray(measurements, dtype=float)
    if values.ndim != 2 or values.shape[1] < 2:
        return None
    observed_times, observed_hz = values[:, 0], values[:, 1]
    if np.any(~np.isfinite(observed_times)) or np.any(~np.isfinite(observed_hz)):
        return None
    if observed_times[-1] - observed_times[0] < MIN_CURVE_SPAN_SECONDS or np.any(observed_hz <= 0):
        return None
    arrivals = recording_start_epoch + clock_offset_seconds + observed_times
    ratio, valid = predict_doppler_ratio(receiver, arrivals, segment, site)
    if np.count_nonzero(valid) < MIN_CURVE_SAMPLES:
        return None
    times = observed_times[valid]
    if times[-1] - times[0] < MIN_CURVE_SPAN_SECONDS:
        return None
    source_hz = float(np.median(observed_hz[valid] / ratio[valid]))
    residual_hz = observed_hz[valid] - source_hz * ratio[valid]
    relative_error = float(np.median(np.abs(residual_hz)) / max(float(np.median(observed_hz[valid])), 1.0))
    return {
        "source_tone_hz": source_hz,
        "median_error_hz": float(np.median(np.abs(residual_hz))),
        "median_relative_error": relative_error,
        "samples": int(np.count_nonzero(valid)),
    }


def _grid(center_lat: float, center_lon: float, radius_km: float, step_km: float) -> list[tuple[float, float]]:
    coords: set[tuple[int, int]] = set()
    n = max(2, math.ceil(radius_km / step_km))
    n = min(n, 12)
    actual_step = radius_km / n
    for north_i in range(-n, n + 1):
        for east_i in range(-n, n + 1):
            north = north_i * actual_step
            east = east_i * actual_step
            if math.hypot(north, east) <= radius_km + 1e-6:
                coords.add((round(north * 1000), round(east * 1000)))
    result = []
    cos_lat = max(abs(math.cos(math.radians(center_lat))), 0.1)
    for north_m, east_m in sorted(coords):
        lat = center_lat + north_m / 111_320_000
        lon = center_lon + east_m / (111_320_000 * cos_lat)
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            result.append((lat, lon))
    if not result:
        result.append((center_lat, center_lon))
    return result


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _offsets(uncertainty_seconds: int) -> list[float]:
    step = 5 if uncertainty_seconds <= 60 else 10 if uncertainty_seconds <= 180 else 20
    count = min(61, max(3, math.ceil(2 * uncertainty_seconds / step) + 1))
    return [float(value) for value in np.linspace(-uncertainty_seconds, uncertainty_seconds, count)]


def _score_hypothesis(
    curves: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    start_epoch: float,
    point: tuple[float, float],
    offset: float,
    site: SiteConditions = DEFAULT_SITE,
) -> dict[str, Any]:
    receiver = geodetic_to_ecef(point[0], point[1], site.ground_elevation_m)
    flights_by_id: dict[str, list[int]] = {}
    for segment_i, segment in enumerate(segments):
        flights_by_id.setdefault(segment["flight_id"], []).append(segment_i)
    flight_ids = list(flights_by_id)
    costs = np.full((len(curves), len(flight_ids)), 1.0, dtype=float)
    evidence: dict[tuple[int, int], tuple[int, dict[str, float]]] = {}
    for curve_i, curve in enumerate(curves):
        for flight_i, flight_id in enumerate(flight_ids):
            best_fit: tuple[int, dict[str, float]] | None = None
            for segment_i in flights_by_id[flight_id]:
                fit = score_curve_against_segment(curve, start_epoch, receiver, segments[segment_i], offset, site)
                if fit and (best_fit is None or fit["median_relative_error"] < best_fit[1]["median_relative_error"]):
                    best_fit = (segment_i, fit)
            if best_fit:
                costs[curve_i, flight_i] = best_fit[1]["median_relative_error"]
                evidence[(curve_i, flight_i)] = best_fit
    if costs.size == 0:
        return {"point": point, "offset": offset, "matches": [], "score": 1.0}
    rows, columns = linear_sum_assignment(costs)
    matches = []
    for row, col in zip(rows, columns):
        error = float(costs[row, col])
        if error > MAX_MATCH_RELATIVE_ERROR:
            continue
        segment_i, fit = evidence[(row, col)]
        segment = segments[segment_i]
        flight_id = flight_ids[col]
        matches.append({
            "flight_id": flight_id,
            "callsign": segment.get("callsign", ""),
            "icao_type": segment.get("icao_type", ""),
            "curve_index": int(curves[row].get("candidate_index", row)),
            "median_relative_error": error,
            "median_error_hz": float(fit.get("median_error_hz", 0.0)),
            "samples": int(fit.get("samples", 0)),
            "estimated_source_tone_hz": float(fit.get("source_tone_hz", 0.0)),
        })
    matches.sort(key=lambda item: item["median_relative_error"])
    score = (-len(matches), float(np.median([m["median_relative_error"] for m in matches])) if matches else 1.0)
    return {"point": point, "offset": offset, "matches": matches, "score": score}


def rank_location_hypotheses(
    curves: list[dict[str, Any]],
    flights: list[dict[str, Any]],
    recording_datetime: str,
    area: SearchArea,
    time_uncertainty_seconds: int,
    site: SiteConditions = DEFAULT_SITE,
    checkpoint: Callable[[], None] | None = None,
) -> dict[str, Any]:
    try:
        recording_start = datetime.fromisoformat(recording_datetime)
    except ValueError as exc:
        raise ValueError("Recording time must be ISO 8601 with a UTC offset.") from exc
    if recording_start.tzinfo is None or recording_start.utcoffset() is None:
        raise ValueError("Recording time must include a UTC offset.")
    if not 1 <= int(time_uncertainty_seconds) <= 600:
        raise ValueError("Time uncertainty must be between 1 and 600 seconds.")

    usable_curves = [
        curve for curve in curves
        if isinstance(curve.get("measurements"), list)
        and len(curve["measurements"]) >= MIN_CURVE_SAMPLES
        and curve["measurements"][-1][0] - curve["measurements"][0][0] >= MIN_CURVE_SPAN_SECONDS
    ][:6]
    if not usable_curves:
        return {
            "status": "insufficient_evidence",
            "location": None,
            "matches": [],
            "reason": "At least one measurable frequency curve and a usable historical track are needed.",
        }

    start_epoch = recording_start.astimezone(UTC).timestamp()
    curve_start = min(float(curve["measurements"][0][0]) for curve in usable_curves)
    curve_end = max(float(curve["measurements"][-1][0]) for curve in usable_curves)
    emission_start = start_epoch + curve_start - int(time_uncertainty_seconds) - MAX_SOUND_RANGE_KM * 1000 / SLOWEST_SOUND_SPEED_MPS
    emission_end = start_epoch + curve_end + int(time_uncertainty_seconds)
    prepared = [
        segment for segment in prepare_segments(flights)
        if segment["times"][-1] >= emission_start and segment["times"][0] <= emission_end
    ]
    if not prepared:
        return {
            "status": "insufficient_evidence",
            "location": None,
            "matches": [],
            "reason": "No historical trajectory segment overlaps the recording time and sound-travel window.",
        }
    spacing = area.radius_km / 2
    points = _grid(area.center_lat, area.center_lon, area.radius_km, spacing)
    best_by_point: dict[tuple[float, float], dict[str, Any]] = {}

    for refinement in range(3):
        for point in points:
            if point in best_by_point:
                continue
            if checkpoint:
                checkpoint()
            existing = None
            for offset in _offsets(time_uncertainty_seconds):
                hypothesis = _score_hypothesis(usable_curves, prepared, start_epoch, point, offset, site)
                if existing is None or hypothesis["score"] < existing["score"]:
                    existing = hypothesis
            if existing:
                best_by_point[point] = existing
        best = min(best_by_point.values(), key=lambda item: item["score"])
        if refinement < 2:
            spacing /= 2
            local_radius = spacing * 2
            cos_lat = max(abs(math.cos(math.radians(best["point"][0]))), 0.1)
            for north_i in range(-2, 3):
                for east_i in range(-2, 3):
                    north_km = north_i * spacing
                    east_km = east_i * spacing
                    if math.hypot(north_km, east_km) > local_radius + 1e-9:
                        continue
                    candidate = (
                        best["point"][0] + north_km / 111.32,
                        best["point"][1] + east_km / (111.32 * cos_lat),
                    )
                    if _distance_km((area.center_lat, area.center_lon), candidate) <= area.radius_km:
                        points.append(candidate)
            # Avoid repeat work while preserving deterministic ordering.
            points = list(dict.fromkeys(points))

    ranked = sorted(best_by_point.values(), key=lambda item: item["score"])
    best = ranked[0]
    matches = best["matches"]
    distinct_flights = len({item["flight_id"] for item in matches})
    reason = ""
    location = None
    status = "insufficient_evidence"
    if distinct_flights < 2:
        reason = "Fewer than two independent aircraft tracks matched; this recording does not constrain a location reliably."
    else:
        best_error = float(best["score"][1])
        separated_alternative = next(
            (
                candidate for candidate in ranked[1:]
                if len(candidate["matches"]) >= distinct_flights
                and _distance_km(candidate["point"], best["point"]) >= max(1.0, spacing * 2)
            ),
            None,
        )
        if best_error > MAX_MATCH_RELATIVE_ERROR:
            reason = "The best shared-location Doppler fit exceeded the conservative residual threshold."
        elif separated_alternative and separated_alternative["score"][1] <= max(best_error * 1.5, best_error + 0.005):
            reason = "Multiple separated locations fit nearly as well; location remains undetermined."
        else:
            status = "experimental_candidate"
            location = {
                "latitude": round(best["point"][0], 6),
                "longitude": round(best["point"][1], 6),
                "grid_resolution_km": round(spacing, 2),
                "recording_clock_offset_seconds": round(float(best["offset"]), 1),
                "matches_required": 2,
                "matched_aircraft_count": distinct_flights,
            }
            reason = "An experimental location candidate met the multi-track fit gates. This model has not been validated on independent known-location recordings."

    return {
        "status": status,
        "location": location,
        "matches": matches,
        "reason": reason,
        "best_median_relative_error": round(float(best["score"][1]), 6) if matches else None,
        "data_truncated": False,
        "attribution": ATTRIBUTION,
        "site_conditions": {
            "ground_temp_c": site.ground_temp_c,
            "ground_elevation_m": site.ground_elevation_m,
        },
        "limitations": [
            "Location scores are fit residuals, not probabilities or calibrated confidence intervals.",
            "The search uses pressure-altitude ADS-B positions, one ground height, and a standard temperature profile from the stated ground temperature; terrain, wind, clock drift, reflections, aircraft maneuvering, and source-tone changes are not fully modeled.",
            "A result is an experimental candidate only; independent known-location validation is still required.",
            "A missing ADS-B track does not show that no aircraft was present.",
        ],
    }


def match_recording(
    curves: list[dict[str, Any]],
    recording_datetime: str | None,
    area_values: dict[str, Any] | None,
    duration_seconds: float,
    time_uncertainty_seconds: int,
    consent: bool,
    enabled: bool,
    request_permit: Callable[[], bool] | None = None,
    site: dict[str, Any] | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> dict[str, Any]:
    base = {"provider": ATTRIBUTION["provider"], "attribution": ATTRIBUTION}
    if not consent:
        return {
            **base,
            "status": "not_requested",
            "location": None,
            "matches": [],
            "reason": "Historical lookup was not requested. Audio measurements remain available without sending the recording time or search area to an external service.",
        }
    if not recording_datetime or not area_values:
        return {
            **base,
            "status": "needs_input",
            "location": None,
            "matches": [],
            "reason": "Enter an explicit recording time with UTC offset and a latitude, longitude, and search radius before requesting historical flight data.",
        }
    if not enabled:
        return {
            **base,
            "status": "provider_disabled",
            "location": None,
            "matches": [],
            "reason": "The server operator has not enabled the historical data connector yet.",
        }
    if not math.isfinite(float(duration_seconds)) or duration_seconds > 600:
        return {
            **base,
            "status": "duration_not_supported",
            "location": None,
            "matches": [],
            "reason": "Historical flight matching currently supports clips up to 10 minutes. No external flight query was sent for this longer recording.",
        }
    usable_curve = any(
        isinstance(curve.get("measurements"), list)
        and len(curve["measurements"]) >= MIN_CURVE_SAMPLES
        and curve["measurements"][-1][0] - curve["measurements"][0][0] >= MIN_CURVE_SPAN_SECONDS
        for curve in curves
    )
    if not usable_curve:
        return {
            **base,
            "status": "insufficient_audio_evidence",
            "location": None,
            "matches": [],
            "reason": "No measured ridge has enough duration and samples for historical matching; no external query was sent.",
        }
    try:
        area = validate_search_area(
            area_values.get("center_lat"), area_values.get("center_lon"), area_values.get("radius_km")
        )
        conditions = validate_site_conditions(
            (site or {}).get("ground_temp_c"), (site or {}).get("ground_elevation_m")
        )
        if request_permit and not request_permit():
            return {
                **base,
                "status": "rate_limited",
                "location": None,
                "matches": [],
                "reason": "Historical lookup is temporarily rate-limited; no external query was sent.",
            }
        flights, truncated = query_historical_tracks(
            recording_datetime, duration_seconds, area, time_uncertainty_seconds
        )
    except (ValueError, FlightDataError) as exc:
        return {
            **base,
            "status": "provider_unavailable",
            "location": None,
            "matches": [],
            "reason": str(exc),
        }
    if truncated:
        return {
            **base,
            "status": "incomplete_data",
            "location": None,
            "matches": [],
            "reason": "The historical service returned a truncated result page. No location was estimated from incomplete track coverage.",
            "data_truncated": True,
        }
    if not flights:
        return {
            **base,
            "status": "no_tracks_returned",
            "location": None,
            "matches": [],
            "reason": "The query returned no usable historical tracks. This does not establish that no aircraft was present.",
            "data_truncated": False,
        }
    result = rank_location_hypotheses(
        curves, flights, recording_datetime, area, time_uncertainty_seconds, conditions, checkpoint
    )
    return {**base, **result, "flight_track_count": len(flights)}
