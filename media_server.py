"""Local HTTP Range server for session videos (large files, seekable)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
import threading
import time

PORT = 18765
_HANDLER_VER = 2
_STATE = Path("/tmp/video_cutter_media_root.txt")
_server: ThreadingHTTPServer | None = None


def set_videos_dir(path: Path) -> None:
    _STATE.write_text(str(Path(path).resolve()), encoding="utf-8")


def _current_root() -> Path | None:
    try:
        text = _STATE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    return Path(text).resolve()


def _parse_byte_range(header: str | None, size: int) -> tuple[int, int, bool]:
    """Return (start, end_inclusive, is_partial). Supports bytes=a-b / a- / -suffix."""
    if size <= 0:
        return 0, -1, False
    if not header or not header.lower().startswith("bytes="):
        return 0, size - 1, False
    spec = header.split("=", 1)[1].split(",", 1)[0].strip()
    if not spec:
        raise ValueError("empty range")
    if spec.startswith("-"):
        suffix = int(spec[1:])
        if suffix <= 0:
            raise ValueError("bad suffix")
        start = max(0, size - suffix)
        return start, size - 1, True
    left, sep, right = spec.partition("-")
    if not sep:
        raise ValueError("bad range")
    start = int(left) if left else 0
    end = int(right) if right else size - 1
    end = min(end, size - 1)
    if start < 0 or start >= size or end < start:
        raise LookupError("unsatisfiable")
    return start, end, True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.close_connection = True

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "Accept-Ranges, Content-Range, Content-Length")
        self.send_header("Connection", "close")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.close_connection = True
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(send_body=False)

    def do_GET(self) -> None:  # noqa: N802
        self._serve(send_body=True)

    def _resolve_file(self) -> Path | None:
        root = _current_root()
        if root is None:
            self.send_error(503, "not ready")
            return None
        rel = unquote(urlparse(self.path).path).lstrip("/")
        if not rel or ".." in rel.split("/"):
            self.send_error(400)
            return None
        fpath = (root / rel).resolve()
        try:
            fpath.relative_to(root)
        except ValueError:
            self.send_error(403)
            return None
        if not fpath.is_file():
            self.send_error(404)
            return None
        return fpath

    def _serve(self, send_body: bool) -> None:
        self.close_connection = True
        fpath = self._resolve_file()
        if fpath is None:
            return
        size = fpath.stat().st_size
        mime = (
            "video/mp4" if fpath.suffix.lower() == ".mp4" else "application/octet-stream"
        )
        try:
            start, end, partial = _parse_byte_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_error(400)
            return
        except LookupError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self._cors()
            self.end_headers()
            return

        if partial:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        length = max(0, end - start + 1)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self._cors()
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not send_body or length <= 0:
            return
        with fpath.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: object, client_address: object) -> None:
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def ensure_server(videos_dir: Path) -> int:
    """Serve videos_dir. Return listen port."""
    set_videos_dir(videos_dir)
    global _server
    srv = _server
    if srv is not None and getattr(srv, "_cutter_ver", 0) != _HANDLER_VER:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
        _server = None
        time.sleep(0.15)
    if _server is None:
        last_err: OSError | None = None
        for _ in range(8):
            try:
                httpd = _Server(("0.0.0.0", PORT), _Handler)
                httpd._cutter_ver = _HANDLER_VER  # type: ignore[attr-defined]
                _server = httpd
                threading.Thread(
                    target=httpd.serve_forever, daemon=True, name="cutter-media"
                ).start()
                last_err = None
                break
            except OSError as exc:
                last_err = exc
                time.sleep(0.1)
        if last_err is not None and _server is None:
            return PORT
    return PORT
