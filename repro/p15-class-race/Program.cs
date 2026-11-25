// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Diagnostics;
using System.Globalization;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading;

namespace Probes
{
    /// <summary>
    /// P15: window classes come and go on --makers threads while --lookers threads ask for one that does not exist, which
    /// makes the system walk the whole list. Every thread counts its rounds. One that makes no round for --stall ms is stuck:
    /// the result names it and the program ends itself. Without one, the program passes after --seconds.
    /// </summary>
    public static unsafe class P15ClassRace
    {
        const string Name = "p15-class-race";

        [StructLayout(LayoutKind.Sequential)]
        struct WindowClass
        {
            public uint Size, Style;
            public IntPtr Procedure;
            public int ClassExtra, WindowExtra;
            public IntPtr Instance, Icon, Cursor, Background;
            public char* Menu;
            public char* Name;
            public IntPtr SmallIcon;
        }

        [DllImport("user32.dll", SetLastError = true, ExactSpelling = true)]
        static extern ushort RegisterClassExW(WindowClass* windowClass);

        [DllImport("user32.dll", SetLastError = true, ExactSpelling = true)]
        static extern int UnregisterClassW(char* name, IntPtr instance);

        [DllImport("user32.dll", SetLastError = true, ExactSpelling = true)]
        static extern int GetClassInfoExW(IntPtr instance, char* name, WindowClass* windowClass);

        static IntPtr instance, procedure;
        static long[] rounds;
        static int failures, stuck = -1;
        static volatile bool done;

        static int Main(string[] args)
        {
            int makers = 2, lookers = 2, seconds = 20, stall = 3000, code = 1;
            try
            {
                for (int i = 0; i < args.Length; i++)
                {
                    if (i + 1 == args.Length) throw new ArgumentException("missing option value");
                    string option = args[i];
                    int value = int.Parse(args[++i], CultureInfo.InvariantCulture);
                    if (option == "--makers") makers = value;
                    else if (option == "--lookers") lookers = value;
                    else if (option == "--seconds") seconds = value;
                    else if (option == "--stall") stall = value;
                    else throw new ArgumentException($"unknown option {option}");
                }
                if (makers is < 0 or > 32 || lookers is < 0 or > 32 || seconds is < 1 or > 600 || stall is < 500 or > 60000)
                    throw new ArgumentException("value out of range");
                Probe.Begin(Name);
                Probe.Result["makers"] = makers;
                Probe.Result["lookers"] = lookers;
                Run(makers, lookers, seconds, stall);
                code = (bool)Probe.Result["pass"] ? 0 : 1;
            }
            catch (Exception error)
            {
                Probe.Result["error"] = error.ToString();
            }
            Probe.Write(Name);
            if (stuck >= 0) Process.GetCurrentProcess().Kill(); // a stuck thread would keep the process
            return code;
        }

        static void Run(int makers, int lookers, int seconds, int stall)
        {
            instance = Probe.GetModuleHandleW(null);
            procedure = Probe.GetProcAddress(Probe.GetModuleHandleW("user32.dll"), "DefWindowProcW");
            rounds = new long[makers + lookers];
            for (int i = 0; i < rounds.Length; i++)
            {
                int index = i;
                new Thread(() => { if (index < makers) Make(index); else Look(index); }) { IsBackground = true }.Start();
            }
            long[] last = new long[rounds.Length], seen = new long[rounds.Length];
            var clock = Stopwatch.StartNew();
            while (stuck < 0 && clock.ElapsedMilliseconds < seconds * 1000L)
            {
                Thread.Sleep(50);
                for (int i = 0; i < rounds.Length; i++)
                {
                    long now = Interlocked.Read(ref rounds[i]);
                    if (now != last[i]) { last[i] = now; seen[i] = clock.ElapsedMilliseconds; }
                    else if (clock.ElapsedMilliseconds - seen[i] > stall) stuck = i;
                }
            }
            done = true;
            Probe.Result["ms"] = clock.ElapsedMilliseconds;
            Probe.Result["rounds"] = rounds.Select((_, i) => Interlocked.Read(ref rounds[i])).ToArray();
            Probe.Result["stuck"] = stuck < 0 ? null : (stuck < makers ? "maker " : "looker ") + stuck;
            Probe.Result["failures"] = failures;
            Probe.Result["pass"] = stuck < 0 && failures == 0;
        }

        static void Make(int index)
        {
            fixed (char* name = "P15 class " + index.ToString(CultureInfo.InvariantCulture))
                while (!done)
                {
                    var made = new WindowClass { Size = (uint)sizeof(WindowClass), Procedure = procedure, Instance = instance, Name = name };
                    if (RegisterClassExW(&made) == 0 || UnregisterClassW(name, instance) == 0) Interlocked.Increment(ref failures);
                    Interlocked.Increment(ref rounds[index]);
                }
        }

        static void Look(int index)
        {
            fixed (char* name = "P15 no such class")
                while (!done)
                {
                    var found = new WindowClass { Size = (uint)sizeof(WindowClass) };
                    if (GetClassInfoExW(instance, name, &found) != 0) Interlocked.Increment(ref failures);
                    Interlocked.Increment(ref rounds[index]);
                }
        }
    }
}
