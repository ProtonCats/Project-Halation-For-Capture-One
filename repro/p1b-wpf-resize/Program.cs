// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Windows.Forms.Integration;
using WpfBrushes = System.Windows.Media.Brushes;
using WpfGrid = System.Windows.Controls.Grid;

namespace Probes
{
    /// <summary>
    /// P1b: does WPF content inside a Windows Forms window repaint after the window is maximized? A magenta form holds a
    /// lime WPF grid filling it; before and after SC_MAXIMIZE the probe records the window, client and WPF sizes and
    /// samples a 16x16 grid of the client area from the screen. Lime share below 1 after maximize is the symptom.
    /// --variant adds behaviours one at a time: plain, ncsize (own client rectangle when maximized), rgn (window region
    /// and WM_SETREDRAW off while the size changes, then on plus RedrawWindow), strips (eight owned almost invisible
    /// windows around the frame), all.
    /// </summary>
    public static class P1b
    {
        const int MsgNcSize = 0x0083, MsgPosChanging = 0x0046, MsgPosChanged = 0x0047, MsgRedraw = 0x000B;
        const int MsgSysCmd = 0x0112, CmdMax = 0xF030, Strip = 30;
        static string _mode = "plain";

        [STAThread]
        static int Main(string[] args)
        {
            int i = Array.IndexOf(args, "--variant");
            if (i >= 0 && i + 1 < args.Length)
                _mode = args[i + 1];
            Probe.Begin("p1b-wpf-resize");
            Probe.Result["variant"] = _mode;
            Application.EnableVisualStyles();
            var form = new TestForm();
            form.Shown += async (s, e) =>
            {
                try
                {
                    await Task.Delay(1500);
                    Probe.Result["before"] = Sample(form);
                    var seen = new List<string>();
                    var names = new Dictionary<int, string>
                    {
                        [0x000F] = "WM_PAINT", [0x0014] = "WM_ERASEBKGND", [0x0005] = "WM_SIZE", [0x0085] = "WM_NCPAINT",
                        [0x0046] = "WM_WINDOWPOSCHANGING", [0x0047] = "WM_WINDOWPOSCHANGED", [0x0083] = "WM_NCCALCSIZE",
                    };
                    var clock = System.Diagnostics.Stopwatch.StartNew();
                    var source = System.Windows.PresentationSource.FromVisual(form.Fill) as System.Windows.Interop.HwndSource;
                    System.Windows.Interop.HwndSourceHook count = (IntPtr h, int msg, IntPtr w, IntPtr l, ref bool handled) =>
                    {
                        if (names.TryGetValue(msg, out string n) && seen.Count < 64)
                            seen.Add(clock.ElapsedMilliseconds + "ms " + n);
                        return IntPtr.Zero;
                    };
                    source?.AddHook(count);
                    SendMessageW(form.Handle, MsgSysCmd, (IntPtr)CmdMax, IntPtr.Zero);
                    await Task.Delay(2000);
                    source?.RemoveHook(count);
                    Probe.Result["after"] = Sample(form);
                    Probe.Result["wpfChildMessagesAfterMaximize"] = source == null ? "no HwndSource" : (object)seen;
                }
                catch (Exception ex)
                {
                    Probe.Result["exception"] = ex.GetType().Name + ": " + ex.Message;
                }
                Console.WriteLine("p1b-wpf-resize: wrote " + Probe.Write("p1b-wpf-resize"));
                await Task.Delay(Probe.HoldMs());
                form.Close();
            };
            Application.Run(form);
            return 0;
        }

