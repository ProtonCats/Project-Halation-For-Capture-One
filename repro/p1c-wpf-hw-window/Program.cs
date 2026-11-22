// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Shapes;
using System.Windows.Threading;

namespace Probes
{
    public static class P1c
    {
        const uint NoSize = 0x0001, NoMove = 0x0002, NoZOrder = 0x0004, NoActivate = 0x0010;
        const int SysCommand = 0x0112, Maximize = 0xF030, Restore = 0xF120;
        static readonly List<string> Errors = new List<string>();
        static readonly List<Dictionary<string, object>> Phases = new List<Dictionary<string, object>>();
        static IntPtr handle;
        static int holdMs = 4000;

        [STAThread]
        static int Main(string[] args)
        {
            bool created = false, started = false;
            try
            {
                Probe.Begin("p1c-wpf-hw-window");
                int index = Array.IndexOf(args, "--hold");
                if (index >= 0)
                {
                    if (index + 1 < args.Length && double.TryParse(args[index + 1], NumberStyles.Float,
                        CultureInfo.InvariantCulture, out double seconds) && double.IsFinite(seconds) &&
                        seconds >= 0 && seconds <= int.MaxValue / 1000.0)
                        holdMs = (int)Math.Ceiling(seconds * 1000);
                    else
                        Errors.Add("invalid --hold; using 4 seconds");
                }
                Probe.Result["holdMs"] = holdMs;
                Probe.Result["rectFormat"] = "left,top,right,bottom";
                Probe.Result["squareOffsetDips"] = new[] { 80, 80 };
                Probe.Result["squareSizeDips"] = new[] { 200, 200 };
                var app = new Application();
                var canvas = new Canvas { Background = Brushes.Lime, ClipToBounds = true };
                var square = new Rectangle { Width = 200, Height = 200, Fill = Brushes.Magenta };
                Canvas.SetLeft(square, 80);
                Canvas.SetTop(square, 80);
                canvas.Children.Add(square);
                var window = new Window
                {
                    Title = "p1c-wpf-hw-window", Width = 900, Height = 600, Left = 100, Top = 100,
                    WindowStartupLocation = WindowStartupLocation.Manual, Background = Brushes.Lime, Content = canvas,
                };
                window.SourceInitialized += (s, e) =>
                {
                    handle = new WindowInteropHelper(window).Handle;
                    created = handle != IntPtr.Zero;
                };
                window.Loaded += async (s, e) =>
                {
                    if (started) return;
                    started = true;
                    try
                    {
                        if (!created) throw new InvalidOperationException("window has no HWND");
                        await RunPhases(window);
                    }
                    catch (Exception ex)
                    {
                        Errors.Add(ex.GetType().Name + ": " + ex.Message);
                    }
                    finally
                    {
                        app.Shutdown(0);
                    }
                };
                app.Run(window);
            }
            catch (Exception ex)
            {
                Errors.Add(ex.GetType().Name + ": " + ex.Message);
            }
            if (Phases.Count != 5) Errors.Add("phase sequence incomplete");
            Probe.Result["phases"] = Phases;
            Probe.Result["windowCreated"] = created;
            Probe.Result["d3d9ModulePath"] = D3D9Path("final");
            Probe.Result["errors"] = Errors;
            try
            {
                Console.WriteLine("p1c-wpf-hw-window: wrote " + Probe.Write("p1c-wpf-hw-window"));
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("result write failed: " + ex.GetType().Name + ": " + ex.Message);
            }
            return created ? 0 : 1;
        }

        static async Task RunPhases(Window window)
        {
            var steps = new (string Name, Action Change)[]
            {
                ("start", () => Place(100, 100, 900, 600, 0)),
                ("moved", () => Place(300, 200, 0, 0, NoSize)),
                ("resized", () => Place(0, 0, 1400, 900, NoMove)),
                ("maximized", () => { SendMessageW(handle, SysCommand, (IntPtr)Maximize, IntPtr.Zero); }),
                ("restored", () => { SendMessageW(handle, SysCommand, (IntPtr)Restore, IntPtr.Zero); }),
            };
            foreach (var step in steps)
            {
                try { step.Change(); }
                catch (Exception ex) { Errors.Add(step.Name + ": " + ex.GetType().Name + ": " + ex.Message); }
                await window.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ContextIdle);
                await Task.Delay(200);
                long stamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                try { Phases.Add(Sample(step.Name, stamp)); }
                catch (Exception ex)
                {
                    Errors.Add(step.Name + ": " + ex.GetType().Name + ": " + ex.Message);
                    Phases.Add(new Dictionary<string, object> { ["name"] = step.Name, ["unixMs"] = stamp });
                }
                Console.WriteLine("PHASE " + step.Name + " " + stamp.ToString(CultureInfo.InvariantCulture));
                Console.Out.Flush();
                await Task.Delay(holdMs);
            }
        }

        static void Place(int x, int y, int width, int height, uint flags)
        {
            if (!SetWindowPos(handle, IntPtr.Zero, x, y, width, height, flags | NoZOrder | NoActivate))
                Errors.Add("SetWindowPos: " + Marshal.GetLastWin32Error());
        }

