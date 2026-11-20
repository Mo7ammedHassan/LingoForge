# -*- coding: utf-8 -*-
"""كاشف محرك Unreal Engine (UE4 / UE5) ومعماريته وهيكلة مجلدات Content / Paks / IoStore."""

from dataclasses import dataclass, field
import json
import os
import re
from typing import List, Optional, Tuple

from core.models import EngineInfo
from engines.unity.detector import read_pe_architecture
from engines.unreal.pak_parser import parse_pak_header_and_entries


SHIPPING_EXE_RE = re.compile(r"-(?:Win64|Win32|WinGDK)-Shipping\.exe$", re.IGNORECASE)
IGNORED_EXE_TOKENS = ("crashreport", "unrealcefsubprocess", "unins", "ueprereqsetup")
IGNORED_SCAN_DIRS = {".arabic_translator", ".git", "__pycache__", "saved", "intermediate"}


@dataclass
class UnrealLayout:
    """بنية مجلدات وملفات Unreal Engine المكتشفة."""

    game_dir: str
    is_unreal: bool = False
    project_name: Optional[str] = None
    uproject_path: Optional[str] = None
    exe_path: Optional[str] = None
    content_dir: Optional[str] = None
    paks_dir: Optional[str] = None
    config_dir: Optional[str] = None
    pak_files: List[str] = field(default_factory=list)
    sig_files: List[str] = field(default_factory=list)
    utoc_files: List[str] = field(default_factory=list)
    ucas_files: List[str] = field(default_factory=list)
    locres_files: List[str] = field(default_factory=list)
    locmeta_files: List[str] = field(default_factory=list)
    text_data_files: List[str] = field(default_factory=list)
    has_engine_binaries: bool = False
    has_ue4_binary_hint: bool = False
    has_ue5_binary_hint: bool = False
    ue_major: Optional[int] = None
    ue_version_hint: str = "UE4/UE5"


def resolve_unreal_game_root(path_in_game: str) -> str:
    """تحديد الجذر الرئيسي للعبة Unreal انطلاقاً من ملف أو مجلد داخلها."""
    cur = os.path.abspath(path_in_game)
    if os.path.isfile(cur):
        cur = os.path.dirname(cur)

    ancestors: List[str] = []
    probe = cur
    for _ in range(6):
        ancestors.append(probe)
        parent = os.path.dirname(probe)
        if not parent or parent == probe:
            break
        probe = parent

    # 1. الأولوية لمجلد يحمل سجل النسخ الاحتياطي أو جذر المحرك أو .uproject أو Shipping.exe
    for anc in ancestors:
        if os.path.isdir(os.path.join(anc, ".arabic_translator")):
            return anc
    for anc in ancestors:
        if os.path.isdir(os.path.join(anc, "Engine")):
            return anc
        try:
            entries = os.listdir(anc)
        except OSError:
            continue
        if any(
            e.lower().endswith(".uproject") or SHIPPING_EXE_RE.search(e)
            for e in entries
        ):
            return anc

    # 2. الرجوع إلى أب Content أو أب مجلد .pak
    for anc in ancestors:
        if os.path.basename(anc).lower() == "content":
            proj_dir = os.path.dirname(anc)
            return proj_dir if proj_dir else anc
    return cur


def _read_uproject_ue_major(uproject_path: str) -> Tuple[Optional[int], Optional[str]]:
    """استخراج رقم إصدار المحرك واسم المشروع من ملف .uproject إن كان نصياً."""
    proj_name = os.path.splitext(os.path.basename(uproject_path))[0]
    try:
        with open(uproject_path, "r", encoding="utf-8-sig", errors="replace") as f:
            data = json.load(f)
        assoc = str(data.get("EngineAssociation") or "").strip()
        if assoc.startswith("5"):
            return 5, proj_name
        if assoc.startswith("4"):
            return 4, proj_name
    except Exception:
        pass
    return None, proj_name


