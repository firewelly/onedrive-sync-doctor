#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Delete postponed change rows that can never succeed (Windows).

The Windows sync engine retries a change by moving it to
``od_Client{File,Folder}PostponedChange_Records`` and incrementing
``postponedCount``. When the object it points at no longer exists in
``od_Client*_Records`` (or its parent folder is gone), every retry is guaranteed
to fail. Those rows are orphans: they keep ``ChangesToProcess`` above zero
forever and burn CPU on each full rescan.

This tool deletes **only** those rows. It never touches the record tables, local
files, or the cloud, so no deletion is ever sent to Microsoft. By default rows
whose record still exists are left in place; ``--all`` clears those too (useful
for long-dead folder-delete orders, but review the preview).

    python od_fix_postponed.py Personal                 # preview (read-only)
    python od_fix_postponed.py Personal --all           # preview including live rows

    # then:
    "C:/Program Files/Microsoft OneDrive/OneDrive.exe" /shutdown
    python od_fix_postponed.py Personal --apply
    python od_fix_postponed.py Personal --apply --all

What --apply does:
  1. refuses to run while OneDrive.exe is alive
  2. copies SyncEngineDatabase.db (+ -wal/-shm) to a timestamped backup folder
  3. writes a TSV audit of every row it is about to delete
  4. deletes inside one transaction, checks the row count, rolls back on mismatch
  5. WAL-checkpoints the database and runs a quick integrity check

    --no-backup     skip the copy (not recommended; the file is ~1.4 GB)
    --backup-dir    where to put the backup (default ~/od-db-backup-<stamp>)
    --audit PATH    where to write the TSV (default next to the backup)
    --restart       start OneDrive again when finished
