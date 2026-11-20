# -*- coding: utf-8 -*-
"""كاشف محرك Godot Engine (3.x و 4.x) عبر تحليل .pck و .exe و project.godot ومعمارية PE."""

import os
from typing import List, Optional, Tuple

from core.models import EngineInfo
from engines.godot.pck_parser import (
    PckInfo,
    parse_pck_header_and_entries,
    parse_project_godot_text,
)
from engines.unity.detector import read_pe_architecture


_IGNORED_GODOT_EXE_TOKENS = ("crashhandler", "unins")
_CONTAINER_SCAN_SKIP_DIRS = {
    ".arabic_translator",
    "arabic_l10n",
    ".git",
    ".godot",
    ".import",
    "__pycache__",
    "addons",
}


def _is_godot_game_exe_name(filename: str) -> bool:
    low = filename.lower()
    return low.endswith(".exe") and not any(tok in low for tok in _IGNORED_GODOT_EXE_TOKENS)


def resolve_godot_game_root(
    path_in_game: str,
    explicit_game_dir: Optional[str] = None,
) -> str:
    """تحديد الجذر الفعلي للعبة/مشروع Godot انطلاقاً من مسار .pck/.exe أو الجذر الصريح."""
    if explicit_game_dir and explicit_game_dir.strip():
        return os.path.abspath(explicit_game_dir)

    abs_path = os.path.abspath(path_in_game)
    is_dir = os.path.isdir(abs_path)
    cur = abs_path if is_dir else os.path.dirname(abs_path)
    base_name = "" if is_dir else os.path.basename(abs_path)
    pck_stem = os.path.splitext(base_name)[0].lower() if base_name else ""
    is_exe_container = base_name.lower().endswith(".exe")

    ancestors: List[str] = []
    probe = cur
    for _ in range(6):
        ancestors.append(probe)
        parent = os.path.dirname(probe)
        if not parent or parent == probe:
            break
        probe = parent

    # 1. فحص المجلد المباشر للحزمة أولاً (الأولوية للأقرب)
    if is_exe_container or os.path.isdir(os.path.join(cur, ".arabic_translator")):
        return cur
    try:
        cur_entries = os.listdir(cur)
    except OSError:
        cur_entries = []
    for name in cur_entries:
        low = name.lower()
        full = os.path.join(cur, name)
        if os.path.isfile(full) and (
            low in ("project.godot", "override.cfg") or _is_godot_game_exe_name(name)
        ):
            return cur

    # 2. البحث في الأسلاف بترتيب الأقرب أولاً دون السماح لـ .arabic_translator أبعد بتجاوز جذر أقرب
    for anc in ancestors[1:]:
        if os.path.isdir(os.path.join(anc, ".arabic_translator")):
            return anc
        try:
            entries = os.listdir(anc)
        except OSError:
            continue
        for name in entries:
            low = name.lower()
            full = os.path.join(anc, name)
            if not os.path.isfile(full):
                continue
            if low in ("project.godot", "override.cfg"):
                return anc
            if pck_stem and _is_godot_game_exe_name(name):
                if os.path.splitext(low)[0] == pck_stem:
                    return anc

    return cur


def find_godot_containers(game_dir: str) -> Tuple[List[str], List[str], Optional[str]]:
    """البحث عن ملفات .pck المستقلة و .exe المحتملة و project.godot داخل مجلد اللعبة."""
    abs_dir = os.path.abspath(game_dir)
    if not os.path.isdir(abs_dir):
        return [], [], None

    proj_candidate = os.path.join(abs_dir, "project.godot")
    project_godot: Optional[str] = proj_candidate if os.path.isfile(proj_candidate) else None
    has_root_override = os.path.isfile(os.path.join(abs_dir, "override.cfg"))

    top_pcks: List[str] = []
    top_exes: List[str] = []
    try:
        top_entries = sorted(os.listdir(abs_dir))
    except OSError:
        return [], [], project_godot

    for name in top_entries:
        low = name.lower()
        full = os.path.join(abs_dir, name)
        if not os.path.isfile(full):
            continue
        if low.endswith(".pck") and low != "arabic_patch.pck":
            top_pcks.append(full)
        elif _is_godot_game_exe_name(name):
            top_exes.append(full)

    def _sort_exes(exes: List[Tuple[int, str]], pcks: List[str]) -> List[str]:
        pck_stems = {os.path.splitext(os.path.basename(p))[0].lower() for p in pcks}

        # ترتيب ملفات .exe لتفضيل الملف الأقرب للجذر، غير الطرفي، والمطابق لاسم .pck
        def _exe_rank(item: Tuple[int, str]) -> Tuple[int, int, int, str]:
            depth, path = item
            base = os.path.splitext(os.path.basename(path))[0].lower()
            is_console = 1 if "console" in base else 0
            matches_pck = 0 if base in pck_stems else 1
            return (depth, is_console, matches_pck, base)

        return [p for _, p in sorted(exes, key=_exe_rank)]

    # 1. مشروع غير مصدّر (project.godot في الجذر): عدم النزول للمجلدات الفرعية لتفادي التصدير المتداخل
    if project_godot is not None:
        return top_pcks, _sort_exes([(0, e) for e in top_exes], top_pcks), project_godot

    # 2. لعبة مصدّرة مكتملة في الجذر (.pck أو .exe مدمج بـ GDPC في المستوى الأعلى)
    has_top_embedded = any(parse_pck_header_and_entries(e) is not None for e in top_exes)
    if top_pcks or has_top_embedded:
        return top_pcks, _sort_exes([(0, e) for e in top_exes], top_pcks), None

    # 3. مسح محدود للمجلدات الفرعية لدعم التخطيطات المقسمة مع استبعاد الألعاب/المشاريع المستقلة المتداخلة
    pck_candidates: List[Tuple[int, str]] = []
    exe_candidates: List[Tuple[int, str]] = [(0, e) for e in top_exes]
    max_depth = 3

    for root, dirs, files in os.walk(abs_dir):
        rel = os.path.relpath(root, abs_dir)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth == 0:
            dirs[:] = [
                d for d in sorted(dirs)
                if d.lower() not in _CONTAINER_SCAN_SKIP_DIRS
            ]
            continue

        dir_pcks: List[str] = []
        dir_exes: List[str] = []
        has_nested_root_marker = False
        for name in sorted(files):
            low = name.lower()
            full = os.path.join(root, name)
            if not os.path.isfile(full):
                continue
            if low in ("project.godot", "override.cfg"):
                has_nested_root_marker = True
                break
            if low.endswith(".pck") and low != "arabic_patch.pck":
                dir_pcks.append(full)
            elif _is_godot_game_exe_name(name):
                dir_exes.append(full)

        # استبعاد المجلد الفرعي إذا كان مشروعاً أو لعبة مصدّرة مستقلة (exe+pck أو exe مدمج)
        has_nested_embedded = any(parse_pck_header_and_entries(e) is not None for e in dir_exes)
        if has_nested_root_marker or has_nested_embedded or (dir_pcks and dir_exes):
            dirs[:] = []
            continue

        if depth >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = [
                d for d in sorted(dirs)
                if d.lower() not in _CONTAINER_SCAN_SKIP_DIRS
            ]

        for p in dir_pcks:
            pck_candidates.append((depth, p))
        for e in dir_exes:
            exe_candidates.append((depth, e))

    # قبول .pck في مجلد فرعي فقط عندما يربطه تنفيذي أو override.cfg في الجذر كتخطيط مقسّم
    if not exe_candidates and not has_root_override:
        return [], [], None

    pck_candidates.sort(key=lambda item: (item[0], item[1].lower()))
    pck_files = [p for _, p in pck_candidates]
    exe_files = _sort_exes(exe_candidates, pck_files)
    return pck_files, exe_files, None


