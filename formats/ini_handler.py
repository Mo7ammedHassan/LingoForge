# -*- coding: utf-8 -*-
"""محلل وكاتب ملفات INI و TXT مع حفظ الأقسام والتعليقات والأسطر والأقواس كما هي."""

import os
import re
from typing import Dict, List, Optional, Tuple

from formats.base import (
    FormatHandler,
    ParsedDocument,
    PathTuple,
    read_text_file_with_encoding,
    resolve_output_encoding,
)


COMMENT_PREFIXES = ("#", ";", "//")
KEY_LIKE_RE = re.compile(r"^[A-Za-z0-9_.\-/:\[\] ]{1,64}$")


def _split_line_eol(full_line: str) -> Tuple[str, str]:
    body = full_line.rstrip("\r\n")
    eol = full_line[len(body) :]
    return body, eol


def _looks_like_kv_in_txt(left: str, sep: str) -> bool:
    """تحديد هل السطر في ملف .txt يمثل مفتاح=قيمة أم سطراً نصياً حراً."""
    s = left.strip()
    if not s:
        return False
    if sep == "\t":
        return True
    if " " not in s:
        return True
    if KEY_LIKE_RE.match(s) and len(s.split()) <= 3:
        return True
    return False


def _detect_line_sep(body: str, fmt: str) -> Optional[str]:
    """تحديد فاصل المفتاح/القيمة في السطر (\\t أو =) حسب صيغة الملف."""
    if fmt == "txt" and "\t" in body:
        left_tab, _ = body.split("\t", 1)
        if _looks_like_kv_in_txt(left_tab, "\t"):
            return "\t"
    if "=" in body:
        left_eq, _ = body.split("=", 1)
        if fmt == "ini" or _looks_like_kv_in_txt(left_eq, "="):
            return "="
    return None


def _unescape_ini_value(raw_val: str, quote: str) -> str:
    val = raw_val
    if quote == '"':
        val = val.replace('\\"', '"')
    elif quote == "'":
        val = val.replace("\\'", "'")
    return val.replace("\\r\\n", "\n").replace("\\n", "\n")


def _escape_ini_value(new_val: str, quote: str) -> str:
    # تحويل أسطر التحرير الفعلية إلى \n نصية حتى لا ينكسر سطر المفتاح الواحد
    encoded = new_val.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "")
    if quote == '"':
        encoded = encoded.replace('\\"', '"').replace('"', '\\"')
    elif quote == "'":
        encoded = encoded.replace("\\'", "'").replace("'", "\\'")
    return encoded


