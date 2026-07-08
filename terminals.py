# terminals.py - "everything that's running" so Vaishak never has to guess where to look.
# Uses ctypes + the Win32 Toolhelp32 API directly (CreateToolhelp32Snapshot/Process32Next) -
# NOT subprocess. Two separate bugs tonight (codex.cmd invisible to os.path.isfile, and a
# tasklist.exe subprocess call that hung indefinitely) both trace to this dashboard's own
# spawn chain (wscript.exe -> WshShell.Run -> python.exe) misbehaving for grandchild process
# spawns/pipes. Direct API calls avoid spawning a process at all, sidestepping both.
import ctypes
from ctypes import wintypes

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260),
    ]

WATCH = {
    "claude.exe": "Claude Code",
    "codex.exe": "Codex",
    "agy.exe": "Antigravity",
    "ollama.exe": "Ollama (Gemma)",
    "python.exe": "Python (server/runner/scripts)",
    "cmd.exe": "Terminal (cmd)",
    "powershell.exe": "Terminal (PowerShell)",
    "wscript.exe": "Boot launcher (wscript)",
}

def running():
    kernel32 = ctypes.windll.kernel32
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return []
    out = []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        found = kernel32.Process32First(snap, ctypes.byref(entry))
        while found:
            try:
                name = entry.szExeFile.decode("mbcs", errors="replace")
            except Exception:
                name = ""
            if name.lower() in WATCH:
                out.append({"process": name, "label": WATCH[name.lower()],
                           "pid": entry.th32ProcessID, "title": "", "mem": ""})
            found = kernel32.Process32Next(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out
