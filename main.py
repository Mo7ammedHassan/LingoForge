# -*- coding: utf-8 -*-
"""نقطة الدخول الرسمية لأداة التعريب التلقائية للألعاب والبرامج."""

import argparse
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ضمان إتاحة الاستيراد النسبي عند التشغيل المباشر للملف
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.backup import TransactionJournal
from core.pipeline import AutoTranslatorPipeline


def _print_scan_report(report) -> None:
    eng = report.engine
    print("=== نتيجة فحص المحرك ===")
    print(f"المحرك: {eng.engine_name} | مدعوم: {'نعم' if eng.supported else 'لا'}")
    print(f"المعمارية: {eng.architecture} | نظام البناء: {eng.scripting_backend}")
    if eng.engine_name.lower() == "unity" or eng.has_tmp or eng.has_ugui:
        print(f"TextMeshPro: {'نعم' if eng.has_tmp else 'لا'} | UGUI: {'نعم' if eng.has_ugui else 'لا'}")
    u_ver = getattr(eng, "unity_version", "")
    loc_sys = getattr(eng, "localization_systems", [])
    if u_ver:
        print(f"إصدار Unity: {u_ver}")
    if loc_sys:
        print(f"أنظمة التعريب/الحزم المكتشفة: {', '.join(loc_sys)}")
    if eng.exe_path:
        print(f"الملف التنفيذي: {eng.exe_path}")
    if eng.data_dir:
        print(f"مجلد البيانات: {eng.data_dir}")

    if report.dll_hints:
        print(f"\n=== أدلة اللغة المستخرجة ({len(report.dll_hints)}) ===")
        for h in report.dll_hints[:12]:
            print(f"  - {h}")

    if report.candidates:
        print(f"\n=== أعلى ملفات اللغة المرشحة ({len(report.candidates)}) ===")
        for idx, c in enumerate(report.candidates[:5], start=1):
            print(
                f"  [{idx}] الدرجة: {c.score}/100 | الصيغة: {c.format_name} | "
                f"قابل للترجمة: {c.translatable_entries}/{c.total_entries} | {c.relative_path}"
            )
            for reason in c.reasons:
                print(f"       • {reason}")

    if report.selected_file:
        print(f"\n>> الملف الفائز المختار تلقائياً: {report.selected_file.relative_path} (الدرجة: {report.selected_file.score}/100)")
        if len(report.selected_files) > 1:
            print(f">> المصادر الإضافية المختارة للدمج ({len(report.selected_files) - 1}):")
            for extra in report.selected_files[1:]:
                print(f"   + {extra.relative_path} [{extra.format_name}] (الدرجة: {extra.score}/100)")

    interrupted = TransactionJournal.get_interrupted_session_summary(eng.game_dir)
    if interrupted.get("has_interrupted"):
        done = interrupted.get("completed_lines", 0)
        total = interrupted.get("total_unique_lines", 0)
        pct = interrupted.get("percent", 0)
        print(
            f"\n[أمان الجلسة] تقدم ترجمة محفوظ: {done}/{total} ({pct}%) | الحالة: {interrupted.get('status')}"
        )

    for w in report.warnings:
        print(f"[تحذير] {w}")
    for e in report.errors:
        print(f"[خطأ] {e}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="أداة التعريب التلقائية للألعاب (اكتشاف، نسخ احتياطي، ترجمة مجمعة، تشبيك، وحقن)."
    )
    sub = parser.add_subparsers(dest="command", required=False)

    p_gui = sub.add_parser("gui", help="تشغيل الواجهة الرسومية (الوضع الافتراضي)")
    p_gui.add_argument("game_dir", nargs="?", default="", help="مسار مجلد اللعبة الاختياري")

    p_scan = sub.add_parser("scan", help="فحص اللعبة واكتشاف ملف اللغة بدون تعديل")
    p_scan.add_argument("game_dir", help="مسار مجلد اللعبة")
    p_scan.add_argument(
        "--fast-scan",
        "--no-deep-scan",
        action="store_true",
        dest="fast_scan",
        help="Unity: تعطيل الفحص العميق وإيقاف البحث عند وجود ملف لغة خارجي قوي",
    )

    p_tr = sub.add_parser("translate", help="تشغيل التعريب والحقن التلقائي الكامل")
    p_tr.add_argument("game_dir", help="مسار مجلد اللعبة")
    p_tr.add_argument("--dry-run", action="store_true", help="معاينة الإحصائيات بدون ترجمة أو تعديل")
    p_tr.add_argument("--skip-inject", action="store_true", help="ترجمة النصوص فقط بدون بناء/حقن الخط")
    p_tr.add_argument("--no-shape", action="store_true", help="عدم تشبيك الحروف وعكس الاتجاه")
    p_tr.add_argument(
        "--fast-scan",
        "--no-deep-scan",
        action="store_true",
        dest="fast_scan",
        help="Unity: تعطيل الفحص العميق وإيقاف البحث عند وجود ملف لغة خارجي قوي",
    )
    for sp in (p_tr,):
        sp.add_argument(
            "--no-rpgmaker-text-fix",
            action="store_true",
            help="RPG Maker: عدم نشر إضافة رسم الحوار العربي متصلاً (AutoTranslator_ArabicText.js)",
        )
        sp.add_argument(
            "--rpgmaker-text-mode",
            choices=("word", "line"),
            default="word",
            help="RPG Maker: رسم الحوار كلمةً كلمة (word، افتراضي) أو المقطع كاملاً (line)",
        )

    p_rb = sub.add_parser("rollback", help="استرجاع النسخة الأصلية للملفات المعدّلة")
    p_rb.add_argument("game_dir", help="مسار مجلد اللعبة")

    p_rec = sub.add_parser(
        "recover",
        help="فحص واستعادة أي عملية كتابة متوقفة عند انقطاع الكهرباء مع الحفاظ على التقدم",
    )
    p_rec.add_argument("game_dir", help="مسار مجلد اللعبة")

    p_uf = sub.add_parser("update-font", help="إضافة أو تحديث الخط العربي الافتراضي على لعبة معرّبة مسبقاً")
    p_uf.add_argument("game_dir", help="مسار مجلد اللعبة")
    p_uf.add_argument("--font", default=None, help="مسار خط TTF/OTF مخصص اختياري")
    p_uf.add_argument(
        "--no-rpgmaker-text-fix",
        action="store_true",
        help="RPG Maker: عدم نشر إضافة رسم الحوار العربي متصلاً",
    )
    p_uf.add_argument("--rpgmaker-text-mode", choices=("word", "line"), default="word")

    p_cap = sub.add_parser(
        "translate-captured",
        help="ترجمة arabic_captured_texts.txt ودمجه في ArabicOverride.json",
    )
    p_cap.add_argument("game_dir", help="مسار مجلد اللعبة")
    p_cap.add_argument("--skip-inject", action="store_true", help="تحديث ArabicOverride.json فقط دون إعادة الحقن")
    p_cap.add_argument("--no-shape", action="store_true", help="عدم تشبيك الحروف وعكس الاتجاه")

    args = parser.parse_args(argv)
    if args.command in (None, "gui"):
        from ui.gui import launch_gui

        return launch_gui(getattr(args, "game_dir", "") or "")

    engine_options = {
        "rpgmaker_text_fix": not getattr(args, "no_rpgmaker_text_fix", False),
        "rpgmaker_text_fix_mode": getattr(args, "rpgmaker_text_mode", "word"),
        "deep_scan": not getattr(args, "fast_scan", False),
    }
    pipeline = AutoTranslatorPipeline(
        shape_arabic=not getattr(args, "no_shape", False),
        engine_options=engine_options,
    )

    if args.command == "scan":
        report = pipeline.scan(args.game_dir)
        _print_scan_report(report)
        return 0 if report.success else 1

    if args.command == "translate":
        def _progress(cur: int, total: int) -> None:
            print(f"جاري الترجمة: الدفعة {cur} / {total}...")

        try:
            report = pipeline.translate(
                args.game_dir,
                dry_run=args.dry_run,
                skip_inject=args.skip_inject,
                progress_cb=_progress,
            )
        except KeyboardInterrupt:
            print("\n[إيقاف آمن] تم حفظ التقدم المكتمل على القرص ولم تُعدّل ملفات اللعبة.")
            return 130
        _print_scan_report(report)
        if report.stats:
            st = report.stats
            print("\n=== إحصائيات الترجمة ===")
            print(
                f"إجمالي النصوص: {st.total_keys} | القابلة للترجمة: {st.translatable_keys} | "
                f"الأسطر الفريدة: {st.unique_lines} | من الكاش: {st.cached_lines} | "
                f"الدفعات المرسلة: {st.batches_sent} | متغيرات مُصلحة: {st.repaired_placeholders}"
            )
        if report.injection:
            inj = report.injection
            print("\n=== نتيجة حقن الخط ===")
            print(f"الأسلوب: {inj.method} | النجاح: {'نعم' if inj.success else 'لا'} | {inj.message}")
        if getattr(report, "interrupted", False):
            print("\n[إيقاف آمن] تم حفظ التقدم بأمان ولم تُعدّل ملفات اللعبة الأصلية.")
            return 130
        return 0 if report.success else 1

    if args.command == "rollback":
        restored, warnings = pipeline.rollback(args.game_dir)
        print(f"الملفات المسترجعة ({len(restored)}):")
        for rel in restored:
            print(f"  ✓ {rel}")
        for w in warnings:
            print(f"[تحذير] {w}")
        return 0

    if args.command == "recover":
        recovered, restored, warnings = pipeline.recover(args.game_dir)
        summary = TransactionJournal.get_interrupted_session_summary(args.game_dir)
        if recovered:
            print(f"تمت استعادة ({len(restored)}) ملفاً بعد التوقف أثناء الالتزام:")
            for rel in restored:
                print(f"  ✓ {rel}")
        elif summary.get("has_interrupted"):
            print(
                f"تقدم محفوظ بأمان ({summary.get('completed_lines', 0)}/"
                f"{summary.get('total_unique_lines', 0)}) — ملفات اللعبة سليمة ولم تُمس."
            )
        else:
            print("ملفات اللعبة سليمة ولا توجد جلسة التزام عالقة.")
        for w in warnings:
            print(f"[تحذير] {w}")
        return 0

    if args.command == "update-font":
        report = pipeline.update_font(args.game_dir, font_path=args.font)
        if report.injection:
            inj = report.injection
            print(f"الأسلوب: {inj.method} | النجاح: {'نعم' if inj.success else 'لا'} | {inj.message}")
        for e in report.errors:
            print(f"[خطأ] {e}")
        return 0 if report.success else 1

    if args.command == "translate-captured":
        def _cap_progress(cur: int, total: int) -> None:
            print(f"جاري ترجمة الملتقطات: الدفعة {cur} / {total}...")

        report = pipeline.translate_captured_texts(
            args.game_dir,
            skip_inject=args.skip_inject,
            progress_cb=_cap_progress,
        )
        _print_scan_report(report)
        if report.stats:
            st = report.stats
            print(f"\nالنصوص الملتقطة المترجمة: {st.translatable_keys} / {st.total_keys}")
        return 0 if report.success else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
