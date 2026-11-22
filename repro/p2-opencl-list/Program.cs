// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace Probes
{
    /// <summary>
    /// P2: what OpenCL reports through this Windows layer, and whether a kernel computes correctly.
    /// Lists every platform and device with versions and extensions, then runs c = 2a + b over 2^20
    /// floats on the first GPU (else the first device) and checks every element on the CPU.
    /// No OpenCL.dll or no platform is a recorded result, not a crash.
    /// </summary>
    public static unsafe class P2
    {
        const int N = 1 << 20;
        const string Kernel = "__kernel void axpb(__global const float* a, __global const float* b, __global float* c)" +
                              "{ size_t i = get_global_id(0); c[i] = 2.0f * a[i] + b[i]; }";

        static int Main(string[] args)
        {
            Probe.Begin("p2-opencl-list");
            var errors = new List<string>();
            try
            {
                Probe.Result["platforms"] = Platforms(out IntPtr device, out string deviceName);
                if (device != IntPtr.Zero)
                    Probe.Result["kernel"] = RunKernel(device, deviceName);
            }
            catch (DllNotFoundException)
            {
                Probe.Result["openclLibrary"] = "missing";
            }
            catch (Exception ex)
            {
                errors.Add(ex.GetType().Name + ": " + ex.Message);
            }
            Probe.Result["errors"] = errors;
            Console.WriteLine("p2-opencl-list: wrote " + Probe.Write("p2-opencl-list"));
            return 0;
        }

        static List<object> Platforms(out IntPtr chosen, out string chosenName)
        {
            chosen = IntPtr.Zero;
            chosenName = null;
            bool chosenIsGpu = false;
            var list = new List<object>();
            int err = clGetPlatformIDs(0, null, out uint count);
            Probe.Result["clGetPlatformIDs"] = err;
            if (err != 0 || count == 0)
                return list;
            var platforms = new IntPtr[count];
            fixed (IntPtr* p = platforms)
                Check(clGetPlatformIDs(count, p, out _), "clGetPlatformIDs");
            foreach (IntPtr platform in platforms)
            {
                var devices = new List<object>();
                var entry = new Dictionary<string, object>
                {
                    ["name"] = PlatformString(platform, 0x0902), ["vendor"] = PlatformString(platform, 0x0903),
                    ["version"] = PlatformString(platform, 0x0901), ["profile"] = PlatformString(platform, 0x0900),
                    ["extensions"] = PlatformString(platform, 0x0904), ["devices"] = devices,
                };
                list.Add(entry);
                err = clGetDeviceIDs(platform, ulong.MaxValue, 0, null, out uint n);
                entry["clGetDeviceIDs"] = err;
                if (err != 0 || n == 0)
                    continue;
                var ids = new IntPtr[n];
                fixed (IntPtr* p = ids)
                    Check(clGetDeviceIDs(platform, ulong.MaxValue, n, p, out _), "clGetDeviceIDs");
                foreach (IntPtr d in ids)
                {
                    ulong type = DeviceULong(d, 0x1000);
                    string name = DeviceString(d, 0x102B);
                    devices.Add(new Dictionary<string, object>
                    {
                        ["name"] = name, ["vendor"] = DeviceString(d, 0x102C), ["type"] = TypeName(type),
                        ["version"] = DeviceString(d, 0x102F), ["openclC"] = DeviceString(d, 0x103D),
                        ["driverVersion"] = DeviceString(d, 0x102D), ["computeUnits"] = DeviceUInt(d, 0x1002),
                        ["globalMemMiB"] = DeviceULong(d, 0x101F) >> 20, ["extensions"] = DeviceString(d, 0x1030),
                    });
                    bool gpu = (type & 4) != 0;
                    if (chosen == IntPtr.Zero || (gpu && !chosenIsGpu))
                    {
                        chosen = d;
                        chosenName = name;
                        chosenIsGpu = gpu;
                    }
                }
            }
            return list;
        }

        static Dictionary<string, object> RunKernel(IntPtr device, string deviceName)
        {
            var r = new Dictionary<string, object> { ["device"] = deviceName, ["elements"] = N };
            var a = new float[N];
            var b = new float[N];
            var c = new float[N];
            for (int i = 0; i < N; i++)
            {
                a[i] = i * 0.5f;
                b[i] = 1000f - i;
            }
            int err;
            IntPtr ctx = clCreateContext(null, 1, &device, IntPtr.Zero, IntPtr.Zero, &err);
            Check(err, "clCreateContext");
            IntPtr queue = clCreateCommandQueue(ctx, device, 0, &err);
            Check(err, "clCreateCommandQueue");
            byte[] src = Encoding.ASCII.GetBytes(Kernel);
            nuint len = (nuint)src.Length;
            IntPtr program;
            fixed (byte* s = src)
            {
                byte* sp = s;
                program = clCreateProgramWithSource(ctx, 1, &sp, &len, &err);
            }
            Check(err, "clCreateProgramWithSource");
            err = clBuildProgram(program, 1, &device, null, IntPtr.Zero, IntPtr.Zero);
            if (err != 0)
            {
                r["buildLog"] = BuildLog(program, device);
                Check(err, "clBuildProgram");
            }
            IntPtr kernel = clCreateKernel(program, "axpb", &err);
            Check(err, "clCreateKernel");
            nuint bytes = (nuint)(N * sizeof(float));
            IntPtr bufA, bufB, bufC;
            fixed (float* pa = a, pb = b)
            {
                bufA = clCreateBuffer(ctx, 4 | 0x20, bytes, pa, &err);
                Check(err, "clCreateBuffer a");
                bufB = clCreateBuffer(ctx, 4 | 0x20, bytes, pb, &err);
                Check(err, "clCreateBuffer b");
            }
            bufC = clCreateBuffer(ctx, 2, bytes, null, &err);
            Check(err, "clCreateBuffer c");
            Check(clSetKernelArg(kernel, 0, (nuint)IntPtr.Size, &bufA), "clSetKernelArg 0");
            Check(clSetKernelArg(kernel, 1, (nuint)IntPtr.Size, &bufB), "clSetKernelArg 1");
            Check(clSetKernelArg(kernel, 2, (nuint)IntPtr.Size, &bufC), "clSetKernelArg 2");
            nuint global = N;
            var clock = Stopwatch.StartNew();
            Check(clEnqueueNDRangeKernel(queue, kernel, 1, null, &global, null, 0, null, null), "clEnqueueNDRangeKernel");
            fixed (float* pc = c)
                Check(clEnqueueReadBuffer(queue, bufC, 1, 0, bytes, pc, 0, null, null), "clEnqueueReadBuffer");
            r["ms"] = Math.Round(clock.Elapsed.TotalMilliseconds, 2);
            int wrong = 0;
            for (int i = 0; i < N; i++)
                if (c[i] != 2f * a[i] + b[i])
                    wrong++;
            r["mismatches"] = wrong;
            r["ok"] = wrong == 0;
            clReleaseMemObject(bufA);
            clReleaseMemObject(bufB);
            clReleaseMemObject(bufC);
            clReleaseKernel(kernel);
            clReleaseProgram(program);
            clReleaseCommandQueue(queue);
            clReleaseContext(ctx);
            return r;
        }

        static void Check(int err, string call)
        {
            if (err != 0)
                throw new InvalidOperationException(call + " returned " + err);
        }

        static string TypeName(ulong t)
        {
            var parts = new List<string>();
            if ((t & 2) != 0) parts.Add("cpu");
            if ((t & 4) != 0) parts.Add("gpu");
            if ((t & 8) != 0) parts.Add("accelerator");
            if ((t & 16) != 0) parts.Add("custom");
            return parts.Count == 0 ? "0x" + t.ToString("x") : string.Join("+", parts);
        }

        static string PlatformString(IntPtr platform, uint param)
        {
            if (clGetPlatformInfo(platform, param, 0, null, out nuint size) != 0 || size == 0)
                return null;
            var buf = new byte[(int)size];
            fixed (byte* p = buf)
                clGetPlatformInfo(platform, param, size, p, out _);
            return Encoding.ASCII.GetString(buf).TrimEnd('\0');
        }

        static string DeviceString(IntPtr device, uint param)
        {
            if (clGetDeviceInfo(device, param, 0, null, out nuint size) != 0 || size == 0)
                return null;
            var buf = new byte[(int)size];
            fixed (byte* p = buf)
                clGetDeviceInfo(device, param, size, p, out _);
            return Encoding.ASCII.GetString(buf).TrimEnd('\0');
        }

        static ulong DeviceULong(IntPtr device, uint param)
        {
            ulong v = 0;
            clGetDeviceInfo(device, param, sizeof(ulong), &v, out _);
            return v;
        }

        static uint DeviceUInt(IntPtr device, uint param)
        {
            uint v = 0;
            clGetDeviceInfo(device, param, sizeof(uint), &v, out _);
            return v;
        }

        static string BuildLog(IntPtr program, IntPtr device)
        {
            if (clGetProgramBuildInfo(program, device, 0x1183, 0, null, out nuint size) != 0 || size == 0)
                return null;
            var buf = new byte[(int)size];
            fixed (byte* p = buf)
                clGetProgramBuildInfo(program, device, 0x1183, size, p, out _);
            return Encoding.ASCII.GetString(buf).TrimEnd('\0');
        }

        const string Cl = "OpenCL.dll";

        [DllImport(Cl)] static extern int clGetPlatformIDs(uint entries, IntPtr* platforms, out uint count);
        [DllImport(Cl)] static extern int clGetPlatformInfo(IntPtr platform, uint param, nuint size, void* value, out nuint sizeRet);
        [DllImport(Cl)] static extern int clGetDeviceIDs(IntPtr platform, ulong type, uint entries, IntPtr* devices, out uint count);
        [DllImport(Cl)] static extern int clGetDeviceInfo(IntPtr device, uint param, nuint size, void* value, out nuint sizeRet);
        [DllImport(Cl)] static extern IntPtr clCreateContext(IntPtr* props, uint n, IntPtr* devices, IntPtr notify, IntPtr user, int* err);
        [DllImport(Cl)] static extern IntPtr clCreateCommandQueue(IntPtr ctx, IntPtr device, ulong props, int* err);
        [DllImport(Cl)] static extern IntPtr clCreateProgramWithSource(IntPtr ctx, uint n, byte** sources, nuint* lengths, int* err);
        [DllImport(Cl)] static extern int clBuildProgram(IntPtr program, uint n, IntPtr* devices, byte* options, IntPtr notify, IntPtr user);
        [DllImport(Cl)] static extern int clGetProgramBuildInfo(IntPtr program, IntPtr device, uint param, nuint size, void* value, out nuint sizeRet);
        [DllImport(Cl)] static extern IntPtr clCreateKernel(IntPtr program, string name, int* err);
        [DllImport(Cl)] static extern IntPtr clCreateBuffer(IntPtr ctx, ulong flags, nuint size, void* host, int* err);
        [DllImport(Cl)] static extern int clSetKernelArg(IntPtr kernel, uint index, nuint size, void* value);
        [DllImport(Cl)] static extern int clEnqueueNDRangeKernel(IntPtr queue, IntPtr kernel, uint dims, nuint* offset, nuint* global, nuint* local, uint nWait, IntPtr* wait, IntPtr* evt);
        [DllImport(Cl)] static extern int clEnqueueReadBuffer(IntPtr queue, IntPtr buffer, uint blocking, nuint offset, nuint size, void* ptr, uint nWait, IntPtr* wait, IntPtr* evt);
        [DllImport(Cl)] static extern int clReleaseMemObject(IntPtr mem);
        [DllImport(Cl)] static extern int clReleaseKernel(IntPtr kernel);
        [DllImport(Cl)] static extern int clReleaseProgram(IntPtr program);
        [DllImport(Cl)] static extern int clReleaseCommandQueue(IntPtr queue);
        [DllImport(Cl)] static extern int clReleaseContext(IntPtr ctx);
    }
}
