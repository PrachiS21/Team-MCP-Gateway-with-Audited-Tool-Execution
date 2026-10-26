"""The secret scan: it must catch each kind of key, never print the value, respect .gitignore, and pass on this repo."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scan_secrets.py"
spec = importlib.util.spec_from_file_location("scan_secrets", SCRIPT)
scan_secrets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan_secrets)

# Built at run time, so this test file itself contains no key-shaped literal.
REAL_LOOKING = {
    "Groq API key": "gsk_" + "Ab3" * 12,
    "xAI API key": "xai-" + "Zq7" * 10,
    "OpenAI-style API key": "sk-proj-" + "Xy9" * 10,
    "gateway API key": "wmcp_1a2b3c4d_" + "9f8e" * 16,
    "WooCommerce consumer key": "ck_" + "a1b2" * 10,
    "WooCommerce consumer secret": "cs_" + "c3d4" * 10,
}


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("Keys look like ck_... and gsk_...; never commit one.\n")
    return tmp_path


@pytest.mark.parametrize("kind, value", REAL_LOOKING.items())
def test_each_kind_of_key_is_caught_without_printing_it(repo, capsys, kind, value):
    (repo / "scripts").mkdir()
    (repo / "scripts" / "set-key.sh").write_text(f'#!/bin/sh\nKEY="{value}"\n')
    assert scan_secrets.main(repo) == 1
    out = capsys.readouterr().out
    assert f"scripts/set-key.sh:2: looks like a {kind}" in out and value not in out


def test_a_clean_repo_and_obvious_test_values_pass(repo):
    (repo / "test.py").write_text('KEY = "wmcp_00000000_' + "0" * 64 + '"\n')
    assert scan_secrets.main(repo) == 0


def test_git_ignored_files_are_not_scanned(repo):
    (repo / ".gitignore").write_text(".env\n")
    (repo / ".env").write_text(REAL_LOOKING["Groq API key"] + "\n")
    assert scan_secrets.main(repo) == 0


def test_this_repository_is_clean():
    assert scan_secrets.main() == 0
