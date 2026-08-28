# Clevo LED Keyboard Reverse-Engineering Summary (Historical, Unsafe)

This document summarizes the discoveries made while reverse-engineering the OEM Clevo Control Center 3.0 LED lighting software (specifically on a PowerSpec 1520 / Clevo PB50/PB51 chassis) to achieve programatic control of the RGB keyboard.

> **Safety status (2026-08-27): historical research only.** The process-kill and
> direct-assembly technique described below is retired and must not be used in
> production, testing, or examples. It removes Clevo's normal platform owner and
> can interfere with Control Center CPU-profile behavior. No supported,
> lighting-only IPC or API has been qualified for this machine.

## Current Safety Invariant: No Takeover

`FnKey.exe` and the other Clevo platform components must remain authoritative.
The widget must not terminate, suspend, replace, import, or compete with the OEM
owner merely to control keyboard lighting. It must also not treat the presence of
an `FnKey` process as proof that a CPU profile was restored.

Keyboard synchronization must remain disabled unless a documented or otherwise
rigorously qualified lighting-only interface is found and hardware testing proves
that it does not disturb profile persistence or CPU performance. This document
does **not** claim that such an interface currently exists.

## 1. The Clevo Software Architecture
The native Clevo software relies on a `.NET` background service and a shared dynamic link library to communicate with the hardware ACPI driver.
- **Core DLL**: `InsydeDCHU.dll` (Interfaces with the low-level WMI/ACPI device driver `\\.\ACPI#CLV0001`).
- **Master Controller**: `FnKey.exe` (A hidden UWP `.NET` background service).
- **Control Interface**: Inside `FnKey.exe`, the static `FnKey.KBModule` class is responsible for managing the state of the keyboard lighting and pushing it to the hardware.

## 2. Historical Background-Process Observation
The native `FnKey.exe` background process runs a continuous timer loop (e.g., `UpdateStatusTimer_Tick`) that polls its internal registry/memory state and aggressively re-applies it to the keyboard.
- **The Problem**: If a custom script attempts to modify the WMI state while `FnKey.exe` is running, the custom color will apply for a split second before being instantly overridden back to the default color by the `FnKey.exe` background loop.
- **Unsafe historical experiment**: Early research terminated `FnKey.exe` and `LedKeyboardSetting.exe` so custom lighting commands would remain visible. This was not a safe solution. It displaced the OEM owner, interfered with profile persistence, and created a credible shared ACPI/platform-control hazard.
- **Current rule**: Do not kill either process. If lighting cannot be changed while the OEM stack remains authoritative, the custom lighting feature must remain off.

## 3. Retired Direct-Assembly Experiment
Because direct `InsydeDCHU.dll` access encountered UWP AppContainer ACL restrictions, the historical experiment loaded the `FnKey.exe` `.NET` assembly into memory and interacted with its `KBModule` class. Subsequent incident evidence showed that this was neither a clean nor a stable integration boundary. It shares vendor platform-control components with Control Center and must not be presented as a supported technique.

The original sequence is retained below solely to explain old code and incident history. **Do not perform it:**
1. **Unsafe owner removal**: The experiment terminated `FnKey.exe` and `LedKeyboardSetting.exe`.
2. **Set Environment**: Set the working directory and `PATH` to `C:\Program Files (x86)\ControlCenter` (where `InsydeDCHU.dll` resides).
3. **Load Assembly**: Load `FnKey.exe` using .NET Reflection (e.g., via `pythonnet` or PowerShell).
4. **Instantiate KBModule**: Create an instance of `FnKey.KBModule` and call its `Init()` method.
5. **Set Color & Brightness**: Call `SetRGBAllColor(System.Drawing.Color)` and `SetBrightnessLevel(int)`.
6. **Broad vendor apply calls**: With `FnKey.exe` absent, the experiment called `UpdateCurrentStatus()`, `SetMode()`, and `SetStatus()` to push state to the physical LEDs. These calls are not qualified as lighting-only and must not be issued by the widget.

The historical scripts `clevo_led_example.py` and `scratch/test_pythonnet.py`
remain in the repository only as disabled evidence. Both now stop before any
process termination, vendor import, or hardware call.

