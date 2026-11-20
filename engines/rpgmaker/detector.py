# -*- coding: utf-8 -*-
"""كاشف محرك RPG Maker MV و RPG Maker MZ بدون تعديل ملفات اللعبة."""

import os
import struct
from typing import Optional

from core.models import EngineInfo


def _detect_pe_architecture(exe_path: str) -> str:
    """قراءة ترويسة PE لتحديد المعمارية (x64 أو x86) للملف التنفيذي."""
    try:
        with open(exe_path, "rb") as f:
            if f.read(2) != b"MZ":
                return "unknown"
            f.seek(0x3C)
            pe_offset = struct.unpack("<I", f.read(4))[0]
            f.seek(pe_offset)
            if f.read(4) != b"PE\x00\x00":
                return "unknown"
            machine = struct.unpack("<H", f.read(2))[0]
            if machine == 0x8664:
                return "x64"
            if machine == 0x014C:
                return "x86"
    except OSError:
        pass
    return "unknown"


def _find_game_exe(game_dir: str) -> Optional[str]:
    """البحث عن الملف التنفيذي للعبة (Game.exe أو nw.exe أو أي exe رئيسي)."""
    preferred = ("Game.exe", "nw.exe")
    for name in preferred:
        candidate = os.path.join(game_dir, name)
        if os.path.isfile(candidate):
            return candidate
    try:
        for entry in sorted(os.listdir(game_dir)):
            if entry.lower().endswith(".exe"):
                full = os.path.join(game_dir, entry)
                if os.path.isfile(full):
                    return full
    except OSError:
        pass
    return None


class RPGMakerDetector:
    """يكتشف ألعاب RPG Maker MV (www/data/System.json) و MZ (data/System.json)."""

    def detect(self, game_dir: str) -> EngineInfo:
        abs_dir = os.path.abspath(game_dir)
        if not os.path.isdir(abs_dir):
            return EngineInfo(
                engine_name="RPG Maker",
                supported=False,
                game_dir=abs_dir,
                unsupported_reason=f"المجلد غير موجود: {abs_dir}",
            )

        mv_data_dir = os.path.join(abs_dir, "www", "data")
        mv_system = os.path.join(mv_data_dir, "System.json")
        mz_data_dir = os.path.join(abs_dir, "data")
        mz_system = os.path.join(mz_data_dir, "System.json")

        is_mv = os.path.isfile(mv_system)
        is_mz = os.path.isfile(mz_system)

        if not is_mv and not is_mz:
            return EngineInfo(
                engine_name="RPG Maker",
                supported=False,
                game_dir=abs_dir,
                unsupported_reason=(
                    "لم يُعثر على ملفات RPG Maker MV (www/data/System.json) "
                    "أو RPG Maker MZ (data/System.json)."
                ),
            )

        # إذا وُجد المساران معاً، نميز عبر ملفات النواة الخاصة بكل إصدار
        if is_mv and is_mz:
            if os.path.isfile(os.path.join(abs_dir, "js", "rmmz_core.js")) or os.path.isfile(
                os.path.join(abs_dir, "css", "game.css")
            ):
                is_mv = False
            else:
                is_mz = False

        if is_mv:
            engine_name = "RPG Maker MV"
            backend = "MV"
            data_dir = mv_data_dir
        elif os.path.isfile(os.path.join(abs_dir, "js", "rpg_core.js")) or os.path.isfile(
            os.path.join(abs_dir, "Game.rpgproject")
        ):
            engine_name = "RPG Maker MV"
            backend = "MV"
            data_dir = mz_data_dir
        else:
            engine_name = "RPG Maker MZ"
            backend = "MZ"
            data_dir = mz_data_dir

        exe_path = _find_game_exe(abs_dir)
        arch = _detect_pe_architecture(exe_path) if exe_path else "x64"
        if arch == "unknown":
            nw_dll = os.path.join(abs_dir, "nw.dll")
            if os.path.isfile(nw_dll):
                arch = _detect_pe_architecture(nw_dll)
        if arch == "unknown":
            arch = "x64"

        return EngineInfo(
            engine_name=engine_name,
            supported=True,
            game_dir=abs_dir,
            architecture=arch,
            scripting_backend=backend,
            exe_path=exe_path,
            data_dir=data_dir,
            requires_arabic_shaping=False,
        )
