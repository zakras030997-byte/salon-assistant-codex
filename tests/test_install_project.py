from pathlib import Path
import importlib.util
import json
import shutil
import tempfile
import unittest

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
        self.assertEqual(len(list((self.project / ".agents/skills").glob("*/SKILL.md"))), 5)
        for wrapper in (self.project / ".agents/skills").glob("*/SKILL.md"):
            target = wrapper.read_text().split("](", 1)[1].split(")", 1)[0]
            self.assertTrue((wrapper.parent / target).is_file())
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
        self.assertEqual(len(list((self.project / ".agents/skills").glob("*/SKILL.md"))), 5)

    def test_lost_bootstrap_does_not_reset_google_binding(self):
        installer.install(self.project)
        state = (self.project / "system/INSTALLATION.json").read_bytes()
        (self.project / "system/BOOTSTRAP.json").unlink()
        with self.assertRaisesRegex(installer.InstallError, "существующему Google-проекту"):
            installer.install(self.project)
        self.assertFalse((self.project / "system/BOOTSTRAP.json").exists())
        self.assertEqual((self.project / "system/INSTALLATION.json").read_bytes(), state)


if __name__ == "__main__":
    unittest.main()
