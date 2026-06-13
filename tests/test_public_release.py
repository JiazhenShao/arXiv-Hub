from __future__ import annotations

import tempfile
import unittest
import os
import subprocess
from datetime import time
from pathlib import Path
from unittest.mock import Mock

from arxiv_daily.config import ProfileConfig
from arxiv_daily.setup import SetupValues, build_profile_text, validate_setup
from scripts.launch_viewer import open_default_browser


ROOT = Path(__file__).resolve().parents[1]


class PublicProfileTests(unittest.TestCase):
    def test_setup_writes_portable_paths_and_viewer_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            values = SetupValues(
                record_dir=home / "Documents" / "arXiv Hub" / "Reports",
                active_library_dir=home / "Documents" / "arXiv Hub" / "Papers",
                archive_library_dir=(
                    home / "Documents" / "arXiv Hub" / "Papers Archive"
                ),
                timezone="America/New_York",
                search_time="21:30",
                contact_email="reader@example.org",
            )

            profile_text = build_profile_text(
                values,
                preset_path=ROOT / "config" / "nuclear-particle.toml",
            )
            profile_path = home / "profile.toml"
            profile_path.write_text(profile_text, encoding="utf-8")
            config = ProfileConfig.load(profile_path)

        self.assertEqual(config.record_dir, values.record_dir)
        self.assertEqual(config.active_library_dir, values.active_library_dir)
        self.assertEqual(config.archive_library_dir, values.archive_library_dir)
        self.assertEqual(config.viewer_timezone, "America/New_York")
        self.assertEqual(config.search_start_time, time(21, 30))
        self.assertIn("reader@example.org", config.user_agent)
        self.assertIn("nuclear astrophysics", config.topic_weights)

    def test_setup_rejects_invalid_timezone_time_email_and_overlapping_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = SetupValues(
                record_dir=root / "Reports",
                active_library_dir=root / "Papers",
                archive_library_dir=root / "Archive",
                timezone="America/Chicago",
                search_time="20:00",
                contact_email="reader@example.org",
            )
            cases = [
                SetupValues(**{**valid.__dict__, "timezone": "Not/AZone"}),
                SetupValues(**{**valid.__dict__, "search_time": "25:00"}),
                SetupValues(**{**valid.__dict__, "contact_email": "not-email"}),
                SetupValues(
                    **{
                        **valid.__dict__,
                        "archive_library_dir": valid.active_library_dir,
                    }
                ),
            ]

            for values in cases:
                with self.subTest(values=values):
                    with self.assertRaises(ValueError):
                        validate_setup(values)


class PublicLauncherTests(unittest.TestCase):
    def test_launcher_uses_default_browser(self) -> None:
        opener = Mock(return_value=True)

        open_default_browser("http://127.0.0.1:8123/", opener=opener)

        opener.assert_called_once_with("http://127.0.0.1:8123/")


class PublicRepositoryTests(unittest.TestCase):
    def test_repository_contains_no_command_launchers(self) -> None:
        command_files = sorted(
            path.relative_to(ROOT)
            for path in ROOT.rglob("*.command")
            if ".git" not in path.parts
        )

        self.assertEqual(
            command_files,
            [
                Path("Install arXiv Hub.command"),
                Path("Uninstall arXiv Hub.command"),
            ],
        )

    def test_pdf_reader_is_pinned_for_seed_library_scans(self) -> None:
        requirements = (ROOT / "requirements.lock").read_text(encoding="utf-8")

        self.assertIn("pypdf==6.13.2\n", requirements)

    def test_spotlight_launcher_uses_the_unified_supervisor(self) -> None:
        launcher = (ROOT / "launcher" / "arXiv Hub.launcher.zsh").read_text(
            encoding="utf-8"
        )

        self.assertIn("scripts/launch_hub.py", launcher)
        self.assertNotIn("scripts/launch_viewer.py", launcher)

    def test_repository_does_not_contain_a_spotlight_command_duplicate(self) -> None:
        self.assertFalse((ROOT / "launcher" / "arXiv Hub.command").exists())

    def test_installer_and_uninstaller_manage_only_one_launcher(self) -> None:
        installer = (ROOT / "Install arXiv Hub.command").read_text(
            encoding="utf-8"
        )
        uninstaller = (ROOT / "Uninstall arXiv Hub.command").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("CONFIGURE_PATH=", installer)
        self.assertNotIn(
            'ditto "$SOURCE_DIR/launcher/Configure arXiv Hub.command"',
            installer,
        )
        self.assertIn(
            'ditto "$SOURCE_DIR/launcher/arXiv Hub.launcher.zsh" '
            '"$LAUNCHER_PATH"',
            installer,
        )
        self.assertIn(
            'rm -f "$LAUNCHER_DIR/Configure arXiv Hub.command"',
            installer,
        )
        self.assertNotIn("CONFIGURE=", uninstaller)
        self.assertIn(
            'rm -f "$HOME/Applications/Configure arXiv Hub.command"',
            uninstaller,
        )

    def test_installer_defers_first_run_setup_to_unified_launcher(self) -> None:
        installer = (ROOT / "Install arXiv Hub.command").read_text(
            encoding="utf-8"
        )

        self.assertNotIn('"$NEW_APP/scripts/setup_profile.py"', installer)
        self.assertIn('if [[ -f "$PROFILE_PATH" ]]', installer)

    def test_installer_manages_private_python_without_homebrew(self) -> None:
        installer = (ROOT / "Install arXiv Hub.command").read_text(
            encoding="utf-8"
        )

        self.assertIn("UV_PYTHON_INSTALL_DIR", installer)
        self.assertIn("python install 3.12", installer)
        self.assertIn("Library/Application Support/arXiv Hub", installer)
        self.assertNotIn("brew install", installer)
        self.assertNotIn("/Users/" + "janshao", installer)

    def test_repository_contains_no_personal_paths_or_runtime_state(self) -> None:
        banned_text = (
            "/Users/" + "janshao",
            "GRAD" + "_STUDY",
            '"Daily" paper' + " reads",
            "shaojzh5" + "@gmail.com",
        )
        banned_parts = {".git", ".venv", ".state", "__pycache__"}
        inspected = 0

        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in banned_parts for part in path.parts):
                continue
            if path.suffix.lower() in {".ttf", ".woff", ".woff2", ".png"}:
                continue
            text = path.read_text(encoding="utf-8")
            inspected += 1
            for value in banned_text:
                self.assertNotIn(value, text, str(path))

        self.assertGreater(inspected, 20)

    def test_installer_dry_run_isolated_from_real_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "HOME": tmp,
                "ARXIV_HUB_INSTALL_DRY_RUN": "1",
            }
            result = subprocess.run(
                ["/bin/zsh", str(ROOT / "Install arXiv Hub.command")],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Dry run", result.stdout)
            self.assertTrue(
                (
                    Path(tmp)
                    / "Library"
                    / "Application Support"
                    / "arXiv Hub"
                ).is_dir()
            )

    def test_offline_math_bundle_contains_only_runtime_assets(self) -> None:
        katex = ROOT / "viewer-assets" / "katex"
        for relative in (
            "katex.min.css",
            "katex.min.js",
            "contrib/auto-render.min.js",
            "LICENSE",
            "VERSION",
        ):
            self.assertTrue((katex / relative).is_file(), relative)
        fonts = list((katex / "fonts").iterdir())
        self.assertGreaterEqual(len(fonts), 20)
        self.assertTrue(all(path.suffix == ".woff2" for path in fonts))


if __name__ == "__main__":
    unittest.main()