        static Dictionary<string, object> Sample(TestForm form)
        {
            GetWindowRect(form.Handle, out Rect outer);
            GetClientRect(form.Handle, out Rect inner);
            var origin = new Point(0, 0);
            ClientToScreen(form.Handle, ref origin);
            return new Dictionary<string, object>
            {
                ["state"] = form.WindowState.ToString(),
                ["window"] = new[] { outer.Left, outer.Top, outer.Right - outer.Left, outer.Bottom - outer.Top },
                ["client"] = new[] { origin.X, origin.Y, inner.Right, inner.Bottom },
                ["hostControl"] = new[] { form.Wrapper.Width, form.Wrapper.Height },
                ["wpf"] = new[] { form.Fill.ActualWidth, form.Fill.ActualHeight },
                ["dpi"] = GetDpiForWindow(form.Handle),
                ["colours"] = Shares(origin.X, origin.Y, inner.Right, inner.Bottom),
            };
        }

        /// <summary>Share of a 16x16 grid over the client area that reads lime, magenta, black or anything else.</summary>
        static Dictionary<string, object> Shares(int x, int y, int w, int h)
        {
            int lime = 0, magenta = 0, black = 0, rest = 0;
            IntPtr dc = GetDC(IntPtr.Zero);
            try
            {
                for (int row = 0; row < 16; row++)
                    for (int col = 0; col < 16; col++)
                    {
                        uint c = GetPixel(dc, x + (col * 2 + 1) * w / 32, y + (row * 2 + 1) * h / 32);
                        byte red = (byte)c, green = (byte)(c >> 8), blue = (byte)(c >> 16);
                        if (c == 0xFFFFFFFF) rest++;
                        else if (green > 200 && red < 60 && blue < 60) lime++;
                        else if (red > 200 && blue > 200 && green < 60) magenta++;
                        else if (red < 20 && green < 20 && blue < 20) black++;
                        else rest++;
                    }
            }
            finally
            {
                ReleaseDC(IntPtr.Zero, dc);
            }
            return new Dictionary<string, object>
            {
                ["lime"] = lime / 256.0, ["magenta"] = magenta / 256.0, ["black"] = black / 256.0, ["rest"] = rest / 256.0,
            };
        }

        static bool On(string part) => _mode == "all" || _mode == part;

        sealed class TestForm : Form
        {
            public readonly ElementHost Wrapper;
            public readonly WpfGrid Fill;
            readonly List<Form> _strips = new List<Form>();
            bool _paused;

            public TestForm()
            {
                Text = "p1b-wpf-resize";
                BackColor = Color.Magenta;
                StartPosition = FormStartPosition.Manual;
                Location = new Point(40, 40);
                ClientSize = new Size(1265, 599);
                Fill = new WpfGrid { Background = WpfBrushes.Lime };
                Wrapper = new ElementHost { Dock = DockStyle.Fill, Child = Fill };
                Controls.Add(Wrapper);
                if (On("strips"))
                    Shown += (s, e) => MakeStrips();
            }

            protected override void WndProc(ref Message m)
            {
                if (m.Msg == MsgNcSize && m.WParam != IntPtr.Zero && On("ncsize") && WindowState == FormWindowState.Maximized)
                {
                    // Maximized: client = work area minus a strip at the top, one pixel shorter when work area = monitor.
                    var screen = Screen.FromHandle(Handle);
                    var work = screen.WorkingArea;
                    var p = Marshal.PtrToStructure<NcParams>(m.LParam);
                    p.First = new Rect { Left = work.Left, Top = work.Top + Strip, Right = work.Right, Bottom = work.Bottom };
                    if (screen.Primary && work == screen.Bounds)
                        p.First.Bottom -= 1;
                    Marshal.StructureToPtr(p, m.LParam, false);
                    m.Result = IntPtr.Zero;
                    return;
                }
                if (m.Msg == MsgPosChanging && On("rgn"))
                {
                    var pos = Marshal.PtrToStructure<PosInfo>(m.LParam);
                    if ((pos.Flags & 0x0001) == 0)   // SWP_NOSIZE clear
                    {
                        var work = Screen.FromHandle(Handle).WorkingArea;
                        bool full = pos.X <= work.Left && pos.Y <= work.Top && pos.X + pos.Cx >= work.Right && pos.Y + pos.Cy >= work.Bottom;
                        SetWindowRgn(Handle, full ? IntPtr.Zero : CreateRectRgn(0, 0, 30000, 30000), false);
                        SendMessageW(Handle, MsgRedraw, IntPtr.Zero, IntPtr.Zero);
                        _paused = true;
                    }
                }
                base.WndProc(ref m);
                if (m.Msg == MsgPosChanged && _paused)
                {
                    _paused = false;
                    SendMessageW(Handle, MsgRedraw, (IntPtr)1, IntPtr.Zero);
                    RedrawWindow(Handle, IntPtr.Zero, IntPtr.Zero, 0x0001 | 0x0004 | 0x0400 | 0x0080);
                }
                if (m.Msg == MsgPosChanged)
                    PlaceStrips();
            }

