# LingoForge — أداة التعريب التلقائية للألعاب

**LingoForge** هي أداة تلقائية لتعريب ألعاب الحاسوب إلى اللغة العربية بمجرد تحديد مجلد اللعبة، دون الحاجة إلى تعديل ملفات الأصول الثابتة (`.assets` / `.pak` / `.pck`) أو تثبيت أي مكتبات Python خارجية (`pip`).

تعمل الأداة عبر **واجهة رسومية (`Tkinter`)** و**سطر أوامر (`CLI`)** متكامل، وهي مبنية بمعمارية معيارية قابلة للتوسع (**Plugin / Adapter Architecture**) تفصل بين اكتشاف المحركات، تحليل صيغ الملفات، الترجمة، معالجة العربية، وحقن الخطوط وقت التشغيل.

---

## المحركات المدعومة

| المحرك | النطاق المدعوم | آلية الاستخراج والحقن | معالجة العربية |
| :--- | :--- | :--- | :--- |
| **Unity** | `Mono` و`IL2CPP` (`x64` / `x86`) من **Unity 4.x حتى Unity 6** | استخراج الملفات الخارجية والأصول المعبأة (`.assets`, `level*`, `.bundle`, `UnityFS`, `Addressables`, `TextAsset`, `I2Languages`, `StringTable`, `TMP_Text`, `UGUI`, `TextMesh`، واحتياطي `unity_code`) وحقن الخط والترجمة وقت التشغيل بدون تعديل ملفات `.assets` الأصلية عبر `Unity Doorstop` (`winhttp.dll`) أو `BepInEx` أو `MelonLoader` مع دعم `TextMeshPro` و`UGUI` و`Legacy IMGUI` و`GUIText`. | تشبيك وعكس بصري (`Presentation Forms-B` + `Visual BiDi`) تلقائياً |
| **Unreal Engine** | **UE4 / UE5** | قراءة وكتابة ملفات `.locres` (`v0–v3`)، فحص حزم `.pak` (`v1–v11`) و`IoStore (.utoc)` وأصول `UStringTable` / `UFontFace`، ونشر التعديل غير المتلف عبر `Content/Paks/~mods/z_ArabicL10n_P.pak` مع تحديث `DefaultEngine.ini`. | بدون تشبيك يدوي (يعتمد على دعم `HarfBuzz / ICU` الأصلي في المحرك) |
| **Godot** | **Godot 3.x / 4.x** | فحص ملفات `.pck` المستقلة أو المدمجة داخل `.exe`، فك ضغط `OptimizedTranslation` (`SMAZ`)، ونشر التعريب والخط العربي عبر `arabic_l10n/` و`override.cfg` وحزمة `arabic_patch.pck` مع دمج الثيم المخصص. | تشبيك تلقائي في **Godot 3**؛ وإبقاء النص الطبيعي لدعم RTL الأصلي في **Godot 4** |
| **RPG Maker** | **RPG Maker MV / MZ** | تعريب ملفات البيانات (`data/*.json`)، حقن الخط العربي في `gamefont.css` / `css/game.css`، ونشر إضافة `AutoTranslator_ArabicText.js` في `js/plugins.js` لحل مشكلة رسم الحوار حرفاً بحرف (`word` / `line`). | تشبيك وعكس بصري تلقائياً |

---

## المميزات الرئيسية

