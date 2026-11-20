using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

#if !IL2CPP_BACKEND
using UnityEngine;
#if USE_TMP
using TMPro;
using UnityEngine.TextCore;
using UnityEngine.TextCore.LowLevel;
#endif
#endif

namespace Doorstop
{
    public static class Entrypoint
    {
        public static void Start()
        {
#if IL2CPP_BACKEND
            ArabicTranslatorRuntime.Il2CppPluginBootstrap.Initialize();
#else
            ArabicTranslatorRuntime.DeferredMonoBootstrap.Start();
#endif
        }

        public static void Main(string[] args)
        {
            Start();
        }
    }
}

#if INCLUDE_INPUT_UTILS && !IL2CPP_BACKEND
namespace Platform.Utils
{
    public static class InputUtils
    {
        public static bool anyKey
        {
            get { return Input.anyKey; }
        }

        public static bool anyKeyDown
        {
            get { return Input.anyKeyDown; }
        }

        public static void Initialize()
        {
            ArabicTranslatorRuntime.ArabicFontInjector.EnsureInitialized();
        }

        public static void Update()
        {
            ArabicTranslatorRuntime.ArabicFontInjector.OnUpdate();
        }

        public static bool GetKeyDown(KeyCode keyCode)
        {
            return Input.GetKeyDown(keyCode);
        }

        public static bool GetKeyUp(KeyCode keyCode)
        {
            return Input.GetKeyUp(keyCode);
        }

        public static bool GetKey(KeyCode keyCode)
        {
            return Input.GetKey(keyCode);
        }

        public static float GetAxis(string axisName)
        {
            return Input.GetAxis(axisName);
        }

        public static string GetDebugInfoString()
        {
            return string.Empty;
        }

        public static void SetGamePadVibration(float leftIntensity, float rightIntensity)
        {
        }
    }
}
#endif

namespace ArabicTranslatorRuntime
{
#if !IL2CPP_BACKEND
    internal static class DeferredMonoBootstrap
    {
        private static int _started;

        public static void Start()
        {
            if (Interlocked.Exchange(ref _started, 1) != 0)
            {
                return;
            }
            Thread t = new Thread(WaitForEngineReady);
            t.IsBackground = true;
            t.Start();
        }

        private static void WaitForEngineReady()
        {
            try
            {
                for (int i = 0; i < 300; i++)
                {
                    bool ready = false;
                    Assembly[] assemblies = AppDomain.CurrentDomain.GetAssemblies();
                    for (int a = 0; a < assemblies.Length; a++)
                    {
                        try
                        {
                            string name = assemblies[a].GetName().Name;
                            if (string.Equals(name, "Assembly-CSharp", StringComparison.OrdinalIgnoreCase) ||
                                string.Equals(name, "UnityEngine.UI", StringComparison.OrdinalIgnoreCase) ||
                                string.Equals(name, "Unity.TextMeshPro", StringComparison.OrdinalIgnoreCase))
                            {
                                ready = true;
                                break;
                            }
                        }
                        catch
                        {
                        }
                    }

                    if (ready)
                    {
                        Thread.Sleep(400);
                        ActivateMainThreadHooks();
                        return;
                    }
                    Thread.Sleep(100);
                }
            }
            catch
            {
            }
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        private static void ActivateMainThreadHooks()
        {
            ArabicFontInjector.RegisterMainThreadHooks();
        }
    }

    internal static class MonoWndProcBootstrap
    {
        private const uint WM_ARABIC_MONO_TICK = 0x8418u;

        [DllImport("kernel32.dll")]
        private static extern uint GetCurrentProcessId();

        [DllImport("user32.dll")]
        private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

        [DllImport("user32.dll")]
        private static extern bool IsWindowVisible(IntPtr hWnd);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetClassNameW(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);

