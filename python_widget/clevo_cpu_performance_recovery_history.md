# Clevo CPU performance interference and recovery history

- **Incident date:** 2026-08-27
- **System:** PowerSpec 1520 / Clevo PB50EF-G
- **Code basis inspected:** branch `keyboard-rgb-diagnostics`, `HEAD` `a66d466`, plus staged and unstaged working-tree changes
- **Outcome:** CPU performance-profile control restored; the widget was left closed and keyboard synchronization was not exercised after recovery
- **Repository status:** this report was added to the working tree but was not committed as part of the investigation
**Time zone:** America/New_York (EDT)

## Executive summary

The heart-rate widget's Clevo keyboard synchronization is not isolated from the laptop's power-management software. It deliberately terminates Clevo's normal `FnKey` process, loads Clevo's own `FnKey.exe` assembly, creates a `KBModule`, and repeatedly applies RGB, brightness, mode, status, and update commands through the vendor stack associated with Clevo's ACPI/embedded-controller control. This created a credible ownership, sequencing, and retained-controller-state hazard.

The investigation established two separate failures:

1. **Clevo application ownership and registration were broken.** With `FnKey` absent, Control Center accepted apparent profile selections but did not preserve them. Reinstalling the factory Control Center stack and repairing its AppX packages restored `FnKey` and made profile choices persist.
2. **A conservative low-level power state remained after the software repair.** Even with the repaired `FnKey` running and Performance mode persisting, a repeatable full-CPU workload produced an estimated effective frequency near **1.29 GHz**. The reinstall sequence included three ordinary reboots, and the conservative state was still present after the third; manually activating `FnKey` also did not clear it. A full shutdown, removal of external power and USB devices, a 30-second power-button hold, a 30-second wait, and an AC-only boot raised the same 12-process saturation workload class to a directly reported **3.31-3.35 GHz** in Performance mode. Switching to Quiet then produced **2.208 GHz**, and returning to Performance restored **3.350 GHz**. This A/B result proved that Clevo profile control was functioning again.

The most strongly supported explanation is that the power-button sequence reset or discharged the embedded-controller and auxiliary power-management domains that survive a warm restart, clearing a retained conservative controller state. The evidence does **not** identify a specific EC register, prove a particular PL1/PL2 value, or prove that one individual keyboard API call directly changed a CPU limit. It does show that the boundary that restored performance was below ordinary Windows software state.

The machine was left in the following known-good condition:

- Clevo Control Center v1.14 factory stack installed.
- All six intended Clevo AppX applications registered and healthy.
- `FnKey` v1.14 running and able to preserve Clevo profiles.
- Stored Clevo/ACPI Bridge mode byte `2` and Control Center UI both showing Performance.
- Final post-reset all-core load averaging **3349.60 MHz** with no reported performance-limit or thermal-throttle flags.
- Heart-rate widget closed and Clevo keyboard synchronization not exercised after recovery.

## Scope and investigation standard

The investigation began with the user's observation that the widget appeared to prevent Clevo Control Center from delivering its normal Performance-mode CPU frequency, typically at least 2.5 GHz under a suitable load. The goals were to identify likely interference points, use the factory-software reinstall as a diagnostic opportunity, restore OEM behavior, and preserve enough evidence to distinguish verified facts from plausible firmware-level mechanisms.

The work deliberately used progressively stronger interventions:

1. Read-only code, process, service, driver, power-policy, event-log, and package inspection.
2. Controlled widget-on/widget-off and profile-selection observations.
3. A user-created System Restore point before software removal.
4. Uninstall and reinstall of the exact factory Control Center package.
5. Narrow removal of only the six stale Clevo AppX package families after explicit authorization.
6. OEM AppX registration repair under the Windows `SYSTEM` account.
7. Repeatable all-core performance tests before and after the embedded-power reset.

No current Intel XTU package, unofficial Control Center package, BIOS update, EC firmware update, or generic Clevo package was installed. No unrelated drivers were removed. The temporary privileged scheduled tasks used during repair were deleted, and a final live query confirmed that no `Codex_Clevo_*` task remained.

## Machine and software baseline

| Item | Observed value |
|---|---|
| Retail system | Micro Electronics / PowerSpec 1520 |
| Clevo baseboard/model | PB50EF-G |
| CPU | Intel Core i7-8750H, 6 cores / 12 logical processors |
| Nominal CPU frequency | 2208 MHz |
| BIOS | Insyde 1.07.04RIN6, dated 2019-01-31 |
| Operating system | Windows 11 Pro, build family 10.0.26200 |
| Windows power plan during tests | Balanced |
| AC/battery state | AC connected; battery full during relevant tests |
| Clevo ACPI Bridge | 1.0.0.6 (`oem24`) |
| Clevo ACPI Bridge1 | 1.0.0.9 (`oem25`) |
| Intel Management Engine Interface | 1828.12.0.1152 (`oem27`) |
| Factory Control Center installer | ControlCenter 3.0 Driver v1.14 / display version 1.14.0 |

The Windows Balanced plan was not imposing the observed cap. Its relevant settings were:

- maximum processor state: 100%;
- minimum processor state: 5%;
- processor performance boost mode: `2`;
- maximum processor frequency: `0`, meaning no explicit MHz ceiling;
- AC energy-performance preference: `0x21` (33).

This matters because a Windows policy cap near 1.3 GHz would have been a simpler explanation. None was found, and the same unchanged Windows plan later supported 3.35 GHz after the power reset.

## What the widget interacts with

### Clevo's shared control path

The project research and live inspection identify `FnKey` as an OEM controller for hotkeys, keyboard behavior, and Control Center integration. Clevo's software uses `InsydeDCHU.dll` and the Clevo ACPI device to reach platform firmware and the embedded controller. The widget reuses that vendor stack instead of using an independent lighting-only protocol.

In the current working tree inspected on the incident date, `hr_widget.py` does the following:

- loads the installed Clevo `FnKey.exe` assembly and constructs `FnKey.KBModule`;
- initializes that object;
- force-terminates `FnKey.exe`, `LedKeyboardSetting.exe`, and `LaunchFnkey.exe`, including process trees;
- for each LED update, calls `SetRGBAllColor`, `SetBrightnessLevel`, `SetMode`, `SetStatus`, and `UpdateCurrentStatus`;
- enables Clevo synchronization by default and invokes the enable handler during widget construction;
- on restoration, kills the OEM processes again, re-registers the AppX manifest, launches the app, and treats the appearance of an `FnKey` process as success.

The relevant current-worktree areas at the time of inspection were `hr_widget.py:28-83`, `hr_widget.py:390-495`, `hr_widget.py:720-781`, `hr_widget.py:1024-1027`, `hr_widget.py:1228-1236`, and `hr_widget.py:2217-2245`. These references describe the 2026-08-27 working tree and may move as the file changes.

This architecture produces four important hazards:

1. **Ownership:** the widget removes the OEM process that normally owns more than keyboard lighting.
2. **Shared hardware transport:** both programs reach the same Clevo ACPI/DCHU/embedded-controller path.
3. **Command semantics:** the widget sends mode/status/apply calls in addition to an RGB value, so its traffic is broader than a simple color write.
4. **Recovery verification:** seeing `FnKey.exe` after relaunch does not prove that the OEM profile was reapplied, persisted, or accepted by firmware.

There is no proof that `KBModule.SetMode()` directly selects the CPU Performance/Quiet profile; it may be local to keyboard mode. The risk comes from terminating the platform owner and repeatedly exercising a shared low-level path, not from an unverified assertion that one named lighting call is itself a CPU-frequency command.

