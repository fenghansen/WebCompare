import ctypes
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from webcompare import app, host_memory
from webcompare.auth import create_user
from webcompare.config import Settings
from webcompare.db import Database


def test_linux_memory_uses_memavailable(monkeypatch, tmp_path):
    meminfo = tmp_path / "meminfo"
    monkeypatch.setattr(host_memory, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(host_memory, "Path", lambda _path: meminfo)
    assert host_memory.available_memory_bytes() is None
    meminfo.write_text("MemFree: 1 kB\nMemAvailable: 10485760 kB\n", encoding="ascii")
    assert host_memory.available_memory_bytes() == 10 * 1024**3
    meminfo.write_text("MemFree: 1 kB\n", encoding="ascii")
    assert host_memory.available_memory_bytes() is None


@pytest.mark.parametrize("success", [True, False])
def test_windows_memory_uses_available_physical_bytes(monkeypatch, success):
    available = 10 * 1024**3

    def read_status(pointer):
        assert ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] == 64
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint64))[2] = available
        return success

    monkeypatch.setattr(host_memory, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(GlobalMemoryStatusEx=read_status)), raising=False)
    assert host_memory.available_memory_bytes() == (available if success else None)


def test_personal_memory_requires_session_and_samples_each_request(monkeypatch, tmp_path):
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    readings = (10 * 1024**3, 0, None)
    samples = iter(readings)
    monkeypatch.setattr(app, "available_memory_bytes", lambda: next(samples))
    with TestClient(app.create_app(Settings(data_dir=tmp_path)), base_url="http://localhost", client=("127.0.0.1", 8000)) as client:
        assert client.get("/api/personal/memory").status_code == 401
        assert client.get("/api/auth/session").status_code == 200
        for available in readings:
            response = client.get("/api/personal/memory")
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert response.json() == {"available_bytes": available}


@pytest.mark.parametrize("transport,peer,headers", [
    ("remote", "127.0.0.1", {}),
    ("auto", "192.0.2.10", {}),
    ("local", "127.0.0.1", {"X-Forwarded-For": "192.0.2.10"}),
    ("auto", "127.0.0.1", {"Host": "public.example"}),
])
def test_personal_memory_skips_unverified_hosts(monkeypatch, tmp_path, transport, peer, headers):
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(app, "available_memory_bytes", lambda: pytest.fail("unexpected host memory probe"))
    settings = Settings(data_dir=tmp_path, client_transport=transport)
    with TestClient(app.create_app(settings), base_url="http://localhost", client=(peer, 8000)) as client:
        assert client.get("/api/auth/session").status_code == 200
        response = client.get("/api/personal/memory", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"available_bytes": None}


def test_server_does_not_register_personal_memory(tmp_path):
    settings = Settings(mode="server", data_dir=tmp_path / "state", data_root=tmp_path / "content",
                        base_url="https://cmp.example",
                        sandbox="none", disk_min_free_bytes=0, thumbnail_prewarm=False)
    settings.prepare()
    create_user(Database(settings.data_dir / "webcompare.sqlite3"), "admin", "test-password", role="admin")
    with TestClient(app.create_app(settings), base_url=settings.base_url) as client:
        assert client.get("/api/personal/memory").status_code == 404
