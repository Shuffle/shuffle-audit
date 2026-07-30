#!/usr/bin/env python3
"""Serve a Shuffle usage audit dashboard on the local loopback interface."""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple


MAX_REPORT_BYTES = 50 * 1024 * 1024
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
VIEWER_DIRECTORY = Path(__file__).resolve().with_name("viewer")
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/styles.css": "styles.css",
    "/app.js": "app.js",
}


class ViewerError(RuntimeError):
    """Raised when the local dashboard cannot be started safely."""


def load_report(path: Path) -> Tuple[bytes, Dict[str, Any]]:
    resolved = path.expanduser().resolve()
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ViewerError(f"cannot read report {resolved}: {exc}") from exc

    if size > MAX_REPORT_BYTES:
        raise ViewerError(
            f"report is larger than the {MAX_REPORT_BYTES // (1024 * 1024)} MiB limit"
        )

    try:
        report_bytes = resolved.read_bytes()
        report = json.loads(report_bytes)
    except OSError as exc:
        raise ViewerError(f"cannot read report {resolved}: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ViewerError(f"{resolved} is not valid JSON: {exc}") from exc

    if not isinstance(report, dict):
        raise ViewerError("the audit report must be a JSON object")
    if not isinstance(report.get("metrics"), dict):
        raise ViewerError("the audit report is missing its metrics object")
    if not isinstance(report["metrics"].get("per_org"), list):
        raise ViewerError(
            "the audit report is missing metrics.per_org; regenerate it with "
            "the current shuffle_usage_audit.py"
        )

    return report_bytes, report


def viewer_asset(filename: str) -> bytes:
    path = VIEWER_DIRECTORY / filename
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ViewerError(f"missing dashboard asset {path}: {exc}") from exc


def make_handler(report_bytes: bytes) -> type[BaseHTTPRequestHandler]:
    assets = {route: viewer_asset(filename) for route, filename in STATIC_FILES.items()}

    class AuditViewerHandler(BaseHTTPRequestHandler):
        server_version = "ShuffleAuditViewer/1"

        def send_security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
            )

        def respond(
            self,
            status: int,
            body: bytes,
            content_type: str,
            include_body: bool = True,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_security_headers()
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def handle_read(self, include_body: bool) -> None:
            route = urllib.parse.urlsplit(self.path).path
            if route == "/report.json":
                self.respond(
                    200,
                    report_bytes,
                    "application/json; charset=utf-8",
                    include_body,
                )
                return

            body = assets.get(route)
            if body is None:
                self.respond(
                    404,
                    b"Not found\n",
                    "text/plain; charset=utf-8",
                    include_body,
                )
                return

            filename = STATIC_FILES[route]
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type == "application/javascript":
                content_type += "; charset=utf-8"
            self.respond(200, body, content_type, include_body)

        def do_GET(self) -> None:  # noqa: N802
            self.handle_read(include_body=True)

        def do_HEAD(self) -> None:  # noqa: N802
            self.handle_read(include_body=False)

        def do_POST(self) -> None:  # noqa: N802
            self.respond(405, b"Method not allowed\n", "text/plain; charset=utf-8")

        def log_message(self, _format: str, *args: Any) -> None:
            # Avoid logging query strings or report contents.
            return

    return AuditViewerHandler


def create_server(report_bytes: bytes, port: int) -> ThreadingHTTPServer:
    if not 0 <= port <= 65535:
        raise ViewerError("--port must be between 0 and 65535")

    try:
        server = ThreadingHTTPServer(
            (LOOPBACK_HOST, port),
            make_handler(report_bytes),
        )
    except OSError as exc:
        raise ViewerError(f"cannot bind {LOOPBACK_HOST}:{port}: {exc}") from exc
    server.daemon_threads = True
    return server


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Open a private, local dashboard for a Shuffle usage audit JSON report."
        )
    )
    parser.add_argument(
        "report",
        nargs="?",
        default="shuffle-usage-audit.json",
        help="audit JSON path (default: shuffle-usage-audit.json)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"local port, or 0 for an available port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="start the local dashboard without opening a browser",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = parse_args(argv)
        report_path = Path(args.report).expanduser().resolve()
        report_bytes, report = load_report(report_path)
        server = create_server(report_bytes, args.port)
        port = server.server_address[1]
        url = f"http://{LOOPBACK_HOST}:{port}/"

        print("Shuffle audit dashboard")
        print(f"Report: {report_path}")
        print(
            "Status: "
            + ("complete" if report.get("audit_status", {}).get("complete") else "incomplete")
        )
        print(f"Local URL: {url}")
        print("The server is bound to this machine only. Press Ctrl+C to stop.")

        if not args.no_browser:
            threading.Timer(0.25, webbrowser.open, args=(url,)).start()

        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            print("\nStopping dashboard.")
        finally:
            server.server_close()
        return 0
    except ViewerError as exc:
        print(f"dashboard failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
