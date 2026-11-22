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
    // A captioned top-level window shown, painted and hidden again within --gap milliseconds, then hidden a second time
    // (--rehide 1). On Windows it is never left on screen; the hold lets an outside check look for a host window still mapped.
    public static class P9ShowHide
    {
        const string Name = "p9-show-hide";
        static readonly WndProc Callback = WindowProc;
        static IntPtr hwnd;
        static int gap, rehide = 1;
        static double hold = 10;

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
                    if (option == "--gap") gap = int.Parse(value, CultureInfo.InvariantCulture);
                    else if (option == "--hold") hold = double.Parse(value, CultureInfo.InvariantCulture);
                    else if (option == "--rehide") rehide = int.Parse(value, CultureInfo.InvariantCulture);
                    else throw new ArgumentException("usage: P9ShowHide [--gap MILLISECONDS] [--hold SECONDS] [--rehide 0|1]");
                }
                if (gap < 0 || gap > 60000 || !double.IsFinite(hold) || hold < 0 || hold > 3600 || rehide is < 0 or > 1)
                    throw new ArgumentException("invalid gap, hold or rehide");
                Probe.Result["gapMs"] = gap;
                Probe.Result["holdSeconds"] = hold;
                Probe.Result["rehide"] = rehide;
                var cls = new WindowClass { Size = (uint)Marshal.SizeOf<WindowClass>(), Proc = Callback,
                    Instance = Probe.GetModuleHandleW(null), ClassName = Name };
                if (RegisterClassExW(ref cls) == 0) Fail("RegisterClassExW");
                // WS_EX_CONTROLPARENT | WS_EX_WINDOWEDGE | WS_EX_DLGMODALFRAME; WS_OVERLAPPEDWINDOW | WS_CLIPSIBLINGS | WS_CLIPCHILDREN
                hwnd = CreateWindowExW(0x10101, Name, Name, 0x06cf0000, 0, 0, 300, 300,
                    IntPtr.Zero, IntPtr.Zero, cls.Instance, IntPtr.Zero);
                if (hwnd == IntPtr.Zero) Fail("CreateWindowExW");
                Probe.Result["hwnd"] = "0x" + hwnd.ToInt64().ToString("x");
                ShowWindow(hwnd, 5);
                SetForegroundWindow(hwnd);
                UpdateWindow(hwnd);
                if (gap > 0) Thread.Sleep(gap);
                ShowWindow(hwnd, 0);
                // SWP_NOSIZE | SWP_NOMOVE | SWP_HIDEWINDOW on the already hidden window
                if (rehide == 1 && !SetWindowPos(hwnd, IntPtr.Zero, 0, 0, 0, 0, 0x83)) Fail("SetWindowPos");
                Probe.Result["visibleAfterHide"] = IsWindowVisible(hwnd);
                var timer = Stopwatch.StartNew();
                while (timer.Elapsed.TotalSeconds < hold)
                {
                    while (PeekMessageW(out Message msg, IntPtr.Zero, 0, 0, 1))
                    {
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

        static IntPtr WindowProc(IntPtr window, uint message, IntPtr wparam, IntPtr lparam)
        {
            if (message == 0x0f)
            {
                IntPtr dc = BeginPaint(window, out Paint paint);
                GetClientRect(window, out Rect rect);
                FillRect(dc, ref rect, GetStockObject(0)); // WHITE_BRUSH
                EndPaint(window, ref paint);
                return IntPtr.Zero;
            }
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
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern ushort RegisterClassExW(ref WindowClass cls);
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr CreateWindowExW(uint ex, string cls, string name, uint style, int x, int y, int w, int h, IntPtr parent, IntPtr menu, IntPtr instance, IntPtr param);
        [DllImport("user32.dll")] static extern IntPtr DefWindowProcW(IntPtr h, uint m, IntPtr w, IntPtr l);
        [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
        [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
        [DllImport("user32.dll", SetLastError = true)] static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int w, int cy, uint flags);
        [DllImport("user32.dll")] static extern bool UpdateWindow(IntPtr h);
        [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
        [DllImport("user32.dll")] static extern bool DestroyWindow(IntPtr h);
        [DllImport("user32.dll")] static extern bool PeekMessageW(out Message msg, IntPtr h, uint min, uint max, uint remove);
        [DllImport("user32.dll")] static extern bool TranslateMessage(ref Message msg);
        [DllImport("user32.dll")] static extern IntPtr DispatchMessageW(ref Message msg);
        [DllImport("user32.dll")] static extern IntPtr BeginPaint(IntPtr h, out Paint paint);
        [DllImport("user32.dll")] static extern bool EndPaint(IntPtr h, ref Paint paint);
        [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr h, out Rect rect);
        [DllImport("user32.dll")] static extern int FillRect(IntPtr dc, ref Rect rect, IntPtr brush);
        [DllImport("gdi32.dll")] static extern IntPtr GetStockObject(int index);
    }
}
