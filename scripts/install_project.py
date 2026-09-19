#!/usr/bin/env python3
"""Install project-scoped skills without account access or global configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

PACKAGE = Path(__file__).resolve().parents[1]
STATE_FILE = Path("system/INSTALLATION.json")
BOOTSTRAP_FILE = Path("system/BOOTSTRAP.json")
UPGRADE_FILE = Path("system/UPGRADE.json")
KIND = "svetlana-project-skills"
SUPPORTED_UPGRADES = {
    ("0.6.2", "0.7.0"), ("0.6.2", "0.8.0"), ("0.7.0", "0.8.0"),
    ("0.6.2", "0.8.1"), ("0.7.0", "0.8.1"), ("0.8.0", "0.8.1"),
}


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
    for parent in path.parents:
        if parent.exists() and not parent.is_dir():
            raise InstallError("Родительский путь занят обычным файлом. Файлы сохранены.")


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
        "system/assistant/INSTALL.md": source_file(package / "INSTALL.md"),
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
                if path.relative_to(directory).as_posix() == "agents/openai.yaml":
                    files[f".agents/skills/{directory.name}/agents/openai.yaml"] = source_file(path)
        names.append(directory.name)
    expected = {"svetlana-assistant", "svetlana-first-run", "svetlana-client-photo", "svetlana-calendar-booking", "svetlana-stock-photo", "svetlana-update-base"}
    if set(names) != expected:
        raise InstallError("Состав шести скиллов пакета не совпадает.")
    if ".agents/skills/svetlana-update-base/agents/openai.yaml" not in files:
        raise InstallError("Нет описания интерфейса навыка обновления базы.")
    return version, files


def _publish(path: Path, content: bytes, *, replace: bool) -> None:
    check_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".install-", delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # Publishing by an exclusive link avoids both overwrites and a
            # partially written destination after an interrupted create.
            os.link(temporary, path)
            temporary.unlink()
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_new(path: Path, content: bytes) -> None:
    _publish(path, content, replace=False)


def write_replacement(path: Path, content: bytes) -> None:
    _publish(path, content, replace=True)


def owned_hashes(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise InstallError("Нет проверяемого списка файлов прежней установки.")
    for name, fingerprint in value.items():
        if (not isinstance(name, str) or name.startswith("/") or "\\" in name
                or any(part in ("", ".", "..") for part in name.split("/"))
                or not (name == "AGENTS.md" or name.startswith("system/assistant/") or name.startswith(".agents/skills/"))
                or not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
            raise InstallError("Некорректный список принадлежащих пакету файлов.")
    return value


def installation_state(raw: bytes) -> dict:
    state = json.loads(raw)
    if not isinstance(state, dict) or state.get("kind") != KIND or not isinstance(state.get("version"), str):
        raise InstallError("Неизвестный журнал установки.")
    owned_hashes(state.get("files"))
    return state


def _current_hash(path: Path) -> str | None:
    check_path(path)
    if path.exists():
        return digest(source_file(path))
    return None


def upgrade_installation(project: Path, version: str, files: dict[str, bytes], hashes: dict[str, str],
                         state: dict, state_raw: bytes, bootstrap_raw: bytes) -> dict:
    """Resume an explicit migration using old/new hashes, never rollback user data."""
    journal_path = project / UPGRADE_FILE
    check_path(journal_path)
    resumed_upgrade = journal_path.exists()
    if resumed_upgrade:
        journal_raw = source_file(journal_path)
        journal = json.loads(journal_raw)
        if (not isinstance(journal, dict) or journal.get("kind") != KIND + "-upgrade"
                or (journal.get("from_version"), journal.get("to_version")) not in SUPPORTED_UPGRADES
                or journal.get("to_version") != version or journal.get("to_files") != hashes):
            raise InstallError("Незавершённое обновление относится к другому пакету. Нужна проверка.")
        old_hashes = owned_hashes(journal.get("from_files"))
        if state["version"] == version and state["files"] == hashes:
            # State publication succeeded and only journal cleanup was interrupted.
            for relative, expected in hashes.items():
                if _current_hash(project / relative) != expected:
                    raise InstallError(f"Файл изменён после обновления: {relative}")
            if source_file(project / BOOTSTRAP_FILE) != bootstrap_raw:
                raise InstallError("Журнал настройки изменился во время проверки; его содержимое сохранено.")
            journal_path.unlink()
            return {"created_files": 0, "updated_files": 0, "upgraded": True, "upgrade_resumed": True}
        if (state["version"] != journal["from_version"] or state["files"] != old_hashes
                or digest(state_raw) != journal.get("from_state_hash")):
            raise InstallError("Журнал прежней установки изменён во время обновления.")
    else:
        if (state["version"], version) not in SUPPORTED_UPGRADES:
            raise InstallError("Автоматическое обновление этой пары версий не поддерживается.")
        old_hashes = state["files"]
        journal = {"kind": KIND + "-upgrade", "from_version": state["version"], "to_version": version,
                   "from_state_hash": digest(state_raw), "from_files": old_hashes, "to_files": hashes}
        journal_raw = json_bytes(journal)

    # Check ALL previous files, including files no longer in the new payload,
    # and all newly occupied paths before the first replacement.
    for relative in sorted(set(old_hashes) | set(hashes)):
        actual = _current_hash(project / relative)
        if relative in old_hashes:
            allowed = {old_hashes[relative]}
            if resumed_upgrade and relative in hashes:
                allowed.add(hashes[relative])
            if actual not in allowed:
                raise InstallError(f"Прежний файл изменён или отсутствует: {relative}. Обновление остановлено.")
        elif actual is not None and not (resumed_upgrade and actual == hashes[relative]):
            raise InstallError(f"Новый путь занят пользовательским файлом: {relative}. Обновление остановлено.")
    if not resumed_upgrade:
        write_new(journal_path, journal_raw)

    created = updated = 0
    for relative, content in sorted(files.items()):
        target = project / relative
        actual = _current_hash(target)
        if actual == hashes[relative]:
            continue
        if relative in old_hashes and actual == old_hashes[relative]:
            write_replacement(target, content)
            updated += 1
        elif relative not in old_hashes and actual is None:
            write_new(target, content)
            created += 1
        else:
            raise InstallError(f"Файл изменился во время обновления: {relative}. Продолжение остановлено.")
    for relative, expected in hashes.items():
        if _current_hash(project / relative) != expected:
            raise InstallError(f"Не пройдена проверка обновления: {relative}")
    # Retired package files remain untouched; user files are never enumerated for deletion.
    for relative in old_hashes.keys() - hashes.keys():
        if _current_hash(project / relative) != old_hashes[relative]:
            raise InstallError(f"Прежний файл изменился во время обновления: {relative}")
    if source_file(project / BOOTSTRAP_FILE) != bootstrap_raw:
        raise InstallError("Журнал настройки изменился во время обновления; новое содержимое сохранено.")
    if source_file(project / STATE_FILE) != state_raw or source_file(journal_path) != journal_raw:
        raise InstallError("Журнал установки изменился во время обновления.")
    new_state = json_bytes({"kind": KIND, "version": version, "files": hashes})
    write_replacement(project / STATE_FILE, new_state)
    if source_file(project / STATE_FILE) != new_state:
        raise InstallError("Не пройдена проверка итогового журнала установки.")
    journal_path.unlink()
    return {"created_files": created, "updated_files": updated, "upgraded": True, "upgrade_resumed": resumed_upgrade}


def install(project: Path | str, package: Path = PACKAGE, *, upgrade: bool = False) -> dict:
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
    state = None
    state_raw = None
    if project.exists():
        if not project.is_dir():
            raise InstallError("Цель не является папкой.")
        if any(project.iterdir()):
            if not state_path.is_file():
                raise InstallError("Папка не пуста и не создана этим установщиком. Файлы сохранены.")
            state_raw = source_file(state_path)
            state = installation_state(state_raw)
            if not upgrade and (state.get("version") != version or state.get("files") != hashes):
                raise InstallError("Другая версия или состав пакета. Нужна отдельная проверка обновления.")
            resumed = True
    else:
        if upgrade:
            raise InstallError("Для --upgrade нужен ранее установленный проект.")
        project.mkdir(mode=0o700)
    if upgrade and state is None:
        raise InstallError("Для --upgrade нужен ранее установленный проект.")
    journal_path = project / UPGRADE_FILE
    check_path(journal_path)
    if journal_path.exists() and not upgrade:
        raise InstallError("Обновление прервано. Продолжите тем же пакетом с --upgrade.")
    migrating = bool(state and (state["version"] != version or journal_path.exists()))
    if state and not migrating and state["files"] != hashes:
        raise InstallError("Состав пакета этой версии изменился. Нужна отдельная проверка.")
    # Validate every existing owned path before making any changes on resume.
    if not migrating:
        for relative, content in files.items():
            path = project / relative
            check_path(path)
            if path.exists() and (not path.is_file() or digest(path.read_bytes()) != hashes[relative]):
                raise InstallError(f"Установленный файл изменён вручную: {relative}. Перезапись остановлена.")
    bootstrap_path = project / BOOTSTRAP_FILE
    check_path(bootstrap_path)
    if bootstrap_path.exists():
        bootstrap_raw = source_file(bootstrap_path)
        bootstrap = json.loads(bootstrap_raw)
        if not isinstance(bootstrap, dict) or bootstrap.get("engine") != "codex-google-skills" or not bootstrap.get("deployment_id"):
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
    if migrating:
        outcome = upgrade_installation(project, version, files, hashes, state, state_raw, bootstrap_raw)
        return {"status": "verified", "version": version, "project": str(project), "resumed": True,
                "verified_files": len(files), "cloud_status": "not_checked", **outcome,
                "next_step": "Откройте рабочую папку в Codex и начните новую задачу, чтобы обнаружить обновлённые скиллы."}
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
        "upgraded": False,
        "created_files": created,
        "verified_files": len(files),
        "cloud_status": "not_checked",
        "next_step": "Откройте рабочую папку в Codex, начните новую задачу и напишите «Привет».",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Установить скиллы Светланы в отдельный локальный проект.")
    parser.add_argument("--project", required=True, help="Абсолютный путь рабочей папки.")
    parser.add_argument("--upgrade", action="store_true", help="Явно обновить прежний проект со скиллами до версии пакета, сохранив настройки и пользовательские файлы.")
    args = parser.parse_args()
    try:
        result = install(args.project, upgrade=args.upgrade)
    except (InstallError, OSError, ValueError, KeyError) as error:
        print(f"Установка остановлена: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
