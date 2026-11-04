from __future__ import annotations

import asyncio
import logging
import socket
from urllib.parse import urlsplit, urlunsplit

from app.url_security import UnsafeMediaURL, normalize_host, resolve_public_host

logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
logger = logging.getLogger("dopplertrack.egress")

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 3128
MAX_HEADER_BYTES = 32 * 1024
MAX_TUNNEL_BYTES = 128 * 1024 * 1024
MAX_IDLE_SECONDS = 60
MAX_TUNNEL_SECONDS = 15 * 60
MAX_CONNECTIONS = 24
connections = asyncio.Semaphore(MAX_CONNECTIONS)


async def _connect_public(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    addresses = await asyncio.wait_for(asyncio.to_thread(resolve_public_host, host, port), timeout=10)
    last_error: OSError | None = None
    loop = asyncio.get_running_loop()
    for family, sockaddr in addresses:
        raw = socket.socket(family, socket.SOCK_STREAM)
        raw.setblocking(False)
        try:
            await asyncio.wait_for(loop.sock_connect(raw, sockaddr), timeout=10)
            return await asyncio.open_connection(sock=raw)
        except (OSError, TimeoutError) as exc:
            raw.close()
            last_error = exc if isinstance(exc, OSError) else OSError("Connection timed out")
    raise last_error or OSError("No public address was reachable")


async def _copy_stream(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
    transferred = 0
    while True:
        chunk = await asyncio.wait_for(source.read(64 * 1024), timeout=MAX_IDLE_SECONDS)
        if not chunk:
            try:
                destination.write_eof()
                await destination.drain()
            except (OSError, RuntimeError, ConnectionError):
                pass
            return
        transferred += len(chunk)
        if transferred > MAX_TUNNEL_BYTES:
            raise ConnectionError("Egress transfer limit exceeded")
        destination.write(chunk)
        await asyncio.wait_for(destination.drain(), timeout=MAX_IDLE_SECONDS)


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    tasks = [
        asyncio.create_task(_copy_stream(client_reader, upstream_writer)),
        asyncio.create_task(_copy_stream(upstream_reader, client_writer)),
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=MAX_TUNNEL_SECONDS)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _respond(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    body = (message + "\n").encode("ascii", errors="replace")
    writer.write(
        f"HTTP/1.1 {status} {message}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "Content-Type: text/plain\r\n\r\n".encode("ascii")
        + body
    )
    await writer.drain()


def _parse_connect(authority: str) -> tuple[str, int]:
    parsed = urlsplit("//" + authority)
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise UnsafeMediaURL("Invalid proxy destination")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise UnsafeMediaURL("Invalid proxy port") from exc
    if parsed.path or parsed.query or parsed.fragment:
        raise UnsafeMediaURL("Invalid proxy destination")
    return normalize_host(parsed.hostname), port


async def _discard_connect_headers(reader: asyncio.StreamReader) -> None:
    total = 0
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        total += len(line)
        if total > MAX_HEADER_BYTES:
            raise UnsafeMediaURL("Proxy headers are too large")
        if line in {b"\r\n", b"\n"}:
            return
        if not line or b":" not in line:
            raise UnsafeMediaURL("Malformed proxy headers")


async def _handle_connect(
    target: str,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    await _discard_connect_headers(reader)
    host, port = _parse_connect(target)
    upstream_reader, upstream_writer = await _connect_public(host, port)
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    try:
        try:
            await _relay(reader, writer, upstream_reader, upstream_writer)
        except (OSError, ConnectionError, TimeoutError):
            pass
    finally:
        upstream_writer.close()
        await upstream_writer.wait_closed()


async def _handle_http(
    method: str,
    target: str,
    version: str,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    if method not in {"GET", "HEAD", "POST"} or version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise UnsafeMediaURL("Unsupported proxy request")
    parsed = urlsplit(target)
    if parsed.scheme.lower() != "http" or not parsed.hostname:
        raise UnsafeMediaURL("HTTPS requests must use CONNECT")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeMediaURL("Embedded credentials are not accepted")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise UnsafeMediaURL("Invalid proxy port") from exc
    if port != 80:
        raise UnsafeMediaURL("Unsupported proxy port")
    host = normalize_host(parsed.hostname)
    upstream_reader, upstream_writer = await _connect_public(host, port)
    try:
        headers: list[bytes] = []
        total = 0
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            total += len(line)
            if total > MAX_HEADER_BYTES:
                raise UnsafeMediaURL("Proxy headers are too large")
            if line in {b"\r\n", b"\n"}:
                break
            if not line or b":" not in line:
                raise UnsafeMediaURL("Malformed proxy headers")
            name, _value = line.split(b":", 1)
            if name.strip().lower() in {b"host", b"connection", b"proxy-connection", b"proxy-authorization"}:
                continue
            headers.append(line)

        host_value = f"[{host}]" if ":" in host else host
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        host_header = f"Host: {host_value}\r\n".encode("ascii")
        upstream_writer.write(f"{method} {path} {version}\r\n".encode("ascii"))
        upstream_writer.writelines(headers)
        upstream_writer.write(host_header + b"Connection: close\r\n\r\n")
        await upstream_writer.drain()
        try:
            await _relay(reader, writer, upstream_reader, upstream_writer)
        except (OSError, ConnectionError, TimeoutError):
            pass
    finally:
        upstream_writer.close()
        await upstream_writer.wait_closed()


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    accepted = False
    try:
        async with connections:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not line:
                return
            if len(line) > 4096:
                raise UnsafeMediaURL("Proxy request is too large")
            try:
                method, target, version = line.decode("ascii").strip().split(" ", 2)
            except (UnicodeDecodeError, ValueError) as exc:
                raise UnsafeMediaURL("Malformed proxy request") from exc
            if method.upper() == "CONNECT":
                await _handle_connect(target, reader, writer)
                accepted = True
            else:
                await _handle_http(method.upper(), target, version, reader, writer)
                accepted = True
    except UnsafeMediaURL as exc:
        logger.info("Blocked egress request (%s)", type(exc).__name__)
        if not accepted and not writer.is_closing():
            try:
                await _respond(writer, 403, "Forbidden")
            except (OSError, ConnectionError):
                pass
    except TimeoutError:
        if not accepted and not writer.is_closing():
            try:
                await _respond(writer, 504, "Gateway Timeout")
            except (OSError, ConnectionError):
                pass
    except (OSError, ConnectionError, asyncio.IncompleteReadError) as exc:
        logger.info("Egress connection ended (%s)", type(exc).__name__)
        if not accepted and not writer.is_closing():
            try:
                await _respond(writer, 502, "Bad Gateway")
            except (OSError, ConnectionError):
                pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def serve() -> None:
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT, limit=MAX_HEADER_BYTES)
    logger.info("URL egress proxy ready")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(serve())
