# -*- coding: utf-8 -*-
"""محلل وكاتب ملفات YAML (.yml / .yaml) بلغة Python خالصة مع حفظ التعليقات والمسافات البادئة والاقتباسات."""

import os
import re
from typing import Dict, List, Optional, Tuple, Union

from arabic.protector import strip_directional_marks
from formats.base import (
    FormatHandler,
    ParsedDocument,
    PathTuple,
    read_text_file_with_encoding,
    resolve_output_encoding,
)


_YAML_NON_STRING_RE = re.compile(
    r"^(?:true|false|yes|no|on|off|null|~|[-+]?(?:\d+(?:\.\d+)?|0x[0-9a-fA-F]+))$",
    re.IGNORECASE,
)
_YAML_SPECIAL_START_CHARS = set("-?:[]{}&*!|>'\"%@`")
_BLOCK_INDICATOR_RE = re.compile(r"^([|>][+-]?)(?:\s+(#.*))?$")


def _split_line_eol(full_line: str) -> Tuple[str, str]:
    body = full_line.rstrip("\r\n")
    eol = full_line[len(body) :]
    return body, eol


def _count_indent(body: str) -> int:
    return len(body) - len(body.lstrip(" "))


def _split_yaml_key_value(text: str) -> Optional[Tuple[str, str, str]]:
    """فصل سطر YAML بصيغة 'key: value' خارج علامات الاقتباس؛ يعيد (key_part, sep_colon, rest)."""
    in_quote = ""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_quote:
            if in_quote == '"' and ch == "\\":
                i += 2
                continue
            if in_quote == "'" and ch == "'" and i + 1 < n and text[i + 1] == "'":
                i += 2
                continue
            if ch == in_quote:
                in_quote = ""
        else:
            if ch in ('"', "'"):
                in_quote = ch
            elif ch == "#" and (i == 0 or text[i - 1] in (" ", "\t")):
                return None
            elif ch == ":" and (i + 1 == n or text[i + 1] in (" ", "\t")):
                return text[:i], ":", text[i + 1 :]
        i += 1
    return None


