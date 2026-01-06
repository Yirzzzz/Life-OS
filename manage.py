from __future__ import annotations

import argparse

from app.db import init_db


def main() -> int:
    parser = argparse.ArgumentParser(description="Life OS management commands")
    parser.add_argument(
        "command",
        choices=["migrate"],
        help="Run database migrations",
    )
    args = parser.parse_args()

    if args.command == "migrate":
        init_db()
        print("Database migrated.")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
