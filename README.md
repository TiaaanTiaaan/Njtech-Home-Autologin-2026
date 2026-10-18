# Njtech 校园网自动登录（Dr.COM EPortal）

自动登录南京工业大学宿舍区域有线校园网的小软件，方便快捷不烧心。

> 📦 **不想装 Python？** 直接下载 [最新 Release](https://github.com/TiaaanTiaaan/Njtech-Home-Autologin-2026/releases/latest)
> 里的 `NjtechAutologin.exe`（约 32 MB，双击即用）。

面向 `http://10.50.255.11` 的局域网认证门户，自动完成上网登录。

## 功能

1. **探测填报项**：访问门户，识别登录页需要填写哪些字段（账号 / 密码 / 服务类型），并打印出来。
2. **询问并保存**：首次运行在终端询问填写内容，保存到 `config.json`。
3. **不再重复询问**：之后每次启动只要读到已保存的配置就直接登录。
4. **网络状态检测**：分别判断“校园网是否已认证”和“本机能否上外网”，可区分「有线未认证 + 无线热点可用」的场景。
5. **接口优先，浏览器兜底**：先直接调用 `/eportal/portal/login`（毫秒级）；失败再启动 Edge 自动填表提交。

## 环境准备

```powershell
& "C:\Program Files\Python312\python.exe" -m pip install requests playwright
```

Edge 为系统自带即可，无需 `playwright install`。

## 用法

### 图形界面（推荐）

双击 `main.pyw`（用 pythonw 运行，不弹黑框），或在命令行：

```powershell
& "C:\Program Files\Python312\python.exe" main.py --gui
```

界面上可以直接：查看网络状态（每 30 秒自动刷新）、填写并保存账号密码、下拉选择运营商、
一键「立即登录 / 刷新状态 / 注销 / 浏览器登录」，运行日志实时显示在下方。
网络操作都在后台线程，界面不会卡死。

**常驻与自启**（界面上的两个勾选项）：

- **关闭窗口时最小化到托盘**（默认不勾选）：勾选后点右上角关闭只会缩到系统托盘（蓝色 N 图标），
  后台继续每 30 秒检测一次；托盘菜单可「显示主界面 / 立即登录 / 刷新状态 / 退出」，
  鼠标悬停显示当前状态，掉线时会弹气泡提醒。未勾选时关闭窗口即退出程序。
- **开机自动登录（计划任务）**：勾选后注册 Windows 计划任务 `NjtechCampusAutoLogin`，
  登录系统后由 `pythonw.exe` 静默运行 `main.py --auto`（无黑框）。取消勾选即移除。

托盘功能依赖 `pystray` 与 `Pillow`，未安装时自动退化为「最小化到任务栏」：

```powershell
& "C:\Program Files\Python312\python.exe" -m pip install pystray pillow
```

### 命令行

```powershell
cd "d:\Njtech Home Autologin-2026"
& "C:\Program Files\Python312\python.exe" main.py            # 检测 -> 已在线则退出，否则登录
& "C:\Program Files\Python312\python.exe" main.py --setup     # 重新探测字段并重新填写
& "C:\Program Files\Python312\python.exe" main.py --status    # 只看网络状态
& "C:\Program Files\Python312\python.exe" main.py --auto      # 静默重试模式（计划任务用）
& "C:\Program Files\Python312\python.exe" main.py --browser   # 只用浏览器登录（不做状态预检，加 --headless 则无界面）
& "C:\Program Files\Python312\python.exe" main.py --logout     # 注销当前在线会话（测试用）

# 开机自启（计划任务）
& "C:\Program Files\Python312\python.exe" main.py --install-task    # 注册（登录 Windows 后自动联网）
& "C:\Program Files\Python312\python.exe" main.py --task-status     # 查看是否已注册
& "C:\Program Files\Python312\python.exe" main.py --uninstall-task  # 移除

# 运营商 / 服务类型
& "C:\Program Files\Python312\python.exe" main.py --list-carriers        # 查看可选项
& "C:\Program Files\Python312\python.exe" main.py --carrier 校园电信      # 按名称设置（@dx）
& "C:\Program Files\Python312\python.exe" main.py --carrier 2            # 按序号设置
& "C:\Program Files\Python312\python.exe" main.py --carrier @cmcc        # 直接写后缀
```

首次 `--setup` 时会交互式让你选运营商（也可直接回车沿用当前设置）。

## 配置说明（`%APPDATA%\NjtechAutologin\config.json`）

配置文件、日志都不在 exe 旁边，而是在用户目录：

```
%APPDATA%\NjtechAutologin\config.json      # 账号、密码、运营商等
%APPDATA%\NjtechAutologin\logs\            # 运行日志
```

这样 exe 放在 `Program Files` 等无写权限位置也能正常工作，升级替换 exe 不会丢配置。
查看实际路径：`main.py --where`（或 exe `--where`）；想换位置就设环境变量 `NJTECH_DATA_DIR`。

旧版本放在程序目录里的 `config.json` 会在首次运行时**自动迁移**过去。

```jsonc
{
  "portal": { "url": "http://10.50.255.11/", "api_base": "http://10.50.255.11:801/eportal/portal/" },
  "fields": [
    { "name": "DDDDD", "label": "账号", "kind": "text", "secret": false, "required": true },
    { "name": "upass", "label": "密码", "kind": "password", "secret": true, "required": true }
  ],
  "values": { "DDDDD": "学号", "upass": "密码" },
  "options": { "account_prefix": ",0,", "carrier": "校园用户", "carrier_suffix": "" },
  "ui": { "minimize_to_tray": true },
  "carriers": [
    { "name": "校园用户", "suffix": "" },
    { "name": "校园电信", "suffix": "@dx" },
    { "name": "校园联通", "suffix": "@lt" },
    { "name": "校园其他", "suffix": "" }
  ]
}
```

- `carrier` / `carrier_suffix`：当前运营商及其账号后缀，登录时拼成 `账号+后缀`（账号本身带 `@` 时不再拼接）。
- `carriers`：**每次运行都会从门户页面重新抓取**（页面内联的 `carrier='{...}'`，或 `<select name="ISP_select">` 的选项），
  与账号密码一起写回本文件；本地额外加的项（如 `@cmcc`）会保留在列表末尾。
- `--carrier` 支持四种写法：序号 `2`、中文名 `校园电信`、后缀 `@cmcc`、简写 `cmcc`（自动补 `@`）。
- 浏览器登录成功后，程序会从抓到的真实请求里识别账号实际后缀并自动写回 `carrier`。
- `account_prefix`：Dr.COM 无感知前缀，程序会自动尝试带/不带两种写法，一般不用改。
- `ui.minimize_to_tray`：关闭窗口时是否缩到托盘（对应界面上的勾选项）。

## 打包为 exe

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

产物在 `dist\`：

| 文件 | 体积 | 说明 |
|---|---|---|
| `NjtechAutologin.exe` | ~32 MB | **精简版 GUI**：接口登录 + 托盘 + 开机自启，不含浏览器兜底（点「浏览器登录」会提示不可用） |
| `NjtechAutologin-Full.exe` | ~69 MB | **完整版 GUI**：额外内置 Playwright 驱动，接口失败时可回退浏览器填表 |
| `NjtechAutologin-CLI.exe` | ~32 MB | 命令行版（精简），行为与 `main.py` 一致 |

体积差异全部来自 Playwright 的 node 驱动（约 37 MB）。只想登录上网的话用精简版即可。

- `dist\` 只放程序本体；**配置与日志写到 `%APPDATA%\NjtechAutologin`**，不会在 exe 旁边生成文件。
- **提权是按需的**：平时启动不弹 UAC；只有点“开机自动登录”且当前权限不足以注册计划任务时，
  才弹一次授权框（取消则告警并回滚勾选状态）。
- **不会闪命令行窗口**：所有 PowerShell 子进程都加了 `CREATE_NO_WINDOW`；
  Playwright 的 node 驱动自带 `SW_HIDE`，登录/定时检测时不会弹出黑框。
- **图标统一**：exe 图标、窗口标题栏、任务栏、托盘统一使用 `assets/app.ico`（蓝色 N）。
- 已内置 `requests`、`playwright` 驱动、`pystray`、`Pillow`，目标机器**无需安装 Python**；
  浏览器兜底仍调用系统自带 Edge（`channel="msedge"`），无需 `playwright install`。
- exe 会读写**用户目录**下的 `config.json` 与 `logs\`（`%APPDATA%\NjtechAutologin`），可把 `dist\` 整个目录拷到别处使用。
- 单文件约 69 MB，主要是 playwright 的 node 驱动；首次运行解包会慢 1~2 秒。
- 移动 exe 位置后，请重新勾选一次「开机自动登录」，让计划任务指向新路径。
- PyInstaller 单文件包偶尔会被杀毒软件误报，可加入白名单或改用 `--onedir` 打包。

## 调试提示

- 同时接校园网和手机热点时，程序会把外网探测**绑定到校园网网卡**，避免热点让“已认证”判断失真。
- 需要反复测试登录流程时，用 `--logout` 注销，再正常运行时观察日志（`logs/autologin-YYYYMMDD.log`）。

## 安全

`config.json`（`%APPDATA%\NjtechAutologin\config.json`）内含明文密码，仅保存于本机，请勿分享或提交到公开仓库（项目里的 `.gitignore` 已排除）。

## 许可协议

本项目基于 [MIT License](LICENSE) 开源，可自由使用、修改与分发，仅需保留版权声明。
