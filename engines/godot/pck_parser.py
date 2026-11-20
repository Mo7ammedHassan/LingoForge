# -*- coding: utf-8 -*-
"""محلل ثنائي بلغة Python خالصة لحزم Godot (.pck المستقلة والمدمجة في .exe) وملفات .translation و project.godot."""

from dataclasses import dataclass, field
import hashlib
import os
import re
import struct
from typing import Any, Dict, List, Optional, Tuple


PCK_MAGIC = b"GDPC"
RSRC_MAGIC = b"RSRC"
ECFG_MAGIC = b"ECFG"

# Variant IDs داخل صيغة Godot Binary Resource (RSRC)
_RSRC_VARIANT_NIL = 1
_RSRC_VARIANT_BOOL = 2
_RSRC_VARIANT_INT = 3
_RSRC_VARIANT_FLOAT = 4
_RSRC_VARIANT_STRING = 5
_RSRC_VARIANT_VECTOR2 = 10
_RSRC_VARIANT_RECT2 = 11
_RSRC_VARIANT_VECTOR3 = 12
_RSRC_VARIANT_PLANE = 13
_RSRC_VARIANT_QUATERNION = 14
_RSRC_VARIANT_AABB = 15
_RSRC_VARIANT_BASIS = 16
_RSRC_VARIANT_TRANSFORM3D = 17
_RSRC_VARIANT_COLOR = 20
_RSRC_VARIANT_NODE_PATH = 21
_RSRC_VARIANT_RID = 22
_RSRC_VARIANT_OBJECT = 23
_RSRC_VARIANT_INPUT_EVENT = 24
_RSRC_VARIANT_DICTIONARY = 25
_RSRC_VARIANT_ARRAY = 26
_RSRC_VARIANT_PACKED_BYTE_ARRAY = 31
_RSRC_VARIANT_PACKED_INT32_ARRAY = 32
_RSRC_VARIANT_PACKED_INT64_ARRAY = 33
_RSRC_VARIANT_PACKED_FLOAT32_ARRAY = 34
_RSRC_VARIANT_PACKED_FLOAT64_ARRAY = 35
_RSRC_VARIANT_PACKED_STRING_ARRAY = 36
_RSRC_VARIANT_VECTOR2_ARRAY = 37
_RSRC_VARIANT_VECTOR3_ARRAY = 38
_RSRC_VARIANT_COLOR_ARRAY = 39
_RSRC_VARIANT_INT64 = 40
_RSRC_VARIANT_DOUBLE = 41
_RSRC_VARIANT_STRING_NAME = 42

_PACK_DIR_ENCRYPTED_FLAG = 1 << 0

# جدول SMAZ القياسي (254 مدخلاً) المطابق لـ thirdparty/misc/smaz.c في محرك Godot
SMAZ_RCB: Tuple[str, ...] = (
    " ", "the", "e", "t", "a", "of", "o", "and", "i", "n", "s", "e ", "r", " th",
    " t", "in", "he", "th", "h", "he ", "to", "\r\n", "l", "s ", "d", " a", "an",
    "er", "c", " o", "d ", "on", " of", "re", "of ", "t ", ", ", "is", "u", "at",
    "   ", "n ", "or", "which", "f", "m", "as", "it", "that", "\n", "was", "en",
    "  ", " w", "es", " an", " i", "\r", "f ", "g", "p", "nd", " s", "nd ", "ed ",
    "w", "ed", "http://", "for", "te", "ing", "y ", "The", " c", "ti", "r ", "his",
    "st", " in", "ar", "nt", ",", " to", "y", "ng", " h", "with", "le", "al", "to ",
    "b", "ou", "be", "were", " b", "se", "o ", "ent", "ha", "ng ", "their", "\"",
    "hi", "from", " f", "in ", "de", "ion", "me", "v", ".", "ve", "all", "re ",
    "ri", "ro", "is ", "co", "f t", "are", "ea", ". ", "her", " m", "er ", " p",
    "es ", "by", "they", "di", "ra", "ic", "not", "s, ", "d t", "at ", "ce", "la",
    "h ", "ne", "as ", "tio", "on ", "n t", "io", "we", " a ", "om", ", a", "s o",
    "ur", "li", "ll", "ch", "had", "this", "e t", "g ", "e\r\n", " wh", "ere",
    " co", "e o", "a ", "us", " d", "ss", "\n\r\n", "\r\n\r", "=\"", " be", " e",
    "s a", "ma", "one", "t t", "or ", "but", "el", "so", "l ", "e s", "s,", "no",
    "ter", " wa", "iv", "ho", "e a", " r", "hat", "s t", "ns", "ch ", "wh", "tr",
    "ut", "/", "have", "ly ", "ta", " ha", " on", "tha", "-", " l", "ati", "en ",
    "pe", " re", "there", "ass", "si", " fo", "wa", "ec", "our", "who", "its", "z",
    "fo", "rs", ">", "ot", "un", "<", "im", "th ", "nc", "ate", "><", "ver", "ad",
    " we", "ly", "ee", " n", "id", " cl", "ac", "il", "</", "rt", " wi", "div",
    "e, ", " it", "whi", " ma", "ge", "x", "e c", "men", ".com",
)
_SMAZ_RCB_BYTES: Tuple[bytes, ...] = tuple(s.encode("latin1") for s in SMAZ_RCB)
_SMAZ_ENCODE_MAP: Dict[bytes, int] = {b: idx for idx, b in enumerate(_SMAZ_RCB_BYTES)}


