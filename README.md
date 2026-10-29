# zirconium-custom

基于官方 Zirconium 的 bootc 派生镜像。安装 KVM/QEMU 虚拟化组件、Waydroid 和 FlClash（x86_64），FlClash RPM 下载后进行 SHA-256 校验。自动构建跟随 FlClash 正式版；直接本地构建的默认版本为 0.8.99。

镜像提供空的 `/nix` 挂载点，本身不安装 Nix。更新镜像并重启后，使用 Determinate Nix Installer 的 `ostree` planner 安装 Nix；安装器会将持久可写的 `/var/lib/nix` 绑定到 `/nix`。安装后可确认挂载：

```bash
findmnt /nix
systemctl status nix.mount
```

如果 `findmnt` 没有输出，先通过 `journalctl -u nix.mount -b` 检查原因。

GitHub Actions 每六小时检测一次；官方基础镜像、定制软件版本或构建文件变化时才构建候选镜像。候选镜像构建成功满 **14 天**后，原样发布到 `stable` 和 `latest`；发布时不重新构建。首次启用这套流程，需要先等待第一个候选满 14 天。原有的 `latest` 在此之前保持原样。

第一次使用时，确认 Actions 已发布 `stable`，再在目标机器上切换：

```bash
sudo bootc switch ghcr.io/sisanwu12/zirconium-custom:stable
sudo reboot
```

FlClash 的 TUN 仍需在程序内启用并完成其授权流程。本仓库不包含代理订阅或凭据。宿主系统的 `LockLayering=true` 无需修改。

**访问权限：** 若 `bootc switch` 提示未授权，请在 GitHub 的 Package settings 将镜像设为 public，或按 bootc 文档为宿主机配置 GHCR registry 凭据。不要将长期有效的 GitHub token 写进本仓库或命令行历史。

已经跟踪本仓库 `latest` 的电脑可以继续使用它；该标签与 `stable` 使用相同的 14 天等待策略。

## 统一交互式更新

新版 `update` 先显示软件、Home Manager、Nix 和系统变更，再询问是否安装；系统候选显示预计发布倒计时，按满 14 天后的下一次计划检查再加 10 分钟估算，并显示本机时区的预计发布时间。实际以云端 stable 发布为准。系统更新最后单独确认并重新授权 sudo，不自动重启。

```bash
# 立即在当前用户安装新版入口（保留旧脚本备份，不更新软件）
bash updates/install-update.sh
update --check
update
update code xmcl              # 仅选择这些应用
update wechat feishu           # chat 容器里的单独应用
update home-manager nix        # Home Manager 完整配置和 Nix 自身
update system                  # 仅系统；仍需独立确认与 sudo
```

详细进度、更新范围、Home Manager 事务边界、配置、恢复和开发说明见 [统一 update 客户端](updates/README.md)。固定系统版本后仍通过 `update` 获取后续正式镜像；不要依赖直接 `bootc upgrade` 跨越固定 digest。

## 更新策略：自动检测和构建，通知后手动安装

### 检测范围

- 官方镜像：比较 `ghcr.io/zirconium-dev/zirconium:latest` 的 linux/amd64 manifest 摘要，并使用摘要固定本次构建的基础镜像。
- 定制 RPM：查询 [软件清单](updates/packages.txt) 中软件及其可解析的硬依赖的可用版本。包括 Waydroid、QEMU、libvirt、swtpm 等；变更后重新构建。检测只查询软件仓库，不在宿主系统安装软件。
- FlClash：检测官方 GitHub 最新正式 Release 的 amd64 RPM 版本和公开 SHA-256。构建时传入固定版本并校验下载文件；没有合适的 RPM 或公开校验值时，检测失败并保留现有版本，不跳过校验。
- 本仓库：Containerfile、updates 目录或 .dockerignore 内容变化时构建。修改软件清单后，无需在脚本中再维护一份名单。

