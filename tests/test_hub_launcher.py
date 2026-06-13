from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts.launch_hub import APP_ROOT, CONFIGURE_EXIT_CODE, run_hub


class FakeBridgeServer:
    server_port = 8124

    def __init__(self) -> None:
        self.stopped = threading.Event()
        self.closed = False

    def serve_forever(self) -> None:
        self.stopped.wait(timeout=2)

    def shutdown(self) -> None:
        self.stopped.set()

    def server_close(self) -> None:
        self.closed = True


class HubSupervisorTests(unittest.TestCase):
    def test_script_entrypoint_loads_application_packages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(APP_ROOT / "scripts" / "launch_hub.py"),
                    "--help",
                ],
                cwd=tmp,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("supervise configuration transitions", result.stdout)

    def run_supervisor(
        self,
        *,
        profile: Path,
        runner: Mock,
    ) -> tuple[int, Mock, FakeBridgeServer]:
        opener = Mock(return_value=True)
        bridge = FakeBridgeServer()
        result = run_hub(
            profile_path=profile,
            python_path=Path("/tmp/python"),
            runner=runner,
            bridge_factory=Mock(return_value=bridge),
            opener=opener,
            token_factory=lambda: "bridge secret",
        )
        return result, opener, bridge

    def test_first_run_configures_then_opens_viewer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile.toml"

            def runner(command: list[str], **kwargs: object) -> Mock:
                if command[1].endswith("setup_profile.py"):
                    profile.write_text("[paths]\n", encoding="utf-8")
                return Mock(returncode=0)

            result, opener, bridge = self.run_supervisor(
                profile=profile,
                runner=Mock(side_effect=runner),
            )

        self.assertEqual(result, 0)
        opener.assert_called_once()
        self.assertIn("after=0", opener.call_args.args[0])
        self.assertTrue(bridge.closed)

    def test_first_run_close_without_profile_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile.toml"
            runner = Mock(return_value=Mock(returncode=0))

            result, _, bridge = self.run_supervisor(
                profile=profile,
                runner=runner,
            )

        self.assertEqual(result, 0)
        self.assertEqual(runner.call_count, 1)
        self.assertTrue(
            runner.call_args.args[0][1].endswith("setup_profile.py")
        )
        self.assertTrue(bridge.closed)

    def test_existing_profile_opens_viewer_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile.toml"
            profile.touch()
            runner = Mock(return_value=Mock(returncode=0))

            result, opener, _ = self.run_supervisor(
                profile=profile,
                runner=runner,
            )

        self.assertEqual(result, 0)
        self.assertEqual(runner.call_count, 1)
        self.assertTrue(
            runner.call_args.args[0][1].endswith("launch_viewer.py")
        )
        opener.assert_called_once()

    def test_configure_exit_switches_to_editor_then_back_to_viewer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile.toml"
            profile.touch()
            runner = Mock(
                side_effect=[
                    Mock(returncode=CONFIGURE_EXIT_CODE),
                    Mock(returncode=0),
                    Mock(returncode=0),
                ]
            )

            result, _, _ = self.run_supervisor(
                profile=profile,
                runner=runner,
            )

        self.assertEqual(result, 0)
        scripts = [
            Path(call.args[0][1]).name for call in runner.call_args_list
        ]
        self.assertEqual(
            scripts,
            ["launch_viewer.py", "setup_profile.py", "launch_viewer.py"],
        )
        commands = [call.args[0] for call in runner.call_args_list]
        for generation, command in enumerate(commands, start=1):
            self.assertIn("--no-open", command)
            self.assertEqual(
                command[command.index("--bridge-generation") + 1],
                str(generation),
            )
            handoff = command[
                command.index("--supervisor-handoff-url") + 1
            ]
            self.assertIn(f"after={generation}", handoff)
        bridge_states = {
            command[command.index("--bridge-state") + 1]
            for command in commands
        }
        self.assertEqual(len(bridge_states), 1)

    def test_viewer_failure_is_returned_without_opening_configure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile.toml"
            profile.touch()
            runner = Mock(return_value=Mock(returncode=2))

            result, _, bridge = self.run_supervisor(
                profile=profile,
                runner=runner,
            )

        self.assertEqual(result, 2)
        self.assertEqual(runner.call_count, 1)
        self.assertTrue(bridge.closed)

    def test_browser_open_failure_stops_before_child_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile.toml"
            profile.touch()
            runner = Mock()
            bridge = FakeBridgeServer()

            result = run_hub(
                profile_path=profile,
                python_path=Path("/tmp/python"),
                runner=runner,
                bridge_factory=Mock(return_value=bridge),
                opener=Mock(return_value=False),
                token_factory=lambda: "bridge secret",
            )

        self.assertEqual(result, 2)
        runner.assert_not_called()
        self.assertTrue(bridge.closed)


if __name__ == "__main__":
    unittest.main()