def smaz_decompress(data: bytes, expected_size: int = 0) -> Optional[bytes]:
    """فك ضغط بايتات SMAZ قياسية (المستخدمة في OptimizedTranslation داخل Godot)."""
    if not data:
        return b"" if expected_size == 0 else None
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        if code == 254:
            if i + 1 >= n:
                return None
            out.append(data[i + 1])
            i += 2
        elif code == 255:
            if i + 1 >= n:
                return None
            run_len = data[i + 1] + 1
            if i + 2 + run_len > n:
                return None
            out.extend(data[i + 2 : i + 2 + run_len])
            i += 2 + run_len
        elif code < len(_SMAZ_RCB_BYTES):
            out.extend(_SMAZ_RCB_BYTES[code])
            i += 1
        else:
            return None
    if expected_size > 0:
        if len(out) < expected_size:
            return None
        if len(out) > expected_size:
            del out[expected_size:]
    return bytes(out)


def smaz_compress(data: bytes) -> bytes:
    """ضغط بايتات باستخدام جدول SMAZ القياسي (لأغراض الاختبار وتكافؤ OptimizedTranslation)."""
    if not data:
        return b""
    out = bytearray()
    verbatim = bytearray()

    def _flush_verbatim() -> None:
        pos = 0
        vlen = len(verbatim)
        while pos < vlen:
            chunk_len = min(256, vlen - pos)
            if chunk_len == 1:
                out.append(254)
                out.append(verbatim[pos])
            else:
                out.append(255)
                out.append(chunk_len - 1)
                out.extend(verbatim[pos : pos + chunk_len])
            pos += chunk_len
        verbatim.clear()

    i = 0
    n = len(data)
    while i < n:
        matched_code: Optional[int] = None
        matched_len = 0
        for length in range(min(7, n - i), 0, -1):
            code = _SMAZ_ENCODE_MAP.get(data[i : i + length])
            if code is not None:
                matched_code = code
                matched_len = length
                break
        if matched_code is not None:
            _flush_verbatim()
            out.append(matched_code)
            i += matched_len
        else:
            verbatim.append(data[i])
            i += 1
    _flush_verbatim()
    return bytes(out)


@dataclass
class PckEntry:
    """يمثل ملفاً واحداً مفهرساً داخل حزمة Godot .pck."""

    res_path: str
    offset: int
    size: int
    md5: bytes = b""
    encrypted: bool = False


@dataclass
class PckInfo:
    """معلومات الترويسة والفهرس لحزمة Godot .pck (مستقلة أو مدمجة في .exe)."""

    container_path: str
    is_embedded: bool
    pck_offset: int
    pck_size: int
    pack_format_version: int
    godot_major: int
    godot_minor: int
    godot_patch: int
    flags: int = 0
    files_base: int = 0
    dir_encrypted: bool = False
    entries: List[PckEntry] = field(default_factory=list)


def normalize_res_path(path: str) -> str:
    """توحيد مسار المورد إلى صيغة res://... القياسية في Godot."""
    cleaned = path.replace("\\", "/").strip()
    if cleaned.startswith("res://"):
        return cleaned
    return "res://" + cleaned.lstrip("/")


def locate_pck_in_file(file_path: str) -> Optional[Tuple[int, int]]:
    """تحديد موقع بداية وحجم حزمة GDPC داخل ملف .pck مستقل أو .exe مدمج."""
    try:
        file_size = os.path.getsize(file_path)
        if file_size < 16:
            return None
        with open(file_path, "rb") as f:
            head = f.read(4)
            if head == PCK_MAGIC:
                return 0, file_size

            # الحزم المدمجة في نهاية .exe تنتهي بـ: uint64 pck_size + b"GDPC" (آخر 12 بايت)
            f.seek(file_size - 12)
            trailer = f.read(12)
            if len(trailer) != 12:
                return None
            pck_size, trailer_magic = struct.unpack("<Q4s", trailer)
            if trailer_magic != PCK_MAGIC or pck_size < 20:
                return None

            candidate_start = file_size - 12 - pck_size
            if 0 <= candidate_start <= file_size - 12:
                f.seek(candidate_start)
                if f.read(4) == PCK_MAGIC:
                    return candidate_start, pck_size

            alt_start = file_size - pck_size
            if 0 <= alt_start < file_size - 12:
                f.seek(alt_start)
                if f.read(4) == PCK_MAGIC:
                    return alt_start, pck_size
    except OSError:
        return None
    return None


