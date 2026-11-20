# ذاكرة المشروع (Project Memory) — أداة التعريب التلقائية للألعاب والبرامج

> **آخر تحديث:** 2026-11-19  
> **مجلد المشروع:** `C:\Users\asus\OneDrive\Desktop\tr\translator\`  
> **مجلد التجربة المرجعية:** `C:\Users\asus\OneDrive\Desktop\tr\`  
> **الغرض من هذا الملف:** توثيق كل ما تم التوصل إليه تقنياً، والمعمارية المخطط لها، وحالة التقدم الحالية، بحيث يستطيع أي شات أو مطوّر جديد قراءة هذا الملف والبدء فوراً في التنفيذ دون فقدان السياق.

---

## 1. نبذة عن المشروع والهدف الأساسي (Project Vision)

تطوير برنامج تلقائي لتعريب الألعاب والبرامج بمجرد تحديد مجلد اللعبة/البرنامج فقط:
1. يبدأ بدعم **محرك Unity (Mono ولاحقاً IL2CPP)** بالكامل وبشكل عام لأي لعبة Unity، دون تعديل ملفات `.assets` التي تتسبب في الشاشة السوداء عند اختلاف إصدارات اللعبة.
2. يُبنى منذ اليوم الأول **بمعمارية معيارية قابلة للتوسع (Plugin / Adapter Architecture)** بحيث يمكن مستقبلاً إضافة محركات أخرى (Unreal Engine، Godot، RPG Maker، Ren'Py، تطبيقات سطح المكتب) أو صيغ ملفات ومترجمين جدد (Online أو Offline) **دون كسر الهيكل الأساسي**.

---

## 2. توثيق التجربة المرجعية (Case Study: Subnautica: Below Zero)

تم استخلاص خطوات العمل التلقائية من التجربة العملية الكاملة لتعريب لعبة **Subnautica: Below Zero** (`D:\Games\Subnautica - Below Zero`)، والتي تضمنت:

### الخطوة 1: اكتشاف وتحليل ملف اللغة المصدر
- ملف اللغة: `English.json` (558,929 بايت، 3,811 مفتاحاً، يتضمن سطراً بتعليق `//` غير قياسي في JSON).
- **الدرس المستفاد:** بعض ألعاب Unity تستخدم JSON غير صارم (تعليقات `//` أو فواصل زائدة)، لذا يجب تنظيف المحتوى برمجياً قبل التحليل مع الحفاظ على ترتيب المفاتيح.

### الخطوة 2: حماية الرموز والترجمة السحابية المجمعة (`translate_subnautica.py`)
- عزل الأجزاء غير القابلة للترجمة قبل إرسالها للمترجم:
  - المتغيرات والتنسيقات: `{0}`, `{0:P0}`, `%s`, `%d`, `\t`
  - وسوم Unity الغنية: `<color=...>`, `<b>`, `<i>`, إلخ.
  - استبدالها برموز آمنة `[[[0]]]`, `[[[1]]]` وتوحيد الأرقام العربية (`٠-٩`) لو حوّلها المترجم.
- استخدام طلبات **POST مجمعة** (`max_chars=2400`, `max_lines=45`) مع التبديل التلقائي بين نقطتي نهاية (`translate.googleapis.com` و`clients5.google.com`) لتفادي `429 Too Many Requests`.
- عند اختلاف عدد الأسطر العائدة من المترجم بسبب التفاف النص، يتم **تقسيم الدفعة إلى نصفين** وإعادة الإرسال تلقائياً.
- حفظ التقدم دورياً (`translation_progress.json`) لاستئناف الترجمة عند أي انقطاع، والتحقق من خلو النتائج من الرموز التالفة أو الأخطاء في المتغيرات (تمت ترجمة 3,295 نصاً من أصل 3,319 نصاً قابلاً للترجمة بنسبة 99.28% وبـ 0 أخطاء في المتغيرات).

### الخطوة 3: معالجة العربية لمحركات LTR (`shape_arabic.py`)
- محرك Unity (مع TextMeshPro في معظم الألعاب) يعرض النصوص من اليسار لليمين (LTR) دون ربط الحروف العربية تلقائياً.
- المعالجة المنفذة:
  1. إزالة التشكيل وعلامات الاتجاه الخفية (`\u064B-\u065F`, `\u0670`, `\u200B-\u200F`) التي تظهر كمربعات أو تكسر تشبيك الحروف.
  2. دمج وصلات **لام ألف** (`لا`, `لأ`, `لإ`, `لآ` -> `U+FEF5–U+FEFC`) وتحويل بقية الحروف لأشكالها المتصلة في **Presentation Forms-B** (`U+FE70–U+FEFF`).
  3. خوارزمية **Visual BiDi** لكل سطر: عكس مقاطع العربية مع عكس الأقواس وعلامات الاقتباس (`()`, `[]`, `«»`, `“”`)، وإبقاء الكلمات الإنجليزية والأرقام والمتغيرات بترتيبها LTR، وحماية الوسوم الزوجية (`<color=...>...</color>`) حتى لا تنعكس علامتا الفتح والإغلاق.

### الخطوة 4: النسخ الاحتياطي وتجنب تعديل `.assets`
- أدوات التعريب القديمة التي تستبدل `resources.assets` / `sharedassets0.assets` من إصدارات أخرى تسبب **شاشة سوداء** عند تحديث اللعبة.
- البديل الآمن: حفظ نسخة احتياطية لكل ملف يتم تعديله (`.bak`)، وحقن الخط العربي برمجياً وقت التشغيل (Runtime).

### الخطوة 5: حقن الخط العربي وقت التشغيل (`PlatformInputDefault.cs` -> `PlatformInputDefault.dll`)
- **بناء الخط الديناميكي:** إنشاء `TMP_FontAsset` باسم `ArabicFallbackSDF` (أطلس `2048x2048` بصيغة `TextureFormat.Alpha8` ووضع `GlyphRenderMode.SDFAA`) من خط النظام (`C:\Windows\Fonts\arial.ttf` أو `tahoma.ttf`) عبر `FontEngine.LoadFontFace` و`FontEngine.TryAddGlyphToTexture` (نجح في توليد 361 رسماً و405 محارف في السجل `arabic_font_log.txt`).
- **مشكلات حقيقية ظهرت أثناء الاختبار وتم حلها (دروس حاسمة للمشروع العام):**
  1. كود اللعبة `LanguageSDF.InitializeInternal` يستدعي `ClearFontAssetData(true)` على `fallbackFontAssetTable` الخاصة بالخطوط بعد الإقلاع، مما مسح محتوى `ArabicFallbackSDF` بعد ثانية واحدة.
  2. وجود `ArabicFallbackSDF` داخل `fallbackFontAssetTable` لخطوط اللعبة تسبب أيضاً في `NullReferenceException` داخل `LanguageSDF.CloneFontAsset` لأن `m_SourceFontFile` كان `null` (تم رصدها في `Player.log`)، مما أوقف تهيئة باقي الخطوط.
  3. **الحل الجذري:**
     - إلحاق `ArabicFallbackSDF` في بداية القائمة العامة **`TMP_Settings.fallbackFontAssets`** فقط (دون تلويث `fallbackFontAssetTable` الفردية للخطوط).
     - تعيين `Font` احتياطي في `m_SourceFontFile` وحماية `LanguageSDF.clones` عبر Reflection.
     - مراقبة تهيئة اللغة (`LanguageSDF.cachedLanguage`) وحالة الجداول كل 30 إطاراً، وإعادة البناء (`RebuildAfterWipe`) + مسح كاش البحث (`k_SearchedFontAssetLookup`, `FallbackSearchQueryLookup`, `m_MissingUnicodesFromFontFile`) + تحديث النصوص (`ForceMeshUpdate(true, true)`) تلقائياً.

---

## 3. خط سير العمل التلقائي (Automated Pipeline — 7 Stages)

عندما يختار المستخدم مجلد اللعبة في البرنامج، ينفّذ النظام المراحل السبع التالية بتسلسل محكم:

1. **Stage 1 — `GameScanner & EngineDetector` (فحص اللعبة واكتشاف المحرك):**
   - تحديد المحرك (`Unity`, وقابلاً لإضافة `Unreal`, `Godot`, `RPGMaker`, `RenPy` لاحقاً).
   - لـ Unity: تحديد المعمارية (`x64` / `x86`)، نوع البناء (`Mono` بوجود `Managed/` أو `IL2CPP` بوجود `GameAssembly.dll`)، إصدار Unity، ومكتبة النصوص المستخدمة (`Unity.TextMeshPro.dll` / `UGUI`).
2. **Stage 2 — `BackupManager` (إدارة النسخ الاحتياطي والاسترجاع):**
   - إنشاء نسخة احتياطية قبل أي تعديل مع سجل `backup_manifest.json` (يحتوي مسارات الملفات الأصلية و SHA-256 hash) لتوفير زر استعادة كامل (`Rollback`) بضغطة واحدة.
3. **Stage 3 — `LocalizationExtractor` (استخراج ملفات النصوص):**
   - مسح ذكي لمجلدات اللغة المعتادة (`StreamingAssets`, `Localization`, `LanguageFiles`, `Resources`, إلخ) باستخدام المحولات المناسبة للصيغ (`JSON`, `CSV`, `XML`, `INI`, `PO`, ولاحقاً أصول Unity المعبأة).