def _unquote_key(raw_key: str) -> str:
    s = raw_key.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        inner = s[1:-1]
        if s[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner.replace("''", "'")
    return s


def _parse_yaml_scalar(raw_val: str) -> Optional[Tuple[str, str, str]]:
    """تحليل قيمة YAML أحادية السطر إلى (decoded_value, quote_char, trailing_suffix)."""
    if not raw_val:
        return None
    if raw_val[0] == '"':
        out: List[str] = []
        i = 1
        n = len(raw_val)
        while i < n:
            ch = raw_val[i]
            if ch == "\\" and i + 1 < n:
                nxt = raw_val[i + 1]
                esc_map = {'"': '"', "\\": "\\", "n": "\n", "r": "\r", "t": "\t", "/": "/"}
                if nxt in esc_map:
                    out.append(esc_map[nxt])
                elif nxt == "u" and i + 5 < n:
                    hex_part = raw_val[i + 2 : i + 6]
                    try:
                        out.append(chr(int(hex_part, 16)))
                        i += 6
                        continue
                    except ValueError:
                        out.append("\\u")
                else:
                    out.append("\\" + nxt)
                i += 2
                continue
            if ch == '"':
                return "".join(out), '"', raw_val[i + 1 :]
            out.append(ch)
            i += 1
        return raw_val[1:], '"', ""

    if raw_val[0] == "'":
        out_sq: List[str] = []
        i = 1
        n = len(raw_val)
        while i < n:
            ch = raw_val[i]
            if ch == "'" and i + 1 < n and raw_val[i + 1] == "'":
                out_sq.append("'")
                i += 2
                continue
            if ch == "'":
                return "".join(out_sq), "'", raw_val[i + 1 :]
            out_sq.append(ch)
            i += 1
        return raw_val[1:], "'", ""

    # غير مقتبس: فصل التعليق المضمن (#) والفراغات الطرفية
    comment_match = re.search(r"([ \t]+#.*)$", raw_val)
    if comment_match:
        val_core = raw_val[: comment_match.start()]
        suffix = comment_match.group(1)
    else:
        stripped_right = raw_val.rstrip(" \t")
        suffix = raw_val[len(stripped_right) :]
        val_core = stripped_right

    if not val_core:
        return None
    if val_core.startswith(("{", "[")) or _YAML_NON_STRING_RE.match(val_core):
        return None
    return val_core, "", suffix


def _needs_yaml_quoting(val: str) -> bool:
    if not val or val != val.strip():
        return True
    if "\n" in val or "\r" in val or "\t" in val:
        return True
    if val[0] in _YAML_SPECIAL_START_CHARS:
        return True
    if ": " in val or val.endswith(":") or " #" in val or val.startswith("#"):
        return True
    if _YAML_NON_STRING_RE.match(val):
        return True
    return False


def _format_yaml_scalar(new_val: str, orig_quote: str) -> str:
    clean = strip_directional_marks(new_val).replace("\r\n", "\n").replace("\r", "")
    quote = orig_quote
    if quote == "'" and ("\n" in clean or "\t" in clean):
        quote = '"'
    if quote == "" and _needs_yaml_quoting(clean):
        quote = '"'

    if quote == '"':
        escaped = (
            clean.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\t", "\\t")
            .replace("\n", "\\n")
        )
        return f'"{escaped}"'
    if quote == "'":
        escaped_sq = clean.replace("'", "''")
        return f"'{escaped_sq}'"
    return clean


class YamlFormatHandler(FormatHandler):
    """محلل وكاتب صيغتي YAML / YML بلغة Python خالصة دون فقدان البنية أو التعليقات."""

    format_names = ("yaml", "yml")

    def load(self, file_path: str) -> ParsedDocument:
        raw_text, detected_enc = read_text_file_with_encoding(file_path)
        lines = raw_text.splitlines(keepends=True)

        entries: List[Tuple[PathTuple, str, str]] = []
        meta: Dict[PathTuple, Dict[str, object]] = {}
        # (indent, key_or_idx, kind: "map" | "seq_item")
        scope_stack: List[Tuple[int, Union[str, int], str]] = []
        seq_counters: Dict[Tuple[Union[str, int], ...], int] = {}
        seen_paths: set = set()

        def _unique_path(base: PathTuple) -> PathTuple:
            if base not in seen_paths:
                seen_paths.add(base)
                return base
            n = 2
            while base + (f"#{n}",) in seen_paths:
                n += 1
            alt = base + (f"#{n}",)
            seen_paths.add(alt)
            return alt

        def _pop_for_indent(indent: int, is_seq_dash: bool = False) -> None:
            while scope_stack:
                top_ind, _, top_kind = scope_stack[-1]
                if top_ind > indent:
                    scope_stack.pop()
                elif top_ind == indent and (not is_seq_dash or top_kind == "seq_item"):
                    scope_stack.pop()
                else:
                    break

        def _read_block_scalar(start_idx: int, min_indent: int, indicator: str) -> Tuple[str, int, int]:
            """قراءة كتلة متعددة الأسطر (| أو >) وإرجاع (النص، آخر_سطر_مشمول، مسافة_البداية)."""
            block_lines: List[str] = []
            block_indent: Optional[int] = None
            idx = start_idx
            last_used = start_idx - 1

            while idx < len(lines):
                b_body, _ = _split_line_eol(lines[idx])
                if not b_body.strip():
                    block_lines.append("")
                    last_used = idx
                    idx += 1
                    continue
                cur_ind = _count_indent(b_body)
                if cur_ind <= min_indent:
                    break
                if block_indent is None:
                    block_indent = cur_ind
                eff_cut = min(cur_ind, block_indent)
                block_lines.append(b_body[eff_cut:])
                last_used = idx
                idx += 1

            # إزالة الأسطر الفارغة اللاحقة التي تتبع نهاية الكتلة
            while block_lines and block_lines[-1] == "":
                block_lines.pop()
                last_used -= 1

            if indicator.startswith(">"):
                joined = " ".join(ln.strip() if ln else "\n" for ln in block_lines)
                joined = re.sub(r" *\n *", "\n", joined).strip()
            else:
                joined = "\n".join(block_lines)

            return joined, max(last_used, start_idx - 1), (block_indent or (min_indent + 2))

        i = 0
        n_lines = len(lines)
        while i < n_lines:
            body, eol = _split_line_eol(lines[i])
            stripped = body.strip()

            if not stripped or stripped.startswith("#"):
                i += 1
                continue
            if stripped in ("---", "..."):
                scope_stack.clear()
                seq_counters.clear()
                i += 1
                continue

            indent = _count_indent(body)
            content = body[indent:]

            is_dash = content == "-" or content.startswith("- ") or content.startswith("-\t")
            _pop_for_indent(indent, is_seq_dash=is_dash)

            if is_dash:
                parent_path = tuple(f[1] for f in scope_stack)
                seq_idx = seq_counters.get(parent_path, 0)
                seq_counters[parent_path] = seq_idx + 1

                after_dash_raw = content[1:]
                after_dash_ws_len = len(after_dash_raw) - len(after_dash_raw.lstrip(" \t"))
                after_dash = after_dash_raw[after_dash_ws_len:]
                prefix_to_val = body[: indent + 1 + after_dash_ws_len]

                if not after_dash or after_dash.startswith("#"):
                    scope_stack.append((indent, seq_idx, "seq_item"))
                    i += 1
                    continue

                kv_in_list = _split_yaml_key_value(after_dash)
                if kv_in_list is not None:
                    scope_stack.append((indent, seq_idx, "seq_item"))
                    eff_col = indent + 1 + after_dash_ws_len
                    key_part, colon_part, rest_after = kv_in_list
                    key_name = _unquote_key(key_part)
                    lead_ws_len = len(rest_after) - len(rest_after.lstrip(" \t"))
                    val_raw = rest_after[lead_ws_len:]
                    full_prefix = prefix_to_val + key_part + colon_part + rest_after[:lead_ws_len]

                    path_tuple = _unique_path(tuple(f[1] for f in scope_stack) + (key_name,))
                    flat_key = ".".join(map(str, path_tuple))
                    block_m = _BLOCK_INDICATOR_RE.match(val_raw.strip()) if val_raw.strip() else None
                    if not val_raw.strip() or val_raw.lstrip().startswith("#"):
                        scope_stack.append((eff_col, key_name, "map"))
                        i += 1
                        continue
                    if block_m:
                        block_text, end_idx, b_ind = _read_block_scalar(i + 1, eff_col, block_m.group(1))
                        if end_idx >= i + 1:
                            meta[path_tuple] = {
                                "kind": "block",
                                "start_line": i,
                                "end_line": end_idx,
                                "header_line": lines[i],
                                "block_indent": b_ind,
                                "eol": eol or "\n",
                            }
                            entries.append((path_tuple, flat_key, block_text))
                            i = end_idx + 1
                            continue
                    scalar = _parse_yaml_scalar(val_raw)
                    if scalar is not None:
                        decoded_val, quote_ch, suffix = scalar
                        meta[path_tuple] = {
                            "kind": "scalar",
                            "line_idx": i,
                            "prefix": full_prefix,
                            "quote": quote_ch,
                            "suffix": suffix,
                            "eol": eol,
                        }
                        entries.append((path_tuple, flat_key, decoded_val))
                    i += 1
                    continue

                # عنصر قائمة نصي مباشر (- "Potion")
                path_tuple = _unique_path(parent_path + (seq_idx,))
                flat_key = ".".join(map(str, path_tuple))
                block_m = _BLOCK_INDICATOR_RE.match(after_dash.strip())
                if block_m:
                    block_text, end_idx, b_ind = _read_block_scalar(i + 1, indent, block_m.group(1))
                    if end_idx >= i + 1:
                        meta[path_tuple] = {
                            "kind": "block",
                            "start_line": i,
                            "end_line": end_idx,
                            "header_line": lines[i],
                            "block_indent": b_ind,
                            "eol": eol or "\n",
                        }
                        entries.append((path_tuple, flat_key, block_text))
                        i = end_idx + 1
                        continue

                scalar = _parse_yaml_scalar(after_dash)
                if scalar is not None:
                    decoded_val, quote_ch, suffix = scalar
                    meta[path_tuple] = {
                        "kind": "scalar",
                        "line_idx": i,
                        "prefix": prefix_to_val,
                        "quote": quote_ch,
                        "suffix": suffix,
                        "eol": eol,
                    }
                    entries.append((path_tuple, flat_key, decoded_val))
                i += 1
                continue

            kv = _split_yaml_key_value(content)
            if kv is None:
                i += 1
                continue

            key_part, colon_part, rest_after = kv
            key_name = _unquote_key(key_part)
            lead_ws_len = len(rest_after) - len(rest_after.lstrip(" \t"))
            val_raw = rest_after[lead_ws_len:]
            full_prefix = body[:indent] + key_part + colon_part + rest_after[:lead_ws_len]

            if not val_raw.strip() or val_raw.lstrip().startswith("#"):
                scope_stack.append((indent, key_name, "map"))
                i += 1
                continue

            path_tuple = _unique_path(tuple(f[1] for f in scope_stack) + (key_name,))
            flat_key = ".".join(map(str, path_tuple))
            block_m = _BLOCK_INDICATOR_RE.match(val_raw.strip())
            if block_m:
                block_text, end_idx, b_ind = _read_block_scalar(i + 1, indent, block_m.group(1))
                if end_idx >= i + 1:
                    meta[path_tuple] = {
                        "kind": "block",
                        "start_line": i,
                        "end_line": end_idx,
                        "header_line": lines[i],
                        "block_indent": b_ind,
                        "eol": eol or "\n",
                    }
                    entries.append((path_tuple, flat_key, block_text))
                    i = end_idx + 1
                    continue

            scalar = _parse_yaml_scalar(val_raw)
            if scalar is not None:
                decoded_val, quote_ch, suffix = scalar
                meta[path_tuple] = {
                    "kind": "scalar",
                    "line_idx": i,
                    "prefix": full_prefix,
                    "quote": quote_ch,
                    "suffix": suffix,
                    "eol": eol,
                }
                entries.append((path_tuple, flat_key, decoded_val))
            i += 1

        return ParsedDocument(
            format_name="yaml",
            raw_data={"lines": lines, "meta": meta, "encoding": detected_enc},
            entries=entries,
        )

    def save(
        self,
        file_path: str,
        doc: ParsedDocument,
        replacements: Dict[PathTuple, str],
    ) -> None:
        orig_lines: List[str] = list(doc.raw_data["lines"])
        meta: Dict[PathTuple, Dict[str, object]] = doc.raw_data["meta"]
        out_enc = resolve_output_encoding(str(doc.raw_data.get("encoding") or "utf-8"))

        # تطبيق التعديلات (بما فيها الكتل متعددة الأسطر) من الأسفل للأعلى لتفادي إزاحة الأسطر
        block_edits: List[Tuple[int, int, List[str]]] = []
        for path_tuple, new_val in replacements.items():
            info = meta.get(path_tuple)
            if not info:
                continue
            if info["kind"] == "scalar":
                line_idx = int(info["line_idx"])
                if not (0 <= line_idx < len(orig_lines)):
                    continue
                formatted = _format_yaml_scalar(new_val, str(info["quote"]))
                orig_lines[line_idx] = (
                    f"{info['prefix']}{formatted}{info['suffix']}{info['eol']}"
                )
            elif info["kind"] == "block":
                start_line = int(info["start_line"])
                end_line = int(info["end_line"])
                b_ind = int(info["block_indent"])
                eol = str(info["eol"] or "\n")
                clean_block = strip_directional_marks(new_val).replace("\r\n", "\n").replace("\r", "")
                hdr_line = str(info["header_line"])
                # تحويل > إلى | عند الكتابة للحفاظ الدقيق على الأسطر العربية دون إعادة التفاف
                hdr_body, hdr_eol = _split_line_eol(hdr_line)
                hdr_fixed = re.sub(r":\s*>[+-]?", ": |", hdr_body, count=1) + (hdr_eol or eol)
                pad = " " * b_ind
                body_lines = [
                    (f"{pad}{ln}{eol}" if ln else eol)
                    for ln in clean_block.split("\n")
                ]
                block_edits.append((start_line, end_line, [hdr_fixed] + body_lines))

        for start_line, end_line, replacement_lines in sorted(block_edits, key=lambda x: x[0], reverse=True):
            orig_lines[start_line : end_line + 1] = replacement_lines

        parent = os.path.dirname(os.path.abspath(file_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = file_path + ".tmp"

        with open(tmp_path, "w", encoding=out_enc, newline="") as f:
            f.write("".join(orig_lines))
            f.flush()
            os.fsync(f.fileno())

        reloaded = self.load(tmp_path)
        if len(reloaded.entries) != len(doc.entries):
            raise ValueError(
                f"فشل التحقق من سلامة YAML بعد الكتابة: عدد المدخلات ({len(reloaded.entries)} != {len(doc.entries)})."
            )

        os.replace(tmp_path, file_path)
