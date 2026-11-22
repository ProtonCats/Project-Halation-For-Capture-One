// SPDX-FileCopyrightText: 2026 ProtonCats and contributors
// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using Microsoft.Win32;

namespace Probes
{
    /// <summary>
    /// P4 --wcs: the monitor's default colour profile through the WCS functions, no window. The device string is the
    /// monitor's DeviceKey from EnumDisplayDevicesW(EDD_GET_DEVICE_INTERFACE_NAME), with CLASS_MONITOR, type CPT_ICC,
    /// subtype 0 and profile id 1; DeviceID, the friendly name and NULL are tried beside it. Each lookup runs before and
    /// after associating the standard sRGB profile with the monitor in both scopes, the association registry keys are
    /// dumped, and the prior association values are restored.
    /// </summary>
    public static unsafe class P4Wcs
    {
        const uint ClassMonitor = 0x6D6E7472;   // 'mntr'
        const uint EddInterfaceName = 1;
        const uint SystemWide = 0, CurrentUser = 1, CptIcc = 0, CpstNone = 4, ProfileId = 1;
        const string Srgb = "sRGB Color Space Profile.icm";

        unsafe struct DisplayDevice
        {
            public uint Cb;
            public fixed char DeviceName[32];
            public fixed char DeviceString[128];
            public uint StateFlags;
            public fixed char DeviceId[128];
            public fixed char DeviceKey[128];
        }

        public static int Run()
        {
            Probe.Begin("p4-wcs");
            var errors = new List<string>();
            try
            {
                var adapter = Device(null, 0);
                Probe.Result["adapter"] = adapter;
                var monitor = Device(adapter["name"] as string, EddInterfaceName);
                Probe.Result["monitor"] = monitor;
                string key = monitor["key"] as string, id = monitor["id"] as string, friendly = monitor["string"] as string;
                Probe.Result["before"] = Lookups(key, id, friendly);
                var saved = SaveAssociation(key);
                try
                {
                    try
                    {
                        Probe.Result["associate"] = new Dictionary<string, object>
                        {
                            ["systemWide"] = Result(WcsAssociateColorProfileWithDevice(SystemWide, Srgb, key)),
                            ["currentUser"] = Result(WcsAssociateColorProfileWithDevice(CurrentUser, Srgb, key)),
                        };
                    }
                    catch (Exception ex) // Wine's unimplemented stub surfaces as SEHException, not EntryPointNotFoundException
                    {
                        Probe.Result["associate"] = "api missing (" + ex.GetType().Name + "); registry: " + WriteAssociation(saved);
                    }
                    Probe.Result["after"] = Lookups(key, id, friendly);
                    Probe.Result["registry"] = Registry(key);
                }
                finally
                {
                    RestoreAssociation(saved, errors);
                }
                Probe.Result["disassociate"] = "prior registry values restored; see errors";
                Probe.Result["afterDisassociate"] = AppChain(key);
            }
            catch (Exception ex)
            {
                errors.Add(ex.GetType().Name + ": " + ex.Message);
            }
            Probe.Result["errors"] = errors;
            Console.WriteLine("p4-wcs: wrote " + Probe.Write("p4-wcs"));
            return 0;
        }

        static Dictionary<string, object> Device(string parent, uint flags)
        {
            var d = new DisplayDevice { Cb = (uint)sizeof(DisplayDevice) };
            bool ok = EnumDisplayDevicesW(parent, 0, &d, flags);
            var r = new Dictionary<string, object> { ["returned"] = ok, ["lastError"] = Marshal.GetLastPInvokeError() };
            if (ok)
            {
                r["name"] = new string(d.DeviceName);
                r["string"] = new string(d.DeviceString);
                r["stateFlags"] = Probe.Hex((int)d.StateFlags);
                r["id"] = new string(d.DeviceId);
                r["key"] = new string(d.DeviceKey);
            }
            return r;
        }

