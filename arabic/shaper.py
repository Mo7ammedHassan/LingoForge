# -*- coding: utf-8 -*-
"""تشبيك الحروف العربية (Presentation Forms-B) والترتيب البصري (Visual BiDi) لمحركات LTR."""

import re
from typing import Dict, List, Optional, Tuple


ARABIC_FORMS: Dict[str, Tuple[str, Optional[str], Optional[str], Optional[str]]] = {
    "\u0621": ("\uFE80", None, None, None),              # ء
    "\u0622": ("\uFE81", "\uFE82", None, None),          # آ
    "\u0623": ("\uFE83", "\uFE84", None, None),          # أ
    "\u0624": ("\uFE85", "\uFE86", None, None),          # ؤ
    "\u0625": ("\uFE87", "\uFE88", None, None),          # إ
    "\u0626": ("\uFE89", "\uFE8A", "\uFE8B", "\uFE8C"),  # ئ
    "\u0627": ("\uFE8D", "\uFE8E", None, None),          # ا
    "\u0628": ("\uFE8F", "\uFE90", "\uFE91", "\uFE92"),  # ب
    "\u0629": ("\uFE93", "\uFE94", None, None),          # ة
    "\u062A": ("\uFE95", "\uFE96", "\uFE97", "\uFE98"),  # ت
    "\u062B": ("\uFE99", "\uFE9A", "\uFE9B", "\uFE9C"),  # ث
    "\u062C": ("\uFE9D", "\uFE9E", "\uFE9F", "\uFEA0"),  # ج
    "\u062D": ("\uFEA1", "\uFEA2", "\uFEA3", "\uFEA4"),  # ح
    "\u062E": ("\uFEA5", "\uFEA6", "\uFEA7", "\uFEA8"),  # خ
    "\u062F": ("\uFEA9", "\uFEAA", None, None),          # د
    "\u0630": ("\uFEAB", "\uFEAC", None, None),          # ذ
    "\u0631": ("\uFEAD", "\uFEAE", None, None),          # ر
    "\u0632": ("\uFEAF", "\uFEB0", None, None),          # ز
    "\u0633": ("\uFEB1", "\uFEB2", "\uFEB3", "\uFEB4"),  # س
    "\u0634": ("\uFEB5", "\uFEB6", "\uFEB7", "\uFEB8"),  # ش
    "\u0635": ("\uFEB9", "\uFEBA", "\uFEBB", "\uFEBC"),  # ص
    "\u0636": ("\uFEBD", "\uFEBE", "\uFEBF", "\uFEC0"),  # ض
    "\u0637": ("\uFEC1", "\uFEC2", "\uFEC3", "\uFEC4"),  # ط
    "\u0638": ("\uFEC5", "\uFEC6", "\uFEC7", "\uFEC8"),  # ظ
    "\u0639": ("\uFEC9", "\uFECA", "\uFECB", "\uFECC"),  # ع
    "\u063A": ("\uFECD", "\uFECE", "\uFECF", "\uFED0"),  # غ
    "\u0640": ("\u0640", "\u0640", "\u0640", "\u0640"),  # ـ
    "\u0641": ("\uFED1", "\uFED2", "\uFED3", "\uFED4"),  # ف
    "\u0642": ("\uFED5", "\uFED6", "\uFED7", "\uFED8"),  # ق
    "\u0643": ("\uFED9", "\uFEDA", "\uFEDB", "\uFEDC"),  # ك
    "\u0644": ("\uFEDD", "\uFEDE", "\uFEDF", "\uFEE0"),  # ل
    "\u0645": ("\uFEE1", "\uFEE2", "\uFEE3", "\uFEE4"),  # م
    "\u0646": ("\uFEE5", "\uFEE6", "\uFEE7", "\uFEE8"),  # ن
    "\u0647": ("\uFEE9", "\uFEEA", "\uFEEB", "\uFEEC"),  # ه
    "\u0648": ("\uFEED", "\uFEEE", None, None),          # و
    "\u0649": ("\uFEEF", "\uFEF0", None, None),          # ى
    "\u064A": ("\uFEF1", "\uFEF2", "\uFEF3", "\uFEF4"),  # ي
}

LAM_ALEF_LIGATURES: Dict[str, Tuple[str, Optional[str], Optional[str], Optional[str]]] = {
    "\u0622": ("\uFEF5", "\uFEF6", None, None),  # لآ
    "\u0623": ("\uFEF7", "\uFEF8", None, None),  # لأ
    "\u0625": ("\uFEF9", "\uFEFA", None, None),  # لإ
    "\u0627": ("\uFEFB", "\uFEFC", None, None),  # لا
}

