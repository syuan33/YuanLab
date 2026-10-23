import pytest

from .fake_upstream import FakeUpstream, ServerThread


@pytest.fixture
def fake():
    """Factory: fake(prefill_tps=..., cliff_tokens=...) -> (FakeUpstream, url)."""
    servers = []

    def make(**kw):
        f = FakeUpstream(**kw)
        s = ServerThread(f.app).start()
        servers.append(s)
        return f, s.url

    yield make
    for s in servers:
        s.stop()
