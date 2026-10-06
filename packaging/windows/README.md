# Windows 整合包：构建与分发

整合包是一个 zip，里面有一个文件夹，盟友解压后双击 `stzb-warroom.exe` 就能用：

```
stzb-warroom/
├── stzb-warroom.exe        双击打开“率土战局”窗口（PyInstaller 打包，自带 Python）
├── _internal/              exe 运行要用的文件（含窗口页面 stzb_warroom/ui），不要删
├── window/                 显示窗口的 Electron（stzb-window.exe，程序自己启动它），不要删
├── 使用说明.txt             给盟友看的说明（来自 packaging/windows/使用说明.txt）
├── config.json             构建时写入的服务器地址（和网站地址）
└── Wireshark-…-x64.exe     可选：构建时用 -Include 放进来的 Wireshark 安装程序
```

### 窗口是怎么做的

程序在本机 `127.0.0.1` 的随机端口开一个只给自己用的网页服务（`stzb_warroom/webui.py`），用包里的 Electron
（`window\stzb-window.exe`，应用代码在仓库的 `electron/`）打开：没有地址栏和菜单，不登录账号、不提示保存密码，
其他网站的链接交给系统默认浏览器打开；窗口数据放在 `%LOCALAPPDATA%\stzb-warroom\window`。
Electron 固定在一个版本（`build.ps1` 里的 `$electronVersion` 和 SHA-256），要升级时改这两处。
找不到 `window\` 时用默认浏览器打开。页面在 `stzb_warroom/ui/`（HTML、CSS、JS），
只有带着启动时随机生成的密钥（Cookie）、并以 `127.0.0.1:端口` 访问的页面才能读写，别的网站和别的电脑用不了。

- **概览**：上报状态、已上传/待上传/最近上传/游戏连接、账号摘要、最新通知。
- **通知**：收到的通知，可按紧急/提醒/消息筛选；紧急通知响铃，可开桌面弹窗。
- **账号与推送**：添加、删除 Token；每个 Token 显示名称、角色、审批状态；
  Token 无效时卡片和顶部都标红。Bark 未绑定时可粘贴地址绑定（先发一条测试通知，成功才保存），
  已绑定的显示地址前几位，可发测试推送或解绑。
- **设置**：开机自动启动（`HKCU\…\Run`，以 `--background` 启动，不弹窗口）、关闭窗口后继续在后台运行、
  dumpcap 路径（自动找到或手动选择）、打开数据文件夹、退出程序。
- **诊断**：连接、账号、网络组件、上报、游戏连接的检查结果；运行日志；“复制诊断信息”（不含 Token 和 Bark 密钥）。
- **关于**：版本、检查更新（GitHub Releases 有更高版本时顶部也会提示）、网站链接。
- 上报遇到问题时，提示直接显示在窗口顶部，不弹窗。

同一台电脑只运行一个：再次双击 exe 只会打开已经在运行的那个窗口（靠 `%LOCALAPPDATA%\stzb-warroom\window.json`）。
关掉窗口几秒内程序退出（停止上报），除非打开了“关闭窗口后继续在后台运行”或是开机启动的。
程序运行时右下角有托盘图标（`winsys.Tray`）：单击打开窗口，右键菜单“打开窗口”“退出”。

Token 添加后自动用 Windows DPAPI 加密，保存在 `%LOCALAPPDATA%\stzb-warroom\tokens.bin`，只有这台电脑的这个
Windows 用户能解开。窗口只用盟友自己添加的 Token，不读环境变量 `ST_CLIENT_TOKEN`；
所有保存的东西（Token、设置、手动选的 dumpcap）都只在盟友操作时才写。

### 怎么找 dumpcap

程序要用 Wireshark 的 `dumpcap.exe`，所以**盟友的电脑要先装 Wireshark（含 Npcap）**。
程序按这个顺序找 `dumpcap.exe`，用找到的第一个：

1. 环境变量 `ST_DUMPCAP`；
2. 用户在“设置”里手动选过的路径（记在 `%LOCALAPPDATA%\stzb-warroom\dumpcap.txt`）；
3. 注册表里 Wireshark 安装程序登记的位置（`App Paths\Wireshark.exe`，以及“卸载”列表里名字以 Wireshark 开头的那一项）；
4. PATH；
5. `%ProgramFiles%`、`%ProgramFiles(x86)%` 下的 `Wireshark` 文件夹。

都找不到时，窗口顶部和概览会提示，并给出“安装 Wireshark”（文件夹里有安装程序就运行它，没有就打开官网下载页）
和“手动选择 dumpcap”两个按钮。找过的位置会写进运行日志。

## 一、要下载的软件

| 软件 | 用途 | 下载 |
|---|---|---|
| Python 3.12（只在本地构建时需要） | 运行 PyInstaller（Electron 由构建脚本自动下载） | <https://www.python.org/downloads/windows/>，选 “Windows installer (64-bit)” |
| Wireshark 4.6.9 x64 | 盟友电脑上必须安装，自带 Npcap 驱动 | 下载页 <https://www.wireshark.org/download.html>，直链 <https://2.na.dl.wireshark.org/win64/Wireshark-4.6.9-x64.exe> |
| Npcap 1.89（备用） | Wireshark 装好了但 Npcap 没装上时单独补装 | <https://npcap.com/#download>，直链 <https://npcap.com/dist/npcap-1.89.exe> |

版本号是写这份文档时（2026 年 10 月）的最新版，以后到下载页取最新的就行。

**关于把安装程序打包进去：**

- **Npcap 的免费版不允许再分发。** Npcap 官网写明 “The free version of Npcap may be used (but not externally redistributed)”，
  要分发得买 OEM 授权。所以**不要把 `npcap-*.exe` 放进整合包**，让盟友自己从官网下载。
- Wireshark 本身是 GPL，可以分发，但它的安装程序里也带着 Npcap 安装程序，属于灰色地带。比较稳妥的做法是不放进去，
  窗口会引导盟友去官网下载。如果你要放，用 `-Include` 放官方原版安装程序，不要改动它。
- Npcap 免费版只用于 Wireshark 时不限装机数量。本程序就是通过 Wireshark 的 dumpcap 使用它。

## 二、构建方法 A：GitHub Actions（推荐，不需要 Windows 电脑）

仓库里的 `.github/workflows/windows.yml` 会在 GitHub 的 Windows 机器上跑测试、打包。

**先设置一次：** 仓库 Settings → Secrets and variables → Actions → New repository secret，添加
`ST_SERVER`（包要连的服务器，如 `https://api.example.com`），可选 `ST_SITE`（窗口“关于”页“打开网站”的地址）。
构建时写进程序文件夹里的 `config.json`，不进源码，日志里也会被遮住。没设 `ST_SERVER` 构建会失败。

