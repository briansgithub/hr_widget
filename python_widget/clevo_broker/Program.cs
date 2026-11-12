using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Win32;

namespace HrWidget.ClevoBroker;

internal static class Program
{
    private const string ExpectedPackageName = "HRWidget.ClevoBroker.Experimental";
    private const string BackendId = "clevo-dchu-103-static-experimental";
    private const string BackendVersion = "0.2.7";
    private const int ProtocolVersion = 1;
    private const int MaxLineBytes = 4096;
    private const int LightingCommand = 103;
    private const int StaticAllColorMode = 8;

    private const string HardwareIdentity = "Micro Electronics Inc|1520|PB50EF-G";
    private const string FnKeyPath = @"C:\Program Files\WindowsApps\CLEVOCO.FnhotkeysandOSD_1.14.0.0_x64__6h6z29zh29qx0\FnKey\FnKey.exe";
    private const string LedPackagePath = @"C:\Program Files\WindowsApps\CLEVOCO.LedKeyboardSetting_7.68.0.0_x64__6h6z29zh29qx0";
    private const string ControlCenterPackagePath = @"C:\Program Files\WindowsApps\CLEVOCO.ControlCenter3.0_7.68.1.0_x64__6h6z29zh29qx0";
    private const string DchuDirectory = LedPackagePath + @"\CC";
    private const string DchuPath = DchuDirectory + @"\InsydeDCHU.dll";
    private const string DchuSha256 = "DAB807B5D4A621893EF5C5C1941E9D8CE91DDA6B2518E931F4BB4727DFD80E98";

    private const int LoadLibrarySearchDefaultDirs = 0x00001000;
    private const int LoadLibrarySearchUserDirs = 0x00000400;
    private const uint ScManagerConnect = 0x0001;
    private const uint ServiceQueryStatus = 0x0004;
    private const int ScStatusProcessInfo = 0;
    private const uint ServiceRunning = 4;

    private static readonly (string Name, int Page, int Offset, int Length)[] ReadFields =
    [
        ("cpu_profile", 1, 1, 1),
        ("led_mode", 2, 32, 1),
        ("led_status", 2, 80, 1),
        ("led_rgb", 2, 81, 3),
        ("led_brightness", 2, 35, 1),
    ];

    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    private delegate int ReadAppSettingsDelegate(int page, int offset, int length, ref byte value);

    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    private delegate int SetLightingDataDelegate(int command, byte[] buffer, int length);

    [StructLayout(LayoutKind.Sequential)]
    private struct ServiceStatusProcess
    {
        public uint ServiceType;
        public uint CurrentState;
        public uint ControlsAccepted;
        public uint Win32ExitCode;
        public uint ServiceSpecificExitCode;
        public uint CheckPoint;
        public uint WaitHint;
        public uint ProcessId;
        public uint ServiceFlags;
    }

    private sealed record StateField(int Page, int Offset, int[] Bytes, int Result);

    private sealed record LightingState(
        byte CpuProfile,
        byte Mode,
        byte Status,
        byte[] StoredSoftwareRgb,
        byte Brightness,
        IReadOnlyDictionary<string, StateField> Fields);

    private sealed record OemHealth(int FnKeyPid, long FnKeyStartTimeUtcTicks);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern int GetCurrentPackageFullName(ref int packageFullNameLength, StringBuilder? packageFullName);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr AddDllDirectory(string newDirectory);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool RemoveDllDirectory(IntPtr cookie);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetDefaultDllDirectories(int directoryFlags);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr OpenSCManager(string? machineName, string? databaseName, uint desiredAccess);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr OpenService(IntPtr serviceControlManager, string serviceName, uint desiredAccess);

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool QueryServiceStatusEx(
        IntPtr service,
        int infoLevel,
        ref ServiceStatusProcess buffer,
        int bufferSize,
        out int bytesNeeded);

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseServiceHandle(IntPtr handle);

