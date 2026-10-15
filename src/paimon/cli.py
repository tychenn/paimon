"""Local development checks, without desktop actions or network requests."""

import argparse
import json
import platform
import sqlite3
import tomllib
from importlib.metadata import version
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(prog="paimon", description=__doc__)
    parser.add_argument("--version", action="version", version=version("paimon"))
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Check the local Python development environment.")
    doctor.add_argument("--config", type=Path, help="Optionally check a TOML file's syntax.")
    args = parser.parse_args()

    if args.config is not None:
        try:
            with args.config.open("rb") as stream:
                tomllib.load(stream)
        except (OSError, ValueError) as exc:
            parser.error(f"Cannot read TOML configuration: {exc}")

    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        parser.error(f"SQLite check failed: {exc}")

    print(
        json.dumps(
            {
                "stage": "development_bootstrap",
                "version": version("paimon"),
                "python": platform.python_version(),
                "platform": platform.system(),
                "architecture": platform.machine(),
                "sqlite": "ok",
                "config_syntax": "ok" if args.config is not None else "not_checked",
                "desktop_bridge": "not_implemented",
                "model_connection": "not_implemented",
                "parameter_training": "not_implemented",
            },
            indent=2,
        )
    )
    return 0
