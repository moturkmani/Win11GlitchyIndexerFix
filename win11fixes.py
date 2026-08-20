"""
Windows Search/Index Fixer
=====================

A small Tkinter control panel that automates the "fix slow/broken Windows
search" steps from the top of glitchy_indexing.docx:

  1. Restart Explorer & Search   -> kills SearchHost.exe and restarts explorer.exe
  2. Turn Off Cloud Search       -> disables the Microsoft/Work-School account
                                     toggles under Settings > Privacy & security
                                     > Search > Search my accounts
  3. Rebuild Search Index        -> stops the Windows Search service, deletes
                                     the index database, restarts the service
  4. Change "Find my files" to
     Classic                    -> switches Enhanced -> Classic indexing mode
  5. Repair System Files         -> opens an elevated console and runs
                                     SFC /SCANNOW, then DISM /RestoreHealth

Requirements
------------
* Windows 10 (2004+) or Windows 11.
* Standard Python 3 with Tkinter (comes with the normal python.org installer;
  no pip installs needed - everything used here is in the standard library).
* Some actions need to run as Administrator (rebuilding the index, changing
  Enhanced/Classic mode, and the SFC/DISM repair). Use the "Relaunch as
  Administrator" button at the top, or right-click the script/shortcut and
  choose "Run as administrator".

IMPORTANT - read before using
------------------------------
* This edits your registry and stops/restarts a Windows service. It's built
  from publicly documented, standard techniques, but nobody has been able to
  test it on an actual Windows 11 machine before handing it to you (this was
  written from a non-Windows environment). Skim each action once before you
  click it, and consider creating a System Restore point first
  (Start -> "Create a restore point"), especially before using "Change to
  Classic" (it modifies registry key permissions) or "Repair System Files".
* The "Change to Classic" registry key is the least officially documented of
  the bunch. Windows doesn't expose a supported one-click switch for it, so
  this uses the same permission-ownership workaround that's commonly posted
  in Windows help forums. If it doesn't visibly change the toggle in
  Settings, open Settings > Privacy & security > Search yourself and flip it
  by hand instead.
* Nothing here is hidden: every command that runs is printed to the log pane
  at the bottom of the window before it runs, so you can always see exactly
  what's happening.
"""

import ctypes
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import winreg
except ImportError:
    winreg = None  # only exists on Windows; lets the file at least import elsewhere


IS_WINDOWS = sys.platform.startswith("win")
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010


# --------------------------------------------------------------------------
# Elevation helpers
# --------------------------------------------------------------------------

def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(root: tk.Tk):
    """Re-launch this same script elevated, then close this (non-elevated) instance."""
    try:
        script = os.path.abspath(__file__)
        params = f'"{script}"'
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )
        # ShellExecuteW returns a value <= 32 on failure
        if int(rc) <= 32:
            raise OSError(f"ShellExecuteW failed with code {rc}")
    except Exception as exc:
        messagebox.showerror(
            "Couldn't elevate",
            "Windows didn't let this relaunch as Administrator "
            f"(you may have clicked 'No' on the UAC prompt).\n\n{exc}",
        )
        return

    # The elevated copy is now starting up in a new process - close this one
    # so there isn't a duplicate, non-admin window left behind.
    root.destroy()
    sys.exit(0)


# --------------------------------------------------------------------------
# Individual fix actions. Each is a generator-ish function that yields log
# lines, so the GUI thread can stream progress to the log box.
# --------------------------------------------------------------------------

def run_cmd(log, args, **kwargs):
    """Run a command, streaming stdout/stderr lines into `log`."""
    log(f"$ {' '.join(args)}")
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
            **kwargs,
        )
    except FileNotFoundError as exc:
        log(f"  ! command not found: {exc}")
        return False
    if proc.stdout.strip():
        for line in proc.stdout.strip().splitlines():
            log(f"  {line}")
    if proc.stderr.strip():
        for line in proc.stderr.strip().splitlines():
            log(f"  {line}")
    ok = proc.returncode == 0
    log(f"  -> exit code {proc.returncode}")
    return ok


def action_restart_explorer_search(log):
    log("Killing SearchHost.exe (Windows will spawn a fresh copy on demand)...")
    run_cmd(log, ["taskkill", "/f", "/im", "SearchHost.exe"])

    log("Restarting Windows Explorer (taskbar/desktop will flicker for a second)...")
    run_cmd(log, ["taskkill", "/f", "/im", "explorer.exe"])
    try:
        subprocess.Popen(["explorer.exe"])
        log("  explorer.exe relaunched.")
    except Exception as exc:
        log(f"  ! failed to relaunch explorer.exe: {exc}")

    log("Done. Give it a few seconds for the taskbar/search box to come back.")


