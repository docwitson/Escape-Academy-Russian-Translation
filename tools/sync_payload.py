#!/usr/bin/env python3
"""Copy finalized translation tables into the distributable payload."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TABLE_COLUMNS = {
    "01_gameplay_ui.csv": [
        "segment_id",
        "asset_name",
        "source_row",
        "translation_ru",
    ],
    "02_dialogue.csv": [
        "segment_id",
        "asset_name",
        "target_asset_es",
        "line_id",
        "speaker_locked",
        "translation_ru",
    ],
    "03_subtitles.csv": [
        "segment_id",
        "asset_name",
        "target_asset_es",
        "cue_number",
        "translation_ru",
    ],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source_dir",
        type=Path,
        help="Directory containing the finalized full CSV tables",
    )
    return parser.parse_args()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def render_table(source: Path, columns: list[str]) -> tuple[bytes, list[dict[str, str]]]:
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    missing = [row.get("segment_id", "") for row in rows if not row.get("translation_ru", "").strip()]
    if missing:
        raise ValueError(f"Untranslated rows in {source.name}: {missing[:10]}")

    import io

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows({column: row.get(column, "") for column in columns} for row in rows)
    # Keep the UTF-8 BOM used by the original payload files so spreadsheet
    # editors on Windows detect Cyrillic reliably.
    return ("\ufeff" + output.getvalue()).encode("utf-8"), rows


def semantic_hash(rows: list[dict[str, str]]) -> str:
    semantic = [
        [row.get("segment_id", ""), row.get("translation_ru", "")]
        for row in rows
    ]
    encoded = json.dumps(
        semantic,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)


def main() -> int:
    args = parse_args()
    source_dir = args.source_dir.resolve()
    output_dir = PROJECT_ROOT / "ollama_pipeline" / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, object]] = {}

    for name, columns in TABLE_COLUMNS.items():
        data, rows = render_table(source_dir / name, columns)
        destination = output_dir / name
        atomic_write(destination, data)
        relative = destination.relative_to(PROJECT_ROOT).as_posix()
        files[relative] = {
            "rows": len(rows),
            "columns": columns,
            "sha256": sha256_bytes(data),
            "size": len(data),
            "semantic_sha256": semantic_hash(rows),
        }
        print(f"Synced {name}: {len(rows)} rows")

    payload = {
        "format_version": 1,
        "total_rows": sum(int(value["rows"]) for value in files.values()),
        "files": files,
    }
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write(PROJECT_ROOT / "manifests" / "payload.json", encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