"""
import argparse
import datetime
import hashlib
import os
import shutil
import subprocess
import sys

import od
import od_win


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest().upper()


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup(db, account, dest):
    os.makedirs(dest, exist_ok=True)
    copied = []
    for suffix in ("", "-wal", "-shm"):
        src = db + suffix
        if os.path.exists(src):
            target = os.path.join(dest, account + "-" + os.path.basename(db) + suffix)
            shutil.copy2(src, target)
            copied.append(target)
    main = os.path.join(dest, account + "-" + os.path.basename(db))
    return main, copied


def _write_audit(path, rows, account):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\t".join((
            "account", "table", "rowid", "resourceID", "parentResourceID",
            "changeType", "flags", "postponedCount", "orphan", "name",
            "timeAdded", "size", "eTag")) + "\n")
        for table, items in (("file", rows["files"]), ("folder", rows["folders"])):
            for r in items:
                name = r.get("fileName") or r.get("folderName") or ""
                fh.write("\t".join(str(x).replace("\t", " ") for x in (
                    account, table, r["rowid"], r["resourceID"],
                    r.get("parentResourceID") or "", r["changeType"], r["flags"],
                    r["postponedCount"], "yes" if r["orphan"] else "no", name,
                    r.get("timeAdded") or "", r.get("size") or "", r.get("eTag") or "",
                )) + "\n")


def _delete(con, table, rowids):
    deleted = 0
    for start in range(0, len(rowids), od_win.CHUNK):
        part = rowids[start:start + od_win.CHUNK]
        marks = ",".join("?" * len(part))
        deleted += con.execute(
            f"DELETE FROM {table} WHERE rowid IN ({marks})", part).rowcount
    return deleted


def _summary(rows):
    for kind in ("files", "folders"):
        items = rows[kind]
        if not items:
            print(f"  {kind:<8}: 0")
            continue
        orphans = sum(1 for r in items if r["orphan"])
        retries = sum(r["postponedCount"] for r in items)
        print(f"  {kind:<8}: {len(items)}  orphan={orphans}  "
              f"live={len(items) - orphans}  retries={retries:,}")


def main():
    ap = argparse.ArgumentParser(
        description="Delete impossible postponed changes (Windows)")
    ap.add_argument("account", help="account name, e.g. Personal")
    ap.add_argument("--db", default=None,
                    help="override the SyncEngineDatabase.db path (testing/advanced)")
    ap.add_argument("--apply", action="store_true", help="actually modify the database")
    ap.add_argument("--all", action="store_true",
                    help="delete every postponed row, not just orphans "
                         "(review the preview first)")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the database backup (not recommended)")
    ap.add_argument("--backup-dir", default=None,
                    help="backup folder (default ~/od-db-backup-<timestamp>)")
    ap.add_argument("--audit", default=None, help="TSV path for the deleted rows")
    ap.add_argument("--integrity", action="store_true",
                    help="run full PRAGMA integrity_check instead of quick_check")
    ap.add_argument("--restart", action="store_true", help="start OneDrive afterwards")
    args = ap.parse_args()

    if not od.is_windows() and not args.db:
        sys.exit("This tool targets the Windows sync engine database.\n"
                 "On macOS use od_tombstone.py / od_fix_records.py; for a macOS\n"
                 "26.212 SyncEngineDatabase.db pass --db explicitly.")

    db = args.db or od.db_path(args.account)
    if not os.path.exists(db):
        sys.exit(f"No database at {db}")

    con = od.connect_ro(db)
    try:
        rows = od_win.postponed_rows(con)
        od_win._annotate(con, rows)
    finally:
        con.close()

    total = len(rows["files"]) + len(rows["folders"])
    print(f"database : {db}")
    print(f"postponed rows:")
    _summary(rows)
    if not total:
        print("\nnothing to do.")
        return 0

    if args.all:
        targets = {"files": list(rows["files"]), "folders": list(rows["folders"])}
        kept = 0
    else:
        targets = {
            "files": [r for r in rows["files"] if r["orphan"]],
            "folders": [r for r in rows["folders"] if r["orphan"]],
        }
        kept = total - len(targets["files"]) - len(targets["folders"])

    print(f"\nto delete: files={len(targets['files'])} "
          f"folders={len(targets['folders'])}  "
          f"(live rows kept: {kept})")
    if kept:
        print("  rows kept because their record still exists (may be in flight):")
        shown = 0
        for r in rows["files"] + rows["folders"]:
            if r["orphan"] or shown >= 10:
                continue
            name = r.get("fileName") or r.get("folderName") or "<empty>"
            print(f"    retries={r['postponedCount']:>8}  type={r['changeType']}"
                  f"/flags={r['flags']}  {name}  {(r['resourceID'] or '')[:16]}")
            shown += 1
        print("  use --all to remove them as well (e.g. long-dead delete orders).")

    if not args.apply:
        print("\npreview only. re-run with --apply (OneDrive must be stopped first):")
        print(f"  {od.stop_command()}")
        return 0

    od.assert_no_processes()

    stamp = _stamp()
    backup_dir = args.backup_dir or os.path.expanduser(f"~/od-db-backup-{stamp}")
    if args.no_backup:
        print("\nbackup : SKIPPED (--no-backup)")
        backup_main = None
    else:
        print(f"\nbackup : copying {os.path.getsize(db) / 1e6:.0f} MB -> {backup_dir}")
        backup_main, copied = _backup(db, args.account, backup_dir)
        print(f"         {backup_main}")
        print(f"         SHA256 {_sha256(backup_main)}")

    audit_path = args.audit or os.path.join(
        backup_dir if not args.no_backup else os.path.expanduser("~"),
        f"od-postponed-{args.account}-{stamp}.tsv")
    _write_audit(audit_path, rows, args.account)
    print(f"audit  : {audit_path}  (all rows, orphan column included)")

    expected = len(targets["files"]) + len(targets["folders"])
    con = od.connect_rw(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            deleted = 0
            deleted += _delete(con, od_win.FILE_POSTPONED,
                               [r["rowid"] for r in targets["files"]])
            deleted += _delete(con, od_win.FOLDER_POSTPONED,
                               [r["rowid"] for r in targets["folders"]])
            if deleted != expected:
                raise RuntimeError(f"deleted {deleted}, expected {expected}")
            con.execute("COMMIT")
        except Exception as exc:                               # noqa: BLE001
            con.execute("ROLLBACK")
            print(f"rolled back: {exc}")
            return 3

        remaining = con.execute(
            f"SELECT COUNT(*) FROM {od_win.FILE_POSTPONED}").fetchone()[0]
        remaining += con.execute(
            f"SELECT COUNT(*) FROM {od_win.FOLDER_POSTPONED}").fetchone()[0]
        check = "integrity_check" if args.integrity else "quick_check"
        result = con.execute(f"PRAGMA {check}").fetchone()[0]
        print(f"deleted: {deleted} rows")
        print(f"remaining postponed rows: {remaining}")
        print(f"{check}: {result}")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()

    if args.restart:
        subprocess.Popen([od.ONEDRIVE_EXE])
        print("\nOneDrive restarted. Re-check in ~90 seconds:")
    else:
        print("\nRestart OneDrive:")
        print(f"  {od.start_command()}")
        print("Then re-check in ~90 seconds:")
    print(f"  python {'od_audit'}.py {args.account}")
    print("Acceptance: ChangesToProcess = 0, FilesToDownload = 0, "
          "numFileInWarning = 0.")
    if backup_main:
        print(f"Rollback: stop OneDrive, copy {backup_main}* back over "
              f"{db}*, then restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
