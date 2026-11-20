# -*- coding: utf-8 -*-
"""عزل المتغيرات والوسوم قبل الترجمة مع الإصلاح الذاتي والتحقق الصارم بعدها."""

from dataclasses import dataclass
import re
from typing import List, Optional, Tuple


BBCODE_PARAM_TAGS = (
    "b|i|u|s|code|center|left|right|fill|indent|url|img|font|"
    "table|cell|ul|ol|wave|pulse|tornado|shake|fade|rainbow|lb|rb|p|br|hr"
)

PLACEHOLDER_RE = re.compile(
    rf"\{{[A-Za-z0-9_]+(?::[^}}\n]+)?\}}\|(?:plural|gender|ordinal|hpp)\([^)\n]+\)|"
    rf"\{{[A-Za-z0-9_]+(?::[^}}\n]+)?\}}|<[^>]+>|"
    rf"\[(?:/[a-zA-Z][a-zA-Z0-9_]*|[a-zA-Z][a-zA-Z0-9_]*(?:=[^\]\n]+|\s+[a-zA-Z_][a-zA-Z0-9_]*=[^\]\n]+)|(?:{BBCODE_PARAM_TAGS}))\]|"
    rf"%(?:(?:[0-9]+\$)?[-+0#]*[0-9]*(?:\.[0-9]+)?[sdifuxXoOgGc%]|[0-9]+)|"
    rf"\\n[cr]?<[^>\n]+>|\\(?:c|C)\[#[0-9A-Fa-f]{{3,8}}\]|"
    rf"\\(?:[A-Za-z]+\[\d+\]|[\{{}}.|!<>^$Gg])|\t|\\n|\\t"
)
ARABIC_DIGITS_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
DIRECTIONAL_MARKS_RE = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2066-\u2069\u061C\uFEFF]")


def strip_directional_marks(text: str) -> str:
    """إزالة علامات الاتجاه الخفية التي قد يحقنها المترجم الآلي داخل النصوص أو الأقواس."""
    return DIRECTIONAL_MARKS_RE.sub("", text) if text else text


@dataclass
class MaskedLine:
    """تمثيل سطر بعد عزل المسافات الطرفية والمتغيرات البرمجية."""

    lead_ws: str
    masked: str
    trail_ws: str
    placeholders: List[str]


def mask_line(line: str) -> MaskedLine:
    """استخراج المسافات الطرفية واستبدال المتغيرات والوسوم برموز [[[i]]]."""
    m = re.match(r"^(\s*)(.*?)(\s*)$", line)
    lead_ws, core, trail_ws = m.group(1), m.group(2), m.group(3)
    placeholders: List[str] = []

    def _repl(match: re.Match) -> str:
        placeholders.append(match.group(0))
        return f"[[[{len(placeholders) - 1}]]]"

    masked = PLACEHOLDER_RE.sub(_repl, core)
    return MaskedLine(lead_ws=lead_ws, masked=masked, trail_ws=trail_ws, placeholders=placeholders)


def _normalize_corrupted_tokens(
    text: str,
    expected_count: Optional[int] = None,
) -> Tuple[str, int]:
    """إصلاح الأرقام العربية أو الأقواس الصينية/المتباعدة داخل رموز الحماية دون المساس بـ [1] العادية."""
    repairs = 0
    cleaned = strip_directional_marks(text)

    token_pat = re.compile(
        r"([\[【]+(?:\s*[\[【]+)*)\s*([0-9\u0660-\u0669\u06F0-\u06F9]+)\s*((?:\]|\】)+(?:\s*(?:\]|\】)+)*)"
    )

    def _has_multi_bracket_token(idx_str: str) -> bool:
        multi_pat = re.compile(
            rf"(?:[\[【]\s*){{2,}}{re.escape(idx_str)}(?:\s*[\]】]){{2,}}"
        )
        return bool(multi_pat.search(cleaned))

    def _fix_brackets(match: re.Match) -> str:
        nonlocal repairs
        raw = match.group(0)
        left_raw, num_raw, right_raw = match.group(1), match.group(2), match.group(3)
        normalized_digits = num_raw.translate(ARABIC_DIGITS_MAP)
        idx = int(normalized_digits)

        if expected_count is not None and not (0 <= idx < expected_count):
            return raw

        left_count = left_raw.count("[") + left_raw.count("【")
        right_count = right_raw.count("]") + right_raw.count("】")
        has_cjk_bracket = ("【" in left_raw) or ("】" in right_raw)

        # منع تحويل الأقواس المفردة العادية [1] إلا إذا كان الرمز المقصود مفقوداً تماماً
        if not has_cjk_bracket and left_count == 1 and right_count == 1:
            if expected_count is None or _has_multi_bracket_token(str(idx)):
                return raw

        canonical = f"[[[{idx}]]]"
        if raw != canonical:
            repairs += 1
        return canonical

    fixed = token_pat.sub(_fix_brackets, cleaned)
    return fixed, repairs


def unmask_line(
    translated_core: str,
    placeholders: List[str],
    lead_ws: str,
    trail_ws: str,
) -> Tuple[str, int]:
    """استعادة المتغيرات البرمجية وإصلاح أي تشويه من المترجم آلياً."""
    if not placeholders:
        cleaned = strip_directional_marks(translated_core)
        return f"{lead_ws}{cleaned}{trail_ws}", 0

    result, repairs = _normalize_corrupted_tokens(
        translated_core, expected_count=len(placeholders)
    )

    for idx, orig_val in enumerate(placeholders):
        exact_token = f"[[[{idx}]]]"
        if exact_token in result:
            result = result.replace(exact_token, orig_val, 1)
            continue

        # محاولة ثانية: أقواس بأي تباعد لم تلتقطها المرحلة الأولى
        loose_pat = re.compile(rf"(?:\[|\【)+\s*{idx}\s*(?:\]|\】)+")
        if loose_pat.search(result):
            result = loose_pat.sub(lambda _, v=orig_val: v, result, count=1)
            repairs += 1
            continue

        # خط دفاع أخير: إذا حذف المترجم الرمز بالكامل، نعيد إلحاق المتغير بأمان
        repairs += 1
        if orig_val.startswith(("</", "[/")):
            result = f"{result}{orig_val}"
        elif orig_val in ("\t", "\\n", "\\t"):
            result = f"{result}{orig_val}"
        else:
            result = f"{result} {orig_val}".strip()

    return f"{lead_ws}{result}{trail_ws}", repairs


def verify_placeholders(placeholders: List[str], final_text: str) -> List[str]:
    """التحقق من وجود كافة المتغيرات الأصلية بنفس التكرار في النص النهائي."""
    missing: List[str] = []
    remaining = final_text
    for val in placeholders:
        pos = remaining.find(val)
        if pos == -1:
            missing.append(val)
        else:
            remaining = remaining[:pos] + remaining[pos + len(val) :]
    return missing
