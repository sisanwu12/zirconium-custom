# zirconium-custom

基于官方 Zirconium 的最小 bootc 派生镜像。安装 FlClash 0.8.98（x86_64），RPM 下载后进行 SHA-256 校验。

镜像提供空的 `/nix` 目录，启动时由 `nix-bind.service` 将持久可写的 `/var/lib/nix` 绑定到该目录。镜像本身不安装 Nix。更新镜像并重启后，安装 Nix 前可确认挂载：

```bash
findmnt /nix
systemctl status nix-bind.service
```

如果 `findmnt` 没有输出，先通过 `journalctl -u nix-bind.service -b` 检查原因。

推送 `main` 中的 Containerfile 或 workflow 后，GitHub Actions 会构建并发布到 `ghcr.io/sisanwu12/zirconium-custom:latest`；也可以在 Actions 页面手动运行。请先确认构建成功，再在目标机器上切换：

```bash
sudo bootc switch ghcr.io/sisanwu12/zirconium-custom:latest
sudo reboot
```

FlClash 的 TUN 仍需在程序内启用并完成其授权流程。本仓库不包含代理订阅或凭据。宿主系统的 `LockLayering=true` 无需修改。

**访问权限：** 若 `bootc switch` 提示未授权，请在 GitHub 的 Package settings 将镜像设为 public，或按 bootc 文档为宿主机配置 GHCR registry 凭据。不要将长期有效的 GitHub token 写进本仓库或命令行历史。

更新 FlClash 时，同时修改 Containerfile 的版本与来自官方 Release 的 SHA-256；官方 Zirconium 更新后可手动重新运行 workflow。确认新镜像构建成功后执行 `sudo bootc upgrade` 并重启。出现问题可以用 `sudo bootc rollback` 回退。