4. **Stage 4 — `TranslationEngine` (محرك الترجمة الموحّد):**
   - واجهة موحدة تدعم مزودين متعددين:
     - **الافتراضي (Online):** الترجمة السحابية المجمعة عبر POST مع التبديل وتقسيم الدفعات.
     - **التوسع المستقبلي (Offline / LLM):** إمكانية إضافة نماذج محلية (مثل Argos Translate / NLLB / Ollama) أو مفاتيح API دون تغيير بقية المراحل.
   - ذاكرة ترجمة محلية (`Translation Cache` / SQLite أو JSON) لتسريع إعادة التشغيل وعدم ترجمة الجمل المكررة أو المترجمة سابقاً.
5. **Stage 5 — `ArabicTextProcessor` (معالج النصوص العربية):**
   - حماية المتغيرات والوسوم + التشكيل (Presentation Forms-B + لام ألف) + ترتيب Visual BiDi.
   - خيارات قابلة للتهيئة حسب المحرك (مثلاً: محرك يدعم RTL أصلاً لا يحتاج عكس النص، بينما محرك Unity LTR يحتاج التشكيل + العكس).
6. **Stage 6 — `FontAndRuntimeInjector` (الحقن التلقائي للخطوط وقت التشغيل):**
   - **قرار معماري مهم لعمومية Unity:** في Subnautica تم الحقن عبر `PlatformInputDefault.dll` لأنه كان مستخدماً من اللعبة، لكن ليس كل ألعاب Unity تملك هذا الملف!
   - لتعميم الحقن على **أي لعبة Unity**:
     - الخيار الأساسي الموصى به: استخدام **Unity Doorstop** (`winhttp.dll` / `version.dll` + `doorstop_config.ini`) لتحميل **Universal Arabic Font Injector DLL** تلقائياً عند تشغيل أي لعبة Unity Mono دون تعديل أي ملف DLL أصلي من اللعبة!
     - الخيار الاحتياطي: Proxy DLL عند الحاجة، أو دعم IL2CPP (عبر BepInEx/MelonLoader) في مرحلة متقدمة.
7. **Stage 7 — `Verifier & Logger` (التحقق والتسجيل):**
   - فحص سلامة ملفات اللغة المترجمة (تطابق المفاتيح، سلامة JSON/CSV/XML، عدم ضياع المتغيرات).
   - توليد تقرير واضح + قراءة سجل الحقن داخل اللعبة (`arabic_translator_log.txt`) عند أول تشغيل لاكتشاف أي تعارض مشابه لـ `LanguageSDF`.

---

## 4. الهيكل البرمجي المقترح (Modular Architecture)

داخل المجلد: `C:\Users\asus\OneDrive\Desktop\tr\translator\`

```text
translator/
├── MEMORY.md                        # هذا الملف — الذاكرة الحية وسجل القرارات والتقدم
├── pyproject.toml / requirements.txt
├── main.py                          # نقطة الدخول (CLI / GUI)
├── core/
│   ├── __init__.py
│   ├── models.py                    # كائنات البيانات (GameInfo, TextEntry, TranslationJob, Report)
│   ├── pipeline.py                  # المنسّق العام للمراحل السبع (Orchestrator)
│   ├── backup.py                    # BackupManager + manifest.json + Rollback
│   └── config.py                    # الإعدادات العامة وخيارات كل محرك
├── engines/                         # طبقة تجريد المحركات (Plugin / Adapter Pattern)
│   ├── __init__.py
│   ├── base.py                      # الواجهة المجردة GameEngineAdapter (detect, extract, inject, verify)
│   ├── registry.py                  # تسجيل المحركات واختيار المحرك المناسب تلقائياً
│   └── unity/                       # محول محرك Unity (Mono و IL2CPP)
│       ├── __init__.py
│       ├── detector.py              # كشف Mono / IL2CPP / x86 / x64 / BepInEx / MelonLoader / TMP / UGUI
│       ├── il2cpp_metadata.py       # محلل ثنائي لملف global-metadata.dat واستخراج أدلة IL2CPP
│       ├── assets_extractor.py      # استخراج TextAsset و I2Languages من .assets بدون تعديلها
│       ├── scanner.py               # البحث عن ملفات اللغة (الخارجية و .assets وأدلة DLL / global-metadata)
│       └── injector.py              # نشر ملفات اللغة + حقن الخط (Doorstop Mono/IL2CPP CoreCLR / BepInEx 6 / MelonLoader)
├── formats/                         # محللات صيغ ملفات الترجمة (مشتركة بين كل المحركات)
│   ├── __init__.py
│   ├── base.py                      # FormatHandler + ParsedDocument (load, save)
│   ├── registry.py                  # get_format_handler لاختيار المحلل حسب الصيغة
│   ├── json_handler.py              # JSON + JSONC (تنظيف تعليقات // و /* */ والفواصل الزائدة)
│   ├── csv_handler.py               # CSV / TSV (ثنائي العمود أو جداول لغات متعددة مثل I2 Localization)
│   ├── xml_handler.py               # XML (حفظ السمات التعريفية name/id/key دون تعديل)
│   ├── ini_handler.py               # INI / TXT (حفظ التعليقات والأقسام وعلامات التنصيص و \n)
│   ├── po_handler.py                # GNU Gettext .po (دعم msgctxt و الأسطر المتعددة وحفظ الترويسة)
│   └── assets_override_handler.py   # صيغة unity_assets لتوليد ArabicOverride.json دون تعديل .assets
├── translators/                     # مزودو الترجمة (Online أولاً، مع قابلية Offline)
│   ├── __init__.py
│   ├── base.py                      # TranslatorProvider (translate_batch)
│   ├── google_batch.py              # مزود POST المجمع + التبديل + تقسيم الدفعات + الكاش
│   └── ...                          # مستقبلاً: offline_argos.py, llm_provider.py
├── arabic/                          # معالجة اللغة العربية المستقلة عن المحركات
│   ├── __init__.py
│   ├── protector.py                 # حماية المتغيرات والوسوم (<...>, {0}, %s)
│   ├── shaper.py                    # تشبيك الحروف (Presentation Forms-B + لام ألف)
│   └── bidi.py                      # الترتيب البصري Visual BiDi + عكس الأقواس
├── injectors/                       # كود الحقن التلقائي بلغة C# / Native
│   └── unity_runtime/
│       ├── ArabicRuntimeInjector.cs # النسخة العامة المعممة (Doorstop Mono + IL2CPP_BACKEND CoreCLR + BepInEx/MelonLoader + حماية LanguageSDF)
│       └── doorstop/                # ثنائيات Unity Doorstop v4.6.0 الرسمية
│           ├── x64/winhttp.dll
│           └── x86/winhttp.dll
└── ui/                              # الواجهة الرسومية GUI
    ├── __init__.py
    └── gui.py                       # واجهة Tkinter التفاعلية (فحص، تعريب بخيط خلفي، شريط تقدم، سجل مباشر، استرجاع)
