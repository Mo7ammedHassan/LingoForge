# -*- coding: utf-8 -*-
"""ماسح ومصفّي ملفات اللغة لمحرك Unreal Engine (.locres وملفات النصوص الخارجية والمضمّنة في .pak)."""

import os
import re
import tempfile
from typing import List, Optional, Set, Tuple

from core.backup import calculate_arabic_ratio
from core.models import CandidateFile, EngineInfo
from engines.unreal.detector import find_unreal_layout
from engines.unreal.pak_parser import (
    PakInfo,
    inspect_uasset_bytes,
    parse_pak_header_and_entries,
    parse_utoc_header,
    read_pak_entry_bytes,
)
from formats.base import detect_bytes_encoding
from formats.locres_handler import LocresData, parse_locmeta_bytes, parse_locres_bytes
from formats.registry import get_format_handler


ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF\uFE70-\uFEFF]")
ASCII_LETTER_RE = re.compile(r"[A-Za-z]")

UE_ASSET_URI_RE = re.compile(
    r"^(?:/Game/|/Engine/|/Script/|[A-Za-z0-9_]+'/Game/|[A-Za-z0-9_]+'/Engine/)",
    re.IGNORECASE,
)
UE_EXT_RE = re.compile(
    r"\.(?:uasset|umap|uexp|ubulk|pak|sig|utoc|ucas|locres|locmeta|"
    r"png|jpe?g|webp|tga|bmp|wav|mp3|ogg|ttf|otf|ini|cfg|dll|exe)$",
    re.IGNORECASE,
)
URL_RE = re.compile(r"^(?:https?://|file://|mailto:)", re.IGNORECASE)
HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6,8}$")
TECH_ID_NO_SPACE_RE = re.compile(
    r"^(?:[a-z]+_[a-z0-9_]+|[A-Z]+_[A-Z0-9_]+|[a-z]+[A-Z][A-Za-z0-9]*|[a-z0-9_]+(?:\.[a-z0-9_]+)+|BP_[A-Za-z0-9_]+|UI_[A-Za-z0-9_]+|E_[A-Za-z0-9_]+)$"
)
STRIP_TOKENS_RE = re.compile(
    r"\{[A-Za-z0-9_]+(?::[^}\n]+)?\}\|(?:plural|gender|ordinal|hpp)\([^)\n]+\)|"
    r"\{[A-Za-z0-9_]+(?::[^}\n]+)?\}|<[^>]+>|%(?:(?:[0-9]+\$)?[-+0#]*[0-9]*(?:\.[0-9]+)?[sdifuxXoOgGc%]|[0-9]+)|\\n|\\t"
)

IGNORED_KEY_SUFFIXES = (
    ".path",
    ".asset",
    ".asset_path",
    ".class",
    ".blueprint",
    ".sound",
    ".texture",
    ".material",
    ".mesh",
    ".animation",
    ".widget",
    ".tag",
    ".gameplay_tag",
)

ENGLISH_TOKENS = {"en", "en_us", "en-us", "en_gb", "en-gb", "eng", "english"}
SIBLING_LANG_TOKENS = {
    "fr", "fr_fr", "fr-fr", "de", "de_de", "de-de", "es", "es_es", "es-es", "es-419",
    "ru", "ru_ru", "ru-ru", "it", "pt", "pt_br", "pt-br", "ja", "jp", "zh", "zh_cn",
    "zh-hans", "zh-hant", "zh_tw", "ko", "pl", "tr", "nl", "uk", "cs", "hu", "sv",
    "no", "da", "fi", "el", "th", "vi", "id", "ar", "ar_sa", "ar-sa", "french",
    "german", "spanish", "russian", "italian", "portuguese", "japanese", "chinese",
    "korean", "polish", "turkish", "dutch", "arabic",
}

TEXT_DATA_EXTS = {".csv", ".tsv", ".json", ".po", ".ini", ".txt", ".yaml", ".yml"}
PACKED_TEXT_PATH_HINTS = (
    "localization", "language", "locale", "dialog", "dialogue",
    "subtitle", "strings", "/text/", "/data/",
)
IGNORED_TEXT_BASENAMES = (
    "default", "base", "compat", "engine", "manifest", "assetregistry", "shader",
)
ARABIC_FONT_TOKENS = (
    "arabic", "noto", "droidsansfallback", "ibmplexsansarabic",
    "cairo", "tajawal", "amiri", "tahoma", "arial", "naskh", "kufi",
)
UASSET_INSPECT_KEYWORDS = (
    "localization", "stringtable", "strings", "dialog", "dialogue",
    "subtitle", "text", "data", "font", "slate", "/ui/",
    "/st_", "/dt_", "/f_",
)


