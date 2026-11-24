# OneDrive（Windows）卡在「正在同步 N 个更改」排查与修复

> 2026-11-14 首次实测（Windows 11，客户端 26.212.1029），2026-11-24 执行修复
> （26.213.1101）：`ChangesToProcess` **201 → 0**，队列里 183 条文件单 + 18 条
> 文件夹单全部清空。与 macOS 版同源问题、同一批表、同一套修法；差异见 §5。
> macOS 侧材料见 [`onedrive-mac-sync-stuck.zh-CN.md`](onedrive-mac-sync-stuck.zh-CN.md)。

---

## 0. 一句话结论

Windows 客户端**没有** macOS 的 `dbfs.db` / File Provider 域那套东西，但有
**两个更好用的入口**：

| 想问什么 | 看哪里 |
|---|---|
| GUI 那个「正在同步 N 个更改」到底是多少 | `logs\<账户>\SyncDiagnostics.log` 里的 `ChangesToProcess`（**等于** postponed 表行数） |
| 具体卡在哪些文件上 | `settings\<账户>\SyncEngineDatabase.db` → `od_ClientFilePostponedChange_Records` / `od_ClientFolderPostponedChange_Records`（**表名与 macOS 26.212 完全一致**） |

**关键判据：`ChangesToProcess = 0` 且 `FilesToDownload = 0` 就是干净；两位数、
三位数且长时间不降 = 卡住。`syncStallDetected` 卡住时通常仍是 0，不能只看它。**

---

## 1. 本机路径

| 账户 | 同步根 | settings | logs |
|---|---|---|---|
| 个人（Personal）| `D:\OneDrive` | `%LOCALAPPDATA%\Microsoft\OneDrive\settings\Personal` | `...\logs\Personal` |
| 公司（Business1）| `D:\SYN_Sync\OneDrive - <Company>` | `...\settings\Business1` | `...\logs\Business1` |

同步根不用猜，在注册表里：

```powershell
Get-ItemProperty 'HKCU:\Software\Microsoft\OneDrive\Accounts\Personal' | Select-Object UserFolder,UserEmail
```

