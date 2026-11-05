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


@pytest.mark.parametrize("value, expected", [(None, "text"), ("", "text"), ("json", "json"), ("text", "text")])
def test_log_format_defaults_to_text(monkeypatch, value, expected):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "test-key")
    monkeypatch.delenv("GATEWAY_LOG_FORMAT", raising=False)
    if value is not None:
        monkeypatch.setenv("GATEWAY_LOG_FORMAT", value)
    assert Settings.from_env().log_format == expected


def test_an_unknown_log_format_stops_the_gateway_with_a_clear_message(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "test-key")
    monkeypatch.setenv("GATEWAY_LOG_FORMAT", "JSON ")
    with pytest.raises(SystemExit, match="GATEWAY_LOG_FORMAT must be one of text, json"):
        Settings.from_env()


def test_an_admin_key_needs_an_admin_email_and_never_shows_in_repr(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "test-key")
    monkeypatch.delenv("GATEWAY_ADMIN_EMAIL", raising=False)
    monkeypatch.setenv("GATEWAY_ADMIN_KEY", "wmcp_secret-value")
    with pytest.raises(SystemExit, match="GATEWAY_ADMIN_EMAIL is not"):
        Settings.from_env()
    monkeypatch.setenv("GATEWAY_ADMIN_EMAIL", "root@example.com")
    settings = Settings.from_env()
    assert settings.admin_key == "wmcp_secret-value" and "secret-value" not in repr(settings)
