#!/usr/bin/env python3
"""Install the native plugin and a separate client project, without Google access."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid

# This file also works when imported directly by the package tests.
_spec = importlib.util.spec_from_file_location("salon_legacy_installer", Path(__file__).with_name("install_project.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
PACKAGE = Path(__file__).resolve().parents[1]
PLUGIN = "svetlana-assistant"
MARKETPLACE = "salon-assistant"
PLUGIN_ID = f"{PLUGIN}@{MARKETPLACE}"
STATE = Path("system/PLUGIN-INSTALLATION.json")
BOOTSTRAP = Path("system/BOOTSTRAP.json")


class PluginInstallError(ValueError):
    pass


def _check(path: Path) -> None:
    try:
        _base.check_path(path)
    except _base.InstallError as exc:
        raise PluginInstallError(str(exc)) from exc


def _read(path: Path) -> bytes:
    _check(path)
    try:
        return _base.source_file(path)
    except (OSError, _base.InstallError) as exc:
        raise PluginInstallError(f"Нельзя прочитать обычный файл: {path}") from exc


def _manifest(package: Path) -> dict:
    value = json.loads(_read(package / ".codex-plugin/plugin.json"))
    if value.get("name") != PLUGIN or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", value.get("version", "")):
        raise PluginInstallError("Неизвестный плагин или версия.")
    return value


def _separate(target: Path, package: Path) -> None:
    _check(target)
    if target == package or package in target.parents or target in package.parents:
        raise PluginInstallError("Нужна отдельная рабочая папка вне исходников плагина.")
    for parent in (target, *target.parents):
        if ((parent / ".codex-plugin/plugin.json").exists()
                or ((parent / ".agents/plugins/marketplace.json").exists()
                    and parent != Path.home())):
            raise PluginInstallError("Рабочая папка не может находиться в пакете или каталоге плагинов.")


def _publish(path: Path, value: bytes, replace: bool = False) -> None:
    _check(path)
    if replace:
        _base.write_replacement(path, value)
    else:
        _base.write_new(path, value)


def _owned_tree(target: Path, files: dict[str, bytes], state_name: str, kind: str, version: str) -> None:
    """Preflight everything, then resume only an identical previously declared tree."""
    _check(target)
    hashes = {name: _base.digest(value) for name, value in sorted(files.items())}
    state_path = target / state_name
    expected = _base.json_bytes({"kind": kind, "version": version, "files": hashes})
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        if not state_path.is_file() or _read(state_path) != expected:
            raise PluginInstallError("Папка занята другим или изменённым пакетом. Файлы сохранены.")
    if target.is_dir():
        for path in target.rglob("*"):
            _check(path)
            if path.is_file() and path.relative_to(target).as_posix() not in {*files, state_name}:
                raise PluginInstallError("В каталоге плагина найдены посторонние файлы. Установка остановлена.")
    for name, value in files.items():
        path = target / name
        _check(path)
        if path.exists() and _read(path) != value:
            raise PluginInstallError(f"Файл изменён: {name}. Перезапись остановлена.")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not state_path.exists():
        _publish(state_path, expected)
    for name, value in files.items():
        path = target / name
        if not path.exists():
            _publish(path, value)
    for name, value in files.items():
        if _read(target / name) != value:
            raise PluginInstallError(f"Не пройдена проверка: {name}")


def build_marketplace(target: Path, package: Path = PACKAGE) -> dict:
    target, package = Path(target).expanduser(), Path(package).absolute()
    _check(target)
    if target == package or package in target.parents or target in package.parents:
        raise PluginInstallError("Каталог установки должен быть отдельно от исходников.")
    manifest = _manifest(package)
    # Reuse the existing six-skill validation; never copy the whole repository.
    try:
        _base.payload(package)
    except _base.InstallError as exc:
        raise PluginInstallError(str(exc)) from exc
    names = [".codex-plugin/plugin.json", "AGENTS.md", "LICENSE", "INSTALL.md",
             "scripts/install_plugin.py", "scripts/install_project.py",
             "templates/PLUGIN-PROJECT-AGENTS.md", "templates/PROJECT-AGENTS.md",
             "templates/CLOUD-PROJECT-CONTEXT.json",
             "templates/PLUGIN-MARKETPLACE.json"]
    names += [p.relative_to(package).as_posix() for p in sorted((package / "skills").rglob("*")) if p.is_file()]
    files = {f"plugins/{PLUGIN}/{name}": _read(package / name) for name in names}
    catalog = _read(package / "templates/PLUGIN-MARKETPLACE.json")
    entry = json.loads(catalog)
    if (entry.get("name") != MARKETPLACE or len(entry.get("plugins", [])) != 1
            or entry["plugins"][0].get("name") != PLUGIN
            or entry["plugins"][0].get("source") != {"source": "local", "path": f"./plugins/{PLUGIN}"}):
        raise PluginInstallError("Неизвестный каталог плагина.")
    files[".agents/plugins/marketplace.json"] = catalog
    _owned_tree(target, files, "BUNDLE.json", "salon-plugin-bundle", manifest["version"])
    return {"root": str(target), "plugin": str(target / "plugins" / PLUGIN), "version": manifest["version"]}


def prepare_project(project: Path, package: Path = PACKAGE) -> dict:
    project, package = Path(project).expanduser(), Path(package).absolute()
    _separate(project, package)
    manifest = _manifest(package)
    agents = _read(package / "templates/PLUGIN-PROJECT-AGENTS.md")
    state = _base.json_bytes({"kind": "salon-plugin-project", "version": manifest["version"],
                             "plugin_id": PLUGIN_ID, "files": {"AGENTS.md": _base.digest(agents)}})
    state_path, bootstrap_path = project / STATE, project / BOOTSTRAP
    _check(state_path)
    _check(bootstrap_path)
    resumed = project.exists() and project.is_dir() and any(project.iterdir())
    if project.exists() and not project.is_dir():
        raise PluginInstallError("Цель не является папкой.")
    if resumed:
        if not state_path.is_file() or _read(state_path) != state:
            raise PluginInstallError("Это чужой или прежний проект со скиллами. Автоматическая миграция не выполняется.")
        if not bootstrap_path.is_file():
            raise PluginInstallError("Утрачен BOOTSTRAP.json. Восстановите прежнюю Google-привязку, новый корень не создаётся.")
        raw = _read(bootstrap_path)
        try:
            bootstrap = json.loads(raw)
        except ValueError as exc:
            raise PluginInstallError("Некорректный BOOTSTRAP.json; исходник сохранён.") from exc
        if not isinstance(bootstrap, dict) or bootstrap.get("engine") != "codex-google-skills" or not bootstrap.get("deployment_id"):
            raise PluginInstallError("Некорректный BOOTSTRAP.json; исходник сохранён.")
        if (project / ".agents/skills").exists() or (project / "system/assistant").exists():
            raise PluginInstallError("Обнаружены копии навыков. Смешанная установка остановлена.")
        if (project / "AGENTS.md").exists() and _read(project / "AGENTS.md") != agents:
            raise PluginInstallError("Инструкции проекта изменены вручную; файл сохранён.")
    else:
        raw = _base.json_bytes({"schema_version": 1, "engine": "codex-google-skills",
                                "deployment_id": str(uuid.uuid4()),
                                "defaults": {"location": "Санкт-Петербург", "timezone": "Europe/Moscow"},
                                "account_email": None, "root_folder_id": None, "context_file_id": None,
                                "phase": "needs_connection", "steps": []})
    project.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A stopped initial creation is never mistaken for a lost live Google binding.
    if not state_path.exists():
        _publish(state_path, state)
    if not bootstrap_path.exists():
        _publish(bootstrap_path, raw)
    if not (project / "AGENTS.md").exists():
        _publish(project / "AGENTS.md", agents)
    if _read(bootstrap_path) != raw or _read(project / "AGENTS.md") != agents or _read(state_path) != state:
        raise PluginInstallError("Проверка рабочего проекта не пройдена.")
    return {"project": str(project), "version": manifest["version"], "resumed": resumed}


class CodexCLI:
    def __init__(self, executable: str, project: Path):
        self.executable = shutil.which(executable)
        if not self.executable:
            raise PluginInstallError("Не найден Codex CLI. Установите официальный Codex CLI и повторите запуск.")
        self.project = project

    def call(self, args: list[str]) -> dict:
        journal = self.project / "system/PLUGIN-OPERATIONS.jsonl"
        _check(journal)
        if journal.exists() and not journal.is_file():
            raise PluginInstallError("Путь журнала операций занят другим объектом.")
        fd = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise PluginInstallError("Журнал операций должен быть обычным файлом.")
        os.close(fd)
        process = subprocess.Popen([self.executable, "plugin", *args, "--json"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def log(event: str, **extra):
            record = {"time": datetime.now(timezone(timedelta(hours=3))).isoformat(timespec="microseconds"),
                      "timezone": "Europe/Moscow",
                      "event": event, "skill": "plugin-installer", "tool": "codex plugin",
                      "role": "installer", "pid": process.pid, "ppid": os.getpid(), "args": args, **extra}
            _check(journal)
            with journal.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            log("start")
        except (OSError, PluginInstallError):
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            raise
        try:
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            log("finish", result="timeout_unknown")
            raise PluginInstallError("Codex не ответил вовремя. Исход установки требует чтения состояния.")
        log("finish", returncode=process.returncode)
        if process.returncode:
            raise PluginInstallError("Команда Codex завершилась ошибкой: " + stderr.strip()[:1200])
        try:
            value = json.loads(stdout)
        except ValueError as exc:
            raise PluginInstallError("Codex вернул непроверяемый ответ; исход требует чтения состояния.") from exc
        if not isinstance(value, dict):
            raise PluginInstallError("Неизвестный формат ответа Codex.")
        return value


def activate(project: Path, bundle: dict, cli: CodexCLI) -> dict:
    """Read before and after CLI mutations; an unresolved intent forbids blind retry."""
    receipt = project / "system/PLUGIN-SETUP.json"
    desired = {"plugin_id": PLUGIN_ID, "root": bundle["root"], "version": bundle["version"]}
    previous = json.loads(_read(receipt)) if receipt.exists() else {}
    if previous and any(previous.get(k) != v for k, v in desired.items()):
        raise PluginInstallError("Журнал относится к другой установке. Проверьте существующий плагин.")
    def save(phase: str):
        content = _base.json_bytes({**desired, "phase": phase})
        if not receipt.exists() or _read(receipt) != content:
            _publish(receipt, content, replace=receipt.exists())
    def marketplace_ready():
        items = cli.call(["marketplace", "list"]).get("marketplaces")
        if not isinstance(items, list):
            raise PluginInstallError("Codex не вернул список каталогов.")
        matches = [x for x in items if x.get("name") == MARKETPLACE]
        if not matches:
            return False
        if len(matches) != 1 or Path(matches[0].get("root", "")).resolve() != Path(bundle["root"]).resolve():
            raise PluginInstallError("Каталог salon-assistant уже связан с другим источником; он сохранён.")
        return True
    if not marketplace_ready():
        if previous:
            raise PluginInstallError("Исход прошлого добавления каталога не установлен. Автоматический повтор запрещён.")
        save("marketplace_pending")
        error = None
        try:
            cli.call(["marketplace", "add", bundle["root"]])
        except PluginInstallError as exc:
            error = exc
        if not marketplace_ready():
            raise error or PluginInstallError("Каталог не подтверждён чтением. Не повторяйте добавление вслепую.")
    def plugin_ready():
        rows = cli.call(["list", "--marketplace", MARKETPLACE]).get("installed")
        if not isinstance(rows, list):
            raise PluginInstallError("Codex не вернул установленные плагины.")
        matches = [x for x in rows if x.get("pluginId") == PLUGIN_ID]
        if not matches:
            return False
        if len(matches) != 1:
            raise PluginInstallError("Найдены неоднозначные записи плагина.")
        item = matches[0]
        source = item.get("source", {})
        if (item.get("version") != bundle["version"] or not item.get("installed") or not item.get("enabled")
                or source.get("source") != "local"
                or Path(source.get("path", "")).resolve() != Path(bundle["plugin"]).resolve()):
            raise PluginInstallError("Установлен другой источник/версия плагина или он выключен. Нужна проверка.")
        return True
    if not plugin_ready():
        if previous.get("phase") in ("plugin_pending", "verified"):
            raise PluginInstallError("Прошлая установка не подтверждена текущим чтением. Автоматический повтор запрещён.")
        save("plugin_pending")
        error = None
        try:
            cli.call(["add", PLUGIN_ID])
        except PluginInstallError as exc:
            error = exc
        if not plugin_ready():
            raise error or PluginInstallError("Плагин не подтверждён чтением. Не повторяйте установку вслепую.")
    save("verified")
    return {"status": "plugin_installed_verified", **desired, "project": str(project),
            "cloud_status": "not_checked", "new_session_status": "not_checked",
            "next_step": "Откройте рабочую папку в Codex, начните новую задачу и напишите «Привет»."}


def main() -> int:
    parser = argparse.ArgumentParser(description="Установить плагин Salon Assistant и подготовить отдельный рабочий проект.")
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--marketplace-root", type=Path, help="Постоянная отдельная папка исходников установленного плагина.")
    parser.add_argument("--prepare-only", action="store_true", help="Собрать пакет и проект, не устанавливая плагин в Codex.")
    parser.add_argument("--codex", default="codex", help="Исполняемый файл официального Codex CLI.")
    args = parser.parse_args()
    try:
        version = _manifest(PACKAGE)["version"]
        root = args.marketplace_root or Path.home() / ".local/share/salon-assistant" / version
        project = args.project.expanduser()
        _separate(project, PACKAGE)
        _check(root)
        if project == root or root in project.parents or project in root.parents:
            raise PluginInstallError("Плагин и рабочий проект должны быть в отдельных папках.")
        # Validate CLI availability before writing anything in the normal install flow.
        if not args.prepare_only and not shutil.which(args.codex):
            raise PluginInstallError("Не найден Codex CLI. Установите официальный Codex CLI и повторите запуск.")
        prepare_project(project)
        bundle = build_marketplace(root)
        if args.prepare_only:
            result = {"status": "prepared_only", **bundle, "project": str(project), "plugin_installed": False}
        else:
            result = activate(project, bundle, CodexCLI(args.codex, project))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PluginInstallError, _base.InstallError, OSError, ValueError) as exc:
        print(json.dumps({"status": "needs_review", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
