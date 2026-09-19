from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import time
from pathlib import Path

# small valid JPEG (same payload used by test_capture.py)
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb0043000806060706050807070709"
    "09080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720221c1c28372c3031"
    "3434341f27393d32383c3432ffc0000b080001000101011100ffc4001400010000000000"
    "0000000000000000000008ffc40014100100000000000000000000000000000000ffda00"
    "08010100003f0054bfffd9"
)

HELPER_SOURCE = (
    "import os, struct, sys, time\n"
    "pidfile = os.environ.get('FAKE_HELPER_PIDFILE')\n"
    "if pidfile:\n"
    "    open(pidfile, 'w').write(str(os.getpid()))\n"
    f"jpeg = {TINY_JPEG!r}\n"
    "out = sys.stdout.buffer\n"
    "try:\n"
    "    while True:\n"
    "        out.write(struct.pack('>I', len(jpeg)) + jpeg)\n"
    "        out.flush()\n"
    "        time.sleep(0.05)\n"
    "except (BrokenPipeError, KeyboardInterrupt):\n"
    "    pass\n"
)


def _make_persistent_helper(tmp_path: Path) -> Path:
    script = tmp_path / "fake-capture.py"
    script.write_text(f"#!{sys.executable}\n" + HELPER_SOURCE, encoding="utf-8")
    if os.name == "nt":
        wrapper = tmp_path / "fake-capture.cmd"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return wrapper
    script.chmod(0o755)
    return script


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_sync(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent_text: list[str] = []
        self.sent_bytes = 0
        self._queue: asyncio.Queue[dict] = asyncio.Queue()

    async def accept(self) -> None:
        return None

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes += len(data)

    async def receive(self) -> dict:
        return await self._queue.get()

    def disconnect(self) -> None:
        self._queue.put_nowait({"type": "websocket.disconnect"})

    def send(self, payload: dict) -> None:
        self._queue.put_nowait({"type": "websocket.receive", "text": __import__("json").dumps(payload)})


async def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


def test_capture_starts_only_on_demand(monkeypatch, tmp_path) -> None:
    from termx.desktop.capture import close_capture
    from termx.desktop.session import DesktopManager

    pidfile = tmp_path / "helper.pid"
    monkeypatch.setenv("TERMX_CAPTURE_BIN", str(_make_persistent_helper(tmp_path)))
    monkeypatch.setenv("FAKE_HELPER_PIDFILE", str(pidfile))
    close_capture()
    manager = DesktopManager()
    manager.snapshot()
    assert not pidfile.exists()


def test_capture_helper_stops_after_last_client(monkeypatch, tmp_path) -> None:
    from termx.desktop.capture import close_capture
    from termx.desktop.session import DesktopManager

    pidfile = tmp_path / "helper.pid"
    monkeypatch.setenv("TERMX_CAPTURE_BIN", str(_make_persistent_helper(tmp_path)))
    monkeypatch.setenv("FAKE_HELPER_PIDFILE", str(pidfile))
    monkeypatch.setenv("TERMX_CAPTURE_IDLE_GRACE", "0.3")
    close_capture()

    async def inner() -> None:
        manager = DesktopManager()
        first = FakeWebSocket()
        second = FakeWebSocket()
        task_one = asyncio.create_task(manager.attach(first))
        assert await _wait_until(lambda: first.sent_bytes > 0)
        task_two = asyncio.create_task(manager.attach(second))
        assert await _wait_until(lambda: second.sent_bytes > 0)
        pid = int(pidfile.read_text())
        assert _pid_alive(pid)

        first.disconnect()
        await asyncio.wait_for(task_one, timeout=10)
        # one client still watching: the stream stays alive
        await asyncio.sleep(0.8)
        assert _pid_alive(pid)

        second.disconnect()
        await asyncio.wait_for(task_two, timeout=10)
        assert await _wait_until(lambda: not _pid_alive(pid), timeout=10)

    try:
        asyncio.run(inner())
    finally:
        close_capture()


def test_reconnect_within_grace_keeps_helper(monkeypatch, tmp_path) -> None:
    from termx.desktop.capture import close_capture
    from termx.desktop.session import DesktopManager

    pidfile = tmp_path / "helper.pid"
    monkeypatch.setenv("TERMX_CAPTURE_BIN", str(_make_persistent_helper(tmp_path)))
    monkeypatch.setenv("FAKE_HELPER_PIDFILE", str(pidfile))
    monkeypatch.setenv("TERMX_CAPTURE_IDLE_GRACE", "1.5")
    close_capture()

    async def inner() -> None:
        manager = DesktopManager()
        first = FakeWebSocket()
        task = asyncio.create_task(manager.attach(first))
        assert await _wait_until(lambda: first.sent_bytes > 0)
        pid = int(pidfile.read_text())
        first.disconnect()
        await asyncio.wait_for(task, timeout=10)
        # reconnect before the grace period expires
        await asyncio.sleep(0.3)
        second = FakeWebSocket()
        task_two = asyncio.create_task(manager.attach(second))
        assert await _wait_until(lambda: second.sent_bytes > 0)
        assert _pid_alive(pid)
        second.disconnect()
        await asyncio.wait_for(task_two, timeout=10)
        assert await _wait_until(lambda: not _pid_alive(pid), timeout=10)

    try:
        asyncio.run(inner())
    finally:
        close_capture()


def test_shutdown_state_closes_capture_helper(monkeypatch, tmp_path) -> None:
    from termx.app import AppState
    from termx.desktop.capture import close_capture, grab_jpeg
    from termx.lifecycle import shutdown_state

    pidfile = tmp_path / "helper.pid"
    monkeypatch.setenv("TERMX_CAPTURE_BIN", str(_make_persistent_helper(tmp_path)))
    monkeypatch.setenv("FAKE_HELPER_PIDFILE", str(pidfile))
    close_capture()
    try:
        frame = grab_jpeg()
        assert frame[:3] == b"\xff\xd8\xff"
        pid = int(pidfile.read_text())
        assert _pid_alive(pid)
        state = AppState(passcode=None)
        asyncio.run(shutdown_state(state, timeout=2.0))
        assert _wait_sync(lambda: not _pid_alive(pid), timeout=10)
    finally:
        close_capture()




def test_pause_stops_capture_and_resume_restarts(monkeypatch, tmp_path) -> None:
    from termx.desktop.capture import close_capture
    from termx.desktop.session import DesktopManager

    pidfile = tmp_path / "helper.pid"
    monkeypatch.setenv("TERMX_CAPTURE_BIN", str(_make_persistent_helper(tmp_path)))
    monkeypatch.setenv("FAKE_HELPER_PIDFILE", str(pidfile))
    monkeypatch.setenv("TERMX_CAPTURE_PAUSE_GRACE", "0.3")
    monkeypatch.setenv("TERMX_CAPTURE_IDLE_GRACE", "0.3")
    close_capture()

    async def inner() -> None:
        manager = DesktopManager()
        ws = FakeWebSocket()
        task = asyncio.create_task(manager.attach(ws))
        assert await _wait_until(lambda: ws.sent_bytes > 0)
        first_pid = int(pidfile.read_text())
        assert _pid_alive(first_pid)

        ws.send({"type": "pause"})
        assert await _wait_until(lambda: any('"paused"' in text for text in ws.sent_text))
        # no new frames while paused, and the helper is released
        assert await _wait_until(lambda: not _pid_alive(first_pid), timeout=10)
        # A pause request can race with one in-flight frame. Sample after the
        # helper exits so we assert the stream has gone quiescent.
        await asyncio.sleep(0.2)
        frozen = ws.sent_bytes
        await asyncio.sleep(0.5)
        assert ws.sent_bytes == frozen

        ws.send({"type": "resume"})
        assert await _wait_until(lambda: any('"resumed"' in text for text in ws.sent_text))
        assert await _wait_until(lambda: ws.sent_bytes > frozen)
        second_pid = int(pidfile.read_text())
        assert _pid_alive(second_pid)

        ws.disconnect()
        await asyncio.wait_for(task, timeout=10)
        assert await _wait_until(lambda: not _pid_alive(second_pid), timeout=10)

    try:
        asyncio.run(inner())
    finally:
        close_capture()


def test_pause_keeps_capture_when_another_viewer_is_active(monkeypatch, tmp_path) -> None:
    from termx.desktop.capture import close_capture
    from termx.desktop.session import DesktopManager

    pidfile = tmp_path / "helper.pid"
    monkeypatch.setenv("TERMX_CAPTURE_BIN", str(_make_persistent_helper(tmp_path)))
    monkeypatch.setenv("FAKE_HELPER_PIDFILE", str(pidfile))
    monkeypatch.setenv("TERMX_CAPTURE_PAUSE_GRACE", "0.3")
    monkeypatch.setenv("TERMX_CAPTURE_IDLE_GRACE", "0.3")
    close_capture()

    async def inner() -> None:
        manager = DesktopManager()
        first = FakeWebSocket()
        second = FakeWebSocket()
        task_one = asyncio.create_task(manager.attach(first))
        task_two = asyncio.create_task(manager.attach(second))
        assert await _wait_until(lambda: first.sent_bytes > 0 and second.sent_bytes > 0)
        pid = int(pidfile.read_text())

        first.send({"type": "pause"})
        assert await _wait_until(lambda: any('"paused"' in text for text in first.sent_text))
        await asyncio.sleep(0.8)
        assert _pid_alive(pid)
        frozen = second.sent_bytes
        assert frozen > 0

        second.send({"type": "pause"})
        assert await _wait_until(lambda: not _pid_alive(pid), timeout=10)

        second.send({"type": "resume"})
        assert await _wait_until(lambda: any('"resumed"' in text for text in second.sent_text))
        assert await _wait_until(lambda: second.sent_bytes > frozen)
        # The helper restarts asynchronously once frames flow again; poll rather
        # than sampling once, so suite load cannot make this flap.
        assert await _wait_until(lambda: _pid_alive(int(pidfile.read_text())), timeout=5.0)

        first.disconnect()
        second.disconnect()
        await asyncio.wait_for(task_one, timeout=10)
        await asyncio.wait_for(task_two, timeout=10)

    try:
        asyncio.run(inner())
    finally:
        close_capture()


def test_quick_resume_within_pause_grace_keeps_helper(monkeypatch, tmp_path) -> None:
    from termx.desktop.capture import close_capture
    from termx.desktop.session import DesktopManager

    pidfile = tmp_path / "helper.pid"
    monkeypatch.setenv("TERMX_CAPTURE_BIN", str(_make_persistent_helper(tmp_path)))
    monkeypatch.setenv("FAKE_HELPER_PIDFILE", str(pidfile))
    monkeypatch.setenv("TERMX_CAPTURE_PAUSE_GRACE", "5")
    close_capture()

    async def inner() -> None:
        manager = DesktopManager()
        ws = FakeWebSocket()
        task = asyncio.create_task(manager.attach(ws))
        assert await _wait_until(lambda: ws.sent_bytes > 0)
        pid = int(pidfile.read_text())
        ws.send({"type": "pause"})
        assert await _wait_until(lambda: any('"paused"' in text for text in ws.sent_text))
        await asyncio.sleep(0.2)
        ws.send({"type": "resume"})
        assert await _wait_until(lambda: any('"resumed"' in text for text in ws.sent_text))
        assert _pid_alive(pid)
        assert await _wait_until(lambda: ws.sent_bytes > 0)
        ws.disconnect()
        await asyncio.wait_for(task, timeout=10)

    try:
        asyncio.run(inner())
    finally:
        close_capture()