        static Dictionary<string, object> Sample(string phase, long stamp)
        {
            bool outerOk = GetWindowRect(handle, out NativeRect outer);
            if (!outerOk) Errors.Add(phase + " GetWindowRect: " + Marshal.GetLastWin32Error());
            bool clientOk = GetClientRect(handle, out NativeRect client);
            if (!clientOk) Errors.Add(phase + " GetClientRect: " + Marshal.GetLastWin32Error());
            var origin = new NativePoint { X = client.Left, Y = client.Top };
            var corner = new NativePoint { X = client.Right, Y = client.Bottom };
            bool originOk = ClientToScreen(handle, ref origin);
            if (!originOk) Errors.Add(phase + " ClientToScreen origin: " + Marshal.GetLastWin32Error());
            bool cornerOk = ClientToScreen(handle, ref corner);
            if (!cornerOk) Errors.Add(phase + " ClientToScreen corner: " + Marshal.GetLastWin32Error());
            bool screenOk = clientOk && originOk && cornerOk;
            var screen = new NativeRect { Left = origin.X, Top = origin.Y, Right = corner.X, Bottom = corner.Y };
            var children = new List<Dictionary<string, object>>();
            bool anyDifferent = false, childRectsOk = true;
            EnumWindowsProc collect = (child, state) =>
            {
                try
                {
                    bool rectOk = GetWindowRect(child, out NativeRect rect);
                    if (!rectOk) Errors.Add(phase + " child GetWindowRect: " + Marshal.GetLastWin32Error());
                    childRectsOk &= rectOk;
                    bool differs = rectOk && screenOk && !Same(rect, screen);
                    anyDifferent |= differs;
                    var name = new StringBuilder(256);
                    int length = GetClassNameW(child, name, name.Capacity);
                    if (length == 0) Errors.Add(phase + " GetClassNameW: " + Marshal.GetLastWin32Error());
                    uint thread = GetWindowThreadProcessId(child, out uint process);
                    if (thread == 0) Errors.Add(phase + " GetWindowThreadProcessId: " + Marshal.GetLastWin32Error());
                    children.Add(new Dictionary<string, object>
                    {
                        ["hwnd"] = Hex(child), ["className"] = name.ToString(), ["windowRect"] = rectOk ? rect.Array() : null,
                        ["visible"] = IsWindowVisible(child), ["threadId"] = thread, ["processId"] = process,
                        ["rectDiffersFromClient"] = rectOk && screenOk ? (object)differs : null,
                    });
                }
                catch (Exception ex)
                {
                    childRectsOk = false;
                    Errors.Add(phase + " child: " + ex.GetType().Name + ": " + ex.Message);
                    children.Add(new Dictionary<string, object> { ["hwnd"] = Hex(child), ["error"] = ex.Message });
                }
                return true;
            };
            bool enumerated = EnumChildWindows(handle, collect, IntPtr.Zero);
            return new Dictionary<string, object>
            {
                ["name"] = phase, ["unixMs"] = stamp, ["renderTier"] = RenderCapability.Tier >> 16,
                ["dpi"] = GetDpiForWindow(handle),
                ["processRenderMode"] = RenderOptions.ProcessRenderMode.ToString(), ["hwnd"] = Hex(handle),
                ["windowRect"] = outerOk ? outer.Array() : null, ["clientRect"] = clientOk ? client.Array() : null,
                ["clientScreenRect"] = screenOk ? screen.Array() : null, ["descendants"] = children,
                ["enumChildWindowsReturned"] = enumerated,
                ["anyDescendantRectDiffers"] = screenOk && childRectsOk ? (object)anyDifferent : null,
                ["d3d9ModulePath"] = D3D9Path(phase),
            };
        }

        static string D3D9Path(string phase)
        {
            try
            {
                using (var process = Process.GetCurrentProcess())
                    foreach (ProcessModule module in process.Modules)
                        if (string.Equals(module.ModuleName, "d3d9.dll", StringComparison.OrdinalIgnoreCase))
                            return module.FileName;
            }
            catch (Exception ex) { Errors.Add(phase + " modules: " + ex.GetType().Name + ": " + ex.Message); }
            return null;
        }

        static string Hex(IntPtr hwnd) => "0x" + hwnd.ToInt64().ToString("x");
        static bool Same(NativeRect a, NativeRect b) => a.Left == b.Left && a.Top == b.Top && a.Right == b.Right && a.Bottom == b.Bottom;

        [StructLayout(LayoutKind.Sequential)]
        struct NativeRect
        {
            public int Left, Top, Right, Bottom;
            public int[] Array() => new[] { Left, Top, Right, Bottom };
        }

        [StructLayout(LayoutKind.Sequential)]
        struct NativePoint
        {
            public int X, Y;
        }

        delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr state);

        [DllImport("user32.dll", SetLastError = true)]
        static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int width, int height, uint flags);
        [DllImport("user32.dll")]
        static extern IntPtr SendMessageW(IntPtr hwnd, int message, IntPtr wparam, IntPtr lparam);
        [DllImport("user32.dll", SetLastError = true)]
        static extern bool GetWindowRect(IntPtr hwnd, out NativeRect rect);
        [DllImport("user32.dll", SetLastError = true)]
        static extern bool GetClientRect(IntPtr hwnd, out NativeRect rect);
        [DllImport("user32.dll", SetLastError = true)]
        static extern bool ClientToScreen(IntPtr hwnd, ref NativePoint point);
        [DllImport("user32.dll")]
        static extern bool EnumChildWindows(IntPtr hwnd, EnumWindowsProc callback, IntPtr state);
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern int GetClassNameW(IntPtr hwnd, StringBuilder name, int count);
        [DllImport("user32.dll")]
        static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll", SetLastError = true)]
        static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
        [DllImport("user32.dll")]
        static extern uint GetDpiForWindow(IntPtr hwnd);
    }
}
