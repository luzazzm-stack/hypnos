"""v0.9: every helper process memory starts (git, the writer models, the service itself) runs hidden.
On Windows a console program started from a windowless parent pops up its own console window; these flags stop that."""
from __future__ import annotations
import os
import shutil
import subprocess
import sys

NO_WINDOW = 0x08000000             # CREATE_NO_WINDOW
DETACHED = 0x00000008              # DETACHED_PROCESS
NEW_GROUP = 0x00000200             # CREATE_NEW_PROCESS_GROUP


def hidden(**kw):
    """subprocess keyword arguments that keep the child out of sight (no console window, nothing in the taskbar)."""
    if os.name == 'nt':
        kw['creationflags'] = kw.get('creationflags', 0) | NO_WINDOW
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0                                               # SW_HIDE
        kw.setdefault('startupinfo', si)
    return kw


def background(**kw):
    """Keyword arguments for a long-running background process that outlives the agent that started it."""
    if os.name == 'nt':
        kw = hidden(**kw)
        kw['creationflags'] = (kw['creationflags'] & ~NO_WINDOW) | DETACHED | NEW_GROUP
    else:
        kw['start_new_session'] = True
    return kw


def total_ram_gb() -> float:
    """Physical memory of this machine in GB (0 if unknown)."""
    try:
        if os.name == 'nt':
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [('len', ctypes.c_ulong), ('load', ctypes.c_ulong), ('total', ctypes.c_ulonglong),
                            ('avail', ctypes.c_ulonglong), ('tp', ctypes.c_ulonglong), ('ap', ctypes.c_ulonglong),
                            ('tv', ctypes.c_ulonglong), ('av', ctypes.c_ulonglong), ('ext', ctypes.c_ulonglong)]
            m = MS(); m.len = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.total / 2 ** 30
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 2 ** 30
    except Exception:
        return 0.0


def windowless_python() -> str:
    """pythonw.exe on Windows (a GUI-subsystem Python: no console window at all), else the current Python."""
    exe = sys.executable
    if os.name == 'nt':
        w = os.path.join(os.path.dirname(exe), 'pythonw.exe')
        if os.path.exists(w):
            return w
        w = shutil.which('pythonw')
        if w:
            return w
    return exe
