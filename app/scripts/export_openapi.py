"""
Write the API's OpenAPI schema to a file, for generating clients.

    uv run python -m app.scripts.export_openapi --output openapi.json

Without ``--output`` the schema is printed. The app serves the same document
at ``/openapi.json``, but only in debug mode and only while it runs; this
needs neither a running server nor a database, because the schema is built
from the routes and nothing connects until the app starts.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from app.main import app


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Write the API's OpenAPI schema.")
    parser.add_argument("--output", type=Path, help="the file to write; printed when left out")
    args = parser.parse_args(argv)

    # Indented and newline-terminated: the file is committed next to the
    # client generated from it, and a change in the API should read as a diff.
    schema = json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n"

    if args.output is None:
        sys.stdout.write(schema)
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(schema, encoding="utf-8")


if __name__ == "__main__":
    main()
