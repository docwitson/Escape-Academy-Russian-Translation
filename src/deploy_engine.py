#!/usr/bin/env python3
"""Build, install, verify, or restore the Russian Escape Academy patch.

The patch reuses the internal English language slot. Unity TextAssets and one
guard in the managed game code are rewritten; every original is backed up.
"""

from __future__ import annotations

import argparse
import base64
import csv
import gc
import hashlib
import io
import json
import os
import re
import shutil
import struct
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import UnityPy


WORK_DIR = Path(__file__).resolve().parents[1]
GAME_ROOT = WORK_DIR.parent
GAME_BUNDLE = GAME_ROOT / "Escape Academy_Data" / "data.unity3d"
ADDRESSABLE_BUNDLE = (
    GAME_ROOT
    / "Escape Academy_Data"
    / "StreamingAssets"
    / "aa"
    / "StandaloneWindows64"
    / "areadioramas_assets_all_740d70d1014660f769811a969d8da879.bundle"
)
CATALOG = GAME_ROOT / "Escape Academy_Data" / "StreamingAssets" / "aa" / "catalog.json"
ASSEMBLY = GAME_ROOT / "Escape Academy_Data" / "Managed" / "Assembly-CSharp.dll"
MANIFEST_PATH = WORK_DIR / "manifests" / "assets.json"
OUTPUT_DIR = WORK_DIR / "ollama_pipeline" / "output"
DEPLOY_DIR = WORK_DIR / "deploy"
BUILD_DIR = DEPLOY_DIR / "build"
BACKUP_DIR = DEPLOY_DIR / "backup"
REPORT_PATH = DEPLOY_DIR / "deployment.json"
STAGED_BUNDLE = BUILD_DIR / "data.unity3d.russian"
ORIGINAL_BACKUP = BACKUP_DIR / "data.unity3d.original"
STAGED_ADDRESSABLE = BUILD_DIR / (ADDRESSABLE_BUNDLE.name + ".russian")
ORIGINAL_ADDRESSABLE_BACKUP = BACKUP_DIR / (ADDRESSABLE_BUNDLE.name + ".original")
STAGED_CATALOG = BUILD_DIR / "catalog.json.russian"
ORIGINAL_CATALOG_BACKUP = BACKUP_DIR / "catalog.json.original"
STAGED_ASSEMBLY = BUILD_DIR / "Assembly-CSharp.dll.russian"
ORIGINAL_ASSEMBLY_BACKUP = BACKUP_DIR / "Assembly-CSharp.dll.original"

ADDRESSABLE_ORIGINAL_SIZE = 1_148_263_689
ADDRESSABLE_ORIGINAL_CRC = 487_251_353
PATCH_VERSION = "0.1.2"
ASSEMBLY_ORIGINAL_SIZE = 1_879_040
ASSEMBLY_ORIGINAL_SHA256 = "d57eb2eeefa6bce26b9e0feb8992a5e9da3f428db8aa654219b4d5e62565576c"
# HintGraphics.ShowCoroutine and RefreshCoroutine dereference hint.camera after
# checking an unrelated singleton. Both replacements perform the intended null
# check and remain 13 bytes, so no metadata or branch offsets move.
HINT_GUARD_ORIGINAL = bytes.fromhex("7e751b000414284100000a2c31")
HINT_GUARD_PATCHES = (
    (0x1E4C9, bytes.fromhex("027b150600047b731b00042c31")),
    (0x1E7E7, bytes.fromhex("027b1a0600047b731b00042c31")),
)

TABLES = {
    "gameplay": OUTPUT_DIR / "01_gameplay_ui.csv",
    "dialogue": OUTPUT_DIR / "02_dialogue.csv",
    "subtitles": OUTPUT_DIR / "03_subtitles.csv",
}
YARN_HEADER = ["id", "text", "file", "node", "lineNumber"]
# A few shipped files contain harmless formatting mistakes such as a missing
# space around the arrow or five digits in the seconds field.  The game's SRT
# reader accepts them, so deployment must preserve rather than reject them.
SRT_TIME_RE = re.compile(r"^\S.*-->.*\S$")

# Entries absent from the model table retain their original English value.
MANUAL_GAMEPLAY: dict[tuple[str, int], str] = {}

