from __future__ import annotations

import math
import unittest
from datetime import UTC, datetime
from unittest.mock import Mock, patch

import numpy as np

from app import geolocation


def _offset_coordinate(north_m: float, east_m: float, altitude_m: float) -> tuple[float, float, float]:
    lat = north_m / 111_320
    lon = east_m / 111_320
    return lat, lon, altitude_m / 0.3048


def _make_flight(flight_id: str, axis: str, cross_track_m: float, center_epoch: float, speed_mps: float) -> dict:
    times = np.arange(center_epoch - 120, center_epoch + 121, 1.0)
    points = []
    for stamp in times:
        along = (stamp - center_epoch) * speed_mps
        if axis == "east":
            north, east = cross_track_m, along
        else:
            north, east = along, cross_track_m
        lat, lon, altitude_ft = _offset_coordinate(north, east, 10000.0)
        points.append((float(stamp), lat, lon, altitude_ft))
    return {
        "flight_id": flight_id,
        "callsign": flight_id.upper(),
        "icao_type": "A320",
        "segments": [points],
    }


def _make_curve(flight: dict, record_epoch: float, clock_offset: float, start_s: float, tone_hz: float) -> dict:
    prepared = geolocation.prepare_segments([flight])[0]
    video_times = np.linspace(start_s, start_s + 8.0, 41)
    receiver = geolocation.geodetic_to_ecef(0.0, 0.0, 0.0)
    ratios, valid = geolocation.predict_doppler_ratio(
        receiver, record_epoch + clock_offset + video_times, prepared
    )
    if not np.all(valid):
        raise AssertionError("synthetic track should cover all retarded emission times")
    measurements = np.column_stack((video_times, tone_hz * ratios)).round(5).tolist()
    return {
        "start_seconds": start_s,
        "end_seconds": start_s + 8.0,
        "measurements": measurements,
    }


