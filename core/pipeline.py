# -*- coding: utf-8 -*-
"""المنسّق العام لخط سير التعريب التلقائي (AutoTranslatorPipeline)."""

import inspect
import os
import re
import shutil
import threading
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from arabic.protector import mask_line, unmask_line, verify_placeholders
from arabic.shaper import process_text
from core.backup import (
    COMMIT_TMP_SUFFIX,
    BackupManager,
    TransactionJournal,
    atomic_copy_file,
    compute_sha256,
    is_file_likely_arabic,
)
from core.models import CandidateFile, EngineInfo, PipelineReport, TranslationStats
from engines.base import GameEngineAdapter
from engines.registry import detect_engine_adapter, get_default_adapter
from formats.assets_override_handler import (
    CAPTURED_TEXTS_FILENAME,
    load_captured_texts_file,
    merge_translations_into_override,
)
from formats.base import ParsedDocument, PathTuple
from formats.registry import get_format_handler
from translators.google_batch import (
    GoogleBatchTranslator,
    TranslationInterruptedError,
    TranslatorProvider,
)


ASCII_LETTER_RE = re.compile(r"[A-Za-z]")


class AutoTranslatorPipeline:
    """يدير كل مراحل الفحص والنسخ الاحتياطي والترجمة والتشبيك والحقن تلقائياً."""

    MIN_CANDIDATE_SCORE = 45

    def __init__(
        self,
        adapter: Optional[GameEngineAdapter] = None,
        translator: Optional[TranslatorProvider] = None,
        shape_arabic: bool = True,
        adapters: Optional[Sequence[GameEngineAdapter]] = None,
        engine_options: Optional[Dict[str, object]] = None,
    ) -> None:
        self._fixed_adapter = adapter
        self._adapters = list(adapters) if adapters is not None else None
        self.adapter: GameEngineAdapter = adapter or get_default_adapter()
        self.translator = translator or GoogleBatchTranslator()
        self.shape_arabic = shape_arabic
        self.engine_options: Dict[str, object] = dict(engine_options or {})

    def _configure_adapter(self, adapter: GameEngineAdapter) -> None:
        configure = getattr(adapter, "configure", None)
        if callable(configure):
            configure(self.engine_options)

    def _resolve_adapter(self, game_dir: str) -> Tuple[GameEngineAdapter, EngineInfo]:
        """تحديد محوّل المحرك المناسب لمجلد اللعبة وتحديث self.adapter."""
        if self._fixed_adapter is not None:
            engine = self._fixed_adapter.detect(game_dir)
            self.adapter = self._fixed_adapter
            self._configure_adapter(self._fixed_adapter)
            return self._fixed_adapter, engine
        adapter, engine = detect_engine_adapter(game_dir, adapters=self._adapters)
        self.adapter = adapter
        self._configure_adapter(adapter)
        return adapter, engine

    def scan(self, game_dir: str) -> PipelineReport:
        """فحص مجلد اللعبة واكتشاف المحرك وملفات اللغة المرشحة بدون تعديل أي ملف."""
        adapter, engine = self._resolve_adapter(game_dir)
        report = PipelineReport(engine=engine)
        for rw in getattr(engine, "risk_warnings", []):
            if rw not in report.warnings:
                report.warnings.append(rw)
        interrupted_summary = TransactionJournal.get_interrupted_session_summary(game_dir)
        if interrupted_summary.get("needs_recovery"):
            report.warnings.append(
                "تنبيه أمان: توجد عملية التزام متوقفة أثناء الكتابة؛ ستتم استعادة الملفات الأصلية تلقائياً قبل المتابعة مع الحفاظ على التقدم."
            )
        elif interrupted_summary.get("has_interrupted"):
            done = interrupted_summary.get("completed_lines", 0)
            total = interrupted_summary.get("total_unique_lines", 0)
            pct = interrupted_summary.get("percent", 0)
            report.warnings.append(
                f"يوجد تقدم ترجمة محفوظ مسبقاً ({done}/{total} — {pct}%)؛ ملفات اللعبة سليمة ولم تُمس."
            )
        if not engine.supported:
            report.errors.append(engine.unsupported_reason)
            return report

        candidates, dll_hints = adapter.scan_language_files(engine)
        report.candidates = candidates
        report.dll_hints = dll_hints
        selected_files = adapter.select_language_files(
            engine, candidates, min_score=self.MIN_CANDIDATE_SCORE
        )
        if selected_files:
            report.selected_files = selected_files
            report.selected_file = selected_files[0]
            report.success = True
        else:
            report.warnings.append(
                "لم يُعثر على ملف لغة بدرجة ثقة كافية (>= 45)؛ قد تكون النصوص مدمجة داخل ملفات اللعبة أو الكود."
            )
        return report

    def _resolve_clean_source(
        self, candidate: CandidateFile, warnings: List[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        """تحديد ملف المصدر النظيف للقراءة مع حماية النسخة الإنجليزية لو كان الملف الحالي معرباً."""
        if candidate.skip_backup or candidate.format_name in ("unity_assets", "unity_code"):
            return candidate.path, None

        bak_path = candidate.path + ".bak"
        if candidate.contains_arabic:
            if os.path.isfile(bak_path) and not is_file_likely_arabic(bak_path):
                warnings.append(
                    f"الملف '{candidate.relative_path}' يحتوي عربية مسبقاً؛ تم استخدام النسخة النظيفة '{os.path.basename(bak_path)}' كمصدر للترجمة."
                )
                return bak_path, None
            return None, (
                f"الملف '{candidate.relative_path}' معرّب مسبقاً ولا توجد نسخة .bak إنجليزية نظيفة بجانبه؛ "
                "تم الإيقاف الآمن لعدم إتلاف الترجمة الموجودة."
            )
        return candidate.path, None

    def translate(
        self,
        game_dir: str,
        dry_run: bool = False,
        skip_inject: bool = False,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> PipelineReport:
        """تشغيل دورة التعريب بنظام الالتزام الآمن على مرحلتين لحماية اللعبة عند انقطاع الكهرباء."""
        pre_warnings: List[str] = []
        if not dry_run:
            recovered, rec_files, rec_warns = TransactionJournal.recover_if_interrupted_commit(game_dir)
            if recovered:
                pre_warnings.append(
                    f"تم اكتشاف انقطاع أثناء الالتزام السابق واستعادة ({len(rec_files)}) ملفاً لحالتها الأصلية بأمان مع الحفاظ على الكاش."
                )
            pre_warnings.extend(rec_warns)

        # المرحلة 0: الفحص والقراءة فقط دون لمس ملفات اللعبة
        report = self.scan(game_dir)
        if pre_warnings:
            report.warnings = pre_warnings + report.warnings
        if not report.engine.supported:
            return report

        adapter = self.adapter
        targets = report.selected_files or (
            [report.selected_file] if report.selected_file is not None else []
        )
        if not targets:
            report.errors.append(
                "لم يتم العثور على ملف لغة قياسي أو نصوص قابلة للاستخراج بدرجة ثقة كافية."
            )
            report.success = False
            return report

        prepared_files: List[Tuple[CandidateFile, object, ParsedDocument, List[Tuple[PathTuple, str, list]]]] = []
        stats = TranslationStats()
        unique_lines: List[str] = []
        seen_lines = set()

        for selected in targets:
            handler = get_format_handler(selected.format_name)
            if handler is None:
                report.errors.append(
                    f"تم اكتشاف ملف اللغة '{selected.relative_path}' بصيغة ({selected.format_name})، "
                    "وهذه الصيغة غير مدعومة حالياً."
                )
                report.success = False
                return report

            source_read_path, resolve_err = self._resolve_clean_source(selected, report.warnings)
            if resolve_err or not source_read_path:
                report.errors.append(resolve_err or "تعذر تحديد مصدر قراءة سليم.")
                report.success = False
                return report

            doc = handler.load(source_read_path)
            if doc.game_dir is None:
                doc.game_dir = os.path.abspath(game_dir)
            flat_items = doc.entries
            stats.total_keys += len(flat_items)
            parsed_entries: List[Tuple[PathTuple, str, list]] = []

            for path_tuple, flat_key, orig_val in flat_items:
                if not adapter.is_translatable_entry(doc, path_tuple, flat_key, orig_val):
                    continue
                stats.translatable_keys += 1
                line_records = self._prepare_line_records(orig_val, seen_lines, unique_lines)
                parsed_entries.append((path_tuple, orig_val, line_records))

            prepared_files.append((selected, handler, doc, parsed_entries))

        stats.unique_lines = len(unique_lines)
        report.stats = stats

        if dry_run:
            report.success = True
            return report

        # المرحلة 1: الترجمة في العزل التام مع حفظ كل دفعة بـ WAL + fsync (اللعبة لم تُمس بعد)
        cache_path = os.path.join(
            BackupManager.get_state_dir(game_dir), "translation_cache.json"
        )
        existing_cache = GoogleBatchTranslator._load_cache(cache_path)
        initial_completed = sum(1 for ln in unique_lines if ln in existing_cache)
        target_meta = [
            {
                "relative_path": sel.relative_path,
                "format_name": sel.format_name,
                "sha256": compute_sha256(sel.path) if os.path.isfile(sel.path) else "",
            }
            for sel in targets
        ]
        session_state: Dict[str, object] = {
            "status": "translating",
            "total_unique_lines": len(unique_lines),
            "completed_lines": initial_completed,
            "targets": target_meta,
            "staged_files": [],
        }
        TransactionJournal.save_session(game_dir, session_state)

        if cancel_event is not None and cancel_event.is_set():
            session_state["status"] = "paused"
            TransactionJournal.save_session(game_dir, session_state)
            report.interrupted = True
            report.success = False
            report.warnings.append(
                f"تم حفظ التقدم ({initial_completed}/{len(unique_lines)}) وإيقاف العملية بأمان؛ ملفات اللعبة لم تُمس."
            )
            return report

        def _on_batch_saved(done_count: int, total_count: int) -> None:
            session_state["completed_lines"] = done_count
            TransactionJournal.save_session(game_dir, session_state)

        tr_kwargs: Dict[str, object] = {
            "source_lang": "en",
            "target_lang": "ar",
            "cache_path": cache_path,
            "progress_cb": progress_cb,
        }
        try:
            sig_params = inspect.signature(self.translator.translate_lines).parameters
            has_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_params.values()
            )
            if has_kwargs or "cancel_event" in sig_params:
                tr_kwargs["cancel_event"] = cancel_event
            if has_kwargs or "batch_saved_cb" in sig_params:
                tr_kwargs["batch_saved_cb"] = _on_batch_saved
        except (TypeError, ValueError):
            pass

        try:
            translated_map, batches_sent, cached_count = self.translator.translate_lines(
                unique_lines, **tr_kwargs
            )
        except (TranslationInterruptedError, KeyboardInterrupt) as exc:
            latest_cache = GoogleBatchTranslator._load_cache(cache_path)
            done_now = sum(1 for ln in unique_lines if ln in latest_cache)
            done_count = max(done_now, int(getattr(exc, "completed_lines", initial_completed)))
            session_state["status"] = "paused"
            session_state["completed_lines"] = done_count
            TransactionJournal.save_session(game_dir, session_state)
            report.interrupted = True
            report.success = False
            report.warnings.append(
                f"تم حفظ التقدم ({done_count}/{len(unique_lines)}) وإيقاف العملية بأمان؛ ملفات اللعبة الأصلية سليمة ولم تُمس."
            )
            return report
        except Exception as exc:
            latest_cache = GoogleBatchTranslator._load_cache(cache_path)
            session_state["completed_lines"] = sum(1 for ln in unique_lines if ln in latest_cache)
            TransactionJournal.save_session(game_dir, session_state)
            report.errors.append(
                f"انقطعت الترجمة ({exc})؛ تم حفظ التقدم المحقق حتى الآن ولم تُعدّل ملفات اللعبة."
            )
            report.success = False
            return report

        stats.batches_sent = batches_sent
        stats.cached_lines = cached_count
        stats.translated_lines = len(translated_map)

        if cancel_event is not None and cancel_event.is_set():
            session_state["status"] = "paused"
            session_state["completed_lines"] = len(unique_lines)
            TransactionJournal.save_session(game_dir, session_state)
            report.interrupted = True
            report.success = False
            report.warnings.append(
                f"تم حفظ كامل الترجمات ({len(unique_lines)}/{len(unique_lines)}) وإيقاف العملية قبل التطبيق؛ ملفات اللعبة لم تُمس."
            )
            return report

        # المرحلة 2: التحضير والتحقق في مجلد معزول (staging/) — اللعبة لا تزال سليمة 100%
        session_state["status"] = "staging"
        session_state["completed_lines"] = len(unique_lines)
        TransactionJournal.save_session(game_dir, session_state)

        staging_dir = TransactionJournal.get_staging_dir(game_dir)
        if os.path.isdir(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)
        os.makedirs(staging_dir, exist_ok=True)

        should_shape = self.shape_arabic and adapter.requires_arabic_shaping(report.engine)
        direct_ops: List[Tuple[CandidateFile, str]] = []
        overlay_ops: List[Tuple[CandidateFile, object, ParsedDocument, Dict[PathTuple, str]]] = []
        staged_files_meta: List[Dict[str, str]] = []

        try:
            for selected, handler, doc, parsed_entries in prepared_files:
                replacements: Dict[PathTuple, str] = {}
                for path_tuple, orig_val, line_records in parsed_entries:
                    replacements[path_tuple] = self._assemble_line_records(
                        orig_val,
                        line_records,
                        translated_map,
                        stats,
                        should_shape,
                        report.warnings,
                        key_label=".".join(map(str, path_tuple)),
                    )

                is_overlay = selected.skip_backup or selected.format_name in (
                    "unity_assets",
                    "unity_code",
                    "godot_pck",
                    "unreal_pak",
                )
                if is_overlay:
                    overlay_ops.append((selected, handler, doc, replacements))
                else:
                    rel_clean = selected.relative_path.lstrip("\\/")
                    staged_path = os.path.join(staging_dir, rel_clean)
                    os.makedirs(os.path.dirname(staged_path), exist_ok=True)
                    handler.save(staged_path, doc, replacements)
                    if not os.path.isfile(staged_path) or os.path.getsize(staged_path) == 0:
                        raise RuntimeError(
                            f"فشل التحقق من الملف المجهز في المنطقة المعزولة: {selected.relative_path}"
                        )
                    direct_ops.append((selected, staged_path))
                    staged_files_meta.append(
                        {
                            "relative_path": selected.relative_path,
                            "staged_path": staged_path,
                            "sha256": compute_sha256(staged_path),
                        }
                    )
        except Exception as exc:
            shutil.rmtree(staging_dir, ignore_errors=True)
            session_state["status"] = "paused"
            TransactionJournal.save_session(game_dir, session_state)
            report.errors.append(
                f"فشل تجهيز الملفات المترجمة في المنطقة المعزولة ({exc})؛ لم تُمس ملفات اللعبة الأصلية."
            )
            report.success = False
            return report

        session_state["staged_files"] = staged_files_meta
        # المرحلة 3: الالتزام الذري السريع (نسخ احتياطي أولاً -> استبدال ذري -> تطبيق overlays -> حقن)
        session_state["status"] = "committing"
        TransactionJournal.save_session(game_dir, session_state)

        try:
            for selected, _ in direct_ops:
                _, bak_warnings = BackupManager.backup_file(game_dir, selected.path)
                report.warnings.extend(bak_warnings)

            for selected, staged_path in direct_ops:
                atomic_copy_file(staged_path, selected.path, tmp_suffix=COMMIT_TMP_SUFFIX)

            for selected, handler, doc, replacements in overlay_ops:
                handler.save(selected.path, doc, replacements)

            injection = adapter.inject(
                report.engine, report.selected_file, skip_runtime_font=skip_inject
            )
        except Exception as exc:
            _, rb_warns = BackupManager.rollback(game_dir)
            report.warnings.extend(rb_warns)
            report.errors.append(
                f"حدث خلل أثناء التطبيق وتمت استعادة اللعبة لحالتها الأصلية بأمان مع حفظ الترجمات: {exc}"
            )
            report.success = False
            return report

        TransactionJournal.clear_session(game_dir, clear_cache=False)
        report.injection = injection
        if not injection.success:
            report.warnings.append(injection.message)
        report.success = True
        return report

    @staticmethod
    def _prepare_line_records(
        orig_val: str, seen_lines: set, unique_lines: List[str]
    ) -> list:
        parts = re.split(r"(\r?\n)", orig_val)
        line_records = []
        for idx, part in enumerate(parts):
            if idx % 2 == 1:
                line_records.append(("sep", part))
            else:
                masked_obj = mask_line(part)
                if not ASCII_LETTER_RE.search(masked_obj.masked):
                    line_records.append(("raw", part))
                else:
                    line_records.append(("trans", masked_obj))
                    if masked_obj.masked not in seen_lines:
                        seen_lines.add(masked_obj.masked)
                        unique_lines.append(masked_obj.masked)
        return line_records

    @staticmethod
    def _assemble_line_records(
        orig_val: str,
        line_records: list,
        translated_map: Dict[str, str],
        stats: TranslationStats,
        should_shape: bool,
        warnings: List[str],
        key_label: str,
    ) -> str:
        chunks: List[str] = []
        for item in line_records:
            kind = item[0]
            if kind in ("sep", "raw"):
                chunks.append(item[1])
            else:
                masked_obj = item[1]
                trans_core = translated_map.get(masked_obj.masked, masked_obj.masked)
                unmasked, repairs = unmask_line(
                    trans_core,
                    masked_obj.placeholders,
                    masked_obj.lead_ws,
                    masked_obj.trail_ws,
                )
                stats.repaired_placeholders += repairs
                missing = verify_placeholders(masked_obj.placeholders, unmasked)
                if missing:
                    warnings.append(
                        f"فقدان متغيرات في مفتاح ({key_label}): {missing}؛ تم الإبقاء على الأصل الآمن."
                    )
                    return orig_val
                chunks.append(unmasked)
        assembled = "".join(chunks)
        return process_text(assembled) if should_shape else assembled

    def translate_captured_texts(
        self,
        game_dir: str,
        skip_inject: bool = False,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> PipelineReport:
        """استيراد وترجمة النصوص الملتقطة وقت التشغيل (arabic_captured_texts.txt) ودمجها في ArabicOverride.json."""
        adapter, engine = self._resolve_adapter(game_dir)
        report = PipelineReport(engine=engine, success=False)
        if not engine.supported:
            report.errors.append(engine.unsupported_reason or "محرك اللعبة غير مدعوم.")
            return report

        abs_dir = os.path.abspath(game_dir)
        captured_path = os.path.join(abs_dir, CAPTURED_TEXTS_FILENAME)
        if not os.path.isfile(captured_path):
            report.errors.append(
                f"لم يُعثر على ملف النصوص الملتقطة '{CAPTURED_TEXTS_FILENAME}' داخل مجلد اللعبة؛ "
                "شغّل اللعبة أولاً لالتقاط أي نصوص ديناميكية غير مترجمة."
            )
            return report

        raw_captured = load_captured_texts_file(captured_path)
        stats = TranslationStats(total_keys=len(raw_captured))
        unique_lines: List[str] = []
        seen_lines = set()
        prepared: List[Tuple[str, list]] = []

        for text in raw_captured:
            if not adapter.is_translatable_value("captured", text):
                continue
            stats.translatable_keys += 1
            records = self._prepare_line_records(text, seen_lines, unique_lines)
            prepared.append((text, records))

        if not prepared:
            report.stats = stats
            report.errors.append(
                f"ملف '{CAPTURED_TEXTS_FILENAME}' لا يحتوي على نصوص إنجليزية قابلة للترجمة."
            )
            return report

        stats.unique_lines = len(unique_lines)
        cache_path = os.path.join(
            BackupManager.get_state_dir(abs_dir), "translation_cache.json"
        )
        translated_map, batches_sent, cached_count = self.translator.translate_lines(
            unique_lines,
            source_lang="en",
            target_lang="ar",
            cache_path=cache_path,
            progress_cb=progress_cb,
        )
        stats.batches_sent = batches_sent
        stats.cached_lines = cached_count
        stats.translated_lines = len(translated_map)
        report.stats = stats

        should_shape = self.shape_arabic and adapter.requires_arabic_shaping(engine)
        new_by_source: Dict[str, str] = {}
        for idx, (orig_val, records) in enumerate(prepared):
            new_by_source[orig_val] = self._assemble_line_records(
                orig_val,
                records,
                translated_map,
                stats,
                should_shape,
                report.warnings,
                key_label=f"captured_{idx}",
            )

        override_path = merge_translations_into_override(
            abs_dir,
            new_by_source=new_by_source,
            source_label=CAPTURED_TEXTS_FILENAME,
        )
        report.warnings.append(
            f"تم دمج {len(new_by_source)} نصاً مترجماً من {CAPTURED_TEXTS_FILENAME} في {os.path.basename(override_path)}."
        )

        if not skip_inject:
            injection = adapter.inject(engine, None, skip_runtime_font=False)
            report.injection = injection
            if not injection.success:
                report.warnings.append(injection.message)
        report.success = True
        return report

    def update_font(self, game_dir: str, font_path: Optional[str] = None) -> PipelineReport:
        """إضافة أو تحديث الخط العربي الافتراضي على لعبة معرّبة مسبقاً دون تعديل نصوصها."""
        adapter, engine = self._resolve_adapter(game_dir)
        report = PipelineReport(engine=engine, success=False)
        if not engine.supported:
            report.errors.append(engine.unsupported_reason or "محرك اللعبة غير مدعوم.")
            return report
        injection = adapter.update_font(engine, custom_font_path=font_path)
        report.injection = injection
        report.success = injection.success
        if not injection.success:
            report.errors.append(injection.message)
        return report

    @staticmethod
    def rollback(game_dir: str) -> Tuple[List[str], List[str]]:
        return BackupManager.rollback(game_dir)

    @staticmethod
    def recover(game_dir: str) -> Tuple[bool, List[str], List[str]]:
        return TransactionJournal.recover_if_interrupted_commit(game_dir)