class IniFormatHandler(FormatHandler):
    """محلل وكاتب صيغتي INI و TXT دون فقدان التعليقات أو تنسيق الأقسام."""

    format_names = ("ini",)
    forced_format: Optional[str] = None

    def load(self, file_path: str) -> ParsedDocument:
        ext = os.path.splitext(file_path)[1].lower()
        fmt = self.forced_format or ("txt" if ext == ".txt" else "ini")

        raw_text, detected_enc = read_text_file_with_encoding(file_path)

        lines = raw_text.splitlines(keepends=True)
        entries: List[Tuple[PathTuple, str, str]] = []
        meta: Dict[int, Dict[str, str]] = {}
        current_section = ""

        treat_brackets_as_sections = fmt == "ini"
        if fmt == "txt":
            kv_lines = 0
            plain_lines = 0
            for full_line in lines:
                body, _ = _split_line_eol(full_line)
                stripped = body.strip()
                if not stripped or stripped.startswith(COMMENT_PREFIXES):
                    continue
                sec_part = stripped.split(";", 1)[0].split("#", 1)[0].strip()
                if sec_part.startswith("[") and sec_part.endswith("]") and "=" not in sec_part:
                    continue
                if _detect_line_sep(body, fmt) is not None:
                    kv_lines += 1
                else:
                    plain_lines += 1
            treat_brackets_as_sections = kv_lines > 0 and kv_lines >= plain_lines

        for line_idx, full_line in enumerate(lines):
            body, eol = _split_line_eol(full_line)
            stripped = body.strip()
            if not stripped or stripped.startswith(COMMENT_PREFIXES):
                continue

            if treat_brackets_as_sections:
                sec_part = stripped.split(";", 1)[0].split("#", 1)[0].strip()
                if sec_part.startswith("[") and sec_part.endswith("]") and "=" not in sec_part:
                    current_section = sec_part[1:-1].strip()
                    continue

            sep = _detect_line_sep(body, fmt)

            if sep is not None:
                sep_idx = body.find(sep)
                raw_left = body[:sep_idx]
                raw_right = body[sep_idx + 1 :]
                lead_ws_len = len(raw_right) - len(raw_right.lstrip(" \t"))
                lead_after_sep = raw_right[:lead_ws_len]
                val_part = raw_right[lead_ws_len:]

                quote = ""
                inner_raw = val_part
                if len(val_part) >= 2 and val_part[0] == val_part[-1] and val_part[0] in ('"', "'"):
                    quote = val_part[0]
                    inner_raw = val_part[1:-1]

                key_name = raw_left.strip()
                flat_key = f"{current_section}.{key_name}" if current_section else key_name
                decoded_val = _unescape_ini_value(inner_raw, quote)

                meta[line_idx] = {
                    "kind": "kv",
                    "prefix": raw_left + sep + lead_after_sep,
                    "quote": quote,
                    "eol": eol,
                }
                entries.append(((line_idx,), flat_key, decoded_val))
            elif fmt == "txt":
                lead_ws_len = len(body) - len(body.lstrip(" \t"))
                prefix = body[:lead_ws_len]
                inner_raw = body[lead_ws_len:]
                decoded_val = _unescape_ini_value(inner_raw, "")
                meta[line_idx] = {
                    "kind": "plain",
                    "prefix": prefix,
                    "quote": "",
                    "eol": eol,
                }
                entries.append(((line_idx,), f"line_{line_idx}", decoded_val))

        return ParsedDocument(
            format_name=fmt,
            raw_data={"lines": lines, "meta": meta, "encoding": detected_enc},
            entries=entries,
        )

    def save(
        self,
        file_path: str,
        doc: ParsedDocument,
        replacements: Dict[PathTuple, str],
    ) -> None:
        orig_lines: List[str] = doc.raw_data["lines"]
        meta: Dict[int, Dict[str, str]] = doc.raw_data["meta"]
        out_enc = resolve_output_encoding(str(doc.raw_data.get("encoding") or "utf-8"))
        new_lines = list(orig_lines)

        for path_tuple, new_val in replacements.items():
            if not path_tuple:
                continue
            line_idx = int(path_tuple[0])
            info = meta.get(line_idx)
            if not info or not (0 <= line_idx < len(new_lines)):
                continue
            quote = info["quote"]
            encoded = _escape_ini_value(new_val, quote)
            if info["kind"] == "kv":
                new_lines[line_idx] = f"{info['prefix']}{quote}{encoded}{quote}{info['eol']}"
            else:
                new_lines[line_idx] = f"{info['prefix']}{encoded}{info['eol']}"

        parent = os.path.dirname(os.path.abspath(file_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = file_path + ".tmp"

        with open(tmp_path, "w", encoding=out_enc, newline="") as f:
            f.write("".join(new_lines))
            f.flush()
            os.fsync(f.fileno())

        verify_enc = out_enc if out_enc.startswith("utf-16") else "utf-8-sig"
        with open(tmp_path, "r", encoding=verify_enc, newline="") as f:
            reloaded_lines = f.read().splitlines(keepends=True)
        if len(reloaded_lines) != len(orig_lines):
            raise ValueError(
                f"فشل التحقق من سلامة INI/TXT بعد الكتابة: عدد الأسطر ({len(reloaded_lines)} != {len(orig_lines)})."
            )

        os.replace(tmp_path, file_path)


class TxtFormatHandler(IniFormatHandler):
    """محلل وكاتب الملفات النصية السطرية (بما فيها الملفات النصية ذات الامتدادات المغايرة مثل .json/.bak) مع حفظ عدد الأسطر والترميز."""

    format_names = ("txt",)
    forced_format = "txt"
