// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace Probes
{
    /// <summary>
    /// Result contract shared by every probe: one JSON file carrying the run's nonce.
    /// Path: HALATION_RESULT, else %ORACLE_OUT%\name.json, else name.json in the working folder.
    /// </summary>
    public static class Probe
    {
        public static readonly Dictionary<string, object> Result = new Dictionary<string, object>();

        public static void Begin(string name)
        {
            Result["nonce"] = Environment.GetEnvironmentVariable("HALATION_NONCE") ?? "none";
            Result["probe"] = name;
            Result["os"] = Environment.OSVersion.VersionString;
            Result["clr"] = Environment.Version.ToString();
            Result["is64"] = Environment.Is64BitProcess;
            Result["wine"] = WineVersion();
        }

        public static string Write(string name)
        {
            string path = Environment.GetEnvironmentVariable("HALATION_RESULT");
            if (string.IsNullOrEmpty(path))
            {
                string dir = Environment.GetEnvironmentVariable("ORACLE_OUT");
                path = Path.Combine(string.IsNullOrEmpty(dir) ? "." : dir, name + ".json");
            }
            string tmp = path + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(Result, new JsonSerializerOptions { WriteIndented = true }) + "\n");
            File.Move(tmp, path, true);
            return path;
        }

        public static int HoldMs()
        {
            return int.TryParse(Environment.GetEnvironmentVariable("HALATION_HOLD_MS"), out int ms) ? ms : 3000;
        }

        public static string Hex(int value)
        {
            return "0x" + value.ToString("x8");
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        public static extern IntPtr GetModuleHandleW(string name);

        [DllImport("kernel32.dll", CharSet = CharSet.Ansi, ExactSpelling = true)]
        public static extern IntPtr GetProcAddress(IntPtr module, string name);

        // Wine's ntdll exports wine_get_version; Windows' does not.
        static unsafe string WineVersion()
        {
            IntPtr f = GetProcAddress(GetModuleHandleW("ntdll.dll"), "wine_get_version");
            return f == IntPtr.Zero ? null : Marshal.PtrToStringAnsi(((delegate* unmanaged<IntPtr>)f)());
        }
    }
}