### Transaction-rate and acknowledgment risk

Historical profiling found incoming color states approximately every 33 ms while some native operations took 35-60 ms. A later commit added pacing, but the present limit can still result in roughly 16-20 complete five-call update sets per second.

The current helper protocol also does not enforce one completed native transaction at a time. The widget writes commands to the helper pipe and continues without waiting for the helper's `DURATION` acknowledgment. The Python-side queue can coalesce pending desired colors, but commands can still accumulate in the operating-system pipe if a native operation slows or stalls. The UI records a state as sent when it is enqueued, not when hardware acknowledges it. A native `UPDATE_ERROR` is logged but does not automatically stop traffic and yield ownership back to Clevo. The helper checks for OEM processes only during the initial takeover; it does not continuously detect an `FnKey` or Control Center relaunch, so a second owner can reappear while native traffic is active.

### Historical implementation risk

The project history shows how the risk accumulated:

- `25d1f6e` documented the direct native approach: terminate the OEM owner, load its assembly, and call the apply functions.
- `cbfd45f` integrated the approach into the widget, enabled it by default, and hard-coded `FnKey` AppX version 2.6.1. The loaded assembly and initialized object could outlive a toggle-off while `LaunchFnkey.exe` was started again, creating the possibility of two initialized owners.
- `c6d2135` added a persistent worker/queue and repeated the complete native sequence for changing LED states.
- `61839ba` reduced excessive CPU usage and added pacing after native-call timing was measured, but it did not remove the shared-owner or shared-ACPI risk.
- At the time of this report, the isolated helper and package-discovery improvements in the working tree were uncommitted.

The committed `HEAD` at `a66d466` is particularly unsafe with the newly installed v1.14 package. It kills `FnKey` before trying the hard-coded v2.6.1 assembly path. If that path fails, its `enabled` flag remains false, and `disable()` returns without restarting `FnKey`. Because sync defaults on and is invoked during construction, merely launching that committed version can terminate v1.14 `FnKey`, fail to load the removed v2.6.1 assembly, and skip recovery.

The uncommitted working tree improves package discovery and partial-takeover tracking but is not safe to launch yet: it still defaults synchronization on and invokes takeover during construction. Its toggle-off path ignores the success/failure returned by restoration, and shutdown proceeds to unconditional `os._exit(0)` after requesting disable. The UI can therefore show sync off, or the process can exit, even when `FnKey` restoration failed.

Two ancillary scripts are also hazardous in the restored environment:

- tracked `clevo_led_example.py` kills the OEM processes, hard-codes the removed v2.6.1 path, suppresses errors, and never restores `FnKey`;
- untracked `scratch/test_pythonnet.py` follows the same kill-then-old-path pattern without restoration.

Neither should be run until it has a prominent safety guard or is redesigned. This report records the risk; it does not delete or alter either user file.

## Chronological incident and recovery record

### 1. Initial inspection and first A/B test, 12:46-13:13

The widget was initially not running and `FnKey` was running. Inspection showed that the active Windows plan allowed 100% processor state and boost, while the widget's default-on Clevo path could terminate `FnKey` and use the same vendor hardware stack.

An early, intentionally low-heat test used a 12-second single-core PowerShell arithmetic load pinned to logical processor `(0,0)`. Eight one-second samples collected Actual Frequency, `% Processor Performance`, `% Utility`, `% of Maximum Frequency`, and `% Performance Limit`.

| State | Approximate observed frequency |
|---|---:|
| Widget off, Clevo Performance | 2002 MHz |
| Widget on, Clevo Performance | 1960 MHz |
| Widget on, Clevo Entertainment | 1998 MHz |
| Widget on, Performance reapplied | 1997 MHz |
| Widget on, Clevo Power Saving | 2010 MHz |
| Widget off/restored, Power Saving | 1962 MHz |
| Widget off/restored, Performance | 2005 MHz |

Those small differences were test noise and were not evidence of a direct frequency cap. The important result was behavioral: after the widget terminated `FnKey`, Control Center visually accepted mode changes, but the modes were indistinguishable and did not behave as persistent state. When the widget exited and `FnKey` was restored, the UI reverted from the selected Power Saving state to Performance. This was the first direct evidence that the widget interfered with the process responsible for applying or preserving Clevo profile state.

This first test was not the decisive performance test. A single-core, short-duration load is insensitive to the all-core package limits controlled by an OEM performance profile. The later 12-process full-load tests are the comparisons used for the recovery conclusion.

### 2. Exact factory package selection, 13:18-13:31

The user identified the original driver archive used when Windows was installed:

`C:\Powerspec 1520 drivers - zipped\1_motherboard_psdriver_17y.exe`

Its SHA-256 was:

`07D3B66205343437BE18E242C95BD8862C09D3DEF7B2C3C7191A2655C457BD31`

The outer self-extractor was not Authenticode-signed, but it contained the exact PowerSpec/Clevo factory package. The embedded installer was:

`C:\drivers\motherboard\07_Control_Center_Driver\setup.exe`

Its SHA-256 was:

`7C6E9880ECF7D43DAF5D5412F624607FB80429EF3B56674213CA371988FCAAB7`

That installer had a valid CLEVO signature and identified itself as ControlCenter 3.0 Driver v1.14, display version 1.14.0, product GUID `{52CF73F1-9FE1-4917-AE56-55BF319988EC}`. It was preferred over a generic newer Clevo package or a current Intel utility because it was the hardware-specific factory payload for this PowerSpec model.

The installed system was on the newer Control Center 2.06.000 generation. Its current-user AppX components were:

- Control Center 7.68.1;
- FnKey 2.6.1;
- Flexikey 6.17.4;
- LED Keyboard 7.68;
- GPU Overclocking 7.68;
- Fan 7.87.

The `CCDCHUService` was stopped with exit code 1067 and had crashed at 13:11:17. A 45-day component-fault sample also contained 49 `LaunchFnkey.exe` application crashes and one `DCHUService.exe` crash. These are evidence of a repeatedly unhealthy OEM ownership/recovery layer, but the historical crashes cannot all be attributed to the widget from the available data.

### 3. Restore point, downgrade guard, and pre-removal evidence

The user created and confirmed a System Restore point named:

`Before Clevo CC factory reinstall 2026-08-27`

The restore point was user-confirmed; its presence could not be independently queried without elevated access.

At approximately 13:33, a direct attempt to run v1.14 correctly displayed a downgrade guard: version 2.06.000 was already installed, v1.14 was earlier, and the existing version had to be uninstalled first.

The first evidence snapshot was stored at:

`C:\Powerspec 1520 drivers - zipped\clevo_reinstall_evidence\before_20260827_132957`

### 4. Uninstall v2.06 and first reboot, approximately 13:31-13:47

The v2.06 signed maintenance wizard was used to uninstall the existing Control Center. The user selected **No, I will restart later** so the immediate state could be captured first.

The post-uninstall/pre-reboot snapshot was stored at:

`C:\Powerspec 1520 drivers - zipped\clevo_reinstall_evidence\after_uninstall_before_reboot_20260827_133907`

At ordinary user scope, the uninstall registration, program/cache files, current-user Clevo AppX registrations, and Clevo processes appeared absent; services were stopped. That observation later proved incomplete: higher-version provisioned/staged AppX packages still existed outside the current user's visible registrations.

