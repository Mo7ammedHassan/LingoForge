# -*- coding: utf-8 -*-
"""معالج موارد الترجمة المستخرجة من حزم Godot (.pck / .exe) دون المساس بالحزمة الأصلية."""

import copy
import csv
import os
import re
import tempfile
from typing import Dict, List, Optional, Tuple

from core.backup import BackupManager
from engines.godot.detector import resolve_godot_game_root
from engines.godot.pck_parser import (
    extract_pck_entry_by_path,
    extract_translation_resource_entries,
)
from formats.base import FormatHandler, ParsedDocument, PathTuple
from formats.csv_handler import CsvFormatHandler
from formats.ini_handler import IniFormatHandler
from formats.json_handler import JsonFormatHandler
from formats.po_handler import PoFormatHandler
from formats.yaml_handler import YamlFormatHandler


ARABIC_DIR_NAME = "arabic_l10n"


def _split_virtual_pck_path(virtual_path: str) -> Tuple[str, str]:
    if "::" not in virtual_path:
        raise ValueError(f"مسار مورد Godot غير صالح (ينقصه ::): {virtual_path}")
    pck_path, res_path = virtual_path.split("::", 1)
    return os.path.abspath(pck_path), res_path


def _resolve_output_relpath(res_path: str, target_ext: Optional[str] = None) -> str:
    clean = res_path.replace("\\", "/")
    if clean.startswith("res://"):
        clean = clean[6:]
    clean = clean.lstrip("/")
    if not clean:
        clean = "strings"
    if target_ext:
        stem = os.path.splitext(clean)[0]
        # إزالة لاحقة .en أو .en_US عند التحويل إلى .ar.po
        stem = re.sub(r"\.(?:en|en_us|en-us|english)$", "", stem, flags=re.IGNORECASE)
        clean = f"{stem}{target_ext}"
    return clean


def _escape_po(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def _set_po_language_ar(po_path: str) -> None:
    """ضمان وجود Language: ar في ترويسة ملف PO ليحمّله Godot تلقائياً عند لغة ar."""
    with open(po_path, "r", encoding="utf-8-sig", newline="") as f:
        content = f.read()

    eol = "\r\n" if "\r\n" in content else "\n"
    if re.search(r'"Language:\s*[^"\\]+\\n"', content):
        updated = re.sub(
            r'"Language:\s*[^"\\]+\\n"',
            '"Language: ar\\\\n"',
            content,
            count=1,
        )
    elif 'msgid ""' in content:
        updated = re.sub(
            r'(msgstr\s+""' + re.escape(eol) + r')',
            r'\1"Language: ar\\n"' + eol,
            content,
            count=1,
        )
    else:
        header = (
            f'msgid ""{eol}'
            f'msgstr ""{eol}'
            f'"Content-Type: text/plain; charset=UTF-8\\n"{eol}'
            f'"Language: ar\\n"{eol}{eol}'
        )
        updated = header + content

    tmp_path = po_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        f.write(updated)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, po_path)


