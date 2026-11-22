// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading;

namespace Probes
{
    public static class P8LayeredAlpha
    {
        const string Name = "p8-layered-alpha";
        static readonly WndProc Callback = WindowProc;
        static IntPtr hwnd;
        static byte alpha = 2;
        static string mode = "slwa";
        static double hold = 15;
        static int then = -1;

        [STAThread]
        static int Main(string[] args)
        {
            int code = 1;
            try
            {
                Probe.Begin(Name);
                for (int i = 0; i < args.Length; i++)
                {
                    if (i + 1 == args.Length) throw new ArgumentException("missing option value");
                    string option = args[i], value = args[++i];
                    if (option == "--alpha") alpha = byte.Parse(value, CultureInfo.InvariantCulture);
                    else if (option == "--mode" && (value == "slwa" || value == "ulw")) mode = value;
                    else if (option == "--then") then = byte.Parse(value, CultureInfo.InvariantCulture);
                    else if (option == "--hold")
                    {
                        hold = double.Parse(value, CultureInfo.InvariantCulture);
                        if (!double.IsFinite(hold) || hold < 0 || hold > 3600) throw new ArgumentException("invalid hold");
                    }
                    else throw new ArgumentException("usage: P8LayeredAlpha [--alpha 0..255] [--mode slwa|ulw] [--hold SECONDS] [--then 0..255]");
                }
                if (then >= 0 && mode != "slwa") throw new ArgumentException("--then needs --mode slwa");
                Probe.Result["alpha"] = alpha;
                Probe.Result["mode"] = mode;
                Probe.Result["holdSeconds"] = hold;
                if (then >= 0) Probe.Result["then"] = then;
                var cls = new WindowClass { Size = (uint)Marshal.SizeOf<WindowClass>(), Proc = Callback,
                    Instance = Probe.GetModuleHandleW(null), ClassName = Name };
                if (RegisterClassExW(ref cls) == 0) Fail("RegisterClassExW");
                hwnd = CreateWindowExW(0x80000, Name, Name, 0x80000000, 100, 100, 200, 200,
                    IntPtr.Zero, IntPtr.Zero, cls.Instance, IntPtr.Zero);
                if (hwnd == IntPtr.Zero) Fail("CreateWindowExW");
                Probe.Result["hwnd"] = "0x" + hwnd.ToInt64().ToString("x");
                bool ok;
                if (mode == "slwa")
                {
                    ok = SetLayeredWindowAttributes(hwnd, 0, alpha, 2);
                    Probe.Result["slwa"] = new { success = ok, error = ok ? 0 : Marshal.GetLastWin32Error() };
                }
                else
                {
                    ok = Upload();
                    Probe.Result["ulw"] = new { success = ok, error = ok ? 0 : Marshal.GetLastWin32Error() };
                }
                bool read = GetLayeredWindowAttributes(hwnd, out uint key, out byte readAlpha, out uint flags);
                int readError = read ? 0 : Marshal.GetLastWin32Error();
                Probe.Result["readback"] = new { success = read, alpha = readAlpha, key, flags, error = readError };
                if (!ok) throw new InvalidOperationException("layered window update failed");
                if (mode == "slwa" && (!read || readAlpha != alpha || flags != 2))
                    throw new InvalidOperationException("layered attributes readback mismatch");
                ShowWindow(hwnd, 5);
                UpdateWindow(hwnd);
                var timer = Stopwatch.StartNew();
                bool switched = then < 0;
                while (timer.Elapsed.TotalSeconds < hold)
                {
                    // --then: change the constant alpha halfway through the hold
                    if (!switched && timer.Elapsed.TotalSeconds >= hold / 2)
                    {
                        switched = true;
                        bool done = SetLayeredWindowAttributes(hwnd, 0, (byte)then, 2);
                        Probe.Result["slwaThen"] = new { success = done, error = done ? 0 : Marshal.GetLastWin32Error() };
                        if (!done) throw new InvalidOperationException("second layered window update failed");
                    }
                    while (PeekMessageW(out Message msg, IntPtr.Zero, 0, 0, 1))
                    {
                        if (msg.Id == 0x12) throw new InvalidOperationException("window closed early");
                        TranslateMessage(ref msg);
                        DispatchMessageW(ref msg);
                    }
                    Thread.Sleep(10);
                }
                code = 0;
            }
            catch (Exception error)
            {
                Probe.Result["error"] = error.ToString();
            }
            finally
            {
                Probe.Result["exitCode"] = code;
                Probe.Result["pass"] = code == 0;
                Probe.Write(Name);
                Console.WriteLine(JsonSerializer.Serialize(Probe.Result));
                if (hwnd != IntPtr.Zero) DestroyWindow(hwnd);
                GC.KeepAlive(Callback);
            }
            return code;
        }