def _is_arabic_font_name(name: str) -> bool:
    low = name.lower()
    if "notosansarabic" in low or "noto" in low and "arab" in low:
        return True
    if "notonaskharabic" in low or "notokufiarabic" in low:
        return True
    return any(tok in low for tok in ("arabic", "droidsansfallback", "ibmplexsansarabic", "cairo", "tajawal", "amiri", "tahoma", "arial"))


def _is_candidate_uasset_for_inspection(internal_path: str) -> bool:
    low = internal_path.replace("\\", "/").lower()
    if not low.endswith(".uasset"):
        return False
    base = os.path.basename(low)
    return base.startswith(("st_", "dt_", "f_", "font")) or any(
        kw in low for kw in UASSET_INSPECT_KEYWORDS
    )


def is_translatable_value(key: str, value: str) -> bool:
    """تحديد هل القيمة نص بشري قابل للترجمة في Unreal Engine أم مسار أصل/معرّف تقني."""
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

    if UE_ASSET_URI_RE.match(stripped):
        return False
    if URL_RE.match(stripped):
        return False
    if HEX_COLOR_RE.match(stripped):
        return False
    if UE_EXT_RE.search(stripped) and " " not in stripped:
        return False

    without_tokens = STRIP_TOKENS_RE.sub("", stripped).strip()
    if not without_tokens or not ASCII_LETTER_RE.search(without_tokens):
        return False

    if " " not in stripped and TECH_ID_NO_SPACE_RE.match(stripped):
        return False

    return True


def _extract_path_tokens(rel_path: str) -> List[str]:
    norm = rel_path.replace("\\", "/").lower()
    segs = [s for s in norm.split("/") if s]
    tokens: List[str] = []
    for seg in segs:
        stem = seg.rsplit(".", 1)[0] if "." in seg else seg
        if stem:
            tokens.append(stem)
        for part in re.split(r"[._]+", stem):
            if part and part != stem:
                tokens.append(part)
    return tokens


def _count_locres_entries(locres: LocresData) -> Tuple[int, int, bool]:
    total = 0
    translatable = 0
    samples: List[str] = []
    for ns in locres.namespaces:
        for entry in ns.entries:
            total += 1
            flat_key = f"{ns.name}.{entry.key}" if ns.name else entry.key
            if is_translatable_value(flat_key, entry.text):
                translatable += 1
            if entry.text and len(samples) < 80:
                samples.append(entry.text)
    has_arabic = calculate_arabic_ratio("\n".join(samples)) > 0.05 if samples else False
    return total, translatable, has_arabic


def _score_unreal_candidate(
    rel_path: str,
    ext: str,
    total_entries: int,
    translatable_entries: int,
    is_packed: bool = False,
    native_cultures: Optional[Set[str]] = None,
) -> Tuple[int, List[str]]:
    reasons: List[str] = []
    if translatable_entries <= 0:
        return 10, [f"ملف {ext} لا يحتوي على نصوص بشرية قابلة للترجمة"]

    if ext == ".locres":
        score = 73 if is_packed else 75
        reasons.append(f"ملف .locres قياسي: {translatable_entries}/{total_entries} نص قابل للترجمة")
    else:
        base_map = {".po": 66, ".csv": 64, ".tsv": 62, ".json": 58, ".ini": 48, ".txt": 44}
        score = base_map.get(ext, 42) - (2 if is_packed else 0)
        reasons.append(f"ملف جداول/نصوص Unreal ({ext}): {translatable_entries}/{total_entries} نص قابل للترجمة")

    low_rel = rel_path.replace("\\", "/").lower()
    if "localization" in low_rel:
        score += 12
        reasons.append("يقع داخل مجلد Localization الرسمي للمحرك")
    elif any(k in low_rel for k in ("language", "locale", "dialog", "dialogue", "subtitle", "strings")):
        score += 8
        reasons.append("يقع داخل مسار نصوص/ترجمة صريح")

    tokens = _extract_path_tokens(rel_path)
    is_english = any(t in ENGLISH_TOKENS for t in tokens)
    is_sibling = any(t in SIBLING_LANG_TOKENS for t in tokens)
    is_native_match = bool(native_cultures and any(t in native_cultures for t in tokens))

    if is_english:
        score += 13
        reasons.append("مسار/اسم الملف يشير إلى اللغة الإنجليزية المصدر")
    elif is_native_match:
        score += 10
        reasons.append("يطابق الثقافة الأم (NativeCulture) المسجلة في .locmeta")
    elif is_sibling:
        score -= 55
        reasons.append("مسار/اسم الملف يشير إلى لغة ثانية غير الإنجليزية (تم خفض الأولوية)")

    score = max(0, min(100, score))
    return score, reasons