定时检测仍会拉取基础镜像，并通过 BuildKit 的临时查询阶段读取 DNF 元数据，只导出版本文本；这可以避开 Docker 常规镜像存储对官方镜像层数的限制。输入没有变化时跳过定制镜像构建和推送。硬依赖检测可能包含多个可用提供者，最终安装版本由 DNF 求解；直接安装的软件按检测到的 NEVRA 固定。Nix 和 Home Manager 由电脑端统一 `update` 单独管理，不属于定制镜像 RPM。Flatpak、Homebrew 应用和 Waydroid 内部 Android 镜像仍按各自方式更新。

### 候选和发布通道

| 标签 | 用途 |
| --- | --- |
| `candidate-日期-运行编号` | 每次成功构建的留存版本，记录固定 digest 和完成时间 |
| `testing` | 最新候选，仅用于主动试用 |
| `stable` / `latest` | 已完成至少 14 天等待的最新未阻止候选，供日常手动更新 |

例如 A 在第 1 天完成构建，第 5 天又构建 B：第 15 天先放行 A，第 19 天才放行 B。B 不会重置 A 的等待时间。放行操作只是给 A 原有的镜像摘要添加正式标签，不会重新下载软件生成另一份镜像。实际发布可能因 GitHub 调度延迟或任务失败晚于 14 天。

等待时间是观察窗口，**不代表镜像已经经过两周实际运行测试，也不保证没有问题**。现有构建执行 bootc lint 和虚拟 TPM 初始化检查，不包含完整桌面或虚拟机启动测试。发现候选有问题时，在发布前到 Actions 手动运行 workflow，将其完整 `sha256:...` 填入 `block_digest`；它将不被放行。该输入不能撤回已经发布的镜像。若需要重新构建相同输入，可启用 `force_rebuild`，新候选重新等待 14 天。

更新记录保存到 GitHub 的独立 `update-state` 分支的 `updates.json`，包含候选摘要、软件版本、时间和正式通道记录。不要删除或重建该分支，否则会失去等待时间和阻止记录。历史条目保存在 git 历史中；当前文件保留近期记录、正在等待的候选和正式版本。不要删除仍在等待的镜像标签或镜像内容。

### 电脑端只通知

新镜像内置桌面用户定时器：登录后约 2–7 分钟检查，之后约每六小时检查。只有正式通道有不同于当前安装版本、已等待至少 14 天的镜像时才通知；同一镜像每天最多提醒一次。检查仅下载公开 JSON 元数据，不运行 bootc，不需要管理员权限，不下载系统镜像，也不安装或重启。

新镜像会 mask `uupd.timer`、`bootc-fetch-apply-updates.timer`、`rpm-ostreed-automatic.timer`，避免已知的系统自动更新任务后台准备部署。若当前系统已启用过自动更新，在第一次安装新镜像之前手动关闭：

```bash
sudo systemctl mask --now uupd.timer bootc-fetch-apply-updates.timer rpm-ostreed-automatic.timer
```

这不会撤销先前已经准备好的 staged 部署；先用 `sudo bootc status` 检查当前状态，避免把它误认为尚未安装的通知。

若想在当前系统上立即启用通知，无需等第一份新镜像，先在本仓库执行：

```bash
bash updates/install-notifier.sh
```

此脚本只安装用户级通知程序和定时器。新镜像安装后，用户级入口自动调用镜像内的新版本程序。默认通知源是本仓库；fork 使用者需要将安装脚本的用户服务添加 `--repository 所有者/仓库`，或安装已经写入自己仓库名的新镜像。

检查通知服务和手动检查：

```bash
systemctl --user list-timers --all zirconium-custom-update-notify.timer
journalctl --user -u zirconium-custom-update-notify.service -n 50 --no-pager
# 安装新镜像后：
zirconium-custom-update-check
# 当前系统尚未安装新镜像时：
python3 updates/notify.py
```

