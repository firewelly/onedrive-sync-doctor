#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for inspecting OneDrive's local sync state. No third-party
dependencies. Everything is discovered from the system, so no paths or account
names are hardcoded.

Two very different layouts are supported:

    macOS (standalone build)
    ------------------------
    ~/Library/Group Containers/UBF8T346G9.OneDriveStandaloneSuite/
        .Dbfs.Dbfs_<Account>.noindex/dbfs.db       <- metadata database
        FileProviderLogs/                           <- extension logs
    ~/Library/Application Support/OneDrive/settings/<Account>/*.ini
                                                    <- records the mount path
    The mount point lives under ~/Library/CloudStorage/OneDrive-<something>;
    its exact name depends on the account, so we read it from the .ini above.

    Windows 10/11
    -------------
    %LOCALAPPDATA%\\Microsoft\\OneDrive\\settings\\<Account>\\SyncEngineDatabase.db
                                                    <- sync engine database
    %LOCALAPPDATA%\\Microsoft\\OneDrive\\logs\\<Account>\\SyncDiagnostics.log
                                                    <- plain-text counters
    HKCU\\Software\\Microsoft\\OneDrive\\Accounts\\<Account>\\UserFolder
                                                    <- the sync root

Both platforms track the same stuck work in
``od_Client{File,Folder}PostponedChange_Records``. What differs is where the
"not synced" number comes from: macOS keeps it in ``dbfs_mftrecord.ph_insync``,
Windows only in ``SyncDiagnostics.log`` (``ChangesToProcess``), because the
engine database has no equivalent flag.
"""
from __future__ import annotations

import glob
import os
import re
import sqlite3
import subprocess

IS_WINDOWS = os.name == "nt"
IS_MACOS = not IS_WINDOWS

# ---------------------------------------------------------------------------
# Platform paths
# ---------------------------------------------------------------------------

GROUP_CONTAINER = os.path.expanduser(
    "~/Library/Group Containers/UBF8T346G9.OneDriveStandaloneSuite")
MAC_SETTINGS_DIR = os.path.expanduser(
    "~/Library/Application Support/OneDrive/settings")

# Env override exists so the tools can be pointed at a fixture during testing.
ONEDRIVE_ROOT = os.environ.get("OD_ONEDRIVE_ROOT") or os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "Microsoft", "OneDrive")
WINDOWS_SETTINGS_DIR = os.environ.get("OD_SETTINGS_DIR") or os.path.join(
    ONEDRIVE_ROOT, "settings")
WINDOWS_LOGS_DIR = os.path.join(ONEDRIVE_ROOT, "logs")

SETTINGS_DIR = WINDOWS_SETTINGS_DIR if IS_WINDOWS else MAC_SETTINGS_DIR

ONEDRIVE_EXE = os.path.join(
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    "Microsoft OneDrive", "OneDrive.exe")

# OneDrive stores item names as "<prefix><display name>" for some entries.
NAME_PREFIX = "//v0/<34>//"

# Special system records that are exempt from normal handling.
ROOT_ITEMID = 1
TEMP_ITEMID = 9223372036854775708


def is_windows() -> bool:
    return IS_WINDOWS


def leaf(name: str) -> str:
    """Strip OneDrive's internal name prefix."""
    return name[len(NAME_PREFIX):] if name and name.startswith(NAME_PREFIX) else name


# ---------------------------------------------------------------------------
# Account / database discovery
# ---------------------------------------------------------------------------

def _win_registry_accounts() -> list[tuple[str, str, str]]:
    """(name, sync root, email) for every account in the OneDrive registry key."""
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except ImportError:
        return []
    out = []
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\OneDrive\Accounts")
    except OSError:
        return []
    with key:
        for i in range(winreg.QueryInfoKey(key)[0]):
            name = winreg.EnumKey(key, i)
            try:
                with winreg.OpenKey(key, name) as sub:
                    root = winreg.QueryValueEx(sub, "UserFolder")[0]
                    try:
                        email = winreg.QueryValueEx(sub, "UserEmail")[0]
                    except OSError:
                        email = ""
            except OSError:
                continue
            out.append((name, root, email))
    return out


def accounts() -> list[str]:
    """Namespace names for every local OneDrive account, e.g. ['Business1', 'Personal']."""
    if IS_WINDOWS:
        found = [name for name, _root, _email in _win_registry_accounts()
                 if os.path.isdir(os.path.join(SETTINGS_DIR, name))]
        if found:
            return sorted(found)
        # Registry unavailable (rare): fall back to the engine databases.
        return sorted(
            os.path.basename(os.path.dirname(p))
            for p in glob.glob(os.path.join(SETTINGS_DIR, "*",
                                            "SyncEngineDatabase.db")))
    out = []
    for path in glob.glob(os.path.join(GROUP_CONTAINER, ".Dbfs.Dbfs_*.noindex")):
        base = os.path.basename(path)
        out.append(base[len(".Dbfs.Dbfs_"):-len(".noindex")])
    return sorted(out)


def db_path(account: str) -> str:
    if IS_WINDOWS:
        return os.path.join(SETTINGS_DIR, account, "SyncEngineDatabase.db")
    return os.path.join(GROUP_CONTAINER, f".Dbfs.Dbfs_{account}.noindex", "dbfs.db")


def logs_path(account: str) -> str:
    if IS_WINDOWS:
        return os.path.join(WINDOWS_LOGS_DIR, account)
    return os.path.expanduser(f"~/Library/Logs/OneDrive/{account}")


def mount_path(account: str) -> str | None:
    """Sync root for an account."""
    if IS_WINDOWS:
        for name, root, _email in _win_registry_accounts():
            if name == account:
                return root
        # Fallback: the per-account .ini is UTF-16 and contains "D:\OneDrive".
        for ini in glob.glob(os.path.join(SETTINGS_DIR, account, "*.ini")):
            try:
                with open(ini, "rb") as fh:
                    text = fh.read().decode("utf-16-le", errors="ignore")
            except OSError:
                continue
            hits = re.findall(r'"([A-Za-z]:\\[^"]+)"', text)
            for hit in hits:
                if os.path.isdir(hit):
                    return hit
        return None

    candidates = set()
    for ini in glob.glob(os.path.join(SETTINGS_DIR, account, "*.ini")):
        try:
            with open(ini, errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for raw in re.findall(r'"(~/Library/CloudStorage/[^"]+)"', text):
            candidates.add(os.path.expanduser(raw))
    for cand in sorted(candidates):
        if os.path.isdir(cand):
            return cand
    return None


# ---------------------------------------------------------------------------
# Client version / processes
# ---------------------------------------------------------------------------

def _win_registry_version() -> str | None:
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\OneDrive") as key:
            return str(winreg.QueryValueEx(key, "Version")[0])
    except (ImportError, OSError):
        return None


def client_version() -> str | None:
    if IS_WINDOWS:
        version = _win_registry_version()
        if version:
            return version
        if os.path.exists(ONEDRIVE_EXE):
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Item '{ONEDRIVE_EXE}').VersionInfo.FileVersion"],
                capture_output=True, text=True, timeout=60)
            return r.stdout.strip() or None
        return None

    app = "/Applications/OneDrive.app"
    plist = os.path.join(app, "Contents", "Info.plist")
    if not os.path.exists(plist):
        return None
    r = subprocess.run(["/usr/bin/plutil", "-extract",
                        "CFBundleShortVersionString", "raw", plist],
                       capture_output=True, text=True, timeout=60)
    return r.stdout.strip() or None


def processes() -> list[str]:
    """One line per running OneDrive process."""
    if IS_WINDOWS:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq OneDrive.exe",
                            "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=60)
        return [ln.strip() for ln in r.stdout.splitlines()
                if "OneDrive.exe" in ln]
    r = subprocess.run(["/bin/ps", "-axo", "pid,pcpu,etime,comm"],
                       capture_output=True, text=True, timeout=60)
    return [ln.strip() for ln in r.stdout.splitlines() if "OneDrive" in ln]


def stop_command() -> str:
    if IS_WINDOWS:
        return f'"{ONEDRIVE_EXE}" /shutdown'
    return "osascript -e 'tell application \"OneDrive\" to quit' && " \
           "pkill -f 'OneDrive File Provider'"


def start_command() -> str:
    if IS_WINDOWS:
        return f'Start-Process "{ONEDRIVE_EXE}"'
    return 'open -a OneDrive'


def assert_no_processes() -> None:
    """Refuse to continue if any OneDrive process is alive."""
    if IS_WINDOWS:
        alive = processes()
        if alive:
            raise SystemExit(
                "OneDrive is still running:\n  "
                + "\n  ".join(alive)
                + "\nStop it first:\n  " + stop_command())
        return

    patterns = ["OneDrive.app/Contents/MacOS/OneDrive", "OneDrive File Provider"]
    alive = []
    for pat in patterns:
        r = subprocess.run(["/usr/bin/pgrep", "-f", pat],
                           capture_output=True, text=True)
        if r.stdout.strip():
            alive.append(pat)
    if alive:
        raise SystemExit(
            "OneDrive is still running (%s). Quit it first:\n  %s"
            % (", ".join(alive), stop_command()))


# ---------------------------------------------------------------------------
# Sync diagnostics log (Windows)
# ---------------------------------------------------------------------------

_NUM = re.compile(r"^-?\d+$")


def sync_diagnostics(account: str) -> dict:
    """Last ``Diagnostic Report`` block from SyncDiagnostics.log.

    This file is the only place Windows exposes the counter the UI shows as
    "Syncing N changes". The file is rewritten in place and may briefly contain
    only "Aborting sync verification", hence the empty-dict fallback.
    """
    if not IS_WINDOWS:
        return {}
    path = os.path.join(logs_path(account), "SyncDiagnostics.log")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as fh:
            data = fh.read().decode("utf-8-sig", errors="ignore")
    except OSError:
        return {}

    parts = data.split("Diagnostic Report")
    block = parts[-1] if len(parts) > 1 else ""
    out: dict = {"_source": path, "_has_report": len(parts) > 1}
    for line in block.splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$", line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if _NUM.match(raw):
            out[key] = int(raw)
        else:
            out[key] = raw
    return out


# ---------------------------------------------------------------------------
# SQLite connections
# ---------------------------------------------------------------------------

def _ro_uri(path: str) -> str:
    # SQLite on Windows wants forward slashes in the URI.
    return "file:" + path.replace("\\", "/") + "?mode=ro"


def connect_ro(path: str) -> sqlite3.Connection:
    """Open read-only without taking write locks."""
    con = sqlite3.connect(_ro_uri(path), uri=True, timeout=30)
    con.execute("PRAGMA query_only=1")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def connect_rw(path: str) -> sqlite3.Connection:
    """Open for modification. Caller must ensure OneDrive is fully stopped."""
    con = sqlite3.connect(path, isolation_level=None, timeout=30)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("PRAGMA busy_timeout=30000")
    return con


# ---------------------------------------------------------------------------
# macOS metadata tree (unused on Windows)
# ---------------------------------------------------------------------------

class Index:
    """In-memory view of the live (non-tombstoned) metadata tree."""

    def __init__(self, con: sqlite3.Connection, mount: str):
        self.mount = mount
        self.parent: dict[int, int] = {}
        self.type: dict[int, int] = {}
        self.name: dict[int, str] = {}
        self.materialized: dict[int, int] = {}
        rows = con.execute(
            "SELECT itemid, parentid, itemtype, name, materialized "
            "FROM dbfs_mftrecord WHERE tombstoned=0")
        for itemid, parentid, itemtype, name, materialized in rows:
            self.parent[itemid] = parentid
            self.type[itemid] = itemtype
            self.name[itemid] = name
            self.materialized[itemid] = materialized
        self._folder_cache: dict[int, str | None] = {ROOT_ITEMID: mount}

    def _chain(self, itemid: int) -> list[int]:
        chain, cur, seen = [], itemid, set()
        while cur not in self._folder_cache and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            if cur == ROOT_ITEMID:
                break
            nxt = self.parent.get(cur)
            if nxt is None or nxt == cur:
                break
            cur = nxt
        return chain

    def folder_path(self, itemid: int) -> str | None:
        """Absolute path of a folder itemid, or None if it does not exist on disk."""
        if itemid in self._folder_cache:
            return self._folder_cache[itemid]
        chain = self._chain(itemid)
        base = self._folder_cache.get(chain[-1] if chain else ROOT_ITEMID, self.mount)
        for cid in reversed(chain):
            if base is None:
                self._folder_cache[cid] = None
                continue
            path = os.path.join(base, leaf(self.name.get(cid, "")))
            try:
                ok = os.path.isdir(path)
            except OSError:
                ok = False
            base = path if ok else None
            self._folder_cache[cid] = base
        return self._folder_cache[itemid]

    def item_path(self, itemid: int) -> str | None:
        """Absolute path of a file itemid, or None if its parent is missing."""
        parent = self.folder_path(self.parent.get(itemid, ROOT_ITEMID))
        if parent is None:
            return None
        return os.path.join(parent, leaf(self.name.get(itemid, "")))

    def rel(self, itemid: int) -> str:
        path = self.item_path(itemid)
        if path is None:
            return f"<ghost>/{leaf(self.name.get(itemid, ''))}"
        return path[len(self.mount) + 1:] if path.startswith(self.mount) else path
