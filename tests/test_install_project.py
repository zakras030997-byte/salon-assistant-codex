from pathlib import Path
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer", PACKAGE / "scripts/install_project.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # macOS aliases /var -> /private/var; the installer rejects symlink paths.
        self.parent = Path(self.temp.name).resolve()
        self.project = self.parent / "Проект Светланы"

    def test_new_project_has_discoverable_skills_and_no_google_binding(self):
        result = installer.install(self.project)
        self.assertEqual(result["status"], "verified")
        self.assertEqual((self.project / "system/assistant/LICENSE").read_bytes(), (PACKAGE / "LICENSE").read_bytes())
        self.assertEqual(len(list((self.project / ".agents/skills").glob("*/SKILL.md"))), 6)
        for wrapper in (self.project / ".agents/skills").glob("*/SKILL.md"):
            target = wrapper.read_text().split("](", 1)[1].split(")", 1)[0]
            self.assertTrue((wrapper.parent / target).is_file())
        for source_ui in (PACKAGE / "skills").glob("*/agents/openai.yaml"):
            local_ui = self.project / ".agents/skills" / source_ui.relative_to(PACKAGE / "skills")
            self.assertEqual(local_ui.read_bytes(), source_ui.read_bytes())
        self.assertTrue((self.project / ".agents/skills/svetlana-update-base/agents/openai.yaml").is_file())
        data = json.loads((self.project / "system/BOOTSTRAP.json").read_text())
        self.assertIsNone(data["context_file_id"])
        self.assertIsNone(data["account_email"])
        self.assertEqual(data["defaults"]["timezone"], "Europe/Moscow")
        self.assertEqual(result["cloud_status"], "not_checked")

    def test_retry_preserves_answers_and_user_files(self):
        installer.install(self.project)
        path = self.project / "system/BOOTSTRAP.json"
        data = json.loads(path.read_text())
        data["context_file_id"] = "example-test-context"
        data["draft"] = {"display_name": "Светлана"}
        path.write_text(json.dumps(data, ensure_ascii=False))
        notes = self.project / "Заметки.txt"
        notes.write_text("Не менять")
        before = path.read_bytes()
        result = installer.install(self.project)
        self.assertEqual(result["created_files"], 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(notes.read_text(), "Не менять")

    def test_existing_unrelated_folder_is_untouched(self):
        self.project.mkdir()
        (self.project / "данные.txt").write_text("Сохранить")
        with self.assertRaises(installer.InstallError):
            installer.install(self.project)
        self.assertEqual([p.name for p in self.project.iterdir()], ["данные.txt"])

    def test_changed_installed_skill_is_not_overwritten(self):
        installer.install(self.project)
        path = self.project / "system/assistant/skills/svetlana-assistant/SKILL.md"
        path.write_text("Ручная правка")
        with self.assertRaises(installer.InstallError):
            installer.install(self.project)
        self.assertEqual(path.read_text(), "Ручная правка")

    def test_missing_installation_file_restored_without_reset(self):
        installer.install(self.project)
        state = (self.project / "system/BOOTSTRAP.json").read_bytes()
        path = self.project / ".agents/skills/svetlana-first-run/SKILL.md"
        path.unlink()
        result = installer.install(self.project)
        self.assertEqual(result["created_files"], 1)
        self.assertTrue(path.is_file())
        self.assertEqual((self.project / "system/BOOTSTRAP.json").read_bytes(), state)

    def test_symlink_does_not_write_outside_project(self):
        destination = self.parent / "other"
        destination.mkdir()
        self.project.symlink_to(destination, target_is_directory=True)
        with self.assertRaises(installer.InstallError):
            installer.install(self.project)
        self.assertEqual(list(destination.iterdir()), [])

    def test_replaced_system_symlink_is_rejected(self):
        installer.install(self.project)
        (self.project / "system").rename(self.project / "original-system")
        (self.project / "system").symlink_to(self.project / "original-system", target_is_directory=True)
        with self.assertRaises(installer.InstallError):
            installer.install(self.project)

    def test_version_change_is_not_an_implicit_upgrade(self):
        installer.install(self.project)
        path = self.project / "system/INSTALLATION.json"
        state = json.loads(path.read_text())
        state["version"] = "0.1.0"
        path.write_text(json.dumps(state))
        with self.assertRaises(installer.InstallError):
            installer.install(self.project)

    def test_relative_target_is_rejected(self):
        with self.assertRaises(installer.InstallError):
            installer.install(Path("relative/project"))

    def test_source_directory_cannot_become_working_data(self):
        with self.assertRaises(installer.InstallError):
            installer.install(PACKAGE / "working")

    def test_windows_checkout_line_endings(self):
        source = self.parent / "downloaded-package"
        shutil.copytree(PACKAGE, source)
        for path in source.rglob("*.md"):
            text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
            path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        result = installer.install(self.project, source)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(list((self.project / ".agents/skills").glob("*/SKILL.md"))), 6)

    def test_lost_bootstrap_does_not_reset_google_binding(self):
        installer.install(self.project)
        state = (self.project / "system/INSTALLATION.json").read_bytes()
        (self.project / "system/BOOTSTRAP.json").unlink()
        with self.assertRaisesRegex(installer.InstallError, "существующему Google-проекту"):
            installer.install(self.project)
        self.assertFalse((self.project / "system/BOOTSTRAP.json").exists())
        self.assertEqual((self.project / "system/INSTALLATION.json").read_bytes(), state)

    def legacy_project(self):
        """A five-skill 0.6.2 installation: routes have no adjacent UI metadata."""
        _, current = installer.payload(PACKAGE)
        old = {name: content for name, content in current.items()
               if "svetlana-update-base" not in name
               and not (name.startswith(".agents/skills/") and name.endswith("/agents/openai.yaml"))}
        old["AGENTS.md"] += b"\nPrevious package instructions.\n"
        manifest_name = "system/assistant/.codex-plugin/plugin.json"
        manifest = json.loads(old[manifest_name])
        manifest["version"] = "0.6.2"
        old[manifest_name] = installer.json_bytes(manifest)
        # A removed payload file must be checked and retained, not silently deleted.
        old["system/assistant/retired-reference.md"] = b"Retain this previous reference.\n"
        for relative, content in old.items():
            target = self.project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        state = {"kind": installer.KIND, "version": "0.6.2",
                 "files": {name: installer.digest(content) for name, content in sorted(old.items())}}
        state_raw = installer.json_bytes(state)
        (self.project / installer.STATE_FILE).write_bytes(state_raw)
        bootstrap = {
            "schema_version": 1, "engine": "codex-google-skills", "deployment_id": "fictional-deployment",
            "account_email": "owner@example.invalid", "root_folder_id": "fictional-root",
            "context_file_id": "fictional-context", "phase": "ready",
            "draft": {"display_name": "Вымышленная владелица"},
            "steps": [{"step": "context", "status": "verified"}],
            "custom_setting": {"preserve_unknown_field": True},
        }
        bootstrap_raw = json.dumps(bootstrap, ensure_ascii=False, indent=3).encode()
        (self.project / installer.BOOTSTRAP_FILE).write_bytes(bootstrap_raw)
        return old, state_raw, bootstrap_raw

    def file_snapshot(self):
        return {str(path.relative_to(self.project)): path.read_bytes()
                for path in self.project.rglob("*") if path.is_file() and not path.is_symlink()}

    def test_explicit_upgrade_keeps_bootstrap_user_files_and_retired_files(self):
        old, _, bootstrap = self.legacy_project()
        user_paths = ["Заметки.txt", "system/ЛИЧНЫЕ-ПРАВИЛА.md",
                      ".agents/skills/svetlana-update-base/personal-note.txt"]
        for name in user_paths:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"User content stays unchanged.\n")
        result = installer.install(self.project, upgrade=True)
        self.assertTrue(result["upgraded"])
        self.assertFalse(result["upgrade_resumed"])
        self.assertEqual(result["version"], installer.payload(PACKAGE)[0])
        self.assertEqual(result["cloud_status"], "not_checked")
        self.assertEqual((self.project / installer.BOOTSTRAP_FILE).read_bytes(), bootstrap)
        for name in user_paths:
            self.assertEqual((self.project / name).read_bytes(), b"User content stays unchanged.\n")
        self.assertEqual((self.project / "system/assistant/retired-reference.md").read_bytes(),
                         old["system/assistant/retired-reference.md"])
        state = json.loads((self.project / installer.STATE_FILE).read_bytes())
        version, expected = installer.payload(PACKAGE)
        self.assertEqual(state["version"], version)
        self.assertEqual(state["files"], {name: installer.digest(content) for name, content in expected.items()})
        self.assertEqual(len(list((self.project / ".agents/skills").glob("*/SKILL.md"))), 6)
        self.assertEqual((self.project / ".agents/skills/svetlana-update-base/agents/openai.yaml").read_bytes(),
                         (PACKAGE / "skills/svetlana-update-base/agents/openai.yaml").read_bytes())
        self.assertFalse((self.project / installer.UPGRADE_FILE).exists())
        after = self.file_snapshot()
        repeated = installer.install(self.project)
        self.assertFalse(repeated["upgraded"])
        self.assertEqual(repeated["created_files"], 0)
        self.assertEqual(self.file_snapshot(), after)

    def test_explicit_upgrade_from_080_to_current_preserves_bootstrap(self):
        version, old = installer.payload(PACKAGE)
        self.assertEqual(version, "0.8.2")
        old = dict(old)
        manifest_name = "system/assistant/.codex-plugin/plugin.json"
        manifest = json.loads(old[manifest_name])
        manifest["version"] = "0.8.0"
        old[manifest_name] = installer.json_bytes(manifest)
        for relative, content in old.items():
            target = self.project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        bootstrap = json.dumps({
            "schema_version": 1, "engine": "codex-google-skills", "deployment_id": "fixture-080",
            "account_email": "owner@example.invalid", "root_folder_id": "fixture-root",
            "context_file_id": "fixture-context", "phase": "ready",
        }, ensure_ascii=False, indent=2).encode()
        (self.project / installer.BOOTSTRAP_FILE).write_bytes(bootstrap)
        state = {"kind": installer.KIND, "version": "0.8.0",
                 "files": {name: installer.digest(content) for name, content in sorted(old.items())}}
        (self.project / installer.STATE_FILE).write_bytes(installer.json_bytes(state))

        result = installer.install(self.project, upgrade=True)

        self.assertTrue(result["upgraded"])
        self.assertEqual(result["version"], "0.8.2")
        self.assertEqual((self.project / installer.BOOTSTRAP_FILE).read_bytes(), bootstrap)
        installed_manifest = json.loads((self.project / manifest_name).read_text())
        self.assertEqual(installed_manifest["version"], "0.8.2")
        self.assertFalse((self.project / installer.UPGRADE_FILE).exists())

    def test_explicit_upgrade_from_081_to_current_preserves_bootstrap(self):
        version, old = installer.payload(PACKAGE)
        self.assertEqual(version, "0.8.2")
        old = dict(old)
        manifest_name = "system/assistant/.codex-plugin/plugin.json"
        manifest = json.loads(old[manifest_name])
        manifest["version"] = "0.8.1"
        old[manifest_name] = installer.json_bytes(manifest)
        for relative, content in old.items():
            target = self.project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        bootstrap = json.dumps({
            "schema_version": 1, "engine": "codex-google-skills", "deployment_id": "fixture-081",
            "account_email": "owner@example.invalid", "root_folder_id": "fixture-root",
            "context_file_id": "fixture-context", "phase": "ready",
        }, ensure_ascii=False, indent=2).encode()
        (self.project / installer.BOOTSTRAP_FILE).write_bytes(bootstrap)
        state = {"kind": installer.KIND, "version": "0.8.1",
                 "files": {name: installer.digest(content) for name, content in sorted(old.items())}}
        (self.project / installer.STATE_FILE).write_bytes(installer.json_bytes(state))

        result = installer.install(self.project, upgrade=True)

        self.assertTrue(result["upgraded"])
        self.assertEqual(result["version"], "0.8.2")
        self.assertEqual((self.project / installer.BOOTSTRAP_FILE).read_bytes(), bootstrap)
        installed_manifest = json.loads((self.project / manifest_name).read_text())
        self.assertEqual(installed_manifest["version"], "0.8.2")
        self.assertFalse((self.project / installer.UPGRADE_FILE).exists())

    def test_upgrade_requires_explicit_flag_and_leaves_legacy_project_unchanged(self):
        self.legacy_project()
        before = self.file_snapshot()
        with self.assertRaisesRegex(installer.InstallError, "Другая версия"):
            installer.install(self.project)
        self.assertEqual(self.file_snapshot(), before)

    def test_upgrade_preflights_every_old_file_before_changing_any_file(self):
        self.legacy_project()
        # This no-longer-distributed file sorts late; still verify it before any write.
        changed = self.project / "system/assistant/retired-reference.md"
        changed.write_bytes(b"Manually changed reference.")
        before = self.file_snapshot()
        with self.assertRaisesRegex(installer.InstallError, "Прежний файл изменён"):
            installer.install(self.project, upgrade=True)
        self.assertEqual(self.file_snapshot(), before)
        self.assertFalse((self.project / installer.UPGRADE_FILE).exists())

    def test_upgrade_rejects_user_file_on_new_route_before_any_change(self):
        self.legacy_project()
        collision = self.project / ".agents/skills/svetlana-update-base/SKILL.md"
        collision.parent.mkdir(parents=True)
        collision.write_bytes(b"User-owned skill.")
        before = self.file_snapshot()
        with self.assertRaisesRegex(installer.InstallError, "пользовательским файлом"):
            installer.install(self.project, upgrade=True)
        self.assertEqual(self.file_snapshot(), before)

    def test_upgrade_rejects_missing_old_file_and_keeps_state(self):
        self.legacy_project()
        (self.project / "AGENTS.md").unlink()
        before = self.file_snapshot()
        with self.assertRaisesRegex(installer.InstallError, "отсутствует"):
            installer.install(self.project, upgrade=True)
        self.assertEqual(self.file_snapshot(), before)

    def test_upgrade_rejects_missing_bootstrap_without_reset(self):
        self.legacy_project()
        (self.project / installer.BOOTSTRAP_FILE).unlink()
        before = self.file_snapshot()
        with self.assertRaisesRegex(installer.InstallError, "существующему Google-проекту"):
            installer.install(self.project, upgrade=True)
        self.assertEqual(self.file_snapshot(), before)

    def test_interrupted_upgrade_resumes_without_reset_or_early_state_publication(self):
        _, state_raw, bootstrap_raw = self.legacy_project()
        real_write = installer.write_replacement

        def interrupt_after_owned_replace(path, content):
            real_write(path, content)
            if path == self.project / "AGENTS.md":
                raise OSError("simulated interruption after completed atomic replacement")

        with patch.object(installer, "write_replacement", side_effect=interrupt_after_owned_replace):
            with self.assertRaises(OSError):
                installer.install(self.project, upgrade=True)
        self.assertEqual((self.project / installer.STATE_FILE).read_bytes(), state_raw)
        self.assertEqual((self.project / installer.BOOTSTRAP_FILE).read_bytes(), bootstrap_raw)
        self.assertTrue((self.project / installer.UPGRADE_FILE).is_file())
        with self.assertRaises(installer.InstallError):
            installer.install(self.project)
        resumed = installer.install(self.project, upgrade=True)
        self.assertTrue(resumed["upgrade_resumed"])
        self.assertEqual(resumed["status"], "verified")
        self.assertEqual((self.project / installer.BOOTSTRAP_FILE).read_bytes(), bootstrap_raw)
        self.assertFalse((self.project / installer.UPGRADE_FILE).exists())

    def test_resume_rejects_manual_edit_after_interruption_before_further_writes(self):
        self.legacy_project()
        real_write = installer.write_replacement

        def interrupt_state_commit(path, content):
            if path == self.project / installer.STATE_FILE:
                raise OSError("simulated failure before state commit")
            return real_write(path, content)

        with patch.object(installer, "write_replacement", side_effect=interrupt_state_commit):
            with self.assertRaises(OSError):
                installer.install(self.project, upgrade=True)
        edited = self.project / ".agents/skills/svetlana-update-base/SKILL.md"
        edited.write_bytes(b"Manual edit after interruption.")
        before = self.file_snapshot()
        with self.assertRaises(installer.InstallError):
            installer.install(self.project, upgrade=True)
        self.assertEqual(self.file_snapshot(), before)

    def test_resume_after_state_publication_only_cleans_up_journal(self):
        self.legacy_project()
        real_write = installer.write_replacement

        def interrupt_after_state_commit(path, content):
            real_write(path, content)
            if path == self.project / installer.STATE_FILE:
                raise OSError("simulated interruption after state commit")

        with patch.object(installer, "write_replacement", side_effect=interrupt_after_state_commit):
            with self.assertRaises(OSError):
                installer.install(self.project, upgrade=True)
        before = self.file_snapshot()
        result = installer.install(self.project, upgrade=True)
        self.assertTrue(result["upgrade_resumed"])
        self.assertEqual(result["created_files"], 0)
        self.assertEqual(result["updated_files"], 0)
        before.pop(installer.UPGRADE_FILE.as_posix())
        self.assertEqual(self.file_snapshot(), before)

    def test_upgrade_rejects_unsafe_old_manifest_path(self):
        self.legacy_project()
        state_path = self.project / installer.STATE_FILE
        state = json.loads(state_path.read_bytes())
        state["files"]["../outside.txt"] = installer.digest(b"outside")
        state_path.write_bytes(installer.json_bytes(state))
        before = self.file_snapshot()
        with self.assertRaisesRegex(installer.InstallError, "список принадлежащих"):
            installer.install(self.project, upgrade=True)
        self.assertEqual(self.file_snapshot(), before)

    def test_cli_upgrade_flag_updates_legacy_project(self):
        self.legacy_project()
        result = subprocess.run([sys.executable, str(PACKAGE / "scripts/install_project.py"),
                                 "--project", str(self.project), "--upgrade"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["upgraded"])

    def test_upgrade_flag_does_not_create_a_fresh_project(self):
        with self.assertRaisesRegex(installer.InstallError, "ранее установленный"):
            installer.install(self.project, upgrade=True)
        self.assertFalse(self.project.exists())


if __name__ == "__main__":
    unittest.main()