- **اكتشاف ذكي لملفات اللغة بدون موديل ذكاء اصطناعي:** تحليل أدلة ملفات `DLL` و`global-metadata.dat`، وتقييم الملفات المرشحة من `0–100`، مع **الفحص العميق الموجَّه بالفهارس** في Unity لتخطي أصول الصور والصوت واستخراج ملفات النصوص بدقة.
- **دعم واسع لصيغ الترجمة:** `JSON / JSONC`, `CSV / TSV`, `XML`, `INI`, `TXT`, `PO`, `YAML`, `.locres`, `.pak`, `.pck`.
- **حماية الرموز والوسوم والمتغيرات:** عزل وحماية `{0}`، `%s`، وسوم `<color=...>` و`[b]...[/b]` و`\c[...]` وصيغ `FText` قبل الترجمة، مع إصلاح تلقائي للرموز المتضررة بعد الترجمة.
- **معالج نصوص عربية مدمج:** ربط الحروف العربية وحالات **لام ألف** في نطاق `Presentation Forms-B` وتطبيق خوارزمية `Visual BiDi`، مع التفعيل أو التعطيل التلقائي حسب دعم المحرك للغة العربية.
- **ترجمة سحابية مجمعة (`Batched POST`):** إرسال دفعات نصية مع التبديل التلقائي بين نقاط النهاية، وتقسيم الدفعة تلقائياً عند التفاف الأسطر، وحفظ كاش محلي (`translation_cache.json`) للاستئناف السريع ومنع تكرار الترجمة.
- **حماية قصوى ضد انقطاع الكهرباء والأعطال:**
  - نظام **الالتزام الذري على مرحلتين بدون لمس ملفات اللعبة (`Zero-Touch Two-Phase Commit`)** وسجل **`WAL`** مع `os.fsync` بعد كل دفعة.
  - نسخ احتياطي آمن بـ `SHA-256` في سجل `backup_manifest.json` مع استرجاع كامل بضغطة واحدة (`Rollback`).
- **خط عربي مدمج وقابل للتخصيص:** يأتي المشروع بخط `IBM Plex Sans Arabic` مدمجاً (`assets/fonts/IBMPlexSansArabic-Regular.ttf`) مع إمكانية تحديث الخط أو تمرير أي خط `.ttf` / `.otf` مخصص.

---

## المتطلبات والتثبيت