SetupAPI evidence showed that the v2.06 uninstaller removed the staged `acpi0002` 1.0.0.26 package (`oem80.inf`) at 13:37:18. It attempted to remove active 1.0.0.9 (`oem25.inf`) at 13:37:33, but Windows correctly refused because `ACPI\CLV0002\1` was using it. This avoided removing the live platform device's driver.

The first reboot completed with a recorded boot time of 13:40:24. The post-reboot/preinstall evidence is at:

`C:\Powerspec 1520 drivers - zipped\clevo_reinstall_evidence\post_reboot_preinstall_20260827_134738`

The backend was then sufficiently clean for the factory installer.

### 5. Install v1.14 and second reboot, approximately 13:49-14:06

The signed factory v1.14 setup was installed. The user again selected **No, I will restart later** to permit an intermediate snapshot. The installer ended with:

`createFirstLogonDelay Failed`

The installation otherwise completed. The warning did not mean the entire package failed, but it became relevant: after reboot, the backend services and filter drivers were present while the current-user Store-style front ends, including `FnKey`, were missing. The likely functional meaning was that a first-logon registration/startup step did not complete, although the exact code path behind the message was not established.

The pre-reboot snapshot was stored at:

`C:\Powerspec 1520 drivers - zipped\clevo_reinstall_evidence\after_v114_install_before_reboot_20260827_135803`

During installation, v1.14 reconfigured ACPI Bridge 1.0.0.6 and reused/copy-registered the already-present active 1.0.0.9 package. The driver end state was coherent.

The second reboot completed with boot time 14:00:33. The next snapshot was stored at:

`C:\Powerspec 1520 drivers - zipped\clevo_reinstall_evidence\post_v114_reboot_before_appx_repair_20260827_140645`

At this point, the core services and filters were healthy, but there were zero current-user Clevo AppX packages and no `FnKey` process.

### 6. OEM application-registration diagnosis and repair, 14:06-14:30

The factory package includes:

`C:\Program Files (x86)\ControlCenter\AppInstall\RunAutoInstall.exe`

This helper is an OEM-installed component from the signed factory setup; the helper executable itself is not Authenticode-signed. It is version 1.10.0.0 with SHA-256:

`4B483910E9D02A7FBD9F0244082D9BEE9A7E85A5297140E6895F4411D4450104`

It must be run with its `AppInstall` directory as the working directory. A normal attempt produced a brief message remembered as referring to no Fn/FnKey component being installed. Normal and elevated-user attempts then failed six DISM provisioning operations with `0x80070005` (access denied). Even the administrative user token could not read the required provisioning registry state.

A narrowly scoped, temporary scheduled-task probe running as Windows `SYSTEM` succeeded. The privileged OEM helper then exposed the true problem: the uninstall had removed visible current-user registrations and much of the Win32 backend, but six higher-version v2.06-era AppX families remained provisioned/staged. Their higher versions caused downgrade conflicts with the v1.14 bundles (`0xC1570118`); the GPU package also had a stale-stub preference conflict (`0xC1570117`).

After the user explicitly authorized removal, exactly these six stale families were removed under `SYSTEM`:

- Control Center 7.68.1;
- FnKey 2.6.1;
- Flexikey 6.17.4;
- LED Keyboard 7.68;
- GPU Overclocking 7.68;
- Fan 7.87.

No driver, service, unrelated AppX family, BIOS component, or user file was removed. The AppX deployment log records deprovisioning, removal, and destaging at approximately 14:26:13-14:26:19.

The OEM helper was then rerun as `SYSTEM` and installed the v1.14 generation's signed AppX bundles:

| Component | Factory version | Deployment time |
|---|---:|---:|
| Control Center | 1.13.2 | 14:27:48-14:27:51 |
| FnKey | 1.14.0 | 14:28:00-14:28:02 |
| Flexikey | 1.12.2 | 14:28:13-14:28:16 |
| LED Keyboard | 1.14.0 | 14:28:27-14:28:29 |
| GPU Overclocking | 1.11.1 | 14:28:37-14:28:39 |
| Fan | 1.13.0 | 14:28:46-14:28:48 |

The bundle also contained CPU Overclocking 1.11.7, but the factory helper intentionally omitted it based on a hardware/WMI feature flag. It was not manually forced onto the machine.

The temporary `SYSTEM` tasks were:

- `Codex_Clevo_Appx_ReadProbe_20260827` — read-only access probe;
- `Codex_Clevo_StaleAppRemoval_20260827` — the explicitly authorized six-family removal;
- `Codex_Clevo_OEM_AppRepair_20260827` — the OEM application repair.

Each task was removed after use. A later live check found no remaining `Codex_Clevo_*` scheduled task.

Repair logs were captured at:

`C:\Powerspec 1520 drivers - zipped\clevo_reinstall_evidence\after_appx_repair_20260827_143023`

That directory contains the raw `dism.log` and `AppXDeploymentServer_Operational.evtx`. Unlike the first five snapshots, it does not contain a manifest or general state file.

### 7. Third reboot, profile-persistence test, and pre-reset benchmark, 14:31-15:00

The third reboot completed with boot time 14:31:58. All six intended packages were present and later remained in `Status: Ok`. Services and filter drivers were healthy, the widget was absent, and the Windows CPU policy remained unrestricted.

Control Center initially showed Quiet. A 12-process workload was used to saturate all 12 logical processors. Each state used 24 half-second samples. These pre-reset values were calculated as nominal 2208 MHz multiplied by `% Processor Performance` divided by 100; they are effective-frequency estimates rather than direct per-core clock readings:

| State | Avg `% Processor Performance` | Estimated effective frequency | Avg CPU time |
|---|---:|---:|---:|
| Quiet; `FnKey` absent | 57.48% | 1269 MHz (1200-1322) | 99.92% |
| Performance selected; `FnKey` absent | 58.27% | 1287 MHz (1224-1385) | 99.29% |

Closing and reopening Control Center reverted the selection to Quiet. This proved that the AppX packages alone were not enough; the active `FnKey` owner was required for profile persistence.

At 14:50:05, the `clevofnkey:` application activation was invoked manually. `FnKey` v1.14 remained resident, and Control Center automatically moved from Quiet to Entertainment. Selecting Performance now persisted after Control Center was closed and reopened. This was a successful repair of the application-ownership layer.

However, the same 12-process saturation workload still produced:

| State | Avg `% Processor Performance` | Estimated effective frequency | Avg CPU time |
|---|---:|---:|---:|
| Performance; `FnKey` active | 58.59% | 1294 MHz (1241-1452) | 98.99% |

The distinction was critical: `FnKey` activation fixed the visible/persistent profile, but it did not clear the deeper conservative hardware state.

The startup manifest contained an enabled `FnKeyStartupId` task, but no immediate-registration request. `FnKey` did start automatically at the later sign-in. That may simply have been the task's first eligible sign-in after registration; the evidence does not establish that manual activation primed the startup entry.

### 8. Intel XTU/DCHU and other-driver investigation

The system contained a stale auto-start `iocbios2` service whose path referred to a missing older Intel Extreme Tuning Utility driver under `Program Files (x86)\Intel\Intel(R) Extreme Tuning Utility`. It failed at every boot. A signed `XtuAcpiDriver` 4.0.0.21 package was present but configured manual/stopped. `XTU3SERVICE` was absent. The installed Clevo layer included DCHUService 1.0.0 and IntelOverclockingSDK 6.4.1.24.

Reverse engineering and live behavior separated optional CPU-overclocking functions from ordinary Clevo power-profile selection:

- DCHU command `clevocc30^103` waits indefinitely for `XTU3SERVICE`, then initializes/reads telemetry; it is not the basic power-mode setter.
- DCHU command `clevocpuoc^107` bypasses that wait and exposes a null CPU-name path during `Init_CPU -> InitCPUXTU -> ReloadXTUInfo`. Its invocation at 13:11:11 immediately preceded, and is strongly implicated in, the verified 13:11:17 DCHU crash. No captured crash stack identifies that null path as the definitive fault site.
- Commands 106/108 are the CPU-overclock tuning paths.
- The ordinary Clevo profile is a separate path: `clevocc30^101^2`, corresponding to WMI method 121, subcommand 25, data 2 for Performance.

The factory INF intentionally comments out the legacy full XTU stack. Inspection of the other drivers under `C:\drivers` found the relevant factory ACPI Bridge and management-engine components, but no compatible missing `XTU3SERVICE`, BIOS, or EC installer that should be added to this model. Current Intel XTU is not a suitable remedy for this locked i7-8750H configuration and was not installed.

This XTU/DCHU issue remains a separate defect in optional CPU-overclocking/telemetry functionality. It was not the cause of the basic profile failure, because Quiet and Performance ultimately worked correctly without `XTU3SERVICE` after the embedded-power reset.

Thermal throttling was also ruled out as the explanation for the conservative pre-reset frequency. The pre-reset capped run was only about 337.5 K (approximately 64 C) and reported no throttle reason. After recovery, a brief full-load Performance run reached approximately 369-370 K (96-97 C) but still reported no thermal-throttle or performance-limit flag. The restored system was producing high frequency and correspondingly higher heat; it was not being held at 1.29 GHz by a hot CPU.

### 9. Embedded-power reset, approximately 15:24-15:30

With Control Center left on Performance and the widget closed, the user performed this sequence:

1. Shut Windows down through the UI rather than selecting Restart.
2. Disconnect AC and USB/external power sources.
3. Hold the laptop power button for 30 seconds.
4. Release it and wait another 30 seconds.
5. Reconnect AC only and boot.

Windows Fast Startup was enabled (`HiberbootEnabled=1`). Event evidence showed a Kernel-Power transition at about 15:24, briefly inhibited by `\Driver\vpcivsp`, and a second transition around 15:26 that produced the longer off interval before startup near 15:29. After boot, the Windows BootId remained 117 and `LastBootUpTime` remained 14:31:58. In other words, Windows restored the same hibernated kernel session rather than performing a fully fresh Windows kernel boot.

Despite that retained Windows kernel session, CPU performance recovered. `FnKey` automatically started at 15:30:41, and at 15:30:54 it was resident as PID 25088. The stored Clevo/ACPI Bridge AppSettings value at page 1, offset 1 (registry byte index 257) read `2`, matching Performance in the UI. This was a registry-backed mode value correlated with Control Center, not a direct EC-register read. The widget process was absent; AC was online; DCHU and HKClip were running; the Windows plan was still Balanced.

### 10. Decisive post-reset A/B test

The same 12-process saturation workload class was repeated without changing the Windows power plan. In these post-reset runs, `Actual Frequency` was read directly from the performance counter, so the MHz column is not calculated by the pre-reset method:

| Test | Stored Clevo mode | Avg `% Processor Performance` | Actual frequency, avg (min-max) | CPU | Limit/throttle flags |
|---|---:|---:|---:|---:|---:|
| Initial Performance after reset | 2 | 149.78% | **3307.03 MHz** (3288.23-3319.34) | 100% | 0 |
| Quiet selected in UI | 0 | 100.00% | **2207.99 MHz** (2207.89-2208.06) | 100% | 0 |
| Performance selected again | 2 | 151.70% | **3349.60 MHz** (3328.73-3407.76) | 100% | 0 |

The final Performance run reached a maximum `% Processor Performance` of 154.34%. There were no reported performance-limit or thermal-throttle flags.

This three-point comparison is the decisive proof of recovery. It demonstrates all of the following under an otherwise unchanged Windows power plan and workload:

- Control Center changed the stored Clevo/ACPI Bridge mode byte in step with its UI selection.
- Quiet and Performance produced materially different, appropriate frequency envelopes.
- Returning to Performance restored turbo behavior above the CPU's 2.208 GHz nominal frequency.
- The prior 1.29 GHz result was not explained by the workload, Windows Balanced, the CPU's capability, simple absence of AC power, or missing `XTU3SERVICE`. Adapter/charger/EC power-budget coordination remains one plausible low-level mechanism.

## Critical path to the failure

The most defensible reconstruction is:

```text
Widget launch
  -> Clevo keyboard sync defaults on
  -> widget force-terminates FnKey and related OEM processes
  -> OEM profile owner is removed
  -> widget initializes another KBModule through the same Clevo DCHU/ACPI/EC route
  -> repeated multi-call updates occur without end-to-end acknowledgments
  -> UI/profile state can diverge from applied firmware state
  -> normal widget recovery verifies only that an FnKey process appeared
  -> warm restarts and software reinstall repair Windows components but preserve
     a conservative retained platform/controller state
  -> Performance can appear selected while full-load frequency remains ~1.29 GHz
```

The direct observations prove the owner removal, shared vendor interface, repeated native calls, nonpersistent profiles without `FnKey`, persistent profiles after `FnKey` activation, and the before/after frequency behavior. The link from those operations to a particular stale EC state is a strong causal inference rather than a captured EC-register trace.

## Critical path to recovery

Each step resolved a distinct layer:

```text
Create recovery point and capture baseline
  -> uninstall v2.06 and reboot
  -> install exact signed OEM v1.14 backend and reboot
  -> discover failed/missing first-logon AppX registration
  -> use SYSTEM visibility to find hidden higher-version staged packages
  -> remove only six stale Clevo families
  -> run OEM helper to provision the signed v1.14 AppX bundles
  -> reboot
  -> manually activate FnKey and prove profile persistence
  -> use all-core test to prove conservative state still exists
  -> rule out Windows frequency settings, thermal throttling, and basic-profile
     dependence on the incomplete XTU stack
  -> leave mode on Performance
  -> perform shutdown/unplug/power-button embedded-power reset
  -> FnKey auto-starts and reapplies stored Performance mode on a clean controller
  -> repeat the Quiet/Performance saturation A/B and verify 2.208 vs 3.350 GHz
```

The reinstall and AppX repair were necessary to the recovery path that was taken: they repaired the observed OEM ownership layer and ensured that a known-good `FnKey` could preserve profiles. They were not, by themselves, sufficient to restore frequency. Because no power-reset-before-reinstall counterfactual was run, the investigation cannot prove that the entire reinstall was strictly required for raw frequency recovery. The embedded-power reset was the final changed boundary, and recovery is most consistent with it clearing the remaining low-level condition.

## Why the embedded-power reset helped

### What an embedded controller is doing here

On this class of laptop, the embedded controller (EC) is a platform microcontroller involved in OEM functions such as keyboard control, hotkeys, fan and thermal policy, adapter/battery coordination, and platform power behavior. Windows applications typically reach it indirectly through ACPI methods and vendor drivers. Clevo's Control Center/FnKey/DCHU stack and the widget's imported Clevo `KBModule` therefore converge on a subsystem that is not reset merely because a user-space process exits.

### Why a normal restart was insufficient