        static void Fail(string api) => throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), api);

        static bool Upload()
        {
            IntPtr dc = CreateCompatibleDC(IntPtr.Zero);
            if (dc == IntPtr.Zero) Fail("CreateCompatibleDC");
            IntPtr bitmap = IntPtr.Zero, previous = IntPtr.Zero;
            try
            {
                var info = new BitmapInfo { Size = 40, Width = 200, Height = -200, Planes = 1, Bits = 32 };
                bitmap = CreateDIBSection(dc, ref info, 0, out IntPtr bits, IntPtr.Zero, 0);
                if (bitmap == IntPtr.Zero) Fail("CreateDIBSection");
                int pixel = unchecked((int)((uint)alpha * 0x01010101));
                var pixels = new int[200 * 200];
                Array.Fill(pixels, pixel);
                Marshal.Copy(pixels, 0, bits, pixels.Length);
                previous = SelectObject(dc, bitmap);
                var point = new Point { X = 100, Y = 100 };
                var source = new Point();
                var size = new Point { X = 200, Y = 200 };
                var blend = new Blend { ConstantAlpha = 255, AlphaFormat = 1 };
                return UpdateLayeredWindow(hwnd, IntPtr.Zero, ref point, ref size, dc, ref source, 0, ref blend, 2);
            }
            finally
            {
                if (previous != IntPtr.Zero) SelectObject(dc, previous);
                if (bitmap != IntPtr.Zero) DeleteObject(bitmap);
                DeleteDC(dc);
            }
        }

        static IntPtr WindowProc(IntPtr window, uint message, IntPtr wparam, IntPtr lparam)
        {
            if (message == 0x0f)
            {
                IntPtr dc = BeginPaint(window, out Paint paint);
                if (mode == "slwa")
                {
                    GetClientRect(window, out Rect rect);
                    FillRect(dc, ref rect, GetStockObject(0)); // WHITE_BRUSH
                }
                EndPaint(window, ref paint);
                return IntPtr.Zero;
            }
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
        [StructLayout(LayoutKind.Sequential)] struct Rect { public int Left, Top, Right, Bottom; }
        [StructLayout(LayoutKind.Sequential)] struct Message
        {
            public IntPtr Hwnd; public uint Id; public UIntPtr Wparam; public IntPtr Lparam;
            public uint Time; public Point Point; public uint Private;
        }
        [StructLayout(LayoutKind.Sequential)] unsafe struct Paint
        {
            public IntPtr Dc; public int Erase; public Rect Rect; public int Restore, Incremental;
            public fixed byte Reserved[32];
        }
        [StructLayout(LayoutKind.Sequential)] struct BitmapInfo
        {
            public uint Size; public int Width, Height; public ushort Planes, Bits;
            public uint Compression, ImageSize; public int Xppm, Yppm; public uint Used, Important, Color;
        }
        [StructLayout(LayoutKind.Sequential, Pack = 1)] struct Blend
        { public byte Operation, Flags, ConstantAlpha, AlphaFormat; }
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern ushort RegisterClassExW(ref WindowClass cls);
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr CreateWindowExW(uint ex, string cls, string name, uint style, int x, int y, int w, int h, IntPtr parent, IntPtr menu, IntPtr instance, IntPtr param);
        [DllImport("user32.dll")] static extern IntPtr DefWindowProcW(IntPtr h, uint m, IntPtr w, IntPtr l);
        [DllImport("user32.dll", SetLastError = true)] static extern bool SetLayeredWindowAttributes(IntPtr h, uint key, byte a, uint flags);
        [DllImport("user32.dll", SetLastError = true)] static extern bool GetLayeredWindowAttributes(IntPtr h, out uint key, out byte a, out uint flags);
        [DllImport("user32.dll", SetLastError = true)] static extern bool UpdateLayeredWindow(IntPtr h, IntPtr dst, ref Point p, ref Point size, IntPtr dc, ref Point src, uint key, ref Blend blend, uint flags);
        [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
        [DllImport("user32.dll")] static extern bool UpdateWindow(IntPtr h);
        [DllImport("user32.dll")] static extern bool DestroyWindow(IntPtr h);
        [DllImport("user32.dll")] static extern void PostQuitMessage(int code);
        [DllImport("user32.dll")] static extern bool PeekMessageW(out Message msg, IntPtr h, uint min, uint max, uint remove);
        [DllImport("user32.dll")] static extern bool TranslateMessage(ref Message msg);
        [DllImport("user32.dll")] static extern IntPtr DispatchMessageW(ref Message msg);
        [DllImport("user32.dll")] static extern IntPtr BeginPaint(IntPtr h, out Paint paint);
        [DllImport("user32.dll")] static extern bool EndPaint(IntPtr h, ref Paint paint);
        [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr h, out Rect rect);
        [DllImport("user32.dll")] static extern int FillRect(IntPtr dc, ref Rect rect, IntPtr brush);
        [DllImport("gdi32.dll")] static extern IntPtr GetStockObject(int index);
        [DllImport("gdi32.dll", SetLastError = true)] static extern IntPtr CreateCompatibleDC(IntPtr dc);
        [DllImport("gdi32.dll", SetLastError = true)] static extern IntPtr CreateDIBSection(IntPtr dc, ref BitmapInfo info, uint usage, out IntPtr bits, IntPtr section, uint offset);
        [DllImport("gdi32.dll")] static extern IntPtr SelectObject(IntPtr dc, IntPtr obj);
        [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr obj);
        [DllImport("gdi32.dll")] static extern bool DeleteDC(IntPtr dc);
    }
}
