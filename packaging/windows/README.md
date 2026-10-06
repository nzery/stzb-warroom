# Windows 整合包的打包机制

Windows 整合包把程序、运行它要用的 Python 和显示窗口用的 Electron 放进一个文件夹，压成 zip。
解压后双击 `stzb-warroom.exe` 就能运行，电脑上不需要装 Python 或 Node。

本目录的文件：

| 文件 | 作用 |
|---|---|
| `build.ps1` | 构建脚本：打包 exe、放入 Electron、写配置、压成 zip |
| `launcher.py` | exe 的入口 |
| `使用说明.txt` | 原样复制进包里的使用说明 |

## 一、包里有什么

```
stzb-warroom/
├── stzb-warroom.exe        程序本体（PyInstaller 打包，自带 Python）
├── _internal/              PyInstaller 的运行文件，含窗口页面 stzb_warroom/ui
├── window/                 Electron（stzb-window.exe），程序自己启动它显示窗口
│   └── resources/app/      窗口的应用代码，来自仓库的 electron/
├── 使用说明.txt             来自 packaging/windows/使用说明.txt
├── config.json             构建时写入的服务器地址（和网站地址）
└── Wireshark-…-x64.exe     可选：构建时用 -Include 放进来的文件
```

用的是 PyInstaller 的文件夹模式（`--onedir`），不是单文件 exe：启动快，被杀毒软件误报的情况也少一些。

## 二、构建脚本做了什么

`build.ps1` 按顺序做这些事：

1. **写版本号**：取 `-Version`（默认是 GitHub Actions 的 `GITHUB_REF_NAME`，也就是正在构建的 tag），去掉开头的 `v`，
   写进 `stzb_warroom/_version.py`。不是 `数字.数字…` 形式的（比如手动构建、分支构建）一律记为 `0.0.0`。
   源码里不写版本号，`pyproject.toml` 也从这里读。
2. **打包 exe**：用 pip 装固定版本的 PyInstaller（6.22.3，只在构建时用），以 `launcher.py` 为入口打包，
   不带控制台（`--windowed`），带上图标，把 `stzb_warroom/ui` 作为数据文件放进去，去掉用不到的 tkinter。
3. **放入 Electron**：从 GitHub 下载固定版本的 Electron 和 rcedit，逐个核对 SHA-256，不对就失败。
   下载的文件留在 `build\downloads`，下次构建核对通过就不再下载。然后：
   - 解压到 `window\`，删掉 Electron 自带的默认应用，语言包只留中文和英文；
   - `electron.exe` 改名为 `stzb-window.exe`，用 rcedit 换上程序图标、名称和版本号；
   - 把 `electron/main.js`、`electron/package.json` 和图标复制到 `window\resources\app`。
4. **写配置**：把 `-Server`（默认取环境变量 `ST_SERVER`，必填）和 `-Site`（默认取 `ST_SITE`，可选）写进 exe 旁边的
   `config.json`（UTF-8，无 BOM）。地址不进源码，换服务器时也可以直接改这个文件。
5. **复制附带文件**：`使用说明.txt`，以及 `-Include` 指定的文件。
6. **压缩**：用 Python 的 `zipfile` 压成 `build\stzb-warroom-windows.zip`，中文文件名带 UTF-8 标记，各种解压工具都能正确识别。

Electron 的版本和 SHA-256 写在 `build.ps1` 的 `$electronVersion` 和 `$downloads` 里，升级时改这两处。

### exe 入口

`launcher.py` 决定 exe 怎么启动：

- 不带参数，或只带 `--background`：打开窗口（`--background` 是开机启动用的，先不显示窗口）；
- 带其他参数：等同于 `python -m stzb_warroom …`，比如 `stzb-warroom.exe --help`。exe 没有控制台，看不到输出，
  命令行排查建议在装了 Python 的电脑上直接用 `python -m stzb_warroom`。

### 窗口怎么跑起来

程序在本机 `127.0.0.1` 的随机端口开一个只给自己用的网页服务（`stzb_warroom/webui.py`），再启动
`window\stzb-window.exe` 打开它：没有地址栏和菜单，外部链接交给系统默认浏览器，窗口数据放在
`%LOCALAPPDATA%\stzb-warroom\window`。找不到 `window\` 时退回用默认浏览器打开，功能一样。
页面只认启动时随机生成的密钥 Cookie，且只接受以 `127.0.0.1:端口` 访问，别的网站和别的电脑用不了。

### 包外的依赖：Wireshark

包里不带抓包组件，程序用 Wireshark 的 `dumpcap.exe`（Wireshark 安装时会一起装 Npcap）。程序按这个顺序找，用找到的第一个：

1. 环境变量 `ST_DUMPCAP`；
2. 用户在“设置”里手动选过的路径（记在 `%LOCALAPPDATA%\stzb-warroom\dumpcap.txt`）；
3. 注册表里 Wireshark 安装程序登记的位置（`App Paths\Wireshark.exe`，以及“卸载”列表里名字以 Wireshark 开头的那一项）；
4. PATH；
5. `%ProgramFiles%`、`%ProgramFiles(x86)%` 下的 `Wireshark` 文件夹。

都找不到时窗口会提示，并提供“安装 Wireshark”（包里有安装程序就运行它，没有就打开官网下载页）和“手动选择 dumpcap”。

### 哪些能放进包里

- **Npcap 不能放。** 它的免费版不允许再分发（“The free version of Npcap may be used (but not externally redistributed)”），
  分发要买 OEM 授权。
- **Wireshark 安装程序可以选择放。** Wireshark 是 GPL，但它的安装程序里也带着 Npcap，属于灰色地带。默认不放，
  由窗口引导去官网下载；要放的话用 `-Include` 放官方原版安装程序，不要改动它。
- Npcap 免费版只经由 Wireshark 使用时不限装机数量，本程序就是通过 Wireshark 的 dumpcap 用它。

## 三、构建方法 A：GitHub Actions（推荐，不需要 Windows 电脑）

`.github/workflows/windows.yml` 在 GitHub 的 Windows 机器上依次：跑测试 → 运行 `build.ps1` → 检查包里关键文件
（exe、窗口页面、`config.json`、`stzb-window.exe`、`main.js`）都在 → 上传程序文件夹。

**先设置一次：** 仓库 Settings → Secrets and variables → Actions → New repository secret，添加
`ST_SERVER`（如 `https://api.example.com`），可选 `ST_SITE`。它们以 secret 形式传给构建脚本，不进源码，日志里也会被遮住。
没设 `ST_SERVER` 构建会失败。