def parse_pck_header_and_entries(file_path: str) -> Optional[PckInfo]:
    """قراءة ترويسة وفهرس حزمة Godot .pck (v1 و v2+) بدون تعديل الملف."""
    located = locate_pck_in_file(file_path)
    if not located:
        return None

    pck_offset, pck_size = located
    is_embedded = pck_offset > 0
    try:
        file_size = os.path.getsize(file_path)
        with open(file_path, "rb") as f:
            f.seek(pck_offset)
            hdr = f.read(20)
            if len(hdr) < 20:
                return None
            magic, pack_fmt, major, minor, patch = struct.unpack("<4sIIII", hdr)
            if magic != PCK_MAGIC:
                return None

            flags = 0
            files_base = 0
            if pack_fmt == 1:
                if len(f.read(64)) < 64:
                    return None
            elif pack_fmt >= 2:
                ext_hdr = f.read(12)
                if len(ext_hdr) < 12:
                    return None
                flags, files_base = struct.unpack("<IQ", ext_hdr)
                if len(f.read(64)) < 64:
                    return None
            else:
                return None

            dir_encrypted = bool(flags & _PACK_DIR_ENCRYPTED_FLAG)
            info = PckInfo(
                container_path=os.path.abspath(file_path),
                is_embedded=is_embedded,
                pck_offset=pck_offset,
                pck_size=pck_size,
                pack_format_version=pack_fmt,
                godot_major=major,
                godot_minor=minor,
                godot_patch=patch,
                flags=flags,
                files_base=files_base,
                dir_encrypted=dir_encrypted,
            )
            if dir_encrypted:
                return info

            cnt_raw = f.read(4)
            if len(cnt_raw) < 4:
                return info
            file_count = struct.unpack("<I", cnt_raw)[0]
            if file_count > 1_000_000:
                return info

            pck_end = pck_offset + pck_size
            for _ in range(file_count):
                plen_raw = f.read(4)
                if len(plen_raw) < 4:
                    break
                path_len = struct.unpack("<I", plen_raw)[0]
                if path_len > 65536:
                    break
                path_bytes = f.read(path_len)
                if len(path_bytes) < path_len:
                    break
                # Godot يكمّل الطول لمضاعفات 4 بأصفار \x00
                res_path = normalize_res_path(
                    path_bytes.rstrip(b"\x00").decode("utf-8", errors="replace")
                )

                if pack_fmt == 1:
                    meta_raw = f.read(32)
                    if len(meta_raw) < 32:
                        break
                    rel_off, size, md5 = struct.unpack("<QQ16s", meta_raw)
                    entry_flags = 0
                    abs_off = pck_offset + rel_off
                    if abs_off + size > file_size and pck_offset <= rel_off and rel_off + size <= file_size:
                        abs_off = rel_off
                else:
                    meta_raw = f.read(36)
                    if len(meta_raw) < 36:
                        break
                    rel_off, size, md5, entry_flags = struct.unpack("<QQ16sI", meta_raw)
                    abs_off = pck_offset + files_base + rel_off
                    if (abs_off + size > pck_end or abs_off + size > file_size) and files_base >= pck_offset:
                        alt_off = files_base + rel_off
                        if alt_off + size <= file_size:
                            abs_off = alt_off

                if abs_off + size <= file_size:
                    info.entries.append(
                        PckEntry(
                            res_path=res_path,
                            offset=abs_off,
                            size=size,
                            md5=md5,
                            encrypted=bool(entry_flags & 0x01),
                        )
                    )
            return info
    except OSError:
        return None


def read_pck_entry_bytes(pck_info: PckInfo, entry: PckEntry) -> bytes:
    """استخراج بايتات ملف واحد من داخل حزمة .pck (أو .exe مدمج) دون تعديلها."""
    if entry.encrypted or entry.size <= 0:
        return b""
    with open(pck_info.container_path, "rb") as f:
        f.seek(entry.offset)
        return f.read(entry.size)


def extract_pck_entry_by_path(container_path: str, res_path: str) -> Optional[bytes]:
    """استخراج بايتات مورد محدد عبر مساره res://... من ملف .pck أو .exe مدمج."""
    pck_info = parse_pck_header_and_entries(container_path)
    if pck_info is None:
        return None
    target = normalize_res_path(res_path)
    for entry in pck_info.entries:
        if entry.res_path == target or entry.res_path.lower() == target.lower():
            return read_pck_entry_bytes(pck_info, entry)
    return None


