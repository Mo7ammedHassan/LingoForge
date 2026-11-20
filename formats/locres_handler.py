# -*- coding: utf-8 -*-
"""محلل وكاتب ملفات Unreal Engine Localization Resource (.locres) بمختلف إصداراتها."""

from dataclasses import dataclass, field
import os
import struct
from typing import Dict, List, Optional, Sequence, Tuple
import zlib

from formats.base import FormatHandler, ParsedDocument, PathTuple


LOCRES_MAGIC = b"\x0e\x14\x74\x75\x67\x4a\x03\xfc\x4a\x15\x90\x9d\xc3\x37\x7f\x1b"
LOCMETA_MAGIC = b"\x4f\xee\x4c\xa1\x68\x48\x55\x83\x6c\x4c\x46\xbd\x70\xd2\x6c\x7c"

VERSION_LEGACY = 0
VERSION_COMPACT = 1
VERSION_OPTIMIZED = 2
VERSION_OPTIMIZED_CITYHASH64 = 3


@dataclass
class LocmetaData:
    """البيانات الوصفية المستخرجة من ملف Unreal .locmeta (الثقافة الأم واللغات المجمّعة)."""

    version: int
    native_culture: str
    native_locres: str
    compiled_cultures: List[str] = field(default_factory=list)


def parse_locmeta_bytes(data: bytes) -> LocmetaData:
    """تحليل بايتات ملف .locmeta لمعرفة اللغة الأم (NativeCulture) واللغات المتاحة."""
    if len(data) < 4:
        raise ValueError("ملف .locmeta قصير جداً أو تالف.")

    if len(data) >= 17 and data[:16] == LOCMETA_MAGIC:
        version = data[16]
        pos = 17
        native_culture, pos = read_fstring(data, pos)
        native_locres, pos = read_fstring(data, pos)
        compiled_cultures: List[str] = []
        if version >= 1 and pos + 4 <= len(data):
            cnt = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            for _ in range(min(cnt, 512)):
                if pos + 4 > len(data):
                    break
                c_val, pos = read_fstring(data, pos)
                if c_val:
                    compiled_cultures.append(c_val)
        return LocmetaData(
            version=version,
            native_culture=native_culture,
            native_locres=native_locres,
            compiled_cultures=compiled_cultures,
        )

    pos = 0
    native_culture, pos = read_fstring(data, pos)
    native_locres, pos = read_fstring(data, pos)
    return LocmetaData(
        version=0,
        native_culture=native_culture,
        native_locres=native_locres,
        compiled_cultures=[native_culture] if native_culture else [],
    )


def read_fstring(data: bytes, offset: int) -> Tuple[str, int]:
    """قراءة سلسلة FString الثنائية (ASCII/UTF-8 أو UTF-16-LE) وإرجاع (النص, الإزاحة التالية)."""
    if offset + 4 > len(data):
        raise ValueError(f"تجاوز حدود البيانات أثناء قراءة طول FString عند {offset}")
    length = struct.unpack_from("<i", data, offset)[0]
    offset += 4
    if length == 0:
        return "", offset
    if length > 0:
        end = offset + length
        if end > len(data):
            raise ValueError(f"تجاوز حدود البيانات أثناء قراءة FString ANSI بطول {length}")
        raw = data[offset:end].rstrip(b"\x00")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")
        return text, end

    char_count = -length
    byte_len = char_count * 2
    end = offset + byte_len
    if end > len(data):
        raise ValueError(f"تجاوز حدود البيانات أثناء قراءة FString UTF-16 بطول {char_count}")
    raw = data[offset:end]
    text = raw.decode("utf-16-le", errors="replace").rstrip("\x00")
    return text, end


