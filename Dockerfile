FROM python:3.13.18-slim-bookworm AS wheelhouse

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements.lock ./requirements.lock
RUN python -m pip wheel --require-hashes --wheel-dir=/wheels -r requirements.lock

FROM python:3.13.18-slim-bookworm AS deno

ARG DENO_VERSION=2.9.8
ARG TARGETARCH
RUN python - <<'PY'
from hashlib import sha256
from io import BytesIO
from os import environ
from pathlib import Path
from urllib.request import Request, urlopen
from zipfile import ZipFile

version = environ["DENO_VERSION"]
assets = {
    "amd64": ("x86_64", "e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609"),
    "arm64": ("aarch64", "d49596c20b9788cf8f5d8ddf644103d4bde56ed239753f0bdb92ba9361d51905"),
}
try:
    architecture, expected = assets[environ["TARGETARCH"]]
except KeyError as exc:
    raise SystemExit("Deno is only pinned for linux/amd64 and linux/arm64") from exc
url = f"https://github.com/denoland/deno/releases/download/v{version}/deno-{architecture}-unknown-linux-gnu.zip"
request = Request(url, headers={"User-Agent": "DopplerTrack-build"})
with urlopen(request, timeout=60) as response:
    archive = response.read()
if sha256(archive).hexdigest() != expected:
    raise SystemExit("Deno archive checksum did not match the pinned digest")
with ZipFile(BytesIO(archive)) as bundle:
    binary = bundle.read("deno")
target = Path("/usr/local/bin/deno")
target.write_bytes(binary)
target.chmod(0o755)
PY

FROM python:3.13.18-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TMPDIR=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 dopplertrack \
    && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin dopplertrack \
    && mkdir -p /uploads /data/jobs /tmp \
    && chown -R 10001:10001 /uploads /data /tmp

WORKDIR /srv/dopplertrack
COPY --from=wheelhouse /wheels /wheels
COPY requirements.lock ./requirements.lock
COPY --from=deno /usr/local/bin/deno /usr/local/bin/deno
RUN python -m pip install --require-hashes --no-index --find-links=/wheels -r requirements.lock \
    && rm -rf /wheels
COPY --chown=10001:10001 app ./app

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
