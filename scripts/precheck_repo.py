"""Pre-push safety check. Run from the repo root BEFORE `git push`:

    python scripts/precheck_repo.py

It lists what git would publish, then fails if any of those files contains a value from your local .env
(only the variable NAME and the file path are printed, never the value), a key-like string, or if a file
that must stay private (.env, logs, virtualenv, zip) is about to be published.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATTERNS = [("Anthropic-style key", r"sk-ant-[A-Za-z0-9_-]{20,}"), ("private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
            ("JWT-style token", r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.")]
FORBIDDEN = (".env", ".venv/", "logs/", "internal/", ".zip", ".sqlite")


def publishable_files():
    r = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        sys.exit("This folder is not a git repo yet. Run `git init`, then run this check again.")
    return [f for f in r.stdout.splitlines() if f]


def env_values():
    path = os.path.join(ROOT, ".env")
    vals = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if len(v) >= 10 and not v.startswith(("claude-", "http://localhost")):
                vals[k.strip()] = v
    return vals


def main():
    files = publishable_files()
    problems = []
    for f in files:
        norm = f.replace("\\", "/")
        if norm == ".env" or any(norm.startswith(p) or norm.endswith(p) for p in FORBIDDEN if p != ".env"):
            problems.append(f"private file would be published: {f}")
    env = env_values()
    for f in files:
        p = os.path.join(ROOT, f)
        if not os.path.isfile(p) or os.path.getsize(p) > 2_000_000:
            continue
        try:
            text = open(p, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for k, v in env.items():
            if v in text:
                problems.append(f"{f} contains the value of {k} from your .env")
        for label, pat in PATTERNS:
            if re.search(pat, text):
                problems.append(f"{f} looks like it contains a {label}")
    print(f"{len(files)} files would be published.")
    if problems:
        print("STOP. Do not push yet:")
        for x in problems:
            print("  -", x)
        sys.exit(1)
    print(f"OK: no secrets found ({len(env)} values from .env checked).")


if __name__ == "__main__":
    main()