# Four tutorial-waiver paragraphs are literal TextMeshProUGUI values rather
# than entries in GameplayStrings. Patch the English GameObjects because the
# Russian translation deliberately keeps English puzzle and poster variants.
EMBEDDED_UI = {
    ("level8", 12803): (
        "I hereby assume all risks of walking with",
        "Я беру на себя все риски, связанные с ходьбой с",
    ),
    ("level8", 12805): (
        "Sprinting with          is not permitted. (But who's gonna stop you?)\r",
        "Бег с          запрещён. (Но кто тебя остановит?)\r",
    ),
    ("level8", 12808): (
        "and interacting with ",
        "и взаимодействием с ",
    ),
    ("level8", 12876): (
        "I agree to look at my inventory<br>with",
        "Я согласен просматривать инвентарь<br>с",
    ),
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_csv_text(text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(normalize_newlines(text), newline=""))
    if reader.fieldnames is None:
        raise ValueError("CSV has no header")
    return list(reader.fieldnames), [
        {key: value or "" for key, value in row.items() if key is not None} for row in reader
    ]


def serialize_csv(fieldnames: list[str], rows: list[dict[str, str]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def parse_srt(text: str) -> list[dict[str, str]]:
    lines = normalize_newlines(text).split("\n")
    cues: list[dict[str, str]] = []
    index = 0
    while index < len(lines):
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index >= len(lines):
            break
        cue_number = lines[index].strip()
        index += 1
        if index >= len(lines) or not SRT_TIME_RE.match(lines[index].strip()):
            raise ValueError(f"Malformed SRT cue {cue_number!r}")
        timecode = lines[index].strip()
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip():
            body.append(lines[index])
            index += 1
        cues.append({"cue_number": cue_number, "timecode": timecode, "text": "\n".join(body)})
    return cues


def serialize_srt(cues: list[dict[str, str]]) -> str:
    blocks = [f"{cue['cue_number']}\n{cue['timecode']}\n{cue['text']}" for cue in cues]
    return "\n\n".join(blocks) + "\n"


def replace_serialized_string(raw: bytes, old: str, new: str) -> bytes:
    old_bytes = old.encode("utf-8")
    new_bytes = new.encode("utf-8")
    old_padding = (-len(old_bytes)) % 4
    new_padding = (-len(new_bytes)) % 4
    needle = struct.pack("<i", len(old_bytes)) + old_bytes + b"\0" * old_padding
    replacement = struct.pack("<i", len(new_bytes)) + new_bytes + b"\0" * new_padding
    if raw.count(needle) != 1:
        raise ValueError(f"Expected exactly one serialized {old!r} string")
    return raw.replace(needle, replacement, 1)


def build_gameplay_texts(manifest: dict, by_object: dict) -> dict[tuple[str, int], str]:
    translated = {
        (row["asset_name"], int(row["source_row"])): row["translation_ru"]
        for row in load_csv(TABLES["gameplay"])
    }
    translated.update(MANUAL_GAMEPLAY)
    result: dict[tuple[str, int], str] = {}
    for asset in manifest["assets"]["gameplay"]:
        copy = asset["copies"][0]
        obj = by_object[(copy["asset_file"], copy["path_id"])]
        data = obj.read()
        if sha256_text(data.m_Script) != copy["sha256"]:
            raise ValueError(f"Original gameplay asset changed: {asset['name']}")
        fields, rows = parse_csv_text(data.m_Script)
        if len(rows) != asset["row_count"]:
            raise ValueError(f"Gameplay row count changed: {asset['name']}")
        for row_number, row in enumerate(rows, start=2):
            target = translated.get((asset["name"], row_number))
            if target is not None:
                row["Value-En"] = target
        text = serialize_csv(fields, rows)
        for copy in asset["copies"]:
            result[(copy["asset_file"], copy["path_id"])] = text
    return result


def build_dialogue_texts(manifest: dict, by_object: dict) -> dict[tuple[str, int], str]:
    grouped: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for row in load_csv(TABLES["dialogue"]):
        grouped[row["asset_name"]][row["line_id"]] = row

    result: dict[tuple[str, int], str] = {}
    for asset in manifest["assets"]["dialogue"]:
        source_copy = asset["source_copies"][0]
        source_obj = by_object[(source_copy["asset_file"], source_copy["path_id"])]
        source_data = source_obj.read()
        if sha256_text(source_data.m_Script) != source_copy["sha256"]:
            raise ValueError(f"Original dialogue asset changed: {asset['source_name']}")
        fields, rows = parse_csv_text(source_data.m_Script)
        if fields != YARN_HEADER or len(rows) != asset["row_count"]:
            raise ValueError(f"Unexpected Yarn structure: {asset['source_name']}")
        translations = grouped[asset["source_name"]]
        for row in rows:
            translated = translations.get(row["id"])
            if translated is None:
                continue
            speaker = translated["speaker_locked"]
            body = translated["translation_ru"]
            # VisualNovelManager requires every displayed line to contain a
            # colon.  Yarn exports sound effects and narration with an empty
            # speaker as ":text"; dropping that prefix calls SkipCutscene().
            if speaker:
                row["text"] = f"{speaker}:{body}"
            else:
                source_text = row["text"].strip('"')
                row["text"] = f":{body}" if source_text.startswith(":") else body
        missing_separator = [row["id"] for row in rows if ":" not in row["text"].strip('"')]
        if missing_separator:
            raise ValueError(
                f"Yarn lines without speaker separator in {asset['source_name']}: "
                + ", ".join(missing_separator[:10])
            )
        text = serialize_csv(fields, rows)
        for copy in asset["source_copies"]:
            result[(copy["asset_file"], copy["path_id"])] = text
    return result


def dialogue_texts_by_english_name(
    manifest: dict,
    dialogue_patches: dict[tuple[str, int], str],
) -> dict[str, str]:
    """Return the rendered Russian Yarn CSV for every named English target."""
    result: dict[str, str] = {}
    for asset in manifest["assets"]["dialogue"]:
        target_name = asset["source_name"]
        target_copies = asset["source_copies"]
        if not target_copies:
            continue
        key = (target_copies[0]["asset_file"], target_copies[0]["path_id"])
        text = dialogue_patches.get(key)
        if text is not None:
            result[target_name] = text
    return result


def build_subtitle_texts(
    manifest: dict,
    by_object: dict,
) -> dict[tuple[str, int], str]:
    grouped: dict[str, dict[str, str]] = defaultdict(dict)
    for row in load_csv(TABLES["subtitles"]):
        grouped[row["asset_name"]][row["cue_number"]] = row["translation_ru"]

    result: dict[tuple[str, int], str] = {}
    for asset in manifest["assets"]["subtitles"]:
        source_copy = asset["source_copies"][0]
        source_obj = by_object[(source_copy["asset_file"], source_copy["path_id"])]
        source_data = source_obj.read()
        if sha256_text(source_data.m_Script) != source_copy["sha256"]:
            raise ValueError(f"Original subtitle asset changed: {asset['source_name']}")
        cues = parse_srt(source_data.m_Script)
        if len(cues) != asset["cue_count"]:
            raise ValueError(f"Subtitle cue count changed: {asset['source_name']}")
        translations = grouped[asset["source_name"]]
        for cue in cues:
            if cue["cue_number"] in translations:
                cue["text"] = translations[cue["cue_number"]]
        text = serialize_srt(cues)

        targets = list(asset["source_copies"])
        for copy in targets:
            result[(copy["asset_file"], copy["path_id"])] = text
    return result


def build_assembly_patch(source: Path) -> dict:
    """Patch the camera-less hint crash in the supported managed assembly."""
    if source.stat().st_size != ASSEMBLY_ORIGINAL_SIZE:
        raise ValueError("Assembly-CSharp.dll has an unsupported size")
    original_hash = sha256_file(source)
    if original_hash != ASSEMBLY_ORIGINAL_SHA256:
        raise ValueError("Assembly-CSharp.dll is not the supported original version")
    raw = bytearray(source.read_bytes())
    for offset, replacement in HINT_GUARD_PATCHES:
        end = offset + len(HINT_GUARD_ORIGINAL)
        if bytes(raw[offset:end]) != HINT_GUARD_ORIGINAL:
            raise ValueError(
                f"The expected HintGraphics instruction sequence was not found at {offset:#x}"
            )
        raw[offset:end] = replacement
    STAGED_ASSEMBLY.write_bytes(raw)
    return {
        "path": str(ASSEMBLY.relative_to(GAME_ROOT)),
        "original_size": source.stat().st_size,
        "original_sha256": original_hash,
        "staged_size": STAGED_ASSEMBLY.stat().st_size,
        "staged_sha256": sha256_file(STAGED_ASSEMBLY),
        "hint_guard_offsets": [offset for offset, _ in HINT_GUARD_PATCHES],
    }


def verify_assembly(path: Path, report: dict) -> None:
    assembly = report["assembly"]
    if path.stat().st_size != assembly["staged_size"]:
        raise ValueError("Patched Assembly-CSharp.dll size mismatch")
    if sha256_file(path) != assembly["staged_sha256"]:
        raise ValueError("Patched Assembly-CSharp.dll checksum mismatch")
    raw = path.read_bytes()
    offsets = assembly["hint_guard_offsets"]
    expected = dict(HINT_GUARD_PATCHES)
    if offsets != list(expected):
        raise ValueError("HintGraphics guard patch list is incomplete")
    for offset in offsets:
        replacement = expected.get(offset)
        if replacement is None or raw[offset : offset + len(replacement)] != replacement:
            raise ValueError(f"HintGraphics guard patch is missing at {offset:#x}")


def ensure_sources() -> dict:
    if not GAME_BUNDLE.is_file():
        raise FileNotFoundError(GAME_BUNDLE)
    if not ADDRESSABLE_BUNDLE.is_file():
        raise FileNotFoundError(ADDRESSABLE_BUNDLE)
    if not ASSEMBLY.is_file():
        raise FileNotFoundError(ASSEMBLY)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for path in TABLES.values():
        if not path.is_file():
            raise FileNotFoundError(path)
    return manifest


def build_catalog_patch() -> dict:
    """Store the modified bundle size and disable its local CRC check.

    Addressables stores offsets into this base64 blob elsewhere in the catalog,
    so the replacement must remain byte-for-byte the same length.  Unity treats
    CRC zero as "do not validate"; JSON whitespace preserves the record width.
    """
    if not CATALOG.is_file():
        raise FileNotFoundError(CATALOG)
    catalog_source = ORIGINAL_CATALOG_BACKUP if ORIGINAL_CATALOG_BACKUP.is_file() else CATALOG
    catalog_text = catalog_source.read_text(encoding="utf-8-sig")
    catalog_data = json.loads(catalog_text)
    encoded = catalog_data["m_ExtraDataString"]
    raw = base64.b64decode(encoded)

    old_crc = f'"m_Crc":{ADDRESSABLE_ORIGINAL_CRC}'.encode("utf-16le")
    crc_width = len(str(ADDRESSABLE_ORIGINAL_CRC))
    patched_crc = ('"m_Crc":0' + " " * (crc_width - 1)).encode("utf-16le")
    if raw.count(old_crc) != 1:
        raise ValueError("Could not uniquely locate the Addressables CRC in catalog.json")
    raw = raw.replace(old_crc, patched_crc, 1)

    old_size_text = str(ADDRESSABLE_ORIGINAL_SIZE)
    new_size_text = str(STAGED_ADDRESSABLE.stat().st_size)
    if len(old_size_text) != len(new_size_text):
        raise ValueError("Patched Addressables size no longer fits the catalog record")
    old_size = f'"m_BundleSize":{old_size_text}'.encode("utf-16le")
    new_size = f'"m_BundleSize":{new_size_text}'.encode("utf-16le")
    if raw.count(old_size) != 1:
        raise ValueError("Could not uniquely locate the Addressables size in catalog.json")
    raw = raw.replace(old_size, new_size, 1)

    replacement = base64.b64encode(raw).decode("ascii")
    if len(replacement) != len(encoded):
        raise ValueError("Catalog payload length changed unexpectedly")
    staged_text = catalog_text.replace(encoded, replacement, 1)
    STAGED_CATALOG.write_text(staged_text, encoding="utf-8")
    return {
        "path": str(CATALOG.relative_to(GAME_ROOT)),
        "original_size": catalog_source.stat().st_size,
        "original_sha256": sha256_file(catalog_source),
        "staged_size": STAGED_CATALOG.stat().st_size,
        "staged_sha256": sha256_file(STAGED_CATALOG),
        "addressable_crc": 0,
        "crc_validation_disabled": True,
        "addressable_size": STAGED_ADDRESSABLE.stat().st_size,
    }


def verify_catalog(path: Path, report: dict) -> None:
    catalog = report["catalog"]
    if sha256_file(path) != catalog["staged_sha256"]:
        raise ValueError("Patched catalog checksum mismatch")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    raw = base64.b64decode(data["m_ExtraDataString"])
    crc_width = len(str(ADDRESSABLE_ORIGINAL_CRC))
    expected_crc = ('"m_Crc":0' + " " * (crc_width - 1)).encode("utf-16le")
    expected_size = f'"m_BundleSize":{catalog["addressable_size"]}'.encode("utf-16le")
    if raw.count(expected_crc) != 1 or raw.count(expected_size) != 1:
        raise ValueError("Patched catalog does not contain the expected Addressables options")


def verify_staged_files(report: dict) -> None:
    """Verify staged output without loading multi-gigabyte Unity archives."""
    expected = (
        (STAGED_BUNDLE, report["staged_size"], report["staged_sha256"]),
        (
            STAGED_ADDRESSABLE,
            report["addressable"]["staged_size"],
            report["addressable"]["staged_sha256"],
        ),
        (STAGED_CATALOG, report["catalog"]["staged_size"], report["catalog"]["staged_sha256"]),
        (
            STAGED_ASSEMBLY,
            report["assembly"]["staged_size"],
            report["assembly"]["staged_sha256"],
        ),
    )
    for path, size, checksum in expected:
        if not path.is_file() or path.stat().st_size != size:
            raise ValueError(f"Staged file is missing or has an unexpected size: {path.name}")
        if sha256_file(path) != checksum:
            raise ValueError(f"Staged file checksum mismatch: {path.name}")
    verify_catalog(STAGED_CATALOG, report)
    verify_assembly(STAGED_ASSEMBLY, report)
    print("Staged file checksums verified.", flush=True)


def build() -> dict:
    manifest = ensure_sources()
    expected_original_size = manifest["game_bundle_size"]
    game_source = ORIGINAL_BACKUP if ORIGINAL_BACKUP.is_file() else GAME_BUNDLE
    addressable_source = (
        ORIGINAL_ADDRESSABLE_BACKUP
        if ORIGINAL_ADDRESSABLE_BACKUP.is_file()
        else ADDRESSABLE_BUNDLE
    )
    assembly_source = ORIGINAL_ASSEMBLY_BACKUP if ORIGINAL_ASSEMBLY_BACKUP.is_file() else ASSEMBLY
    if game_source.stat().st_size != expected_original_size:
        raise ValueError("Original data.unity3d is unavailable or unsupported")
    if addressable_source.stat().st_size != ADDRESSABLE_ORIGINAL_SIZE:
        raise ValueError("Original Addressables bundle is unavailable or unsupported")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = STAGED_BUNDLE.with_suffix(".tmp")
    addressable_temp = STAGED_ADDRESSABLE.with_suffix(".tmp")
    for path in (temp_path, addressable_temp):
        if path.exists():
            path.unlink()

    print(f"Loading {game_source} ...", flush=True)
    env = UnityPy.load(str(game_source))
    by_object = {(obj.assets_file.name, obj.path_id): obj for obj in env.objects}
    patches: dict[tuple[str, int], str] = {}
    patches.update(build_gameplay_texts(manifest, by_object))
    dialogue_patches = build_dialogue_texts(manifest, by_object)
    patches.update(dialogue_patches)
    patches.update(build_subtitle_texts(manifest, by_object))
    expected_hashes = {}
    for key, text in patches.items():
        data = by_object[key].read()
        data.m_Script = text
        data.save()
        expected_hashes[f"{key[0]}:{key[1]}"] = sha256_text(text)

    embedded_ui_hashes = {}
    for key, (old, new) in EMBEDDED_UI.items():
        obj = by_object.get(key)
        if obj is None:
            raise ValueError(f"Embedded UI object is missing: {key}")
        raw = replace_serialized_string(obj.get_raw_data(), old, new)
        obj.set_raw_data(raw)
        embedded_ui_hashes[f"{key[0]}:{key[1]}"] = hashlib.sha256(raw).hexdigest()

    # Rename the visible English option. Runtime identity stays English so all
    # untranslated puzzles, textures, posters and localized objects stay in
    # their original English variants.
    dropdown_key = ("level2", 3381)
    dropdown = by_object.get(dropdown_key)
    dropdown_renamed = False
    if dropdown is not None:
        dropdown.set_raw_data(
            replace_serialized_string(dropdown.get_raw_data(), "English", "Русский")
        )
        dropdown_renamed = True

    print(f"Writing staged bundle ({len(patches)} TextAssets) ...", flush=True)
    packed = env.file.save(packer="lz4")
    with temp_path.open("wb") as handle:
        handle.write(packed)
        handle.flush()
        os.fsync(handle.fileno())
    del packed
    os.replace(temp_path, STAGED_BUNDLE)

    # Area cutscenes are loaded through Addressables and contain another set
    # of the English Yarn TextAssets. Patch them using the same rendered CSV.
    russian_dialogue = dialogue_texts_by_english_name(manifest, dialogue_patches)
    patch_count = len(patches)
    # UnityPy retains the complete source archive and its object graph. Release
    # it before opening the next large bundle to keep the peak memory bounded.
    del env, by_object, patches, dialogue_patches, data, obj, dropdown, raw
    gc.collect()
    print(f"Loading {addressable_source} ...", flush=True)
    addressable_env = UnityPy.load(str(addressable_source))
    addressable_hashes = {}
    addressable_names = {}
    for obj in addressable_env.objects:
        if obj.type.name != "TextAsset":
            continue
        data = obj.read()
        text = russian_dialogue.get(data.m_Name)
        if text is None:
            continue
        data.m_Script = text
        data.save()
        label = f"{obj.assets_file.name}:{obj.path_id}"
        addressable_hashes[label] = sha256_text(text)
        addressable_names[label] = data.m_Name
    if "TUT_Intro (en-US)" not in addressable_names.values():
        raise ValueError("Addressables TUT_Intro (en-US) target was not found")

    print(
        f"Writing staged Addressables bundle ({len(addressable_hashes)} Yarn TextAssets) ...",
        flush=True,
    )
    addressable_packed = addressable_env.file.save(packer="lz4")
    with addressable_temp.open("wb") as handle:
        handle.write(addressable_packed)
        handle.flush()
        os.fsync(handle.fileno())
    del addressable_packed
    os.replace(addressable_temp, STAGED_ADDRESSABLE)
    del addressable_env, data, obj, russian_dialogue
    gc.collect()
    catalog_report = build_catalog_patch()
    assembly_report = build_assembly_patch(assembly_source)

    report = {
        "format_version": 3,
        "patch_version": PATCH_VERSION,
        "built_at": now(),
        "strategy": "replace_english_slot",
        "original_size": expected_original_size,
        "original_sha256": sha256_file(game_source),
        "staged_size": STAGED_BUNDLE.stat().st_size,
        "staged_sha256": sha256_file(STAGED_BUNDLE),
        "patched_text_assets": patch_count,
        "patched_embedded_ui": len(embedded_ui_hashes),
        "dropdown_renamed": dropdown_renamed,
        "expected_text_hashes": expected_hashes,
        "expected_embedded_ui_hashes": embedded_ui_hashes,
        "addressable": {
            "path": str(ADDRESSABLE_BUNDLE.relative_to(GAME_ROOT)),
            "original_size": addressable_source.stat().st_size,
            "original_sha256": sha256_file(addressable_source),
            "staged_size": STAGED_ADDRESSABLE.stat().st_size,
            "staged_sha256": sha256_file(STAGED_ADDRESSABLE),
            "patched_text_assets": len(addressable_hashes),
            "expected_text_hashes": addressable_hashes,
            "asset_names": addressable_names,
        },
        "catalog": catalog_report,
        "assembly": assembly_report,
        "installed": False,
    }
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    verify_staged_files(report)
    return report


def verify_bundle(path: Path, report: dict | None = None) -> None:
    if report is None:
        report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    print(f"Verifying {path} ...", flush=True)
    env = UnityPy.load(str(path))
    by_object = {(obj.assets_file.name, obj.path_id): obj for obj in env.objects}
    mismatches = []
    for label, expected in report["expected_text_hashes"].items():
        asset_file, path_id = label.rsplit(":", 1)
        obj = by_object.get((asset_file, int(path_id)))
        if obj is None:
            mismatches.append(f"missing {label}")
            continue
        actual = sha256_text(obj.read().m_Script)
        if actual != expected:
            mismatches.append(f"hash mismatch {label}")
    for label, expected in report.get("expected_embedded_ui_hashes", {}).items():
        asset_file, path_id = label.rsplit(":", 1)
        obj = by_object.get((asset_file, int(path_id)))
        if obj is None:
            mismatches.append(f"missing embedded UI {label}")
            continue
        actual = hashlib.sha256(obj.get_raw_data()).hexdigest()
        if actual != expected:
            mismatches.append(f"embedded UI hash mismatch {label}")
    if mismatches:
        raise ValueError("; ".join(mismatches[:20]))
    print(f"Verified {len(report['expected_text_hashes'])} localized TextAssets.", flush=True)


def verify_addressable_bundle(path: Path, report: dict | None = None) -> None:
    if report is None:
        report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    addressable = report["addressable"]
    print(f"Verifying {path} ...", flush=True)
    env = UnityPy.load(str(path))
    by_object = {(obj.assets_file.name, obj.path_id): obj for obj in env.objects}
    mismatches = []
    for label, expected in addressable["expected_text_hashes"].items():
        asset_file, path_id = label.rsplit(":", 1)
        obj = by_object.get((asset_file, int(path_id)))
        if obj is None:
            mismatches.append(f"missing {label}")
            continue
        actual = sha256_text(obj.read().m_Script)
        if actual != expected:
            mismatches.append(f"hash mismatch {label}")
    if mismatches:
        raise ValueError("; ".join(mismatches[:20]))
    print(
        f"Verified {len(addressable['expected_text_hashes'])} localized Addressables TextAssets.",
        flush=True,
    )


def install(report: dict | None = None, *, staged_verified: bool = False) -> None:
    if report is None:
        if (
            not STAGED_BUNDLE.is_file()
            or not STAGED_ADDRESSABLE.is_file()
            or not STAGED_ASSEMBLY.is_file()
        ):
            report = build()
            staged_verified = True
        else:
            report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    if "catalog" not in report or report["catalog"].get("addressable_crc") != 0:
        report["catalog"] = build_catalog_patch()
        REPORT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if not staged_verified:
        verify_staged_files(report)

    current_hash = sha256_file(GAME_BUNDLE)
    addressable_hash = sha256_file(ADDRESSABLE_BUNDLE)
    addressable = report["addressable"]
    catalog_hash = sha256_file(CATALOG)
    catalog = report["catalog"]
    assembly_hash = sha256_file(ASSEMBLY)
    assembly = report["assembly"]
    if current_hash not in (report["original_sha256"], report["staged_sha256"]):
        raise ValueError("Current data.unity3d is neither the recorded original nor this patch")
    if addressable_hash not in (addressable["original_sha256"], addressable["staged_sha256"]):
        raise ValueError("Current Addressables bundle is neither the recorded original nor this patch")
    if catalog_hash not in (catalog["original_sha256"], catalog["staged_sha256"]):
        raise ValueError("Current catalog.json is neither the recorded original nor this patch")
    if assembly_hash not in (assembly["original_sha256"], assembly["staged_sha256"]):
        raise ValueError("Current Assembly-CSharp.dll is neither the original nor this patch")
    if (
        current_hash == report["staged_sha256"]
        and addressable_hash == addressable["staged_sha256"]
        and catalog_hash == catalog["staged_sha256"]
        and assembly_hash == assembly["staged_sha256"]
    ):
        print("Russian patch is already installed.")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if ORIGINAL_BACKUP.exists():
        if sha256_file(ORIGINAL_BACKUP) != report["original_sha256"]:
            raise ValueError("Existing original backup has an unexpected checksum")
    else:
        print(f"Creating original backup: {ORIGINAL_BACKUP}", flush=True)
        temp_backup = ORIGINAL_BACKUP.with_suffix(".tmp")
        shutil.copy2(GAME_BUNDLE, temp_backup)
        if sha256_file(temp_backup) != report["original_sha256"]:
            temp_backup.unlink(missing_ok=True)
            raise ValueError("Backup checksum verification failed")
        os.replace(temp_backup, ORIGINAL_BACKUP)

    if ORIGINAL_ADDRESSABLE_BACKUP.exists():
        if sha256_file(ORIGINAL_ADDRESSABLE_BACKUP) != addressable["original_sha256"]:
            raise ValueError("Existing Addressables backup has an unexpected checksum")
    else:
        if addressable_hash != addressable["original_sha256"]:
            raise ValueError("Cannot create an original Addressables backup from a patched bundle")
        print(f"Creating original backup: {ORIGINAL_ADDRESSABLE_BACKUP}", flush=True)
        temp_backup = ORIGINAL_ADDRESSABLE_BACKUP.with_suffix(".tmp")
        shutil.copy2(ADDRESSABLE_BUNDLE, temp_backup)
        if sha256_file(temp_backup) != addressable["original_sha256"]:
            temp_backup.unlink(missing_ok=True)
            raise ValueError("Addressables backup checksum verification failed")
        os.replace(temp_backup, ORIGINAL_ADDRESSABLE_BACKUP)

    if ORIGINAL_CATALOG_BACKUP.exists():
        if sha256_file(ORIGINAL_CATALOG_BACKUP) != catalog["original_sha256"]:
            raise ValueError("Existing catalog backup has an unexpected checksum")
    else:
        if catalog_hash != catalog["original_sha256"]:
            raise ValueError("Cannot create an original catalog backup from a patched catalog")
        print(f"Creating original backup: {ORIGINAL_CATALOG_BACKUP}", flush=True)
        shutil.copy2(CATALOG, ORIGINAL_CATALOG_BACKUP)
        if sha256_file(ORIGINAL_CATALOG_BACKUP) != catalog["original_sha256"]:
            ORIGINAL_CATALOG_BACKUP.unlink(missing_ok=True)
            raise ValueError("Catalog backup checksum verification failed")

    if ORIGINAL_ASSEMBLY_BACKUP.exists():
        if sha256_file(ORIGINAL_ASSEMBLY_BACKUP) != assembly["original_sha256"]:
            raise ValueError("Existing Assembly-CSharp.dll backup has an unexpected checksum")
    else:
        if assembly_hash != assembly["original_sha256"]:
            raise ValueError("Cannot create an original assembly backup from a patched file")
        print(f"Creating original backup: {ORIGINAL_ASSEMBLY_BACKUP}", flush=True)
        shutil.copy2(ASSEMBLY, ORIGINAL_ASSEMBLY_BACKUP)
        if sha256_file(ORIGINAL_ASSEMBLY_BACKUP) != assembly["original_sha256"]:
            ORIGINAL_ASSEMBLY_BACKUP.unlink(missing_ok=True)
            raise ValueError("Assembly-CSharp.dll backup checksum verification failed")

    print("Installing Russian bundles ...", flush=True)
    # UnityPy may keep the verified source bundle open until process exit on
    # Windows.  Copy to a same-directory temporary file and atomically replace
    # the game bundle instead of trying to move that open source file.
    install_temp = GAME_BUNDLE.with_suffix(".russian.tmp")
    addressable_temp = ADDRESSABLE_BUNDLE.with_suffix(".russian.tmp")
    catalog_temp = CATALOG.with_suffix(".russian.tmp")
    assembly_temp = ASSEMBLY.with_suffix(".russian.tmp")
    if current_hash != report["staged_sha256"]:
        shutil.copy2(STAGED_BUNDLE, install_temp)
        if sha256_file(install_temp) != report["staged_sha256"]:
            install_temp.unlink(missing_ok=True)
            raise ValueError("Install copy checksum verification failed")
    if addressable_hash != addressable["staged_sha256"]:
        shutil.copy2(STAGED_ADDRESSABLE, addressable_temp)
        if sha256_file(addressable_temp) != addressable["staged_sha256"]:
            addressable_temp.unlink(missing_ok=True)
            install_temp.unlink(missing_ok=True)
            raise ValueError("Addressables install copy checksum verification failed")
    if catalog_hash != catalog["staged_sha256"]:
        shutil.copy2(STAGED_CATALOG, catalog_temp)
        if sha256_file(catalog_temp) != catalog["staged_sha256"]:
            catalog_temp.unlink(missing_ok=True)
            addressable_temp.unlink(missing_ok=True)
            install_temp.unlink(missing_ok=True)
            raise ValueError("Catalog install copy checksum verification failed")
    if assembly_hash != assembly["staged_sha256"]:
        shutil.copy2(STAGED_ASSEMBLY, assembly_temp)
        if sha256_file(assembly_temp) != assembly["staged_sha256"]:
            assembly_temp.unlink(missing_ok=True)
            catalog_temp.unlink(missing_ok=True)
            addressable_temp.unlink(missing_ok=True)
            install_temp.unlink(missing_ok=True)
            raise ValueError("Assembly-CSharp.dll install copy checksum verification failed")
    if current_hash != report["staged_sha256"]:
        os.replace(install_temp, GAME_BUNDLE)
    if addressable_hash != addressable["staged_sha256"]:
        os.replace(addressable_temp, ADDRESSABLE_BUNDLE)
    if catalog_hash != catalog["staged_sha256"]:
        os.replace(catalog_temp, CATALOG)
    if assembly_hash != assembly["staged_sha256"]:
        os.replace(assembly_temp, ASSEMBLY)
    if sha256_file(GAME_BUNDLE) != report["staged_sha256"]:
        raise ValueError("Installed bundle checksum verification failed")
    if sha256_file(ADDRESSABLE_BUNDLE) != addressable["staged_sha256"]:
        raise ValueError("Installed Addressables checksum verification failed")
    if sha256_file(CATALOG) != catalog["staged_sha256"]:
        raise ValueError("Installed catalog checksum verification failed")
    if sha256_file(ASSEMBLY) != assembly["staged_sha256"]:
        raise ValueError("Installed Assembly-CSharp.dll checksum verification failed")
    verify_catalog(CATALOG, report)
    verify_assembly(ASSEMBLY, report)
    report["installed"] = True
    report["installed_at"] = now()
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Russian localization installed successfully.", flush=True)


def restore() -> None:
    if not ORIGINAL_BACKUP.is_file():
        raise FileNotFoundError(f"Original backup not found: {ORIGINAL_BACKUP}")
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    if sha256_file(ORIGINAL_BACKUP) != report["original_sha256"]:
        raise ValueError("Original backup checksum verification failed")
    restored_temp = GAME_BUNDLE.with_suffix(".restore.tmp")
    shutil.copy2(ORIGINAL_BACKUP, restored_temp)
    os.replace(restored_temp, GAME_BUNDLE)
    if sha256_file(GAME_BUNDLE) != report["original_sha256"]:
        raise ValueError("Restored bundle checksum verification failed")
    addressable = report.get("addressable")
    if addressable is not None:
        if not ORIGINAL_ADDRESSABLE_BACKUP.is_file():
            raise FileNotFoundError(f"Original backup not found: {ORIGINAL_ADDRESSABLE_BACKUP}")
        if sha256_file(ORIGINAL_ADDRESSABLE_BACKUP) != addressable["original_sha256"]:
            raise ValueError("Original Addressables backup checksum verification failed")
        restored_temp = ADDRESSABLE_BUNDLE.with_suffix(".restore.tmp")
        shutil.copy2(ORIGINAL_ADDRESSABLE_BACKUP, restored_temp)
        os.replace(restored_temp, ADDRESSABLE_BUNDLE)
        if sha256_file(ADDRESSABLE_BUNDLE) != addressable["original_sha256"]:
            raise ValueError("Restored Addressables checksum verification failed")
    catalog = report.get("catalog")
    if catalog is not None:
        if not ORIGINAL_CATALOG_BACKUP.is_file():
            raise FileNotFoundError(f"Original backup not found: {ORIGINAL_CATALOG_BACKUP}")
        if sha256_file(ORIGINAL_CATALOG_BACKUP) != catalog["original_sha256"]:
            raise ValueError("Original catalog backup checksum verification failed")
        shutil.copy2(ORIGINAL_CATALOG_BACKUP, CATALOG)
        if sha256_file(CATALOG) != catalog["original_sha256"]:
            raise ValueError("Restored catalog checksum verification failed")
    assembly = report.get("assembly")
    if assembly is not None:
        if not ORIGINAL_ASSEMBLY_BACKUP.is_file():
            raise FileNotFoundError(f"Original backup not found: {ORIGINAL_ASSEMBLY_BACKUP}")
        if sha256_file(ORIGINAL_ASSEMBLY_BACKUP) != assembly["original_sha256"]:
            raise ValueError("Original Assembly-CSharp.dll backup checksum verification failed")
        restored_temp = ASSEMBLY.with_suffix(".restore.tmp")
        shutil.copy2(ORIGINAL_ASSEMBLY_BACKUP, restored_temp)
        os.replace(restored_temp, ASSEMBLY)
        if sha256_file(ASSEMBLY) != assembly["original_sha256"]:
            raise ValueError("Restored Assembly-CSharp.dll checksum verification failed")
    report["installed"] = False
    report["restored_at"] = now()
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Original game bundles restored.")


def status() -> None:
    if not REPORT_PATH.is_file():
        print("No deployment has been built yet.")
        return
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    current_hash = sha256_file(GAME_BUNDLE) if GAME_BUNDLE.is_file() else "missing"
    state = "patched" if current_hash == report.get("staged_sha256") else (
        "original" if current_hash == report.get("original_sha256") else "unknown"
    )
    addressable = report.get("addressable")
    if addressable is None:
        addressable_state = "not_recorded"
    else:
        current_addressable_hash = (
            sha256_file(ADDRESSABLE_BUNDLE) if ADDRESSABLE_BUNDLE.is_file() else "missing"
        )
        addressable_state = (
            "patched" if current_addressable_hash == addressable.get("staged_sha256") else
            "original" if current_addressable_hash == addressable.get("original_sha256") else
            "unknown"
        )
    catalog = report.get("catalog")
    if catalog is None:
        catalog_state = "not_recorded"
    else:
        current_catalog_hash = sha256_file(CATALOG) if CATALOG.is_file() else "missing"
        catalog_state = (
            "patched" if current_catalog_hash == catalog.get("staged_sha256") else
            "original" if current_catalog_hash == catalog.get("original_sha256") else
            "unknown"
        )
    assembly = report.get("assembly")
    if assembly is None:
        assembly_state = "not_recorded"
    else:
        current_assembly_hash = sha256_file(ASSEMBLY) if ASSEMBLY.is_file() else "missing"
        assembly_state = (
            "patched" if current_assembly_hash == assembly.get("staged_sha256") else
            "original" if current_assembly_hash == assembly.get("original_sha256") else
            "unknown"
        )
    print(json.dumps({
        "game_bundle_state": state,
        "addressable_bundle_state": addressable_state,
        "catalog_state": catalog_state,
        "assembly_state": assembly_state,
        "backup_exists": ORIGINAL_BACKUP.is_file(),
        "addressable_backup_exists": ORIGINAL_ADDRESSABLE_BACKUP.is_file(),
        "catalog_backup_exists": ORIGINAL_CATALOG_BACKUP.is_file(),
        "assembly_backup_exists": ORIGINAL_ASSEMBLY_BACKUP.is_file(),
        "patched_text_assets": report.get("patched_text_assets"),
        "patched_embedded_ui": report.get("patched_embedded_ui"),
        "patched_addressable_text_assets": (
            addressable.get("patched_text_assets") if addressable else None
        ),
        "built_at": report.get("built_at"),
        "installed_at": report.get("installed_at"),
    }, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "install", "verify", "restore", "status"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "build":
            build()
        elif args.command == "install":
            install()
        elif args.command == "verify":
            verify_bundle(GAME_BUNDLE)
            report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
            if "addressable" in report:
                verify_addressable_bundle(ADDRESSABLE_BUNDLE, report)
            if "catalog" in report:
                verify_catalog(CATALOG, report)
            if "assembly" in report:
                verify_assembly(ASSEMBLY, report)
        elif args.command == "restore":
            restore()
        else:
            status()
        return 0
    except Exception as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
