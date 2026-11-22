// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;

namespace Probes
{
    /// <summary>
    /// P5 dialog half: which file dialog implementation appears for WPF's OpenFileDialog and OpenFolderDialog,
    /// and what it returns. Each dialog opens with a start folder and a name set; a helper thread waits 2 s,
    /// records the dialog's window classes and the loaded comdlg32 and shell32, then presses the dialog's own
    /// OK (or Cancel) button. Positive control: the returned path equals the one set. Negative control: Cancel
    /// returns no path.
    /// </summary>
    public static class P5
    {
        const int IdOk = 1, IdCancel = 2;
        const uint BmClick = 0x00F5, WmCommand = 0x0111;
        static readonly object Gate = new object();
        static IntPtr _main;

        [STAThread]
        static int Main(string[] args)
        {
            Probe.Begin("p5-dialog");
            var app = new Application();
            var window = new Window
            {
                Title = "p5-dialog", Width = 400, Height = 300, Left = 0, Top = 0,
                Background = Brushes.DarkOrange, Content = new Border { Background = Brushes.DarkOrange },
            };
            window.ContentRendered += (s, e) =>
            {
                _main = new WindowInteropHelper(window).Handle;
                RunCases(window);
                Console.WriteLine("p5-dialog: wrote " + Write(null));
                app.Shutdown(0);
            };
            return app.Run(window);
        }

        static void RunCases(Window owner)
        {
            string root = Path.Combine(Path.GetTempPath(), "halation-p5");
            string inner = Path.Combine(root, "inner");
            Directory.CreateDirectory(inner);
            string a = Path.Combine(root, "a.txt"), b = Path.Combine(root, "b.txt");
            File.WriteAllText(a, "a");
            File.WriteAllText(b, "b");
            var cases = new Dictionary<string, object>();
            Probe.Result["cases"] = cases;
            Probe.Result["folder"] = root;

            var file = new Microsoft.Win32.OpenFileDialog { InitialDirectory = root, FileName = a };
            cases["file"] = Case(() => file.ShowDialog(owner), IdOk, () => file.FileNames, new[] { a });

            var folder = new Microsoft.Win32.OpenFolderDialog { InitialDirectory = root, FolderName = inner };
            cases["folder"] = Case(() => folder.ShowDialog(owner), IdOk, () => folder.FolderNames, new[] { inner });

            // Multiple selection, start folder from a path, the selection read back as several names.
            var multi = new Microsoft.Win32.OpenFileDialog
            {
                Multiselect = true, InitialDirectory = root, FileName = "\"a.txt\" \"b.txt\"",
            };
            cases["multi"] = Case(() => multi.ShowDialog(owner), IdOk, () => multi.FileNames, new[] { a, b });

            var cancel = new Microsoft.Win32.OpenFileDialog { InitialDirectory = root, FileName = a };
            cases["cancel"] = Case(() => cancel.ShowDialog(owner), IdCancel, () => cancel.FileNames, null);
        }

        /// <summary>Shows one dialog and presses <paramref name="button"/> from a helper thread.</summary>
        static Dictionary<string, object> Case(Func<bool?> show, int button, Func<string[]> picked, string[] expected)
        {
            var r = new Dictionary<string, object> { ["button"] = button == IdOk ? "ok" : "cancel" };
            var done = new ManualResetEventSlim();
            var helper = new Thread(() => Press(r, button, done)) { IsBackground = true };
            var clock = Stopwatch.StartNew();
            helper.Start();
            bool? shown = null;
            try
            {
                shown = show();
            }
            catch (Exception ex)
            {
                lock (Gate)
                    r["exception"] = ex.GetType().Name + ": " + ex.Message;
            }
            done.Set();
            helper.Join();
            r["showDialog"] = shown;
            r["ms"] = clock.ElapsedMilliseconds;
            string[] got = picked() ?? new string[0];
            r["returned"] = got;
            r["expected"] = expected;
            r["pass"] = expected == null
                ? shown != true
                : shown == true && got.OrderBy(x => x).SequenceEqual(expected.OrderBy(x => x), StringComparer.OrdinalIgnoreCase);
            return r;
        }

