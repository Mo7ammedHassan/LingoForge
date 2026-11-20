# -*- coding: utf-8 -*-
"""استخراج نصوص التعريب من ملفات أصول Unity المعبأة (.assets) بدون تعديلها."""

import base64
from collections import Counter, OrderedDict
import csv
from dataclasses import dataclass, field
import gzip
import io
import json
import lzma
import math
import os
import re
import struct
import tempfile
import threading
from typing import Dict, Iterable, List, Optional, Set, Tuple
import zlib

try:
    import lz4.block as _C_LZ4_BLOCK  # type: ignore[import-not-found]
except ImportError:
    _C_LZ4_BLOCK = None

from formats.base import PathTuple
from formats.csv_handler import SIBLING_LANG_COLS
from formats.json_handler import clean_jsonc, flatten_json_strings
from formats.registry import get_format_handler


MAX_ASSET_READ_BYTES = 128 * 1024 * 1024
MAX_NAME_LEN = 120
MAX_SCRIPT_LEN = 10 * 1024 * 1024

ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9_.\- ()/]+$")
TERM_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-/ :(){}\[\]'\",!?#&+=]+$")
ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
ARABIC_ANY_RE = re.compile(r"[\u0600-\u06FF\uFE70-\uFEFF]")
INPUT_BINDING_RE = re.compile(r"^<[A-Za-z0-9_]+>/")
CAMEL_CASE_RE = re.compile(r"^[a-z]+[A-Z][A-Za-z0-9]*$")
HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6,8}$")
URL_OR_PATH_RE = re.compile(r"^(?:https?://|file://|mailto:|Assets[/\\]|[A-Za-z]:[/\\])", re.IGNORECASE)

IGNORED_TEXT_ASSET_NAMES = {
    "performancetestruninfo",
    "performancetestrunsettings",
    "packages-lock",
    "package",
    "linebreaking leading characters",
    "linebreaking following characters",
    "default sprite asset",
    "emojione",
}
_MONOSCRIPT_CLS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_`<>.\-]*$")
_ASSM_TYPE_LEFT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.+`\[\]]*$")
_ASSM_TYPE_RIGHT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]+$")

# مطابقة سريعة في مستوى البايت لبداية سلسلة اسم TextAsset محاذاة الطول (1..120)
TEXT_ASSET_SCAN_RE = re.compile(rb"[\x01-\x78]\x00\x00\x00[A-Za-z0-9_.\- ()/]{1,120}")
# مطابقة سريعة لبداية أول مصطلح داخل جدول m_Terms في I2 Localization
I2_TERM_SCAN_RE = re.compile(rb"[\x01-\xc8]\x00\x00\x00[A-Za-z0-9_.\-/ :]{1,64}")

SHADER_OR_CODE_MARKERS = (
    'shader "',
    "subshader",
    "cgprogram",
    "hlslprogram",
    "endcg",
    "endhlsl",
    "pass {",
    "properties {",
    "void main(",
    "using system;",
    "namespace ",
)

ENGLISH_LANG_LABELS = {
    "english",
    "english [en]",
    "en",
    "en-us",
    "en_us",
    "en-gb",
    "en_gb",
}


@dataclass
class AssetLocalizationBlock:
    """كتلة نصوص تعريب مستخرجة من داخل ملف .assets."""

    asset_file: str
    block_name: str
    block_kind: str
    entries: List[Tuple[PathTuple, str, str]]


def _align4(offset: int) -> int:
    """محاذاة الإزاحة لأقرب مضاعف للعدد 4."""
    return (offset + 3) & ~3


def _pack_aligned_string(text: str) -> bytes:
    """حزم سلسلة نصية بصيغة Unity Serialized (uint32 طول + UTF-8 + حشو 4 بايت)."""
    encoded = text.encode("utf-8")
    pad_len = (-len(encoded)) & 3
    return struct.pack("<I", len(encoded)) + encoded + (b"\x00" * pad_len)


def build_unity_text_asset_blob(name: str, script_text: str) -> bytes:
    """بناء تمثيل ثنائي لكتلة Unity TextAsset (m_Name يتبعه m_Script) للاختبار والمحاكاة."""
    return _pack_aligned_string(name) + _pack_aligned_string(script_text)


def build_unity_i2_languages_blob(
    asset_name: str,
    terms: List[Tuple[str, List[str]]],
    languages: Optional[List[str]] = None,
    descriptions: Optional[List[str]] = None,
    term_types: Optional[List[int]] = None,
    pre_terms_ints: Optional[List[int]] = None,
) -> bytes:
    """بناء تمثيل ثنائي لكتلة I2Languages (m_Name + m_Terms + m_Languages اختيارياً)."""
    buf = bytearray()
    buf.extend(_pack_aligned_string(asset_name))
    if pre_terms_ints:
        for val in pre_terms_ints:
            buf.extend(struct.pack("<i", val))
    buf.extend(struct.pack("<I", len(terms)))
    for idx, (term_key, lang_values) in enumerate(terms):
        buf.extend(_pack_aligned_string(term_key))
        ttype = term_types[idx] if term_types and idx < len(term_types) else 0
        buf.extend(struct.pack("<i", ttype))
        if descriptions is not None:
            desc_val = descriptions[idx] if idx < len(descriptions) else ""
            buf.extend(_pack_aligned_string(desc_val))
        buf.extend(struct.pack("<I", len(lang_values)))
        for val in lang_values:
            buf.extend(_pack_aligned_string(val))
        buf.extend(struct.pack("<I", 0))  # Flags[] len = 0
        buf.extend(struct.pack("<I", 0))  # Languages_Touch[] len = 0

    if languages:
        buf.extend(struct.pack("<I", len(languages)))
        for lang_name in languages:
            buf.extend(_pack_aligned_string(lang_name))
            buf.extend(_pack_aligned_string(lang_name[:2].lower()))
            buf.extend(struct.pack("<I", 0))  # LanguageData Flags (byte + align4)
    return bytes(buf)


def _read_aligned_string(
    data: bytes,
    offset: int,
    max_len: int = MAX_SCRIPT_LEN,
    base_align: int = 0,
    endian: str = "<",
) -> Optional[Tuple[str, int]]:
    """قراءة سلسلة Unity مسبوقة بطول 32-بت ومحاذاة 4 بايت."""
    if offset < 0 or offset + 4 > len(data):
        return None
    str_len = struct.unpack_from(endian + "I", data, offset)[0]
    if str_len > max_len or offset + 4 + str_len > len(data):
        return None

    raw = data[offset + 4 : offset + 4 + str_len]
    if b"\x00" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None

    rel_end = (offset - base_align) + 4 + str_len
    aligned_next = base_align + _align4(rel_end)
    if aligned_next > len(data):
        aligned_next = offset + 4 + str_len
    else:
        pad = data[offset + 4 + str_len : aligned_next]
        if any(b != 0 for b in pad):
            return None
    return text, aligned_next


def _read_aligned_bytes(
    data: bytes,
    offset: int,
    max_len: int = MAX_SCRIPT_LEN,
    base_align: int = 0,
    endian: str = "<",
) -> Optional[Tuple[bytes, int]]:
    """قراءة بايتات Unity مسبوقة بطول 32-بت ومحاذاة 4 بايت."""
    if offset < 0 or offset + 4 > len(data):
        return None
    raw_len = struct.unpack_from(endian + "I", data, offset)[0]
    if raw_len == 0 or raw_len > max_len or offset + 4 + raw_len > len(data):
        return None
    raw = data[offset + 4 : offset + 4 + raw_len]
    rel_end = (offset - base_align) + 4 + raw_len
    aligned_next = base_align + _align4(rel_end)
    if aligned_next > len(data):
        aligned_next = offset + 4 + raw_len
    else:
        pad = data[offset + 4 + raw_len : aligned_next]
        if any(b != 0 for b in pad):
            return None
    return raw, aligned_next


def decode_compressed_or_encoded_payload(raw_bytes: bytes, depth: int = 0) -> Optional[str]:
    """فك ضغط/تغليف الحزم القياسية الشائعة (GZip / Zlib / Deflate / Base64) وإرجاع النص المهيكل."""
    if not raw_bytes or depth > 2 or len(raw_bytes) > MAX_SCRIPT_LEN:
        return None

    decompressed: Optional[bytes] = None
    # 1) GZip
    if raw_bytes.startswith(b"\x1f\x8b\x08"):
        try:
            decompressed = gzip.decompress(raw_bytes)
        except Exception:
            decompressed = None
    # 2) Zlib
    elif len(raw_bytes) >= 6 and raw_bytes[0] == 0x78 and raw_bytes[1] in (0x01, 0x5E, 0x9C, 0xDA):
        try:
            decompressed = zlib.decompress(raw_bytes)
        except Exception:
            decompressed = None

    if decompressed is not None and len(decompressed) <= MAX_SCRIPT_LEN:
        try:
            text = decompressed.decode("utf-8-sig")
            if _looks_like_structured_text(text):
                return text
        except UnicodeDecodeError:
            pass
        nested = decode_compressed_or_encoded_payload(decompressed, depth + 1)
        if nested is not None:
            return nested

    # 3) Base64 (قد يغلّف نصاً أو GZip/Zlib)
    stripped_b = raw_bytes.strip()
    if 16 <= len(stripped_b) <= MAX_SCRIPT_LEN and b"\x00" not in stripped_b:
        try:
            ascii_b = re.sub(rb"\s+", b"", stripped_b)
            if len(ascii_b) >= 16 and len(ascii_b) % 4 == 0 and re.fullmatch(rb"[A-Za-z0-9+/=]+", ascii_b):
                decoded = base64.b64decode(ascii_b, validate=True)
                if decoded and len(decoded) <= MAX_SCRIPT_LEN:
                    try:
                        text = decoded.decode("utf-8-sig")
                        if _looks_like_structured_text(text):
                            return text
                    except UnicodeDecodeError:
                        pass
                    nested = decode_compressed_or_encoded_payload(decoded, depth + 1)
                    if nested is not None:
                        return nested
        except Exception:
            pass

    # 4) Raw Deflate كخيار احتياطي أخير إذا أنتج نصاً مهيكلاً نظيفاً
    if decompressed is None and len(raw_bytes) >= 8:
        try:
            raw_def = zlib.decompress(raw_bytes, -zlib.MAX_WBITS)
            if raw_def and len(raw_def) <= MAX_SCRIPT_LEN:
                text = raw_def.decode("utf-8-sig")
                if _looks_like_structured_text(text):
                    return text
        except Exception:
            pass

    return None


def _looks_like_shader_or_code(text: str) -> bool:
    stripped = text.strip()
    if INPUT_BINDING_RE.match(stripped):
        return True
    sample = text[:800].lower()
    return any(marker in sample for marker in SHADER_OR_CODE_MARKERS)


def _looks_like_structured_text(text: str) -> bool:
    stripped = text.lstrip("\ufeff \t\r\n")
    if len(stripped) < 12 or _looks_like_shader_or_code(stripped):
        return False
    if stripped.startswith(("{", "[", "<")):
        return True
    if "msgid" in stripped and "msgstr" in stripped:
        return True
    lines = [ln for ln in stripped.splitlines()[:8] if ln.strip()]
    if len(lines) >= 2:
        if any("=" in ln for ln in lines):
            return True
        if any("," in ln or "\t" in ln or ";" in ln for ln in lines):
            return True
        if sum(1 for ln in lines if re.match(r"^\s*[A-Za-z0-9_.\-'\"]+\s*:(?:\s+.*|\s*)$", ln)) >= 2:
            return True
    return False


def extract_text_assets_from_bytes(data: bytes) -> List[Tuple[str, str]]:
    """مسح ثنائي سريع لاستخراج كتل TextAsset (الاسم، المحتوى) من بيانات ملف .assets."""
    results: List[Tuple[str, str]] = []
    consumed_until = 0

    for match in TEXT_ASSET_SCAN_RE.finditer(data):
        pos = match.start()
        if pos < consumed_until or pos + 8 > len(data):
            continue

        name_res = _read_aligned_string(data, pos, max_len=MAX_NAME_LEN, base_align=pos)
        if name_res is None:
            continue
        name, script_off = name_res
        if not name or not ASSET_NAME_RE.match(name):
            continue
        if name.lower().strip() in IGNORED_TEXT_ASSET_NAMES:
            continue

        script_res = _read_aligned_string(
            data, script_off, max_len=MAX_SCRIPT_LEN, base_align=pos
        )
        if script_res is not None:
            script_text, next_off = script_res
            if len(script_text.strip()) >= 12 and _looks_like_structured_text(script_text):
                results.append((name, script_text))
                consumed_until = next_off
                continue
            decoded_text = decode_compressed_or_encoded_payload(script_text.encode("utf-8", errors="ignore"))
            if decoded_text is not None:
                results.append((name, decoded_text))
                consumed_until = next_off
                continue
        else:
            raw_res = _read_aligned_bytes(
                data, script_off, max_len=MAX_SCRIPT_LEN, base_align=pos
            )
            if raw_res is not None:
                raw_bytes, next_off = raw_res
                decoded_text = decode_compressed_or_encoded_payload(raw_bytes)
                if decoded_text is not None:
                    results.append((name, decoded_text))
                    consumed_until = next_off
                    continue

    return results


def _read_optional_i2_languages_metadata(
    data: bytes, offset: int, expected_count: int, base_align: int
) -> Optional[List[str]]:
    """محاولة قراءة جدول m_Languages بعد m_Terms (مباشرة أو بعد حقول وسيطة مثل CaseInsensitiveTerms)."""
    if expected_count <= 0 or offset + 4 > len(data):
        return None

    scan_limit = min(len(data) - 4, offset + 64)
    for probe_off in range(offset, scan_limit + 1, 4):
        meta_count = struct.unpack_from("<I", data, probe_off)[0]
        if meta_count != expected_count:
            continue

        cur = probe_off + 4
        names: List[str] = []
        valid = True
        for _ in range(meta_count):
            name_res = _read_aligned_string(data, cur, max_len=64, base_align=base_align)
            if name_res is None:
                valid = False
                break
            lang_name, cur = name_res
            code_res = _read_aligned_string(data, cur, max_len=32, base_align=base_align)
            if code_res is None:
                valid = False
                break
            _, cur = code_res
            if cur + 4 > len(data):
                valid = False
                break
            cur += 4  # byte Flags + 3 bytes padding
            names.append(lang_name)

        if valid and len(names) == expected_count:
            return names
    return None