    private sealed class DchuSession : IDisposable
    {
        private readonly IntPtr _directoryCookie;
        private IntPtr _library;

        public ReadAppSettingsDelegate ReadSettings { get; }
        public SetLightingDataDelegate SetLightingData { get; }

        public DchuSession(bool allowLightingWrite)
        {
            if (!SetDefaultDllDirectories(LoadLibrarySearchDefaultDirs | LoadLibrarySearchUserDirs))
            {
                throw new InvalidOperationException($"SetDefaultDllDirectories failed: {Marshal.GetLastWin32Error()}");
            }

            _directoryCookie = AddDllDirectory(DchuDirectory);
            if (_directoryCookie == IntPtr.Zero)
            {
                throw new InvalidOperationException($"AddDllDirectory failed: {Marshal.GetLastWin32Error()}");
            }

            try
            {
                _library = NativeLibrary.Load(DchuPath);
                ReadSettings = Marshal.GetDelegateForFunctionPointer<ReadAppSettingsDelegate>(
                    NativeLibrary.GetExport(_library, "ReadAppSettings"));
                if (allowLightingWrite)
                {
                    SetLightingData = Marshal.GetDelegateForFunctionPointer<SetLightingDataDelegate>(
                        NativeLibrary.GetExport(_library, "SetDCHU_Data"));
                }
                else
                {
                    SetLightingData = static (_, _, _) => throw new InvalidOperationException(
                        "The read-only probe cannot issue lighting commands.");
                }
            }
            catch
            {
                Dispose();
                throw;
            }
        }

        public void Dispose()
        {
            if (_library != IntPtr.Zero)
            {
                NativeLibrary.Free(_library);
                _library = IntPtr.Zero;
            }
            if (_directoryCookie != IntPtr.Zero)
            {
                _ = RemoveDllDirectory(_directoryCookie);
            }
        }
    }

    private static int Main(string[] args)
    {
        Console.Out.NewLine = "\n";
        if (args.Length != 1 || (args[0] != "--probe" && args[0] != "--serve"))
        {
            WriteJson(new { status = "blocked", error = "Only --probe or --serve is supported." });
            return 2;
        }

        try
        {
            VerifyPackageAndHardware();
            return args[0] == "--probe" ? RunProbe() : RunServer();
        }
        catch (Exception ex)
        {
            WriteJson(new { status = "blocked", error_type = ex.GetType().Name, error = ex.Message });
            return 3;
        }
    }

    private static int RunProbe()
    {
        OemHealth health = VerifyOemHealth();
        using DchuSession dchu = new(allowLightingWrite: false);
        LightingState state = ReadState(dchu);
        int[] physicalRgb =
        [
            state.StoredSoftwareRgb[1],
            state.StoredSoftwareRgb[0],
            state.StoredSoftwareRgb[2],
        ];

        WriteJson(new
        {
            status = state.Mode == StaticAllColorMode
                && state.Status is 0 or 1
                && state.Brightness is >= 1 and <= 4 ? "ok" : "blocked",
            mode = "read_only",
            backend_id = BackendId,
            backend_version = BackendVersion,
            package_full_name = GetPackageFullName(),
            hardware_identity = HardwareIdentity,
            fnkey_pid = health.FnKeyPid,
            fnkey_start_time_utc_ticks = health.FnKeyStartTimeUtcTicks,
            dll = DchuPath,
            sha256 = DchuSha256,
            state = state.Fields,
            original_physical_rgb = physicalRgb,
            original_brightness_level = state.Status == 0 ? 0 : state.Brightness,
            limitation = "No hardware color or brightness readback is available; writable ACKs mean synchronous vendor return 103 only.",
            reason = state.Mode != StaticAllColorMode
                ? "Clevo lighting must already be in static all-color mode 8."
                : state.Status is not (0 or 1)
                    ? "Clevo lighting status is outside the observed range."
                    : state.Brightness is not (>= 1 and <= 4)
                        ? "Clevo brightness is outside the observed range 1 through 4."
                        : string.Empty,
        });
        return state.Mode == StaticAllColorMode
            && state.Status is 0 or 1
            && state.Brightness is >= 1 and <= 4 ? 0 : 4;
    }

