# 统一 update 客户端

`update` 分为检测、计划展示、软件确认与执行、系统独立确认与执行四个阶段。检测不会安装应用、激活 Home Manager 或部署系统。它会访问官方发布元数据、刷新 chat 的 APT 索引、下载 Nix 求值所需的源/元数据；如果已有 chat 容器已停止，会启动它以检查包版本。

## 安装与使用

当前系统无需等待新系统镜像：

```bash
bash updates/install-update.sh
update --check
update
```

安装器将新客户端放入 `~/.local/lib/zirconium-updater/`，原子切换 `~/.local/bin/update` 和 `opt-updater`，保留第一次安装前的两个入口为 `*.before-unified-update`。安装器自身不执行软件更新。派生镜像也内置 `/usr/bin/update`；用户目录的版本优先于系统版本，需要更新客户端时重新运行安装器。

```bash
update                       # 默认检测全部已安装的受支持目标，展示变更与系统倒计时
update code xmcl              # 仅更新这两个软件
update code,localsend         # 也支持逗号
update wechat feishu          # 仅更新 chat 容器里的微信和飞书
update chat:apt               # 仅更新 chat 容器的 APT 软件包
update chat                   # APT + QQ、微信、飞书、腾讯会议
update home-manager nix       # Home Manager 配置事务 + Nix 自身
update system                 # 只处理已发布、已满 14 天的系统更新
update --check                # 只检查，不安装
update --check code system    # 只检查指定目标
update --only code            # 兼容旧参数；可重复使用 --only
update --list                 # 所有目标及别名
```

输入拼错或选择未安装的目标会报错；不会悄悄退回全部更新。多个目标去重。没有 `--yes` 绕过确认；非交互终端不会授权安装。`--notify` 隐含只检查，适合用户定时器。旧 `opt-updater --check --notify` 定时器保留原来的本地应用/chat 范围，不把较慢的 Home Manager 求值加入原来的八分钟服务；系统通知继续由已有的独立通知服务负责。

### 支持范围

| 目标 | 更新单位及展示内容 |
| --- | --- |
| `code` / `vscode` | `~/.local/opt/vscode/current` 管理的 VS Code，版本与发布说明链接 |
| `xmcl`、`localsend` | 现有 `~/.local/opt` 安装，固定发布资产、SHA-256 和版本 |
| `chat`、`chat:apt` | 现有 `chat` Distrobox，逐个列出 APT 包版本；固定已求解的版本集合 |
| `qq`、`wechat`、`feishu`、`wemeet` | 对应官方 DEB 单独选择，下载后再次验证包名、架构、版本和厂商提供的摘要 |
| `home-manager` / `hm` | 整个 flake 的锁文件与 generation，逐项列出包版本、构建变化及 flake revision |
| `nix` | 本机的 Determinate Nix，精确指定已展示版本，单独 sudo 授权 |
| `system` / `os` | 本仓库正式系统镜像，基础镜像、定制 RPM、FlClash、构建配置与依赖快照变化 |

Home Manager 共享 `nixpkgs` 和 `flake.lock`，因此是一个声明式事务，不能像独立安装的应用一样只替换其中的 kitty 而声称其他包不变。请用 `update home-manager` 查看所有受影响的软件再确认；`update kitty` 或 `home-manager:kitty` 会明确报错。系统里的 FlClash、Waydroid 和 KVM RPM 同样随完整系统镜像更新。当前不接管 Homebrew、Flatpak、独立 Nix profile 或 Waydroid 的 Android 镜像；它们不属于旧客户端的管理范围。非 Determinate 的 Nix 安装会明确报告不支持，而不会误用升级命令。

### 进度

- 本地应用与官方 DEB：实际下载字节、百分比、MiB、速率；没有 Content-Length 时显示已传输字节和“总量未知”。摘要校验完成才显示下载 100%。
- tar 软件安装：按实际解包字节显示百分比；完成后原子切换 `current`，保留旧安装。AppImage 无解包阶段，校验完成后直接原子激活。
- Home Manager：解析 Nix 的结构化活动，显示每个 store path 的下载/写入字节百分比、已完成任务数和构建阶段；自定义构建和激活脚本没有可靠总量时显示真实日志。总任务数可能随依赖发现而增长。
- APT 和 bootc：保留终端及包管理器原生下载、安装、镜像层进度。Nix daemon 升级保留官方升级器日志。后端未提供总量的步骤不显示虚构的平滑进度条。
- 每个软件显示 `[当前项/总项数]`、开始、完成或失败；检测较慢时每 20 秒报告仍在检查。

### 系统观察期与权限

