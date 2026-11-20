# -*- coding: utf-8 -*-
"""حاقن حزم التعديل (~mods/*.pak) والخط العربي وإعدادات التهيئة لمحرك Unreal Engine."""

import os
import shutil
from typing import Dict, List, Optional, Tuple

from core.backup import BackupManager, atomic_copy_file, atomic_write_bytes, atomic_write_text
from core.models import EngineInfo, InjectionResult
from engines.unity.injector import resolve_arabic_font_path
from engines.unreal.detector import find_unreal_layout
from engines.unreal.pak_parser import build_unreal_pak_blob, parse_pak_header_and_entries
from formats.unreal_pak_handler import load_staging_manifest


MOD_PAK_FILENAME = "z_ArabicL10n_P.pak"
MODS_SUBDIR = "~mods"


def _is_tracked_created_file(game_dir: str, target_path: str) -> bool:
    """التحقق مما إذا كان الملف مسجلاً مسبقاً في قائمة الملفات المُنشأة بواسطة الأداة."""
    abs_game = os.path.abspath(game_dir)
    rel = os.path.relpath(os.path.abspath(target_path), abs_game).lower()
    manifest = BackupManager.load_manifest(abs_game)
    return any(p.lower() == rel for p in manifest.get("created_files", []))


def _upsert_ini_file(
    game_dir: str,
    ini_path: str,
    updates: Dict[str, Dict[str, str]],
    preserve_existing_keys: Optional[Dict[str, set]] = None,
) -> None:
    """تحديث آمن لملف INI في Unreal مع الحفاظ على الأقسام والمفاتيح والتعليقات الحالية."""
    existed = os.path.exists(ini_path)
    tracked_created = existed and _is_tracked_created_file(game_dir, ini_path)
    lines: List[str] = []
    if existed:
        if not tracked_created:
            BackupManager.backup_file(game_dir, ini_path)
        with open(ini_path, "r", encoding="utf-8-sig", errors="replace") as f:
            lines = f.read().splitlines()

    preserve_map = preserve_existing_keys or {}
    pending = {sec: dict(kv) for sec, kv in updates.items()}
    out_lines: List[str] = []
    current_sec: Optional[str] = None

    def _flush_section(sec_name: Optional[str]) -> None:
        if sec_name and sec_name in pending:
            rem = pending.pop(sec_name)
            for k, v in rem.items():
                out_lines.append(f"{k}={v}")

    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]") and "=" not in stripped:
            _flush_section(current_sec)
            current_sec = stripped[1:-1].strip()
            out_lines.append(raw)
            continue

        if current_sec and current_sec in pending and "=" in stripped and not stripped.startswith((";", "#")):
            k = stripped.split("=", 1)[0].strip()
            if k in pending[current_sec]:
                if k in preserve_map.get(current_sec, set()):
                    pending[current_sec].pop(k)
                    out_lines.append(raw)
                else:
                    new_val = pending[current_sec].pop(k)
                    out_lines.append(f"{k}={new_val}")
                continue
        out_lines.append(raw)

    _flush_section(current_sec)

    for sec, kv in pending.items():
        if not kv:
            continue
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append(f"[{sec}]")
        for k, v in kv.items():
            out_lines.append(f"{k}={v}")

    if not existed:
        BackupManager.register_created_file(game_dir, ini_path)
    atomic_write_text(ini_path, "\n".join(out_lines).rstrip() + "\n", encoding="utf-8", newline="\n")


