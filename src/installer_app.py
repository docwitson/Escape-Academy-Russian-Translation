#!/usr/bin/env python3
"""Standalone Windows installer for the Escape Academy Russian localization."""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path

try:
    from src import deploy_engine as engine
except ModuleNotFoundError:  # Direct `python src/installer_app.py` execution.
    import deploy_engine as engine


APP_NAME = "Русификатор Escape Academy"
APP_VERSION = "0.1.0"
STEAM_APP_ID = "1812090"
STATE_DIR_NAME = "EscapeAcademyRussian"
MIN_FREE_BYTES = 12 * 1024**3


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def validate_game_dir(path: Path) -> bool:
    required = (
        path / "Escape Academy.exe",
        path / "Escape Academy_Data" / "data.unity3d",
        path
        / "Escape Academy_Data"
        / "StreamingAssets"
        / "aa"
        / "StandaloneWindows64"
        / "areadioramas_assets_all_740d70d1014660f769811a969d8da879.bundle",
        path / "Escape Academy_Data" / "StreamingAssets" / "aa" / "catalog.json",
    )
    return all(item.is_file() for item in required)


def steam_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        import winreg

        for hive, key_name in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        ):
            try:
                with winreg.OpenKey(hive, key_name) as key:
                    for value_name in ("SteamPath", "InstallPath"):
                        try:
                            roots.append(Path(winreg.QueryValueEx(key, value_name)[0]))
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass
    roots.extend(
        (
            Path(r"C:\Program Files (x86)\Steam"),
            Path(r"C:\Program Files\Steam"),
        )
    )
    return roots


def steam_library_roots() -> list[Path]:
    libraries: list[Path] = []
    for root in steam_roots():
        if root not in libraries:
            libraries.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        try:
            text = vdf.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            candidate = Path(match.group(1).replace(r"\\", "\\"))
            if candidate not in libraries:
                libraries.append(candidate)
    return libraries


def find_game_dir() -> Path | None:
    direct = (executable_dir(), Path.cwd())
    for candidate in direct:
        if validate_game_dir(candidate):
            return candidate.resolve()
    for library in steam_library_roots():
        candidate = library / "steamapps" / "common" / "Escape Academy"
        if validate_game_dir(candidate):
            return candidate.resolve()
    return None


def configure_engine(game_dir: Path) -> None:
    data_dir = game_dir / "Escape Academy_Data"
    state_dir = game_dir / STATE_DIR_NAME
    engine.GAME_ROOT = game_dir
    engine.GAME_BUNDLE = data_dir / "data.unity3d"
    engine.ADDRESSABLE_BUNDLE = (
        data_dir
        / "StreamingAssets"
        / "aa"
        / "StandaloneWindows64"
        / "areadioramas_assets_all_740d70d1014660f769811a969d8da879.bundle"
    )
    engine.CATALOG = data_dir / "StreamingAssets" / "aa" / "catalog.json"
    engine.DEPLOY_DIR = state_dir
    engine.BUILD_DIR = state_dir / "build"
    engine.BACKUP_DIR = state_dir / "backup"
    engine.REPORT_PATH = state_dir / "deployment.json"
    engine.STAGED_BUNDLE = engine.BUILD_DIR / "data.unity3d.russian"
    engine.ORIGINAL_BACKUP = engine.BACKUP_DIR / "data.unity3d.original"
    engine.STAGED_ADDRESSABLE = engine.BUILD_DIR / (engine.ADDRESSABLE_BUNDLE.name + ".russian")
    engine.ORIGINAL_ADDRESSABLE_BACKUP = engine.BACKUP_DIR / (
        engine.ADDRESSABLE_BUNDLE.name + ".original"
    )
    engine.STAGED_CATALOG = engine.BUILD_DIR / "catalog.json.russian"
    engine.ORIGINAL_CATALOG_BACKUP = engine.BACKUP_DIR / "catalog.json.original"


def game_is_running() -> bool:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Escape Academy.exe", "/NH"],
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=flags,
        check=False,
    )
    return "Escape Academy.exe" in result.stdout


def require_game_dir(game_dir: Path) -> None:
    if not validate_game_dir(game_dir):
        raise ValueError("В выбранной папке не найдена установленная Escape Academy.")
    if game_is_running():
        raise RuntimeError("Закройте Escape Academy перед изменением файлов.")


def read_report() -> dict | None:
    if not engine.REPORT_PATH.is_file():
        return None
    return json.loads(engine.REPORT_PATH.read_text(encoding="utf-8"))


