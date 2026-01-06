from __future__ import annotations

import argparse

from app.db import migrate_db


def main() -> int:
    parser = argparse.ArgumentParser(description="Life OS management commands")
    parser.add_argument(
        "command",
        choices=["migrate"],
        help="Run database migrations",
    )
    args = parser.parse_args()

    if args.command == "migrate":
        migrate_db()
        print("Database migrated.")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