        [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")]
        private static extern IntPtr SetWindowLongPtr64(IntPtr hWnd, int nIndex, IntPtr dwNewLong);

        [DllImport("user32.dll", EntryPoint = "SetWindowLongW")]
        private static extern IntPtr SetWindowLong32(IntPtr hWnd, int nIndex, IntPtr dwNewLong);

        [DllImport("user32.dll")]
        private static extern IntPtr CallWindowProcW(IntPtr lpPrevWndFunc, IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern bool PostMessageW(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

        private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate IntPtr WndProcDelegate(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

        private static int _started;
        private static int _inTick;
        private static int _unhooked;
        private static Action _tickCallback;
        private static Action<string> _log;
        private static WndProcDelegate _pinnedWndProc;
        private static IntPtr _hwnd;
        private static IntPtr _origWndProc;

        public static void EnsureStarted(Action tickCallback, Action<string> log)
        {
            _tickCallback = tickCallback;
            _log = log;
            if (Interlocked.Exchange(ref _started, 1) != 0)
            {
                return;
            }
            Thread t = new Thread(PumpLoop);
            t.IsBackground = true;
            t.Start();
        }

        private static void PumpLoop()
        {
            try
            {
                for (int i = 0; i < 60; i++)
                {
                    if (ArabicFontInjector.IsRunnerActive)
                    {
                        return;
                    }
                    if (_hwnd == IntPtr.Zero)
                    {
                        TryInstallHook();
                    }
                    if (_hwnd != IntPtr.Zero && _origWndProc != IntPtr.Zero)
                    {
                        PostMessageW(_hwnd, WM_ARABIC_MONO_TICK, IntPtr.Zero, IntPtr.Zero);
                    }
                    Thread.Sleep(250);
                }
            }
            catch
            {
            }
        }

        private static void TryInstallHook()
        {
            IntPtr hwnd = FindUnityMainWindow();
            if (hwnd == IntPtr.Zero)
            {
                return;
            }
            if (_pinnedWndProc == null)
            {
                _pinnedWndProc = new WndProcDelegate(HookedWndProc);
            }
            IntPtr hookPtr = Marshal.GetFunctionPointerForDelegate(_pinnedWndProc);
            IntPtr prev = IntPtr.Size == 8
                ? SetWindowLongPtr64(hwnd, -4, hookPtr)
                : SetWindowLong32(hwnd, -4, hookPtr);
            if (prev == IntPtr.Zero)
            {
                return;
            }
            _hwnd = hwnd;
            _origWndProc = prev;
            if (_log != null)
            {
                _log("Installed Unity 4.x Main-Thread WndProc bootstrap hook.");
            }
        }

        private static IntPtr FindUnityMainWindow()
        {
            uint pid = GetCurrentProcessId();
            IntPtr unityHwnd = IntPtr.Zero;
            IntPtr anyHwnd = IntPtr.Zero;
            EnumWindows(delegate(IntPtr hWnd, IntPtr lParam)
            {
                uint winPid;
                GetWindowThreadProcessId(hWnd, out winPid);
                if (winPid != pid || !IsWindowVisible(hWnd))
                {
                    return true;
                }
                StringBuilder sb = new StringBuilder(128);
                GetClassNameW(hWnd, sb, sb.Capacity);
                string cls = sb.ToString();
                if (string.Equals(cls, "UnityWndClass", StringComparison.Ordinal))
                {
                    unityHwnd = hWnd;
                    return false;
                }
                if (anyHwnd == IntPtr.Zero)
                {
                    anyHwnd = hWnd;
                }
                return true;
            }, IntPtr.Zero);
            return unityHwnd != IntPtr.Zero ? unityHwnd : anyHwnd;
        }

        private static IntPtr HookedWndProc(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam)
        {
            if (msg == WM_ARABIC_MONO_TICK)
            {
                if (Interlocked.CompareExchange(ref _inTick, 1, 0) == 0)
                {
                    try
                    {
                        if (_tickCallback != null)
                        {
                            _tickCallback();
                        }
                        if (ArabicFontInjector.IsRunnerActive && _origWndProc != IntPtr.Zero && Interlocked.Exchange(ref _unhooked, 1) == 0)
                        {
                            if (IntPtr.Size == 8)
                            {
                                SetWindowLongPtr64(_hwnd, -4, _origWndProc);
                            }
                            else
                            {
                                SetWindowLong32(_hwnd, -4, _origWndProc);
                            }
                            if (_log != null)
                            {
                                _log("Unity 4.x runner initialized via WndProc hook; restored original WndProc.");
                            }
                        }
                    }
                    catch
                    {
                    }
                    finally
                    {
                        Interlocked.Exchange(ref _inTick, 0);
                    }
                }
                return IntPtr.Zero;
            }
            return CallWindowProcW(_origWndProc, hWnd, msg, wParam, lParam);
        }
    }

    internal sealed class ArabicInjectorBehaviour : MonoBehaviour
    {
        private void Update()
        {
            ArabicFontInjector.MarkBehaviourActive();
            ArabicFontInjector.OnUpdate();
        }
    }

    internal static class ArabicFontInjector
    {
        private static int _frameCounter;
        private static int _overrideTickCounter;
        private static bool _runnerCreated;
        private static ArabicInjectorBehaviour _runnerInstance;
        private static bool _mainThreadHooksRegistered;
        private static int _lastHookFrame = -1;
        private static int _lastBehaviourFrame = -1;

        private static readonly OverrideTranslationStore _store = new OverrideTranslationStore();
        private static Type _i2LocManagerType;
        private static bool _i2Searched;
        private static Type _uguiTextType;
        private static bool _uguiSearched;
        private static Type _textMeshType;
        private static bool _textMeshSearched;
        private static Type _guiTextType;
        private static bool _guiTextSearched;
        private static Font _uguiArabicOsFont;
        private static MethodInfo _requestCharsMethod;
        private static bool _requestCharsSearched;

        public static bool IsRunnerActive
        {
            get { return _runnerCreated && _runnerInstance != null; }
        }

#if USE_TMP
        private static int _attemptCount;
        private static Type _languageSdfType;
        private static string _lastCachedLanguage;
        private static TMP_FontAsset _arabicFont;
#else
        private static Font _fallbackOsFont;
#endif

        private static string GetLogPath()
        {
            try
            {
                string root = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
                return Path.Combine(root, "arabic_font_log.txt");
            }
            catch
            {
                return "arabic_font_log.txt";
            }
        }

        private static void Log(string message)
        {
            try
            {
                File.AppendAllText(
                    GetLogPath(),
                    "[" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "] " + message + Environment.NewLine
                );
            }
            catch
            {
            }
        }

#if HAS_RUNTIME_INIT_LOAD
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void OnAfterSceneLoad()
        {
            RegisterMainThreadHooks();
            EnsureRunner();
            EnsureInitialized();
        }
#endif

        public static void MarkBehaviourActive()
        {
            try
            {
                _lastBehaviourFrame = Time.frameCount;
            }
            catch
            {
            }
        }

        public static void RegisterMainThreadHooks()
        {
            if (_mainThreadHooksRegistered)
            {
                return;
            }
            _mainThreadHooksRegistered = true;

            bool hasRenderEventHook = false;
            try
            {
                MethodInfo camCb = typeof(ArabicFontInjector).GetMethod(
                    "OnCameraPreRender",
                    BindingFlags.NonPublic | BindingFlags.Static
                );
                if (camCb != null)
                {
                    EventInfo preRenderEv = typeof(Camera).GetEvent(
                        "onPreRender",
                        BindingFlags.Public | BindingFlags.Static
                    );
                    if (preRenderEv != null && preRenderEv.EventHandlerType != null)
                    {
                        Delegate del = Delegate.CreateDelegate(preRenderEv.EventHandlerType, camCb, false);
                        if (del != null)
                        {
                            preRenderEv.AddEventHandler(null, del);
                            hasRenderEventHook = true;
                        }
                    }
                    else
                    {
                        FieldInfo preRenderFi = typeof(Camera).GetField(
                            "onPreRender",
                            BindingFlags.Public | BindingFlags.Static
                        );
                        if (preRenderFi != null && preRenderFi.FieldType != null)
                        {
                            Delegate del = Delegate.CreateDelegate(preRenderFi.FieldType, camCb, false);
                            if (del != null)
                            {
                                Delegate existing = preRenderFi.GetValue(null) as Delegate;
                                preRenderFi.SetValue(null, Delegate.Combine(existing, del));
                                hasRenderEventHook = true;
                            }
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Log("WARNING: Failed to register Camera.onPreRender hook: " + ex.Message);
            }

            try
            {
                EventInfo beforeRenderEv = typeof(Application).GetEvent(
                    "onBeforeRender",
                    BindingFlags.Public | BindingFlags.Static
                );
                if (beforeRenderEv != null && beforeRenderEv.EventHandlerType != null)
                {
                    MethodInfo cb = typeof(ArabicFontInjector).GetMethod(
                        "OnBeforeRenderFallback",
                        BindingFlags.NonPublic | BindingFlags.Static
                    );
                    if (cb != null)
                    {
                        Delegate del = Delegate.CreateDelegate(beforeRenderEv.EventHandlerType, cb, false);
                        if (del != null)
                        {
                            beforeRenderEv.AddEventHandler(null, del);
                            hasRenderEventHook = true;
                        }
                    }
                }
            }
            catch
            {
                // قد لا يتوفر Application.onBeforeRender في إصدارات Unity القديمة جداً
            }

            if (!hasRenderEventHook)
            {
                MonoWndProcBootstrap.EnsureStarted(OnMainThreadRenderTick, Log);
            }
        }

        private static void OnCameraPreRender(Camera cam)
        {
            OnMainThreadRenderTick();
        }

        private static void OnBeforeRenderFallback()
        {
            OnMainThreadRenderTick();
        }

        private static void OnMainThreadRenderTick()
        {
            try
            {
                int frame = Time.frameCount;
                if (frame == _lastHookFrame)
                {
                    return;
                }
                _lastHookFrame = frame;

                EnsureRunner();
#if USE_TMP
                if (_arabicFont == null)
                {
                    EnsureInitialized();
                }
#else
                if (_fallbackOsFont == null)
                {
                    EnsureInitialized();
                }
#endif
                if (_lastBehaviourFrame < 0 || (frame - _lastBehaviourFrame) > 2)
                {
                    OnUpdate();
                }
            }
            catch
            {
            }
        }

        private static void EnsureRunner()
        {
            if (_runnerCreated && _runnerInstance != null)
            {
                return;
            }
            try
            {
                GameObject go = new GameObject("[ArabicFontInjectorRunner]");
                go.hideFlags = HideFlags.HideAndDontSave;
                UnityEngine.Object.DontDestroyOnLoad(go);
                _runnerInstance = go.AddComponent<ArabicInjectorBehaviour>();
                _runnerCreated = true;
            }
            catch
            {
                // قد يُستدعى من Doorstop قبل جهوزية Unity Scene؛ سيتكفل OnCameraPreRender أو OnAfterSceneLoad به
            }
        }

        public static void EnsureInitialized()
        {
            EnsureRunner();
            EnsureOverridesLoaded();
#if USE_TMP
            if (_arabicFont == null && _attemptCount < 10)
            {
                _attemptCount++;
                try
                {
                    Log("Initializing ArabicFallbackSDF (attempt " + _attemptCount + ")...");
                    _arabicFont = BuildArabicFontAsset();
                    if (_arabicFont != null)
                    {
                        Log("ArabicFallbackSDF built successfully. Glyphs=" + _arabicFont.glyphTable.Count + ", Chars=" + _arabicFont.characterTable.Count);
                        ProtectFromLanguageSDF();
                    }
                    else
                    {
                        Log("WARNING: BuildArabicFontAsset returned null on attempt " + _attemptCount + ".");
                    }
                }
                catch (Exception ex)
                {
                    Log("EXCEPTION in EnsureInitialized: " + ex);
                }
            }
            AttachFallback();
#else
            if (_fallbackOsFont == null)
            {
                _fallbackOsFont = GetUguiArabicOsFont();
            }
#endif
#if HAS_IMGUI
            if (_store.HasOverrides)
            {
                MonoImGuiInterceptor.EnsureInstalled(_store, GetUguiArabicOsFont, Log);
            }
#endif
            if (_store.HasOverrides)
            {
                ApplyI2LocalizationOverrides();
            }
            ApplyActiveUiTextOverrides();
        }

        public static void OnUpdate()
        {
            _frameCounter++;
            _overrideTickCounter++;
            if (!_store.LoadAttempted)
            {
                EnsureOverridesLoaded();
            }
#if HAS_IMGUI
            if (_store.HasOverrides)
            {
                MonoImGuiInterceptor.EnsureInstalled(_store, GetUguiArabicOsFont, Log);
            }
#endif
            if (_overrideTickCounter >= 15)
            {
                _overrideTickCounter = 0;
                if (_store.HasOverrides)
                {
                    ApplyI2LocalizationOverrides();
                }
                ApplyActiveUiTextOverrides();
            }

#if USE_TMP
            if (_arabicFont == null)
            {
                if (_attemptCount < 10 && _frameCounter >= 30)
                {
                    _frameCounter = 0;
                    EnsureInitialized();
                }
                return;
            }

            if (_frameCounter >= 30)
            {
                _frameCounter = 0;
                bool wiped = _arabicFont.characterTable == null ||
                             _arabicFont.characterTable.Count == 0 ||
                             _arabicFont.glyphTable == null ||
                             _arabicFont.glyphTable.Count == 0;
                if (wiped)
                {
                    Log("Detected ArabicFallbackSDF was cleared; rebuilding...");
                    RebuildAfterWipe();
                }
                else
                {
                    ProtectFromLanguageSDF();
                    string currentLang = GetLanguageSDFCachedLanguage();
                    bool langChanged = !string.IsNullOrEmpty(currentLang) &&
                                       !string.Equals(_lastCachedLanguage, currentLang, StringComparison.Ordinal);
                    if (!string.IsNullOrEmpty(currentLang))
                    {
                        _lastCachedLanguage = currentLang;
                    }
                    if (langChanged)
                    {
                        Log("Detected LanguageSDF initialization (" + currentLang + "); refreshing TMP_Text.");
                    }
                    AttachFallback(langChanged);
                }
            }
#endif
        }

#if USE_TMP
        private static void RebuildAfterWipe()
        {
            try
            {
                TMP_FontAsset oldFont = _arabicFont;
                _arabicFont = null;

                if (oldFont != null)
                {
                    List<TMP_FontAsset> globalFallbacks = TMP_Settings.fallbackFontAssets;
                    if (globalFallbacks != null)
                    {
                        globalFallbacks.Remove(oldFont);
                    }
                    UnityEngine.Object.Destroy(oldFont);
                }

                _arabicFont = BuildArabicFontAsset();
                if (_arabicFont != null)
                {
                    Log("ArabicFallbackSDF rebuilt after wipe. Glyphs=" + _arabicFont.glyphTable.Count + ", Chars=" + _arabicFont.characterTable.Count);
                    ProtectFromLanguageSDF();
                    AttachFallback(true);
                }
            }
            catch (Exception ex)
            {
                Log("EXCEPTION in RebuildAfterWipe: " + ex);
            }
        }

        private static Type GetLanguageSDFType()
        {
            if (_languageSdfType != null)
            {
                return _languageSdfType;
            }

            _languageSdfType = Type.GetType("LanguageSDF, Assembly-CSharp", false);
            if (_languageSdfType != null)
            {
                return _languageSdfType;
            }

            Assembly[] assemblies = AppDomain.CurrentDomain.GetAssemblies();
            for (int i = 0; i < assemblies.Length; i++)
            {
                Type t = assemblies[i].GetType("LanguageSDF", false);
                if (t != null)
                {
                    _languageSdfType = t;
                    return t;
                }
            }
            return null;
        }

        private static void ProtectFromLanguageSDF()
        {
            if (_arabicFont == null)
            {
                return;
            }
            try
            {
                Type langSdf = GetLanguageSDFType();
                if (langSdf == null)
                {
                    return;
                }
                FieldInfo clonesFi = langSdf.GetField("clones", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                if (clonesFi != null)
                {
                    Dictionary<TMP_FontAsset, TMP_FontAsset> clones = clonesFi.GetValue(null) as Dictionary<TMP_FontAsset, TMP_FontAsset>;
                    if (clones != null && !clones.ContainsKey(_arabicFont))
                    {
                        clones[_arabicFont] = _arabicFont;
                    }
                }
            }
            catch (Exception ex)
            {
                Log("WARNING in ProtectFromLanguageSDF: " + ex.Message);
            }
        }

        private static string GetLanguageSDFCachedLanguage()
        {
            try
            {
                Type langSdf = GetLanguageSDFType();
                if (langSdf == null)
                {
                    return null;
                }
                FieldInfo fi = langSdf.GetField("cachedLanguage", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                if (fi != null)
                {
                    return fi.GetValue(null) as string;
                }
            }
            catch
            {
            }
            return null;
        }

        private static void ClearAllFontLookupCaches()
        {
            FieldInfo searchedStaticFi = typeof(TMP_FontAsset).GetField("k_SearchedFontAssetLookup", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
            if (searchedStaticFi != null)
            {
                HashSet<int> hs = searchedStaticFi.GetValue(null) as HashSet<int>;
                if (hs != null) hs.Clear();
            }

            FieldInfo fallbackLookupFi = typeof(TMP_FontAsset).GetField("FallbackSearchQueryLookup", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            FieldInfo missingUnicodesFi = typeof(TMP_FontAsset).GetField("m_MissingUnicodesFromFontFile", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);

            TMP_FontAsset[] allFonts = Resources.FindObjectsOfTypeAll<TMP_FontAsset>();
            for (int i = 0; i < allFonts.Length; i++)
            {
                TMP_FontAsset f = allFonts[i];
                if (f == null) continue;
                if (fallbackLookupFi != null)
                {
                    HashSet<int> fl = fallbackLookupFi.GetValue(f) as HashSet<int>;
                    if (fl != null) fl.Clear();
                }
                if (missingUnicodesFi != null)
                {
                    HashSet<uint> mu = missingUnicodesFi.GetValue(f) as HashSet<uint>;
                    if (mu != null) mu.Clear();
                }
            }
        }

        private static List<uint> GetTargetCodepoints()
        {
            return OverrideTranslationStore.GetArabicTargetCodepoints();
        }

        private static string FindSystemArabicFontPath()
        {
            string bundled = OverrideTranslationStore.FindAndRegisterBundledArabicFont(Log);
            if (!string.IsNullOrEmpty(bundled) && File.Exists(bundled))
            {
                return bundled;
            }
            string fontsDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "Fonts");
            string[] candidates = new string[]
            {
                Path.Combine(fontsDir, "arial.ttf"),
                Path.Combine(fontsDir, "tahoma.ttf"),
                Path.Combine(fontsDir, "arialbd.ttf"),
                @"C:\Windows\Fonts\arial.ttf",
                @"C:\Windows\Fonts\tahoma.ttf"
            };
            for (int i = 0; i < candidates.Length; i++)
            {
                if (File.Exists(candidates[i]))
                {
                    return candidates[i];
                }
            }
            return null;
        }

        private static TMP_FontAsset BuildArabicFontAsset()
        {
            List<uint> codepoints = GetTargetCodepoints();
            string bundledPath = OverrideTranslationStore.FindAndRegisterBundledArabicFont(Log);

            try
            {
                string[] osNames = !string.IsNullOrEmpty(bundledPath)
                    ? new string[] { "IBM Plex Sans Arabic" }
                    : new string[] { "Arial", "Tahoma" };
                Font osFont = Font.CreateDynamicFontFromOSFont(osNames, 48);
                if (osFont != null)
                {
                    TMP_FontAsset dyn = TMP_FontAsset.CreateFontAsset(osFont, 48, 5, GlyphRenderMode.SDFAA, 2048, 2048, AtlasPopulationMode.Dynamic, false);
                    if (dyn != null)
                    {
                        dyn.name = "ArabicFallbackSDF";
                        uint[] missing;
                        dyn.TryAddCharacters(codepoints.ToArray(), out missing, false);
                        if (dyn.characterLookupTable != null &&
                            dyn.characterLookupTable.ContainsKey(0xFE8E) &&
                            dyn.characterLookupTable.ContainsKey(0x0627))
                        {
                            dyn.hideFlags = HideFlags.DontSave;
                            if (dyn.atlasTexture != null) dyn.atlasTexture.hideFlags = HideFlags.DontSave;
                            if (dyn.material != null) dyn.material.hideFlags = HideFlags.DontSave;
                            UnityEngine.Object.DontDestroyOnLoad(dyn);
                            Log("Built ArabicFallbackSDF via OS dynamic font.");
                            return dyn;
                        }
                        UnityEngine.Object.Destroy(dyn);
                    }
                }
            }
            catch (Exception ex)
            {
                Log("OS dynamic font path skipped: " + ex.Message);
            }

            string fontPath = FindSystemArabicFontPath();
            if (string.IsNullOrEmpty(fontPath))
            {
                Log("ERROR: Could not find arial.ttf or tahoma.ttf.");
                return null;
            }
            Log("Loading font face directly from: " + fontPath);

            const int pointSize = 48;
            const int padding = 5;
            const int atlasW = 2048;
            const int atlasH = 2048;

            FontEngineError initErr = FontEngine.InitializeFontEngine();
            Log("FontEngine.InitializeFontEngine => " + initErr);

            FontEngineError loadErr = FontEngine.LoadFontFace(fontPath, pointSize);
            Log("FontEngine.LoadFontFace => " + loadErr);
            if (loadErr != FontEngineError.Success)
            {
                byte[] fontBytes = File.ReadAllBytes(fontPath);
                loadErr = FontEngine.LoadFontFace(fontBytes, pointSize);
                Log("FontEngine.LoadFontFace(bytes) => " + loadErr);
                if (loadErr != FontEngineError.Success)
                {
                    return null;
                }
            }

            MethodInfo tryAddGlyphMethod = typeof(FontEngine).GetMethod(
                "TryAddGlyphToTexture",
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static
            );
            if (tryAddGlyphMethod == null)
            {
                Log("ERROR: FontEngine.TryAddGlyphToTexture method not found.");
                return null;
            }

            MethodInfo resetAtlasMethod = typeof(FontEngine).GetMethod(
                "ResetAtlasTexture",
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static
            );

            Shader sdfShader = GetMobileSDFShader();
            if (sdfShader == null)
            {
                Log("ShaderRef_MobileSDF not ready yet; deferring.");
                return null;
            }

            TMP_FontAsset fa = ScriptableObject.CreateInstance<TMP_FontAsset>();
            fa.name = "ArabicFallbackSDF";
            SetField(fa, "m_Version", "1.1.0");
            fa.faceInfo = FontEngine.GetFaceInfo();
            fa.atlasPopulationMode = AtlasPopulationMode.Static;
            Font dummyFont = new Font("Arial");
            dummyFont.hideFlags = HideFlags.DontSave;
            UnityEngine.Object.DontDestroyOnLoad(dummyFont);
            SetField(fa, "m_SourceFontFile", dummyFont);
            SetField(fa, "m_AtlasWidth", atlasW);
            SetField(fa, "m_AtlasHeight", atlasH);
            SetField(fa, "m_AtlasPadding", padding);
            SetField(fa, "m_AtlasRenderMode", GlyphRenderMode.SDFAA);

            Texture2D tex = new Texture2D(atlasW, atlasH, TextureFormat.Alpha8, false);
            tex.name = "ArabicFallbackSDF Atlas";
            if (resetAtlasMethod != null)
            {
                resetAtlasMethod.Invoke(null, new object[] { tex });
            }
            else
            {
                Color32[] blank = new Color32[atlasW * atlasH];
                tex.SetPixels32(blank);
                tex.Apply(false, false);
            }

            fa.atlasTextures = new Texture2D[] { tex };
            fa.isMultiAtlasTexturesEnabled = false;

            List<GlyphRect> freeRects = new List<GlyphRect> { new GlyphRect(0, 0, atlasW - 1, atlasH - 1) };
            List<GlyphRect> usedRects = new List<GlyphRect>();
            SetField(fa, "m_FreeGlyphRects", freeRects);
            SetField(fa, "m_UsedGlyphRects", usedRects);

            Material mat = new Material(sdfShader);
            mat.name = "ArabicFallbackSDF Material";
            mat.SetTexture(ShaderUtilities.ID_MainTex, tex);
            mat.SetFloat(ShaderUtilities.ID_TextureWidth, (float)atlasW);
            mat.SetFloat(ShaderUtilities.ID_TextureHeight, (float)atlasH);
            mat.SetFloat(ShaderUtilities.ID_GradientScale, (float)(padding + 1));
            mat.SetFloat(ShaderUtilities.ID_WeightNormal, fa.normalStyle);
            mat.SetFloat(ShaderUtilities.ID_WeightBold, fa.boldStyle);
            fa.material = mat;

            Dictionary<uint, Glyph> glyphByIndex = new Dictionary<uint, Glyph>();
            List<Glyph> glyphTable = new List<Glyph>();
            List<TMP_Character> charTable = new List<TMP_Character>();

            int packedCount = 0;
            for (int i = 0; i < codepoints.Count; i++)
            {
                uint cp = codepoints[i];
                uint glyphIndex;
                if (!FontEngine.TryGetGlyphIndex(cp, out glyphIndex) || glyphIndex == 0)
                {
                    continue;
                }

                Glyph glyph;
                if (!glyphByIndex.TryGetValue(glyphIndex, out glyph))
                {
                    object[] args = new object[]
                    {
                        glyphIndex,
                        padding,
                        GlyphPackingMode.BestShortSideFit,
                        freeRects,
                        usedRects,
                        GlyphRenderMode.SDFAA,
                        tex,
                        null
                    };
                    bool ok = (bool)tryAddGlyphMethod.Invoke(null, args);
                    if (!ok || args[7] == null)
                    {
                        continue;
                    }
                    glyph = (Glyph)args[7];
                    glyph.atlasIndex = 0;
                    glyphByIndex[glyphIndex] = glyph;
                    glyphTable.Add(glyph);
                    packedCount++;
                }

                TMP_Character ch = new TMP_Character(cp, fa, glyph);
                charTable.Add(ch);
            }

            tex.Apply(false, false);

            SetField(fa, "m_GlyphTable", glyphTable);
            SetField(fa, "m_CharacterTable", charTable);
            fa.ReadFontAssetDefinition();

            tex.hideFlags = HideFlags.DontSave;
            mat.hideFlags = HideFlags.DontSave;
            fa.hideFlags = HideFlags.DontSave;
            UnityEngine.Object.DontDestroyOnLoad(tex);
            UnityEngine.Object.DontDestroyOnLoad(mat);
            UnityEngine.Object.DontDestroyOnLoad(fa);

            Log("Packed unique glyphs=" + packedCount + ", character entries=" + charTable.Count);
            return fa;
        }

        private static void SetField(object target, string fieldName, object value)
        {
            FieldInfo fi = target.GetType().GetField(fieldName, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (fi != null)
            {
                fi.SetValue(target, value);
            }
            else
            {
                Log("WARNING: Field not found: " + fieldName);
            }
        }

        private static Shader GetMobileSDFShader()
        {
            try
            {
                PropertyInfo prop = typeof(ShaderUtilities).GetProperty("ShaderRef_MobileSDF", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                if (prop != null)
                {
                    Shader s = prop.GetValue(null, null) as Shader;
                    if (s != null) return s;
                }
            }
            catch
            {
            }

            Shader found = Shader.Find("TextMeshPro/Mobile/Distance Field");
            if (found != null) return found;

            found = Shader.Find("TextMeshPro/Distance Field");
            if (found != null) return found;

            try
            {
                TMP_FontAsset defFont = RuntimeOverrideReflection.GetStaticMemberValue(
                    typeof(TMP_Settings),
                    "defaultFontAsset"
                ) as TMP_FontAsset;
                if (defFont != null && defFont.material != null && defFont.material.shader != null)
                {
                    return defFont.material.shader;
                }
            }
            catch
            {
            }

            TMP_FontAsset[] existing = Resources.FindObjectsOfTypeAll<TMP_FontAsset>();
            for (int i = 0; i < existing.Length; i++)
            {
                if (existing[i] != null && existing[i].material != null && existing[i].material.shader != null)
                {
                    return existing[i].material.shader;
                }
            }
            return null;
        }

        private static bool EnsureFontAssetHasArabicFallback(TMP_FontAsset f)
        {
            if (f == null || f == _arabicFont || _arabicFont == null)
            {
                return false;
            }
            try
            {
                PropertyInfo tableProp = typeof(TMP_FontAsset).GetProperty(
                    "fallbackFontAssetTable",
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance
                );
                if (tableProp == null || !tableProp.CanRead)
                {
                    return false;
                }
                List<TMP_FontAsset> tbl = tableProp.GetValue(f, null) as List<TMP_FontAsset>;
                if (tbl == null && tableProp.CanWrite)
                {
                    tbl = new List<TMP_FontAsset>();
                    tableProp.SetValue(f, tbl, null);
                }
                if (tbl != null)
                {
                    int idx = tbl.IndexOf(_arabicFont);
                    if (idx != 0)
                    {
                        if (idx > 0)
                        {
                            tbl.RemoveAt(idx);
                        }
                        tbl.Insert(0, _arabicFont);
                        return true;
                    }
                }
            }
            catch
            {
            }
            return false;
        }

        private static void AttachFallback(bool forceRefresh = false)
        {
            if (_arabicFont == null)
            {
                return;
            }

            bool changed = false;
            try
            {
                TMP_Settings settings = TMP_Settings.instance;
                if (settings != null)
                {
                    List<TMP_FontAsset> globalFallbacks = TMP_Settings.fallbackFontAssets;
                    if (globalFallbacks == null)
                    {
                        globalFallbacks = new List<TMP_FontAsset>();
                        SetField(settings, "m_fallbackFontAssets", globalFallbacks);
                    }
                    if (globalFallbacks != null)
                    {
                        int idx = globalFallbacks.IndexOf(_arabicFont);
                        if (idx != 0)
                        {
                            if (idx > 0)
                            {
                                globalFallbacks.RemoveAt(idx);
                            }
                            globalFallbacks.Insert(0, _arabicFont);
                            changed = true;
                            Log("Inserted ArabicFallbackSDF at index 0 of TMP_Settings.fallbackFontAssets.");
                        }
                    }
                }

                if (GetLanguageSDFType() == null)
                {
                    TMP_FontAsset[] allFonts = Resources.FindObjectsOfTypeAll<TMP_FontAsset>();
                    for (int i = 0; i < allFonts.Length; i++)
                    {
                        if (EnsureFontAssetHasArabicFallback(allFonts[i]))
                        {
                            changed = true;
                        }
                    }
                }

                if (changed || forceRefresh)
                {
                    if (_store.HasOverrides)
                    {
                        ApplyActiveUiTextOverrides();
                    }
                    ClearAllFontLookupCaches();
                    TMP_Text[] texts = Resources.FindObjectsOfTypeAll<TMP_Text>();
                    for (int i = 0; i < texts.Length; i++)
                    {
                        if (texts[i] != null && texts[i].gameObject != null && texts[i].gameObject.activeInHierarchy)
                        {
                            texts[i].ForceMeshUpdate(true, true);
                            texts[i].SetAllDirty();
                        }
                    }
                    Log("Refreshed active TMP_Text components (ForceMeshUpdate).");
                }
            }
            catch (Exception ex)
            {
                Log("EXCEPTION in AttachFallback: " + ex);
            }
        }
#endif

        private static void EnsureOverridesLoaded()
        {
            if (_store.LoadAttempted)
            {
                return;
            }

            List<string> candidates = new List<string>();
            try
            {
                if (!string.IsNullOrEmpty(Application.dataPath))
                {
                    candidates.Add(Path.GetFullPath(Path.Combine(Application.dataPath, "..")));
                    candidates.Add(Application.dataPath);
                }
            }
            catch
            {
            }
            try
            {
                if (!string.IsNullOrEmpty(Application.streamingAssetsPath))
                {
                    candidates.Add(Application.streamingAssetsPath);
                }
            }
            catch
            {
            }
            try
            {
                candidates.Add(AppDomain.CurrentDomain.BaseDirectory);
            }
            catch
            {
            }

            _store.EnsureLoaded(candidates.ToArray(), Log);
        }

        private static bool TryTranslateRuntimeText(string current, out string translated)
        {
            return _store.TryTranslateRuntimeText(current, out translated);
        }

        private static void ApplyI2LocalizationOverrides()
        {
            RuntimeOverrideReflection.ApplyI2LocalizationOverrides(_store, ref _i2LocManagerType, ref _i2Searched, Log);
        }

        private static Type GetCachedType(string fullName, string assemblyQualifiedFallback, ref Type cached, ref bool searched)
        {
            if (searched)
            {
                return cached;
            }
            searched = true;
            if (!string.IsNullOrEmpty(assemblyQualifiedFallback))
            {
                cached = Type.GetType(assemblyQualifiedFallback, false);
                if (cached != null)
                {
                    return cached;
                }
            }
            cached = RuntimeOverrideReflection.FindTypeInLoadedAssemblies(fullName);
            return cached;
        }

        private static Type GetUguiTextType()
        {
            return GetCachedType("UnityEngine.UI.Text", "UnityEngine.UI.Text, UnityEngine.UI", ref _uguiTextType, ref _uguiSearched);
        }

        private static Type GetTextMeshType()
        {
            return GetCachedType("UnityEngine.TextMesh", "UnityEngine.TextMesh, UnityEngine.TextRenderingModule", ref _textMeshType, ref _textMeshSearched);
        }

        private static Type GetGuiTextType()
        {
            return GetCachedType("UnityEngine.GUIText", "UnityEngine.GUIText, UnityEngine", ref _guiTextType, ref _guiTextSearched);
        }

        internal static bool FontSupportsArabic(Font f)
        {
            if (f == null)
            {
                return false;
            }
            try
            {
                if (!_requestCharsSearched)
                {
                    _requestCharsSearched = true;
                    _requestCharsMethod = typeof(Font).GetMethod(
                        "RequestCharactersInTexture",
                        BindingFlags.Public | BindingFlags.Instance,
                        null,
                        new Type[] { typeof(string) },
                        null
                    );
                }
                if (_requestCharsMethod != null)
                {
                    _requestCharsMethod.Invoke(f, new object[] { "\uFE8E\uFEEF" });
                }
            }
            catch
            {
            }
            try
            {
                return f.HasCharacter('\uFE8E') && f.HasCharacter('\uFEEF');
            }
            catch
            {
                return false;
            }
        }

        internal static Font GetUguiArabicOsFont()
        {
            if (_uguiArabicOsFont != null)
            {
                return _uguiArabicOsFont;
            }
#if !USE_TMP
            if (_fallbackOsFont != null)
            {
                _uguiArabicOsFont = _fallbackOsFont;
                return _uguiArabicOsFont;
            }
#endif
            try
            {
                OverrideTranslationStore.FindAndRegisterBundledArabicFont(Log);
                MethodInfo m = typeof(Font).GetMethod(
                    "CreateDynamicFontFromOSFont",
                    BindingFlags.Public | BindingFlags.Static,
                    null,
                    new Type[] { typeof(string[]), typeof(int) },
                    null
                );
                if (m != null)
                {
                    _uguiArabicOsFont = m.Invoke(null, new object[] { new string[] { "IBM Plex Sans Arabic", "Arial", "Tahoma" }, 24 }) as Font;
                    if (_uguiArabicOsFont != null)
                    {
                        _uguiArabicOsFont.hideFlags = HideFlags.DontSave;
                        UnityEngine.Object.DontDestroyOnLoad(_uguiArabicOsFont);
                        Log("Initialized UGUI fallback OS font.");
                        return _uguiArabicOsFont;
                    }
                }
            }
            catch
            {
            }

            try
            {
                MethodInfo getBuiltin = typeof(Resources).GetMethod(
                    "GetBuiltinResource",
                    BindingFlags.Public | BindingFlags.Static,
                    null,
                    new Type[] { typeof(Type), typeof(string) },
                    null
                );
                Font builtin = null;
                if (getBuiltin != null)
                {
                    string[] builtinNames = new string[] { "Arial.ttf", "LegacyRuntime.ttf" };
                    for (int i = 0; i < builtinNames.Length; i++)
                    {
                        try
                        {
                            builtin = getBuiltin.Invoke(null, new object[] { typeof(Font), builtinNames[i] }) as Font;
                            if (builtin != null)
                            {
                                break;
                            }
                        }
                        catch
                        {
                        }
                    }
                }
                if (builtin == null)
                {
                    UnityEngine.Object[] loadedFonts = FindAllMonoObjects(typeof(Font));
                    for (int i = 0; i < loadedFonts.Length; i++)
                    {
                        Font candidate = loadedFonts[i] as Font;
                        if (FontSupportsArabic(candidate))
                        {
                            builtin = candidate;
                            break;
                        }
                    }
                }
                if (builtin != null)
                {
                    if (!FontSupportsArabic(builtin))
                    {
                        PropertyInfo fnProp = typeof(Font).GetProperty("fontNames", BindingFlags.Public | BindingFlags.Instance);
                        if (fnProp != null && fnProp.CanWrite)
                        {
                            try
                            {
                                fnProp.SetValue(builtin, new string[] { "Arial", "Tahoma" }, null);
                            }
                            catch
                            {
                            }
                        }
                    }
                    _uguiArabicOsFont = builtin;
                    Log("Initialized Unity 4.x built-in Arabic OS font: " + (_uguiArabicOsFont.name ?? "Arial"));
                }
            }
            catch
            {
            }
            return _uguiArabicOsFont;
        }

        private static UnityEngine.Object[] FindAllMonoObjects(Type compType)
        {
            if (compType == null)
            {
                return new UnityEngine.Object[0];
            }
            UnityEngine.Object[] items = null;
            try
            {
                items = Resources.FindObjectsOfTypeAll(compType);
            }
            catch
            {
            }
            if (items != null && items.Length > 0)
            {
                return items;
            }
            try
            {
                Type apiType = RuntimeOverrideReflection.FindTypeInLoadedAssemblies("UnityEngine.ResourcesAPIInternal");
                if (apiType != null)
                {
                    MethodInfo m = apiType.GetMethod(
                        "FindObjectsOfTypeAll",
                        BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static,
                        null,
                        new Type[] { typeof(Type) },
                        null
                    );
                    if (m != null)
                    {
                        UnityEngine.Object[] fallbackItems = m.Invoke(null, new object[] { compType }) as UnityEngine.Object[];
                        if (fallbackItems != null)
                        {
                            return fallbackItems;
                        }
                    }
                }
            }
            catch
            {
            }
            return items ?? new UnityEngine.Object[0];
        }

        private static string _lastDiagSummary;

        private static void ApplyActiveUiTextOverrides()
        {
            int tmpActive = 0, tmpAr = 0, tmpUnmatched = 0;
#if USE_TMP
            try
            {
                PropertyInfo tmpFontProp = typeof(TMP_Text).GetProperty("font", BindingFlags.Public | BindingFlags.Instance);
                bool allowPerFontFallback = (_arabicFont != null && GetLanguageSDFType() == null && tmpFontProp != null && tmpFontProp.CanRead);
                UnityEngine.Object[] tmpObjs = FindAllMonoObjects(typeof(TMP_Text));
                for (int i = 0; i < tmpObjs.Length; i++)
                {
                    TMP_Text txt = tmpObjs[i] as TMP_Text;
                    if (txt == null || txt.gameObject == null || !txt.gameObject.activeInHierarchy)
                    {
                        continue;
                    }
                    bool updated = false;
                    if (allowPerFontFallback)
                    {
                        TMP_FontAsset compFont = tmpFontProp.GetValue(txt, null) as TMP_FontAsset;
                        if (EnsureFontAssetHasArabicFallback(compFont))
                        {
                            updated = true;
                        }
                    }
                    string curText = txt.text;
                    if (!string.IsNullOrEmpty(curText))
                    {
                        tmpActive++;
                        string translated;
                        if (TryTranslateRuntimeText(curText, out translated))
                        {
                            txt.text = translated;
                            updated = true;
                            tmpAr++;
                        }
                        else if (OverrideTranslationStore.ContainsArabicPresentationForms(curText))
                        {
                            tmpAr++;
                        }
                        else
                        {
                            tmpUnmatched++;
                        }
                    }
                    if (updated)
                    {
                        txt.ForceMeshUpdate(true, true);
                        txt.SetAllDirty();
                    }
                }
            }
            catch (Exception ex)
            {
                Log("WARNING in TMP override loop: " + ex.Message);
            }
#endif

            int uguiActive = 0, uguiAr = 0, uguiUnmatched = 0, uguiSwitched = 0;
            int tmActive = 0, tmAr = 0, tmUnmatched = 0, tmSwitched = 0;
            int gtActive = 0, gtAr = 0, gtUnmatched = 0, gtSwitched = 0;
            ApplyOsFontTextOverrides(GetUguiTextType(), "UGUI", out uguiActive, out uguiAr, out uguiUnmatched, out uguiSwitched);
            ApplyOsFontTextOverrides(GetTextMeshType(), "TextMesh", out tmActive, out tmAr, out tmUnmatched, out tmSwitched);
            ApplyOsFontTextOverrides(GetGuiTextType(), "GUIText", out gtActive, out gtAr, out gtUnmatched, out gtSwitched);

#if HAS_IMGUI
            if (_store.HasOverrides)
            {
                MonoImGuiInterceptor.PatchLoadedGuiSkins(GetUguiArabicOsFont(), Log);
            }
#endif

            if (tmpActive + uguiActive + tmActive + gtActive > 0)
            {
                string summary = "[Diag] TMP(active=" + tmpActive + ", ar=" + tmpAr + ", unmatched=" + tmpUnmatched +
                                 ") UGUI(active=" + uguiActive + ", ar=" + uguiAr + ", unmatched=" + uguiUnmatched + ", fontSwitched=" + uguiSwitched +
                                 ") TextMesh(active=" + tmActive + ", ar=" + tmAr + ", unmatched=" + tmUnmatched + ", fontSwitched=" + tmSwitched + ")";
                if (gtActive > 0)
                {
                    summary += " GUIText(active=" + gtActive + ", ar=" + gtAr + ", unmatched=" + gtUnmatched + ", fontSwitched=" + gtSwitched + ")";
                }
                if (!string.Equals(summary, _lastDiagSummary, StringComparison.Ordinal))
                {
                    _lastDiagSummary = summary;
                    Log(summary);
                }
            }
        }

        private static void ApplyOsFontTextOverrides(
            Type compType,
            string label,
            out int activeCount,
            out int arabicCount,
            out int unmatchedCount,
            out int fontSwitchedCount
        )
        {
            activeCount = 0;
            arabicCount = 0;
            unmatchedCount = 0;
            fontSwitchedCount = 0;
            if (compType == null)
            {
                return;
            }
            try
            {
                PropertyInfo textProp = compType.GetProperty("text", BindingFlags.Public | BindingFlags.Instance);
                PropertyInfo fontProp = compType.GetProperty("font", BindingFlags.Public | BindingFlags.Instance);
                if (textProp == null)
                {
                    return;
                }

                UnityEngine.Object[] items = FindAllMonoObjects(compType);
                for (int i = 0; i < items.Length; i++)
                {
                    Component comp = items[i] as Component;
                    if (comp == null || comp.gameObject == null || !comp.gameObject.activeInHierarchy)
                    {
                        continue;
                    }
                    string curText = textProp.GetValue(comp, null) as string;
                    if (string.IsNullOrEmpty(curText))
                    {
                        continue;
                    }
                    activeCount++;
                    string translated;
                    bool hasArabic = false;
                    if (TryTranslateRuntimeText(curText, out translated))
                    {
                        textProp.SetValue(comp, translated, null);
                        hasArabic = true;
                    }
                    else if (OverrideTranslationStore.ContainsArabicPresentationForms(curText))
                    {
                        hasArabic = true;
                    }
                    else
                    {
                        unmatchedCount++;
                    }

                    if (hasArabic)
                    {
                        arabicCount++;
                        if (fontProp != null)
                        {
                            Font arFont = GetUguiArabicOsFont();
                            if (arFont != null)
                            {
                                Font curFont = fontProp.GetValue(comp, null) as Font;
                                if (!object.ReferenceEquals(curFont, arFont))
                                {
                                    bool needsFallbackFont = (curFont == null) || !FontSupportsArabic(curFont);
                                    if (needsFallbackFont)
                                    {
                                        fontProp.SetValue(comp, arFont, null);
                                        fontSwitchedCount++;
                                    }
                                }
                            }
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Log("WARNING in " + label + " override loop: " + ex.Message);
            }
        }
    }
#if HAS_IMGUI

    internal static class MonoImGuiInterceptor
    {
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool VirtualProtect(IntPtr lpAddress, UIntPtr dwSize, uint flNewProtect, out uint lpflOldProtect);

        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentProcess();

        [DllImport("kernel32.dll")]
        private static extern bool FlushInstructionCache(IntPtr hProcess, IntPtr lpBaseAddress, UIntPtr dwSize);

        private delegate void InternalCallDoLabelDelegate(ref Rect position, GUIContent content, IntPtr style);
        private delegate bool InternalCallDoButtonDelegate(ref Rect position, GUIContent content, IntPtr style);

        private static bool _installAttempted;
        private static OverrideTranslationStore _store;
        private static Func<Font> _fontProvider;
        private static Action<string> _log;
        private static GUIContent _cachedSText;
        private static InternalCallDoLabelDelegate _icallDoLabel;
        private static InternalCallDoButtonDelegate _icallDoButton;
        private static readonly HashSet<long> _patchedAddrs = new HashSet<long>();
        private static readonly HashSet<int> _patchedSkinIds = new HashSet<int>();
        private static readonly Dictionary<string, string> _textCache = new Dictionary<string, string>(StringComparer.Ordinal);

        public static void EnsureInstalled(OverrideTranslationStore store, Func<Font> fontProvider, Action<string> log)
        {
            _store = store;
            _fontProvider = fontProvider;
            _log = log;
            if (_installAttempted || store == null || !store.HasOverrides)
            {
                return;
            }
            _installAttempted = true;

            int hooksInstalled = 0;

            try
            {
                MethodInfo tempMethod = typeof(GUIContent).GetMethod(
                    "Temp",
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static,
                    null,
                    new Type[] { typeof(string) },
                    null
                );
                FieldInfo sTextField = typeof(GUIContent).GetField(
                    "s_Text",
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static
                );
                MethodInfo hookTemp = typeof(MonoImGuiInterceptor).GetMethod(
                    "Hook_GuiContentTemp",
                    BindingFlags.NonPublic | BindingFlags.Static
                );
                if (tempMethod != null && sTextField != null && hookTemp != null)
                {
                    GUIContent tmpInit = tempMethod.Invoke(null, new object[] { string.Empty }) as GUIContent;
                    _cachedSText = (sTextField.GetValue(null) as GUIContent) ?? tmpInit;
                    if (_cachedSText != null && TryPatchStaticMethod(tempMethod, hookTemp, log))
                    {
                        hooksInstalled++;
                    }
                }
            }
            catch (Exception ex)
            {
                if (log != null) log("WARNING: GUIContent.Temp hook skipped: " + ex.Message);
            }

            try
            {
                Type[] glArgs = new Type[] { typeof(GUIContent), typeof(GUIStyle), typeof(GUILayoutOption[]) };
                MethodInfo doBtn = typeof(GUILayout).GetMethod("DoButton", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static, null, glArgs, null);
                MethodInfo hookBtn = typeof(MonoImGuiInterceptor).GetMethod("Hook_GuiLayoutDoButton", BindingFlags.NonPublic | BindingFlags.Static);
                if (doBtn != null && hookBtn != null && TryPatchStaticMethod(doBtn, hookBtn, log))
                {
                    hooksInstalled++;
                }

                MethodInfo doLbl = typeof(GUILayout).GetMethod("DoLabel", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static, null, glArgs, null);
                MethodInfo hookLbl = typeof(MonoImGuiInterceptor).GetMethod("Hook_GuiLayoutDoLabel", BindingFlags.NonPublic | BindingFlags.Static);
                if (doLbl != null && hookLbl != null && TryPatchStaticMethod(doLbl, hookLbl, log))
                {
                    hooksInstalled++;
                }

                MethodInfo doBox = typeof(GUILayout).GetMethod("DoBox", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static, null, glArgs, null);
                MethodInfo hookBox = typeof(MonoImGuiInterceptor).GetMethod("Hook_GuiLayoutDoBox", BindingFlags.NonPublic | BindingFlags.Static);
                if (doBox != null && hookBox != null && TryPatchStaticMethod(doBox, hookBox, log))
                {
                    hooksInstalled++;
                }
            }
            catch (Exception ex)
            {
                if (log != null) log("WARNING: GUILayout hooks skipped: " + ex.Message);
            }

            try
            {
                Type[] guiArgs = new Type[] { typeof(Rect), typeof(GUIContent), typeof(IntPtr) };
                Type[] icallArgs = new Type[] { typeof(Rect).MakeByRefType(), typeof(GUIContent), typeof(IntPtr) };

                MethodInfo doLabel = typeof(GUI).GetMethod("DoLabel", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static, null, guiArgs, null);
                MethodInfo icallLabel = typeof(GUI).GetMethod("INTERNAL_CALL_DoLabel", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static, null, icallArgs, null);
                MethodInfo hookLabel = typeof(MonoImGuiInterceptor).GetMethod("Hook_GuiDoLabel", BindingFlags.NonPublic | BindingFlags.Static);
                if (doLabel != null && icallLabel != null && hookLabel != null)
                {
                    _icallDoLabel = Delegate.CreateDelegate(typeof(InternalCallDoLabelDelegate), icallLabel, false) as InternalCallDoLabelDelegate;
                    if (_icallDoLabel != null && TryPatchStaticMethod(doLabel, hookLabel, log))
                    {
                        hooksInstalled++;
                    }
                }

                MethodInfo doButton = typeof(GUI).GetMethod("DoButton", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static, null, guiArgs, null);
                MethodInfo icallButton = typeof(GUI).GetMethod("INTERNAL_CALL_DoButton", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static, null, icallArgs, null);
                MethodInfo hookButton = typeof(MonoImGuiInterceptor).GetMethod("Hook_GuiDoButton", BindingFlags.NonPublic | BindingFlags.Static);
                if (doButton != null && icallButton != null && hookButton != null)
                {
                    _icallDoButton = Delegate.CreateDelegate(typeof(InternalCallDoButtonDelegate), icallButton, false) as InternalCallDoButtonDelegate;
                    if (_icallDoButton != null && TryPatchStaticMethod(doButton, hookButton, log))
                    {
                        hooksInstalled++;
                    }
                }
            }
            catch (Exception ex)
            {
                if (log != null) log("WARNING: GUI.Do* hooks skipped: " + ex.Message);
            }

            if (log != null)
            {
                log("Legacy IMGUI interceptor initialized (hooks=" + hooksInstalled + ").");
            }
        }

        private static bool TryPatchStaticMethod(MethodInfo target, MethodInfo hook, Action<string> log)
        {
            if (target == null || hook == null)
            {
                return false;
            }
            try
            {
                RuntimeHelpers.PrepareMethod(target.MethodHandle);
                RuntimeHelpers.PrepareMethod(hook.MethodHandle);
                IntPtr targetPtr = target.MethodHandle.GetFunctionPointer();
                IntPtr hookPtr = hook.MethodHandle.GetFunctionPointer();
                if (targetPtr == IntPtr.Zero || hookPtr == IntPtr.Zero || targetPtr == hookPtr)
                {
                    return false;
                }
                long addrKey = targetPtr.ToInt64();
                if (_patchedAddrs.Contains(addrKey))
                {
                    return true;
                }

                if (IntPtr.Size == 4)
                {
                    uint oldProt;
                    if (!VirtualProtect(targetPtr, new UIntPtr(8u), 0x40u, out oldProt))
                    {
                        return false;
                    }
                    int rel = unchecked((int)(hookPtr.ToInt32() - (targetPtr.ToInt32() + 5)));
                    Marshal.WriteByte(targetPtr, 0, 0xE9);
                    Marshal.WriteInt32(targetPtr, 1, rel);
                    uint dummy;
                    VirtualProtect(targetPtr, new UIntPtr(8u), oldProt, out dummy);
                    FlushInstructionCache(GetCurrentProcess(), targetPtr, new UIntPtr(8u));
                }
                else
                {
                    uint oldProt;
                    if (!VirtualProtect(targetPtr, new UIntPtr(16u), 0x40u, out oldProt))
                    {
                        return false;
                    }
                    Marshal.WriteByte(targetPtr, 0, 0x48);
                    Marshal.WriteByte(targetPtr, 1, 0xB8);
                    Marshal.WriteInt64(targetPtr, 2, hookPtr.ToInt64());
                    Marshal.WriteByte(targetPtr, 10, 0xFF);
                    Marshal.WriteByte(targetPtr, 11, 0xE0);
                    uint dummy;
                    VirtualProtect(targetPtr, new UIntPtr(16u), oldProt, out dummy);
                    FlushInstructionCache(GetCurrentProcess(), targetPtr, new UIntPtr(16u));
                }

                _patchedAddrs.Add(addrKey);
                return true;
            }
            catch (Exception ex)
            {
                if (log != null) log("WARNING: Failed to patch IMGUI method " + target.Name + ": " + ex.Message);
                return false;
            }
        }

        private static Font ResolveArabicFont()
        {
            if (_fontProvider == null)
            {
                return null;
            }
            try
            {
                return _fontProvider();
            }
            catch
            {
                return null;
            }
        }

        private static string TranslateImGuiText(string raw)
        {
            if (string.IsNullOrEmpty(raw))
            {
                return raw;
            }
            if (OverrideTranslationStore.ContainsArabicPresentationForms(raw))
            {
                EnsureCurrentGuiSkinArabic();
                return raw;
            }
            string cached;
            if (_textCache.TryGetValue(raw, out cached))
            {
                if (!object.ReferenceEquals(cached, raw))
                {
                    EnsureCurrentGuiSkinArabic();
                }
                return cached;
            }
            string translated;
            if (_store != null && _store.TryTranslateRuntimeText(raw, out translated) && !string.IsNullOrEmpty(translated))
            {
                if (_textCache.Count < 4096)
                {
                    _textCache[raw] = translated;
                }
                EnsureCurrentGuiSkinArabic();
                return translated;
            }
            if (_textCache.Count < 4096)
            {
                _textCache[raw] = raw;
            }
            return raw;
        }

        private static void TranslateContent(GUIContent content, GUIStyle style)
        {
            if (content == null)
            {
                return;
            }
            string orig = content.text;
            if (!string.IsNullOrEmpty(orig))
            {
                string updated = TranslateImGuiText(orig);
                if (!string.Equals(orig, updated, StringComparison.Ordinal))
                {
                    content.text = updated;
                }
                if (style != null && OverrideTranslationStore.ContainsArabicPresentationForms(content.text))
                {
                    PatchGuiStyleFont(style, ResolveArabicFont(), true);
                }
            }
        }

        private static void EnsureCurrentGuiSkinArabic()
        {
            try
            {
                GUISkin skin = GUI.skin;
                if (skin != null)
                {
                    PatchGuiSkinFonts(skin, ResolveArabicFont(), _log);
                }
            }
            catch
            {
            }
        }

        public static void PatchLoadedGuiSkins(Font arFont, Action<string> log)
        {
            if (arFont == null)
            {
                return;
            }
            try
            {
                UnityEngine.Object[] skins = Resources.FindObjectsOfTypeAll(typeof(GUISkin));
                if (skins == null)
                {
                    return;
                }
                for (int i = 0; i < skins.Length; i++)
                {
                    GUISkin skin = skins[i] as GUISkin;
                    if (skin != null)
                    {
                        PatchGuiSkinFonts(skin, arFont, log);
                    }
                }
            }
            catch
            {
            }
        }

        public static void PatchGuiSkinFonts(GUISkin skin, Font arFont, Action<string> log)
        {
            if (skin == null || arFont == null)
            {
                return;
            }
            int id = skin.GetInstanceID();
            if (_patchedSkinIds.Contains(id))
            {
                return;
            }
            _patchedSkinIds.Add(id);

            bool changed = false;
            try
            {
                if (skin.font == null || !ArabicFontInjector.FontSupportsArabic(skin.font))
                {
                    skin.font = arFont;
                    changed = true;
                }
                if (PatchGuiStyleFont(skin.box, arFont, false)) changed = true;
                if (PatchGuiStyleFont(skin.label, arFont, false)) changed = true;
                if (PatchGuiStyleFont(skin.button, arFont, false)) changed = true;
                if (PatchGuiStyleFont(skin.toggle, arFont, false)) changed = true;
                if (PatchGuiStyleFont(skin.textField, arFont, false)) changed = true;
                if (PatchGuiStyleFont(skin.textArea, arFont, false)) changed = true;
                if (PatchGuiStyleFont(skin.window, arFont, false)) changed = true;
                GUIStyle[] custom = skin.customStyles;
                if (custom != null)
                {
                    for (int i = 0; i < custom.Length; i++)
                    {
                        if (PatchGuiStyleFont(custom[i], arFont, false))
                        {
                            changed = true;
                        }
                    }
                }
                if (changed && log != null)
                {
                    log("Switched GUISkin '" + (skin.name ?? "unnamed") + "' font to Arabic OS font.");
                }
            }
            catch
            {
            }
        }

        private static bool PatchGuiStyleFont(GUIStyle style, Font arFont, bool patchIfNull)
        {
            if (style == null || arFont == null)
            {
                return false;
            }
            try
            {
                Font cur = style.font;
                if (object.ReferenceEquals(cur, arFont))
                {
                    return false;
                }
                if (cur == null)
                {
                    if (!patchIfNull)
                    {
                        return false;
                    }
                    style.font = arFont;
                    return true;
                }
                if (!ArabicFontInjector.FontSupportsArabic(cur))
                {
                    style.font = arFont;
                    return true;
                }
            }
            catch
            {
            }
            return false;
        }

        private static GUIContent Hook_GuiContentTemp(string t)
        {
            GUIContent c = _cachedSText;
            if (c == null)
            {
                c = new GUIContent();
                _cachedSText = c;
            }
            c.text = TranslateImGuiText(t);
            c.tooltip = string.Empty;
            return c;
        }

        private static bool Hook_GuiLayoutDoButton(GUIContent content, GUIStyle style, GUILayoutOption[] options)
        {
            TranslateContent(content, style);
            return GUI.Button(GUILayoutUtility.GetRect(content, style, options), content, style);
        }

        private static void Hook_GuiLayoutDoLabel(GUIContent content, GUIStyle style, GUILayoutOption[] options)
        {
            TranslateContent(content, style);
            GUI.Label(GUILayoutUtility.GetRect(content, style, options), content, style);
        }

        private static void Hook_GuiLayoutDoBox(GUIContent content, GUIStyle style, GUILayoutOption[] options)
        {
            TranslateContent(content, style);
            GUI.Box(GUILayoutUtility.GetRect(content, style, options), content, style);
        }

        private static void Hook_GuiDoLabel(Rect position, GUIContent content, IntPtr style)
        {
            TranslateContent(content, null);
            _icallDoLabel(ref position, content, style);
        }

        private static bool Hook_GuiDoButton(Rect position, GUIContent content, IntPtr style)
        {
            TranslateContent(content, null);
            return _icallDoButton(ref position, content, style);
        }
    }
#endif
#endif

    internal sealed class OverrideTranslationStore
    {
        public bool LoadAttempted;
        public bool HasOverrides;
        public Dictionary<string, string> ByKey = new Dictionary<string, string>(StringComparer.Ordinal);
        public Dictionary<string, string> BySource = new Dictionary<string, string>(StringComparer.Ordinal);
        public List<ParameterizedOverrideTemplate> Templates = new List<ParameterizedOverrideTemplate>();

        [System.Runtime.InteropServices.DllImport("gdi32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
        private static extern int AddFontResourceExW(string lpszFilename, uint fl, IntPtr pdv);

        private static bool _bundledFontChecked;
        private static string _bundledFontPath;
        private static bool _privateFontRegistered;

        public static string FindAndRegisterBundledArabicFont(Action<string> log)
        {
            if (_bundledFontChecked)
            {
                return _bundledFontPath;
            }
            _bundledFontChecked = true;

            List<string> dirs = new List<string>();
            try
            {
                string baseDir = AppDomain.CurrentDomain.BaseDirectory;
                if (!string.IsNullOrEmpty(baseDir))
                {
                    dirs.Add(baseDir);
                    DirectoryInfo p1 = Directory.GetParent(baseDir);
                    if (p1 != null)
                    {
                        dirs.Add(p1.FullName);
                        if (p1.Parent != null)
                        {
                            dirs.Add(p1.Parent.FullName);
                        }
                    }
                }
            }
            catch
            {
            }
            try
            {
                string cwd = Directory.GetCurrentDirectory();
                if (!string.IsNullOrEmpty(cwd))
                {
                    dirs.Add(cwd);
                }
            }
            catch
            {
            }

            string[] relCandidates = new string[]
            {
                "ArabicFont.ttf",
                "IBMPlexSansArabic-Regular.ttf",
                Path.Combine("IBM_Plex_Sans_Arabic", "IBMPlexSansArabic-Regular.ttf")
            };

            for (int d = 0; d < dirs.Count; d++)
            {
                string dir = dirs[d];
                if (string.IsNullOrEmpty(dir)) continue;
                for (int r = 0; r < relCandidates.Length; r++)
                {
                    try
                    {
                        string full = Path.Combine(dir, relCandidates[r]);
                        if (File.Exists(full))
                        {
                            FileInfo fi = new FileInfo(full);
                            if (fi.Length > 1024)
                            {
                                _bundledFontPath = full;
                                break;
                            }
                        }
                    }
                    catch
                    {
                    }
                }
                if (_bundledFontPath != null)
                {
                    break;
                }
            }

            if (!string.IsNullOrEmpty(_bundledFontPath) && !_privateFontRegistered)
            {
                try
                {
                    int added = AddFontResourceExW(_bundledFontPath, 0x10u, IntPtr.Zero);
                    _privateFontRegistered = (added > 0);
                    if (log != null)
                    {
                        log("Bundled Arabic font detected: " + _bundledFontPath + " (GDI private registered=" + _privateFontRegistered + ").");
                    }
                }
                catch
                {
                }
            }
            return _bundledFontPath;
        }

        public static List<uint> GetArabicTargetCodepoints()
        {
            List<uint> list = new List<uint>();
            for (uint cp = 0x0600; cp <= 0x06FF; cp++)
            {
                list.Add(cp);
            }
            for (uint cp = 0xFE70; cp <= 0xFEFF; cp++)
            {
                list.Add(cp);
            }
            uint[] extras = new uint[]
            {
                0x00AB, 0x00BB, 0x00D7,
                0x2013, 0x201C, 0x201D, 0x2022, 0x2026,
                0x2074, 0x2082, 0x2083, 0x2084, 0x2085, 0x2086, 0x2088
            };
            for (int i = 0; i < extras.Length; i++)
            {
                list.Add(extras[i]);
            }
            return list;
        }

        public static bool ContainsArabicPresentationForms(string text)
        {
            if (string.IsNullOrEmpty(text))
            {
                return false;
            }
            for (int i = 0; i < text.Length; i++)
            {
                char ch = text[i];
                if ((ch >= '\uFE70' && ch <= '\uFEFF') || (ch >= '\u0600' && ch <= '\u06FF'))
                {
                    return true;
                }
            }
            return false;
        }

        public static string FindOverrideJsonPath(string[] candidateDirs)
        {
            if (candidateDirs == null)
            {
                return null;
            }
            for (int i = 0; i < candidateDirs.Length; i++)
            {
                string dir = candidateDirs[i];
                if (string.IsNullOrEmpty(dir))
                {
                    continue;
                }
                try
                {
                    string full = Path.Combine(dir, "ArabicOverride.json");
                    if (File.Exists(full))
                    {
                        return full;
                    }
                }
                catch
                {
                }
            }
            return null;
        }

        public void EnsureLoaded(string[] candidateDirs, Action<string> log)
        {
            if (LoadAttempted)
            {
                return;
            }
            LoadAttempted = true;

            try
            {
                string overridePath = FindOverrideJsonPath(candidateDirs);
                if (string.IsNullOrEmpty(overridePath))
                {
                    return;
                }

                string rawJson = File.ReadAllText(overridePath, Encoding.UTF8);
                Dictionary<string, string> byKey;
                Dictionary<string, string> bySource;
                SimpleOverrideJsonParser.Parse(rawJson, out byKey, out bySource);

                ByKey = byKey ?? new Dictionary<string, string>(StringComparer.Ordinal);
                BySource = bySource ?? new Dictionary<string, string>(StringComparer.Ordinal);
                Templates = BuildOverrideTemplates(BySource);
                HasOverrides = (ByKey.Count > 0 || BySource.Count > 0);

                if (log != null)
                {
                    log(
                        "Loaded ArabicOverride.json from " + overridePath +
                        " (by_key=" + ByKey.Count +
                        ", by_source=" + BySource.Count +
                        ", templates=" + Templates.Count + ")."
                    );
                }
            }
            catch (Exception ex)
            {
                if (log != null)
                {
                    log("WARNING: Failed to load ArabicOverride.json: " + ex.Message);
                }
            }
        }

        private static List<ParameterizedOverrideTemplate> BuildOverrideTemplates(Dictionary<string, string> bySource)
        {
            List<ParameterizedOverrideTemplate> list = new List<ParameterizedOverrideTemplate>();
            if (bySource == null)
            {
                return list;
            }

            foreach (KeyValuePair<string, string> kv in bySource)
            {
                ParameterizedOverrideTemplate tpl = ParameterizedOverrideTemplate.TryCreate(kv.Key, kv.Value);
                if (tpl != null)
                {
                    list.Add(tpl);
                }
            }

            list.Sort(delegate(ParameterizedOverrideTemplate a, ParameterizedOverrideTemplate b)
            {
                return b.LiteralLength.CompareTo(a.LiteralLength);
            });
            return list;
        }

        private readonly HashSet<string> _capturedUnmatched = new HashSet<string>(StringComparer.Ordinal);
        private readonly HashSet<string> _seenUnmatchedOnce = new HashSet<string>(StringComparer.Ordinal);
        private string _captureFilePath;

        private void RecordUnmatchedText(string text)
        {
            if (string.IsNullOrEmpty(text) || _capturedUnmatched.Count >= 500)
            {
                return;
            }
            string trimmed = text.Trim();
            if (trimmed.Length < 2 || trimmed.Length > 300 || ContainsArabicPresentationForms(trimmed))
            {
                return;
            }
            int letters = 0;
            for (int i = 0; i < trimmed.Length; i++)
            {
                char ch = trimmed[i];
                if ((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z'))
                {
                    letters++;
                    if (letters >= 2) break;
                }
            }
            if (letters < 2 || _capturedUnmatched.Contains(trimmed))
            {
                return;
            }
            // تسجيل النص فقط عند ثباته في أكثر من دورة لتجنب التقاط خطوات تأثير الآلة الكاتبة (Typewriter)
            if (_seenUnmatchedOnce.Count > 2000)
            {
                _seenUnmatchedOnce.Clear();
            }
            if (_seenUnmatchedOnce.Add(trimmed))
            {
                return;
            }
            if (!_capturedUnmatched.Add(trimmed))
            {
                return;
            }
            try
            {
                if (string.IsNullOrEmpty(_captureFilePath))
                {
                    _captureFilePath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory ?? ".", "arabic_captured_texts.txt");
                }
                File.AppendAllText(_captureFilePath, trimmed.Replace("\r", "").Replace("\n", "\\n") + Environment.NewLine, Encoding.UTF8);
            }
            catch
            {
            }
        }

        public bool TryTranslateRuntimeText(string current, out string translated)
        {
            translated = null;
            if (!HasOverrides || string.IsNullOrEmpty(current) || ContainsArabicPresentationForms(current))
            {
                return false;
            }

            if (BySource != null)
            {
                if (BySource.TryGetValue(current, out translated))
                {
                    return true;
                }
                string trimmed = current.Trim();
                if (trimmed.Length > 0 && trimmed.Length != current.Length && BySource.TryGetValue(trimmed, out translated))
                {
                    int startIdx = current.IndexOf(trimmed, StringComparison.Ordinal);
                    if (startIdx >= 0)
                    {
                        string lead = current.Substring(0, startIdx);
                        string trail = current.Substring(startIdx + trimmed.Length);
                        translated = lead + translated + trail;
                    }
                    return true;
                }
                if (current.IndexOf('\r') >= 0)
                {
                    string normalized = current.Replace("\r\n", "\n").Replace("\r", "\n");
                    if (BySource.TryGetValue(normalized, out translated))
                    {
                        return true;
                    }
                    string normTrimmed = normalized.Trim();
                    if (normTrimmed.Length > 0 && normTrimmed.Length != normalized.Length && BySource.TryGetValue(normTrimmed, out translated))
                    {
                        int startIdx = normalized.IndexOf(normTrimmed, StringComparison.Ordinal);
                        if (startIdx >= 0)
                        {
                            translated = normalized.Substring(0, startIdx) + translated + normalized.Substring(startIdx + normTrimmed.Length);
                        }
                        return true;
                    }
                }
            }

            if (Templates != null)
            {
                for (int i = 0; i < Templates.Count; i++)
                {
                    if (Templates[i].TryApply(current, out translated))
                    {
                        return true;
                    }
                }
            }

            if (BySource != null)
            {
                string wrappedOut;
                if (TryTranslateWrappedText(current, out wrappedOut))
                {
                    translated = wrappedOut;
                    return true;
                }
            }

            RecordUnmatchedText(current);
            return false;
        }

        private static bool IsDecorativeWrapperChar(char ch)
        {
            if (char.IsWhiteSpace(ch))
            {
                return true;
            }
            switch (ch)
            {
                case '\u25B8': // ▸
                case '\u25C2': // ◂
                case '\u25BA': // ►
                case '\u25C4': // ◄
                case '\u25B6': // ▶
                case '\u25C0': // ◀
                case '\u2022': // •
                case '\u00B7': // ·
                case '\u2605': // ★
                case '\u2606': // ☆
                case '*':
                case '>':
                case '<':
                case '[':
                case ']':
                case '\u2014': // —
                case '\u2013': // –
                case '-':
                    return true;
                default:
                    return false;
            }
        }

        private bool TryTranslateWrappedText(string current, out string translated)
        {
            translated = null;
            if (string.IsNullOrEmpty(current) || BySource == null)
            {
                return false;
            }
            int start = 0;
            int end = current.Length - 1;
            while (start <= end && IsDecorativeWrapperChar(current[start]))
            {
                start++;
            }
            while (end >= start && IsDecorativeWrapperChar(current[end]))
            {
                end--;
            }
            int innerLen = end - start + 1;
            if (innerLen < 2 || (start == 0 && end == current.Length - 1))
            {
                return false;
            }
            string inner = current.Substring(start, innerLen);
            string innerTrans;
            if (BySource.TryGetValue(inner, out innerTrans))
            {
                string lead = current.Substring(0, start);
                string trail = current.Substring(end + 1);
                translated = lead + innerTrans + trail;
                return true;
            }
            return false;
        }
    }

    internal static class RuntimeOverrideReflection
    {
        public static Type FindTypeInLoadedAssemblies(string fullName)
        {
            if (string.IsNullOrEmpty(fullName))
            {
                return null;
            }
            Assembly[] assemblies = AppDomain.CurrentDomain.GetAssemblies();
            for (int i = 0; i < assemblies.Length; i++)
            {
                try
                {
                    Type t = assemblies[i].GetType(fullName, false);
                    if (t != null)
                    {
                        return t;
                    }
                }
                catch
                {
                }
            }
            return null;
        }

        public static object GetMemberValue(object target, string memberName)
        {
            if (target == null || string.IsNullOrEmpty(memberName))
            {
                return null;
            }
            Type t = target.GetType();
            FieldInfo fi = t.GetField(memberName, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (fi != null)
            {
                return fi.GetValue(target);
            }
            PropertyInfo pi = t.GetProperty(memberName, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (pi != null && pi.CanRead && pi.GetIndexParameters().Length == 0)
            {
                return pi.GetValue(target, null);
            }
            return null;
        }

        public static object GetStaticMemberValue(Type targetType, string memberName)
        {
            if (targetType == null || string.IsNullOrEmpty(memberName))
            {
                return null;
            }
            PropertyInfo pi = targetType.GetProperty(memberName, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
            if (pi != null && pi.CanRead && pi.GetIndexParameters().Length == 0)
            {
                return pi.GetValue(null, null);
            }
            FieldInfo fi = targetType.GetField(memberName, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
            if (fi != null)
            {
                return fi.GetValue(null);
            }
            return null;
        }

        public static void SetMemberValue(object target, string memberName, object value)
        {
            if (target == null || string.IsNullOrEmpty(memberName))
            {
                return;
            }
            Type t = target.GetType();
            PropertyInfo pi = t.GetProperty(memberName, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (pi != null && pi.CanWrite && pi.GetIndexParameters().Length == 0)
            {
                pi.SetValue(target, value, null);
                return;
            }
            FieldInfo fi = t.GetField(memberName, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (fi != null)
            {
                fi.SetValue(target, value);
            }
        }

        public static int GetCollectionCount(object col)
        {
            if (col == null)
            {
                return 0;
            }
            IList list = col as IList;
            if (list != null)
            {
                return list.Count;
            }
            PropertyInfo countProp = col.GetType().GetProperty("Count", BindingFlags.Public | BindingFlags.Instance);
            if (countProp != null && countProp.CanRead)
            {
                object val = countProp.GetValue(col, null);
                if (val is int) return (int)val;
            }
            PropertyInfo lenProp = col.GetType().GetProperty("Length", BindingFlags.Public | BindingFlags.Instance);
            if (lenProp != null && lenProp.CanRead)
            {
                object val = lenProp.GetValue(col, null);
                if (val is int) return (int)val;
            }
            return 0;
        }

        public static object GetCollectionItem(object col, int idx)
        {
            if (col == null)
            {
                return null;
            }
            IList list = col as IList;
            if (list != null)
            {
                return list[idx];
            }
            PropertyInfo itemProp = col.GetType().GetProperty("Item", BindingFlags.Public | BindingFlags.Instance);
            if (itemProp != null && itemProp.CanRead)
            {
                return itemProp.GetValue(col, new object[] { idx });
            }
            MethodInfo getItem = col.GetType().GetMethod("get_Item", BindingFlags.Public | BindingFlags.Instance);
            if (getItem != null)
            {
                return getItem.Invoke(col, new object[] { idx });
            }
            return null;
        }

        public static void SetCollectionItem(object col, int idx, object value)
        {
            if (col == null)
            {
                return;
            }
            IList list = col as IList;
            if (list != null)
            {
                list[idx] = value;
                return;
            }
            PropertyInfo itemProp = col.GetType().GetProperty("Item", BindingFlags.Public | BindingFlags.Instance);
            if (itemProp != null && itemProp.CanWrite)
            {
                itemProp.SetValue(col, value, new object[] { idx });
                return;
            }
            MethodInfo setItem = col.GetType().GetMethod("set_Item", BindingFlags.Public | BindingFlags.Instance);
            if (setItem != null)
            {
                setItem.Invoke(col, new object[] { idx, value });
            }
        }

        public static void ApplyI2LocalizationOverrides(
            OverrideTranslationStore store,
            ref Type cachedI2Type,
            ref bool searched,
            Action<string> log
        )
        {
            if (store == null || !store.HasOverrides)
            {
                return;
            }
            try
            {
                if (!searched)
                {
                    cachedI2Type = FindTypeInLoadedAssemblies("I2.Loc.LocalizationManager");
                    if (cachedI2Type != null)
                    {
                        searched = true;
                    }
                }
                if (cachedI2Type == null)
                {
                    return;
                }

                object sources = GetStaticMemberValue(cachedI2Type, "Sources");
                int sourcesCount = GetCollectionCount(sources);
                if (sources == null || sourcesCount == 0)
                {
                    MethodInfo initMethod = cachedI2Type.GetMethod(
                        "InitializeIfNeeded",
                        BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static,
                        null,
                        Type.EmptyTypes,
                        null
                    );
                    if (initMethod != null)
                    {
                        try
                        {
                            initMethod.Invoke(null, null);
                            sources = GetStaticMemberValue(cachedI2Type, "Sources");
                            sourcesCount = GetCollectionCount(sources);
                        }
                        catch (Exception ex)
                        {
                            if (log != null)
                            {
                                string detail = ex.InnerException != null ? ex.InnerException.Message : ex.Message;
                                log("WARNING in I2.InitializeIfNeeded: " + detail);
                            }
                        }
                    }
                    if (sources == null || sourcesCount == 0)
                    {
                        return;
                    }
                }

                int totalUpdated = 0;
                for (int s = 0; s < sourcesCount; s++)
                {
                    object src = GetCollectionItem(sources, s);
                    if (src == null) continue;

                    object terms = GetMemberValue(src, "mTerms") ?? GetMemberValue(src, "m_Terms");
                    int termsCount = GetCollectionCount(terms);
                    if (terms == null || termsCount == 0) continue;

                    int arabicIdx = -1;
                    object langs = GetMemberValue(src, "mLanguages") ?? GetMemberValue(src, "m_Languages");
                    int langsCount = GetCollectionCount(langs);
                    for (int l = 0; l < langsCount; l++)
                    {
                        object langObj = GetCollectionItem(langs, l);
                        string lName = GetMemberValue(langObj, "Name") as string;
                        string lCode = GetMemberValue(langObj, "Code") as string;
                        if ((!string.IsNullOrEmpty(lName) && lName.IndexOf("Arabic", StringComparison.OrdinalIgnoreCase) >= 0) ||
                            (!string.IsNullOrEmpty(lCode) && string.Equals(lCode, "ar", StringComparison.OrdinalIgnoreCase)))
                        {
                            arabicIdx = l;
                            break;
                        }
                    }

                    bool sourceChanged = false;
                    for (int tIdx = 0; tIdx < termsCount; tIdx++)
                    {
                        object termData = GetCollectionItem(terms, tIdx);
                        if (termData == null) continue;

                        object termTypeObj = GetMemberValue(termData, "TermType");
                        if (termTypeObj != null)
                        {
                            try
                            {
                                if (Convert.ToInt32(termTypeObj) != 0)
                                {
                                    continue;
                                }
                            }
                            catch
                            {
                            }
                        }

                        string termKey = GetMemberValue(termData, "Term") as string;
                        object termLangs = GetMemberValue(termData, "Languages");
                        int termLangsCount = GetCollectionCount(termLangs);
                        if (termLangs == null || termLangsCount == 0) continue;

                        string translated = null;
                        bool found = false;
                        if (!string.IsNullOrEmpty(termKey) && store.ByKey != null)
                        {
                            found = store.ByKey.TryGetValue(termKey, out translated);
                            if (!found && termKey.IndexOf('/') >= 0)
                            {
                                found = store.ByKey.TryGetValue(termKey.Replace('/', '.'), out translated);
                                if (!found)
                                {
                                    int slashIdx = termKey.LastIndexOf('/');
                                    if (slashIdx >= 0 && slashIdx + 1 < termKey.Length)
                                    {
                                        found = store.ByKey.TryGetValue(termKey.Substring(slashIdx + 1), out translated);
                                    }
                                }
                            }
                        }

                        if (!found)
                        {
                            for (int vIdx = 0; vIdx < termLangsCount; vIdx++)
                            {
                                string rawVal = GetCollectionItem(termLangs, vIdx) as string;
                                if (store.TryTranslateRuntimeText(rawVal, out translated))
                                {
                                    found = true;
                                    break;
                                }
                            }
                        }

                        if (!found || string.IsNullOrEmpty(translated))
                        {
                            continue;
                        }

                        bool termUpdated = false;
                        for (int vIdx = 0; vIdx < termLangsCount; vIdx++)
                        {
                            string curVal = GetCollectionItem(termLangs, vIdx) as string;
                            string tmpCheck;
                            bool shouldReplace = (vIdx == arabicIdx) ||
                                                 (vIdx == 0 && !OverrideTranslationStore.ContainsArabicPresentationForms(curVal)) ||
                                                 store.TryTranslateRuntimeText(curVal, out tmpCheck);
                            if (shouldReplace && !string.Equals(curVal, translated, StringComparison.Ordinal))
                            {
                                SetCollectionItem(termLangs, vIdx, translated);
                                termUpdated = true;
                            }
                        }
                        if (termUpdated)
                        {
                            sourceChanged = true;
                            totalUpdated++;
                        }
                    }

                    if (sourceChanged)
                    {
                        try
                        {
                            MethodInfo updateWithBool = src.GetType().GetMethod("UpdateDictionary", new Type[] { typeof(bool) });
                            if (updateWithBool != null)
                            {
                                updateWithBool.Invoke(src, new object[] { true });
                            }
                            else
                            {
                                MethodInfo updateNoArg = src.GetType().GetMethod("UpdateDictionary", Type.EmptyTypes);
                                if (updateNoArg != null)
                                {
                                    updateNoArg.Invoke(src, null);
                                }
                            }
                        }
                        catch (Exception ex)
                        {
                            if (log != null)
                            {
                                string detail = ex.InnerException != null ? ex.InnerException.Message : ex.Message;
                                log("WARNING in I2.UpdateDictionary: " + detail);
                            }
                        }
                    }
                }

                if (totalUpdated > 0)
                {
                    try
                    {
                        FieldInfo r2lField = cachedI2Type.GetField(
                            "IsRight2Left",
                            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static
                        );
                        if (r2lField != null && r2lField.FieldType == typeof(bool))
                        {
                            r2lField.SetValue(null, false);
                        }
                        MethodInfo locAll = cachedI2Type.GetMethod(
                            "LocalizeAll",
                            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static,
                            null,
                            new Type[] { typeof(bool) },
                            null
                        );
                        if (locAll != null)
                        {
                            locAll.Invoke(null, new object[] { true });
                        }
                    }
                    catch (Exception ex)
                    {
                        if (log != null)
                        {
                            string detail = ex.InnerException != null ? ex.InnerException.Message : ex.Message;
                            log("WARNING in I2.LocalizeAll: " + detail);
                        }
                    }
                    if (log != null)
                    {
                        log("Applied " + totalUpdated + " I2 Localization runtime overrides.");
                    }
                }
            }
            catch (Exception ex)
            {
                if (log != null)
                {
                    log("WARNING in ApplyI2LocalizationOverrides: " + ex.Message);
                }
            }
        }
    }

#if IL2CPP_BACKEND
    public static class Il2CppPluginBootstrap
    {
        public static void Initialize()
        {
            Il2CppRuntimeInjector.Initialize();
        }

        public static void OnUpdate()
        {
            Il2CppRuntimeInjector.OnUpdate();
        }
    }

    internal static class Il2CppRuntimeInjector
    {
        private static int _started;
        private static int _nativeProbeLogged;
        private static readonly OverrideTranslationStore _store = new OverrideTranslationStore();
        private static Type _i2LocManagerType;
        private static bool _i2Searched;
        private static object _interopArabicTmpFont;
        private static object _interopArabicOsFont;
        private static bool _tmpFallbackAttached;
        private static SynchronizationContext _capturedSyncContext;

        private static string GetBaseDir()
        {
            try
            {
                string baseDir = AppDomain.CurrentDomain.BaseDirectory;
                if (!string.IsNullOrEmpty(baseDir))
                {
                    return baseDir;
                }
            }
            catch
            {
            }
            return ".";
        }

        private static string GetLogPath()
        {
            try
            {
                return Path.Combine(GetBaseDir(), "arabic_font_log.txt");
            }
            catch
            {
                return "arabic_font_log.txt";
            }
        }

        private static void Log(string message)
        {
            try
            {
                File.AppendAllText(
                    GetLogPath(),
                    "[" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "] [IL2CPP] " + message + Environment.NewLine
                );
            }
            catch
            {
            }
        }

        private static void EnsureOverridesLoaded()
        {
            if (_store.LoadAttempted)
            {
                return;
            }

            List<string> candidates = new List<string>();
            string baseDir = GetBaseDir();
            candidates.Add(baseDir);
            try
            {
                string cwd = Directory.GetCurrentDirectory();
                candidates.Add(cwd);
                DirectoryInfo parent = Directory.GetParent(baseDir);
                if (parent != null)
                {
                    candidates.Add(parent.FullName);
                    if (parent.Parent != null)
                    {
                        candidates.Add(parent.Parent.FullName);
                    }
                }
            }
            catch
            {
            }

            _store.EnsureLoaded(candidates.ToArray(), Log);
        }

        public static void Initialize()
        {
            if (Interlocked.Exchange(ref _started, 1) != 0)
            {
                return;
            }

            Log("Initializing IL2CPP Arabic runtime injector...");
            EnsureOverridesLoaded();

            Thread worker = new Thread(BackgroundLoop);
            worker.IsBackground = true;
            worker.Start();
        }

        private static void BackgroundLoop()
        {
            try
            {
                Thread.Sleep(1500);
                bool attached = false;
                for (int attempt = 0; attempt < 120; attempt++)
                {
                    if (!attached)
                    {
                        attached = Il2CppNativeApi.TryBindAndAttach(Log);
                        if (attached)
                        {
                            ProbeNativeIl2CppMetadata();
                        }
                    }

                    EnsureOverridesLoaded();
                    TryCaptureUnitySyncContext();
                    if (attached && _capturedSyncContext == null)
                    {
                        Il2CppNativeApi.TryInstallMainThreadHook(OnNativeMainThreadTick, Log);
                    }
                    DispatchManagedInteropTick();
                    Thread.Sleep(attached ? 1500 : 500);
                }

                while (true)
                {
                    if (_capturedSyncContext == null)
                    {
                        Il2CppNativeApi.TryInstallMainThreadHook(OnNativeMainThreadTick, Log);
                    }
                    DispatchManagedInteropTick();
                    Thread.Sleep(2500);
                }
            }
            catch (Exception ex)
            {
                Log("WARNING in IL2CPP BackgroundLoop: " + ex.Message);
            }
        }

        public static void OnUpdate()
        {
            EnsureOverridesLoaded();
            ApplyManagedInteropTick(true);
        }

        private static void OnNativeMainThreadTick()
        {
            try
            {
                EnsureOverridesLoaded();
                bool justAttachedFont = Il2CppNativeApi.EnsureNativeTmpFallbackFont(Log);
                if (_store.HasOverrides)
                {
                    Il2CppNativeApi.ApplyNativeIl2CppOverrides(_store, Log, justAttachedFont);
                }
                else if (justAttachedFont)
                {
                    Il2CppNativeApi.ForceAllNativeTmpMeshUpdates(Log);
                }
            }
            catch (Exception ex)
            {
                Log("WARNING in OnNativeMainThreadTick: " + ex.Message);
            }
        }

        private static void ProbeNativeIl2CppMetadata()
        {
            if (!Il2CppNativeApi.IsAttached || Interlocked.Exchange(ref _nativeProbeLogged, 1) != 0)
            {
                return;
            }
            try
            {
                IntPtr i2Class = Il2CppNativeApi.FindClass("I2.Loc", "LocalizationManager");
                IntPtr tmpSettingsClass = Il2CppNativeApi.FindClass("TMPro", "TMP_Settings");
                IntPtr tmpTextClass = Il2CppNativeApi.FindClass("TMPro", "TMP_Text");
                Log(
                    "Native IL2CPP class probe: I2.Loc.LocalizationManager=" + (i2Class != IntPtr.Zero) +
                    ", TMPro.TMP_Settings=" + (tmpSettingsClass != IntPtr.Zero) +
                    ", TMPro.TMP_Text=" + (tmpTextClass != IntPtr.Zero)
                );
            }
            catch (Exception ex)
            {
                Log("WARNING in ProbeNativeIl2CppMetadata: " + ex.Message);
            }
        }

        private static void TryCaptureUnitySyncContext()
        {
            if (_capturedSyncContext != null)
            {
                return;
            }
            try
            {
                Type syncType = RuntimeOverrideReflection.FindTypeInLoadedAssemblies("UnityEngine.UnitySynchronizationContext");
                if (syncType == null)
                {
                    return;
                }
                object inst = RuntimeOverrideReflection.GetStaticMemberValue(syncType, "Current") ??
                              RuntimeOverrideReflection.GetStaticMemberValue(syncType, "Instance");
                SynchronizationContext ctx = inst as SynchronizationContext;
                if (ctx != null)
                {
                    _capturedSyncContext = ctx;
                }
            }
            catch
            {
            }
        }

        private static void DispatchManagedInteropTick()
        {
            if (_capturedSyncContext != null)
            {
                try
                {
                    _capturedSyncContext.Post(delegate(object state)
                    {
                        ApplyManagedInteropTick(true);
                    }, null);
                    return;
                }
                catch
                {
                }
            }
            ApplyManagedInteropTick(false);
        }

        private static void ApplyManagedInteropTick(bool isMainThread)
        {
            try
            {
                Type resType = RuntimeOverrideReflection.FindTypeInLoadedAssemblies("UnityEngine.Resources") ??
                               RuntimeOverrideReflection.FindTypeInLoadedAssemblies("UnityEngine.ResourcesAPIInternal");
                if (resType != null)
                {
                    EnsureTmpFallbackFontViaReflection();
                    if (_store.HasOverrides)
                    {
                        RuntimeOverrideReflection.ApplyI2LocalizationOverrides(
                            _store,
                            ref _i2LocManagerType,
                            ref _i2Searched,
                            Log
                        );
                    }
                    ApplyActiveUiTextOverridesViaReflection();
                }
                else if (_store.HasOverrides && Il2CppNativeApi.IsAttached)
                {
                    if (isMainThread)
                    {
                        Il2CppNativeApi.ApplyNativeIl2CppOverrides(_store, Log);
                    }
                    else
                    {
                        Il2CppNativeApi.ApplyNativeI2LocalizationOverrides(_store, Log);
                    }
                }
            }
            catch (Exception ex)
            {
                Log("WARNING in ApplyManagedInteropTick: " + ex.Message);
            }
        }

        private static object GetOrCreateInteropOsFont(Type fontType, Type objectType, int size)
        {
            if (_interopArabicOsFont != null)
            {
                return _interopArabicOsFont;
            }
            try
            {
                MethodInfo createOsFont = fontType.GetMethod(
                    "CreateDynamicFontFromOSFont",
                    BindingFlags.Public | BindingFlags.Static,
                    null,
                    new Type[] { typeof(string[]), typeof(int) },
                    null
                );
                if (createOsFont != null)
                {
                    OverrideTranslationStore.FindAndRegisterBundledArabicFont(Log);
                    _interopArabicOsFont = createOsFont.Invoke(
                        null,
                        new object[] { new string[] { "IBM Plex Sans Arabic", "Arial", "Tahoma" }, size }
                    );
                    if (_interopArabicOsFont != null)
                    {
                        SetDontDestroyAndHideFlags(_interopArabicOsFont, objectType);
                    }
                }
            }
            catch
            {
            }
            return _interopArabicOsFont;
        }

        private static void SetDontDestroyAndHideFlags(object unityObj, Type objectType)
        {
            if (unityObj == null)
            {
                return;
            }
            try
            {
                Type hideFlagsType = RuntimeOverrideReflection.FindTypeInLoadedAssemblies("UnityEngine.HideFlags");
                if (hideFlagsType != null && Enum.IsDefined(hideFlagsType, "DontSave"))
                {
                    object dontSaveVal = Enum.Parse(hideFlagsType, "DontSave");
                    RuntimeOverrideReflection.SetMemberValue(unityObj, "hideFlags", dontSaveVal);
                }
                if (objectType != null)
                {
                    MethodInfo dontDestroy = objectType.GetMethod(
                        "DontDestroyOnLoad",
                        BindingFlags.Public | BindingFlags.Static
                    );
                    if (dontDestroy != null)
                    {
                        dontDestroy.Invoke(null, new object[] { unityObj });
                    }
                }
            }
            catch
            {
            }
        }

        private static void EnsureTmpFallbackFontViaReflection()
        {
            if (_tmpFallbackAttached)
            {
                return;
            }
            Type tmpSettingsType = RuntimeOverrideReflection.FindTypeInLoadedAssemblies("TMPro.TMP_Settings");
            Type tmpFontAssetType = RuntimeOverrideReflection.FindTypeInLoadedAssemblies("TMPro.TMP_FontAsset");
            Type fontType = RuntimeOverrideReflection.FindTypeInLoadedAssemblies("UnityEngine.Font");
            Type objectType = RuntimeOverrideReflection.FindTypeInLoadedAssemblies("UnityEngine.Object");
            if (tmpSettingsType == null || tmpFontAssetType == null || fontType == null)
            {
                return;
            }

            try
            {
                object settings = RuntimeOverrideReflection.GetStaticMemberValue(tmpSettingsType, "instance");
                if (settings == null)
                {
                    return;
                }

                if (_interopArabicTmpFont == null)
                {
                    object osFont = GetOrCreateInteropOsFont(fontType, objectType, 48);
                    if (osFont == null)
                    {
                        return;
                    }

                    MethodInfo[] methods = tmpFontAssetType.GetMethods(BindingFlags.Public | BindingFlags.Static);
                    for (int i = 0; i < methods.Length; i++)
                    {
                        MethodInfo m = methods[i];
                        if (!string.Equals(m.Name, "CreateFontAsset", StringComparison.Ordinal))
                        {
                            continue;
                        }
                        ParameterInfo[] ps = m.GetParameters();
                        if (ps.Length == 1 && ps[0].ParameterType == fontType)
                        {
                            _interopArabicTmpFont = m.Invoke(null, new object[] { osFont });
                            break;
                        }
                        if (ps.Length == 8 && ps[0].ParameterType == fontType)
                        {
                            object renderMode = Enum.ToObject(ps[3].ParameterType, 4165);
                            object popMode = Enum.ToObject(ps[6].ParameterType, 1);
                            _interopArabicTmpFont = m.Invoke(
                                null,
                                new object[] { osFont, 48, 5, renderMode, 2048, 2048, popMode, false }
                            );
                            break;
                        }
                    }

                    if (_interopArabicTmpFont != null)
                    {
                        RuntimeOverrideReflection.SetMemberValue(_interopArabicTmpFont, "name", "ArabicFallbackSDF");
                        SetDontDestroyAndHideFlags(_interopArabicTmpFont, objectType);
                        List<uint> codepoints = OverrideTranslationStore.GetArabicTargetCodepoints();
                        MethodInfo addChars = _interopArabicTmpFont.GetType().GetMethod(
                            "TryAddCharacters",
                            BindingFlags.Public | BindingFlags.Instance,
                            null,
                            new Type[] { typeof(uint[]), typeof(uint[]).MakeByRefType(), typeof(bool) },
                            null
                        );
                        if (addChars != null)
                        {
                            object[] args = new object[] { codepoints.ToArray(), null, false };
                            addChars.Invoke(_interopArabicTmpFont, args);
                        }
                    }
                }

                if (_interopArabicTmpFont == null)
                {
                    return;
                }

                object fallbacks = RuntimeOverrideReflection.GetStaticMemberValue(tmpSettingsType, "fallbackFontAssets");
                if (fallbacks == null)
                {
                    return;
                }

                MethodInfo insertMethod = fallbacks.GetType().GetMethod("Insert", BindingFlags.Public | BindingFlags.Instance);
                MethodInfo containsMethod = fallbacks.GetType().GetMethod("Contains", BindingFlags.Public | BindingFlags.Instance);
                bool alreadyAdded = false;
                if (containsMethod != null)
                {
                    object res = containsMethod.Invoke(fallbacks, new object[] { _interopArabicTmpFont });
                    if (res is bool)
                    {
                        alreadyAdded = (bool)res;
                    }
                }
                if (!alreadyAdded)
                {
                    if (insertMethod != null)
                    {
                        insertMethod.Invoke(fallbacks, new object[] { 0, _interopArabicTmpFont });
                        _tmpFallbackAttached = true;
                        Log("Attached ArabicFallbackSDF to IL2CPP TMP_Settings.fallbackFontAssets via Interop.");
                    }
                    else
                    {
                        MethodInfo addMethod = fallbacks.GetType().GetMethod("Add", BindingFlags.Public | BindingFlags.Instance);
                        if (addMethod != null)
                        {
                            addMethod.Invoke(fallbacks, new object[] { _interopArabicTmpFont });
                            _tmpFallbackAttached = true;
                            Log("Appended ArabicFallbackSDF to IL2CPP TMP_Settings.fallbackFontAssets via Interop.");
                        }
                    }
                }
                else
                {
                    _tmpFallbackAttached = true;
                }
            }
            catch (Exception ex)
            {
                Log("WARNING in EnsureTmpFallbackFontViaReflection: " + ex.Message);
            }
        }

        private static MethodInfo FindStaticTypeMethod(Type ownerType, string methodName, bool includeNonPublic)
        {
            if (ownerType == null)
            {
                return null;
            }
            BindingFlags flags = BindingFlags.Public | BindingFlags.Static;
            if (includeNonPublic)
            {
                flags |= BindingFlags.NonPublic;
            }
            MethodInfo[] methods = ownerType.GetMethods(flags);
            for (int i = 0; i < methods.Length; i++)
            {
                if (string.Equals(methods[i].Name, methodName, StringComparison.Ordinal) && !methods[i].IsGenericMethod)
                {
                    ParameterInfo[] ps = methods[i].GetParameters();
                    if (ps.Length == 1 && ps[0].ParameterType == typeof(Type))
                    {
                        return methods[i];
                    }
                }
            }
            return null;
        }

        private static object FindInteropComponentCollection(Type compType)
        {
            if (compType == null)
            {
                return null;
            }
            string[] resTypeNames = new string[] { "UnityEngine.Resources", "UnityEngine.ResourcesAPIInternal" };
            for (int t = 0; t < resTypeNames.Length; t++)
            {
                Type resType = RuntimeOverrideReflection.FindTypeInLoadedAssemblies(resTypeNames[t]);
                MethodInfo findMethod = FindStaticTypeMethod(resType, "FindObjectsOfTypeAll", t > 0);
                if (findMethod != null)
                {
                    try
                    {
                        object found = findMethod.Invoke(null, new object[] { compType });
                        if (found != null)
                        {
                            return found;
                        }
                    }
                    catch
                    {
                    }
                }
            }

            Type objectType = RuntimeOverrideReflection.FindTypeInLoadedAssemblies("UnityEngine.Object");
            if (objectType != null)
            {
                MethodInfo[] objMethods = objectType.GetMethods(BindingFlags.Public | BindingFlags.Static);
                for (int i = 0; i < objMethods.Length; i++)
                {
                    if (!string.Equals(objMethods[i].Name, "FindObjectsByType", StringComparison.Ordinal) || objMethods[i].IsGenericMethod)
                    {
                        continue;
                    }
                    ParameterInfo[] ps = objMethods[i].GetParameters();
                    if (ps.Length == 3 && ps[0].ParameterType == typeof(Type) && ps[1].ParameterType.IsEnum && ps[2].ParameterType.IsEnum)
                    {
                        try
                        {
                            object inactiveArg = Enum.ToObject(ps[1].ParameterType, 1);
                            object sortArg = Enum.ToObject(ps[2].ParameterType, 0);
                            return objMethods[i].Invoke(null, new object[] { compType, inactiveArg, sortArg });
                        }
                        catch
                        {
                        }
                    }
                }
            }
            return null;
        }

        private static void ApplyActiveUiTextOverridesViaReflection()
        {
            Type tmpTextType = RuntimeOverrideReflection.FindTypeInLoadedAssemblies("TMPro.TMP_Text");
            if (tmpTextType != null)
            {
                UpdateUiComponentTexts(tmpTextType, true, null, null);
            }

            Type uguiTextType = RuntimeOverrideReflection.FindTypeInLoadedAssemblies("UnityEngine.UI.Text");
            if (uguiTextType != null)
            {
                Type fontType = RuntimeOverrideReflection.FindTypeInLoadedAssemblies("UnityEngine.Font");
                Type objectType = RuntimeOverrideReflection.FindTypeInLoadedAssemblies("UnityEngine.Object");
                UpdateUiComponentTexts(uguiTextType, false, fontType, objectType);
            }
        }

        private static void UpdateUiComponentTexts(
            Type compType,
            bool isTmp,
            Type fontType,
            Type objectType
        )
        {
            try
            {
                object found = FindInteropComponentCollection(compType);
                int count = RuntimeOverrideReflection.GetCollectionCount(found);
                if (count == 0)
                {
                    return;
                }

                PropertyInfo textProp = compType.GetProperty("text", BindingFlags.Public | BindingFlags.Instance);
                PropertyInfo fontProp = isTmp ? null : compType.GetProperty("font", BindingFlags.Public | BindingFlags.Instance);
                MethodInfo forceMesh = isTmp
                    ? compType.GetMethod("ForceMeshUpdate", new Type[] { typeof(bool), typeof(bool) })
                    : null;
                MethodInfo setAllDirty = isTmp
                    ? compType.GetMethod("SetAllDirty", Type.EmptyTypes)
                    : null;

                for (int i = 0; i < count; i++)
                {
                    object comp = RuntimeOverrideReflection.GetCollectionItem(found, i);
                    if (comp == null) continue;
                    object go = RuntimeOverrideReflection.GetMemberValue(comp, "gameObject");
                    if (go == null) continue;
                    object activeObj = RuntimeOverrideReflection.GetMemberValue(go, "activeInHierarchy");
                    if (activeObj is bool && !((bool)activeObj))
                    {
                        continue;
                    }
                    if (textProp == null) continue;

                    string curText = textProp.GetValue(comp, null) as string;
                    string translated;
                    bool hasArabic = false;
                    if (_store.TryTranslateRuntimeText(curText, out translated))
                    {
                        textProp.SetValue(comp, translated, null);
                        hasArabic = true;
                        if (isTmp)
                        {
                            if (forceMesh != null) forceMesh.Invoke(comp, new object[] { true, true });
                            if (setAllDirty != null) setAllDirty.Invoke(comp, null);
                        }
                    }
                    else if (OverrideTranslationStore.ContainsArabicPresentationForms(curText))
                    {
                        hasArabic = true;
                    }

                    if (hasArabic && !isTmp && fontProp != null && fontType != null)
                    {
                        object arFont = GetOrCreateInteropOsFont(fontType, objectType, 24);
                        if (arFont != null)
                        {
                            object curFont = fontProp.GetValue(comp, null);
                            if (!object.ReferenceEquals(curFont, arFont))
                            {
                                fontProp.SetValue(comp, arFont, null);
                            }
                        }
                    }
                }
            }
            catch
            {
            }
        }
    }

    internal static class Il2CppNativeApi
    {
        [DllImport("kernel32.dll", EntryPoint = "GetModuleHandleA", CharSet = CharSet.Ansi)]
        private static extern IntPtr GetModuleHandleA(string lpModuleName);

        [DllImport("kernel32.dll", EntryPoint = "GetProcAddress", CharSet = CharSet.Ansi)]
        private static extern IntPtr GetProcAddress(IntPtr hModule, string lpProcName);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool VirtualProtect(IntPtr lpAddress, UIntPtr dwSize, uint flNewProtect, out uint lpflOldProtect);

        [DllImport("kernel32.dll")]
        private static extern uint GetCurrentProcessId();

        [DllImport("user32.dll")]
        private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

        [DllImport("user32.dll")]
        private static extern bool IsWindowVisible(IntPtr hWnd);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetClassNameW(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);

        [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")]
        private static extern IntPtr SetWindowLongPtr64(IntPtr hWnd, int nIndex, IntPtr dwNewLong);

        [DllImport("user32.dll", EntryPoint = "SetWindowLongW")]
        private static extern IntPtr SetWindowLong32(IntPtr hWnd, int nIndex, IntPtr dwNewLong);

        [DllImport("user32.dll")]
        private static extern IntPtr CallWindowProcW(IntPtr lpPrevWndFunc, IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern bool PostMessageW(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

        private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate IntPtr WndProcDelegate(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate void Il2CppStaticVoidMethodDelegate(IntPtr methodInfo);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate void Il2CppStaticOneArgMethodDelegate(IntPtr arg0, IntPtr methodInfo);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate IntPtr Il2CppDomainGetDelegate();

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate IntPtr Il2CppThreadAttachDelegate(IntPtr domain);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate IntPtr Il2CppDomainGetAssembliesDelegate(IntPtr domain, out UIntPtr size);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate IntPtr Il2CppAssemblyGetImageDelegate(IntPtr assembly);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate IntPtr Il2CppImageGetNameDelegate(IntPtr image);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
        private delegate IntPtr Il2CppClassFromNameDelegate(IntPtr image, string namespaze, string name);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
        private delegate IntPtr Il2CppClassGetMethodFromNameDelegate(IntPtr klass, string name, int argsCount);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
        private delegate IntPtr Il2CppClassGetFieldFromNameDelegate(IntPtr klass, string name);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate IntPtr Il2CppClassGetParentDelegate(IntPtr klass);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate IntPtr Il2CppClassGetTypeDelegate(IntPtr klass);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate IntPtr Il2CppTypeGetObjectDelegate(IntPtr type);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate void Il2CppFieldGetValueDelegate(IntPtr obj, IntPtr field, IntPtr valuePtr);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate void Il2CppFieldSetValueDelegate(IntPtr obj, IntPtr field, IntPtr valuePtr);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate void Il2CppFieldStaticGetValueDelegate(IntPtr field, IntPtr valuePtr);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate IntPtr Il2CppRuntimeInvokeDelegate(IntPtr method, IntPtr obj, IntPtr paramsPtr, ref IntPtr exc);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate uint Il2CppArrayLengthDelegate(IntPtr array);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate int Il2CppArrayObjectHeaderSizeDelegate();

        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
        private delegate IntPtr Il2CppStringNewDelegate(string text);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet = CharSet.Unicode)]
        private delegate IntPtr Il2CppStringNewUtf16Delegate(string text, int len);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate IntPtr Il2CppStringCharsDelegate(IntPtr str);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate int Il2CppStringLengthDelegate(IntPtr str);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate IntPtr Il2CppObjectGetClassDelegate(IntPtr obj);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate IntPtr Il2CppObjectNewDelegate(IntPtr klass);

        private static bool _bound;
        public static bool IsAttached;
        private static IntPtr _domain;
        private static int _nativeOverrideAttempted;
        private static int _nativeErrorLogged;

        private static Action _mainThreadCallback;
        private static Action<string> _mainThreadLog;
        private static int _mainThreadHookState; // 0 = not installed, 1 = MethodInfo hook, 2 = WndProc hook
        private static int _mainThreadHookInstallAttempts;
        private static volatile bool _mainThreadHookFired;
        private static int _lastMainThreadTickMs;
        private static int _inMainThreadTick;
        private static IntPtr _hookedMethodInfo;
        private static IntPtr _origMethodPtr;
        private static Il2CppStaticVoidMethodDelegate _origStaticVoidDelegate;
        private static Il2CppStaticOneArgMethodDelegate _origStaticOneArgDelegate;
        private static Il2CppStaticVoidMethodDelegate _pinnedStaticVoidHook;
        private static Il2CppStaticOneArgMethodDelegate _pinnedStaticOneArgHook;
        private static IntPtr _gameWindowHwnd;
        private static IntPtr _origWndProc;
        private static WndProcDelegate _pinnedWndProcHook;
        private static bool _nativeTmpFallbackAttached;
        private static bool _nativeTmpFallbackAttempted;
        private static IntPtr _nativeArabicFontAsset;
        private static IntPtr _nativeArabicOsFont;
        private const uint WM_ARABIC_MAIN_TICK = 0x8000 + 0x4152;

        private static Il2CppDomainGetDelegate _domainGet;
        private static Il2CppThreadAttachDelegate _threadAttach;
        private static Il2CppDomainGetAssembliesDelegate _domainGetAssemblies;
        private static Il2CppAssemblyGetImageDelegate _assemblyGetImage;
        private static Il2CppImageGetNameDelegate _imageGetName;
        private static Il2CppClassFromNameDelegate _classFromName;
        private static Il2CppClassGetMethodFromNameDelegate _classGetMethod;
        private static Il2CppClassGetFieldFromNameDelegate _classGetField;
        private static Il2CppClassGetParentDelegate _classGetParent;
        private static Il2CppClassGetTypeDelegate _classGetType;
        private static Il2CppTypeGetObjectDelegate _typeGetObject;
        private static Il2CppFieldGetValueDelegate _fieldGet;
        private static Il2CppFieldSetValueDelegate _fieldSet;
        private static Il2CppFieldStaticGetValueDelegate _fieldStaticGet;
        private static Il2CppRuntimeInvokeDelegate _runtimeInvoke;
        private static Il2CppArrayLengthDelegate _arrayLength;
        private static Il2CppArrayObjectHeaderSizeDelegate _arrayHeaderSize;
        private static Il2CppStringNewDelegate _stringNew;
        private static Il2CppStringNewUtf16Delegate _stringNewUtf16;
        private static Il2CppStringCharsDelegate _stringChars;
        private static Il2CppStringLengthDelegate _stringLength;
        private static Il2CppObjectGetClassDelegate _objectGetClass;
        private static Il2CppObjectNewDelegate _objectNew;

        private static Delegate BindExport(IntPtr module, string exportName, Type delegateType)
        {
            IntPtr proc = GetProcAddress(module, exportName);
            if (proc == IntPtr.Zero)
            {
                return null;
            }
            return Marshal.GetDelegateForFunctionPointer(proc, delegateType);
        }

        public static bool TryBindAndAttach(Action<string> log)
        {
            if (IsAttached)
            {
                return true;
            }
            IntPtr gameAsm = GetModuleHandleA("GameAssembly.dll");
            if (gameAsm == IntPtr.Zero)
            {
                return false;
            }

            if (!_bound)
            {
                _domainGet = BindExport(gameAsm, "il2cpp_domain_get", typeof(Il2CppDomainGetDelegate)) as Il2CppDomainGetDelegate;
                _threadAttach = BindExport(gameAsm, "il2cpp_thread_attach", typeof(Il2CppThreadAttachDelegate)) as Il2CppThreadAttachDelegate;
                _domainGetAssemblies = BindExport(gameAsm, "il2cpp_domain_get_assemblies", typeof(Il2CppDomainGetAssembliesDelegate)) as Il2CppDomainGetAssembliesDelegate;
                _assemblyGetImage = BindExport(gameAsm, "il2cpp_assembly_get_image", typeof(Il2CppAssemblyGetImageDelegate)) as Il2CppAssemblyGetImageDelegate;
                _imageGetName = BindExport(gameAsm, "il2cpp_image_get_name", typeof(Il2CppImageGetNameDelegate)) as Il2CppImageGetNameDelegate;
                _classFromName = BindExport(gameAsm, "il2cpp_class_from_name", typeof(Il2CppClassFromNameDelegate)) as Il2CppClassFromNameDelegate;
                _classGetMethod = BindExport(gameAsm, "il2cpp_class_get_method_from_name", typeof(Il2CppClassGetMethodFromNameDelegate)) as Il2CppClassGetMethodFromNameDelegate;
                _classGetField = BindExport(gameAsm, "il2cpp_class_get_field_from_name", typeof(Il2CppClassGetFieldFromNameDelegate)) as Il2CppClassGetFieldFromNameDelegate;
                _classGetParent = BindExport(gameAsm, "il2cpp_class_get_parent", typeof(Il2CppClassGetParentDelegate)) as Il2CppClassGetParentDelegate;
                _classGetType = BindExport(gameAsm, "il2cpp_class_get_type", typeof(Il2CppClassGetTypeDelegate)) as Il2CppClassGetTypeDelegate;
                _typeGetObject = BindExport(gameAsm, "il2cpp_type_get_object", typeof(Il2CppTypeGetObjectDelegate)) as Il2CppTypeGetObjectDelegate;
                _fieldGet = BindExport(gameAsm, "il2cpp_field_get_value", typeof(Il2CppFieldGetValueDelegate)) as Il2CppFieldGetValueDelegate;
                _fieldSet = BindExport(gameAsm, "il2cpp_field_set_value", typeof(Il2CppFieldSetValueDelegate)) as Il2CppFieldSetValueDelegate;
                _fieldStaticGet = BindExport(gameAsm, "il2cpp_field_static_get_value", typeof(Il2CppFieldStaticGetValueDelegate)) as Il2CppFieldStaticGetValueDelegate;
                _runtimeInvoke = BindExport(gameAsm, "il2cpp_runtime_invoke", typeof(Il2CppRuntimeInvokeDelegate)) as Il2CppRuntimeInvokeDelegate;
                _arrayLength = BindExport(gameAsm, "il2cpp_array_length", typeof(Il2CppArrayLengthDelegate)) as Il2CppArrayLengthDelegate;
                _arrayHeaderSize = BindExport(gameAsm, "il2cpp_array_object_header_size", typeof(Il2CppArrayObjectHeaderSizeDelegate)) as Il2CppArrayObjectHeaderSizeDelegate;
                _stringNew = BindExport(gameAsm, "il2cpp_string_new", typeof(Il2CppStringNewDelegate)) as Il2CppStringNewDelegate;
                _stringNewUtf16 = BindExport(gameAsm, "il2cpp_string_new_utf16", typeof(Il2CppStringNewUtf16Delegate)) as Il2CppStringNewUtf16Delegate;
                _stringChars = BindExport(gameAsm, "il2cpp_string_chars", typeof(Il2CppStringCharsDelegate)) as Il2CppStringCharsDelegate;
                _stringLength = BindExport(gameAsm, "il2cpp_string_length", typeof(Il2CppStringLengthDelegate)) as Il2CppStringLengthDelegate;
                _objectGetClass = BindExport(gameAsm, "il2cpp_object_get_class", typeof(Il2CppObjectGetClassDelegate)) as Il2CppObjectGetClassDelegate;
                _objectNew = BindExport(gameAsm, "il2cpp_object_new", typeof(Il2CppObjectNewDelegate)) as Il2CppObjectNewDelegate;
                _bound = true;
            }

            if (_domainGet == null || _threadAttach == null)
            {
                return false;
            }

            IntPtr domain = _domainGet();
            if (domain == IntPtr.Zero)
            {
                return false;
            }

            _domain = domain;
            _threadAttach(domain);
            IsAttached = true;
            if (log != null)
            {
                log("Attached to GameAssembly.dll IL2CPP domain successfully.");
            }
            return true;
        }

        public static IntPtr FindClass(string namespaze, string className)
        {
            if (!IsAttached || _domain == IntPtr.Zero || _domainGetAssemblies == null || _assemblyGetImage == null || _classFromName == null)
            {
                return IntPtr.Zero;
            }
            UIntPtr countVal;
            IntPtr assembliesPtr = _domainGetAssemblies(_domain, out countVal);
            int count = (int)(ulong)countVal;
            if (assembliesPtr == IntPtr.Zero || count <= 0)
            {
                return IntPtr.Zero;
            }
            for (int i = 0; i < count; i++)
            {
                IntPtr asm = Marshal.ReadIntPtr(assembliesPtr, i * IntPtr.Size);
                if (asm == IntPtr.Zero) continue;
                IntPtr img = _assemblyGetImage(asm);
                if (img == IntPtr.Zero) continue;
                IntPtr klass = _classFromName(img, namespaze, className);
                if (klass != IntPtr.Zero)
                {
                    return klass;
                }
            }
            return IntPtr.Zero;
        }

        public static IntPtr FindFieldInHierarchy(IntPtr klass, string fieldName)
        {
            if (_classGetField == null || string.IsNullOrEmpty(fieldName))
            {
                return IntPtr.Zero;
            }
            IntPtr cur = klass;
            while (cur != IntPtr.Zero)
            {
                IntPtr fld = _classGetField(cur, fieldName);
                if (fld != IntPtr.Zero)
                {
                    return fld;
                }
                cur = _classGetParent != null ? _classGetParent(cur) : IntPtr.Zero;
            }
            return IntPtr.Zero;
        }

        public static IntPtr FindMethodInHierarchy(IntPtr klass, string methodName, int argsCount)
        {
            if (_classGetMethod == null || string.IsNullOrEmpty(methodName))
            {
                return IntPtr.Zero;
            }
            IntPtr cur = klass;
            while (cur != IntPtr.Zero)
            {
                IntPtr m = _classGetMethod(cur, methodName, argsCount);
                if (m != IntPtr.Zero)
                {
                    return m;
                }
                cur = _classGetParent != null ? _classGetParent(cur) : IntPtr.Zero;
            }
            return IntPtr.Zero;
        }

        private static IntPtr InvokeMethod(IntPtr method, IntPtr obj, IntPtr[] args, out IntPtr exc)
        {
            exc = IntPtr.Zero;
            if (method == IntPtr.Zero || _runtimeInvoke == null)
            {
                return IntPtr.Zero;
            }
            IntPtr paramsPtr = IntPtr.Zero;
            try
            {
                if (args != null && args.Length > 0)
                {
                    paramsPtr = Marshal.AllocHGlobal(args.Length * IntPtr.Size);
                    for (int i = 0; i < args.Length; i++)
                    {
                        Marshal.WriteIntPtr(paramsPtr, i * IntPtr.Size, args[i]);
                    }
                }
                return _runtimeInvoke(method, obj, paramsPtr, ref exc);
            }
            finally
            {
                if (paramsPtr != IntPtr.Zero)
                {
                    Marshal.FreeHGlobal(paramsPtr);
                }
            }
        }

        public static string ReadIl2CppString(IntPtr strObj)
        {
            if (strObj == IntPtr.Zero || _stringChars == null || _stringLength == null)
            {
                return null;
            }
            int len = _stringLength(strObj);
            if (len <= 0)
            {
                return string.Empty;
            }
            IntPtr charsPtr = _stringChars(strObj);
            if (charsPtr == IntPtr.Zero)
            {
                return null;
            }
            return Marshal.PtrToStringUni(charsPtr, len);
        }

        public static IntPtr NewIl2CppString(string text)
        {
            if (text == null)
            {
                return IntPtr.Zero;
            }
            if (_stringNewUtf16 != null)
            {
                return _stringNewUtf16(text, text.Length);
            }
            if (_stringNew != null)
            {
                return _stringNew(text);
            }
            return IntPtr.Zero;
        }

        public static void ApplyNativeI2LocalizationOverrides(OverrideTranslationStore store, Action<string> log)
        {
            if (!IsAttached || store == null || !store.HasOverrides || _domain == IntPtr.Zero || _threadAttach == null)
            {
                return;
            }
            if (_fieldStaticGet == null || _fieldGet == null || _arrayLength == null || _arrayHeaderSize == null)
            {
                return;
            }

            try
            {
                _threadAttach(_domain);
                IntPtr locMgr = FindClass("I2.Loc", "LocalizationManager");
                if (locMgr == IntPtr.Zero)
                {
                    return;
                }

                IntPtr sourcesField = FindFieldInHierarchy(locMgr, "Sources");
                if (sourcesField == IntPtr.Zero)
                {
                    return;
                }

                IntPtr ptrBuf = Marshal.AllocHGlobal(IntPtr.Size);
                IntPtr intBuf = Marshal.AllocHGlobal(4);
                try
                {
                    Marshal.WriteIntPtr(ptrBuf, IntPtr.Zero);
                    _fieldStaticGet(sourcesField, ptrBuf);
                    IntPtr sourcesList = Marshal.ReadIntPtr(ptrBuf);
                    if (sourcesList == IntPtr.Zero)
                    {
                        return;
                    }

                    IntPtr listItemsField = FindFieldInHierarchy(FindClass("System.Collections.Generic", "List`1"), "_items");
                    IntPtr listSizeField = FindFieldInHierarchy(FindClass("System.Collections.Generic", "List`1"), "_size");
                    if (listItemsField == IntPtr.Zero || listSizeField == IntPtr.Zero)
                    {
                        return;
                    }

                    Marshal.WriteIntPtr(ptrBuf, IntPtr.Zero);
                    _fieldGet(sourcesList, listItemsField, ptrBuf);
                    IntPtr sourcesArr = Marshal.ReadIntPtr(ptrBuf);
                    Marshal.WriteInt32(intBuf, 0);
                    _fieldGet(sourcesList, listSizeField, intBuf);
                    int srcCount = Marshal.ReadInt32(intBuf);
                    if (sourcesArr == IntPtr.Zero || srcCount <= 0)
                    {
                        return;
                    }

                    int headerSz = _arrayHeaderSize();
                    int applied = 0;
                    for (int s = 0; s < srcCount && s < (int)_arrayLength(sourcesArr); s++)
                    {
                        IntPtr srcObj = Marshal.ReadIntPtr(sourcesArr, headerSz + s * IntPtr.Size);
                        if (srcObj == IntPtr.Zero) continue;

                        IntPtr srcClass = FindClass("I2.Loc", "LanguageSourceData");
                        if (srcClass == IntPtr.Zero) continue;
                        IntPtr termsField = FindFieldInHierarchy(srcClass, "mTerms");
                        if (termsField == IntPtr.Zero) continue;

                        Marshal.WriteIntPtr(ptrBuf, IntPtr.Zero);
                        _fieldGet(srcObj, termsField, ptrBuf);
                        IntPtr termsList = Marshal.ReadIntPtr(ptrBuf);
                        if (termsList == IntPtr.Zero) continue;

                        Marshal.WriteIntPtr(ptrBuf, IntPtr.Zero);
                        _fieldGet(termsList, listItemsField, ptrBuf);
                        IntPtr termsArr = Marshal.ReadIntPtr(ptrBuf);
                        Marshal.WriteInt32(intBuf, 0);
                        _fieldGet(termsList, listSizeField, intBuf);
                        int termCount = Marshal.ReadInt32(intBuf);
                        if (termsArr == IntPtr.Zero || termCount <= 0) continue;

                        IntPtr termDataClass = FindClass("I2.Loc", "TermData");
                        if (termDataClass == IntPtr.Zero) continue;
                        IntPtr termStrField = FindFieldInHierarchy(termDataClass, "Term");
                        IntPtr termTypeField = FindFieldInHierarchy(termDataClass, "TermType");
                        IntPtr langsField = FindFieldInHierarchy(termDataClass, "Languages");
                        if (termStrField == IntPtr.Zero || langsField == IntPtr.Zero) continue;

                        for (int t = 0; t < termCount && t < (int)_arrayLength(termsArr); t++)
                        {
                            IntPtr termObj = Marshal.ReadIntPtr(termsArr, headerSz + t * IntPtr.Size);
                            if (termObj == IntPtr.Zero) continue;

                            if (termTypeField != IntPtr.Zero)
                            {
                                Marshal.WriteInt32(intBuf, 0);
                                _fieldGet(termObj, termTypeField, intBuf);
                                if (Marshal.ReadInt32(intBuf) != 0) continue;
                            }

                            Marshal.WriteIntPtr(ptrBuf, IntPtr.Zero);
                            _fieldGet(termObj, termStrField, ptrBuf);
                            string termKey = ReadIl2CppString(Marshal.ReadIntPtr(ptrBuf));

                            Marshal.WriteIntPtr(ptrBuf, IntPtr.Zero);
                            _fieldGet(termObj, langsField, ptrBuf);
                            IntPtr langsArr = Marshal.ReadIntPtr(ptrBuf);
                            if (langsArr == IntPtr.Zero) continue;
                            int langLen = (int)_arrayLength(langsArr);
                            if (langLen <= 0) continue;

                            IntPtr firstValPtr = Marshal.ReadIntPtr(langsArr, headerSz);
                            string firstVal = ReadIl2CppString(firstValPtr);
                            string translated = null;
                            bool found = false;
                            if (!string.IsNullOrEmpty(termKey) && store.ByKey != null)
                            {
                                found = store.ByKey.TryGetValue(termKey, out translated);
                                if (!found)
                                {
                                    int slashIdx = termKey.LastIndexOf('/');
                                    if (slashIdx >= 0 && slashIdx + 1 < termKey.Length)
                                    {
                                        found = store.ByKey.TryGetValue(termKey.Substring(slashIdx + 1), out translated);
                                    }
                                }
                            }
                            if (!found)
                            {
                                found = store.TryTranslateRuntimeText(firstVal, out translated);
                            }
                            if (!found || string.IsNullOrEmpty(translated))
                            {
                                continue;
                            }
                            IntPtr newPtr = NewIl2CppString(translated);
                            if (newPtr == IntPtr.Zero) continue;

                            for (int l = 0; l < langLen; l++)
                            {
                                Marshal.WriteIntPtr(langsArr, headerSz + l * IntPtr.Size, newPtr);
                            }
                            applied++;
                        }
                    }

                    if (applied > 0 && Interlocked.Exchange(ref _nativeOverrideAttempted, 1) == 0 && log != null)
                    {
                        log("[IL2CPP] Native I2.Loc override pass updated " + applied + " terms.");
                    }
                }
                finally
                {
                    Marshal.FreeHGlobal(ptrBuf);
                    Marshal.FreeHGlobal(intBuf);
                }
            }
            catch (Exception ex)
            {
                if (Interlocked.Exchange(ref _nativeErrorLogged, 1) == 0 && log != null)
                {
                    log("WARNING in ApplyNativeI2LocalizationOverrides: " + ex.Message);
                }
            }
        }

        public static void TryInstallMainThreadHook(Action callback, Action<string> log)
        {
            if (!IsAttached || callback == null)
            {
                return;
            }
            _mainThreadCallback = callback;
            _mainThreadLog = log;

            if (_mainThreadHookState == 2 && _gameWindowHwnd != IntPtr.Zero)
            {
                PostMessageW(_gameWindowHwnd, WM_ARABIC_MAIN_TICK, IntPtr.Zero, IntPtr.Zero);
                return;
            }

            if (_mainThreadHookState == 1)
            {
                if (_mainThreadHookFired)
                {
                    return;
                }
                if (Interlocked.Increment(ref _mainThreadHookInstallAttempts) < 3)
                {
                    return;
                }
                if (TryInstallWndProcHook(log))
                {
                    PostMessageW(_gameWindowHwnd, WM_ARABIC_MAIN_TICK, IntPtr.Zero, IntPtr.Zero);
                }
                return;
            }

            if (TryInstallMethodInfoHook(log))
            {
                return;
            }

            if (TryInstallWndProcHook(log))
            {
                PostMessageW(_gameWindowHwnd, WM_ARABIC_MAIN_TICK, IntPtr.Zero, IntPtr.Zero);
            }
        }

        private static bool TryInstallMethodInfoHook(Action<string> log)
        {
            string[][] targets = new string[][]
            {
                new string[] { "UnityEngine", "Canvas", "SendWillRenderCanvases", "0" },
                new string[] { "UnityEngine", "Application", "InvokeOnBeforeRender", "0" },
                new string[] { "UnityEngine", "Camera", "FireOnPreRender", "1" }
            };

            for (int i = 0; i < targets.Length; i++)
            {
                string ns = targets[i][0];
                string cls = targets[i][1];
                string mName = targets[i][2];
                int argCount = targets[i][3] == "1" ? 1 : 0;

                IntPtr klass = FindClass(ns, cls);
                if (klass == IntPtr.Zero)
                {
                    continue;
                }
                IntPtr methodInfo = FindMethodInHierarchy(klass, mName, argCount);
                if (methodInfo == IntPtr.Zero)
                {
                    continue;
                }

                IntPtr origPtr = Marshal.ReadIntPtr(methodInfo);
                if (origPtr == IntPtr.Zero)
                {
                    continue;
                }

                IntPtr hookPtr;
                if (argCount == 0)
                {
                    _origStaticVoidDelegate = Marshal.GetDelegateForFunctionPointer(
                        origPtr, typeof(Il2CppStaticVoidMethodDelegate)
                    ) as Il2CppStaticVoidMethodDelegate;
                    _pinnedStaticVoidHook = new Il2CppStaticVoidMethodDelegate(HookedStaticVoidMethod);
                    hookPtr = Marshal.GetFunctionPointerForDelegate(_pinnedStaticVoidHook);
                }
                else
                {
                    _origStaticOneArgDelegate = Marshal.GetDelegateForFunctionPointer(
                        origPtr, typeof(Il2CppStaticOneArgMethodDelegate)
                    ) as Il2CppStaticOneArgMethodDelegate;
                    _pinnedStaticOneArgHook = new Il2CppStaticOneArgMethodDelegate(HookedStaticOneArgMethod);
                    hookPtr = Marshal.GetFunctionPointerForDelegate(_pinnedStaticOneArgHook);
                }

                uint oldProt;
                VirtualProtect(methodInfo, (UIntPtr)(uint)IntPtr.Size, 0x04u, out oldProt);
                Marshal.WriteIntPtr(methodInfo, hookPtr);
                _hookedMethodInfo = methodInfo;
                _origMethodPtr = origPtr;
                _mainThreadHookState = 1;
                if (log != null)
                {
                    log("Installed Main-Thread MethodInfo hook on " + ns + "." + cls + "." + mName + ".");
                }
                return true;
            }
            return false;
        }

        private static void HookedStaticVoidMethod(IntPtr methodInfo)
        {
            if (_origStaticVoidDelegate != null)
            {
                _origStaticVoidDelegate(methodInfo);
            }
            _mainThreadHookFired = true;
            InvokeThrottledMainThreadCallback();
        }

        private static void HookedStaticOneArgMethod(IntPtr arg0, IntPtr methodInfo)
        {
            if (_origStaticOneArgDelegate != null)
            {
                _origStaticOneArgDelegate(arg0, methodInfo);
            }
            _mainThreadHookFired = true;
            InvokeThrottledMainThreadCallback();
        }

        private static bool TryInstallWndProcHook(Action<string> log)
        {
            IntPtr hwnd = FindUnityMainWindow();
            if (hwnd == IntPtr.Zero)
            {
                return false;
            }

            _pinnedWndProcHook = new WndProcDelegate(HookedWndProc);
            IntPtr hookPtr = Marshal.GetFunctionPointerForDelegate(_pinnedWndProcHook);
            IntPtr prev = IntPtr.Size == 8
                ? SetWindowLongPtr64(hwnd, -4, hookPtr)
                : SetWindowLong32(hwnd, -4, hookPtr);
            if (prev == IntPtr.Zero)
            {
                return false;
            }

            _gameWindowHwnd = hwnd;
            _origWndProc = prev;
            _mainThreadHookState = 2;
            if (log != null)
            {
                log("Installed Main-Thread WndProc hook on Unity game window.");
            }
            return true;
        }

        private static IntPtr FindUnityMainWindow()
        {
            uint pid = GetCurrentProcessId();
            IntPtr unityHwnd = IntPtr.Zero;
            IntPtr anyHwnd = IntPtr.Zero;
            EnumWindows(delegate(IntPtr hWnd, IntPtr lParam)
            {
                uint winPid;
                GetWindowThreadProcessId(hWnd, out winPid);
                if (winPid != pid || !IsWindowVisible(hWnd))
                {
                    return true;
                }
                StringBuilder sb = new StringBuilder(128);
                GetClassNameW(hWnd, sb, sb.Capacity);
                string cls = sb.ToString();
                if (string.Equals(cls, "UnityWndClass", StringComparison.Ordinal))
                {
                    unityHwnd = hWnd;
                    return false;
                }
                if (anyHwnd == IntPtr.Zero)
                {
                    anyHwnd = hWnd;
                }
                return true;
            }, IntPtr.Zero);
            return unityHwnd != IntPtr.Zero ? unityHwnd : anyHwnd;
        }

        private static IntPtr HookedWndProc(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam)
        {
            if (msg == WM_ARABIC_MAIN_TICK)
            {
                _mainThreadHookFired = true;
                if (IsAttached && _domain != IntPtr.Zero && _threadAttach != null)
                {
                    _threadAttach(_domain);
                }
                InvokeThrottledMainThreadCallback();
                return IntPtr.Zero;
            }
            return CallWindowProcW(_origWndProc, hWnd, msg, wParam, lParam);
        }

        private static void InvokeThrottledMainThreadCallback()
        {
            int now = Environment.TickCount;
            int elapsed = unchecked(now - _lastMainThreadTickMs);
            if (_lastMainThreadTickMs != 0 && elapsed >= 0 && elapsed < 500)
            {
                return;
            }
            if (Interlocked.CompareExchange(ref _inMainThreadTick, 1, 0) != 0)
            {
                return;
            }
            try
            {
                _lastMainThreadTickMs = now;
                Action cb = _mainThreadCallback;
                if (cb != null)
                {
                    cb();
                }
            }
            catch (Exception ex)
            {
                if (Interlocked.Exchange(ref _nativeErrorLogged, 1) == 0 && _mainThreadLog != null)
                {
                    _mainThreadLog("WARNING in Main-Thread callback: " + ex.Message);
                }
            }
            finally
            {
                Interlocked.Exchange(ref _inMainThreadTick, 0);
            }
        }

        private static IntPtr CreateNativeTmpFontAsset(IntPtr tmpFontAssetClass, IntPtr unityFontClass, Action<string> log)
        {
            string bundledFont = OverrideTranslationStore.FindAndRegisterBundledArabicFont(log);
            // 1) Try TMP_FontAsset.CreateFontAsset(string fontFilePath, int faceIndex, int samplingPointSize, int atlasPadding, GlyphRenderMode renderMode, int atlasWidth, int atlasHeight)
            IntPtr create7 = FindMethodInHierarchy(tmpFontAssetClass, "CreateFontAsset", 7);
            if (create7 != IntPtr.Zero)
            {
                string winFonts = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "Fonts");
                string[] ttfCandidates = new string[]
                {
                    bundledFont,
                    Path.Combine(winFonts, "arial.ttf"),
                    Path.Combine(winFonts, "tahoma.ttf"),
                    Path.Combine(winFonts, "segoeui.ttf")
                };
                IntPtr intBuf = Marshal.AllocHGlobal(24);
                try
                {
                    Marshal.WriteInt32(intBuf, 0, 0);      // faceIndex
                    Marshal.WriteInt32(intBuf, 4, 64);     // samplingPointSize
                    Marshal.WriteInt32(intBuf, 8, 6);      // atlasPadding
                    Marshal.WriteInt32(intBuf, 12, 4165);  // GlyphRenderMode.SDFAA
                    Marshal.WriteInt32(intBuf, 16, 1024);  // atlasWidth
                    Marshal.WriteInt32(intBuf, 20, 1024);  // atlasHeight

                    for (int i = 0; i < ttfCandidates.Length; i++)
                    {
                        if (string.IsNullOrEmpty(ttfCandidates[i]) || !File.Exists(ttfCandidates[i])) continue;
                        IntPtr pathStr = NewIl2CppString(ttfCandidates[i]);
                        if (pathStr == IntPtr.Zero) continue;
                        IntPtr exc;
                        IntPtr fa = InvokeMethod(
                            create7,
                            IntPtr.Zero,
                            new IntPtr[]
                            {
                                pathStr,
                                intBuf,
                                (IntPtr)((long)intBuf + 4),
                                (IntPtr)((long)intBuf + 8),
                                (IntPtr)((long)intBuf + 12),
                                (IntPtr)((long)intBuf + 16),
                                (IntPtr)((long)intBuf + 20)
                            },
                            out exc
                        );
                        if (exc == IntPtr.Zero && fa != IntPtr.Zero)
                        {
                            return fa;
                        }
                    }
                }
                finally
                {
                    Marshal.FreeHGlobal(intBuf);
                }
            }

            // 2) Try TMP_FontAsset.CreateFontAsset(string familyName, string styleName, int pointSize)
            IntPtr create3 = FindMethodInHierarchy(tmpFontAssetClass, "CreateFontAsset", 3);
            if (create3 != IntPtr.Zero)
            {
                IntPtr sizePtr = Marshal.AllocHGlobal(4);
                Marshal.WriteInt32(sizePtr, 64);
                try
                {
                    string[] families = new string[] { "IBM Plex Sans Arabic", "Arial", "Tahoma", "Segoe UI" };
                    IntPtr regularStr = NewIl2CppString("Regular");
                    for (int i = 0; i < families.Length; i++)
                    {
                        IntPtr famStr = NewIl2CppString(families[i]);
                        if (famStr == IntPtr.Zero) continue;
                        IntPtr exc;
                        IntPtr fa = InvokeMethod(create3, IntPtr.Zero, new IntPtr[] { famStr, regularStr, sizePtr }, out exc);
                        if (exc == IntPtr.Zero && fa != IntPtr.Zero)
                        {
                            return fa;
                        }
                    }
                }
                finally
                {
                    Marshal.FreeHGlobal(sizePtr);
                }
            }

            // 3) Try creating UnityEngine.Font + TMP_FontAsset.CreateFontAsset(Font)
            IntPtr create1 = FindMethodInHierarchy(tmpFontAssetClass, "CreateFontAsset", 1);
            if (create1 == IntPtr.Zero)
            {
                return IntPtr.Zero;
            }

            if (_nativeArabicOsFont == IntPtr.Zero)
            {
                string[] fontNames = new string[] { "IBM Plex Sans Arabic", "Arial", "Tahoma", "Segoe UI", "Microsoft Sans Serif" };
                IntPtr createOsFont = FindMethodInHierarchy(unityFontClass, "CreateDynamicFontFromOSFont", 2);
                if (createOsFont != IntPtr.Zero)
                {
                    IntPtr sizePtr = Marshal.AllocHGlobal(4);
                    Marshal.WriteInt32(sizePtr, 32);
                    try
                    {
                        for (int i = 0; i < fontNames.Length; i++)
                        {
                            IntPtr fnStr = NewIl2CppString(fontNames[i]);
                            if (fnStr == IntPtr.Zero) continue;
                            IntPtr fExc;
                            IntPtr osFont = InvokeMethod(createOsFont, IntPtr.Zero, new IntPtr[] { fnStr, sizePtr }, out fExc);
                            if (fExc == IntPtr.Zero && osFont != IntPtr.Zero)
                            {
                                _nativeArabicOsFont = osFont;
                                break;
                            }
                        }
                    }
                    finally
                    {
                        Marshal.FreeHGlobal(sizePtr);
                    }
                }

                if (_nativeArabicOsFont == IntPtr.Zero && _objectNew != null)
                {
                    IntPtr ctorStr = FindMethodInHierarchy(unityFontClass, ".ctor", 1);
                    if (ctorStr != IntPtr.Zero)
                    {
                        for (int i = 0; i < fontNames.Length; i++)
                        {
                            IntPtr fontObj = _objectNew(unityFontClass);
                            if (fontObj == IntPtr.Zero) break;
                            IntPtr cExc;
                            InvokeMethod(ctorStr, fontObj, new IntPtr[] { NewIl2CppString(fontNames[i]) }, out cExc);
                            if (cExc == IntPtr.Zero)
                            {
                                _nativeArabicOsFont = fontObj;
                                break;
                            }
                        }
                    }
                }

                if (_nativeArabicOsFont == IntPtr.Zero && _objectNew != null)
                {
                    IntPtr ctor0 = FindMethodInHierarchy(unityFontClass, ".ctor", 0);
                    IntPtr fromPath = FindMethodInHierarchy(unityFontClass, "Internal_CreateFontFromPath", 2);
                    if (fromPath != IntPtr.Zero)
                    {
                        string winFonts = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "Fonts");
                        string fallbackPath = (!string.IsNullOrEmpty(bundledFont) && File.Exists(bundledFont))
                            ? bundledFont
                            : Path.Combine(winFonts, "arial.ttf");
                        if (File.Exists(fallbackPath))
                        {
                            IntPtr fontObj = _objectNew(unityFontClass);
                            if (fontObj != IntPtr.Zero)
                            {
                                if (ctor0 != IntPtr.Zero)
                                {
                                    IntPtr c0Exc;
                                    InvokeMethod(ctor0, fontObj, null, out c0Exc);
                                }
                                IntPtr pExc;
                                InvokeMethod(fromPath, IntPtr.Zero, new IntPtr[] { fontObj, NewIl2CppString(fallbackPath) }, out pExc);
                                if (pExc == IntPtr.Zero)
                                {
                                    _nativeArabicOsFont = fontObj;
                                }
                            }
                        }
                    }
                }
            }

            if (_nativeArabicOsFont != IntPtr.Zero)
            {
                IntPtr faExc;
                IntPtr fa = InvokeMethod(create1, IntPtr.Zero, new IntPtr[] { _nativeArabicOsFont }, out faExc);
                if (faExc == IntPtr.Zero && fa != IntPtr.Zero)
                {
                    return fa;
                }
            }

            if (log != null)
            {
                log("WARNING in CreateNativeTmpFontAsset: create7=" + (create7 != IntPtr.Zero) + ", create3=" + (create3 != IntPtr.Zero) + ", create1=" + (create1 != IntPtr.Zero) + ", osFont=" + (_nativeArabicOsFont != IntPtr.Zero));
            }
            return IntPtr.Zero;
        }

        public static bool EnsureNativeTmpFallbackFont(Action<string> log)
        {
            if (_nativeTmpFallbackAttached || _nativeTmpFallbackAttempted)
            {
                return false;
            }
            if (!IsAttached || _domain == IntPtr.Zero || _threadAttach == null || _runtimeInvoke == null)
            {
                return false;
            }

            try
            {
                _threadAttach(_domain);
                IntPtr tmpSettingsClass = FindClass("TMPro", "TMP_Settings");
                IntPtr tmpFontAssetClass = FindClass("TMPro", "TMP_FontAsset");
                IntPtr unityFontClass = FindClass("UnityEngine", "Font");
                if (tmpSettingsClass == IntPtr.Zero || tmpFontAssetClass == IntPtr.Zero || unityFontClass == IntPtr.Zero)
                {
                    return false;
                }

                IntPtr getInstanceMethod = FindMethodInHierarchy(tmpSettingsClass, "get_instance", 0);
                if (getInstanceMethod != IntPtr.Zero)
                {
                    IntPtr instExc;
                    InvokeMethod(getInstanceMethod, IntPtr.Zero, null, out instExc);
                }

                IntPtr getFallbackListMethod = FindMethodInHierarchy(tmpSettingsClass, "get_fallbackFontAssets", 0);
                if (getFallbackListMethod == IntPtr.Zero)
                {
                    _nativeTmpFallbackAttempted = true;
                    if (log != null) log("WARNING in EnsureNativeTmpFallbackFont: TMP_Settings.get_fallbackFontAssets not found.");
                    return false;
                }
                IntPtr listExc;
                IntPtr fallbackList = InvokeMethod(getFallbackListMethod, IntPtr.Zero, null, out listExc);
                if (listExc != IntPtr.Zero || fallbackList == IntPtr.Zero)
                {
                    if (log != null) log("WARNING in EnsureNativeTmpFallbackFont: fallbackFontAssets list is null or raised exception.");
                    return false;
                }

                if (_nativeArabicFontAsset == IntPtr.Zero)
                {
                    IntPtr fontAsset = CreateNativeTmpFontAsset(tmpFontAssetClass, unityFontClass, log);
                    if (fontAsset == IntPtr.Zero)
                    {
                        _nativeTmpFallbackAttempted = true;
                        if (log != null) log("WARNING in EnsureNativeTmpFallbackFont: Failed to create TMP_FontAsset from OS font.");
                        return false;
                    }
                    _nativeArabicFontAsset = fontAsset;

                    IntPtr unityObjClass = FindClass("UnityEngine", "Object");
                    if (unityObjClass != IntPtr.Zero)
                    {
                        IntPtr setNameMethod = FindMethodInHierarchy(unityObjClass, "set_name", 1);
                        if (setNameMethod != IntPtr.Zero)
                        {
                            IntPtr nExc;
                            InvokeMethod(setNameMethod, fontAsset, new IntPtr[] { NewIl2CppString("ArabicFallbackSDF") }, out nExc);
                        }
                        IntPtr ddolMethod = FindMethodInHierarchy(unityObjClass, "DontDestroyOnLoad", 1);
                        if (ddolMethod != IntPtr.Zero)
                        {
                            IntPtr dExc;
                            InvokeMethod(ddolMethod, IntPtr.Zero, new IntPtr[] { fontAsset }, out dExc);
                        }
                    }
                }

                IntPtr listClass = _objectGetClass != null ? _objectGetClass(fallbackList) : IntPtr.Zero;
                if (listClass == IntPtr.Zero)
                {
                    return false;
                }

                IntPtr insertMethod = FindMethodInHierarchy(listClass, "Insert", 2);
                IntPtr addMethod = FindMethodInHierarchy(listClass, "Add", 1);
                bool added = false;
                if (insertMethod != IntPtr.Zero)
                {
                    IntPtr idxPtr = Marshal.AllocHGlobal(4);
                    Marshal.WriteInt32(idxPtr, 0);
                    try
                    {
                        IntPtr insExc;
                        InvokeMethod(insertMethod, fallbackList, new IntPtr[] { idxPtr, _nativeArabicFontAsset }, out insExc);
                        added = (insExc == IntPtr.Zero);
                    }
                    finally
                    {
                        Marshal.FreeHGlobal(idxPtr);
                    }
                }
                if (!added && addMethod != IntPtr.Zero)
                {
                    IntPtr addExc;
                    InvokeMethod(addMethod, fallbackList, new IntPtr[] { _nativeArabicFontAsset }, out addExc);
                    added = (addExc == IntPtr.Zero);
                }

                if (added)
                {
                    _nativeTmpFallbackAttached = true;
                    if (log != null)
                    {
                        log("Created and attached native ArabicFallbackSDF to TMP_Settings.fallbackFontAssets.");
                    }
                    return true;
                }
            }
            catch (Exception ex)
            {
                _nativeTmpFallbackAttempted = true;
                if (log != null)
                {
                    log("WARNING in EnsureNativeTmpFallbackFont: " + ex.Message);
                }
            }
            return false;
        }

        public static void ForceAllNativeTmpMeshUpdates(Action<string> log)
        {
            if (!IsAttached || _domain == IntPtr.Zero || _threadAttach == null || _classGetType == null || _typeGetObject == null)
            {
                return;
            }
            try
            {
                _threadAttach(_domain);
                IntPtr resClass = FindClass("UnityEngine", "Resources");
                IntPtr findMethod = FindMethodInHierarchy(resClass, "FindObjectsOfTypeAll", 1);
                if (findMethod == IntPtr.Zero)
                {
                    IntPtr apiClass = FindClass("UnityEngine", "ResourcesAPIInternal");
                    findMethod = FindMethodInHierarchy(apiClass, "FindObjectsOfTypeAll", 1);
                }
                IntPtr compClass = FindClass("TMPro", "TMP_Text");
                if (findMethod == IntPtr.Zero || compClass == IntPtr.Zero)
                {
                    return;
                }
                IntPtr compType = _classGetType(compClass);
                IntPtr typeObj = compType != IntPtr.Zero ? _typeGetObject(compType) : IntPtr.Zero;
                if (typeObj == IntPtr.Zero)
                {
                    return;
                }
                IntPtr exc;
                IntPtr arr = InvokeMethod(findMethod, IntPtr.Zero, new IntPtr[] { typeObj }, out exc);
                if (exc != IntPtr.Zero || arr == IntPtr.Zero)
                {
                    return;
                }
                int count = (int)_arrayLength(arr);
                int headerSz = _arrayHeaderSize();
                IntPtr forceMeshMethod = FindMethodInHierarchy(compClass, "ForceMeshUpdate", 2);
                bool twoArgs = (forceMeshMethod != IntPtr.Zero);
                if (!twoArgs)
                {
                    forceMeshMethod = FindMethodInHierarchy(compClass, "ForceMeshUpdate", 0);
                }
                if (forceMeshMethod == IntPtr.Zero || count <= 0 || headerSz <= 0)
                {
                    return;
                }
                IntPtr truePtr = twoArgs ? Marshal.AllocHGlobal(1) : IntPtr.Zero;
                if (truePtr != IntPtr.Zero)
                {
                    Marshal.WriteByte(truePtr, 1);
                }
                try
                {
                    for (int i = 0; i < count; i++)
                    {
                        IntPtr obj = Marshal.ReadIntPtr(arr, headerSz + i * IntPtr.Size);
                        if (obj == IntPtr.Zero) continue;
                        IntPtr fmExc;
                        InvokeMethod(forceMeshMethod, obj, twoArgs ? new IntPtr[] { truePtr, truePtr } : null, out fmExc);
                    }
                }
                finally
                {
                    if (truePtr != IntPtr.Zero)
                    {
                        Marshal.FreeHGlobal(truePtr);
                    }
                }
            }
            catch (Exception ex)
            {
                if (log != null)
                {
                    log("WARNING in ForceAllNativeTmpMeshUpdates: " + ex.Message);
                }
            }
        }

        public static void ApplyNativeIl2CppOverrides(OverrideTranslationStore store, Action<string> log)
        {
            ApplyNativeIl2CppOverrides(store, log, false);
        }

        public static void ApplyNativeIl2CppOverrides(OverrideTranslationStore store, Action<string> log, bool forceRefreshTmpMesh)
        {
            if (!IsAttached || store == null || !store.HasOverrides || _domain == IntPtr.Zero || _threadAttach == null)
            {
                return;
            }
            if (_classGetType == null || _typeGetObject == null || _runtimeInvoke == null || _arrayLength == null || _arrayHeaderSize == null)
            {
                return;
            }

            try
            {
                _threadAttach(_domain);
                IntPtr resClass = FindClass("UnityEngine", "Resources");
                IntPtr findMethod = FindMethodInHierarchy(resClass, "FindObjectsOfTypeAll", 1);
                if (findMethod == IntPtr.Zero)
                {
                    IntPtr apiClass = FindClass("UnityEngine", "ResourcesAPIInternal");
                    findMethod = FindMethodInHierarchy(apiClass, "FindObjectsOfTypeAll", 1);
                }
                if (findMethod == IntPtr.Zero)
                {
                    return;
                }

                int totalReplaced = 0;
                totalReplaced += UpdateNativeComponentClass(store, findMethod, "TMPro", "TMP_Text", true, forceRefreshTmpMesh, log);
                totalReplaced += UpdateNativeComponentClass(store, findMethod, "UnityEngine.UI", "Text", false, false, log);
                totalReplaced += UpdateNativeComponentClass(store, findMethod, "UnityEngine", "TextMesh", false, false, log);
                if (totalReplaced > 0 && Interlocked.Exchange(ref _nativeOverrideAttempted, 1) == 0 && log != null)
                {
                    log("Native IL2CPP text override pass applied " + totalReplaced + " entries.");
                }
            }
            catch (Exception ex)
            {
                if (Interlocked.Exchange(ref _nativeErrorLogged, 1) == 0 && log != null)
                {
                    log("WARNING in ApplyNativeIl2CppOverrides: " + ex.Message);
                }
            }
        }

        private static int UpdateNativeComponentClass(
            OverrideTranslationStore store,
            IntPtr findMethod,
            string ns,
            string clsName,
            bool isTmp,
            bool forceRefreshAll,
            Action<string> log
        )
        {
            IntPtr compClass = FindClass(ns, clsName);
            if (compClass == IntPtr.Zero)
            {
                return 0;
            }
            IntPtr compType = _classGetType(compClass);
            IntPtr typeObj = compType != IntPtr.Zero ? _typeGetObject(compType) : IntPtr.Zero;
            if (typeObj == IntPtr.Zero)
            {
                return 0;
            }

            IntPtr exc;
            IntPtr arr = InvokeMethod(findMethod, IntPtr.Zero, new IntPtr[] { typeObj }, out exc);
            if (exc != IntPtr.Zero || arr == IntPtr.Zero)
            {
                if (exc != IntPtr.Zero && Interlocked.Exchange(ref _nativeErrorLogged, 1) == 0 && log != null)
                {
                    log("WARNING in native FindObjectsOfTypeAll(" + clsName + "): IL2CPP exception raised (likely background thread restriction).");
                }
                return 0;
            }

            int count = (int)_arrayLength(arr);
            int headerSz = _arrayHeaderSize();
            if (count <= 0 || headerSz <= 0)
            {
                return 0;
            }

            IntPtr getTextMethod = FindMethodInHierarchy(compClass, "get_text", 0);
            IntPtr setTextMethod = FindMethodInHierarchy(compClass, "set_text", 1);
            IntPtr mTextField = FindFieldInHierarchy(compClass, "m_text");
            IntPtr forceMeshMethod = isTmp ? FindMethodInHierarchy(compClass, "ForceMeshUpdate", 0) : IntPtr.Zero;

            int replaced = 0;
            IntPtr fieldValBuf = Marshal.AllocHGlobal(IntPtr.Size);
            try
            {
                for (int i = 0; i < count; i++)
                {
                    IntPtr obj = Marshal.ReadIntPtr(arr, headerSz + i * IntPtr.Size);
                    if (obj == IntPtr.Zero)
                    {
                        continue;
                    }

                    IntPtr curStrPtr = IntPtr.Zero;
                    if (getTextMethod != IntPtr.Zero)
                    {
                        IntPtr getExc;
                        curStrPtr = InvokeMethod(getTextMethod, obj, null, out getExc);
                        if (getExc != IntPtr.Zero) curStrPtr = IntPtr.Zero;
                    }
                    if (curStrPtr == IntPtr.Zero && mTextField != IntPtr.Zero && _fieldGet != null)
                    {
                        Marshal.WriteIntPtr(fieldValBuf, IntPtr.Zero);
                        _fieldGet(obj, mTextField, fieldValBuf);
                        curStrPtr = Marshal.ReadIntPtr(fieldValBuf);
                    }

                    string curText = ReadIl2CppString(curStrPtr);
                    string translated;
                    if (!store.TryTranslateRuntimeText(curText, out translated))
                    {
                        if (forceRefreshAll && forceMeshMethod != IntPtr.Zero)
                        {
                            IntPtr rfExc;
                            InvokeMethod(forceMeshMethod, obj, null, out rfExc);
                        }
                        continue;
                    }

                    IntPtr newStrPtr = NewIl2CppString(translated);
                    if (newStrPtr == IntPtr.Zero)
                    {
                        continue;
                    }

                    if (setTextMethod != IntPtr.Zero)
                    {
                        IntPtr setExc;
                        InvokeMethod(setTextMethod, obj, new IntPtr[] { newStrPtr }, out setExc);
                        if (setExc == IntPtr.Zero)
                        {
                            replaced++;
                        }
                    }
                    else if (mTextField != IntPtr.Zero && _fieldSet != null)
                    {
                        _fieldSet(obj, mTextField, newStrPtr);
                        replaced++;
                    }

                    if (forceMeshMethod != IntPtr.Zero)
                    {
                        IntPtr fmExc;
                        InvokeMethod(forceMeshMethod, obj, null, out fmExc);
                    }
                }
            }
            finally
            {
                Marshal.FreeHGlobal(fieldValBuf);
            }
            return replaced;
        }
    }
#endif

    internal sealed class ParameterizedOverrideTemplate
    {
        public string[] Segments;
        public string[] Tokens;
        public string TargetValue;
        public int LiteralLength;

        public static ParameterizedOverrideTemplate TryCreate(string sourceKey, string targetVal)
        {
            if (string.IsNullOrEmpty(sourceKey) || string.IsNullOrEmpty(targetVal))
            {
                return null;
            }
            if (sourceKey.IndexOf('{') < 0 && sourceKey.IndexOf('%') < 0)
            {
                return null;
            }

            List<string> segments = new List<string>();
            List<string> tokens = new List<string>();
            int lastPos = 0;
            int i = 0;
            int literalLen = 0;

            while (i < sourceKey.Length)
            {
                string tok = null;
                int tokLen = 0;
                char ch = sourceKey[i];
                if (ch == '{')
                {
                    int close = sourceKey.IndexOf('}', i + 1);
                    if (close > i + 1 && close - i <= 16 && char.IsDigit(sourceKey[i + 1]))
                    {
                        tok = sourceKey.Substring(i, close - i + 1);
                        tokLen = tok.Length;
                    }
                }
                else if (ch == '%' && i + 1 < sourceKey.Length)
                {
                    char nxt = sourceKey[i + 1];
                    if (nxt == 's' || nxt == 'd' || nxt == 'f' || nxt == 'i')
                    {
                        tok = sourceKey.Substring(i, 2);
                        tokLen = 2;
                    }
                }

                if (tok != null)
                {
                    string seg = sourceKey.Substring(lastPos, i - lastPos);
                    if (segments.Count > 0 && seg.Length == 0)
                    {
                        return null;
                    }
                    segments.Add(seg);
                    literalLen += seg.Length;
                    tokens.Add(tok);
                    i += tokLen;
                    lastPos = i;
                }
                else
                {
                    i++;
                }
            }

            if (tokens.Count == 0 || tokens.Count > 4)
            {
                return null;
            }

            string tailSeg = sourceKey.Substring(lastPos);
            segments.Add(tailSeg);
            literalLen += tailSeg.Length;
            if (literalLen < 3)
            {
                return null;
            }

            ParameterizedOverrideTemplate tpl = new ParameterizedOverrideTemplate();
            tpl.Segments = segments.ToArray();
            tpl.Tokens = tokens.ToArray();
            tpl.TargetValue = targetVal;
            tpl.LiteralLength = literalLen;
            return tpl;
        }

        public bool TryApply(string input, out string result)
        {
            result = null;
            if (string.IsNullOrEmpty(input))
            {
                return false;
            }
            if (!input.StartsWith(Segments[0], StringComparison.Ordinal))
            {
                return false;
            }

            int cursor = Segments[0].Length;
            int n = Tokens.Length;
            string[] captured = new string[n];

            for (int i = 0; i < n; i++)
            {
                string nextSeg = Segments[i + 1];
                int endIdx;
                if (i == n - 1)
                {
                    if (nextSeg.Length == 0)
                    {
                        endIdx = input.Length;
                    }
                    else
                    {
                        if (!input.EndsWith(nextSeg, StringComparison.Ordinal))
                        {
                            return false;
                        }
                        endIdx = input.Length - nextSeg.Length;
                        if (endIdx < cursor)
                        {
                            return false;
                        }
                    }
                }
                else
                {
                    endIdx = input.IndexOf(nextSeg, cursor, StringComparison.Ordinal);
                    if (endIdx < 0)
                    {
                        return false;
                    }
                }

                string cap = input.Substring(cursor, endIdx - cursor);
                if (cap.Length == 0 || cap.Length > 120 || cap.IndexOf('\n') >= 0)
                {
                    return false;
                }
                captured[i] = cap;
                cursor = endIdx + nextSeg.Length;
            }

            if (cursor != input.Length)
            {
                return false;
            }

            // استبدال المتغيرات بمسح يساري أحادي المرور لتفادي تداخل القيم المستخرجة
            StringBuilder sb = new StringBuilder(TargetValue.Length + 32);
            int pos = 0;
            while (pos < TargetValue.Length)
            {
                int matchedToken = -1;
                for (int t = 0; t < n; t++)
                {
                    if (string.CompareOrdinal(TargetValue, pos, Tokens[t], 0, Tokens[t].Length) == 0)
                    {
                        matchedToken = t;
                        break;
                    }
                }
                if (matchedToken >= 0)
                {
                    sb.Append(captured[matchedToken]);
                    pos += Tokens[matchedToken].Length;
                }
                else
                {
                    sb.Append(TargetValue[pos]);
                    pos++;
                }
            }

            result = sb.ToString();
            return true;
        }
    }

    internal static class SimpleOverrideJsonParser
    {
        public static void Parse(
            string json,
            out Dictionary<string, string> byKey,
            out Dictionary<string, string> bySource
        )
        {
            byKey = new Dictionary<string, string>(StringComparer.Ordinal);
            bySource = new Dictionary<string, string>(StringComparer.Ordinal);
            if (string.IsNullOrEmpty(json))
            {
                return;
            }

            int pos = 0;
            SkipWhitespace(json, ref pos);
            if (pos >= json.Length || json[pos] != '{')
            {
                return;
            }
            pos++;

            while (pos < json.Length)
            {
                SkipWhitespace(json, ref pos);
                if (pos >= json.Length || json[pos] == '}')
                {
                    break;
                }
                if (json[pos] == ',')
                {
                    pos++;
                    continue;
                }
                if (json[pos] != '"')
                {
                    break;
                }
                string key = ReadString(json, ref pos);
                SkipWhitespace(json, ref pos);
                if (pos < json.Length && json[pos] == ':')
                {
                    pos++;
                }
                SkipWhitespace(json, ref pos);

                if (pos < json.Length && json[pos] == '{' && string.Equals(key, "by_key", StringComparison.Ordinal))
                {
                    ParseFlatStringObject(json, ref pos, byKey);
                }
                else if (pos < json.Length && json[pos] == '{' && string.Equals(key, "by_source", StringComparison.Ordinal))
                {
                    ParseFlatStringObject(json, ref pos, bySource);
                }
                else
                {
                    SkipValue(json, ref pos);
                }
            }
        }

        private static void ParseFlatStringObject(string json, ref int pos, Dictionary<string, string> target)
        {
            if (pos >= json.Length || json[pos] != '{')
            {
                return;
            }
            pos++;
            while (pos < json.Length)
            {
                SkipWhitespace(json, ref pos);
                if (pos >= json.Length)
                {
                    break;
                }
                if (json[pos] == '}')
                {
                    pos++;
                    break;
                }
                if (json[pos] == ',')
                {
                    pos++;
                    continue;
                }
                if (json[pos] != '"')
                {
                    break;
                }
                string key = ReadString(json, ref pos);
                SkipWhitespace(json, ref pos);
                if (pos < json.Length && json[pos] == ':')
                {
                    pos++;
                }
                SkipWhitespace(json, ref pos);
                if (pos < json.Length && json[pos] == '"')
                {
                    string val = ReadString(json, ref pos);
                    target[key] = val;
                }
                else
                {
                    SkipValue(json, ref pos);
                }
            }
        }

        private static void SkipWhitespace(string json, ref int pos)
        {
            while (pos < json.Length)
            {
                char c = json[pos];
                if (c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\uFEFF')
                {
                    pos++;
                }
                else
                {
                    break;
                }
            }
        }

        private static string ReadString(string json, ref int pos)
        {
            if (pos >= json.Length || json[pos] != '"')
            {
                return string.Empty;
            }
            pos++;
            StringBuilder sb = new StringBuilder();
            while (pos < json.Length)
            {
                char ch = json[pos++];
                if (ch == '"')
                {
                    break;
                }
                if (ch == '\\' && pos < json.Length)
                {
                    char esc = json[pos++];
                    switch (esc)
                    {
                        case '"': sb.Append('"'); break;
                        case '\\': sb.Append('\\'); break;
                        case '/': sb.Append('/'); break;
                        case 'b': sb.Append('\b'); break;
                        case 'f': sb.Append('\f'); break;
                        case 'n': sb.Append('\n'); break;
                        case 'r': sb.Append('\r'); break;
                        case 't': sb.Append('\t'); break;
                        case 'u':
                            if (pos + 4 <= json.Length)
                            {
                                string hex = json.Substring(pos, 4);
                                try
                                {
                                    int code = Convert.ToInt32(hex, 16);
                                    sb.Append((char)code);
                                    pos += 4;
                                }
                                catch
                                {
                                    sb.Append("\\u");
                                }
                            }
                            break;
                        default:
                            sb.Append(esc);
                            break;
                    }
                }
                else
                {
                    sb.Append(ch);
                }
            }
            return sb.ToString();
        }

        private static void SkipValue(string json, ref int pos)
        {
            SkipWhitespace(json, ref pos);
            if (pos >= json.Length)
            {
                return;
            }
            char c = json[pos];
            if (c == '"')
            {
                ReadString(json, ref pos);
                return;
            }
            if (c == '{' || c == '[')
            {
                int depth = 0;
                while (pos < json.Length)
                {
                    char ch = json[pos];
                    if (ch == '"')
                    {
                        ReadString(json, ref pos);
                        continue;
                    }
                    if (ch == '{' || ch == '[')
                    {
                        depth++;
                    }
                    else if (ch == '}' || ch == ']')
                    {
                        depth--;
                        pos++;
                        if (depth <= 0)
                        {
                            break;
                        }
                        continue;
                    }
                    pos++;
                }
                return;
            }
            while (pos < json.Length)
            {
                char ch = json[pos];
                if (ch == ',' || ch == '}' || ch == ']' || ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n')
                {
                    break;
                }
                pos++;
            }
        }
    }
}