An EC and related charger/voltage-regulator/always-on logic can remain powered from battery or auxiliary rails across a Windows restart and, on some designs, across an ordinary shutdown. Reinstalling an application changes files, registrations, services, and drivers; it does not necessarily reset the state machine or volatile command state of that controller.

The incident provided unusually useful separation between those layers:

- the install sequence included three ordinary reboots, and after the third the saturation workload still produced an estimated effective frequency near 1.29 GHz; individual benchmarks were not taken after the first two reboots;
- manually starting the correct `FnKey` restored profile persistence but still left the load near 1.29 GHz;
- Windows Fast Startup preserved the 14:31:58 kernel session across the final shutdown;
- nevertheless, after removal of external power and the long power-button hold, the retained Windows kernel session, same plan, OEM application, and same workload class produced a directly reported 3.31-3.35 GHz.

Because a fresh Windows kernel boot did not occur, recovery cannot reasonably be credited merely to reloading Windows or selecting a different Windows plan. The changed boundary was the power treatment of the platform hardware.

### What the button-hold sequence likely did

The power-button hold with external power removed can invoke an EC hard-off/reset path and/or allow auxiliary domains to discharge far enough to reset retained platform logic. On the next power-on, the firmware and `FnKey` initialize against a clean controller and the stored Performance value can be applied normally.

Plausible cleared state includes:

- a stale ACPI/EC command or mailbox state;
- a controller state machine left in a conservative fallback;
- a retained platform power-budget or turbo-enable value;
- adapter/charger or voltage-regulator coordination state;
- ownership state left after forced termination during a vendor transaction.

These are examples of mechanisms consistent with the evidence, not individually verified findings. The internal battery was not disconnected, no EC registers were dumped before and after, and the test does not prove that every auxiliary rail reached zero. The accurate conclusion is therefore:

> Recovery is most consistent with the hard/embedded-power reset clearing a retained platform-level state that ordinary reboots and software repair did not clear; the exact register or subcomponent was not identified.

The post-reset Quiet-to-Performance A/B then proved that the firmware control path was live again rather than producing a one-time unexplained turbo spike.

## Findings by confidence level

### Proven by code or observation

- The widget force-terminates `FnKey` and related Clevo processes.
- The widget loads Clevo's own `FnKey` assembly and drives a vendor `KBModule`.
- Each update includes mode, status, and apply calls in addition to RGB/brightness.
- Without `FnKey`, Control Center selections did not persist.
- Starting v1.14 `FnKey` restored profile persistence.
- AppX downgrade residue prevented the factory applications from provisioning until the six higher-version families were removed.
- Reinstall, AppX repair, and normal reboot did not raise the all-core load above approximately 1.29 GHz.
- Windows processor settings did not specify a conservative maximum.
- The low pre-reset frequency occurred without a thermal-throttle flag and at a comparatively low temperature.
- After the embedded-power reset, the same saturation workload class reached approximately 3.35 GHz in Performance and 2.208 GHz in Quiet.
- Basic Clevo profiles now work without `XTU3SERVICE` or the optional CPU Overclocking AppX package.

### Strongly supported inference

- The timing and layer separation strongly support the long power-button sequence having reset retained state in the EC or a closely related always-on power-management domain.
- After that reset boundary, the repaired and auto-starting `FnKey` operated with stored mode `2`, and Clevo's intended Performance behavior returned.

### Architecture-supported leading hypothesis, not reproduced after recovery

- Project reverse engineering indicates that the widget's imported Clevo interface and the OEM stack converge on the same DCHU/ACPI/platform-control route, but that route was not instrumented end to end during this incident.
- Forced OEM-process termination, non-acknowledged repeated vendor calls, or ownership interruption is the leading explanation for how the retained conservative platform-controller state was caused or exposed.
- Widget interference with profile ownership was reproduced. Widget causation of the persistent CPU ceiling was not deliberately reproduced after recovery because doing so risked recreating the fault.

### Plausible but not proven

- The retained value was specifically PL1, PL2, a turbo ratio, an adapter power budget, or a voltage-regulator setting.
- One particular `KBModule` method directly changed CPU limits.
- The controller was interrupted in the middle of a transaction.
- A particular EC register or mailbox was stuck.
- Every relevant rail fully discharged during the reset.

### Ruled out as the primary explanation

- A Windows maximum-processor-state or maximum-frequency cap.
- The Windows Balanced plan itself.
- Thermal throttling as the cause of the pre-reset 1.29 GHz ceiling.
- Absence of the optional CPU Overclocking/XTU component as a requirement for ordinary Quiet/Performance profiles.
- A fundamentally incapable CPU or workload, because the post-reset saturation test produced 3.35 GHz.

## Current known-good state

The final live package check found these current-user packages present with `Status: Ok`:

- Fan 1.13.0.0;
- Control Center 1.13.2.0;
- Flexikey 1.12.2.0 (x86);
- FnKey 1.14.0.0;
- GPU Overclocking 1.11.1.0;
- LED Keyboard 1.14.0.0.

CPUOverclocking is absent by the factory installer's hardware-selection logic. A current-user process cannot repeat the full provisioned-package query without elevation, so successful system provisioning is supported by the captured `SYSTEM` deployment log plus the live healthy current-user registrations.

At the end of testing:

- Control Center showed Performance;
- the stored Clevo/ACPI Bridge mode byte was `2`;
- `FnKey` was running;
- the widget was closed;
- the Windows plan remained Balanced;
- AC was connected;
- the final full-load average was 3349.60 MHz;
- no performance-limit or thermal-throttle flag was reported.

An Event 34 at the earlier 14:32 boot stated that idle power-management features were disabled due to a firmware problem for all 12 logical processors. This is residual firmware telemetry worth retaining, but it is not evidence that the restored full-load run was throttled.

## Required operational precautions

Until the Clevo integration is redesigned and hardware-regression tested:

1. The incident-era widget must remain closed. On the later `keyboard-rgb-diagnostics` branch, synchronization defaults off, constructor-time takeover is removed, and the real Clevo backend is fail-closed. That prototype is safe to launch only in this blocked/no-write state; it is not evidence that functional keyboard sync is safe.
2. Do not run the committed `clevo_led_example.py` or `scratch/test_pythonnet.py` on this installation.
3. Do not follow `clevo_led_research.md:12-25` as current operating guidance; its historical “solution” still instructs readers to kill `FnKey` and must be rewritten before reuse.
4. Do not launch the committed build that hard-codes FnKey 2.6.1 or restore any of its takeover behavior. Do not bypass the later prototype's blocked backend or experimental gate.
5. Use Control Center/FnKey as the sole owner of the vendor interface when verifying performance.
6. If the CPU again remains near a conservative frequency while Performance is selected:
   - close the widget;
   - confirm `FnKey` is running and the profile persists after reopening Control Center;
   - if testing is needed, use a short, user-initiated, temperature-monitored Quiet/Performance comparison with explicit abort limits rather than an automatic or unattended stress test;
   - if the profiles remain indistinguishable, use the documented shutdown/unplug/30-second hold/30-second wait/AC-only boot sequence once as a bounded recovery step while the widget remains closed and sync is inactive;
   - if the fault recurs while sync remains off, capture evidence and investigate the OEM/firmware/hardware path instead of repeating resets in a loop;
   - do not install a current Intel XTU package as a shortcut.
7. Treat a high post-recovery full-load temperature as a cooling/maintenance question separate from this incident; the brief test reached roughly 96-97 C even though no throttle flag was asserted.

## Engineering changes recommended before re-enabling sync

### Ownership and startup