```

### لماذا هذا الهيكل قابل للتوسع دون كسر شيء؟
- **`engines/`** مفصولة تماماً عن **`translators/`** و **`formats/`** و **`arabic/`**:
  - إذا أردنا لاحقاً دعم **Unreal Engine** أو **Godot** أو **RPG Maker**، نضيف مجلداً جديداً داخل `engines/` يطبّق واجهة `GameEngineAdapter` فقط؛ كل منطق الترجمة المجمعة، وحماية المتغيرات، وتشبيك العربية، والنسخ الاحتياطي يعمل معه فوراً دون أي تعديل!
  - إذا أردنا إضافة **مترجم Offline**، نضيف ملفاً داخل `translators/` يطبّق `TranslatorProvider`.
  - إذا أردنا دعم **صيغة ملفات جديدة** (مثل `.csv` أو `.locres` أو `.yaml`)، نضيف ملفاً داخل `formats/` يطبّق `FormatHandler`.

---

## 5. حالة التقدم الحالية والخطوات القادمة (Status & Roadmap)

### الحالة الحالية
- [x] إتمام التجربة المرجعية على *Subnautica: Below Zero* وتحديد كل التحديات (429 rate limits، حماية المتغيرات والوسوم، Presentation Forms-B + BiDi، الشاشة السوداء بسبب `.assets`، وحل تعارض `LanguageSDF` مع `TMP_FontAsset` عبر `TMP_Settings.fallbackFontAssets` و`m_SourceFontFile` و`LanguageSDF.clones`).
- [x] إنشاء مجلد المشروع `C:\Users\asus\OneDrive\Desktop\tr\translator\` وكتابة ملف الذاكرة والتخطيط `MEMORY.md`.
- [x] بناء المرحلة الأولى الكاملة داخل `C:\Users\asus\OneDrive\Desktop\tr\translator\`:
  - النواة والنسخ الاحتياطي (`core/models.py`, `core/backup.py`, `core/pipeline.py`).
  - خوارزمية اكتشاف المحرك وملف اللغة بدون موديل (`engines/base.py`, `engines/unity/detector.py`, `engines/unity/scanner.py`, `engines/unity/adapter.py`).
  - قارئ/كاتب JSON المتسامح (`formats/json_handler.py`).
  - الحماية والإصلاح الذاتي للمتغيرات (`arabic/protector.py`) وتشبيك الحروف والـ BiDi (`arabic/shaper.py`).
  - المترجم السحابي المجمع بدفعات كبيرة مع تبديل السيرفرات والانقسام الثنائي والكاش الذري (`translators/google_batch.py`).
  - الحاقن العام بلغة C# (`injectors/unity_runtime/ArabicRuntimeInjector.cs`) ومترجم الـ DLL التلقائي (`engines/unity/injector.py`).
  - واجهة CLI (`main.py`) والاختبارات الآلية (`tests/test_translator_pipeline.py`).

- [x] بناء المرحلة الثانية الكاملة داخل `C:\Users\asus\OneDrive\Desktop\tr\translator\`:
  - الواجهة الموحدة للصيغ والسجل (`formats/base.py`, `formats/registry.py`, `formats/__init__.py`) وتحديث `formats/json_handler.py` بـ `JsonFormatHandler`.
  - إضافة محللات وكتابات الصيغ المتعددة: `formats/csv_handler.py` (`CSV`/`TSV` وجداول `I2 Localization`)، `formats/xml_handler.py` (`XML`)، `formats/ini_handler.py` (`INI`/`TXT`)، و`formats/po_handler.py` (`GNU Gettext .po`) مع الكتابة الذرية والتحقق.
  - ربط جميع الصيغ مباشرة في `core/pipeline.py` (`AutoTranslatorPipeline`) والماسح `engines/unity/scanner.py`.
  - توفير ثنائيات **Unity Doorstop v4.6.0** الرسمية (`injectors/unity_runtime/doorstop/x64/winhttp.dll` و `x86/winhttp.dll`)، وتحديث `ArabicRuntimeInjector.cs` لدعم `Doorstop.Entrypoint.Start`/`Main` وربط `Camera.onPreRender` + `Application.onBeforeRender` على الخيط الرئيسي.
  - دعم تتبع الملفات المضافة (`BackupManager.register_created_file` في `core/backup.py`) وحذفها تلقائياً عند `rollback` دون المساس بالملفات الأصلية الموجودة مسبقاً، وتوليد `doorstop_config.ini` المتوافق مع Doorstop v3 و v4 في `engines/unity/injector.py`.
  - توسيع الاختبارات الآلية في `tests/test_translator_pipeline.py` (13 اختباراً ناجحاً تغطي كل الصيغ + نشر Doorstop + Rollback).

- [x] بناء المرحلة الثالثة الكاملة داخل `C:\Users\asus\OneDrive\Desktop\tr\translator\` (الخيار ج: أصول `.assets` المعبأة + الواجهة الرسومية GUI):
  - **استخراج الأصول المعبأة بدون تعديلها:**
    - `engines/unity/assets_extractor.py`: مسح ثنائي استدلالي لملفات `resources.assets` و`sharedassets*.assets` لاستخراج كتل `TextAsset` (بصيغ JSON/CSV/XML/INI/PO مع استبعاد الشادرات واللغات الثانوية) وجداول `I2Languages` الثنائية (`m_Terms`) وتحديد عمود الإنجليزية تلقائياً.
    - `formats/assets_override_handler.py` (`UnityAssetsFormatHandler` المسجل لصيغة `unity_assets` في `formats/registry.py`): حفظ الترجمات في `ArabicOverride.json` (`by_key` و`by_source`) في جذر اللعبة وتسجيله في `created_files` ليُحذف نظيفاً عند `rollback` دون تغيير أي بايت من ملفات `.assets`.
    - ربط فحص الأصول المعبأة في `engines/unity/scanner.py` (`score_assets_file`) و`core/pipeline.py` مع أولوية للملفات الخارجية الموثوقة (`>= 45`).
  - **اعتراض النصوص وقت التشغيل (`injectors/unity_runtime/ArabicRuntimeInjector.cs`):**
    - قراءة `ArabicOverride.json` بمحلل JSON مدمج متوافق مع C# 5 (`SimpleOverrideJsonParser`) ودعم القوالب الديناميكية ذات المتغيرات (`ParameterizedOverrideTemplate`).
    - تحديث مصطلحات `I2.Loc.LocalizationManager.Sources` في الذاكرة عبر Reflection، واعتراض وتحديث نصوص `TMP_Text` و`UnityEngine.UI.Text` النشطة وقت التشغيل.
  - **الواجهة الرسومية (GUI):**
    - `ui/__init__.py` و`ui/gui.py` (`TranslatorGUI` و`launch_gui`): واجهة Tkinter عربية لاختيار مجلد اللعبة، الفحص التلقائي، خيارات التشبيك والحقن، تنفيذ التعريب في خيط خلفي مع شريط تقدم وسجل مباشر (`queue.Queue`)، وزر الاسترجاع الكامل (`Rollback`).
    - تحديث `main.py` لفتح الواجهة الرسومية افتراضياً عند التشغيل بدون معاملات أو عبر `python main.py gui` مع إبقاء أوامر CLI (`scan`, `translate`, `rollback`).
  - **الاختبارات والتحقق:**
    - توسيع `tests/test_translator_pipeline.py` إلى **17 اختباراً آلياً ناجحاً** تغطي استخراج `TextAsset` و`I2Languages` دون تغيير بصمة `.assets`، وتوليد/حذف `ArabicOverride.json`، والترجمة الفعلية لكود `ArabicRuntimeInjector.cs` عبر `csc.exe` (لمساري `UGUI` و`USE_TMP + INCLUDE_INPUT_UTILS`)، وسير العمل الكامل في `TranslatorGUI`.

- [x] بناء المرحلة الرابعة الكاملة داخل `C:\Users\asus\OneDrive\Desktop\tr\translator\` (الخيار أ: دعم ألعاب **Unity IL2CPP** بالكامل):
  - **توسيع الاكتشاف وبصمات IL2CPP بدون موديل:**
    - `core/models.py`: إضافة الحقول `il2cpp_metadata_path` و`il2cpp_interop_dir` و`mod_loader` داخل `EngineInfo`.
    - `engines/unity/il2cpp_metadata.py`: محلل ثنائي لملف `global-metadata.dat` (`0xFAB11BAF`) لاستخراج `String Literals` والمعرّفات النصية بدون أدوات فك خارجية (`extract_il2cpp_metadata_strings`)، وباني ملفات اختبارية (`build_il2cpp_global_metadata_blob`)، وكاشف مكتبات النصوص `TextMeshPro` و`UGUI` (`detect_il2cpp_ui_frameworks_from_files`).
    - `engines/unity/detector.py` و`engines/unity/scanner.py` و`core/pipeline.py`: اكتشاف `BepInEx` / `MelonLoader` ومجلدات `interop`/`Il2CppAssemblies`، ودمج أدلة `global-metadata.dat` (`extract_il2cpp_localization_hints`) مع الماسح رباعي الطبقات، ودعم الترجمة الخارجية و`.assets` (`ArabicOverride.json`) لألعاب IL2CPP.
  - **الحاقن المزدوج وبيئة التشغيل وقت التشغيل:**
    - `injectors/unity_runtime/ArabicRuntimeInjector.cs`: إضافة وضع `IL2CPP_BACKEND` المتوافق تماماً مع C# 5 (`Il2CppPluginBootstrap` و`Il2CppRuntimeInjector` و`Il2CppNativeApi` عبر P/Invoke مع `GameAssembly.dll` + دعم Interop Reflection لبناء `ArabicFallbackSDF` وتحديث `I2.Loc` و`TMP_Text`/`UGUI Text`) مع مشاركة `OverrideTranslationStore` و`RuntimeOverrideReflection` بين Mono وIL2CPP.
    - `engines/unity/injector.py`: إضافة `find_coreclr_runtime` و`compile_il2cpp_injector` ودعم `[Il2Cpp]` (`coreclr_path` و`corlib_dir`) داخل `doorstop_config.ini`، مع مسارات حقن تلقائية لكل من `il2cpp_bepinex` (`BepInEx/plugins`) و`il2cpp_melonloader` (`Mods`) و`il2cpp_doorstop` (Doorstop v4.6.0 CoreCLR) وتتبع كامل في `created_files` للاسترجاع النظيف.
  - **الاختبارات والتحقق:**
    - توسيع `tests/test_translator_pipeline.py` بإضافة `Il2CppPhase4Tests` و`BackupUxAndGeneralizationTests` و`Unity6Il2CppAndUnityFsTests` ليصل الإجمالي إلى **27 اختباراً آلياً ناجحاً (`OK`)** تغطي اكتشاف `IL2CPP` وأدلة `global-metadata.dat` (الكلاسيكية والحديثة ثلاثية الحقول v31+/v108)، وفك ضغط حزم `UnityFS` (`data.unity3d` بـ LZ4) واستخراج `SceneText:binary`، وتعريب `.assets` + نشر Doorstop `[Il2Cpp]` + CoreCLR + الاسترجاع، والنشر النظيف لـ `BepInEx` و`MelonLoader`، والترجمة الفعلية بـ `csc.exe` لجميع الأوضاع (`UGUI`, `USE_TMP + INCLUDE_INPUT_UTILS`, `IL2CPP_BACKEND`) مع التحقق من اسم التجميع الداخلي وتحميله عبر `coreclr.dll`، وتجربة النسخ الاحتياطي التفاعلية عند إعادة فتح البرنامج، وكشف `.bytes` والجداول الثنائية `StringTable:binary`.
  - **تحسينات التوافق العملي (التحقق على لعبة *Getting Over It with Bennett Foddy v1.7* و*Paws Menu v0.1.1* وتعميم Unity):**
    - `engines/unity/il2cpp_metadata.py`: دعم ترويسة `global-metadata.dat` الحديثة (Unity 2023+ / Unity 6 — v31+/v108 بتخطيط ثلاثيات الإزاحات) بجانب الترويسة الكلاسيكية، وفحص `ScriptingAssemblies.json` و`data.unity3d` في `detect_il2cpp_ui_frameworks_from_files`.
    - `engines/unity/assets_extractor.py`: دعم فك ضغط حزم `UnityFS` (`data.unity3d` / `.unity3d` / `.bundle`) بخوارزمية LZ4 خالصة (`lz4_decompress_block` و`extract_unityfs_nodes_from_bytes`) مع تخطي `.resS`، واستخراج نصوص الواجهة وجمل المشاهد (`SceneText:binary` via `extract_scene_ui_texts_from_bytes`) كبديل عند غياب `TextAsset`/`I2Languages`/`StringTable`، واستبعاد مسارات `InputSystem` و`PerformanceTestRunInfo`.
    - `engines/unity/assets_extractor.py`: دعم مخطط `I2.Loc.TermData` المحتوي على حقل `Description` وبادئة حقول `I2Languages` (`0, 0, 1`) وحقول ما بعد `m_Terms` قبل `m_Languages`، وقصر الاستخراج على `TermType == 0`، إضافةً إلى استخراج جداول أزواج `(Key/Value)` المتتالية (`StringTable:binary`) في الأصول.
    - `engines/unity/scanner.py` و`formats/assets_override_handler.py`: استبعاد أسماء خطوط `* SDF` / `*_SDF` ومسارات `InputSystem` و`RuntimeInitializeOnLoads.json` و`ArabicOverride.json`، وفهرسة الأسطر المقسّمة بالسطر الجديد `\n` داخل `ArabicOverride.json`، ودعم الاكتشاف التلقائي لصيغة المحتوى الداخلي لملفات `.txt` و`.bytes` (`detect_candidate_format`)، وضم `data.unity3d` و`level0` و`.bundle`/`.unity3d` و`globalgamemanagers.assets` لملفات الأصول المفحوصة وتقييم `SceneText:binary`.
    - `engines/unity/injector.py` و`injectors/unity_runtime/ArabicRuntimeInjector.cs`: بناء `_run_csc_compile` داخل مجلد مؤقت بنفس اسم الـ DLL النهائي (بدلاً من `.tmp`) لتفادي فشل `coreclr_create_delegate` (`0x80070002`) في Doorstop CoreCLR، وربط `il2cpp_string_new_utf16` ودوال التسلسل الهرمي في `Il2CppNativeApi` مع قصر استدعاء `Resources.FindObjectsOfTypeAll` (`ApplyNativeIl2CppOverrides`) على سياق الخيط الرئيسي (`isMainThread == true`) وتخصيص `ApplyNativeI2LocalizationOverrides` الآمنة لخيط الخلفية لتفادي `0xC0000005` في IL2CPP بدون Mod Loader، إضافة إلى دعم `netstandard.dll` و`DeferredMonoBootstrap` و`ResourcesAPIInternal` و`UnityEngine.TextMesh`.
    - `core/backup.py` و`ui/gui.py`: إضافة `BackupManager.get_backup_summary` وتصفير السجل وتنظيف ملفات `arabic_font_log.txt`/`arabic_translator_log.txt` عند `rollback`، وتفعيل الاكتشاف التلقائي للنسخ الاحتياطي المحفوظ في الواجهة (`prompt_backup_decision_if_present`) فور تحديد مجلد اللعبة لتخيير المستخدم بين الاسترجاع الفوري أو الإبقاء على النسخة الاحتياطية وإعادة تجربة التعريب حتى بعد إغلاق البرنامج وإعادة تشغيله.

- [x] بناء المرحلة الخامسة الكاملة (IL2CPP بدون Mod Loader + الصيغ المضغوطة والخاصة):
  - **تشغيل IL2CPP على الخيط الرئيسي بدون BepInEx/MelonLoader (`injectors/unity_runtime/ArabicRuntimeInjector.cs`):**
    - إضافة `TryInstallMainThreadHook` داخل `Il2CppNativeApi` بطبقتين:
      1. خطاف `MethodInfo->methodPointer` (إزاحة `0`) على دوال Unity الرئيسية (`UnityEngine.Canvas.SendWillRenderCanvases` ثم `Application.InvokeOnBeforeRender` ثم `Camera.FireOnPreRender`) لتنفيذ التحديث الدوري على الخيط الرئيسي بدون فك شيفرة أو تعديل `.text`.
      2. خطاف احتياطي عبر `WndProc` (`SetWindowLongPtrW`/`SetWindowLongW` + `PostMessageW` على نافذة `UnityWndClass`) إذا لم تتوفر الدوال السابقة.
    - إضافة `EnsureNativeTmpFallbackFont` + `CreateNativeTmpFontAsset` عبر `Il2CppNativeApi` (تجربة `TMP_FontAsset.CreateFontAsset` بـ 7 معاملات لملف TTF أو 3 معاملات لاسم الخط أو 1 معامل مع `UnityEngine.Font` عبر `CreateDynamicFontFromOSFont`/`.ctor`/`Internal_CreateFontFromPath`) وبناء وإرفاق `ArabicFallbackSDF` في `TMP_Settings.fallbackFontAssets` مع `ForceMeshUpdate`.
    - إضافة `RecordUnmatchedText` في `OverrideTranslationStore` لالتقاط النصوص الحية غير المترجمة إلى `arabic_captured_texts.txt` كشبكة أمان للألعاب المشفرة بخوارزميات خاصة.
    - **التحقق العملي على *Paws Menu v0.1.1* (Unity 6000.6.5f1 IL2CPP بدون Mod Loader):** نجح حقن `Canvas.SendWillRenderCanvases` وبناء وإرفاق `ArabicFallbackSDF` وتطبيق `8` نصوص عربية في `TMP_Text` مع بقاء اللعبة تعمل بثبات وبدون أي خطأ في `arabic_font_log.txt` أو `Player.log`.
  - **دعم الصيغ المضغوطة/المغلّفة وجداول Unity Localization وAddressables (`engines/unity/assets_extractor.py`, `engines/unity/scanner.py`):**
    - فك ضغط وتغليف حمولات `GZip` / `Zlib` / `Deflate` / `Base64` (بما فيها التغليف المتداخل مثل `Base64(GZip(JSON))`) داخل `TextAsset` وملفات `.bytes` عبر `decode_compressed_or_encoded_payload`.
    - دعم جداول **Unity Official Localization Package** (`UnityEngine.Localization.Tables.StringTable` — `m_TableData` ثنائي) عبر `extract_unity_localization_string_tables_from_bytes` (`UnityLocalization:binary`).
    - توسيع `_collect_assets_paths` للبحث المتعمق عن حزم **Addressables** (`.bundle` / `.unity3d` داخل `StreamingAssets/aa/` وغيرها) وملفات `.bytes` المضغوطة، مع استبعاد `aa/catalog.json` و`aa/settings.json` من الترشيح الخاطئ.
  - **الخط العربي الافتراضي (`IBM Plex Sans Arabic`) وتحديث الألعاب المعرّبة مسبقاً:**
    - دمج خط `IBMPlexSansArabic-Regular.ttf` في `assets/fonts/IBMPlexSansArabic-Regular.ttf` ونشره تلقائياً باسم `ArabicFont.ttf` مع تسجيله في `created_files` داخل `engines/unity/injector.py` (`resolve_arabic_font_path` و`deploy_arabic_font`).
    - تحديث `ArabicRuntimeInjector.cs` (`FindAndRegisterBundledArabicFont` + `AddFontResourceExW` بوضع `FR_PRIVATE`) لتفضيل `ArabicFont.ttf` / `IBM Plex Sans Arabic` في جميع مسارات الحقن (Mono `TMP` و`UGUI`، وIL2CPP Interop وNative).
    - إضافة مسار `update_font` في `core/pipeline.py` و`engines/unity/injector.py` وأمر `update-font` في `main.py` وزر **"تحديث الخط العربي"** في `ui/gui.py` لإضافة/تحديث الخط على أي لعبة معرّبة مسبقاً دون المساس بملفات الترجمة أو النسخ الاحتياطية.
  - **الاختبارات:** توسيع `tests/test_translator_pipeline.py` بإضافة `CompressedAndLocalizationTableTests` و`DefaultArabicFontAndUpdateTests` و`EdgeCasesAndResilienceTests` ليصل الإجمالي إلى **34 اختباراً آلياً ناجحاً (`OK`)**.

- [x] إضافة دعم محرك **RPG Maker MV / MZ** (`engines/rpgmaker/`) مع `requires_arabic_shaping=False` وفلترة سياقية لأوامر الأحداث وحقن CSS والخط العربي (`38` اختباراً).
- [x] إضافة دعم كامل لمحرك **Godot Engine (3.x / 4.x)** (`engines/godot/` و `formats/godot_pck_handler.py`):
  - محلل ثنائي بلغة Python خالصة لحزم `.pck` المستقلة والمدمجة في `.exe` (`GDPC` v1/v2+)، و`project.godot`/`project.binary`، وموارد `.translation` (`RSRC` `Translation`/`OptimizedTranslation` و`.tres`) دون المساس ببصمة الملفات الأصلية.
  - تمييز دقيق بين **Godot 3.x** (`requires_arabic_shaping=True` + توليد `arabic_font.tres` `DynamicFont` + `PoolStringArray`) و**Godot 4.x** (`requires_arabic_shaping=False` + `PackedStringArray` + HarfBuzz/ICU الأصلي) مع كشف معمارية PE (`x64`/`x86`).
  - تحديث `arabic/protector.py` و`arabic/shaper.py` لحماية متغيرات Godot المسماة `{player_name}` ووسوم BBCode (`[color=...]`, `[b]`, `[url=...]`)، وتحديث `formats/csv_handler.py` (`keys`) و`formats/po_handler.py` (`.pot`).
  - نشر الخط `IBMPlexSansArabic-Regular.ttf` في `arabic_l10n/` وتوليد/تحديث `override.cfg` وحزمة تكميلية `arabic_patch.pck` مع دعم `update_font` و`Rollback` كامل (`42` اختباراً ناجحاً `OK`).
- [x] إضافة دعم كامل لمحرك **Unreal Engine (UE4 / UE5)** (`engines/unreal/` و`formats/locres_handler.py` و`formats/unreal_pak_handler.py`):
  - اكتشاف مؤشرات Unreal (`*.uproject`, `Engine/Binaries`, `*-Win64-Shipping.exe`/`*-Win32-Shipping.exe`, `Content/Paks/`, `.pak`/`.utoc`/`.ucas`/`.locres`) وتحديد المعمارية من PE والتمييز بين UE4 وUE5 مع الحفاظ على `engine_name="Unreal Engine"` و`requires_arabic_shaping=False` (دعم HarfBuzz/ICU الأصلي).
  - محلل وكاتب ثنائي بلغة Python خالصة لملفات `.locres` بجميع إصداراتها (`Legacy` v0، `Compact` v1، `Optimized` v2، `OptimizedCityHash64` v3) مع حفظ الهاشات والمفاتيح والـ `RefCount` وترميز UTF-16-LE للعربية.
  - محلل وباني حزم `.pak` (`v1–v11` بما فيها `EncodedPakEntries` و`FullDirectoryIndex` وفك ضغط `Zlib`/`Gzip`/`LZ4` مع تنبيهات AES وIoStore `.utoc`/`.ucas`) دون المساس ببصمة `.pak` الأصلية؛ بناء حزمة التعديل غير المُتلفة `Content/Paks/~mods/z_ArabicL10n_P.pak` ونشر `IBMPlexSansArabic-Regular.ttf` وتحديث `DefaultEngine.ini` مع `update_font` و`Rollback` كامل.
  - تحديث `arabic/shaper.py` لحماية أزواج وسوم Unreal RichText (`<Bold>...</>`, `<span style="...">...</>`)، وحماية صيغ `FText` (`{Count}|plural(...)` / `gender` / `ordinal`)، ومعالجة حالات الفشل الواقعية: توحيد `MountPoint="../../../"` في `~mods` لمنع تكرار اسم المشروع، استخدام `+LocalizationPaths` لتجنب مسح إعدادات `BaseEngine.ini`، قراءة `.locmeta` واكتشاف ملفات `.sig` و`FullDirectoryIndex` المحذوف، دعم فك ضغط `Oodle` عبر `oo2core_*.dll`، ومسح ملفات DataTables/النصوص (`.csv`/`.json`/`.po`/`.ini`/`.txt`) الخارجية والمضمّنة في `.pak`.
- [x] تحصين شامل لحالات الفشل عبر جميع المحركات والصيغ (`52` اختباراً ناجحاً `OK`):
  - دعم كشف ترميز **UTF-16-LE / UTF-16-BE** وحفظه في [formats/base.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/formats/base.py) وجميع محللات الصيغ و`UnityScanner` (.bytes)، وحماية صفوف رؤوس DataTables (`---`, `RowName`, `Name`) في [formats/csv_handler.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/formats/csv_handler.py).
  - إصلاح `is_file_likely_arabic` في [core/backup.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/core/backup.py) لتفحص الملفات المنتهية بـ `.bak` (بما فيها `.locres.bak`) قبل الاعتماد عليها كمصدر نظيف.
  - RPG Maker: استثناء مفتاح `locale` الجذري من الترجمة، وحماية وسوم NameBox/الألوان (`\n<Hero>`, `\c[#FF0000]`)، وتوسيع `@font-face` (`unicode-range`) لتغطية أسماء الخطوط المخصصة في `gamefont.css` / `css/game.css` تلقائياً.
  - Godot: تنبيه واضح عند مصادفة موارد `OptimizedTranslation` مضغوطة بـ SMAZ بدون ملفات `.po`/`.csv` مصدرية.
- [x] تحسينات الجودة والدعم المتقدم عبر المحركات الأربعة (`64` اختباراً ناجحاً `OK`):
  - **RPG Maker MV / MZ (`AutoTranslator_ArabicText.js`):** حل مشكلة رسم الحوار حرفاً بحرف (`Window_Base.processNormalCharacter` في MV و`Window_Message.canBreakHere` في MZ) عبر حقن إضافة [AutoTranslator_ArabicText.js](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/injectors/rpgmaker_runtime/AutoTranslator_ArabicText.js) وإدارة [engines/rpgmaker/plugin_patch.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/rpgmaker/plugin_patch.py) لتسجيلها بأمان في `js/plugins.js` مع خيارات CLI/GUI (`--no-rpgmaker-text-fix` / `--rpgmaker-text-mode`) و`Rollback` كامل.
  - **Godot (SMAZ & Theme Merge):** إكمال جدول وضغط/فك ضغط **SMAZ** القياسي (`SMAZ_RCB` ذو 254 مدخلاً، `smaz_decompress`، `smaz_compress`) في [engines/godot/pck_parser.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/godot/pck_parser.py) لفك ترجمات `OptimizedTranslation` المضغوطة تلقائياً، وتطوير [engines/godot/injector.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/godot/injector.py) لدمج الخط العربي داخل نسخه من الثيم المخصص الأصلي (`theme/custom` `.tres`) دون مسح أنماط الأزرار والألوان.
  - **Unreal Engine (IoStore `.utoc` + `UStringTable` + تقرير جاهزية الخط العربي):** إضافة محلل ترويسة وفهرس **IoStore (`.utoc`)** (`FIoStoreTocHeader` — `parse_utoc_header`) وفاحص ومستخرج أصول `.uasset`/`.uexp` (`0x9E2A83C1` — `inspect_uasset_bytes` لكشف `UStringTable`/`UDataTable`/`UFontFace`) في [engines/unreal/pak_parser.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unreal/pak_parser.py)، وربطهما في [engines/unreal/scanner.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unreal/scanner.py) لإصدار تقرير جاهزية الخط العربي (تمييز الخطوط العربية المدعومة عن الخطوط اللاتينية المسببة للمربعات).
  - **YAML + تعدد ملفات Unity + النصوص الملتقطة + حماية الرموز:** إضافة محلل وكاتب **YAML (`.yaml` / `.yml`)** بلغة Python خالصة في [formats/yaml_handler.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/formats/yaml_handler.py) (مع دعم التعليقات والقوائم والكتل المتعددة `|`/`>` وUTF-16)، وتحصين [arabic/protector.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/arabic/protector.py) ضد التحويل الخاطئ للأقواس المفردة `[1]` مع تنظيف علامات الاتجاه الخفية (`\u200E`/`\u200F`/`\u061C`)، ودعم اختيار الملفات المتكاملة داخل مجلد اللغة الإنجليزية الصريح في [engines/unity/adapter.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unity/adapter.py)، وإتاحة استيراد وترجمة `arabic_captured_texts.txt` ودمجه في `ArabicOverride.json` عبر `translate_captured_texts` (`translate-captured` في CLI وزر الواجهة).
- [x] ترقية ذكاء وسرعة خوارزمية اكتشاف Unity — **الفحص العميق الذكي الموجَّه بالفهارس (`75` اختباراً ناجحاً `OK`):**
  - **جرد Build Inventory في [engines/unity/detector.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unity/detector.py) و[core/models.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/core/models.py):** استخراج `unity_version` تلقائياً، وبصمة `localization_systems` (`Unity.Localization`, `I2.Localization`, `Addressables`, `YarnSpinner`, `Ink`, `Fungus`, `DialogueSystem`, `Naninovel`, `LeanLocalization`)، وتحذيرات المخاطر `risk_warnings` (`EasyAntiCheat`, `BattlEye`, `GameGuard`).
  - **محلل فهرس `SerializedFile` المتدفق + تسريع `LZ4` في [engines/unity/assets_extractor.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unity/assets_extractor.py):** قراءة ترويسة وجدول كائنات `.assets`/`level*` (v9–v22+) عبر `seek` لتخطي ملفات الصور/الأصوات في أجزاء من الثانية، واستخراج دقيق لكائنات `TextAsset` (49) و`TextMesh` (102) و`MonoBehaviour`/`ScriptableObject` dialogue (114)، مع قراءة متدفقة لـ `UnityFS` وذاكرة تخزين مؤقت LRU متعددة الخيوط، وتسريع `LZ4` عبر `lz4.block` اختيارياً مع تحسين تكرار التطابق في Python.
  - **خريطة Addressables (`catalog.json`) + `level*` + التقييم المتوازي + احتياطي سلاسل الكود في [engines/unity/scanner.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unity/scanner.py):** فك ترميز `m_EntryDataString`/`m_BucketDataString`/`m_resourceTypes` لتقديم حزم الترجمة وتخطي حزم الوسائط الخالصة، وشمول جميع `level*` و`sharedassets*`، ومطابقة لغات الشقيقات على مستوى الكلمات الكاملة (`is_sibling_lang_asset`)، وفحص متعدد الخيوط، واحتياطي `unity_code` (`UnityCodeStringsFormatHandler` في [formats/assets_override_handler.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/formats/assets_override_handler.py)) لاستخراج نصوص الواجهة النظيفة من `#US`/`global-metadata.dat` عند غياب ملفات اللغة والأصول.
  - **الفحص العميق الافتراضي القابل للتعطيل + دمج المصادر ومنع التكرار:** تفعيل `deep_scan=True` افتراضياً في [engines/unity/adapter.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unity/adapter.py) مع دمج جميع ملفات `.assets`/`level*`/`.bundle` الإنجليزية معاً وإضافة الأصول غير المكررة للملفات الخارجية، مع خيار تعطيل الفحص العميق (`deep_scan_chk` في [ui/gui.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/ui/gui.py) و`--fast-scan`/`--no-deep-scan` في [main.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/main.py)).
- [x] حماية انقطاع الكهرباء والأعطال بنظام **الالتزام الذري على مرحلتين بدون لمس اللعبة (`Zero-Touch Two-Phase Commit`) + سجل `WAL` بـ `fsync` (`80` اختباراً ناجحاً `OK`):**
  - **كتابة ذرية صلبة وسجل الجلسات في [core/backup.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/core/backup.py):** دوال `atomic_write_bytes` و`atomic_write_text` و`atomic_copy_file` مع `flush()` + `os.fsync()` + `os.replace()`، وحفظ نسخة احتياطية `.prev` لملف `backup_manifest.json`، وفئة `TransactionJournal` لإدارة `session_state.json` ومجلد `staging/` والاستعادة التلقائية `recover_if_interrupted_commit`.
  - **حفظ التقدم بعد كل دفعة بـ WAL في [translators/google_batch.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/translators/google_batch.py):** كتابة كل دفعة فوراً إلى `translation_cache.json.wal.jsonl` مع `os.fsync` ونسخة `.prev` (مع تجاهل السطر الأخير المبتور تلقائياً عند التلف)، وإضافة `TranslationInterruptedError` ودعم `cancel_event` للإيقاف الآمن الفوري.
  - **العزل الكامل في [core/pipeline.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/core/pipeline.py):** المرحلة 1 (الترجمة) والمرحلة 2 (التجهيز والتحقق داخل `.arabic_translator/staging/`) لا تلمسان أي ملف لعبة ولا تنشئان `.bak`، ثم المرحلة 3 تنسخ احتياطياً وتلتزم ذرياً مع `Rollback` تلقائي عند أي فشل أو انقطاع مفاجئ دون مسح الكاش، مع دعم الإيقاف الآمن (`حفظ وإيقاف مؤقت` في [ui/gui.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/ui/gui.py)) وأمر `recover` في [main.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/main.py)، وتحصين `os.fsync` في جميع محللات الصيغ والحاقنات الأربعة.
- [x] دعم استخراج وتقييم **`UnityEngine.UI.Text` (UGUI)** والمشاهد ذات النص الواحد وتنقية `unity_code` واستثناء ملفات السجل والنصوص الملتقطة (`84` اختباراً ناجحاً `OK`):
  - **محلل ثنائي لـ `UGUI:Text` في [engines/unity/assets_extractor.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unity/assets_extractor.py):** إضافة `_extract_ugui_text_from_monobehaviour` و`_validate_ugui_font_data` (مع التحقق من بصمة `CullStateChangedEvent` وتخطيط `m_FontData` ومحاذاته، بالإضافة لمسار احتياطي لـ Unity 2020.1+ بدون `m_TypeName`) لاستخراج النصوص القصيرة والكلمات المفردة ومتعددة الأسطر (`\n`) من كائنات `MonoBehaviour` (`ClassID == 114`) ككتلة موثّقة بنوع `UGUI:Text` حتى لمشاهد النص الواحد (`len >= 1`)، مع السماح بالأسطر المتعددة في `extract_scene_ui_texts_from_bytes` للترويسات الموثّقة (`TMP_Text` و`UGUI:Text`).
  - **ترجيح ودمج مشاهد `UGUI:Text` و`TextMesh` في [engines/unity/scanner.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unity/scanner.py) و[engines/unity/adapter.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unity/adapter.py):** منح كتل `UGUI:Text` و`TextMesh` الموثّقة درجة `+35` (+10 لمشاهد/أصول اللعبة) وتجاوز حد `len < 3` بحيث تنجح مشاهد النص الواحد مثل `level1`/`level2` وتُختار جميعاً في `UnityAdapter.select_language_files` (أساسياً وتكميلياً مع الملفات الخارجية).
  - **تنقية فلتر `unity_code` واستثناء `arabic_captured_texts.txt`:** تشديد `_is_clean_code_ui_string` و`_CODE_NOISE_SUBSTRINGS` لاستبعاد مسارات الشيدر (`/`) وربط الأزرار (`joystick button 0`) ورسائل خطأ/تحذيرات Standard Assets وPostProcessing وFMOD، وإضافة `arabic_captured_texts.txt` مع سجلات التشغيل إلى `CONFIG_PENALTY_NAMES` وتخطيها صراحة في `UnityLanguageScanner.scan()`، مع توحيد مطابقة `\r\n`/`\n` وتفضيل الخط العربي المدمج في `GetUguiArabicOsFont()` داخل [injectors/unity_runtime/ArabicRuntimeInjector.cs](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/injectors/unity_runtime/ArabicRuntimeInjector.cs) و[formats/assets_override_handler.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/formats/assets_override_handler.py).
- [x] تحصين معماري شامل لـ Unity: ربط **`MonoScript` (`ClassID 115`)** ومستخرج **`TMP:Text`** المخصص ومنع ضوضاء Typewriter/AssemblyQualifiedName وتجاوز الاستيراد الدائري (`88` اختباراً ناجحاً `OK`):
  - **فك الاستيراد الدائري في [formats/registry.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/formats/registry.py):** تحويل تحميل المحللات إلى `_ensure_registry()` الكسول المحمي بقفل `threading.Lock()` للسماح باستيراد `assets_extractor` أو ماسحات المحركات مباشرةً بأي ترتيب.
  - **قراءة `m_Externals` وربط `MonoScript (115)` في [engines/unity/assets_extractor.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unity/assets_extractor.py):** تحليل `m_ScriptTypes`/`m_Externals` في `SerializedFileIndex` وبناء جدول `MonoScript` المخزّن مؤقتاً (`parse_monoscript_from_bytes` و`_load_monoscript_table_for_file`) لفصل كائنات المحرك التقنية (`TMP_SpriteAsset`, `UI.Image`, `Button`, `EventSystem`, إلخ) عن النصوص الحقيقية وتوفير مسار استخراج مؤكد للرؤوس المزاحة.
  - **مستخرج `TMP:Text` مخصص ودقيق عبر Unity 2018–Unity 6:** إضافة `_extract_tmp_text_from_monobehaviour` و`_validate_tmp_post_text` لاستخراج `TextMeshProUGUI`/`TextMeshPro` ككتلة موثّقة `TMP:Text` (بما فيها الكلمات المفردة والأسطر المتعددة ومشاهد النص الواحد وتعايشها مع `MonoBehaviour:dialogue`) مع تقييمها واختيارها في [engines/unity/scanner.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unity/scanner.py) و[engines/unity/adapter.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unity/adapter.py) (تم التحقق بالقراءة فقط على *Strange Little Town* و*Getting Over It* و*Subnautica: Below Zero*).
  - **منع أسماء الأنواع البرمجية وحماية التقاط النصوص + تشخيص `[Diag]`:** إضافة `_is_assembly_qualified_type_name` لمنع سلاسل `UnityEvents` مثل `GameStateChanger, Assembly-CSharp` دون المساس بالحوارات الطبيعية ذات الفواصل مثل `Hi, Sensei.`، وتحصين `RecordUnmatchedText` ضد التقاط مقاطع الآلة الكاتبة (Typewriter debounce) مع تسجيل ملخص `[Diag]` عند تغيّر الحالة فقط في [injectors/unity_runtime/ArabicRuntimeInjector.cs](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/injectors/unity_runtime/ArabicRuntimeInjector.cs).
- [x] حل الثغرات العامة المكتشفة من اختبار *Helltaker* (الملفات السطرية بامتداد `.json` ومجلدات تقسيم النصوص وخط UGUI للنصوص الخارجية) (`92` اختباراً ناجحاً `OK`):
  - **`TxtFormatHandler` منفصل في [formats/ini_handler.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/formats/ini_handler.py) و[formats/registry.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/formats/registry.py):** فصل `TxtFormatHandler` (`forced_format = "txt"`) عن `IniFormatHandler` بحيث تُقرأ الملفات النصية السطرية ذات الامتدادات المغايرة (مثل `.json` أو `.bak`) بوضع `"txt"` مع حفظ ترميز UTF-16 BOM وعدد الأسطر والفراغات وعلامات الاقتباس بدقة.
  - **فحص المحتوى الفعلي وتمييز CSV عن الحوارات في [engines/unity/assets_extractor.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unity/assets_extractor.py) و[engines/unity/scanner.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unity/scanner.py):** إضافة `_detect_structured_delimited_format` لإلزام التماثل العمودي أو الرؤوس القياسية في CSV/TSV حتى لا تُصنّف السطور الحوارية ذات الفواصل الطبيعية خطأً كجداول CSV، وتحديث `detect_candidate_format` لاكتشاف محتوى ملفات `.json` بترميز UTF-16/UTF-8 (تحويل الملفات السطرية الصالحة مثل `local/*.json` إلى `"txt"` مع إبقاء JSON الصالح `"json"` ورفض الملفات الثنائية بـ `None`)، وإضافة `local`/`loc`/`l10n` لأولويات المجلدات وتوحيد قراءة UTF-16 في `_read_entries_sample`.
  - **دعم مجلدات تقسيم النصوص المتعددة في [engines/unity/adapter.py](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/engines/unity/adapter.py):** إضافة `_dir_has_sibling_languages` وتحديث `UnityAdapter.select_language_files` لاختيار جميع ملفات مجلد التقسيم المتعدد (مثل `local/*.json`) وأي مجلد تقسيم آخر مطابق لأدلة DLL (مثل `localHM/*.json`) دون كسر اختيار اللغة الإنجليزية المنفردة في المجلدات أحادية اللغة ذات اللغات الشقيقة (مثل `English.json` بجانب `French.json`).
  - **تبديل خط UGUI/TextMesh للنصوص المعرّبة خارجياً في [injectors/unity_runtime/ArabicRuntimeInjector.cs](file:///c:/Users/asus/OneDrive/Desktop/tr/translator/injectors/unity_runtime/ArabicRuntimeInjector.cs):** إزالة اشتراط `_store.HasOverrides` لمنع حجب `ApplyActiveUiTextOverrides()` و`ApplyActiveUiTextOverridesViaReflection()` وتوحيد تبديل خط `UnityEngine.UI.Text` / `TextMesh` ليشمل النصوص المعرّبة مسبقاً من ملفات اللغة الخارجية (`ContainsArabicPresentationForms(curText)`) في وضعي Mono وIL2CPP (تم التحقق بالقراءة فقط على *Helltaker*: اختيار كامل الـ `47` ملفاً في `local/` و`localHM/` بصيغة `txt` بجانب الأصول بدون أي تراجع على *Strange Little Town* و*Subnautica: Below Zero*).

### خريطة الطريق المقترحة (Next Steps)
1. **المرحلة القادمة — التوسع لمحركات ومترجمات إضافية:**
   - إضافة دعم محرك **Ren'Py** (`game/tl/<lang>/*.rpy` واستخراج `.rpa` والخطوط العربية) كـ Adapter جديد داخل `engines/`.
   - إضافة خيار **الترجمة Offline** (مثل Argos Translate / NLLB / نماذج محلية مثل Ollama) داخل `translators/` مع التبديل من الواجهة الرسومية.
   - اختبار الواجهة والأداة عملياً على ألعاب Unity وRPG Maker وGodot وUnreal Engine حقيقية متنوعة.

---

## 6. سجل القرارات والملاحظات المهمة (Decision Log)
- **2026-11-15:** اعتماد الترجمة **Online (Batched POST)** كخيار أول افتراضي للسرعة والبساطة، مع تصميم طبقة `translators/` بواجهة مجردة تسمح بإضافة Offline لاحقاً دون إعادة هيكلة.
- **2026-11-15:** اعتماد حقن الخط العربي **وقت التشغيل (Runtime Injection)** بدلاً من تعديل ملفات `.assets` الثابتة لتفادي مشاكل توافق الإصدارات (الشاشة السوداء).
- **2026-11-15:** في ألعاب Unity التي تستخدم `TextMeshPro` وأنظمة تبديل لغة مثل `LanguageSDF`، يجب إرفاق الخط العربي في `TMP_Settings.fallbackFontAssets` (مع فحص ومراقبة تهيئة اللغة) وعدم إضافته لقوائم `fallbackFontAssetTable` الفردية للخطوط إلا للضرورة، وتعيين `m_SourceFontFile` احتياطي لتجنب استنساخ/مسح يكسر تهيئة خطوط اللعبة.
- **2026-11-16:** اعتماد خوارزمية اكتشاف ملف اللغة **بدون موديل** بـ 4 طبقات (`#US` User Strings داخل DLL + تقييم النقاط 0–100 + خصم اللغات الثانوية وفحص `.bak` النظيفة + فلتر المعرّفات البرمجية).
- **2026-11-16:** ربط `ArabicRuntimeInjector.cs` بحدث `Camera.onPreRender` (و `Application.onBeforeRender` احتياطياً) من نقطة دخول `Doorstop.Entrypoint` لضمان تهيئة `MonoBehaviour` وبناء الخط العربي على الخيط الرئيسي حتى في الألعاب التي لا تستدعي `[RuntimeInitializeOnLoadMethod]` للمكتبات المحقونة خارجياً.
- **2026-11-16:** تتبع الملفات المنشأة حديثاً في `backup_manifest.json` (`created_files`) بشكل منفصل عن الملفات الأصلية المعدلة (`files`) لضمان استرجاع نظيف يحذف ملفات Doorstop المضافة ولا يمس أي `winhttp.dll` أصلي كان موجوداً قبل التعريب.
- **2026-11-16:** التعامل مع أصول `.assets` المعبأة (`TextAsset` / `I2Languages`) عبر الاستخراج غير المعدِّل إلى `ArabicOverride.json` واعتراض النصوص وقت التشغيل (عبر `I2.Loc` Reflection و`TMP_Text` و`UGUI Text` مع دعم القوالب ذات المتغيرات) للحفاظ على سلامة بصمة ملفات `.assets` الأصلية بنسبة 100%.
- **2026-11-16:** الالتزام بصياغة **C# 5** داخل `ArabicRuntimeInjector.cs` لضمان نجاح الترجمة التلقائية عبر `csc.exe` المدمج في `.NET Framework 4.x` في ويندوز بدون الحاجة إلى تثبيت أدوات SDK إضافية على جهاز المستخدم.
- **2026-11-16:** دعم ألعاب **Unity IL2CPP** عبر قراءة `global-metadata.dat` ثنائياً (`0xFAB11BAF`) لاقتناص أدلة اللغة ومكتبات النصوص دون فك شيفرة خارجي، وترجمة `ArabicRuntimeInjector.cs` بوضع `IL2CPP_BACKEND` (اعتماداً على مكتبات النظام الأساسية فقط `mscorlib`/`System`/`System.Core` دون الحاجة لمجلد `Managed`) ونشره حسب حالة اللعبة (`BepInEx/plugins` أو `Mods` في MelonLoader أو Doorstop v4.6.0 `[Il2Cpp]` CoreCLR).
- **2026-11-16:** في ألعاب Unity 2020+ تحت Doorstop، تُستدعى `Doorstop.Entrypoint.Start()` أثناء `mono_jit_init_version` قبل تسجيل `UnityPlayer.dll` لدوال `mono_add_internal_call`؛ لذا يجب عدم لمس أي نوع من `UnityEngine` داخل `Start()` مباشرة، بل تأجيله عبر `DeferredMonoBootstrap` (بدالة `[MethodImpl(MethodImplOptions.NoInlining)]`) حتى يتم تحميل `Assembly-CSharp` بعد `MonoManager ReloadAssembly` ثم تسجيل `Camera.onPreRender`.
- **2026-11-16:** تعزيز عمومية محرك Unity عبر الاكتشاف الداخلي لملفات `.bytes`/`.txt`، وفحص `globalgamemanagers.assets`، واستخراج جداول `StringTable:binary`، وتعدد بدائل شادر SDF وخط `TextMesh`/`TMP_Text`، مع ربط `BackupManager.get_backup_summary` بالواجهة لتنبيه المستخدم وتخييره بين `Rollback` أو إعادة المحاولة عند اختيار مجلد سبق تعريبه حتى بعد إغلاق البرنامج.
- **2026-11-16:** توسيع دعم Unity 6 / IL2CPP المضغوط (التحقق العملي على *Paws Menu v0.1.1*): دعم ترويسة `global-metadata.dat` ثلاثية الحقول (v31+/v108)، وفك ضغط حزم `UnityFS` (`data.unity3d` بـ LZ4 نقي) واستخراج نصوص الواجهة والمشاهد `SceneText:binary`، وبناء `csc.exe` داخل مجلد مؤقت بنفس اسم الـ DLL النهائي لضمان مطابقة اسم التجميع عند تحميله بـ Doorstop CoreCLR، وقصر استدعاء `Resources.FindObjectsOfTypeAll` في `Il2CppNativeApi` على الخيط الرئيسي (`isMainThread == true`) لمنع `0xC0000005` عند التشغيل بدون Mod Loader.
- **2026-11-16:** حل مشكلة الخيط الرئيسي في IL2CPP بدون Mod Loader عبر خطاف `MethodInfo->methodPointer` (`Canvas.SendWillRenderCanvases` / `InvokeOnBeforeRender` / `FireOnPreRender`) مع خطاف `WndProc` احتياطي، وبناء `ArabicFallbackSDF` عبر `TMP_FontAsset.CreateFontAsset` متعددة المسارات (7/3/1 معاملات لتجاوز تجريد `CreateDynamicFontFromOSFont` في Unity 6)، بالإضافة لدعم `GZip`/`Zlib`/`Deflate`/`Base64` في `.bytes`/`TextAsset`، وجداول `UnityEngine.Localization.Tables.StringTable`، وحزم Addressables (`.bundle` في `StreamingAssets/aa/`).
- **2026-11-17:** إضافة محوّل محرك **Godot (3.x / 4.x)** مع محلل `.pck` (`GDPC` v1/v2 مستقل ومدمج في `.exe`) وموارد `.translation` (`RSRC`) دون تعديل الحزم الأصلية؛ اعتماد `arabic_l10n/` + `override.cfg` (`locale/test="ar"`, `locale/translations`, `gui/theme/custom_font`) + `arabic_patch.pck` التكميلية، مع فصل التشبيك (`requires_arabic_shaping=True` في Godot 3 مع `arabic_font.tres` مقابل `False` في Godot 4).
- **2026-11-17:** إضافة محوّل محرك **Unreal Engine (UE4 / UE5)** مع قارئ/كاتب `.locres` (v0–v3) ومحلل/باني `.pak` (v1–v11) ونشر الترجمة المعبأة عبر حزمة `Content/Paks/~mods/z_ArabicL10n_P.pak` دون المساس ببصمة `.pak` الأصلية؛ مع الإبقاء على `requires_arabic_shaping=False` لدعم HarfBuzz/ICU الأصلي وتوثيق اعتماد الألعاب المجمّعة على أصول `UFontFace` (`.uasset`/`.uexp`) عند غياب خط Slate عربي احتياطي.
- **2026-11-17:** ترقية شاملة لمعالجة الحوار واللغات والخطوط: إضافة JS في RPG Maker لحل مشكلة الرسم حرفاً بحرف (`processNormalCharacter`/`canBreakHere`)، فك ضغط SMAZ قياسياً لـ `OptimizedTranslation` ودمج ثيمات `.tres` في Godot، فحص ترويسة `.utoc` وأصول `UStringTable`/`UFontFace` وتقرير جاهزية الخط في Unreal، ودعم YAML وتعدد ملفات مجلد اللغة الإنجليزية في Unity وترجمة `arabic_captured_texts.txt` (`64` اختباراً ناجحاً).
- **2026-11-19:** ترقية ذكاء وسرعة خوارزمية اكتشاف Unity عبر جعل **الفحص العميق الموجَّه بالفهارس (`deep_scan=True`)** هو الوضع الافتراضي (مع إمكانية إيقافه من الواجهة أو عبر `--fast-scan` في CLI): قراءة فهرس `SerializedFile` بالـ `seek` لتجاهل ملفات الوسائط فوراً واستخراج `TextAsset`/`MonoBehaviour`/`TextMesh` بدقة، فك `Addressables catalog.json` لتخطي حزم الصور والصوت، شمول `level*` ودمج عدة ملفات أصول ومنع تكرار ترجمة النصوص المماثلة للملفات الخارجية، وإضافة احتياطي `unity_code` غير المتلف لـ `ArabicOverride.json` (`75` اختباراً ناجحاً).
- **2026-11-19:** تطبيق حماية انقطاع الكهرباء بنظام **الالتزام الذري على مرحلتين (`Zero-Touch Two-Phase Commit`) + سجل `WAL` بـ `os.fsync` بعد كل دفعة**: لا يُمس أي ملف في اللعبة ولا يُنشأ `.bak` خلال الترجمة أو التجهيز داخل `.arabic_translator/staging/`، وتُحفظ كل دفعة فورياً في `translation_cache.json.wal.jsonl` مع `session_state.json`، وفي حال انقطاع نادر أثناء الالتزام النهائي (`status == "committing"`) تُستعاد اللعبة تلقائياً للأصل من دون خسارة التقدم (`80` اختباراً ناجحاً).
- **2026-11-19:** التحقق على لعبة UGUI حقيقية (*Everlight v1.2* — Unity 2018.1.0f2) وإضافة استخراج `UGUI:Text` ثنائياً من `MonoBehaviour` (`ClassID == 114`) بما يشمل الكلمات المفردة والنصوص متعددة الأسطر `\n` ومشاهد النص الواحد (`level1`/`level2`)، مع تنقية `unity_code` من الشيدرات/الأزرار/تحذيرات المحرك واستثناء `arabic_captured_texts.txt` وسجلات التشغيل من إعادة الفحص وتوحيد مطابقة `\r\n`/`\n` في الحاقن (`84` اختباراً ناجحاً).
- **2026-11-19:** تحصين معماري شامل لـ Unity: حل الاستيراد الدائري في `formats/registry.py` عبر التسجيل الكسول، وربط `m_Externals` و`MonoScript (ClassID 115)` لاستبعاد كائنات `TMP_SpriteAsset`/`UI.Image` التقنية تلقائياً، وإضافة مستخرج `TMP:Text` مخصص يدعم Unity 2018–Unity 6 ومشاهد النص الواحد وتعايشه مع `MonoBehaviour:dialogue`، ومنع سلاسل `AssemblyQualifiedName` مثل `*, Assembly-CSharp` دون المساس بالحوارات، مع إضافة Anti-Typewriter debounce وسجل `[Diag]` وقت التشغيل (`88` اختباراً ناجحاً).
- **2026-11-19:** حل ثغرات الألعاب ذات النصوص الخارجية السطرية بامتداد `.json` وترميز UTF-16 ومجلدات تقسيم النصوص (*Helltaker*): فصل `TxtFormatHandler`، وتصحيح تمييز CSV البنيوي في `_detect_payload_format`، وكشف المحتوى الفعلي لملفات `.json`، واختيار كامل مجلدات التقسيم غير الشقيقة في `UnityAdapter.select_language_files`، وتشغيل تبديل خط UGUI/TextMesh للنصوص المعرّبة مسبقاً حتى بدون `ArabicOverride.json` (`92` اختباراً ناجحاً).
- **2026-11-19:** حل مشكلة التعريب الجزئي في الألعاب التي تبدأ فيها الملفات النصية بامتداد `.json` بأسماء متحدثين أو ترويسات بين أقواس مربعة `[ Zmora ]` (*Awaria*): إضافة التحقق الدلالي `_looks_like_json_payload()` في `detect_candidate_format()` لمنع تصنيف `1.json`..`13.json` خطأً كـ JSON مع احتساب السطور العربية عند إعادة الفحص، وتحييد تجاهل أقواس `[...]` كأقسام INI داخل `TxtFormatHandler` عندما يكون الملف نصياً حراً لا يحتوي على `key=value` مع الإبقاء على أقسام INI للملفات المهيكلة (`95` اختباراً ناجحاً).
- **2026-11-20:** دعم ألعاب **Unity 4.x** وواجهة **Legacy IMGUI (`OnGUI`)** (*The Plan* — Unity 4.5.5p5 x86 Mono): إضافة كشف الميزات في `UnityInjector` (`HAS_RUNTIME_INIT_LOAD` و`HAS_IMGUI` مع `UnityEngine.IMGUIModule.dll`)، وتغليف `[RuntimeInitializeOnLoadMethod]` مع ربط `Camera.onPreRender` بالـ Reflection وإطلاق `MonoWndProcBootstrap` كبديل على الخيط الرئيسي، واستخدام الخط المدمج `Arial.ttf`/`LegacyRuntime.ttf` عند غياب `CreateDynamicFontFromOSFont`، وإضافة `GUIText` و`MonoImGuiInterceptor` (خطافات `GUIContent.Temp`/`GUILayout.Do*`/`GUI.Do*` + تبديل خط `GUISkin`/`GUIStyle`) ومطابقة الزخارف الجانبية مثل `▸ START ◂` في `TryTranslateRuntimeText`، وتوسيع `unity_code` لالتقاط كلمات القوائم ونصوص الاعتمادات متعددة الأسطر (`97` اختباراً ناجحاً).
