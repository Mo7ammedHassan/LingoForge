# -*- coding: utf-8 -*-
"""خوارزمية اكتشاف ملف اللغة بدون موديل (4 طبقات: فحص كود DLL + تقييم النقاط + فلتر الحقول + بدائل الطوارئ)."""

import base64
import concurrent.futures
import csv
from dataclasses import dataclass, field
import json
import os
import re
import struct
from typing import Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET

from core.backup import calculate_arabic_ratio, is_file_likely_arabic
from core.models import CandidateFile, EngineInfo
from engines.unity.assets_extractor import (
    _detect_payload_format,
    _is_assembly_qualified_type_name,
    extract_assets_localization_blocks,
)
from engines.unity.il2cpp_metadata import extract_il2cpp_metadata_strings
from formats.base import detect_bytes_encoding, read_text_file_with_encoding
from formats.json_handler import clean_jsonc, flatten_json_strings, load_json_file
from formats.registry import get_format_handler


CANDIDATE_EXTENSIONS = {".json", ".csv", ".tsv", ".xml", ".ini", ".po", ".txt", ".bytes", ".yaml", ".yml"}
SKIP_DIR_NAMES = {
    "monobleedingedge",
    "plugins",
    "unitysubsystems",
    ".arabic_translator",
    "__pycache__",
    ".git",
    "logs",
    "crashdumps",
}
EXTERNAL_SKIP_DIR_NAMES = SKIP_DIR_NAMES | {"managed", "il2cpp_data"}
PRIORITY_DIR_NAMES = {
    "streamingassets",
    "resources",
    "localization",
    "language",
    "languages",
    "languagefiles",
    "locale",
    "locales",
    "local",
    "loc",
    "l10n",
}
ENGLISH_FILE_STEMS = {"english", "en", "en-us", "en_us", "en-gb", "en_gb", "eng"}
NAME_KEYWORDS = ENGLISH_FILE_STEMS | {
    "default",
    "strings",
    "localization",
    "localisation",
    "local",
    "loc",
    "l10n",
    "lang",
    "language",
    "languages",
    "text",
    "dialogue",
    "dialogs",
    "subtitles",
}
SIBLING_LANG_KEYWORDS = {
    "afrikaans",
    "albanian",
    "arabic",
    "armenian",
    "basque",
    "belarusian",
    "brazilian",
    "bulgarian",
    "catalan",
    "chinese",
    "croatian",
    "czech",
    "danish",
    "dutch",
    "estonian",
    "farsi",
    "finnish",
    "french",
    "galician",
    "georgian",
    "german",
    "greek",
    "hebrew",
    "hindi",
    "hungarian",
    "icelandic",
    "indonesian",
    "irish",
    "italian",
    "japanese",
    "korean",
    "latin",
    "latvian",
    "lithuanian",
    "macedonian",
    "malay",
    "norwegian",
    "persian",
    "polish",
    "portuguese",
    "romanian",
    "russian",
    "scottish",
    "serbian",
    "slovak",
    "slovene",
    "slovenian",
    "spanish",
    "swedish",
    "thai",
    "turkish",
    "ukrainian",
    "vietnamese",
    "welsh",
    "fr",
    "de",
    "es",
    "it",
    "ru",
    "ja",
    "zh",
    "ko",
    "pt",
}
UI_VOCABULARY = {
    "new game",
    "continue",
    "options",
    "settings",
    "audio",
    "graphics",
    "resolution",
    "fullscreen",
    "save",
    "load",
    "quit",
    "exit",
    "cancel",
    "back",
    "confirm",
    "press",
    "controls",
    "volume",
    "language",
    "play",
    "subtitles",
    "resume",
    "start",
    "credits",
    "return",
    "retry",
    "restart",
    "pause",
    "gallery",
    "tutorial",
    "achievements",
    "loading",
    "apply",
    "reset",
    "defaults",
    "windowed",
    "keyboard",
    "gamepad",
}
CONFIG_PENALTY_NAMES = {
    "catalog.json",
    "settings.json",
    "manifest.json",
    "package.json",
    "steam_appid.txt",
    "boot.config",
    "scriptingassemblies.json",
    "runtimeinitializeonload.json",
    "runtimeinitializeonloads.json",
    "arabicoverride.json",
    "arabic_captured_texts.txt",
    "app.info",
    "backup_manifest.json",
    "arabic_font_log.txt",
    "arabic_translator_log.txt",
}
GENERATED_RUNTIME_FILENAMES = frozenset(
    {
        "arabicoverride.json",
        "arabic_captured_texts.txt",
        "arabic_font_log.txt",
        "arabic_translator_log.txt",
    }
)
HINT_KEYWORDS = (
    "language",
    "locale",
    "localiz",
    "i2lang",
    "strings",
    "subtitle",
    "dialog",
    ".json",
    ".csv",
    ".xml",
    ".ini",
    ".po",
    ".yaml",
    ".yml",
    "english",
    "en-us",
    "en_us",
)

ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
ARABIC_ANY_RE = re.compile(r"[\u0600-\u06FF\uFE70-\uFEFF]")
INPUT_BINDING_RE = re.compile(r"^<[A-Za-z0-9_]+>/")
PLACEHOLDER_FIND_RE = re.compile(r"\{[0-9]+(?::[^}]+)?\}|<[^>]+>|%[sdif]|\\n")
URL_OR_PATH_RE = re.compile(r"^(?:https?://|file://|mailto:|Assets[/\\]|[A-Za-z]:[/\\])", re.IGNORECASE)
TECH_SNAKE_OR_DOT_RE = re.compile(r"^[A-Za-z0-9_.\-/\\]+$")
CAMEL_CASE_RE = re.compile(r"^[a-z]+[A-Z][A-Za-z0-9]*$")
HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6,8}$")


def _read_compressed_uint(data: bytes, offset: int) -> Tuple[int, int]:
    """قراءة طول مضغوط وفق تنسيق بيانات تعريف .NET CLI (#US)."""
    b0 = data[offset]
    if (b0 & 0x80) == 0:
        return b0, offset + 1
    if (b0 & 0xC0) == 0x80 and offset + 1 < len(data):
        val = ((b0 & 0x3F) << 8) | data[offset + 1]
        return val, offset + 2
    if (b0 & 0xE0) == 0xC0 and offset + 3 < len(data):
        val = ((b0 & 0x1F) << 24) | (data[offset + 1] << 16) | (data[offset + 2] << 8) | data[offset + 3]
        return val, offset + 4
    return 0, offset + 1