def installed_matches(report: dict) -> bool:
    addressable = report.get("addressable", {})
    catalog = report.get("catalog", {})
    return (
        engine.sha256_file(engine.GAME_BUNDLE) == report.get("staged_sha256")
        and engine.sha256_file(engine.ADDRESSABLE_BUNDLE) == addressable.get("staged_sha256")
        and engine.sha256_file(engine.CATALOG) == catalog.get("staged_sha256")
    )


def original_matches(report: dict) -> bool:
    addressable = report.get("addressable", {})
    catalog = report.get("catalog", {})
    return (
        engine.sha256_file(engine.GAME_BUNDLE) == report.get("original_sha256")
        and engine.sha256_file(engine.ADDRESSABLE_BUNDLE) == addressable.get("original_sha256")
        and engine.sha256_file(engine.CATALOG) == catalog.get("original_sha256")
    )


def clear_build_files() -> None:
    if engine.BUILD_DIR.is_dir():
        shutil.rmtree(engine.BUILD_DIR)


def verify_installation(report: dict | None = None) -> None:
    report = report or read_report()
    if report is None:
        raise FileNotFoundError("Данные установленного русификатора не найдены.")
    if not installed_matches(report):
        raise ValueError("Контрольные суммы установленных файлов не совпадают с русификатором.")
    engine.verify_bundle(engine.GAME_BUNDLE, report)
    engine.verify_addressable_bundle(engine.ADDRESSABLE_BUNDLE, report)
    engine.verify_catalog(engine.CATALOG, report)


def install_localization(game_dir: Path, keep_build: bool = False) -> None:
    require_game_dir(game_dir)
    configure_engine(game_dir)
    report = read_report()
    if report is not None and installed_matches(report):
        print("Русификатор уже установлен. Выполняется проверка...", flush=True)
        verify_installation(report)
        print("Установленные файлы исправны.", flush=True)
        return
    if report is not None and not original_matches(report):
        raise ValueError(
            "Файлы игры не совпадают ни с оригиналом, ни с предыдущей установкой. "
            "Восстановите файлы через Steam и повторите попытку."
        )
    free = shutil.disk_usage(game_dir).free
    if free < MIN_FREE_BYTES:
        raise OSError(
            f"Недостаточно свободного места: доступно {free / 1024**3:.1f} ГБ, "
            f"требуется не менее {MIN_FREE_BYTES / 1024**3:.0f} ГБ."
        )
    print(f"Escape Academy: {game_dir}", flush=True)
    print("Проверка и сборка русских игровых архивов...", flush=True)
    engine.build()
    engine.install()
    if not keep_build:
        clear_build_files()
        print("Временные сборочные файлы удалены.", flush=True)
    print("Русификатор установлен. В настройках игры выберите «Русский».", flush=True)


def uninstall_localization(game_dir: Path) -> None:
    require_game_dir(game_dir)
    configure_engine(game_dir)
    report = read_report()
    if report is None:
        raise FileNotFoundError("Резервная копия русификатора не найдена.")
    engine.restore()
    if engine.DEPLOY_DIR.is_dir():
        shutil.rmtree(engine.DEPLOY_DIR)
    print("Оригинальные файлы восстановлены, данные русификатора удалены.", flush=True)


def status_text(game_dir: Path) -> str:
    if not validate_game_dir(game_dir):
        return "Игра не найдена"
    configure_engine(game_dir)
    report = read_report()
    if report is None:
        manifest = json.loads(engine.MANIFEST_PATH.read_text(encoding="utf-8"))
        if (
            engine.GAME_BUNDLE.stat().st_size == manifest["game_bundle_size"]
            and engine.ADDRESSABLE_BUNDLE.stat().st_size == engine.ADDRESSABLE_ORIGINAL_SIZE
        ):
            return "Готово к установке (поддерживаемая версия)"
        return "Неизвестная или уже изменённая версия игры"
    if installed_matches(report):
        return "Русификатор установлен"
    if original_matches(report):
        return "Оригинальные файлы восстановлены"
    return "Состояние файлов не соответствует журналу установки"


class QueueStream(io.TextIOBase):
    def __init__(self, events: queue.Queue):
        self.events = events

    def write(self, value: str) -> int:
        if value:
            self.events.put(("log", value))
        return len(value)

    def flush(self) -> None:
        return None


