import pytest

from gateway import __main__ as entry_point
from gateway.config import Settings, load_secrets_file


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


# -- GATEWAY_SECRETS_FILE: several secrets in one mounted file ----------------------------------------------------

def test_the_secrets_file_fills_unset_variables_and_never_overrides_the_environment(tmp_path):
    f = tmp_path / "env"
    f.write_text("# the gateway's secrets\nGATEWAY_SECRET_KEY=from-file\n\nGATEWAY_ADMIN_KEY=a=b\nGATEWAY_LLM_API_KEY=file\n")
    environ = {"GATEWAY_SECRETS_FILE": str(f), "GATEWAY_SECRET_KEY": "", "GATEWAY_LLM_API_KEY": "from-env"}
    assert load_secrets_file(environ) == ["GATEWAY_SECRET_KEY", "GATEWAY_ADMIN_KEY"]
    assert environ["GATEWAY_SECRET_KEY"] == "from-file"  # empty counts as unset
    assert environ["GATEWAY_ADMIN_KEY"] == "a=b"  # split at the first "=" only
    assert environ["GATEWAY_LLM_API_KEY"] == "from-env"  # the real environment wins


def test_without_a_secrets_file_nothing_changes():
    environ = {"X": "1"}
    assert load_secrets_file(environ) == [] and environ == {"X": "1"}


@pytest.mark.parametrize("content, says", [
    ("GATEWAY_SECRET_KEY s3cret-value\n", "line 1: expected NAME=value"),
    ("A=1\n2BAD=s3cret-value\n", "line 2: expected NAME=value"),
    ("export A=s3cret-value\n", "line 1: expected NAME=value"),  # shell syntax is not run, so it is refused
    ("A=1\nA=s3cret-value\n", "line 2: A is set twice"),
])
def test_a_malformed_secrets_file_stops_the_gateway_without_showing_a_value(tmp_path, content, says):
    f = tmp_path / "env"
    f.write_text(content)
    with pytest.raises(SystemExit) as e:
        load_secrets_file({"GATEWAY_SECRETS_FILE": str(f)})
    assert says in str(e.value) and "s3cret-value" not in str(e.value)


def test_a_missing_secrets_file_stops_the_gateway(tmp_path):
    with pytest.raises(SystemExit, match="cannot be read"):
        load_secrets_file({"GATEWAY_SECRETS_FILE": str(tmp_path / "missing")})


def test_the_gateway_reads_the_secrets_file_before_its_settings(tmp_path, monkeypatch):
    """`python -m gateway`: loaded any later, the GATEWAY_SECRET_KEY in the file would be missed."""
    f = tmp_path / "env"
    f.write_text("GATEWAY_SECRET_KEY=from-the-file\n")
    monkeypatch.setenv("GATEWAY_SECRETS_FILE", str(f))
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "")  # unset for the loader; monkeypatch restores the old value afterwards
    monkeypatch.setattr(entry_point, "setup_logging", lambda *a: None)
    seen = []

    class Stop(Exception):
        pass

    def create_gateway(settings):
        seen.append(settings)
        raise Stop

    monkeypatch.setattr(entry_point, "create_gateway", create_gateway)
    with pytest.raises(Stop):
        entry_point.main()
    assert seen[0].secret_key == "from-the-file"


# -- GATEWAY_SEED_JSON ----------------------------------------------------------------------------------------------

def test_the_seed_can_come_as_text_but_not_from_two_places_at_once(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "test-key")
    monkeypatch.delenv("GATEWAY_SEED_FILE", raising=False)
    monkeypatch.setenv("GATEWAY_SEED_JSON", '{"servers": []}')
    settings = Settings.from_env()
    assert settings.seed_json == '{"servers": []}' and settings.seed_file is None
    monkeypatch.setenv("GATEWAY_SEED_FILE", "/config/seed.json")
    with pytest.raises(SystemExit, match="not both"):
        Settings.from_env()


# -- the Dialogflow CX webhook ------------------------------------------------------------------------------------

def test_cx_is_off_by_default_and_its_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "test-key")
    for name in ("GATEWAY_CX_ENABLED", "GATEWAY_CX_TIMEOUT", "GATEWAY_CX_HISTORY_TURNS", "GATEWAY_CX_TOOLS"):
        monkeypatch.delenv(name, raising=False)
    s = Settings.from_env()
    assert (s.cx_enabled, s.cx_timeout, s.cx_history_turns, s.cx_tools) == (False, 25.0, 6, ())
    monkeypatch.setenv("GATEWAY_CX_ENABLED", "1")
    monkeypatch.setenv("GATEWAY_CX_TIMEOUT", "20")
    monkeypatch.setenv("GATEWAY_CX_HISTORY_TURNS", "0")
    monkeypatch.setenv("GATEWAY_CX_TOOLS", " woo__list_products, gst__calculate_gst ,")
    s = Settings.from_env()
    assert (s.cx_enabled, s.cx_timeout, s.cx_history_turns) == (True, 20.0, 0)
    assert s.cx_tools == ("woo__list_products", "gst__calculate_gst")


@pytest.mark.parametrize("name, value, says", [
    ("GATEWAY_CX_TIMEOUT", "30", "under 30 seconds"),  # Dialogflow CX gives up at 30
    ("GATEWAY_CX_TIMEOUT", "0.5", "at least 1"),
    ("GATEWAY_CX_TIMEOUT", "soon", "must be a number"),
    ("GATEWAY_CX_HISTORY_TURNS", "11", "between 0 and 10"),
    ("GATEWAY_CX_HISTORY_TURNS", "2.5", "whole number"),
    ("GATEWAY_CX_TOOLS", "woo__list products", "not a tool name"),
])
def test_a_bad_cx_setting_stops_the_gateway_with_a_clear_message(monkeypatch, name, value, says):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "test-key")
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match=says):
        Settings.from_env()
