// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;

namespace Probes
{
    /// <summary>P0 console control. --no-write is the negative control: the harness must report no result.</summary>
    public static class P0Console
    {
        static int Main(string[] args)
        {
            Probe.Begin("p0-console");
            if (Array.IndexOf(args, "--no-write") >= 0)
            {
                Console.WriteLine("p0-console: negative control, no result written");
                return 0;
            }
            Console.WriteLine("p0-console: wrote " + Probe.Write("p0-console"));
            return 0;
        }
    }
}