def _pick_english_column_index(
    rows: List[Tuple[str, List[str]]],
    lang_names: Optional[List[str]],
) -> int:
    """اختيار فهرس العمود الإنجليزي من جدول I2Languages."""
    if lang_names:
        for idx, name in enumerate(lang_names):
            norm = name.strip().lower()
            base = norm.split("[")[0].strip()
            if norm in ENGLISH_LANG_LABELS or base in ENGLISH_LANG_LABELS:
                return idx

    max_cols = max((len(vals) for _, vals in rows), default=1)
    if max_cols == 1:
        return 0

    best_idx = 0
    best_score = -1
    for col in range(max_cols):
        ascii_hits = 0
        space_hits = 0
        for _, vals in rows:
            if col >= len(vals):
                continue
            v = vals[col].strip()
            if v and ASCII_LETTER_RE.search(v):
                ascii_hits += 1
                if " " in v:
                    space_hits += 1
        col_score = ascii_hits * 2 + space_hits
        if col_score > best_score:
            best_score = col_score
            best_idx = col
    return best_idx


def _try_parse_i2_terms_with_schema(
    data: bytes, table_offset: int, has_description: bool
) -> Optional[Tuple[List[Tuple[str, str]], int]]:
    """تحليل جدول m_Terms ثنائي متسلسل مع أو بدون حقل Description."""
    if table_offset < 0 or table_offset + 8 > len(data):
        return None
    terms_count = struct.unpack_from("<I", data, table_offset)[0]
    if terms_count < 3 or terms_count > 100000:
        return None

    cur = table_offset + 4
    base_align = table_offset
    parsed_rows: List[Tuple[str, int, List[str]]] = []
    expected_lang_count: Optional[int] = None

    for term_idx in range(terms_count):
        term_res = _read_aligned_string(data, cur, max_len=256, base_align=base_align)
        if term_res is None:
            return None
        term_key, cur = term_res
        if not term_key or len(term_key) > 256 or not TERM_KEY_RE.match(term_key):
            return None

        if cur + 8 > len(data):
            return None
        term_type = struct.unpack_from("<i", data, cur)[0]
        if term_type < 0 or term_type > 25:
            return None
        cur += 4

        if has_description:
            desc_res = _read_aligned_string(data, cur, max_len=4096, base_align=base_align)
            if desc_res is None:
                return None
            _, cur = desc_res
            if cur + 4 > len(data):
                return None

        lang_count = struct.unpack_from("<I", data, cur)[0]
        if lang_count == 0 or lang_count > 64:
            return None
        if expected_lang_count is None:
            expected_lang_count = lang_count
        elif lang_count != expected_lang_count:
            return None
        cur += 4

        lang_vals: List[str] = []
        for _ in range(lang_count):
            val_res = _read_aligned_string(data, cur, max_len=65536, base_align=base_align)
            if val_res is None:
                return None
            val, cur = val_res
            lang_vals.append(val)

        # Flags[] (uint32 len + padded bytes)
        if cur + 4 <= len(data):
            flags_len = struct.unpack_from("<I", data, cur)[0]
            if flags_len <= 128 and cur + 4 + flags_len <= len(data):
                cur = base_align + _align4((cur - base_align) + 4 + flags_len)
            else:
                return None
        else:
            return None

        # معالجة حقل Languages_Touch[] الاختياري في بعض إصدارات I2
        if cur + 4 <= len(data):
            touch_peek = struct.unpack_from("<I", data, cur)[0]
            if touch_peek == 0:
                cur += 4
            elif 1 <= touch_peek <= lang_count:
                probe = cur + 4
                ok_touch = True
                for _ in range(touch_peek):
                    t_res = _read_aligned_string(
                        data, probe, max_len=65536, base_align=base_align
                    )
                    if t_res is None:
                        ok_touch = False
                        break
                    _, probe = t_res
                if ok_touch and (
                    term_idx == terms_count - 1
                    or _read_aligned_string(data, probe, max_len=256, base_align=base_align)
                    is not None
                ):
                    cur = probe

        parsed_rows.append((term_key, term_type, lang_vals))

    lang_count_ref = expected_lang_count or 0
    lang_names = _read_optional_i2_languages_metadata(
        data, cur, lang_count_ref, base_align
    )
    rows_for_col = [(term_key, lang_vals) for term_key, _, lang_vals in parsed_rows]
    en_col = _pick_english_column_index(rows_for_col, lang_names)

    pairs: List[Tuple[str, str]] = []
    for term_key, term_type, lang_vals in parsed_rows:
        if term_type != 0:
            continue
        en_val = lang_vals[en_col] if en_col < len(lang_vals) else ""
        if en_val:
            pairs.append((term_key, en_val))

    if len(pairs) < 3:
        return None
    return pairs, cur


def _try_parse_i2_terms_table(
    data: bytes, table_offset: int
) -> Optional[Tuple[List[Tuple[str, str]], int]]:
    """محاولة تحليل جدول m_Terms ثنائي متسلسل بدءاً من table_offset."""
    res = _try_parse_i2_terms_with_schema(data, table_offset, has_description=False)
    if res is not None:
        return res
    return _try_parse_i2_terms_with_schema(data, table_offset, has_description=True)


def _find_preceding_asset_name(data: bytes, table_offset: int, fallback: str) -> str:
    """البحث للخلف قرب جدول m_Terms عن اسم الأصل الثنائي (مثل I2Languages)."""
    start_search = max(0, table_offset - 128)
    for step in (-4, -1):
        for offset in range(table_offset - 4, start_search - 1, step):
            res = _read_aligned_string(data, offset, max_len=64, base_align=offset)
            if res is None:
                continue
            candidate_name, next_off = res
            if (
                next_off <= table_offset
                and len(candidate_name) >= 2
                and ASSET_NAME_RE.match(candidate_name)
                and ASCII_LETTER_RE.search(candidate_name)
            ):
                return candidate_name
    return fallback


def extract_i2_binary_terms_from_bytes(
    data: bytes,
) -> List[Tuple[str, List[Tuple[str, str]]]]:
    """استخراج جداول I2Languages (m_Terms) الثنائية من بيانات ملف .assets."""
    tables: List[Tuple[str, List[Tuple[str, str]]]] = []
    consumed_until = 0

    for match in I2_TERM_SCAN_RE.finditer(data):
        t0_off = match.start()
        table_off = t0_off - 4
        if table_off < consumed_until:
            continue
        parsed = _try_parse_i2_terms_table(data, table_off)
        if parsed is None:
            continue
        pairs, end_off = parsed
        name = _find_preceding_asset_name(
            data, table_off, f"I2Languages_{len(tables) + 1}" if tables else "I2Languages"
        )
        tables.append((name, pairs))
        consumed_until = max(end_off, t0_off + 4)

    return tables


def _try_parse_serialized_kv_table(
    data: bytes, table_offset: int
) -> Optional[Tuple[List[Tuple[str, str]], int]]:
    """تحليل جدول أزواج (مفتاح، قيمة) متتالية في ScriptableObject / StringTable."""
    if table_offset < 0 or table_offset + 8 > len(data):
        return None
    entries_count = struct.unpack_from("<I", data, table_offset)[0]
    if entries_count < 5 or entries_count > 50000:
        return None

    cur = table_offset + 4
    base_align = table_offset
    pairs: List[Tuple[str, str]] = []
    space_hits = 0

    for _ in range(entries_count):
        key_res = _read_aligned_string(data, cur, max_len=120, base_align=base_align)
        if key_res is None:
            return None
        key, cur = key_res
        if not key or not TERM_KEY_RE.match(key) or not ASCII_LETTER_RE.search(key):
            return None

        val_res = _read_aligned_string(data, cur, max_len=32768, base_align=base_align)
        if val_res is None:
            return None
        val, cur = val_res
        if _looks_like_shader_or_code(val):
            return None
        if val and ASCII_LETTER_RE.search(val):
            pairs.append((key, val))
            if " " in val:
                space_hits += 1

    if len(pairs) < 5 or space_hits < 2:
        return None
    return pairs, cur


def extract_serialized_kv_tables_from_bytes(
    data: bytes,
) -> List[Tuple[str, List[Tuple[str, str]]]]:
    """استخراج جداول النصوص المتتالية (Key/Value) من أصول ScriptableObject/StringTable."""
    tables: List[Tuple[str, List[Tuple[str, str]]]] = []
    consumed_until = 0

    for match in I2_TERM_SCAN_RE.finditer(data):
        t0_off = match.start()
        table_off = t0_off - 4
        if table_off < consumed_until:
            continue
        parsed = _try_parse_serialized_kv_table(data, table_off)
        if parsed is None:
            continue
        pairs, end_off = parsed
        name = _find_preceding_asset_name(
            data, table_off, f"StringTable_{len(tables) + 1}"
        )
        tables.append((name, pairs))
        consumed_until = max(end_off, t0_off + 4)

    return tables


def build_unity_localization_string_table_blob(
    table_name: str, rows: List[Tuple[int, str]]
) -> bytes:
    """بناء كتلة ثنائية مطابقة لتخطيط UnityEngine.Localization.Tables.StringTable (m_TableData) للاختبارات."""
    buf = bytearray()
    buf.extend(_pack_aligned_string(table_name))
    buf.extend(_pack_aligned_string("en"))
    buf.extend(struct.pack("<I", len(rows)))
    for entry_id, localized in rows:
        buf.extend(struct.pack("<q", int(entry_id)))
        buf.extend(_pack_aligned_string(localized))
        buf.extend(struct.pack("<I", 0))  # m_Metadata.m_Items count = 0
    return bytes(buf)


def _try_parse_unity_localization_table(
    data: bytes, table_offset: int
) -> Optional[Tuple[List[Tuple[str, str]], int]]:
    if table_offset < 0 or table_offset + 20 > len(data):
        return None
    count = struct.unpack_from("<I", data, table_offset)[0]
    if count < 3 or count > 50000:
        return None

    cursor = table_offset + 4
    pairs: List[Tuple[str, str]] = []
    for _ in range(count):
        if cursor + 16 > len(data):
            return None
        entry_id = struct.unpack_from("<q", data, cursor)[0]
        if entry_id == 0:
            return None
        cursor += 8
        str_res = _read_aligned_string(data, cursor, max_len=4096, base_align=table_offset)
        if str_res is None:
            return None
        localized, cursor = str_res
        if cursor + 4 > len(data):
            return None
        meta_count = struct.unpack_from("<I", data, cursor)[0]
        if meta_count != 0:
            return None
        cursor += 4

        clean_val = localized.strip()
        if clean_val and not _looks_like_shader_or_code(clean_val) and ASCII_LETTER_RE.search(clean_val):
            pairs.append((f"id_{entry_id}", localized))

    if len(pairs) < 3:
        return None
    return pairs, cursor


UNITY_LOC_SCAN_RE = re.compile(
    rb"(?:[\x03-\xff]\x00|[\x00-\xff][\x01-\x0f])\x00\x00.{8}(?:[\x02-\xff]\x00|[\x00-\xff][\x01-\x0f])\x00\x00[A-Za-z0-9\"'\[{<]",
    re.DOTALL,
)


def extract_unity_localization_string_tables_from_bytes(
    data: bytes,
) -> List[Tuple[str, List[Tuple[str, str]]]]:
    """استخراج جداول Unity Official Localization Package (StringTable m_TableData) الثنائية."""
    tables: List[Tuple[str, List[Tuple[str, str]]]] = []
    consumed_until = 0

    for match in UNITY_LOC_SCAN_RE.finditer(data):
        off = match.start()
        if off < consumed_until:
            continue
        entry_id = struct.unpack_from("<q", data, off + 4)[0]
        if entry_id == 0:
            continue

        parsed = _try_parse_unity_localization_table(data, off)
        if parsed is None:
            continue
        pairs, end_off = parsed
        name = _find_preceding_asset_name(data, off, f"UnityLocTable_{len(tables) + 1}")
        tables.append((name, pairs))
        consumed_until = max(end_off, off + 4)

    return tables


_CSV_HEADER_WORDS = frozenset({
    "key", "keys", "id", "term", "msgid", "rowname", "row_name", "type",
    "english", "en", "en-us", "en_us", "source", "text", "value", "translation",
    "french", "german", "spanish", "russian", "japanese", "chinese", "korean",
    "speaker", "dialogue", "dialog", "description", "name", "title",
})
_CSV_KEY_COL_RE = re.compile(r"^[A-Za-z0-9_.\-:/\[\]]{1,48}$")
_INI_LEFT_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-/:\[\] ]{1,64}$")


def _detect_structured_delimited_format(lines: List[str]) -> Optional[str]:
    """تمييز جداول CSV/TSV البنيوية عن النصوص الحوارية التي تحتوي فواصل طبيعية."""
    sample = [ln for ln in lines[:16] if not ln.startswith(("#", ";", "//", "["))]
    if len(sample) < 2:
        return None

    for delim, fmt in (("\t", "tsv"), (",", "csv"), (";", "csv")):
        rows: List[List[str]] = []
        for ln in sample:
            if delim not in ln:
                rows.append([ln])
                continue
            try:
                parsed = next(csv.reader(io.StringIO(ln), delimiter=delim))
            except Exception:
                parsed = ln.split(delim)
            rows.append(parsed)

        multi_counts = [len(r) for r in rows if len(r) >= 2]
        if len(multi_counts) < 2:
            continue

        modal_cols, modal_hits = Counter(multi_counts).most_common(1)[0]
        min_needed = len(sample) if len(sample) == 2 else max(2, int(len(sample) * 0.6 + 0.999))
        if modal_hits < min_needed:
            continue

        if fmt == "tsv" or modal_cols >= 3:
            return fmt

        multi_rows = [r for r in rows if len(r) == modal_cols]
        first_row_cells = {c.strip().lower() for c in multi_rows[0] if c.strip()}
        if first_row_cells & _CSV_HEADER_WORDS:
            return fmt

        key_like = 0
        for r in multi_rows:
            c0 = r[0].strip()
            c1 = r[1]
            if not c0 or not c1.strip():
                continue
            if _CSV_KEY_COL_RE.match(c0) and not (
                c1.startswith(" ")
                and c1.lstrip()[:1].islower()
                and c0.isalpha()
                and c0[0].isupper()
                and c0[1:].islower()
            ):
                key_like += 1
        if key_like >= max(2, int(len(multi_rows) * 0.7)):
            return fmt

    return None


def _detect_payload_format(text: str) -> Optional[str]:
    """تحديد صيغة النص المستخرج من TextAsset بدقة."""
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped or _looks_like_shader_or_code(stripped):
        return None

    if stripped.startswith(("{", "[")):
        try:
            json.loads(clean_jsonc(text))
            return "json"
        except Exception:
            pass

    if stripped.startswith("<") and ">" in stripped:
        return "xml"

    if "msgid " in stripped and "msgstr" in stripped:
        return "po"

    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None

    # فحص خصائص INI مقارنة بـ CSV/TSV البنيوي
    eq_hits = 0
    for ln in lines[:12]:
        if ln.startswith(("#", ";", "//", "[")):
            continue
        if "=" in ln and not ("," in ln and ln.index(",") < ln.index("=")):
            left = ln.split("=", 1)[0].strip()
            if left and _INI_LEFT_KEY_RE.match(left) and len(left.split()) <= 3:
                eq_hits += 1

    delim_fmt = _detect_structured_delimited_format(lines)
    if eq_hits >= 2:
        return "ini"
    if delim_fmt is not None:
        return delim_fmt

    yaml_hits = sum(
        1
        for ln in lines[:12]
        if not ln.startswith("#")
        and re.match(r"^[A-Za-z0-9_.\-'\"]+\s*:(?:\s+.*|\s*)$", ln)
        and not re.match(r"^https?://", ln, re.IGNORECASE)
    )
    if yaml_hits >= 2 and eq_hits == 0 and delim_fmt is None:
        return "yaml"
    return None


