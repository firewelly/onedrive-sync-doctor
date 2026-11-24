#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows-specific analysis of the OneDrive sync engine database.

On Windows there is no ``dbfs.db`` and no ``ph_insync`` flag. The engine keeps
the same "could not process this change" queue as macOS 26.212, but in
``SyncEngineDatabase.db``:

    od_ClientFilePostponedChange_Records     <- pending file changes
    od_ClientFolderPostponedChange_Records   <- pending folder changes
    od_ClientFile_Records                   <- what the engine believes exists
    od_ClientFolder_Records

The UI's "Syncing N changes" number is the ``ChangesToProcess`` field in
``logs/<account>/SyncDiagnostics.log``, and it equals the row count of the two
postponed tables (measured: 201 = 183 files + 18 folders).

A row is an **orphan** when the object it refers to is no longer in the engine's
record tables. Those rows can never be processed: the engine keeps retrying and
postponing them forever, which produces the permanent counter and burns CPU.
Deleting the orphan rows does not touch local files, the cloud, or the record
tables, so it cannot delete anything.

Read-only by default. Nothing here writes; the repair lives in
``od_fix_postponed.py``.
"""
from __future__ import annotations

import datetime
import os
import sqlite3

import od

FILE_POSTPONED = "od_ClientFilePostponedChange_Records"
FOLDER_POSTPONED = "od_ClientFolderPostponedChange_Records"
FILE_RECORDS = "od_ClientFile_Records"
FOLDER_RECORDS = "od_ClientFolder_Records"
MIGRATE_POSTPONED = "od_MigrateItemPostponedChange_Records"

CHUNK = 400


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _existing(con: sqlite3.Connection, table: str, ids: set[str]) -> set[str]:
    """Which of these resourceIDs still exist in ``table``."""
    found: set[str] = set()
    wanted = sorted(i for i in ids if i)
    for start in range(0, len(wanted), CHUNK):
        part = wanted[start:start + CHUNK]
        marks = ",".join("?" * len(part))
        found.update(r[0] for r in con.execute(
            f"SELECT resourceID FROM {table} WHERE resourceID IN ({marks})",
            part))
    return found


def postponed_rows(con: sqlite3.Connection) -> dict[str, list[dict]]:
    """Every row from both postponed tables, with a stable ``rowid``."""
    out = {"files": [], "folders": []}
    if _has_table(con, FILE_POSTPONED):
        for row in con.execute(
                f"SELECT rowid, resourceID, parentResourceID, changeType, flags, "
                f"postponedCount, fileName, timeAdded, size, eTag "
                f"FROM {FILE_POSTPONED}"):
            out["files"].append({
                "rowid": row[0], "resourceID": row[1], "parentResourceID": row[2],
                "changeType": row[3], "flags": row[4],
                "postponedCount": row[5] or 0, "fileName": row[6] or "",
                "timeAdded": row[7], "size": row[8], "eTag": row[9] or "",
            })
    if _has_table(con, FOLDER_POSTPONED):
        for row in con.execute(
                f"SELECT rowid, resourceID, parentResourceID, changeType, flags, "
                f"postponedCount, folderName, eTag FROM {FOLDER_POSTPONED}"):
            out["folders"].append({
                "rowid": row[0], "resourceID": row[1], "parentResourceID": row[2],
                "changeType": row[3], "flags": row[4],
                "postponedCount": row[5] or 0, "folderName": row[6] or "",
                "eTag": row[7] or "",
            })
    return out


def _annotate(con: sqlite3.Connection, rows: dict[str, list[dict]]) -> None:
    """Mark each row ``orphan`` / ``live``; mutates the dicts in place."""
    files = rows["files"]
    live_file_ids = _existing(con, FILE_RECORDS,
                              {r["resourceID"] for r in files})
    for r in files:
        r["liveRecord"] = r["resourceID"] in live_file_ids
        r["orphan"] = not r["liveRecord"]

    folders = rows["folders"]
    wanted_ids = {r["resourceID"] for r in folders}
    wanted_parents = {r["parentResourceID"] for r in folders}
    live_ids = _existing(con, FOLDER_RECORDS, wanted_ids)
    live_parents = _existing(con, FOLDER_RECORDS, wanted_parents)
    for r in folders:
        r["liveRecord"] = r["resourceID"] in live_ids
        r["liveParent"] = (r["parentResourceID"] or "") in live_parents
        # A change whose folder or parent is gone can never resolve a path.
        r["orphan"] = not r["liveRecord"] or not r["liveParent"]


def _stats(rows: list[dict], kind: str) -> dict:
    retries = [r["postponedCount"] for r in rows]
    orphans = [r for r in rows if r["orphan"]]
    by_change: dict[tuple, dict] = {}
    for r in rows:
        key = (r["changeType"], r["flags"])
        slot = by_change.setdefault(key, {"count": 0, "orphans": 0,
                                          "min": None, "max": 0})
        slot["count"] += 1
        slot["orphans"] += 1 if r["orphan"] else 0
        pc = r["postponedCount"]
        slot["min"] = pc if slot["min"] is None else min(slot["min"], pc)
        slot["max"] = max(slot["max"], pc)
    return {
        "kind": kind,
        "total": len(rows),
        "orphan": len(orphans),
        "live": len(rows) - len(orphans),
        "retries": sum(retries),
        "retry_min": min(retries) if retries else 0,
        "retry_max": max(retries) if retries else 0,
        "by_change": [
            {"changeType": ct, "flags": fl, **slot}
            for (ct, fl), slot in sorted(by_change.items(),
                                         key=lambda kv: (kv[0][0], kv[0][1]))
        ],
    }


def _record_counts(con: sqlite3.Connection) -> dict:
    def count(table):
        if not _has_table(con, table):
            return None
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return {
        "file_records": count(FILE_RECORDS),
        "folder_records": count(FOLDER_RECORDS),
        "migrate_postponed": count(MIGRATE_POSTPONED),
    }


def analyze(account: str, integrity: bool = False) -> dict:
    """Everything the tools need about one Windows account, in one read pass."""
    db = od.db_path(account)
    diag = od.sync_diagnostics(account)
    info: dict = {
        "account": account,
        "platform": "windows",
        "db_path": db,
        "db_exists": os.path.exists(db),
        "sync_root": od.mount_path(account),
        "diagnostics": diag,
        "has_report": bool(diag.get("_has_report")),
        "changes_to_process": diag.get("ChangesToProcess"),
        "files_to_download": diag.get("FilesToDownload"),
        "bytes_to_download": diag.get("BytesToDownload"),
        "num_file_in_warning": diag.get("numFileInWarning"),
        "sync_stall_detected": diag.get("syncStallDetected"),
        "uptime_secs": diag.get("uptimeSecs"),
        "report_time": diag.get("timeUtc") or diag.get("UtcNow"),
    }
    if not info["db_exists"]:
        info["verdict"] = "no-database"
        return info

    info["db_size_mb"] = round(os.path.getsize(db) / 1e6, 1)
    con = od.connect_ro(db)
    try:
        info.update(_record_counts(con))
        rows = postponed_rows(con)
        _annotate(con, rows)
        info["files"] = _stats(rows["files"], "files")
        info["folders"] = _stats(rows["folders"], "folders")
        info["postponed_total"] = info["files"]["total"] + info["folders"]["total"]
        info["orphan_total"] = info["files"]["orphan"] + info["folders"]["orphan"]
        info["retry_total"] = info["files"]["retries"] + info["folders"]["retries"]
        if integrity:
            info["integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()

    counter = info["changes_to_process"]
    if counter is None:
        # SyncDiagnostics.log is being rewritten or has no report yet. The
        # postponed row count is the same number, one refresh behind.
        counter = info["postponed_total"]
        info["changes_to_process_source"] = "postponed-rows"
    else:
        info["changes_to_process_source"] = "SyncDiagnostics.log"
    info["changes_to_process"] = counter

    if counter == 0 and info["postponed_total"] == 0:
        info["verdict"] = "clean"
    elif info["postponed_total"] == 0:
        info["verdict"] = "clean"          # log stale, queue empty
    elif info["orphan_total"] == info["postponed_total"]:
        info["verdict"] = "stuck-orphan"
    elif info["orphan_total"]:
        info["verdict"] = "stuck-mixed"
    else:
        info["verdict"] = "queued"         # all rows still resolve; likely in flight
    return info


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def _fmt_time(value) -> str:
    if not value:
        return "-"
    try:
        return datetime.datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return str(value)


def audit_text(account: str, details: bool = False) -> dict:
    info = analyze(account)
    print(f"\n=== {account} ===")
    print(f"settings   : {od.db_path(account)}")
    print(f"sync root  : {info['sync_root']}")
    if not info["db_exists"]:
        print("database   : MISSING")
        return info

    print(f"database   : {info['db_path']} ({info['db_size_mb']} MB)")
    version = od.client_version()
    if version:
        print(f"client     : {version}")

    diag = info["diagnostics"]
    if info["has_report"]:
        print(f"diagnostics: ChangesToProcess={info['changes_to_process']} "
              f"FilesToDownload={info['files_to_download']} "
              f"numFileInWarning={info['num_file_in_warning']} "
              f"syncStallDetected={info['sync_stall_detected']} "
              f"uptimeSecs={info['uptime_secs']} "
              f"({info['report_time']})")
    else:
        print("diagnostics: no Diagnostic Report in SyncDiagnostics.log "
              "(engine idle/rotating); using the postponed queue")
        print(f"diagnostics: ChangesToProcess={info['changes_to_process']} "
              f"(from {info['changes_to_process_source']})")

    print(f"engine rows: files={info['file_records']} "
          f"folders={info['folder_records']}")

    for kind in ("files", "folders"):
        s = info[kind]
        if not s["total"]:
            print(f"pending {kind:<8}: 0")
            continue
        print(f"pending {kind:<8}: {s['total']}  "
              f"orphan={s['orphan']}  live={s['live']}  "
              f"retries={s['retries']:,} ({s['retry_min']:,}-{s['retry_max']:,})")
        for b in s["by_change"]:
            print(f"    changeType={b['changeType']:<4} flags={b['flags']:<4} "
                  f"rows={b['count']:<5} orphan={b['orphans']:<5} "
                  f"retries={b['min']}-{b['max']}")
    if info["migrate_postponed"]:
        print(f"migrate queue: {info['migrate_postponed']} "
              f"(not part of ChangesToProcess; usually 0)")

    verdict = info["verdict"]
    labels = {
        "clean": "CLEAN (nothing pending)",
        "stuck-orphan": "STUCK - every postponed row is an orphan",
        "stuck-mixed": "STUCK - orphan rows plus rows whose record still exists",
        "queued": "QUEUED - rows still resolve; likely in flight, watch it",
        "no-database": "NO DATABASE",
    }
    print(f"verdict    : {labels.get(verdict, verdict)}  "
          f"(ChangesToProcess={info['changes_to_process']})")
    if info.get("integrity"):
        print(f"integrity  : {info['integrity']}")

    if details and info["postponed_total"]:
        con = od.connect_ro(info["db_path"])
        try:
            rows = postponed_rows(con)
            _annotate(con, rows)
        finally:
            con.close()
        shown = 0
        for item in sorted(rows["files"] + rows["folders"],
                           key=lambda r: -r["postponedCount"]):
            if shown >= 40:
                print(f"    ... and {info['postponed_total'] - 40} more")
                break
            name = item.get("fileName") or item.get("folderName") or "<empty>"
            print(f"    {'orphan' if item['orphan'] else 'live  '} "
                  f"{item['postponedCount']:>8}  {_fmt_time(item.get('timeAdded'))}"
                  f"  type={item['changeType']}/flags={item['flags']}  "
                  f"{name}  {(item['resourceID'] or '')[:16]}")
            shown += 1
    return info


def counts(account: str) -> dict:
    """Compact numeric snapshot used by od_state.py / summary.py."""
    info = analyze(account)
    return {
        k: info.get(k) for k in (
            "db_path", "db_exists", "db_size_mb", "sync_root",
            "changes_to_process", "changes_to_process_source",
            "files_to_download", "num_file_in_warning",
            "sync_stall_detected", "uptime_secs", "report_time",
            "postponed_total", "orphan_total", "retry_total",
            "file_records", "folder_records", "verdict",
        )
    } | {
        "postponed_files": info.get("files", {}).get("total", 0),
        "postponed_folders": info.get("folders", {}).get("total", 0),
        "orphan_files": info.get("files", {}).get("orphan", 0),
        "orphan_folders": info.get("folders", {}).get("orphan", 0),
        "integrity": info.get("integrity"),
        "has_report": info.get("has_report"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Audit the Windows OneDrive sync queue (read-only)")
    ap.add_argument("accounts", nargs="*", help="account names (default: all)")
    ap.add_argument("--details", action="store_true", help="list worst rows")
    args = ap.parse_args()
    for name in (args.accounts or od.accounts()):
        audit_text(name, args.details)
    print("\nNext steps:")
    print("  clean        -> nothing to do")
    print("  stuck-orphan -> python od_fix_postponed.py <account>")
    print("                  (preview), then --apply after OneDrive.exe /shutdown")
    print("  stuck-mixed  -> preview with od_fix_postponed.py; --apply removes")
    print("                  orphan rows only, review live rows first")
    print("  queued       -> wait and re-check; if it never moves, run --details")


if __name__ == "__main__":
    main()