class UnrealInjector:
    """يدير بناء حزمة ~mods/z_ArabicL10n_P.pak ونشر الخط العربي وتحديث DefaultEngine.ini."""

    def _resolve_dirs(self, engine: EngineInfo) -> Tuple[str, str, str, str, str]:
        game_dir = os.path.abspath(engine.game_dir)
        layout = find_unreal_layout(game_dir)

        proj_name = layout.project_name or os.path.basename(game_dir) or "Game"
        content_dir = layout.content_dir
        if not content_dir:
            candidate = os.path.join(game_dir, proj_name, "Content")
            if os.path.isdir(os.path.join(game_dir, proj_name)):
                content_dir = candidate
            else:
                content_dir = os.path.join(game_dir, "Content")

        proj_dir = os.path.dirname(content_dir)
        paks_dir = layout.paks_dir or os.path.join(content_dir, "Paks")
        config_dir = layout.config_dir or os.path.join(proj_dir, "Config")
        return game_dir, proj_name, content_dir, paks_dir, config_dir

    def _collect_staged_pak_entries(
        self,
        game_dir: str,
        content_dir: str,
    ) -> Tuple[Dict[str, bytes], str, int]:
        manifest = load_staging_manifest(game_dir)
        entries_meta = manifest.get("entries", {})
        if not entries_meta:
            alt_root = os.path.dirname(content_dir)
            if alt_root and alt_root != game_dir:
                manifest = load_staging_manifest(alt_root)
                entries_meta = manifest.get("entries", {})

        packed: Dict[str, bytes] = {}
        mount_point = "../../../"
        pak_version = 8

        for internal_path, meta in entries_meta.items():
            staged_path = meta.get("staged_path")
            if staged_path and os.path.isfile(staged_path):
                with open(staged_path, "rb") as f:
                    packed[internal_path] = f.read()
                if isinstance(meta.get("pak_version"), int):
                    ver = int(meta["pak_version"])
                    pak_version = 4 if ver < 8 else 8

        # المسارات المخزنة مُطبّعة مسبقاً نسبة إلى الجذر ../../../ لمنع تكرار اسم المشروع
        return packed, mount_point, pak_version

    def _detect_base_pak_version(self, game_dir: str) -> int:
        layout = find_unreal_layout(game_dir)
        for p in layout.pak_files[:2]:
            pinfo = parse_pak_header_and_entries(p)
            if pinfo is not None:
                return 4 if pinfo.version < 8 else 8
        return 8

    def _deploy_font_and_ini(
        self,
        game_dir: str,
        content_dir: str,
        config_dir: str,
        custom_font_path: Optional[str] = None,
    ) -> Tuple[Optional[str], List[str]]:
        src_font = resolve_arabic_font_path(custom_font_path)
        if not src_font:
            return None, []

        fonts_dir = os.path.join(content_dir, "Localization", "ArabicFonts")
        os.makedirs(fonts_dir, exist_ok=True)
        font_filename = os.path.basename(src_font)
        dst_font = os.path.join(fonts_dir, font_filename)

        existed = os.path.exists(dst_font)
        if existed and not _is_tracked_created_file(game_dir, dst_font):
            BackupManager.backup_file(game_dir, dst_font)
        elif not existed:
            BackupManager.register_created_file(game_dir, dst_font)
        atomic_copy_file(src_font, dst_font)

        ini_path = os.path.join(config_dir, "DefaultEngine.ini")
        _upsert_ini_file(
            game_dir,
            ini_path,
            updates={
                "Internationalization": {
                    "+LocalizationPaths": "%GAMEDIR%Content/Localization/Game",
                    "ShouldUseLocalizedPropertyNames": "True",
                },
                "/Script/Engine.Engine": {
                    "ArabicFontPath": f"Content/Localization/ArabicFonts/{font_filename}",
                },
            },
            preserve_existing_keys={
                "Internationalization": {"+LocalizationPaths", "LocalizationPaths"},
            },
        )
        return dst_font, [dst_font, ini_path]

    def _write_mod_pak(
        self,
        game_dir: str,
        paks_dir: str,
        packed_files: Dict[str, bytes],
        mount_point: str,
        pak_version: int,
    ) -> str:
        mods_dir = os.path.join(paks_dir, MODS_SUBDIR)
        os.makedirs(mods_dir, exist_ok=True)
        mod_pak_path = os.path.join(mods_dir, MOD_PAK_FILENAME)
        existed = os.path.exists(mod_pak_path)
        if existed and not _is_tracked_created_file(game_dir, mod_pak_path):
            BackupManager.backup_file(game_dir, mod_pak_path)

        blob = build_unreal_pak_blob(
            packed_files,
            mount_point=mount_point,
            version=pak_version,
        )
        if not existed:
            BackupManager.register_created_file(game_dir, mod_pak_path)
        atomic_write_bytes(mod_pak_path, blob)
        return mod_pak_path

    def inject(
        self,
        engine: EngineInfo,
        skip_runtime_font: bool = False,
        custom_font_path: Optional[str] = None,
    ) -> InjectionResult:
        """نشر حزمة ~mods/*.pak (للنصوص المعبأة) والخط العربي وتهيئة DefaultEngine.ini."""
        game_dir, proj_name, content_dir, paks_dir, config_dir = self._resolve_dirs(engine)
        staged_files, mount_point, pak_version = self._collect_staged_pak_entries(
            game_dir, content_dir
        )

        if skip_runtime_font:
            if not staged_files:
                return InjectionResult(
                    success=True,
                    method="skipped",
                    message="تم تعديل ملف .locres الخارجي وتخطي حقن الخط العربي بناءً على طلب المستخدم.",
                )
            mod_pak = self._write_mod_pak(
                game_dir, paks_dir, staged_files, mount_point, pak_version
            )
            return InjectionResult(
                success=True,
                method="unreal_mod_pak_only",
                output_files=[mod_pak],
                message=(
                    f"تم إنشاء حزمة التعديل غير المُتلفة للأصل ({os.path.basename(mod_pak)}) "
                    "مع تخطي حقن الخط العربي."
                ),
            )

        dst_font, font_outputs = self._deploy_font_and_ini(
            game_dir, content_dir, config_dir, custom_font_path=custom_font_path
        )
        if not dst_font:
            return InjectionResult(
                success=False,
                method="font_not_found",
                message="تعذر العثور على ملف الخط العربي (IBMPlexSansArabic-Regular.ttf).",
            )

        output_files = list(font_outputs)
        layout = find_unreal_layout(game_dir)
        font_filename = os.path.basename(dst_font)
        proj_prefix = f"{proj_name}/" if os.path.isdir(os.path.join(game_dir, proj_name, "Content")) else ""

        if staged_files or layout.pak_files:
            pak_payload = dict(staged_files)
            with open(dst_font, "rb") as f:
                font_bytes = f.read()
            pak_payload[f"{proj_prefix}Content/Localization/ArabicFonts/{font_filename}"] = font_bytes
            pak_payload[f"{proj_prefix}Content/Slate/Fonts/{font_filename}"] = font_bytes
            if not staged_files:
                pak_version = self._detect_base_pak_version(game_dir)
            mod_pak = self._write_mod_pak(
                game_dir, paks_dir, pak_payload, mount_point, pak_version
            )
            output_files.append(mod_pak)
            method = "unreal_mods_pak"
            sig_note = (
                " تنبيه: توجد ملفات توقيع (.sig) بجانب حزم اللعبة؛ قد يتطلب تحميل ~mods تعطيل التحقق من التوقيع."
                if layout.sig_files
                else ""
            )
            msg = (
                f"تم بناء حزمة التعديل '{os.path.basename(mod_pak)}' داخل Content/Paks/~mods/ "
                f"ونشر الخط العربي ({font_filename}) وتحديث DefaultEngine.ini دون لمس الحزم الأصلية. "
                "ملاحظة تقنية: تعتمد بعض ألعاب UE المجمّعة على أصول UFontFace ثنائية (.uasset/.uexp) "
                f"عند غياب خط Slate احتياطي يدعم العربية.{sig_note}"
            )
        else:
            method = "unreal_font_config"
            msg = (
                f"تم نشر الخط العربي ({font_filename}) وتحديث DefaultEngine.ini بنجاح. "
                "ملاحظة تقنية: قد تتطلب الألعاب المجمّعة بدون خط Slate عربي احتياطي أصل UFontFace (.uasset)."
            )

        return InjectionResult(
            success=True,
            method=method,
            output_files=output_files,
            message=msg,
        )

    def update_arabic_font(
        self,
        engine: EngineInfo,
        custom_font_path: Optional[str] = None,
    ) -> InjectionResult:
        """تحديث الخط العربي الافتراضي أو المخصص وإعادة تغليف ~mods دون مساس بنصوص .locres."""
        return self.inject(
            engine,
            skip_runtime_font=False,
            custom_font_path=custom_font_path,
        )
