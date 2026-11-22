"""The command that writes the API's schema to a file, for generating clients."""

import json
from pathlib import Path

import pytest

from app.main import app
from app.scripts.export_openapi import main


def test_export_writes_the_schema_the_app_serves(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "openapi.json"

    main(["--output", str(target)])

    written = json.loads(target.read_text(encoding="utf-8"))
    assert written == app.openapi()
    assert "/api/v1/tags" in written["paths"]
    # A file a person will diff: indented, and ending in a newline.
    assert target.read_text(encoding="utf-8").endswith("}\n")


def test_export_prints_the_schema_when_no_file_is_named(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main([])

    assert json.loads(capsys.readouterr().out) == app.openapi()
