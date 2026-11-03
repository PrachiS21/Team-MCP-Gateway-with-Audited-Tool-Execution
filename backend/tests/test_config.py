import pytest

from gateway.config import Settings


@pytest.mark.parametrize("env, port", [
    ({}, 8000),
    ({"PORT": "8080"}, 8080),  # Cloud Run sets PORT
    ({"PORT": "8080", "GATEWAY_PORT": "9000"}, 9000),  # an explicit GATEWAY_PORT still wins
    ({"GATEWAY_PORT": ""}, 8000),  # empty counts as unset
])
def test_port_comes_from_gateway_port_then_port(monkeypatch, env, port):
    for name in ("GATEWAY_PORT", "PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "test-key")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert Settings.from_env().port == port
