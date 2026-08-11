"""
TOPUP HUB Telegram Bot Supervisor (Windows watchdog)

Runs bot.py as a subprocess and keeps it alive 24/7:
  • Starts bot.py automatically
  • Auto-restarts it if it crashes or exits (with a short delay)
  • Logs every start / restart / exit to .freebuff/bot-supervisor.log
  • Stops gracefully on Ctrl+C (also terminates the bot child)

Registered as a Windows Scheduled Task ("TopupHubBot") so it starts
automatically when the user logs in / the PC starts, and keeps the
bot alive even if it dies.

Run manually:  python bot_supervisor.py   (backend/, venv active)
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
BOT_SCRIPT = os.path.join(BASE_DIR, 'bot.py')
LOG_FILE = os.path.join(os.path.dirname(BASE_DIR), '.freebuff', 'bot-supervisor.log')

RESTART_DELAY = 5   # seconds before restarting a crashed bot
TOKEN_BAD_DELAY = 60  # longer wait when the bot token is invalid (exit code 2)


def log(msg: str):
    """Print and append a timestamped line to the supervisor log."""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass  # logging must never crash the supervisor


LOCK_PORT = 18712  # kept for the watchdog's port check (optimization only)


class _SupervisorLock:
    """Single-instance lock via a Windows named mutex.

    The old TCP-port lock leaked zombie sockets: after a crash the port
    stayed bound and later instances either refused to start or — with
    leftover sockets — duplicates slipped through and double-polled
    Telegram (409 Conflict). A named mutex is released by the OS the
    moment the process dies, so no zombies are possible.
    """
    MUTEX_NAME = r'Local\DONZO_BotSupervisor_Lock'

    def __init__(self):
        self._handle = None

    def acquire(self):
        """Return True if this instance won the lock, False otherwise."""
        try:
            k32 = ctypes.windll.kernel32
            handle = k32.CreateMutexW(None, False, self.MUTEX_NAME)
            if not handle:
                return False
            if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                k32.CloseHandle(handle)
                return False
            self._handle = handle  # keep it open for the process lifetime
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


_lock = _SupervisorLock()


def _kill_stale_bots():
    """Terminate any orphaned bot.py processes from previous runs.

    Prevents two bots polling Telegram at once (409 Conflict).
    Uses wmic to find python processes whose command line contains bot.py
    but NOT bot_supervisor.py (so we never kill ourselves).
    """
    try:
        out = subprocess.run(
            ['wmic', 'process', 'where', "name='python.exe'",
             'get', 'ProcessId,CommandLine'],
            capture_output=True, text=True, timeout=10,
        ).stdout or ''
    except Exception:
        return  # wmic unavailable — skip cleanup

    killed = 0
    for line in out.splitlines():
        if 'bot.py' in line and 'bot_supervisor' not in line:
            match = re.search(r'(\d+)\s*$', line.strip())
            if not match:
                continue
            pid = match.group(1)
            try:
                subprocess.run(
                    ['taskkill', '/PID', pid, '/F'],
                    capture_output=True, timeout=10,
                )
                log(f"[SUP] Eski/orfand bot.py jarayoni o'chirildi (PID {pid})")
                killed += 1
            except Exception:
                pass
    return killed


def main():
    if not os.path.exists(BOT_SCRIPT):
        log(f"XATO: bot.py topilmadi: {BOT_SCRIPT}")
        sys.exit(1)
    if not os.path.exists(PYTHON):
        log(f"XATO: venv python topilmadi: {PYTHON}")
        sys.exit(1)

    log("=" * 60)
    log(f"Supervisor ishga tushdi. Python: {PYTHON}")
    log(f"Bot: {BOT_SCRIPT} | Restart delay: {RESTART_DELAY}s")

    if not _lock.acquire():
        log("[SUP] Boshqa supervisor allaqachon ishlamoqda — bu nusxa chiqadi "
            "(dublikat 409 Conflict keltirmasligi uchun).")
        sys.exit(0)

    proc = None
    try:
        while True:
            killed = _kill_stale_bots()
            if killed:
                log(f"[SUP] {killed} ta eski bot.py jarayoni tozalandi")
            log("[BOT] bot.py ni ishga tushirish...")
            proc = subprocess.Popen(
                [PYTHON, '-u', BOT_SCRIPT],
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
            )

            # Stream bot output into the supervisor log in real time
            for line in proc.stdout:
                log(f"[BOT] {line.rstrip()}")

            exit_code = proc.wait()
            if exit_code == 2:
                # Token noto'g'ri — foydalanuvchi admin panelda tuzatmaguncha
                # tez-tez restart qilishning ma'nosi yo'q.
                log(f"[BOT] bot.py chiqdi (code {exit_code} = token xato). "
                    f"Token tuzatilishini kutamiz ({TOKEN_BAD_DELAY}s)...")
                time.sleep(TOKEN_BAD_DELAY)
            else:
                log(f"[BOT] bot.py chiqdi (code {exit_code}). "
                    f"{RESTART_DELAY}s dan keyin qayta ishga tushiriladi...")
                time.sleep(RESTART_DELAY)
    except KeyboardInterrupt:
        log("Supervisor to'xtatildi (Ctrl+C).")
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        _lock.close()
        sys.exit(0)


if __name__ == '__main__':
    main()