def _extract_dotnet_us_strings(dll_path: str) -> List[str]:
    """استخراج السلاسل النصية مباشرة من مجرى #US في ملفات .NET PE."""
    try:
        with open(dll_path, "rb") as f:
            data = f.read()
    except OSError:
        return []

    if len(data) < 256 or data[:2] != b"MZ":
        return []

    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_off + 24 > len(data) or data[pe_off : pe_off + 4] != b"PE\x00\x00":
        return []

    num_sections = struct.unpack_from("<H", data, pe_off + 6)[0]
    opt_hdr_size = struct.unpack_from("<H", data, pe_off + 20)[0]
    opt_off = pe_off + 24
    if opt_off + opt_hdr_size > len(data):
        return []

    magic = struct.unpack_from("<H", data, opt_off)[0]
    data_dir_off = opt_off + (96 if magic == 0x10B else 112)
    clr_dir_off = data_dir_off + 14 * 8
    if clr_dir_off + 8 > len(data):
        return []

    clr_rva, clr_size = struct.unpack_from("<II", data, clr_dir_off)
    if clr_rva == 0 or clr_size == 0:
        return []

    sec_table_off = opt_off + opt_hdr_size
    sections: List[Tuple[int, int, int]] = []
    for idx in range(num_sections):
        sh_off = sec_table_off + idx * 40
        if sh_off + 40 > len(data):
            break
        v_size, v_addr, raw_size, raw_ptr = struct.unpack_from("<IIII", data, sh_off + 8)
        sections.append((v_addr, max(v_size, raw_size), raw_ptr))

    def rva_to_off(rva: int) -> Optional[int]:
        for v_addr, span, raw_ptr in sections:
            if v_addr <= rva < v_addr + span:
                return raw_ptr + (rva - v_addr)
        return None

    clr_off = rva_to_off(clr_rva)
    if clr_off is None or clr_off + 16 > len(data):
        return []

    meta_rva, _ = struct.unpack_from("<II", data, clr_off + 8)
    meta_off = rva_to_off(meta_rva)
    if meta_off is None or meta_off + 16 > len(data) or data[meta_off : meta_off + 4] != b"BSJB":
        return []

    ver_len = struct.unpack_from("<I", data, meta_off + 12)[0]
    ver_padded = (ver_len + 3) & ~3
    streams_hdr_off = meta_off + 16 + ver_padded
    if streams_hdr_off + 4 > len(data):
        return []

    num_streams = struct.unpack_from("<H", data, streams_hdr_off + 2)[0]
    cur = streams_hdr_off + 4
    us_offset = None
    us_size = 0

    for _ in range(num_streams):
        if cur + 8 > len(data):
            break
        s_off, s_size = struct.unpack_from("<II", data, cur)
        cur += 8
        name_start = cur
        while cur < len(data) and data[cur] != 0:
            cur += 1
        s_name = data[name_start:cur].decode("ascii", errors="ignore")
        cur += 1
        # محاذاة لأقرب 4 بايت
        cur = (cur + 3) & ~3
        if s_name == "#US":
            us_offset = meta_off + s_off
            us_size = s_size
            break

    if us_offset is None or us_offset + us_size > len(data):
        return []

    us_blob = data[us_offset : us_offset + us_size]
    strings: List[str] = []
    pos = 1  # البايت الأول فارغ دائماً في #US
    while pos < len(us_blob):
        byte_len, next_pos = _read_compressed_uint(us_blob, pos)
        if next_pos <= pos:
            break
        pos = next_pos
        if byte_len <= 1:
            pos += byte_len
            continue
        end = min(len(us_blob), pos + byte_len - 1)
        try:
            s = us_blob[pos:end].decode("utf-16-le", errors="ignore")
            if s:
                strings.append(s)
        except Exception:
            pass
        pos += byte_len

    return strings


def _filter_localization_hints(
    strings: List[str], hints: List[str], seen: Set[str]
) -> None:
    """تصفية السلاسل المستخرجة لاقتناص أدلة مسارات وصيغ ملفات اللغة."""
    for s in strings:
        if len(s) < 4 or len(s) > 120 or "\n" in s or "\r" in s:
            continue
        low = s.lower()
        if any(kw in low for kw in HINT_KEYWORDS):
            if s not in seen:
                seen.add(s)
                hints.append(s)


def extract_dll_localization_hints(managed_dir: Optional[str]) -> List[str]:
    """الطبقة 1: استخراج أدلة مسارات وصيغ ملفات اللغة مباشرة من ملفات DLL اللعبة."""
    if not managed_dir or not os.path.isdir(managed_dir):
        return []

    target_dlls: List[str] = []
    main_asm = os.path.join(managed_dir, "Assembly-CSharp.dll")
    if os.path.isfile(main_asm):
        target_dlls.append(main_asm)

    try:
        for name in sorted(os.listdir(managed_dir)):
            low = name.lower()
            if not low.endswith(".dll") or name == "Assembly-CSharp.dll":
                continue
            if low.startswith(("system.", "unity.", "unityengine.", "mono.", "mscorlib", "netstandard")):
                continue
            if "assembly" in low or "game" in low or "local" in low or "i2" in low:
                target_dlls.append(os.path.join(managed_dir, name))
    except OSError:
        pass

    hints: List[str] = []
    seen: Set[str] = set()
    for dll in target_dlls[:6]:
        _filter_localization_hints(_extract_dotnet_us_strings(dll), hints, seen)
    return hints


def extract_il2cpp_localization_hints(
    metadata_path: Optional[str], interop_dir: Optional[str] = None
) -> List[str]:
    """الطبقة 1 (IL2CPP): استخراج أدلة اللغة من global-metadata.dat ومجلد Interop."""
    hints: List[str] = []
    seen: Set[str] = set()

    if metadata_path and os.path.isfile(metadata_path):
        _filter_localization_hints(
            extract_il2cpp_metadata_strings(metadata_path), hints, seen
        )

    if interop_dir and os.path.isdir(interop_dir):
        for hint in extract_dll_localization_hints(interop_dir):
            if hint not in seen:
                seen.add(hint)
                hints.append(hint)

    return hints


def is_translatable_value(key: str, value: str) -> bool:
    """الطبقة 3: فلتر الحقول الذكي لاستبعاد المعرفات البرمجية والمسارات والنصوص المترجمة مسبقاً."""
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped or not ASCII_LETTER_RE.search(stripped):
        return False
    if ARABIC_ANY_RE.search(stripped):
        return False
    if URL_OR_PATH_RE.match(stripped) or HEX_COLOR_RE.match(stripped):
        return False
    if INPUT_BINDING_RE.match(stripped):
        return False
    if stripped.endswith(" SDF") or stripped.endswith("_SDF"):
        return False

    # إزالة المتغيرات والوسوم للتأكد أن النص يحتوي كلاماً بشرياً فعلياً وليس مجرد "{0}"
    without_tokens = PLACEHOLDER_FIND_RE.sub("", stripped).strip()
    if not ASCII_LETTER_RE.search(without_tokens):
        return False

    # إذا احتوى النص على مسافات، فهو على الأرجح جملة طبيعية (إلا لو كان مساراً تقنياً صريحاً)
    if " " in without_tokens:
        return True

    # كلمة مفردة بدون مسافات: نستبعد snake_case / camelCase / المسارات التقنية
    if "_" in without_tokens or "/" in without_tokens or "\\" in without_tokens:
        return False
    if "." in without_tokens.rstrip("."):
        return False
    if CAMEL_CASE_RE.match(without_tokens):
        return False

    # قبول الكلمات الواضحة في القوائم مثل "Play", "Save", "OK", "Exit", "Yes"
    if len(without_tokens) >= 2 and ASCII_LETTER_RE.search(without_tokens):
        return True
    return False


