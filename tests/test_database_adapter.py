"""Exercise the database adapter against a real temporary SQLite view."""

import csv
import io
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.import_from_database import COLUMNS, export_view


@pytest.fixture
def source_view(tmp_path):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as connection:
        columns = ", ".join(f'"{column}" TEXT' for column in COLUMNS)
        connection.execute(f"CREATE TABLE source_values ({columns})")
        placeholders = ", ".join("?" for _ in COLUMNS)
        connection.executemany(
            f"INSERT INTO source_values VALUES ({placeholders})",
            [
                ("LS001", "funds_npl_balance", "2026-10-29", "123456789012345.12345678", "元", None, None, None, "raw"),
                ("LS002", "npl_ratio", "2026-10-28", "1.65", "%", "72", "82", "province_societies", "percent_number"),
            ],
        )
        connection.execute("CREATE VIEW risk_indicator_export AS SELECT * FROM source_values")
    return "sqlite:///" + str(path), path


def test_database_adapter_reads_view_with_date_filter_and_preserves_values(source_view):
    source_url, path = source_view
    payload = export_view(source_url, "risk_indicator_export", "2026-10-29")
    assert payload.startswith(b"\xef\xbb\xbf")
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    rows = list(reader)
    assert reader.fieldnames == COLUMNS
    assert len(rows) == 1
    assert rows[0]["org_code"] == "LS001"
    assert rows[0]["value"] == "123456789012345.12345678"
    assert rows[0]["rank"] == rows[0]["population"] == ""
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM source_values").fetchone()[0] == 2


def test_database_adapter_rejects_unsafe_identifiers_and_empty_results(source_view):
    source_url, _ = source_view
    for name in (
        "risk_indicator_export; DROP TABLE source_values",
        "a.b.c",
        "risk_indicator_export--",
        '"risk_indicator_export"',
        "",
    ):
        with pytest.raises(ValueError, match="来源必须"):
            export_view(source_url, name, "2026-10-29")
    with pytest.raises(ValueError, match="无来源记录"):
        export_view(source_url, "risk_indicator_export", "2026-10-30")

