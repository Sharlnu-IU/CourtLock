"""Repository rules that must never silently regress."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_DIRS = ("src", "tests", "scripts", "alembic")

# Built from pieces so this file does not trip its own scan.
FORBIDDEN_SUPPRESSIONS = (
    "no" + "qa",
    "type:" + " ignore",
    "pyright:" + " ignore",
    "fmt:" + " off",
    "fmt:" + " skip",
    "pylint:" + " disable",
)


def python_files() -> list[Path]:
    return [p for d in CODE_DIRS for p in (ROOT / d).rglob("*.py")]


def test_there_are_no_inline_suppressions() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}: {marker}"
        for path in python_files()
        for marker in FORBIDDEN_SUPPRESSIONS
        if marker in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_identity_headers_are_read_in_exactly_one_module() -> None:
    """NFR-6: swapping the identity scheme must be a one-file change."""
    headers = ("x-member-id", "x-staff-key")
    users = {
        path.relative_to(ROOT / "src").as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if any(h in path.read_text(encoding="utf-8").lower() for h in headers)
    }
    assert users == {"courtlock/api/deps.py"}


def test_services_never_touch_http_or_headers() -> None:
    for path in (ROOT / "src" / "courtlock" / "services").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "fastapi" not in text, path.name
        assert "Request" not in text, path.name


def test_domain_is_pure() -> None:
    forbidden = ("sqlalchemy", "fastapi", "psycopg")
    for path in (ROOT / "src" / "courtlock" / "domain").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(name in text for name in forbidden), path.name
