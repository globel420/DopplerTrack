from __future__ import annotations

import os
import re


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


APP_ENV = os.getenv("DOPPLERTRACK_ENV", "development").lower()
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/tmp/dopplertrack-uploads")
DATA_DIR = os.getenv("DATA_DIR", "/tmp/dopplertrack-data")
MAX_UPLOAD_MB = env_int("MAX_UPLOAD_MB", 4096, 1, 4096)
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_DURATION_SECONDS = env_int("MAX_DURATION_SECONDS", 3600, 5, 3600)
MAX_QUEUE = env_int("MAX_QUEUE", 2, 1, 25)
RETENTION_SECONDS = env_int("RETENTION_SECONDS", 86400, 3600, 604800)
ANALYSIS_TIMEOUT_SECONDS = env_int("ANALYSIS_TIMEOUT_SECONDS", 7200, 60, 10800)
UPLOAD_SESSION_TTL_SECONDS = env_int("UPLOAD_SESSION_TTL_SECONDS", 14400, 600, 86400)
MIN_FREE_DISK_GB = env_int("MIN_FREE_DISK_GB", 10, 1, 500)
ADSB_HISTORY_ENABLED = os.getenv("ADSB_HISTORY_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
URL_IMPORT_PROXY = os.getenv("URL_IMPORT_PROXY", "").strip()
CF_ACCESS_TEAM_DOMAIN = os.getenv("CF_ACCESS_TEAM_DOMAIN", "").strip().lower()
CF_ACCESS_AUD = os.getenv("CF_ACCESS_AUD", "").strip()
CF_ACCESS_ISSUER = f"https://{CF_ACCESS_TEAM_DOMAIN}" if CF_ACCESS_TEAM_DOMAIN else ""

def validate_production_access() -> None:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.cloudflareaccess\.com", CF_ACCESS_TEAM_DOMAIN):
        raise RuntimeError("CF_ACCESS_TEAM_DOMAIN must be the Cloudflare Access team domain")
    if not CF_ACCESS_AUD:
        raise RuntimeError("CF_ACCESS_AUD is required in production")

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