- Default keyboard sync to off.
- Do not invoke hardware takeover during widget construction.
- Prefer a documented lighting-only interface that does not terminate or import the platform-wide `FnKey` owner.
- If takeover remains unavoidable, require explicit opt-in and explain that Control Center profile control is temporarily unavailable.
- Do not treat a widget mutex as mutual exclusion with Clevo: `FnKey` cannot participate in it. A mutex may prevent duplicate widget instances, but a supported lighting-only interface that does not displace the OEM stack is the actual safety boundary. Any experimental takeover must also monitor continuously for OEM relaunch and yield safely.

### Transaction safety

- Permit at most one native update in flight.
- Add sequence-numbered acknowledgments; mark a color applied only after a successful native response.
- While waiting, retain only the newest desired color.
- On timeout, `UPDATE_ERROR`, helper exit, sleep/resume anomaly, or OEM-process relaunch, stop all native calls and restore OEM ownership.
- Prove which calls are strictly required for lighting. Omit `SetMode`, `SetStatus`, and `UpdateCurrentStatus` unless controlled testing establishes that each is necessary and safe.
- Reduce update rate substantially below native-call latency and avoid writes when the displayed color is unchanged.

### Recovery design

- Discover and validate the installed package before terminating any OEM process.
- Track partial takeover so every failure after ownership changes invokes restoration.
- Use the OEM activation path for normal launch; do not routinely use `Add-AppxPackage -Register` as a toggle-off mechanism.
- Verify restoration by more than process existence: the process must remain resident, the selected profile must persist after reopening Control Center, and DCHU must be responsive. Any performance verification must be short, user-initiated, temperature-monitored, and abortable; never place a 12-process stress test in normal toggle-off or shutdown recovery.
- If an independent watchdog or broker is added, it must own the entire takeover lease, confirm the native helper is dead, and then use ordinary OEM activation. It must never launch `FnKey` concurrently with native calls or perform routine AppX re-registration. Cleanup located only in normal widget shutdown cannot run after Task Manager termination or a hard crash.

### Regression coverage

At incident time, no tracked test exercised `ClevoKeyboardController`, `FnKey`, `KBModule`, partial takeover, helper errors, or restoration. The later fail-closed prototype added software-only lifecycle and negative-path coverage. Any future real backend still requires hardware tests for:

- default-off startup;
- package-discovery failure before any process termination;
- failure injection after every takeover step;
- native timeout and `UPDATE_ERROR`;
- Control Center opening while sync is active;
- a duplicate widget instance;
- normal exit, helper crash, parent crash, and Task Manager termination;
- sleep, hibernate, resume, shutdown, and Fast Startup;
- version changes in the FnKey AppX path;
- hardware A/B confirmation of profile persistence and effective CPU frequency before and after sync.

## Post-recovery safe-IPC qualification (2026-08-27 evening)

After the CPU-performance recovery, the widget was patched around a strict
invariant: `FnKey` must remain resident and Clevo profile switching must remain
available. The unsafe constructor-time activation, vendor-assembly import,
`KBModule` calls, process termination, AppX repair, and direct DCHU/ACPI paths
were removed from the runtime integration. A serialized software-only controller,
protocol validation, lifecycle handling, immutable model/package allowlist, and
fail-closed `ClevoIpcBackend` were added. The real backend performs no hardware
write because qualification had not succeeded.

### Exact restored target and binary snapshot

The qualification target matched the expected PowerSpec 1520 / Clevo PB50EF-G
and factory package versions:

- FnKey `CLEVOCO.FnhotkeysandOSD` 1.14.0.0;
- LED application `CLEVOCO.LedKeyboardSetting` 1.14.0.0;
- Control Center `CLEVOCO.ControlCenter3.0` 1.13.2.0;
- publisher-family suffix `6h6z29zh29qx0`;
- `CCDCHUService` and `HKClipSvc` running;
- `FnKey.exe` running continuously from its allowlisted package path.

Selected SHA-256 identities were:

- `FnKey.exe`: `E0AC77D77FD27735D52840CD8A96DD5AEA8701406E639D2B400626F8EFF8EFEE`;
- FnKey `kb.dll`: `90722FF44D4595930A30DA5854E30FB5C10087037E2F7F4AC742E40DB4E194BF`;
- `InsydeDCHU.dll`: `98AF9793B911AB57FCBD09FD75441A120A19A529F4D18D03D83BE42DE7020A47`;
- `LedKeyboardSetting.exe`: `301F88912D1BD137C4D4572BA5EFE9D37825D949932155B62A9A6C6FCBC08F14`;
- `ControlCenter30.exe`: `50A7879B6DB36E1866CED674BD93844C7CC48C8BD092822A13D83A2407D5E167`.

Most OEM executables and DLLs were not individually Authenticode-signed;
`InsydeDCHU.dll` carried a valid Microsoft Windows Hardware Compatibility
Publisher signature. Consequently, package identity, exact version, path, and
hash are required identity evidence rather than relying on executable signature
alone.

### Static routing result

IL call-graph inspection of the exact v1.14 package found:

- `clevokeyboard:` launches the official LED application; it is not a developer API;
- the LED application's `WM_COPYDATA` receiver handles keyboard status and brightness notifications;
- the receiver's larger-package branch does not perform a lighting write;
- `SendMsg_WMI` is a generic five-byte pass-through to `HkeyTray`, so it cannot be safely allowlisted as lighting-only;
- the observable lighting implementations ultimately call vendor `SetDCHU_Data`/ACPI paths directly.

This eliminated the obvious OEM message machinery as a qualified lighting-write
interface before any active probe.

### Passive action captures

An initial broad passive trace recorded a single software-red selection, which
produced physical green as expected from this keyboard's red/green channel swap.
A narrower trace configuration then excluded the high-volume internal message
pump and captured four isolated actions:

| Tag | Duration | Action/result |
|---|---:|---|
| `ColorBlue` | 4.223 s | one software-blue selection; physical blue appeared |
| `BrightnessDown` | 4.412 s | one OEM brightness decrement |
| `ProfileQuiet` | 5.311 s | one Quiet selection |
| `ProfilePerformance` | 4.912 s | one Performance selection |

No trace contained `WM_COPYDATA`. During the color and brightness actions,
`LedKeyboardSetting.exe` sent no window message to `FnKey.exe` or Control Center.
No WMI Activity event was attributable to the LED PID or named the Clevo/DCHU/ACPI
lighting path. The only `SendMessage` delivered to the LED process was an
unrelated `WM_NULL` from Desktop Window Manager. Quiet and Performance also
exposed no lighting message that could be isolated and reused. Throughout the
session, `FnKey` retained PID 25088 and its exact package path, Control Center
retained PID 6336, the LED app retained PID 29168, and both required services
remained running.

The short ETL evidence remains under `%TEMP%` with these SHA-256 hashes:

| Capture | Win32k ETL SHA-256 | WMI ETL SHA-256 |
|---|---|---|
| `ColorBlue` | `BEEEE08E926019BB94297423DAD76E87B339E0E531B4459A11DD3CDDF862F406` | `0FFFEEA460BB50CE8BA0629A02EF0BE2A14ED5887D4ED57A2921577F182BFF3E` |
| `BrightnessDown` | `441B2E187EB01131E173C77EE4807D516D86082A296921CA163169AE617C9BAC` | `71610E70B3F549CD382922325783D59A3E1BAFF4439F994E0CBBECEEAA03F374` |
| `ProfileQuiet` | `1E2C148D7F4C93CCD87A17F88901727ACAB4BF4AED249E2E26C45F0AE8869E2E` | `1E10F5646FFD8A0F84695DDC1FB247C493FD4C4127E7F99193957F0AA0D364D2` |
| `ProfilePerformance` | `638C43B1E80716952A79626DDB988C17CBDCD176B6A89FEE926F38A5058699EE` | `C31021CAA5D9504F1BE6E6C1AC9DC501627F49EEB05528EBBA4B3C1FF9DAC6E4` |

