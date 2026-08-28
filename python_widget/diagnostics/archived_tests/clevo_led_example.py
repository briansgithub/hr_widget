"""Disabled historical Clevo LED experiment.

This file is retained only to document the original reverse-engineering work.
It must not take ownership away from Clevo Control Center/FnKey or issue vendor
hardware commands.
"""

import sys


_SAFETY_WARNING = """
SAFETY STOP: clevo_led_example.py is disabled historical research.

The legacy experiment below force-terminates Clevo FnKey processes and imports
the OEM KBModule. That takeover can disrupt Clevo CPU performance-profile
control and may leave retained platform state that survives an ordinary reboot.
No supported, lighting-only, no-takeover interface has been qualified.

Nothing was changed: no Clevo process was stopped and no hardware call was made.
""".strip()

print(_SAFETY_WARNING, file=sys.stderr)
raise SystemExit(2)


# Historical implementation below. Intentionally unreachable; do not move it
# above the safety stop or use it as an executable example.
import subprocess
import time

def set_clevo_keyboard_color(r: int, g: int, b: int, brightness: int = 4):
    """
    Sets the Clevo keyboard LED color and brightness using the native .NET KBModule API.
    
    WARNING (Hardware Pin Swap): 
    On the PowerSpec 1520 (and some PB50/PB51 chassis), the Red and Green hardware pins 
    are swapped at the matrix controller level. 
    This function handles the swap in software: to get Red, it sends Green; to get Green, it sends Red.
    """
    
    # Apply the hardware pin swap fix
    swapped_r = g
    swapped_g = r
    swapped_b = b

    ps_script = f"""
    # 1. Kill Clevo background processes that aggressively override custom lighting
    Stop-Process -Name "FnKey" -Force -ErrorAction SilentlyContinue
    Stop-Process -Name "LedKeyboardSetting" -Force -ErrorAction SilentlyContinue
    
    # 2. Set environment to load the DLL properly
    [System.Environment]::CurrentDirectory = 'C:\\Program Files (x86)\\ControlCenter'
    $env:PATH = 'C:\\Program Files (x86)\\ControlCenter;' + $env:PATH

    # 3. Load the FnKey.exe .NET assembly
    $asmPath = 'C:\\Program Files\\WindowsApps\\CLEVOCO.FnhotkeysandOSD_2.6.1.0_x64__6h6z29zh29qx0\\FnKey\\FnKey.exe'
    $asm = [Reflection.Assembly]::LoadFile($asmPath)
    Add-Type -AssemblyName System.Drawing

    # 4. Instantiate KBModule and initialize WMI
    $type = $asm.GetType('FnKey.KBModule')
    $flags = [System.Reflection.BindingFlags]'NonPublic,Public,Instance,Static'
    $inst = [Activator]::CreateInstance($type)
    
    $m_init = $type.GetMethod('Init', $flags)
    if ($m_init) {{ $m_init.Invoke($inst, $null) }}

    # 5. Set Color and Brightness (in memory WMI)
    $color = [System.Drawing.Color]::FromArgb(255, {swapped_r}, {swapped_g}, {swapped_b})
    $m_all = $type.GetMethod('SetRGBAllColor', $flags)
    if ($m_all) {{ $m_all.Invoke($inst, @($color)) }}
    
    $m_bright = $type.GetMethod('SetBrightnessLevel', $flags)
    if ($m_bright) {{ $m_bright.Invoke($inst, @([int]{brightness})) }}
    
    # 6. FORCE HARDWARE APPLY
    $m_set_mode = $type.GetMethod('SetMode', $flags)
    $m_set_status = $type.GetMethod('SetStatus', $flags)
    $m_update_status = $type.GetMethod('UpdateCurrentStatus', $flags)
    
    if ($m_set_mode) {{ $m_set_mode.Invoke($inst, $null) }}
    if ($m_set_status) {{ $m_set_status.Invoke($inst, $null) }}
    if ($m_update_status) {{ $m_update_status.Invoke($inst, $null) }}
    """
    
    subprocess.run(["powershell", "-Command", ps_script], 
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

if __name__ == "__main__":
    print("Testing concise Clevo LED hook...")
    print("Setting physical RED (sending Green)...")
    set_clevo_keyboard_color(255, 0, 0)
    time.sleep(2)
    
    print("Setting physical GREEN (sending Red)...")
    set_clevo_keyboard_color(0, 255, 0)
    time.sleep(2)
    
    print("Setting physical CYAN (sending Magenta)...")
    set_clevo_keyboard_color(0, 255, 255)
    time.sleep(2)
    
    print("Done!")