def build_godot_pck_blob(
    files: Dict[str, bytes],
    pack_format_version: int = 2,
    godot_version: Tuple[int, int, int] = (4, 2, 1),
    embedded_exe_prefix: Optional[bytes] = None,
) -> bytes:
    """بناء حزمة Godot .pck قياسية (v1 لـ Godot 3 أو v2 لـ Godot 4) مع خيار الدمج في .exe."""
    major, minor, patch = godot_version
    prepared: List[Tuple[bytes, bytes, bytes]] = []
    for raw_path, content in files.items():
        norm = normalize_res_path(raw_path).encode("utf-8")
        pad = (-len(norm)) % 4
        if pad == 0:
            # Godot يضيف على الأقل بايت نهاية أو يحاذي لـ 4؛ المحاذاة لأقرب 4 كافية
            padded_path = norm
        else:
            padded_path = norm + (b"\x00" * pad)
        md5 = hashlib.md5(content).digest()
        prepared.append((padded_path, content, md5))

    header_size = 84 if pack_format_version == 1 else 96
    entry_fixed_size = 32 if pack_format_version == 1 else 36
    dir_size = 4 + sum(4 + len(p) + entry_fixed_size for p, _, _ in prepared)
    data_start = header_size + dir_size
    # محاذاة بداية البيانات لـ 16 بايت
    data_pad = (-data_start) % 16
    files_base = data_start + data_pad

    dir_buf = bytearray()
    dir_buf.extend(struct.pack("<I", len(prepared)))
    data_buf = bytearray()
    cur_rel_off = 0
    for padded_path, content, md5 in prepared:
        dir_buf.extend(struct.pack("<I", len(padded_path)))
        dir_buf.extend(padded_path)
        if pack_format_version == 1:
            entry_off = files_base + cur_rel_off
            dir_buf.extend(struct.pack("<QQ16s", entry_off, len(content), md5))
        else:
            dir_buf.extend(struct.pack("<QQ16sI", cur_rel_off, len(content), md5, 0))
        data_buf.extend(content)
        cur_rel_off += len(content)

    hdr_buf = bytearray()
    hdr_buf.extend(struct.pack("<4sIIII", PCK_MAGIC, pack_format_version, major, minor, patch))
    if pack_format_version == 1:
        hdr_buf.extend(b"\x00" * 64)
    else:
        hdr_buf.extend(struct.pack("<IQ", 0, files_base))
        hdr_buf.extend(b"\x00" * 64)

    pck_blob = bytes(hdr_buf) + bytes(dir_buf) + (b"\x00" * data_pad) + bytes(data_buf)
    if embedded_exe_prefix is None:
        return pck_blob

    trailer = struct.pack("<Q4s", len(pck_blob), PCK_MAGIC)
    return embedded_exe_prefix + pck_blob + trailer


# ---------------------------------------------------------------------------
# تحليل project.godot و project.binary (ECFG)
# ---------------------------------------------------------------------------

_RES_PATH_RE = re.compile(r'"(res://[^"]+)"')


def parse_project_godot_text(text: str) -> Dict[str, Any]:
    """تحليل نص project.godot (Godot 3 و 4) لاستخراج config_version ومسارات الترجمة والثيم."""
    result: Dict[str, Any] = {
        "config_version": None,
        "name": "",
        "translations": [],
        "fallback": "",
        "custom_theme": "",
        "text_driver": "",
    }
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().lower()
        val = val.strip()

        if not section and key == "config_version":
            try:
                result["config_version"] = int(val)
            except ValueError:
                pass
        elif section == "application" and key == "config/name":
            result["name"] = val.strip('"')
        elif (section == "locale" and key == "translations") or (
            section == "internationalization" and key == "locale/translations"
        ):
            result["translations"] = _RES_PATH_RE.findall(val)
        elif (section == "locale" and key == "fallback") or (
            section == "internationalization" and key == "locale/fallback"
        ):
            result["fallback"] = val.strip('"')
        elif section == "gui" and key == "theme/custom":
            result["custom_theme"] = val.strip('"')
        elif section == "internationalization" and key == "rendering/text_driver":
            result["text_driver"] = val.strip('"')
    return result


