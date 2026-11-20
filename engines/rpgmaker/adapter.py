# -*- coding: utf-8 -*-
"""محوّل محرك RPG Maker MV / MZ الكامل عبر GameEngineAdapter."""

from typing import List, Optional, Tuple

from core.models import CandidateFile, EngineInfo, InjectionResult
from engines.base import GameEngineAdapter
from engines.rpgmaker.detector import RPGMakerDetector
from engines.rpgmaker.injector import RPGMakerInjector
from engines.rpgmaker.scanner import (
    RPGMakerLanguageScanner,
    is_translatable_entry_in_data,
    is_translatable_value,
)


class RPGMakerAdapter(GameEngineAdapter):
    """تنفيذ واجهة GameEngineAdapter لمحرك RPG Maker MV و MZ."""

    def __init__(self) -> None:
        self.detector = RPGMakerDetector()
        self.scanner = RPGMakerLanguageScanner()
        self.injector = RPGMakerInjector()

    def configure(self, options=None) -> None:
        super().configure(options)
        opts = self.options
        self.injector.enable_text_fix = bool(opts.get("rpgmaker_text_fix", True))
        mode = str(opts.get("rpgmaker_text_fix_mode", "word")).lower()
        self.injector.text_fix_mode = "line" if mode == "line" else "word"

    def detect(self, game_dir: str) -> EngineInfo:
        return self.detector.detect(game_dir)

    def scan_language_files(self, engine: EngineInfo) -> Tuple[List[CandidateFile], List[str]]:
        return self.scanner.scan(engine)

    def select_language_files(
        self,
        engine: EngineInfo,
        candidates: List[CandidateFile],
        min_score: int = 45,
    ) -> List[CandidateFile]:
        """تحديد جميع ملفات البيانات القابلة للترجمة في RPG Maker دفعة واحدة."""
        return [
            c for c in candidates
            if c.score >= min_score and c.translatable_entries > 0
        ]

    def is_translatable_value(self, key: str, value: str) -> bool:
        return is_translatable_value(key, value)

    def is_translatable_entry(
        self,
        doc: object,
        path_tuple: tuple,
        key: str,
        value: str,
    ) -> bool:
        raw_data = getattr(doc, "raw_data", None)
        return is_translatable_entry_in_data(raw_data, path_tuple, key, value)

    def inject(
        self,
        engine: EngineInfo,
        language_file: Optional[CandidateFile],
        skip_runtime_font: bool = False,
    ) -> InjectionResult:
        return self.injector.inject(engine, skip_runtime_font=skip_runtime_font)

    def update_font(
        self,
        engine: EngineInfo,
        custom_font_path: Optional[str] = None,
    ) -> InjectionResult:
        return self.injector.update_arabic_font(engine, custom_font_path=custom_font_path)
