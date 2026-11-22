// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;

namespace Probes
{
    public static class P1d
    {
        const string Name = "p1d-wpf-input";
        static readonly object Gate = new object();
        static readonly List<Dictionary<string, object>> Events = new List<Dictionary<string, object>>();
        static readonly List<Dictionary<string, string>> Errors = new List<Dictionary<string, string>>();
        static readonly Dictionary<string, string> WindowTitles = new Dictionary<string, string>();
        static readonly Dictionary<string, long> Counts = new Dictionary<string, long>
        {
            ["previewMouseWheel"] = 0, ["mouseWheel"] = 0, ["scrollChanged"] = 0, ["rawWheel"] = 0,
            ["key"] = 0, ["text"] = 0, ["mousedown"] = 0, ["click"] = 0, ["focus"] = 0,
        };
        static readonly SortedSet<int> WheelDeltas = new SortedSet<int>();
        static string mode = "input";
        static string rawInput = "none";
        static int rawInputError = -1, rawInputMessages;

        [StructLayout(LayoutKind.Sequential)]
        struct RawInputDevice
        {
            public ushort UsagePage, Usage;
            public uint Flags;
            public IntPtr Target;
        }

        [DllImport("user32.dll", SetLastError = true)]
        static extern bool RegisterRawInputDevices(RawInputDevice[] devices, uint count, uint size);
        static string ownerMode = "none";
        static int holdMs = 30000;
        static int stallMs, stalls, autotypeMs, autotyped;
        static readonly List<long> AutotypeTimes = new List<long>();
        static bool finished;
        static int exitCode = 1;

        [STAThread]
        static int Main(string[] args)
        {
            AppDomain.CurrentDomain.ProcessExit += (s, e) =>
                Environment.ExitCode = Finish(1, new InvalidOperationException("process exited before the hold ended"));
            AppDomain.CurrentDomain.UnhandledException += (s, e) =>
                Environment.Exit(Finish(1, e.ExceptionObject as Exception ?? new Exception("unhandled exception")));
            try
            {
                Probe.Begin(Name);
                UpdateResult();
                if (!Options(args))
                    return Finish(1, new ArgumentException("usage: P1dWpfInput [--mode input|wheel] [--rawinput none|mouse] [--hold SECONDS] [--owner none|hidden|shown-small|popup] [--stall MILLISECONDS] [--autotype MILLISECONDS]"));
                UpdateResult();
                using (var deadline = new Timer(state => Environment.Exit(Finish(0)), null, holdMs, Timeout.Infinite))
                {
                    var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
                    app.DispatcherUnhandledException += (s, e) =>
                    {
                        e.Handled = true;
                        app.Shutdown(Finish(1, e.Exception));
                    };
                    var input = new TextBox
                    {
                        Name = "input", Height = 120, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap,
                        VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Margin = new Thickness(0, 0, 0, 12),
                    };
                    var button = new Button
                    {
                        Name = "button", Content = "Click", Width = 120, Height = 40,
                        HorizontalAlignment = HorizontalAlignment.Left,
                    };
                    var grid = new Grid { Margin = new Thickness(24) };
                    grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                    grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                    Grid.SetRow(button, 1);
                    grid.Children.Add(input);
                    grid.Children.Add(button);
                    var main = new Window
                    {
                        Name = "main", Title = "p1d", Width = 800, Height = mode == "wheel" ? 460 : 400, Left = 100, Top = 100,
                        ResizeMode = ResizeMode.NoResize, WindowStartupLocation = WindowStartupLocation.Manual,
                        Content = mode == "wheel" ? WheelPanel() : grid,
                    };
                    app.MainWindow = main;
                    Observe(main);
                    button.Click += (s, e) => Emit("click", new { window = main.Title, target = button.Name });
                    if (mode == "input") FocusManager.SetFocusedElement(main, input);
                    if (ownerMode == "hidden" || ownerMode == "shown-small")
                    {
                        var owner = new Window
                        {
                            Name = "owner", Title = "p1d-owner", Width = 541, Height = 326, Left = 950, Top = 100,
                            ShowInTaskbar = false, WindowStartupLocation = WindowStartupLocation.Manual,
                        };
                        Observe(owner);
                        if (ownerMode == "shown-small")
                            owner.Show();
                        else
                            new WindowInteropHelper(owner).EnsureHandle();
                        main.Owner = owner;
                    }
                    bool started = false;
                    main.ContentRendered += (s, e) =>
                    {
                        if (started) return;
                        started = true;
                        Probe.Result["renderTierAtStart"] = RenderCapability.Tier >> 16;
                        if (autotypeMs > 0 && mode == "input")
                        {
                            // Append one character on a timer: text changes with no input at all.
                            var typing = new System.Windows.Threading.DispatcherTimer { Interval = TimeSpan.FromMilliseconds(autotypeMs) };
                            typing.Tick += (s3, e3) => { input.AppendText(((char)('a' + autotyped % 26)).ToString()); lock (AutotypeTimes) { autotyped++; AutotypeTimes.Add(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()); } };
                            typing.Start();
                        }
                        if (stallMs > 0)
                        {
                            // Block the UI thread so input events queue unread, then pump for 300 ms.
                            var stall = new System.Windows.Threading.DispatcherTimer { Interval = TimeSpan.FromMilliseconds(stallMs + 300) };
                            stall.Tick += (s2, e2) => { Thread.Sleep(stallMs); stalls++; };
                            stall.Start();
                        }
                        if (mode == "input")
                        {
                            input.Focus();
                            Keyboard.Focus(input);
                        }
                        if (ownerMode == "popup")
                        {
                            var popup = new Window
                            {
                                Name = "popup", Title = "p1d-popup", Width = 240, Height = 160,
                                Left = main.Left + main.Width - 260, Top = main.Top + main.Height - 200,
                                Owner = main, ShowActivated = false, ShowInTaskbar = false,
                                WindowStartupLocation = WindowStartupLocation.Manual,
                            };
                            Observe(popup);
                            popup.Show();
                        }
                    };
                    main.Closed += (s, e) => app.Shutdown(Finish(1,
                        new InvalidOperationException("main window closed before the hold ended")));
                    app.Run(main);
                    return Finish(1, new InvalidOperationException("application stopped before the hold ended"));
                }
            }
            catch (Exception error)
            {
                return Finish(1, error);
            }
        }