def parse_project_binary(raw_bytes: bytes) -> Dict[str, Any]:
    """تحليل project.binary (ECFG) لاستخراج مسارات الترجمة والإعدادات النصية (Godot 3 و 4)."""
    result: Dict[str, Any] = {
        "name": "",
        "translations": [],
        "fallback": "",
        "custom_theme": "",
        "text_driver": "",
    }
    if len(raw_bytes) < 8 or raw_bytes[:4] != ECFG_MAGIC:
        return result

    pos = 4
    count = struct.unpack_from("<I", raw_bytes, pos)[0]
    pos += 4
    if count > 100_000:
        return result

    def _read_var_str(vdata: bytes) -> str:
        if len(vdata) >= 8 and struct.unpack_from("<I", vdata, 0)[0] == 4:
            slen = struct.unpack_from("<I", vdata, 4)[0]
            if 8 + slen <= len(vdata):
                return vdata[8 : 8 + slen].decode("utf-8", errors="replace").rstrip("\x00")
        return ""

    for _ in range(count):
        if pos + 4 > len(raw_bytes):
            break
        klen = struct.unpack_from("<I", raw_bytes, pos)[0]
        pos += 4
        if pos + klen + 4 > len(raw_bytes):
            break
        key = raw_bytes[pos : pos + klen].decode("utf-8", errors="replace").rstrip("\x00")
        pos += klen
        vlen = struct.unpack_from("<I", raw_bytes, pos)[0]
        pos += 4
        if pos + vlen > len(raw_bytes):
            break
        vdata = raw_bytes[pos : pos + vlen]
        pos += vlen

        if key in ("locale/translations", "internationalization/locale/translations"):
            found = re.findall(rb"res://[^\x00\"]+", vdata)
            result["translations"] = [
                m.decode("utf-8", errors="replace") for m in found
            ]
        elif key == "application/config/name":
            result["name"] = _read_var_str(vdata)
        elif key in ("locale/fallback", "internationalization/locale/fallback"):
            result["fallback"] = _read_var_str(vdata)
        elif key == "gui/theme/custom":
            result["custom_theme"] = _read_var_str(vdata)
        elif key == "internationalization/rendering/text_driver":
            result["text_driver"] = _read_var_str(vdata)
    return result


# ---------------------------------------------------------------------------
# تحليل موارد الترجمة في Godot (.translation RSRC و .tres)
# ---------------------------------------------------------------------------

class _BinaryReader:
    def __init__(self, data: bytes, big_endian: bool = False) -> None:
        self.data = data
        self.pos = 0
        self.endian = ">" if big_endian else "<"

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def u32(self) -> int:
        val = struct.unpack_from(self.endian + "I", self.data, self.pos)[0]
        self.pos += 4
        return val

    def i32(self) -> int:
        val = struct.unpack_from(self.endian + "i", self.data, self.pos)[0]
        self.pos += 4
        return val

    def u64(self) -> int:
        val = struct.unpack_from(self.endian + "Q", self.data, self.pos)[0]
        self.pos += 8
        return val

    def i64(self) -> int:
        val = struct.unpack_from(self.endian + "q", self.data, self.pos)[0]
        self.pos += 8
        return val

    def f32(self) -> float:
        val = struct.unpack_from(self.endian + "f", self.data, self.pos)[0]
        self.pos += 4
        return val

    def f64(self) -> float:
        val = struct.unpack_from(self.endian + "d", self.data, self.pos)[0]
        self.pos += 8
        return val

    def read_bytes(self, n: int) -> bytes:
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def unicode_string(self) -> str:
        length = self.u32()
        if length <= 0 or length > self.remaining():
            return ""
        raw = self.read_bytes(length)
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace")

    def align4(self) -> None:
        rem = self.pos % 4
        if rem:
            self.pos += 4 - rem


