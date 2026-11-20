# -*- coding: utf-8 -*-
"""معالج موارد الترجمة (.locres و DataTables/Text) المستخرجة من حزم Unreal Engine (.pak) دون تعديل الأصل."""

import json
import os
import tempfile
from typing import Dict, Tuple

from core.backup import BackupManager
from engines.unreal.detector import resolve_unreal_game_root
from engines.unreal.pak_parser import extract_pak_entry_by_path
from formats.base import FormatHandler, ParsedDocument, PathTuple
from formats.csv_handler import CsvFormatHandler
from formats.ini_handler import IniFormatHandler
from formats.json_handler import JsonFormatHandler
from formats.locres_handler import (
    LocresData,
    locres_to_entries,
    parse_locres_bytes,
    serialize_locres_bytes,
)
from formats.po_handler import PoFormatHandler
from formats.yaml_handler import YamlFormatHandler


STAGING_DIR_NAME = "unreal_staging"
STAGING_MANIFEST_NAME = "staging_manifest.json"


def _split_virtual_pak_path(virtual_path: str) -> Tuple[str, str]:
    if "::" not in virtual_path:
        raise ValueError(f"مسار مورد Unreal غير صالح (ينقصه ::): {virtual_path}")
    pak_path, internal_path = virtual_path.split("::", 1)
    return os.path.abspath(pak_path), internal_path.replace("\\", "/").lstrip("/")


def get_unreal_staging_dir(game_dir: str) -> str:
    return os.path.join(BackupManager.get_state_dir(game_dir), STAGING_DIR_NAME)


def load_staging_manifest(game_dir: str) -> Dict:
    manifest_path = os.path.join(get_unreal_staging_dir(game_dir), STAGING_MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        return {"entries": {}}
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            return data
    except Exception:
        pass
    return {"entries": {}}


def save_staging_manifest(game_dir: str, manifest: Dict) -> None:
    staging_dir = get_unreal_staging_dir(game_dir)
    os.makedirs(staging_dir, exist_ok=True)
    manifest_path = os.path.join(staging_dir, STAGING_MANIFEST_NAME)
    tmp_path = manifest_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, manifest_path)


def _get_inner_text_handler(ext: str) -> Tuple[str, FormatHandler]:
    low = ext.lower()
    if low in (".csv", ".tsv"):
        return "csv", CsvFormatHandler()
    if low == ".json":
        return "json", JsonFormatHandler()
    if low in (".po", ".pot"):
        return "po", PoFormatHandler()
    if low in (".ini", ".txt"):
        return "ini", IniFormatHandler()
    if low in (".yaml", ".yml"):
        return "yaml", YamlFormatHandler()
    raise ValueError(f"امتداد غير مدعوم داخل حزمة Unreal: {ext}")


class UnrealPakFormatHandler(FormatHandler):
    """يقرأ .locres أو ملفات النصوص من داخل .pak ويجهز النسخة المعرّبة للتعبئة داخل ~mods/*.pak."""

    format_names = ("unreal_pak",)

    def load(self, file_path: str) -> ParsedDocument:
        pak_path, internal_path = _split_virtual_pak_path(file_path)
        extracted = extract_pak_entry_by_path(pak_path, internal_path)
        if extracted is None:
            raise FileNotFoundError(
                f"تعذر استخراج '{internal_path}' من حزمة Unreal: {pak_path}"
            )
        pak_info, entry, raw_bytes = extracted
        ext = os.path.splitext(entry.path.lower())[1]

        if ext == ".locres":
            locres = parse_locres_bytes(raw_bytes)
            return ParsedDocument(
                format_name="unreal_pak",
                raw_data={
                    "inner_mode": "locres",
                    "pak_path": pak_path,
                    "internal_path": entry.path,
                    "mount_point": pak_info.mount_point,
                    "pak_version": pak_info.version,
                    "locres": locres,
                },
                entries=locres_to_entries(locres),
            )

        inner_mode, inner_handler = _get_inner_text_handler(ext)
        with tempfile.NamedTemporaryFile("wb", suffix=ext or ".txt", delete=False) as tf:
            tf.write(raw_bytes)
            tmp_src = tf.name
        try:
            inner_doc = inner_handler.load(tmp_src)
        finally:
            try:
                os.remove(tmp_src)
            except OSError:
                pass

        return ParsedDocument(
            format_name="unreal_pak",
            raw_data={
                "inner_mode": inner_mode,
                "inner_ext": ext,
                "inner_doc": inner_doc,
                "pak_path": pak_path,
                "internal_path": entry.path,
                "mount_point": pak_info.mount_point,
                "pak_version": pak_info.version,
            },
            entries=inner_doc.entries,
        )

    def save(
        self,
        file_path: str,
        doc: ParsedDocument,
        replacements: Dict[PathTuple, str],
    ) -> None:
        raw = doc.raw_data
        pak_path = raw["pak_path"]
        internal_path = raw["internal_path"].replace("\\", "/").lstrip("/")
        game_dir = resolve_unreal_game_root(pak_path)
        staging_dir = get_unreal_staging_dir(game_dir)
        staged_file = os.path.join(staging_dir, "files", internal_path.replace("/", os.sep))
        os.makedirs(os.path.dirname(staged_file), exist_ok=True)

        inner_mode = raw.get("inner_mode", "locres")
        if inner_mode == "locres":
            locres: LocresData = raw["locres"]
            updated_bytes = serialize_locres_bytes(locres, replacements)
            parse_locres_bytes(updated_bytes)
            tmp_path = staged_file + ".tmp"
            with open(tmp_path, "wb") as f:
                f.write(updated_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, staged_file)
        else:
            ext = raw.get("inner_ext") or os.path.splitext(internal_path)[1]
            _, inner_handler = _get_inner_text_handler(ext)
            inner_handler.save(staged_file, raw["inner_doc"], replacements)

        manifest = load_staging_manifest(game_dir)
        manifest["entries"][internal_path] = {
            "staged_path": staged_file,
            "mount_point": "../../../",
            "pak_version": raw.get("pak_version") or 8,
            "source_pak": pak_path,
        }
        save_staging_manifest(game_dir, manifest)