    private static int RunServer()
    {
        using DchuSession dchu = new(allowLightingWrite: true);
        OemHealth originalHealth = VerifyOemHealth();
        LightingState original = ReadState(dchu);
        if (original.Mode != StaticAllColorMode
            || original.Status is not (0 or 1)
            || original.Brightness is not (>= 1 and <= 4))
        {
            throw new InvalidOperationException(
                "Clevo lighting must be in static all-color mode 8 with observed status and brightness values.");
        }

        WriteJson(new
        {
            type = "HELLO",
            protocol = ProtocolVersion,
            backend_id = BackendId,
            backend_version = BackendVersion,
        });

        using JsonDocument config = ReadProtocolLine();
        JsonElement root = config.RootElement;
        RequireExactProperties(root, "type", "lease_id", "hardware_identity", "min_interval_ms");
        RequireString(root, "type", "CONFIG");
        string leaseId = RequireBoundedString(root, "lease_id", 128);
        RequireString(root, "hardware_identity", HardwareIdentity);
        // The sender reports its intended cadence for protocol observability,
        // but the broker does not reject based on scheduler timing. Safety is
        // provided by strict sequencing and one synchronous command at a time.
        _ = RequireInt(root, "min_interval_ms", 0, int.MaxValue);

        WriteJson(new { type = "READY", lease_id = leaseId });

        int nextSequence = 1;
        bool applied = false;
        while (true)
        {
            using JsonDocument message = ReadProtocolLine();
            JsonElement body = message.RootElement;
            string type = RequireBoundedString(body, "type", 32);
            if (type == "STOP")
            {
                RequireExactProperties(body, "type", "lease_id");
                RequireString(body, "lease_id", leaseId);
                if (applied && PersistedLightingStateMatches(dchu, original))
                {
                    ApplyStoredSoftwareColor(dchu, original.StoredSoftwareRgb);
                    ApplyBrightness(dchu, original.Status == 0 ? (byte)0 : original.Brightness);
                    VerifyOemHealth(originalHealth);
                }
                WriteJson(new { type = "STOPPED", lease_id = leaseId });
                return 0;
            }

            bool setColor = type == "SET_COLOR";
            bool setBrightness = type == "SET_BRIGHTNESS";
            if (!setColor && !setBrightness)
            {
                throw new InvalidOperationException("Unknown protocol message type.");
            }

            RequireExactProperties(
                body,
                setColor
                    ? new[] { "type", "lease_id", "sequence", "rgb" }
                    : new[] { "type", "lease_id", "sequence", "level" });
            RequireString(body, "lease_id", leaseId);
            int sequence = RequireInt(body, "sequence", 1, int.MaxValue);
            if (sequence != nextSequence)
            {
                throw new InvalidOperationException("Unexpected command sequence.");
            }

            byte red = 0;
            byte green = 0;
            byte blue = 0;
            int brightnessLevel = 0;
            if (setColor)
            {
                JsonElement rgb = body.GetProperty("rgb");
                RequireExactProperties(rgb, "r", "g", "b");
                red = checked((byte)RequireInt(rgb, "r", 0, 255));
                green = checked((byte)RequireInt(rgb, "g", 0, 255));
                blue = checked((byte)RequireInt(rgb, "b", 0, 255));
            }
            else
            {
                brightnessLevel = RequireInt(body, "level", 0, 4);
            }

            try
            {
                VerifyOemHealth(originalHealth);
                if (!PersistedLightingStateMatches(dchu, original))
                {
                    SendNack(leaseId, sequence, "pre_apply", true, "OEM_LIGHTING_RECLAIMED");
                    return 5;
                }
            }
            catch
            {
                SendNack(leaseId, sequence, "pre_apply", true, "OEM_HEALTH_CHANGED");
                return 6;
            }

            Stopwatch operation = Stopwatch.StartNew();
            try
            {
                if (setColor)
                {
                    ApplyPhysicalColor(dchu, red, green, blue);
                }
                else
                {
                    ApplyBrightness(dchu, checked((byte)brightnessLevel));
                }
                applied = true;
                VerifyOemHealth(originalHealth);
                if (!PersistedLightingStateMatches(dchu, original))
                {
                    SendNack(leaseId, sequence, "post_apply", true, "OEM_STATE_CHANGED");
                    return 7;
                }
            }
            catch
            {
                SendNack(leaseId, sequence, "unknown", true, "VENDOR_WRITE_UNCERTAIN");
                return 8;
            }
            finally
            {
                operation.Stop();
            }

            if (setColor)
            {
                WriteJson(new
                {
                    type = "ACK",
                    lease_id = leaseId,
                    sequence,
                    confirmed_rgb = new { r = red, g = green, b = blue },
                    confirmation = "synchronous_vendor_return_103",
                    duration_ms = operation.Elapsed.TotalMilliseconds,
                });
            }
            else
            {
                WriteJson(new
                {
                    type = "BRIGHTNESS_ACK",
                    lease_id = leaseId,
                    sequence,
                    confirmed_level = brightnessLevel,
                    confirmation = "synchronous_vendor_return_103",
                    duration_ms = operation.Elapsed.TotalMilliseconds,
                });
            }
            nextSequence++;
        }
    }

