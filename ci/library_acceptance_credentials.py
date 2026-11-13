"""Provision the acceptance worker's private libpq password file."""

import json
import os
import re
import sys
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict


def password(text):
    """Read a bare password, a connection string, or a labeled password."""
    trimmed = text.lstrip("\ufeff").strip()
    if trimmed.startswith("{"):
        config = json.loads(trimmed)
        if isinstance(config, dict):
            for field, value in config.items():
                if field.casefold() in {
                    "password",
                    "pgpassword",
                    "db_password",
                    "pass",
                    "pwd",
                }:
                    return value if isinstance(value, str) else None
    if "=" in trimmed and re.search(r"\bpassword\s*=", trimmed, re.I):
        try:
            value = conninfo_to_dict(trimmed).get("password")
        except Exception:
            value = None
        if value:
            return value
    lines = [
        line.strip()
        for line in text.lstrip("\ufeff").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for index, line in enumerate(lines):
        line = line.strip("`")
        if line.startswith(("postgresql://", "postgres://")):
            return conninfo_to_dict(line).get("password")
        if line.startswith("192.168.0.59:5432:metadata_test:metadata_test:"):
            return re.sub(r"\\(.)", r"\1", line.split(":", 4)[4])
        match = re.match(
            r"(?:export\s+)?[\s*\-\"']*"
            r"(?:PGPASSWORD|[\w-]*PASSWORD|pass|pwd)[\s*\"']*[:=]\s*"
            r"[\s*]*(.*)",
            line,
            re.I,
        )
        if match:
            value = match[1].strip()
            if not value and index + 1 < len(lines):
                value = lines[index + 1]
            if value.endswith(","):
                value = value[:-1].rstrip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'`":
                return value[1:-1]
            return value
    if len(lines) == 2 and lines[0] == "metadata_test":
        return lines[1]
    if len(lines) == 5 and lines[:4] == [
        "192.168.0.59",
        "5432",
        "metadata_test",
        "metadata_test",
    ]:
        return lines[4]
    if len(lines) == 1:
        line = lines[0]
        if re.match(r"(?:host|port|dbname|database|user)\s*=", line, re.I):
            return conninfo_to_dict(line).get("password")
        return line
    return None


def main():
    """Write credentials atomically without logging their values."""
    destination = Path(sys.argv[1])
    try:
        text = sys.stdin.read()
        value = password(text)
    except (ValueError, TypeError):
        value = None
    except Exception:
        raise SystemExit("Unsupported PostgreSQL credentials format.") from None
    if not value or any(char in value for char in "\r\n\0"):
        raise SystemExit(
            "Unrecognized credential layout "
            f"({len(text.splitlines())} lines; "
            f"colon fields={sum(':' in line for line in text.splitlines())}; "
            f"equals fields={sum('=' in line for line in text.splitlines())})."
        )
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