def _detect_csharp_presence(game_dir: str) -> bool:
    """تحديد هل يستخدم المشروع/اللعبة Godot Mono/C# أم GDScript فقط."""
    try:
        for name in os.listdir(game_dir):
            low = name.lower()
            full = os.path.join(game_dir, name)
            if low.endswith(".csproj"):
                return True
            if os.path.isdir(full) and (low.startswith("data_") or low == "godotsharp"):
                try:
                    if any(f.lower().endswith(".dll") for f in os.listdir(full)):
                        return True
                except OSError:
                    pass
    except OSError:
        pass
    return False


class GodotDetector:
    """يكتشف ألعاب ومشاريع Godot 3.x / 4.x ويميز دعم العربية الأصلي (HarfBuzz/ICU) من عدمه."""

    def detect(self, game_dir: str) -> EngineInfo:
        abs_dir = os.path.abspath(game_dir)
        if not os.path.isdir(abs_dir):
            return EngineInfo(
                engine_name="Godot",
                supported=False,
                game_dir=abs_dir,
                unsupported_reason=f"المجلد غير موجود: {abs_dir}",
            )

        pck_files, exe_files, project_godot = find_godot_containers(abs_dir)

        parsed_pcks: List[PckInfo] = []
        for pck_path in pck_files:
            info = parse_pck_header_and_entries(pck_path)
            if info is not None:
                parsed_pcks.append(info)

        embedded_exe: Optional[str] = None
        for exe_path in exe_files:
            info = parse_pck_header_and_entries(exe_path)
            if info is not None:
                parsed_pcks.append(info)
                if embedded_exe is None:
                    embedded_exe = exe_path

        if not parsed_pcks and not project_godot:
            return EngineInfo(
                engine_name="Godot",
                supported=False,
                game_dir=abs_dir,
                unsupported_reason=(
                    "لم يُعثر على حزمة Godot (.pck أو .exe مدمج ببصمة GDPC) أو ملف project.godot."
                ),
            )

        primary_exe = embedded_exe or (exe_files[0] if exe_files else None)
        arch = read_pe_architecture(primary_exe) if primary_exe else "unknown"
        if arch == "unknown" and primary_exe is None and parsed_pcks:
            arch = "x64"

        is_godot4: Optional[bool] = None
        version_label = ""

        if parsed_pcks:
            pck = parsed_pcks[0]
            if pck.godot_major >= 4 or pck.pack_format_version >= 2:
                is_godot4 = True
            else:
                is_godot4 = False
            version_label = f"{pck.godot_major}.{pck.godot_minor}.{pck.godot_patch}"
        elif project_godot:
            try:
                with open(project_godot, "r", encoding="utf-8-sig", errors="replace") as f:
                    proj_meta = parse_project_godot_text(f.read())
            except OSError:
                proj_meta = {}
            cfg_ver = proj_meta.get("config_version")
            if isinstance(cfg_ver, int):
                is_godot4 = cfg_ver >= 5
                version_label = "4.x" if is_godot4 else "3.x"
            else:
                is_godot4 = True
                version_label = "4.x"

        engine_name = "Godot 4" if is_godot4 else "Godot 3"
        requires_shaping = not bool(is_godot4)
        lang_mode = "C# / GDScript" if _detect_csharp_presence(abs_dir) else "GDScript"
        backend = f"{lang_mode} ({version_label})" if version_label else lang_mode

        return EngineInfo(
            engine_name=engine_name,
            supported=True,
            game_dir=abs_dir,
            architecture=arch,
            scripting_backend=backend,
            exe_path=primary_exe,
            data_dir=abs_dir,
            requires_arabic_shaping=requires_shaping,
        )
