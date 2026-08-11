"""
DONZO User Client Supervisor (Windows watchdog)

Keeps user_client.py alive 24/7:
  • Starts user_client.py automatically
  • Auto-restarts it if it crashes or exits
  • Logs to .freebuff/user-client-supervisor.log
  • Single-instance lock via a loopback TCP port (like the bot supervisor)

Register as a Windows Scheduled Task to start at logon (see start_all.ps1
/ README) — then the card payment verification runs forever.

Run manually:  python user_client_supervisor.py   (backend/, venv active)
"""

import ctypes
import os
import re
import subprocess
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(BASE_DIR, 'venv', 'Scripts', 'python.exe')
CLIENT_SCRIPT = os.path.join(BASE_DIR, 'user_client.py')
LOG_FILE = os.path.join(os.path.dirname(BASE_DIR), '.freebuff', 'user-client-supervisor.log')

RESTART_DELAY = 5
CRED_BAD_DELAY = 60   # credentials missing → wait longer before retry

LOCK_PORT = 18713  # separate from the bot supervisor's 18712


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        # Windows pipes default to the ANSI codepage (cp1252) which cannot
        # encode non-Latin-1 characters (emoji, arrows) and kills the log.
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


class _Lock:
    """Single-instance lock via a Windows named mutex.

    The old TCP-port lock leaked zombie sockets after a crash: the port
    stayed bound and later instances either refused to start or, with
    leftover sockets, duplicates slipped through and double-processed
    card payments. A named mutex is released by the OS the moment the
    process dies — no zombies, duplicates impossible.
    """
    MUTEX_NAME = r'Local\DONZO_UserClientSupervisor_Lock'

    def __init__(self):
        self._handle = None

    def acquire(self):
        try:
            k32 = ctypes.windll.kernel32
            handle = k32.CreateMutexW(None, False, self.MUTEX_NAME)
            if not handle:
                return False
            if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                k32.CloseHandle(handle)
                return False
            self._handle = handle
            return True
        except Exception:
            return False

    def close(self):
        if self._handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None


def _kill_stale():
    """Terminate orphaned user_client.py processes (avoid duplicate logins)."""
    try:
        out = subprocess.run(
            ['wmic', 'process', 'where', "name='python.exe'",
             'get', 'ProcessId,CommandLine'],
            capture_output=True, text=True, timeout=10,
        ).stdout or ''
    except Exception:
        return 0
    killed = 0
    for line in out.splitlines():
        if 'user_client.py' in line and 'supervisor' not in line:
            match = re.search(r'(\d+)\s*$', line.strip())
            if not match:
                continue
            pid = match.group(1)
            try:
                subprocess.run(['taskkill', '/PID', pid, '/F'],
                               capture_output=True, timeout=10)
                log(f"[SUP] Eski user_client.py jarayoni o'chirildi (PID {pid})")
                killed += 1
            except Exception:
                pass
    return killed


def main():
    if not os.path.exists(CLIENT_SCRIPT):
        log(f"XATO: user_client.py topilmadi: {CLIENT_SCRIPT}")
        sys.exit(1)

    log("=" * 60)
    log("User Client supervisor ishga tushdi.")
    lock = _Lock()
    if not lock.acquire():
        # Another supervisor already holds the lock port — duplicate getUpdates
        # would double-process payments. Exit quietly (the task will restart us
        # later if the winner dies).
        log("[SUP] Boshqa supervisor allaqachon ishlamoqda — bu nusxa chiqadi.")
        sys.exit(0)

    proc = None
    try:
        while True:
            killed = _kill_stale()
            if killed:
                log(f"[SUP] {killed} ta eski user_client.py tozalandi")
            log("[UC] user_client.py ni ishga tushirish...")
            env = dict(os.environ)
            env['PYTHONIOENCODING'] = 'utf-8'
            env['PYTHONUTF8'] = '1'
            proc = subprocess.Popen(
                [PYTHON, '-u', CLIENT_SCRIPT],
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                env=env,
            )
            for line in proc.stdout:
                log(f"[UC] {line.rstrip()}")
            exit_code = proc.wait()
            if exit_code in (3, 4):
                # Credentials missing / not authorized — user must act.
                log(f"[UC] user_client.py chiqdi (code {exit_code}). "
                    f"Sozlamalarni tuzatish kutiladi ({CRED_BAD_DELAY}s)...")
                time.sleep(CRED_BAD_DELAY)
            else:
                log(f"[UC] user_client.py chiqdi (code {exit_code}). "
                    f"{RESTART_DELAY}s dan keyin qayta ishga tushiriladi...")
                time.sleep(RESTART_DELAY)
    except KeyboardInterrupt:
        log("Supervisor to'xtatildi (Ctrl+C).")
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        sys.exit(0)


if __name__ == '__main__':
    main()