class GodotPckFormatHandler(FormatHandler):
    """يقرأ الموارد من داخل .pck/.exe ويكتب النسخة المعرّبة داخل arabic_l10n/ دون لمس الحزمة الأصلية."""

    format_names = ("godot_pck",)

    def load(self, file_path: str) -> ParsedDocument:
        pck_path, res_path = _split_virtual_pck_path(file_path)
        raw_bytes = extract_pck_entry_by_path(pck_path, res_path)
        if raw_bytes is None:
            raise FileNotFoundError(
                f"تعذر العثور على المورد '{res_path}' داخل الحزمة: {pck_path}"
            )

        ext = os.path.splitext(res_path.lower())[1]
        if ext in (".translation", ".tres"):
            parsed = extract_translation_resource_entries(raw_bytes)
            if not parsed:
                raise ValueError(f"تعذر تحليل مورد الترجمة: {res_path}")
            locale, pairs = parsed
            entries: List[Tuple[PathTuple, str, str]] = [
                ((idx,), k, v) for idx, (k, v) in enumerate(pairs)
            ]
            return ParsedDocument(
                format_name="godot_pck",
                raw_data={
                    "inner_mode": "translation",
                    "pck_path": pck_path,
                    "res_path": res_path,
                    "locale": locale,
                    "pairs": pairs,
                },
                entries=entries,
            )

        with tempfile.NamedTemporaryFile("wb", suffix=ext or ".txt", delete=False) as tf:
            tf.write(raw_bytes)
            tmp_src = tf.name

        try:
            if ext in (".po", ".pot"):
                inner_handler: FormatHandler = PoFormatHandler()
                mode = "po"
            elif ext in (".csv", ".tsv"):
                inner_handler = CsvFormatHandler()
                mode = "csv"
            elif ext == ".json":
                inner_handler = JsonFormatHandler()
                mode = "json"
            elif ext in (".ini", ".txt"):
                inner_handler = IniFormatHandler()
                mode = "ini"
            elif ext in (".yaml", ".yml"):
                inner_handler = YamlFormatHandler()
                mode = "yaml"
            else:
                raise ValueError(f"امتداد غير مدعوم داخل حزمة Godot: {ext}")

            inner_doc = inner_handler.load(tmp_src)
        finally:
            try:
                os.remove(tmp_src)
            except OSError:
                pass

        return ParsedDocument(
            format_name="godot_pck",
            raw_data={
                "inner_mode": mode,
                "inner_ext": ext,
                "inner_doc": inner_doc,
                "pck_path": pck_path,
                "res_path": res_path,
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
        pck_path = raw["pck_path"]
        res_path = raw["res_path"]
        inner_mode = raw["inner_mode"]
        game_dir = resolve_godot_game_root(pck_path, explicit_game_dir=doc.game_dir)
        l10n_dir = os.path.join(game_dir, ARABIC_DIR_NAME)

        if inner_mode == "po":
            out_rel = _resolve_output_relpath(res_path, target_ext=".ar.po")
            out_path = os.path.join(l10n_dir, out_rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            PoFormatHandler().save(out_path, raw["inner_doc"], replacements)
            _set_po_language_ar(out_path)
            BackupManager.register_created_file(game_dir, out_path)
            return

        if inner_mode == "csv":
            inner_ext = raw.get("inner_ext") or ".csv"
            out_rel = _resolve_output_relpath(res_path)
            out_path = os.path.join(l10n_dir, out_rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)

            inner_doc: ParsedDocument = raw["inner_doc"]
            # إذا لم يوجد عمود ar في CSV Godot، نضيف عمود ar لتعريف Godot لغة العربية مباشرة
            if inner_doc.raw_data.get("arabic_col_idx") is None and inner_doc.raw_data.get("rows"):
                rows = copy.deepcopy(inner_doc.raw_data["rows"])
                if rows and any(c.strip().lower() in ("key", "keys", "id", "en", "english") for c in rows[0]):
                    ar_col = len(rows[0])
                    rows[0].append("ar")
                    for r in rows[1:]:
                        while len(r) <= ar_col:
                            r.append("")
                    inner_doc = ParsedDocument(
                        format_name=inner_doc.format_name,
                        raw_data={
                            **inner_doc.raw_data,
                            "rows": rows,
                            "arabic_col_idx": ar_col,
                        },
                        entries=inner_doc.entries,
                    )
            CsvFormatHandler().save(out_path, inner_doc, replacements)
            BackupManager.register_created_file(game_dir, out_path)
            return

        if inner_mode == "translation":
            out_rel = _resolve_output_relpath(res_path, target_ext=".ar.po")
            out_path = os.path.join(l10n_dir, out_rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)

            lines: List[str] = [
                'msgid ""',
                'msgstr ""',
                '"Content-Type: text/plain; charset=UTF-8\\n"',
                '"Language: ar\\n"',
                "",
            ]
            seen_msgids = set()
            for path_tuple, key, orig_val in doc.entries:
                translated = replacements.get(path_tuple)
                if not translated:
                    continue
                # إذا كان المفتاح مُجزّأً (OptimizedTranslation) نستخدم النص الأصلي كـ msgid
                msgid_val = orig_val if key.startswith("msg_") else key
                if msgid_val in seen_msgids:
                    continue
                seen_msgids.add(msgid_val)
                lines.append(f'msgid "{_escape_po(msgid_val)}"')
                lines.append(f'msgstr "{_escape_po(translated)}"')
                lines.append("")

            BackupManager.register_created_file(game_dir, out_path)
            tmp_path = out_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, out_path)
            return

        if inner_mode == "json":
            out_rel = _resolve_output_relpath(res_path)
            out_path = os.path.join(l10n_dir, out_rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            JsonFormatHandler().save(out_path, raw["inner_doc"], replacements)
            BackupManager.register_created_file(game_dir, out_path)
            return

        if inner_mode == "ini":
            out_rel = _resolve_output_relpath(res_path)
            out_path = os.path.join(l10n_dir, out_rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            IniFormatHandler().save(out_path, raw["inner_doc"], replacements)
            BackupManager.register_created_file(game_dir, out_path)
            return

        if inner_mode == "yaml":
            out_rel = _resolve_output_relpath(res_path)
            out_path = os.path.join(l10n_dir, out_rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            YamlFormatHandler().save(out_path, raw["inner_doc"], replacements)
            BackupManager.register_created_file(game_dir, out_path)
            return

        raise ValueError(f"وضع حفظ غير مدعوم في GodotPckFormatHandler: {inner_mode}")
