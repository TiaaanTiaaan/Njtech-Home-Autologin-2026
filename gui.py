#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""南工大校园网自动登录 —— tkinter 图形界面。

入口：``python main.py --gui``，或直接双击 ``main.pyw`` / 用 pythonw 运行 main.py。

界面分四块：网络状态 / 账号设置 / 操作按钮 / 运行日志。
所有网络操作都在后台线程执行，日志通过队列回到主线程，界面不会卡死。
"""
from __future__ import annotations

import argparse
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import main as core

try:  # 托盘依赖（缺失时退化为最小化到任务栏）
    import pystray
    from PIL import Image, ImageDraw, ImageTk

    HAS_TRAY = True
except ImportError:  # pragma: no cover
    pystray = None
    HAS_TRAY = False

DONE = object()
FONT = "Microsoft YaHei UI"


def icon_path() -> Path | None:
    """定位 app.ico：打包后在 _MEIPASS，源码运行在 assets/ 下。"""
    candidates = []
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        if base:
            candidates.append(Path(base) / "app.ico")
    candidates.append(core.ROOT / "assets" / "app.ico")
    for path in candidates:
        if path.exists():
            return path
    return None


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.queue: queue.Queue = queue.Queue()
        self.busy = False
        self.pending_refresh = False
        self.cfg = core.load_config()
        self.carriers = core.carrier_table(self.cfg)
        self.tray = None
        self._last_online = None

        root.title("南工大校园网自动登录")
        root.geometry("680x580")
        root.minsize(600, 500)
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
        self._apply_window_icon()

        core.set_log_sink(self._enqueue_log)
        core.log(f"数据目录：{core.DATA_DIR}")
        self._build()
        self._refresh_carrier_box()
        self._fill_form()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._pump()
        self.refresh_status()
        self._auto_tick()
        self._load_autostart_state()

    # ------------------------------------------------------------------ UI --
    def _apply_window_icon(self) -> None:
        """标题栏 / 任务栏 / 托盘用同一个图标。

        Tk 的 iconbitmap 在 Windows 上对含 PNG 帧的 ico 支持不稳定，
        所以统一用 iconphoto（与托盘图标同一张图），保证三处一致。
        """
        if not HAS_TRAY:
            return
        image = None
        path = icon_path()
        if path is not None:
            try:
                image = Image.open(path).convert("RGBA")
            except Exception:  # noqa: BLE001
                image = None
        if image is None:
            image = self._tray_image()
        try:
            self._icon_image = ImageTk.PhotoImage(image.resize((64, 64), Image.LANCZOS))
            self.root.iconphoto(True, self._icon_image)
        except Exception:  # noqa: BLE001
            pass

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        # 网络状态
        box = ttk.LabelFrame(outer, text="网络状态", padding=10)
        box.pack(fill="x")
        self.var_state = tk.StringVar(value="检测中 …")
        self.var_ip = tk.StringVar(value="-")
        self.var_carrier = tk.StringVar(value="-")
        self.lbl_state = tk.Label(
            box, textvariable=self.var_state, font=(FONT, 12, "bold"), fg="#666666",
        )
        self.lbl_state.grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(box, text="出口 IP：", font=(FONT, 9)).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(box, textvariable=self.var_ip, font=(FONT, 9)).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Label(box, text="运营商：", font=(FONT, 9)).grid(row=1, column=2, sticky="w", pady=(8, 0), padx=(16, 0))
        ttk.Label(box, textvariable=self.var_carrier, font=(FONT, 9)).grid(row=1, column=3, sticky="w", pady=(8, 0))

        # 账号设置
        form = ttk.LabelFrame(outer, text="账号设置（保存后不再询问）", padding=10)
        form.pack(fill="x", pady=(10, 0))
        form.columnconfigure(1, weight=1)

        self.field_vars: dict[str, tuple[tk.StringVar, dict, ttk.Entry]] = {}
        row = 0
        for f in (self.cfg.get("fields") or core.DEFAULT_FIELDS):
            ttk.Label(form, text=f"{f.get('label', f['name'])}：", font=(FONT, 9)).grid(
                row=row, column=0, sticky="w", pady=3,
            )
            var = tk.StringVar()
            entry = ttk.Entry(
                form, textvariable=var, width=32,
                show="*" if f.get("secret") else "",
            )
            entry.grid(row=row, column=1, sticky="we", padx=(0, 8), pady=3)
            self.field_vars[f["name"]] = (var, f, entry)
            if f.get("secret"):
                shown = tk.BooleanVar(value=False)
                ttk.Checkbutton(
                    form, text="显示", variable=shown,
                    command=lambda e=entry, v=shown: e.configure(show="" if v.get() else "*"),
                ).grid(row=row, column=2, sticky="w")
            row += 1

        ttk.Label(form, text="运营商：", font=(FONT, 9)).grid(row=row, column=0, sticky="w", pady=3)
        self.var_choice = tk.StringVar()
        self.cbo = ttk.Combobox(form, textvariable=self.var_choice, state="readonly", width=30)
        self.cbo.grid(row=row, column=1, sticky="w", pady=3)
        ttk.Button(form, text="保存配置", command=self.save_form).grid(row=row, column=2, sticky="w")

        # 操作按钮
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(12, 0))
        self.btn_login = ttk.Button(bar, text="立即登录", command=self.do_login)
        self.btn_status = ttk.Button(bar, text="刷新状态", command=self.refresh_status)
        self.btn_logout = ttk.Button(bar, text="注销", command=self.do_logout)
        self.btn_browser = ttk.Button(bar, text="浏览器登录", command=self.do_browser)
        self.btn_logs = ttk.Button(bar, text="打开数据目录", command=self.open_logs)
        for btn in (self.btn_login, self.btn_status, self.btn_logout, self.btn_browser, self.btn_logs):
            btn.pack(side="left", padx=(0, 8))

        # 选项
        opts = ttk.Frame(outer)
        opts.pack(fill="x", pady=(8, 0))
        self.var_tray = tk.BooleanVar(
            value=bool((self.cfg.get("ui") or {}).get("minimize_to_tray", False)) and HAS_TRAY
        )
        self.chk_tray = ttk.Checkbutton(
            opts, text="关闭窗口时最小化到托盘", variable=self.var_tray,
            command=self._save_ui_options,
        )
        self.chk_tray.pack(side="left")
        if not HAS_TRAY:
            self.chk_tray.configure(state="disabled")

        self.var_autostart = tk.BooleanVar(value=False)
        self.chk_autostart = ttk.Checkbutton(
            opts, text="开机自动登录（计划任务）", variable=self.var_autostart,
            command=self._toggle_autostart,
        )
        self.chk_autostart.pack(side="left", padx=(18, 0))

        # 日志
        logf = ttk.LabelFrame(outer, text="运行日志", padding=6)
        logf.pack(fill="both", expand=True, pady=(10, 0))
        self.txt = tk.Text(logf, height=10, wrap="word", state="disabled", font=("Consolas", 9))
        bar2 = ttk.Scrollbar(logf, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=bar2.set)
        self.txt.pack(side="left", fill="both", expand=True)
        bar2.pack(side="right", fill="y")

    # -------------------------------------------------------------- 配置同步 --
    def _fill_form(self) -> None:
        values = core.config_values(self.cfg)
        for name, (var, _f, _e) in self.field_vars.items():
            var.set(str(values.get(name, "")))

    def _refresh_carrier_box(self) -> None:
        self.carriers = core.carrier_table(self.cfg)
        names = [core.format_carrier(c) for c in self.carriers]
        self.cbo.configure(values=names)
        current = core.current_carrier(self.cfg)
        index = 0
        for i, c in enumerate(self.carriers):
            if c.get("name") == current.get("name") and (c.get("suffix") or "") == (current.get("suffix") or ""):
                index = i
                break
        if names:
            self.cbo.current(index)
        self.var_carrier.set(core.format_carrier(current))

    def save_form(self, silent: bool = False) -> bool:
        values = {name: var.get().strip() for name, (var, _f, _e) in self.field_vars.items()}
        empty = [f.get("label", n) for n, (_v, f, _e) in self.field_vars.items() if not values[n]]
        if empty:
            if not silent:
                messagebox.showwarning("提示", "以下项目不能为空：" + "、".join(empty))
            return False

        self.cfg["values"] = {**core.config_values(self.cfg), **values}
        index = self.cbo.current()
        if 0 <= index < len(self.carriers):
            core.set_carrier(self.cfg, self.carriers[index])
        core.save_config(self.cfg)
        self.var_carrier.set(core.format_carrier(core.current_carrier(self.cfg)))
        return True

    # -------------------------------------------------------------- 选项设置 --
    def _save_ui_options(self) -> None:
        self.cfg["ui"] = {**(self.cfg.get("ui") or {}), "minimize_to_tray": bool(self.var_tray.get())}
        core.save_config(self.cfg)

    def _load_autostart_state(self) -> None:
        def work():
            try:
                installed = core.task_installed()
            except Exception:  # noqa: BLE001
                installed = False
            self.queue.put(("autostart", installed))

        threading.Thread(target=work, daemon=True).start()

    def _toggle_autostart(self) -> None:
        want = self.var_autostart.get()
        self.chk_autostart.configure(state="disabled")

        def work():
            ok, msg = core.install_task() if want else core.uninstall_task()
            core.log(("✅ " if ok else "❌ ") + msg)
            try:
                installed = core.task_installed()
            except Exception:  # noqa: BLE001
                installed = want
            self.queue.put(("autostart", installed))

        self._run_bg(work)

    # ------------------------------------------------------------------ 托盘 --
    def _tray_image(self):
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((3, 3, size - 3, size - 3), fill=(30, 120, 220, 255))
        draw.line((22, 45, 22, 19), fill="white", width=6)
        draw.line((22, 19, 42, 45), fill="white", width=6)
        draw.line((42, 45, 42, 19), fill="white", width=6)
        return img

    def _ensure_tray(self) -> None:
        if self.tray is not None:
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", self._tray_show, default=True),
            pystray.MenuItem("立即登录", self._tray_login),
            pystray.MenuItem("刷新状态", self._tray_refresh),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_quit),
        )
        self.tray = pystray.Icon("njtech_autologin", self._tray_image(), "南工大校园网自动登录", menu)
        self.tray.run_detached()

    # 托盘回调在 pystray 线程里，必须转回 tkinter 主线程
    def _tray_show(self, *_args) -> None:
        self.root.after(0, self._show_window)

    def _tray_login(self, *_args) -> None:
        self.root.after(0, self.do_login)

    def _tray_refresh(self, *_args) -> None:
        self.root.after(0, self.refresh_status)

    def _tray_quit(self, *_args) -> None:
        self.root.after(0, self._quit)

    def _hide_to_tray(self) -> None:
        if not HAS_TRAY:
            self.root.iconify()
            return
        try:
            self._ensure_tray()
        except Exception as exc:  # noqa: BLE001
            core.log(f"创建托盘图标失败：{type(exc).__name__}: {exc}")
            self.root.iconify()
            return
        self.root.withdraw()
        core.log("已最小化到系统托盘，双击托盘图标可恢复窗口。")

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def _quit(self) -> None:
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:  # noqa: BLE001
                pass
            self.tray = None
        core.set_log_sink(None)
        self.root.destroy()

    # ------------------------------------------------------------------ 状态 --
    def refresh_status(self) -> None:
        def work():
            ip = core.detect_campus_ip()
            online = core.is_online(ip) if ip else None
            self.queue.put(("status", {
                "ip": ip,
                "online": online,
                "carrier": core.current_carrier(self.cfg),
            }))

        threading.Thread(target=work, daemon=True).start()

    def _apply_status(self, data: dict) -> None:
        ip = data.get("ip")
        self.var_ip.set(ip or "未检测到（检查网线 / 校园网连接）")
        self.var_carrier.set(core.format_carrier(data.get("carrier") or core.current_carrier(self.cfg)))
        online = data.get("online")
        if online is None:
            text, color = "● 未连接校园网", "#b8860b"
        elif online:
            text, color = "● 已认证，可以上网", "#1b7f3b"
        else:
            text, color = "● 未认证（被门户拦截）", "#c0392b"
        self.var_state.set(text)
        self.lbl_state.configure(fg=color)

        # 托盘提示 & 掉线提醒
        if self.tray is not None:
            try:
                self.tray.title = f"校园网：{text.replace('● ', '')}"
            except Exception:  # noqa: BLE001
                pass
            if self._last_online is True and online is False:
                try:
                    self.tray.notify("校园网已断开，需要重新认证", "南工大校园网自动登录")
                except Exception:  # noqa: BLE001
                    pass
        self._last_online = online

    # ------------------------------------------------------------------ 日志 --
    def _enqueue_log(self, line: str) -> None:
        self.queue.put(("log", line))

    def _append_log(self, line: str) -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", line + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _pump(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                if item is DONE:
                    self._set_busy(False)
                    self.cfg = core.load_config()
                    self._refresh_carrier_box()
                    if self.pending_refresh:
                        self.pending_refresh = False
                        self.refresh_status()
                    continue
                kind, payload = item
                if kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    self._apply_status(payload)
                elif kind == "autostart":
                    self.var_autostart.set(bool(payload))
                    self.chk_autostart.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    # ------------------------------------------------------------------ 任务 --
    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for btn in (self.btn_login, self.btn_status, self.btn_logout, self.btn_browser):
            btn.configure(state=state)

    def _run_bg(self, func, *args, refresh: bool = False) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.pending_refresh = self.pending_refresh or refresh

        def worker():
            try:
                func(*args)
            except Exception as exc:  # noqa: BLE001
                core.log(f"操作异常：{type(exc).__name__}: {exc}")
            finally:
                self.queue.put(DONE)

        threading.Thread(target=worker, daemon=True).start()

    def _namespace(self, **overrides) -> argparse.Namespace:
        base = dict(setup=False, status=False, logout=False, carrier=None,
                    list_carriers=False, force=False, browser=False, headless=True, gui=False)
        base.update(overrides)
        return argparse.Namespace(**base)

    def do_login(self) -> None:
        if not self.save_form(silent=True):
            messagebox.showwarning("提示", "请先填写账号和密码，再点击“保存配置”。")
            return
        self._run_bg(core.run, self._namespace(), refresh=True)

    def do_browser(self) -> None:
        if not self.save_form(silent=True):
            messagebox.showwarning("提示", "请先填写账号和密码，再点击“保存配置”。")
            return
        self._run_bg(core.run, self._namespace(browser=True, force=True, headless=False), refresh=True)

    def do_logout(self) -> None:
        if not messagebox.askyesno("确认", "确定要注销当前校园网会话吗？"):
            return

        def work():
            ok, msg = core.logout(core.detect_campus_ip())
            core.log(("注销成功：" if ok else "注销失败：") + msg)

        self._run_bg(work, refresh=True)

    def open_logs(self) -> None:
        try:
            core.ensure_data_dir()
            import os
            os.startfile(core.DATA_DIR)  # noqa: S606
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ 其它 --
    def _auto_tick(self) -> None:
        if not self.busy:
            self.refresh_status()
        self.root.after(30000, self._auto_tick)

    def _on_close(self) -> None:
        if self.var_tray.get() and HAS_TRAY:
            self._hide_to_tray()
        else:
            self._quit()


def launch() -> int:
    # 让任务栏把这个进程当成独立应用，显示自带图标而不是 Python 的
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Njtech.CampusAutologin")
    except Exception:  # noqa: BLE001
        pass
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        try:
            print(f"无法创建窗口（无图形环境？）：{exc}")
        except Exception:  # noqa: BLE001
            pass
        return 5
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch())
