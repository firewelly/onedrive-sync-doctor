# OneDrive sync stall: what "Syncing N files" is actually stuck on — and how to fix it

Diagnose and repair the permanent OneDrive "Syncing N" queue — on macOS **and
Windows** — without resetting the account.

> Measured on real machines: macOS 2026-10-30 (two accounts stuck at 307 and 1,
> repaired to 1 and 1), Windows 2026-11-24 (201 changes stuck for **16 months**,
> repaired to 0). The Mac case looked like a client-version regression and cost
> a night of downgrade/rollback. The Windows case looked like a queue that would
> eventually drain. Both had the same cause: engine records that point at
> objects which no longer exist.

**Contents**

- [The symptom](#the-symptom)
- [Where the state actually lives](#where-the-state-actually-lives)
- [What N really is](#what-n-really-is)
- [The kinds of stuck item](#the-kinds-of-stuck-item)
- [Fixing it, cheapest first](#fixing-it-cheapest-first)
- [Traps](#traps)
- [Why it comes back](#why-it-comes-back)
- [Prevention and a 1-minute health check](#prevention-and-a-1-minute-health-check)
- [Acceptance criteria](#acceptance-criteria)
- [The tools in this repo](#the-tools-in-this-repo)

Chinese write-ups: [macOS](docs/onedrive-mac-sync-stuck.zh-CN.md) ·
[Windows](docs/onedrive-windows-sync-stuck.zh-CN.md)

---

## The symptom

**macOS.** OneDrive spins in the menu bar and reports *Syncing N files*. N does
not move — not up, not down, for hours. Edits do not upload; brand-new files
never leave the machine. Both `OneDrive File Provider` processes burn 70–90%
CPU; the fan comes on; `~/Library/Logs/OneDrive/<account>/` fills at hundreds of
files per day. Restarting OneDrive or the machine changes nothing.

**Windows.** The status line reports *Syncing 201 changes / 3 files* and stays
there. The queue does not drain and CPU burns on every full rescan, but unlike
macOS the extension does not restart and the fan may stay quiet. Restarting the
client and installing a newer build both preserve the number. On the case
machine it survived a client upgrade, a reboot, and 16 months of daily use.

It is tempting to blame the client version, especially if it updated recently.
**In these cases it was not the version.**

---

## Where the state actually lives

### macOS: three layers

```
~/Library/Group Containers/UBF8T346G9.OneDriveStandaloneSuite/
├── .Dbfs.Dbfs_<account>.noindex/dbfs.db     # one per account, ~1 GB
└── FileProviderLogs/
```

A SQLite database with a million-plus rows (schema `version=7`). Two tables carry
the story:

**`dbfs_mftrecord`** — "what I believe exists on the filesystem".

| Column | Meaning |
|---|---|
| `itemid` / `parentid` | Self-referencing tree; the root is `itemid=1, parentid=1` |
| `name` | Name, sometimes prefixed with OneDrive's internal `//v0/<34>//` |
| `tombstoned` | 0 = present, 1 = deleted |
| `materialized` | 1 = content is local, 0 = placeholder (content is in the cloud) |
| **`ph_insync`** | **0 = not synced — this is what the UI counts** |
| `content_hash` / `size` | Used to diff against the cloud |

**`dbfs_changerecord`** — "local changes still waiting to be uploaded".

The relationship between them is the entire bug:

> A change record says *"this file needs uploading"*. The metadata says *"this
> file lives here"*. Once the metadata describes a path that no longer exists on
> disk, that change can never complete.

Layer 2 is the File Provider domain under `~/Library/CloudStorage/`, managed by
macOS. It records which client version created or upgraded it — which becomes a
trap later. Layer 3 is the binary engine log under
`~/Library/Logs/OneDrive/<account>/SyncEngine-*.odl` (read with `strings`):

```
ProcessLocalChange LC_REPLACE_FILE 3
UnexpectedFailure!
PostponedChanges.cpp  SetPostponedStatus
PostponedChanges.cpp  PostponeChange
Diagnostics.cpp       RunDiagnostics
Diagnostics.cpp       RetryPostponedChanges     ← repeat forever
```

A few lines further up is the actual error source:

```
DbfsMacboxGetUserVisibleItemPath
Errno::2 (0x2) [NotFound] [source:0x049d02d2]
...
OdfsDbfs::-615838174 (0xdb4b0e22) [Permanent]
FileForRead.cpp       Read
ReplaceFile.cpp       CompareDiskFileAgainstDB
```

`DbfsMacboxGetUserVisibleItemPath → NotFound` means: **the itemid in the database
cannot be mapped back to a path on disk.**

### Windows: two entries, same table names

Windows has no `dbfs.db` and no File Provider domain. Everything is in the sync
engine database and one plain-text log:

```
%LOCALAPPDATA%\Microsoft\OneDrive\settings\<Account>\
├── SyncEngineDatabase.db (+ -wal/-shm)   # engine DB (1.4 GB personal, 724 MB business, measured)
├── OCSI.db, SafeDelete.db, ...
└── ...

%LOCALAPPDATA%\Microsoft\OneDrive\logs\<Account>\
├── SyncDiagnostics.log                   # plain text, rewritten in place — this is the useful one
└── SyncEngine-*.odl                      # binary, 1 MB rolling, not greppable
```

The sync root for each account is in
`HKCU\Software\Microsoft\OneDrive\Accounts\<Account>\UserFolder` — no need to
hardcode `D:\OneDrive` or any account name.

The engine's queue lives in the **same tables as macOS 26.212**:

| Table | Meaning |
|---|---|
| `od_ClientFilePostponedChange_Records` | file changes the engine postponed |
| `od_ClientFolderPostponedChange_Records` | folder changes the engine postponed |
| `od_ClientFile_Records` | files the engine believes exist |
| `od_ClientFolder_Records` | folders the engine believes exist |

A postponed row is retried, fails, and gets `postponedCount += 1`. If the object
it points at is no longer in the corresponding `od_Client*_Records` table, no
retry can ever succeed.

---

## What N really is

**macOS** — the most useful fact in the Mac write-up:

```sql
-- ~/Library/Group Containers/UBF8T346G9.OneDriveStandaloneSuite/.Dbfs.Dbfs_<account>.noindex/dbfs.db
SELECT COUNT(*) FROM dbfs_mftrecord WHERE tombstoned = 0 AND ph_insync = 0;
```

Measured 346 → UI showed 307, and 9 → UI showed 1. Same order, same trend.

**Windows** — the field is in `logs\<account>\SyncDiagnostics.log`:

```text
ChangesToProcess = 201
FilesToDownload  = 3
numFileInWarning = 0
```

and it equals the postponed row count, exactly: **201 = 183 file rows + 18
folder rows** (measured). `numFileInWarning` counts items the GUI shows as
errors, and was 0 — the queue was stuck, not failing loudly.

Two counter-intuitive corollaries that hold on both platforms:

- **The change-record / postponed count is not a progress indicator.** On macOS
  the stuck account had 25,020 change records while the UI showed 307; on
  Windows 201 rows had been retried 4.7 million (files) + 0.47 million (folders)
  times over 16 months without moving.
- **`syncStallDetected = 0` does not mean it is not stuck.** Both engines report
  "no stall" while parked on postponed changes. Judge by `ChangesToProcess` /
  `ph_insync`, not by that flag.

> If the log currently contains only `Sync Diagnostics - Sync Verification` /
> `Aborting sync verification`, it is between report writes. The tools fall back
> to the postponed row count, which is the same number one refresh behind.

---

## The kinds of stuck item

### macOS: five kinds

Resolve every `ph_insync = 0` record to its real path on disk and the pile
splits into five kinds (distribution of 346 records in the Mac case):

| Kind | Count | What it is | Fix |
|---|---|---|---|
| **ghost** | 305 | metadata points at a path that no longer exists | tombstone the folder |
| **symlink** | 9 | symlinks and `._*` AppleDouble files; OneDrive never syncs them | delete |
| **`__MACOSX/`** | 25 | zip-extraction leftovers | delete the directory |
| **build-artifact** | 4 | `ephemeral/`, `.dart_tool/`, lock files | delete; regenerated |
| **stubborn** | 2 | file is present and readable, yet the replace never lands | recreate the file |
| *pending* | — | just created/modified (change type 2/5/260) | wait |
| *system* | 1 | OneDrive's own `temp` record (`itemid 9223372036854775708`) | ignore forever |

Ghost records were caused by a `_trash` folder emptied months earlier while its
7,136 metadata records stayed `tombstoned = 0`. A later full rescan tried to
resolve all of them and parked the queue. Symlinks, `__MACOSX/` leftovers and
build artifacts never sync by design, but are still recorded and then never
clear.

### Windows: orphan rows, zombie rows, live rows

On the Windows case, all 201 rows were unresolvable, in two flavours:

| Type | Rows | Signature |
|---|---|---|
| pure zombie | 122 | `changeType=4, flags=0`, empty `fileName`/`eTag`, NULL `size`/`timeAdded` |
| old bill | 61 | `changeType=5, flags=4`, real name/eTag/size, `lastChange` in 2025-06/07 |

Plus 5 folder-provision rows (`changeType=1`) and 13 folder-delete rows
(`changeType=2`, empty `folderName`) pointing at the same old IDs.

Why they can never succeed:

- the files are on disk and readable (e.g. `__pycache__/*.pyc`, `Archive|ReparsePoint|Pinned`);
- the engine has **re-registered the same objects under new resource IDs** after
  the folder was recreated;
- the 201 changes still reference the **old parent ID**, whose record was dropped,
  so every retry fails to resolve a path and gets postponed again.

The tools classify rows as:

| Class | Test | Action |
|---|---|---|
| **orphan** | file `resourceID` absent from `od_ClientFile_Records`; folder `resourceID` or `parentResourceID` absent from `od_ClientFolder_Records` | delete the row |
| **live** | the record still exists | leave it; it may be in flight |
| **stuck-mixed** | both kinds present | delete orphans, review live rows first |

Windows has no `__MACOSX`/symlink distinction to make — the engine queue only
contains changes it already accepted. The same prevention rules still apply.

---

## Fixing it, cheapest first

### macOS

**Tier 1 — drop impossible change records.** Delete only `dbfs_changerecord`
rows for a gone subtree; leave `dbfs_mftrecord` alone, so nothing is sent to the
cloud:

```bash
python3 od_fix_records.py Personal --root 1255264            # preview
python3 od_fix_records.py Personal --root 1255264 --apply
```

On the case machine this removed 76,797 rows (75.7% of the queue). It does **not**
fix the UI counter, because stuck items usually carry no change record.

**Tier 2 — delete what should never have been in OneDrive.** `__MACOSX/`,
`macos|linux/flutter/ephemeral/`, `.dart_tool/`, `venv/`, `.conda/`,
`node_modules/`. No database work.

**Tier 3 — tombstone the ghost folder.** This is the real fix for the counter.
The schema trigger `enforce_constraint_update_tombstone` refuses to mark a parent
deleted while a child is alive, so the tool goes deepest-first in one transaction:

```bash
python3 od_tombstone.py Personal --root 1293203              # preview
python3 od_tombstone.py Personal --root 1293203 --apply      # OneDrive must be stopped
```

Result on the case: 7,136 tombstoned, `remaining alive: 0`, `ph_insync` 346 → 41,
then 41 → 1 after clearing leftovers. That 1 is the immortal system record.

**Tier 4 — reset the domain.** Not recommended. The shipped
`ResetOneDriveAppStandalone.command` removes every account's `dbfs.db`, the
content cache, the domains and the keychain credentials. If the queue holds
locally created files that were never uploaded, that is data loss.

### Windows

**Tier 1 — move the offending project out of the sync root.** Move the folder
(e.g. a Python project carrying `.git` + `__pycache__`) outside OneDrive, let the
engine tombstone the whole batch, move it back if needed. Costs a re-upload but
also removes the root cause. No database work.

**Tier 2 — delete the orphan postponed rows (the real fix).** Read-only preview
first; then stop the engine, backup, and delete only rows that cannot resolve:

```powershell
python od_audit.py                       # what is queued, orphan vs live
python od_fix_postponed.py Personal      # preview: rows, retries, live rows kept

& "C:\Program Files\Microsoft OneDrive\OneDrive.exe" /shutdown
Start-Sleep 5
Get-Process OneDrive -ErrorAction SilentlyContinue     # must be empty

python od_fix_postponed.py Personal --apply
Start-Process "C:\Program Files\Microsoft OneDrive\OneDrive.exe"
```

What `--apply` does, in order:

1. refuses to run while `OneDrive.exe` is alive;
2. copies `SyncEngineDatabase.db` (+ `-wal`/`-shm`) to
   `~/od-db-backup-<timestamp>/` and prints its SHA256;
3. writes a TSV audit of every row (including the `orphan` column);
4. deletes inside one transaction, verifies the deleted count, rolls back on mismatch;
5. `PRAGMA wal_checkpoint(TRUNCATE)` + `quick_check`.

Rows whose record still exists are **kept** by default. `--all` clears them too —
needed for long-dead folder-delete orders such as the three rows in the case
that pointed at a folder deleted 16 months earlier. Preview first and read the
live rows it lists.

Manual equivalent, if you prefer SQL (same invariants: engine stopped, backup
kept, read-only proof first, single transaction):

```sql
-- must be 0 before deleting the whole table
SELECT COUNT(*) FROM od_ClientFilePostponedChange_Records p
 WHERE EXISTS (SELECT 1 FROM od_ClientFile_Records f
               WHERE f.resourceID = p.resourceID);

BEGIN;
DELETE FROM od_ClientFilePostponedChange_Records;       -- 183 rows in the case
DELETE FROM od_ClientFolderPostponedChange_Records;     -- 18 rows in the case
COMMIT;
PRAGMA wal_checkpoint(TRUNCATE);
```

Deleting the queue rows does **not** touch `od_ClientFile_Records`, the disk, or
the cloud, so no deletion is ever sent to Microsoft. The row audit and the backup
are kept so the 201 rows can be restored if needed.

Result on the case machine: 183 + 18 deleted, `quick_check = ok`, and 90 seconds
after restart `ChangesToProcess = 0`, `FilesToDownload = 0`,
`numFileInWarning = 0` while a full rescan was in progress.

**Tier 3 — `/reset` / reinstall.** Same verdict as macOS: it wipes the local
engine state and credentials, risks unsynced local content, and does not address
the cause. It can also come back after the next full rescan.

---

## Traps

### macOS (four)

1. **The system `temp` record can never be tombstoned.** It points at itself, so
   the trigger always sees a live child. When the count settles at 1, that 1 is
   this record — you are done.
2. **The queue grows right after a tombstone.** Reconciliation inserts new
   `change_type=260` rows (25,057 → 30,467 in the case); judge by `ph_insync`,
   never by queue length.
3. **Downgrading the client is a one-way door.** `OneDrive.pkg`'s preinstall
   silently skips a downgrade (`exit 0`, zero files copied), and the newer
   client's File Provider domain version stamp makes the older client unusable
   (modal dialog with only "Quit OneDrive"). Do not use downgrading.
4. **`Errno::2 NotFound` does not mean the file is gone.** The metadata→path
   layer reports NotFound when it cannot resolve an itemid. Verify with
   `shasum`; all 24,777 files in the case were present and readable.

### Windows (five)

1. **Never hot-edit the database with `-wal` while the engine runs.** Use
   `OneDrive.exe /shutdown`, confirm `Get-Process OneDrive` is empty, then edit.
   The database is 1–1.5 GB; a live writer can overwrite your changes or leave
   the WAL inconsistent. The tools enforce this.
2. **Do not use `/reset`.** Same class of destruction as the Mac reset script:
   local state, cache and credentials go away; never-uploaded local files can
   go with them.
3. **`SyncEngine-*.odl` is binary.** 1 MB rolling files, not greppable. Only
   `SyncDiagnostics.log` is text; `strings` is not a substitute for the
   database.
4. **`syncStallDetected = 0` is not a clean bill.** The engine reports it while
   parked on postponed changes. Use `ChangesToProcess`.
5. **The log is rewritten in place.** It may briefly contain only
   "Aborting sync verification" and no counters. Fall back to the postponed row
   count — same number. Do not conclude "synced" from a missing report.

---

## Why it comes back

Nothing in either client prevents unsyncable or unstable content from entering
the sync root:

- build artifacts and caches — every rebuild leaves an orphaned record behind;
  `__pycache__`, `.pyc`, `.git`, `node_modules`, `venv`, `.dart_tool` are the
  usual suspects on both platforms;
- deleting or renaming a directory while sync is unhealthy — the cloud
  converges, local metadata does not, and the mismatch surfaces months later
  during a full rescan;
- recreating a folder — the engine re-registers the same objects under new
  resource IDs while old change orders still reference the dropped parent;
- client auto-updates trigger full rescans, which is when old bills surface
  (Windows: client updated 2026-11-13, first rescan 2026-11-14 exposed 201 rows
  from 2025-06; macOS: likewise);
- heavy multi-device sync — conflict copies (`name-<Machine Name> (2).md`) add
  metadata noise.

Treat it as **periodic maintenance**, not a one-time repair.

---

## Prevention and a 1-minute health check

1. Never put build/environment directories in OneDrive: `node_modules`, `venv`,
   `.conda`, `.dart_tool`, `ephemeral`, `target`, `dist`, `.git`, `__pycache__`.
2. Delete or rename directories **while sync is healthy**, and give OneDrive
   time to finish before shutting down. The dangerous case is "deleted or
   renamed while sync was already broken".
3. Run a check every month or two, and on the day the client updates.

**macOS:**

```bash
DB=~/Library/Group\ Containers/UBF8T346G9.OneDriveStandaloneSuite/.Dbfs.Dbfs_Personal.noindex/dbfs.db
sqlite3 "$DB" "SELECT COUNT(*) FROM dbfs_mftrecord WHERE tombstoned=0 AND ph_insync=0;"
```

**Windows:**

```powershell
python od_audit.py
# or just the counter:
Select-String "$env:LOCALAPPDATA\Microsoft\OneDrive\logs\Personal\SyncDiagnostics.log" `
  -Pattern '^(ChangesToProcess|FilesToDownload|numFileInWarning) ='
```

A single digit (macOS) or 0 (Windows) is healthy. Two or three digits that do not
move = stuck. Before touching any database, back it up with the client fully
stopped (`lsof` on macOS; `Get-Process OneDrive` on Windows).

Windows has no per-subfolder exclusion; deduplicate by choosing which top-level
folders sync, or by moving projects out of the sync root.

---

## Acceptance criteria

Do not settle for "the spinner stopped". All of these should hold:

- **macOS:** `ph_insync = 0` back to a single digit (that 1 is the system record);
  `PRAGMA integrity_check` = `ok`; a new test file uploads within a minute;
  extension PIDs stop churning; CPU drops from 80%+ to single digits.
- **Windows:** `ChangesToProcess = 0`, `FilesToDownload = 0`,
  `numFileInWarning = 0`; GUI shows up to date; the number stays 0 after the
  post-restart full rescan (`fullScanCount` increments); `quick_check` = `ok`.
- **Both:** a newly created file appears in the cloud within a minute.

After the Mac repair: `ph_insync` 346 → 1, `integrity_check = ok`, main process
CPU 129% → 10%, extension down from 95%. After the Windows repair: 201 → 0 in
90 seconds, no data touched, audit list and backup retained.

---

## The tools in this repo

Python 3.10+, standard library only, nothing to install.

```bash
git clone https://github.com/firewelly/onedrive-sync-doctor.git
cd onedrive-sync-doctor
```

Accounts, database paths, sync roots, client version and logs are discovered
from OneDrive's own settings on both platforms. Nothing is hardcoded; on
Windows the account list comes from the OneDrive registry key.

**Read-only diagnosis (both platforms)**

```bash
python od_state.py                    # version, processes, per-account counters (JSON)
python od_state.py --pretty
python od_audit.py                    # macOS: five buckets; Windows: orphan/live rows
python od_audit.py --details          # ... plus paths / worst rows
python summary.py                     # one-screen verdict
```

**Repair**

```bash
# macOS
python od_tombstone.py Personal --root 1293203            # preview
python od_tombstone.py Personal --root 1293203 --apply    # ghost subtree
python od_fix_records.py Personal --root 1255264          # preview
python od_fix_records.py Personal --root 1255264 --apply  # impossible change records
python od_backup_list.py Personal --out /Volumes/Backup/od-personal

# Windows
python od_fix_postponed.py Personal                       # preview
python od_fix_postponed.py Personal --all                 # preview, incl. live rows
python od_fix_postponed.py Personal --apply               # after OneDrive.exe /shutdown
python od_fix_postponed.py Personal --apply --all
```

| Script | Platform | Purpose |
|---|---|---|
| `od.py` | both | Discovery: accounts, DB path, sync root, logs, processes, version |
| `od_win.py` | Windows | Postponed queue analysis, orphan proof, audit output |
| `od_state.py` | both | Snapshot of version, processes, counters and integrity |
| `od_audit.py` | both | macOS: classify `ph_insync=0` into five kinds; Windows: orphan/live queue |
| `summary.py` | both | One-screen health verdict per account |
| `od_tombstone.py` | macOS | Bottom-up tombstone of a ghost subtree |
| `od_fix_records.py` | macOS | Delete `dbfs_changerecord` rows that can never complete |
| `od_fix_postponed.py` | Windows | Delete orphan `od_Client*PostponedChange_Records` rows |
| `od_backup_list.py` | macOS | Export local-only files as a list for `tar` |

Write tools default to a preview, call `assert_no_processes()` first, run inside
a transaction, and roll back when the affected row count differs from the
prediction. `od_fix_postponed.py` also backs up the engine database by default
and keeps a row-level TSV audit.

For a controlled environment (a database copy, or a macOS 26.212 engine
database), `od_fix_postponed.py` accepts `--db /path/to/SyncEngineDatabase.db`,
`--no-backup` and `--audit <path>`.

---

## Requirements and warnings

- macOS with the **standalone** OneDrive build (the App Store build has a
  different layout), or Windows 10/11.
- Python 3.10+.
- These tools touch OneDrive's private, undocumented database. Schema versions
  change without notice. Read the code before running it, and keep the backups.
- No warranty. This operates on data you cannot easily regenerate.

## License

MIT.