def _read_rsrc_variant(
    reader: _BinaryReader,
    string_table: List[str],
    use_real64: bool,
    depth: int = 0,
) -> Any:
    """قراءة قيمة Variant واحدة من ملف RSRC الثنائي."""
    if depth > 16 or reader.remaining() < 4:
        return None
    vtype_raw = reader.u32()
    vtype = vtype_raw & 0xFFFF

    if vtype == _RSRC_VARIANT_NIL:
        return None
    if vtype == _RSRC_VARIANT_BOOL:
        return bool(reader.u32())
    if vtype == _RSRC_VARIANT_INT:
        return reader.i32()
    if vtype == _RSRC_VARIANT_INT64:
        return reader.i64()
    if vtype == _RSRC_VARIANT_FLOAT:
        return reader.f64() if use_real64 else reader.f32()
    if vtype == _RSRC_VARIANT_DOUBLE:
        return reader.f64()
    if vtype in (_RSRC_VARIANT_STRING, _RSRC_VARIANT_STRING_NAME):
        return reader.unicode_string()
    if vtype == _RSRC_VARIANT_VECTOR2:
        reader.read_bytes(16 if use_real64 else 8)
        return None
    if vtype == _RSRC_VARIANT_RECT2:
        reader.read_bytes(32 if use_real64 else 16)
        return None
    if vtype == _RSRC_VARIANT_VECTOR3:
        reader.read_bytes(24 if use_real64 else 12)
        return None
    if vtype in (_RSRC_VARIANT_PLANE, _RSRC_VARIANT_QUATERNION, _RSRC_VARIANT_COLOR):
        reader.read_bytes(32 if (use_real64 and vtype != _RSRC_VARIANT_COLOR) else 16)
        return None
    if vtype == _RSRC_VARIANT_AABB:
        reader.read_bytes(48 if use_real64 else 24)
        return None
    if vtype == _RSRC_VARIANT_BASIS:
        reader.read_bytes(72 if use_real64 else 36)
        return None
    if vtype == _RSRC_VARIANT_TRANSFORM3D:
        reader.read_bytes(96 if use_real64 else 48)
        return None
    if vtype == _RSRC_VARIANT_NODE_PATH:
        name_cnt = reader.u32()
        sub_cnt = reader.u32()
        _flags = reader.u32()
        for _ in range(name_cnt & 0x7FFFFFFF):
            idx = reader.u32()
            if idx & 0x80000000:
                reader.unicode_string()
        for _ in range(sub_cnt):
            idx = reader.u32()
            if idx & 0x80000000:
                reader.unicode_string()
        return None
    if vtype == _RSRC_VARIANT_RID:
        reader.u32()
        return None
    if vtype == _RSRC_VARIANT_OBJECT:
        obj_kind = reader.u32()
        if obj_kind in (1, 2):  # external / internal resource index
            reader.u32()
        elif obj_kind == 3:
            reader.unicode_string()
            reader.unicode_string()
        return None
    if vtype == _RSRC_VARIANT_DICTIONARY:
        count = reader.u32() & 0x7FFFFFFF
        out: Dict[Any, Any] = {}
        for _ in range(min(count, 200_000)):
            k = _read_rsrc_variant(reader, string_table, use_real64, depth + 1)
            v = _read_rsrc_variant(reader, string_table, use_real64, depth + 1)
            if isinstance(k, str) and isinstance(v, str):
                out[k] = v
        return out
    if vtype == _RSRC_VARIANT_ARRAY:
        count = reader.u32() & 0x7FFFFFFF
        arr = []
        for _ in range(min(count, 200_000)):
            arr.append(_read_rsrc_variant(reader, string_table, use_real64, depth + 1))
        return arr
    if vtype == _RSRC_VARIANT_PACKED_BYTE_ARRAY:
        count = reader.u32()
        raw = reader.read_bytes(count)
        reader.align4()
        return raw
    if vtype == _RSRC_VARIANT_PACKED_INT32_ARRAY:
        count = reader.u32()
        vals = list(struct.unpack_from(reader.endian + f"{count}i", reader.data, reader.pos))
        reader.pos += count * 4
        return vals
    if vtype == _RSRC_VARIANT_PACKED_INT64_ARRAY:
        count = reader.u32()
        vals = list(struct.unpack_from(reader.endian + f"{count}q", reader.data, reader.pos))
        reader.pos += count * 8
        return vals
    if vtype == _RSRC_VARIANT_PACKED_FLOAT32_ARRAY:
        count = reader.u32()
        reader.pos += count * 4
        return []
    if vtype == _RSRC_VARIANT_PACKED_FLOAT64_ARRAY:
        count = reader.u32()
        reader.pos += count * 8
        return []
    if vtype == _RSRC_VARIANT_PACKED_STRING_ARRAY:
        count = reader.u32()
        return [reader.unicode_string() for _ in range(min(count, 200_000))]
    return None


def _get_rsrc_string_name(idx: int, string_table: List[str], reader: _BinaryReader) -> str:
    if idx & 0x80000000:
        return reader.unicode_string()
    if 0 <= idx < len(string_table):
        return string_table[idx]
    return ""