_JSON_ARRAY_ELEM_START_RE = re.compile(
    r'^(?:\]|\{|\[|"|-?\d|(?:true|false|null)(?=[\s,\]]|$))'
)


def _looks_like_json_payload(sample_text: str, is_complete: bool) -> bool:
    """التحقق من أن بداية ملف .json تمثل JSON/JSONC فعلياً لا نصاً سطرياً يبدأ بـ [الاسم] أو تعليق."""
    cleaned = clean_jsonc(sample_text).lstrip(" \t\r\n")
    if not cleaned:
        return False

    first = cleaned[0]
    if first not in ("{", "["):
        return False

    if is_complete:
        try:
            json.loads(cleaned)
            return True
        except Exception:
            pass

    after = cleaned[1:].lstrip(" \t\r\n")
    if first == "{":
        return not after or after[0] in ('"', "}")

    if not after:
        return True
    if after[0] in ("{", '"', "["):
        return True
    if _JSON_ARRAY_ELEM_START_RE.match(after):
        return not is_complete
    return False


def detect_candidate_format(file_path: str, ext: str) -> Optional[str]:
    """تحديد صيغة ملف اللغة فعلياً حسب الامتداد أو المحتوى الداخلي لملفات .json/.txt/.bytes."""
    ext_low = ext.lower()
    if ext_low in (".csv", ".tsv", ".xml", ".ini", ".po", ".yaml", ".yml"):
        return "yaml" if ext_low == ".yml" else ext_low.lstrip(".")

    if ext_low not in (".json", ".txt", ".bytes"):
        return None

    read_cap = 16384 if ext_low == ".json" else 65536
    try:
        with open(file_path, "rb") as f:
            raw_probe = f.read(read_cap + 1)
    except OSError:
        return None

    is_complete = len(raw_probe) <= read_cap
    raw_head = raw_probe[:read_cap]
    if not raw_head:
        return None

    enc = detect_bytes_encoding(raw_head)
    if ext_low in (".bytes", ".json") and not enc.startswith("utf-16") and b"\x00" in raw_head:
        return None

    sample_text = raw_head.decode(enc, errors="ignore")
    if ext_low == ".json":
        stripped = sample_text.lstrip(" \t\r\n\ufeff")
        if not stripped:
            return None
        if stripped.startswith(("{", "[", "//", "/*")) and _looks_like_json_payload(sample_text, is_complete):
            return "json"
        inner_fmt = _detect_payload_format(sample_text)
        if inner_fmt:
            return inner_fmt
        sample_check = sample_text[:4096]
        printable_ratio = sum(
            1 for ch in sample_check if ch.isprintable() or ch in "\r\n\t"
        ) / max(1, len(sample_check))
        non_empty = [ln.strip() for ln in sample_text.splitlines() if ln.strip()]
        text_lines = sum(
            1 for ln in non_empty if ASCII_LETTER_RE.search(ln) or ARABIC_ANY_RE.search(ln)
        )
        if printable_ratio >= 0.95 and len(non_empty) >= 2 and text_lines >= 2:
            return "txt"
        return "json"

    inner_fmt = _detect_payload_format(sample_text)
    if inner_fmt:
        return inner_fmt
    if ext_low == ".txt":
        return "txt"
    return None