def _is_packed_text_candidate(internal_path: str) -> bool:
    low = internal_path.replace("\\", "/").lower()
    ext = os.path.splitext(low)[1]
    if ext not in TEXT_DATA_EXTS:
        return False
    base = os.path.basename(low)
    if base.startswith(IGNORED_TEXT_BASENAMES):
        return False
    return any(tok in low for tok in PACKED_TEXT_PATH_HINTS)


def _parse_packed_text_entries(ext: str, raw_bytes: bytes) -> Optional[Tuple[int, int, bool]]:
    fmt = "csv" if ext in (".csv", ".tsv") else ("ini" if ext in (".ini", ".txt") else ext.lstrip("."))
    handler = get_format_handler(fmt)
    if handler is None:
        return None
    with tempfile.NamedTemporaryFile("wb", suffix=ext, delete=False) as tf:
        tf.write(raw_bytes)
        tmp_path = tf.name
    try:
        doc = handler.load(tmp_path)
    except Exception:
        return None
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    total = len(doc.entries)
    trans = sum(1 for _, k, v in doc.entries if is_translatable_value(k, v))
    enc = detect_bytes_encoding(raw_bytes)
    sample_text = raw_bytes[:200000].decode(enc, errors="replace")
    has_arabic = calculate_arabic_ratio(sample_text) > 0.05
    return total, trans, has_arabic