## 4. Hardware Quirk: Red/Green Pin Swap
**Hardware-Specific Quirk**:
On this specific PowerSpec 1520 / Clevo PB50/PB51 keyboard (and potentially a subset of other Clevo models), the **Red and Green data pins are physically swapped** at the matrix controller level.

Because of this physical hardware swap, sending standard RGB color codes will result in incorrect colors displaying on the keyboard. A software compensation layer must be hard-coded into the custom controller to invert Red and Green before sending them to the API:

- **To display RED physically**: You must send **GREEN** `(0, 255, 0)` to the API.
- **To display GREEN physically**: You must send **RED** `(255, 0, 0)` to the API.
- **To display CYAN physically**: You must send **MAGENTA** `(255, 0, 255)` to the API (Green + Blue sent results in Red + Blue physically).
- **To display MAGENTA physically**: You must send **CYAN** `(0, 255, 255)` to the API (Red + Blue sent results in Green + Blue physically).

Blue and White are unaffected, as Blue remains on its own pin, and White illuminates all pins equally.

## 5. v1.14 Lighting-IPC Qualification Result (2026-08-27)

The restored factory package set was examined statically and with passive ETW
captures while all OEM processes remained resident:

- `CLEVOCO.FnhotkeysandOSD` 1.14.0.0;
- `CLEVOCO.LedKeyboardSetting` 1.14.0.0;
- `CLEVOCO.ControlCenter3.0` 1.13.2.0;
- publisher-family suffix `6h6z29zh29qx0`;
- `FnKey.exe` PID 25088 remained at its allowlisted package path;
- `CCDCHUService` and `HKClipSvc` remained running.

Static IL inspection established that `clevokeyboard:` launches the official LED
application but does not expose a lighting API. The LED application's obvious
`WM_COPYDATA` receiver handles keyboard status and brightness notifications. Its
larger-package handler does not apply lighting. Its `SendMsg_WMI` path is a
generic five-byte WMI/DCHU pass-through and therefore fails the lighting-only
safety requirement. The actual color and brightness implementation reaches
`SetDCHU_Data` through vendor code rather than sending a dedicated acknowledged
lighting command to an existing OEM owner.

Four isolated passive traces were then captured:

| Capture | Duration | Observed action |
|---|---:|---|
| `ColorBlue` | 4.223 s | software blue produced physical blue |
| `BrightnessDown` | 4.412 s | one OEM brightness decrement |
| `ProfileQuiet` | 5.311 s | Control Center switched to Quiet |
| `ProfilePerformance` | 4.912 s | Control Center returned to Performance |

The earlier color control also established that software red produces physical
green, confirming the red/green channel swap described above.

No isolated capture contained `WM_COPYDATA`. The LED application sent no window
message to `FnKey` or Control Center during either lighting action, and no WMI
Activity event was attributable to the LED application or named an OEM/DCHU/ACPI
lighting operation. The only `SendMessage` delivered to the LED application was
an unrelated `WM_NULL` from Desktop Window Manager. The Quiet and Performance
captures likewise exposed no lighting message that could be separated and
reused. These dynamic results agree with the static call graph: the OEM UI is a
direct participant in the shared vendor hardware route, not a broker offering a
dedicated lighting-only IPC.

**Qualification decision at that stage:** no real IPC backend was approved.
Guessed command numbers, the generic WMI pass-through, UI automation, and the
retired `KBModule` takeover remained prohibited. The later HID and exact-DCHU
work in section 6 supersedes the earlier statement that no active probe was
attempted. The widget's production backend remains fail-closed pending the
hardware and CPU-profile gates described below.

If a future independent and acknowledged route is discovered, its public color
contract should use desired physical RGB. Only at the final Clevo-specific wire
boundary should it transform `(r, g, b)` to `(g, r, b)` for this exact machine;
ACK/readback comparison must use the same explicitly defined color domain.

## 6. Exact HID and OEM DCHU Results (2026-08-27)

### Independent HID route did not control this keyboard

The lighting collection was identified exactly as USB `048D:8910`, collection
`COL02`, usage page `FF89`, usage `00CC`. Its Windows descriptor reports a
seven-byte feature report. A one-key blue frame and then one complete uniform
white 6-by-20 matrix pass were submitted using the documented ITE 829x command
shape. Windows accepted every `HidD_SetFeature` call, but neither operation
produced a visible change. A feature read returned `CE 00 00 00 00 00 00`, not
a semantic color acknowledgement. `FnKey` and both Clevo services remained
unchanged. This HID route therefore remains blocked.

