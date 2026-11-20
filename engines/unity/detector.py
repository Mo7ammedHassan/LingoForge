# -*- coding: utf-8 -*-
"""فحص مجلد اللعبة لاكتشاف محرك Unity والمعمارية ونوع البناء (Mono/IL2CPP) ومكتبة النصوص."""

import os
import struct
from typing import List, Optional, Tuple

from core.models import EngineInfo
from engines.unity.il2cpp_metadata import detect_il2cpp_ui_frameworks_from_files


def read_pe_architecture(exe_path: str) -> str:
    """قراءة ترويسة PE لتحديد معمارية الملف التنفيذي (x64 أو x86)."""
    try:
        with open(exe_path, "rb") as f:
            dos_header = f.read(64)
            if len(dos_header) < 64 or dos_header[:2] != b"MZ":
                return "unknown"
            pe_offset = struct.unpack_from("<I", dos_header, 0x3C)[0]
            f.seek(pe_offset)
            pe_header = f.read(6)
            if len(pe_header) < 6 or pe_header[:4] != b"PE\x00\x00":
                return "unknown"
            machine = struct.unpack_from("<H", pe_header, 4)[0]
            if machine == 0x8664:
                return "x64"
            if machine == 0x014C:
                return "x86"
            if machine == 0xAA64:
                return "arm64"
    except OSError:
        pass
    return "unknown"


def _find_unity_data_dir(game_dir: str) -> Optional[str]:
    """العثور على مجلد *_Data الخاص بلعبة Unity."""
    try:
        entries = sorted(os.listdir(game_dir))
    except OSError:
        return None

    for name in entries:
        full = os.path.join(game_dir, name)
        if os.path.isdir(full) and name.endswith("_Data"):
            markers = (
                "globalgamemanagers",
                "data.unity3d",
                "Managed",
                "il2cpp_data",
                "Resources",
            )
            if any(os.path.exists(os.path.join(full, m)) for m in markers):
                return full
    return None


def _find_game_exe(game_dir: str, data_dir: Optional[str]) -> Optional[str]:
    if data_dir:
        base_name = os.path.basename(data_dir)[:-5]
        candidate = os.path.join(game_dir, base_name + ".exe")
        if os.path.isfile(candidate):
            return candidate

    try:
        for name in sorted(os.listdir(game_dir)):
            low = name.lower()
            if low.endswith(".exe") and "crashhandler" not in low and "unins" not in low:
                full = os.path.join(game_dir, name)
                if os.path.isfile(full):
                    return full
    except OSError:
        pass
    return None


def _detect_other_engine(game_dir: str) -> EngineInfo:
    """تحديد اسم المحرك في حال لم تكن اللعبة مبنية على Unity لإصدار تقرير دقيق."""
    abs_dir = os.path.abspath(game_dir)
    for root, dirs, files in os.walk(abs_dir):
        rel_depth = os.path.relpath(root, abs_dir).count(os.sep)
        if rel_depth > 2:
            dirs[:] = []
            continue
        low_files = {f.lower() for f in files}
        low_dirs = {d.lower() for d in dirs}
        if any(f.endswith(".uproject") or "-win64-shipping.exe" in f for f in low_files) or (
            "engine" in low_dirs and os.path.isdir(os.path.join(root, "Engine", "Binaries"))
        ):
            return EngineInfo(
                engine_name="Unreal Engine",
                supported=False,
                game_dir=abs_dir,
                unsupported_reason="تم اكتشاف محرك Unreal Engine، وهو غير مدعوم في المرحلة الأولى (مخطط إضافته كـ Adapter لاحقاً).",
            )
        if any(f.endswith(".pck") for f in low_files):
            return EngineInfo(
                engine_name="Godot",
                supported=False,
                game_dir=abs_dir,
                unsupported_reason="تم اكتشاف محرك Godot، وهو غير مدعوم في المرحلة الأولى (مخطط إضافته كـ Adapter لاحقاً).",
            )
        if "renpy" in low_dirs:
            return EngineInfo(
                engine_name="Ren'Py",
                supported=False,
                game_dir=abs_dir,
                unsupported_reason="تم اكتشاف محرك Ren'Py، وهو غير مدعوم في المرحلة الأولى (مخطط إضافته كـ Adapter لاحقاً).",
            )
        if "rpgproject" in "".join(low_files) or ("www" in low_dirs and os.path.isdir(os.path.join(root, "www", "data"))):
            return EngineInfo(
                engine_name="RPG Maker",
                supported=False,
                game_dir=abs_dir,
                unsupported_reason="تم اكتشاف محرك RPG Maker، وهو غير مدعوم في المرحلة الأولى (مخطط إضافته كـ Adapter لاحقاً).",
            )

    return EngineInfo(
        engine_name="Unknown",
        supported=False,
        game_dir=abs_dir,
        unsupported_reason="لم يتم التعرف على محرك مدعوم داخل المجلد المحدد؛ تم إيقاف العملية بأمان بدون تعديل أي ملف.",
    )


