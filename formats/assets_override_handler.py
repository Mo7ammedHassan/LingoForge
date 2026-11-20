# -*- coding: utf-8 -*-
"""معالج أصول Unity المعبأة (.assets) الذي يستخرج النصوص ويحفظ الترجمة في ArabicOverride.json دون المساس بملف .assets."""

import json
import os
from typing import Dict, List, Optional

from core.backup import BackupManager
from formats.base import FormatHandler, ParsedDocument, PathTuple
from formats.json_handler import write_json_atomic_and_verify


CAPTURED_TEXTS_FILENAME = "arabic_captured_texts.txt"


def load_captured_texts_file(captured_path: str) -> List[str]:
    """قراءة النصوص الملتقطة وقت التشغيل من arabic_captured_texts.txt مع إزالة التكرار."""
    if not os.path.isfile(captured_path):
        return []
    seen = set()
    items: List[str] = []
    with open(captured_path, "r", encoding="utf-8-sig", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\r\n")
            if not line.strip():
                continue
            restored = line.replace("\\n", "\n")
            if restored not in seen:
                seen.add(restored)
                items.append(restored)
    return items


def _add_source_mapping(by_source: Dict[str, str], orig_val: str, translated: str) -> None:
    by_source[orig_val] = translated
    if "\r" in orig_val:
        norm_lf = orig_val.replace("\r\n", "\n").replace("\r", "\n")
        by_source.setdefault(norm_lf, translated)
    stripped_src = orig_val.strip()
    if stripped_src and stripped_src != orig_val:
        by_source.setdefault(stripped_src, translated)
    if "\n" in orig_val:
        orig_lines = [ln.strip() for ln in orig_val.splitlines()]
        trans_lines = [ln.strip() for ln in translated.splitlines()]
        if len(orig_lines) == len(trans_lines) and len(orig_lines) > 1:
            for o_ln, t_ln in zip(orig_lines, trans_lines):
                if o_ln and t_ln:
                    by_source.setdefault(o_ln, t_ln)


def merge_translations_into_override(
    game_dir: str,
    new_by_source: Dict[str, str],
    new_by_key: Optional[Dict[str, str]] = None,
    source_label: str = CAPTURED_TEXTS_FILENAME,
) -> str:
    """دمج ترجمات جديدة داخل ArabicOverride.json مع حفظ المدخلات السابقة وتتبع BackupManager."""
    abs_game = os.path.abspath(game_dir)
    override_path = os.path.join(abs_game, UnityAssetsFormatHandler.OVERRIDE_FILENAME)

    existing_payload: Dict = {}
    existed = os.path.isfile(override_path)
    if existed:
        manifest = BackupManager.load_manifest(abs_game)
        rel_override = os.path.relpath(override_path, abs_game).lower()
        is_tracked_created = any(
            p.lower() == rel_override for p in manifest.get("created_files", [])
        )
        is_tracked_backup = any(
            k.lower() == rel_override for k in manifest.get("files", {}).keys()
        )
        if not is_tracked_created and not is_tracked_backup:
            BackupManager.backup_file(abs_game, override_path)
        try:
            with open(override_path, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing_payload = loaded
        except Exception:
            existing_payload = {}

    by_key: Dict[str, str] = {}
    if isinstance(existing_payload.get("by_key"), dict):
        by_key.update({str(k): str(v) for k, v in existing_payload["by_key"].items()})
    if new_by_key:
        by_key.update(new_by_key)

    by_source: Dict[str, str] = {}
    if isinstance(existing_payload.get("by_source"), dict):
        by_source.update({str(k): str(v) for k, v in existing_payload["by_source"].items()})
    for src_txt, trans_txt in new_by_source.items():
        _add_source_mapping(by_source, src_txt, trans_txt)

    existing_sources: List[str] = []
    raw_prev_list = existing_payload.get("source_assets")
    if isinstance(raw_prev_list, list):
        for item in raw_prev_list:
            s_item = str(item)
            if s_item and s_item not in existing_sources:
                existing_sources.append(s_item)
    first_source = str(existing_payload.get("source_asset") or "")
    if first_source and first_source not in existing_sources:
        existing_sources.insert(0, first_source)
    if source_label and source_label not in existing_sources:
        existing_sources.append(source_label)

    payload = {
        "version": int(existing_payload.get("version") or 1),
        "source_asset": first_source or source_label,
        "source_assets": existing_sources,
        "by_key": by_key,
        "by_source": by_source,
    }
    if not existed:
        BackupManager.register_created_file(abs_game, override_path)
    write_json_atomic_and_verify(override_path, payload)
    return override_path


class UnityAssetsFormatHandler(FormatHandler):
    """محلل وكاتب صيغة unity_assets عبر توليد ملف اعتراض وقت التشغيل ArabicOverride.json."""

    format_names = ("unity_assets",)
    OVERRIDE_FILENAME = "ArabicOverride.json"

    @classmethod
    def resolve_game_dir_from_assets(cls, assets_path: str) -> str:
        """استنتاج مجلد اللعبة الجذري من مسار ملف .assets (سواء داخل *_Data أو خارجه)."""
        abs_path = os.path.abspath(assets_path)
        parent = os.path.dirname(abs_path)
        probe = parent
        while True:
            base = os.path.basename(probe)
            if base.lower().endswith("_data"):
                root = os.path.dirname(probe)
                if root:
                    return root
            next_probe = os.path.dirname(probe)
            if not next_probe or next_probe == probe:
                break
            probe = next_probe
        return parent

    @classmethod
    def get_override_path(cls, assets_path: str) -> str:
        """إرجاع المسار المستهدف لملف ArabicOverride.json في جذر اللعبة."""
        game_dir = cls.resolve_game_dir_from_assets(assets_path)
        return os.path.join(game_dir, cls.OVERRIDE_FILENAME)

    def load(self, file_path: str) -> ParsedDocument:
        from engines.unity.assets_extractor import extract_assets_localization_blocks

        blocks = extract_assets_localization_blocks(file_path)
        all_entries = []
        blocks_meta: List[Dict[str, object]] = []
        for blk in blocks:
            all_entries.extend(blk.entries)
            blocks_meta.append(
                {
                    "block_name": blk.block_name,
                    "block_kind": blk.block_kind,
                    "entry_count": len(blk.entries),
                }
            )

        return ParsedDocument(
            format_name="unity_assets",
            raw_data={
                "assets_path": os.path.abspath(file_path),
                "blocks": blocks_meta,
            },
            entries=all_entries,
        )

    def save(
        self,
        file_path: str,
        doc: ParsedDocument,
        replacements: Dict[PathTuple, str],
    ) -> None:
        """كتابة الترجمات إلى ArabicOverride.json ذرياً وتسجيله كملف مضاف بدون تعديل ملف .assets."""
        game_dir = self.resolve_game_dir_from_assets(file_path)
        override_path = self.get_override_path(file_path)

        by_key: Dict[str, str] = {}
        by_source: Dict[str, str] = {}

        for path_tuple, flat_key, orig_val in doc.entries:
            if path_tuple not in replacements:
                continue
            translated = replacements[path_tuple]
            block_name = str(path_tuple[0]) if path_tuple else ""

            by_key[flat_key] = translated
            if block_name and flat_key.startswith(block_name + "."):
                short_flat = flat_key[len(block_name) + 1 :]
                if short_flat:
                    by_key.setdefault(short_flat, translated)
                    if (
                        len(path_tuple) == 3
                        and isinstance(path_tuple[1], int)
                        and isinstance(path_tuple[2], int)
                        and "." in short_flat
                    ):
                        row_key = short_flat.rsplit(".", 1)[0]
                        if row_key:
                            by_key.setdefault(row_key, translated)

            if (
                len(path_tuple) >= 2
                and isinstance(path_tuple[-1], str)
                and path_tuple[-1] not in ("text", "attr")
            ):
                by_key.setdefault(path_tuple[-1], translated)

            _add_source_mapping(by_source, orig_val, translated)

        merge_translations_into_override(
            game_dir,
            new_by_source=by_source,
            new_by_key=by_key,
            source_label=os.path.relpath(os.path.abspath(file_path), game_dir),
        )


class UnityCodeStringsFormatHandler(FormatHandler):
    """محلل وكاتب صيغة unity_code لنصوص الواجهة المستخرجة من الكود إلى ArabicOverride.json دون تعديل DLL."""

    format_names = ("unity_code",)

    def load(self, file_path: str) -> ParsedDocument:
        from engines.unity.scanner import extract_code_ui_strings_from_file

        strings = extract_code_ui_strings_from_file(file_path)
        entries = [
            (("code", idx, f"code_{idx}"), f"code_{idx}", text)
            for idx, text in enumerate(strings)
        ]
        return ParsedDocument(
            format_name="unity_code",
            raw_data={"code_path": os.path.abspath(file_path)},
            entries=entries,
        )

    def save(
        self,
        file_path: str,
        doc: ParsedDocument,
        replacements: Dict[PathTuple, str],
    ) -> None:
        game_dir = UnityAssetsFormatHandler.resolve_game_dir_from_assets(file_path)
        by_key: Dict[str, str] = {}
        by_source: Dict[str, str] = {}
        for path_tuple, flat_key, orig_val in doc.entries:
            if path_tuple not in replacements:
                continue
            translated = replacements[path_tuple]
            by_key[flat_key] = translated
            _add_source_mapping(by_source, orig_val, translated)

        merge_translations_into_override(
            game_dir,
            new_by_source=by_source,
            new_by_key=by_key,
            source_label=os.path.relpath(os.path.abspath(file_path), game_dir),
        )
