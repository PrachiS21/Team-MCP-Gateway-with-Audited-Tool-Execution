"""Fails if anything that looks like a real key or token is in a file git would push.

  python3 scripts/scan_secrets.py          run it before every push; exit code 1 = something found

It checks the files git tracks plus new files git would pick up (it respects .gitignore, so .env files and
private notes are not scanned because they never leave the machine). Findings show the file, the line and the kind
of key, never the value. Why: an LLM key was once pasted into scripts/set-llm-key.sh; nothing caught it.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# kind -> pattern. Each needs a long random-looking tail, so names like "ck_..." in prose do not match.
PATTERNS = {
    "Groq API key": r"gsk_[A-Za-z0-9]{20,}",
    "xAI API key": r"xai-[A-Za-z0-9]{20,}",
    "OpenAI-style API key": r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}",
    "Anthropic API key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "gateway API key": r"wmcp_[0-9a-f]{8}_[0-9a-f]{64}",
    "WooCommerce consumer key": r"ck_[0-9a-f]{40}",
    "WooCommerce consumer secret": r"cs_[0-9a-f]{40}",
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}
_COMPILED = {kind: re.compile(p) for kind, p in PATTERNS.items()}


def looks_fake(value: str) -> bool:
    """Test data such as wmcp_00000000_000…0: a tail made of one repeated character."""
    tail = re.split(r"[_-]", value, maxsplit=1)[-1].replace("_", "").replace("-", "")
    return len(set(tail)) <= 1


def files_to_scan(root: Path) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                         cwd=root, capture_output=True, check=True).stdout
    return [root / name for name in out.decode().split("\0") if name]


def scan(paths: list[Path], root: Path) -> list[tuple[str, int, str]]:
    findings = []
    for path in paths:
        try:
            text = path.read_text(errors="ignore")
        except (OSError, UnicodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for kind, pattern in _COMPILED.items():
                if any(not looks_fake(m.group(0)) for m in pattern.finditer(line)):
                    findings.append((str(path.relative_to(root)), number, kind))
    return findings


def main(root: Path = ROOT) -> int:
    findings = scan(files_to_scan(root), root)
    for file, number, kind in findings:
        print(f"{file}:{number}: looks like a {kind} (value not shown)")
    if findings:
        print(f"\n{len(findings)} possible secret(s). Remove them, and rotate any real key that was ever in a file.")
        return 1
    print("no secrets found in the files git would push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