`settings\<账户>\` 关键文件：

| 文件 | 说明 |
|---|---|
| `SyncEngineDatabase.db`（+`-wal`/`-shm`）| 引擎主库（SQLite）；个人 1.43 GB、公司 724 MB（本机实测）|
| `OCSI.db` | 协同编辑库 |
| `SafeDelete.db` | 安全删除/回收站映射 |
| `uploads.txt` / `downloads3.txt` | 遗留小状态文件，全 0 不代表没队列 |

---

## 2. 一分钟体检（只读）

### 2.1 用仓库工具（推荐）

```bash
python od_state.py --pretty      # JSON：版本、进程、每账户计数
python od_audit.py --details     # 队列明细：孤儿/有效、重试次数、最老的条目
python summary.py                # 一屏结论
```

三个工具都会自动发现账户、同步根和日志路径，不需要改代码；Windows 账户名
从注册表读。

### 2.2 手工看数字

```powershell
$p = "$env:LOCALAPPDATA\Microsoft\OneDrive\logs\Personal\SyncDiagnostics.log"
Select-String -Path $p -Pattern '^(ChangesToProcess|FilesToDownload|BytesToDownload|numFileInWarning|syncStallDetected|uptimeSecs|fullScanCount) ='
```

字段语义（实测）：

| 字段 | 含义 |
|---|---|
| `ChangesToProcess` | **待处理变更数 = postponed 表总行数**（本次 201 = 183 文件 + 18 文件夹，精确吻合）|
| `FilesToDownload` / `BytesToDownload` | 其中需要下载内容的条目数与字节数 |
| `numFileInWarning` | GUI「需要注意」的条目（真正报错的），本次 = 0 |
| `syncStallDetected` / `scanStateStallDetected` | 引擎自认是否停摆；**卡在 postponed 时通常仍是 0** |
| `uptimeSecs` / `fullScanCount` | 引擎运行时长 / 已完成的全量扫描次数；升级后 `fullScanCount` 从 0 开始 |
| `pid` | 对上哪个 `OneDrive.exe` |

> 日志会被原地重写，有时只剩 `Sync Diagnostics - Sync Verification` /
> `Aborting sync verification`，没有 Diagnostics 段。此时用 postponed 表行数，
> 它与 `ChangesToProcess` 是同一个数字，只慢一拍。

### 2.3 看明细（SQLite，必须只读）

Python 自带的 sqlite3 就够（仓库工具已经封装好）；也可以用平台工具里的
`sqlite3.exe`。手工查询时务必加 `-readonly`：

```powershell
$sq = "$env:LOCALAPPDATA\Microsoft\OneDrive\settings\Personal\SyncEngineDatabase.db"
# 示例：只读打开后执行
SELECT COUNT(*) FROM od_ClientFilePostponedChange_Records;
SELECT COUNT(*) FROM od_ClientFolderPostponedChange_Records;
```

**判死三条**（与 macOS 一一对应）：

1. `postponedCount` 是**累计重试次数**，五位数（本次 25,358–26,350）说明已被
   反复重试上万次；
2. `od_ClientFile_Records` 里**查不到**这些 `resourceID`（本次全部 183 条都是）
   → 孤儿单；
3. `parentResourceID` 在 `od_ClientFolder_Records` 里也不存在 → 连父目录记录
   都被换了新 ID。

三条同时成立 = **永远不会成功的死单**，删掉不影响云端与本地内容。

---

## 3. 本次实测（2026-11-14）

引擎在升级后首次全量扫描（累计 CPU 1,070 s），状态如下：

| 指标 | 个人 | 公司（Business1）|
|---|---|---|
| 引擎记录 文件 / 文件夹 | 1,220,747 / 99,010 | 804,715 / 59,218 |
| `ChangesToProcess` | **201** | **0** |
| `FilesToDownload` / `BytesToDownload` | **3 / 56,218** | 0 / 0 |
| postponed 行数（文件 / 文件夹）| **183 / 18** | 0 / 0 |
| 累计重试（`sum(postponedCount)`）| **4,761,436**（183 文件）+ **469,327**（18 文件夹）| 0 |
| 最早 / 最晚 `timeAdded` | 2025-06-10 / 2025-07-21 | — |
| `numFileInWarning` | 0 | 0 |
| 结论 | **卡住（无害但永久）** | **干净** |

### 3.1 卡的是什么

全部指向同步根下一个 Python 项目（带 `.git` 与 `__pycache__`）里的 `.py` 与
`__pycache__/*.pyc`：共 **62 个不同文件名 / 61 个不同 resourceID**；涉及文件夹
只有 `cropper`、`web`、`__pycache__`×3。

两类行：

| 类型 | 行数 | 特征 |
|---|---|---|
| `changeType=4, flags=0` | **122** | `fileName` 空、`eTag` 空、`size`/`timeAdded` 全 NULL —— **纯僵尸行** |
| `changeType=5, flags=4` | **61** | 有真实文件名/eTag/size，`lastChange` = 2025-06-10 ~ 07-21 —— **旧账单** |
| 文件夹 `changeType=1`（建/provision）| 5 | 同一批旧 ID |
| 文件夹 `changeType=2`（删）| 13 | `folderName` 空，指向同一批旧 ID |

### 3.2 为什么永远修不好

- 磁盘上文件**都在**（`.pyc` 后来还重新生成过，属性 `Archive|ReparsePoint|Pinned`，
  占位符可用）；
- 引擎侧同一批对象**已换新 resourceID 重新登记**（文件夹现有记录是新号段）；
- 而 201 条变更单仍挂在**旧父目录 ID** 上，该记录已被丢弃 → 引擎每次拿到单子
  都找不到对象，只能 `postpone`，一挂就是 **16 个月**；
- 结果是个人账户**永远显示「正在同步 201 个更改 / 3 个文件」**，每次全量重扫
  都会重新挂上，CPU 白烧。

**不会丢数据、不会重复上传、云端无影响**；与 macOS 那种「扩展每 30–60 秒重启
+ CPU 80%」的死循环不是一回事。

---

## 4. 修法（只删引擎侧死单）

> 原则：**只删 `od_*PostponedChange_Records` 里的孤儿行，不动
> `od_ClientFile_Records` / 云端 / 磁盘**，因此不会产生云端删除。

### 4.1 备份 + 停引擎

```powershell
$od = "C:\Program Files\Microsoft OneDrive\OneDrive.exe"
& $od /shutdown
Start-Sleep 5
Get-Process OneDrive -ErrorAction SilentlyContinue      # 应为空

$s = "$env:LOCALAPPDATA\Microsoft\OneDrive\settings\Personal"
$bk = "$env:USERPROFILE\od-db-backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
New-Item -ItemType Directory -Force $bk | Out-Null
Copy-Item "$s\SyncEngineDatabase.db*" $bk -Force
Get-FileHash "$bk\SyncEngineDatabase.db" -Algorithm SHA256
```

### 4.2 先证明「全是孤儿」，再删

用工具会自动做这一步，并默认只删孤儿、保留仍能解析的行：

```bash
python od_fix_postponed.py Personal           # 预览：行数、重试、保留的 live 行
python od_fix_postponed.py Personal --apply   # 引擎已停后执行：备份 + TSV 审计 + 事务删除
python od_fix_postponed.py Personal --apply --all   # 连「record 仍在」的旧单一起删
```

手工 SQL 等价版本：

```sql
-- 必须为 0 才能整表删；不为 0 就按 rowid / resourceID 精确删
SELECT COUNT(*) FROM od_ClientFilePostponedChange_Records p
 WHERE EXISTS (SELECT 1 FROM od_ClientFile_Records f
               WHERE f.resourceID = p.resourceID);

BEGIN;
DELETE FROM od_ClientFilePostponedChange_Records;      -- 本次 183 行
DELETE FROM od_ClientFolderPostponedChange_Records;    -- 本次 18 行
COMMIT;
PRAGMA wal_checkpoint(TRUNCATE);
```

本次 18 条文件夹单里有 **3 条 resourceID 仍有效**（`changeType=2` 删除单，重试
1,207–1,209 次，指向一个 16 个月前已删除的文件夹）。这类旧删除单同样永远不会
完成，所以本次用 `--all` 一并清掉；工具默认会保留它们并在预览里列出来。

### 4.3 重启 + 验收

```powershell
Start-Process $od
# 90 秒后
python od_audit.py Personal
```

合格标准：`ChangesToProcess = 0`、`FilesToDownload = 0`、
`numFileInWarning = 0`，GUI 显示「已是最新」，且重启后的全量重扫结束时仍为 0。

### 4.4 回滚

用 §4.1 的备份覆盖 `SyncEngineDatabase.db`（连同 `-wal`/`-shm`）→ 重启。删单本身
不丢内容，回滚只是把 201 条死单放回去（会重新卡）。

### 4.5 备选（更温和，可先试）

把出问题的项目目录整个**先移出同步根**，等引擎把这批对象全部 tombstone 掉，再
视需要移回。代价是文件要重新上传一次，但能顺带把「构建产物/缓存目录放进
OneDrive」这个根因一起解决。

### 4.6 2026-11-24 执行记录

| 步骤 | 结果 |
|---|---|
| 停引擎 | `OneDrive.exe /shutdown` → 两进程全部退出 |
| 备份 | `SyncEngineDatabase.db` 1.37 GB，SHA256 与源库一致 |
| 只读验证 | 文件单 183（changeType=4 共 122 纯僵尸 / 5 共 61 旧账单），`files_still_exist=0`；文件夹单 18（7 个不同 resourceID），其中 3 条仍有效 |
| 审计清单 | 201 行 TSV（resourceID / changeType / postponedCount / fileName / timeAdded）|
| 删除 | 两表 DELETE + `wal_checkpoint(TRUNCATE)` + `quick_check = ok` |
| 重启验收 | 90 秒后 `ChangesToProcess = 0`、`FilesToDownload = 0`、`numFileInWarning = 0`、`syncStallDetected = 0`（全量重扫进行中）|

---

## 5. 与 macOS 的差异

| 维度 | macOS | Windows |
|---|---|---|
| 主库 | `dbfs.db`（Group Container）+ `SyncEngineDatabase.db` | 只有 `%LOCALAPPDATA%\Microsoft\OneDrive\settings\<账户>\SyncEngineDatabase.db` |
| 队列表名 | `od_ClientFilePostponedChange_Records` 等 | 同 |
| 「未同步」口径 | `dbfs_mftrecord WHERE tombstoned=0 AND ph_insync=0` | **`SyncDiagnostics.log` 的 `ChangesToProcess`** |
| 目录树记录 | `dbfs_mftrecord` + `dbfs_changerecord` | `od_ClientFile_Records` / `od_ClientFolder_Records` |
| 典型症状 | 扩展每 30–60 秒重启、CPU 80%+；或引擎停工 | 队列不降、CPU 空转；**不会**重启扩展 |
| 停引擎 | `osascript ... quit` + `pkill` | `OneDrive.exe /shutdown` |
| 日志 | `~/Library/Logs/OneDrive/*.log`（文本）| `SyncEngine-*.odl`（**二进制，1 MB 滚动**）+ `SyncDiagnostics.log`（文本，**这才是宝**）|
| 健康计数 | `ph_insync=0` 个位数（1 是系统 temp 记录）| `ChangesToProcess=0`（没有「永远剩 1」的 temp 记录）|
| 铁律 | 不降级、不跑 Reset 脚本 | 同；额外：**别在引擎运行时热改 `-wal` 库，先 `/shutdown`** |

工具对应关系：

| 用途 | macOS | Windows |
|---|---|---|
| 体检 | `od_audit.py` | `od_audit.py`（自动切换）|
| 删死单 | `od_tombstone.py` / `od_fix_records.py` | `od_fix_postponed.py` |
| 本地-only 清单 | `od_backup_list.py` | 用 `robocopy <同步根> <目标> /MIR /XJ` |

---

## 6. 预防

1. `__pycache__` / `.pyc` / `.venv` / `node_modules` / `.git` **永远不放
   OneDrive**；本次罪魁就是带 `.git` + `__pycache__` 的 Python 项目。
2. 客户端自动升级后**当天**跑一次 §2.1 体检（本机 11-13 更新，11-14 首扫就把
   2025 年 6 月的旧账翻了出来）。
3. Windows 没有「按子目录排除同步」的能力，去重只能靠选择同步的顶层文件夹，
   或把工程移出同步根。
4. 一两个月跑一次 §2.1；`ChangesToProcess` 到两位数且不降就要处理。
5. 删除/改名大目录要在同步健康时做，做完等队列归零再关机。

---

## 7. 验收

- `ChangesToProcess = 0`、`FilesToDownload = 0`、`numFileInWarning = 0`
- GUI 显示「已是最新」
- 重启后的全量重扫结束仍为 0（`fullScanCount` 已递增）
- `PRAGMA quick_check` = `ok`
- 新建一个测试文件，一分钟内出现在云端

本次修复后：201 → 0（90 秒内），内容零改动，201 行审计清单与数据库备份均已保留。
