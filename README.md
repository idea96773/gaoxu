# 稿序

稿序（Gaoxu）是本地运行的文章分发管理台。你可以从 Easy WeChat 固定下载目录选稿，保留原文、署名、配图和文末链接。审核通过后，系统按平台轮流分给所选模拟账号，根据北京时间、账号每日上限和发布间隔排期。

在管理台可按文章标题、平台、账号和日期跨批次查询，核对计划时间、实际交付时间和模拟回执。后台调度器独立运行，支持在服务重启后恢复未完成任务。

## 在 Windows 上运行

首次使用请安装 Python、uv 和 Node.js，然后在项目根目录按运行手册安装依赖并构建前端。构建完成后执行：

```powershell
.\scripts\start-distribution.ps1
```

打开[本机管理台](http://127.0.0.1:5419/#/distribution)。通过 `DIST_LIBRARY` 指定本机的 Easy WeChat 下载目录。运行数据和模拟回执保存在本机的 `data/distribution`。换电脑时请分别迁移文章目录和运行数据。

[依赖安装、后台调度、MCP 和回归测试](DISTRIBUTION.md)

## 来源与许可

管理台基于 [千帆云递 QianFan Sync](https://github.com/laoshalab/social-auto-upload-web-ui-pro) 的提交 `6b1474be2de1859122ab2f0f30e781cacec4e5e4` 构建。原项目说明和 MIT 许可证随仓库保留。

[上游项目说明](README.qianfan.md) · [许可证](LICENSE)
