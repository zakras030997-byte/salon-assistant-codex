#!/usr/bin/env python3
"""Install project-scoped skills without account access or global configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

PACKAGE = Path(__file__).resolve().parents[1]
STATE_FILE = Path("system/INSTALLATION.json")
BOOTSTRAP_FILE = Path("system/BOOTSTRAP.json")
KIND = "svetlana-project-skills"


class InstallError(ValueError):
    pass


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def check_path(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise InstallError("Нужен абсолютный путь отдельной папки без «..».")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise InstallError("Символические ссылки в пути установки не поддерживаются.")


def source_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"Ожидался обычный файл пакета: {path.name}")
    return path.read_bytes()


def payload(package: Path) -> tuple[str, dict[str, bytes]]:
    manifest_bytes = source_file(package / ".codex-plugin/plugin.json")
    manifest = json.loads(manifest_bytes)
    if manifest.get("name") != "svetlana-assistant":
        raise InstallError("Неизвестный пакет.")
    version = manifest["version"]
    files = {
        "AGENTS.md": source_file(package / "templates/PROJECT-AGENTS.md"),
        "system/assistant/.codex-plugin/plugin.json": manifest_bytes,
        "system/assistant/LICENSE": source_file(package / "LICENSE"),
    }
    skill_root = package / "skills"
    if skill_root.is_symlink() or not skill_root.is_dir():
        raise InstallError("Нет каталога скиллов.")
    names = []
    for directory in sorted(skill_root.iterdir()):
        if directory.is_symlink() or not directory.is_dir():
            raise InstallError("Каталог skills должен содержать обычные папки навыков.")
        main = directory / "SKILL.md"
        main_text = source_file(main).decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        if not main_text.startswith("---\n") or "\n---\n" not in main_text[4:]:
            raise InstallError("Отсутствует описание скилла.")
        header = main_text.split("\n---\n", 1)[0] + "\n---\n"
        route = (
            f"\nПеред выполнением прочитай полный навык "
            f"[SKILL.md](../../../system/assistant/skills/{directory.name}/SKILL.md). "
            "Относительные ссылки внутри него считай относительно полного файла навыка.\n"
        )
        files[f".agents/skills/{directory.name}/SKILL.md"] = (header + route).encode("utf-8")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise InstallError("Скилл содержит символическую ссылку.")
            if path.is_file():
                relative = path.relative_to(skill_root).as_posix()
                files[f"system/assistant/skills/{relative}"] = source_file(path)
        names.append(directory.name)
    expected = {"svetlana-assistant", "svetlana-first-run", "svetlana-client-photo", "svetlana-calendar-booking", "svetlana-stock-photo"}
    if set(names) != expected:
        raise InstallError("Состав пяти скиллов пакета не совпадает.")
    return version, files


def write_new(path: Path, content: bytes) -> None:
    check_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation never overwrites an existing user file.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)


def install(project: Path | str, package: Path = PACKAGE) -> dict:
    project = Path(project).expanduser()
    check_path(project)
    if not project.parent.is_dir():
        raise InstallError("Родительская папка должна уже существовать.")
    if project == package or package in project.parents or project in package.parents:
        raise InstallError("Рабочая папка должна находиться отдельно от исходников пакета.")
    version, files = payload(package)
    hashes = {name: digest(content) for name, content in sorted(files.items())}
    state_path = project / STATE_FILE
    check_path(state_path)
    resumed = False
    if project.exists():
        if not project.is_dir():
            raise InstallError("Цель не является папкой.")
        if any(project.iterdir()):
            if not state_path.is_file():
                raise InstallError("Папка не пуста и не создана этим установщиком. Файлы сохранены.")
            state = json.loads(source_file(state_path))
            if state.get("kind") != KIND or state.get("version") != version or state.get("files") != hashes:
                raise InstallError("Другая версия или состав пакета. Нужна отдельная проверка обновления.")
            resumed = True
    else:
        project.mkdir(mode=0o700)
    # Validate every existing owned path before making any changes on resume.
    for relative, content in files.items():
        path = project / relative
        check_path(path)
        if path.exists() and (not path.is_file() or digest(path.read_bytes()) != hashes[relative]):
            raise InstallError(f"Установленный файл изменён вручную: {relative}. Перезапись остановлена.")
    bootstrap_path = project / BOOTSTRAP_FILE
    check_path(bootstrap_path)
    if bootstrap_path.exists():
        bootstrap = json.loads(source_file(bootstrap_path))
        if bootstrap.get("engine") != "codex-google-skills" or not bootstrap.get("deployment_id"):
            raise InstallError("Существующий журнал настройки требует проверки.")
    else:
        if resumed:
            raise InstallError("Утрачен BOOTSTRAP.json. Восстановите привязку к существующему Google-проекту; новый корень автоматически не создаётся.")
        bootstrap = {
            "schema_version": 1,
            "engine": "codex-google-skills",
            "deployment_id": str(uuid.uuid4()),
            "defaults": {"location": "Санкт-Петербург", "timezone": "Europe/Moscow"},
            "account_email": None,
            "root_folder_id": None,
            "context_file_id": None,
            "phase": "needs_connection",
            "steps": [],
        }
    if not state_path.exists():
        write_new(state_path, json_bytes({"kind": KIND, "version": version, "files": hashes}))
    if not bootstrap_path.exists():
        write_new(bootstrap_path, json_bytes(bootstrap))
    created = 0
    for relative, content in files.items():
        target = project / relative
        if not target.exists():
            write_new(target, content)
            created += 1
    # Final read-back is authoritative; the presence of the state file alone is not success.
    for relative, expected in hashes.items():
        if digest(source_file(project / relative)) != expected:
            raise InstallError(f"Не пройдена проверка файла: {relative}")
    json.loads(source_file(bootstrap_path))
    return {
        "status": "verified",
        "version": version,
        "project": str(project),
        "resumed": resumed,
        "created_files": created,
        "verified_files": len(files),
        "cloud_status": "not_checked",
        "next_step": "Откройте рабочую папку в Codex, начните новую задачу и напишите «Привет».",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Установить скиллы Светланы в отдельный локальный проект.")
    parser.add_argument("--project", required=True, help="Абсолютный путь новой рабочей папки.")
    args = parser.parse_args()
    try:
        result = install(args.project)
    except (InstallError, OSError, ValueError, KeyError) as error:
        print(f"Установка остановлена: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