def _dir_has_dlls(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    try:
        return any(name.lower().endswith(".dll") for name in os.listdir(path))
    except OSError:
        return False


def _detect_mod_loader(abs_dir: str) -> Tuple[str, Optional[str]]:
    """اكتشاف BepInEx أو MelonLoader ومجلد مكتبات Interop إن وجد."""
    bepinex_dir = os.path.join(abs_dir, "BepInEx")
    if os.path.isdir(bepinex_dir):
        for sub in ("interop", "unhollowed"):
            candidate = os.path.join(bepinex_dir, sub)
            if _dir_has_dlls(candidate):
                return "BepInEx", candidate
        return "BepInEx", None

    melon_dir = os.path.join(abs_dir, "MelonLoader")
    if os.path.isdir(melon_dir):
        for sub in ("Il2CppAssemblies", "Managed"):
            candidate = os.path.join(melon_dir, sub)
            if _dir_has_dlls(candidate):
                return "MelonLoader", candidate
        return "MelonLoader", None

    return "none", None


_LOCALIZATION_SYSTEM_SIGNATURES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("Unity.Localization", ("unity.localization", "unityengine.localization")),
    ("I2.Localization", ("i2.localization", "i2.loc", "i2languages")),
    ("Addressables", ("unity.addressables",)),
    ("YarnSpinner", ("yarnspinner", "yarn.unity")),
    ("Ink", ("ink-libraries", "inkruntime", "ink.runtime")),
    ("Fungus", ("fungus.dll", "fungus.")),
    ("DialogueSystem", ("pixelcrushers.dialoguesystem", "dialoguesystem.dll")),
    ("Naninovel", ("elringus.naninovel", "naninovel.")),
    ("LeanLocalization", ("leanlocalization", "lean.localization")),
)


def _detect_unity_version(data_dir: Optional[str]) -> str:
    """استخراج إصدار محرك Unity من الملفات الأساسية داخل *_Data."""
    if not data_dir or not os.path.isdir(data_dir):
        return ""
    from engines.unity.assets_extractor import extract_unity_version_from_file

    for fname in ("globalgamemanagers", "data.unity3d", "resources.assets", "globalgamemanagers.assets"):
        candidate = os.path.join(data_dir, fname)
        if os.path.isfile(candidate):
            ver = extract_unity_version_from_file(candidate)
            if ver:
                return ver
    return ""


def _detect_localization_systems(
    data_dir: Optional[str],
    managed_dir: Optional[str],
    il2cpp_interop_dir: Optional[str],
    il2cpp_metadata_path: Optional[str],
) -> List[str]:
    """اكتشاف أنظمة التعريب وإدارة الحوارات والحزم المستخدمة في اللعبة."""
    corpus_chunks: List[str] = []
    for dll_dir in (managed_dir, il2cpp_interop_dir):
        if dll_dir and os.path.isdir(dll_dir):
            try:
                corpus_chunks.extend(name.lower() for name in os.listdir(dll_dir))
            except OSError:
                pass

    has_aa_catalog = False
    if data_dir and os.path.isdir(data_dir):
        sa_path = os.path.join(data_dir, "ScriptingAssemblies.json")
        if os.path.isfile(sa_path):
            try:
                with open(sa_path, "r", encoding="utf-8", errors="ignore") as f:
                    corpus_chunks.append(f.read(1024 * 1024).lower())
            except OSError:
                pass
        aa_dir = os.path.join(data_dir, "StreamingAssets", "aa")
        if os.path.isdir(aa_dir):
            try:
                if any(
                    n.lower() == "catalog.json" or (n.lower().startswith("catalog_") and n.lower().endswith(".json"))
                    for n in os.listdir(aa_dir)
                ):
                    has_aa_catalog = True
            except OSError:
                pass

    meta_blob = b""
    if il2cpp_metadata_path and os.path.isfile(il2cpp_metadata_path):
        try:
            with open(il2cpp_metadata_path, "rb") as f:
                meta_blob = f.read(16 * 1024 * 1024).lower()
        except OSError:
            pass

    joined_text = "\n".join(corpus_chunks)
    detected: List[str] = []
    for sys_name, markers in _LOCALIZATION_SYSTEM_SIGNATURES:
        matched = any(m in joined_text for m in markers)
        if not matched and meta_blob:
            matched = any(m.encode("ascii") in meta_blob for m in markers)
        if not matched and sys_name == "Addressables" and has_aa_catalog:
            matched = True
        if matched:
            detected.append(sys_name)
    return detected


