// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace Probes
{
    /// <summary>
    /// P10: OpenGL drawing inside a layered top-level window over a black window. --layer makes the top-level window layered:
    /// constant (SetLayeredWindowAttributes), ulw (UpdateLayeredWindow, per-pixel alpha) or none. --target draws on that window
    /// (self), on a child that fills it (child), or on a child that a second process puts into it (foreign). --alphas gives the
    /// constant alphas in turn (default 200), each with --frames buffer swaps. The pixel at the centre must be opaque, faded or
    /// the layered bitmap; --expect names the one to demand. Prints a "present" line before the first swap and the result
    /// object at the end; a run that stops after the first died inside a present and leaves no result file.
    /// </summary>
    public static class P10LayeredChild
    {
        const string Name = "p10-layered-child";
        static readonly WndProc Callback = WindowProc;
        static IntPtr hwnd, target, parent;
        static string layer = "constant", place = "child", expect, alphaText = "200";
        static int frames = 10;
        static double hold = 1;

        [STAThread]
        static int Main(string[] args)
        {
            int code = 1;
            string name = Name;
            try
            {
                for (int i = 0; i < args.Length; i++)
                {
                    if (i + 1 == args.Length) throw new ArgumentException("missing option value");
                    string option = args[i], value = args[++i];
                    if (option == "--layer" && (value == "constant" || value == "ulw" || value == "none")) layer = value;
                    else if (option == "--target" && (value == "self" || value == "child" || value == "foreign")) place = value;
                    else if (option == "--expect" && (value == "opaque" || value == "faded" || value == "bitmap")) expect = value;
                    else if (option == "--alphas") alphaText = value;
                    else if (option == "--into") parent = new IntPtr(long.Parse(value, CultureInfo.InvariantCulture));
                    else if (option == "--frames")
                    {
                        frames = int.Parse(value, CultureInfo.InvariantCulture);
                        if (frames < 1 || frames > 1000) throw new ArgumentException("invalid frames");
                    }
                    else if (option == "--hold")
                    {
                        hold = double.Parse(value, CultureInfo.InvariantCulture);
                        if (!double.IsFinite(hold) || hold < 0 || hold > 3600) throw new ArgumentException("invalid hold");
                    }
                    else throw new ArgumentException($"unknown option {option} {value}");
                }
                byte[] alphas = Array.ConvertAll(alphaText.Split(','), x => byte.Parse(x, CultureInfo.InvariantCulture));
                if (alphas.Length > 1 && layer != "constant") throw new ArgumentException("--alphas needs --layer constant");
                RegisterClass();
                if (parent != IntPtr.Zero) return DrawInto(parent, frames * alphas.Length);
                name = $"{Name}-{layer}-{place}" + (alphaText == "200" ? "" : "-" + alphaText.Replace(',', '-'));
                Probe.Begin(name);
                Probe.Result["layer"] = layer;
                Probe.Result["target"] = place;
                Probe.Result["expect"] = expect;
                IntPtr back = CreateWindowExW(0x8u /* WS_EX_TOPMOST */, Name, Name + "-back", 0x80000000 /* WS_POPUP */, Left - 20, Top - 20,
                                              Width + 40, Height + 40, IntPtr.Zero, IntPtr.Zero, Probe.GetModuleHandleW(null), IntPtr.Zero);
                if (back == IntPtr.Zero) Fail("CreateWindowExW back");
                ShowWindow(back, 4 /* SW_SHOWNOACTIVATE */);
                Pump(300);
                hwnd = CreateWindowExW(layer == "none" ? 0x8u : 0x80008u /* WS_EX_LAYERED | WS_EX_TOPMOST */, Name, Name, 0x80000000 /* WS_POPUP */,
                                       Left, Top, Width, Height, IntPtr.Zero, IntPtr.Zero, Probe.GetModuleHandleW(null), IntPtr.Zero);
                if (hwnd == IntPtr.Zero) Fail("CreateWindowExW");
                if (layer == "constant") Fade(alphas[0]);
                if (layer == "ulw") PerPixelAlpha();
                target = place == "self" ? hwnd : place == "child" ? Child(hwnd) : IntPtr.Zero;
                ShowWindow(hwnd, 4 /* SW_SHOWNOACTIVATE */);
                Pump(300);
                var seen = new List<Dictionary<string, object>>();
                bool right = true;
                IntPtr win = target == IntPtr.Zero ? IntPtr.Zero : Context(target);
                Console.WriteLine("{\"step\":\"present\"}"); Console.Out.Flush();
                using (Process other = place == "foreign" ? Foreign() : null)
                {
                    foreach (byte alpha in alphas)
                    {
                        if (layer == "constant") Fade(alpha);
                        if (win != IntPtr.Zero) Draw(win, frames);
                        else if (other.HasExited) throw new InvalidOperationException($"the second process ended with {other.ExitCode}");
                        Pump(600);
                        uint pixel = ScreenPixel();
                        string kind = Kind(pixel, alpha);
                        bool ok = expect != null ? kind == expect : layer == "none" ? kind == "opaque" : layer == "ulw" ? kind == "bitmap"
                                  : kind == "faded" || kind == "opaque";
                        if (alpha == 255 && layer == "constant") ok = kind == "opaque";
                        right &= ok;
                        seen.Add(new Dictionary<string, object> { ["alpha"] = alpha, ["pixel"] = Probe.Hex(unchecked((int)pixel)), ["kind"] = kind, ["ok"] = ok });
                        Probe.Result["screenPixel"] = Probe.Hex(unchecked((int)pixel));
                    }
                    Pump((int)(hold * 1000));
                    if (other != null && other.HasExited) throw new InvalidOperationException($"the second process ended with {other.ExitCode}");
                    other?.StandardInput.Close();
                    other?.WaitForExit(10000);
                }
                Probe.Result["pixels"] = seen;
                if (win != IntPtr.Zero) ReleaseDC(target, win);
                if (right) code = 0;
                else Probe.Result["error"] = "the pixel is not what this arm must show";
            }
            catch (Exception error)
            {
                Probe.Result["error"] = error.ToString();
                if (parent != IntPtr.Zero) Console.Error.WriteLine(error);
            }
            finally
            {
                if (parent == IntPtr.Zero)
                {
                    Probe.Result["exitCode"] = code;
                    Probe.Result["pass"] = code == 0;
                    Probe.Write(name);
                    Console.WriteLine(JsonSerializer.Serialize(Probe.Result));
                }
                if (hwnd != IntPtr.Zero) DestroyWindow(hwnd);
                GC.KeepAlive(Callback);
            }
            return code;
        }

        const int Left = 100, Top = 100, Width = 320, Height = 200;

        static void RegisterClass()
        {
            var cls = new WindowClass
            {
                Size = (uint)Marshal.SizeOf<WindowClass>(), Proc = Callback, Instance = Probe.GetModuleHandleW(null),
                Background = GetStockObject(4 /* BLACK_BRUSH */), ClassName = Name
            };
            if (RegisterClassExW(ref cls) == 0) Fail("RegisterClassExW");
        }

        static void Fade(byte alpha)
        {
            if (!SetLayeredWindowAttributes(hwnd, 0, alpha, 2 /* LWA_ALPHA */)) Fail("SetLayeredWindowAttributes");
        }

        static IntPtr Child(IntPtr into)
        {
            IntPtr window = CreateWindowExW(0, Name, Name + "-child", 0x50000000 /* WS_CHILD | WS_VISIBLE */, 0, 0, Width, Height, into,
                                            IntPtr.Zero, Probe.GetModuleHandleW(null), IntPtr.Zero);
            if (window == IntPtr.Zero) Fail("CreateWindowExW child");
            return window;
        }

        static IntPtr Context(IntPtr window)
        {
            NativeLibrary.Load("opengl32.dll"); // Windows makes no context when it is loaded after the pixel format is set
            IntPtr win = GetDC(window);
            var pfd = new PixelFormat { Size = 40, Version = 1, Flags = 0x25 /* DRAW_TO_WINDOW | SUPPORT_OPENGL | DOUBLEBUFFER */,
                                        ColorBits = 32, DepthBits = 24 };
            int format = ChoosePixelFormat(win, ref pfd);
            if (format == 0 || !SetPixelFormat(win, format, ref pfd)) Fail("SetPixelFormat");
            IntPtr context = wglCreateContext(win);
            if (context == IntPtr.Zero || !wglMakeCurrent(win, context)) Fail("wglCreateContext");
            Probe.Result["pixelFormat"] = format;
            return win;
        }

        static void Draw(IntPtr win, int count)
        {
            for (int i = 0; i < count; i++)
            {
                glClearColor(0f, 0.5f, 1f, 1f); glClear(0x4000 /* COLOR_BUFFER_BIT */);
                if (!SwapBuffers(win)) Fail("SwapBuffers");
                Pump(30);
            }
        }

        /// <summary>The second process: a child of the first one's window, drawn on until the first closes our input.</summary>
        static int DrawInto(IntPtr into, int count)
        {
            IntPtr win = Context(target = Child(into));
            var closed = new System.Threading.Thread(() => Console.In.ReadToEnd()) { IsBackground = true };
            closed.Start();
            Draw(win, count);
            Console.WriteLine("drawn"); Console.Out.Flush();
            while (closed.IsAlive) Draw(win, 1);
            return 0;
        }

        static Process Foreign()
        {
            var start = new ProcessStartInfo(Environment.ProcessPath) { UseShellExecute = false, RedirectStandardInput = true, RedirectStandardOutput = true };
            foreach (string item in new[] { "--into", hwnd.ToInt64().ToString(CultureInfo.InvariantCulture), "--frames", frames.ToString(CultureInfo.InvariantCulture) })
                start.ArgumentList.Add(item);
            Process other = Process.Start(start);
            var drawn = System.Threading.Tasks.Task.Run(() => other.StandardOutput.ReadLine());
            for (int i = 0; i < 100 && !drawn.IsCompleted; i++) Pump(100);
            if (!drawn.IsCompleted || drawn.Result != "drawn") throw new InvalidOperationException("the second process did not draw");
            return other;
        }

        static uint ScreenPixel()
        {
            IntPtr screen = GetDC(IntPtr.Zero);
            uint pixel = GetPixel(screen, Left + Width / 2, Top + Height / 2);
            ReleaseDC(IntPtr.Zero, screen);
            return pixel;
        }

        static string Kind(uint pixel, byte alpha)
        {
            int r = (int)(pixel & 255), g = (int)(pixel >> 8 & 255), b = (int)(pixel >> 16 & 255);
            if (Near(r, 0) && Near(g, 128) && Near(b, 255)) return "opaque";
            if (Near(r, 0) && Near(g, 128 * alpha / 255) && Near(b, alpha)) return "faded";
            return Near(r, 0x40) && Near(g, 0x40) && Near(b, 0x40) ? "bitmap" : "other";
        }

        static bool Near(int a, int b) => Math.Abs(a - b) <= 3;

        /// <summary>Gives the layered window a half-transparent grey bitmap with per-pixel alpha.</summary>
        static unsafe void PerPixelAlpha()
        {
            IntPtr screen = GetDC(IntPtr.Zero), memory = CreateCompatibleDC(screen);
            var header = new BitmapHeader { Size = 40, Width = Width, Height = -Height, Planes = 1, BitCount = 32 };
            IntPtr bitmap = CreateDIBSection(screen, ref header, 0, out IntPtr bits, IntPtr.Zero, 0);
            if (bitmap == IntPtr.Zero) Fail("CreateDIBSection");
            for (int i = 0; i < Width * Height; i++) ((uint*)bits)[i] = 0x80404040;
            IntPtr old = SelectObject(memory, bitmap);
            var at = new Point { X = Left, Y = Top };
            var size = new Point { X = Width, Y = Height };
            var origin = new Point();
            var blend = new Blend { SourceConstantAlpha = 255, AlphaFormat = 1 /* AC_SRC_ALPHA */ };
            if (!UpdateLayeredWindow(hwnd, screen, ref at, ref size, memory, ref origin, 0, ref blend, 2 /* ULW_ALPHA */)) Fail("UpdateLayeredWindow");
            SelectObject(memory, old); DeleteObject(bitmap); DeleteDC(memory); ReleaseDC(IntPtr.Zero, screen);
        }

        static void Fail(string api) => throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), api);

        static void Pump(int ms)
        {
            var clock = Stopwatch.StartNew();
            while (clock.ElapsedMilliseconds < ms)
            {
                while (PeekMessageW(out Message msg, IntPtr.Zero, 0, 0, 1))
                {
                    if (msg.Id == 0x12 /* WM_QUIT */) throw new InvalidOperationException("a window was closed before the end");
                    TranslateMessage(ref msg); DispatchMessageW(ref msg);
                }
                System.Threading.Thread.Sleep(10);
            }
        }

        static IntPtr WindowProc(IntPtr window, uint message, IntPtr wparam, IntPtr lparam)
        {
            if (message == 2) PostQuitMessage(0);
            return DefWindowProcW(window, message, wparam, lparam);
        }

        [UnmanagedFunctionPointer(CallingConvention.Winapi)]
        delegate IntPtr WndProc(IntPtr hwnd, uint msg, IntPtr wparam, IntPtr lparam);
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        struct WindowClass
        {
            public uint Size, Style;
            public WndProc Proc;
            public int ClassExtra, WindowExtra;
            public IntPtr Instance, Icon, Cursor, Background;
            public string MenuName, ClassName;
            public IntPtr SmallIcon;
        }
        [StructLayout(LayoutKind.Sequential)] struct Point { public int X, Y; }
        [StructLayout(LayoutKind.Sequential)] struct Blend { public byte Operation, Flags, SourceConstantAlpha, AlphaFormat; }
        [StructLayout(LayoutKind.Sequential)] struct BitmapHeader
        { public int Size, Width, Height; public short Planes, BitCount; public int Compression, SizeImage, XPels, YPels, ClrUsed, ClrImportant; }
        [StructLayout(LayoutKind.Sequential)] struct Message
        {
            public IntPtr Hwnd; public uint Id; public UIntPtr Wparam; public IntPtr Lparam;
            public uint Time; public Point Point; public uint Private;
        }
        [StructLayout(LayoutKind.Sequential)] struct PixelFormat
        {
            public ushort Size, Version; public uint Flags;
            public byte PixelType, ColorBits, RedBits, RedShift, GreenBits, GreenShift, BlueBits, BlueShift, AlphaBits, AlphaShift,
                        AccumBits, AccumRedBits, AccumGreenBits, AccumBlueBits, AccumAlphaBits, DepthBits, StencilBits, AuxBuffers,
                        LayerType, Reserved;
            public uint LayerMask, VisibleMask, DamageMask;
        }
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern ushort RegisterClassExW(ref WindowClass cls);
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr CreateWindowExW(uint ex, string cls, string name, uint style, int x, int y, int w, int h, IntPtr parent, IntPtr menu, IntPtr instance, IntPtr param);
        [DllImport("user32.dll")] static extern IntPtr DefWindowProcW(IntPtr h, uint m, IntPtr w, IntPtr l);
        [DllImport("user32.dll", SetLastError = true)] static extern bool SetLayeredWindowAttributes(IntPtr h, uint key, byte a, uint flags);
        [DllImport("user32.dll", SetLastError = true)] static extern bool UpdateLayeredWindow(IntPtr h, IntPtr dst, ref Point at, ref Point size, IntPtr src, ref Point origin, uint key, ref Blend blend, uint flags);
        [DllImport("gdi32.dll")] static extern IntPtr CreateCompatibleDC(IntPtr dc);
        [DllImport("gdi32.dll", SetLastError = true)] static extern IntPtr CreateDIBSection(IntPtr dc, ref BitmapHeader info, uint usage, out IntPtr bits, IntPtr section, uint offset);
        [DllImport("gdi32.dll")] static extern IntPtr SelectObject(IntPtr dc, IntPtr item);
        [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr item);
        [DllImport("gdi32.dll")] static extern bool DeleteDC(IntPtr dc);
        [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
        [DllImport("user32.dll")] static extern bool DestroyWindow(IntPtr h);
        [DllImport("user32.dll")] static extern void PostQuitMessage(int code);
        [DllImport("user32.dll")] static extern bool PeekMessageW(out Message msg, IntPtr h, uint min, uint max, uint remove);
        [DllImport("user32.dll")] static extern bool TranslateMessage(ref Message msg);
        [DllImport("user32.dll")] static extern IntPtr DispatchMessageW(ref Message msg);
        [DllImport("user32.dll")] static extern IntPtr GetDC(IntPtr h);
        [DllImport("user32.dll")] static extern int ReleaseDC(IntPtr h, IntPtr dc);
        [DllImport("gdi32.dll", SetLastError = true)] static extern int ChoosePixelFormat(IntPtr dc, ref PixelFormat pfd);
        [DllImport("gdi32.dll", SetLastError = true)] static extern bool SetPixelFormat(IntPtr dc, int format, ref PixelFormat pfd);
        [DllImport("gdi32.dll", SetLastError = true)] static extern bool SwapBuffers(IntPtr dc);
        [DllImport("gdi32.dll")] static extern IntPtr GetStockObject(int item);
        [DllImport("gdi32.dll")] static extern uint GetPixel(IntPtr dc, int x, int y);
        [DllImport("opengl32.dll", SetLastError = true)] static extern IntPtr wglCreateContext(IntPtr dc);
        [DllImport("opengl32.dll", SetLastError = true)] static extern bool wglMakeCurrent(IntPtr dc, IntPtr context);
        [DllImport("opengl32.dll")] static extern void glClearColor(float r, float g, float b, float a);
        [DllImport("opengl32.dll")] static extern void glClear(uint mask);
    }
}