def _read_entries_sample(
    file_path: str,
    ext: str,
    limit: int = 1200,
    fmt_name: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """استخراج أزواج (مفتاح، قيمة) من مختلف صيغ الملفات لغرض التقييم."""
    resolved_fmt = fmt_name or detect_candidate_format(file_path, ext) or ext.lstrip(".")
    handler = get_format_handler(resolved_fmt)
    if handler is not None:
        try:
            doc = handler.load(file_path)
            return [(flat_key, val) for _, flat_key, val in doc.entries[:limit]]
        except Exception:
            pass

    entries: List[Tuple[str, str]] = []
    if resolved_fmt == "json":
        data = load_json_file(file_path)
        for _, flat_key, val in flatten_json_strings(data):
            entries.append((flat_key, val))
            if len(entries) >= limit:
                break
        return entries

    if resolved_fmt in ("csv", "tsv"):
        delim = "\t" if resolved_fmt == "tsv" else ","
        with open(file_path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.reader(f, delimiter=delim)
            for row_idx, row in enumerate(reader):
                if not row:
                    continue
                row_key = row[0] if row else f"row_{row_idx}"
                for col_idx, cell in enumerate(row[1:] if len(row) > 1 else row):
                    if cell and ASCII_LETTER_RE.search(cell):
                        entries.append((f"{row_key}_{col_idx}", cell))
                if len(entries) >= limit:
                    break
        return entries

    if resolved_fmt == "xml":
        tree = ET.parse(file_path)
        for elem in tree.getroot().iter():
            if elem.text and elem.text.strip():
                entries.append((elem.tag, elem.text.strip()))
            for attr_k, attr_v in elem.attrib.items():
                if attr_v and ASCII_LETTER_RE.search(attr_v):
                    entries.append((f"{elem.tag}@{attr_k}", attr_v))
            if len(entries) >= limit:
                break
        return entries

    # .ini / .po / .txt
    raw_text, _ = read_text_file_with_encoding(file_path)
    for idx, line in enumerate(raw_text.splitlines()):
        s = line.strip()
        if not s or s.startswith(("#", ";", "//")):
            continue
        if "=" in s:
            k, v = s.split("=", 1)
            entries.append((k.strip(), v.strip()))
        else:
            entries.append((f"line_{idx}", s))
        if len(entries) >= limit:
            break
    return entries


def _matches_dll_hint(rel_path: str, dll_hints: List[str]) -> Optional[str]:
    norm_rel = rel_path.replace("\\", "/").lower()
    base_name = os.path.basename(norm_rel)
    stem = os.path.splitext(base_name)[0]
    ext = os.path.splitext(base_name)[1]
    parent_dir = os.path.basename(os.path.dirname(norm_rel))

    for hint in dll_hints:
        h = hint.replace("\\", "/").lower().strip()
        if not h:
            continue
        # مثال: "LanguageFiles/{0}.json" أو "LanguageFiles" أو "*.json"
        if "{0}" in h:
            prefix, suffix = h.split("{0}", 1)
            prefix_clean = prefix.strip("/")
            if suffix == ext and (not prefix_clean or prefix_clean in norm_rel):
                return hint
        if "*." in h and ext and h.endswith(ext) and len(h) <= 10:
            continue
        if h == base_name or h == stem:
            return hint
        if "/" in h:
            h_dir = os.path.dirname(h).strip("/")
            if h_dir and h_dir in norm_rel and (h.endswith(ext) or h.endswith(base_name)):
                return hint
        elif len(h) >= 5 and h == parent_dir:
            return hint
    return None


def _score_entries_content(
    eval_entries: List[Tuple[str, str]]
) -> Tuple[int, List[str], List[str]]:
    """حساب نقاط محتوى النصوص (مفردات القوائم، الكثافة الطبيعية، والمتغيرات)."""
    score = 0
    reasons: List[str] = []
    translatable_vals: List[str] = []
    vocab_hits: Set[str] = set()
    natural_count = 0
    placeholder_hits = 0

    for k, val in eval_entries:
        if is_translatable_value(k, val):
            translatable_vals.append(val)
            low_val = val.lower()
            if " " in val:
                natural_count += 1
            if PLACEHOLDER_FIND_RE.search(val):
                placeholder_hits += 1
            for word in UI_VOCABULARY:
                if word in low_val:
                    vocab_hits.add(word)

    if vocab_hits:
        pts = min(25, len(vocab_hits) * 4)
        score += pts
        reasons.append(f"+{pts} تطابق مفردات قوائم الألعاب ({len(vocab_hits)} كلمات)")

    if eval_entries:
        density = natural_count / len(eval_entries)
        pts = min(15, int(round(15 * density * 1.5)))
        if pts > 0:
            score += pts
            reasons.append(f"+{pts} كثافة جمل طبيعية ({natural_count}/{len(eval_entries)})")

    if placeholder_hits >= 2:
        score += 10
        reasons.append(f"+10 وجود متغيرات/وسوم ({placeholder_hits})")
    elif placeholder_hits == 1:
        score += 5
        reasons.append("+5 وجود متغير/وسم تنسيق")

    return score, reasons, translatable_vals


def score_candidate_file(
    file_path: str,
    game_dir: str,
    dll_hints: List[str],
    sibling_names: Set[str],
) -> Optional[CandidateFile]:
    """الطبقة 2: حساب درجة الترشيح من 100 لملف نصي مرشح."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in CANDIDATE_EXTENSIONS:
        return None

    rel_path = os.path.relpath(file_path, game_dir)
    base_lower = os.path.basename(file_path).lower()
    stem_lower = os.path.splitext(base_lower)[0]
    parent_lower = os.path.basename(os.path.dirname(file_path)).lower()

    try:
        if os.path.getsize(file_path) > 15 * 1024 * 1024 or os.path.getsize(file_path) < 16:
            return None
        detected_fmt = detect_candidate_format(file_path, ext)
        if detected_fmt is None:
            return None
        entries = _read_entries_sample(file_path, ext, fmt_name=detected_fmt)
    except Exception:
        return None

    if len(entries) < 3:
        return None

    initial_arabic = sum(1 for _, v in entries if ARABIC_ANY_RE.search(v))
    contains_arabic = (initial_arabic / len(entries)) > 0.05 or (
        detected_fmt == "po" and is_file_likely_arabic(file_path)
    )

    eval_entries = entries
    used_clean_bak = False
    bak_path = file_path + ".bak"
    if contains_arabic and os.path.isfile(bak_path):
        try:
            bak_entries = _read_entries_sample(bak_path, ext, fmt_name=detected_fmt)
            if len(bak_entries) >= 3:
                bak_arabic = sum(1 for _, v in bak_entries if ARABIC_ANY_RE.search(v))
                if (bak_arabic / len(bak_entries)) <= 0.05:
                    eval_entries = bak_entries
                    used_clean_bak = True
        except Exception:
            pass

    score = 0
    reasons: List[str] = []
    dll_matched = False

    # 1. تطابق مع أدلة DLL (+35)
    matched_hint = _matches_dll_hint(rel_path, dll_hints)
    if matched_hint:
        score += 35
        dll_matched = True
        reasons.append(f"+35 تطابق مع كود اللعبة ({matched_hint})")

    # 2. اسم الملف والمجلد + اللغات الشقيقة (+25)
    base_lang_stem = stem_lower.split("(")[0].strip()
    rel_dir_parts = {
        p.lower() for p in rel_path.replace("\\", "/").split("/")[:-1] if p
    }
    in_loc_folder = bool(rel_dir_parts & (PRIORITY_DIR_NAMES - {"streamingassets", "resources"}))

    if stem_lower in ENGLISH_FILE_STEMS or parent_lower in ENGLISH_FILE_STEMS:
        score += 15
        reasons.append("+15 اسم ملف أو مجلد لغة إنجليزية أساسي")
        if in_loc_folder and parent_lower in ENGLISH_FILE_STEMS:
            score += 15
            reasons.append("+15 يقع داخل مجلد Localization صريح")
    elif stem_lower in NAME_KEYWORDS or parent_lower in NAME_KEYWORDS or in_loc_folder:
        score += 10
        reasons.append("+10 اسم ملف أو مجلد لغة صريح")

    if base_lang_stem in SIBLING_LANG_KEYWORDS or parent_lower in SIBLING_LANG_KEYWORDS:
        score -= 20
        reasons.append("-20 ملف أو مجلد لغة ثانوية غير الإنجليزية")

    sibling_langs = [s for s in sibling_names if s.split("(")[0].strip() in SIBLING_LANG_KEYWORDS]
    if len(sibling_langs) >= 2:
        score += 10
        reasons.append(f"+10 وجود ملفات لغات شقيقة ({len(sibling_langs)})")

    # 3-5. تقييم المحتوى والمفردات والكثافة والمتغيرات
    content_score, content_reasons, translatable_vals = _score_entries_content(eval_entries)
    score += content_score
    reasons.extend(content_reasons)

    # 6. عقوبة الملفات التقنية (-50)
    if base_lower in CONFIG_PENALTY_NAMES or any(
        tok in base_lower for tok in ("save", "slot", "config", "cache", "manifest")
    ):
        score -= 50
        reasons.append("-50 ملف إعدادات/نظام غير لغوي")

    if contains_arabic:
        if used_clean_bak:
            reasons.append("ملاحظة: معرّب حالياً وتوجد نسخة أصلية نظيفة (.bak)")
        else:
            reasons.append("تنبيه: يحتوي نسبة عربية (قد يكون معرّباً مسبقاً)")

    clamped = max(0, min(100, score))
    return CandidateFile(
        path=file_path,
        relative_path=rel_path,
        format_name=detected_fmt,
        score=clamped,
        reasons=reasons,
        dll_matched=dll_matched,
        contains_arabic=contains_arabic,
        total_entries=len(eval_entries),
        translatable_entries=len(translatable_vals),
    )


PURE_MEDIA_RESOURCE_TYPES = frozenset(
    {
        "UnityEngine.Texture2D",
        "UnityEngine.Sprite",
        "UnityEngine.AudioClip",
        "UnityEngine.Mesh",
        "UnityEngine.Shader",
        "UnityEngine.Material",
        "UnityEngine.AnimationClip",
        "UnityEngine.Video.VideoClip",
        "UnityEngine.Font",
        "UnityEngine.Cubemap",
        "UnityEngine.RenderTexture",
        "UnityEngine.ComputeShader",
        "UnityEngine.TerrainData",
        "UnityEngine.LightingDataAsset",
        "UnityEngine.PhysicMaterial",
        "UnityEngine.PhysicsMaterial2D",
        "UnityEngine.Avatar",
    }
)
TEXT_RESOURCE_TYPE_KEYWORDS = (
    "stringtable",
    "sharedtabledata",
    "textasset",
    "languagesource",
    "localization",
    "yarnproject",
    "dialogue",
)
_CODE_NOISE_SUBSTRINGS = (
    "exception",
    "nullreference",
    "failed to ",
    "cannot ",
    "invalid ",
    "error:",
    "warning:",
    "[debug]",
    "[error]",
    "unityengine",
    "system.",
    "mscorlib",
    "select ",
    "insert ",
    "update ",
    "delete from",
    "http://",
    "https://",
    "::",
    "->",
    "=>",
    "!=",
    "==",
    "&&",
    "||",
    "/*",
    "*/",
    "${",
    "\\x00",
    "stacktrace",
    "argumentoutofrange",
    "notimplemented",
    "missingreference",
    "invalidoperation",
    "hidden/",
    "post fx",
    "shader ",
    "shader not found",
    "rendertexture",
    "texture2d",
    "rigidbody",
    "monobehaviour",
    "scriptableobject",
    "gameobject",
    "is null",
    "cannot be null",
    "constructor",
    "fovkick",
    "this script need",
    "this script requires",
    "virtual axis",
    "virtual button",
    "standalone input",
    "not possible to be called",
    "check your platform",
    "fmod",
    "not found in the ",
    "not allocated",
    "attempting to remove",
    "gvr listener",
    "audio listener",
    "debug view",
    "lookup texture",
    "spectrum lookup",
    "curves texture",
    "log lut",
    "taa history",
    "waypoint target",
    "must be attached",
    "missing component",
    "is not assigned",
)
_CODE_INPUT_BINDING_RE = re.compile(
    r"^(?:joystick(?:\s+\d+)?\s+button\s+\d+|mouse\s+\d+|axis\s+\d+|key\s+\d+)$",
    re.IGNORECASE,
)
_CODE_SLASH_PATH_RE = re.compile(r"[A-Za-z][/\\]|[/\\][A-Za-z]")
_CODE_IDENT_UNDERSCORE_RE = re.compile(r"[A-Za-z0-9]_[A-Za-z0-9]")
_ASSET_LEVEL_RE = re.compile(r"^level(\d+)$")
_ASSET_SHARED_RE = re.compile(r"^sharedassets(\d+)\.assets$")


@dataclass
class AddressablesCatalogInfo:
    """ملخص خريطة Addressables المستخلصة من catalog.json لتوجيه الفحص وتخطي الوسائط."""

    priority_bundles: Set[str] = field(default_factory=set)
    skip_bundles: Set[str] = field(default_factory=set)
    bundle_types: Dict[str, Set[str]] = field(default_factory=dict)


def _stem_tokens(name_or_path: str) -> List[str]:
    norm = name_or_path.replace("\\", "/").lower()
    tokens: List[str] = []
    parts = [p for p in norm.split("/") if p]
    for idx, part in enumerate(parts):
        chunk = os.path.splitext(part)[0] if idx == len(parts) - 1 else part
        for tok in re.split(r"[._\-\s()\[\]]+", chunk):
            if tok:
                tokens.append(tok)
    return tokens


def is_sibling_lang_asset(rel_or_name: str) -> bool:
    """تحديد ما إذا كان ملف الأصول/الحزمة مخصصاً للغة ثانوية غير الإنجليزية (مطابقة كلمات كاملة)."""
    tokens = _stem_tokens(rel_or_name)
    has_en = any(t in ENGLISH_FILE_STEMS for t in tokens)
    has_sibling = any(t in SIBLING_LANG_KEYWORDS for t in tokens)
    return has_sibling and not has_en


def parse_addressables_catalog(engine: EngineInfo) -> AddressablesCatalogInfo:
    """قراءة catalog.json في Addressables لتحديد حزم النصوص وتخطي حزم الصور/الصوت الخالصة."""
    info = AddressablesCatalogInfo()
    candidate_dirs: List[str] = []
    if engine.data_dir:
        candidate_dirs.append(os.path.join(engine.data_dir, "StreamingAssets", "aa"))
    if engine.game_dir:
        candidate_dirs.append(os.path.join(engine.game_dir, "StreamingAssets", "aa"))

    catalog_paths: List[str] = []
    seen_cat: Set[str] = set()
    for aa_dir in candidate_dirs:
        if not os.path.isdir(aa_dir):
            continue
        try:
            for fname in sorted(os.listdir(aa_dir)):
                low = fname.lower()
                if low == "catalog.json" or (low.startswith("catalog_") and low.endswith(".json")):
                    full = os.path.abspath(os.path.join(aa_dir, fname))
                    if full.lower() not in seen_cat and os.path.isfile(full):
                        seen_cat.add(full.lower())
                        catalog_paths.append(full)
        except OSError:
            pass

    for cat_path in catalog_paths:
        try:
            if os.path.getsize(cat_path) > 32 * 1024 * 1024:
                continue
            with open(cat_path, "r", encoding="utf-8-sig", errors="replace") as f:
                cat = json.load(f)
        except Exception:
            continue
        if not isinstance(cat, dict):
            continue

        internal_ids = cat.get("m_InternalIds") or []
        res_types_raw = cat.get("m_resourceTypes") or []
        if not isinstance(internal_ids, list):
            internal_ids = []
        if not isinstance(res_types_raw, list):
            res_types_raw = []

        resource_types: List[str] = [
            str(rt.get("m_ClassName", "")) if isinstance(rt, dict) else ""
            for rt in res_types_raw
        ]

        # 1. تحليل مسارات m_InternalIds المباشرة للعثور على حزم التعريب الصريحة
        for iid in internal_ids:
            if not isinstance(iid, str):
                continue
            low_iid = iid.replace("\\", "/").lower()
            if low_iid.endswith((".bundle", ".unity3d")):
                b_name = os.path.basename(low_iid)
                if any(
                    kw in low_iid
                    for kw in ("local", "lang", "string", "i2", "text", "dialogue", "subtitle")
                ) and not is_sibling_lang_asset(low_iid):
                    info.priority_bundles.add(b_name)

        # 2. فك ترميز m_EntryDataString و m_BucketDataString لربط كل .bundle بأنواع الأصول بداخله
        entry_b64 = cat.get("m_EntryDataString")
        bucket_b64 = cat.get("m_BucketDataString")
        if isinstance(entry_b64, str) and isinstance(bucket_b64, str) and entry_b64 and bucket_b64:
            try:
                entry_bytes = base64.b64decode(entry_b64)
                bucket_bytes = base64.b64decode(bucket_b64)
                if len(entry_bytes) >= 4 and len(bucket_bytes) >= 4:
                    entry_count = struct.unpack_from("<I", entry_bytes, 0)[0]
                    if 4 + entry_count * 28 <= len(entry_bytes):
                        entries = [
                            struct.unpack_from("<7i", entry_bytes, 4 + idx * 28)
                            for idx in range(entry_count)
                        ]
                        bucket_count = struct.unpack_from("<I", bucket_bytes, 0)[0]
                        buckets: List[List[int]] = []
                        bp = 4
                        for _ in range(bucket_count):
                            if bp + 8 > len(bucket_bytes):
                                buckets = []
                                break
                            _data_off, b_len = struct.unpack_from("<ii", bucket_bytes, bp)
                            bp += 8
                            if b_len < 0 or bp + b_len * 4 > len(bucket_bytes):
                                buckets = []
                                break
                            b_entries = list(struct.unpack_from(f"<{b_len}i", bucket_bytes, bp))
                            bp += b_len * 4
                            buckets.append(b_entries)

                        if buckets:
                            for e_row in entries:
                                dep_key_idx = e_row[2]
                                res_type_idx = e_row[6]
                                if not (0 <= dep_key_idx < len(buckets)):
                                    continue
                                if not (0 <= res_type_idx < len(resource_types)):
                                    continue
                                res_cls = resource_types[res_type_idx]
                                if not res_cls or "IAssetBundleResource" in res_cls:
                                    continue
                                for dep_entry_idx in buckets[dep_key_idx]:
                                    if 0 <= dep_entry_idx < len(entries):
                                        b_iid = entries[dep_entry_idx][0]
                                        if 0 <= b_iid < len(internal_ids):
                                            b_path = str(internal_ids[b_iid]).replace("\\", "/")
                                            if b_path.lower().endswith((".bundle", ".unity3d")):
                                                b_name = os.path.basename(b_path).lower()
                                                info.bundle_types.setdefault(b_name, set()).add(res_cls)
            except Exception:
                pass

    for b_name, types in info.bundle_types.items():
        if any(
            any(kw in t.lower() for kw in TEXT_RESOURCE_TYPE_KEYWORDS)
            for t in types
        ):
            if not is_sibling_lang_asset(b_name):
                info.priority_bundles.add(b_name)
        elif types and types.issubset(PURE_MEDIA_RESOURCE_TYPES):
            info.skip_bundles.add(b_name)

    return info


def _is_clean_code_ui_string(text: str) -> bool:
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if len(stripped) < 2:
        return False
    if not is_translatable_value("code", stripped):
        return False
    if _is_assembly_qualified_type_name(stripped):
        return False
    if "\\" in stripped or _CODE_SLASH_PATH_RE.search(stripped):
        return False
    low = stripped.lower()
    if any(noise in low for noise in _CODE_NOISE_SUBSTRINGS):
        return False

    if "\n" in stripped or "\r" in stripped:
        if len(stripped) > 600:
            return False
        if any(tok in stripped for tok in ("{", "}", ";", "()")):
            return False
        lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
        if not (2 <= len(lines) <= 15):
            return False
        multiword_lines = sum(
            1 for ln in lines if len(re.findall(r"[A-Za-z]{2,}", ln)) >= 2
        )
        words = re.findall(r"[A-Za-z]{2,}", stripped)
        if multiword_lines < 2 or len(words) < 6 or not any(c.islower() for c in stripped):
            return False
        underscore_hits = len(_CODE_IDENT_UNDERSCORE_RE.findall(stripped))
        if underscore_hits > 1 or (underscore_hits == 1 and len(words) < 8):
            return False
        return True

    if len(stripped) > 200:
        return False
    if _CODE_INPUT_BINDING_RE.match(stripped) or _CODE_IDENT_UNDERSCORE_RE.search(stripped):
        return False
    bare_low = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", low)
    if low in UI_VOCABULARY or bare_low in UI_VOCABULARY:
        return True
    if " " not in stripped:
        return False
    words = re.findall(r"[A-Za-z]{2,}", stripped)
    if len(words) < 2:
        return False
    return any(c.islower() for c in stripped) or any(w in low for w in UI_VOCABULARY)


def extract_code_ui_strings_from_file(file_path: str) -> List[str]:
    """استخراج السلاسل النصية البشرية الواضحة من Assembly-CSharp.dll أو global-metadata.dat."""
    if not file_path or not os.path.isfile(file_path):
        return []
    if file_path.lower().endswith(".dll"):
        raw_strings = _extract_dotnet_us_strings(file_path)
    else:
        raw_strings = extract_il2cpp_metadata_strings(file_path)

    clean: List[str] = []
    seen: Set[str] = set()
    for s in raw_strings:
        if _is_clean_code_ui_string(s) and s not in seen:
            seen.add(s)
            clean.append(s)
    return clean


def score_code_strings_candidate(engine: EngineInfo) -> Optional[CandidateFile]:
    """الطبقة 4 (الطوارئ): ترشيح نصوص الواجهة المدمجة داخل الكود عند غياب ملفات اللغة والأصول."""
    target_path: Optional[str] = None
    if engine.managed_dir:
        asm_path = os.path.join(engine.managed_dir, "Assembly-CSharp.dll")
        if os.path.isfile(asm_path):
            target_path = asm_path
    if target_path is None and engine.il2cpp_metadata_path and os.path.isfile(engine.il2cpp_metadata_path):
        target_path = engine.il2cpp_metadata_path
    if target_path is None:
        return None

    clean_strings = extract_code_ui_strings_from_file(target_path)
    if len(clean_strings) < 5:
        return None

    eval_entries = [(f"code_{i}", s) for i, s in enumerate(clean_strings)]
    content_score, content_reasons, translatable_vals = _score_entries_content(eval_entries)
    if len(translatable_vals) < 5:
        return None

    score = max(45, min(80, 35 + content_score))
    reasons = [
        f"+35 استخراج نصوص واجهة نظيفة مباشرة من الكود ({os.path.basename(target_path)})",
        *content_reasons,
    ]
    return CandidateFile(
        path=target_path,
        relative_path=os.path.relpath(target_path, engine.game_dir),
        format_name="unity_code",
        score=score,
        reasons=reasons,
        dll_matched=True,
        contains_arabic=False,
        total_entries=len(clean_strings),
        translatable_entries=len(translatable_vals),
        skip_backup=True,
    )


def score_assets_file(
    assets_path: str,
    game_dir: str,
    dll_hints: List[str],
    catalog_info: Optional[AddressablesCatalogInfo] = None,
) -> Optional[CandidateFile]:
    """تقييم ملف .assets يحتوي كتلاً نصية (TextAsset / I2Languages / MonoBehaviour / UGUI) عبر ArabicOverride.json."""
    blocks = extract_assets_localization_blocks(assets_path)
    if not blocks:
        return None

    has_ugui_text = any(blk.block_kind == "UGUI:Text" for blk in blocks)
    has_tmp_text = any(blk.block_kind == "TMP:Text" for blk in blocks)
    has_textmesh = any(
        blk.block_kind == "SceneText:binary" and "_textmesh" in blk.block_name.lower()
        for blk in blocks
    )
    has_verified_ui = has_ugui_text or has_tmp_text or has_textmesh

    eval_entries: List[Tuple[str, str]] = [
        (flat_key, val) for blk in blocks for _, flat_key, val in blk.entries
    ]
    if not eval_entries or (len(eval_entries) < 3 and not has_verified_ui):
        return None

    rel_path = os.path.relpath(assets_path, game_dir)
    base_lower = os.path.basename(assets_path).lower()
    score = 0
    reasons: List[str] = []
    dll_matched = False

    block_names = [blk.block_name for blk in blocks]
    has_text_asset = any(blk.block_kind.startswith("TextAsset:") for blk in blocks)
    has_i2_binary = any(blk.block_kind == "I2Languages:binary" for blk in blocks)
    has_unity_loc = any(blk.block_kind == "UnityLocalization:binary" for blk in blocks)
    has_string_table = any(blk.block_kind == "StringTable:binary" for blk in blocks)
    has_bytes_decoded = any(blk.block_kind.startswith("Bytes:") for blk in blocks)
    has_mono_dialogue = any(blk.block_kind == "MonoBehaviour:dialogue" for blk in blocks)
    has_scene_text = any(blk.block_kind == "SceneText:binary" for blk in blocks)

    matched_hint = _matches_dll_hint(rel_path, dll_hints)
    if not matched_hint:
        for bname in block_names:
            matched_hint = _matches_dll_hint(bname, dll_hints)
            if matched_hint:
                break
    if not matched_hint and (has_i2_binary or has_unity_loc):
        for hint in dll_hints:
            low_h = hint.lower()
            if "i2" in low_h or "local" in low_h:
                matched_hint = hint
                break

    if matched_hint:
        score += 35
        dll_matched = True
        reasons.append(f"+35 تطابق كتلة الأصول مع كود اللعبة ({matched_hint})")
    elif has_unity_loc:
        score += 25
        reasons.append("+25 اكتشاف جدول Unity Localization StringTable ثنائي")
    elif has_i2_binary:
        score += 20
        reasons.append("+20 اكتشاف جدول I2Languages ثنائي داخل الأصول")
    elif has_bytes_decoded:
        score += 25
        reasons.append("+25 فك ضغط/تغليف ملف .bytes بنجاح (GZip/Zlib/Base64)")
    elif has_text_asset:
        score += 30
        reasons.append("+30 اكتشاف كتلة TextAsset مهيكلة قابلة للترجمة")
    elif has_mono_dialogue:
        score += 30
        reasons.append("+30 اكتشاف نصوص حوارات/بيانات في MonoBehaviour أو ScriptableObject")
    elif has_ugui_text:
        score += 35
        reasons.append("+35 اكتشاف نصوص واجهة Unity UGUI (UnityEngine.UI.Text)")
    elif has_tmp_text:
        score += 35
        reasons.append("+35 اكتشاف نصوص واجهة Unity TextMeshPro (TMP_Text)")
    elif has_textmesh:
        score += 35
        reasons.append("+35 اكتشاف نصوص واجهة Unity TextMesh موثّقة")
    elif has_string_table:
        score += 15
        reasons.append("+15 اكتشاف جدول نصوص StringTable ثنائي داخل الأصول")
    elif has_scene_text:
        score += 25
        reasons.append("+25 اكتشاف نصوص واجهة داخل المشاهد/الأصول (SceneText)")

    if catalog_info is not None and base_lower in catalog_info.priority_bundles:
        score += 15
        reasons.append("+15 مدرجة كحزمة تعريب في خريطة Addressables (catalog.json)")

    lower_names = [b.lower() for b in block_names]
    if any(b in ENGLISH_FILE_STEMS for b in lower_names):
        score += 15
        reasons.append("+15 كتلة TextAsset باسم لغة إنجليزية")
    elif any(
        b in NAME_KEYWORDS
        or "i2" in b
        or "local" in b
        or "lang" in b
        or "english" in b
        or "string" in b
        for b in lower_names
    ):
        score += 10
        reasons.append("+10 كتلة أصول باسم تعريب صريح")

    if is_sibling_lang_asset(rel_path):
        score -= 25
        reasons.append("-25 ملف أصول أو حزمة لغة ثانوية غير الإنجليزية")

    content_score, content_reasons, translatable_vals = _score_entries_content(eval_entries)
    if not translatable_vals:
        return None
    score += content_score
    reasons.extend(content_reasons)

    if (
        has_verified_ui
        and not is_sibling_lang_asset(rel_path)
        and base_lower != "globalgamemanagers.assets"
        and (
            _ASSET_LEVEL_RE.match(base_lower)
            or _ASSET_SHARED_RE.match(base_lower)
            or base_lower in ("resources.assets", "data.unity3d")
            or base_lower.endswith((".assets", ".bundle", ".unity3d"))
        )
    ):
        score += 10
        reasons.append(f"+10 نصوص واجهة موثّقة داخل مشهد/أصل لعبة ({len(translatable_vals)})")
    elif has_scene_text and not matched_hint:
        if base_lower == "data.unity3d":
            score += 10
            reasons.append("+10 أرشيف أصول اللعبة الرئيسي (data.unity3d)")
        elif len(translatable_vals) >= 15:
            score += 10
            reasons.append(f"+10 كثافة نصوص واجهة مرتفعة داخل المشهد ({len(translatable_vals)})")
        elif (
            (_ASSET_LEVEL_RE.match(base_lower) or _ASSET_SHARED_RE.match(base_lower))
            and len(translatable_vals) >= 5
        ):
            score += 5
            reasons.append(f"+5 نصوص واجهة واضحة داخل مشهد/أصل ({len(translatable_vals)})")

    kinds_summary = ", ".join(sorted({blk.block_kind for blk in blocks}))
    reasons.append(f"كتل مكتشفة ({len(blocks)}): {kinds_summary}")

    clamped = max(0, min(100, score))
    return CandidateFile(
        path=assets_path,
        relative_path=rel_path,
        format_name="unity_assets",
        score=clamped,
        reasons=reasons,
        dll_matched=dll_matched,
        contains_arabic=False,
        total_entries=len(eval_entries),
        translatable_entries=len(translatable_vals),
        skip_backup=True,
    )


def _asset_priority_key(path: str, catalog_info: Optional[AddressablesCatalogInfo]) -> Tuple[int, int, str]:
    low = os.path.basename(path).lower()
    if catalog_info is not None and low in catalog_info.priority_bundles:
        return (0, 0, low)
    if low == "resources.assets":
        return (1, 0, low)
    if low == "data.unity3d":
        return (1, 1, low)
    if low == "globalgamemanagers.assets":
        return (1, 2, low)
    m_shared = _ASSET_SHARED_RE.match(low)
    if m_shared:
        return (2, int(m_shared.group(1)), low)
    m_lvl = _ASSET_LEVEL_RE.match(low)
    if m_lvl:
        return (3, int(m_lvl.group(1)), low)
    return (4, 0, low)


def _collect_assets_paths(
    engine: EngineInfo, catalog_info: Optional[AddressablesCatalogInfo] = None
) -> List[str]:
    """جمع ملفات الأصول والحزم (level*, sharedassets*, Addressables .bundle, .bytes المضغوطة) مرتبة بالأولوية."""
    roots: List[str] = []
    if engine.data_dir and os.path.isdir(engine.data_dir):
        roots.append(engine.data_dir)
    elif engine.game_dir and os.path.isdir(engine.game_dir):
        roots.append(engine.game_dir)

    seen: Set[str] = set()
    found: List[str] = []
    skip_bundles = catalog_info.skip_bundles if catalog_info is not None else set()
    priority_bundles = catalog_info.priority_bundles if catalog_info is not None else set()

    for base_root in roots:
        for root, dirs, files in os.walk(base_root):
            dirs[:] = sorted(
                d for d in dirs if d.lower() not in EXTERNAL_SKIP_DIR_NAMES
            )
            for fname in sorted(files):
                low = fname.lower()
                if low.endswith((".bak", ".tmp")):
                    continue
                if low in skip_bundles:
                    continue
                is_asset = (
                    low in ("data.unity3d", "resources.assets", "globalgamemanagers.assets")
                    or bool(_ASSET_LEVEL_RE.match(low))
                    or bool(_ASSET_SHARED_RE.match(low))
                    or low.endswith((".unity3d", ".bundle"))
                )
                full = os.path.abspath(os.path.join(root, fname))
                if not is_asset and low.endswith(".bytes"):
                    if detect_candidate_format(full, ".bytes") is None:
                        is_asset = True
                if is_asset and full.lower() not in seen and os.path.isfile(full):
                    if (
                        low.endswith((".bundle", ".unity3d"))
                        and low != "data.unity3d"
                        and low not in priority_bundles
                    ):
                        try:
                            if os.path.getsize(full) > 64 * 1024 * 1024 and not any(
                                kw in low for kw in ("loc", "lang", "string", "text", "ui", "i2")
                            ):
                                continue
                        except OSError:
                            continue
                    seen.add(full.lower())
                    found.append(full)

    found.sort(key=lambda p: _asset_priority_key(p, catalog_info))
    return found


class UnityLanguageScanner:
    """الماسح الذكي رباعي الطبقات لاكتشاف ملفات اللغة في ألعاب Unity بدون موديل."""

    def scan(
        self, engine: EngineInfo, deep_scan: bool = True
    ) -> Tuple[List[CandidateFile], List[str]]:
        dll_hints: List[str] = []
        seen_hints: Set[str] = set()
        for hint in extract_dll_localization_hints(engine.managed_dir) + extract_il2cpp_localization_hints(
            engine.il2cpp_metadata_path, engine.il2cpp_interop_dir
        ):
            if hint not in seen_hints:
                seen_hints.add(hint)
                dll_hints.append(hint)
        candidates: List[CandidateFile] = []
        catalog_info = parse_addressables_catalog(engine)

        for root, dirs, files in os.walk(engine.game_dir):
            # استبعاد مجلدات النظام والنسخ الاحتياطي وManaged لتسريع الفحص
            dirs[:] = sorted(
                [d for d in dirs if d.lower() not in EXTERNAL_SKIP_DIR_NAMES],
                key=lambda d: (0 if d.lower() in PRIORITY_DIR_NAMES else 1, d.lower()),
            )
            sibling_stems = {
                os.path.splitext(f)[0].lower()
                for f in files
                if not f.lower().endswith(".bak")
            }
            for fname in sorted(files):
                low = fname.lower()
                if low in GENERATED_RUNTIME_FILENAMES or low.endswith((".bak", ".tmp")):
                    continue
                if (
                    low == "catalog.json" or (low.startswith("catalog_") and low.endswith(".json")) or low == "settings.json"
                ) and os.path.basename(root).lower() == "aa":
                    continue
                ext = os.path.splitext(low)[1]
                if ext not in CANDIDATE_EXTENSIONS:
                    continue
                full_path = os.path.join(root, fname)
                cand = score_candidate_file(full_path, engine.game_dir, dll_hints, sibling_stems)
                if cand is not None and cand.score > 0:
                    candidates.append(cand)

        has_strong_external = any(c.score >= 65 for c in candidates)
        if deep_scan or not has_strong_external:
            assets_paths = _collect_assets_paths(engine, catalog_info=catalog_info)
            if len(assets_paths) >= 3:
                max_workers = min(4, os.cpu_count() or 2, len(assets_paths))
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                    results = list(
                        pool.map(
                            lambda ap: score_assets_file(
                                ap, engine.game_dir, dll_hints, catalog_info=catalog_info
                            ),
                            assets_paths,
                        )
                    )
                for cand in results:
                    if cand is not None and cand.score > 0:
                        candidates.append(cand)
            else:
                for assets_path in assets_paths:
                    cand = score_assets_file(
                        assets_path, engine.game_dir, dll_hints, catalog_info=catalog_info
                    )
                    if cand is not None and cand.score > 0:
                        candidates.append(cand)

        if deep_scan and (
            not any(c.score >= 45 and c.translatable_entries >= 5 for c in candidates)
            and sum(c.translatable_entries for c in candidates if c.score >= 45) < 3
        ):
            code_cand = score_code_strings_candidate(engine)
            if code_cand is not None and code_cand.score > 0:
                candidates.append(code_cand)

        candidates.sort(
            key=lambda c: (
                2
                if (c.format_name not in ("unity_assets", "unity_code") and c.score >= 45)
                else (1 if (c.format_name == "unity_assets" and c.score >= 45) else 0),
                c.score,
                1 if c.dll_matched else 0,
                c.translatable_entries,
            ),
            reverse=True,
        )
        return candidates, dll_hints
