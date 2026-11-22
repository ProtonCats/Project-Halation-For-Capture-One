// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;

namespace Probes
{
    /// <summary>
    /// P4: colour profile lookups through mscms and gdi32, no window.
    /// C1 asks GetStandardColorSpaceProfileW for the needed size with no buffer; Windows documents that the
    /// size is written back. C2 to C4 vary the buffer, C5 asks the screen DC for its profile the same way.
    /// Controls: GetColorDirectoryW must succeed; an unknown colour space id must fail.
    /// C6 to C8 pin the edges a fix must keep: unknown id with no buffer, a buffer with no room for the
    /// terminating NUL, and the Windows colour space id.
    /// </summary>
    public static unsafe class P4
    {
        const uint LcsSrgb = 0x73524742;   // 'sRGB'
        const uint LcsWindows = 0x57696E20; // 'Win '
        const uint Unknown = 0x12345678;   // not a colour space id
        const int MaxPath = 260;

        static int Main(string[] args)
        {
            if (Array.IndexOf(args, "--wcs") >= 0)
                return P4Wcs.Run();
            Probe.Begin("p4-colour");
            var cases = new Dictionary<string, object>();
            var errors = new List<string>();
            try
            {
                cases["C1a"] = Standard(LcsSrgb, null, 0);
                cases["C1b"] = Standard(LcsSrgb, null, 12345);
                cases["C2"] = Standard(LcsSrgb, 1, 2);
                cases["C3"] = Standard(LcsSrgb, MaxPath, MaxPath * 2);
                var query = Standard(LcsSrgb, null, 0);
                uint need = (uint)query["sizeAfter"];
                cases["C4"] = new Dictionary<string, object>
                {
                    ["query"] = query,
                    ["fetch"] = Standard(LcsSrgb, (int)Math.Max(need / 2, 1), need),
                };
                cases["C5"] = ScreenProfile();
                cases["positive"] = ColourDirectory();
                cases["negative"] = Standard(Unknown, MaxPath, MaxPath * 2);
                cases["C6"] = Standard(Unknown, null, 0);
                var full = (Dictionary<string, object>)cases["C3"];
                int chars = full.TryGetValue("path", out object p) ? ((string)p).Length : MaxPath;
                cases["C7"] = Standard(LcsSrgb, chars, (uint)chars * 2);
                cases["C8"] = Standard(LcsWindows, null, 0);
            }
            catch (Exception ex)
            {
                errors.Add(ex.GetType().Name + ": " + ex.Message);
            }
            Probe.Result["cases"] = cases;
            Probe.Result["errors"] = errors;
            Console.WriteLine("p4-colour: wrote " + Probe.Write("p4-colour"));
            return 0;
        }

        /// <summary>One GetStandardColorSpaceProfileW call. bufChars null passes a NULL buffer; size is in bytes.</summary>
        static Dictionary<string, object> Standard(uint id, int? bufChars, uint size)
        {
            char[] buf = bufChars.HasValue ? new char[bufChars.Value] : null;
            uint before = size;
            bool ok;
            int err;
            fixed (char* p = buf)
            {
                ok = GetStandardColorSpaceProfileW(null, id, p, ref size);
                err = Marshal.GetLastPInvokeError();
            }
            var r = Row(ok, err, before, size);
            r["id"] = Probe.Hex((int)id);
            r["bufferChars"] = bufChars;
            if (ok && buf != null)
                AddPath(r, Text(buf), false);
            return r;
        }

        /// <summary>GetICMProfileW on the screen DC: a size query with no buffer, then a fetch with that size (in characters).</summary>
        static Dictionary<string, object> ScreenProfile()
        {
            IntPtr dc = GetDC(IntPtr.Zero);
            try
            {
                uint size = 0;
                bool ok = GetICMProfileW(dc, ref size, null);
                var query = Row(ok, Marshal.GetLastPInvokeError(), 0, size);
                var r = new Dictionary<string, object> { ["screenDc"] = dc != IntPtr.Zero, ["query"] = query };
                if (size > 0 && size < 32768)
                {
                    var buf = new char[size];
                    uint before = size;
                    fixed (char* p = buf)
                        ok = GetICMProfileW(dc, ref size, p);
                    var fetch = Row(ok, Marshal.GetLastPInvokeError(), before, size);
                    if (ok)
                        AddPath(fetch, Text(buf), false);
                    r["fetch"] = fetch;
                }
                return r;
            }
            finally
            {
                if (dc != IntPtr.Zero)
                    ReleaseDC(IntPtr.Zero, dc);
            }
        }

        static Dictionary<string, object> ColourDirectory()
        {
            var buf = new char[MaxPath];
            uint size = MaxPath * 2;
            bool ok;
            fixed (char* p = buf)
                ok = GetColorDirectoryW(null, p, ref size);
            var r = Row(ok, Marshal.GetLastPInvokeError(), MaxPath * 2, size);
            if (ok)
                AddPath(r, Text(buf), true);
            return r;
        }

        static Dictionary<string, object> Row(bool ok, int err, uint before, uint after)
        {
            return new Dictionary<string, object>
            {
                ["returned"] = ok, ["lastError"] = err, ["sizeBefore"] = before, ["sizeAfter"] = after,
            };
        }

        static void AddPath(Dictionary<string, object> r, string path, bool directory)
        {
            r["path"] = path;
            r["exists"] = path.Length > 0 && (directory ? Directory.Exists(path) : File.Exists(path));
        }

        static string Text(char[] buf)
        {
            int end = Array.IndexOf(buf, '\0');
            return new string(buf, 0, end < 0 ? buf.Length : end);
        }

        [DllImport("mscms.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool GetStandardColorSpaceProfileW(string machine, uint profileId, char* profile, ref uint size);

        [DllImport("mscms.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool GetColorDirectoryW(string machine, char* buffer, ref uint size);

        [DllImport("gdi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool GetICMProfileW(IntPtr dc, ref uint size, char* filename);

        [DllImport("user32.dll")]
        static extern IntPtr GetDC(IntPtr window);

        [DllImport("user32.dll")]
        static extern int ReleaseDC(IntPtr window, IntPtr dc);
    }
}
