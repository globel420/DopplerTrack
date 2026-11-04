from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import wave
import xml.etree.ElementTree as ET
from base64 import b64decode
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import httpx
import jwt
import numpy as np
from cryptography.hazmat.primitives.asymmetric import rsa

from app import access, analysis, egress_proxy, main, settings, worker
from app.url_import import download_page_video
from app.url_security import UnsafeMediaURL, resolve_public_host, validate_media_url


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.counts: dict[str, int] = {}

    def ping(self):
        return True

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = str(value)
        return True

    def setex(self, key, ttl, value):
        self.values[key] = str(value)
        return True

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, ttl):
        return key in self.values or key in self.counts

    def llen(self, key):
        return len(self.lists.get(key, []))

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(str(value))
        return len(self.lists[key])

    def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.values:
                del self.values[key]
                removed += 1
        return removed

    def eval(self, script, numkeys, key, expected):
        if self.values.get(key) == expected:
            del self.values[key]
            return 1
        return 0


def make_video(path: Path, mode: str, seconds: int = 8) -> None:
    sample_rate = 44100
    count = sample_rate * seconds
    samples = np.zeros(count, dtype=np.float32)
    if mode == "chirp":
        times = np.arange(count, dtype=np.float64) / sample_rate
        start_hz, end_hz = 420.0, 1050.0
        phase = 2 * math.pi * (start_hz * times + 0.5 * (end_hz - start_hz) / seconds * times**2)
        samples = (0.45 * np.sin(phase) + 0.12 * np.sin(2 * phase)).astype(np.float32)
    audio_path = path.with_suffix(".wav")
    pcm = np.clip(samples * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(audio_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())

    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        f"color=c=black:s=160x90:r=10:d={seconds}",
    ]
    if mode != "no-audio":
        command.extend(["-i", str(audio_path)])
    command.extend(["-t", str(seconds), "-map", "0:v:0"])
    if mode != "no-audio":
        command.extend(["-map", "1:a:0"])
    command.extend(["-c:v", "mpeg4", "-q:v", "7", "-pix_fmt", "yuv420p"])
    if mode != "no-audio":
        command.extend(["-c:a", "aac", "-b:a", "96k"])
    command.extend(["-shortest", "-movflags", "+faststart", "-y", str(path)])
    subprocess.run(command, check=True, capture_output=True, timeout=90)
    audio_path.unlink(missing_ok=True)


class DopplerTrackPipelineTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = Path(tempfile.mkdtemp(prefix="dopplertrack-fixtures-"))
        cls.chirp = cls.fixtures / "chirp.mp4"
        cls.silence = cls.fixtures / "silence.mp4"
        cls.no_audio = cls.fixtures / "no-audio.mp4"
        make_video(cls.chirp, "chirp")
        make_video(cls.silence, "silence")
        make_video(cls.no_audio, "no-audio")

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.fixtures, ignore_errors=True)

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.temp = tempfile.TemporaryDirectory(prefix="dopplertrack-test-")
        self.original_upload_dir = settings.UPLOAD_DIR
        self.original_data_dir = settings.DATA_DIR
        self.original_env = settings.APP_ENV
        self.original_max_upload = settings.MAX_UPLOAD_BYTES
        self.original_max_queue = settings.MAX_QUEUE
        settings.UPLOAD_DIR = str(Path(self.temp.name) / "uploads")
        settings.DATA_DIR = str(Path(self.temp.name) / "data")
        settings.APP_ENV = "development"
        settings.MAX_UPLOAD_BYTES = 4096 * 1024 * 1024
        settings.MAX_QUEUE = 5
        self.fake_redis = FakeRedis()
        self.redis_patches = [
            patch.object(main, "redis_client", self.fake_redis),
            patch.object(worker, "client", self.fake_redis),
        ]
        for item in self.redis_patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.redis_patches):
            item.stop()
        settings.UPLOAD_DIR = self.original_upload_dir
        settings.DATA_DIR = self.original_data_dir
        settings.APP_ENV = self.original_env
        settings.MAX_UPLOAD_BYTES = self.original_max_upload
        settings.MAX_QUEUE = self.original_max_queue
        self.temp.cleanup()
        logging.disable(logging.NOTSET)

    async def request(self, method: str, path: str, **kwargs):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    async def upload(self, fixture: Path):
        return await self.request(
            "POST",
            "/api/jobs",
            files={"video": (fixture.name, fixture.read_bytes(), "video/mp4")},
        )

    def read_job(self, job_id: str) -> dict:
        return json.loads(self.fake_redis.get(f"job:{job_id}"))

    async def test_quick_outcomes_and_deep_measurements(self):
        chirp_response = await self.upload(self.chirp)
        self.assertEqual(chirp_response.status_code, 200, chirp_response.text)
        chirp_id = chirp_response.json()["id"]
        worker.process_job(chirp_id)
        chirp_job = self.read_job(chirp_id)
        self.assertEqual(chirp_job["quick_result"]["qualification"], "likely_suitable")
        self.assertGreaterEqual(len(chirp_job["quick_result"]["candidate_features"]), 1)
        quick_plot = Path(settings.DATA_DIR) / chirp_id / "quick.svg"
        self.assertLess(quick_plot.stat().st_size, 2_000_000)
        svg_root = ET.fromstring(quick_plot.read_text(encoding="utf-8"))
        raster = svg_root.find("{http://www.w3.org/2000/svg}image")
        self.assertIsNotNone(raster)
        png_data = raster.attrib["href"].split(",", 1)[1]
        self.assertTrue(b64decode(png_data).startswith(b"\x89PNG\r\n\x1a\n"))

        silence_response = await self.upload(self.silence)
        self.assertEqual(silence_response.status_code, 200, silence_response.text)
        silence_id = silence_response.json()["id"]
        worker.process_job(silence_id)
        silence_job = self.read_job(silence_id)
        self.assertEqual(silence_job["quick_result"]["qualification"], "inconclusive")
        self.assertEqual((await self.request("POST", f"/api/jobs/{silence_id}/deep")).status_code, 409)

        deep_response = await self.request(
            "POST", f"/api/jobs/{silence_id}/deep", data={"manual_override": "true"}
        )
        self.assertEqual(deep_response.status_code, 200, deep_response.text)
        worker.process_job(silence_id)
        deep_job = self.read_job(silence_id)
        self.assertEqual(deep_job["status"], "completed")
        self.assertEqual(deep_job["deep_result"]["flight_matching"]["status"], "not_requested")
        self.assertIsNone(deep_job["deep_result"]["flight_matching"]["location"])
        self.assertIn("ODbL) 1.0", deep_job["deep_result"]["flight_matching"]["attribution"]["license"])
        report = await self.request("GET", f"/api/jobs/{silence_id}/report")
        self.assertIn("https://www.adsb.lol/docs/open-data/historical", report.text)
        self.assertIn("opendatacommons.org/licenses/odbl/1-0/", report.text)
        self.assertTrue((Path(settings.DATA_DIR) / silence_id / "deep.svg").is_file())
        self.assertLess((Path(settings.DATA_DIR) / silence_id / "deep.svg").stat().st_size, 2_000_000)

        no_audio_response = await self.upload(self.no_audio)
        self.assertEqual(no_audio_response.status_code, 200, no_audio_response.text)
        no_audio_id = no_audio_response.json()["id"]
        worker.process_job(no_audio_id)
        self.assertEqual(self.read_job(no_audio_id)["quick_result"]["qualification"], "unsuitable")

    async def test_upload_validation_size_url_and_cancellation(self):
        unsupported = await self.request(
            "POST", "/api/jobs", files={"video": ("video.avi", b"x", "video/x-msvideo")}
        )
        self.assertEqual(unsupported.status_code, 415)

        invalid = await self.request(
            "POST", "/api/jobs", files={"video": ("broken.mp4", b"not media", "video/mp4")}
        )
        self.assertEqual(invalid.status_code, 415)

        settings.MAX_UPLOAD_BYTES = 5
        oversized = await self.request(
            "POST", "/api/jobs", files={"video": ("large.mp4", b"123456", "video/mp4")}
        )
        self.assertEqual(oversized.status_code, 413)
        settings.MAX_UPLOAD_BYTES = self.original_max_upload

        blocked_url = await self.request("POST", "/api/url-import", data={"url": "http://127.0.0.1/secret"})
        self.assertEqual(blocked_url.status_code, 400)

        self.fake_redis.counts.clear()
        missing_offset = await self.request(
            "POST",
            "/api/jobs",
            data={"recording_datetime": "2026-11-04T14:32:00"},
            files={"video": (self.chirp.name, self.chirp.read_bytes(), "video/mp4")},
        )
        self.assertEqual(missing_offset.status_code, 400)

        self.fake_redis.counts.clear()
        missing_lookup_inputs = await self.request(
            "POST",
            "/api/jobs",
            data={"flight_lookup_consent": "true"},
            files={"video": (self.chirp.name, self.chirp.read_bytes(), "video/mp4")},
        )
        self.assertEqual(missing_lookup_inputs.status_code, 400)

        self.fake_redis.counts.clear()
        queued = await self.request(
            "POST",
            "/api/jobs",
            data={
                "recording_datetime": "2026-11-04T14:32:00-08:00",
                "center_lat": "38.5816",
                "center_lon": "-121.4944",
                "search_radius_km": "10",
                "time_uncertainty_seconds": "45",
            },
            files={"video": (self.chirp.name, self.chirp.read_bytes(), "video/mp4")},
        )
        self.assertEqual(queued.status_code, 200, queued.text)
        job_id = queued.json()["id"]
        self.assertEqual(queued.json()["recording_datetime"], "2026-11-04T22:32:00+00:00")
        self.assertEqual(queued.json()["search_area"], {
            "center_lat": 38.5816, "center_lon": -121.4944, "radius_km": 10.0,
        })
        self.assertEqual(queued.json()["time_uncertainty_seconds"], 45)
        self.assertFalse(queued.json()["flight_lookup_consent"])
        cancelled = await self.request("POST", f"/api/jobs/{job_id}/cancel")
        self.assertEqual(cancelled.status_code, 200)
        worker.process_job(job_id)
        self.assertEqual(self.read_job(job_id)["status"], "cancelled")

    async def test_resumable_upload_and_large_declared_size(self):
        original_disk_usage = main.shutil.disk_usage
        main.shutil.disk_usage = lambda _path: SimpleNamespace(free=30 * 1024 * 1024 * 1024)
        try:
            large = await self.request(
                "POST", "/api/uploads", json={"filename": "long-recording.mp4", "size": 4 * 1024 * 1024 * 1024}
            )
            self.assertEqual(large.status_code, 200, large.text)
            large_id = large.json()["id"]
            self.assertEqual(large.json()["chunk_count"], 128)
            self.assertEqual(
                (Path(settings.UPLOAD_DIR) / large_id / "source.mp4").stat().st_size,
                4 * 1024 * 1024 * 1024,
            )
            cancelled = await self.request("DELETE", f"/api/uploads/{large_id}")
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            self.assertFalse((Path(settings.UPLOAD_DIR) / large_id).exists())

            payload = self.chirp.read_bytes()
            session_response = await self.request(
                "POST", "/api/uploads", json={"filename": "chirp.mp4", "size": len(payload)}
            )
            self.assertEqual(session_response.status_code, 200, session_response.text)
            session = session_response.json()
            chunk_response = await self.request(
                "PUT",
                f"/api/uploads/{session['id']}/chunks/0",
                content=payload,
                headers={
                    "Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}",
                    "Content-Length": str(len(payload)),
                },
            )
            self.assertEqual(chunk_response.status_code, 200, chunk_response.text)
            duplicate = await self.request(
                "PUT",
                f"/api/uploads/{session['id']}/chunks/0",
                content=payload,
                headers={
                    "Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}",
                    "Content-Length": str(len(payload)),
                },
            )
            self.assertEqual(duplicate.status_code, 200, duplicate.text)
            completed = await self.request("POST", f"/api/uploads/{session['id']}/complete")
            self.assertEqual(completed.status_code, 200, completed.text)
            job_id = completed.json()["id"]
            self.assertEqual((Path(settings.UPLOAD_DIR) / job_id / "source.mp4").read_bytes(), payload)
            worker.process_job(job_id)
            self.assertIn("quick_result", self.read_job(job_id))
        finally:
            main.shutil.disk_usage = original_disk_usage

    async def test_long_video_limits_and_windowed_audio_path(self):
        original_disk_usage = main.shutil.disk_usage
        main.shutil.disk_usage = lambda _path: SimpleNamespace(free=30 * 1024 * 1024 * 1024)
        try:
            too_large = await self.request(
                "POST", "/api/uploads", json={"filename": "too-large.mp4", "size": settings.MAX_UPLOAD_BYTES + 1}
            )
            self.assertEqual(too_large.status_code, 413)
            invalid_range = await self.request(
                "POST", "/api/uploads", json={"filename": "bad-offset.mp4", "size": 100},
            )
            upload_id = invalid_range.json()["id"]
            broken_chunk = await self.request(
                "PUT", f"/api/uploads/{upload_id}/chunks/0", content=b"x" * 100,
                headers={"Content-Range": "bytes 0-99/101", "Content-Length": "100"},
            )
            self.assertEqual(broken_chunk.status_code, 400)
            cancelled = await self.request("DELETE", f"/api/uploads/{upload_id}")
            self.assertEqual(cancelled.status_code, 200)
        finally:
            main.shutil.disk_usage = original_disk_usage

        long_metadata = {
            "duration_seconds": 601.0,
            "has_audio": True,
            "sample_rate": 44100,
            "channels": 1,
        }
        report_dir = Path(settings.DATA_DIR) / "windowed-test"
        quick = analysis.run_quick(
            self.chirp, long_metadata, report_dir, lambda: False, time.monotonic()
        )
        self.assertTrue((report_dir / "quick.svg").is_file())
        self.assertIn("one-minute windows", " ".join(quick["limitations"]))

        previous_adsb = settings.ADSB_HISTORY_ENABLED
        settings.ADSB_HISTORY_ENABLED = True
        try:
            deep = analysis.run_deep(
                self.chirp,
                long_metadata,
                report_dir,
                "2026-11-04T14:32:00Z",
                {"center_lat": 38.5, "center_lon": -121.5, "radius_km": 5},
                30,
                True,
                lambda: False,
                time.monotonic(),
                request_permit=lambda: self.fail("Long recordings must not send an ADS-B query"),
            )
        finally:
            settings.ADSB_HISTORY_ENABLED = previous_adsb
        self.assertTrue((report_dir / "deep.svg").is_file())
        self.assertEqual(deep["flight_matching"]["status"], "duration_not_supported")
        self.assertIsNone(deep["flight_matching"]["location"])

    def test_probe_media_accepts_one_hour_and_rejects_more(self):
        response_duration = [3600]

        def fake_probe(*_args, **_kwargs):
            return SimpleNamespace(stdout=json.dumps({
                "format": {"duration": str(response_duration[0])},
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 160, "height": 90},
                    {"codec_type": "audio", "codec_name": "aac", "sample_rate": "44100", "channels": 1},
                ],
            }))

        old_limit = settings.MAX_DURATION_SECONDS
        settings.MAX_DURATION_SECONDS = 3600
        try:
            with patch.object(analysis.subprocess, "run", side_effect=fake_probe):
                accepted = analysis.probe_media(self.chirp)
                self.assertEqual(accepted["duration_seconds"], 3600.0)
                response_duration[0] = 3601
                with self.assertRaises(analysis.AnalysisError):
                    analysis.probe_media(self.chirp)
        finally:
            settings.MAX_DURATION_SECONDS = old_limit

    async def test_public_video_page_import_queues_and_runs_quick_scan(self):
        source_url = "https://video.example/watch?id=private-query-value"
        with patch.object(main, "validate_media_url", return_value=source_url):
            queued = await self.request("POST", "/api/url-import", data={"url": source_url})
        self.assertEqual(queued.status_code, 200, queued.text)
        job_id = queued.json()["id"]
        self.assertEqual(queued.json()["phase"], "import")
        self.assertEqual(queued.json()["source_type"], "url")
        self.assertNotIn(source_url, json.dumps(queued.json()))
        self.assertNotIn(source_url, self.fake_redis.get(f"job:{job_id}"))
        url_file = Path(settings.UPLOAD_DIR) / job_id / ".source_url"
        self.assertEqual(url_file.read_text(), source_url)

        def fake_download(_url, destination, _cancelled, _started, progress):
            path = destination / "source.mp4"
            path.write_bytes(self.chirp.read_bytes())
            progress(18)
            return path

        with patch.object(worker, "validate_media_url", return_value=source_url), patch.object(
            worker, "download_page_video", side_effect=fake_download
        ):
            worker.process_job(job_id)
        job = self.read_job(job_id)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["phase"], "quick")
        self.assertEqual(job["quick_result"]["qualification"], "likely_suitable")
        self.assertFalse(url_file.exists())

    def test_url_validator_blocks_private_and_rebinding_dns(self):
        for url in (
            "file:///etc/passwd",
            "http://127.0.0.1/video.mp4",
            "http://169.254.169.254/latest/meta-data/",
            "http://user:password@example.com/video.mp4",
            "https://example.com:8443/video.mp4",
        ):
            with self.subTest(url=url), self.assertRaises(UnsafeMediaURL):
                validate_media_url(url)

        public_answer = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
        with patch("app.url_security.socket.getaddrinfo", return_value=[public_answer]):
            normalized = validate_media_url("https://video.example/path#fragment")
        self.assertEqual(normalized, "https://video.example/path")
        with patch("app.url_security.socket.getaddrinfo", side_effect=AssertionError("worker must use the proxy DNS")):
            self.assertEqual(
                validate_media_url("https://video.example/path", resolve_dns=False),
                "https://video.example/path",
            )

        private_answer = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.7", 443))
        with (
            patch("app.url_security.socket.getaddrinfo", return_value=[public_answer, private_answer]),
            self.assertRaises(UnsafeMediaURL),
        ):
            resolve_public_host("video.example", 443)

    async def test_egress_proxy_rejects_loopback_connect(self):
        server = await asyncio.start_server(egress_proxy.handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"CONNECT 127.0.0.1:80 HTTP/1.1\r\nHost: 127.0.0.1:80\r\n\r\n")
            await writer.drain()
            response = await reader.readuntil(b"\r\n\r\n")
            self.assertIn(b"403 Forbidden", response)
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    async def test_egress_proxy_consumes_connect_headers_before_tunnel(self):
        received = bytearray()

        async def handle_upstream(reader, writer):
            received.extend(await reader.readexactly(11))
            writer.write(b"serverhello")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
        upstream_address = upstream.sockets[0].getsockname()
        proxy = await asyncio.start_server(egress_proxy.handle_client, "127.0.0.1", 0)
        proxy_port = proxy.sockets[0].getsockname()[1]

        async def fake_connect(_host, _port):
            return await asyncio.open_connection(upstream_address[0], upstream_address[1])

        try:
            with patch.object(egress_proxy, "_connect_public", side_effect=fake_connect):
                reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
                writer.write(
                    b"CONNECT example.com:443 HTTP/1.1\r\n"
                    b"Host: example.com:443\r\n"
                    b"Proxy-Authorization: Basic ignored\r\n\r\n"
                )
                await writer.drain()
                response = await reader.readuntil(b"\r\n\r\n")
                self.assertIn(b"200 Connection Established", response)
                writer.write(b"clienthello")
                await writer.drain()
                self.assertEqual(await reader.readexactly(11), b"serverhello")
                writer.close()
                await writer.wait_closed()
        finally:
            proxy.close()
            upstream.close()
            await proxy.wait_closed()
            await upstream.wait_closed()
        self.assertEqual(received, b"clienthello")

    def test_page_downloader_uses_proxy_limits_and_pinned_js_runtime(self):
        options_seen = {}
        target_dir = Path(self.temp.name) / "source"
        target_dir.mkdir()
        previous_proxy = settings.URL_IMPORT_PROXY
        settings.URL_IMPORT_PROXY = "http://url-egress:3128"

        class FakeYoutubeDL:
            def __init__(self, options):
                options_seen.update(options)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def download(self, urls):
                self.assert_urls = urls
                hooks = options_seen["progress_hooks"]
                hooks[0]({"status": "downloading", "downloaded_bytes": 10, "total_bytes": 20})
                path = Path(options_seen["outtmpl"].replace("%(ext)s", "mp4"))
                path.write_bytes(b"video")
                hooks[0]({"status": "finished", "downloaded_bytes": 5, "filename": str(path)})
                return 0

        fake_module = ModuleType("yt_dlp")
        fake_module.YoutubeDL = FakeYoutubeDL
        progress_values = []
        try:
            with patch.dict(sys.modules, {"yt_dlp": fake_module}):
                path = download_page_video(
                    "https://video.example/watch",
                    target_dir,
                    lambda: False,
                    time.monotonic(),
                    progress_values.append,
                )
        finally:
            settings.URL_IMPORT_PROXY = previous_proxy
        self.assertEqual(path.read_bytes(), b"video")
        self.assertEqual(options_seen["proxy"], "http://url-egress:3128")
        self.assertEqual(options_seen["format"], "bestvideo*+bestaudio/best")
        self.assertEqual(options_seen["merge_output_format"], "mkv")
        self.assertTrue(options_seen["noplaylist"])
        self.assertEqual(options_seen["max_filesize"], settings.MAX_UPLOAD_BYTES)
        self.assertEqual(options_seen["js_runtimes"]["deno"]["path"], "/usr/local/bin/deno")
        self.assertNotIn("cookies", options_seen)
        self.assertEqual(progress_values[-1], 18)

    async def test_deep_scan_accepts_location_details_after_quick_check(self):
        created = await self.upload(self.chirp)
        job_id = created.json()["id"]
        worker.process_job(job_id)

        missing = await self.request("POST", f"/api/jobs/{job_id}/deep", data={"flight_lookup_consent": "true"})
        self.assertEqual(missing.status_code, 400)
        self.assertFalse(self.read_job(job_id)["flight_lookup_consent"])

        no_offset = await self.request(
            "POST",
            f"/api/jobs/{job_id}/deep",
            data={
                "flight_lookup_consent": "true",
                "recording_datetime": "2026-06-01T12:00:00",
                "center_lat": "38.5",
                "center_lon": "-121.5",
                "search_radius_km": "10",
            },
        )
        self.assertEqual(no_offset.status_code, 400)

        accepted = await self.request(
            "POST",
            f"/api/jobs/{job_id}/deep",
            data={
                "flight_lookup_consent": "true",
                "recording_datetime": "2026-06-01T12:00:00-07:00",
                "center_lat": "38.5",
                "center_lon": "-121.5",
                "search_radius_km": "10",
                "time_uncertainty_seconds": "60",
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        job = self.read_job(job_id)
        self.assertEqual(job["phase"], "deep")
        self.assertTrue(job["flight_lookup_consent"])
        self.assertEqual(job["time_uncertainty_seconds"], 60)
        self.assertEqual(job["recording_datetime"], "2026-06-01T19:00:00+00:00")
        self.assertEqual(job["search_area"]["radius_km"], 10.0)
        worker.process_job(job_id)
        matching = self.read_job(job_id)["deep_result"]["flight_matching"]
        self.assertEqual(matching["status"], "provider_disabled")

    async def test_deep_scan_without_consent_field_keeps_no_lookup(self):
        created = await self.upload(self.chirp)
        job_id = created.json()["id"]
        worker.process_job(job_id)
        accepted = await self.request("POST", f"/api/jobs/{job_id}/deep")
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertFalse(self.read_job(job_id)["flight_lookup_consent"])
        self.assertIsNone(self.read_job(job_id)["search_area"])

    async def test_ui_routes_and_security_headers(self):
        home = await self.request("GET", "/")
        script = await self.request("GET", "/app.js")
        styles = await self.request("GET", "/styles.css")
        self.assertEqual(home.status_code, 200)
        self.assertIn("DopplerTrack", home.text)
        self.assertIn("flightLookupConsent", home.text)
        self.assertIn("ADSB.lol archive", home.text)
        self.assertIn("ODbL 1.0", home.text)
        self.assertIn("not the picture", home.text)
        self.assertIn("urlImportButton", home.text)
        self.assertIn("public video-page or direct media link", home.text)
        explainer = await self.request("GET", "/how-it-works")
        demo = await self.request("GET", "/how-it-works.js")
        self.assertEqual(explainer.status_code, 200)
        self.assertEqual(demo.status_code, 200)
        self.assertIn("t = τ + r(τ) / c", explainer.text)
        self.assertIn("DOPPLERGEIST", explainer.text)
        self.assertIn("ODbL 1.0", explainer.text)
        self.assertEqual(script.status_code, 200)
        self.assertEqual(styles.status_code, 200)
        self.assertIn("@media (max-width: 720px)", styles.text)
        self.assertEqual(home.headers.get("x-frame-options"), "DENY")
        self.assertIn("frame-ancestors 'none'", home.headers.get("content-security-policy", ""))

    def test_historical_provider_budget_is_shared_and_rate_limited(self):
        self.assertTrue(worker.reserve_adsb_query())
        self.assertFalse(worker.reserve_adsb_query())

    def test_harmonic_ridges_count_as_one_independent_pass(self):
        candidates = [
            {"candidate_index": 0, "harmonic_partner_indexes": [1], "median_prominence_db": 8, "duration_seconds": 12},
            {"candidate_index": 1, "harmonic_partner_indexes": [0], "median_prominence_db": 11, "duration_seconds": 9},
            {"candidate_index": 2, "median_prominence_db": 7, "duration_seconds": 6},
        ]
        selected = analysis._independent_curve_candidates(candidates)
        self.assertEqual([item["candidate_index"] for item in selected], [1, 2])

    async def test_access_identity_is_verified_and_jobs_are_isolated(self):
        settings.APP_ENV = "production"
        settings.CF_ACCESS_AUD = "test-aud"
        settings.CF_ACCESS_ISSUER = "https://test.cloudflareaccess.com"
        with patch.object(main, "verify_access_token", side_effect=lambda value: {"email": value}):
            missing = await self.request("GET", "/api/jobs/invalid")
            self.assertEqual(missing.status_code, 401)

            created = await self.request(
                "POST",
                "/api/jobs",
                headers={"Cf-Access-Jwt-Assertion": "alice@example.com"},
                files={"video": (self.chirp.name, self.chirp.read_bytes(), "video/mp4")},
            )
            self.assertEqual(created.status_code, 200, created.text)
            job_id = created.json()["id"]
            alice = await self.request(
                "GET", f"/api/jobs/{job_id}", headers={"Cf-Access-Jwt-Assertion": "alice@example.com"}
            )
            bob = await self.request(
                "GET", f"/api/jobs/{job_id}", headers={"Cf-Access-Jwt-Assertion": "bob@example.com"}
            )
            self.assertEqual(alice.status_code, 200)
            self.assertEqual(bob.status_code, 404)
            self.assertNotIn("alice@example.com", self.fake_redis.get(f"job:{job_id}"))

    async def test_retry_limit_and_report_export(self):
        created = await self.upload(self.chirp)
        job_id = created.json()["id"]
        with patch.object(worker, "run_quick", side_effect=analysis.AnalysisError("temporary test failure")):
            worker.process_job(job_id)
        self.assertEqual(self.read_job(job_id)["status"], "failed")
        retried = await self.request("POST", f"/api/jobs/{job_id}/retry")
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["attempt"], 2)
        with patch.object(worker, "run_quick", side_effect=analysis.AnalysisError("temporary test failure")):
            worker.process_job(job_id)
        self.assertEqual(self.read_job(job_id)["status"], "failed")
        exhausted = await self.request("POST", f"/api/jobs/{job_id}/retry")
        self.assertEqual(exhausted.status_code, 409)

        job = self.read_job(job_id)
        job["quick_result"] = {"qualification": "inconclusive", "candidate_features": []}
        job["status"] = "completed"
        main._save_job(job)
        report = await self.request("GET", f"/api/jobs/{job_id}/report")
        self.assertEqual(report.status_code, 200)
        self.assertIn("application/json", report.headers["content-type"])
        self.assertIn("not confirm aircraft", report.text)

    def test_duration_limit_is_enforced_after_probe(self):
        metadata = {
            "format": {"duration": "3601"},
            "streams": [{"codec_type": "video", "codec_name": "mpeg4", "width": 160, "height": 90}],
        }
        result = SimpleNamespace(stdout=json.dumps(metadata))
        with patch.object(analysis.subprocess, "run", return_value=result), self.assertRaisesRegex(
            analysis.AnalysisError, "duration limit"
        ):
            analysis.probe_media(Path("not-opened.mp4"))

    def test_access_jwt_signature_issuer_and_audience(self):
        original_env = settings.APP_ENV
        original_aud = settings.CF_ACCESS_AUD
        original_issuer = settings.CF_ACCESS_ISSUER
        settings.APP_ENV = "production"
        settings.CF_ACCESS_AUD = "dopplertrack-test-aud"
        settings.CF_ACCESS_ISSUER = "https://test.cloudflareaccess.com"
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = {
            "iss": settings.CF_ACCESS_ISSUER,
            "aud": [settings.CF_ACCESS_AUD],
            "iat": int(time.time()),
            "nbf": int(time.time()),
            "exp": int(time.time()) + 60,
            "email": "alice@example.com",
        }
        token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "fixture"})
        signing_key = SimpleNamespace(key=private_key.public_key())
        client = SimpleNamespace(get_signing_key_from_jwt=lambda supplied: signing_key)
        try:
            with patch.object(access, "_jwks_client", return_value=client):
                verified = access.verify_access_token(token)
                self.assertEqual(access.principal(verified), "alice@example.com")
                wrong_aud = jwt.encode({**claims, "aud": ["other-aud"]}, private_key, algorithm="RS256", headers={"kid": "fixture"})
                with self.assertRaises(jwt.InvalidTokenError):
                    access.verify_access_token(wrong_aud)
                wrong_signature = jwt.encode(claims, rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256", headers={"kid": "fixture"})
                with self.assertRaises(jwt.InvalidTokenError):
                    access.verify_access_token(wrong_signature)
        finally:
            settings.APP_ENV = original_env
            settings.CF_ACCESS_AUD = original_aud
            settings.CF_ACCESS_ISSUER = original_issuer

    def test_worker_production_import_does_not_need_web_access_settings(self):
        env = os.environ.copy()
        env["DOPPLERTRACK_ENV"] = "production"
        env.pop("CF_ACCESS_TEAM_DOMAIN", None)
        env.pop("CF_ACCESS_AUD", None)
        result = subprocess.run(
            [sys.executable, "-c", "import app.worker"],
            check=False,
            cwd=Path(__file__).parents[1],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_timeout_gate_and_24_hour_cleanup(self):
        original_timeout = settings.ANALYSIS_TIMEOUT_SECONDS
        settings.ANALYSIS_TIMEOUT_SECONDS = 60
        metadata = analysis.probe_media(self.chirp)
        with self.assertRaisesRegex(analysis.AnalysisError, "timed out"):
            analysis.run_quick(
                self.chirp,
                metadata,
                Path(settings.DATA_DIR) / "timeout-job",
                lambda: False,
                time.monotonic() - 120,
            )
        settings.ANALYSIS_TIMEOUT_SECONDS = original_timeout

        old = time.time() - settings.RETENTION_SECONDS - 10
        old_upload = Path(settings.UPLOAD_DIR) / "expired-upload"
        old_data = Path(settings.DATA_DIR) / "expired-report"
        new_upload = Path(settings.UPLOAD_DIR) / "recent-upload"
        for directory in (old_upload, old_data, new_upload):
            directory.mkdir(parents=True)
        (old_upload / ".created_at").write_text(str(old), encoding="ascii")
        (old_data / ".created_at").write_text(str(old), encoding="ascii")
        (new_upload / ".created_at").write_text(str(time.time()), encoding="ascii")
        self.assertEqual(worker.cleanup_expired(), 2)
        self.assertFalse(old_upload.exists())
        self.assertFalse(old_data.exists())
        self.assertTrue(new_upload.exists())


if __name__ == "__main__":
    unittest.main()