### Exact official color-click call chain

The exact restored binaries were decompiled read-only with ILSpy 9.1. The OEM
whole-keyboard color handler in `CC.Page_LED_Device.Set_color` executes:

```text
KB.Mode = 8
KB.SetRGBAllColor(color)
KB.SetRGB3ParStatusOn()
KB.SetMode()
```

`CC.KBModule` delegates to `CC.RGBKB`. `SetRGBAllColor` persists the color through
`WriteAppSettings`, while `SetMode` sends brightness and color frames through
`InsydeDCHU.SetDCHU_Data(103, four_bytes, 4)`. For keyboard type 6, each segment
frame is little-endian `[G, R, B, segment]`, with segment selectors `F0`, `F1`,
and `F2`. This byte order is the OEM software's encoding; the machine's observed
physical red/green swap still requires the widget's desired-color compensation.

An observer was injected only into the official LED process, not into `FnKey`.
It logged native arguments and return values without changing them. One
software-blue click produced these calls, all on LED-app thread 27960:

| Order | Native operation | Payload | Meaning |
|---:|---|---|---|
| 1 | `WriteAppSettings(2,81,3)` | `00 00 C8` | persist RGB blue 200 |
| 2 | `WriteAppSettings(2,80,1)` | `01` | persist keyboard-on status |
| 3 | `SetDCHU_Data(103,...,4)` | `8F 00 00 F4` | current brightness frame |
| 4 | `SetDCHU_Data(103,...,4)` | `00 00 C8 F0` | left segment blue |
| 5 | `SetDCHU_Data(103,...,4)` | `00 00 C8 F1` | middle segment blue |
| 6 | `SetDCHU_Data(103,...,4)` | `00 00 C8 F2` | right segment blue |
| 7 | `SetDCHU_Data(103,...,4)` | `01 B0 07 E0` | LED status/on-off frame |

Both AppSettings calls returned `1`; all five DCHU calls returned `103`. The raw
3,409-byte trace is
`%TEMP%\ClevoDchuTrace_20260827_212201.ndjson`, SHA-256
`44781D188FFCA399D349435C93B1935690008C3A29CF268F19101ADD824A4B1E`.
The initial observer emitted warnings after attempting to assign Frida's
read-only thread-ID property, but argument capture had already completed and
each corresponding return event contains the complete call. The helper was
corrected before reuse.

### `FnKey` owner boundary and CPU-profile separation

`FnKey.exe` contains equivalent `FnKey.KBModule` and `FnKey.RGBKB` methods, but
they are ordinary in-process methods, not a cross-process API. Exhaustive call-
site inspection of the signed v1.14 executable found no RGB payload receiver:

- its `WndProc` handles power broadcasts, not lighting commands;
- its `PowerBIOSServer_Out` listener accepts `clevokeyboard^202` only for the
  per-key ripple hook and accepts no color payload;
- `clevokeyboard:` launches the LED application rather than forwarding RGB;
- no named pipe, socket, targeted window message, or other lighting IPC calls
  `SetRGBAllColor` or `SetMode` on the resident `Global.KB` instance.

Therefore the exact transaction **cannot be invoked through the already-running
`FnKey` owner using any interface present in v1.14**. The official architecture
instead permits the separate signed LED process to use the shared DCHU library
while `FnKey` remains resident.

The static CPU-profile path is distinct: `FnKey.Features.SetPowerMode` writes
page 1, offset 1 and sends DCHU command `121`, subcommand `25`. The captured
lighting action wrote page 2 and sent only command `103`. This is meaningful
structural separation, but it does not prove that repeated command-103 traffic
cannot race, overload, or leave retained EC state. A future experimental broker
may allowlist only the exact observed command-103 frames and exact required
AppSettings fields, keep `FnKey` resident, and forbid all generic DCHU access.
It must still pass one-shot restoration, CPU Quiet/Performance A/B, repetition,
crash, sleep/resume, and multi-day canary gates before widget integration or
default-on use.

## 7. Signed Broker and Live Heart-Rate Result (2026-08-27)

