"""Disabled historical pythonnet experiment; retained for forensic context."""

import sys


_SAFETY_WARNING = """
SAFETY STOP: scratch/test_pythonnet.py is disabled historical research.

The preserved experiment below kills Clevo FnKey and imports its vendor
KBModule. That unsafe takeover can interfere with CPU performance profiles.
No supported, lighting-only, no-takeover interface has been qualified.

Nothing was changed: no Clevo process was stopped and no hardware call was made.
""".strip()

print(_SAFETY_WARNING, file=sys.stderr)
raise SystemExit(2)


# Historical implementation below. Intentionally unreachable; do not move it
# above the safety stop or run it against a Clevo installation.
import os
import subprocess
import clr

print("Killing FnKey...")
subprocess.run(["powershell", "-Command", 'Stop-Process -Name "FnKey" -Force -ErrorAction SilentlyContinue'])

old_cwd = os.getcwd()
os.chdir(r"C:\Program Files (x86)\ControlCenter")

print("Adding reference to FnKey.exe...")
clr.AddReference(r"C:\Program Files\WindowsApps\CLEVOCO.FnhotkeysandOSD_2.6.1.0_x64__6h6z29zh29qx0\FnKey\FnKey.exe")
clr.AddReference("System.Drawing")

import FnKey
from System.Drawing import Color

print("Instantiating KBModule...")
inst = FnKey.KBModule()
print("Init...")
inst.Init()

print("Setting Color to Cyan...")
# Send Magenta to physical, which displays Cyan due to swap
inst.SetRGBAllColor(Color.FromArgb(255, 255, 0, 255))
inst.SetBrightnessLevel(4)

print("Applying...")
inst.SetMode()
inst.SetStatus()
inst.UpdateCurrentStatus()

os.chdir(old_cwd)
print("Success!")
