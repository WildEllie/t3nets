"""Shared BaseHandler — HTTP utilities inherited by DevHandler and AWSHandler."""

import json
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path


class BaseHandler(BaseHTTPRequestHandler):
    """Shared HTTP plumbing for local and AWS request handlers.

    Provides: JSON responses, static file serving, body helpers,
    CORS pre-flight, request logging suppression, and a dispatch
    helper that replaces long if/elif routing chains.
    """

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _json_response(self, data: dict, status: int = 200) -> None:  # type: ignore[type-arg]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _serve_file(self, filename: str, search_dir: str | None = None) -> None:
        """Serve an HTML file from the project root or a subdirectory."""
        base = Path(__file__).parent.parent.parent
        path = base / search_dir / filename if search_dir else base / filename
        if path.exists():
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(path.read_bytes())
        else:
            self.send_error(404, f"{filename} not found")

    # ------------------------------------------------------------------
    # Request body helpers
    # ------------------------------------------------------------------

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length)

    def _read_json(self) -> dict:  # type: ignore[type-arg]
        return json.loads(self._read_body())

    # ------------------------------------------------------------------
    # Standard HTTP overrides
    # ------------------------------------------------------------------

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # suppress default per-request logging

    # ------------------------------------------------------------------
    # Dispatch helper
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        routes: dict[str, Callable[[], None]],
        path: str,
        *,
        fallback: Callable[[], None] | None = None,
    ) -> None:
        """Route a request to its handler.

        Tries exact match first, then prefix match (route key must end with '*').
        If nothing matches, calls *fallback* if provided, otherwise sends 404.
        """
        handler: Callable[[], None] | None = routes.get(path)
        if handler is None:
            for prefix, fn in routes.items():
                if prefix.endswith("*") and path.startswith(prefix[:-1]):
                    handler = fn
                    break
        if handler is not None:
            handler()
        elif fallback is not None:
            fallback()
        else:
            self.send_error(404)