    private static void ApplyPhysicalColor(DchuSession dchu, byte red, byte green, byte blue)
    {
        ApplyFrames(dchu, red, green, blue);
    }

    private static void ApplyStoredSoftwareColor(DchuSession dchu, byte[] softwareRgb)
    {
        ApplyFrames(dchu, softwareRgb[1], softwareRgb[0], softwareRgb[2]);
    }

    private static void ApplyBrightness(DchuSession dchu, byte level)
    {
        if (level > 4)
        {
            throw new InvalidOperationException("Only observed brightness levels 0 through 4 are qualified.");
        }
        if (level == 0)
        {
            byte[] offFrame = [0x07, 0, 0, 0xE0];
            int offResult = dchu.SetLightingData(LightingCommand, offFrame, offFrame.Length);
            if (offResult != LightingCommand)
            {
                throw new InvalidOperationException(
                    $"Vendor lighting-off command returned {offResult}.");
            }
            return;
        }
        byte brightnessValue = checked((byte)(level * 48 - 1));
        byte[] brightnessFrame = [brightnessValue, 0, 0, 0xF4];
        int brightnessResult = dchu.SetLightingData(
            LightingCommand,
            brightnessFrame,
            brightnessFrame.Length);
        if (brightnessResult != LightingCommand)
        {
            throw new InvalidOperationException(
                $"Vendor brightness command returned {brightnessResult}.");
        }

        byte[] statusFrame = [0x01, 0xB0, 0x07, 0xE0];
        int statusResult = dchu.SetLightingData(
            LightingCommand,
            statusFrame,
            statusFrame.Length);
        if (statusResult != LightingCommand)
        {
            throw new InvalidOperationException(
                $"Vendor lighting-status command returned {statusResult}.");
        }
    }

    private static void ApplyFrames(DchuSession dchu, byte first, byte second, byte third)
    {
        foreach (byte segment in new byte[] { 0xF0, 0xF1, 0xF2 })
        {
            byte[] frame = [first, second, third, segment];
            int result = dchu.SetLightingData(LightingCommand, frame, frame.Length);
            if (result != LightingCommand)
            {
                throw new InvalidOperationException($"Vendor lighting command returned {result}.");
            }
        }
    }

    private static bool PersistedLightingStateMatches(DchuSession dchu, LightingState original)
    {
        LightingState current = ReadState(dchu);
        return current.Mode == original.Mode
            && current.Status == original.Status
            && current.Brightness == original.Brightness
            && current.StoredSoftwareRgb.SequenceEqual(original.StoredSoftwareRgb);
    }

