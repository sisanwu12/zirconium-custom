# zirconium-custom

基于官方 Zirconium 的最小 bootc 派生镜像。首版只安装 FlClash 0.8.98（x86_64），RPM 下载后进行 SHA-256 校验。

推送 `main` 中的 Containerfile 或 workflow 后，GitHub Actions 会构建并发布到 `ghcr.io/sisanwu12/zirconium-custom:latest`；也可以在 Actions 页面手动运行。请先确认构建成功，再在目标机器上切换：

```bash
sudo bootc switch ghcr.io/sisanwu12/zirconium-custom:latest
sudo reboot
```

FlClash 的 TUN 仍需在程序内启用并完成其授权流程。本仓库不包含代理订阅或凭据。宿主系统的 `LockLayering=true` 无需修改。

**访问权限：** 此 GitHub 仓库为私有仓库，GHCR 包可能也是私有的。若 `bootc switch` 提示未授权，请先在 GitHub 的 Package settings 将该镜像设为 public，或按 bootc 文档为宿主机配置 GHCR registry 凭据。不要将长期有效的 GitHub token 写进本仓库或命令行历史。

更新 FlClash 时，同时修改 Containerfile 的版本与来自官方 Release 的 SHA-256；官方 Zirconium 更新后可手动重新运行 workflow。确认新镜像构建成功后执行 `sudo bootc upgrade` 并重启。出现问题可以用 `sudo bootc rollback` 回退。
