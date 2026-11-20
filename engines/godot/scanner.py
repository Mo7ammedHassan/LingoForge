# -*- coding: utf-8 -*-
"""ماسح ومصفّي ملفات اللغة لمحرك Godot (3.x / 4.x) للملفات الخارجية وحزم .pck."""

import os
import re
from typing import Dict, Iterable, List, Optional, Set, Tuple

from core.backup import calculate_arabic_ratio, is_file_likely_arabic
from core.models import CandidateFile, EngineInfo
from engines.godot.detector import find_godot_containers
from engines.godot.pck_parser import (
    PckEntry,
    PckInfo,
    extract_translation_resource_entries,
    normalize_res_path,
    parse_pck_header_and_entries,
    parse_project_binary,
    parse_project_godot_text,
    read_pck_entry_bytes,
)
from formats.base import ParsedDocument, detect_bytes_encoding
from formats.csv_handler import CsvFormatHandler
from formats.ini_handler import IniFormatHandler
from formats.json_handler import clean_jsonc, flatten_json_strings
from formats.po_handler import PoFormatHandler
from formats.registry import get_format_handler


ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF\uFE70-\uFEFF]")
ASCII_LETTER_RE = re.compile(r"[A-Za-z]")

GODOT_URI_RE = re.compile(r"^(?:res|user|uid)://", re.IGNORECASE)
NODE_PATH_RE = re.compile(
    r"^(?:/[A-Za-z0-9_/@-]+|\.\.?/[A-Za-z0-9_./@-]+|[%$][A-Za-z0-9_/@]+|NodePath\s*\(.*\))$",
    re.IGNORECASE,
)
INPUT_ACTION_RE = re.compile(r"^ui_[a-z0-9_]+$")
CALLBACK_OR_SIGNAL_RE = re.compile(r"^_(?:on_[A-Za-z0-9_]+|[a-z]+(?:_[a-z0-9]+)*)$")
ASSET_OR_SCENE_EXT_RE = re.compile(
    r"\.(?:tscn|scn|tres|res|gd|cs|gdshader|shader|import|translation|pck|"
    r"png|jpe?g|webp|svg|bmp|ogg|wav|mp3|ttf|otf|woff2?|glb|gltf|obj|fbx|cfg)$",
    re.IGNORECASE,
)
URL_RE = re.compile(r"^(?:https?://|file://|mailto:)", re.IGNORECASE)
HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6,8}$")
TECH_ID_NO_SPACE_RE = re.compile(
    r"^(?:[a-z]+_[a-z0-9_]+|[A-Z]+_[A-Z0-9_]+|[a-z]+[A-Z][A-Za-z0-9]*|[a-z0-9_]+(?:\.[a-z0-9_]+)+)$"
)
STRIP_TOKENS_RE = re.compile(
    r"\{[A-Za-z0-9_]+(?::[^}\n]+)?\}|<[^>]+>|\[/?[a-zA-Z][a-zA-Z0-9_]*(?:=[^\]\n]+|\s+[a-zA-Z_][a-zA-Z0-9_]*=[^\]\n]+)?\]|%(?:(?:[0-9]+\$)?[-+0#]*[0-9]*(?:\.[0-9]+)?[sdifuxXoOgGc%]|[0-9]+)|\\n|\\t"
)

IGNORED_KEY_SUFFIXES = (
    ".node_path",
    ".nodepath",
    ".path",
    ".resource_path",
    ".script",
    ".signal",
    ".method",
    ".theme",
    ".font",
    ".shader",
    ".texture",
    ".scene",
    ".uid",
    ".locale",
    ".action",
    ".input_action",
)

IGNORED_DISK_DIRS = {
    ".arabic_translator",
    "arabic_l10n",
    ".git",
    ".godot",
    ".import",
    "__pycache__",
    "addons",
}
IGNORED_DISK_FILES = {
    "override.cfg",
    "project.godot",
    "export_presets.cfg",
    "backup_manifest.json",
    "arabic_font_log.txt",
    "arabic_translator_log.txt",
    "arabic_captured_texts.txt",
    "arabicoverride.json",
}

