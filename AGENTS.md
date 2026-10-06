# Repository Instructions

本仓库是率土游戏助手（《率土之滨》）的电脑客户端“率土战局”（公开仓库 github.com/nzery/stzb-warroom）。

- **保持薄**：客户端只读取本机游戏连接、切帧、判断是否游戏连接、战报去重、上传，以及原样显示收到的通知、提示和账号状态。
  分流、绑定、过滤、通知内容等业务逻辑都不放在客户端。
- 只用 Python 标准库。安装或下载依赖前先征得用户许可。
- 不引入任何自动操作游戏的代码（截图、模拟输入、发包）。只读，不改动任何流量。
- Token 只从 `ST_CLIENT_TOKEN` 读取，或者由用户在 Windows 窗口里添加，用 DPAPI 加密保存（`vault.py`）。
  绝不打印、不明文保存、不提交；窗口页面和诊断信息里只出现 `sta_…末四位`。不提交录下的数据、队列数据库或日志。
- Windows 窗口是本机网页（`webui.py` + `stzb_warroom/ui/`，Edge 应用模式打开），只监听 127.0.0.1 并校验密钥 Cookie、Host 和 Origin；
  页面只用自己的文件（CSP 禁止外部脚本和内联脚本）。
- Windows 整合包用 PyInstaller 构建（`packaging/windows/`，只在构建时用，不是运行依赖）。不要把 Npcap 安装程序放进包里，它的免费版不允许再分发。
- 不写个人环境信息（主机名、内网地址、私有服务配置）。
- 本仓库是公开的：README 只写用户能做什么、怎么用，不写原理、协议和服务器相关内容。
- 测试：`python3 -m unittest discover -s tests`。提交和推送只在用户要求时进行。
