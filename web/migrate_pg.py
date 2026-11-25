"""Apply versioned SQL migrations to PostgreSQL."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
VERSION_RE = re.compile(r"^(\d+)_.*\.sql$")


def list_migrations() -> list[tuple[str, Path]]:
    items = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        match = VERSION_RE.match(path.name)
        if not match:
            continue
        items.append((match.group(1), path))
    return items


def split_sql(script: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def applied_versions(cur) -> set[str]:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    cur.execute("SELECT version FROM schema_migrations")
    return {row["version"] for row in cur.fetchall()}


def apply_migrations() -> list[str]:
    from web.pg import connection

    applied: list[str] = []
    with connection() as conn:
        with conn.cursor() as cur:
            done = applied_versions(cur)
            for version, path in list_migrations():
                if version in done:
                    continue
                for statement in split_sql(path.read_text(encoding="utf-8")):
                    cur.execute(statement)
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s) "
                    "ON CONFLICT (version) DO NOTHING",
                    (version,),
                )
                applied.append(version)
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply PostgreSQL migrations")
    parser.parse_args(argv)
    try:
        applied = apply_migrations()
    except Exception as error:  # noqa: BLE001
        print("Migration failed: {}".format(error), file=sys.stderr)
        return 1
    if applied:
        print("Applied: {}".format(", ".join(applied)))
    else:
        print("No pending migrations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
