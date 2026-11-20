# -*- coding: utf-8 -*-
"""حاقن الخط العربي ودعم RTL في محرك RPG Maker MV / MZ عبر gamefont.css."""

import os
import re
from typing import List, Optional, Tuple

from core.backup import BackupManager, atomic_copy_file, atomic_write_text
from core.models import EngineInfo, InjectionResult
from engines.rpgmaker.plugin_patch import PLUGIN_NAME, deploy_arabic_text_plugin
from engines.unity.injector import resolve_arabic_font_path


ARABIC_CSS_START = "/* AutoTranslator Arabic Font & RTL START */"
ARABIC_CSS_END = "/* AutoTranslator Arabic Font & RTL END */"
ARABIC_CSS_BLOCK_RE = re.compile(
    re.escape(ARABIC_CSS_START) + r".*?" + re.escape(ARABIC_CSS_END),
    re.DOTALL,
)


FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*['\"]?([^;'\"\r\n}]+)['\"]?", re.IGNORECASE)


def _extract_css_font_families(css_text: str) -> List[str]:
    """استخراج أسماء الخطوط المعرّفة مسبقاً في ملف CSS اللعبة لتغطيتها بالعربية عبر unicode-range."""
    families: List[str] = []
    seen = {"arabicgamefont", "gamefont", "rmmz-mainfont"}
    for match in FONT_FAMILY_RE.finditer(css_text or ""):
        raw = match.group(1).strip().strip("'\"")
        # أخذ أول اسم خط في حال وجود قائمة خطوط مفصولة بفواصل
        first = raw.split(",")[0].strip().strip("'\"")
        if not first:
            continue
        low = first.lower()
        if low in seen or low in ("sans-serif", "serif", "monospace", "inherit", "initial"):
            continue
        seen.add(low)
        families.append(first)
    return families


def _build_arabic_css_block(font_url: str, extra_families: Optional[List[str]] = None) -> str:
    """بناء كتلة CSS لحقن الخط العربي وتفعيل اتجاه RTL في NW.js / Chromium."""
    unicode_range = "U+0600-06FF, U+0750-077F, U+08A0-08FF, U+FB50-FDFF, U+FE70-FEFF"
    rules = [
        f"{ARABIC_CSS_START}",
        "@font-face {",
        "    font-family: 'ArabicGameFont';",
        f"    src: url('{font_url}') format('truetype');",
        "}",
    ]
    target_families = ["GameFont", "rmmz-mainfont"] + list(extra_families or [])
    for fam in target_families:
        safe_fam = fam.replace("'", "\\'")
        rules.extend([
            "@font-face {",
            f"    font-family: '{safe_fam}';",
            f"    src: url('{font_url}') format('truetype');",
            f"    unicode-range: {unicode_range};",
            "}",
        ])
    rules.extend([
        "body, canvas {",
        "    direction: rtl;",
        "    unicode-bidi: plaintext;",
        "}",
        f"{ARABIC_CSS_END}",
        "",
    ])
    return "\n".join(rules)


def _upsert_arabic_css_file(game_dir: str, css_path: str, font_url: str) -> None:
    """تحديث أو إنشاء ملف CSS بكتلة الخط العربي ودعم RTL مع النسخ الاحتياطي الكامل."""
    css_existed = os.path.exists(css_path)
    existing_css = ""
    if css_existed:
        BackupManager.backup_file(game_dir, css_path)
        with open(css_path, "r", encoding="utf-8", errors="replace") as f:
            existing_css = f.read()

    # استخراج خطوط اللعبة الأصلية خارج كتلة الأداة السابقة فقط
    clean_base = ARABIC_CSS_BLOCK_RE.sub("", existing_css)
    extra_families = _extract_css_font_families(clean_base)
    css_block = _build_arabic_css_block(font_url, extra_families=extra_families)
    if ARABIC_CSS_BLOCK_RE.search(existing_css):
        new_css = ARABIC_CSS_BLOCK_RE.sub(css_block.rstrip("\n"), existing_css)
    else:
        sep = "\n\n" if existing_css.strip() else ""
        new_css = existing_css.rstrip() + sep + css_block

    if not css_existed:
        BackupManager.register_created_file(game_dir, css_path)
    atomic_write_text(css_path, new_css, encoding="utf-8")


