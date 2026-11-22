// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace Probes
{
    /// <summary>
    /// P5b: the common open dialog driven through COM, with no wrapper. Multiple selection, start folder from a
    /// parsing name; 2 s into Show a thread timer (same thread, inside the dialog's loop) records QueryInterface
    /// answers, the shell view's selection through IFolderView2 and the dialog's own selected items, then closes it.
    /// --accept follows a hook-based accept path instead: an object-creation WinEvent hook finds the dialog, a
    /// WH_CALLWNDPROC hook on its thread catches the OK command, reads GetSelection(TRUE) while the dialog is still
    /// open and clicks Cancel; once with every item selected, once with none.
    /// Vtable slots follow the public IDL; call_as stubs take no slot.
    /// </summary>
    public static unsafe class P5b
    {
        static readonly Guid ClsidFileOpenDialog = new Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7");
        static readonly Guid IidFileOpenDialog = new Guid("D57C7288-D4AD-4768-BE02-9D969532D960");
        static readonly Guid IidFileDialog = new Guid("42F85136-DB7E-439C-85F1-E4075D135FC8");
        static readonly Guid IidShellItem = new Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe");
        static readonly Guid IidServiceProvider = new Guid("6d5140c1-7436-11ce-8034-00aa006009fa");
        static readonly Guid SidFolderView = new Guid("cde725b0-ccc9-4519-917e-325d72fab4ce");
        static readonly Guid IidFolderView2 = new Guid("1af3a467-214f-4298-908e-06b03e0b39f9");
        static readonly Guid IidShellItem2 = new Guid("7e9fb0d3-919f-4307-ab2e-9b1860310c93");
        static readonly Guid IidShellFolder = new Guid("000214E6-0000-0000-C000-000000000046");
        static readonly (string Name, Guid Iid)[] Queries =
        {
            ("IAgileObject", new Guid("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90")),
            ("INoMarshal", new Guid("ecc8691b-c1db-4dc0-855e-65f6c551af49")),
            ("{5c13e51c-4f32-4726-a3fd-f3edd63da3a0}", new Guid("5c13e51c-4f32-4726-a3fd-f3edd63da3a0")),
            ("IFileDialog (positive)", IidFileDialog),
            ("nowhere (negative)", new Guid("0badf00d-0000-4000-8000-00000000c0de")),
        };

        const uint FosAllowMultiselect = 0x200, FosAllNonStorageItems = 0x80, SvgioAllview = 2, SvsiSelect = 1;
        const uint SigdnFilesyspath = 0x80058000, EventObjectCreate = 0x8000, WmCommand = 0x0111, BmClick = 0x00F5;
        const int WhCallWndProc = 4, IdOk = 1, IdCancel = 2;
        const int Cancelled = unchecked((int)0x800704C7);
        static readonly object Gate = new object();
        static readonly ManualResetEventSlim Done = new ManualResetEventSlim();
        static IntPtr _dialog, _dialogHwnd, _callHook;
        static bool _selectAll, _collected;
        static Dictionary<string, object> _case;

        [StructLayout(LayoutKind.Sequential)]
        struct CwpStruct
        {
            public IntPtr LParam;
            public IntPtr WParam;
            public uint Message;
            public IntPtr Hwnd;
        }

        [STAThread]
        static int Main(string[] args)
        {
            bool accept = Array.IndexOf(args, "--accept") >= 0, item = Array.IndexOf(args, "--item") >= 0;
            string name = accept ? "p5b-accept" : item ? "p5b-item" : "p5b-dialog-com";
            Probe.Begin(name);
            string root = Path.Combine(Path.GetTempPath(), "halation-p5b");
            Directory.CreateDirectory(Path.Combine(root, "inner"));
            File.WriteAllText(Path.Combine(root, "a.txt"), "a");
            File.WriteAllText(Path.Combine(root, "b.txt"), "b");
            Probe.Result["folder"] = root;
            new Thread(() => Watchdog(name)) { IsBackground = true }.Start();
            try
            {
                if (item)
                {
                    _case = Probe.Result;
                    RunItem(root);
                }
                else if (accept)
                {
                    var cases = new Dictionary<string, object>();
                    Probe.Result["cases"] = cases;
                    foreach (bool select in new[] { true, false })
                    {
                        _case = new Dictionary<string, object>();
                        lock (Gate)
                            cases[select ? "selected" : "none"] = _case;
                        _selectAll = select;
                        RunAccept(root);
                    }
                }
                else
                {
                    _case = Probe.Result;
                    Run(root);
                }
            }
            catch (Exception ex)
            {
                Set("exception", ex.GetType().Name + ": " + ex.Message);
            }
            Done.Set();
            Console.WriteLine(name + ": wrote " + Write(name, null));
            return 0;
        }

        static IntPtr CreateDialog(string root, uint extraOptions)
        {
            Guid clsid = ClsidFileOpenDialog, iid = IidFileOpenDialog;
            IntPtr dialog;
            int hr = CoCreateInstance(&clsid, IntPtr.Zero, 1, &iid, &dialog);
            Set("create", Probe.Hex(hr));
            if (hr < 0)
                return IntPtr.Zero;
            uint options;
            Set("getOptions", Probe.Hex(((delegate* unmanaged<IntPtr, uint*, int>)Slot(dialog, 10))(dialog, &options)));
            Set("setOptions", Probe.Hex(((delegate* unmanaged<IntPtr, uint, int>)Slot(dialog, 9))(dialog, options | extraOptions)));
            Guid itemIid = IidShellItem;
            IntPtr folder;
            hr = SHCreateItemFromParsingName(root, IntPtr.Zero, &itemIid, &folder);
            Set("parsingName", Probe.Hex(hr));
            if (hr >= 0)
            {
                Set("setFolder", Probe.Hex(((delegate* unmanaged<IntPtr, IntPtr, int>)Slot(dialog, 12))(dialog, folder)));
                Release(folder);
            }
            return dialog;
        }

        static void Run(string root)
        {
            IntPtr dialog = CreateDialog(root, FosAllowMultiselect);
            if (dialog == IntPtr.Zero)
                return;
            _dialog = dialog;
            SetTimer(IntPtr.Zero, 0, 2000, &OnTimer);
            Set("show", Probe.Hex(((delegate* unmanaged<IntPtr, IntPtr, int>)Slot(dialog, 3))(dialog, IntPtr.Zero)));
            _dialog = IntPtr.Zero;
            Release(dialog);
        }

        [UnmanagedCallersOnly]
        static void OnTimer(IntPtr hwnd, uint msg, nuint id, uint time)
        {
            KillTimer(IntPtr.Zero, id);
            IntPtr dialog = _dialog;
            try
            {
                var q1 = new Dictionary<string, object>();
                foreach (var (name, iid) in Queries)
                    q1[name] = Probe.Hex(QueryInterface(dialog, iid, out IntPtr p));
                Set("Q1", q1);
                Set("Q2", FolderView(dialog));
                IntPtr items;
                int hr = ((delegate* unmanaged<IntPtr, IntPtr*, int>)Slot(dialog, 28))(dialog, &items);
                Set("S2", ItemArray(hr, items));
            }
            catch (Exception ex)
            {
                Set("timerException", ex.GetType().Name + ": " + ex.Message);
            }
            Set("close", Probe.Hex(((delegate* unmanaged<IntPtr, int, int>)Slot(dialog, 23))(dialog, Cancelled)));
        }

        /// <summary>
        /// --item: SHCreateItemWithParent for a file, from a parent ID list, from a parent folder object (no parent ID
        /// list) and from both, asking for IShellItem and IShellItem2. Positive control: SHCreateItemFromParsingName on
        /// the same file. Negative control: an interface the item does not have.
        /// </summary>
        static void RunItem(string root)
        {
            string file = Path.Combine(root, "a.txt");
            Guid iidItem = IidShellItem;
            IntPtr direct;
            int hr = SHCreateItemFromParsingName(file, IntPtr.Zero, &iidItem, &direct);
            Set("controlParsingName", ItemName(hr, direct));
            IntPtr abs;
            uint attrs;
            hr = SHParseDisplayName(file, IntPtr.Zero, &abs, 0, &attrs);
            Set("parseDisplayName", Probe.Hex(hr));
            if (hr < 0)
                return;
            IntPtr child = ILClone(ILFindLastID(abs)), parent = ILClone(abs);
            ILRemoveLastID(parent);
            Set("pidlParent-IShellItem", WithParent(parent, IntPtr.Zero, child, IidShellItem));
            Set("pidlParent-IShellItem2", WithParent(parent, IntPtr.Zero, child, IidShellItem2));
            IntPtr desktop, folder = IntPtr.Zero;
            hr = SHGetDesktopFolder(&desktop);
            if (hr >= 0)
            {
                Guid iidFolder = IidShellFolder;
                hr = ((delegate* unmanaged<IntPtr, IntPtr, IntPtr, Guid*, IntPtr*, int>)Slot(desktop, 5))(desktop, parent, IntPtr.Zero, &iidFolder, &folder);
                Release(desktop);
            }
            Set("bindParentFolder", Probe.Hex(hr));
            if (folder != IntPtr.Zero)
            {
                Set("folderParent-IShellItem", WithParent(IntPtr.Zero, folder, child, IidShellItem));
                Set("folderParent-IShellItem2", WithParent(IntPtr.Zero, folder, child, IidShellItem2));
                Set("both-IShellItem2", WithParent(parent, folder, child, IidShellItem2));
                Release(folder);
            }
            Set("negative-IFolderView2", WithParent(parent, IntPtr.Zero, child, IidFolderView2));
            ILFree(child);
            ILFree(parent);
            ILFree(abs);
        }

        static Dictionary<string, object> WithParent(IntPtr parent, IntPtr folder, IntPtr child, Guid iid)
        {
            IntPtr item;
            int hr = SHCreateItemWithParent(parent, folder, child, &iid, &item);
            return ItemName(hr, item);
        }

        static Dictionary<string, object> ItemName(int hr, IntPtr item)
        {
            var r = new Dictionary<string, object> { ["hr"] = Probe.Hex(hr) };
            if (hr < 0 || item == IntPtr.Zero)
                return r;
            char* text;
            int h = ((delegate* unmanaged<IntPtr, uint, char**, int>)Slot(item, 5))(item, SigdnFilesyspath, &text);
            r["name"] = h < 0 ? "GetDisplayName " + Probe.Hex(h) : new string(text);
            if (h >= 0)
                Marshal.FreeCoTaskMem((IntPtr)text);
            Release(item);
            return r;
        }

        /// <summary>Hook-based accept path: hooks find the dialog and catch OK; the selection is read before Cancel.</summary>
        static void RunAccept(string root)
        {
            IntPtr dialog = CreateDialog(root, FosAllowMultiselect | FosAllNonStorageItems);
            if (dialog == IntPtr.Zero)
                return;
            _dialog = dialog;
            _dialogHwnd = IntPtr.Zero;
            _callHook = IntPtr.Zero;
            _collected = false;
            IntPtr winEvent = SetWinEventHook(EventObjectCreate, EventObjectCreate, IntPtr.Zero, &OnWinEvent,
                                              (uint)Environment.ProcessId, 0, 0);
            Set("winEventHook", winEvent != IntPtr.Zero);
            SetTimer(IntPtr.Zero, 0, 2000, &OnAcceptTimer);
            int hr = ((delegate* unmanaged<IntPtr, IntPtr, int>)Slot(dialog, 3))(dialog, IntPtr.Zero);
            Set("show", Probe.Hex(hr));
            if (_callHook != IntPtr.Zero)
                UnhookWindowsHookEx(_callHook);
            if (winEvent != IntPtr.Zero)
                UnhookWinEvent(winEvent);
            if (hr >= 0)
            {
                IntPtr items;
                int r = ((delegate* unmanaged<IntPtr, IntPtr*, int>)Slot(dialog, 27))(dialog, &items);
                Set("resultsAfterOk", ItemArray(r, items));
            }
            _dialog = IntPtr.Zero;
            Release(dialog);
        }

        [UnmanagedCallersOnly]
        static void OnWinEvent(IntPtr hook, uint ev, IntPtr hwnd, int idObject, int idChild, uint thread, uint time)
        {
            try
            {
                if (idObject != 0 || idChild != 0 || _dialogHwnd != IntPtr.Zero || ClassOf(hwnd) != "#32770")
                    return;
                _dialogHwnd = hwnd;
                Set("dialogFoundBy", "winevent");
                InstallCallHook(hwnd);
            }
            catch (Exception ex)
            {
                Set("winEventException", ex.GetType().Name + ": " + ex.Message);
            }
        }

        static void InstallCallHook(IntPtr hwnd)
        {
            uint tid = GetWindowThreadProcessId(hwnd, out _);
            _callHook = SetWindowsHookExW(WhCallWndProc, &OnCallWndProc, IntPtr.Zero, tid);
            Set("callWndProcHook", _callHook != IntPtr.Zero ? "installed" : "failed " + Marshal.GetLastPInvokeError());
        }

        [UnmanagedCallersOnly]
        static void OnAcceptTimer(IntPtr hwnd, uint msg, nuint id, uint time)
        {
            KillTimer(IntPtr.Zero, id);
            try
            {
                if (_dialogHwnd == IntPtr.Zero)
                {
                    _dialogHwnd = FindDialog();
                    Set("dialogFoundBy", _dialogHwnd != IntPtr.Zero ? "fallback (no WinEvent)" : "none");
                    if (_dialogHwnd != IntPtr.Zero)
                        InstallCallHook(_dialogHwnd);
                }
                if (_selectAll)
                    Set("select", SelectAll(_dialog));
                IntPtr ok = GetDlgItem(_dialogHwnd, IdOk);
                Set("okButton", ok != IntPtr.Zero);
                if (ok != IntPtr.Zero)
                    PostMessageW(ok, BmClick, IntPtr.Zero, IntPtr.Zero);
                SetTimer(IntPtr.Zero, 0, 5000, &OnSafetyTimer);
            }
            catch (Exception ex)
            {
                Set("timerException", ex.GetType().Name + ": " + ex.Message);
            }
        }

        /// <summary>If no hook caught OK within 5 s, close the dialog so the next case can run.</summary>
        [UnmanagedCallersOnly]
        static void OnSafetyTimer(IntPtr hwnd, uint msg, nuint id, uint time)
        {
            KillTimer(IntPtr.Zero, id);
            if (_collected || _dialog == IntPtr.Zero)
                return;
            Set("safetyClose", Probe.Hex(((delegate* unmanaged<IntPtr, int, int>)Slot(_dialog, 23))(_dialog, Cancelled)));
        }

        [UnmanagedCallersOnly]
        static IntPtr OnCallWndProc(int code, IntPtr wParam, IntPtr lParam)
        {
            try
            {
                var cwp = (CwpStruct*)lParam;
                if (code >= 0 && !_collected && cwp->Message == WmCommand && ((long)cwp->WParam & 0xffff) == IdOk
                    && cwp->Hwnd == _dialogHwnd)
                {
                    _collected = true;
                    Set("okCommandSeen", true);
                    Set("selection", GetSelection(_dialog));
                    IntPtr cancel = GetDlgItem(cwp->Hwnd, IdCancel);
                    Set("cancelButton", cancel != IntPtr.Zero);
                    if (cancel != IntPtr.Zero)
                        SendMessageW(cancel, BmClick, IntPtr.Zero, IntPtr.Zero);
                    Set("cancelSent", cancel != IntPtr.Zero);
                }
            }
            catch (Exception ex)
            {
                Set("hookException", ex.GetType().Name + ": " + ex.Message);
            }
            return CallNextHookEx(IntPtr.Zero, code, wParam, lParam);
        }

        /// <summary>The folder view through the dialog's service provider; hresults go into <paramref name="r"/>.</summary>
        static IntPtr FolderView2(IntPtr dialog, Dictionary<string, object> r)
        {
            int hr = QueryInterface(dialog, IidServiceProvider, out IntPtr sp);
            r["serviceProvider"] = Probe.Hex(hr);
            if (hr < 0)
                return IntPtr.Zero;
            Guid sid = SidFolderView, iid = IidFolderView2;
            IntPtr view;
            hr = ((delegate* unmanaged<IntPtr, Guid*, Guid*, IntPtr*, int>)Slot(sp, 3))(sp, &sid, &iid, &view);
            Release(sp);
            r["queryServiceFolderView2"] = Probe.Hex(hr);
            return hr < 0 ? IntPtr.Zero : view;
        }

        /// <summary>Q2 and S1: every item in the view selected, then GetSelection(TRUE).</summary>
        static Dictionary<string, object> FolderView(IntPtr dialog)
        {
            var r = SelectAll(dialog);
            IntPtr view = FolderView2(dialog, new Dictionary<string, object>());
            if (view == IntPtr.Zero)
                return r;
            IntPtr array;
            int hr = ((delegate* unmanaged<IntPtr, int, IntPtr*, int>)Slot(view, 32))(view, 1, &array);
            r["S1"] = ItemArray(hr, array);
            Release(view);
            return r;
        }

        static Dictionary<string, object> SelectAll(IntPtr dialog)
        {
            var r = new Dictionary<string, object>();
            IntPtr view = FolderView2(dialog, r);
            if (view == IntPtr.Zero)
                return r;
            int count;
            r["itemCount"] = Probe.Hex(((delegate* unmanaged<IntPtr, uint, int*, int>)Slot(view, 7))(view, SvgioAllview, &count));
            r["items"] = count;
            var selects = new List<string>();
            for (int i = 0; i < count && i < 16; i++)
                selects.Add(Probe.Hex(((delegate* unmanaged<IntPtr, int, uint, int>)Slot(view, 15))(view, i, SvsiSelect)));
            r["selectItem"] = selects;
            Release(view);
            return r;
        }

        static Dictionary<string, object> GetSelection(IntPtr dialog)
        {
            var r = new Dictionary<string, object>();
            IntPtr view = FolderView2(dialog, r);
            if (view == IntPtr.Zero)
                return r;
            IntPtr array;
            int hr = ((delegate* unmanaged<IntPtr, int, IntPtr*, int>)Slot(view, 32))(view, 1, &array);
            r["getSelection"] = ItemArray(hr, array);
            Release(view);
            return r;
        }

        static Dictionary<string, object> ItemArray(int hr, IntPtr array)
        {
            var r = new Dictionary<string, object> { ["hr"] = Probe.Hex(hr) };
            if (hr < 0 || array == IntPtr.Zero)
                return r;
            uint count;
            r["getCount"] = Probe.Hex(((delegate* unmanaged<IntPtr, uint*, int>)Slot(array, 7))(array, &count));
            var names = new List<string>();
            for (uint i = 0; i < count && i < 16; i++)
            {
                IntPtr item;
                int h = ((delegate* unmanaged<IntPtr, uint, IntPtr*, int>)Slot(array, 8))(array, i, &item);
                if (h < 0)
                {
                    names.Add("GetItemAt " + Probe.Hex(h));
                    continue;
                }
                char* text;
                h = ((delegate* unmanaged<IntPtr, uint, char**, int>)Slot(item, 5))(item, SigdnFilesyspath, &text);
                names.Add(h < 0 ? "GetDisplayName " + Probe.Hex(h) : new string(text));
                if (h >= 0)
                    Marshal.FreeCoTaskMem((IntPtr)text);
                Release(item);
            }
            r["names"] = names;
            Release(array);
            return r;
        }

        static int QueryInterface(IntPtr obj, Guid iid, out IntPtr result)
        {
            IntPtr p;
            int hr = ((delegate* unmanaged<IntPtr, Guid*, IntPtr*, int>)Slot(obj, 0))(obj, &iid, &p);
            result = hr >= 0 ? p : IntPtr.Zero;
            if (hr >= 0 && p != IntPtr.Zero && iid != IidServiceProvider)
                Release(p);
            return hr;
        }

        static IntPtr FindDialog()
        {
            uint self = (uint)Environment.ProcessId;
            IntPtr found = IntPtr.Zero;
            EnumWindows((h, _) =>
            {
                GetWindowThreadProcessId(h, out uint pid);
                if (pid == self && IsWindowVisible(h) && ClassOf(h) == "#32770")
                {
                    found = h;
                    return false;
                }
                return true;
            }, IntPtr.Zero);
            return found;
        }

        static string ClassOf(IntPtr h)
        {
            var sb = new StringBuilder(256);
            GetClassNameW(h, sb, sb.Capacity);
            return sb.ToString();
        }

        static IntPtr Slot(IntPtr obj, int index) => (*(IntPtr**)obj)[index];

        static void Release(IntPtr obj) => ((delegate* unmanaged<IntPtr, uint>)Slot(obj, 2))(obj);

        static void Set(string key, object value)
        {
            lock (Gate)
                (_case ?? Probe.Result)[key] = value;
        }

        static void Watchdog(string name)
        {
            if (Done.Wait(60000))
                return;
            Console.WriteLine(name + ": still open after 60 s; wrote " + Write(name, "still open after 60 s"));
            Environment.Exit(3);
        }

        static string Write(string name, string incomplete)
        {
            lock (Gate)
            {
                Probe.Result["complete"] = incomplete == null;
                if (incomplete != null)
                    Probe.Result["incomplete"] = incomplete;
                return Probe.Write(name);
            }
        }

        delegate bool EnumProc(IntPtr hwnd, IntPtr param);

        [DllImport("ole32.dll")]
        static extern int CoCreateInstance(Guid* clsid, IntPtr outer, uint context, Guid* iid, IntPtr* result);

        [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
        static extern int SHCreateItemFromParsingName(string path, IntPtr bindContext, Guid* iid, IntPtr* result);

        [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
        static extern int SHParseDisplayName(string name, IntPtr bindContext, IntPtr* pidl, uint attrsIn, uint* attrsOut);

        [DllImport("shell32.dll")]
        static extern int SHCreateItemWithParent(IntPtr parent, IntPtr folder, IntPtr child, Guid* iid, IntPtr* item);

        [DllImport("shell32.dll")]
        static extern int SHGetDesktopFolder(IntPtr* folder);

        [DllImport("shell32.dll")]
        static extern IntPtr ILFindLastID(IntPtr pidl);

        [DllImport("shell32.dll")]
        static extern IntPtr ILClone(IntPtr pidl);

        [DllImport("shell32.dll")]
        static extern bool ILRemoveLastID(IntPtr pidl);

        [DllImport("shell32.dll")]
        static extern void ILFree(IntPtr pidl);

        [DllImport("user32.dll")]
        static extern nuint SetTimer(IntPtr hwnd, nuint id, uint ms, delegate* unmanaged<IntPtr, uint, nuint, uint, void> callback);

        [DllImport("user32.dll")]
        static extern bool KillTimer(IntPtr hwnd, nuint id);

        [DllImport("user32.dll")]
        static extern IntPtr SetWinEventHook(uint min, uint max, IntPtr module,
            delegate* unmanaged<IntPtr, uint, IntPtr, int, int, uint, uint, void> proc, uint pid, uint tid, uint flags);

        [DllImport("user32.dll")]
        static extern bool UnhookWinEvent(IntPtr hook);

        [DllImport("user32.dll", SetLastError = true)]
        static extern IntPtr SetWindowsHookExW(int id, delegate* unmanaged<int, IntPtr, IntPtr, IntPtr> proc, IntPtr module, uint tid);

        [DllImport("user32.dll")]
        static extern bool UnhookWindowsHookEx(IntPtr hook);

        [DllImport("user32.dll")]
        static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr wParam, IntPtr lParam);

        [DllImport("user32.dll")]
        static extern bool EnumWindows(EnumProc proc, IntPtr param);

        [DllImport("user32.dll")]
        static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);

        [DllImport("user32.dll")]
        static extern bool IsWindowVisible(IntPtr hwnd);

        [DllImport("user32.dll")]
        static extern IntPtr GetDlgItem(IntPtr dialog, int id);

        [DllImport("user32.dll")]
        static extern bool PostMessageW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);

        [DllImport("user32.dll")]
        static extern IntPtr SendMessageW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        static extern int GetClassNameW(IntPtr hwnd, StringBuilder name, int count);
    }
}
