"""Durable phone ping when the MEDIK ETF bot places or exits a trade.

Tails the bot's log and POSTs to the owner's ntfy topic on real order events —
independent of any Claude session, so it fires even with nothing watching.
Read-only: it never touches the bot, its config, or any order path. Stdlib only.

Started at logon via Startup\\MedikTradeNotifier.vbs; stops on STOP_MEDIK.
"""
from __future__ import annotations

import glob
import os
import re
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "logs"
STOP_FILE = REPO_ROOT / "STOP_MEDIK"
LOCK_FILE = LOG_DIR / "medik_trade_notifier.lock"
NTFY_URL = os.environ.get("MEDIK_NTFY_URL", "https://ntfy.sh/alca-kraken-f07c84d02fd2a45f")

# Real order events worth a ping — not routine "no setup" scans.
PAT = re.compile(r"(submitted:\s*parent=|BRACKET FAILURE|parent unfilled|"
                 r"flatten status|Emergency exit|EXIT [A-Z]|ORDER PLACED)", re.I)


def _pid_alive(pid: int) -> bool:
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def _singleton() -> bool:
    try:
        LOG_DIR.mkdir(exist_ok=True)
        if LOCK_FILE.exists():
            old = LOCK_FILE.read_text(encoding="utf-8").strip()
            if old.isdigit() and int(old) != os.getpid() and _pid_alive(int(old)):
                return False
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return True


def _newest_log():
    fs = glob.glob(str(LOG_DIR / "medik_etf*.log"))
    return max(fs, key=os.path.getmtime) if fs else None


def _push(line: str) -> None:
    try:
        req = urllib.request.Request(
            NTFY_URL, data=line.encode("utf-8"), method="POST",
            headers={"Title": "MEDIK: trade", "Priority": "high", "Tags": "moneybag"})
        urllib.request.urlopen(req, timeout=6).read()
    except Exception:
        pass


def main() -> int:
    if not _singleton():
        return 0
    cur = _newest_log()
    pos = os.path.getsize(cur) if cur else 0     # start at end: only NEW events
    while True:
        if STOP_FILE.exists():
            return 0
        try:
            f = _newest_log()
            if f != cur:
                cur, pos = f, 0
            if cur:
                sz = os.path.getsize(cur)
                if sz < pos:
                    pos = 0
                if sz > pos:
                    with open(cur, "r", errors="replace") as fh:
                        fh.seek(pos)
                        chunk = fh.read()
                        pos = fh.tell()
                    for line in chunk.splitlines():
                        if PAT.search(line):
                            _push(line.strip()[:300])
        except Exception:
            pass
        time.sleep(8)


if __name__ == "__main__":
    raise SystemExit(main())
