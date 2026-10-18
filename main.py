#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""南工大校园网自动登录（Dr.COM EPortal，http://10.50.255.11）。

设计要点
--------
1. 访问 http://10.50.255.11 并识别需要填写的字段（账号 / 密码 / 运营商 ...）。
2. 首次运行询问并保存填写内容到 config.json；之后检测到已填写就不再询问。
3. 登录优先走接口（requests 直连 /eportal/portal/login），失败则回退浏览器
   （Playwright + 系统自带 Edge）自动填表提交。
4. 检测网络状态：分别判断“校园网是否已认证”和“是否可上外网”。

用法
----
    python main.py              # 正常流程：检测 -> 已在线则退出，否则登录
    python main.py --setup      # 重新探测字段并重新填写配置
    python main.py --status     # 只打印网络状态
    python main.py --force      # 忽略“已在线”，强制登录
    python main.py --browser    # 跳过接口，直接用浏览器登录
    python main.py --headless   # 浏览器模式无界面
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖：pip install requests")

FROZEN = bool(getattr(sys, "frozen", False))
# 程序所在目录（打包后为 exe 所在目录，仅用于定位 exe / 作为工作目录）
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent

APP_NAME = "NjtechAutologin"


def _data_dir() -> Path:
    """配置与日志的存放目录：优先环境变量，其次 %APPDATA%，最后用户主目录。"""
    override = os.environ.get("NJTECH_DATA_DIR")
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ("." + APP_NAME.lower())


# 用户数据目录（不在 exe 旁边，避免 Program Files 无写权限 / 升级被覆盖）
DATA_DIR = _data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
LOG_DIR = DATA_DIR / "logs"
TASK_NAME = "NjtechCampusAutoLogin"  # 开机自动登录的计划任务名
CREATE_NO_WINDOW = 0x08000000  # 子进程不弹控制台窗口（仅 Windows 有效）

PORTAL_URL = "http://10.50.255.11/"
PORTAL_HOST = "10.50.255.11"
API_BASE = "http://10.50.255.11:801/eportal/portal/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
)

# 已知字段的中文名（兜底，页面解析不出时使用）
KNOWN_LABELS = {
    "DDDDD": "账号",
    "upass": "密码",
    "captcha": "验证码",
    "ISP_select": "运营商 / 服务类型",
    "R3": "服务类型（编码）",
}
SECRET_FIELDS = {"upass", "password", "passwd", "pwd", "captcha"}

# 兜底字段表：Dr.COM 标准登录表单
DEFAULT_FIELDS = [
    {"name": "DDDDD", "label": "账号", "kind": "text", "secret": False, "required": True},
    {"name": "upass", "label": "密码", "kind": "password", "secret": True, "required": True},
]

# 运营商 / 服务类型（来自门户 a41.js 的 carrier 配置）：决定账号后缀
# 校园移动(@cmcc) 未出现在注销页模板里，但真实登录请求中出现过，故一并保留
CARRIERS = [
    {"name": "校园用户", "suffix": ""},
    {"name": "校园电信", "suffix": "@dx"},
    {"name": "校园联通", "suffix": "@lt"},
    {"name": "校园移动", "suffix": "@cmcc"},
    {"name": "校园其他", "suffix": ""},
]
SUFFIX_NAMES = {"@dx": "校园电信", "@lt": "校园联通", "@cmcc": "校园移动"}

_log_file: Path | None = None
_log_sink = None  # GUI 用：把日志行转发到界面


def set_log_sink(sink) -> None:
    """设置日志回调（传入 None 恢复终端输出）。"""
    global _log_sink
    _log_sink = sink


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    global _log_file
    stamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    if _log_sink is not None:
        try:
            _log_sink(line)
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            print(line, flush=True)
        except Exception:  # noqa: BLE001  pythonw 下 stdout 可能为 None
            pass
    if _log_file is None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _log_file = LOG_DIR / f"autologin-{datetime.now():%Y%m%d}.log"
        except OSError:
            return
    try:
        with _log_file.open("a", encoding="utf-8") as fp:
            fp.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 用户数据目录