def _parse_rsrc_translation(raw_bytes: bytes) -> Optional[Tuple[str, List[Tuple[str, str]]]]:
    """استخراج أزواج الترجمة من مورد ثنائي بصيغة Godot RSRC (Translation أو OptimizedTranslation)."""
    if len(raw_bytes) < 64 or raw_bytes[:4] != RSRC_MAGIC:
        return None

    big_endian = bool(struct.unpack_from("<I", raw_bytes, 4)[0])
    reader = _BinaryReader(raw_bytes, big_endian=big_endian)
    reader.pos = 8
    use_real64 = bool(reader.u32())
    _ver_major = reader.u32()
    _ver_minor = reader.u32()
    ver_format = reader.u32()
    res_type = reader.unicode_string()
    _importmd_ofs = reader.u64()
    flags = reader.u32()
    if ver_format >= 4:
        _uid = reader.u64()
        if flags & 0x08:  # FORMAT_FLAG_HAS_SCRIPT_CLASS
            reader.unicode_string()
        reader.read_bytes(11 * 4)
    else:
        reader.read_bytes(14 * 4)

    if res_type not in ("Translation", "OptimizedTranslation", "PHashTranslation"):
        # قد يكون Resource عام يحتوي خصائص ترجمة
        if "translation" not in res_type.lower():
            return None

    string_count = reader.u32()
    if string_count > 100_000:
        return None
    string_table = [reader.unicode_string() for _ in range(string_count)]

    ext_count = reader.u32()
    if ext_count > 10_000:
        return None
    for _ in range(ext_count):
        reader.unicode_string()  # type
        reader.unicode_string()  # path
        if ver_format >= 4:
            reader.u64()  # uid

    int_count = reader.u32()
    if int_count <= 0 or int_count > 10_000:
        return None
    int_offsets: List[Tuple[str, int]] = []
    for _ in range(int_count):
        path = reader.unicode_string()
        ofs = reader.u64()
        int_offsets.append((path, ofs))

    props: Dict[str, Any] = {}
    for _, ofs in int_offsets:
        if ofs <= 0 or ofs >= len(raw_bytes):
            continue
        reader.pos = ofs
        _obj_type = reader.unicode_string()
        prop_count = reader.u32()
        for _ in range(min(prop_count, 256)):
            if reader.remaining() < 4:
                break
            name_idx = reader.u32()
            prop_name = _get_rsrc_string_name(name_idx, string_table, reader)
            val = _read_rsrc_variant(reader, string_table, use_real64)
            if prop_name:
                props[prop_name] = val

    locale = str(props.get("locale") or "en")
    entries: List[Tuple[str, str]] = []

    messages = props.get("messages")
    if isinstance(messages, dict):
        for k, v in messages.items():
            if isinstance(k, str) and isinstance(v, str) and v.strip():
                entries.append((k, v))
        return locale, entries

    if isinstance(messages, list):
        # أحياناً تُخزن أزواج key, value متتالية في PackedStringArray
        for idx in range(0, len(messages) - 1, 2):
            k, v = messages[idx], messages[idx + 1]
            if isinstance(k, str) and isinstance(v, str) and v.strip():
                entries.append((k, v))
        return locale, entries

    hash_table = props.get("hash_table")
    bucket_table = props.get("bucket_table")
    strings_blob = props.get("strings")
    if (
        isinstance(hash_table, list)
        and isinstance(bucket_table, list)
        and isinstance(strings_blob, (bytes, bytearray))
    ):
        seen_buckets = set()
        for b_idx in hash_table:
            if not isinstance(b_idx, int) or b_idx < 0 or b_idx in seen_buckets:
                continue
            seen_buckets.add(b_idx)
            if b_idx + 2 > len(bucket_table):
                continue
            b_size = bucket_table[b_idx]
            if b_size <= 0 or b_idx + 2 + b_size * 4 > len(bucket_table):
                continue
            for j in range(b_size):
                base = b_idx + 2 + j * 4
                key_hash = bucket_table[base] & 0xFFFFFFFF
                str_off = bucket_table[base + 1]
                comp_size = bucket_table[base + 2]
                uncomp_size = bucket_table[base + 3]
                if str_off < 0 or comp_size <= 0 or str_off + comp_size > len(strings_blob):
                    continue
                chunk = bytes(strings_blob[str_off : str_off + comp_size])
                if comp_size == uncomp_size:
                    try:
                        decoded = chunk.decode("utf-8").rstrip("\x00")
                        if decoded:
                            entries.append((f"msg_{key_hash:08x}", decoded))
                    except UnicodeDecodeError:
                        pass
                elif uncomp_size > 0:
                    raw_dec = smaz_decompress(chunk, expected_size=uncomp_size)
                    if raw_dec:
                        try:
                            decoded = raw_dec.decode("utf-8").rstrip("\x00")
                            if decoded:
                                entries.append((f"msg_{key_hash:08x}", decoded))
                        except UnicodeDecodeError:
                            pass
        return locale, entries

    return None