def _load_entries_via_handler(fmt: str, text: str) -> List[Tuple[PathTuple, str, str]]:
    """تحليل النص المستخرج باستخدام محلل الصيغة المعتمد من حزمة formats."""
    if fmt == "json":
        data = json.loads(clean_jsonc(text))
        return flatten_json_strings(data)

    handler = get_format_handler(fmt)
    if handler is None:
        return []

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=f".{fmt}",
            delete=False,
            newline="",
        ) as tf:
            tf.write(text)
            tmp_path = tf.name
        doc = handler.load(tmp_path)
        return doc.entries
    except Exception:
        return []
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def parse_text_payload_entries(
    block_name: str, text: str
) -> Optional[Tuple[str, List[Tuple[PathTuple, str, str]]]]:
    """تحليل محتوى TextAsset وإرجاع (الصيغة، قائمة المدخلات مسبوقة باسم الكتلة)."""
    fmt = _detect_payload_format(text)
    if fmt is None:
        return None

    raw_entries = _load_entries_via_handler(fmt, text)
    if len(raw_entries) < 3:
        return None

    prefixed: List[Tuple[PathTuple, str, str]] = [
        ((block_name,) + path_tuple, f"{block_name}.{flat_key}", val)
        for path_tuple, flat_key, val in raw_entries
    ]
    return fmt, prefixed


def _make_unique_block_name(base_name: str, used_names: Set[str]) -> str:
    if base_name not in used_names:
        used_names.add(base_name)
        return base_name
    idx = 2
    while f"{base_name}_{idx}" in used_names:
        idx += 1
    unique = f"{base_name}_{idx}"
    used_names.add(unique)
    return unique


UNITYFS_MAGIC = b"UnityFS\x00"
_UGUI_CULL_EVENT_SIG = b"UnityEngine.UI.MaskableGraphic+CullStateChangedEvent"
_SCENE_SKIP_PREFIXES = (
    "universal render pipeline/",
    "textmeshpro/",
    "unityengine.",
    "system.",
    "assets/",
    "packages/",
    "fonts & materials/",
    "style sheets/",
    "sprite assets/",
    "color gradient presets/",
)


def _read_cstr(data: bytes, offset: int) -> Optional[Tuple[str, int]]:
    end = data.find(b"\x00", offset)
    if end < 0:
        return None
    return data[offset:end].decode("utf-8", errors="ignore"), end + 1