- **نظام التشغيل:** Windows (مطلوب لترجمة حاقن Unity C# تلقائياً باستخدام `csc.exe` المدمج في `.NET Framework 4.x`).
- **بيئة Python:** إصدار **Python 3.9+** مع مكتبة **`Tkinter`** (تأتي افتراضياً مع مثبت Python الرسمي على ويندوز).
- **اعتماديات خارجية:** لا توجد أي حزم `pip` مطلوبة؛ يعمل المشروع بمكتبة Python القياسية فقط.

```bash
git clone https://github.com/Mo7ammedHassan/LingoForge.git
cd LingoForge
```

---

## طريقة الاستخدام

### 1. الواجهة الرسومية (`GUI`)

لتشغيل الواجهة الرسومية مباشرة:

```bash
python main.py
```

أو فتح الواجهة مع تحديد مجلد اللعبة مسبقاً:

```bash
python main.py gui "C:\Path\To\Game"
```

### 2. سطر الأوامر (`CLI`)

#### فحص اللعبة بدون تعديل (`scan`)
يفحص المحرك، المعمارية، وأعلى ملفات اللغة المرشحة مع عرض تفاصيل التقييم والتحذيرات:

```bash
python main.py scan "C:\Path\To\Game"
```

- لتعطيل الفحص العميق في Unity والاكتفاء بالملفات الخارجية القوية:
  ```bash
  python main.py scan "C:\Path\To\Game" --fast-scan
  ```
  *(يدعم أيضاً الاسم البديل `--no-deep-scan`)*

#### التعريب والحقن التلقائي الكامل (`translate`)

```bash
python main.py translate "C:\Path\To\Game"
```

**الخيارات المتاحة:**
- `--dry-run`: معاينة الإحصائيات واكتشاف الملفات دون ترجمة أو تعديل أي ملف.
- `--skip-inject`: ترجمة النصوص فقط دون بناء أو حقن الخط.
- `--no-shape`: تعطيل تشبيك الحروف وعكس الاتجاه.
- `--fast-scan` / `--no-deep-scan`: في Unity، تعطيل الفحص العميق عند وجود ملف لغة خارجي قوي.
- `--no-rpgmaker-text-fix`: في RPG Maker، عدم نشر إضافة رسم الحوار العربي متصلاً (`AutoTranslator_ArabicText.js`).
- `--rpgmaker-text-mode {word,line}`: في RPG Maker، تحديد نمط رسم الحوار (`word` كلمةً كلمة افتراضياً، أو `line` للمقطع كاملاً).

#### استرجاع النسخة الأصلية (`rollback`)
يعيد جميع الملفات المعدّلة إلى حالتها الأصلية ويحذف الملفات المضافة أثناء التعريب بناءً على `backup_manifest.json`:

```bash
python main.py rollback "C:\Path\To\Game"
```

#### فحص واستعادة جلسة متوقفة (`recover`)
يفحص حالة الالتزام في `.arabic_translator/` ويستعيد ملفات اللعبة إن حدث انقطاع أثناء مرحلة الكتابة النهائية دون مسح تقدم الترجمة:

```bash
python main.py recover "C:\Path\To\Game"
```

#### تحديث أو تغيير الخط العربي (`update-font`)
يحدث الخط العربي الافتراضي أو يحقن خطاً مخصصاً (`.ttf` / `.otf`) على لعبة معرّبة مسبقاً:

```bash
python main.py update-font "C:\Path\To\Game" --font "C:\Path\To\CustomArabicFont.ttf"
```

*(يدعم أيضاً `--no-rpgmaker-text-fix` و`--rpgmaker-text-mode {word,line}` لـ RPG Maker).*

#### ترجمة النصوص الملتقطة وقت التشغيل (`translate-captured`)
يترجم النصوص التي التقطها الحاقن في ملف `arabic_captured_texts.txt` ويدمجها داخل `ArabicOverride.json`:

```bash
python main.py translate-captured "C:\Path\To\Game"
```

**الخيارات المتاحة:**
- `--skip-inject`: تحديث `ArabicOverride.json` فقط دون إعادة الحقن.
- `--no-shape`: عدم تشبيك الحروف وعكس الاتجاه.

---

## هيكل المشروع

```text
LingoForge/
├── main.py                      # نقطة الدخول الرئيسية (GUI و CLI)
├── MEMORY.md                    # التوثيق الهندسي وسجل القرارات التقنية
├── arabic/                      # حماية المتغيرات والوسوم ومعالج النصوص العربية (التشبيك و BiDi)
├── assets/
│   └── fonts/                   # الخط العربي المدمج (IBM Plex Sans Arabic) وترخيصه OFL.txt
├── core/                        # نماذج البيانات، المنسق العام (Pipeline)، والنسخ الاحتياطي و WAL
├── engines/                     # محوّلات المحركات (Unity, Unreal, Godot, RPG Maker) وسجل المحركات
├── formats/                     # محللات صيغ الملفات (JSON, CSV, XML, INI, TXT, PO, YAML, ...)
├── injectors/                   # حاقنات وقت التشغيل (Unity C# + Doorstop DLLs, RPG Maker JS)
├── translators/                 # محرك الترجمة المجمعة عبر الإنترنت مع الكاش و WAL
├── ui/                          # الواجهة الرسومية (Tkinter)
└── tests/                       # اختبارات الوحدة والتكامل الشاملة
```

---

## تشغيل الاختبارات

للتحقق من عمل جميع وحدات الاكتشاف، المحللات، المعالجة العربية، والنسخ الاحتياطي:

```bash
python -m unittest discover -s tests
```

---

## ملاحظات وتنبيهات

- **أنظمة مكافحة الغش (Anti-Cheat):** الألعاب المحمية بأنظمة مثل `EasyAntiCheat` أو `BattlEye` أو `GameGuard` قد تمنع تحميل `Doorstop / DLL` وقت التشغيل. تصدر الأداة تحذيراً تلقائياً عند اكتشاف هذه الأنظمة.
- **ترخيص الخط المدمج:** الخط المرفق `IBM Plex Sans Arabic` مرخص بموجب رخصة SIL Open Font License المرفقة في [`assets/fonts/OFL.txt`](assets/fonts/OFL.txt).
- **التوثيق التقني المفصّل:** للاطلاع على التفاصيل المعمارية العميقة لكل محرك وتحديات الحقن التي تمت معالجتها، راجع ملف [`MEMORY.md`](MEMORY.md).