def action_turn_off_cloud_search(log):
    if winreg is None:
        log("winreg isn't available on this OS - skipping.")
        return
    key_path = r"Software\Microsoft\Windows\CurrentVersion\SearchSettings"
    log(rf"Writing HKEY_CURRENT_USER\{key_path}")
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "IsMSACloudSearchEnabled", 0, winreg.REG_DWORD, 0)
        winreg.SetValueEx(key, "IsAADCloudSearchEnabled", 0, winreg.REG_DWORD, 0)
        winreg.CloseKey(key)
        log("  IsMSACloudSearchEnabled = 0 (Microsoft account cloud search off)")
        log("  IsAADCloudSearchEnabled = 0 (Work/School account cloud search off)")
        log("Done. Open Settings > Privacy & security > Search to confirm both")
        log("toggles under 'Search my accounts' now show Off.")
    except Exception as exc:
        log(f"  ! registry write failed: {exc}")


def action_rebuild_index(log):
    if not is_admin():
        log("! This needs Administrator rights. Use 'Relaunch as Administrator' first.")
        return

    log("Stopping the Windows Search service (wsearch)...")
    run_cmd(log, ["net", "stop", "wsearch"])

    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    for fname in ("Windows.edb", "Windows.db"):
        path = os.path.join(program_data, "Microsoft", "Search", "Data", "Applications", "Windows", fname)
        if os.path.exists(path):
            log(f"Deleting old index database: {path}")
            try:
                os.remove(path)
                log("  deleted.")
            except Exception as exc:
                log(f"  ! couldn't delete it: {exc}")
        else:
            log(f"  ({fname} not found, nothing to delete)")

    log("Starting the Windows Search service again...")
    run_cmd(log, ["net", "start", "wsearch"])

    log("Done. Windows will rebuild the index in the background - this can")
    log("take anywhere from a few minutes to a few hours depending on how")
    log("much you have indexed. Search results will be incomplete meanwhile.")


def action_change_to_classic(log):
    if not is_admin():
        log("! This needs Administrator rights. Use 'Relaunch as Administrator' first.")
        return

    log("Taking ownership of the SystemIndex registry key and setting")
    log("EnableFindMyFiles = 0 (Classic mode)...")

    ps_script = (
        "$ErrorActionPreference='Stop';"
        "$p='HKLM:\\SOFTWARE\\Microsoft\\Windows Search\\Gather\\Windows\\SystemIndex';"
        "$acl = Get-Acl $p;"
        "$admins = New-Object System.Security.Principal.NTAccount('BUILTIN','Administrators');"
        "$acl.SetOwner($admins);"
        "Set-Acl -Path $p -AclObject $acl;"
        "$acl = Get-Acl $p;"
        "$rule = New-Object System.Security.AccessControl.RegistryAccessRule("
        "'Administrators','FullControl','ContainerInherit','None','Allow');"
        "$acl.SetAccessRule($rule);"
        "Set-Acl -Path $p -AclObject $acl;"
        "New-ItemProperty -Path $p -Name 'EnableFindMyFiles' -PropertyType DWord "
        "-Value 0 -Force | Out-Null;"
        "Write-Output 'OK'"
    )
    ok = run_cmd(
        log,
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
    )

    if ok:
        log("Registry value set. Restarting Windows Search service so it")
        log("picks up the change...")
        run_cmd(log, ["net", "stop", "wsearch"])
        run_cmd(log, ["net", "start", "wsearch"])
        log("Done. Open Settings > Privacy & security > Search and check that")
        log("'Find my files' now shows Classic. If it still shows Enhanced,")
        log("switch it by hand there - this specific key isn't officially")
        log("documented by Microsoft.")
    else:
        log("The PowerShell step reported an error - the registry key may be")
        log("locked down differently on your build. Open Settings > Privacy")
        log("& security > Search and switch Enhanced -> Classic by hand instead.")