The capture helpers are `diagnostics/clevo_passive_etw.ps1` and
`diagnostics/clevo_passive_etw_quick.ps1`. They only manage passive trace
sessions and do not invoke Clevo hardware or alter OEM processes.

### Qualification decision

The required lighting-only, fixed-payload, targeted, acknowledged, readable,
and reversible OEM IPC was not found. Static and dynamic evidence instead agrees
that the OEM lighting UI directly participates in the shared DCHU/ACPI hardware
route. Therefore:

- the active one-frame probe was not attempted;
- no guessed command, generic WMI pass-through, direct DCHU/ACPI call, UI automation, registry write, or old `KBModule` takeover may substitute for the missing IPC;
- `ClevoIpcBackend` remains deliberately blocked;
- keyboard sync remains off by default and cannot accurately be called functional;
- the widget is safe with respect to this incident only while the backend remains blocked and performs no Clevo writes.

For any future qualified backend, the public color domain must represent desired
physical RGB. At the final machine-specific wire boundary only, this PowerSpec
1520 must map `(r, g, b)` to `(g, r, b)`. ACK and readback comparisons must state
whether they represent desired physical color or transmitted Clevo channels.

## Exact OEM Lighting Transaction Follow-up (2026-08-27)

Later investigation first tested the independent USB HID collection
`048D:8910/COL02/FF89:00CC`. Windows accepted one documented key-color feature
report and a complete 120-address white matrix pass, but the keyboard did not
change. Feature readback returned `CE 00 00 00 00 00 00`, which was not a color
acknowledgement. The HID route was therefore rejected without changing `FnKey`
or either Clevo service.

Read-only decompilation of the exact restored v1.14 LED and FnKey packages then
identified the official whole-keyboard call chain. A color click calls
`CC.Page_LED_Device.Set_color`, assigns mode 8 and the all-color value, enables
the three RGB segments, and calls `CC.KBModule.SetMode`. This persists LED state
through AppSettings and sends four-byte command-103 frames through the package's
`InsydeDCHU.dll`.

A dynamic observer attached only to the signed official LED process and did not
attach to `FnKey`. One software-blue click produced:

```text
WriteAppSettings page 2 offset 81: 00 00 C8
WriteAppSettings page 2 offset 80: 01
SetDCHU_Data command 103: 8F 00 00 F4
SetDCHU_Data command 103: 00 00 C8 F0
SetDCHU_Data command 103: 00 00 C8 F1
SetDCHU_Data command 103: 00 00 C8 F2
SetDCHU_Data command 103: 01 B0 07 E0
```

`FnKey` retained PID 25088, `LedKeyboardSetting` retained PID 29168, and
`CCDCHUService` plus `HKClipSvc` remained running. The raw trace is
`%TEMP%\ClevoDchuTrace_20260827_212201.ndjson` (3,409 bytes), SHA-256
`44781D188FFCA399D349435C93B1935690008C3A29CF268F19101ADD824A4B1E`.

The resident v1.14 `FnKey` process has no RGB IPC. Its `clevokeyboard:` handling
launches or coordinates the LED application, and its only inbound
`clevokeyboard^202` event controls the ripple hook without accepting color
bytes. Its RGB methods are callable only inside its process. The official LED
application is therefore a separate, concurrent OEM DCHU client rather than a
caller routed through `FnKey`.

The CPU-profile path is structurally distinct in the inspected code:
`FnKey.Features.SetPowerMode` writes AppSettings page 1/offset 1 and sends DCHU
command 121/subcommand 25, whereas the captured color click wrote page 2 and
sent only command 103. This materially narrows the next prototype, but does not
by itself prove repeated shared-DCHU access safe. Any direct experimental broker
must reproduce only the observed color transaction, keep `FnKey` resident,
forbid generic DCHU commands, restore the original LED state, and pass the full
CPU-profile and lifecycle acceptance gates before being integrated or enabled
by default.

## Evidence retained

The following on-disk evidence directories were created during the reinstall:

- `C:\Powerspec 1520 drivers - zipped\clevo_reinstall_evidence\before_20260827_132957`
- `C:\Powerspec 1520 drivers - zipped\clevo_reinstall_evidence\after_uninstall_before_reboot_20260827_133907`
- `C:\Powerspec 1520 drivers - zipped\clevo_reinstall_evidence\post_reboot_preinstall_20260827_134738`
- `C:\Powerspec 1520 drivers - zipped\clevo_reinstall_evidence\after_v114_install_before_reboot_20260827_135803`
- `C:\Powerspec 1520 drivers - zipped\clevo_reinstall_evidence\post_v114_reboot_before_appx_repair_20260827_140645`
- `C:\Powerspec 1520 drivers - zipped\clevo_reinstall_evidence\after_appx_repair_20260827_143023`

The first five directories contain `manifest.json` files with SHA-256 hashes. The final AppX-repair directory contains the raw DISM and AppX deployment event logs but no manifest. No separate post-power-reset snapshot or raw benchmark artifact was created. The exact decisive numbers currently depend on the task transcript and their reproduction in this narrative; this report is the durable project copy of those values.

Relevant project evidence includes:

- `clevo_led_research.md` for the original native-interface research, with its kill-`FnKey` procedure now treated as hazardous historical material rather than current instructions;
- `hr_widget.py` for the active integration and recovery path;
- Git commits `25d1f6e`, `cbfd45f`, `c6d2135`, and `61839ba` for the implementation evolution;
- tracked `clevo_led_example.py` and untracked `scratch/test_pythonnet.py` for legacy kill-and-load patterns that must not be run in the restored environment.

## External technical references