从云端候选**构建成功**开始算 14 × 24 小时，不从 merge 开始。未成功构建的提交没有可计算的候选倒计时。每个候选独立计时；默认调用展示各自剩余天/小时/分钟。达到 14 天却尚未被 workflow 发布到 stable 的候选显示“等待云端发布”，不会提前安装。候选差异的第一项对比本机，其后对比上一候选，避免重复输出相同软件清单。基础镜像只有摘要变化时会如实显示摘要，不推测无法获得的上游逐包变更。

软件全部成功后，系统再次单独询问确认，再执行 `sudo -k -v`，要求新的 sudo 认证。程序再次验证云端正式版本、观察期、阻止状态和本机待重启部署；状态发生变化则要求重新预览。`sudo` 是否必须输入密码仍由本机 sudoers 决定。拒绝第一次软件确认会结束本次操作；软件检测或执行有失败时跳过系统，可修复后单独运行 `update system`。

为确保安装的是预览中的镜像，使用 `bootc switch ghcr.io/所有者/仓库@sha256:...` 固定 digest，避免拉取期间 stable 标签移动。以后继续通过 `update` 获取云端 stable；直接运行 `bootc upgrade` 不会跨越已固定的 digest。已有 staged 部署不会被覆盖，不自动重启。切换到其他仓库或 testing 的机器会明确报错，需要先由管理员选择本仓库来源。

### Home Manager 配置与恢复

默认使用 `~/.config/home-manager#bootstrap`。其他输出可以配置 `~/.config/zirconium-updater/config.json`：

```json
{
  "repository": "sisanwu12/zirconium-custom",
  "home_manager_dir": "~/.config/home-manager",
  "home_manager_output": "ssw12"
}
```

配置路径遵循 `XDG_CONFIG_HOME`，状态路径遵循 `XDG_STATE_HOME`。可以用 `--config 文件` 选择另一份配置。

检测在临时副本更新 lock、求值包列表和目标 generation，不修改原始配置。确认后构建同一副本，校验输出路径没有改变，再执行该 generation 的激活脚本。只有激活成功才写回此次 lock。预览后原配置或已激活 generation 被别的进程修改会中止。当前支持源码位于同一配置目录的本地 flake；目录外相对路径、自定义首次部署等不自动迁移，求值失败会保留原配置。

上一次 generation 路径及锁文件保存在 `~/.local/state/zirconium-updater/home-manager-previous-*`，已有 Home Manager generations 也继续保留。激活失败可能已经执行部分用户定义的激活步骤，程序会报告失败，不假定能回滚这些任意脚本。可通过 Home Manager 已有 generation 恢复，并根据备份恢复锁文件。

软件失败不会阻止后续独立软件执行，但最终退出码为 1；中断返回 130。没有跨所有包管理器的整体回滚。并发检查/更新由同一用户状态目录中的文件锁互斥。重新执行 `update` 会重新检测当前状态，不复用失效计划。

恢复旧入口：

```bash
cp -P --remove-destination ~/.local/bin/update.before-unified-update ~/.local/bin/update
cp -P --remove-destination ~/.local/bin/opt-updater.before-unified-update ~/.local/bin/opt-updater
```

## 代码结构与验证

- `client/cli.py`：目标解析、互斥锁、计划展示、确认顺序、失败汇总。
- `client/core.py`：计划模型、HTTPS 下载、校验、真实进度、子进程及交互。
- `client/local.py`、`chat.py`、`nix.py`、`system.py`：独立的 `available / plan / apply` 后端；计划保存具体版本/摘要，不在执行时重新选择 latest。
- `client/chat_sources.py`：从已有 chat 更新器迁入的厂商元数据解析。
- `client/nix_progress.py`：Nix 结构化进度转换。
- `notify.py`：保持系统通知只读，客户端复用其正式版本/14 天策略。
- `pipeline.py`：原有云端构建与放行流程；新增把构建输入嵌入镜像，供未来的本机差异比较。

```bash
python3 -m unittest discover -s updates/tests -v
bash -n updates/install-update.sh
python3 updates/update --list
python3 updates/update --check
```

测试使用临时目录和模拟后端，覆盖拒绝确认、非交互输入、系统执行顺序和重新认证、检测/安装失败、镜像版本变化、倒计时边界、坏包与哈希校验、原子入口、Home Manager 配置竞态及安装器备份。

后端协议参考：[Determinate Nix 升级](https://docs.determinate.systems/determinate-nix/determinate-nixd/)、[Nix flake update](https://nix.dev/manual/nix/stable/command-ref/new-cli/nix3-flake-update.html)、[Nix 活动协议](https://github.com/NixOS/nix/blob/master/src/libutil/include/nix/util/logging.hh)、[bootc switch](https://bootc-dev.github.io/bootc/man/bootc-switch.8.html)。