**手动构建一次：**

1. 把代码推到 GitHub。
2. 打开仓库页面，点 Actions → 左边选 “windows” → 右边点 “Run workflow”。
3. 跑完后点进这次运行，在页面底部 Artifacts 里下载 `stzb-warroom-windows`。里面是程序文件夹（带 `使用说明.txt`），
   GitHub 下载时会自动压成 zip。

**发版本（推荐）：**

```sh
# 先改 pyproject.toml 和 stzb_warroom/__init__.py 里的 version（例如 1.0.1），提交后：
git tag v1.0.1
git push origin v1.0.1
```

推送 tag 后会自动构建，并在 Releases 页面创建 `v1.0.1`，附上 `stzb-warroom-windows.zip`。把 Release 链接发给盟友即可。

GitHub 上构建的包里没有 Wireshark 安装程序。要放的话，下载 zip 后解压，把 Wireshark 安装程序放进 `stzb-warroom` 文件夹，
再重新压缩。

## 三、构建方法 B：在 Windows 电脑上本地构建

1. 安装 Python 3.12：运行安装程序，**勾选 “Add python.exe to PATH”**，选 “Customize installation”，
   确认 “tcl/tk and IDLE” 是勾选的（默认就是），然后安装。
2. 获取代码（任选一种）：
   - 装了 Git：`git clone https://github.com/nzery/stzb-warroom.git`
   - 没装 Git：在 GitHub 仓库页点 Code → Download ZIP，解压。
3. 打开 PowerShell，进入代码目录，先跑测试：

   ```powershell
   cd D:\stzb-warroom
   python -m unittest discover -s tests
   ```

