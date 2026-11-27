"""Run the README's curl examples against a live stack, so the README cannot drift.

Extracts the `bash` blocks from the variable set-up and from every numbered API section
(1-7), runs them in order in one shell, and checks the observable results.

Needs: bash, curl, jq and a running stack (default http://localhost:8000). Section 5 books a
12-week series on a fixed court and time, so run it against a fresh database
(`docker compose down -v && docker compose up -d --wait`); a second run would see its own
earlier series and report a conflict.
Usage: python scripts/readme_check.py [--base-url URL]
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"
BASH_BLOCK = re.compile(r"```bash\n(.*?)\n```", re.DOTALL)
DEFAULT_BASE = "BASE=http://localhost:8000"

# Things that must appear in the combined output if the documented behaviour is real.
EXPECTED = (
    '"status": "AVAILABLE"',  # section 1: availability
    '"status": "HELD"',  # section 2: hold
    '"code": "slot_unavailable"',  # section 2: Bob is refused
    '"status": "CONFIRMED"',  # section 3: confirm
    "idempotent-replay: true",  # section 3: retrying confirm
    '"status": "CANCELLED"',  # section 4: cancel
    '"refund_percent": 100',  # section 4: >= 24 h notice
    '"refund_percent": 0',  # section 4: late cancellation
    '"series_id"',  # section 5: the series was booked
    '"code": "series_conflict"',  # section 5: a taken week refuses the whole series
    '"utilization_pct"',  # section 6: utilization report
    '"venue_name"',  # section 7: court listing
)


def sections(text: str) -> list[tuple[str, str]]:
    """Split on level-2 and level-3 headings: [(heading, body), ...]."""
    parts = re.split(r"^(#{2,3} .*)$", text, flags=re.MULTILINE)
    return [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def runnable_blocks(text: str) -> list[str]:
    """Blocks of the set-up subsection and of API sections 1-7 (their subsections included)."""
    blocks: list[str] = []
    parent = ""  # the most recent level-2 heading; level-3 headings inherit from it
    for heading, body in sections(text):
        if heading.startswith("## "):
            parent = heading
        is_setup = heading.startswith("### Set up variables")
        is_api = re.match(r"## [1-7]\. ", parent) is not None
        if is_setup or is_api:
            blocks.extend(BASH_BLOCK.findall(body))
    return blocks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    blocks = runnable_blocks(README.read_text(encoding="utf-8"))
    if not blocks:
        print("no runnable bash blocks found in README.md", file=sys.stderr)
        return 1
    script = "\n".join(blocks).replace(DEFAULT_BASE, f"BASE={args.base_url}")
    if f"BASE={args.base_url}" not in script:
        print("README no longer sets BASE=http://localhost:8000", file=sys.stderr)
        return 1

    result = subprocess.run(
        ["bash", "-c", "set -u\n" + script],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    output = result.stdout + result.stderr
    print(output)

    problems = [f"exit code {result.returncode}"] if result.returncode != 0 else []
    problems += [f"missing in output: {needle}" for needle in EXPECTED if needle not in output]
    if re.search(r"^null$", output, flags=re.MULTILINE):
        problems.append("a value that should have been an id came back as null")
    for problem in problems:
        print(f"README CHECK FAILED: {problem}", file=sys.stderr)
    if not problems:
        print(f"README check passed: {len(blocks)} blocks, {len(EXPECTED)} expectations met")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