TEXT_FILE_EXTS = {".po", ".pot", ".csv", ".tsv", ".json", ".ini", ".txt", ".yaml", ".yml"}
PACKED_EXTS = TEXT_FILE_EXTS | {".translation", ".tres"}

SIBLING_LANG_TOKENS = {
    "fr", "de", "es", "ru", "it", "pt", "pt_br", "ja", "jp", "zh", "zh_cn", "zh_tw",
    "ko", "pl", "tr", "nl", "uk", "cs", "hu", "sv", "no", "da", "fi", "el", "th",
    "vi", "id", "ar", "french", "german", "spanish", "russian", "italian", "portuguese",
    "japanese", "chinese", "korean", "polish", "turkish", "dutch", "arabic",
}
ENGLISH_TOKENS = {"en", "en_us", "en-us", "en_gb", "en-gb", "eng", "english"}
LANG_DIR_KEYWORDS = ("translation", "locale", "localiz", "lang", "i18n", "dialog", "text")
LANG_NAME_KEYWORDS = ("english", "messages", "strings", "localization", "dialogue", "dialog", "ui", "locale")


def is_translatable_value(key: str, value: str) -> bool:
    """تحديد هل القيمة نص بشري قابل للترجمة في Godot أم رمز/مسار/عقدة تقنية."""
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return False

    if ARABIC_CHAR_RE.search(stripped):
        return False
    if not ASCII_LETTER_RE.search(stripped):
        return False

    key_low = (key or "").lower()
    if key_low.endswith(IGNORED_KEY_SUFFIXES):
        return False

    if GODOT_URI_RE.match(stripped):
        return False
    if NODE_PATH_RE.match(stripped):
        return False
    if INPUT_ACTION_RE.match(stripped):
        return False
    if CALLBACK_OR_SIGNAL_RE.match(stripped):
        return False
    if URL_RE.match(stripped):
        return False
    if HEX_COLOR_RE.match(stripped):
        return False
    if ASSET_OR_SCENE_EXT_RE.search(stripped) and " " not in stripped:
        return False

    # التحقق من بقاء حروف بشرية فعلية بعد إزالة الوسوم والمتغيرات (BBCode و {var})
    without_tokens = STRIP_TOKENS_RE.sub("", stripped).strip()
    if not without_tokens or not ASCII_LETTER_RE.search(without_tokens):
        return False

    if " " not in stripped and TECH_ID_NO_SPACE_RE.match(stripped):
        return False

    return True


def _extract_stem_tokens(rel_path: str) -> List[str]:
    norm = rel_path.replace("\\", "/").lower()
    base = os.path.basename(norm)
    # إزالة امتدادين متتاليين مثل strings.en.translation أو messages.en.po
    parts = [p for p in re.split(r"[/._\-]+", base) if p]
    return parts


