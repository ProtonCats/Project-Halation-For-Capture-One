// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace Probes
{
    /// <summary>
    /// P0 WPF control: a borderless window of solid #00ff00 over the primary screen, so the screen's
    /// mean colour proves it drew. The result is written once the content has rendered.
    /// </summary>
    public static class P0Wpf
    {
        [STAThread]
        static int Main(string[] args)
        {
            Probe.Begin("p0-wpf");
            bool write = Array.IndexOf(args, "--no-write") < 0;
            var app = new Application();
            var window = new Window
            {
                Title = "p0-wpf",
                WindowStyle = WindowStyle.None,
                ResizeMode = ResizeMode.NoResize,
                Left = 0,
                Top = 0,
                Width = SystemParameters.PrimaryScreenWidth,
                Height = SystemParameters.PrimaryScreenHeight,
                Background = Brushes.Lime,
                Content = new Border { Background = Brushes.Lime },
            };
            window.ContentRendered += async (s, e) =>
            {
                Probe.Result["windowColour"] = "#00ff00";
                if (write)
                    Console.WriteLine("p0-wpf: wrote " + Probe.Write("p0-wpf"));
                else
                    Console.WriteLine("p0-wpf: negative control, no result written");
                await Task.Delay(Probe.HoldMs());
                app.Shutdown(0);
            };
            return app.Run(window);
        }
    }
}
