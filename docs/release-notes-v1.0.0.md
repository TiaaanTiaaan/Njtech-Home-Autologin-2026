## 南工大校园网自动登录 v1.0.0

首个可用版本，针对 Dr.COM EPortal（`http://10.50.255.11`）自动完成上网认证。

### 下载哪个

| 文件 | 体积 | 说明 |
|---|---|---|
| `NjtechAutologin.exe` | ~32 MB | **精简版 GUI（推荐）**：接口登录 + 托盘 + 开机自启 |
| `NjtechAutologin-Full.exe` | ~69 MB | **完整版 GUI**：额外内置 Playwright，接口失败时可回退浏览器填表 |
| `NjtechAutologin-CLI.exe` | ~32 MB | 命令行 / 计划任务用 |

体积差异全部来自 Playwright 的 node 驱动；只想正常上网，精简版足够。

### 功能

- 首次运行填写账号 / 密码 / 运营商，之后自动复用
- 接口登录优先（毫秒级），失败可回退浏览器（完整版）
- 网络状态检测：区分「校园网是否已认证」与「本机能否上外网」
- 托盘常驻、掉线气泡提醒（勾选后关闭窗口不退出）
- 一键注册开机自启计划任务（按需提权，平时不弹 UAC）
- 自动从门户页面抓取运营商选项并保存

### 使用

1. 下载并双击 `NjtechAutologin.exe`
2. 填账号密码 → 选运营商 → 保存配置 → 点「立即登录」
3. 想开机自动联网：勾选「开机自动登录（计划任务）」

配置与日志保存在 `%APPDATA%\NjtechAutologin`，`--where` 可查看实际路径。

### 说明

- 需要 Windows 10/11，**无需安装 Python**
- `config.json` 内含明文密码，仅保存在本机，请勿分享
- 部分杀软可能对 PyInstaller 单文件包误报，可加白名单

完整说明见 [README](https://github.com/TiaaanTiaaan/Njtech-Home-Autologin-2026#readme)。
