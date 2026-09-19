from pathlib import Path
import importlib.util
import json
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

PACKAGE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("plugin_installer", PACKAGE / "scripts/install_plugin.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class FakeCodexCLI:
    """In-memory CLI responses: no subprocess, Google access, or Codex config."""
    def __init__(self, bundle, *, catalog_exists=False, installed=False, lose_add_reply=False):
        self.bundle = bundle
        self.calls = []
        self.lose_add_reply = lose_add_reply
        self.marketplaces = ([{"name": installer.MARKETPLACE, "root": bundle["root"]}]
                             if catalog_exists else [])
        self.installed = [self.plugin_row()] if installed else []

    def plugin_row(self):
        return {"pluginId": installer.PLUGIN_ID, "version": self.bundle["version"],
                "installed": True, "enabled": True,
                "source": {"source": "local", "path": self.bundle["plugin"]}}

    def call(self, args):
        self.calls.append(list(args))
        if args == ["marketplace", "list"]:
            return {"marketplaces": self.marketplaces}
        if args == ["marketplace", "add", self.bundle["root"]]:
            self.marketplaces = [{"name": installer.MARKETPLACE, "root": self.bundle["root"]}]
            return {"ok": True}
        if args == ["list", "--marketplace", installer.MARKETPLACE]:
            return {"installed": self.installed}
        if args == ["add", installer.PLUGIN_ID]:
            self.installed = [self.plugin_row()]
            if self.lose_add_reply:
                raise installer.PluginInstallError("Synthetic lost reply after a successful add")
            return {"ok": True}
        raise AssertionError(f"Unexpected fake CLI command: {args}")

    def mutations(self):
        return [args for args in self.calls if args[0] == "add" or args[:2] == ["marketplace", "add"]]


class PluginInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # macOS aliases /var -> /private/var; test canonical paths.
        self.parent = Path(self.temp.name).resolve()
        self.marketplace = self.parent / "Каталог плагинов"
        self.project = self.parent / "Проект Светланы"

    @staticmethod
    def snapshot(root):
        if not root.exists():
            return None
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*") if path.is_file() and not path.is_symlink()}

    def package_fixture(self):
        source = self.parent / "package-source"
        source.mkdir()
        for directory in (".codex-plugin", "skills", "scripts", "templates"):
            shutil.copytree(PACKAGE / directory, source / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("AGENTS.md", "LICENSE", "INSTALL.md"):
            shutil.copyfile(PACKAGE / name, source / name)
        return source

    def test_fresh_project_has_only_bootstrap_and_project_instructions(self):
        result = installer.prepare_project(self.project)
        self.assertEqual(Path(result["project"]), self.project)
        self.assertEqual(result["version"], json.loads((PACKAGE / ".codex-plugin/plugin.json").read_text())["version"])
        self.assertEqual(set(self.snapshot(self.project)), {
            "AGENTS.md", "system/BOOTSTRAP.json", "system/PLUGIN-INSTALLATION.json"})
        self.assertFalse((self.project / ".agents/skills").exists())
        self.assertFalse((self.project / "system/assistant").exists())
        state = json.loads((self.project / "system/PLUGIN-INSTALLATION.json").read_text())
        self.assertEqual(state["kind"], "salon-plugin-project")
        bootstrap = json.loads((self.project / "system/BOOTSTRAP.json").read_text())
        self.assertEqual(bootstrap["engine"], "codex-google-skills")
        self.assertTrue(bootstrap["deployment_id"])
        self.assertIsNone(bootstrap["account_email"])
        self.assertIsNone(bootstrap["root_folder_id"])
        self.assertIsNone(bootstrap["context_file_id"])
        self.assertEqual(bootstrap["defaults"]["timezone"], "Europe/Moscow")

    def test_retry_preserves_google_binding_unknown_fields_and_user_files(self):
        installer.prepare_project(self.project)
        path = self.project / "system/BOOTSTRAP.json"
        bootstrap = json.loads(path.read_text())
        bootstrap.update(account_email="owner@example.invalid", root_folder_id="fictional-root",
                         context_file_id="fictional-context", phase="ready",
                         custom_setting={"preserve_unknown_field": [1, 2, 3]})
        path.write_bytes(json.dumps(bootstrap, ensure_ascii=False, indent=3).encode())
        (self.project / "Заметки.txt").write_text("Сохранить личную заметку")
        before = self.snapshot(self.project)
        installer.prepare_project(self.project)
        self.assertEqual(self.snapshot(self.project), before)

    def test_independent_projects_get_distinct_deployment_ids(self):
        another = self.parent / "Второй салон"
        installer.prepare_project(self.project)
        installer.prepare_project(another)
        first = json.loads((self.project / "system/BOOTSTRAP.json").read_text())
        second = json.loads((another / "system/BOOTSTRAP.json").read_text())
        self.assertNotEqual(first["deployment_id"], second["deployment_id"])
        self.assertIsNone(first["context_file_id"])
        self.assertIsNone(second["context_file_id"])

    def test_foreign_and_legacy_projects_are_untouched(self):
        for name, files in (
            ("foreign", {"notes.txt": b"User file"}),
            ("legacy", {"AGENTS.md": b"Legacy route", "system/INSTALLATION.json": b'{"kind":"svetlana-project-skills","version":"0.7.0"}',
                        ".agents/skills/svetlana-assistant/SKILL.md": b"Legacy skill"}),
        ):
            with self.subTest(name=name):
                project = self.parent / name
                for relative, content in files.items():
                    target = project / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                before = self.snapshot(project)
                with self.assertRaises(installer.PluginInstallError):
                    installer.prepare_project(project)
                self.assertEqual(self.snapshot(project), before)

    def test_lost_bootstrap_is_not_recreated(self):
        installer.prepare_project(self.project)
        (self.project / "system/BOOTSTRAP.json").unlink()
        before = self.snapshot(self.project)
        with self.assertRaises(installer.PluginInstallError):
            installer.prepare_project(self.project)
        self.assertEqual(self.snapshot(self.project), before)

    def test_invalid_bootstrap_is_not_replaced(self):
        installer.prepare_project(self.project)
        path = self.project / "system/BOOTSTRAP.json"
        for invalid in (b"broken-json", b"null", b"{}", b'{"engine":"foreign","deployment_id":"fixture"}'):
            with self.subTest(invalid=invalid):
                path.write_bytes(invalid)
                before = self.snapshot(self.project)
                with self.assertRaises(installer.PluginInstallError):
                    installer.prepare_project(self.project)
                self.assertEqual(self.snapshot(self.project), before)

    def test_changed_project_instructions_are_not_overwritten(self):
        installer.prepare_project(self.project)
        (self.project / "AGENTS.md").write_text("User-edited instructions")
        before = self.snapshot(self.project)
        with self.assertRaises(installer.PluginInstallError):
            installer.prepare_project(self.project)
        self.assertEqual(self.snapshot(self.project), before)

    def test_project_cannot_be_inside_package_source(self):
        source = self.package_fixture()
        before = self.snapshot(source)
        with self.assertRaises(installer.PluginInstallError):
            installer.prepare_project(source / "working-project", package=source)
        self.assertEqual(self.snapshot(source), before)
        self.assertFalse((source / "working-project").exists())

    def test_project_symlink_component_is_rejected(self):
        outside = self.parent / "outside"
        outside.mkdir()
        alias = self.parent / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(installer.PluginInstallError):
            installer.prepare_project(alias / "workspace")
        self.assertEqual(list(outside.iterdir()), [])

    def test_replaced_project_system_symlink_does_not_write_outside(self):
        installer.prepare_project(self.project)
        system = self.project / "system"
        outside = self.parent / "saved-system"
        system.rename(outside)
        system.symlink_to(outside, target_is_directory=True)
        before = self.snapshot(outside)
        with self.assertRaises(installer.PluginInstallError):
            installer.prepare_project(self.project)
        self.assertEqual(self.snapshot(outside), before)

    def test_marketplace_build_is_repeatable_and_contains_six_skills(self):
        result = installer.build_marketplace(self.marketplace)
        self.assertEqual(Path(result["root"]), self.marketplace)
        self.assertIn("plugin", result)
        self.assertEqual(result["version"], json.loads((PACKAGE / ".codex-plugin/plugin.json").read_text())["version"])
        manifest = self.marketplace / ".agents/plugins/marketplace.json"
        self.assertIsInstance(json.loads(manifest.read_text()), dict)
        plugin = self.marketplace / "plugins/svetlana-assistant"
        self.assertEqual(json.loads((plugin / ".codex-plugin/plugin.json").read_text())["version"], result["version"])
        self.assertEqual((plugin / "AGENTS.md").read_bytes(), (PACKAGE / "AGENTS.md").read_bytes())
        self.assertEqual((plugin / "LICENSE").read_bytes(), (PACKAGE / "LICENSE").read_bytes())
        self.assertEqual({p.parent.name for p in (plugin / "skills").glob("*/SKILL.md")},
                         {p.parent.name for p in (PACKAGE / "skills").glob("*/SKILL.md")})
        self.assertEqual(len(list((plugin / "skills").glob("*/SKILL.md"))), 6)
        cloud_template = json.loads((plugin / "templates/CLOUD-PROJECT-CONTEXT.json").read_text())
        self.assertEqual(cloud_template["binding_kind"], "salon-assistant-cloud-project")
        self.assertEqual(cloud_template["plugin_id"], installer.PLUGIN_ID)
        before = self.snapshot(self.marketplace)
        installer.build_marketplace(self.marketplace)
        self.assertEqual(self.snapshot(self.marketplace), before)

    def test_all_six_skills_accept_the_explicit_cloud_context_without_bootstrap(self):
        resolver = (PACKAGE / "skills/svetlana-assistant/references/project-context.md").read_text()
        self.assertIn("CLOUD-PROJECT-CONTEXT.json", resolver)
        self.assertIn("новом устройстве", resolver)
        for skill in (PACKAGE / "skills").glob("*/SKILL.md"):
            with self.subTest(skill=skill.parent.name):
                content = skill.read_text()
                self.assertIn("единый resolver контекста", content)
                self.assertNotIn("Если его нет, не ищи чужие проекты", content)

    def test_marketplace_excludes_private_and_unrelated_files(self):
        source = self.package_fixture()
        marker = b"PRIVATE_FIXTURE_DO_NOT_SHIP_f2ac73"
        private_paths = (".env", "secrets.json", "system/BOOTSTRAP.json", "clients/client.txt",
                         ".git/config", "tests/private-fixture.txt", "__pycache__/private.pyc")
        for relative in private_paths:
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(marker)
        before = self.snapshot(source)
        installer.build_marketplace(self.marketplace, package=source)
        self.assertEqual(self.snapshot(source), before)
        packaged = self.snapshot(self.marketplace)
        self.assertTrue(packaged)
        for relative, content in packaged.items():
            with self.subTest(path=relative):
                self.assertNotIn(marker, content)
                self.assertNotIn("__pycache__", relative)

    def test_foreign_marketplace_directory_is_untouched(self):
        self.marketplace.mkdir()
        (self.marketplace / "user.txt").write_text("Keep me")
        before = self.snapshot(self.marketplace)
        with self.assertRaises(installer.PluginInstallError):
            installer.build_marketplace(self.marketplace)
        self.assertEqual(self.snapshot(self.marketplace), before)

    def test_changed_packaged_skill_is_not_overwritten(self):
        installer.build_marketplace(self.marketplace)
        skill = self.marketplace / "plugins/svetlana-assistant/skills/svetlana-assistant/SKILL.md"
        skill.write_text("User-edited packaged skill")
        before = self.snapshot(self.marketplace)
        with self.assertRaises(installer.PluginInstallError):
            installer.build_marketplace(self.marketplace)
        self.assertEqual(self.snapshot(self.marketplace), before)

    def test_marketplace_symlink_component_is_rejected(self):
        outside = self.parent / "outside"
        outside.mkdir()
        alias = self.parent / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(installer.PluginInstallError):
            installer.build_marketplace(alias / "marketplace")
        self.assertEqual(list(outside.iterdir()), [])

    def test_plugin_package_and_marketplace_cannot_be_working_projects(self):
        installer.build_marketplace(self.marketplace)
        plugin = self.marketplace / "plugins/svetlana-assistant"
        before = self.snapshot(self.marketplace)
        for project, source in ((self.marketplace, PACKAGE), (plugin, plugin), (plugin / "workspace", plugin)):
            with self.subTest(project=project):
                with self.assertRaises(installer.PluginInstallError):
                    installer.prepare_project(project, package=source)
                self.assertEqual(self.snapshot(self.marketplace), before)

    def test_relative_targets_are_rejected(self):
        for action in (installer.prepare_project, installer.build_marketplace):
            with self.subTest(action=action.__name__):
                with self.assertRaises(installer.PluginInstallError):
                    action(Path("relative-target"))


    def activation_fixture(self):
        installer.prepare_project(self.project)
        return {"root": str(self.marketplace),
                "plugin": str(self.marketplace / "plugins/svetlana-assistant"),
                "version": json.loads((PACKAGE / ".codex-plugin/plugin.json").read_text())["version"]}

    def test_activate_installs_then_repeats_without_add_or_receipt_churn(self):
        bundle = self.activation_fixture()
        cli = FakeCodexCLI(bundle)
        result = installer.activate(self.project, bundle, cli)
        self.assertEqual(result["status"], "plugin_installed_verified")
        self.assertEqual(result["cloud_status"], "not_checked")
        self.assertEqual(result["new_session_status"], "not_checked")
        self.assertEqual(cli.mutations(), [["marketplace", "add", bundle["root"]], ["add", installer.PLUGIN_ID]])
        receipt = self.project / "system/PLUGIN-SETUP.json"
        before = (receipt.read_bytes(), receipt.stat().st_mtime_ns)
        cli.calls.clear()
        repeated = installer.activate(self.project, bundle, cli)
        self.assertEqual(repeated["status"], "plugin_installed_verified")
        self.assertEqual(cli.mutations(), [])
        self.assertEqual((receipt.read_bytes(), receipt.stat().st_mtime_ns), before)

    def test_pending_plugin_unknown_blocks_repeat_before_mutation(self):
        bundle = self.activation_fixture()
        receipt = self.project / "system/PLUGIN-SETUP.json"
        receipt.write_text(json.dumps({"plugin_id": installer.PLUGIN_ID, "root": bundle["root"],
                                      "version": bundle["version"], "phase": "plugin_pending"}))
        before = receipt.read_bytes()
        cli = FakeCodexCLI(bundle, catalog_exists=True)
        with self.assertRaises(installer.PluginInstallError):
            installer.activate(self.project, bundle, cli)
        self.assertEqual(cli.mutations(), [])
        self.assertEqual(receipt.read_bytes(), before)

    def test_lost_add_reply_is_resolved_by_readback_without_second_add(self):
        bundle = self.activation_fixture()
        cli = FakeCodexCLI(bundle, catalog_exists=True, lose_add_reply=True)
        result = installer.activate(self.project, bundle, cli)
        self.assertEqual(result["status"], "plugin_installed_verified")
        self.assertEqual(cli.mutations(), [["add", installer.PLUGIN_ID]])
        self.assertEqual(cli.calls[-1], ["list", "--marketplace", installer.MARKETPLACE])
        self.assertEqual(json.loads((self.project / "system/PLUGIN-SETUP.json").read_text())["phase"], "verified")

    def test_foreign_registered_marketplace_root_blocks_mutations(self):
        bundle = self.activation_fixture()
        cli = FakeCodexCLI(bundle, catalog_exists=True)
        cli.marketplaces[0]["root"] = str(self.parent / "different-publisher")
        before = self.snapshot(self.project)
        with self.assertRaises(installer.PluginInstallError):
            installer.activate(self.project, bundle, cli)
        self.assertEqual(cli.mutations(), [])
        self.assertEqual(self.snapshot(self.project), before)

    def test_disabled_wrong_version_or_wrong_source_plugin_is_preserved(self):
        bundle = self.activation_fixture()
        for changed in ({"enabled": False}, {"version": "99.0.0"},
                        {"source": {"source": "local", "path": str(self.parent / "other-plugin")}}):
            with self.subTest(changed=changed):
                cli = FakeCodexCLI(bundle, catalog_exists=True, installed=True)
                cli.installed[0].update(changed)
                before = self.snapshot(self.project)
                with self.assertRaises(installer.PluginInstallError):
                    installer.activate(self.project, bundle, cli)
                self.assertEqual(cli.mutations(), [])
                self.assertEqual(self.snapshot(self.project), before)

    def test_cli_operations_journal_symlink_blocks_subprocess_launch(self):
        self.activation_fixture()
        outside = self.parent / "outside-record.txt"
        outside.write_text("Do not append anything")
        (self.project / "system/PLUGIN-OPERATIONS.jsonl").symlink_to(outside)
        with patch.object(installer.shutil, "which", return_value="/fictional/codex"):
            cli = installer.CodexCLI("codex", self.project)
        with patch.object(installer.subprocess, "Popen") as popen:
            with self.assertRaises(installer.PluginInstallError):
                cli.call(["marketplace", "list"])
            popen.assert_not_called()
        self.assertEqual(outside.read_text(), "Do not append anything")

    def test_log_start_failure_reaps_only_the_started_process(self):
        self.activation_fixture()
        with patch.object(installer.shutil, "which", return_value="/fictional/codex"):
            cli = installer.CodexCLI("codex", self.project)
        process = Mock(pid=987654)
        process.communicate.return_value = ("", "")
        with patch.object(installer.subprocess, "Popen", return_value=process) as popen:
            with patch.object(Path, "open", side_effect=OSError("Synthetic journal write failure")):
                with self.assertRaises(OSError):
                    cli.call(["marketplace", "list"])
            popen.assert_called_once()
            process.terminate.assert_called_once_with()
            process.communicate.assert_called_once_with(timeout=5)
            process.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