def _detect_risk_warnings(abs_dir: str, data_dir: Optional[str]) -> List[str]:
    """فحص وجود أنظمة حماية أو مكافحة غش قد تعيق حقن وقت التشغيل."""
    probe_dirs: List[str] = [abs_dir]
    if data_dir:
        plugins_dir = os.path.join(data_dir, "Plugins")
        if os.path.isdir(plugins_dir):
            probe_dirs.append(plugins_dir)
            for sub in ("x86_64", "x86"):
                sub_p = os.path.join(plugins_dir, sub)
                if os.path.isdir(sub_p):
                    probe_dirs.append(sub_p)

    names_lower: List[str] = []
    for pdir in probe_dirs:
        try:
            names_lower.extend(n.lower() for n in os.listdir(pdir))
        except OSError:
            pass

    joined = "\n".join(names_lower)
    warnings: List[str] = []
    if "easyanticheat" in joined:
        warnings.append(
            "تحذير: تم رصد نظام حماية EasyAntiCheat؛ قد يمنع حقن Doorstop/DLL وقت التشغيل."
        )
    if "battleye" in joined:
        warnings.append(
            "تحذير: تم رصد نظام حماية BattlEye؛ قد يمنع حقن DLL وقت التشغيل."
        )
    if "gameguard" in joined:
        warnings.append(
            "تحذير: تم رصد نظام حماية GameGuard؛ قد يمنع حقن DLL وقت التشغيل."
        )
    return warnings


class UnityDetector:
    """كاشف محرك Unity وخصائصه."""

    def detect(self, game_dir: str) -> EngineInfo:
        abs_dir = os.path.abspath(game_dir)
        data_dir = _find_unity_data_dir(abs_dir)
        has_unity_player = os.path.isfile(os.path.join(abs_dir, "UnityPlayer.dll"))
        if not data_dir and not has_unity_player:
            return _detect_other_engine(abs_dir)

        exe_path = _find_game_exe(abs_dir, data_dir)
        arch = read_pe_architecture(exe_path) if exe_path else "unknown"
        if arch == "unknown" and has_unity_player:
            arch = read_pe_architecture(os.path.join(abs_dir, "UnityPlayer.dll"))

        managed_dir = os.path.join(data_dir, "Managed") if data_dir else None
        has_managed = bool(managed_dir and os.path.isdir(managed_dir))
        has_game_assembly = os.path.isfile(os.path.join(abs_dir, "GameAssembly.dll"))

        if has_managed and os.path.isfile(os.path.join(managed_dir, "Assembly-CSharp.dll")):
            backend = "Mono"
        elif has_game_assembly or (data_dir and os.path.isdir(os.path.join(data_dir, "il2cpp_data"))):
            backend = "IL2CPP"
        elif has_managed:
            backend = "Mono"
        else:
            backend = "unknown"

        il2cpp_metadata_path: Optional[str] = None
        if data_dir:
            meta_candidate = os.path.join(
                data_dir, "il2cpp_data", "Metadata", "global-metadata.dat"
            )
            if os.path.isfile(meta_candidate):
                il2cpp_metadata_path = meta_candidate

        mod_loader, il2cpp_interop_dir = _detect_mod_loader(abs_dir)

        has_tmp = bool(
            managed_dir and os.path.isfile(os.path.join(managed_dir, "Unity.TextMeshPro.dll"))
        )
        has_ugui = bool(
            managed_dir and os.path.isfile(os.path.join(managed_dir, "UnityEngine.UI.dll"))
        )

        if backend == "IL2CPP" and (not has_tmp or not has_ugui):
            il2cpp_tmp, il2cpp_ugui = detect_il2cpp_ui_frameworks_from_files(
                il2cpp_metadata_path, data_dir, il2cpp_interop_dir
            )
            has_tmp = has_tmp or il2cpp_tmp
            has_ugui = has_ugui or il2cpp_ugui

        unity_version = _detect_unity_version(data_dir)
        localization_systems = _detect_localization_systems(
            data_dir,
            managed_dir if has_managed else None,
            il2cpp_interop_dir,
            il2cpp_metadata_path,
        )
        risk_warnings = _detect_risk_warnings(abs_dir, data_dir)

        supported = backend in ("Mono", "IL2CPP")
        reason = ""
        if not supported:
            reason = "تم اكتشاف بنية Unity غير مكتملة (لا Mono ولا IL2CPP واضح)."

        return EngineInfo(
            engine_name="Unity",
            supported=supported,
            game_dir=abs_dir,
            architecture=arch,
            scripting_backend=backend,
            exe_path=exe_path,
            data_dir=data_dir,
            managed_dir=managed_dir if has_managed else None,
            il2cpp_metadata_path=il2cpp_metadata_path,
            il2cpp_interop_dir=il2cpp_interop_dir,
            mod_loader=mod_loader,
            has_tmp=has_tmp,
            has_ugui=has_ugui,
            unsupported_reason=reason,
            unity_version=unity_version,
            localization_systems=localization_systems,
            risk_warnings=risk_warnings,
        )
