# Windows 安装与模拟验收

这套流程适用于能执行本机 PowerShell 命令的 WorkBuddy、Trae 或人工测试。首次安装需要联网。模拟转载使用本地文件，无需登录内容平台、提供 Cookie 或配置模型 API Key。

## 启动管理台

1. 在独立工作目录执行 `git clone https://github.com/idea96773/gaoxu.git`，然后 `cd gaoxu`。记录 `git rev-parse --short HEAD`。保留已有工作目录和任务库。
2. 检查 `git --version`、`uv --version`、`node --version`、`npm.cmd --version`。本次验证使用 Node.js 24。缺少 Git、uv 或 Node.js 时，可通过 Windows 软件包管理器分别安装 Git.Git、astral-sh.uv、OpenJS.NodeJS.LTS，重开终端。
3. 在仓库根目录用下面的命令启动。脚本会创建项目专用的 Python 3.12 环境、安装依赖并构建管理台。若 5419 端口属于其他项目，保留该进程并报告冲突。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\launch-gaoxu.ps1 -NoBrowser
if ($LASTEXITCODE -ne 0) { throw '稿序启动失败，请查看上方报错' }
Invoke-RestMethod http://127.0.0.1:5419/api/distribution/health |
    Select-Object application,mode,worker_online
(Resolve-Path .\examples\demo-library).Path
```

健康结果应为 `gaoxu`、`dry-run`、`True`。打开 [本机稿序管理台](http://127.0.0.1:5419/#/distribution)。整个测试使用模拟账号。单篇知乎草稿助手属于另一次独立的平台验证。

## 用三篇样稿验收界面

1. 打开「批量转载 → 保存目录设置」，填入上面打印的样稿目录绝对路径并读取。干净安装应出现三篇「稿序样稿」。样稿里的原文地址是用于区分文章的合成标识。
2. 选中三篇文章，为知乎和今日头条各选两个模拟账号，两个平台均使用默认的文末公众号直链。打开审核页，检查三篇的蓝色圆形、绿色方形、橙色三角形分别显示在各自两段正文之间，并检查文末链接的实际地址。
3. 预览分稿与北京时间排期，然后审核通过。在新的模拟任务库中，每篇文章应各有一条知乎任务和一条头条任务，共六条。每个平台的两个账号应各分到两篇和一篇。记录账号、版本和排期。
4. 打开「跨批次查询」，按一篇样稿的标题、分配账号及**排期所在日期**筛选，核对详情。计划时间未到时，显示排队中属于预期。办公电脑保持原来的系统时间。

## 立即验证模拟交付

从仓库根目录运行以下用例。它使用独立临时库和测试时钟生成 100 篇文章，审核冻结、分配账号、排期、重开应用、执行到期任务并核对回执。运行结束时应显示 `OK`。这 200 份模拟回执属于自动化用例，与界面中的六条样稿排期分别记录。

```powershell
Push-Location .\backend
try {
    ..\.dist-venv\Scripts\python.exe -B -m unittest -v distribution.test_reprint.Reprint.test_100_articles_200_independent_receipts
    if ($LASTEXITCODE -ne 0) { throw '100 篇模拟交付未通过' }
} finally { Pop-Location }
```

测试反馈请写明提交号、Windows 和 Node.js 版本、三项健康结果、界面实际生成的任务数、账号分配以及自动化用例结果。有失败时，附第一条错误和所在步骤。工具无法操作浏览器时，可完成命令行部分，把界面核对项交给同事填写。