def find_unreal_layout(game_dir: str) -> UnrealLayout:
    """مسح مجلد اللعبة لاكتشاف مؤشرات Unreal Engine ومسارات Content/Paks/Localization."""
    abs_dir = os.path.abspath(game_dir)
    layout = UnrealLayout(game_dir=abs_dir)
    if not os.path.isdir(abs_dir):
        return layout

    if os.path.isdir(os.path.join(abs_dir, "Engine", "Binaries")):
        layout.has_engine_binaries = True

    shipping_exes: List[str] = []
    other_exes: List[str] = []

    for root, dirs, files in os.walk(abs_dir):
        rel = os.path.relpath(root, abs_dir)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = [
            d for d in sorted(dirs)
            if d.lower() not in IGNORED_SCAN_DIRS and depth < 6
        ]

        low_root = root.replace("\\", "/").lower()
        base_dir = os.path.basename(root).lower()

        if base_dir == "binaries" and os.path.basename(os.path.dirname(root)).lower() == "engine":
            layout.has_engine_binaries = True

        if base_dir == "content" and "engine" not in low_root.split("/"):
            if layout.content_dir is None or depth <= 2:
                layout.content_dir = root
                parent_name = os.path.basename(os.path.dirname(root))
                if os.path.abspath(os.path.dirname(root)) != abs_dir and not layout.project_name:
                    layout.project_name = parent_name
                cfg_candidate = os.path.join(os.path.dirname(root), "Config")
                if os.path.isdir(cfg_candidate) and layout.config_dir is None:
                    layout.config_dir = cfg_candidate

        if base_dir == "paks" and "/~mods" not in low_root and layout.paks_dir is None:
            layout.paks_dir = root

        if base_dir == "config" and "engine" not in low_root.split("/") and layout.config_dir is None:
            if depth <= 2:
                layout.config_dir = root

        for fname in sorted(files):
            low = fname.lower()
            full = os.path.join(root, fname)

            if low.endswith(".uproject") and layout.uproject_path is None:
                layout.uproject_path = full
            elif low.endswith(".exe"):
                if any(tok in low for tok in IGNORED_EXE_TOKENS):
                    continue
                if SHIPPING_EXE_RE.search(fname):
                    shipping_exes.append(full)
                elif depth <= 1:
                    other_exes.append(full)
                if low.startswith("ue4"):
                    layout.has_ue4_binary_hint = True
                elif low.startswith("unrealgame") or low.startswith("unrealeditor"):
                    layout.has_ue5_binary_hint = True
            elif low.endswith(".dll"):
                if low.startswith("ue4-") or low.startswith("ue4editor-"):
                    layout.has_ue4_binary_hint = True
                elif low.startswith("unrealeditor-") or low.startswith("unrealgame-"):
                    layout.has_ue5_binary_hint = True
            elif low.endswith(".pak"):
                if "/~mods/" not in low_root and low != "z_arabicl10n_p.pak":
                    layout.pak_files.append(full)
            elif low.endswith(".sig"):
                if "/~mods/" not in low_root:
                    layout.sig_files.append(full)
            elif low.endswith(".utoc"):
                layout.utoc_files.append(full)
            elif low.endswith(".ucas"):
                layout.ucas_files.append(full)
            elif low.endswith(".locres"):
                layout.locres_files.append(full)
            elif low.endswith(".locmeta"):
                layout.locmeta_files.append(full)
            elif low.endswith((".csv", ".tsv", ".json", ".po", ".ini", ".txt", ".yaml", ".yml")):
                if any(
                    tok in low_root
                    for tok in ("localization", "language", "locale", "dialog", "subtitle", "strings", "/text", "/data")
                ) and not (
                    low.startswith(("default", "base", "compat", "engine", "manifest", "assetregistry"))
                    or low in ("backup_manifest.json", "arabic_font_log.txt", "arabic_translator_log.txt")
                ):
                    layout.text_data_files.append(full)

    if shipping_exes:
        layout.exe_path = shipping_exes[0]
        if not layout.project_name:
            layout.project_name = SHIPPING_EXE_RE.sub("", os.path.basename(layout.exe_path))
    elif other_exes:
        layout.exe_path = other_exes[0]
        if not layout.project_name:
            layout.project_name = os.path.splitext(os.path.basename(layout.exe_path))[0]

    uproj_major: Optional[int] = None
    if layout.uproject_path:
        uproj_major, uproj_name = _read_uproject_ue_major(layout.uproject_path)
        if uproj_name:
            layout.project_name = uproj_name

    # التحقق من مؤشرات Unreal Engine الفعلية
    layout.is_unreal = bool(
        layout.uproject_path
        or layout.has_engine_binaries
        or shipping_exes
        or layout.pak_files
        or layout.utoc_files
        or layout.ucas_files
        or layout.locres_files
    )

    if not layout.is_unreal:
        return layout

    # تحديد UE4 مقابل UE5
    pak_version: Optional[int] = None
    for p in layout.pak_files[:3]:
        pinfo = parse_pak_header_and_entries(p)
        if pinfo is not None:
            pak_version = pinfo.version
            break

    has_iostore = bool(layout.utoc_files or layout.ucas_files)
    if uproj_major is not None:
        layout.ue_major = uproj_major
        layout.ue_version_hint = f"UE{uproj_major}"
    elif layout.has_ue5_binary_hint or (has_iostore and not layout.has_ue4_binary_hint):
        layout.ue_major = 5
        layout.ue_version_hint = "UE5 (IoStore)" if has_iostore else "UE5"
    elif layout.has_ue4_binary_hint or (pak_version is not None and pak_version <= 9 and not has_iostore):
        layout.ue_major = 4
        layout.ue_version_hint = f"UE4 (Pak v{pak_version})" if pak_version else "UE4"
    elif pak_version is not None and pak_version >= 10:
        layout.ue_version_hint = f"UE4.25+ / UE5 (Pak v{pak_version})"
    else:
        layout.ue_version_hint = "UE4/UE5"

    if layout.paks_dir is None and layout.pak_files:
        layout.paks_dir = os.path.dirname(layout.pak_files[0])
    if layout.content_dir is None and layout.paks_dir:
        parent = os.path.dirname(layout.paks_dir)
        if os.path.basename(parent).lower() == "content":
            layout.content_dir = parent

    return layout