# --------------------------------------------------------------------------- #
def ensure_data_dir() -> None:
    """确保用户数据目录（默认 %APPDATA%/NjtechAutologin）存在。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


_migrated = False


def migrate_legacy_config() -> bool:
    """把旧版本放在程序目录里的 config.json 搬到用户目录（返回是否迁移过）。"""
    global _migrated
    if _migrated:
        return False
    _migrated = True
    if CONFIG_PATH.exists():
        return False
    legacy = ROOT / "config.json"
    if legacy == CONFIG_PATH or not legacy.exists():
        return False
    ensure_data_dir()
    try:
        shutil.move(str(legacy), str(CONFIG_PATH))
    except OSError:
        try:
            shutil.copy2(legacy, CONFIG_PATH)
        except OSError:
            return False
    log(f"已把旧配置迁移到用户目录：{CONFIG_PATH}")
    return True


# --------------------------------------------------------------------------- #
# 会话（可绑定源 IP，用于区分“校园网”与“热点”两条链路）
# --------------------------------------------------------------------------- #
class _SourceAdapter(HTTPAdapter):
    def __init__(self, source_ip: str, **kwargs):
        self._source_ip = source_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):  # noqa: D102
        kwargs["source_address"] = (self._source_ip, 0)
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):  # noqa: D102
        kwargs["source_address"] = (self._source_ip, 0)
        return super().proxy_manager_for(proxy, **kwargs)


def make_session(bind_ip: str | None = None) -> requests.Session:
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    sess.trust_env = False  # 不读系统代理，避免干扰
    if bind_ip:
        adapter = _SourceAdapter(bind_ip, max_retries=0)
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)
    return sess


# --------------------------------------------------------------------------- #
# 本机网卡 IP 探测
# --------------------------------------------------------------------------- #
def _powershell(cmd: str, timeout: float = 10.0) -> str:
    """执行 PowerShell 并返回 stdout（失败返回空串，绝不抛异常）。"""
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        ).stdout
    except Exception:  # noqa: BLE001
        return ""


def local_ipv4_list() -> list[str]:
    """枚举本机 IPv4 地址（排除回环与 APIPA）。"""
    out = _powershell(
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object {$_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*'} | "
        "ForEach-Object { \"$($_.IPAddress)|$($_.InterfaceAlias)\" }"
    )
    ips = [line.strip() for line in out.splitlines() if "|" in line]
    if not ips:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect((PORTAL_HOST, 80))
            ips.append(f"{probe.getsockname()[0]}|unknown")
            probe.close()
        except OSError:
            pass
    return ips


def detect_campus_ip(timeout: float = 3.0) -> str | None:
    """找出通往门户那张网卡的 IP（毫秒级：直接问系统路由表）。"""
    out = _powershell(
        f"Find-NetRoute -RemoteIPAddress {PORTAL_HOST} -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty IPAddress"
    )
    for line in out.splitlines():
        ip = line.strip()
        if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) and not ip.startswith("127."):
            return ip

    # 兜底：逐个绑定测试，严格短超时
    for entry in local_ipv4_list():
        ip = entry.split("|", 1)[0]
        try:
            make_session(ip).get(PORTAL_URL, timeout=(1.5, timeout), allow_redirects=False)
            return ip
        except Exception:  # noqa: BLE001
            continue
    return None


# --------------------------------------------------------------------------- #
# 网络状态检测
# --------------------------------------------------------------------------- #
PROBE_URLS = [
    ("http://connect.rom.miui.com/generate_204", 204),
    ("http://www.baidu.com/", 200),
    ("http://www.msftconnecttest.com/connecttest.txt", 200),
]
PROBE_TIMEOUT = (2.0, 3.0)


def is_online(bind_ip: str | None = None, timeout=PROBE_TIMEOUT) -> bool:
    """能否直接访问外网（未被劫持到门户）。任何异常都视为“不可用”。"""
    sess = make_session(bind_ip)
    stamp = int(time.time())
    for url, expect in PROBE_URLS:
        probe = f"{url}{'&' if '?' in url else '?'}_t={stamp}"
        try:
            r = sess.get(probe, timeout=timeout, allow_redirects=True)
        except Exception:  # noqa: BLE001  含 TimeoutError / ConnectTimeout / SSLError
            continue
        if PORTAL_HOST in (r.url or ""):
            continue  # 被劫持到门户 => 未认证
        if r.status_code == expect and len(r.content) < 300_000:
            return True
    return False


def wait_online(bind_ip: str | None, seconds: float = 25.0) -> bool:
    """登录后给网络一点时间生效，期间轮询（认证生效可能有几秒到几十秒延迟）。"""
    deadline = time.time() + seconds
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        if is_online(bind_ip):
            if attempt > 1:
                log(f"  网络在第 {attempt} 次探测后恢复。")
            return True
        time.sleep(2.0)
    return False


def portal_state(bind_ip: str | None = None, timeout: float = 5.0) -> dict:
    """读取门户页面（仅作参考信息，是否在线以真实联网探测为准）。"""
    info = {"reachable": False, "needs_login": False, "html": ""}
    try:
        sess = make_session(bind_ip)
        r = sess.get(PORTAL_URL, timeout=(3.0, timeout))
        r.encoding = _guess_encoding(r)
        html = r.text
        info.update(reachable=True, html=html)
        has_password = bool(re.search(r'type\s*=\s*["\']?password', html, re.I))
        has_account = bool(re.search(r'name\s*=\s*["\']?DDDDD', html, re.I))
        has_logout = bool(re.search(r"注销|已经成功登录", html))
        info["needs_login"] = has_password or has_account
        info["has_logout"] = has_logout
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def _guess_encoding(resp: requests.Response) -> str:
    m = re.search(rb'charset\s*=\s*["\']?([\w-]+)', resp.content[:2048], re.I)
    if m:
        return m.group(1).decode("ascii", "ignore")
    return resp.apparent_encoding or "gbk"


# --------------------------------------------------------------------------- #
# 字段探测
# --------------------------------------------------------------------------- #
def discover_fields(html: str) -> list[dict]:
    """从登录页 HTML 解析需要填写的项目。"""
    if not html:
        return list(DEFAULT_FIELDS)

    fields: list[dict] = []
    for m in re.finditer(r"<input\b[^>]*>", html, re.I):
        tag = m.group(0)
        name = _attr(tag, "name") or _attr(tag, "id")
        itype = (_attr(tag, "type") or "text").lower()
        if not name or itype in {"hidden", "button", "submit", "reset", "image", "checkbox", "radio"}:
            continue
        if name in CARRIER_FIELD_NAMES:  # 运营商交给 discover_carriers 统一处理
            continue
        fields.append({
            "name": name,
            "label": KNOWN_LABELS.get(name, _nearby_text(html, m.start()) or name),
            "kind": "password" if itype == "password" else "text",
            "secret": itype == "password" or name in SECRET_FIELDS,
            "required": True,
        })

    for m in re.finditer(r"<select\b[^>]*>.*?</select>", html, re.I | re.S):
        tag = m.group(0)
        name = _attr(tag, "name") or _attr(tag, "id")
        if not name or name in CARRIER_FIELD_NAMES:
            continue
        options = [_strip_tags(o) for o in re.findall(r"<option\b[^>]*>(.*?)</option>", tag, re.I | re.S)]
        options = [o for o in options if o]
        fields.append({
            "name": name,
            "label": KNOWN_LABELS.get(name, _nearby_text(html, m.start()) or name),
            "kind": "select",
            "secret": False,
            "required": True,
            "options": options,
        })

    return fields or list(DEFAULT_FIELDS)


def _attr(tag: str, key: str) -> str | None:
    m = re.search(rf'{key}\s*=\s*["\']?([^"\' >]+)', tag, re.I)
    return m.group(1) if m else None


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


CARRIER_FIELD_NAMES = {"ISP_select", "ISP_radio", "R3"}


def discover_carriers(html: str) -> list[dict]:
    """从门户页面抓取“服务类型 / 运营商”选项。

    Dr.COM 门户会把配置内联成 JS 变量：
        carrier='{"yys":{"title":"服务类型","data":[{"id":"1","name":"校园用户","suffix":""}, ...]}}';
    实测该 JSON 常缺引号（`"title": "服务类型,"mode"`），json.loads 会失败，
    因此优先尝试标准解析，失败后用正则逐项提取。
    没有内联配置时，退回到 <select name="ISP_select"> 的 option。
    """
    if not html:
        return []

    out: list[dict] = []
    m = re.search(r"carrier\s*=\s*'(.*?)'\s*;", html, re.S)
    if m:
        raw = m.group(1)
        default_id = ""
        dm = re.search(r'"defaultID"\s*:\s*"?(\d+)"?', raw)
        if dm:
            default_id = dm.group(1)

        data = None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None

        if isinstance(data, dict):
            for group in data.values():
                if not isinstance(group, dict):
                    continue
                gid = str(group.get("defaultID") or default_id)
                for item in group.get("data") or []:
                    if isinstance(item, dict) and item.get("name"):
                        out.append({
                            "name": str(item["name"]).strip(),
                            "suffix": str(item.get("suffix") or ""),
                            "id": str(item.get("id") or ""),
                            "default": str(item.get("id") or "") == gid,
                        })
        if not out:  # JSON 残缺时的兜底
            for im in re.finditer(
                r'\{\s*"id"\s*:\s*"?(\d+)"?\s*,\s*'
                r'"name"\s*:\s*"([^"]*)"\s*,\s*'
                r'"suffix"\s*:\s*"([^"]*)"\s*\}',
                raw,
            ):
                out.append({
                    "name": im.group(2).strip(),
                    "suffix": im.group(3),
                    "id": im.group(1),
                    "default": im.group(1) == default_id,
                })

    if not out:
        for sel in re.finditer(
            r"<select\b[^>]*name\s*=\s*[\"']?ISP_select[\"']?[^>]*>(.*?)</select>",
            html, re.I | re.S,
        ):
            for om in re.finditer(r"<option\b([^>]*)>(.*?)</option>", sel.group(1), re.I | re.S):
                name = _strip_tags(om.group(2))
                if not name:
                    continue
                value = _attr("<option" + om.group(1) + ">", "value") or ""
                out.append({"name": name, "suffix": value})

    # 去重（同名同后缀只留一份）
    seen: set[tuple[str, str]] = set()
    uniq: list[dict] = []
    for c in out:
        key = (c["name"], c["suffix"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


def _nearby_text(html: str, pos: int, radius: int = 300) -> str:
    window = html[max(0, pos - radius): pos]
    texts = re.findall(r">([^<>{}]{2,20})<", window)
    return texts[-1].strip() if texts else ""


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    ensure_data_dir()
    migrate_legacy_config()
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"配置文件损坏，将重新创建：{exc}")
    return {}


def save_config(cfg: dict) -> None:
    ensure_data_dir()
    cfg["saved_at"] = datetime.now().isoformat(timespec="seconds")
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:  # 仅当前用户可读
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    log(f"配置已保存：{CONFIG_PATH}")


def config_values(cfg: dict) -> dict:
    return cfg.get("values") or {}


def config_complete(cfg: dict) -> bool:
    fields = cfg.get("fields") or []
    values = config_values(cfg)
    return bool(fields) and all(
        (values.get(f["name"]) or not f.get("required", True)) for f in fields
    )


# --------------------------------------------------------------------------- #
# 交互填写
# --------------------------------------------------------------------------- #
def ask_values(fields: list[dict], old: dict | None = None) -> dict:
    old = old or {}
    values: dict[str, str] = {}
    print("\n请填写以下登录信息（配置保存后不再询问）：")
    for f in fields:
        name, label = f["name"], f.get("label") or f["name"]
        preset = old.get(name, "")
        options = [o for o in (f.get("options") or []) if o]
        if options:
            print(f"  · {label} 可选值：" + "  ".join(f"{i + 1}){o}" for i, o in enumerate(options)))
        hint = f"（直接回车沿用 {('*' * 6) if f.get('secret') and preset else preset}）" if preset else ""
        while True:
            if f.get("secret"):
                val = getpass.getpass(f"  {label}{hint}: ") or preset
            else:
                val = input(f"  {label}{hint}: ").strip() or preset
            if options and val.isdigit() and 1 <= int(val) <= len(options):
                val = options[int(val) - 1]
            if val or not f.get("required", True):
                break
            print("    ⚠ 该项不能为空")
        values[name] = val
    return values


def carrier_suffix_of(values: dict, cfg: dict | None = None) -> str:
    """把页面上的“服务类型”选项映射成账号后缀。"""
    table = {c["name"]: c.get("suffix", "") for c in carrier_table(cfg or {})}
    table.update({"校园电信": "@dx", "校园联通": "@lt", "校园移动": "@cmcc"})
    for val in values.values():
        if val in table:
            return table[val]
    return ""


# --------------------------------------------------------------------------- #
# 运营商选择
# --------------------------------------------------------------------------- #
def carrier_table(cfg: dict) -> list[dict]:
    """可选的运营商列表（可在 config.json 里用 carriers 覆盖）。"""
    table = cfg.get("carriers")
    if isinstance(table, list) and table:
        return [c for c in table if isinstance(c, dict) and c.get("name")]
    return list(CARRIERS)


def format_carrier(carrier: dict) -> str:
    suffix = carrier.get("suffix") or ""
    return f"{carrier.get('name', '未知')}（{suffix or '无后缀'}）"


def merge_carriers(cfg: dict, scraped: list[dict]) -> list[dict]:
    """以门户抓取结果为准，本地自定义的额外项保留在后面。"""
    names = {c.get("name") for c in scraped}
    clean = [{"name": c["name"], "suffix": c.get("suffix", "")} for c in scraped]
    extras = [
        {"name": c["name"], "suffix": c.get("suffix", "")}
        for c in (cfg.get("carriers") or [])
        if isinstance(c, dict) and c.get("name") and c["name"] not in names
    ]
    return clean + extras


def current_carrier(cfg: dict) -> dict:
    opts = cfg.get("options") or {}
    return {"name": opts.get("carrier") or "校园用户", "suffix": opts.get("carrier_suffix") or ""}


def carrier_by_suffix(cfg: dict, suffix: str) -> dict:
    for c in carrier_table(cfg):
        if (c.get("suffix") or "") == suffix:
            return c
    return {"name": suffix or "校园用户", "suffix": suffix}


def remember_suffix(cfg: dict, suffix: str) -> dict:
    """从真实登录请求里发现了新的运营商后缀时，补进 carriers 并设为当前项。"""
    suffix = suffix or ""
    carrier = carrier_by_suffix(cfg, suffix)
    known = {(c.get("suffix") or "") for c in carrier_table(cfg)}
    if suffix not in known:
        name = SUFFIX_NAMES.get(suffix, suffix or "校园用户")
        cfg.setdefault("carriers", list(CARRIERS))
        cfg["carriers"].append({"name": name, "suffix": suffix})
        carrier = {"name": name, "suffix": suffix}
    set_carrier(cfg, carrier)
    return carrier


def resolve_carrier(cfg: dict, text: str) -> dict:
    """支持：序号 / 名称 / 后缀（@dx）/ 自定义后缀。"""
    text = (text or "").strip()
    table = carrier_table(cfg)
    if not text:
        return {"name": "校园用户", "suffix": ""}
    if text.isdigit() and 1 <= int(text) <= len(table):
        return table[int(text) - 1]
    for c in table:
        if text == c.get("name") or text == (c.get("suffix") or ""):
            return c
    if text.startswith("@"):
        return {"name": SUFFIX_NAMES.get(text, text), "suffix": text}
    # 只写英文（dx / lt / cmcc）时按后缀理解
    if re.fullmatch(r"[A-Za-z]{2,8}", text):
        suffix = f"@{text.lower()}"
        return {"name": SUFFIX_NAMES.get(suffix, suffix), "suffix": suffix}
    # 名称里带后缀的写法，如“校园电信@dx”
    m = re.search(r"@\w+$", text)
    if m:
        return {"name": text[: m.start()], "suffix": m.group(0)}
    return {"name": text, "suffix": ""}


def set_carrier(cfg: dict, carrier: dict) -> None:
    opts = cfg.setdefault("options", {})
    opts["carrier"] = carrier.get("name") or "校园用户"
    opts["carrier_suffix"] = carrier.get("suffix") or ""


def ask_carrier(cfg: dict) -> dict:
    table = carrier_table(cfg)
    cur = current_carrier(cfg)
    print("\n请选择运营商 / 服务类型（会自动拼到账号后面）：")
    print("  " + "   ".join(f"{i + 1}){format_carrier(c)}" for i, c in enumerate(table)))
    print("  也可以直接输入后缀（如 @cmcc）或回车沿用当前设置")
    while True:
        raw = input(f"  运营商（当前 {format_carrier(cur)}）: ").strip()
        if not raw:
            return cur
        return resolve_carrier(cfg, raw)


def list_carriers(cfg: dict) -> None:
    log("可选运营商 / 服务类型：")
    for i, c in enumerate(carrier_table(cfg), 1):
        log(f"  {i}) {format_carrier(c)}")
    log(f"当前设置：{format_carrier(current_carrier(cfg))}")


def open_in_editor() -> None:
    """无法交互时，打开记事本让用户手工填写。"""
    ensure_data_dir()
    template = {
        "fields": list(DEFAULT_FIELDS),
        "values": {"DDDDD": "", "upass": ""},
        "carriers": list(CARRIERS),
        "options": {"account_prefix": ",0,", "carrier": "校园用户", "carrier_suffix": ""},
    }
    CONFIG_PATH.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"已生成配置模板，请填写账号密码后保存：{CONFIG_PATH}")
    try:
        subprocess.Popen(["notepad.exe", str(CONFIG_PATH)])
        input("填写并保存后按回车继续...")
    except OSError:
        pass


def learn_from_captured(cfg: dict, captured: dict) -> dict | None:
    """从抓到的真实登录请求里解析账号后缀（运营商），写回配置。"""
    url = captured.get("url") or ""
    if "?" not in url:
        return None
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}
    raw = query.get("user_account") or query.get("DDDDD") or ""
    if not raw:
        return None
    acct = unquote(raw)
    for prefix in (",0,", ",1,", ",a,", ",b,", "__", "_"):
        if acct.startswith(prefix):
            acct = acct[len(prefix):]
            break
    base = config_values(cfg).get("DDDDD") or ""
    if not base or not acct.startswith(base):
        return None
    suffix = acct[len(base):]
    if not suffix.startswith("@"):
        return None
    carrier = remember_suffix(cfg, suffix)
    log(f"从真实登录请求识别到运营商：{format_carrier(carrier)}")
    return carrier


# --------------------------------------------------------------------------- #
# 接口登录
# --------------------------------------------------------------------------- #
# 本地认证（login_method=0）的固定参数
LOCAL_EXTRA = {
    "0MKKey": "123456",
    "R1": "",
    "R2": "",
    "R3": "",
    "R6": "0",
    "para": "",
    "v6ip": "",
}


def _parse_jsonp(text: str) -> dict | None:
    start, end = text.find("("), text.rfind(")")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start + 1:end])
    except json.JSONDecodeError:
        return None


def _captured_param_set(
    cfg: dict, bind_ip: str | None, account: str, password: str
) -> dict | None:
    """把浏览器里抓到的真实登录请求当作模板重放（参数最贴合门户要求）。"""
    captured = (cfg.get("login") or {}).get("captured") or {}
    url = captured.get("url") or ""
    if "?" not in url:
        return None
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}
    if not query:
        return None
    for key in ("user_account", "DDDDD"):
        if key in query:
            query[key] = account
    for key in ("user_password", "upass"):
        if key in query:
            query[key] = password
    if "wlan_user_ip" in query and bind_ip:
        query["wlan_user_ip"] = bind_ip
    query.pop("callback", None)  # 由调用方统一补
    return query


def _param_sets(cfg: dict, bind_ip: str | None) -> list[dict]:
    """生成候选登录参数。

    南工大门户的 login_method=1（Portal 协议），字段为 user_account / user_password；
    同时保留 login_method=0（本地认证 DDDDD / upass）的写法作为兜底。
    账号还可能需要“无感知前缀”(,0,) 或运营商后缀(@dx/@lt/@cmcc)。
    """
    values = config_values(cfg)
    account = values.get("DDDDD") or values.get("username") or values.get("user_account") or ""
    password = values.get("upass") or values.get("password") or values.get("user_password") or ""
    options = cfg.get("options") or {}
    suffix = options.get("carrier_suffix", "") or ""
    prefix = options.get("account_prefix", ",0,") or ""
    # 账号已自带 @xx 时不再拼接运营商后缀
    if not suffix or "@" in account:
        base = account
    else:
        base = f"{account}{suffix}"

    accounts = []
    for cand in (f"{prefix}{base}" if prefix else None, base):
        if cand and cand not in accounts:
            accounts.append(cand)

    sets: list[dict] = []
    # 优先用“抓到的真实请求”模板
    captured = _captured_param_set(cfg, bind_ip, accounts[0], password)
    if captured:
        sets.append(captured)
    for acct in accounts:
        sets.append({
            "login_method": "1",
            "user_account": acct,
            "user_password": password,
            "wlan_user_ip": bind_ip or "",
            "wlan_user_ipv6": "",
            "wlan_user_mac": "",
            "wlan_ac_ip": "",
            "wlan_ac_name": "",
            "jsVersion": "4.X",
            "terminal_type": "1",
            "lang": "zh",
        })
    for acct in accounts:
        params = dict(LOCAL_EXTRA)
        params.update({"DDDDD": acct, "upass": password, "terminal_type": "1", "lang": "zh"})
        sets.append(params)
    return sets


def login_via_api(cfg: dict, bind_ip: str | None = None) -> tuple[str, str]:
    """返回 (状态, 说明)，状态 ∈ {ok, already, fail}。"""
    values = config_values(cfg)
    if not (values.get("DDDDD") or values.get("username")) or not (
        values.get("upass") or values.get("password")
    ):
        return "fail", "配置缺少账号或密码"

    sess = make_session(bind_ip)
    last = "未知错误"
    for params in _param_sets(cfg, bind_ip):
        payload = {"callback": "dr1000"}
        payload.update(params)
        try:
            r = sess.get(API_BASE + "login", params=payload, timeout=PROBE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            return "fail", f"接口请求失败：{type(exc).__name__}: {exc}"
        data = _parse_jsonp(r.text)
        if not data:
            last = f"返回非预期内容：{r.text[:120]}"
            continue
        msg = str(data.get("msg") or "")
        if str(data.get("result")) in ("1", "ok"):
            return "ok", msg or "登录成功"
        if "已经在线" in msg:
            return "already", msg
        last = f"result={data.get('result')} msg={msg}"
        log(f"  该参数组合未通过：{last}")
    return "fail", last


# --------------------------------------------------------------------------- #
# 浏览器登录（回退方案，兼“学习”真实请求）
# --------------------------------------------------------------------------- #
def login_via_browser(
    cfg: dict, headless: bool = False, bind_ip: str | None = None
) -> tuple[bool, str, dict | None]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "未安装 playwright", None

    fields = cfg.get("fields") or []
    values = config_values(cfg)
    captured: dict = {}
    filled = 0
    title = ""
    note = ""

    def on_request(req):
        if "login" in req.url.lower() and req.resource_type in ("xhr", "fetch", "script", "document"):
            if "loadConfig" in req.url:
                return
            captured.update({"method": req.method, "url": req.url, "post": req.post_data})

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=headless)
            try:
                page = browser.new_page(user_agent=UA)
                page.set_default_timeout(15000)
                page.on("request", on_request)
                page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2500)

                missing = []
                for f in fields:
                    val = values.get(f["name"])
                    if not val or f["kind"] in {"radio"} or page.is_closed():
                        continue
                    selector = f"[name='{f['name']}']"
                    try:
                        # count() 不等待，能立刻判断页面上有没有这个字段
                        if page.locator(selector).count() == 0:
                            missing.append(f["name"])
                            continue
                        if f["kind"] == "select":
                            page.select_option(selector, label=val)
                        else:
                            page.fill(selector, val)
                        filled += 1
                    except Exception as exc:  # noqa: BLE001
                        note = f"字段 {f['name']} 填充失败"
                        log(f"  {note}：{str(exc).splitlines()[0]}")
                if missing and not filled:
                    note = f"页面上没有登录表单（缺字段 {', '.join(missing)}），可能已在线"
                    log(f"  {note}")

                clicked = False
                if filled:
                    for sel in ("input[type=submit]", "button[type=submit]", "[name=login]",
                                "#login", ".login-btn", "button.login", "input[type=button]"):
                        if page.is_closed():
                            break
                        try:
                            el = page.query_selector(sel)
                            if el and el.is_visible():
                                el.click()
                                clicked = True
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    if not clicked and not page.is_closed():
                        try:
                            page.press("[name=upass]", "Enter")
                            clicked = True
                        except Exception:  # noqa: BLE001
                            pass

                try:
                    page.wait_for_timeout(3500)
                    title = page.title()
                except Exception as exc:  # noqa: BLE001
                    note = "页面在提交后被关闭"
                    log(f"  {note}：{str(exc).splitlines()[0]}")
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001
        # 浏览器起不来 / 被手动关闭 / 驱动异常，都不能让主流程崩掉
        return False, f"浏览器流程异常：{type(exc).__name__}: {str(exc).splitlines()[0]}", (captured or None)

    # 浏览器侧也验证一次网络
    ok = wait_online(bind_ip, 25)
    detail = (
        f"浏览器{'已提交' if filled else '未能填充'}（填充 {filled} 项"
        + (f"，标题：{title}" if title else "")
        + (f"，备注：{note}" if note else "")
        + f"），网络{'已通' if ok else '未通'}"
    )
    return ok, detail, (captured or None)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def report_status(bind_ip: str | None) -> dict:
    campus_online = is_online(bind_ip) if bind_ip else None
    # 校园网已通时不必再重复探测“本机外网”
    other_online = True if campus_online else is_online()
    state = portal_state(bind_ip)
    log("— 网络状态 ————————————————————————")
    log(f"  校园网出口 IP : {bind_ip or '未找到（检查网线/校园网连接）'}")
    saved = load_config()
    if saved.get("options"):
        log(f"  当前运营商   : {format_carrier(current_carrier(saved))}")
    if bind_ip:
        log(f"  校园网外网   : {'✅ 已认证，可上网' if campus_online else '❌ 未认证（被门户拦截）'}")
    log(f"  本机外网     : {'✅ 可用' if other_online else '❌ 不可用'}")
    log(f"  门户可达     : {'✅' if state['reachable'] else '❌ ' + str(state.get('error', ''))}")
    if state["reachable"]:
        # 门户页面只作参考：Dr.COM 即使未认证也可能返回“注销页”模板
        if state.get("needs_login"):
            desc = "登录页（需要认证）"
        elif state.get("has_logout"):
            desc = "注销页模板（页面本身不能判断是否在线，以上面探测结果为准）"
        else:
            desc = "未知模板"
        log(f"  门户页面     : {desc}")
    return {
        "campus_online": campus_online,
        "other_online": other_online,
        "portal": state,
        "bind_ip": bind_ip,
        "carrier": current_carrier(saved) if saved else None,
    }


def sync_carriers(cfg: dict, html: str) -> bool:
    """把门户页面上的运营商选项同步进配置，返回配置是否发生变化。"""
    scraped = discover_carriers(html)
    if not scraped:
        log("门户页面未提供运营商选项，沿用本地配置列表。")
        return False

    merged = merge_carriers(cfg, scraped)
    changed = merged != cfg.get("carriers")
    if changed:
        cfg["carriers"] = merged
    log("从门户抓取到可选运营商：")
    for i, c in enumerate(merged, 1):
        log(f"    {i}) {format_carrier(c)}")

    cur = current_carrier(cfg)
    if (cfg.get("options") or {}).get("carrier") and cur not in merged:
        log(f"    注意：当前设置的 {format_carrier(cur)} 不在门户列表中，仍按配置使用。")

    if not (cfg.get("options") or {}).get("carrier"):
        default = next((c for c in scraped if c.get("default")), scraped[0])
        set_carrier(cfg, {"name": default["name"], "suffix": default.get("suffix", "")})
        log(f"    未设置过运营商，按门户默认值取：{format_carrier(current_carrier(cfg))}")
        changed = True
    return changed


def run(args: argparse.Namespace) -> int:
    log("===== 校园网自动登录 =====")

    # 0) 先处理运营商设置（即使当前已在线也要能改配置）
    cfg = load_config()
    cfg.setdefault("portal", {"url": PORTAL_URL, "api_base": API_BASE})
    cfg.setdefault("options", {"account_prefix": ",0,", "carrier_suffix": ""})
    cfg.setdefault("carriers", list(CARRIERS))
    if args.carrier:
        carrier = resolve_carrier(cfg, args.carrier)
        # 自定义后缀会顺带补进 carriers 列表，下次仍可选
        carrier = remember_suffix(cfg, carrier.get("suffix") or "")
        save_config(cfg)
        log(f"运营商已设为：{format_carrier(carrier)}")

    bind_ip = detect_campus_ip()
    status = report_status(bind_ip)

    if not status["portal"]["reachable"]:
        log("门户不可达，确认已连接校园网（有线/WiFi）后重试。")
        return 2

    # 运营商选项随账号密码一起持久化（即使当前已在线也同步一次）
    html = status["portal"]["html"]
    if sync_carriers(cfg, html):
        save_config(cfg)

    if status["campus_online"] and not args.force:
        log("校园网已处于登录状态，无需操作。")
        return 0

    # 1) 探测需要填写的项目
    cfg_dirty = False
    if args.setup or not cfg.get("fields"):
        fields = discover_fields(html) if status["portal"]["needs_login"] else (cfg.get("fields") or list(DEFAULT_FIELDS))
        cfg["fields"] = fields
        log("检测到需要填写以下项目：")
        for f in fields:
            note = f"（可选：{', '.join(f['options'])}）" if f.get("options") else ""
            log(f"    - {f['label']}  [字段名 {f['name']}, 类型 {f['kind']}] {note}")
        cfg_dirty = True

    if cfg_dirty:
        save_config(cfg)

    # 2) 已填写过则直接使用，否则询问
    if args.setup or not config_complete(cfg):
        if sys.stdin and sys.stdin.isatty():
            values = ask_values(cfg["fields"], config_values(cfg))
            cfg["values"] = {**config_values(cfg), **values}
            # 页面上若有“服务类型”选项，先按其取值；否则明确询问
            page_suffix = carrier_suffix_of(values, cfg)
            if page_suffix:
                set_carrier(cfg, carrier_by_suffix(cfg, page_suffix))
            if args.setup or not (cfg.get("options") or {}).get("carrier"):
                set_carrier(cfg, ask_carrier(cfg))
            save_config(cfg)
        else:
            open_in_editor()
            cfg = load_config()
            if not config_complete(cfg):
                log("配置仍不完整，已退出。")
                return 3
    else:
        log(f"检测到已保存的配置（{cfg.get('saved_at', '未知时间')}），跳过询问。")

    log(f"运营商：{format_carrier(current_carrier(cfg))}")

    # 3) 登录：优先接口，失败回退浏览器
    ok = False
    reason = ""
    state = ""
    if not args.browser:
        log("尝试接口登录 ...")
        state, reason = login_via_api(cfg, bind_ip)
        log(f"  接口返回：{state} - {reason}")
        if state in ("ok", "already"):
            log("  等待认证生效 ...")
            ok = wait_online(bind_ip, 25)
            if not ok and state == "already":
                # AC 里可能残留了失效会话：先注销，再重新登录
                log("  接口说“已经在线”但网络不通，先注销残留会话再重登 ...")
                done, msg = logout(bind_ip)
                log(f"  注销：{'成功' if done else '失败'} - {msg}")
                time.sleep(2)
                state, reason = login_via_api(cfg, bind_ip)
                log(f"  重登接口返回：{state} - {reason}")
                ok = state in ("ok", "already") and wait_online(bind_ip, 25)
        elif state == "fail":
            ok = False

    if not ok:
        if state in ("ok", "already"):
            log(f"  接口已回复认证成功，但网络未在等待期内生效（可能系统侧延迟）：{reason}")
        log("回退到浏览器登录 ...")
        ok, reason, captured = login_via_browser(cfg, headless=args.headless, bind_ip=bind_ip)
        log(f"  结果：{reason}")
        if captured:
            cfg.setdefault("login", {})["captured"] = captured
            learn_from_captured(cfg, captured)
            save_config(cfg)
            log("已记录真实登录请求，后续可优先用接口复现。")

    # 4) 最终状态
    final = report_status(detect_campus_ip())
    if ok and final["campus_online"]:
        log("✅ 认证成功，网络已连通。")
        return 0
    log("❌ 认证未完成，请检查账号密码或运营商选项。")
    return 1


def logout(bind_ip: str | None = None) -> tuple[bool, str]:
    """注销当前 IP 的在线会话（调试/测试用）。"""
    sess = make_session(bind_ip)
    try:
        r = sess.get(API_BASE + "logout", params={"callback": "dr1001"}, timeout=PROBE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return False, f"请求失败：{type(exc).__name__}: {exc}"
    data = _parse_jsonp(r.text)
    if data and str(data.get("result")) in ("1", "ok"):
        return True, str(data.get("msg") or "注销成功")
    return False, str((data or {}).get("msg") or r.text[:120])


# --------------------------------------------------------------------------- #
# 开机自动登录（Windows 计划任务）
# --------------------------------------------------------------------------- #
def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _run_ps(script: str, timeout: float = 60.0) -> tuple[bool, str]:
    """执行 PowerShell 脚本，返回 (是否成功, 输出)。"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, output