            void MakeStrips()
            {
                for (int i = 0; i < 8; i++)
                {
                    var s = new Form
                    {
                        FormBorderStyle = FormBorderStyle.None, ShowInTaskbar = false, Opacity = 0.01,
                        StartPosition = FormStartPosition.Manual, BackColor = Color.White, Owner = this,
                    };
                    _strips.Add(s);
                    s.Show(this);
                }
                PlaceStrips();
            }

            void PlaceStrips()
            {
                if (_strips.Count != 8)
                    return;
                var b = Bounds;
                const int t = 6;
                var spots = new[]
                {
                    new Rectangle(b.Left - t, b.Top - t, t, t), new Rectangle(b.Left, b.Top - t, b.Width, t),
                    new Rectangle(b.Right, b.Top - t, t, t), new Rectangle(b.Right, b.Top, t, b.Height),
                    new Rectangle(b.Right, b.Bottom, t, t), new Rectangle(b.Left, b.Bottom, b.Width, t),
                    new Rectangle(b.Left - t, b.Bottom, t, t), new Rectangle(b.Left - t, b.Top, t, b.Height),
                };
                for (int i = 0; i < 8; i++)
                    _strips[i].Bounds = spots[i];
            }
        }

        [StructLayout(LayoutKind.Sequential)]
        struct Rect
        {
            public int Left, Top, Right, Bottom;
        }

        [StructLayout(LayoutKind.Sequential)]
        struct NcParams
        {
            public Rect First, Second, Third;
            public IntPtr Pos;
        }

        [StructLayout(LayoutKind.Sequential)]
        struct PosInfo
        {
            public IntPtr Hwnd, After;
            public int X, Y, Cx, Cy;
            public uint Flags;
        }

        [DllImport("user32.dll")]
        static extern IntPtr SendMessageW(IntPtr hwnd, int msg, IntPtr w, IntPtr l);

        [DllImport("user32.dll")]
        static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);

        [DllImport("user32.dll")]
        static extern bool GetClientRect(IntPtr hwnd, out Rect rect);

        [DllImport("user32.dll")]
        static extern bool ClientToScreen(IntPtr hwnd, ref Point point);

        [DllImport("user32.dll")]
        static extern uint GetDpiForWindow(IntPtr hwnd);

        [DllImport("user32.dll")]
        static extern IntPtr GetDC(IntPtr hwnd);

        [DllImport("user32.dll")]
        static extern int ReleaseDC(IntPtr hwnd, IntPtr dc);

        [DllImport("gdi32.dll")]
        static extern uint GetPixel(IntPtr dc, int x, int y);

        [DllImport("gdi32.dll")]
        static extern IntPtr CreateRectRgn(int left, int top, int right, int bottom);

        [DllImport("user32.dll")]
        static extern int SetWindowRgn(IntPtr hwnd, IntPtr region, bool redraw);

        [DllImport("user32.dll")]
        static extern bool RedrawWindow(IntPtr hwnd, IntPtr rect, IntPtr region, uint flags);
    }
}