def _score_godot_candidate(
    rel_path: str,
    ext: str,
    total_entries: int,
    translatable_entries: int,
    hinted_res_paths: Set[str],
    res_path: Optional[str] = None,
) -> Tuple[int, List[str]]:
    """حساب درجة الثقة لملف ترجمة Godot مع ذكر الأسباب."""
    reasons: List[str] = []
    if translatable_entries <= 0:
        return 10, ["لا يحتوي على نصوص بشرية قابلة للترجمة"]

    base_scores = {
        ".po": 72,
        ".csv": 68,
        ".tsv": 65,
        ".translation": 66,
        ".tres": 60,
        ".pot": 56,
        ".json": 48,
        ".yaml": 46,
        ".yml": 46,
        ".ini": 40,
        ".txt": 35,
    }
    score = base_scores.get(ext, 30)
    reasons.append(f"صيغة Godot ({ext}): {translatable_entries}/{total_entries} نص قابل للترجمة")

    low_rel = rel_path.replace("\\", "/").lower()
    if any(k in low_rel for k in LANG_DIR_KEYWORDS):
        score += 14
        reasons.append("يقع داخل مسار/مجلد ترجمة قياسي")

    tokens = _extract_stem_tokens(rel_path)
    # تجاهل الامتداد الأخير عند فحص الرموز اللغوية
    stem_tokens = tokens[:-1] if len(tokens) > 1 else tokens

    is_english = any(t in ENGLISH_TOKENS for t in stem_tokens)
    is_sibling = any(t in SIBLING_LANG_TOKENS for t in stem_tokens)

    if is_english:
        score += 15
        reasons.append("اسم الملف يشير إلى اللغة الإنجليزية المصدر")
    elif is_sibling:
        score -= 55
        reasons.append("اسم الملف يشير إلى لغة ثانية غير الإنجليزية (تم خفض الأولوية)")
    elif any(k in low_rel for k in LANG_NAME_KEYWORDS):
        score += 10
        reasons.append("اسم ملف ترجمة عام")

    if res_path and res_path.lower() in hinted_res_paths:
        score += 18
        reasons.append("مُسجل في إعدادات locale/translations داخل project.godot")

    if ".godot/imported/" in low_rel or ".import/" in low_rel:
        score -= 12

    if ext == ".pot" and not is_english:
        score -= 5

    score = max(0, min(100, score))
    return score, reasons


def _parse_entries_from_bytes(ext: str, raw_bytes: bytes) -> Optional[Tuple[List[Tuple[str, str]], bool]]:
    """تحليل بايتات ملف نصي أو مورد ترجمة في الذاكرة دون كتابة على القرص."""
    if ext in (".translation", ".tres"):
        parsed = extract_translation_resource_entries(raw_bytes)
        if not parsed:
            return None
        locale, pairs = parsed
        sample_text = "\n".join(v for _, v in pairs[:50])
        has_arabic = calculate_arabic_ratio(sample_text) > 0.05
        return pairs, has_arabic

    enc = detect_bytes_encoding(raw_bytes)
    text = raw_bytes.decode(enc, errors="replace")
    has_arabic = calculate_arabic_ratio(text[:200000]) > 0.05

    if ext in (".po", ".pot"):
        import tempfile

        handler = PoFormatHandler()
        with tempfile.NamedTemporaryFile("wb", suffix=ext, delete=False) as tf:
            tf.write(raw_bytes)
            tmp_path = tf.name
        try:
            doc = handler.load(tmp_path)
            return [(k, v) for _, k, v in doc.entries], has_arabic
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    if ext in (".csv", ".tsv"):
        import tempfile

        handler = CsvFormatHandler()
        with tempfile.NamedTemporaryFile("wb", suffix=ext, delete=False) as tf:
            tf.write(raw_bytes)
            tmp_path = tf.name
        try:
            doc = handler.load(tmp_path)
            return [(k, v) for _, k, v in doc.entries], has_arabic
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    if ext == ".json":
        import json

        try:
            data = json.loads(clean_jsonc(text))
            flat = flatten_json_strings(data)
            return [(k, v) for _, k, v in flat], has_arabic
        except Exception:
            return None

    if ext in (".ini", ".txt", ".yaml", ".yml"):
        fmt_key = "yaml" if ext in (".yaml", ".yml") else "ini"
        handler = get_format_handler(fmt_key)
        if handler is None:
            return None
        import tempfile

        with tempfile.NamedTemporaryFile("wb", suffix=ext, delete=False) as tf:
            tf.write(raw_bytes)
            tmp_path = tf.name
        try:
            doc = handler.load(tmp_path)
            return [(k, v) for _, k, v in doc.entries], has_arabic
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return None