class UnrealLanguageScanner:
    """يمسح ملفات .locres وملفات النصوص/البيانات الخارجية والمضمّنة داخل .pak لمحرك Unreal Engine."""

    def scan(self, engine: EngineInfo) -> Tuple[List[CandidateFile], List[str]]:
        abs_dir = os.path.abspath(engine.game_dir)
        layout = find_unreal_layout(abs_dir)
        candidates: List[CandidateFile] = []
        hints: List[str] = []
        native_cultures: Set[str] = set()

        if layout.project_name:
            hints.append(f"Project: {layout.project_name} ({layout.ue_version_hint})")
        if layout.sig_files:
            hints.append(
                f"تحذير توقيع الحزم (.sig): تم اكتشاف {len(layout.sig_files)} ملف توقيع؛ "
                "قد ترفض اللعبة تحميل ~mods/z_ArabicL10n_P.pak بدون تعطيل التحقق من التوقيع."
            )
        utoc_font_assets: List[str] = []
        if layout.utoc_files or layout.ucas_files:
            hints.append(
                f"تم اكتشاف ملفات IoStore (.utoc/.ucas: {len(layout.utoc_files)}/{len(layout.ucas_files)})."
            )
            for utoc_path in layout.utoc_files[:8]:
                utoc_name = os.path.basename(utoc_path)
                uinfo = parse_utoc_header(utoc_path)
                if uinfo is None:
                    continue
                if not uinfo.header_complete:
                    hints.append(f"IoStore ({utoc_name}): ترويسة أولية صالحة بدون جسم كامل.")
                    continue
                flags_list: List[str] = []
                if uinfo.compressed:
                    flags_list.append("مضغوط")
                if uinfo.encrypted:
                    flags_list.append("مشفّر AES")
                if uinfo.signed:
                    flags_list.append("موقّع")
                if uinfo.indexed:
                    flags_list.append("مفهرس")
                flags_desc = "، ".join(flags_list) if flags_list else "غير مضغوط"
                comp_desc = f" | ضغط: {', '.join(uinfo.compression_methods)}" if uinfo.compression_methods else ""
                hints.append(
                    f"IoStore ({utoc_name} v{uinfo.version}): {uinfo.toc_entry_count} أصل [{flags_desc}]{comp_desc}"
                )
                if uinfo.detected_locres_paths:
                    hints.append(
                        f"IoStore ({utoc_name}) مؤشرات .locres: {', '.join(uinfo.detected_locres_paths[:5])}"
                    )
                if uinfo.detected_string_tables:
                    hints.append(
                        f"IoStore ({utoc_name}) مؤشرات StringTable: {', '.join(uinfo.detected_string_tables[:5])}"
                    )
                if uinfo.detected_font_assets:
                    utoc_font_assets.extend(uinfo.detected_font_assets)

        for lm_path in layout.locmeta_files:
            try:
                with open(lm_path, "rb") as f:
                    lm = parse_locmeta_bytes(f.read())
                if lm.native_culture:
                    native_cultures.add(lm.native_culture.lower())
                cultures_str = ", ".join(lm.compiled_cultures) if lm.compiled_cultures else lm.native_culture
                hints.append(
                    f".locmeta ({os.path.basename(lm_path)}): Native={lm.native_culture} | Cultures=[{cultures_str}]"
                )
            except Exception:
                pass

        external_rel_seen: Set[str] = set()

        # 1. مسح ملفات .locres الخارجية على القرص
        for locres_path in layout.locres_files:
            rel_path = os.path.relpath(locres_path, abs_dir).replace(os.sep, "/")
            try:
                with open(locres_path, "rb") as f:
                    raw_bytes = f.read()
                locres = parse_locres_bytes(raw_bytes)
            except Exception as exc:
                hints.append(f"تعذر تحليل ملف .locres '{rel_path}': {exc}")
                continue

            total, trans, has_arabic = _count_locres_entries(locres)
            score, reasons = _score_unreal_candidate(
                rel_path, ".locres", total, trans, is_packed=False, native_cultures=native_cultures
            )
            if has_arabic:
                reasons.append("يحتوي على نصوص عربية مسبقاً")

            external_rel_seen.add(rel_path.lower())
            candidates.append(
                CandidateFile(
                    path=locres_path,
                    relative_path=rel_path,
                    format_name="locres",
                    score=score,
                    reasons=reasons,
                    dll_matched=("localization" in rel_path.lower()),
                    contains_arabic=has_arabic,
                    total_entries=total,
                    translatable_entries=trans,
                    skip_backup=False,
                )
            )

        # 2. مسح ملفات النصوص/الجداول الخارجية (CSV/JSON/PO/INI/TXT) في مجلدات اللغة والبيانات
        for txt_path in layout.text_data_files:
            rel_path = os.path.relpath(txt_path, abs_dir).replace(os.sep, "/")
            ext = os.path.splitext(txt_path.lower())[1]
            fmt = "csv" if ext in (".csv", ".tsv") else ("ini" if ext in (".ini", ".txt") else ext.lstrip("."))
            handler = get_format_handler(fmt)
            if handler is None:
                continue
            try:
                doc = handler.load(txt_path)
            except Exception:
                continue

            total = len(doc.entries)
            trans = sum(1 for _, k, v in doc.entries if is_translatable_value(k, v))
            if trans <= 0:
                continue
            try:
                with open(txt_path, "rb") as f:
                    sample_raw = f.read(200000)
                has_arabic = calculate_arabic_ratio(
                    sample_raw.decode(detect_bytes_encoding(sample_raw), errors="replace")
                ) > 0.05
            except OSError:
                has_arabic = False

            score, reasons = _score_unreal_candidate(
                rel_path, ext, total, trans, is_packed=False, native_cultures=native_cultures
            )
            if has_arabic:
                reasons.append("يحتوي على نصوص عربية مسبقاً")

            external_rel_seen.add(rel_path.lower())
            candidates.append(
                CandidateFile(
                    path=txt_path,
                    relative_path=rel_path,
                    format_name=fmt,
                    score=score,
                    reasons=reasons,
                    dll_matched=("localization" in rel_path.lower()),
                    contains_arabic=has_arabic,
                    total_entries=total,
                    translatable_entries=trans,
                    skip_backup=False,
                )
            )

        # 3. مسح الملفات المضمّنة داخل حزم .pak دون تعديل الحزم الأصلية
        packed_loc_found = 0
        cooked_string_tables: List[str] = []
        cooked_data_tables: List[str] = []
        discovered_fonts: List[str] = list(utoc_font_assets)

        # جمع ملفات الخطوط الموجودة على القرص (خاصة Engine/Content/Slate/Fonts و Content/Slate/Fonts)
        for font_root in (
            os.path.join(abs_dir, "Engine", "Content", "Slate", "Fonts"),
            os.path.join(layout.content_dir, "Slate", "Fonts") if layout.content_dir else "",
            os.path.join(layout.content_dir, "Localization", "ArabicFonts") if layout.content_dir else "",
        ):
            if font_root and os.path.isdir(font_root):
                try:
                    for fname in sorted(os.listdir(font_root)):
                        if fname.lower().endswith((".ttf", ".otf", ".uasset")):
                            discovered_fonts.append(fname)
                except OSError:
                    pass

        for pak_path in layout.pak_files:
            pak_rel = os.path.relpath(pak_path, abs_dir).replace(os.sep, "/")
            pak_info: Optional[PakInfo] = parse_pak_header_and_entries(pak_path)
            if pak_info is None:
                hints.append(f"تعذر قراءة ترويسة الحزمة: {pak_rel}")
                continue
            if pak_info.encrypted_index:
                hints.append(
                    f"تحذير: فهرس الحزمة {pak_rel} مشفّر بـ AES (يتطلب مفتاح فك التشفير لاستخراج .locres)."
                )
                continue
            if not pak_info.has_full_dir_index and pak_info.raw_entry_count > 0:
                hints.append(
                    f"تحذير: الحزمة {pak_rel} لا تحتوي FullDirectoryIndex ({pak_info.raw_entry_count} مدخل بأسماء مسارات محذوفة)."
                )

            hints.append(f"PAK v{pak_info.version} ({pak_rel}): {len(pak_info.entries)} ملف")
            pak_entries_by_low = {e.path.replace("\\", "/").lower(): e for e in pak_info.entries}
            uasset_inspected = 0
            for entry in pak_info.entries:
                low_entry = entry.path.replace("\\", "/").lower()
                if low_entry.endswith((".ttf", ".otf")):
                    discovered_fonts.append(entry.path)
                if low_entry.endswith(".locmeta") and not entry.encrypted:
                    lm_bytes = read_pak_entry_bytes(pak_info, entry)
                    if lm_bytes:
                        try:
                            lm = parse_locmeta_bytes(lm_bytes)
                            if lm.native_culture:
                                native_cultures.add(lm.native_culture.lower())
                            cultures_str = ", ".join(lm.compiled_cultures) if lm.compiled_cultures else lm.native_culture
                            hints.append(
                                f"PAK .locmeta ({os.path.basename(entry.path)}): Native={lm.native_culture} | Cultures=[{cultures_str}]"
                            )
                        except Exception:
                            pass
                    continue

                if (
                    not entry.encrypted
                    and uasset_inspected < 60
                    and _is_candidate_uasset_for_inspection(entry.path)
                ):
                    uasset_inspected += 1
                    uasset_bytes = read_pak_entry_bytes(pak_info, entry)
                    if uasset_bytes:
                        uexp_entry = pak_entries_by_low.get(low_entry[:-7] + ".uexp")
                        uexp_bytes = (
                            read_pak_entry_bytes(pak_info, uexp_entry)
                            if (uexp_entry is not None and not uexp_entry.encrypted)
                            else None
                        )
                        insp = inspect_uasset_bytes(uasset_bytes, uexp_bytes)
                        if insp.is_uasset:
                            if insp.asset_kind == "StringTable":
                                st_label = (
                                    f"{os.path.basename(entry.path)} ({len(insp.string_table_entries)} نص)"
                                    if insp.string_table_entries
                                    else os.path.basename(entry.path)
                                )
                                cooked_string_tables.append(st_label)
                            elif insp.asset_kind == "DataTable":
                                cooked_data_tables.append(os.path.basename(entry.path))
                            elif insp.asset_kind == "Font":
                                discovered_fonts.extend(insp.font_references or [os.path.basename(entry.path)])
                    continue

                is_locres = low_entry.endswith(".locres")
                is_packed_text = (not is_locres) and _is_packed_text_candidate(entry.path)
                if not is_locres and not is_packed_text:
                    continue
                if entry.encrypted:
                    hints.append(f"تحذير: الملف '{entry.path}' داخل {pak_rel} مشفّر بـ AES.")
                    continue
                if low_entry in external_rel_seen:
                    continue

                raw_bytes = read_pak_entry_bytes(pak_info, entry)
                if raw_bytes is None:
                    hints.append(
                        f"تحذير: تعذر فك ضغط '{entry.path}' داخل {pak_rel} (طريقة الضغط: {entry.compression_method})."
                    )
                    continue

                if is_locres:
                    try:
                        locres = parse_locres_bytes(raw_bytes)
                    except Exception as exc:
                        hints.append(f"تعذر تحليل '{entry.path}' داخل {pak_rel}: {exc}")
                        continue
                    total, trans, has_arabic = _count_locres_entries(locres)
                    ext = ".locres"
                else:
                    ext = os.path.splitext(low_entry)[1]
                    parsed_txt = _parse_packed_text_entries(ext, raw_bytes)
                    if not parsed_txt:
                        continue
                    total, trans, has_arabic = parsed_txt
                    if trans <= 0:
                        continue

                packed_loc_found += 1
                score, reasons = _score_unreal_candidate(
                    entry.path, ext, total, trans, is_packed=True, native_cultures=native_cultures
                )
                reasons.insert(0, f"مستخرج من حزمة Unreal ({pak_rel})")
                if has_arabic:
                    reasons.append("يحتوي على نصوص عربية مسبقاً")

                candidates.append(
                    CandidateFile(
                        path=f"{pak_path}::{entry.path}",
                        relative_path=f"{pak_rel}::{entry.path}",
                        format_name="unreal_pak",
                        score=score,
                        reasons=reasons,
                        dll_matched=("localization" in low_entry),
                        contains_arabic=has_arabic,
                        total_entries=total,
                        translatable_entries=trans,
                        skip_backup=True,
                    )
                )

        if cooked_string_tables or cooked_data_tables:
            parts: List[str] = []
            if cooked_string_tables:
                parts.append(f"UStringTable ({len(cooked_string_tables)}: {', '.join(cooked_string_tables[:4])})")
            if cooked_data_tables:
                parts.append(f"UDataTable ({len(cooked_data_tables)}: {', '.join(cooked_data_tables[:4])})")
            hints.append(
                "أصول مطبوخة (.uasset/.uexp) مكتشفة: " + " | ".join(parts)
            )

        if discovered_fonts:
            arabic_fonts = [fn for fn in discovered_fonts if _is_arabic_font_name(fn)]
            if arabic_fonts:
                hints.append(
                    f"تقرير الخط العربي (جاهز): تم رصد خط يدعم العربية مدمجاً ({os.path.basename(arabic_fonts[0])})."
                )
            else:
                sample_latin = ", ".join(os.path.basename(f) for f in discovered_fonts[:3])
                hints.append(
                    f"تقرير الخط العربي (تحذير مربعات محتمل): تم رصد خطوط لاتينية/مطبوخة فقط ({sample_latin}) "
                    "دون خط Slate عربي احتياطي؛ قد تظهر مربعات ما لم تتوفر أصول UFontFace عربية."
                )
        elif layout.pak_files or layout.utoc_files:
            hints.append(
                "تقرير الخط العربي (تحذير مربعات محتمل): لم يُرصد خط عربي احتياطي صريح في Slate/Fonts."
            )

        if (layout.utoc_files or layout.ucas_files) and not layout.locres_files and packed_loc_found == 0:
            hints.append(
                "تنبيه IoStore: لم يُعثر على .locres داخل .pak؛ قد تكون ملفات اللغة مخزّنة داخل حاوية .utoc/.ucas وتتطلب استخراجها خارجياً."
            )

        candidates.sort(key=lambda c: (-c.score, -c.translatable_entries, c.relative_path))
        return candidates, hints
