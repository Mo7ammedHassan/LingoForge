# -*- coding: utf-8 -*-
"""هياكل البيانات الموحدة بين جميع مراحل خط العمل والمحركات."""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class EngineInfo:
    """معلومات المحرك والمعمارية المكتشفة لمجلد اللعبة."""

    engine_name: str
    supported: bool
    game_dir: str
    architecture: str = "unknown"
    scripting_backend: str = "unknown"
    exe_path: Optional[str] = None
    data_dir: Optional[str] = None
    managed_dir: Optional[str] = None
    il2cpp_metadata_path: Optional[str] = None
    il2cpp_interop_dir: Optional[str] = None
    mod_loader: str = "none"
    has_tmp: bool = False
    has_ugui: bool = False
    requires_arabic_shaping: bool = True
    unsupported_reason: str = ""
    unity_version: str = ""
    localization_systems: List[str] = field(default_factory=list)
    risk_warnings: List[str] = field(default_factory=list)


@dataclass
class CandidateFile:
    """ملف لغة مرشح مع نتيجة التقييم وأسبابه."""

    path: str
    relative_path: str
    format_name: str
    score: int
    reasons: List[str] = field(default_factory=list)
    dll_matched: bool = False
    contains_arabic: bool = False
    total_entries: int = 0
    translatable_entries: int = 0
    skip_backup: bool = False


@dataclass
class LocalizationEntry:
    """عنصر نصي مستخرج من ملف اللغة."""

    key: str
    value: str
    translatable: bool = True


@dataclass
class TranslationStats:
    """إحصائيات عملية الترجمة والحماية."""

    total_keys: int = 0
    translatable_keys: int = 0
    unique_lines: int = 0
    cached_lines: int = 0
    translated_lines: int = 0
    repaired_placeholders: int = 0
    batches_sent: int = 0


@dataclass
class InjectionResult:
    """نتيجة بناء وحقن الخط العربي وقت التشغيل."""

    success: bool
    method: str
    compiled_dll: Optional[str] = None
    output_files: List[str] = field(default_factory=list)
    message: str = ""


@dataclass
class PipelineReport:
    """التقرير النهائي الشامل لعملية الفحص أو التعريب."""

    engine: EngineInfo
    candidates: List[CandidateFile] = field(default_factory=list)
    selected_file: Optional[CandidateFile] = None
    selected_files: List[CandidateFile] = field(default_factory=list)
    dll_hints: List[str] = field(default_factory=list)
    stats: Optional[TranslationStats] = None
    injection: Optional[InjectionResult] = None
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    success: bool = False
    interrupted: bool = False
