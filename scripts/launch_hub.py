#!/usr/bin/env python3
from __future__ import annotations

import argparse
import secrets
import subprocess
import sys
import tempfile
import webbrowser
from collections.abc import Callable
from pathlib import Path
from threading import Thread

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from scripts.browser_bridge import (
    create_bridge_server,
    handoff_url,
)


CONFIGURE_EXIT_CODE = 75


def run_hub(
    *,
    profile_path: Path,
    python_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
    bridge_factory: Callable[..., object] = create_bridge_server,
    opener: Callable[[str], bool] = webbrowser.open,
    token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
) -> int:
    with tempfile.TemporaryDirectory(prefix="arxiv-hub-bridge-") as temporary:
        state_path = Path(temporary) / "destination.json"
        bridge_token = token_factory()
        bridge = bridge_factory(
            token=bridge_token,
            state_path=state_path,
        )
        bridge_thread = Thread(target=bridge.serve_forever, daemon=True)
        bridge_thread.start()
        generation = 0

        def run_child(script_name: str) -> subprocess.CompletedProcess[object]:
            nonlocal generation
            generation += 1
            state_path.unlink(missing_ok=True)
            current_handoff = handoff_url(
                bridge.server_port,
                bridge_token,
                after=generation,
            )
            return runner(
                [
                    str(python_path),
                    str(APP_ROOT / "scripts" / script_name),
                    "--profile",
                    str(profile_path),
                    "--no-open",
                    "--bridge-state",
                    str(state_path),
                    "--bridge-generation",
                    str(generation),
                    "--supervisor-handoff-url",
                    current_handoff,
                ],
                check=False,
            )

        try:
            initial_url = handoff_url(
                bridge.server_port,
                bridge_token,
                after=0,
            )
            if not opener(initial_url):
                print(
                    f"Could not open a browser. Open this URL: {initial_url}",
                    file=sys.stderr,
                )
                return 2
            while True:
                if not profile_path.is_file():
                    configured = run_child("setup_profile.py")
                    if (
                        configured.returncode != 0
                        or not profile_path.is_file()
                    ):
                        return int(configured.returncode)

                viewed = run_child("launch_viewer.py")
                if viewed.returncode != CONFIGURE_EXIT_CODE:
                    return int(viewed.returncode)

                configured = run_child("setup_profile.py")
                if configured.returncode != 0:
                    return int(configured.returncode)
        finally:
            bridge.shutdown()
            bridge.server_close()
            bridge_thread.join(timeout=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open arXiv Hub and supervise configuration transitions."
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=(
            Path.home()
            / "Library"
            / "Application Support"
            / "arXiv Hub"
            / "profile.toml"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run_hub(
        profile_path=args.profile.expanduser(),
        python_path=Path(sys.executable),
    )


if __name__ == "__main__":
    raise SystemExit(main())
