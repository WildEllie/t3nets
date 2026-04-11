"""Tests for the SSE Connection Manager."""

import io
import threading

from agent.sse import SSEConnectionManager


class MockWFile:
    """Mock wfile that captures written bytes."""

    def __init__(self, fail_on_write=False):
        self._buffer = io.BytesIO()
        self._fail_on_write = fail_on_write

    def write(self, data: bytes) -> int:
        if self._fail_on_write:
            raise BrokenPipeError("Connection closed")
        return self._buffer.write(data)

    def flush(self):
        if self._fail_on_write:
            raise BrokenPipeError("Connection closed")

    @property
    def output(self) -> str:
        return self._buffer.getvalue().decode()


def test_register_and_unregister():
    mgr = SSEConnectionManager()
    wfile = MockWFile()

    mgr.register("user1", wfile)
    assert mgr.connection_count == 1

    mgr.unregister("user1", wfile)
    assert mgr.connection_count == 0


def test_register_multiple_connections():
    mgr = SSEConnectionManager()
    wf1 = MockWFile()
    wf2 = MockWFile()

    mgr.register("user1", wf1)
    mgr.register("user1", wf2)
    assert mgr.connection_count == 2

    mgr.unregister("user1", wf1)
    assert mgr.connection_count == 1


def test_send_event():
    mgr = SSEConnectionManager()
    wfile = MockWFile()

    mgr.register("user1", wfile)
    sent = mgr.send_event("user1", "message", {"text": "hello", "request_id": "abc"})

    assert sent == 1
    output = wfile.output
    assert "event: message\n" in output
    assert '"text": "hello"' in output
    assert '"request_id": "abc"' in output


def test_send_event_to_nonexistent_user():
    mgr = SSEConnectionManager()
    sent = mgr.send_event("nobody", "message", {"text": "hello"})
    assert sent == 0


def test_send_event_multiple_tabs():
    mgr = SSEConnectionManager()
    wf1 = MockWFile()
    wf2 = MockWFile()

    mgr.register("user1", wf1)
    mgr.register("user1", wf2)
    sent = mgr.send_event("user1", "message", {"text": "hello"})

    assert sent == 2
    assert "hello" in wf1.output
    assert "hello" in wf2.output


def test_dead_connections_cleaned_up():
    mgr = SSEConnectionManager()
    live = MockWFile()
    dead = MockWFile(fail_on_write=True)

    mgr.register("user1", live)
    mgr.register("user1", dead)
    assert mgr.connection_count == 2

    sent = mgr.send_event("user1", "message", {"text": "test"})
    assert sent == 1
    assert mgr.connection_count == 1


def test_keepalive():
    mgr = SSEConnectionManager()
    wfile = MockWFile()

    mgr.register("user1", wfile)
    mgr.send_keepalive()

    assert ": keepalive\n\n" in wfile.output


def test_keepalive_cleans_dead():
    mgr = SSEConnectionManager()
    dead = MockWFile(fail_on_write=True)

    mgr.register("user1", dead)
    assert mgr.connection_count == 1

    mgr.send_keepalive()
    assert mgr.connection_count == 0


def test_thread_safety():
    """Verify concurrent register/send doesn't crash."""
    mgr = SSEConnectionManager()
    errors = []

    def writer():
        try:
            for _ in range(50):
                wf = MockWFile()
                mgr.register("user1", wf)
                mgr.send_event("user1", "test", {"n": 1})
                mgr.unregister("user1", wf)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"Thread safety errors: {errors}"


def test_unregister_nonexistent():
    """Unregistering a connection that doesn't exist should not error."""
    mgr = SSEConnectionManager()
    wfile = MockWFile()
    mgr.unregister("nobody", wfile)  # Should not raise
    assert mgr.connection_count == 0