def is_admin() -> bool:
    """当前进程是否具有管理员权限。"""
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _run_ps_elevated(script: str, timeout: float = 240.0) -> tuple[bool, str]:
    """用 UAC 提权执行 PowerShell（会弹一次授权框）。"""
    import tempfile

    log("需要管理员权限，正在请求 UAC 授权（可能弹出确认框）...")
    fd, path = tempfile.mkstemp(prefix="njtech-", suffix=".ps1")
    os.close(fd)
    try:
        Path(path).write_text(script, encoding="utf-8-sig")
        inner = (
            "$p = Start-Process -FilePath 'powershell' -Verb RunAs -PassThru -Wait -WindowStyle Hidden "
            "-ArgumentList '-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',"
            f"{_ps_quote(path)}; exit $p.ExitCode"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", inner],
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return False, output or "提权失败（可能取消了 UAC 授权）"
    return True, output


def pythonw_path() -> str:
    """优先用 pythonw.exe 运行，避免计划任务弹黑框。"""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def launch_command() -> tuple[str, str]:
    """计划任务要执行的 (可执行文件, 参数)；打包后直接指向 exe 自身。"""
    if FROZEN:
        return str(Path(sys.executable).resolve()), "--auto"
    return pythonw_path(), '"' + str(Path(__file__).resolve()) + '" --auto'


def task_installed() -> bool:
    ok, out = _run_ps(
        f"$t = Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue; "
        "if ($t) { 'INSTALLED' } else { 'MISSING' }"
    )
    return ok and "INSTALLED" in out


def _task_register_script(exe: str, argument: str, highest: bool) -> str:
    principal = ""
    if highest:
        principal = (
            " -Principal (New-ScheduledTaskPrincipal -UserId $env:USERNAME "
            "-LogonType Interactive -RunLevel Highest)"
        )
    return (
        f"$a = New-ScheduledTaskAction -Execute {_ps_quote(exe)} "
        f"-Argument {_ps_quote(argument)} -WorkingDirectory {_ps_quote(str(ROOT))}; "
        "$t = New-ScheduledTaskTrigger -AtLogOn; "
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30); "
        f"Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Action $a -Trigger $t "
        f"-Settings $s{principal} -Description '登录校园网（Njtech Home Autologin）' -Force | Out-Null; 'OK'"
    )