Further isolated traces completed the command-103 picture. A software-red click,
which produced physical green on this machine, persisted `FF 00 00` and sent
`00 FF 00 F0/F1/F2`. Brightness down changed AppSettings page 2/offset 35 to 2
and sent `5F 00 00 F4`; brightness up changed it to 3 and sent
`8F 00 00 F4`. Each vendor call returned 103. The retained traces are:

| Action | Trace | SHA-256 |
|---|---|---|
| software red | `ClevoDchuTrace_20260827_213250.ndjson` | `D676A755F30A5EA01A71995B48BDC3F309D150B7453874CAF82F5A41DDEB339B` |
| brightness down | `ClevoDchuTrace_20260827_213329.ndjson` | `58404D220B6A9C8AE4AFB6AC33537D3DF6A6D7997408103A674374256DEFE8E2` |
| brightness up | `ClevoDchuTrace_20260827_213429.ndjson` | `0C353F0D7D0F4AF7C829E16ED68F9FC77092F9BED880272EE4C443F3EC030EAA` |

Loading the OEM DCHU library from an ordinary administrator process failed with
access denied because the restored library resides in the protected OEM AppX
package. The safe solution was not to weaken that package's ACL. Instead, the
prototype was built as a separately signed packaged application so Windows
could grant it an explicit package identity while leaving the Clevo packages
and permissions intact.

The installed prototype is
`HRWidget.ClevoBroker.Experimental_0.2.1.0_x64__tzh64df2et02a`. Its certificate
thumbprint is `8FE55B1A7E3CFEE20754B882B91AD04BAAD280A7`, trusted only in Local Machine
Trusted People for this experiment. The installed MSIX has SHA-256
`478FD6FC1E6AEA482E49167F3A70591346366CA72EEC9F17CE9667BAE6509F38`.

The broker is deliberately narrower than the OEM LED application:

- it accepts only static physical-RGB requests at intervals of at least one second;
- it maps physical `(r,g,b)` to this machine's Clevo `(g,r,b)` boundary;
- it binds only the settings reads needed for safety and command-103 writes;
- it contains no command-121, power-profile, service-control, process-control,
  AppX-registration, generic DCHU, ACPI, WMI, PowerShell, or brightness-write path;
- it verifies the exact machine, package versions, OEM DLL hash, `FnKey` path and
  process identity, and both Clevo services before the first write;
- it uses a lease, strict sequence numbers, one in-flight request, coalescing,
  timeouts, and a parent-owned Windows Job object;
- normal stop restores the OEM software color captured before the lease; any
  uncertain result stops further writes and never restarts or repairs OEM software.

Exact color restoration is cosmetic rather than an acceptance requirement. The
tested restore remains as a best-effort convenience, but OEM control is returned
by ending the lease and broker process while leaving `FnKey` and its services
untouched; the last widget color may safely remain until Clevo changes it.

`SetDCHU_Data(103)` returned 103 synchronously, but
`GetDCHU_Data_Buffer(103)` returned zeros rather than current RGB. Consequently,
the protocol calls this a synchronous vendor acceptance, not hardware readback.
The user's visual observations supplied the hardware confirmation during the
qualification run.

The following LED-only hardware checks passed while `FnKey` stayed resident and
both Clevo services stayed running:

- direct packaged-broker blue and red/green-compensated color changes, each
  followed by restoration of the original physical green;
- ten alternating red/blue enable, apply, and disable cycles;
- rejection of a duplicate session before it could write;
- a live widget run in which physical blue appeared and subsequent keyboard
  colors changed with live heart-rate zone changes.

The widget now uses the signed broker and never the historical `KBModule`
takeover. Ordinary future launches default keyboard sync on after the background
loop and broker safety probe are ready. `--keyboard-sync-off` suppresses
auto-start for one launch, and `--disable-experimental-clevo-ipc` removes broker
availability for that session. Object construction itself remains inert for
tests and embedded callers.

Per user direction, CPU load testing was discontinued and produced no valid new
performance measurement. This does not weaken the architectural rule that the
broker must never touch Clevo CPU-profile controls. It means the current result
is an LED-focused experimental qualification, not a new CPU-performance
certification. Brightness writes also remain excluded; they require a separate
canary and qualification phase.