**手动构建：** Actions → 左边选 “windows” → “Run workflow”。跑完后在这次运行页面底部 Artifacts 里下载
`stzb-warroom-windows`（GitHub 会自动压成 zip）。手动构建的版本号是 `0.0.0`。

**发版本：**

```sh
git tag v1.0.2
git push origin v1.0.2
```

推送 `v*` tag 会自动构建，版本号取自 tag，并在 Releases 创建同名版本，附上 `stzb-warroom-windows.zip`。

GitHub 上构建的包里没有 Wireshark 安装程序。要放的话，解压后把安装程序放进 `stzb-warroom` 文件夹再重新压缩。

## 四、构建方法 B：在 Windows 电脑上本地构建

PyInstaller 只能打包当前系统的程序，所以不能在 Linux 上构建 Windows 包。

1. 安装 Python 3.12：勾选 “Add python.exe to PATH”。
2. 获取代码：`git clone https://github.com/nzery/stzb-warroom.git`，或在 GitHub 上 Code → Download ZIP 后解压。
3. 在 PowerShell 里进入代码目录，先跑测试：

   ```powershell
   cd D:\stzb-warroom
   python -m unittest discover -s tests
   ```

4. 构建：

   ```powershell
   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Server https://api.example.com
   # 指定版本号、网站地址，并把 Wireshark 安装程序一起放进去：
   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Server https://api.example.com `
       -Site https://example.com -Version 1.0.2 -Include D:\下载\Wireshark-4.6.9-x64.exe
   ```

5. 产物：
   - `build\dist\stzb-warroom\`：程序文件夹，可以直接运行里面的 exe；
   - `build\stzb-warroom-windows.zip`：整合包。

## 五、打包后的检查

在一台装好 Wireshark 的 Windows 电脑上：

1. 解压 zip，双击 `stzb-warroom.exe`：打开标题为“率土战局”的窗口，没有地址栏，没有黑色命令行窗口；
   任务栏和标题栏的图标一出现就是清晰的，“关于”页显示的版本号对、链接在默认浏览器里打开。
2. 添加一个错误的 Token：卡片和顶部都标红“Token 无效”。换成正确的：显示角色和“正常”，概览变成“正在上报”。
3. “诊断”里各项检查都正常，运行日志里有 `dumpcap: …`，没有报错。
4. 登录游戏：概览的“最近上传”有时间，“游戏连接”大于 0。
5. Bark 绑定、测试推送、解绑都正常。
6. 右下角有托盘图标，单击能打开窗口。默认关掉窗口后程序和托盘仍在，继续上报；
   托盘右键菜单只显示“退出”，选择后窗口和上报程序一起关闭。设置页没有“退出程序”按钮。
7. 重新打开：Token 还在，自动开始上报。再双击一次 exe：不会开第二个程序，只是再打开一个窗口。
8. 打开“开机自动启动”，注销再登录：有 `stzb-warroom.exe`、没有窗口；双击 exe 能打开窗口。
9. 关闭“关闭窗口后继续在后台运行”，正常启动时关窗口后程序退出；重新打开后该设置仍为关闭。
10. 在一台没装 Wireshark 的电脑上打开：提示没有找到 Wireshark，两个按钮都能用。
11. “诊断 → 复制诊断信息”：没有完整 Token，没有 Bark key。
12. 把 `window` 文件夹改名后打开：退回用默认浏览器显示页面。

## 六、签名与更新

- 包没有代码签名，第一次运行会出现 SmartScreen 的“Windows 已保护你的电脑”，要点“更多信息 → 仍要运行”。
  个别杀毒软件可能误报（PyInstaller 打包的程序常见），被隔离时表现为双击没反应或 `window` 文件夹不见了。
  要彻底解决只能买代码签名证书。
- 更新就是用新文件夹替换旧文件夹。数据和 Token 在 `%LOCALAPPDATA%\stzb-warroom`，不在程序文件夹里，替换不受影响。