def _parse_tres_translation(raw_bytes: bytes) -> Optional[Tuple[str, List[Tuple[str, str]]]]:
    """استخراج أزواج الترجمة من ملف نصي .tres من نوع Translation."""
    try:
        text = raw_bytes.decode("utf-8-sig", errors="replace")
    except Exception:
        return None
    if "[gd_resource" not in text or "Translation" not in text:
        return None

    locale = "en"
    m_loc = re.search(r'^locale\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if m_loc:
        locale = m_loc.group(1)

    entries: List[Tuple[str, str]] = []
    m_msg = re.search(r"messages\s*=\s*\{(.*?)\}", text, re.DOTALL)
    if m_msg:
        body = m_msg.group(1)
        for k, v in re.findall(r'"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"', body):
            entries.append((k, v))
    if entries:
        return locale, entries
    return None


def extract_translation_resource_entries(
    raw_bytes: bytes,
) -> Optional[Tuple[str, List[Tuple[str, str]]]]:
    """استخراج السلاسل النصية من مورد ترجمة Godot (.translation الثنائي أو .tres النصي)."""
    if raw_bytes[:4] == RSRC_MAGIC:
        return _parse_rsrc_translation(raw_bytes)
    return _parse_tres_translation(raw_bytes)


def build_godot_translation_rsrc_blob(
    locale: str,
    messages: Dict[str, str],
    optimized: bool = False,
    godot_version: Tuple[int, int, int] = (4, 2, 1),
    compress_smaz: bool = False,
) -> bytes:
    """بناء ملف .translation ثنائي بصيغة Godot RSRC لأغراض الاختبار والتحقق."""
    major, minor, patch = godot_version

    def _pack_str(s: str) -> bytes:
        raw = s.encode("utf-8") + b"\x00"
        return struct.pack("<I", len(raw)) + raw

    string_table = ["locale", "messages"] if not optimized else ["locale", "hash_table", "bucket_table", "strings"]
    res_type = "OptimizedTranslation" if optimized else "Translation"

    hdr = bytearray()
    hdr.extend(RSRC_MAGIC)
    hdr.extend(struct.pack("<IIIII", 0, 0, major, minor, 4))  # little endian, real32, ver_format=4
    hdr.extend(_pack_str(res_type))
    hdr.extend(struct.pack("<Q", 0))  # importmd_ofs
    hdr.extend(struct.pack("<I", 0))  # flags
    hdr.extend(struct.pack("<Q", 0))  # uid
    hdr.extend(b"\x00" * (11 * 4))

    hdr.extend(struct.pack("<I", len(string_table)))
    for name in string_table:
        hdr.extend(_pack_str(name))
    hdr.extend(struct.pack("<I", 0))  # ext_resources
    hdr.extend(struct.pack("<I", 1))  # int_resources

    int_table_pos = len(hdr)
    hdr.extend(_pack_str("local://1"))
    ofs_placeholder_pos = len(hdr)
    hdr.extend(struct.pack("<Q", 0))  # offset placeholder

    body_offset = len(hdr)
    struct.pack_into("<Q", hdr, ofs_placeholder_pos, body_offset)

    body = bytearray()
    body.extend(_pack_str(res_type))
    if not optimized:
        body.extend(struct.pack("<I", 2))  # 2 properties: locale, messages
        body.extend(struct.pack("<I", 0))  # string_table[0] = "locale"
        body.extend(struct.pack("<I", _RSRC_VARIANT_STRING))
        body.extend(_pack_str(locale))

        body.extend(struct.pack("<I", 1))  # string_table[1] = "messages"
        body.extend(struct.pack("<I", _RSRC_VARIANT_DICTIONARY))
        body.extend(struct.pack("<I", len(messages)))
        for k, v in messages.items():
            body.extend(struct.pack("<I", _RSRC_VARIANT_STRING))
            body.extend(_pack_str(k))
            body.extend(struct.pack("<I", _RSRC_VARIANT_STRING))
            body.extend(_pack_str(v))
    else:
        strings_blob = bytearray()
        bucket_table: List[int] = [len(messages), 0]
        for idx, (k, v) in enumerate(messages.items()):
            raw_v = v.encode("utf-8")
            stored_v = smaz_compress(raw_v) if compress_smaz else raw_v
            str_off = len(strings_blob)
            strings_blob.extend(stored_v)
            key_hash = int.from_bytes(hashlib.md5(k.encode("utf-8")).digest()[:4], "little")
            bucket_table.extend([key_hash, str_off, len(stored_v), len(raw_v)])
        hash_table = [0] if messages else [-1]

        body.extend(struct.pack("<I", 4))
        # 0: locale
        body.extend(struct.pack("<II", 0, _RSRC_VARIANT_STRING))
        body.extend(_pack_str(locale))
        # 1: hash_table
        body.extend(struct.pack("<II", 1, _RSRC_VARIANT_PACKED_INT32_ARRAY))
        body.extend(struct.pack("<I", len(hash_table)))
        for h in hash_table:
            body.extend(struct.pack("<i", h))
        # 2: bucket_table
        body.extend(struct.pack("<II", 2, _RSRC_VARIANT_PACKED_INT32_ARRAY))
        body.extend(struct.pack("<I", len(bucket_table)))
        for b in bucket_table:
            body.extend(struct.pack("<i", b))
        # 3: strings
        body.extend(struct.pack("<II", 3, _RSRC_VARIANT_PACKED_BYTE_ARRAY))
        body.extend(struct.pack("<I", len(strings_blob)))
        body.extend(strings_blob)
        pad = (-len(strings_blob)) % 4
        body.extend(b"\x00" * pad)

    return bytes(hdr) + bytes(body)
