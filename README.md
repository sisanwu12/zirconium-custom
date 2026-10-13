# zirconium-custom

基于官方 Zirconium 的 bootc 派生镜像。安装 KVM/QEMU 虚拟化组件、Waydroid 和 FlClash 0.8.99（x86_64），FlClash RPM 下载后进行 SHA-256 校验。

镜像提供空的 `/nix` 挂载点，本身不安装 Nix。更新镜像并重启后，使用 Determinate Nix Installer 的 `ostree` planner 安装 Nix；安装器会将持久可写的 `/var/lib/nix` 绑定到 `/nix`。安装后可确认挂载：

```bash
findmnt /nix
systemctl status nix.mount
```

如果 `findmnt` 没有输出，先通过 `journalctl -u nix.mount -b` 检查原因。

推送 `main` 中的 Containerfile 或 workflow 后，GitHub Actions 会构建并发布到 `ghcr.io/sisanwu12/zirconium-custom:latest`；也可以在 Actions 页面手动运行。请先确认构建成功，再在目标机器上切换：

```bash
sudo bootc switch ghcr.io/sisanwu12/zirconium-custom:latest
sudo reboot
```

FlClash 的 TUN 仍需在程序内启用并完成其授权流程。本仓库不包含代理订阅或凭据。宿主系统的 `LockLayering=true` 无需修改。

**访问权限：** 若 `bootc switch` 提示未授权，请在 GitHub 的 Package settings 将镜像设为 public，或按 bootc 文档为宿主机配置 GHCR registry 凭据。不要将长期有效的 GitHub token 写进本仓库或命令行历史。

更新 FlClash 时，同时修改 Containerfile 的版本与来自官方 Release 的 SHA-256；官方 Zirconium 更新后可手动重新运行 workflow。确认新镜像构建成功后执行 `sudo bootc upgrade` 并重启。出现问题可以用 `sudo bootc rollback` 回退。

## KVM / QEMU 虚拟机

镜像包含 QEMU/KVM、libvirt、virt-manager 图形管理器、virt-install / virsh 命令行工具和 virt-viewer。另包含 OVMF UEFI 固件、swtpm 虚拟 TPM（可用于 Windows 11）和 virtiofsd 目录共享组件。KVM 内核模块由官方 Zirconium 内核提供；宿主机 BIOS/UEFI 需要开启 AMD-V/SVM 或 Intel VT-x。

采用 Fedora 的 [libvirt 模块化服务](https://libvirt.org/daemons.html#modular-driver-daemons)，镜像启用 `virtqemud.service` 以支持虚拟机开机自启，并启用 QEMU、网络、设备、网络过滤、密钥、存储以及日志和锁管理的本地 socket。普通用户通过系统默认的 polkit 授权连接；在桌面弹出的认证窗口中输入管理员密码。

### 更新后检查

构建并发布新镜像后，执行 `sudo bootc upgrade` 并重启（首次切换镜像按上面的 `bootc switch` 步骤）。然后检查：

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