STRIP_CHARS_RE = re.compile(r"[\u064B-\u065F\u0670\u200B-\u200F]")
ARABIC_ANY_RE = re.compile(r"[\u0600-\u06FF\uFE70-\uFEFF]")
PAIRED_TAG_RE = re.compile(
    r"<([a-zA-Z][a-zA-Z0-9_.-]*)(?:(?:=[^>]*)?|(?:\s+[^>]*))(?<!/)>(.*?)</\1>"
)
UNREAL_PAIRED_TAG_RE = re.compile(
    r"<([a-zA-Z][a-zA-Z0-9_.-]*)(?:(?:=[^>]*)?|(?:\s+[^>]*))(?<!/)>(.*?)</>"
)
PAIRED_BBCODE_RE = re.compile(
    r"\[([a-zA-Z][a-zA-Z0-9_]*)(?:=[^\]\n]+|\s+[a-zA-Z_][a-zA-Z0-9_]*=[^\]\n]+)?\](.*?)\[/\1\]"
)
_BBCODE_SINGLE_RE = (
    r"\[(?:/[a-zA-Z][a-zA-Z0-9_]*|[a-zA-Z][a-zA-Z0-9_]*(?:=[^\]\n]+|\s+[a-zA-Z_][a-zA-Z0-9_]*=[^\]\n]+)|"
    r"(?:b|i|u|s|code|center|left|right|fill|indent|url|img|font|table|cell|ul|ol|wave|pulse|tornado|shake|fade|rainbow|lb|rb|p|br|hr))\]"
)
_UE_PLURAL_OR_VAR_RE = (
    r"\{[A-Za-z0-9_]+(?::[^}\n]+)?\}\|(?:plural|gender|ordinal|hpp)\([^)\n]+\)|"
    r"\{[A-Za-z0-9_]+(?::[^}\n]+)?\}"
)
ATOMIC_TAG_OR_VAR_RE = re.compile(
    rf"(\uE000TAG\d+\uE001|{_UE_PLURAL_OR_VAR_RE}|<[^>]+>|{_BBCODE_SINGLE_RE}|%(?:(?:[0-9]+\$)?[-+0#]*[0-9]*(?:\.[0-9]+)?[sdifuxXoOgGc%]|[0-9]+)|\\n[cr]?<[^>\n]+>|\\(?:c|C)\[#[0-9A-Fa-f]{{3,8}}\]|\\n|\\t)"
)
COMBINED_LTR_RE = re.compile(
    rf"(\uE000TAG\d+\uE001|{_UE_PLURAL_OR_VAR_RE}|<[^>]+>|{_BBCODE_SINGLE_RE}|%(?:(?:[0-9]+\$)?[-+0#]*[0-9]*(?:\.[0-9]+)?[sdifuxXoOgGc%]|[0-9]+)|\\n[cr]?<[^>\n]+>|\\(?:c|C)\[#[0-9A-Fa-f]{{3,8}}\]|\\n|\\t|[A-Za-z0-9\u2070-\u209F\u00B2\u00B3\u00B9]+)"
)

MIRROR_MAP = str.maketrans({
    "(": ")",
    ")": "(",
    "[": "]",
    "]": "[",
    "«": "»",
    "»": "«",
    "“": "”",
    "”": "“",
})


