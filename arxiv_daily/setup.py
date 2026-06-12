from __future__ import annotations

import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class SetupValues:
    record_dir: Path
    active_library_dir: Path
    archive_library_dir: Path
    timezone: str
    search_time: str
    contact_email: str


def validate_setup(values: SetupValues) -> None:
    paths = (
        values.record_dir.expanduser(),
        values.active_library_dir.expanduser(),
        values.archive_library_dir.expanduser(),
    )
    if any(not path.is_absolute() for path in paths):
        raise ValueError("All data folders must be absolute paths")
    if len({path.resolve(strict=False) for path in paths}) != len(paths):
        raise ValueError("Reports, papers, and archive folders must be different")
    try:
        ZoneInfo(values.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {values.timezone}") from exc
    try:
        parsed_time = time.fromisoformat(values.search_time)
    except ValueError as exc:
        raise ValueError("Search time must use HH:MM format") from exc
    if parsed_time.second or parsed_time.microsecond:
        raise ValueError("Search time must use HH:MM format")
    if not EMAIL_RE.fullmatch(values.contact_email.strip()):
        raise ValueError("Enter a valid contact email for the arXiv user agent")


def build_profile_text(values: SetupValues, *, preset_path: Path) -> str:
    return build_profile_from_preset_text(
        values,
        preset_path.read_text(encoding="utf-8"),
    )


def build_profile_from_preset_text(
    values: SetupValues,
    preset_text: str,
) -> str:
    validate_setup(values)
    try:
        preset_data = tomllib.loads(preset_text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Advanced profile TOML is invalid: {exc}") from exc
    forbidden = {"paths", "viewer", "source"}.intersection(preset_data)
    if forbidden:
        names = ", ".join(sorted(forbidden))
        raise ValueError(f"Advanced profile must not redefine: {names}")
    for required in ("categories", "ranking", "model", "topics"):
        if required not in preset_data:
            raise ValueError(f"Advanced profile is missing [{required}] settings")
    preset = preset_text.strip()
    email = values.contact_email.strip()
    return (
        "[paths]\n"
        f'record_dir = "{_toml_string(values.record_dir.expanduser())}"\n'
        f'active_library_dir = "{_toml_string(values.active_library_dir.expanduser())}"\n'
        f'archive_library_dir = "{_toml_string(values.archive_library_dir.expanduser())}"\n'
        "\n[viewer]\n"
        f'timezone = "{_toml_string(values.timezone)}"\n'
        f'search_time = "{_toml_string(values.search_time)}"\n'
        "\n[source]\n"
        f'user_agent = "arxiv-hub/1.0 ({_toml_string(email)})"\n'
        "min_interval_seconds = 3.0\n"
        "retry_backoffs = [10.0, 20.0, 40.0]\n"
        "retry_deadline_seconds = 90.0\n"
        "timeout_seconds = 60.0\n\n"
        f"{preset}\n"
    )


def write_profile_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _toml_string(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')