def lz4_decompress_block(src: bytes, uncompressed_size: int) -> bytes:
    """فك ضغط كتلة LZ4 باستخدام C lz4.block عند توفره مع بديل Python خالص سريع."""
    if uncompressed_size <= 0:
        return b""
    if _C_LZ4_BLOCK is not None:
        try:
            dec = _C_LZ4_BLOCK.decompress(src, uncompressed_size=uncompressed_size)
            if len(dec) == uncompressed_size:
                return dec
        except Exception:
            pass

    dst = bytearray()
    pos = 0
    src_len = len(src)

    while pos < src_len and len(dst) < uncompressed_size:
        token = src[pos]
        pos += 1
        lit_len = token >> 4
        if lit_len == 15:
            while pos < src_len:
                b = src[pos]
                pos += 1
                lit_len += b
                if b != 255:
                    break
        if lit_len > 0:
            dst.extend(src[pos : pos + lit_len])
            pos += lit_len
        if pos >= src_len or len(dst) >= uncompressed_size:
            break

        if pos + 2 > src_len:
            break
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if offset == 0 or offset > len(dst):
            break

        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while pos < src_len:
                b = src[pos]
                pos += 1
                match_len += b
                if b != 255:
                    break

        match_pos = len(dst) - offset
        if offset >= match_len:
            dst.extend(dst[match_pos : match_pos + match_len])
        else:
            chunk = dst[match_pos:]
            reps = (match_len // offset) + 1
            dst.extend((chunk * reps)[:match_len])

    return bytes(dst[:uncompressed_size])


def _lz4_encode_literals_block(raw: bytes) -> bytes:
    """تغليف بيانات ككتلة LZ4 صالحة (Literals) لبناء حزم الاختبار."""
    n = len(raw)
    if n < 15:
        return bytes([n << 4]) + raw
    out = bytearray([0xF0])
    rem = n - 15
    while rem >= 255:
        out.append(255)
        rem -= 255
    out.append(rem)
    out.extend(raw)
    return bytes(out)


def build_unityfs_bundle_blob(
    nodes: List[Tuple[str, bytes]],
    unity_revision: str = "6000.0.0f1",
    compress_lz4: bool = True,
) -> bytes:
    """بناء حزمة UnityFS قياسية (مضغوطة بـ LZ4 اختيارياً) للاختبارات الآلية."""
    raw_stream = bytearray()
    node_entries = bytearray()
    for name, payload in nodes:
        n_off = len(raw_stream)
        raw_stream.extend(payload)
        node_entries.extend(struct.pack(">qqI", n_off, len(payload), 0x4))
        node_entries.extend(name.encode("utf-8") + b"\x00")

    block_u_sz = len(raw_stream)
    if compress_lz4:
        block_comp = _lz4_encode_literals_block(bytes(raw_stream))
        b_flags = 0x42
        h_comp_flag = 0x242
    else:
        block_comp = bytes(raw_stream)
        b_flags = 0x0
        h_comp_flag = 0x240

    info = bytearray(b"\x00" * 16)
    info.extend(struct.pack(">I", 1))
    info.extend(struct.pack(">IIH", block_u_sz, len(block_comp), b_flags))
    info.extend(struct.pack(">I", len(nodes)))
    info.extend(node_entries)

    info_comp = _lz4_encode_literals_block(bytes(info)) if compress_lz4 else bytes(info)

    hdr = bytearray(b"UnityFS\x00")
    hdr.extend(struct.pack(">I", 8))
    hdr.extend(b"5.x.x\x00")
    hdr.extend(unity_revision.encode("utf-8") + b"\x00")
    size_pos = len(hdr)
    hdr.extend(struct.pack(">qIII", 0, len(info_comp), len(info), h_comp_flag))
    while len(hdr) % 16 != 0:
        hdr.append(0)
    hdr.extend(info_comp)
    while len(hdr) % 16 != 0:
        hdr.append(0)
    hdr.extend(block_comp)
    struct.pack_into(">q", hdr, size_pos, len(hdr))
    return bytes(hdr)


def _parse_unityfs_header_info(
    head: bytes, total_size: int
) -> Optional[Tuple[int, int, int, int, int, int]]:
    """تحليل ترويسة UnityFS لتحديد موقع وحجم blocks_info وبداية الكتل المضغوطة."""
    if not head.startswith(UNITYFS_MAGIC) or len(head) < 48:
        return None

    sig_res = _read_cstr(head, 0)
    if sig_res is None:
        return None
    _, pos = sig_res
    if pos + 4 > len(head):
        return None
    version = struct.unpack_from(">I", head, pos)[0]
    pos += 4

    uver_res = _read_cstr(head, pos)
    urev_res = _read_cstr(head, uver_res[1]) if uver_res else None
    if urev_res is None:
        return None
    pos = urev_res[1]
    if pos + 20 > len(head):
        return None

    _, c_info_sz, u_info_sz, flags = struct.unpack_from(">qIII", head, pos)
    pos += 20
    if c_info_sz <= 0 or u_info_sz <= 0 or u_info_sz > MAX_ASSET_READ_BYTES:
        return None

    if flags & 0x80:
        if c_info_sz > total_size:
            return None
        info_offset = total_size - c_info_sz
        blocks_start = ((pos + 15) & ~15) if version >= 7 else pos
    else:
        if version >= 7:
            pos = (pos + 15) & ~15
        if pos + c_info_sz > total_size:
            return None
        info_offset = pos
        pos += c_info_sz
        if flags & 0x200:
            pos = (pos + 15) & ~15
        blocks_start = pos

    return version, c_info_sz, u_info_sz, flags, info_offset, blocks_start


def _decode_unityfs_blocks_and_nodes(
    info_raw: bytes, u_info_sz: int, flags: int, blocks_start: int
) -> Optional[Tuple[List[Tuple[int, int, int, int, int]], List[Tuple[str, int, int]]]]:
    """فك ضغط فهرس UnityFS واستخلاص الكتل والعقد المطلوبة (مع استبعاد .resS والأصول غير النصية)."""
    info_comp = flags & 0x3F
    if info_comp == 0:
        info = info_raw
    elif info_comp in (2, 3):
        info = lz4_decompress_block(info_raw, u_info_sz)
    elif info_comp == 1:
        try:
            info = lzma.decompress(info_raw)
        except lzma.LZMAError:
            return None
    else:
        return None

    if len(info) < 24:
        return None

    ip = 16
    block_cnt = struct.unpack_from(">I", info, ip)[0]
    ip += 4
    if block_cnt > 200000 or ip + block_cnt * 10 + 4 > len(info):
        return None

    blocks: List[Tuple[int, int, int, int, int]] = []
    u_cursor = 0
    c_cursor = blocks_start
    for _ in range(block_cnt):
        u_sz, c_sz, b_flags = struct.unpack_from(">IIH", info, ip)
        ip += 10
        blocks.append((u_cursor, u_sz, c_cursor, c_sz, b_flags))
        u_cursor += u_sz
        c_cursor += c_sz

    node_cnt = struct.unpack_from(">I", info, ip)[0]
    ip += 4
    if node_cnt > 50000:
        return None

    wanted_nodes: List[Tuple[str, int, int]] = []
    for _ in range(node_cnt):
        if ip + 20 > len(info):
            break
        n_off, n_sz, _ = struct.unpack_from(">qqI", info, ip)
        ip += 20
        name_res = _read_cstr(info, ip)
        if name_res is None:
            break
        n_name, ip = name_res
        low_n = n_name.lower()
        if (
            n_sz < 24
            or n_sz > MAX_ASSET_READ_BYTES
            or low_n.endswith((".ress", ".resource"))
            or "unity_builtin_extra" in low_n
        ):
            continue
        wanted_nodes.append((n_name, n_off, n_sz))

    return blocks, wanted_nodes


def _assemble_unityfs_nodes(
    blocks: List[Tuple[int, int, int, int, int]],
    wanted_nodes: List[Tuple[str, int, int]],
    read_block_fn,
) -> List[Tuple[str, bytes]]:
    """تجميع العقد المطلوبة من الكتل الضرورية فقط باستخدام دالة قراءة مرنة (ذاكرة أو ملف)."""
    block_cache: Dict[int, bytes] = {}
    extracted: List[Tuple[str, bytes]] = []
    for n_name, n_off, n_sz in wanted_nodes:
        out = bytearray()
        n_end = n_off + n_sz
        failed = False
        for b_idx, (b_u_off, b_u_sz, b_c_off, b_c_sz, b_flags) in enumerate(blocks):
            b_u_end = b_u_off + b_u_sz
            if b_u_end <= n_off or b_u_off >= n_end:
                continue
            if b_idx not in block_cache:
                raw_blk = read_block_fn(b_c_off, b_c_sz)
                if raw_blk is None or len(raw_blk) != b_c_sz:
                    failed = True
                    break
                comp_t = b_flags & 0x3F
                if comp_t == 0:
                    dec = raw_blk
                elif comp_t in (2, 3):
                    dec = lz4_decompress_block(raw_blk, b_u_sz)
                elif comp_t == 1:
                    try:
                        dec = lzma.decompress(raw_blk)
                    except lzma.LZMAError:
                        failed = True
                        break
                else:
                    failed = True
                    break
                block_cache[b_idx] = dec
            dec_blk = block_cache[b_idx]
            s = max(0, n_off - b_u_off)
            e = min(len(dec_blk), n_end - b_u_off)
            if e > s:
                out.extend(dec_blk[s:e])
        if not failed and len(out) >= 24:
            extracted.append((n_name, bytes(out)))
    return extracted


def extract_unityfs_nodes_from_bytes(data: bytes) -> List[Tuple[str, bytes]]:
    """استخراج عقد الأصول والمشاهد (مثل level0 وresources.assets) من حزمة UnityFS."""
    hdr = _parse_unityfs_header_info(data[:512], len(data))
    if hdr is None:
        return []
    _, c_info_sz, u_info_sz, flags, info_offset, blocks_start = hdr
    if info_offset + c_info_sz > len(data):
        return []
    decoded = _decode_unityfs_blocks_and_nodes(
        data[info_offset : info_offset + c_info_sz], u_info_sz, flags, blocks_start
    )
    if decoded is None:
        return []
    blocks, wanted_nodes = decoded
    if not wanted_nodes:
        return []
    return _assemble_unityfs_nodes(
        blocks,
        wanted_nodes,
        lambda off, sz: data[off : off + sz] if off + sz <= len(data) else None,
    )


def extract_unityfs_nodes_from_file(file_path: str) -> List[Tuple[str, bytes]]:
    """قراءة متدفقة (Streaming) لعقد حزمة UnityFS من القرص بقراءة الكتل المطلوبة فقط."""
    try:
        with open(file_path, "rb") as f:
            total_size = os.fstat(f.fileno()).st_size
            head = f.read(512)
            hdr = _parse_unityfs_header_info(head, total_size)
            if hdr is None:
                return []
            _, c_info_sz, u_info_sz, flags, info_offset, blocks_start = hdr
            f.seek(info_offset)
            info_raw = f.read(c_info_sz)
            if len(info_raw) != c_info_sz:
                return []
            decoded = _decode_unityfs_blocks_and_nodes(
                info_raw, u_info_sz, flags, blocks_start
            )
            if decoded is None:
                return []
            blocks, wanted_nodes = decoded
            if not wanted_nodes:
                return []

            def _read_blk(off: int, sz: int) -> Optional[bytes]:
                if off + sz > total_size:
                    return None
                f.seek(off)
                return f.read(sz)

            return _assemble_unityfs_nodes(blocks, wanted_nodes, _read_blk)
    except OSError:
        return []


def _is_assembly_qualified_type_name(text: str) -> bool:
    """تحديد ما إذا كانت السلسلة اسم نوع برمجي بصيغة AssemblyQualifiedName من UnityEvents."""
    if "," not in text:
        return False
    parts = text.split(",", 1)
    left = parts[0].strip()
    right = parts[1].strip()
    if not left or not right or any(ch.isspace() for ch in left):
        return False
    if not _ASSM_TYPE_LEFT_RE.match(left):
        return False

    low = text.lower()
    if ", version=" in low or ", culture=" in low or ", publickeytoken=" in low:
        return True

    right_first = right.split(",", 1)[0].strip()
    right_low = right_first.lower()
    if right_low.startswith(
        (
            "assembly-csharp",
            "unityengine",
            "unity.",
            "mscorlib",
            "system",
            "netstandard",
            "unity.textmeshpro",
        )
    ) or right_low.endswith(".dll"):
        return True

    if (
        not any(ch.isspace() for ch in right_first)
        and _ASSM_TYPE_RIGHT_RE.match(right_first)
        and ("." in left or "+" in left or "`" in left)
        and ("." in right_first or "-" in right_first)
    ):
        return True
    return False


def _is_printable_ui_text(text: str, allow_multiline: bool = False) -> bool:
    if not text:
        return False
    if not allow_multiline:
        return text.isprintable()
    return all(ch.isprintable() or ch in "\n\r\t" for ch in text)


def _is_valid_scene_ui_text(text: str, require_sentence: bool, max_len: int = 500) -> bool:
    stripped = text.strip()
    if len(stripped) < 2 or len(stripped) > max_len:
        return False
    if not ASCII_LETTER_RE.search(stripped) or ARABIC_ANY_RE.search(stripped):
        return False
    if stripped.endswith((" SDF", "_SDF")):
        return False
    if _looks_like_shader_or_code(stripped):
        return False
    if _is_assembly_qualified_type_name(stripped):
        return False
    if URL_OR_PATH_RE.match(stripped) or HEX_COLOR_RE.match(stripped):
        return False
    low = stripped.lower()
    if low.startswith(_SCENE_SKIP_PREFIXES):
        return False

    if not any(ch.isspace() for ch in stripped):
        if "_" in stripped or "/" in stripped or "\\" in stripped:
            return False
        if "." in stripped.rstrip("."):
            return False
        if CAMEL_CASE_RE.match(stripped):
            return False
        return not require_sentence

    if not require_sentence:
        return True

    words = stripped.split()
    if len(words) >= 4 and any(len(w) >= 2 and w.islower() for w in words):
        return True
    if stripped.endswith((".", "!", "?")) and any(
        len(w) >= 2 and any(c.islower() for c in w) for w in words
    ):
        return True
    return False


def _validate_ugui_font_data(
    obj_bytes: bytes,
    fd_off: int,
    pptr_size: int,
    endian: str = "<",
    sf_version: int = 17,
    min_font_size: int = 0,
) -> Optional[int]:
    """التحقق من بنية m_FontData في UnityEngine.UI.Text وإرجاع إزاحة حقل m_Text."""
    if fd_off < 0 or fd_off + pptr_size > len(obj_bytes):
        return None
    font_file_id = struct.unpack_from(endian + "i", obj_bytes, fd_off)[0]
    if font_file_id < 0 or font_file_id > 100000:
        return None

    fields_off = fd_off + pptr_size
    if fields_off + 48 <= len(obj_bytes):
        (
            font_size,
            font_style,
            best_fit,
            min_size,
            max_size,
            alignment,
            align_by_geo,
            rich_text,
            h_ovf,
            v_ovf,
            line_spacing,
        ) = struct.unpack_from(endian + "iiiiiiiiiif", obj_bytes, fields_off)
        if (
            min_font_size <= font_size <= 2000
            and 0 <= font_style <= 3
            and best_fit in (0, 1)
            and 0 <= min_size <= 2000
            and 0 <= max_size <= 2000
            and 0 <= alignment <= 8
            and align_by_geo in (0, 1)
            and rich_text in (0, 1)
            and h_ovf in (0, 1)
            and v_ovf in (0, 1)
            and math.isfinite(line_spacing)
            and 0.05 <= line_spacing <= 20.0
        ):
            return fields_off + 44

    if sf_version <= 15 and fields_off + 44 <= len(obj_bytes):
        (
            font_size,
            font_style,
            best_fit,
            min_size,
            max_size,
            alignment,
            rich_text,
            h_ovf,
            v_ovf,
            line_spacing,
        ) = struct.unpack_from(endian + "iiiiiiiiif", obj_bytes, fields_off)
        if (
            min_font_size <= font_size <= 2000
            and 0 <= font_style <= 3
            and best_fit in (0, 1)
            and 0 <= min_size <= 2000
            and 0 <= max_size <= 2000
            and 0 <= alignment <= 8
            and rich_text in (0, 1)
            and h_ovf in (0, 1)
            and v_ovf in (0, 1)
            and math.isfinite(line_spacing)
            and 0.05 <= line_spacing <= 20.0
        ):
            return fields_off + 40

    return None


def _extract_ugui_text_from_monobehaviour(
    obj_bytes: bytes,
    sf_version: int = 17,
    endian: str = "<",
    confirmed_ugui_script: bool = False,
) -> Optional[str]:
    """استخراج نص m_Text من كائن UnityEngine.UI.Text (MonoBehaviour ClassID=114)."""
    pptr_size = 12 if sf_version >= 14 else 8
    pos = obj_bytes.find(_UGUI_CULL_EVENT_SIG)
    if pos != -1:
        if pos < 4 or (pos & 3) != 0:
            return None
        sig_len = struct.unpack_from(endian + "I", obj_bytes, pos - 4)[0]
        if not (len(_UGUI_CULL_EVENT_SIG) <= sig_len <= 512) or pos + sig_len > len(obj_bytes):
            return None
        fd_off = _align4(pos + sig_len)
        txt_off = _validate_ugui_font_data(
            obj_bytes, fd_off, pptr_size, endian=endian, sf_version=sf_version
        )
        if txt_off is None:
            return None
        txt_res = _read_aligned_string(obj_bytes, txt_off, max_len=4000, base_align=0, endian=endian)
        if txt_res is None:
            return None
        text, after_off = txt_res
        if after_off != len(obj_bytes):
            return None
        if not _is_printable_ui_text(text, allow_multiline=True):
            return None
        if not _is_valid_scene_ui_text(text, require_sentence=False, max_len=4000):
            return None
        return text

    # المسار الاحتياطي لإصدارات Unity 2020.1+ حيث أُزيل حقل m_TypeName من UnityEventBase
    if sf_version >= 17 and len(obj_bytes) >= pptr_size * 3 + 32:
        enabled = struct.unpack_from(endian + "I", obj_bytes, pptr_size)[0]
        name_len = struct.unpack_from(endian + "I", obj_bytes, pptr_size * 2 + 4)[0]
        if enabled in (0, 1) and name_len == 0:
            after_name = pptr_size * 2 + 8
            mat_file_id = struct.unpack_from(endian + "i", obj_bytes, after_name)[0]
            color_off = after_name + pptr_size
            ray_off = color_off + 16
            if 0 <= mat_file_id <= 100000 and ray_off + 8 <= len(obj_bytes):
                rgba = struct.unpack_from(endian + "4f", obj_bytes, color_off)
                raycast_target = struct.unpack_from(endian + "I", obj_bytes, ray_off)[0]
                if all(math.isfinite(c) and 0.0 <= c <= 10.0 for c in rgba) and raycast_target in (0, 1):
                    pad_base = ray_off + 4
                    candidate_fd_offsets: List[int] = []
                    # 1. مع m_RaycastPadding (4 floats) + m_Maskable + m_Calls==0
                    if pad_base + 24 <= len(obj_bytes):
                        rpad = struct.unpack_from(endian + "4f", obj_bytes, pad_base)
                        maskable = struct.unpack_from(endian + "I", obj_bytes, pad_base + 16)[0]
                        calls = struct.unpack_from(endian + "I", obj_bytes, pad_base + 20)[0]
                        if (
                            all(math.isfinite(v) and -10000.0 <= v <= 10000.0 for v in rpad)
                            and maskable in (0, 1)
                            and calls == 0
                        ):
                            candidate_fd_offsets.append(pad_base + 24)
                    # 2. بدون m_RaycastPadding: m_Maskable + m_Calls==0
                    if pad_base + 8 <= len(obj_bytes):
                        maskable = struct.unpack_from(endian + "I", obj_bytes, pad_base)[0]
                        calls = struct.unpack_from(endian + "I", obj_bytes, pad_base + 4)[0]
                        if maskable in (0, 1) and calls == 0:
                            candidate_fd_offsets.append(pad_base + 8)

                    for cand_fd in candidate_fd_offsets:
                        txt_off = _validate_ugui_font_data(
                            obj_bytes,
                            cand_fd,
                            pptr_size,
                            endian=endian,
                            sf_version=sf_version,
                            min_font_size=1,
                        )
                        if txt_off is None:
                            continue
                        txt_res = _read_aligned_string(
                            obj_bytes, txt_off, max_len=4000, base_align=0, endian=endian
                        )
                        if txt_res is None:
                            continue
                        text, after_off = txt_res
                        if after_off != len(obj_bytes):
                            continue
                        if not _is_printable_ui_text(text, allow_multiline=True):
                            continue
                        if _is_valid_scene_ui_text(text, require_sentence=False, max_len=4000):
                            return text

    # مسار مؤكد عبر MonoScript (ClassID 115) لو تغيرت ترويسة الحقول أو وُجدت مستمعات m_OnCullStateChanged
    if confirmed_ugui_script:
        start_fd = pptr_size * 2 + 8
        end_fd = len(obj_bytes) - (pptr_size + 44)
        for cand_fd in range(_align4(start_fd), end_fd + 1, 4):
            txt_off = _validate_ugui_font_data(
                obj_bytes,
                cand_fd,
                pptr_size,
                endian=endian,
                sf_version=sf_version,
                min_font_size=1,
            )
            if txt_off is None:
                continue
            txt_res = _read_aligned_string(
                obj_bytes, txt_off, max_len=4000, base_align=0, endian=endian
            )
            if txt_res is None:
                continue
            text, after_off = txt_res
            if after_off != len(obj_bytes):
                continue
            if not _is_printable_ui_text(text, allow_multiline=True):
                continue
            if _is_valid_scene_ui_text(text, require_sentence=False, max_len=4000):
                return text

    return None


def _validate_tmp_post_text(
    obj_bytes: bytes,
    after_off: int,
    pptr_size: int,
    endian: str = "<",
    strict: bool = True,
) -> bool:
    """التحقق من الحقول التي تلي m_text مباشرة في TextMeshProUGUI / TextMeshPro."""
    if after_off < 0 or after_off + 8 + 2 * pptr_size > len(obj_bytes):
        return False
    rtl = struct.unpack_from(endian + "I", obj_bytes, after_off)[0]
    if rtl not in (0, 1):
        return False
    font_fid = struct.unpack_from(endian + "i", obj_bytes, after_off + 4)[0]
    mat_fid = struct.unpack_from(endian + "i", obj_bytes, after_off + 4 + pptr_size)[0]
    shared_cnt = struct.unpack_from(endian + "i", obj_bytes, after_off + 4 + 2 * pptr_size)[0]
    if not (0 <= font_fid <= 100000 and 0 <= mat_fid <= 100000 and 0 <= shared_cnt <= 64):
        return False

    p = after_off + 8 + 2 * pptr_size + shared_cnt * pptr_size
    if p + pptr_size + 4 <= len(obj_bytes):
        fmat_fid = struct.unpack_from(endian + "i", obj_bytes, p)[0]
        fmats_cnt = struct.unpack_from(endian + "i", obj_bytes, p + pptr_size)[0]
        if not (0 <= fmat_fid <= 100000 and 0 <= fmats_cnt <= 64):
            return False
        c_off = p + pptr_size + 4 + fmats_cnt * pptr_size
        if c_off + 28 <= len(obj_bytes):
            rgba = struct.unpack_from(endian + "4f", obj_bytes, c_off + 4)
            enable_grad, color_mode = struct.unpack_from(endian + "II", obj_bytes, c_off + 20)
            if (
                all(math.isfinite(c) and 0.0 <= c <= 10.0 for c in rgba)
                and enable_grad in (0, 1)
                and 0 <= color_mode <= 3
            ):
                return True
            if strict:
                return False

    return not strict


def _extract_tmp_text_from_monobehaviour(
    obj_bytes: bytes,
    sf_version: int = 17,
    endian: str = "<",
    confirmed_tmp_script: bool = False,
) -> Optional[str]:
    """استخراج نص m_text من كائن TextMeshProUGUI أو TextMeshPro (MonoBehaviour ClassID=114)."""
    pptr_size = 12 if sf_version >= 14 else 8
    min_len = pptr_size * 4 + 36
    if len(obj_bytes) < min_len:
        return None

    candidates: List[int] = []
    pos = obj_bytes.find(_UGUI_CULL_EVENT_SIG)
    if pos != -1 and pos >= 4 and (pos & 3) == 0:
        sig_len = struct.unpack_from(endian + "I", obj_bytes, pos - 4)[0]
        if len(_UGUI_CULL_EVENT_SIG) <= sig_len <= 512 and pos + sig_len <= len(obj_bytes):
            candidates.append(_align4(pos + sig_len))

    after_name: Optional[int] = None
    enabled = struct.unpack_from(endian + "I", obj_bytes, pptr_size)[0]
    if enabled in (0, 1):
        name_res = _read_aligned_string(
            obj_bytes, pptr_size * 2 + 4, max_len=MAX_NAME_LEN, base_align=0, endian=endian
        )
        if name_res is not None:
            after_name = name_res[1]
            # مسار TextMeshProUGUI: m_Material(PPtr) + m_Color(4f) + m_RaycastTarget
            if after_name + pptr_size + 20 <= len(obj_bytes):
                mat_fid = struct.unpack_from(endian + "i", obj_bytes, after_name)[0]
                rgba = struct.unpack_from(endian + "4f", obj_bytes, after_name + pptr_size)
                ray = struct.unpack_from(endian + "I", obj_bytes, after_name + pptr_size + 16)[0]
                if (
                    0 <= mat_fid <= 100000
                    and all(math.isfinite(c) and 0.0 <= c <= 10.0 for c in rgba)
                    and ray in (0, 1)
                ):
                    pad_base = after_name + pptr_size + 20
                    if pad_base + 24 <= len(obj_bytes):
                        rpad = struct.unpack_from(endian + "4f", obj_bytes, pad_base)
                        maskable = struct.unpack_from(endian + "I", obj_bytes, pad_base + 16)[0]
                        calls = struct.unpack_from(endian + "I", obj_bytes, pad_base + 20)[0]
                        if (
                            all(math.isfinite(v) and -10000.0 <= v <= 10000.0 for v in rpad)
                            and maskable in (0, 1)
                            and calls == 0
                        ):
                            candidates.append(pad_base + 24)
                    if pad_base + 8 <= len(obj_bytes):
                        maskable = struct.unpack_from(endian + "I", obj_bytes, pad_base)[0]
                        calls = struct.unpack_from(endian + "I", obj_bytes, pad_base + 4)[0]
                        if maskable in (0, 1) and calls == 0:
                            candidates.append(pad_base + 8)
                    if pad_base + 4 <= len(obj_bytes):
                        calls = struct.unpack_from(endian + "I", obj_bytes, pad_base)[0]
                        if calls == 0:
                            candidates.append(pad_base + 4)
            # مسار TextMeshPro 3D: m_text مباشرة بعد m_Name في بعض الإصدارات
            if confirmed_tmp_script and after_name < len(obj_bytes):
                candidates.append(after_name)

    tried: Set[int] = set()
    for m_off in candidates:
        if m_off in tried or m_off < 0 or m_off + 8 > len(obj_bytes):
            continue
        tried.add(m_off)
        txt_res = _read_aligned_string(obj_bytes, m_off, max_len=4000, base_align=0, endian=endian)
        if txt_res is None:
            continue
        text, after_off = txt_res
        if not _validate_tmp_post_text(
            obj_bytes, after_off, pptr_size, endian=endian, strict=not confirmed_tmp_script
        ):
            continue
        if not _is_printable_ui_text(text, allow_multiline=True):
            continue
        if _is_valid_scene_ui_text(text, require_sentence=False, max_len=4000):
            return text

    if confirmed_tmp_script and after_name is not None:
        scan_end = min(len(obj_bytes) - 48, after_name + 256)
        for m_off in range(_align4(after_name), scan_end + 1, 4):
            if m_off in tried:
                continue
            txt_res = _read_aligned_string(obj_bytes, m_off, max_len=4000, base_align=0, endian=endian)
            if txt_res is None:
                continue
            text, after_off = txt_res
            if not _validate_tmp_post_text(
                obj_bytes, after_off, pptr_size, endian=endian, strict=True
            ):
                continue
            if not _is_printable_ui_text(text, allow_multiline=True):
                continue
            if _is_valid_scene_ui_text(text, require_sentence=False, max_len=4000):
                return text

    return None


def build_unity_tmp_text_blob(
    text: str,
    sf_version: int = 22,
    endian: str = "<",
    include_raycast_padding: bool = True,
    include_cull_type_name: bool = False,
    script_file_id: int = 1,
    script_path_id: int = 1186,
) -> bytes:
    """بناء تمثيل ثنائي قياسي لكائن TMPro.TextMeshProUGUI (MonoBehaviour ClassID=114) للاختبارات."""
    pptr_fmt = endian + ("iq" if sf_version >= 14 else "ii")
    buf = bytearray()
    buf.extend(struct.pack(pptr_fmt, 0, 10))  # m_GameObject
    buf.extend(struct.pack(endian + "I", 1))  # m_Enabled
    buf.extend(struct.pack(pptr_fmt, script_file_id, script_path_id))  # m_Script
    buf.extend(_pack_aligned_string(""))  # m_Name
    buf.extend(struct.pack(pptr_fmt, 0, 0))  # m_Material
    buf.extend(struct.pack(endian + "4f", 1.0, 1.0, 1.0, 1.0))  # m_Color
    buf.extend(struct.pack(endian + "I", 1))  # m_RaycastTarget
    if include_cull_type_name:
        buf.extend(struct.pack(endian + "I", 0))  # m_Calls
        buf.extend(
            _pack_aligned_string(
                "UnityEngine.UI.MaskableGraphic+CullStateChangedEvent, UnityEngine.UI, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null"
            )
        )
    else:
        if include_raycast_padding:
            buf.extend(struct.pack(endian + "4f", 0.0, 0.0, 0.0, 0.0))  # m_RaycastPadding
        buf.extend(struct.pack(endian + "I", 1))  # m_Maskable
        buf.extend(struct.pack(endian + "I", 0))  # m_Calls
    buf.extend(_pack_aligned_string(text))
    buf.extend(struct.pack(endian + "I", 0))  # m_isRightToLeft
    buf.extend(struct.pack(pptr_fmt, 2, 105))  # m_fontAsset
    buf.extend(struct.pack(pptr_fmt, 2, 3))  # m_sharedMaterial
    buf.extend(struct.pack(endian + "I", 0))  # m_fontSharedMaterials
    buf.extend(struct.pack(pptr_fmt, 0, 0))  # m_fontMaterial
    buf.extend(struct.pack(endian + "I", 0))  # m_fontMaterials
    buf.extend(struct.pack(endian + "I", 0xFFFFFFFF))  # m_fontColor32
    buf.extend(struct.pack(endian + "4f", 1.0, 1.0, 1.0, 1.0))  # m_fontColor
    buf.extend(struct.pack(endian + "II", 0, 3))  # m_enableVertexGradient, m_colorMode
    buf.extend(b"\x00" * 32)
    return bytes(buf)


def build_unity_ugui_text_blob(
    text: str,
    sf_version: int = 17,
    endian: str = "<",
    include_cull_type_name: bool = True,
    font_size: int = 60,
    line_spacing: float = 1.0,
) -> bytes:
    """بناء تمثيل ثنائي قياسي لكائن UnityEngine.UI.Text (MonoBehaviour ClassID=114) للاختبارات."""
    pptr_fmt = endian + ("iq" if sf_version >= 14 else "ii")
    buf = bytearray()
    buf.extend(struct.pack(pptr_fmt, 0, 10))  # m_GameObject
    buf.extend(struct.pack(endian + "I", 1))  # m_Enabled
    buf.extend(struct.pack(pptr_fmt, 1, 314))  # m_Script
    buf.extend(_pack_aligned_string(""))  # m_Name
    buf.extend(struct.pack(pptr_fmt, 0, 0))  # m_Material
    buf.extend(struct.pack(endian + "4f", 1.0, 1.0, 1.0, 1.0))  # m_Color
    buf.extend(struct.pack(endian + "I", 1))  # m_RaycastTarget
    if include_cull_type_name:
        buf.extend(struct.pack(endian + "I", 0))  # m_OnCullStateChanged.m_PersistentCalls.m_Calls
        buf.extend(
            _pack_aligned_string(
                "UnityEngine.UI.MaskableGraphic+CullStateChangedEvent, UnityEngine.UI, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null"
            )
        )
    else:
        buf.extend(struct.pack(endian + "4f", 0.0, 0.0, 0.0, 0.0))  # m_RaycastPadding
        buf.extend(struct.pack(endian + "I", 1))  # m_Maskable
        buf.extend(struct.pack(endian + "I", 0))  # m_Calls
    buf.extend(struct.pack(pptr_fmt, 0, 10102))  # m_FontData.m_Font
    buf.extend(
        struct.pack(
            endian + "iiiiiiiiiif",
            font_size,
            0,  # m_FontStyle
            0,  # m_BestFit
            10,  # m_MinSize
            65,  # m_MaxSize
            4,  # m_Alignment
            0,  # m_AlignByGeometry
            1,  # m_RichText
            1,  # m_HorizontalOverflow
            1,  # m_VerticalOverflow
            line_spacing,
        )
    )
    buf.extend(_pack_aligned_string(text))
    return bytes(buf)


def extract_scene_ui_texts_from_bytes(data: bytes) -> List[Tuple[str, str]]:
    """استخراج نصوص واجهة TMP_Text/UGUI وجمل MonoBehaviour مباشرة من ملفات المشاهد والأصول."""
    if len(data) < 64:
        return []

    results: List[Tuple[str, str]] = []
    seen_texts: Set[str] = set()
    limit = len(data) - 8

    for off in range(0, limit, 4):
        slen = struct.unpack_from("<I", data, off)[0]
        if slen < 2 or slen > 2000 or off + 4 + slen > len(data):
            continue
        pad = (-slen) & 3
        after_off = off + 4 + slen + pad
        if after_off > len(data):
            continue
        if pad and any(b != 0 for b in data[off + 4 + slen : after_off]):
            continue

        raw = data[off + 4 : off + 4 + slen]
        if b"\x00" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        has_multiline = ("\n" in text or "\r" in text or "\t" in text)
        if not _is_printable_ui_text(text, allow_multiline=True):
            continue

        is_tmp_header = False
        if off >= 28 and after_off + 8 <= len(data):
            w = struct.unpack_from("<7I", data, off - 28)
            if w in ((0, 0, 0, 0, 0, 1, 0), (1, 0, 0, 0, 0, 1, 0)):
                rtl, fid = struct.unpack_from("<II", data, after_off)
                if rtl in (0, 1) and fid <= 64:
                    is_tmp_header = True

        is_ugui_header = False
        if not is_tmp_header and off >= 56:
            for cand_pptr in (12, 8):
                fd_off = off - (cand_pptr + 44)
                if fd_off < 0:
                    continue
                if (
                    _validate_ugui_font_data(
                        data,
                        fd_off,
                        cand_pptr,
                        endian="<",
                        sf_version=17 if cand_pptr == 12 else 13,
                    )
                    == off
                ):
                    win_start = max(0, fd_off - 256)
                    sig_rel = data.find(_UGUI_CULL_EVENT_SIG, win_start, fd_off)
                    if sig_rel >= 4:
                        sig_len = struct.unpack_from("<I", data, sig_rel - 4)[0]
                        if (
                            len(_UGUI_CULL_EVENT_SIG) <= sig_len <= 512
                            and _align4(sig_rel + sig_len) == fd_off
                        ):
                            is_ugui_header = True
                            break

        is_verified_ui = is_tmp_header or is_ugui_header
        if has_multiline and not is_verified_ui:
            continue

        if not _is_valid_scene_ui_text(
            text,
            require_sentence=not is_verified_ui,
            max_len=2000 if is_verified_ui else 500,
        ):
            continue

        if text not in seen_texts:
            seen_texts.add(text)
            results.append((f"ui_{len(results)}", text))

    return results


def _extract_blocks_from_blob(
    assets_path: str,
    blob_name: str,
    data: bytes,
    blocks: List[AssetLocalizationBlock],
    used_names: Set[str],
) -> None:
    """استخراج TextAsset وI2Languages وStringTable من كتلة بايتات واحدة."""
    for raw_name, script_text in extract_text_assets_from_bytes(data):
        parsed = parse_text_payload_entries(raw_name, script_text)
        if parsed is None:
            continue
        fmt, entries = parsed
        unique_name = _make_unique_block_name(raw_name, used_names)
        if unique_name != raw_name:
            entries = [
                ((unique_name,) + pt[1:], f"{unique_name}.{fk[len(raw_name) + 1 :]}", val)
                for pt, fk, val in entries
            ]
        blocks.append(
            AssetLocalizationBlock(
                asset_file=assets_path,
                block_name=unique_name,
                block_kind=f"TextAsset:{fmt}",
                entries=entries,
            )
        )

    for raw_name, pairs in extract_i2_binary_terms_from_bytes(data):
        if len(pairs) < 3:
            continue
        unique_name = _make_unique_block_name(raw_name, used_names)
        entries = [
            ((unique_name, idx, term_key), f"{unique_name}.{term_key}", en_val)
            for idx, (term_key, en_val) in enumerate(pairs)
        ]
        blocks.append(
            AssetLocalizationBlock(
                asset_file=assets_path,
                block_name=unique_name,
                block_kind="I2Languages:binary",
                entries=entries,
            )
        )

    for raw_name, pairs in extract_unity_localization_string_tables_from_bytes(data):
        unique_name = _make_unique_block_name(raw_name, used_names)
        entries = [
            ((unique_name, idx, k), f"{unique_name}.{k}", v)
            for idx, (k, v) in enumerate(pairs)
        ]
        blocks.append(
            AssetLocalizationBlock(
                asset_file=assets_path,
                block_name=unique_name,
                block_kind="UnityLocalization:binary",
                entries=entries,
            )
        )

    existing_keys: Set[str] = {
        pt[2]
        for blk in blocks
        if blk.block_kind in ("I2Languages:binary", "UnityLocalization:binary")
        for pt, _, _ in blk.entries
        if len(pt) >= 3 and isinstance(pt[2], str)
    }
    for raw_name, pairs in extract_serialized_kv_tables_from_bytes(data):
        filtered_pairs = [(k, v) for k, v in pairs if k not in existing_keys]
        if len(filtered_pairs) < 5:
            continue
        unique_name = _make_unique_block_name(raw_name, used_names)
        entries = [
            ((unique_name, idx, k), f"{unique_name}.{k}", v)
            for idx, (k, v) in enumerate(filtered_pairs)
        ]
        blocks.append(
            AssetLocalizationBlock(
                asset_file=assets_path,
                block_name=unique_name,
                block_kind="StringTable:binary",
                entries=entries,
            )
        )


SERIALIZED_TEXT_CLASS_IDS = frozenset({49, 102, 114})  # TextAsset, TextMesh, MonoBehaviour
_GUID_RE = re.compile(
    r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
_UNITY_VER_RE = re.compile(r"\d+\.\d+\.\d+")

_ASSETS_CACHE_LOCK = threading.Lock()
_ASSETS_BLOCK_CACHE: "OrderedDict[Tuple[str, int, int], List[AssetLocalizationBlock]]" = OrderedDict()
_MONOSCRIPT_TABLE_CACHE: "OrderedDict[Tuple[str, int, int], Dict[int, Tuple[str, str, str]]]" = OrderedDict()
_ASSETS_CACHE_MAX_ENTRIES = 64


def clear_assets_extraction_cache() -> None:
    """مسح ذاكرة التخزين المؤقت لاستخراج الأصول وجداول MonoScript."""
    with _ASSETS_CACHE_LOCK:
        _ASSETS_BLOCK_CACHE.clear()
        _MONOSCRIPT_TABLE_CACHE.clear()


@dataclass
class SerializedObjectEntry:
    """مدخل كائن واحد داخل جدول كائنات Unity SerializedFile."""

    path_id: int
    byte_start: int
    byte_size: int
    class_id: int
    type_id: int


@dataclass
class SerializedFileIndex:
    """فهرس الترويسة وجدول الكائنات لملف Unity SerializedFile."""

    version: int
    endian: str
    unity_version: str
    data_offset: int
    objects: List[SerializedObjectEntry]
    externals: List[str] = field(default_factory=list)


def _parse_serialized_file_metadata(
    buf: bytes, file_size: int
) -> Optional[SerializedFileIndex]:
    """تحليل ترويسة وجدول كائنات Unity SerializedFile بدقة الإصدارات 9..30."""
    if len(buf) < 24:
        return None
    metadata_size, f_size, version, data_offset = struct.unpack_from(">IIII", buf, 0)
    endian_flag = buf[16]
    if not (9 <= version <= 30) or endian_flag not in (0, 1):
        return None

    if version >= 22:
        if len(buf) < 48:
            return None
        metadata_size = struct.unpack_from(">I", buf, 20)[0]
        f_size, data_offset, _unknown = struct.unpack_from(">qqq", buf, 24)
        pos = 48
    else:
        pos = 20

    if metadata_size < 16 or metadata_size > 32 * 1024 * 1024:
        return None
    if f_size <= 0 or f_size > file_size:
        return None
    if pos + metadata_size > f_size or data_offset < pos + metadata_size or data_offset > f_size:
        return None
    if len(buf) < pos + metadata_size:
        return None

    meta_end = pos + metadata_size
    endian = ">" if endian_flag else "<"

    ver_end = buf.find(b"\x00", pos, min(pos + 65, meta_end))
    if ver_end < 0:
        return None
    try:
        unity_version = buf[pos:ver_end].decode("ascii")
    except UnicodeDecodeError:
        return None
    if not all(32 <= ord(c) < 127 for c in unity_version):
        return None
    pos = ver_end + 1

    if pos + 4 > meta_end:
        return None
    pos += 4  # m_TargetPlatform

    if version >= 13:
        if pos + 1 > meta_end:
            return None
        enable_type_tree = bool(buf[pos])
        pos += 1
    else:
        enable_type_tree = True

    if enable_type_tree and (version < 12 or version >= 23):
        return None

    if pos + 4 > meta_end:
        return None
    type_count = struct.unpack_from(endian + "i", buf, pos)[0]
    pos += 4
    if type_count < 0 or type_count > 20000:
        return None

    type_class_ids: List[int] = []
    for _ in range(type_count):
        if pos + 4 > meta_end:
            return None
        class_id = struct.unpack_from(endian + "i", buf, pos)[0]
        pos += 4
        type_class_ids.append(class_id)

        if version >= 16:
            if pos + 1 > meta_end:
                return None
            pos += 1  # is_stripped_type
        if version >= 17:
            if pos + 2 > meta_end:
                return None
            pos += 2  # script_type_index
        if version >= 13:
            if (version < 16 and class_id < 0) or (version >= 16 and class_id == 114):
                pos += 16  # script_id
            pos += 16  # old_type_hash
            if pos > meta_end:
                return None

        if enable_type_tree:
            if pos + 8 > meta_end:
                return None
            node_count, str_buf_size = struct.unpack_from(endian + "ii", buf, pos)
            pos += 8
            if (
                node_count < 0
                or node_count > 50000
                or str_buf_size < 0
                or str_buf_size > meta_end - pos
            ):
                return None
            node_sz = 32 if version >= 19 else 24
            pos += node_count * node_sz + str_buf_size
            if pos > meta_end:
                return None
            if version >= 21:
                if pos + 4 > meta_end:
                    return None
                dep_count = struct.unpack_from(endian + "i", buf, pos)[0]
                pos += 4
                if dep_count < 0 or dep_count > 100000 or pos + dep_count * 4 > meta_end:
                    return None
                pos += dep_count * 4

    if 7 <= version < 14:
        if pos + 4 > meta_end:
            return None
        big_id_enabled = struct.unpack_from(endian + "i", buf, pos)[0]
        pos += 4
    else:
        big_id_enabled = 0

    if pos + 4 > meta_end:
        return None
    object_count = struct.unpack_from(endian + "i", buf, pos)[0]
    pos += 4
    if object_count < 0 or object_count > 500000:
        return None

    objects: List[SerializedObjectEntry] = []
    for _ in range(object_count):
        if big_id_enabled:
            if pos + 8 > meta_end:
                return None
            path_id = struct.unpack_from(endian + "q", buf, pos)[0]
            pos += 8
        elif version < 14:
            if pos + 4 > meta_end:
                return None
            path_id = struct.unpack_from(endian + "i", buf, pos)[0]
            pos += 4
        else:
            pos = _align4(pos)
            if pos + 8 > meta_end:
                return None
            path_id = struct.unpack_from(endian + "q", buf, pos)[0]
            pos += 8

        if version >= 22:
            if pos + 16 > meta_end:
                return None
            rel_start, byte_size, type_id = struct.unpack_from(endian + "qIi", buf, pos)
            pos += 16
        else:
            if pos + 12 > meta_end:
                return None
            rel_start, byte_size, type_id = struct.unpack_from(endian + "IIi", buf, pos)
            pos += 12

        byte_start = data_offset + rel_start
        if version < 16:
            if pos + 2 > meta_end:
                return None
            class_id = struct.unpack_from(endian + "H", buf, pos)[0]
            pos += 2
        else:
            if type_id < 0 or type_id >= len(type_class_ids):
                return None
            class_id = type_class_ids[type_id]

        if version < 11:
            pos += 2
        if 11 <= version < 17:
            pos += 2
        if version in (15, 16):
            pos += 1
        if pos > meta_end:
            return None
        if rel_start < 0 or byte_start < data_offset or byte_start + byte_size > f_size:
            return None

        objects.append(
            SerializedObjectEntry(
                path_id=path_id,
                byte_start=byte_start,
                byte_size=byte_size,
                class_id=class_id,
                type_id=type_id,
            )
        )

    externals: List[str] = []
    try:
        ext_pos = pos
        ok_ext = True
        if version >= 11:
            if ext_pos + 4 > meta_end:
                ok_ext = False
            else:
                script_count = struct.unpack_from(endian + "i", buf, ext_pos)[0]
                ext_pos += 4
                if script_count < 0 or script_count > 200000:
                    ok_ext = False
                else:
                    for _ in range(script_count):
                        if ext_pos + 4 > meta_end:
                            ok_ext = False
                            break
                        ext_pos += 4
                        if version >= 14:
                            ext_pos = _align4(ext_pos)
                            if ext_pos + 8 > meta_end:
                                ok_ext = False
                                break
                            ext_pos += 8
                        else:
                            if ext_pos + 4 > meta_end:
                                ok_ext = False
                                break
                            ext_pos += 4
        if ok_ext and ext_pos + 4 <= meta_end:
            ext_count = struct.unpack_from(endian + "i", buf, ext_pos)[0]
            ext_pos += 4
            if 0 <= ext_count <= 5000:
                for _ in range(ext_count):
                    if version >= 6:
                        tmp_c = _read_cstr(buf, ext_pos)
                        if tmp_c is None or tmp_c[1] > meta_end:
                            break
                        ext_pos = tmp_c[1]
                    if version >= 5:
                        if ext_pos + 20 > meta_end:
                            break
                        ext_pos += 20
                    path_c = _read_cstr(buf, ext_pos)
                    if path_c is None or path_c[1] > meta_end:
                        break
                    externals.append(path_c[0])
                    ext_pos = path_c[1]
    except Exception:
        pass

    return SerializedFileIndex(
        version=version,
        endian=endian,
        unity_version=unity_version,
        data_offset=data_offset,
        objects=objects,
        externals=externals,
    )


def parse_serialized_file_index_from_bytes(data: bytes) -> Optional[SerializedFileIndex]:
    """تحليل فهرس SerializedFile من بايتات في الذاكرة."""
    return _parse_serialized_file_metadata(data, len(data))


def parse_serialized_file_index_from_file(file_path: str) -> Optional[SerializedFileIndex]:
    """قراءة الترويسة والفهرس فقط (seek/read) من ملف .assets دون تحميل جسم الملف."""
    try:
        with open(file_path, "rb") as f:
            file_size = os.fstat(f.fileno()).st_size
            if file_size < 48:
                return None
            head = f.read(48)
            if head.startswith(UNITYFS_MAGIC):
                return None
            metadata_size, f_size, version, data_offset = struct.unpack_from(">IIII", head, 0)
            endian_flag = head[16]
            if not (9 <= version <= 30) or endian_flag not in (0, 1):
                return None
            hdr_len = 48 if version >= 22 else 20
            if version >= 22:
                metadata_size = struct.unpack_from(">I", head, 20)[0]
                f_size, data_offset, _ = struct.unpack_from(">qqq", head, 24)
            if metadata_size < 16 or metadata_size > 32 * 1024 * 1024:
                return None
            if f_size <= 0 or f_size > file_size:
                return None
            if hdr_len + metadata_size > f_size or data_offset < hdr_len + metadata_size or data_offset > f_size:
                return None
            total_meta_end = hdr_len + metadata_size
            if total_meta_end > len(head):
                rem = f.read(total_meta_end - len(head))
                buf = head + rem
            else:
                buf = head[:total_meta_end]
            return _parse_serialized_file_metadata(buf, file_size)
    except OSError:
        return None


def extract_unity_version_from_file(file_path: str) -> str:
    """استخراج إصدار محرك Unity بدقة من UnityFS أو ترويسة SerializedFile."""
    try:
        with open(file_path, "rb") as f:
            head = f.read(256)
    except OSError:
        return ""
    if len(head) < 24:
        return ""

    if head.startswith(UNITYFS_MAGIC):
        sig_res = _read_cstr(head, 0)
        if sig_res is None:
            return ""
        pos = sig_res[1] + 4
        uver_res = _read_cstr(head, pos) if pos < len(head) else None
        urev_res = _read_cstr(head, uver_res[1]) if uver_res else None
        if urev_res:
            rev = urev_res[0].strip()
            if _UNITY_VER_RE.search(rev):
                return rev
        return ""

    _, _, version, _ = struct.unpack_from(">IIII", head, 0)
    if not (7 <= version <= 30):
        return ""
    hdr_len = 48 if version >= 22 else 20
    if len(head) <= hdr_len:
        return ""
    ver_res = _read_cstr(head, hdr_len)
    if ver_res:
        ver_str = ver_res[0].strip()
        if _UNITY_VER_RE.search(ver_str) and all(32 <= ord(c) < 127 for c in ver_str):
            return ver_str
    return ""


def build_unity_serialized_file_blob(
    objects: List[Tuple[int, int, bytes]],
    unity_version: str = "2022.3.15f1",
    version: int = 22,
    endian: str = "<",
    externals: Optional[List[str]] = None,
) -> bytes:
    """بناء ملف Unity SerializedFile قياسي (v16..v22+) للاختبارات الآلية."""
    unique_classes: List[int] = []
    class_to_type_id: Dict[int, int] = {}
    for _, class_id, _ in objects:
        if class_id not in class_to_type_id:
            class_to_type_id[class_id] = len(unique_classes)
            unique_classes.append(class_id)

    header_size = 48 if version >= 22 else 20
    meta = bytearray()
    meta.extend(unity_version.encode("utf-8") + b"\x00")
    meta.extend(struct.pack(endian + "i", 19))  # StandaloneWindows64
    if version >= 13:
        meta.append(0)  # enable_type_tree = False
    meta.extend(struct.pack(endian + "i", len(unique_classes)))
    for cid in unique_classes:
        meta.extend(struct.pack(endian + "i", cid))
        if version >= 16:
            meta.append(0)  # is_stripped_type
        if version >= 17:
            meta.extend(struct.pack(endian + "h", -1))  # script_type_index
        if version >= 13:
            if (version < 16 and cid < 0) or (version >= 16 and cid == 114):
                meta.extend(b"\x00" * 16)
            meta.extend(b"\x00" * 16)

    if 7 <= version < 14:
        meta.extend(struct.pack(endian + "i", 0))
    meta.extend(struct.pack(endian + "i", len(objects)))

    data_buf = bytearray()
    obj_records: List[Tuple[int, int, int, int, int]] = []
    for path_id, class_id, payload in objects:
        while len(data_buf) % 8 != 0:
            data_buf.append(0)
        rel_off = len(data_buf)
        data_buf.extend(payload)
        obj_records.append((path_id, rel_off, len(payload), class_to_type_id[class_id], class_id))

    for path_id, rel_off, obj_sz, type_id, class_id in obj_records:
        if version >= 14:
            while (header_size + len(meta)) % 4 != 0:
                meta.append(0)
            meta.extend(struct.pack(endian + "q", path_id))
        else:
            meta.extend(struct.pack(endian + "i", path_id))
        if version >= 22:
            meta.extend(struct.pack(endian + "qIi", rel_off, obj_sz, type_id))
        else:
            meta.extend(struct.pack(endian + "IIi", rel_off, obj_sz, type_id))
        if version < 16:
            meta.extend(struct.pack(endian + "H", class_id))
        if version < 11:
            meta.extend(struct.pack(endian + "H", 0))
        if 11 <= version < 17:
            meta.extend(struct.pack(endian + "h", -1))
        if version in (15, 16):
            meta.append(0)

    if version >= 11:
        meta.extend(struct.pack(endian + "i", 0))  # script_count
    ext_list = externals or []
    meta.extend(struct.pack(endian + "i", len(ext_list)))  # externals_count
    for ext_path in ext_list:
        if version >= 6:
            meta.extend(b"\x00")
        if version >= 5:
            meta.extend(b"\x00" * 16)
            meta.extend(struct.pack(endian + "i", 0))
        meta.extend(ext_path.encode("utf-8") + b"\x00")
    if version >= 20:
        meta.extend(struct.pack(endian + "i", 0))  # ref_type_count
    if version >= 5:
        meta.append(0)  # userInformation

    metadata_size = len(meta)
    data_offset = (header_size + metadata_size + 15) & ~15
    pad_len = data_offset - (header_size + metadata_size)
    file_size = data_offset + len(data_buf)
    endian_flag = 1 if endian == ">" else 0

    if version >= 22:
        hdr = struct.pack(">IIIIB3x", 0, 0, version, 0, endian_flag) + struct.pack(
            ">Iqqq", metadata_size, file_size, data_offset, 0
        )
    else:
        hdr = struct.pack(">IIIIB3x", metadata_size, file_size, version, data_offset, endian_flag)

    return hdr + bytes(meta) + (b"\x00" * pad_len) + bytes(data_buf)


def _is_clean_monobehaviour_dialogue_string(text: str, is_scriptable_object: bool) -> bool:
    stripped = text.strip()
    if len(stripped) < 3 or len(stripped) > 1500:
        return False
    if _GUID_RE.match(stripped) or _is_assembly_qualified_type_name(stripped):
        return False
    if not _is_valid_scene_ui_text(stripped, require_sentence=False):
        return False
    if " " not in stripped:
        if not is_scriptable_object:
            return False
        return len(stripped) >= 3 and any(c.islower() for c in stripped)
    words = stripped.split()
    if len(words) < 2:
        return False
    return any(len(w) >= 2 and any(c.islower() for c in w) for w in words)


def _extract_monobehaviour_dialogue_strings(
    obj_bytes: bytes,
    start_offset: int,
    endian: str = "<",
    is_scriptable_object: Optional[bool] = None,
) -> List[str]:
    """استخراج جمل الحوار/النصوص الطبيعية من كائن MonoBehaviour أو ScriptableObject."""
    if start_offset < 0 or start_offset + 8 > len(obj_bytes):
        return []
    if is_scriptable_object is None:
        is_so = len(obj_bytes) >= 12 and struct.unpack_from(endian + "q", obj_bytes, 4)[0] == 0
    else:
        is_so = is_scriptable_object
    results: List[str] = []
    seen: Set[str] = set()
    off = _align4(start_offset)
    limit = len(obj_bytes) - 4

    while off < limit:
        slen = struct.unpack_from(endian + "I", obj_bytes, off)[0]
        if 3 <= slen <= 1500 and off + 4 + slen <= len(obj_bytes):
            pad = (-slen) & 3
            after = off + 4 + slen + pad
            if after <= len(obj_bytes) and (not pad or all(b == 0 for b in obj_bytes[off + 4 + slen : after])):
                raw = obj_bytes[off + 4 : off + 4 + slen]
                if b"\x00" not in raw:
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        text = ""
                    if (
                        text
                        and text.isprintable()
                        and _is_clean_monobehaviour_dialogue_string(text, is_so)
                    ):
                        if text not in seen:
                            seen.add(text)
                            results.append(text)
                        off = after
                        continue
        off += 4

    multi_word_count = sum(1 for txt in results if " " in txt.strip())
    if len(results) >= 3 and multi_word_count >= 2:
        return results
    return []


def parse_monoscript_from_bytes(
    obj_bytes: bytes,
    sf_version: int = 17,
    endian: str = "<",
) -> Optional[Tuple[str, str, str]]:
    """استخراج (m_ClassName, m_Namespace, m_AssemblyName) من كائن MonoScript (ClassID=115)."""
    n_res = _read_aligned_string(obj_bytes, 0, max_len=256, base_align=0, endian=endian)
    if n_res is None:
        return None
    _, after_name = n_res
    if after_name + 8 > len(obj_bytes):
        return None
    base_pos = after_name + 4  # m_ExecutionOrder
    hash_sizes = (16, 4) if sf_version >= 14 else (4, 16)
    for h_sz in hash_sizes:
        pos = base_pos + h_sz
        if pos >= len(obj_bytes):
            continue
        c_res = _read_aligned_string(obj_bytes, pos, max_len=256, base_align=0, endian=endian)
        if c_res is None:
            continue
        cls, pos = c_res
        if not cls or not _MONOSCRIPT_CLS_RE.match(cls):
            continue
        ns_res = _read_aligned_string(obj_bytes, pos, max_len=256, base_align=0, endian=endian)
        if ns_res is None:
            continue
        ns, pos = ns_res
        asm_res = _read_aligned_string(obj_bytes, pos, max_len=256, base_align=0, endian=endian)
        if asm_res is None:
            continue
        asm, end_pos = asm_res
        if 0 <= len(obj_bytes) - end_pos <= 8:
            return cls, ns, asm
    return None


def build_unity_monoscript_blob(
    class_name: str,
    namespace: str = "",
    assembly_name: str = "Assembly-CSharp.dll",
    sf_version: int = 17,
    endian: str = "<",
) -> bytes:
    """بناء تمثيل ثنائي قياسي لكائن MonoScript (ClassID=115) للاختبارات."""
    buf = bytearray()
    buf.extend(_pack_aligned_string(class_name))  # m_Name
    buf.extend(struct.pack(endian + "i", 0))  # m_ExecutionOrder
    buf.extend(b"\x00" * (16 if sf_version >= 14 else 4))  # m_PropertiesHash
    buf.extend(_pack_aligned_string(class_name))  # m_ClassName
    buf.extend(_pack_aligned_string(namespace))  # m_Namespace
    buf.extend(_pack_aligned_string(assembly_name))  # m_AssemblyName
    buf.append(0)  # m_IsEditorScript
    return bytes(buf)


def _extract_monoscript_table_from_blob(
    sf_idx: SerializedFileIndex,
    blob_bytes: bytes,
) -> Dict[int, Tuple[str, str, str]]:
    table: Dict[int, Tuple[str, str, str]] = {}
    for o in sf_idx.objects:
        if o.class_id != 115 or not (8 <= o.byte_size <= 8192):
            continue
        parsed = parse_monoscript_from_bytes(
            blob_bytes[o.byte_start : o.byte_start + o.byte_size],
            sf_version=sf_idx.version,
            endian=sf_idx.endian,
        )
        if parsed is not None:
            table[o.path_id] = parsed
    return table


def _load_monoscript_table_for_file(file_path: str) -> Dict[int, Tuple[str, str, str]]:
    """قراءة وتخزين جدول MonoScript (ClassID=115) لملف أصول خارجي مثل globalgamemanagers.assets."""
    abs_path = os.path.abspath(file_path)
    try:
        st = os.stat(abs_path)
    except OSError:
        return {}
    cache_key = (abs_path, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), st.st_size)
    with _ASSETS_CACHE_LOCK:
        cached = _MONOSCRIPT_TABLE_CACHE.get(cache_key)
        if cached is not None:
            _MONOSCRIPT_TABLE_CACHE.move_to_end(cache_key)
            return cached

    sf_idx = parse_serialized_file_index_from_file(abs_path)
    if sf_idx is None:
        return {}
    wanted_ms = [
        o for o in sf_idx.objects if o.class_id == 115 and 8 <= o.byte_size <= 8192
    ]
    table: Dict[int, Tuple[str, str, str]] = {}
    if wanted_ms:
        try:
            with open(abs_path, "rb") as f:
                for o in wanted_ms:
                    f.seek(o.byte_start)
                    raw = f.read(o.byte_size)
                    if len(raw) == o.byte_size:
                        parsed = parse_monoscript_from_bytes(
                            raw, sf_version=sf_idx.version, endian=sf_idx.endian
                        )
                        if parsed is not None:
                            table[o.path_id] = parsed
        except OSError:
            return {}

    with _ASSETS_CACHE_LOCK:
        _MONOSCRIPT_TABLE_CACHE[cache_key] = table
        _MONOSCRIPT_TABLE_CACHE.move_to_end(cache_key)
        while len(_MONOSCRIPT_TABLE_CACHE) > _ASSETS_CACHE_MAX_ENTRIES:
            _MONOSCRIPT_TABLE_CACHE.popitem(last=False)
    return table


def _resolve_external_asset_path(assets_dir: str, ext_ref: str) -> Optional[str]:
    norm = ext_ref.replace("\\", "/").strip()
    if not norm or norm.lower().startswith("library/"):
        return None
    base = os.path.basename(norm)
    if not base:
        return None
    cand = os.path.join(assets_dir, base)
    if os.path.isfile(cand):
        return cand
    try:
        low_base = base.lower()
        for fname in os.listdir(assets_dir):
            if fname.lower() == low_base:
                full = os.path.join(assets_dir, fname)
                if os.path.isfile(full):
                    return full
    except OSError:
        pass
    return None


def _is_non_text_monoscript(script_info: Optional[Tuple[str, str, str]]) -> bool:
    """تحديد السكربتات التقنية للمحرك التي لا تحمل نصوص تعريب لتجنب الضوضاء الكاذبة."""
    if script_info is None:
        return False
    cls, ns, _ = script_info
    if ns == "TMPro":
        return cls not in ("TextMeshProUGUI", "TextMeshPro", "TMP_Text", "TMP_Dropdown")
    if ns == "UnityEngine.UI":
        return cls in (
            "Image",
            "RawImage",
            "Button",
            "CanvasScaler",
            "GraphicRaycaster",
            "Slider",
            "Toggle",
            "ToggleGroup",
            "Scrollbar",
            "ScrollRect",
            "Mask",
            "RectMask2D",
            "LayoutElement",
            "HorizontalLayoutGroup",
            "VerticalLayoutGroup",
            "GridLayoutGroup",
            "ContentSizeFitter",
            "AspectRatioFitter",
            "Shadow",
            "Outline",
            "PositionAsUV1",
            "Selectable",
            "CanvasRenderer",
        )
    if ns.startswith(
        (
            "UnityEngine.EventSystems",
            "UnityEngine.InputSystem",
            "UnityEngine.Rendering",
            "UnityEngine.Timeline",
            "UnityEngine.Playables",
            "UnityEngine.PostProcessing",
            "UnityEngine.AI",
            "UnityEngine.Tilemaps",
            "UnityEngine.U2D",
            "Cinemachine",
        )
    ):
        return True
    return False


def _resolve_monobehaviour_script(
    obj_bytes: bytes,
    pptr_size: int,
    sf_version: int,
    endian: str,
    assets_dir: str,
    externals: Optional[List[str]],
    local_monoscripts: Optional[Dict[int, Tuple[str, str, str]]],
    bundle_monoscripts: Optional[Dict[str, Dict[int, Tuple[str, str, str]]]],
    ext_table_cache: Dict[int, Dict[int, Tuple[str, str, str]]],
) -> Optional[Tuple[str, str, str]]:
    if len(obj_bytes) < pptr_size * 2 + 4:
        return None
    script_off = pptr_size + 4
    if sf_version >= 14:
        file_id, path_id = struct.unpack_from(endian + "iq", obj_bytes, script_off)
    else:
        file_id, path_id = struct.unpack_from(endian + "ii", obj_bytes, script_off)

    if file_id == 0:
        return local_monoscripts.get(path_id) if local_monoscripts else None
    if not externals or file_id < 1 or file_id > len(externals):
        return None

    tbl = ext_table_cache.get(file_id)
    if tbl is None:
        ext_ref = externals[file_id - 1]
        norm = ext_ref.replace("\\", "/").strip().lower()
        base = os.path.basename(norm)
        if bundle_monoscripts:
            tbl = bundle_monoscripts.get(norm) or bundle_monoscripts.get(base) or {}
        elif assets_dir:
            ext_path = _resolve_external_asset_path(assets_dir, ext_ref)
            tbl = _load_monoscript_table_for_file(ext_path) if ext_path else {}
        else:
            tbl = {}
        ext_table_cache[file_id] = tbl
    return tbl.get(path_id)


def _extract_blocks_from_serialized_objects(
    assets_path: str,
    blob_name: str,
    sf_version: int,
    endian: str,
    obj_slices: Iterable[Tuple[int, int, bytes]],
    blocks: List[AssetLocalizationBlock],
    used_names: Set[str],
    externals: Optional[List[str]] = None,
    local_monoscripts: Optional[Dict[int, Tuple[str, str, str]]] = None,
    bundle_monoscripts: Optional[Dict[str, Dict[int, Tuple[str, str, str]]]] = None,
) -> None:
    """استخراج الكتل النصية بدقة من كائنات TextAsset (49) وTextMesh (102) وMonoBehaviour (114)."""
    base_blob = os.path.splitext(os.path.basename(blob_name))[0] or "SceneUI"
    assets_dir = os.path.dirname(os.path.abspath(assets_path)) if assets_path else ""
    pptr_size = 12 if sf_version >= 14 else 8
    textmesh_pairs: List[Tuple[str, str]] = []
    ugui_pairs: List[Tuple[str, str]] = []
    seen_ugui_texts: Set[str] = set()
    tmp_pairs: List[Tuple[str, str]] = []
    seen_tmp_texts: Set[str] = set()
    fallback_scene_pairs: List[Tuple[str, str]] = []
    seen_fallback_texts: Set[str] = set()
    ext_table_cache: Dict[int, Dict[int, Tuple[str, str, str]]] = {}
    structured_added = False

    for class_id, path_id, obj_bytes in obj_slices:
        if class_id == 49:  # TextAsset
            name_res = _read_aligned_string(obj_bytes, 0, max_len=MAX_NAME_LEN, base_align=0, endian=endian)
            if name_res is None:
                continue
            raw_name, script_off = name_res
            if not raw_name or not ASSET_NAME_RE.match(raw_name):
                continue
            if raw_name.lower().strip() in IGNORED_TEXT_ASSET_NAMES:
                continue

            script_text: Optional[str] = None
            script_res = _read_aligned_string(
                obj_bytes, script_off, max_len=MAX_SCRIPT_LEN, base_align=0, endian=endian
            )
            if script_res is not None:
                candidate_txt = script_res[0]
                if len(candidate_txt.strip()) >= 12 and _looks_like_structured_text(candidate_txt):
                    script_text = candidate_txt
                else:
                    script_text = decode_compressed_or_encoded_payload(
                        candidate_txt.encode("utf-8", errors="ignore")
                    )
            else:
                raw_res = _read_aligned_bytes(
                    obj_bytes, script_off, max_len=MAX_SCRIPT_LEN, base_align=0, endian=endian
                )
                if raw_res is not None:
                    script_text = decode_compressed_or_encoded_payload(raw_res[0])

            if script_text is None:
                continue
            parsed = parse_text_payload_entries(raw_name, script_text)
            if parsed is None:
                continue
            fmt, entries = parsed
            unique_name = _make_unique_block_name(raw_name, used_names)
            if unique_name != raw_name:
                entries = [
                    ((unique_name,) + pt[1:], f"{unique_name}.{fk[len(raw_name) + 1 :]}", val)
                    for pt, fk, val in entries
                ]
            blocks.append(
                AssetLocalizationBlock(
                    asset_file=assets_path,
                    block_name=unique_name,
                    block_kind=f"TextAsset:{fmt}",
                    entries=entries,
                )
            )
            structured_added = True

        elif class_id == 102:  # TextMesh
            txt_res = _read_aligned_string(
                obj_bytes, pptr_size, max_len=2000, base_align=0, endian=endian
            )
            if txt_res is not None:
                raw_tm = txt_res[0]
                if _is_printable_ui_text(raw_tm, allow_multiline=True) and _is_valid_scene_ui_text(
                    raw_tm, require_sentence=False, max_len=2000
                ):
                    textmesh_pairs.append((f"tm_{path_id}", raw_tm))

        elif class_id == 114:  # MonoBehaviour
            script_info = _resolve_monobehaviour_script(
                obj_bytes,
                pptr_size,
                sf_version,
                endian,
                assets_dir,
                externals,
                local_monoscripts,
                bundle_monoscripts,
                ext_table_cache,
            )
            if _is_non_text_monoscript(script_info):
                continue

            is_ugui_script = bool(
                script_info is not None
                and script_info[0] == "Text"
                and script_info[1] == "UnityEngine.UI"
            )
            is_tmp_script = bool(
                script_info is not None
                and script_info[1] == "TMPro"
                and script_info[0] in ("TextMeshProUGUI", "TextMeshPro", "TMP_Text")
            )

            if is_ugui_script or not is_tmp_script:
                ugui_text = _extract_ugui_text_from_monobehaviour(
                    obj_bytes,
                    sf_version=sf_version,
                    endian=endian,
                    confirmed_ugui_script=is_ugui_script,
                )
                if ugui_text is not None:
                    if ugui_text not in seen_ugui_texts:
                        seen_ugui_texts.add(ugui_text)
                        ugui_pairs.append((f"ugui_{path_id}", ugui_text))
                    structured_added = True
                    continue
                if is_ugui_script:
                    continue

            tmp_text = _extract_tmp_text_from_monobehaviour(
                obj_bytes,
                sf_version=sf_version,
                endian=endian,
                confirmed_tmp_script=is_tmp_script,
            )
            if tmp_text is not None:
                if tmp_text not in seen_tmp_texts:
                    seen_tmp_texts.add(tmp_text)
                    tmp_pairs.append((f"tmp_{path_id}", tmp_text))
                structured_added = True
                continue
            if is_tmp_script:
                continue

            matched_structured = False
            for raw_name, pairs in extract_i2_binary_terms_from_bytes(obj_bytes):
                if len(pairs) < 3:
                    continue
                unique_name = _make_unique_block_name(raw_name, used_names)
                entries = [
                    ((unique_name, idx, term_key), f"{unique_name}.{term_key}", en_val)
                    for idx, (term_key, en_val) in enumerate(pairs)
                ]
                blocks.append(
                    AssetLocalizationBlock(
                        asset_file=assets_path,
                        block_name=unique_name,
                        block_kind="I2Languages:binary",
                        entries=entries,
                    )
                )
                matched_structured = True
                structured_added = True

            for raw_name, pairs in extract_unity_localization_string_tables_from_bytes(obj_bytes):
                unique_name = _make_unique_block_name(raw_name, used_names)
                entries = [
                    ((unique_name, idx, k), f"{unique_name}.{k}", v)
                    for idx, (k, v) in enumerate(pairs)
                ]
                blocks.append(
                    AssetLocalizationBlock(
                        asset_file=assets_path,
                        block_name=unique_name,
                        block_kind="UnityLocalization:binary",
                        entries=entries,
                    )
                )
                matched_structured = True
                structured_added = True

            if not matched_structured:
                for raw_name, pairs in extract_serialized_kv_tables_from_bytes(obj_bytes):
                    if len(pairs) < 5:
                        continue
                    unique_name = _make_unique_block_name(raw_name, used_names)
                    entries = [
                        ((unique_name, idx, k), f"{unique_name}.{k}", v)
                        for idx, (k, v) in enumerate(pairs)
                    ]
                    blocks.append(
                        AssetLocalizationBlock(
                            asset_file=assets_path,
                            block_name=unique_name,
                            block_kind="StringTable:binary",
                            entries=entries,
                        )
                    )
                    matched_structured = True
                    structured_added = True

            if not matched_structured:
                name_off = pptr_size + 4 + pptr_size
                mb_name = ""
                after_name = name_off
                name_res = _read_aligned_string(
                    obj_bytes, name_off, max_len=MAX_NAME_LEN, base_align=0, endian=endian
                )
                if name_res is not None:
                    mb_name, after_name = name_res
                if mb_name.lower().strip() in IGNORED_TEXT_ASSET_NAMES:
                    continue

                if sf_version >= 14:
                    is_so = len(obj_bytes) >= 12 and struct.unpack_from(endian + "q", obj_bytes, 4)[0] == 0
                else:
                    is_so = len(obj_bytes) >= 8 and struct.unpack_from(endian + "i", obj_bytes, 4)[0] == 0
                dlg_texts = _extract_monobehaviour_dialogue_strings(
                    obj_bytes, after_name, endian=endian, is_scriptable_object=is_so
                )
                if dlg_texts:
                    block_label = mb_name or f"MonoBehaviour_{path_id}"
                    unique_name = _make_unique_block_name(block_label, used_names)
                    entries = [
                        ((unique_name, idx, f"dlg_{idx}"), f"{unique_name}.dlg_{idx}", val)
                        for idx, val in enumerate(dlg_texts)
                    ]
                    blocks.append(
                        AssetLocalizationBlock(
                            asset_file=assets_path,
                            block_name=unique_name,
                            block_kind="MonoBehaviour:dialogue",
                            entries=entries,
                        )
                    )
                    structured_added = True
                else:
                    for _, ui_txt in extract_scene_ui_texts_from_bytes(obj_bytes):
                        if ui_txt not in seen_fallback_texts:
                            seen_fallback_texts.add(ui_txt)
                            fallback_scene_pairs.append((f"ui_{len(fallback_scene_pairs)}", ui_txt))

    if ugui_pairs:
        unique_name = _make_unique_block_name(f"UGUI_{base_blob}", used_names)
        entries = [
            ((unique_name, idx, key), f"{unique_name}.{key}", val)
            for idx, (key, val) in enumerate(ugui_pairs)
        ]
        blocks.append(
            AssetLocalizationBlock(
                asset_file=assets_path,
                block_name=unique_name,
                block_kind="UGUI:Text",
                entries=entries,
            )
        )

    if tmp_pairs:
        unique_name = _make_unique_block_name(f"TMP_{base_blob}", used_names)
        entries = [
            ((unique_name, idx, key), f"{unique_name}.{key}", val)
            for idx, (key, val) in enumerate(tmp_pairs)
        ]
        blocks.append(
            AssetLocalizationBlock(
                asset_file=assets_path,
                block_name=unique_name,
                block_kind="TMP:Text",
                entries=entries,
            )
        )

    if textmesh_pairs:
        unique_name = _make_unique_block_name(f"SceneText_{base_blob}_TextMesh", used_names)
        entries = [
            ((unique_name, idx, key), f"{unique_name}.{key}", val)
            for idx, (key, val) in enumerate(textmesh_pairs)
        ]
        blocks.append(
            AssetLocalizationBlock(
                asset_file=assets_path,
                block_name=unique_name,
                block_kind="SceneText:binary",
                entries=entries,
            )
        )

    if (
        not structured_added
        and not textmesh_pairs
        and not ugui_pairs
        and not tmp_pairs
        and len(fallback_scene_pairs) >= 3
    ):
        if os.path.basename(blob_name).lower() != "globalgamemanagers":
            unique_name = _make_unique_block_name(f"SceneText_{base_blob}", used_names)
            entries = [
                ((unique_name, idx, key), f"{unique_name}.{key}", val)
                for idx, (key, val) in enumerate(fallback_scene_pairs)
            ]
            blocks.append(
                AssetLocalizationBlock(
                    asset_file=assets_path,
                    block_name=unique_name,
                    block_kind="SceneText:binary",
                    entries=entries,
                )
            )


def _finalize_primary_blocks(blocks: List[AssetLocalizationBlock]) -> List[AssetLocalizationBlock]:
    primary_blocks = [
        blk
        for blk in blocks
        if os.path.splitext(blk.block_name.lower())[0].split("(")[0].strip()
        not in SIBLING_LANG_COLS
    ]
    if primary_blocks:
        return primary_blocks
    return blocks


def extract_assets_localization_blocks(assets_path: str) -> List[AssetLocalizationBlock]:
    """استخراج جميع كتل النصوص الصالحة للتعريب من .assets أو حزم UnityFS مع فهرسة سريعة وتخزين مؤقت."""
    abs_path = os.path.abspath(assets_path)
    try:
        st = os.stat(abs_path)
    except OSError:
        return []

    if st.st_size < 24:
        return []

    cache_key = (abs_path, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), st.st_size)
    with _ASSETS_CACHE_LOCK:
        cached = _ASSETS_BLOCK_CACHE.get(cache_key)
        if cached is not None:
            _ASSETS_BLOCK_CACHE.move_to_end(cache_key)
            return list(cached)

    def _store_cache(result: List[AssetLocalizationBlock]) -> List[AssetLocalizationBlock]:
        with _ASSETS_CACHE_LOCK:
            _ASSETS_BLOCK_CACHE[cache_key] = list(result)
            _ASSETS_BLOCK_CACHE.move_to_end(cache_key)
            while len(_ASSETS_BLOCK_CACHE) > _ASSETS_CACHE_MAX_ENTRIES:
                _ASSETS_BLOCK_CACHE.popitem(last=False)
        return result

    try:
        with open(abs_path, "rb") as f:
            magic_head = f.read(16)
    except OSError:
        return []

    blocks: List[AssetLocalizationBlock] = []
    used_names: Set[str] = set()

    # المسار 1: حزم UnityFS المتدفقة (قراءة الكتل المطلوبة فقط + فحص SerializedFileIndex لكل عقدة)
    if magic_head.startswith(UNITYFS_MAGIC):
        if st.st_size > MAX_ASSET_READ_BYTES * 8:
            return _store_cache([])
        blobs = extract_unityfs_nodes_from_file(abs_path)
        if not blobs:
            return _store_cache([])

        parsed_nodes: List[Tuple[str, bytes, Optional[SerializedFileIndex]]] = []
        bundle_monoscripts: Dict[str, Dict[int, Tuple[str, str, str]]] = {}
        for blob_name, blob_bytes in blobs:
            sf_idx = parse_serialized_file_index_from_bytes(blob_bytes)
            parsed_nodes.append((blob_name, blob_bytes, sf_idx))
            if sf_idx is not None and any(o.class_id == 115 for o in sf_idx.objects):
                ms_tbl = _extract_monoscript_table_from_blob(sf_idx, blob_bytes)
                if ms_tbl:
                    norm_n = blob_name.replace("\\", "/").lower()
                    bundle_monoscripts[norm_n] = ms_tbl
                    bundle_monoscripts[os.path.basename(norm_n)] = ms_tbl

        unindexed_blobs: List[Tuple[str, bytes]] = []
        for blob_name, blob_bytes, sf_idx in parsed_nodes:
            if sf_idx is not None:
                wanted = [
                    o
                    for o in sf_idx.objects
                    if o.class_id in SERIALIZED_TEXT_CLASS_IDS
                    and 8 <= o.byte_size <= 16 * 1024 * 1024
                ]
                if not wanted:
                    continue
                slices = (
                    (o.class_id, o.path_id, blob_bytes[o.byte_start : o.byte_start + o.byte_size])
                    for o in wanted
                )
                local_ms = bundle_monoscripts.get(blob_name.replace("\\", "/").lower())
                _extract_blocks_from_serialized_objects(
                    assets_path,
                    blob_name,
                    sf_idx.version,
                    sf_idx.endian,
                    slices,
                    blocks,
                    used_names,
                    externals=sf_idx.externals,
                    local_monoscripts=local_ms,
                    bundle_monoscripts=bundle_monoscripts,
                )
            else:
                unindexed_blobs.append((blob_name, blob_bytes))
                _extract_blocks_from_blob(assets_path, blob_name, blob_bytes, blocks, used_names)

        if not blocks and unindexed_blobs:
            for blob_name, blob_bytes in unindexed_blobs:
                if os.path.basename(blob_name).lower() == "globalgamemanagers":
                    continue
                ui_pairs = extract_scene_ui_texts_from_bytes(blob_bytes)
                if len(ui_pairs) < 3:
                    continue
                base_scene = os.path.splitext(os.path.basename(blob_name))[0] or "SceneUI"
                unique_name = _make_unique_block_name(f"SceneText_{base_scene}", used_names)
                entries = [
                    ((unique_name, idx, key), f"{unique_name}.{key}", val)
                    for idx, (key, val) in enumerate(ui_pairs)
                ]
                blocks.append(
                    AssetLocalizationBlock(
                        asset_file=assets_path,
                        block_name=unique_name,
                        block_kind="SceneText:binary",
                        entries=entries,
                    )
                )

        return _store_cache(_finalize_primary_blocks(blocks))

    # المسار 2: ملفات .assets و level* المفهرسة (تقرأ الترويسة أولاً وتتخطى الملفات غير النصية فوراً)
    if not abs_path.lower().endswith(".bytes"):
        sf_idx = parse_serialized_file_index_from_file(abs_path)
        if sf_idx is not None:
            wanted = [
                o
                for o in sf_idx.objects
                if o.class_id in SERIALIZED_TEXT_CLASS_IDS
                and 8 <= o.byte_size <= 16 * 1024 * 1024
            ]
            if not wanted:
                return _store_cache([])
            has_mb = any(o.class_id == 114 for o in wanted)
            local_ms_objs = (
                [o for o in sf_idx.objects if o.class_id == 115 and 8 <= o.byte_size <= 8192]
                if has_mb
                else []
            )
            local_monoscripts: Dict[int, Tuple[str, str, str]] = {}
            try:
                with open(abs_path, "rb") as f:
                    obj_slices: List[Tuple[int, int, bytes]] = []
                    for o in wanted:
                        f.seek(o.byte_start)
                        obj_bytes = f.read(o.byte_size)
                        if len(obj_bytes) == o.byte_size:
                            obj_slices.append((o.class_id, o.path_id, obj_bytes))
                    for ms_obj in local_ms_objs:
                        f.seek(ms_obj.byte_start)
                        ms_raw = f.read(ms_obj.byte_size)
                        if len(ms_raw) == ms_obj.byte_size:
                            parsed_ms = parse_monoscript_from_bytes(
                                ms_raw, sf_version=sf_idx.version, endian=sf_idx.endian
                            )
                            if parsed_ms is not None:
                                local_monoscripts[ms_obj.path_id] = parsed_ms
            except OSError:
                return []

            _extract_blocks_from_serialized_objects(
                assets_path,
                os.path.basename(abs_path),
                sf_idx.version,
                sf_idx.endian,
                obj_slices,
                blocks,
                used_names,
                externals=sf_idx.externals,
                local_monoscripts=local_monoscripts,
            )
            return _store_cache(_finalize_primary_blocks(blocks))

    # المسار 3: البديل المرن لملفات .bytes والكتل المختبرية غير المفهرسة
    if st.st_size > MAX_ASSET_READ_BYTES:
        return _store_cache([])
    try:
        with open(abs_path, "rb") as f:
            data = f.read()
    except OSError:
        return []

    if abs_path.lower().endswith(".bytes"):
        decoded_payload = decode_compressed_or_encoded_payload(data)
        if decoded_payload is not None:
            base_name = os.path.splitext(os.path.basename(assets_path))[0] or "CompressedBytes"
            parsed = parse_text_payload_entries(base_name, decoded_payload)
            if parsed is not None:
                fmt, entries = parsed
                return _store_cache(
                    [
                        AssetLocalizationBlock(
                            asset_file=assets_path,
                            block_name=base_name,
                            block_kind=f"Bytes:{fmt}",
                            entries=entries,
                        )
                    ]
                )

    blob_name = os.path.basename(assets_path)
    _extract_blocks_from_blob(assets_path, blob_name, data, blocks, used_names)

    if not blocks and os.path.basename(blob_name).lower() != "globalgamemanagers":
        ui_pairs = extract_scene_ui_texts_from_bytes(data)
        if len(ui_pairs) >= 3:
            base_scene = os.path.splitext(os.path.basename(blob_name))[0] or "SceneUI"
            unique_name = _make_unique_block_name(f"SceneText_{base_scene}", used_names)
            entries = [
                ((unique_name, idx, key), f"{unique_name}.{key}", val)
                for idx, (key, val) in enumerate(ui_pairs)
            ]
            blocks.append(
                AssetLocalizationBlock(
                    asset_file=assets_path,
                    block_name=unique_name,
                    block_kind="SceneText:binary",
                    entries=entries,
                )
            )

    return _store_cache(_finalize_primary_blocks(blocks))