def run_gui(initial_game_dir: Path | None = None) -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class InstallerWindow:
        def __init__(self) -> None:
            self.root = tk.Tk()
            self.root.title(f"{APP_NAME} {APP_VERSION}")
            self.root.geometry("820x600")
            self.root.minsize(720, 500)
            self.events: queue.Queue = queue.Queue()
            self.worker: threading.Thread | None = None
            self.path_var = tk.StringVar(value=str(initial_game_dir or find_game_dir() or ""))
            self.status_var = tk.StringVar(value="Проверка папки игры...")

            outer = ttk.Frame(self.root, padding=14)
            outer.pack(fill="both", expand=True)
            ttk.Label(outer, text=APP_NAME, font=("Segoe UI", 17, "bold")).pack(anchor="w")
            ttk.Label(
                outer,
                text=(
                    "Версия 0.1.0 · заменяет внутренний испанский слот · "
                    "для Steam-сборки Escape Academy 3.0.7.4"
                ),
            ).pack(anchor="w", pady=(2, 14))

            path_frame = ttk.LabelFrame(outer, text="Папка игры", padding=10)
            path_frame.pack(fill="x")
            self.path_entry = ttk.Entry(path_frame, textvariable=self.path_var)
            self.path_entry.pack(side="left", fill="x", expand=True)
            ttk.Button(path_frame, text="Обзор...", command=self.browse).pack(side="left", padx=(8, 0))
            ttk.Button(path_frame, text="Найти Steam", command=self.autodetect).pack(
                side="left", padx=(8, 0)
            )

            self.status_label = ttk.Label(outer, textvariable=self.status_var)
            self.status_label.pack(anchor="w", pady=(10, 8))

            actions = ttk.Frame(outer)
            actions.pack(fill="x")
            self.install_button = ttk.Button(actions, text="Установить", command=self.install)
            self.install_button.pack(side="left")
            self.verify_button = ttk.Button(actions, text="Проверить", command=self.verify)
            self.verify_button.pack(side="left", padx=8)
            self.remove_button = ttk.Button(actions, text="Удалить русификатор", command=self.remove)
            self.remove_button.pack(side="left")

            self.progress = ttk.Progressbar(outer, mode="indeterminate")
            self.progress.pack(fill="x", pady=(12, 8))

            ttk.Label(outer, text="Журнал").pack(anchor="w")
            log_frame = ttk.Frame(outer)
            log_frame.pack(fill="both", expand=True, pady=(4, 8))
            self.log = tk.Text(log_frame, wrap="word", state="disabled", font=("Consolas", 9))
            scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
            self.log.configure(yscrollcommand=scroll.set)
            self.log.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")

            ttk.Label(
                outer,
                text=(
                    "Во время установки игра должна быть закрыта. Требуется около 12 ГБ "
                    "свободного места; оригинальные файлы сохраняются для удаления патча."
                ),
                wraplength=760,
            ).pack(anchor="w")

            self.root.protocol("WM_DELETE_WINDOW", self.close)
            self.root.after(100, self.poll_events)
            self.root.after(150, self.refresh_status)

        def selected_path(self) -> Path:
            return Path(self.path_var.get().strip()).expanduser()

        def append_log(self, text: str) -> None:
            self.log.configure(state="normal")
            self.log.insert("end", text)
            self.log.see("end")
            self.log.configure(state="disabled")

        def set_busy(self, busy: bool) -> None:
            state = "disabled" if busy else "normal"
            for button in (self.install_button, self.verify_button, self.remove_button):
                button.configure(state=state)
            self.path_entry.configure(state=state)
            if busy:
                self.progress.start(12)
            else:
                self.progress.stop()

        def browse(self) -> None:
            selected = filedialog.askdirectory(title="Выберите папку Escape Academy")
            if selected:
                self.path_var.set(selected)
                self.refresh_status()

        def autodetect(self) -> None:
            found = find_game_dir()
            if found is None:
                messagebox.showwarning(APP_NAME, "Escape Academy не найдена в библиотеках Steam.")
                return
            self.path_var.set(str(found))
            self.refresh_status()

        def refresh_status(self) -> None:
            if self.worker and self.worker.is_alive():
                return
            path = self.selected_path()
            if not validate_game_dir(path):
                self.status_var.set("Игра не найдена — выберите папку вручную")
                return
            self.start_task("status", lambda: print("STATUS:" + status_text(path)))

        def start_task(self, name: str, action) -> None:
            if self.worker and self.worker.is_alive():
                return
            self.set_busy(True)
            if name != "status":
                self.append_log(f"\n--- {name} ---\n")

            def target() -> None:
                stream = QueueStream(self.events)
                try:
                    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                        action()
                    self.events.put(("done", name))
                except Exception:
                    self.events.put(("log", traceback.format_exc()))
                    self.events.put(("error", name))

            self.worker = threading.Thread(target=target, daemon=True)
            self.worker.start()

        def install(self) -> None:
            path = self.selected_path()
            self.start_task("Установка", lambda: install_localization(path))

        def verify(self) -> None:
            path = self.selected_path()

            def action() -> None:
                require_game_dir(path)
                configure_engine(path)
                verify_installation()
                print("Проверка завершена: установленные файлы исправны.")

            self.start_task("Проверка", action)

        def remove(self) -> None:
            if not messagebox.askyesno(
                APP_NAME, "Восстановить оригинальные файлы и удалить данные русификатора?"
            ):
                return
            path = self.selected_path()
            self.start_task("Удаление", lambda: uninstall_localization(path))

        def poll_events(self) -> None:
            try:
                while True:
                    kind, value = self.events.get_nowait()
                    if kind == "log":
                        if value.startswith("STATUS:"):
                            self.status_var.set(value[7:].strip())
                        else:
                            self.append_log(value)
                    elif kind == "done":
                        self.set_busy(False)
                        if value != "status":
                            messagebox.showinfo(APP_NAME, f"Операция «{value}» завершена.")
                            self.refresh_status()
                    elif kind == "error":
                        self.set_busy(False)
                        messagebox.showerror(APP_NAME, "Операция завершилась с ошибкой. См. журнал.")
                        self.refresh_status()
            except queue.Empty:
                pass
            self.root.after(100, self.poll_events)

        def close(self) -> None:
            if self.worker and self.worker.is_alive():
                messagebox.showwarning(APP_NAME, "Дождитесь завершения текущей операции.")
                return
            self.root.destroy()

        def run(self) -> int:
            self.root.mainloop()
            return 0

    return InstallerWindow().run()