4. 构建（会用 pip 装 PyInstaller 6.22.3，只在构建时用；还会从 GitHub 下载 Electron 和 rcedit，存在 `build\downloads`）：

   ```powershell
   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Server https://api.example.com
   # 要把 Wireshark 安装程序一起放进去：
   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Server https://api.example.com -Include D:\下载\Wireshark-4.6.9-x64.exe
   # 可选 -Site https://... 给“关于”页的“打开网站”；换服务器时也可以直接改程序文件夹里的 config.json
   ```

5. 产物：
   - `build\dist\stzb-warroom\`：程序文件夹，可以直接双击里面的 exe 试用；
   - `build\stzb-warroom-windows.zip`：发给盟友的整合包。

不能在 Linux 上直接构建：PyInstaller 只能打包当前系统的程序。

## 四、发给盟友前在 Windows 上测一遍

在一台装好 Wireshark 的 Windows 电脑上：

1. 解压 zip，双击 `stzb-warroom.exe`：打开标题为“率土战局”的窗口，没有地址栏，没有黑色命令行窗口；
   任务栏和标题栏的图标一出现就是清晰的，“关于”页的链接在默认浏览器里打开。
2. 没有 Token 时，顶部提示“还没有添加 Token”。到“账号与推送”粘贴一个错误的 Token（比如改掉最后一位）：
   卡片和顶部都标红“Token 无效”。删掉它，添加正确的 Token：显示角色和“正常”，概览变成“正在上报”。
3. “诊断”里各项检查都是正常，运行日志里有 `dumpcap: …`，没有报错。
4. 登录游戏：概览的“最近上传”有时间，“游戏连接”大于 0。
5. Bark：未绑定的账号粘贴 Bark 地址点“绑定”，手机收到测试通知；“发测试推送”“解绑”都正常。
6. 右下角有托盘图标，单击能打开窗口。关掉窗口，几秒后托盘图标消失，任务管理器里 `stzb-warroom.exe`、`stzb-window.exe` 和 `dumpcap.exe` 都已退出。
   托盘右键“退出”时窗口也一起关掉。
7. 重新打开：Token 还在，自动开始上报。再双击一次 exe：不会开第二个程序，只是再打开一个窗口。
8. “设置”里打开“开机自动启动”，注销再登录：任务管理器里有 `stzb-warroom.exe`、没有窗口；双击 exe 能打开窗口。
9. 打开“关闭窗口后继续在后台运行”，关窗口后程序和托盘图标仍在；单击托盘图标能重新打开窗口，右键“退出”能退出。
10. 在一台**没装** Wireshark 的电脑上打开：提示没有找到 Wireshark，两个按钮都能用。
11. “诊断 → 复制诊断信息”，粘贴出来检查：没有完整 Token，没有 Bark key。

## 五、分发与更新

- 程序没有代码签名，盟友第一次运行会看到 SmartScreen 的“Windows 已保护你的电脑”，要点“更多信息 → 仍要运行”。
  个别杀毒软件可能误报（PyInstaller 打包的程序常见）。用的是文件夹版（`--onedir`）而不是单文件 exe，误报会少一些。
  要彻底解决只能买代码签名证书。
- 更新时，让盟友关掉窗口，用新文件夹替换旧文件夹。数据和 Token 在 `%LOCALAPPDATA%\stzb-warroom`，替换程序不受影响。

## 六、常见问题

| 现象 | 处理 |
|---|---|
| 状态栏提示“网络组件没能启动” | Npcap 没装好或没重启。重装 Wireshark 并勾选 Npcap，或单独装 Npcap，然后重启 |
| 双击 exe 没反应 | 多半是被杀毒软件隔离了，把文件夹加入信任后重新解压 |
| 已经装了 Wireshark 还提示没找到 | “设置 → 手动选择”，选 Wireshark 安装文件夹里的 `dumpcap.exe`。如果找不到这个文件，说明装的是便携版或者没装完整，要用官网的 “Windows x64 Installer” 重新安装。把诊断信息发给维护者，可以帮助改进自动查找 |
| 窗口打开是普通浏览器标签页 | 程序文件夹里的 `window` 文件夹不见了（多半被杀毒软件隔离或解压不完整），用了默认浏览器，功能一样。重新解压整个 zip 即可 |
| 想用命令行排查 | exe 后面带参数时等同于 `python -m stzb_warroom`，例如 `stzb-warroom.exe --help`。窗口程序没有控制台，看不到输出，排查建议在装了 Python 的电脑上直接用 `python -m stzb_warroom capture` |