class RPGMakerInjector:
    """ينشر الخط العربي ويحدّث gamefont.css (و css/game.css في MZ) مع تتبع كامل في BackupManager.

    ويُنشر أيضاً (افتراضياً) إضافة AutoTranslator_ArabicText.js لإصلاح رسم الحوار حرفاً حرفاً.
    """

    def __init__(self, enable_text_fix: bool = True, text_fix_mode: str = "word") -> None:
        self.enable_text_fix = enable_text_fix
        self.text_fix_mode = text_fix_mode

    @staticmethod
    def _resolve_fonts_dir(engine: EngineInfo) -> str:
        """تحديد مجلد الخطوط حسب بنية اللعبة (www/fonts أو fonts)."""
        if engine.data_dir and os.path.basename(os.path.dirname(engine.data_dir)).lower() == "www":
            return os.path.join(engine.game_dir, "www", "fonts")
        if os.path.isdir(os.path.join(engine.game_dir, "www")) and not os.path.isdir(
            os.path.join(engine.game_dir, "data")
        ):
            return os.path.join(engine.game_dir, "www", "fonts")
        return os.path.join(engine.game_dir, "fonts")

    def deploy_arabic_font(
        self,
        engine: EngineInfo,
        custom_font_path: Optional[str] = None,
    ) -> Tuple[Optional[str], List[str]]:
        """نسخ الخط العربي إلى مجلد خطوط اللعبة وتحديث gamefont.css (و css/game.css في MZ)."""
        src_font = resolve_arabic_font_path(custom_font_path)
        if not src_font or not os.path.isdir(engine.game_dir):
            return None, []

        fonts_dir = self._resolve_fonts_dir(engine)
        os.makedirs(fonts_dir, exist_ok=True)

        font_filename = os.path.basename(src_font)
        dst_font = os.path.join(fonts_dir, font_filename)
        font_existed = os.path.exists(dst_font)
        if font_existed:
            BackupManager.backup_file(engine.game_dir, dst_font)
        else:
            BackupManager.register_created_file(engine.game_dir, dst_font)
        atomic_copy_file(src_font, dst_font)

        updated_css_files: List[str] = []
        gamefont_css = os.path.join(fonts_dir, "gamefont.css")
        _upsert_arabic_css_file(engine.game_dir, gamefont_css, font_filename)
        updated_css_files.append(gamefont_css)

        # في RPG Maker MZ، قد يربط index.html ملف css/game.css فقط بدون fonts/gamefont.css
        base_root = os.path.dirname(fonts_dir)
        mz_game_css = os.path.join(base_root, "css", "game.css")
        if os.path.isfile(mz_game_css):
            _upsert_arabic_css_file(engine.game_dir, mz_game_css, f"../fonts/{font_filename}")
            updated_css_files.append(mz_game_css)

        return dst_font, updated_css_files

    def inject(
        self,
        engine: EngineInfo,
        skip_runtime_font: bool = False,
        custom_font_path: Optional[str] = None,
    ) -> InjectionResult:
        """تنفيذ حقن الخط العربي ودعم RTL لمحرك RPG Maker MV/MZ."""
        if skip_runtime_font:
            return InjectionResult(
                success=True,
                method="skipped",
                message="تم تخطي حقن الخط العربي بناءً على طلب المستخدم.",
            )

        dst_font, css_files = self.deploy_arabic_font(engine, custom_font_path=custom_font_path)
        if not dst_font or not css_files:
            return InjectionResult(
                success=False,
                method="font_not_found",
                message="تعذر العثور على ملف الخط العربي (IBMPlexSansArabic-Regular.ttf).",
            )

        output_files: List[str] = [dst_font] + css_files
        message = (
            f"تم نشر الخط العربي ({os.path.basename(dst_font)}) وتحديث "
            f"gamefont.css بدعم العربية وRTL بنجاح."
        )
        if self.enable_text_fix:
            plugin_files, plugin_warnings = deploy_arabic_text_plugin(
                engine, mode=self.text_fix_mode
            )
            output_files.extend(plugin_files)
            if plugin_files:
                message += f" وتفعيل إضافة {PLUGIN_NAME}.js لرسم الحوار العربي متصلاً."
            for w in plugin_warnings:
                message += f" [تنبيه] {w}"
        return InjectionResult(
            success=True,
            method="rpgmaker_css_font",
            output_files=output_files,
            message=message,
        )

    def update_arabic_font(
        self,
        engine: EngineInfo,
        custom_font_path: Optional[str] = None,
    ) -> InjectionResult:
        """إضافة أو تحديث الخط العربي الافتراضي على لعبة RPG Maker معرّبة مسبقاً."""
        return self.inject(engine, skip_runtime_font=False, custom_font_path=custom_font_path)
