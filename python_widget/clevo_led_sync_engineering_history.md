# Engineering History: Safe(er) Clevo LED Control for the HR Widget

**Date:** 2026-08-27

**Target:** PowerSpec 1520 / Clevo PB50EF-G

**Final broker:** HR Widget Clevo Broker Experimental 0.2.5.0

**Outcome:** Live heart-rate color and brightness animation verified by the user

## Executive summary

The original keyboard-sync implementation was unsafe because it took ownership
away from Clevo's platform software. It terminated `FnKey.exe`, imported the
vendor `FnKey` assembly, constructed `KBModule`, and repeatedly sent RGB,
brightness, mode, status, and update calls through the same DCHU/ACPI/embedded-
controller stack used by Control Center. The observed failure was not merely a
lighting glitch: Clevo CPU-profile selections stopped behaving as persistent,
distinguishable states.

Reinstalling the factory v1.14 Clevo software repaired the OEM application layer.
A subsequent embedded-power reset cleared a retained low-level conservative
state and restored working Quiet/Performance behavior. The replacement lighting
architecture was therefore built around one invariant: **Clevo `FnKey` and CPU-
profile control must remain available throughout every lighting session.**

The final widget does not stop, replace, inject into, restart, repair, or re-
register Clevo software. Investigation established that the official LED app is
itself a separate DCHU client while `FnKey` remains resident. Lighting uses fixed
DCHU command **103**; CPU-profile selection uses the structurally separate command
**121**, subcommand **25**. The widget now delegates only observed command-103
color and brightness frames to a narrow, locally signed packaged broker.

This is materially safer than process takeover, but it remains an exact-machine
experimental integration rather than a zero-risk or vendor-supported API.

## Original interference and platform recovery

The historical widget:

- enabled keyboard synchronization during construction;
- force-terminated `FnKey.exe`, `LedKeyboardSetting.exe`, and `LaunchFnkey.exe`;
- imported the installed `FnKey.exe` assembly and constructed `FnKey.KBModule`;
- called `SetRGBAllColor`, `SetBrightnessLevel`, `SetMode`, `SetStatus`, and
  `UpdateCurrentStatus` for lighting updates;
- attempted to restore ownership by killing processes again, re-registering
  AppX packages, and relaunching OEM applications;
- treated the reappearance of `FnKey` as sufficient recovery without proving
  that firmware had accepted or reapplied the intended CPU profile.

With the OEM owner displaced, Control Center could display a newly selected
profile, but reopening it reverted that choice and CPU modes were behaviorally
indistinguishable. The factory Control Center v1.14 reinstall restored a
persistent v1.14 `FnKey` owner and repaired the OEM package set. Performance
remained near 1.29 GHz until a full embedded-power reset—shutdown, external-power
and USB removal, power-button hold, wait, and AC-only boot—cleared retained
platform state. The same workload then measured about 2.208 GHz in Quiet and
3.350 GHz in Performance.

This made two design rules non-negotiable: never displace `FnKey`, and never
expose a generic platform-control command from the HR widget.

## Discovery work and rejected approaches

### Window messages and OEM WMI IPC

Static inspection showed that `clevokeyboard:` launches the official LED app; it
is not a developer lighting API. The obvious `WM_COPYDATA` receiver handles
status and brightness notifications rather than RGB writes. Its larger-payload
branch does not perform lighting work, and `SendMsg_WMI` is a generic five-byte
pass-through to `HkeyTray`, not a lighting-only interface that can be safely
allowlisted.

Passive Win32k and WMI traces of isolated color, brightness, Quiet, and
Performance actions found no reusable lighting write sent to `FnKey` or Control
Center. Guessed window messages and generic WMI pass-through were rejected.

### Independent USB HID

The candidate `048D:8910/COL02/FF89:00CC` collection accepted a documented
feature report and a complete 120-address white probe without error, but neither
changed the keyboard. Its feature read returned `CE 00 00 00 00 00 00`, which
was not a color acknowledgment. The HID route was abandoned without changing
any Clevo process or service.

### Direct library access and old takeover

An ordinary Python process could not load the exact protected WindowsApps copy
of `InsydeDCHU.dll`, even from an administrator account. Elevation alone did not
supply the package identity expected by the OEM library. Weakening WindowsApps
ACLs, copying vendor libraries, UI-coordinate automation, command-number
guessing, brute-force probing, registry writes, and a return to `KBModule`
takeover were all rejected.