        static Dictionary<string, object> Lookups(string key, string id, string friendly)
        {
            var r = new Dictionary<string, object> { ["app"] = AppChain(key) };
            var perUser = new Dictionary<string, object>();
            foreach (var (name, dev) in new[] { ("key", key), ("id", id), ("friendly", friendly), ("null", null) })
                perUser[name] = PerUser(dev);
            r["perUser"] = perUser;
            r["perUserNullOut"] = Result(WcsGetUsePerUserProfiles(key, ClassMonitor, null));
            var sizes = new Dictionary<string, object>();
            foreach (uint scope in new[] { SystemWide, CurrentUser })
                foreach (uint subtype in new[] { 0u, CpstNone })
                    sizes[$"scope{scope}-subtype{subtype}"] = Size(scope, key, subtype);
            r["size"] = sizes;
            r["sizeNullOut"] = Result(WcsGetDefaultColorProfileSize(SystemWide, key, CptIcc, 0, ProfileId, null));
            uint need = 0;
            bool known = WcsGetDefaultColorProfileSize(SystemWide, key, CptIcc, 0, ProfileId, &need);
            if (known && need > 2)
                r["fetchShort"] = Fetch(SystemWide, key, 0, need - 2);
            r["fetchNullBuffer"] = Result(WcsGetDefaultColorProfile(SystemWide, key, CptIcc, 0, ProfileId, 512, null));
            return r;
        }

        /// <summary>The order a caller uses: per-user flag, then the size in that scope, then the name.</summary>
        static Dictionary<string, object> AppChain(string key)
        {
            var r = new Dictionary<string, object>();
            var perUser = PerUser(key);
            r["perUser"] = perUser;
            if (!(bool)perUser["returned"])
                return r;
            uint scope = perUser["value"] is int v && v != 0 ? CurrentUser : SystemWide;
            r["scope"] = scope;
            var size = Size(scope, key, 0);
            r["size"] = size;
            if (!(bool)size["returned"])
                return r;
            r["fetch"] = Fetch(scope, key, 0, (uint)size["sizeAfter"]);
            return r;
        }

        static Dictionary<string, object> PerUser(string dev)
        {
            int flag = 0x5A5A5A5A;
            var r = Result(WcsGetUsePerUserProfiles(dev, ClassMonitor, &flag));
            r["value"] = flag;
            return r;
        }

        static Dictionary<string, object> Size(uint scope, string dev, uint subtype)
        {
            uint size = 0xBEEF;
            var r = Result(WcsGetDefaultColorProfileSize(scope, dev, CptIcc, subtype, ProfileId, &size));
            r["sizeAfter"] = size;
            return r;
        }

        static Dictionary<string, object> Fetch(uint scope, string dev, uint subtype, uint bytes)
        {
            var buf = new char[Math.Max(bytes / 2, 1) + 1];
            Dictionary<string, object> r;
            fixed (char* p = buf)
                r = Result(WcsGetDefaultColorProfile(scope, dev, CptIcc, subtype, ProfileId, bytes, p));
            int end = Array.IndexOf(buf, '\0');
            r["bytes"] = bytes;
            r["name"] = new string(buf, 0, end < 0 ? buf.Length : end);
            return r;
        }

        static Dictionary<string, object> Result(bool ok)
        {
            return new Dictionary<string, object> { ["returned"] = ok, ["lastError"] = Marshal.GetLastPInvokeError() };
        }

        sealed class AssociationValue
        {
            public RegistryKey Root;
            public string Path;
            public object Value;
            public RegistryValueKind Kind;
        }

        static List<AssociationValue> SaveAssociation(string deviceKey)
        {
            var saved = new List<AssociationValue>();
            const string machine = "\\Registry\\Machine\\";
            if (deviceKey == null || !deviceKey.StartsWith(machine, StringComparison.OrdinalIgnoreCase))
                return saved;
            string classPath = deviceKey.Substring(machine.Length);
            string sub = classPath.Substring(classPath.IndexOf("Class\\", StringComparison.OrdinalIgnoreCase) + 6);
            string userPath = @"Software\Microsoft\Windows NT\CurrentVersion\ICM\ProfileAssociations\Display\" + sub;
            foreach (var (root, path) in new[] { (Microsoft.Win32.Registry.LocalMachine, classPath), (Microsoft.Win32.Registry.CurrentUser, userPath) })
                using (var key = root.OpenSubKey(path))
                {
                    object value = key == null ? null : key.GetValue("ICMProfile", null, RegistryValueOptions.DoNotExpandEnvironmentNames);
                    saved.Add(new AssociationValue
                    {
                        Root = root, Path = path, Value = value,
                        Kind = value == null ? RegistryValueKind.Unknown : key.GetValueKind("ICMProfile"),
                    });
                }
            return saved;
        }

