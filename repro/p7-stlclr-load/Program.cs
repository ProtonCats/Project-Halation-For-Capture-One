// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32;

namespace Probes
{
    /// <summary>
    /// P7: does this .NET Framework runtime resolve Microsoft.VisualC.STLCLR 2.0.0.0 by its full name, and can a
    /// type from it be constructed. Controls: Microsoft.VisualBasic must load; a name that exists nowhere must not.
    /// </summary>
    public static class P7
    {
        const string Name = "p7-stlclr-load";
        const string Stlclr = "Microsoft.VisualC.STLCLR, Version=2.0.0.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a";
        const string VisualBasic = "Microsoft.VisualBasic, Version=10.0.0.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a";
        const string Nowhere = "Halation.Nowhere, Version=1.0.0.0, Culture=neutral, PublicKeyToken=0123456789abcdef";

        static int Main()
        {
            var r = new Dictionary<string, object>
            {
                ["nonce"] = Environment.GetEnvironmentVariable("HALATION_NONCE") ?? "none",
                ["probe"] = Name,
                ["os"] = Environment.OSVersion.VersionString,
                ["clr"] = Environment.Version.ToString(),
                ["is64"] = Environment.Is64BitProcess,
                ["wine"] = WineVersion(),
                ["runtime"] = RuntimeEnvironment.GetSystemVersion(),
                ["framework"] = RuntimeInformation.FrameworkDescription,
                ["mono"] = Type.GetType("Mono.Runtime") != null,
                ["releaseKey"] = ReleaseKey(),
            };
            var l1 = Load(Stlclr, out Assembly stlclr);
            r["L1"] = l1;
            r["L2"] = stlclr == null ? new Dictionary<string, object> { ["reached"] = false } : Construct(stlclr);
            r["positive"] = Load(VisualBasic, out _);
            r["negative"] = Load(Nowhere, out _);
            Console.WriteLine(Name + ": wrote " + Write(r));
            return 0;
        }

        static Dictionary<string, object> Load(string fullName, out Assembly asm)
        {
            var r = new Dictionary<string, object> { ["name"] = fullName };
            asm = null;
            try
            {
                asm = Assembly.Load(fullName);
                r["loaded"] = true;
                r["fullName"] = asm.FullName;
                r["location"] = asm.Location;
                r["gac"] = asm.GlobalAssemblyCache;
            }
            catch (Exception ex)
            {
                r["loaded"] = false;
                r["exception"] = ex.GetType().FullName;
                r["hresult"] = "0x" + ex.HResult.ToString("x8");
                r["message"] = ex.Message.Length > 300 ? ex.Message.Substring(0, 300) : ex.Message;
            }
            return r;
        }

        /// <summary>L2: GenericPair&lt;int, string&gt;(1, "x") from the loaded assembly, read back through its public field.</summary>
        static Dictionary<string, object> Construct(Assembly asm)
        {
            var r = new Dictionary<string, object> { ["reached"] = true };
            try
            {
                Type open = asm.GetType("Microsoft.VisualC.StlClr.GenericPair`2", true);
                Type closed = open.MakeGenericType(typeof(int), typeof(string));
                object pair = Activator.CreateInstance(closed, 1, "x");
                r["type"] = closed.FullName;
                r["first"] = closed.GetField("first").GetValue(pair);
                r["constructed"] = Equals(r["first"], 1);
            }
            catch (Exception ex)
            {
                r["constructed"] = false;
                r["exception"] = ex.GetType().FullName;
                r["hresult"] = "0x" + ex.HResult.ToString("x8");
                r["message"] = ex.Message.Length > 300 ? ex.Message.Substring(0, 300) : ex.Message;
            }
            return r;
        }

        static object ReleaseKey()
        {
            using (var key = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"))
                return key?.GetValue("Release");
        }

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        delegate IntPtr WineGetVersion();

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        static extern IntPtr GetModuleHandleW(string name);

        [DllImport("kernel32.dll", CharSet = CharSet.Ansi, ExactSpelling = true)]
        static extern IntPtr GetProcAddress(IntPtr module, string name);

        // Wine's ntdll exports wine_get_version; Windows' does not.
        static string WineVersion()
        {
            IntPtr f = GetProcAddress(GetModuleHandleW("ntdll.dll"), "wine_get_version");
            return f == IntPtr.Zero ? null : Marshal.PtrToStringAnsi(Marshal.GetDelegateForFunctionPointer<WineGetVersion>(f)());
        }

        /// <summary>Same path rule as the shared probes: HALATION_RESULT, else %ORACLE_OUT%\name.json, else the working folder.</summary>
        static string Write(Dictionary<string, object> r)
        {
            string path = Environment.GetEnvironmentVariable("HALATION_RESULT");
            if (string.IsNullOrEmpty(path))
            {
                string dir = Environment.GetEnvironmentVariable("ORACLE_OUT");
                path = Path.Combine(string.IsNullOrEmpty(dir) ? "." : dir, Name + ".json");
            }
            var sb = new StringBuilder();
            Json(sb, r);
            string tmp = path + ".tmp";
            File.WriteAllText(tmp, sb.Append('\n').ToString());
            if (File.Exists(path))
                File.Delete(path);
            File.Move(tmp, path);
            return path;
        }

        static void Json(StringBuilder sb, object v)
        {
            switch (v)
            {
                case null: sb.Append("null"); break;
                case bool b: sb.Append(b ? "true" : "false"); break;
                case int _: case long _: case uint _: sb.Append(Convert.ToString(v, CultureInfo.InvariantCulture)); break;
                case string s:
                    sb.Append('"');
                    foreach (char c in s)
                    {
                        if (c == '"' || c == '\\') sb.Append('\\').Append(c);
                        else if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                    }
                    sb.Append('"');
                    break;
                case IDictionary d:
                    sb.Append('{');
                    bool first = true;
                    foreach (DictionaryEntry e in d)
                    {
                        if (!first) sb.Append(',');
                        first = false;
                        Json(sb, e.Key.ToString());
                        sb.Append(':');
                        Json(sb, e.Value);
                    }
                    sb.Append('}');
                    break;
                default: Json(sb, v.ToString()); break;
            }
        }
    }
}