## Identifying the exact lighting transaction

Read-only decompilation of the exact v1.14 LED package showed that a whole-
keyboard color action selects static all-color mode 8, maintains its page-2 LED
state, enables three RGB segments, and calls `InsydeDCHU.dll` command 103.

A dynamic observer attached only to the official LED app and captured this
software-blue action:

```text
WriteAppSettings page 2 offset 81: 00 00 C8
WriteAppSettings page 2 offset 80: 01
SetDCHU_Data command 103: 8F 00 00 F4
SetDCHU_Data command 103: 00 00 C8 F0
SetDCHU_Data command 103: 00 00 C8 F1
SetDCHU_Data command 103: 00 00 C8 F2
SetDCHU_Data command 103: 01 B0 07 E0
```

The same analysis separated platform profiles from lighting:

| Function | AppSettings location | DCHU route |
|---|---|---|
| Keyboard lighting | Page 2 | Command 103 |
| CPU profile | Page 1, offset 1 | Command 121, subcommand 25 |

`FnKey` exposes no inbound RGB command carrying color bytes. The official LED
app being a separate concurrent DCHU client established that coexistence with a
resident `FnKey`—not IPC into `FnKey` and not replacement of it—was the correct
architecture.

## Packaged broker and qualification boundary

The final package is:

```text
HRWidget.ClevoBroker.Experimental_0.2.5.0_x64__tzh64df2et02a
```

It is locally signed with certificate thumbprint:

```text
8FE55B1A7E3CFEE20754B882B91AD04BAAD280A7
```

The archived release artifacts are:

| Artifact | SHA-256 |
|---|---|
| `HRWidget.ClevoBroker.Experimental_0.2.5.0_x64.msix` | `21DA34F2A2E87DA368797CA7047FA285C776E2081C60DC58DFC80DD24036286B` |
| `HRWidget.ClevoBroker.Experimental.cer` | `DECEAA937C41ACE2832A7C85E6CA47D022F959A433E9C9987F6B91999CE4CD67` |

Packaging supplies the Windows package context needed to load the OEM library
without changing Clevo ACLs, copying vendor DLLs, or elevating the widget. The
broker accepts only `--probe` or `--serve` and a fixed newline-delimited JSON
protocol. Its writable surface contains only `SET_COLOR`, `SET_BRIGHTNESS`, and
`STOP`. It accepts no raw command number or arbitrary byte buffer.

Every request carries a unique lease and increasing sequence number. Messages
use exact schemas and have a 4 KiB limit. The broker hard-codes command 103. It
contains no CPU command 121, generic DCHU pass-through, `WriteAppSettings`,
`SetPowerMode`, PowerShell, process/service control, AppX registration, or OEM
repair function.

The backend starts only when all exact values match:

- `Micro Electronics Inc` / `1520` / `PB50EF-G`;
- `CLEVOCO.FnhotkeysandOSD` 1.14.0.0;
- `CLEVOCO.LedKeyboardSetting` 1.14.0.0;
- `CLEVOCO.ControlCenter3.0` 1.13.2.0;
- publisher suffix `6h6z29zh29qx0` and fixed WindowsApps paths;
- `InsydeDCHU.dll` SHA-256
  `98AF9793B911AB57FCBD09FD75441A120A19A529F4D18D03D83BE42DE7020A47`;
- one allowlisted `FnKey.exe`;
- running `CCDCHUService` and `HKClipSvc`;
- static all-color mode 8 and observed status/brightness ranges.

The broker records the original `FnKey` PID and start time and checks that
identity and both services around writes. OEM state reclamation, service loss,
`FnKey` restart, package mismatch, malformed payload, wrong lease, or wrong
sequence ends the widget's lighting lease rather than attempting OEM repair.

## Color, brightness, and heartbeat behavior

This keyboard's visible red and green channels are reversed relative to the OEM
software representation. The widget/controller speaks desired physical RGB; the
red/green translation is confined to the broker boundary. Blue and compensated
red/green probes were visually confirmed before live integration.

Isolated OEM traces established the brightness map:

| Level | Command-103 frame | Additional frame |
|---:|---|---|
| 0/off | `07 00 00 E0` | none |
| 1 | `2F 00 00 F4` | `01 B0 07 E0` |
| 2 | `5F 00 00 F4` | `01 B0 07 E0` |
| 3 | `8F 00 00 F4` | `01 B0 07 E0` |
| 4 | `BF 00 00 F4` | `01 B0 07 E0` |

