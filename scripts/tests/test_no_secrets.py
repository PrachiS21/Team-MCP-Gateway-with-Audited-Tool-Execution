"""No real-looking secret in any file that would be pushed to GitHub.

Scans what git would publish: tracked files plus new files that .gitignore does not exclude. Run before every push
(scripts/tests/run.sh). A value made of one repeated character (test placeholders) is not counted as a secret.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PATTERNS = {
    "Groq API key": r"gsk_[A-Za-z0-9]{20,}",
    "xAI API key": r"xai-[A-Za-z0-9]{20,}",
    "OpenAI / Anthropic style key": r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}",
    "gateway API key": r"wmcp_[0-9a-f]{8}_[0-9a-f]{64}",
    "WooCommerce consumer key": r"\bck_[0-9a-f]{40}\b",
    "WooCommerce consumer secret": r"\bcs_[0-9a-f]{40}\b",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".woff", ".woff2", ".lock"}


def publishable_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / p for p in out.split("\0") if p and (ROOT / p).is_file() and Path(p).suffix not in SKIP_SUFFIXES]


def placeholder(value: str) -> bool:
    tail = re.sub(r"^[a-z]+[_-](?:ant-)?(?:[0-9a-f]{8}_)?", "", value)
    return len(set(tail)) <= 2  # "ffff…", "aaaa…": made up for a test


def find_secrets(text: str) -> list[str]:
    return [name for name, rx in PATTERNS.items() for m in re.finditer(rx, text) if not placeholder(m.group(0))]


def test_the_scanner_catches_real_looking_keys_and_ignores_placeholders():
    fake = "wmcp_6437243c_" + "3f9a1c0b7e" * 6 + "1234"
    assert find_secrets(f"key={fake}") == ["gateway API key"]
    assert find_secrets("gsk_" + "Ab3dE9xQ" * 6) == ["Groq API key"]
    assert find_secrets("ck_" + "0123456789abcdef0123456789abcdef01234567") == ["WooCommerce consumer key"]
    assert find_secrets("wmcp_0123abcd_" + "f" * 64) == []  # a test placeholder
    assert find_secrets("Bearer wmcp_… and ck_… in prose") == []


def test_no_file_that_would_be_pushed_contains_a_secret():
    found = []
    for path in publishable_files():
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for name in find_secrets(text):
            found.append(f"{path.relative_to(ROOT)}: {name}")  # never print the value itself
    assert not found, "remove these before pushing (and rotate the key):\n" + "\n".join(found)