- [UEFI ACPI Specification 6.6, including embedded-controller architecture](https://uefi.org/sites/default/files/resources/ACPI_Spec_6.6.pdf)
- [Microsoft: Fast Startup can hibernate the kernel session during shutdown](https://learn.microsoft.com/en-us/troubleshoot/windows-client/setup-upgrade-and-drivers/fast-startup-causes-system-hibernation-shutdown-fail)
- [Microsoft: Windows system power states](https://learn.microsoft.com/en-us/windows/win32/power/system-power-states)
- [Intel: Extreme Tuning Utility processor and platform requirements](https://www.intel.com/content/www/us/en/support/articles/000006636/processors/processor-utilities-and-programs.html)
- [Micro Center: PowerSpec laptop Control Center is supplied with motherboard drivers](https://community.microcenter.com/kb/articles/598-where-is-the-control-center-app-on-a-powerspec-1510-or-1710-laptop)
- [Dell example of an OEM laptop embedded-controller/hard reset procedure](https://www.dell.com/support/kbdoc/en-us/000225744/inspiron-and-vostro-laptop-with-no-power)

The Dell reference is cross-OEM context for the general reset mechanism, not model-specific proof for the PowerSpec 1520.

## Final conclusion

The software reinstall was valuable because it exposed and repaired a hidden version-layering problem, restored the correct factory `FnKey`, and made Clevo profile choices persistent. The reinstall alone did not restore CPU frequency. The decisive recovery occurred only after the repaired OEM stack was paired with an embedded-power reset; the result is most consistent with that reset clearing retained platform state. The post-reset Quiet/Performance A/B established that the laptop's native performance functionality is again working.

The widget remains the leading causal risk because it intentionally displaces the OEM owner and drives the same low-level Clevo interface with frequent multi-call updates and incomplete acknowledgment/recovery semantics. The precise firmware variable affected was not captured, so the report does not claim a specific direct CPU-limit write. The correct engineering response is to keep sync off, make OEM ownership authoritative, redesign the transaction and crash-recovery model, and require hardware-level profile verification before the feature is enabled again.

## 2026-08-27 LED-Safe Prototype Addendum

The preceding conclusion describes the unsafe takeover implementation and the
state before a replacement backend existed. It is superseded for LED operation
by a signed, package-identity broker that reproduces only the exact OEM static-
color transaction while leaving `FnKey`, Control Center, and both Clevo services
running.

The decisive discoveries were:

1. The independent USB HID collection accepted reports but did not change the
   keyboard, so it was rejected without disturbing OEM software.
2. The official LED application was observed sending only DCHU command 103 for
   color and brightness. Its CPU-profile code is structurally separate and uses
   command 121/subcommand 25.
3. The resident v1.14 `FnKey` exposes no callable lighting IPC. The supported OEM
   architecture instead allows the separate LED application to be a concurrent
   DCHU client while `FnKey` remains resident.
4. Ordinary elevated access to the protected OEM library was denied. A signed
   packaged broker supplied the required Windows package identity without
   changing Clevo ACLs, registration, services, or processes.
5. This PowerSpec 1520 has a physical red/green swap. The broker therefore
   accepts desired physical RGB and swaps red and green only at its fixed wire
   boundary.

The installed experimental broker is version 0.2.1.0, package full name
`HRWidget.ClevoBroker.Experimental_0.2.1.0_x64__tzh64df2et02a`, certificate
thumbprint `8FE55B1A7E3CFEE20754B882B91AD04BAAD280A7`, and MSIX SHA-256
`478FD6FC1E6AEA482E49167F3A70591346366CA72EEC9F17CE9667BAE6509F38`.
It is hard-limited to the restored PowerSpec/Clevo identities and OEM DLL hash
`98AF9793B911AB57FCBD09FD75441A120A19A529F4D18D03D83BE42DE7020A47`.
It has no CPU-profile command, generic DCHU pass-through, process or service
control, AppX repair, direct ACPI/WMI route, or brightness-write command.

The broker and controller passed 80 LED-focused software tests. On hardware,
one-shot blue and red/green-compensated changes restored the original lighting,
ten alternating enable/apply/disable cycles completed, and a competing second
session was rejected before writing. `FnKey` remained PID 25088 and
`CCDCHUService` plus `HKClipSvc` remained running. The user then confirmed that
the live widget produced physical blue and continued changing keyboard color as
the live heart-rate zone changed.

Color completion means synchronous vendor return 103; the OEM API did not offer
true RGB readback. The broker therefore never retries an uncertain operation and
quarantines the session instead. It restores the saved OEM color on an orderly
stop. Static color is limited to one acknowledged update per second and only
zone changes generate writes.

The user clarified that the exact post-sync color does not matter. Accordingly,
restoring it is a tested best-effort convenience, not the definition of returning
control. Control is returned when the broker lease and process end while the OEM
`FnKey` process and services remain untouched and available to make the next
lighting change.

Future ordinary launches default sync on only after background readiness and a
fresh broker safety probe. `--keyboard-sync-off` keeps it off for one run, while
`--disable-experimental-clevo-ipc` disables the backend entirely. The current
live session was deliberately left running after successful user confirmation.

At the user's direction, the attempted CPU stress benchmark was stopped and no
valid new CPU measurement was produced. The retained passive CPU-profile
sentinel is a safety tripwire, not a performance test and never writes a profile.
The result should therefore be described as a successful LED-focused
experimental deployment, not a renewed CPU-performance certification.
Brightness writes remain disabled pending their own canary and lifecycle gate.

### 2026-08-27 brightness qualification and final widget integration

The final sentence above is now superseded. Passive OEM traces captured every
brightness level in both directions, and the same fixed DCHU command-103 frames
were reproduced through the isolated broker. The observed whole-keyboard map is
level 0/off (`07 00 00 E0`), then levels 1–4 using `2F`, `5F`, `8F`, and `BF`
respectively in a brightness frame ending in `F4`; nonzero levels are followed
by the observed lighting-on frame `01 B0 07 E0`. The user visually confirmed a
broker sweep through 0→4→0 with every level distinguishable.

The installed broker is now version 0.2.5.0, package full name
`HRWidget.ClevoBroker.Experimental_0.2.5.0_x64__tzh64df2et02a`, and MSIX SHA-256
`21DA34F2A2E87DA368797CA7047FA285C776E2081C60DC58DFC80DD24036286B`. Its
certificate and fixed OEM/hardware allowlist are unchanged. Color and brightness
share one sequence, one in-flight command, synchronous vendor-return ACKs, newest-
pending coalescing, OEM-health checks, and the same broker lease. No CPU command,
generic pass-through, Clevo process control, or service control was added.

The widget now keeps color event-driven by heart-rate zone and animates brightness
from level 1 through 4 using the same monotonic heartbeat phase as its visual pulse.
Brightness is capped at 10 updates per second (100 ms), with a small measured-call
latency lead; unchanged discrete levels are not resent. This replaces the earlier
one-second prototype pacing, which was appropriate for first static-color canaries
but visibly too slow for animation. Ten hertz is below the former 20 Hz-era widget
traffic, above the measured 8–35 ms vendor-call duration, and sufficient for the
hardware's four nonzero brightness steps without needless commands.

The focused software suite passed 85 tests after integration. The signed package
was verified, the installed live probe qualified backend 0.2.5, `FnKey` remained
resident, and `CCDCHUService` plus `HKClipSvc` remained running. Normal widget
launches default keyboard sync on after the background loop and broker probe are
ready; `--keyboard-sync-off` and `--disable-experimental-clevo-ipc` remain the
session-level escape hatches. Final acceptance still requires the user's visual
check of live color and heartbeat-synchronized brightness in the launched widget.

An initial v0.2.3 launch quarantined before sustained animation because
the widget paced command starts while the broker measured 100 ms from the prior
vendor call's completion. That effectively added the 8–35 ms vendor duration to
every interval. Version 0.2.4 aligns both guards to command-start timing while
retaining the 100 ms minimum, so correctly paced 10 Hz traffic is accepted and
faster traffic was still rejected. At the user's direction, v0.2.5 removes that
broker timing rejection entirely: the widget still schedules animation at 10 Hz,
but harmless scheduler jitter or a simultaneous color-zone change cannot trigger
quarantine. Strict sequencing, one synchronous in-flight command, pending-value
coalescing, identity checks, and OEM-health checks remain enforced. A live
color-then-brightness regression finished active with both acknowledgments, then
stopped cleanly in OEM-only state with broker termination confirmed.

The former Minimum Brightness and Maximum Brightness controls were subsequently
restored to the hover panel. Both span levels 0–4, default to 1 and 4, preserve an
ordered range when crossed, and bound the same phase-synchronized 10 Hz animation;
they do not alter broker ownership or pacing. The focused suite then passed 88
tests, and the relaunched live widget retained its broker lease with `FnKey` and
both Clevo services healthy.