The user visually confirmed a complete 0→4→0 sweep. Color remains event-driven
and changes only with the heart-rate zone. Brightness uses the same monotonic
heartbeat phase as the visual pulse, with a small measured-call latency lead.
The widget schedules no more than 10 brightness updates per second and does not
resend an unchanged discrete level. Restored Minimum and Maximum Brightness
sliders span 0–4, default to 1 and 4, preserve an ordered range, and directly
bound the pulse.

An early brightness revision measured its 100 ms interval differently on the
two sides and could quarantine valid traffic. Version 0.2.5 removed the broker's
redundant timing rejection. Harmless scheduler jitter and simultaneous zone
changes can no longer quarantine the session; strict sequencing and one
synchronous native operation at a time remain the safety boundary.

## Serialization, acknowledgment, and lifecycle

Color and brightness share one lease, one sequence counter, one in-flight
command, matching ACK validation, newest-pending coalescing, and bounded probe,
operation, and stop deadlines. Applied state advances only after the matching
ACK. That ACK deliberately means synchronous vendor return 103, not physical LED
readback. An uncertain operation is never retried.

A Windows mutex rejects a second session before it can write. The broker is
attached to a kill-on-close Job object so a parent crash contains only the
widget-owned broker. It never terminates an OEM process.

On orderly disable, the controller drains the possible in-flight command and
sends `STOP`. The broker restores captured color/brightness only while persisted
OEM state indicates that the user has not reclaimed lighting, acknowledges
`STOPPED`, and exits. Returning control means ending the broker lease/process
while `FnKey` and both services remain untouched; exact final color is a best-
effort convenience rather than the ownership boundary.

If an apply or cleanup outcome is uncertain, the HR Widget stops LED writes and
marks its own feature blocked or quarantined. This is an application safety
state, not Windows, antivirus, or Clevo quarantining the system.

## Verification

The final LED-focused suite passed 88 tests, covering allowlist blocking,
protocol bounds, absence of legacy takeover calls, one-in-flight serialization,
coalescing, color/brightness ACK matching, timeouts, late replies, malformed
payloads, sequence mismatches, crashes, OEM-health changes, duplicate sessions,
idempotent disable, lifecycle containment, heartbeat phase/cadence, and slider
behavior.

Hardware work included one-shot blue, compensated red/green changes, ten
enable/apply/disable cycles, competing-session rejection, the 0→4→0 sweep, a
live color-then-brightness regression, and clean return to OEM-only state. The
user confirmed that live colors followed HR zones and brightness pulsed smoothly
while `FnKey`, `CCDCHUService`, and `HKClipSvc` remained present.

No final CPU stress benchmark accompanied the LED deployment because acceptance
was intentionally focused on lighting. The earlier post-recovery Quiet/
Performance A/B remains the known-good CPU baseline; this deployment is not a
renewed CPU-performance certification.

## Residual risks and limitations

1. The broker still shares Clevo's DCHU transport; exact command restriction
   reduces risk but cannot make vendor firmware behavior risk-free.
2. No physical LED readback exists. ACK proves vendor return, not visible color.
3. Only this exact hardware/package/DLL identity is supported.
4. Only static whole-keyboard mode is supported; per-key and effect modes are not.
5. The experimental package uses a locally trusted certificate that should be
   reviewed and removed if the feature is retired.
6. No multi-day canary combined with repeated CPU-profile stress testing was run.
7. Restoration is best effort; manual OEM changes take precedence and uncertain
   native outcomes are not retried.
8. The shutdown guard can remain visibly open rather than force-exit when cleanup
   has not yet been confirmed.

## Final assessment

Whole-keyboard color and brightness control was restored by replacing process
takeover with a fixed-command, identity-bound, serialized broker that coexists
with the official Clevo owner. The central breakthrough was proving that
lighting uses command 103 while CPU profiles use a distinct command-121 path,
then reproducing only observed lighting frames inside the required package
context.

The design is safer because `FnKey` and Clevo services remain resident, no
generic platform-control primitive exists, one operation is acknowledged at a
time, OEM changes take precedence, and failures are contained to the widget-
owned broker. It should continue to be labeled an exact-machine experimental
integration, with the historical kill-`FnKey` implementation retained only as
unsafe research material.