def install_task() -> tuple[bool, str]:
    """注册“登录时自动运行”的计划任务（按需提权）。

    打包后的 exe 不再强制管理员运行，所以先用当前权限注册；
    仅当权限不够时才弹 UAC 提权，避免每次启动都提示。
    """
    exe, argument = launch_command()
    normal = _task_register_script(exe, argument, highest=False)

    ok, out = _run_ps(normal)
    if ok and task_installed():
        return True, f"已注册计划任务 {TASK_NAME}（登录 Windows 后自动联网）"

    if is_admin():
        ok, out = _run_ps(_task_register_script(exe, argument, highest=True))
        if ok and task_installed():
            return True, f"已注册计划任务 {TASK_NAME}（最高权限，登录 Windows 后自动联网）"
        return False, out or "注册计划任务失败"

    # 非管理员：按需提权重试一次
    ok, out = _run_ps_elevated(normal)
    if ok and task_installed():
        return True, f"已注册计划任务 {TASK_NAME}（已提权注册，登录 Windows 后自动联网）"
    return False, out or "注册计划任务失败（可能取消了 UAC 授权）"


def uninstall_task() -> tuple[bool, str]:
    script = f"Unregister-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Confirm:$false; 'OK'"
    ok, out = _run_ps(script)
    if ok and not task_installed():
        return True, f"已移除计划任务 {TASK_NAME}"
    if not is_admin():
        ok, out = _run_ps_elevated(script)
        if ok and not task_installed():
            return True, f"已移除计划任务 {TASK_NAME}"
    return False, out or "移除计划任务失败（可能本来就未安装）"