def shape_arabic_chars(text: str) -> str:
    """تحويل الحروف العربية إلى أشكالها المتصلة (Presentation Forms-B) مع دمج لام ألف."""
    text = STRIP_CHARS_RE.sub("", text)
    tokens: List[Tuple[str, Optional[Tuple[str, Optional[str], Optional[str], Optional[str]]]]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\u0644" and i + 1 < n and text[i + 1] in LAM_ALEF_LIGATURES:
            tokens.append((ch + text[i + 1], LAM_ALEF_LIGATURES[text[i + 1]]))
            i += 2
        elif ch in ARABIC_FORMS:
            tokens.append((ch, ARABIC_FORMS[ch]))
            i += 1
        else:
            tokens.append((ch, None))
            i += 1

    out: List[str] = []
    m = len(tokens)
    for idx, (raw, forms) in enumerate(tokens):
        if forms is None:
            out.append(raw)
            continue

        prev_connects_left = idx > 0 and tokens[idx - 1][1] is not None and tokens[idx - 1][1][2] is not None
        next_connects_right = idx + 1 < m and tokens[idx + 1][1] is not None and tokens[idx + 1][1][1] is not None

        iso, fin, ini, med = forms
        conn_right = prev_connects_left and fin is not None
        conn_left = next_connects_right and ini is not None

        if conn_right and conn_left and med is not None:
            out.append(med)
        elif conn_right and fin is not None:
            out.append(fin)
        elif conn_left and ini is not None:
            out.append(ini)
        else:
            out.append(iso)

    return "".join(out)


def _reverse_rtl_segment(seg: str) -> str:
    return seg.translate(MIRROR_MAP)[::-1]


def _split_and_merge_ltr(shaped_line: str) -> List[Tuple[str, str]]:
    """تقسيم السطر لوحدات LTR و RTL مع دمج الكلمات الإنجليزية المتجاورة."""
    tokens: List[Tuple[str, str]] = []
    pos = 0
    for m in COMBINED_LTR_RE.finditer(shaped_line):
        if m.start() > pos:
            tokens.append(("rtl", shaped_line[pos : m.start()]))
        tokens.append(("ltr", m.group(0)))
        pos = m.end()
    if pos < len(shaped_line):
        tokens.append(("rtl", shaped_line[pos:]))

    merged: List[Tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        kind, val = tokens[i]
        if kind == "ltr":
            buf = val
            j = i + 1
            while j + 1 < len(tokens):
                mid_kind, mid_val = tokens[j]
                nxt_kind, nxt_val = tokens[j + 1]
                if (
                    mid_kind == "rtl"
                    and nxt_kind == "ltr"
                    and not ARABIC_ANY_RE.search(mid_val)
                    and re.fullmatch(r"[ \t\-/:.+×&_']+", mid_val)
                ):
                    buf += mid_val + nxt_val
                    j += 2
                else:
                    break
            merged.append(("ltr", buf))
            i = j
        else:
            merged.append((kind, val))
            i += 1
    return merged


def bidi_line_visual(line: str) -> str:
    """تشكيل سطر واحد وترتيبه بصرياً من اليسار لليمين لعرضه في محركات LTR."""
    if not line:
        return line

    cleaned = STRIP_CHARS_RE.sub("", line)
    if not ARABIC_ANY_RE.search(cleaned):
        return cleaned

    tag_placeholders: List[str] = []

    def _replace_paired(match: re.Match) -> str:
        full_open = match.group(0)[: match.group(0).index(">") + 1]
        tag_name = match.group(1)
        inner = match.group(2)
        processed_inner = bidi_line_visual(inner)
        idx = len(tag_placeholders)
        tag_placeholders.append(f"{full_open}{processed_inner}</{tag_name}>")
        return f"\uE000TAG{idx}\uE001"

    def _replace_paired_unreal(match: re.Match) -> str:
        full_open = match.group(0)[: match.group(0).index(">") + 1]
        inner = match.group(2)
        processed_inner = bidi_line_visual(inner)
        idx = len(tag_placeholders)
        tag_placeholders.append(f"{full_open}{processed_inner}</>")
        return f"\uE000TAG{idx}\uE001"

    def _replace_paired_bbcode(match: re.Match) -> str:
        full_open = match.group(0)[: match.group(0).index("]") + 1]
        tag_name = match.group(1)
        inner = match.group(2)
        processed_inner = bidi_line_visual(inner)
        idx = len(tag_placeholders)
        tag_placeholders.append(f"{full_open}{processed_inner}[/{tag_name}]")
        return f"\uE000TAG{idx}\uE001"

    work = cleaned
    while True:
        new_work = PAIRED_TAG_RE.sub(_replace_paired, work)
        new_work = UNREAL_PAIRED_TAG_RE.sub(_replace_paired_unreal, new_work)
        new_work = PAIRED_BBCODE_RE.sub(_replace_paired_bbcode, new_work)
        if new_work == work:
            break
        work = new_work

    parts = ATOMIC_TAG_OR_VAR_RE.split(work)
    shaped_parts = [
        p if idx % 2 == 1 else shape_arabic_chars(p) for idx, p in enumerate(parts)
    ]
    shaped_line = "".join(shaped_parts)

    merged = _split_and_merge_ltr(shaped_line)
    visual_chunks = [
        val if kind == "ltr" else _reverse_rtl_segment(val)
        for kind, val in reversed(merged)
    ]
    result = "".join(visual_chunks)

    for idx, rendered_tag in enumerate(tag_placeholders):
        result = result.replace(f"\uE000TAG{idx}\uE001", rendered_tag)
    return result


def process_text(text: str) -> str:
    """معالجة نص كامل (سطر واحد أو متعدد الأسطر) بالتشبيك والترتيب البصري."""
    if not isinstance(text, str) or not text:
        return text

    m_full = re.fullmatch(
        r"(<([a-zA-Z][a-zA-Z0-9_.-]*)(?:(?:=[^>]*)|(?:\s+[^>]*))(?<!/)>)(.*)(</\2>)",
        text,
        re.DOTALL,
    )
    if m_full and "\n" in m_full.group(3):
        open_tag, _, inner, close_tag = m_full.groups()
        return f"{open_tag}{process_text(inner)}{close_tag}"

    m_ue_full = re.fullmatch(
        r"(<([a-zA-Z][a-zA-Z0-9_.-]*)(?:(?:=[^>]*)|(?:\s+[^>]*))(?<!/)>)(.*)(</>)",
        text,
        re.DOTALL,
    )
    if m_ue_full and "\n" in m_ue_full.group(3):
        open_tag, _, inner, close_tag = m_ue_full.groups()
        return f"{open_tag}{process_text(inner)}{close_tag}"

    m_bb_full = re.fullmatch(
        r"(\[([a-zA-Z][a-zA-Z0-9_]*)(?:=[^\]\n]+|\s+[a-zA-Z_][a-zA-Z0-9_]*=[^\]\n]+)?\])(.*)(\[/\2\])",
        text,
        re.DOTALL,
    )
    if m_bb_full and "\n" in m_bb_full.group(3):
        open_tag, _, inner, close_tag = m_bb_full.groups()
        return f"{open_tag}{process_text(inner)}{close_tag}"

    parts = re.split(r"(\r?\n)", text)
    out = [
        part if idx % 2 == 1 else bidi_line_visual(part)
        for idx, part in enumerate(parts)
    ]
    return "".join(out)
