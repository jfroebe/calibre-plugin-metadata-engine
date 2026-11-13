"""Provision the acceptance worker's private libpq password file."""

import os
import re
import sys
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict


def password(text):
    """Read a bare password, a connection string, or a labeled password."""
    text = text.rstrip("\r\n")
    if text.startswith(("postgresql://", "postgres://")):
        return conninfo_to_dict(text).get("password")
    for line in text.splitlines():
        if line.startswith("192.168.0.59:5432:metadata_test:metadata_test:"):
            return re.sub(r"\\(.)", r"\1", line.split(":", 4)[4])
        match = re.match(
            r"\s*(?:export\s+)?(?:PGPASSWORD|password|pass)\s*[:=]\s*(.+)", line, re.I
        )
        if match:
            value = match[1].strip()
            return (
                value[1:-1]
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
                else value
            )
    if "\n" not in text and text:
        if re.match(r"(?:host|port|dbname|database|user)\s*=", text, re.I):
            return conninfo_to_dict(text).get("password")
        return text
    return None


def main():
    """Write credentials atomically without logging their values."""
    destination = Path(sys.argv[1])
    try:
        value = password(sys.stdin.read())
    except (ValueError, TypeError):
        value = None
    except Exception:
        raise SystemExit("Unsupported PostgreSQL credentials format.") from None
    if not value or any(char in value for char in "\r\n\0"):
        raise SystemExit("Could not read a PostgreSQL password from the worker file.")
    os.umask(0o077)
    temporary = destination.with_suffix(".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            escaped = value.replace("\\", "\\\\").replace(":", "\\:")
            output.write(
                "192.168.0.59:5432:metadata_test:metadata_test:" + escaped + "\n"
            )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
