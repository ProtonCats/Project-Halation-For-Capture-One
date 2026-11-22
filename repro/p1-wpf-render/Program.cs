// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace Probes
{
    /// <summary>
    /// P1: which rendering path WPF takes and how long a frame lasts.
    /// Modes: pan (pan and zoom over a synthetic 40 MP image, frame intervals), d3dimage (a D3D9Ex
    /// surface shown through D3DImage, blue), hwndhost (a GDI child window, magenta), hwndhost-d3d
    /// (a D3D9Ex swap chain in a child window, cyan). Render tier is recorded but cannot tell
    /// hardware from software, so loaded modules, the D3D9 adapter and frame times are the rulers.
    /// </summary>
    public static class P1
    {
        const int ImageW = 7728, ImageH = 5152;
        const int WarmupFrames = 30, MaxFrames = 300;
        const double MaxSeconds = 45;
        static readonly string[] Modes = { "pan", "d3dimage", "hwndhost", "hwndhost-d3d" };
        static readonly HashSet<string> Watched = new HashSet<string>
        {
            "d3d9.dll", "d3d11.dll", "dxgi.dll", "wined3d.dll", "opengl32.dll", "vulkan-1.dll",
            "winevulkan.dll", "wpfgfx_cor3.dll", "d3dcompiler_47_cor3.dll", "dxvk_config.dll",
        };
        static readonly List<string> Errors = new List<string>();
        static string mode;
        static Window window;
        static MatrixTransform transform;
        static SolidChild child;
        static IntPtr keepDevice, keepSurface;

        [STAThread]
        static int Main(string[] args)
        {
            mode = args.FirstOrDefault(a => !a.StartsWith("--")) ?? "pan";
            if (Array.IndexOf(Modes, mode) < 0)
            {
                Console.Error.WriteLine("usage: P1WpfRender [" + string.Join("|", Modes) + "]");
                return 2;
            }
            Probe.Begin("p1-wpf-render");
            Probe.Result["mode"] = mode;
            var app = new Application();
            window = new Window
            {
                Title = "p1-wpf-render",
                WindowStyle = WindowStyle.None,
                ResizeMode = ResizeMode.NoResize,
                Left = 0,
                Top = 0,
                Width = SystemParameters.PrimaryScreenWidth,
                Height = SystemParameters.PrimaryScreenHeight,
                Background = Brushes.Black,
                Content = new Border { Background = Brushes.Black },
            };
            if (mode == "pan")
                window.Content = PanContent();
            else if (mode == "d3dimage")
                window.SourceInitialized += (s, e) => Guard("d3dimage setup", () => D3DImageContent(new WindowInteropHelper(window).Handle));
            else
                window.Content = child = new SolidChild(mode == "hwndhost-d3d");
            window.ContentRendered += async (s, e) =>
            {
                if (mode == "pan")
                {
                    StartPan();
                    return;
                }
                if (mode == "hwndhost-d3d")
                    Guard("child swap chain", () => child.StartD3D());
                await Task.Delay(500);
                if (child != null)
                    Probe.Result["childPixel"] = child.Pixel();
                await Finish();
            };
            return app.Run(window);
        }

        static void Guard(string what, Action action)
        {
            try { action(); }
            catch (Exception ex) { Errors.Add(what + ": " + ex.GetType().Name + ": " + ex.Message); }
        }

        static async Task Finish()
        {
            Guard("screen centre", () => Probe.Result["screenCentre"] = ScreenPixel(
                (int)(window.ActualWidth / 2), (int)(window.ActualHeight / 2)));
            Guard("rulers", Rulers);
            Probe.Result["errors"] = Errors;
            Probe.Result["ok"] = Errors.Count == 0;
            Console.WriteLine("p1-wpf-render: wrote " + Probe.Write("p1-" + mode));
            await Task.Delay(Probe.HoldMs());
            Application.Current.Shutdown(0);
        }

        // What the desktop shows at a point, read through GDI: the same ruler on Windows and Wine,
        // and the only one the Windows control returns (it takes no screenshot).
        static string ScreenPixel(int x, int y)
        {
            IntPtr dc = GetDC(IntPtr.Zero);
            uint c = GetPixel(dc, x, y);
            ReleaseDC(IntPtr.Zero, dc);
            return c == 0xffffffff ? "invalid" : "#" + (c & 0xff).ToString("x2") + ((c >> 8) & 0xff).ToString("x2") + ((c >> 16) & 0xff).ToString("x2");
        }

        [DllImport("user32.dll")]
        static extern IntPtr GetDC(IntPtr hwnd);

        [DllImport("user32.dll")]
        static extern int ReleaseDC(IntPtr hwnd, IntPtr dc);

        [DllImport("gdi32.dll")]
        static extern uint GetPixel(IntPtr dc, int x, int y);

        // ---- pan: a synthetic 40 MP image, moved by frame count so every variant draws the same sequence

        static unsafe FrameworkElement PanContent()
        {
            var bmp = new WriteableBitmap(ImageW, ImageH, 96, 96, PixelFormats.Bgr32, null);
            bmp.Lock();
            IntPtr buffer = bmp.BackBuffer;
            int stride = bmp.BackBufferStride;
            Parallel.For(0, ImageH, y =>
            {
                uint* row = (uint*)((byte*)buffer + (long)y * stride);
                uint g = (uint)(y * 255 / ImageH);
                for (int x = 0; x < ImageW; x++)
                {
                    uint r = (uint)(x * 255 / ImageW);
                    uint b = (((x >> 6) ^ (y >> 6)) & 1) != 0 ? 255u : 0u;
                    row[x] = (r << 16) | (g << 8) | b;
                }
            });
            bmp.AddDirtyRect(new Int32Rect(0, 0, ImageW, ImageH));
            bmp.Unlock();
            bmp.Freeze();
            var image = new Image { Source = bmp, Stretch = Stretch.None };
            RenderOptions.SetBitmapScalingMode(image, BitmapScalingMode.Linear);
            transform = new MatrixTransform();
            image.RenderTransform = transform;
            var canvas = new Canvas { ClipToBounds = true, Background = Brushes.Black };
            canvas.Children.Add(image);
            Probe.Result["image"] = new Dictionary<string, object> { ["width"] = ImageW, ["height"] = ImageH, ["format"] = "Bgr32" };
            return canvas;
        }

        static void StartPan()
        {
            var clock = Stopwatch.StartNew();
            var wall = new List<double>();
            var scheduled = new List<double>();
            TimeSpan lastTime = TimeSpan.MinValue;
            double lastWall = 0, start = 0;
            int n = 0;
            EventHandler tick = null;
            tick = (s, e) =>
            {
                TimeSpan t = ((RenderingEventArgs)e).RenderingTime;
                if (t == lastTime)
                    return; // the same frame raised twice
                double now = clock.Elapsed.TotalMilliseconds;
                if (n > WarmupFrames)
                {
                    wall.Add(now - lastWall);
                    scheduled.Add((t - lastTime).TotalMilliseconds);
                }
                else if (n == WarmupFrames)
                    start = now;
                lastTime = t;
                lastWall = now;
                n++;
                double w = window.ActualWidth, h = window.ActualHeight, phase = n / 60.0;
                double fit = Math.Min(w / ImageW, h / ImageH);
                double zoom = fit + (1 - fit) * (0.5 - 0.5 * Math.Cos(phase * 0.8));
                double x = -Math.Max(0, ImageW * zoom - w) * (0.5 - 0.5 * Math.Cos(phase * 1.3));
                double y = -Math.Max(0, ImageH * zoom - h) * (0.5 - 0.5 * Math.Sin(phase * 1.1));
                transform.Matrix = new Matrix(zoom, 0, 0, zoom, x, y);
                if (wall.Count >= MaxFrames || (n > WarmupFrames && now - start > MaxSeconds * 1000))
                {
                    CompositionTarget.Rendering -= tick;
                    Probe.Result["frames"] = Stats(wall, now - start);
                    Probe.Result["scheduledIntervals"] = Stats(scheduled, now - start);
                    _ = Finish();
                }
            };
            CompositionTarget.Rendering += tick;
        }

        static Dictionary<string, object> Stats(List<double> ms, double seconds)
        {
            var sorted = ms.OrderBy(v => v).ToList();
            double At(double q) => sorted.Count == 0 ? double.NaN : sorted[Math.Max(0, (int)Math.Ceiling(q * sorted.Count) - 1)];
            return new Dictionary<string, object>
            {
                ["count"] = sorted.Count,
                ["warmupFrames"] = WarmupFrames,
                ["medianMs"] = Math.Round(At(0.5), 2),
                ["p95Ms"] = Math.Round(At(0.95), 2),
                ["maxMs"] = Math.Round(sorted.Count == 0 ? double.NaN : sorted[^1], 2),
                ["over33Ms"] = sorted.Count(v => v > 33.0),
                ["measuredSeconds"] = Math.Round(seconds / 1000, 2),
            };
        }

        // ---- rulers recorded in every mode

        static void Rulers()
        {
            Probe.Result["renderTierUntrusted"] = RenderCapability.Tier >> 16;
            Probe.Result["processRenderMode"] = RenderOptions.ProcessRenderMode.ToString();
            var source = PresentationSource.FromVisual(window) as HwndSource;
            Probe.Result["windowRenderMode"] = source?.CompositionTarget.RenderMode.ToString();
            var watched = new Dictionary<string, object>();
            var names = new List<string>();
            foreach (ProcessModule m in Process.GetCurrentProcess().Modules)
            {
                string name = m.ModuleName.ToLowerInvariant();
                names.Add(name);
                if (!Watched.Contains(name))
                    continue;
                FileVersionInfo v = m.FileVersionInfo;
                watched[name] = new Dictionary<string, object>
                {
                    ["path"] = m.FileName, ["fileVersion"] = v.FileVersion, ["product"] = v.ProductName, ["company"] = v.CompanyName,
                };
            }
            names.Sort();
            Probe.Result["watchedModules"] = watched;
            Probe.Result["modules"] = names;
            Probe.Result["adapter"] = D3D9.Adapter();
        }

        // ---- d3dimage: a D3D9Ex render target filled blue, handed to D3DImage

        static unsafe void D3DImageContent(IntPtr hwnd)
        {
            var r = new Dictionary<string, object>();
            Probe.Result["d3dimage"] = r;
            IntPtr device = D3D9.CreateDevice(hwnd, 1, 1, r);
            if (device == IntPtr.Zero)
                return;
            keepDevice = device;
            IntPtr surface, shared = IntPtr.Zero;
            int hr = D3D9.CreateRenderTarget(device, 512, 512, &surface, &shared);
            r["hrCreateRenderTarget"] = Probe.Hex(hr);
            if (hr < 0)
                return;
            keepSurface = surface;
            r["hrColorFill"] = Probe.Hex(D3D9.ColorFill(device, surface, 0xff0000ff));
            var image = new D3DImage();
            image.Lock();
            image.SetBackBuffer(D3DResourceType.IDirect3DSurface9, surface, true);
            image.AddDirtyRect(new Int32Rect(0, 0, 512, 512));
            image.Unlock();
            r["frontBufferAvailable"] = image.IsFrontBufferAvailable;
            window.Content = new Image { Source = image, Stretch = Stretch.Fill };
        }
    }

    /// <summary>A Win32 child window inside WPF: GDI-filled magenta, or cleared cyan by a D3D9Ex swap chain.</summary>
    public sealed class SolidChild : HwndHost
    {
        const uint WS_CHILD = 0x40000000, WS_VISIBLE = 0x10000000, WS_CLIPSIBLINGS = 0x04000000;
        readonly bool d3d;
        IntPtr hwnd, device;
        DispatcherTimer presenter;

        public SolidChild(bool d3d)
        {
            this.d3d = d3d;
        }

        protected override HandleRef BuildWindowCore(HandleRef parent)
        {
            string cls = "ProbeSolidChild" + (d3d ? "D3D" : "Gdi");
            var wc = new WNDCLASSEXW
            {
                cbSize = (uint)Marshal.SizeOf<WNDCLASSEXW>(),
                lpfnWndProc = Probe.GetProcAddress(Probe.GetModuleHandleW("user32.dll"), "DefWindowProcW"),
                hInstance = Probe.GetModuleHandleW(null),
                hbrBackground = d3d ? IntPtr.Zero : CreateSolidBrush(0x00ff00ff),
                lpszClassName = cls,
            };
            if (RegisterClassExW(ref wc) == 0)
                throw new InvalidOperationException("RegisterClassExW failed, error " + Marshal.GetLastWin32Error());
            hwnd = CreateWindowExW(0, cls, "", WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS, 0, 0, 16, 16,
                parent.Handle, IntPtr.Zero, wc.hInstance, IntPtr.Zero);
            if (hwnd == IntPtr.Zero)
                throw new InvalidOperationException("CreateWindowExW failed, error " + Marshal.GetLastWin32Error());
            return new HandleRef(this, hwnd);
        }

        protected override void DestroyWindowCore(HandleRef h)
        {
            presenter?.Stop();
            DestroyWindow(h.Handle);
        }

        public void StartD3D()
        {
            var r = new Dictionary<string, object>();
            Probe.Result["childD3D"] = r;
            GetClientRect(hwnd, out RECT rc);
            r["clientSize"] = (rc.Right - rc.Left) + "x" + (rc.Bottom - rc.Top);
            device = D3D9.CreateDevice(hwnd, (uint)Math.Max(1, rc.Right - rc.Left), (uint)Math.Max(1, rc.Bottom - rc.Top), r);
            if (device == IntPtr.Zero)
                return;
            r["hrClear"] = Probe.Hex(D3D9.Clear(device, 0xff00ffff));
            r["hrPresent"] = Probe.Hex(D3D9.Present(device));
            presenter = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(100) };
            presenter.Tick += (s, e) =>
            {
                D3D9.Clear(device, 0xff00ffff);
                D3D9.Present(device);
            };
            presenter.Start();
        }

        public string Pixel()
        {
            IntPtr dc = GetDC(hwnd);
            uint c = GetPixel(dc, 4, 4);
            ReleaseDC(hwnd, dc);
            return c == 0xffffffff ? "invalid" : "#" + (c & 0xff).ToString("x2") + ((c >> 8) & 0xff).ToString("x2") + ((c >> 16) & 0xff).ToString("x2");
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        struct WNDCLASSEXW
        {
            public uint cbSize, style;
            public IntPtr lpfnWndProc;
            public int cbClsExtra, cbWndExtra;
            public IntPtr hInstance, hIcon, hCursor, hbrBackground;
            public string lpszMenuName, lpszClassName;
            public IntPtr hIconSm;
        }

        [StructLayout(LayoutKind.Sequential)]
        struct RECT
        {
            public int Left, Top, Right, Bottom;
        }

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern ushort RegisterClassExW(ref WNDCLASSEXW wc);

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr CreateWindowExW(uint exStyle, string cls, string name, uint style, int x, int y, int w, int h,
            IntPtr parent, IntPtr menu, IntPtr instance, IntPtr param);

        [DllImport("user32.dll")]
        static extern bool DestroyWindow(IntPtr hwnd);

        [DllImport("user32.dll")]
        static extern bool GetClientRect(IntPtr hwnd, out RECT rect);

        [DllImport("user32.dll")]
        static extern IntPtr GetDC(IntPtr hwnd);

        [DllImport("user32.dll")]
        static extern int ReleaseDC(IntPtr hwnd, IntPtr dc);

        [DllImport("gdi32.dll")]
        static extern uint GetPixel(IntPtr dc, int x, int y);

        [DllImport("gdi32.dll")]
        static extern IntPtr CreateSolidBrush(uint colorref);
    }

    /// <summary>
    /// Direct3D 9Ex through raw COM vtables, slot numbers from the method order in the public d3d9.h:
    /// IDirect3D9: 2 Release, 4 GetAdapterCount, 5 GetAdapterIdentifier; IDirect3D9Ex: 20 CreateDeviceEx.
    /// IDirect3DDevice9: 17 Present, 28 CreateRenderTarget, 35 ColorFill, 43 Clear.
    /// </summary>
    public static unsafe class D3D9
    {
        const uint SdkVersion = 32, DevTypeHal = 1, FormatA8R8G8B8 = 21, SwapDiscard = 1, IntervalImmediate = 0x80000000;
        const uint FpuPreserve = 0x2, Multithreaded = 0x4, SoftwareVertexProcessing = 0x20, HardwareVertexProcessing = 0x40;

        [StructLayout(LayoutKind.Sequential)]
        struct PresentParameters
        {
            public uint BackBufferWidth, BackBufferHeight, BackBufferFormat, BackBufferCount, MultiSampleType, MultiSampleQuality, SwapEffect;
            public IntPtr DeviceWindow;
            public int Windowed, EnableAutoDepthStencil;
            public uint AutoDepthStencilFormat, Flags, FullScreenRefreshRateInHz, PresentationInterval;
        }

        [DllImport("d3d9.dll")]
        static extern int Direct3DCreate9Ex(uint sdkVersion, out IntPtr d3d);

        static IntPtr Slot(IntPtr com, int index) => (*(IntPtr**)com)[index];

        static void Release(IntPtr com) => ((delegate* unmanaged<IntPtr, uint>)Slot(com, 2))(com);

        static string Ansi(byte* p, int max)
        {
            int n = 0;
            while (n < max && p[n] != 0)
                n++;
            return Marshal.PtrToStringAnsi((IntPtr)p, n);
        }

        public static Dictionary<string, object> Adapter()
        {
            var r = new Dictionary<string, object>();
            int hr = Direct3DCreate9Ex(SdkVersion, out IntPtr d3d);
            r["hrCreate9Ex"] = Probe.Hex(hr);
            if (hr < 0 || d3d == IntPtr.Zero)
                return r;
            try
            {
                r["adapterCount"] = ((delegate* unmanaged<IntPtr, uint>)Slot(d3d, 4))(d3d);
                byte* id = stackalloc byte[1104]; // D3DADAPTER_IDENTIFIER9
                hr = ((delegate* unmanaged<IntPtr, uint, uint, byte*, int>)Slot(d3d, 5))(d3d, 0, 0, id);
                r["hrIdentifier"] = Probe.Hex(hr);
                if (hr >= 0)
                {
                    long v = *(long*)(id + 1056);
                    r["driver"] = Ansi(id, 512);
                    r["description"] = Ansi(id + 512, 512);
                    r["deviceName"] = Ansi(id + 1024, 32);
                    r["driverVersion"] = $"{(v >> 48) & 0xffff}.{(v >> 32) & 0xffff}.{(v >> 16) & 0xffff}.{v & 0xffff}";
                    r["vendorId"] = "0x" + (*(uint*)(id + 1064)).ToString("x4");
                    r["deviceId"] = "0x" + (*(uint*)(id + 1068)).ToString("x4");
                }
            }
            finally
            {
                Release(d3d);
            }
            return r;
        }

        public static IntPtr CreateDevice(IntPtr hwnd, uint width, uint height, Dictionary<string, object> r)
        {
            int hr = Direct3DCreate9Ex(SdkVersion, out IntPtr d3d);
            r["hrCreate9Ex"] = Probe.Hex(hr);
            if (hr < 0 || d3d == IntPtr.Zero)
                return IntPtr.Zero;
            var pp = new PresentParameters
            {
                BackBufferWidth = width, BackBufferHeight = height, BackBufferCount = 1, SwapEffect = SwapDiscard,
                DeviceWindow = hwnd, Windowed = 1, PresentationInterval = IntervalImmediate,
            };
            var create = (delegate* unmanaged<IntPtr, uint, uint, IntPtr, uint, PresentParameters*, void*, IntPtr*, int>)Slot(d3d, 20);
            IntPtr device = IntPtr.Zero;
            hr = create(d3d, 0, DevTypeHal, hwnd, HardwareVertexProcessing | Multithreaded | FpuPreserve, &pp, null, &device);
            r["hrCreateDeviceEx"] = Probe.Hex(hr);
            if (hr < 0)
            {
                hr = create(d3d, 0, DevTypeHal, hwnd, SoftwareVertexProcessing | Multithreaded | FpuPreserve, &pp, null, &device);
                r["hrCreateDeviceExSoftwareVertex"] = Probe.Hex(hr);
            }
            Release(d3d);
            return hr < 0 ? IntPtr.Zero : device;
        }

        public static int CreateRenderTarget(IntPtr device, uint w, uint h, IntPtr* surface, IntPtr* shared) =>
            ((delegate* unmanaged<IntPtr, uint, uint, uint, uint, uint, int, IntPtr*, IntPtr*, int>)Slot(device, 28))(
                device, w, h, FormatA8R8G8B8, 0, 0, 0, surface, shared);

        public static int ColorFill(IntPtr device, IntPtr surface, uint argb) =>
            ((delegate* unmanaged<IntPtr, IntPtr, void*, uint, int>)Slot(device, 35))(device, surface, null, argb);

        public static int Clear(IntPtr device, uint argb) =>
            ((delegate* unmanaged<IntPtr, uint, void*, uint, uint, float, uint, int>)Slot(device, 43))(device, 0, null, 1, argb, 1f, 0);

        public static int Present(IntPtr device) =>
            ((delegate* unmanaged<IntPtr, void*, void*, IntPtr, void*, int>)Slot(device, 17))(device, null, null, IntPtr.Zero, null);
    }
}
