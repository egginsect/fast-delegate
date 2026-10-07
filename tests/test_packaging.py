"""Tests for fast-delegate packaging (plugin manifests and installation)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / ".claude-plugin"
INSTALL_SCRIPT = ROOT / "install.sh"
SKILL_DIR = ROOT / "skills/fast-delegate"


class TestPluginManifests(unittest.TestCase):
    """Test Claude Code plugin JSON files."""

    def test_plugin_json_valid(self):
        """plugin.json must be valid JSON with required fields."""
        plugin_file = PLUGIN_DIR / "plugin.json"
        self.assertTrue(plugin_file.exists(), f"{plugin_file} does not exist")

        with open(plugin_file) as f:
            plugin = json.load(f)

        # Check required fields
        required_fields = {"name", "description", "version", "author", "license", "repository", "homepage"}
        missing_fields = required_fields - set(plugin.keys())
        self.assertFalse(missing_fields, f"Missing fields in plugin.json: {missing_fields}")

        # Validate specific values
        self.assertEqual(plugin["name"], "fast-delegate")
        self.assertEqual(plugin["license"], "MIT")
        self.assertIn("github.com/egginsect/fast-delegate", plugin["repository"])
        self.assertIn("github.com/egginsect/fast-delegate", plugin["homepage"])
        # Validate author is an object with "name"
        self.assertIsInstance(plugin["author"], dict, "author must be an object")
        self.assertIn("name", plugin["author"], "author must have a name field")

    def test_marketplace_json_valid(self):
        """marketplace.json must be valid JSON with required fields."""
        marketplace_file = PLUGIN_DIR / "marketplace.json"
        self.assertTrue(marketplace_file.exists(), f"{marketplace_file} does not exist")

        with open(marketplace_file) as f:
            marketplace = json.load(f)

        # Check required fields
        required_fields = {"name", "owner", "plugins"}
        missing_fields = required_fields - set(marketplace.keys())
        self.assertFalse(missing_fields, f"Missing fields in marketplace.json: {missing_fields}")

        # Validate structure
        self.assertEqual(marketplace["name"], "fast-delegate")
        # Validate owner is an object with "name"
        self.assertIsInstance(marketplace["owner"], dict, "owner must be an object")
        self.assertIn("name", marketplace["owner"], "owner must have a name field")
        self.assertIsInstance(marketplace["plugins"], list)
        self.assertGreater(len(marketplace["plugins"]), 0)

        # Validate plugin entry
        plugin_entry = marketplace["plugins"][0]
        self.assertIn("name", plugin_entry)
        self.assertIn("description", plugin_entry)
        self.assertIn("source", plugin_entry)
        self.assertEqual(plugin_entry["name"], "fast-delegate")

    def test_skill_directory_exists(self):
        """The plugin must reference an existing skill directory."""
        self.assertTrue(SKILL_DIR.exists(), f"Skill directory {SKILL_DIR} does not exist")
        skill_md = SKILL_DIR / "SKILL.md"
        self.assertTrue(skill_md.exists(), f"SKILL.md not found in {SKILL_DIR}")


class TestInstallScript(unittest.TestCase):
    """Test the install.sh script for Codex and Claude."""

    def _run_install(self, runtime, temp_home, temp_claude_config, force=False):
        """Helper to run install.sh with temp directories."""
        env = os.environ.copy()
        env["HOME"] = temp_home
        if runtime == "claude":
            env["CLAUDE_CONFIG_DIR"] = temp_claude_config

        cmd = [str(INSTALL_SCRIPT), runtime]
        if force:
            cmd.append("--force")

        result = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True)
        return result

    def _check_installed_files(self, target_dir):
        """Verify required files are installed."""
        skill_md = Path(target_dir) / "SKILL.md"
        harness_json = Path(target_dir) / "harness.json"
        fdel_py = Path(target_dir) / "scripts/fdel.py"

        self.assertTrue(skill_md.exists(), f"SKILL.md not found in {target_dir}")
        self.assertTrue(harness_json.exists(), f"harness.json not found in {target_dir}")
        self.assertTrue(fdel_py.exists(), f"scripts/fdel.py not found in {target_dir}")

        # Verify fdel.py is executable
        result = subprocess.run([sys.executable, str(fdel_py), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"fdel.py --help failed: {result.stderr}")

    def test_install_codex(self):
        """install.sh codex should copy to ~/.agents/skills/fast-delegate."""
        with tempfile.TemporaryDirectory() as temp_home:
            result = self._run_install("codex", temp_home, None)
            self.assertEqual(result.returncode, 0, f"install codex failed: {result.stderr}")

            target_dir = Path(temp_home) / ".agents/skills/fast-delegate"
            self._check_installed_files(target_dir)

    def test_install_claude(self):
        """install.sh claude should copy to $CLAUDE_CONFIG_DIR/skills/fast-delegate."""
        with tempfile.TemporaryDirectory() as temp_home:
            with tempfile.TemporaryDirectory() as temp_claude_config:
                result = self._run_install("claude", temp_home, temp_claude_config)
                self.assertEqual(result.returncode, 0, f"install claude failed: {result.stderr}")

                target_dir = Path(temp_claude_config) / "skills/fast-delegate"
                self._check_installed_files(target_dir)

    def test_install_refuses_overwrite(self):
        """install.sh should refuse to overwrite without --force."""
        with tempfile.TemporaryDirectory() as temp_home:
            # Install once
            result = self._run_install("codex", temp_home, None)
            self.assertEqual(result.returncode, 0)

            # Try to install again without --force
            result = self._run_install("codex", temp_home, None, force=False)
            self.assertNotEqual(result.returncode, 0, "Should fail without --force on existing directory")

    def test_install_with_force_overwrites(self):
        """install.sh --force should allow overwriting."""
        with tempfile.TemporaryDirectory() as temp_home:
            # Install once
            result = self._run_install("codex", temp_home, None)
            self.assertEqual(result.returncode, 0)

            # Install again with --force
            result = self._run_install("codex", temp_home, None, force=True)
            self.assertEqual(result.returncode, 0, f"install --force failed: {result.stderr}")

            # Verify files still exist and are correct
            target_dir = Path(temp_home) / ".agents/skills/fast-delegate"
            self._check_installed_files(target_dir)

    def test_install_excludes_pycache(self):
        """install.sh should exclude __pycache__ directories."""
        with tempfile.TemporaryDirectory() as temp_home:
            result = self._run_install("codex", temp_home, None)
            self.assertEqual(result.returncode, 0)

            target_dir = Path(temp_home) / ".agents/skills/fast-delegate"
            pycache_dirs = list(target_dir.glob("**/__pycache__"))
            self.assertEqual(len(pycache_dirs), 0, f"Found __pycache__ in {target_dir}: {pycache_dirs}")


class TestInstallDetection(unittest.TestCase):
    """install.sh with no runtime named detects runtimes from PATH and config dirs."""

    TOOLS = ("sh", "find", "cp", "mkdir", "dirname", "rm")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        # A PATH holding only the utilities install.sh needs, so CLIs installed on the
        # machine running the tests never count as detected.
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for tool in self.TOOLS:
            real = subprocess.run(["sh", "-c", f"command -v {tool}"], capture_output=True, text=True).stdout.strip()
            (self.bin / tool).symlink_to(real)

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_cli(self, name):
        path = self.bin / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)

    def _run(self, *args):
        env = {"HOME": str(self.home), "PATH": str(self.bin)}
        return subprocess.run(["sh", str(INSTALL_SCRIPT), *args], cwd=str(ROOT), env=env,
                              capture_output=True, text=True)

    def _installed(self, rel):
        return (self.home / rel / "fast-delegate/SKILL.md").exists()

    def test_nothing_detected_fails_without_writing(self):
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No supported runtime detected", result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_detects_cli_on_path(self):
        self._fake_cli("claude")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._installed(".claude/skills"))
        self.assertFalse(self._installed(".agents/skills"))

    def test_detects_config_dir(self):
        (self.home / ".codex").mkdir()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._installed(".agents/skills"))
        self.assertFalse(self._installed(".claude/skills"))

    def test_cursor_installs_to_shared_agents_dir(self):
        self._fake_cli("cursor-agent")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._installed(".agents/skills"))

    def test_codex_and_cursor_share_one_copy(self):
        self._fake_cli("codex")
        (self.home / ".cursor").mkdir()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("Installed fast-delegate to"), 1, result.stdout)
        self.assertTrue(self._installed(".agents/skills"))

    def test_all_detected(self):
        for cli in ("claude", "codex", "cursor-agent"):
            self._fake_cli(cli)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._installed(".claude/skills"))
        self.assertTrue(self._installed(".agents/skills"))

    def test_explicit_runtime_skips_detection(self):
        self._fake_cli("claude")
        result = self._run("codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._installed(".agents/skills"))
        self.assertFalse(self._installed(".claude/skills"))

    def test_force_accepted_in_any_position(self):
        self._fake_cli("codex")
        self.assertEqual(self._run().returncode, 0)
        result = self._run("--force", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self._run("--force")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_conflict_on_one_target_writes_nothing(self):
        self._fake_cli("claude")
        self._fake_cli("codex")
        existing = self.home / ".agents/skills/fast-delegate"
        existing.mkdir(parents=True)
        (existing / "keep.txt").write_text("mine")
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--force", result.stderr)
        self.assertFalse(self._installed(".claude/skills"))
        self.assertTrue((existing / "keep.txt").exists())

    def test_invalid_argument_rejected(self):
        result = self._run("vscode")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid argument", result.stderr)


if __name__ == "__main__":
    unittest.main()
