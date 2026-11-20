# -*- coding: utf-8 -*-
"""مترجم وحاقن الخط العربي العام لألعاب Unity Mono باستخدام csc.exe."""

import os
import shutil
import subprocess
import tempfile
from typing import List, Optional, Tuple

from core.backup import BackupManager, atomic_copy_file, atomic_write_text
from core.models import EngineInfo, InjectionResult


def find_csc_compiler() -> Optional[str]:
    """البحث عن مترجم C# المدمج في نظام ويندوز (.NET Framework 4.x)."""
    candidates = [
        r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return shutil.which("csc")


def resolve_arabic_font_path(custom_font_path: Optional[str] = None) -> Optional[str]:
    """تحديد مسار الخط العربي الافتراضي (IBM Plex Sans Arabic) أو خط مخصص."""
    if custom_font_path:
        abs_custom = os.path.abspath(custom_font_path)
        if os.path.isfile(abs_custom) and os.path.getsize(abs_custom) > 1024:
            return abs_custom

    translator_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    parent_root = os.path.dirname(translator_root)
    candidates = [
        os.path.join(translator_root, "assets", "fonts", "IBMPlexSansArabic-Regular.ttf"),
        os.path.join(parent_root, "IBM_Plex_Sans_Arabic", "IBMPlexSansArabic-Regular.ttf"),
        os.path.join(translator_root, "assets", "fonts", "ArabicFont.ttf"),
    ]
    for path in candidates:
        if os.path.isfile(path) and os.path.getsize(path) > 1024:
            return path
    return None


def find_coreclr_runtime(
    game_dir: str, architecture: str = "x64"
) -> Optional[Tuple[str, str]]:
    """البحث عن بيئة CoreCLR (coreclr.dll + corlib_dir) اللازمة لتشغيل Doorstop مع IL2CPP."""
    if game_dir:
        local_candidates = [
            os.path.join(game_dir, "dotnet"),
            os.path.join(game_dir, "MelonLoader", "net6"),
        ]
        for folder in local_candidates:
            coreclr_dll = os.path.join(folder, "coreclr.dll")
            if os.path.isfile(coreclr_dll):
                return coreclr_dll, folder

    pf64 = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    if architecture == "x86":
        program_roots = [pf86, pf64]
    else:
        program_roots = [pf64, pf86]

    def _ver_key(name: str) -> Tuple[int, ...]:
        parts = []
        for tok in name.split("."):
            try:
                parts.append(int(tok))
            except ValueError:
                parts.append(0)
        return tuple(parts)

    seen_roots = set()
    for root in program_roots:
        if not root or root.lower() in seen_roots:
            continue
        seen_roots.add(root.lower())
        netcore_base = os.path.join(root, "dotnet", "shared", "Microsoft.NETCore.App")
        if not os.path.isdir(netcore_base):
            continue
        try:
            versions = sorted(os.listdir(netcore_base), key=_ver_key, reverse=True)
        except OSError:
            continue
        for ver in versions:
            ver_dir = os.path.join(netcore_base, ver)
            coreclr_dll = os.path.join(ver_dir, "coreclr.dll")
            if os.path.isfile(coreclr_dll):
                return coreclr_dll, ver_dir

    return None


def collect_managed_references(managed_dir: str) -> List[str]:
    """تجميع مكتبات DLL المرجعية اللازمة من مجلد Managed الخاص باللعبة المستهدفة."""
    ordered_names = [
        "mscorlib.dll",
        "netstandard.dll",
        "System.dll",
        "System.Core.dll",
        "UnityEngine.CoreModule.dll",
        "UnityEngine.dll",
        "UnityEngine.InputLegacyModule.dll",
        "UnityEngine.TextRenderingModule.dll",
        "UnityEngine.TextCoreModule.dll",
        "UnityEngine.IMGUIModule.dll",
        "UnityEngine.UI.dll",
        "UnityEngine.UIModule.dll",
        "Unity.TextMeshPro.dll",
    ]
    refs: List[str] = []
    for name in ordered_names:
        full = os.path.join(managed_dir, name)
        if os.path.isfile(full):
            # في Unity 2018+ نكتفي بـ CoreModule ولا نحتاج UnityEngine.dll المجمعة إذا وجد CoreModule
            if name == "UnityEngine.dll" and os.path.isfile(os.path.join(managed_dir, "UnityEngine.CoreModule.dll")):
                continue
            refs.append(full)
    return refs


def _dll_contains_ascii(dll_path: str, token: bytes) -> bool:
    """فحص سريع لوجود رمز نصي داخل تجميع .NET قبل تفعيل تعريفات الترجمة الشرطية."""
    try:
        with open(dll_path, "rb") as f:
            return token in f.read()
    except OSError:
        return False


class UnityInjector:
    """بناء ونشر حاقن الخط العربي ليتوافق مع إصدار Unity ومكتبات اللعبة المستهدفة."""

    def __init__(self, template_cs_path: Optional[str] = None) -> None:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        self.base_dir = base_dir
        self.template_cs_path = template_cs_path or os.path.join(
            base_dir, "injectors", "unity_runtime", "ArabicRuntimeInjector.cs"
        )

    def _run_csc_compile(
        self,
        csc: str,
        refs: List[str],
        defines: List[str],
        output_dll_path: str,
    ) -> Tuple[bool, str]:
        """استدعاء csc.exe لترجمة القالب البرمجي وكتابته ذرياً."""
        if not os.path.isfile(self.template_cs_path):
            return False, f"ملف المصدر C# غير موجود: {self.template_cs_path}"

        out_dir = os.path.dirname(os.path.abspath(output_dll_path))
        os.makedirs(out_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=out_dir, prefix=".csc_build_") as tmp_dir:
            tmp_dll = os.path.join(tmp_dir, os.path.basename(output_dll_path))

            cmd = [
                csc,
                "/nologo",
                "/target:library",
                "/optimize+",
                "/noconfig",
                "/nostdlib",
            ]
            if defines:
                cmd.append(f"/define:{';'.join(defines)}")
            for ref in refs:
                cmd.append(f"/r:{ref}")
            cmd.append(f"/out:{tmp_dll}")
            cmd.append(self.template_cs_path)

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            combined_out = (proc.stdout + "\n" + proc.stderr).strip()
            if proc.returncode != 0 or not os.path.isfile(tmp_dll):
                return False, combined_out or f"فشل مترجم csc برمز خروج {proc.returncode}"

            os.replace(tmp_dll, output_dll_path)
            return True, combined_out

    def compile_injector(
        self,
        engine: EngineInfo,
        output_dll_path: str,
        include_input_utils: bool = True,
    ) -> Tuple[bool, str]:
        """ترجمة ArabicRuntimeInjector.cs إلى DLL متوافق مع مكتبات اللعبة (Mono)."""
        csc = find_csc_compiler()
        if not csc:
            return False, "لم يتم العثور على مترجم csc.exe (.NET Framework 4.x) في النظام."
        if not engine.managed_dir or not os.path.isdir(engine.managed_dir):
            return False, "مجلد Managed غير موجود في اللعبة."

        refs = collect_managed_references(engine.managed_dir)
        defines: List[str] = []
        ref_basenames = {os.path.basename(r).lower(): r for r in refs}
        if "unity.textmeshpro.dll" in ref_basenames:
            defines.append("USE_TMP")
        for core_name in ("unityengine.coremodule.dll", "unityengine.dll"):
            core_path = ref_basenames.get(core_name)
            if core_path and _dll_contains_ascii(core_path, b"RuntimeInitializeOnLoadMethodAttribute"):
                defines.append("HAS_RUNTIME_INIT_LOAD")
                break
        if "unityengine.imguimodule.dll" in ref_basenames or (
            "unityengine.dll" in ref_basenames
            and _dll_contains_ascii(ref_basenames["unityengine.dll"], b"GUIContent")
        ):
            defines.append("HAS_IMGUI")
        has_legacy_input = any(
            name in ("unityengine.inputlegacymodule.dll", "unityengine.dll")
            for name in ref_basenames
        )
        if include_input_utils and has_legacy_input:
            defines.append("INCLUDE_INPUT_UTILS")

        return self._run_csc_compile(csc, refs, defines, output_dll_path)

    def compile_il2cpp_injector(
        self,
        engine: EngineInfo,
        output_dll_path: str,
    ) -> Tuple[bool, str]:
        """ترجمة ArabicRuntimeInjector.cs بوضع IL2CPP_BACKEND بدون الاعتماد على مجلد Managed."""
        csc = find_csc_compiler()
        if not csc:
            return False, "لم يتم العثور على مترجم csc.exe (.NET Framework 4.x) في النظام."

        fx_dir = os.path.dirname(csc)
        refs: List[str] = []
        for dll_name in ("mscorlib.dll", "System.dll", "System.Core.dll"):
            candidate = None
            if engine.managed_dir and os.path.isfile(os.path.join(engine.managed_dir, dll_name)):
                candidate = os.path.join(engine.managed_dir, dll_name)
            else:
                fx_candidate = os.path.join(fx_dir, dll_name)
                if os.path.isfile(fx_candidate):
                    candidate = fx_candidate
            if candidate:
                refs.append(candidate)

        if not refs:
            return False, "تعذر العثور على مكتبات الإطار الأساسية (mscorlib.dll / System.dll)."

        return self._run_csc_compile(csc, refs, ["IL2CPP_BACKEND"], output_dll_path)

    @staticmethod
    def _track_target_file(game_dir: str, target_path: str) -> None:
        """نسخ الملف احتياطياً إذا كان أصلياً، أو تسجيله كملف مضاف إذا لم يكن موجوداً مسبقاً."""
        abs_game = os.path.abspath(game_dir)
        abs_target = os.path.abspath(target_path)
        rel_path = os.path.relpath(abs_target, abs_game)
        manifest = BackupManager.load_manifest(abs_game)
        created_lower = {p.lower() for p in manifest.get("created_files", [])}
        if os.path.exists(abs_target) and rel_path.lower() not in created_lower:
            BackupManager.backup_file(abs_game, abs_target)
        else:
            BackupManager.register_created_file(abs_game, abs_target)

    def _deploy_doorstop_if_bundled(
        self,
        engine: EngineInfo,
        target_dll_path: str,
        is_il2cpp: bool = False,
        coreclr_info: Optional[Tuple[str, str]] = None,
    ) -> Tuple[bool, List[str]]:
        """نشر ملفات Unity Doorstop إذا كانت متوفرة في مجلد injectors/unity_runtime/doorstop."""
        arch_dir = os.path.join(
            self.base_dir, "injectors", "unity_runtime", "doorstop", engine.architecture
        )
        winhttp_src = os.path.join(arch_dir, "winhttp.dll")
        if not os.path.isfile(winhttp_src):
            return False, []

        deployed: List[str] = []
        winhttp_dst = os.path.join(engine.game_dir, "winhttp.dll")
        self._track_target_file(engine.game_dir, winhttp_dst)
        atomic_copy_file(winhttp_src, winhttp_dst)
        deployed.append(winhttp_dst)

        cfg_dst = os.path.join(engine.game_dir, "doorstop_config.ini")
        self._track_target_file(engine.game_dir, cfg_dst)
        rel_target = os.path.relpath(target_dll_path, engine.game_dir)
        cfg_content = (
            "[General]\n"
            "enabled=true\n"
            f"target_assembly={rel_target}\n"
            "redirect_output_log=false\n"
            "ignore_disable_switch=false\n\n"
            "[UnityDoorstop]\n"
            "enabled=true\n"
            f"targetAssembly={rel_target}\n"
            "redirectOutputLog=false\n"
        )
        if is_il2cpp and coreclr_info:
            coreclr_path, corlib_dir = coreclr_info
            cfg_content += (
                "\n[Il2Cpp]\n"
                f"coreclr_path={coreclr_path}\n"
                f"corlib_dir={corlib_dir}\n"
            )
        atomic_write_text(cfg_dst, cfg_content, encoding="utf-8")
        deployed.append(cfg_dst)
        return True, deployed

    def _inject_il2cpp(self, engine: EngineInfo) -> InjectionResult:
        """نشر وبناء الحاقن العربي لألعاب Unity IL2CPP (BepInEx 6 / MelonLoader / Doorstop CoreCLR)."""
        if engine.mod_loader == "BepInEx":
            target_dll = os.path.join(
                engine.game_dir, "BepInEx", "plugins", "ArabicRuntimeInjector.dll"
            )
            existed = os.path.exists(target_dll)
            if existed:
                self._track_target_file(engine.game_dir, target_dll)
            ok, msg = self.compile_il2cpp_injector(engine, target_dll)
            if not ok:
                return InjectionResult(
                    success=False,
                    method="il2cpp_bepinex",
                    message=f"فشل بناء حاقن BepInEx IL2CPP: {msg}",
                )
            if not existed:
                BackupManager.register_created_file(engine.game_dir, target_dll)
            return InjectionResult(
                success=True,
                method="il2cpp_bepinex",
                compiled_dll=target_dll,
                output_files=[target_dll],
                message="تم بناء ونشر ArabicRuntimeInjector.dll داخل BepInEx/plugins (IL2CPP) بنجاح.",
            )

        if engine.mod_loader == "MelonLoader":
            target_dll = os.path.join(
                engine.game_dir, "Mods", "ArabicRuntimeInjector.dll"
            )
            existed = os.path.exists(target_dll)
            if existed:
                self._track_target_file(engine.game_dir, target_dll)
            ok, msg = self.compile_il2cpp_injector(engine, target_dll)
            if not ok:
                return InjectionResult(
                    success=False,
                    method="il2cpp_melonloader",
                    message=f"فشل بناء حاقن MelonLoader IL2CPP: {msg}",
                )
            if not existed:
                BackupManager.register_created_file(engine.game_dir, target_dll)
            return InjectionResult(
                success=True,
                method="il2cpp_melonloader",
                compiled_dll=target_dll,
                output_files=[target_dll],
                message="تم بناء ونشر ArabicRuntimeInjector.dll داخل Mods (MelonLoader IL2CPP) بنجاح.",
            )

        target_dll = os.path.join(engine.game_dir, "ArabicRuntimeInjector.dll")
        existed = os.path.exists(target_dll)
        if existed:
            self._track_target_file(engine.game_dir, target_dll)
        ok, msg = self.compile_il2cpp_injector(engine, target_dll)
        if not ok:
            return InjectionResult(
                success=False,
                method="il2cpp_doorstop",
                message=f"فشل بناء ArabicRuntimeInjector.dll (IL2CPP): {msg}",
            )
        if not existed:
            BackupManager.register_created_file(engine.game_dir, target_dll)

        coreclr_info = find_coreclr_runtime(engine.game_dir, engine.architecture)
        doorstop_ok, doorstop_files = self._deploy_doorstop_if_bundled(
            engine,
            target_dll,
            is_il2cpp=True,
            coreclr_info=coreclr_info,
        )
        if doorstop_ok:
            if coreclr_info:
                note = "تم بناء ArabicRuntimeInjector.dll ونشر Unity Doorstop [Il2Cpp] مع CoreCLR بنجاح."
            else:
                note = (
                    "تم بناء ArabicRuntimeInjector.dll ونشر Unity Doorstop لـ IL2CPP، "
                    "ولم يُعثر على بيئة .NET CoreCLR على الجهاز لإضافتها داخل [Il2Cpp]."
                )
            return InjectionResult(
                success=True,
                method="il2cpp_doorstop",
                compiled_dll=target_dll,
                output_files=[target_dll] + doorstop_files,
                message=note,
            )

        return InjectionResult(
            success=True,
            method="il2cpp_doorstop_ready",
            compiled_dll=target_dll,
            output_files=[target_dll],
            message=(
                f"تم بناء حاقن IL2CPP في {target_dll}؛ أضف ملف winhttp.dll الخاص بـ Unity Doorstop "
                f"({engine.architecture}) بجانب ملف اللعبة لتفعيله."
            ),
        )

    def deploy_arabic_font(
        self, game_dir: str, custom_font_path: Optional[str] = None
    ) -> Optional[str]:
        """نشر ملف الخط العربي الافتراضي (ArabicFont.ttf) في مجلد اللعبة وتسجيله للاسترجاع."""
        src_font = resolve_arabic_font_path(custom_font_path)
        if not src_font or not game_dir or not os.path.isdir(game_dir):
            return None
        dst_font = os.path.join(os.path.abspath(game_dir), "ArabicFont.ttf")
        existed = os.path.exists(dst_font)
        if existed:
            self._track_target_file(game_dir, dst_font)
        else:
            BackupManager.register_created_file(game_dir, dst_font)
        atomic_copy_file(src_font, dst_font)
        return dst_font

    def update_arabic_font(
        self, engine: EngineInfo, custom_font_path: Optional[str] = None
    ) -> InjectionResult:
        """إضافة أو تحديث الخط العربي الافتراضي على لعبة معرّبة مسبقاً دون المساس بملفات الترجمة."""
        font_dst = self.deploy_arabic_font(engine.game_dir, custom_font_path=custom_font_path)
        if not font_dst:
            return InjectionResult(
                success=False,
                method="font_not_found",
                message="تعذر العثور على ملف الخط العربي (IBMPlexSansArabic-Regular.ttf).",
            )

        res = self.inject(engine, skip_runtime_font=False)
        if res.success:
            if font_dst not in res.output_files:
                res.output_files.append(font_dst)
            res.message = (
                f"تم نشر/تحديث الخط العربي ({os.path.basename(font_dst)}) وإعادة بناء الحاقن بنجاح: "
                + res.message
            )
        return res

    def inject(self, engine: EngineInfo, skip_runtime_font: bool = False) -> InjectionResult:
        """تنفيذ الحقن المناسب للعبة Unity حسب نوع البناء والملفات المتوفرة."""
        if skip_runtime_font:
            return InjectionResult(
                success=True,
                method="skipped",
                message="تم تخطي حقن الخط العربي وقت التشغيل بناءً على طلب المستخدم.",
            )

        deployed_font = self.deploy_arabic_font(engine.game_dir)

        if engine.scripting_backend == "IL2CPP":
            res = self._inject_il2cpp(engine)
            if deployed_font and deployed_font not in res.output_files:
                res.output_files.append(deployed_font)
            return res

        if engine.scripting_backend != "Mono" or not engine.managed_dir:
            return InjectionResult(
                success=False,
                method="unsupported_backend",
                message=(
                    f"تمت ترجمة النصوص، لكن حقن الخط وقت التشغيل لا يدعم بنية اللعبة الحالية "
                    f"({engine.scripting_backend})."
                ),
            )

        platform_input_dll = os.path.join(engine.managed_dir, "PlatformInputDefault.dll")
        if os.path.isfile(platform_input_dll):
            BackupManager.backup_file(engine.game_dir, platform_input_dll)
            ok, msg = self.compile_injector(engine, platform_input_dll, include_input_utils=True)
            if not ok:
                return InjectionResult(
                    success=False,
                    method="platform_input_dll",
                    message=f"فشل بناء PlatformInputDefault.dll: {msg}",
                )
            outputs = [platform_input_dll] + ([deployed_font] if deployed_font else [])
            return InjectionResult(
                success=True,
                method="platform_input_dll",
                compiled_dll=platform_input_dll,
                output_files=outputs,
                message="تم بناء ونشر PlatformInputDefault.dll مع الحاقن العربي وحماية LanguageSDF بنجاح.",
            )

        # المسار العام لأي لعبة Unity Mono أخرى: بناء ArabicRuntimeInjector.dll + Doorstop
        generic_dll = os.path.join(engine.managed_dir, "ArabicRuntimeInjector.dll")
        generic_existed = os.path.exists(generic_dll)
        if generic_existed:
            self._track_target_file(engine.game_dir, generic_dll)
        ok, msg = self.compile_injector(engine, generic_dll, include_input_utils=False)
        if not ok:
            return InjectionResult(
                success=False,
                method="generic_doorstop",
                message=f"فشل بناء ArabicRuntimeInjector.dll: {msg}",
            )
        if not generic_existed:
            BackupManager.register_created_file(engine.game_dir, generic_dll)

        doorstop_ok, doorstop_files = self._deploy_doorstop_if_bundled(engine, generic_dll)
        font_list = [deployed_font] if deployed_font else []
        if doorstop_ok:
            return InjectionResult(
                success=True,
                method="doorstop",
                compiled_dll=generic_dll,
                output_files=[generic_dll] + doorstop_files + font_list,
                message="تم بناء ArabicRuntimeInjector.dll ونشر Unity Doorstop والخط العربي بنجاح.",
            )

        return InjectionResult(
            success=True,
            method="compiled_doorstop_ready",
            compiled_dll=generic_dll,
            output_files=[generic_dll] + font_list,
            message=(
                f"تم بناء الحاقن العام في {generic_dll}؛ أضف ملف winhttp.dll الخاص بـ Unity Doorstop "
                f"({engine.architecture}) بجانب ملف اللعبة لتفعيله تلقائياً عند التشغيل."
            ),
        )