    private static LightingState ReadState(DchuSession dchu)
    {
        Dictionary<string, StateField> fields = new(StringComparer.Ordinal);
        foreach ((string name, int page, int offset, int length) in ReadFields)
        {
            byte[] bytes = new byte[length];
            int result = dchu.ReadSettings(page, offset, length, ref bytes[0]);
            fields[name] = new StateField(page, offset, bytes.Select(static value => (int)value).ToArray(), result);
        }

        return new LightingState(
            CpuProfile: checked((byte)fields["cpu_profile"].Bytes[0]),
            Mode: checked((byte)fields["led_mode"].Bytes[0]),
            Status: checked((byte)fields["led_status"].Bytes[0]),
            StoredSoftwareRgb: fields["led_rgb"].Bytes.Select(value => checked((byte)value)).ToArray(),
            Brightness: checked((byte)fields["led_brightness"].Bytes[0]),
            Fields: fields);
    }

    private static void VerifyPackageAndHardware()
    {
        string packageFullName = GetPackageFullName();
        if (!packageFullName.StartsWith(ExpectedPackageName + "_", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("Unexpected broker package identity.");
        }

        using RegistryKey bios = Registry.LocalMachine.OpenSubKey(@"HARDWARE\DESCRIPTION\System\BIOS")
            ?? throw new InvalidOperationException("System BIOS identity is unavailable.");
        string identity = string.Join(
            "|",
            bios.GetValue("SystemManufacturer") as string,
            bios.GetValue("SystemProductName") as string,
            bios.GetValue("BaseBoardProduct") as string);
        if (!string.Equals(identity, HardwareIdentity, StringComparison.Ordinal))
        {
            throw new InvalidOperationException($"Hardware identity is not allowlisted: {identity}");
        }

        foreach (string path in new[] { FnKeyPath, LedPackagePath, ControlCenterPackagePath, DchuPath })
        {
            if (!File.Exists(path) && !Directory.Exists(path))
            {
                throw new FileNotFoundException("An exact allowlisted Clevo component is missing.", path);
            }
        }

        string actualHash = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(DchuPath)));
        if (!string.Equals(actualHash, DchuSha256, StringComparison.Ordinal))
        {
            throw new InvalidOperationException($"Clevo DCHU hash mismatch: {actualHash}");
        }
    }

    private static OemHealth VerifyOemHealth(OemHealth? expected = null)
    {
        Process[] candidates = Process.GetProcessesByName("FnKey");
        try
        {
            Process[] matches = candidates.Where(process =>
            {
                try
                {
                    return string.Equals(process.MainModule?.FileName, FnKeyPath, StringComparison.OrdinalIgnoreCase);
                }
                catch
                {
                    return false;
                }
            }).ToArray();
            if (matches.Length != 1)
            {
                throw new InvalidOperationException("Exactly one allowlisted FnKey process must be running.");
            }

            Process fnKey = matches[0];
            OemHealth current = new(fnKey.Id, fnKey.StartTime.ToUniversalTime().Ticks);
            if (expected is not null && current != expected)
            {
                throw new InvalidOperationException("FnKey identity changed during the lighting lease.");
            }

            foreach (string serviceName in new[] { "CCDCHUService", "HKClipSvc" })
            {
                if (!IsServiceRunning(serviceName))
                {
                    throw new InvalidOperationException($"Required Clevo service is not running: {serviceName}");
                }
            }
            return current;
        }
        finally
        {
            foreach (Process process in candidates)
            {
                process.Dispose();
            }
        }
    }

    private static bool IsServiceRunning(string serviceName)
    {
        IntPtr manager = OpenSCManager(null, null, ScManagerConnect);
        if (manager == IntPtr.Zero)
        {
            throw new InvalidOperationException($"OpenSCManager failed: {Marshal.GetLastWin32Error()}");
        }
        try
        {
            IntPtr service = OpenService(manager, serviceName, ServiceQueryStatus);
            if (service == IntPtr.Zero)
            {
                return false;
            }
            try
            {
                ServiceStatusProcess status = default;
                bool ok = QueryServiceStatusEx(
                    service,
                    ScStatusProcessInfo,
                    ref status,
                    Marshal.SizeOf<ServiceStatusProcess>(),
                    out _);
                return ok && status.CurrentState == ServiceRunning;
            }
            finally
            {
                _ = CloseServiceHandle(service);
            }
        }
        finally
        {
            _ = CloseServiceHandle(manager);
        }
    }

    private static JsonDocument ReadProtocolLine()
    {
        string? line = Console.ReadLine();
        if (line is null)
        {
            throw new EndOfStreamException("Broker input closed.");
        }
        if (Encoding.UTF8.GetByteCount(line) + 1 > MaxLineBytes || line.Contains('\r') || line.Contains('\n'))
        {
            throw new InvalidOperationException("Protocol line is malformed or oversized.");
        }

        JsonDocument document = JsonDocument.Parse(line);
        if (document.RootElement.ValueKind != JsonValueKind.Object)
        {
            document.Dispose();
            throw new InvalidOperationException("Protocol root must be an object.");
        }
        HashSet<string> names = new(StringComparer.Ordinal);
        foreach (JsonProperty property in document.RootElement.EnumerateObject())
        {
            if (!names.Add(property.Name))
            {
                document.Dispose();
                throw new InvalidOperationException("Duplicate protocol field.");
            }
        }
        return document;
    }

    private static void RequireExactProperties(JsonElement value, params string[] names)
    {
        HashSet<string> expected = new(names, StringComparer.Ordinal);
        HashSet<string> actual = value.EnumerateObject().Select(property => property.Name).ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(expected))
        {
            throw new InvalidOperationException("Protocol fields do not match the fixed schema.");
        }
    }

    private static string RequireBoundedString(JsonElement value, string name, int maximum)
    {
        if (!value.TryGetProperty(name, out JsonElement property) || property.ValueKind != JsonValueKind.String)
        {
            throw new InvalidOperationException($"Protocol field {name} must be a string.");
        }
        string result = property.GetString() ?? string.Empty;
        if (result.Length == 0 || result.Length > maximum)
        {
            throw new InvalidOperationException($"Protocol field {name} is outside its length bound.");
        }
        return result;
    }

    private static void RequireString(JsonElement value, string name, string expected)
    {
        if (!string.Equals(RequireBoundedString(value, name, 256), expected, StringComparison.Ordinal))
        {
            throw new InvalidOperationException($"Protocol field {name} has an unexpected value.");
        }
    }

    private static int RequireInt(JsonElement value, string name, int minimum, int maximum)
    {
        if (!value.TryGetProperty(name, out JsonElement property)
            || property.ValueKind != JsonValueKind.Number
            || !property.TryGetInt32(out int result)
            || result < minimum
            || result > maximum)
        {
            throw new InvalidOperationException($"Protocol field {name} is outside its integer bound.");
        }
        return result;
    }

    private static void SendNack(string leaseId, int sequence, string phase, bool fatal, string code)
    {
        WriteJson(new { type = "NACK", lease_id = leaseId, sequence, phase, fatal, code });
    }

    private static string GetPackageFullName()
    {
        int length = 0;
        int result = GetCurrentPackageFullName(ref length, null);
        const int ErrorInsufficientBuffer = 122;
        if (result != ErrorInsufficientBuffer || length <= 1)
        {
            throw new InvalidOperationException($"The broker has no usable package identity ({result}).");
        }
        StringBuilder value = new(length);
        result = GetCurrentPackageFullName(ref length, value);
        if (result != 0)
        {
            throw new InvalidOperationException($"Could not read package identity ({result}).");
        }
        return value.ToString();
    }

    private static void WriteJson(object value)
    {
        Console.WriteLine(JsonSerializer.Serialize(value));
        Console.Out.Flush();
    }
}
