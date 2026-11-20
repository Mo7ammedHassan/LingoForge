# -*- coding: utf-8 -*-
"""الواجهة الرسومية (GUI) لأداة التعريب التلقائية باستخدام Tkinter."""

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Callable, Dict, List, Optional, Tuple

from core.backup import BackupManager, TransactionJournal
from core.models import PipelineReport
from core.pipeline import AutoTranslatorPipeline


class TranslatorGUI:
    """النافذة الرئيسية لاختيار مجلد اللعبة، الفحص، التعريب التلقائي، والاسترجاع."""

    def __init__(
        self,
        root: Optional[tk.Tk] = None,
        initial_game_dir: str = "",
        pipeline: Optional[AutoTranslatorPipeline] = None,
    ) -> None:
        self._owns_root = root is None
        self.root = root if root is not None else tk.Tk()
        self.root.title("أداة التعريب التلقائية للألعاب والبرامج — Auto Translator")
        self.root.geometry("840x640")
        self.root.minsize(720, 540)

        self._custom_pipeline = pipeline
        self._ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self._is_busy = False
        self._cancel_event = threading.Event()
        self.last_report: Optional[PipelineReport] = None

        self.game_dir_var = tk.StringVar(value=initial_game_dir)
        self.engine_label_var = tk.StringVar(value="—")
        self.arch_label_var = tk.StringVar(value="—")
        self.file_label_var = tk.StringVar(value="—")
        self.score_label_var = tk.StringVar(value="—")
        self.backup_label_var = tk.StringVar(value="—")
        self.shape_var = tk.BooleanVar(value=True)
        self.inject_var = tk.BooleanVar(value=True)
        self.rpgmaker_fix_var = tk.BooleanVar(value=True)
        self.deep_scan_var = tk.BooleanVar(value=True)
        self.progress_text_var = tk.StringVar(value="جاهز — اختر مجلد اللعبة للبدء.")

        self._build_widgets()
        if self._owns_root:
            self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self.root.after(80, self._poll_queue)

        if initial_game_dir and os.path.isdir(initial_game_dir):
            self.root.after(50, self.run_scan)

    def _build_widgets(self) -> None:
        main_frame = ttk.Frame(self.root, padding=12)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. قسم اختيار مجلد اللعبة
        path_frame = ttk.LabelFrame(main_frame, text="1. مجلد اللعبة أو البرنامج", padding=8)
        path_frame.pack(fill=tk.X, pady=(0, 8))

        self.dir_entry = ttk.Entry(path_frame, textvariable=self.game_dir_var)
        self.dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        self.browse_btn = ttk.Button(
            path_frame, text="استعراض...", command=self.browse_game_dir
        )
        self.browse_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.scan_btn = ttk.Button(
            path_frame, text="فحص اللعبة", command=self.start_scan_async
        )
        self.scan_btn.pack(side=tk.LEFT)

        # 2. قسم معلومات المحرك والملف المرشح
        info_frame = ttk.LabelFrame(
            main_frame, text="2. نتائج الفحص التلقائي (بدون موديل)", padding=8
        )
        info_frame.pack(fill=tk.X, pady=(0, 8))
        info_frame.columnconfigure(1, weight=1)

        info_rows = (
            ("المحرك:", self.engine_label_var),
            ("المعمارية / الواجهة:", self.arch_label_var),
            ("مصدر النصوص الفائز:", self.file_label_var),
            ("درجة الثقة / القابل للترجمة:", self.score_label_var),
            ("حالة النسخ الاحتياطي:", self.backup_label_var),
        )
        for idx, (label_txt, var) in enumerate(info_rows):
            ttk.Label(info_frame, text=label_txt, anchor="e").grid(
                row=idx, column=0, sticky="w", padx=(0, 10), pady=2
            )
            ttk.Label(info_frame, textvariable=var, anchor="w").grid(
                row=idx, column=1, sticky="ew", pady=2
            )

        # 3. قسم خيارات المعالجة والحقن
        opts_frame = ttk.LabelFrame(main_frame, text="3. خيارات التعريب", padding=8)
        opts_frame.pack(fill=tk.X, pady=(0, 8))

        self.shape_chk = ttk.Checkbutton(
            opts_frame,
            text="تشبيك الحروف العربية وعكس الاتجاه (Visual BiDi — مناسب لمحركات LTR)",
            variable=self.shape_var,
        )
        self.shape_chk.pack(anchor="w", pady=2)

        self.inject_chk = ttk.Checkbutton(
            opts_frame,
            text="بناء وحقن دعم الخط العربي وقت التشغيل (عند توفره للمحرك)",
            variable=self.inject_var,
        )
        self.inject_chk.pack(anchor="w", pady=2)

        self.rpgmaker_fix_chk = ttk.Checkbutton(
            opts_frame,
            text="RPG Maker: رسم الحوار العربي متصلاً بدل حرفاً حرفاً (إضافة JS قابلة للاسترجاع)",
            variable=self.rpgmaker_fix_var,
        )
        self.rpgmaker_fix_chk.pack(anchor="w", pady=2)

        self.deep_scan_chk = ttk.Checkbutton(
            opts_frame,
            text="فحص عميق ذكي لمصادر Unity (الأصول والمشاهد وAddressables — سريع بالفهارس ويمكن تعطيله)",
            variable=self.deep_scan_var,
        )
        self.deep_scan_chk.pack(anchor="w", pady=2)

        # 4. أزرار التنفيذ الرئيسية
        actions_frame = ttk.Frame(main_frame)
        actions_frame.pack(fill=tk.X, pady=(0, 8))

        self.translate_btn = ttk.Button(
            actions_frame,
            text="تعريب تلقائي كامل",
            command=self.start_translate_async,
        )
        self.translate_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.pause_btn = ttk.Button(
            actions_frame,
            text="حفظ وإيقاف مؤقت",
            command=self.on_pause_click,
            state=tk.DISABLED,
        )
        self.pause_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.rollback_btn = ttk.Button(
            actions_frame,
            text="استرجاع النسخة الأصلية (Rollback)",
            command=self.on_rollback_click,
        )
        self.rollback_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.update_font_btn = ttk.Button(
            actions_frame,
            text="تحديث الخط العربي",
            command=self.on_update_font_click,
        )
        self.update_font_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.captured_btn = ttk.Button(
            actions_frame,
            text="ترجمة النصوص الملتقطة",
            command=self.on_translate_captured_click,
        )
        self.captured_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.clear_log_btn = ttk.Button(
            actions_frame,
            text="مسح السجل",
            command=self.clear_log,
        )
        self.clear_log_btn.pack(side=tk.RIGHT)

        # 5. شريط التقدم
        prog_frame = ttk.Frame(main_frame)
        prog_frame.pack(fill=tk.X, pady=(0, 8))

        self.progress_bar = ttk.Progressbar(
            prog_frame, mode="determinate", maximum=100, value=0
        )
        self.progress_bar.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(prog_frame, textvariable=self.progress_text_var).pack(anchor="w")

        # 6. السجل المباشر
        log_frame = ttk.LabelFrame(main_frame, text="4. السجل المباشر", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = ScrolledText(
            log_frame,
            height=13,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 9),
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _get_pipeline(self) -> AutoTranslatorPipeline:
        shape_flag = bool(self.shape_var.get())
        engine_options = {
            "rpgmaker_text_fix": bool(self.rpgmaker_fix_var.get()),
            "deep_scan": bool(self.deep_scan_var.get()),
        }
        if self._custom_pipeline is not None:
            self._custom_pipeline.shape_arabic = shape_flag
            if isinstance(getattr(self._custom_pipeline, "engine_options", None), dict):
                self._custom_pipeline.engine_options.update(engine_options)
            return self._custom_pipeline
        return AutoTranslatorPipeline(shape_arabic=shape_flag, engine_options=engine_options)

    def _set_busy(self, busy: bool, translating: bool = False) -> None:
        self._is_busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for btn in (
            self.browse_btn,
            self.scan_btn,
            self.translate_btn,
            self.rollback_btn,
            self.update_font_btn,
            self.captured_btn,
        ):
            btn.configure(state=state)
        self.pause_btn.configure(state=tk.NORMAL if (busy and translating) else tk.DISABLED)

    def on_pause_click(self) -> None:
        """طلب إيقاف آمن مع حفظ التقدم المحقق دون المساس بملفات اللعبة."""
        if not self._is_busy:
            return
        self._cancel_event.set()
        self.pause_btn.configure(state=tk.DISABLED)
        self.progress_text_var.set("جاري حفظ التقدم والإيقاف بأمان دون المساس باللعبة...")
        self.append_log("طلب إيقاف مؤقت آمن — جاري حفظ آخر دفعة مكتملة...")

    def _on_window_close(self) -> None:
        if self._is_busy:
            self._cancel_event.set()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def append_log(self, msg: str) -> None:
        """إضافة سطر إلى نافذة السجل المباشر."""
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg.rstrip() + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def clear_log(self) -> None:
        """مسح محتوى السجل المباشر."""
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def browse_game_dir(self) -> Optional[str]:
        """فتح نافذة اختيار مجلد اللعبة وتشغيل الفحص التلقائي فور الاختيار."""
        initial = self.game_dir_var.get().strip()
        chosen = filedialog.askdirectory(
            title="اختر مجلد اللعبة الرئيسي",
            initialdir=initial if initial and os.path.isdir(initial) else None,
        )
        if chosen:
            self.game_dir_var.set(chosen)
            self.start_scan_async(chosen, prompt_backup=True)
            return chosen
        return None

    def _resolve_target_dir(self, game_dir: Optional[str]) -> Optional[str]:
        target = (game_dir if game_dir is not None else self.game_dir_var.get()).strip()
        if not target or not os.path.isdir(target):
            self.append_log("[خطأ] يرجى تحديد مسار مجلد لعبة صالح أولاً.")
            self.progress_text_var.set("مسار غير صالح — يرجى اختيار مجلد لعبة موجود.")
            return None
        if game_dir is not None and self.game_dir_var.get().strip() != target:
            self.game_dir_var.set(target)
        return target

    def _refresh_backup_status(self, game_dir: str) -> Dict[str, object]:
        summary = BackupManager.get_backup_summary(game_dir)
        interrupted = TransactionJournal.get_interrupted_session_summary(game_dir)
        if interrupted.get("needs_recovery"):
            self.backup_label_var.set(
                "تنبيه: جلسة متوقفة أثناء الكتابة (تُستعاد اللعبة تلقائياً مع حفظ التقدم)"
            )
        elif interrupted.get("has_interrupted"):
            done = interrupted.get("completed_lines", 0)
            total = interrupted.get("total_unique_lines", 0)
            pct = interrupted.get("percent", 0)
            self.backup_label_var.set(
                f"تقدم محفوظ ({done}/{total} — {pct}%) — ملفات اللعبة الأصلية لم تُمس"
            )
        elif summary.get("has_backup"):
            self.backup_label_var.set(
                f"متوفر ومحفوظ — ملفات أصلية مؤمّنة: {summary['replaced_count']} | "
                f"ملفات مضافة: {summary['created_count']} (يمكنك الاسترجاع أو إعادة التجربة)"
            )
        else:
            self.backup_label_var.set("لا يوجد نسخ احتياطي نشط (اللعبة بحالتها الأصلية)")
        return summary

    def prompt_interrupted_session_if_present(self, game_dir: str) -> bool:
        """التحقق من وجود ترجمة متوقفة/منقطعة واستعادة أو استكمال التقدم المحفوظ بأمان."""
        summary = TransactionJournal.get_interrupted_session_summary(game_dir)
        if not summary.get("has_interrupted"):
            return False

        if summary.get("needs_recovery"):
            recovered, restored, warnings = TransactionJournal.recover_if_interrupted_commit(game_dir)
            self._refresh_backup_status(game_dir)
            if recovered:
                self.append_log(
                    f"[أمان] تمت استعادة ({len(restored)}) ملفاً لحالتها الأصلية بعد انقطاع الكتابة مع الاحتفاظ بكاش الترجمة."
                )
            for w in warnings:
                self.append_log(f"[تحذير] {w}")
            messagebox.showinfo(
                "استعادة تلقائية بعد انقطاع",
                (
                    "تم اكتشاف انقطاع مفاجئ أثناء مرحلة الكتابة الأخيرة.\n"
                    f"تمت استعادة ملفات اللعبة الأصلية بأمان ({len(restored)} ملفاً) مع الاحتفاظ بكل التقدم المحفوظ."
                ),
            )
            return True

        done = summary.get("completed_lines", 0)
        total = summary.get("total_unique_lines", 0)
        pct = summary.get("percent", 0)
        resume_now = messagebox.askyesno(
            "تقدم ترجمة محفوظ (لم تُعدّل اللعبة بعد)",
            (
                "تم العثور على جلسة ترجمة متوقفة سابقاً لهذه اللعبة:\n"
                f"• التقدم المحفوظ: {done} / {total} سطراً ({pct}%)\n"
                "• حالة ملفات اللعبة: سليمة 100% ولم تُمس بعد.\n\n"
                "هل تريد استكمال الترجمة من حيث توقفت الآن؟"
            ),
        )
        if resume_now:
            self.start_translate_async()
        else:
            self.append_log(
                f"تم الإبقاء على التقدم المحفوظ ({done}/{total}) دون لمس اللعبة؛ اضغط «تعريب تلقائي كامل» وقتما تشاء للاستكمال."
            )
        return True

    def prompt_backup_decision_if_present(self, game_dir: str) -> bool:
        """عرض خيار استرجاع النسخة الأصلية أو إبقاء النسخة الاحتياطية عند وجود تعريب سابق."""
        summary = BackupManager.get_backup_summary(game_dir)
        if not summary.get("has_backup"):
            return False

        restore_now = messagebox.askyesno(
            "تم اكتشاف نسخة احتياطية سابقة",
            (
                "تم العثور على نسخة احتياطية محفوظة لهذه اللعبة على القرص:\n"
                f"• ملفات أصلية مؤمّنة: {summary['replaced_count']}\n"
                f"• ملفات تعريب/حقن مضافة: {summary['created_count']}\n\n"
                "هل تريد استرجاع اللعبة لحالتها الأصلية الآن (Rollback)؟\n"
                "• اختر «نعم (Yes)» لاستعادة الأصل وحذف التعريب فوراً.\n"
                "• اختر «لا (No)» للإبقاء على النسخة الاحتياطية الأصلية وإعادة تجربة التعريب."
            ),
        )
        if restore_now:
            self.run_rollback(game_dir)
            self.run_scan(game_dir)
            return True

        self.append_log("تم الإبقاء على النسخة الاحتياطية الأصلية؛ يمكنك إعادة التعريب أو الاسترجاع في أي وقت.")
        return False

    def _update_info_from_report(self, report: PipelineReport) -> None:
        eng = report.engine
        sup_txt = "مدعوم" if eng.supported else "غير مدعوم"
        self.engine_label_var.set(f"{eng.engine_name} ({sup_txt})")
        if eng.engine_name.lower() == "unity" or eng.has_tmp or eng.has_ugui:
            self.arch_label_var.set(
                f"{eng.architecture} | {eng.scripting_backend} | "
                f"TMP: {'نعم' if eng.has_tmp else 'لا'} | UGUI: {'نعم' if eng.has_ugui else 'لا'}"
            )
        else:
            self.arch_label_var.set(f"{eng.architecture} | {eng.scripting_backend}")
        if report.selected_file is not None:
            sel = report.selected_file
            extra_count = len(report.selected_files)
            if extra_count > 1:
                self.file_label_var.set(f"{extra_count} ملفات (أولها: {sel.relative_path}) [{sel.format_name}]")
            else:
                self.file_label_var.set(f"{sel.relative_path} [{sel.format_name}]")
            self.score_label_var.set(
                f"{sel.score}/100 | نصوص قابلة للترجمة: {sel.translatable_entries}/{sel.total_entries}"
            )
        else:
            self.file_label_var.set("لم يُحدد ملف لغة موثوق")
            self.score_label_var.set("—")
        self._refresh_backup_status(eng.game_dir)

    @staticmethod
    def _format_report_log_lines(report: PipelineReport, phase: str) -> List[str]:
        lines: List[str] = []
        eng = report.engine
        lines.append(
            f"=== [{phase}] المحرك: {eng.engine_name} | {eng.architecture} | {eng.scripting_backend} ==="
        )
        u_ver = getattr(eng, "unity_version", "")
        loc_sys = getattr(eng, "localization_systems", [])
        if u_ver or loc_sys:
            parts = []
            if u_ver:
                parts.append(f"إصدار Unity: {u_ver}")
            if loc_sys:
                parts.append(f"أنظمة التعريب: {', '.join(loc_sys)}")
            lines.append(" | ".join(parts))
        if report.selected_file is not None:
            sel = report.selected_file
            lines.append(
                f"المصدر المختار: {sel.relative_path} ({sel.format_name}) — الدرجة {sel.score}/100"
            )
            for reason in sel.reasons:
                lines.append(f"  • {reason}")
            if len(report.selected_files) > 1:
                lines.append(f"المصادر الإضافية المدمجة ({len(report.selected_files) - 1}):")
                for extra in report.selected_files[1:]:
                    lines.append(
                        f"  + {extra.relative_path} ({extra.format_name}) — {extra.score}/100"
                    )
        if report.stats is not None:
            st = report.stats
            lines.append(
                f"الإحصائيات: إجمالي {st.total_keys} | قابل للترجمة {st.translatable_keys} | "
                f"أسطر فريدة {st.unique_lines} | من الكاش {st.cached_lines} | "
                f"دفعات {st.batches_sent} | متغيرات مُصلحة {st.repaired_placeholders}"
            )
        if report.injection is not None:
            inj = report.injection
            lines.append(
                f"الحقن ({inj.method}): {'نجاح' if inj.success else 'فشل'} — {inj.message}"
            )
        for w in report.warnings:
            lines.append(f"[تحذير] {w}")
        for e in report.errors:
            lines.append(f"[خطأ] {e}")
        return lines

    def _apply_scan_completion(self, report: PipelineReport, target: str) -> None:
        self.last_report = report
        self._update_info_from_report(report)

        for ln in self._format_report_log_lines(report, "فحص"):
            self.append_log(ln)

        if BackupManager.get_backup_summary(target).get("has_backup"):
            self.append_log(
                "[نسخ احتياطي] توجد نسخة احتياطية محفوظة في المجلد — يمكنك استرجاع الأصل (Rollback) أو إعادة الترجمة بأمان."
            )

        if report.success and report.selected_file is not None:
            self.progress_text_var.set(
                f"اكتمل الفحص — جاهز للتعريب ({report.selected_file.relative_path})."
            )
        else:
            self.progress_text_var.set("اكتمل الفحص مع وجود ملاحظات أو أخطاء.")

    def start_scan_async(
        self, game_dir: Optional[str] = None, prompt_backup: bool = False
    ) -> None:
        """تشغيل فحص اللعبة في خيط خلفي لمنع تجمّد الواجهة الرسومية."""
        if self._is_busy:
            return
        target = self._resolve_target_dir(game_dir)
        if not target:
            return

        self._set_busy(True)
        self.progress_bar.configure(value=0)
        self.progress_text_var.set("جاري فحص اللعبة واكتشاف مصادر النصوص...")
        pipeline = self._get_pipeline()

        def _worker() -> None:
            try:
                report = pipeline.scan(target)
                self._ui_queue.put(("scan_done", report, target, prompt_backup))
            except Exception as exc:
                self._ui_queue.put(("error", str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

    def run_scan(self, game_dir: Optional[str] = None) -> Optional[PipelineReport]:
        """فحص اللعبة وتحديث معلومات الواجهة دون تعديل الملفات."""
        target = self._resolve_target_dir(game_dir)
        if not target:
            return None

        self.progress_bar.configure(value=0)
        self.progress_text_var.set("جاري فحص اللعبة واكتشاف مصادر النصوص...")
        pipeline = self._get_pipeline()
        report = pipeline.scan(target)
        self._apply_scan_completion(report, target)
        return report

    def _apply_translate_completion(self, report: PipelineReport) -> None:
        self.last_report = report
        self._update_info_from_report(report)
        for ln in self._format_report_log_lines(report, "تعريب"):
            self.append_log(ln)
        if report.success:
            self.progress_bar.configure(value=100)
            self.progress_text_var.set("اكتمل التعريب والحقن بنجاح.")
        elif getattr(report, "interrupted", False):
            self.progress_text_var.set("تم حفظ التقدم وإيقاف العملية بأمان — ملفات اللعبة لم تُمس.")
        else:
            self.progress_text_var.set("انتهى التعريب بوجود أخطاء — راجع السجل.")

    def run_translate_sync(
        self,
        game_dir: Optional[str] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> Optional[PipelineReport]:
        """تنفيذ التعريب بشكل متزامن (مناسب للاختبارات الآلية)."""
        target = self._resolve_target_dir(game_dir)
        if not target:
            return None

        self.progress_bar.configure(value=0)
        self.progress_text_var.set("جاري بدء عملية التعريب...")

        def _sync_progress(cur: int, total: int) -> None:
            pct = int((cur / max(1, total)) * 100)
            self.progress_bar.configure(value=pct)
            self.progress_text_var.set(f"جاري الترجمة: الدفعة {cur} / {total}...")
            self.append_log(f"جاري الترجمة: الدفعة {cur} / {total}...")
            if progress_cb is not None:
                progress_cb(cur, total)

        pipeline = self._get_pipeline()
        report = pipeline.translate(
            target,
            dry_run=False,
            skip_inject=not bool(self.inject_var.get()),
            progress_cb=_sync_progress,
        )
        self._apply_translate_completion(report)
        return report

    def start_translate_async(self) -> None:
        """تشغيل التعريب في خيط خلفي مع تحديث شريط التقدم والسجل لحظياً."""
        if self._is_busy:
            return
        target = self._resolve_target_dir(None)
        if not target:
            return

        shape_flag = bool(self.shape_var.get())
        inject_flag = bool(self.inject_var.get())
        pipeline = self._get_pipeline()

        self._cancel_event.clear()
        self._set_busy(True, translating=True)
        self.progress_bar.configure(value=0)
        self.progress_text_var.set("جاري تحليل وترجمة النصوص في الخلفية...")
        self.append_log(f"بدء التعريب التلقائي للمجلد: {target}")

        def _worker() -> None:
            try:
                pipeline.shape_arabic = shape_flag

                def _bg_progress(cur: int, total: int) -> None:
                    self._ui_queue.put(("progress", cur, total))

                report = pipeline.translate(
                    target,
                    dry_run=False,
                    skip_inject=not inject_flag,
                    progress_cb=_bg_progress,
                    cancel_event=self._cancel_event,
                )
                self._ui_queue.put(("translate_done", report))
            except Exception as exc:
                self._ui_queue.put(("error", f"حدث خطأ غير متوقع أثناء التعريب: {exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    def run_rollback(
        self, game_dir: Optional[str] = None
    ) -> Tuple[List[str], List[str]]:
        """استرجاع النسخة الأصلية وحذف الملفات المضافة."""
        target = self._resolve_target_dir(game_dir)
        if not target:
            return [], ["مسار مجلد اللعبة غير صالح."]

        restored, warnings = AutoTranslatorPipeline.rollback(target)
        self.append_log(f"=== [استرجاع Rollback] الملفات المسترجعة/المحذوفة ({len(restored)}) ===")
        for item in restored:
            self.append_log(f"  ✓ {item}")
        for w in warnings:
            self.append_log(f"[تحذير] {w}")

        self._refresh_backup_status(target)
        self.progress_bar.configure(value=0)
        self.progress_text_var.set(
            f"اكتمل الاسترجاع ({len(restored)} عنصر)."
            if restored
            else "اكتمل فحص الاسترجاع."
        )
        return restored, warnings

    def on_rollback_click(self) -> None:
        if self._is_busy:
            return
        target = self._resolve_target_dir(None)
        if not target:
            return
        confirm = messagebox.askyesno(
            "تأكيد الاسترجاع",
            f"هل تريد استعادة النسخ الأصلية وحذف ملفات التعريب والحقن من:\n{target}؟",
        )
        if confirm:
            self.run_rollback(target)

    def on_update_font_click(self) -> None:
        if self._is_busy:
            return
        target = self._resolve_target_dir(None)
        if not target:
            return
        self._set_busy(True)
        self.progress_text_var.set("جاري نشر وتحديث الخط العربي...")
        pipeline = self._get_pipeline()

        def _worker() -> None:
            try:
                report = pipeline.update_font(target)
                self._ui_queue.put(("update_font_done", report))
            except Exception as exc:
                self._ui_queue.put(("error", str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

    def on_translate_captured_click(self) -> None:
        if self._is_busy:
            return
        target = self._resolve_target_dir(None)
        if not target:
            return
        self._set_busy(True)
        self.progress_bar.configure(value=0)
        self.progress_text_var.set("جاري ترجمة النصوص الملتقطة وقت التشغيل...")
        pipeline = self._get_pipeline()
        skip_inject = not bool(self.inject_var.get())

        def _worker() -> None:
            def _bg_progress(cur: int, total: int) -> None:
                self._ui_queue.put(("progress", cur, total))

            try:
                report = pipeline.translate_captured_texts(
                    target, skip_inject=skip_inject, progress_cb=_bg_progress
                )
                self._ui_queue.put(("captured_done", report))
            except Exception as exc:
                self._ui_queue.put(("error", str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self._ui_queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    cur, total = item[1], item[2]
                    pct = int((cur / max(1, total)) * 100)
                    self.progress_bar.configure(value=pct)
                    self.progress_text_var.set(f"جاري الترجمة: الدفعة {cur} / {total}...")
                    self.append_log(f"جاري الترجمة: الدفعة {cur} / {total}...")
                elif kind == "log":
                    self.append_log(item[1])
                elif kind == "scan_done":
                    self._set_busy(False)
                    report, target, prompt_backup = item[1], item[2], item[3]
                    self._apply_scan_completion(report, target)
                    if prompt_backup:
                        if not self.prompt_interrupted_session_if_present(target):
                            self.prompt_backup_decision_if_present(target)
                elif kind == "update_font_done":
                    self._set_busy(False)
                    report = item[1]
                    if report.injection:
                        self.append_log(f"=== [تحديث الخط العربي] {report.injection.message} ===")
                    for err in report.errors:
                        self.append_log(f"[خطأ] {err}")
                    self.progress_text_var.set(
                        "تم تحديث الخط العربي بنجاح." if report.success else "فشل تحديث الخط العربي."
                    )
                elif kind == "translate_done":
                    self._set_busy(False)
                    self._apply_translate_completion(item[1])
                elif kind == "captured_done":
                    self._set_busy(False)
                    report = item[1]
                    for line in self._format_report_log_lines(report, "ترجمة الملتقطات"):
                        self.append_log(line)
                    self.progress_bar.configure(value=100 if report.success else 0)
                    self.progress_text_var.set(
                        "تمت ترجمة النصوص الملتقطة ودمجها في ArabicOverride.json بنجاح."
                        if report.success
                        else "تعذر ترجمة النصوص الملتقطة — راجع السجل."
                    )
                elif kind == "error":
                    self._set_busy(False)
                    self.progress_text_var.set("فشل التنفيذ — راجع السجل.")
                    self.append_log(f"[خطأ] {item[1]}")
        except queue.Empty:
            pass

        try:
            if self.root.winfo_exists():
                self.root.after(80, self._poll_queue)
        except tk.TclError:
            pass


def launch_gui(initial_game_dir: str = "") -> int:
    """تشغيل الواجهة الرسومية الرئيسية."""
    app = TranslatorGUI(initial_game_dir=initial_game_dir)
    app.root.mainloop()
    return 0