        static bool Options(string[] args)
        {
            for (int i = 0; i < args.Length; i++)
            {
                if (i + 1 >= args.Length) return false;
                string option = args[i], value = args[++i];
                if (option == "--hold")
                {
                    if (!double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out double seconds)
                        || !double.IsFinite(seconds) || seconds < 0 || seconds > int.MaxValue / 1000.0)
                        return false;
                    holdMs = (int)Math.Ceiling(seconds * 1000);
                }
                else if (option == "--rawinput")
                {
                    if (value != "none" && value != "mouse") return false;
                    rawInput = value;
                }
                else if (option == "--autotype")
                {
                    if (!int.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out autotypeMs) || autotypeMs is < 10 or > 10000)
                        return false;
                }
                else if (option == "--stall")
                {
                    if (!int.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out stallMs) || stallMs > 10000)
                        return false;
                }
                else if (option == "--mode")
                {
                    if (value != "input" && value != "wheel") return false;
                    mode = value;
                }
                else if (option == "--owner")
                {
                    if (value != "none" && value != "hidden" && value != "shown-small" && value != "popup")
                        return false;
                    ownerMode = value;
                }
                else return false;
            }
            return true;
        }

        static FrameworkElement WheelPanel()
        {
            var rows = new StackPanel { Name = "wheelRows" };
            for (int i = 1; i <= 30; i++)
            {
                var row = new StackPanel { Name = "row" + i, Orientation = Orientation.Horizontal,
                    Height = 48, Margin = new Thickness(4) };
                row.Children.Add(new TextBlock { Name = "label" + i, Text = "Tool section " + i,
                    Width = 180, VerticalAlignment = VerticalAlignment.Center });
                row.Children.Add(new Slider { Name = "slider" + i, Minimum = 0, Maximum = 100,
                    Value = 50, Width = 400, VerticalAlignment = VerticalAlignment.Center });
                rows.Children.Add(row);
            }
            var viewer = new ScrollViewer { Name = "toolScroll", Height = 300, Content = rows,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
            viewer.ScrollChanged += (s, e) => Emit("scrollChanged", new
            {
                window = Window.GetWindow(viewer)?.Title, target = viewer.Name,
                VerticalOffset = e.VerticalOffset, VerticalChange = e.VerticalChange,
            });
            var panel = new StackPanel { Margin = new Thickness(16) };
            panel.Children.Add(viewer);
            panel.Children.Add(new TextBlock { Name = "outsideWheel", Height = 40,
                Text = "Plain area outside the tool panel: point here to compare wheel delivery.",
                Margin = new Thickness(0, 8, 0, 0) });
            return panel;
        }

        static void WheelEvent(Window window, string kind, MouseWheelEventArgs e)
        {
            var detail = new Dictionary<string, object>
            {
                ["window"] = window.Title, ["target"] = Target(e.OriginalSource),
                ["source"] = Target(e.Source), ["Delta"] = e.Delta,
                ["Handled"] = e.Handled, ["HandledAfterRouting"] = null,
            };
            lock (Gate) WheelDeltas.Add(e.Delta);
            Emit(kind, detail);
            // The window's preview handler runs before children can handle the event.
            // Read the same args after the synchronous route has returned to the dispatcher.
            window.Dispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() =>
            {
                lock (Gate)
                {
                    if (!finished) detail["HandledAfterRouting"] = e.Handled;
                }
            }));
        }

        // Raw mouse input (usage page 1, usage 2), delivered even when the window is not in front.
        static void RegisterMouse(Window window)
        {
            var hwnd = new WindowInteropHelper(window).Handle;
            var devices = new[] { new RawInputDevice { UsagePage = 1, Usage = 2, Flags = 0x100, Target = hwnd } };
            bool ok = RegisterRawInputDevices(devices, 1, (uint)Marshal.SizeOf<RawInputDevice>());
            rawInputError = ok ? 0 : Marshal.GetLastWin32Error();
            HwndSource.FromHwnd(hwnd).AddHook((IntPtr h, int message, IntPtr w, IntPtr l, ref bool handled) =>
            {
                if (message == 0x00ff) Interlocked.Increment(ref rawInputMessages);
                return IntPtr.Zero;
            });
        }

        static void ObserveWheel(Window window)
        {
            window.AddHandler(Mouse.PreviewMouseWheelEvent,
                new MouseWheelEventHandler((s, e) => WheelEvent(window, "previewMouseWheel", e)), true);
            window.AddHandler(Mouse.MouseWheelEvent,
                new MouseWheelEventHandler((s, e) => WheelEvent(window, "mouseWheel", e)), true);
            window.SourceInitialized += (s, e) =>
            {
                var source = HwndSource.FromHwnd(new WindowInteropHelper(window).Handle);
                source.AddHook((IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam, ref bool handled) =>
                {
                    if (message == 0x020a || message == 0x020e)
                    {
                        int delta = unchecked((short)((wParam.ToInt64() >> 16) & 0xffff));
                        long point = lParam.ToInt64();
                        lock (Gate) WheelDeltas.Add(delta);
                        Emit("rawWheel", new
                        {
                            window = window.Title, message = message == 0x020a ? "WM_MOUSEWHEEL" : "WM_MOUSEHWHEEL",
                            Delta = delta, x = unchecked((short)(point & 0xffff)),
                            y = unchecked((short)((point >> 16) & 0xffff)), Handled = handled,
                        });
                    }
                    return IntPtr.Zero;
                });
            };
        }

        static void Observe(Window window)
        {
            if (mode == "wheel") ObserveWheel(window);
            if (rawInput == "mouse" && window.Name == "main") window.SourceInitialized += (s, e) => RegisterMouse(window);
            lock (Gate)
                WindowTitles[window.Name] = window.Title;
            window.AddHandler(Keyboard.PreviewKeyDownEvent, new KeyEventHandler((s, e) =>
                Emit("key", new
                {
                    window = window.Title, target = Target(e.OriginalSource), key = e.Key.ToString(),
                    systemKey = e.SystemKey.ToString(), modifiers = Keyboard.Modifiers.ToString(), repeat = e.IsRepeat,
                })), true);
            window.AddHandler(TextCompositionManager.TextInputEvent, new TextCompositionEventHandler((s, e) =>
                Emit("text", new { window = window.Title, target = Target(e.OriginalSource), text = e.Text })), true);
            window.AddHandler(Mouse.PreviewMouseDownEvent, new MouseButtonEventHandler((s, e) =>
            {
                Point point = e.GetPosition(window);
                Emit("mousedown", new
                {
                    window = window.Title, target = Target(e.OriginalSource), button = e.ChangedButton.ToString(),
                    clicks = e.ClickCount, x = point.X, y = point.Y,
                });
            }), true);
            window.AddHandler(Keyboard.GotKeyboardFocusEvent, new KeyboardFocusChangedEventHandler((s, e) =>
                Emit("focus", new { window = window.Title, target = Target(e.NewFocus), previous = Target(e.OldFocus) })), true);
        }

        static string Target(object value)
        {
            if (value is FrameworkElement element && !string.IsNullOrEmpty(element.Name)) return element.Name;
            return value == null ? null : value.GetType().Name;
        }

        static void Emit(string kind, object detail)
        {
            lock (Gate)
            {
                if (finished) return;
                Counts[kind]++;
                var row = Row(kind, detail);
                Events.Add(row);
                Write(row);
            }
        }

        static void UpdateResult()
        {
            Probe.Result["mode"] = mode;
            Probe.Result["rawInput"] = new Dictionary<string, object>
            {
                ["requested"] = rawInput, ["registerError"] = rawInputError, ["messages"] = rawInputMessages,
            };
            Probe.Result["distinctWheelDeltas"] = new List<int>(WheelDeltas);
            Probe.Result["events"] = Events;
            Probe.Result["counts"] = Counts;
            Probe.Result["errors"] = Errors;
            Probe.Result["ownerMode"] = ownerMode;
            Probe.Result["holdMs"] = holdMs;
            Probe.Result["stallMs"] = stallMs;
            Probe.Result["stalls"] = stalls;
            Probe.Result["autotypeMs"] = autotypeMs;
            Probe.Result["autotyped"] = autotyped;
            lock (AutotypeTimes) Probe.Result["autotypeUnixMs"] = new List<long>(AutotypeTimes);
            Probe.Result["renderTierAtEnd"] = RenderCapability.Tier >> 16;
            Probe.Result["windowTitles"] = WindowTitles;
            Probe.Result["exitCode"] = exitCode;
        }

        static void Error(Exception error)
        {
            Errors.Add(new Dictionary<string, string>
            {
                ["type"] = error.GetType().FullName, ["message"] = error.Message,
            });
        }

        static int Finish(int code, Exception error = null)
        {
            lock (Gate)
            {
                if (finished) return exitCode;
                finished = true;
                if (error != null) Error(error);
                exitCode = Errors.Count == 0 ? code : 1;
                UpdateResult();
                var summary = Row("summary", new { mode, owner = ownerMode, distinctWheelDeltas = new List<int>(WheelDeltas), holdSeconds = holdMs / 1000.0, counts = Counts });
                Events.Add(summary);
                try
                {
                    Probe.Write(Name);
                }
                catch (Exception failure)
                {
                    Error(failure);
                    exitCode = 1;
                    UpdateResult();
                }
                try
                {
                    Write(summary);
                }
                catch (Exception failure)
                {
                    Error(failure);
                    exitCode = 1;
                    UpdateResult();
                    try { Probe.Write(Name); }
                    catch (Exception writeFailure) { Error(writeFailure); }
                }
                return exitCode;
            }
        }

        static Dictionary<string, object> Row(string kind, object detail)
        {
            return new Dictionary<string, object>
            {
                ["t"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(), ["event"] = kind, ["detail"] = detail,
            };
        }

        static void Write(Dictionary<string, object> row)
        {
            Console.WriteLine(JsonSerializer.Serialize(row));
            Console.Out.Flush();
        }
    }
}