def action_repair_system_files(log):
    if not is_admin():
        log("! This needs Administrator rights. Use 'Relaunch as Administrator' first.")
        return

    log("Opening an elevated console to run SFC /SCANNOW, then")
    log("DISM /Online /Cleanup-Image /RestoreHealth once SFC finishes.")
    log("This can take 15-30+ minutes total - a separate window will open")
    log("and stay open so you can watch its progress; this app is not frozen.")

    cmd_line = (
        'title Windows Index/Search Fixer - System Repair '
        '&& echo Running SFC /SCANNOW ... '
        '&& sfc /scannow '
        '&& echo. && echo SFC finished. Starting DISM RestoreHealth ... '
        '&& DISM /Online /Cleanup-Image /RestoreHealth '
        '&& echo. && echo All done. You can close this window (a restart is recommended).'
    )
    try:
        subprocess.Popen(
            ["cmd.exe", "/k", cmd_line],
            creationflags=CREATE_NEW_CONSOLE,
        )
        log("Console window launched.")
    except Exception as exc:
        log(f"  ! failed to launch console: {exc}")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Windows Index/Search Fixer")
        self.minsize(640, 480)

        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._busy = set()

        self._build_ui()
        self._poll_log_queue()
        self._center_on_screen(720, 560)

        if not IS_WINDOWS:
            self._log("This machine isn't Windows - the buttons below will mostly no-op.")
        if IS_WINDOWS and not is_admin():
            self._log("Not running as Administrator. 'Rebuild Search Index', "
                       "'Change to Classic', and 'Repair System Files' need admin - "
                       "use the button in the top-right first.")

    # -- UI construction ---------------------------------------------------

    def _center_on_screen(self, width: int, height: int):
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build_ui(self):
        style = ttk.Style(self)
        style.configure("Bold.TButton", font=("Segoe UI", 10, "bold"))
        style.configure("BoldSmall.TButton", font=("Segoe UI", 9, "bold"))

        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        admin_text = "Running as Administrator" if is_admin() else "NOT running as Administrator"
        self.admin_label = ttk.Label(top, text=admin_text, font=("Segoe UI", 10, "bold"))
        self.admin_label.pack(side="left")

        if not is_admin():
            ttk.Button(
                top, text="Relaunch as Administrator", style="Bold.TButton",
                command=lambda: relaunch_as_admin(self),
            ).pack(side="right")

        actions = ttk.Frame(self, padding=(10, 0, 10, 10))
        actions.pack(fill="x")

        # (label, description, action function, needs admin, confirmation text)
        buttons = [
            ("1. Restart Explorer & Search",
             "Kills SearchHost.exe and restarts explorer.exe",
             action_restart_explorer_search, False,
             "This will kill SearchHost.exe and restart explorer.exe.\n"
             "Your taskbar and desktop icons will disappear for a second or two."),
            ("2. Turn Off Cloud Search",
             "Disables the Microsoft/Work-School account search toggles",
             action_turn_off_cloud_search, False,
             "This will turn off the 'Microsoft account' and 'Work or School "
             "account' toggles under Search my accounts, so Windows Search "
             "stops including your cloud/OneDrive content."),
            ("3. Rebuild Search Index",
             "Stops Windows Search, deletes the index DB, restarts it",
             action_rebuild_index, True,
             "This will stop the Windows Search service, permanently delete "
             "the current search index database, and restart the service so "
             "Windows rebuilds it from scratch.\n\n"
             "Search results will be incomplete until the rebuild finishes, "
             "which can take a while."),
            ("4. Change to Classic",
             "Switches 'Find my files' from Enhanced to Classic",
             action_change_to_classic, True,
             "This will change registry key ownership/permissions on a "
             "system-protected key and switch 'Find my files' from Enhanced "
             "to Classic mode (indexing only Documents, Pictures, Desktop, "
             "etc. instead of your whole PC)."),
            ("5. Repair System Files",
             "Opens an elevated console and runs SFC then DISM",
             action_repair_system_files, True,
             "This will open a new elevated console window and run "
             "SFC /SCANNOW, then DISM /Online /Cleanup-Image /RestoreHealth.\n\n"
             "This can take 15-30+ minutes and a restart is recommended "
             "afterward."),
        ]

        for i, (label, desc, func, needs_admin, confirm_text) in enumerate(buttons):
            row = ttk.Frame(actions)
            row.pack(fill="x", pady=4)
            btn = ttk.Button(
                row, text=label, width=28, style="Bold.TButton",
                command=lambda f=func, l=label, c=confirm_text: self._run_action(f, l, c),
            )
            btn.pack(side="left")
            suffix = "  (admin)" if needs_admin else ""
            ttk.Label(row, text=desc + suffix, foreground="#555").pack(side="left", padx=8)

        log_frame = ttk.LabelFrame(self, text="Log", padding=6)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.log_box = tk.Text(
            log_frame, wrap="word", state="disabled", font=("Consolas", 9),
            bg="#0b3d91", fg="white", insertbackground="white",
        )
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scrollbar.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    # -- Action running ------------------------------------------------

    def _run_action(self, func, label, confirm_text=None):
        if label in self._busy:
            return

        if confirm_text:
            proceed = messagebox.askokcancel(
                f"Confirm: {label}",
                f"{confirm_text}\n\nDo you want to continue?",
                icon="warning",
            )
            if not proceed:
                self._log(f"\n=== {label} === cancelled by user, nothing was done.")
                return

        self._busy.add(label)
        self._log(f"\n=== {label} ===")

        def worker():
            try:
                func(self._log)
            except Exception as exc:
                self._log(f"  ! unexpected error: {exc}")
            finally:
                self._busy.discard(label)

        threading.Thread(target=worker, daemon=True).start()

    # -- Logging (thread-safe via queue) --------------------------------

    def _log(self, msg: str):
        self._log_queue.put(msg)

    def _poll_log_queue(self):
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", msg + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)


if __name__ == "__main__":
    app = App()
    app.mainloop()