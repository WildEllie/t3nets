"""
Server-Sent Events (SSE) Connection Manager

Thread-safe manager for SSE connections. Used by both local and AWS servers
to push async skill results to dashboard clients.

Part of the cloud-agnostic core — no AWS/cloud imports.
"""

import json
import logging
import threading
import time

logger = logging.getLogger(__name__)


class SSEConnectionManager:
    """Thread-safe manager for Server-Sent Events connections.

    Tracks active SSE connections per user so background threads (e.g. SQS poller)
    can push async skill results to the correct dashboard client.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # user_key → list of wfile objects (one user can have multiple tabs)
        self._connections: dict[str, list] = {}

    def register(self, user_key: str, wfile) -> None:
        """Register an SSE connection for a user."""
        with self._lock:
            if user_key not in self._connections:
                self._connections[user_key] = []
            self._connections[user_key].append(wfile)
            logger.info(
                f"SSE: registered connection for {user_key} "
                f"(total: {len(self._connections[user_key])})"
            )

    def unregister(self, user_key: str, wfile) -> None:
        """Remove an SSE connection."""
        with self._lock:
            if user_key in self._connections:
                try:
                    self._connections[user_key].remove(wfile)
                except ValueError:
                    pass
                if not self._connections[user_key]:
                    del self._connections[user_key]
                logger.info(f"SSE: unregistered connection for {user_key}")

    def send_event(self, user_key: str, event_type: str, data: dict) -> int:
        """Send an SSE event to all connections for a user.

        Returns the number of connections that received the event.
        """
        payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        sent = 0
        dead: list[tuple[str, object]] = []

        with self._lock:
            connections = list(self._connections.get(user_key, []))

        for wfile in connections:
            try:
                wfile.write(payload.encode())
                wfile.flush()
                sent += 1
            except (BrokenPipeError, ConnectionResetError, OSError):
                dead.append((user_key, wfile))

        # Clean up dead connections outside the send loop
        if dead:
            self._remove_dead(dead)

        return sent

    def send_keepalive(self) -> None:
        """Send keepalive comment to all connections. Called periodically."""
        all_connections: list[tuple[str, object]] = []
        with self._lock:
            for user_key, conns in self._connections.items():
                for wfile in conns:
                    all_connections.append((user_key, wfile))

        dead: list[tuple[str, object]] = []
        for user_key, wfile in all_connections:
            try:
                wfile.write(b": keepalive\n\n")  # type: ignore[union-attr]
                wfile.flush()  # type: ignore[union-attr]
            except (BrokenPipeError, ConnectionResetError, OSError):
                dead.append((user_key, wfile))

        if dead:
            self._remove_dead(dead)

    def _remove_dead(self, dead: list[tuple[str, object]]) -> None:
        """Remove dead connections from the registry."""
        with self._lock:
            for user_key, wfile in dead:
                if user_key in self._connections:
                    try:
                        self._connections[user_key].remove(wfile)
                    except ValueError:
                        pass
                    if not self._connections[user_key]:
                        del self._connections[user_key]

    @property
    def connection_count(self) -> int:
        with self._lock:
            return sum(len(c) for c in self._connections.values())


def start_keepalive_thread(manager: SSEConnectionManager) -> threading.Thread:
    """Start a daemon thread that sends SSE keepalive every 15 seconds."""

    def _loop():
        while True:
            time.sleep(15)
            try:
                manager.send_keepalive()
            except Exception:
                pass

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return thread
