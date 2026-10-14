# 程序内更新（从源码编译）

Keyrealm 可以在程序内检查并一键更新：应用内从 GitHub 查最新源码、提示有更新，用户确认后
**关闭程序 → 备份旧产物 → 从源码完整重编译 → 逐级迁移数据库 → 自动重启**。

> **适用范围**：面向**源码运行**的部署（程序目录是一个已 clone 的 git 仓库，`.git` 与产物同在）。
> 用打包版 `Keyrealm.exe` 运行的，请从 GitHub **Releases** 获取新版。程序内更新**不修改**你未提交的
> 本地改动（见 [§4](#4-更新主流程)）。更新**方式目前只有「从源码编译」**一种。

---

## 1. 两种更新档

| 档位 | 比较基准 | 判定「有更新」 |
|---|---|---|
| **正式版** | `APP_VERSION` | 远端版本号**更高** |
| **测试版** | `APP_VERSION` + commit | 版本号 **或** commit **任一不同** |

两档都跟踪 **`main`** 分支。渠道选择存在 `settings.update_channel`，在「设置 → 更新」里切换。

- **正式版**适合日常使用：只有作者把版本号提上去（发版）才提示更新。
- **测试版**每次都跟到 `main` 最新提交——只要远端有新 commit 就提示，适合跟开发进度。
  源码运行且工作区有未提交改动时，本地 commit 会带 `-dirty`；比较时**忽略** `-dirty` 后缀，
  因此「只改了自己本地文件、没拉新提交」不会误报。

---

## 2. 在应用里更新

**设置 → 更新** 卡片：

1. 选**更新渠道**（正式版 / 测试版）。
2. 点**检查更新**：只读，扫描 GitHub 上 `main` 的最新版本（不改动本机源码）。
3. 若有更新 → 显示「发现新版本 X」+ **立即更新**按钮；同时若数据库结构有变动，提示
   「将逐级迁移 X → Y」。
4. 点**立即更新** → 二次确认 → **程序关闭**，在一个终端窗口里执行更新，完成后**自动重启**。
   卡片会显示「上次更新」的结果（新版本启动后读取）。

> 应用内这一步是**交棒**：后端先把更新脚本起成一个**脱离进程**，确认起来了，才关闭本程序。
> 顺序不可颠倒——先关程序就没机会再起更新进程了。

---

## 3. 命令行更新（四个独立脚本）

四个能力各自独立、**无交互**、可单独运行。逻辑只写一份 Python（`backend/updater.py`），
`.sh` / `.bat` 只是**薄引导**（找 Python；缺 `.venv` 就自动 `python -m venv` 并安装
`requirements.txt` + `pyinstaller`，全程不问话）。

| 能力 | Linux/macOS | Windows | 底层脚本 |
|---|---|---|---|
| 版本检查 | `scripts/check_version.sh` | `scripts/check_version.bat` | `check_version.py` |
| 构建更新 | `scripts/build_update.sh` | `scripts/build_update.bat` | `build_update.py` |
| 数据库更新 | `scripts/db_update.sh` | `scripts/db_update.bat` | `db_update.py` |
| 程序重启 | `scripts/restart.sh` | `scripts/restart.bat` | `restart.py` |

> 已经手动维护了 `.venv` 的话，直接 `python scripts/check_version.py …` 也行；`.sh`/`.bat`
> 主要是给「环境不确定 / 缺依赖」的场景兜底。
> **Windows 的 `.bat` 分支未经实测**（见 [§9](#9-边界与已知限制)）。

### 常用参数

```bash
# 检查更新（正式/测试；--json 便于脚本消费）
scripts/check_version.sh --kind formal
scripts/check_version.sh --kind test --json

# 一键更新（备份→取源码→编译→迁移→重启）；先看演练
scripts/build_update.sh --dry-run
scripts/build_update.sh                      # 完整流程
scripts/build_update.sh --no-restart         # 到迁移为止，不重启
scripts/build_update.sh --no-frontend        # 跳过 npm 前端构建
scripts/build_update.sh --force              # 工作区有改动时先 git stash 再合并
scripts/build_update.sh --rollback           # 从最近一次备份恢复
scripts/build_update.sh --rollback <zip>     # 从指定备份恢复

# 数据库逐级迁移（默认只演练）
scripts/db_update.sh --list
scripts/db_update.sh                         # 演练：打印将执行的迁移链
scripts/db_update.sh --apply                 # 真正写库

# 重启
scripts/restart.sh
scripts/restart.sh --wait                    # 先等应用完全退出再重启
```

> 离线自测：环境变量 `KEYREALM_UPDATE_REMOTE` 可覆盖远端地址（指向本地 bare 仓库即可，
> 不联网也能验证检查/更新逻辑）。

---

## 4. 更新主流程

`scripts/build_update.py` 依次执行（每步都写 `data/.update-status.json`）：

```
0  等应用退出          # 轮询单实例锁；拿不到锁=应用还在跑
1  读更新请求          # data/.update-request.json（渠道/是否重启/重启命令）
2  备份                # 可执行文件 + plugins/ + data/ → backups/ 单个 zip
3  取源码              # git fetch --depth 1 origin main → git merge --ff-only
4  构建                # <venv-python> scripts/build.py --frontend
5  数据库迁移          # 若有结构变动，逐级应用
6  重启                # 脱离终端启动构建出的可执行程序
```

- **单实例锁**贯穿全程：更新期间占住锁，**谁都起不了应用**，避免「边更新边运行」打架；
  重启新实例**之前**才释放。
- **构建产物默认落在仓库根**（`.git` 同级），即源码运行时的程序目录（`scripts/build.py`
  的 `--output` 可改）。

### 失败与回滚

- **构建失败**：不覆盖旧产物（`assemble_release` 是收尾步）；覆盖前会先留一份
  `Keyrealm.old`，迁移/重启阶段失败则据此还原。脚本报错并指向 `backups/` 里的备份。
- **取源码失败**（工作区分叉 / 含未提交改动）：`--ff-only` + 脏树拒绝，**不覆盖**你的改动；
  要强制前进用 `--force`（先 `git stash`）。
- **迁移失败**：该跳事务回滚 + 还原该跳的数据库快照，中止，保留整树备份。
- **整树回滚**：`scripts/build_update.sh --rollback`（或手动解压 `backups/` 里那份 zip）。

---

## 5. 备份与恢复

- **内容**：可执行文件 + `plugins/` + `data/`（数据库走 SQLite 一致性快照；`logs/` 排除）。
- **位置**：`backups/keyrealm-backup-<渠道>-<版本>-<时间戳>.zip`。
- **只保留一份**：写新备份前会删掉旧的，目录里**恒定只有一份**。
- **⚠ 含真实密钥**：`data/` 里有 `credentials.db` 与明文密钥文件；备份 zip 因此**含明文**。
  它以 `0600` 落盘、目录已被 `.gitignore` 忽略，请自行妥善保管、勿外传。
- **恢复**：`scripts/build_update.sh --rollback` 会把备份解压覆盖回程序目录；也可手动解压。

---

## 6. 数据库逐级迁移

结构与接口详见 **`scripts/migrations/README.md`**。要点：

- 迁移脚本命名 `scripts/migrations/m_<from>__to_<to>.py`，**只提供「低一级 → 高一级」**
  （如 `m_1.0.0__to_1.1.0.py`），每个暴露 `FROM_VERSION`/`TO_VERSION`/`preview(conn)`/`apply(conn)`。
- 运行器把链**一层层串起来**升（1.0.0→1.1.0→1.2.0），**绝不跨级**——跨级脚本随版本增多会组合
  爆炸、无法维护；逐级脚本每份只管相邻两版。
- 每跳前留 SQLite 快照；`--apply` 前抢单实例锁；**缺链会点名缺哪一跳**，不会硬升。
- **改了数据库结构必须**：① 写 `m_<old>__to_<new>.py`；② 提升 `backend/version.py` 的
  `SCHEMA_VERSION`。（结构版本与应用 `APP_VERSION` 是两套号。）

---

## 7. 更新时产生的文件

| 路径 | 作用 | 是否入库 |
|---|---|---|
| `backups/*.zip` | 更新前备份（含密钥） | 忽略 |
| `data/.update-request.json` | 应用交给更新进程的请求（渠道/重启命令） | 忽略（`data/`） |
| `data/.update-status.json` | 更新进度与结果，重启后由新应用读取展示 | 忽略（`data/`） |
| `data/logs/update-*.log` | 无终端时的更新输出 | 忽略 |
| `Keyrealm` / `Keyrealm.exe` / `Keyrealm.old` | 构建产物与回滚副本 | 忽略（`Keyrealm*`） |

---

## 8. 故障排查

| 现象 | 原因与处理 |
|---|---|
| 检查更新报「未安装 git」 | `check_update` 依赖 `git`（用浅克隆读远端版本）。装 git 后重试。 |
| 检查更新报「无法访问远端仓库」 | 网络不可达。检查网络/代理；也可用 `KEYREALM_UPDATE_REMOTE` 指向可达的镜像。 |
| 提示有更新但「立即更新」不可用 | 当前不是源码仓库（无 `.git`）。请从 Releases 获取分发包，或在源码目录里运行。 |
| 更新卡在「工作区有未提交改动」 | 有本地改动。先 commit/stash，或加 `--force`（会先 `git stash` 再合并）。 |
| 构建失败 | 多为缺依赖 / 缺 npm 或 `frontend/node_modules` / `build.py` 的建表自检未过。看终端输出；`.sh`/`.bat` 会自动补 Python 依赖（但不含 Node）。 |
| 迁移报「迁移链断档」 | 目标结构版本与现有脚本之间缺一跳。补齐 `m_<缺的from>__to_*.py` 或核对 `SCHEMA_VERSION`。 |
| 迁移报「检测到应用正在运行」 | 应用没完全退出。先退出应用再 `--apply`。 |
| 重启后没起来 | 看 `data/logs/relaunch-*.log`；或手动 `scripts/restart.sh`。 |

---

## 9. 边界与已知限制

- **打包版 exe 无源码**（目录里没有 `.git`）：**检查更新仍可用**，但**更新禁用**并提示从
  Releases 获取。本功能面向源码运行。
- **Windows 的 `.bat` 与真机端到端未经实测**（开发机为 Linux/WSL）。逻辑已对齐 `.sh`；
  首次在 Windows 上使用请按下节清单验收。
- **PyInstaller 不能跨平台编译**：要产出 Windows exe 必须在 **Windows** 上构建；Linux 上构建出的
  是 Linux 可执行文件。
- **备份含明文密钥**（见 [§5](#5-备份与恢复)）。
- **无 Node/npm** 时 `--frontend` 会失败（引导只装 Python 依赖）；可 `--no-frontend` 前提是已有
  `frontend/dist`。

---

## 10. Windows 手工验收清单（首次使用）

1. `py -3 scripts\_bootstrap.bat` → 建出 `.venv\Scripts\python.exe` 并装好依赖。
2. `scripts\build_update.bat --dry-run` → 打印将执行的步骤，中文不乱码。
3. 启动应用 → 设置 → 更新 → **检查更新** → 有更新时点**立即更新**。
4. 确认：弹出**独立 CMD 窗口**执行更新；程序自己关闭；窗口里能看到备份/编译/迁移进度。
5. 构建产物 `Keyrealm.exe` 落在**仓库根**；`backups\` 里有且仅有一份 zip。
6. 更新完成后程序**自动重启**；设置页「上次更新」显示结果。
7. 应用运行期间再跑一次 `scripts\db_update.bat --apply` → 应被单实例闸门拒绝。