def write_fstring(text: str) -> bytes:
    """كتابة سلسلة بصيغة FString الثنائية القياسية في Unreal Engine."""
    if not text:
        return struct.pack("<i", 0)
    if all(ord(ch) < 128 for ch in text):
        raw = text.encode("ascii") + b"\x00"
        return struct.pack("<i", len(raw)) + raw
    raw = text.encode("utf-16-le") + b"\x00\x00"
    return struct.pack("<i", -(len(raw) // 2)) + raw


@dataclass
class LocresKeyEntry:
    """مدخل نصي واحد داخل نطاق Namespace في ملف .locres."""

    key: str
    text: str
    source_hash: int = 0
    key_hash: int = 0
    string_index: int = 0


@dataclass
class LocresNamespace:
    """نطاق Namespace يضم مفاتيح الترجمة في ملف .locres."""

    name: str
    name_hash: int = 0
    entries: List[LocresKeyEntry] = field(default_factory=list)


@dataclass
class LocresData:
    """التمثيل الكامل لملف .locres مع حفظ الإصدار والهاشات الأصلية."""

    version: int
    namespaces: List[LocresNamespace] = field(default_factory=list)


def _str_crc32(text: str) -> int:
    return zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF


def parse_locres_bytes(data: bytes) -> LocresData:
    """تحليل بايتات ملف .locres (Legacy v0 / Compact v1 / Optimized v2 / CityHash64 v3)."""
    if len(data) < 4:
        raise ValueError("ملف .locres قصير جداً أو تالف.")

    if len(data) >= 25 and data[:16] == LOCRES_MAGIC:
        version = data[16]
        if version not in (VERSION_COMPACT, VERSION_OPTIMIZED, VERSION_OPTIMIZED_CITYHASH64):
            raise ValueError(f"إصدار .locres غير مدعوم: {version}")
        string_array_offset = struct.unpack_from("<q", data, 17)[0]
        localized_strings: List[str] = []
        if string_array_offset >= 0:
            if string_array_offset + 4 > len(data):
                raise ValueError("إزاحة جدول السلاسل في .locres خارج الحدود.")
            s_pos = string_array_offset
            str_count = struct.unpack_from("<I", data, s_pos)[0]
            s_pos += 4
            for _ in range(str_count):
                s_val, s_pos = read_fstring(data, s_pos)
                if version >= VERSION_OPTIMIZED:
                    if s_pos + 4 > len(data):
                        raise ValueError("تجاوز الحدود عند قراءة RefCount في .locres.")
                    s_pos += 4
                localized_strings.append(s_val)

        pos = 25
        if version >= VERSION_OPTIMIZED:
            if pos + 4 > len(data):
                raise ValueError("بيانات EntriesCount مفقودة في .locres.")
            pos += 4

        if pos + 4 > len(data):
            raise ValueError("بيانات NamespaceCount مفقودة في .locres.")
        ns_count = struct.unpack_from("<I", data, pos)[0]
        pos += 4

        namespaces: List[LocresNamespace] = []
        for _ in range(ns_count):
            ns_hash = 0
            if version >= VERSION_OPTIMIZED:
                ns_hash = struct.unpack_from("<I", data, pos)[0]
                pos += 4
            ns_name, pos = read_fstring(data, pos)
            key_count = struct.unpack_from("<I", data, pos)[0]
            pos += 4

            entries: List[LocresKeyEntry] = []
            for _ in range(key_count):
                key_hash = 0
                if version >= VERSION_OPTIMIZED:
                    key_hash = struct.unpack_from("<I", data, pos)[0]
                    pos += 4
                key_name, pos = read_fstring(data, pos)
                source_hash, str_idx = struct.unpack_from("<Ii", data, pos)
                pos += 8
                text = localized_strings[str_idx] if 0 <= str_idx < len(localized_strings) else ""
                entries.append(
                    LocresKeyEntry(
                        key=key_name,
                        text=text,
                        source_hash=source_hash,
                        key_hash=key_hash,
                        string_index=str_idx,
                    )
                )
            namespaces.append(
                LocresNamespace(name=ns_name, name_hash=ns_hash, entries=entries)
            )
        return LocresData(version=version, namespaces=namespaces)

    # صيغة Legacy (v0) بدون Magic GUID
    pos = 0
    ns_count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    namespaces = []
    for _ in range(ns_count):
        ns_name, pos = read_fstring(data, pos)
        key_count = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        entries = []
        for _ in range(key_count):
            key_name, pos = read_fstring(data, pos)
            source_hash = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            text, pos = read_fstring(data, pos)
            entries.append(
                LocresKeyEntry(
                    key=key_name,
                    text=text,
                    source_hash=source_hash,
                    key_hash=0,
                    string_index=0,
                )
            )
        namespaces.append(LocresNamespace(name=ns_name, name_hash=0, entries=entries))
    return LocresData(version=VERSION_LEGACY, namespaces=namespaces)


def serialize_locres_bytes(
    locres: LocresData,
    replacements: Optional[Dict[PathTuple, str]] = None,
) -> bytes:
    """إعادة بناء بايتات ملف .locres مع تطبيق الترجمات وحفظ المفاتيح والهاشات الأصلية."""
    repl = replacements or {}
    version = locres.version

    if version == VERSION_LEGACY:
        chunks: List[bytes] = [struct.pack("<I", len(locres.namespaces))]
        for ns_idx, ns in enumerate(locres.namespaces):
            chunks.append(write_fstring(ns.name))
            chunks.append(struct.pack("<I", len(ns.entries)))
            for key_idx, entry in enumerate(ns.entries):
                final_text = repl.get((ns_idx, key_idx), entry.text)
                chunks.append(write_fstring(entry.key))
                chunks.append(struct.pack("<I", entry.source_hash))
                chunks.append(write_fstring(final_text))
        return b"".join(chunks)

    if version not in (VERSION_COMPACT, VERSION_OPTIMIZED, VERSION_OPTIMIZED_CITYHASH64):
        raise ValueError(f"إصدار .locres غير مدعوم للكتابة: {version}")

    # تجميع جدول السلاسل الفريدة وحساب RefCount بدقة دون التأثير على المفاتيح المشتركة
    string_to_idx: Dict[str, int] = {}
    unique_strings: List[str] = []
    ref_counts: List[int] = []
    resolved_indices: Dict[Tuple[int, int], int] = {}
    total_entries = 0

    for ns_idx, ns in enumerate(locres.namespaces):
        for key_idx, entry in enumerate(ns.entries):
            total_entries += 1
            if (ns_idx, key_idx) not in repl and entry.string_index < 0:
                resolved_indices[(ns_idx, key_idx)] = -1
                continue
            final_text = repl.get((ns_idx, key_idx), entry.text)
            idx = string_to_idx.get(final_text)
            if idx is None:
                idx = len(unique_strings)
                string_to_idx[final_text] = idx
                unique_strings.append(final_text)
                ref_counts.append(1)
            else:
                ref_counts[idx] += 1
            resolved_indices[(ns_idx, key_idx)] = idx

    body_chunks: List[bytes] = []
    if version >= VERSION_OPTIMIZED:
        body_chunks.append(struct.pack("<I", total_entries))
    body_chunks.append(struct.pack("<I", len(locres.namespaces)))

    for ns_idx, ns in enumerate(locres.namespaces):
        if version >= VERSION_OPTIMIZED:
            body_chunks.append(struct.pack("<I", ns.name_hash))
        body_chunks.append(write_fstring(ns.name))
        body_chunks.append(struct.pack("<I", len(ns.entries)))
        for key_idx, entry in enumerate(ns.entries):
            if version >= VERSION_OPTIMIZED:
                body_chunks.append(struct.pack("<I", entry.key_hash))
            body_chunks.append(write_fstring(entry.key))
            str_idx = resolved_indices[(ns_idx, key_idx)]
            body_chunks.append(struct.pack("<Ii", entry.source_hash, str_idx))

    body_bytes = b"".join(body_chunks)
    string_array_offset = 25 + len(body_bytes)

    str_chunks: List[bytes] = [struct.pack("<I", len(unique_strings))]
    for s_val, ref_cnt in zip(unique_strings, ref_counts):
        str_chunks.append(write_fstring(s_val))
        if version >= VERSION_OPTIMIZED:
            str_chunks.append(struct.pack("<i", ref_cnt))

    header = LOCRES_MAGIC + struct.pack("<Bq", version, string_array_offset)
    return header + body_bytes + b"".join(str_chunks)


def build_locres_bytes(
    items: Sequence[Tuple[str, str, str]],
    version: int = VERSION_OPTIMIZED,
    source_hashes: Optional[Sequence[int]] = None,
) -> bytes:
    """بناء بايتات ملف .locres صالح من قائمة (namespace, key, text) لأي إصدار."""
    ns_map: Dict[str, LocresNamespace] = {}
    ns_list: List[LocresNamespace] = []

    for idx, (ns_name, key_name, text) in enumerate(items):
        ns = ns_map.get(ns_name)
        if ns is None:
            ns = LocresNamespace(name=ns_name, name_hash=_str_crc32(ns_name))
            ns_map[ns_name] = ns
            ns_list.append(ns)
        src_hash = (
            source_hashes[idx]
            if source_hashes is not None and idx < len(source_hashes)
            else _str_crc32(text)
        )
        ns.entries.append(
            LocresKeyEntry(
                key=key_name,
                text=text,
                source_hash=src_hash,
                key_hash=_str_crc32(key_name),
            )
        )
    return serialize_locres_bytes(LocresData(version=version, namespaces=ns_list))


def locres_to_entries(locres: LocresData) -> List[Tuple[PathTuple, str, str]]:
    """تحويل LocresData إلى قائمة عناصر ParsedDocument الموحدة."""
    entries: List[Tuple[PathTuple, str, str]] = []
    for ns_idx, ns in enumerate(locres.namespaces):
        for key_idx, entry in enumerate(ns.entries):
            flat_key = f"{ns.name}.{entry.key}" if ns.name else entry.key
            entries.append(((ns_idx, key_idx), flat_key, entry.text))
    return entries


class LocresFormatHandler(FormatHandler):
    """محلل وكاتب ملفات Unreal .locres الخارجية مع كتابة ذرية والتحقق الفوري."""

    format_names = ("locres",)

    def load(self, file_path: str) -> ParsedDocument:
        with open(file_path, "rb") as f:
            raw = f.read()
        locres = parse_locres_bytes(raw)
        return ParsedDocument(
            format_name="locres",
            raw_data=locres,
            entries=locres_to_entries(locres),
        )

    def save(
        self,
        file_path: str,
        doc: ParsedDocument,
        replacements: Dict[PathTuple, str],
    ) -> None:
        locres: LocresData = doc.raw_data
        updated_bytes = serialize_locres_bytes(locres, replacements)
        # التحقق الفوري من سلامة الملف الثنائي قبل الكتابة النهائية
        parse_locres_bytes(updated_bytes)

        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        tmp_path = file_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(updated_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)