class UnrealDetector:
    """يكتشف ألعاب ومشاريع Unreal Engine (UE4 / UE5) ومعماريتها بدون تعديل أي ملف."""

    def detect(self, game_dir: str) -> EngineInfo:
        abs_dir = os.path.abspath(game_dir)
        if not os.path.isdir(abs_dir):
            return EngineInfo(
                engine_name="Unreal Engine",
                supported=False,
                game_dir=abs_dir,
                unsupported_reason=f"المجلد غير موجود: {abs_dir}",
            )

        layout = find_unreal_layout(abs_dir)
        if not layout.is_unreal:
            return EngineInfo(
                engine_name="Unreal Engine",
                supported=False,
                game_dir=abs_dir,
                unsupported_reason=(
                    "لم يُعثر على مؤشرات Unreal Engine (*.uproject أو Engine/Binaries أو "
                    "*-Shipping.exe أو Content/Paks/*.pak أو ملفات .locres)."
                ),
            )

        arch = read_pe_architecture(layout.exe_path) if layout.exe_path else "unknown"
        if arch == "unknown" and layout.exe_path:
            low_exe = os.path.basename(layout.exe_path).lower()
            if "win64" in low_exe:
                arch = "x64"
            elif "win32" in low_exe:
                arch = "x86"
        if arch == "unknown" and (layout.pak_files or layout.locres_files or layout.uproject_path):
            arch = "x64"

        return EngineInfo(
            engine_name="Unreal Engine",
            supported=True,
            game_dir=abs_dir,
            architecture=arch,
            scripting_backend=layout.ue_version_hint,
            exe_path=layout.exe_path,
            data_dir=layout.content_dir or abs_dir,
            # محرك Unreal Engine 4/5 يدعم HarfBuzz/ICU وتشكيل العربية أصلاً عند توفر خط عربي
            requires_arabic_shaping=False,
        )