        static void Press(Dictionary<string, object> r, int button, ManualResetEventSlim done)
        {
            var clock = Stopwatch.StartNew();
            IntPtr dialog = IntPtr.Zero;
            while (dialog == IntPtr.Zero && clock.ElapsedMilliseconds < 20000 && !done.IsSet)
            {
                dialog = FindDialog();
                if (dialog == IntPtr.Zero)
                    Thread.Sleep(100);
            }
            if (done.IsSet)
                return;
            if (dialog == IntPtr.Zero)
            {
                Abort(r, "no dialog window within 20 s");
                return;
            }
            Thread.Sleep(2000);
            IntPtr control = GetDlgItem(dialog, button);
            lock (Gate)
            {
                r["foundMs"] = clock.ElapsedMilliseconds - 2000;
                r["dialogClass"] = ClassOf(dialog);
                r["dialogTitle"] = TextOf(dialog);
                r["childClasses"] = ChildClasses(dialog);
                r["modules"] = Modules("comdlg32.dll", "shell32.dll", "explorerframe.dll");
                r["pressedWith"] = control != IntPtr.Zero ? "BM_CLICK on " + ClassOf(control) + " '" + TextOf(control) + "'" : "WM_COMMAND";
            }
            if (control != IntPtr.Zero)
                PostMessageW(control, BmClick, IntPtr.Zero, IntPtr.Zero);
            else
                PostMessageW(dialog, WmCommand, (IntPtr)button, IntPtr.Zero);
            if (!done.Wait(15000))
                Abort(r, "ShowDialog did not return within 15 s of the press");
        }

        /// <summary>Writes what is known so far and ends the process: the stuck dialog cannot be closed from here.</summary>
        static void Abort(Dictionary<string, object> r, string why)
        {
            lock (Gate)
                r["aborted"] = why;
            Console.WriteLine("p5-dialog: " + why + "; wrote " + Write(why));
            Environment.Exit(3);
        }

        static string Write(string incomplete)
        {
            lock (Gate)
            {
                Probe.Result["complete"] = incomplete == null;
                if (incomplete != null)
                    Probe.Result["incomplete"] = incomplete;
                return Probe.Write("p5-dialog");
            }
        }

        static IntPtr FindDialog()
        {
            uint self = (uint)Environment.ProcessId;
            IntPtr found = IntPtr.Zero;
            EnumWindows((h, _) =>
            {
                GetWindowThreadProcessId(h, out uint pid);
                if (pid == self && h != _main && IsWindowVisible(h))
                {
                    found = h;
                    return false;
                }
                return true;
            }, IntPtr.Zero);
            return found;
        }

        static List<string> ChildClasses(IntPtr parent)
        {
            var names = new SortedSet<string>(StringComparer.Ordinal);
            EnumChildWindows(parent, (h, _) =>
            {
                names.Add(ClassOf(h));
                return names.Count < 60;
            }, IntPtr.Zero);
            return names.ToList();
        }

        /// <summary>Path of each module and the 16 bytes at offset 0x40, where Wine's builtin DLLs carry "Wine builtin DLL".</summary>
        static Dictionary<string, object> Modules(params string[] names)
        {
            var r = new Dictionary<string, object>();
            foreach (string name in names)
            {
                IntPtr h = Probe.GetModuleHandleW(name);
                if (h == IntPtr.Zero)
                {
                    r[name] = null;
                    continue;
                }
                var path = new StringBuilder(1024);
                GetModuleFileNameW(h, path, path.Capacity);
                var stub = new byte[16];
                Marshal.Copy(h + 0x40, stub, 0, stub.Length);
                string text = Encoding.ASCII.GetString(stub);
                r[name] = new Dictionary<string, object>
                {
                    ["path"] = path.ToString(), ["builtin"] = text == "Wine builtin DLL",
                };
            }
            return r;
        }

        static string ClassOf(IntPtr h)
        {
            var sb = new StringBuilder(256);
            GetClassNameW(h, sb, sb.Capacity);
            return sb.ToString();
        }

        static string TextOf(IntPtr h)
        {
            var sb = new StringBuilder(512);
            GetWindowTextW(h, sb, sb.Capacity);
            return sb.ToString();
        }

        delegate bool EnumProc(IntPtr hwnd, IntPtr param);

        [DllImport("user32.dll")]
        static extern bool EnumWindows(EnumProc proc, IntPtr param);

        [DllImport("user32.dll")]
        static extern bool EnumChildWindows(IntPtr parent, EnumProc proc, IntPtr param);

        [DllImport("user32.dll")]
        static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);

        [DllImport("user32.dll")]
        static extern bool IsWindowVisible(IntPtr hwnd);

        [DllImport("user32.dll")]
        static extern IntPtr GetDlgItem(IntPtr dialog, int id);

        [DllImport("user32.dll")]
        static extern bool PostMessageW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        static extern int GetClassNameW(IntPtr hwnd, StringBuilder name, int count);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        static extern int GetWindowTextW(IntPtr hwnd, StringBuilder text, int count);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        static extern uint GetModuleFileNameW(IntPtr module, StringBuilder path, int size);
    }
}