        static string WriteAssociation(List<AssociationValue> saved)
        {
            foreach (var entry in saved)
                using (var key = entry.Root.CreateSubKey(entry.Path))
                    key.SetValue("ICMProfile", new[] { Srgb }, RegistryValueKind.MultiString);
            return saved.Count == 0 ? "no device key" : "written";
        }

        static void RestoreAssociation(List<AssociationValue> values, List<string> errors)
        {
            foreach (var saved in values)
                try
                {
                    using (var key = saved.Value == null ? saved.Root.OpenSubKey(saved.Path, true) : saved.Root.CreateSubKey(saved.Path))
                    {
                        if (saved.Value == null)
                        {
                            if (key != null) key.DeleteValue("ICMProfile", false);
                        }
                        else
                        {
                            key.SetValue("ICMProfile", saved.Value, saved.Kind);
                        }
                    }
                }
                catch (Exception ex)
                {
                    errors.Add("restore ICMProfile: " + ex.GetType().Name + ": " + ex.Message);
                }
        }

        /// <summary>Values under the monitor's device key and both ICM trees, so Wine can mirror where they live.</summary>
        static Dictionary<string, object> Registry(string deviceKey)
        {
            var r = new Dictionary<string, object>();
            const string machine = "\\Registry\\Machine\\";
            if (deviceKey != null && deviceKey.StartsWith(machine, StringComparison.OrdinalIgnoreCase))
                r["deviceKey"] = Dump(Microsoft.Win32.Registry.LocalMachine, deviceKey.Substring(machine.Length), 2);
            r["userIcm"] = Dump(Microsoft.Win32.Registry.CurrentUser, @"Software\Microsoft\Windows NT\CurrentVersion\ICM", 5);
            r["machineIcm"] = Dump(Microsoft.Win32.Registry.LocalMachine, @"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM", 5);
            return r;
        }

        static object Dump(RegistryKey root, string path, int depth)
        {
            using (var key = root.OpenSubKey(path))
            {
                if (key == null)
                    return "absent";
                var values = new Dictionary<string, object>();
                foreach (string name in key.GetValueNames())
                {
                    object v = key.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames);
                    string text = v is string[] list ? string.Join(" | ", list) : v is byte[] bytes ? bytes.Length + " bytes" : Convert.ToString(v);
                    values[(name.Length == 0 ? "(default)" : name) + " [" + key.GetValueKind(name) + "]"] =
                        text != null && text.Length > 200 ? text.Substring(0, 200) : text;
                }
                var r = new Dictionary<string, object> { ["values"] = values };
                if (depth > 0)
                {
                    var subs = new Dictionary<string, object>();
                    foreach (string sub in key.GetSubKeyNames())
                        subs[sub] = Dump(key, sub, depth - 1);
                    r["subkeys"] = subs;
                }
                return r;
            }
        }

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool EnumDisplayDevicesW(string device, uint index, DisplayDevice* info, uint flags);

        [DllImport("mscms.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool WcsGetUsePerUserProfiles(string device, uint deviceClass, int* usePerUser);

        [DllImport("mscms.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool WcsGetDefaultColorProfileSize(uint scope, string device, uint type, uint subtype, uint id, uint* bytes);

        [DllImport("mscms.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool WcsGetDefaultColorProfile(uint scope, string device, uint type, uint subtype, uint id, uint bytes, char* name);

        [DllImport("mscms.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool WcsAssociateColorProfileWithDevice(uint scope, string profile, string device);

        [DllImport("mscms.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool WcsDisassociateColorProfileFromDevice(uint scope, string profile, string device);
    }
}