收到通知后，保存工作、妥善关闭虚拟机，再由自己执行：

```bash
update system
sudo bootc status
sudo reboot
```

确认系统更新并完成 sudo 授权后才下载并准备安装正式通道的镜像。统一客户端安装预览中的固定 digest，之后继续通过 `update` 检查本仓库正式发布。不要切到官方镜像或 `testing`。出现问题可执行 `sudo bootc rollback` 后重启；系统回退不等于回退用户数据。

### GitHub 设置和首次上线

1. 将这些修改提交并推送到 GitHub 的默认分支 `main`。本地修改本身不会启动云端任务。
2. 确认仓库启用了 Actions，并允许 workflow 的 `contents: write` 和 `packages: write`；workflow 会创建/维护 `update-state` 分支并发布镜像。
3. 仓库和 GHCR Package 需要公开，通知程序才能不保存凭据地读取元数据和更新。当前通知程序不支持私有仓库认证。
4. 手动运行一次 **Build Zirconium Custom**，在任务 Summary 中查看构建、等待和发布状态。首次候选成功后开始计时，满 14 天的下一次成功检测才会发布正式标签。
5. 公开仓库连续 60 天没有仓库活动时，GitHub 可能停用定时 workflow；定期查看 Actions 运行状态。不要把“没有通知”当作更新服务正常的证明。

流程只自动管理构建和发布；安装始终由用户决定。14 天内遇到需要立即修复的严重问题时，应另行评估是否主动使用具体候选版本，而不是假定等待总比及时修复安全。

## 默认终端（kitty）

镜像移除 foot、foot-server 的默认启用项及上游分发的 foot 配置，桌面终端入口优先选择 `kitty.desktop`。kitty 继续由用户的 Nix 环境管理；需确保 kitty 已安装，且 Nix profile 的 `share` 目录在 `XDG_DATA_DIRS` 中。

更新镜像并重启后，可通过 `xdg-terminal-exec --print-id` 确认输出为 `kitty.desktop`。Niri 的 `Mod+T` 和 `Shift+Mod+T` 会通过该入口启动 kitty。

## KVM / QEMU 虚拟机

镜像包含 QEMU/KVM、libvirt、virt-manager 图形管理器、virt-install / virsh 命令行工具和 virt-viewer。另包含 OVMF UEFI 固件、swtpm 虚拟 TPM（可用于 Windows 11）和 virtiofsd 目录共享组件。KVM 内核模块由官方 Zirconium 内核提供；宿主机 BIOS/UEFI 需要开启 AMD-V/SVM 或 Intel VT-x。