def run_auto(args: argparse.Namespace) -> int:
    """开机静默模式：等待网络就绪并自动重试。"""
    args.headless = True
    for attempt in range(1, 13):
        log(f"===== 开机自动登录（第 {attempt} 次尝试）=====")
        code = run(args)
        if code == 0:
            return 0
        if code == 2 and attempt < 12:  # 门户还不可达，等网络起来
            time.sleep(10)
            continue
        if code == 1 and attempt < 3:   # 认证失败，稍后重试
            time.sleep(5)
            continue
        return code
    return 1


def launch_gui() -> int:
    """启动图形界面（tkinter）。"""
    try:
        import gui
    except Exception as exc:  # noqa: BLE001
        log(f"无法加载图形界面：{type(exc).__name__}: {exc}")
        return 5
    return gui.launch()


def main() -> int:
    parser = argparse.ArgumentParser(description="南工大校园网自动登录（Dr.COM EPortal）")
    parser.add_argument("--setup", action="store_true", help="重新探测字段并重新填写")
    parser.add_argument("--status", action="store_true", help="只打印网络状态")
    parser.add_argument("--logout", action="store_true", help="注销当前在线会话")
    parser.add_argument(
        "--carrier", metavar="运营商",
        help="设置运营商：序号 / 校园用户 / 校园电信(@dx) / 校园联通(@lt)，或直接写后缀如 @cmcc",
    )
    parser.add_argument("--list-carriers", action="store_true", help="列出可选运营商")
    parser.add_argument("--force", action="store_true", help="已在线也强制登录")
    parser.add_argument("--browser", action="store_true", help="跳过接口，直接用浏览器")
    parser.add_argument("--headless", action="store_true", help="浏览器模式无界面")
    parser.add_argument("--auto", action="store_true", help="开机静默模式：等待网络就绪并自动重试")
    parser.add_argument("--gui", action="store_true", help="启动图形界面")
    parser.add_argument("--install-task", action="store_true", help="注册开机自动登录计划任务")
    parser.add_argument("--uninstall-task", action="store_true", help="移除开机自动登录计划任务")
    parser.add_argument("--task-status", action="store_true", help="查看计划任务是否已安装")
    parser.add_argument("--where", action="store_true", help="显示配置与日志的保存位置")
    args = parser.parse_args()

    # 提示：双击 main.pyw 可免控制台启动界面；计划任务请用 CLI 参数，避免弹窗
    if args.gui:
        return launch_gui()

    # 打包成窗口版 exe（无控制台）时：没带任何命令就打开界面；
    # 带 --auto 等参数时仍按命令行静默执行（供计划任务调用）
    cli_intent = any([
        args.setup, args.status, args.logout, args.list_carriers, args.carrier,
        args.force, args.browser, args.headless, args.auto,
        args.install_task, args.uninstall_task, args.task_status, args.where,
    ])
    if FROZEN and sys.stdout is None and not cli_intent:
        return launch_gui()

    if args.where:
        exists = CONFIG_PATH.exists()
        log(f"数据目录：{DATA_DIR}")
        log(f"配置文件：{CONFIG_PATH}（{'已存在' if exists else '尚未创建'}）")
        log(f"日志目录：{LOG_DIR}")
        return 0
    if args.install_task:
        ok, msg = install_task()
        log(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1
    if args.uninstall_task:
        ok, msg = uninstall_task()
        log(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1
    if args.task_status:
        log("计划任务：" + ("已安装（登录后自动联网）" if task_installed() else "未安装"))
        return 0
    if args.list_carriers:
        list_carriers(load_config())
        return 0
    if args.status:
        report_status(detect_campus_ip())
        return 0
    if args.logout:
        ok, msg = logout(detect_campus_ip())
        log(("注销成功：" if ok else "注销失败：") + msg)
        return 0 if ok else 1
    try:
        return run_auto(args) if args.auto else run(args)
    except KeyboardInterrupt:
        log("已手动中断。")
        return 130
    except Exception as exc:  # noqa: BLE001  最后一道防线，避免堆栈刷屏
        log(f"运行异常：{type(exc).__name__}: {exc}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