def self_test() -> None:
    manifest = json.loads(engine.MANIFEST_PATH.read_text(encoding="utf-8"))
    payload_manifest_path = engine.WORK_DIR / "manifests" / "payload.json"
    payload_manifest = json.loads(payload_manifest_path.read_text(encoding="utf-8"))
    counts = {}
    for name, path in engine.TABLES.items():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            counts[name] = sum(1 for _ in csv.DictReader(handle))
    if not manifest.get("assets") or any(value <= 0 for value in counts.values()):
        raise ValueError("Bundled localization payload is incomplete")
    for relative, expected in payload_manifest["files"].items():
        payload_path = engine.WORK_DIR / relative
        if engine.sha256_file(payload_path) != expected["sha256"]:
            raise ValueError(f"Bundled payload checksum mismatch: {relative}")
    if sum(counts.values()) != payload_manifest["total_rows"]:
        raise ValueError("Bundled localization row count mismatch")
    print(f"{APP_NAME} {APP_VERSION}")
    print(f"Payload rows: {counts}")
    print("SELF_TEST_OK")


def run_headless(args: argparse.Namespace) -> int:
    log_path = executable_dir() / "EscapeAcademyRussianInstaller.log"
    try:
        with log_path.open("a", encoding="utf-8") as log, contextlib.redirect_stdout(
            log
        ), contextlib.redirect_stderr(log):
            print(f"\n=== {APP_NAME} {APP_VERSION} ===")
            if args.self_test:
                self_test()
            else:
                game_dir = Path(args.game_dir).resolve() if args.game_dir else find_game_dir()
                if game_dir is None:
                    raise FileNotFoundError("Escape Academy не найдена")
                if args.install:
                    install_localization(game_dir, keep_build=args.keep_build)
                elif args.verify:
                    require_game_dir(game_dir)
                    configure_engine(game_dir)
                    verify_installation()
                    print("VERIFY_OK")
                elif args.uninstall:
                    uninstall_localization(game_dir)
        return 0
    except Exception:
        with log_path.open("a", encoding="utf-8") as log:
            traceback.print_exc(file=log)
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--install", action="store_true")
    group.add_argument("--verify", action="store_true")
    group.add_argument("--uninstall", action="store_true")
    group.add_argument("--self-test", action="store_true")
    parser.add_argument("--game-dir")
    parser.add_argument("--keep-build", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.install or args.verify or args.uninstall or args.self_test:
        return run_headless(args)
    initial = Path(args.game_dir).resolve() if args.game_dir else None
    return run_gui(initial)


if __name__ == "__main__":
    raise SystemExit(main())