class GodotLanguageScanner:
    """يمسح ملفات الترجمة الخارجية وحزم .pck (المستقلة والمدمجة) لمحرك Godot."""

    def _collect_project_hints(
        self,
        game_dir: str,
        pcks: List[PckInfo],
    ) -> Tuple[Set[str], List[str]]:
        hinted_res: Set[str] = set()
        hints: List[str] = []

        def _record_project_meta(meta: Dict, source_label: str) -> None:
            if meta.get("name"):
                hints.append(f"{source_label}Project: {meta['name']}")
            for rp in meta.get("translations", []):
                norm = normalize_res_path(rp)
                hinted_res.add(norm.lower())
                hints.append(f"{source_label}locale/translations: {norm}")
            if meta.get("custom_theme"):
                hints.append(f"{source_label}Custom Theme: {meta['custom_theme']} (سيتم توفير تغطية عربية في override.cfg)")
            if str(meta.get("text_driver") or "").lower() == "fallback":
                hints.append(f"{source_label}تحذير: rendering/text_driver=Fallback (قد لا يدعم تشكيل العربية دون TextServer Advanced)")

        proj_path = os.path.join(game_dir, "project.godot")
        if os.path.isfile(proj_path):
            try:
                with open(proj_path, "r", encoding="utf-8-sig", errors="replace") as f:
                    _record_project_meta(parse_project_godot_text(f.read()), "")
            except OSError:
                pass

        for pck in pcks:
            kind = "Embedded PCK" if pck.is_embedded else "PCK"
            hints.append(
                f"{kind} v{pck.pack_format_version} (Godot {pck.godot_major}.{pck.godot_minor}.{pck.godot_patch}): "
                f"{len(pck.entries)} ملف"
            )
            if pck.dir_encrypted:
                hints.append(f"تحذير: فهرس الحزمة {os.path.basename(pck.container_path)} مشفّر (PACK_DIR_ENCRYPTED).")
                continue
            enc_count = sum(1 for e in pck.entries if e.encrypted)
            if enc_count:
                hints.append(f"تنبيه: {enc_count} ملف(ملفات) مشفّرة داخل {os.path.basename(pck.container_path)}.")
            for entry in pck.entries:
                low_res = entry.res_path.lower()
                if low_res == "res://project.godot":
                    raw = read_pck_entry_bytes(pck, entry)
                    _record_project_meta(parse_project_godot_text(raw.decode("utf-8-sig", errors="replace")), "PCK ")
                elif low_res == "res://project.binary":
                    raw = read_pck_entry_bytes(pck, entry)
                    _record_project_meta(parse_project_binary(raw), "PCK ")

        return hinted_res, hints

    def scan(self, engine: EngineInfo) -> Tuple[List[CandidateFile], List[str]]:
        candidates: List[CandidateFile] = []
        abs_dir = os.path.abspath(engine.game_dir)

        pck_files, exe_files, _ = find_godot_containers(abs_dir)
        pcks: List[PckInfo] = []
        for p in pck_files + exe_files:
            info = parse_pck_header_and_entries(p)
            if info is not None:
                pcks.append(info)

        hinted_res, hints = self._collect_project_hints(abs_dir, pcks)

        # 1. مسح الملفات الخارجية على القرص
        external_res_seen: Set[str] = set()
        for root, dirs, files in os.walk(abs_dir):
            dirs[:] = [d for d in sorted(dirs) if d.lower() not in IGNORED_DISK_DIRS]
            for fname in sorted(files):
                low = fname.lower()
                if low in IGNORED_DISK_FILES:
                    continue
                ext = os.path.splitext(low)[1]
                if ext not in TEXT_FILE_EXTS:
                    continue
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, abs_dir).replace(os.sep, "/")
                res_path = normalize_res_path(rel_path)

                fmt = ext.lstrip(".")
                handler = get_format_handler(fmt)
                if handler is None:
                    continue
                try:
                    doc = handler.load(full_path)
                except Exception:
                    continue

                total_entries = len(doc.entries)
                trans_entries = sum(
                    1 for _, flat_key, val in doc.entries if is_translatable_value(flat_key, val)
                )
                score, reasons = _score_godot_candidate(
                    rel_path, ext, total_entries, trans_entries, hinted_res, res_path=res_path
                )
                contains_arabic = is_file_likely_arabic(full_path)
                if contains_arabic:
                    reasons.append("يحتوي على نصوص عربية مسبقاً")

                external_res_seen.add(res_path.lower())
                candidates.append(
                    CandidateFile(
                        path=full_path,
                        relative_path=rel_path,
                        format_name=fmt,
                        score=score,
                        reasons=reasons,
                        dll_matched=res_path.lower() in hinted_res,
                        contains_arabic=contains_arabic,
                        total_entries=total_entries,
                        translatable_entries=trans_entries,
                        skip_backup=False,
                    )
                )

        # 2. مسح الموارد المعبأة داخل .pck / .exe بدون تعديل الحزمة الأصلية
        for pck in pcks:
            pck_rel = os.path.relpath(pck.container_path, abs_dir).replace(os.sep, "/")
            packed_stems_with_source: Set[str] = set()
            for entry in pck.entries:
                ext = os.path.splitext(entry.res_path.lower())[1]
                if ext in (".po", ".csv", ".tsv", ".pot"):
                    packed_stems_with_source.add(entry.res_path.lower().rsplit(".", 1)[0])

            for entry in pck.entries:
                if entry.encrypted:
                    continue
                ext = os.path.splitext(entry.res_path.lower())[1]
                if ext not in PACKED_EXTS:
                    continue
                low_res = entry.res_path.lower()
                # تجنب تكرار .translation إذا وُجد المصدر الخام (.po/.csv) لنفس الملف في الحزمة
                if ext == ".translation":
                    base_stem = low_res.rsplit(".", 1)[0]
                    if base_stem in packed_stems_with_source or (
                        (".godot/imported/" in low_res or ".import/" in low_res)
                        and any(os.path.basename(stem) in low_res for stem in packed_stems_with_source)
                    ):
                        continue
                # تخطي إذا وُجد نفس الملف خارجياً على القرص
                if low_res in external_res_seen:
                    continue

                raw_bytes = read_pck_entry_bytes(pck, entry)
                if not raw_bytes:
                    continue
                parsed = _parse_entries_from_bytes(ext, raw_bytes)
                if not parsed:
                    continue
                pairs, has_arabic = parsed
                if ext == ".translation" and not pairs:
                    hints.append(
                        f"تنبيه Godot: المورد '{entry.res_path}' داخل {pck_rel} يستخدم OptimizedTranslation مضغوط بـ SMAZ بدون ملف .po/.csv مصدري."
                    )
                    continue
                total_entries = len(pairs)
                trans_entries = sum(1 for k, v in pairs if is_translatable_value(k, v))

                rel_display = f"{pck_rel}::{entry.res_path}"
                score, reasons = _score_godot_candidate(
                    entry.res_path,
                    ext,
                    total_entries,
                    trans_entries,
                    hinted_res,
                    res_path=entry.res_path,
                )
                reasons.insert(0, f"مستخرج من حزمة Godot ({pck_rel})")
                if has_arabic:
                    reasons.append("يحتوي على نصوص عربية مسبقاً")

                candidates.append(
                    CandidateFile(
                        path=f"{pck.container_path}::{entry.res_path}",
                        relative_path=rel_display,
                        format_name="godot_pck",
                        score=score,
                        reasons=reasons,
                        dll_matched=low_res in hinted_res,
                        contains_arabic=has_arabic,
                        total_entries=total_entries,
                        translatable_entries=trans_entries,
                        skip_backup=True,
                    )
                )

        candidates.sort(key=lambda c: (-c.score, -c.translatable_entries, c.relative_path))
        return candidates, hints