class DopplerGeolocationTests(unittest.TestCase):
    def test_search_area_and_provider_query_are_bounded_and_utc(self):
        area = geolocation.validate_search_area("38.5", "-121.4", "10")
        payload = geolocation.build_query_payload(
            "2026-11-04T14:32:00-08:00", 60, area, 30
        )
        constraint = payload["match"]["trajectory_intersects"]
        self.assertEqual(constraint["geometry"]["type"], "Circle")
        self.assertEqual(constraint["geometry"]["coordinates"], [-121.4, 38.5])
        self.assertEqual(constraint["geometry"]["radius"], 70000)
        self.assertEqual(constraint["time_from"], "2026-11-04T22:28:30Z")
        self.assertEqual(constraint["time_to"], "2026-11-04T22:33:30Z")
        self.assertEqual(payload["limit"], geolocation.ADSB_AERO_LIMIT)
        self.assertTrue(payload["include_path"])
        with self.assertRaises(ValueError):
            geolocation.validate_search_area(91, 0, 5)
        with self.assertRaises(ValueError):
            geolocation.validate_search_area(0, 0, 51)

    def test_flight_parser_keeps_track_gaps_separate(self):
        parsed = geolocation.parse_flight_track({
            "flight_id": "abc123:2026-11-04T00:00:00Z",
            "callsign": "TEST1",
            "path": {
                "type": "MultiLineString",
                "coordinates": [[[-120, 38, 30000], [-120.01, 38.01, 30100], [-120.02, 38.02, 30200]]],
            },
            "timestamps": [[1000, 1005, 1070]],
        })
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed["segments"]), 1)
        self.assertEqual(len(parsed["segments"][0]), 2)
        self.assertEqual(parsed["callsign"], "TEST1")

    def test_two_synthetic_tracks_can_form_only_an_experimental_candidate(self):
        recording = datetime(2026, 1, 1, tzinfo=UTC)
        epoch = recording.timestamp()
        clock_offset = 15.0
        # The first event is an east-west pass, the second a north-south pass.
        first_center = epoch + clock_offset + 4.0 - 10000.0 / geolocation.SOUND_SPEED_MPS
        second_center = epoch + clock_offset + 54.0 - 10000.0 / geolocation.SOUND_SPEED_MPS
        flights = [
            _make_flight("flight-one", "east", 12000, first_center, 210),
            _make_flight("flight-two", "north", -11000, second_center, 195),
        ]
        curves = [
            _make_curve(flights[0], epoch, clock_offset, 0.0, 880.0),
            _make_curve(flights[1], epoch, clock_offset, 50.0, 1320.0),
        ]
        area = geolocation.SearchArea(0.0, 0.0, 2.0)
        result = geolocation.rank_location_hypotheses(
            curves, flights, recording.isoformat(), area, 15
        )
        self.assertEqual(result["status"], "experimental_candidate", result)
        self.assertEqual(len({item["flight_id"] for item in result["matches"]}), 2)
        self.assertIn("ODbL) 1.0", geolocation.ATTRIBUTION["license"])
        self.assertLess(math.hypot(result["location"]["latitude"], result["location"]["longitude"]) * 111.32, 1.0)
        self.assertIn("not been validated", result["reason"])

    def test_one_flight_never_produces_a_location(self):
        recording = datetime(2026, 1, 1, tzinfo=UTC)
        epoch = recording.timestamp()
        flight = _make_flight("single-flight", "east", 12000, epoch - 20, 210)
        curve = _make_curve(flight, epoch, 0, 0.0, 880)
        result = geolocation.rank_location_hypotheses(
            [curve], [flight], recording.isoformat(), geolocation.SearchArea(0, 0, 2), 10
        )
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertIsNone(result["location"])

    def test_sound_speed_follows_temperature_and_height(self):
        cool = geolocation.sound_speed_along_path(geolocation.SiteConditions(15.0, 0.0), np.array([0.0]))[0]
        hot = geolocation.sound_speed_along_path(geolocation.SiteConditions(35.0, 0.0), np.array([0.0]))[0]
        high = geolocation.sound_speed_along_path(geolocation.SiteConditions(15.0, 0.0), np.array([10000.0]))[0]
        self.assertAlmostEqual(cool, 340.3, delta=0.5)
        self.assertGreater(hot, cool + 10)
        self.assertLess(high, cool - 15)

    def test_site_conditions_are_bounded_and_blank_means_standard(self):
        self.assertEqual(geolocation.validate_site_conditions("", ""), geolocation.DEFAULT_SITE)
        self.assertEqual(geolocation.validate_site_conditions("20", "1600"), geolocation.SiteConditions(20.0, 1600.0))
        with self.assertRaises(ValueError):
            geolocation.validate_site_conditions("61", "")
        with self.assertRaises(ValueError):
            geolocation.validate_site_conditions("", "nan")

    def test_stated_elevation_recovers_a_high_altitude_microphone(self):
        recording = datetime(2026, 1, 1, tzinfo=UTC)
        epoch = recording.timestamp()
        site = geolocation.SiteConditions(10.0, 1600.0)
        receiver = geolocation.geodetic_to_ecef(0.0, 0.0, site.ground_elevation_m)
        flight = _make_flight("one", "east", 3000, epoch + 20, 90)
        for point in range(len(flight["segments"][0])):
            stamp, lat, lon, _ = flight["segments"][0][point]
            flight["segments"][0][point] = (stamp, lat, lon, 3000.0 / 0.3048)
        segment = geolocation.prepare_segments([flight])[0]
        moments = np.linspace(0.0, 40.0, 41)
        ratios, _ = geolocation.predict_doppler_ratio(receiver, epoch + moments, segment, site)
        curve = {"measurements": np.column_stack((moments, 200.0 * ratios)).tolist()}
        right = geolocation.score_curve_against_segment(curve, epoch, receiver, segment, 0.0, site)
        wrong = geolocation.score_curve_against_segment(
            curve, epoch, geolocation.geodetic_to_ecef(0.0, 0.0, 0.0), segment, 0.0, geolocation.DEFAULT_SITE
        )
        self.assertLess(right["median_error_hz"], 1e-6)
        self.assertGreater(wrong["median_error_hz"], right["median_error_hz"] + 0.05)

    def test_search_never_scores_a_hypothesis_twice_and_can_be_stopped(self):
        recording = datetime(2026, 1, 1, tzinfo=UTC)
        epoch = recording.timestamp()
        flights = [_make_flight("a", "east", 3000, epoch + 4, 100), _make_flight("b", "north", 3000, epoch + 4, 100)]
        curve = _make_curve(flights[0], epoch, 0, 0.0, 300.0)
        seen = []
        original = geolocation._score_hypothesis

        def counting(curves, segments, start, point, offset, site):
            seen.append((point, offset))
            return original(curves, segments, start, point, offset, site)

        with patch.object(geolocation, "_score_hypothesis", counting):
            geolocation.rank_location_hypotheses(
                [curve], flights, recording.isoformat(), geolocation.SearchArea(0, 0, 2), 10
            )
        self.assertEqual(len(seen), len(set(seen)))

        class Stop(Exception):
            pass

        def stop():
            raise Stop

        with self.assertRaises(Stop):
            geolocation.rank_location_hypotheses(
                [curve], flights, recording.isoformat(), geolocation.SearchArea(0, 0, 2), 10, checkpoint=stop
            )

    def test_lookup_requires_explicit_consent_and_admin_enablement(self):
        with patch.object(geolocation, "query_historical_tracks") as query:
            result = geolocation.match_recording([], None, None, 20, 30, False, True)
            self.assertEqual(result["status"], "not_requested")
            result = geolocation.match_recording(
                [], "2026-01-01T00:00:00+00:00", {"center_lat": 0, "center_lon": 0, "radius_km": 2}, 20, 30, True, False
            )
            self.assertEqual(result["status"], "provider_disabled")
            query.assert_not_called()

    def test_truncated_provider_page_never_returns_location(self):
        area = geolocation.SearchArea(38.5, -121.4, 10)
        curve = {"measurements": [[float(i) / 3, 500.0 + i] for i in range(16)]}
        with patch.object(geolocation, "query_historical_tracks", return_value=([], True)) as query:
            result = geolocation.match_recording(
                [curve], "2026-11-04T22:32:00+00:00",
                {"center_lat": area.center_lat, "center_lon": area.center_lon, "radius_km": area.radius_km},
                30, 30, True, True,
            )
        self.assertEqual(result["status"], "incomplete_data")
        self.assertIsNone(result["location"])
        query.assert_called_once()

    def test_lookup_does_not_spend_provider_budget_without_curve_or_permit(self):
        area = {"center_lat": 38.5, "center_lon": -121.4, "radius_km": 10}
        timestamp = "2026-11-04T22:32:00+00:00"
        permit = Mock(return_value=False)
        with patch.object(geolocation, "query_historical_tracks") as query:
            no_curve = geolocation.match_recording(
                [], timestamp, area, 20, 30, True, True, permit
            )
            self.assertEqual(no_curve["status"], "insufficient_audio_evidence")
            permit.assert_not_called()
            curve = {"measurements": [[float(i) / 3, 500.0 + i] for i in range(16)]}
            rate_limited = geolocation.match_recording(
                [curve], timestamp, area, 20, 30, True, True, permit
            )
        self.assertEqual(rate_limited["status"], "rate_limited")
        query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
