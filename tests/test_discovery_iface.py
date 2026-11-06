"""Discovery.refresh_iface: 出口网卡变了要重新绑定组播。"""
import asyncio

from flixcli import discovery as disc_mod


class _FakeTransport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _make(iface="10.0.0.5"):
    return disc_mod.Discovery(lambda *_a, **_k: None, iface_ip=iface)


def test_refresh_is_noop_when_interface_unchanged(monkeypatch):
    d = _make()
    d.bound = True
    d.transport = _FakeTransport()
    monkeypatch.setattr(disc_mod, "default_iface_ip", lambda: "10.0.0.5")

    assert asyncio.run(d.refresh_iface()) is False
    assert d.iface == "10.0.0.5"
    assert d.transport.closed is False


def test_refresh_rebinds_when_interface_moves(monkeypatch):
    d = _make()
    d.bound = True
    d.transport = _FakeTransport()
    started = []

    async def fake_start():
        started.append(d.iface)
        d.bound = True
        return True

    monkeypatch.setattr(disc_mod, "default_iface_ip", lambda: "192.168.1.9")
    monkeypatch.setattr(d, "start", fake_start)

    assert asyncio.run(d.refresh_iface()) is True
    assert d.iface == "192.168.1.9"
    assert started == ["192.168.1.9"]


def test_refresh_skips_when_never_bound(monkeypatch):
    d = _make()
    d.bound = False
    monkeypatch.setattr(disc_mod, "default_iface_ip", lambda: "192.168.1.9")

    assert asyncio.run(d.refresh_iface()) is False
    assert d.iface == "10.0.0.5"