采用 Fedora 的 [libvirt 模块化服务](https://libvirt.org/daemons.html#modular-driver-daemons)，镜像启用 `virtqemud.service` 以支持虚拟机开机自启，并启用 QEMU、网络、设备、网络过滤、密钥、存储以及日志和锁管理的本地 socket。普通用户通过系统默认的 polkit 授权连接；在桌面弹出的认证窗口中输入管理员密码。

### 更新后检查

构建并发布新镜像后，执行 `update system` 并重启（首次切换镜像按上面的 `bootc switch` 步骤）。然后检查：

```bash
ls -l /dev/kvm
sudo virt-host-validate qemu
systemctl status virtqemud.socket virtnetworkd.socket
sudo virsh -c qemu:///system list --all
sudo virsh -c qemu:///system domcapabilities --virttype kvm
```

`domcapabilities` 应显示 `<domain>kvm</domain>`。`virt-host-validate` 的 `/dev/kvm`、硬件虚拟化等核心检查应为 PASS；IOMMU 用于设备直通，普通虚拟机无需为这一项的警告配置直通。

### 创建虚拟机

以普通桌面用户启动图形管理器：

```bash
virt-manager --connect qemu:///system
```

在“新建虚拟机”向导中选择安装 ISO、内存、CPU 和磁盘。连接使用 `qemu:///system`，网络选择 `default` NAT 网络。系统级虚拟机的默认磁盘位置为持久可写的 `/var/lib/libvirt/images`，该目录由 systemd tmpfiles 在开机时创建，系统镜像更新不会清除这些磁盘。安装 ISO 也建议放在此目录，避免 QEMU 无法读取用户主目录：

```bash
sudo install -m 0644 /path/to/installer.iso /var/lib/libvirt/images/installer.iso
sudo restorecon -v /var/lib/libvirt/images/installer.iso
```

`/path/to/installer.iso` 请替换为实际路径。创建 Windows 11 虚拟机时，在安装前勾选“自定义配置”，选择 UEFI 固件并添加 TPM 2.0 模拟器；VirtIO 磁盘和网卡所需的 Windows 驱动需要另行准备。

### 网络与故障排查

`libvirt-daemon-config-network` 提供 `default` NAT 网络及其自启配置，可检查：

```bash
sudo virsh -c qemu:///system net-list --all
```

如果 `default` 存在但处于 inactive 状态，执行：

```bash
sudo virsh -c qemu:///system net-start default
sudo virsh -c qemu:///system net-autostart default
```

默认网段为 `192.168.122.0/24`。如与已有网络冲突，在 virt-manager 的“连接详情 → 虚拟网络”中调整。启用 FlClash TUN 后若虚拟机无法联网，检查代理规则是否允许此网段和 `virbr0` 的流量。

若安装时报 `无法连接到 virtlogd`，并提示无法打开 `/var/log/libvirt/qemu/*.log`、权限不够，检查日志目录的 SELinux 标签：

```bash
ls -ldZ /var/log/libvirt /var/log/libvirt/qemu
sudo restorecon -RFv /var/log/libvirt
```

日志目录应为 `virt_log_t`，然后重试安装。镜像通过 systemd tmpfiles 在开机时创建日志目录并递归恢复其标签，同时修复已有目录。此修复无需重启 virtlogd。

若 Windows 11 的 TPM 初始化报 `swtpm_setup` 失败，查看 `sudo tail -n 80 /var/log/swtpm/libvirt/qemu/win11-swtpm.log`。若日志包含 `Could not create directory for 'statedir': Permission denied`，修复证书状态目录后重试：

```bash
sudo install -d -m 0750 -o tss -g root /var/lib/swtpm-localca
sudo restorecon -RFv /var/lib/swtpm-localca
```

镜像通过 systemd tmpfiles 创建该目录。证书和密钥在宿主机首次使用时生成，并持久保存在 `/var` 中。

若提示连接 socket 不存在，先检查是否已更新并重启，然后启用相关服务：

```bash
sudo systemctl enable --now virtqemud.service
for driver in qemu network nodedev nwfilter secret storage; do
  sudo systemctl enable --now "virt${driver}d.socket"
done
sudo systemctl enable --now virtlogd.socket virtlockd.socket
sudo journalctl -b -u virtqemud -u virtnetworkd
```

若 `/dev/kvm` 不存在，检查 BIOS/UEFI 的虚拟化开关，并按 CPU 厂商执行 `sudo modprobe kvm_amd` 或 `sudo modprobe kvm_intel`，再查看 `sudo journalctl -k -b`。若宿主本身运行在虚拟机内，还需要外层宿主启用嵌套虚拟化。

## Waydroid（无谷歌组件）

镜像包含 Waydroid 软件包及其依赖。Android 镜像在宿主机首次使用时下载到持久存储，不在构建阶段初始化。更新系统并重启后执行：

```bash
sudo waydroid init -s VANILLA
sudo systemctl enable --now waydroid-container.service
waydroid session start
```

然后另开一个终端运行：

```bash
waydroid show-full-ui
```

`VANILLA` 不包含谷歌组件。Waydroid 会话和界面命令以普通用户运行；需要当前桌面为 Wayland 会话。
