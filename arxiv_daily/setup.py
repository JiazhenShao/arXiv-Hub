from __future__ import annotations

import os
import re
import shutil
import tempfile
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime, time
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


@dataclass(frozen=True)
class CategoryPreference:
    name: str
    weight: float


@dataclass(frozen=True)
class TopicPreference:
    name: str
    weight: float
    phrases: tuple[str, ...]


@dataclass(frozen=True)
class ProfileEditorState:
    record_dir: Path
    active_library_dir: Path
    archive_library_dir: Path
    seed_library_dir: Path | None
    seed_library_limit: int
    timezone: str
    search_time: str
    categories: tuple[CategoryPreference, ...]
    topics: tuple[TopicPreference, ...]
    ranking: dict[str, object]
    model: dict[str, object]
    source: dict[str, object]


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


def load_profile_editor(path: Path) -> ProfileEditorState:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    paths = data["paths"]
    viewer = data.get("viewer", {})
    ranking = dict(data["ranking"])
    seed_value = str(paths.get("seed_library_dir", "")).strip()
    return ProfileEditorState(
        record_dir=Path(paths["record_dir"]).expanduser(),
        active_library_dir=Path(paths["active_library_dir"]).expanduser(),
        archive_library_dir=Path(paths["archive_library_dir"]).expanduser(),
        seed_library_dir=Path(seed_value).expanduser() if seed_value else None,
        seed_library_limit=int(ranking.pop("seed_library_limit", 100)),
        timezone=str(viewer.get("timezone", "America/Chicago")),
        search_time=str(viewer.get("search_time", "20:00")),
        categories=tuple(
            CategoryPreference(str(name), float(weight))
            for name, weight in data["categories"].items()
        ),
        topics=tuple(
            TopicPreference(
                name=str(item["name"]),
                weight=float(item["weight"]),
                phrases=tuple(str(value) for value in item["phrases"]),
            )
            for item in data["topics"]
        ),
        ranking=ranking,
        model=dict(data["model"]),
        source=dict(data["source"]),
    )


def render_profile_editor(state: ProfileEditorState) -> str:
    _validate_editor_state(state)
    lines = [
        "[paths]",
        f'record_dir = "{_toml_string(state.record_dir)}"',
        f'active_library_dir = "{_toml_string(state.active_library_dir)}"',
        f'archive_library_dir = "{_toml_string(state.archive_library_dir)}"',
    ]
    if state.seed_library_dir is not None:
        lines.append(
            f'seed_library_dir = "{_toml_string(state.seed_library_dir)}"'
        )
    lines.extend(
        [
            "",
            "[viewer]",
            f'timezone = "{_toml_string(state.timezone)}"',
            f'search_time = "{_toml_string(state.search_time)}"',
            "",
            "[source]",
        ]
    )
    lines.extend(_render_mapping(state.source))
    lines.extend(["", "[categories]"])
    lines.extend(
        f'"{_toml_string(item.name)}" = {_toml_number(item.weight)}'
        for item in state.categories
    )
    lines.extend(["", "[ranking]"])
    ranking = dict(state.ranking)
    ranking["seed_library_limit"] = state.seed_library_limit
    lines.extend(_render_mapping(ranking))
    lines.extend(["", "[model]"])
    lines.extend(_render_mapping(state.model))
    for topic in state.topics:
        lines.extend(
            [
                "",
                "[[topics]]",
                f'name = "{_toml_string(topic.name)}"',
                f"weight = {_toml_number(topic.weight)}",
                "phrases = [",
            ]
        )
        lines.extend(
            f'  "{_toml_string(phrase)}",'
            for phrase in topic.phrases
        )
        lines.append("]")
    return "\n".join(lines) + "\n"


def apply_profile_editor_payload(
    current: ProfileEditorState,
    payload: dict[str, object],
) -> ProfileEditorState:
    seed_text = str(payload.get("seed_library_dir", "")).strip()
    categories_raw = payload.get("categories", [])
    topics_raw = payload.get("topics", [])
    if not isinstance(categories_raw, list) or not isinstance(topics_raw, list):
        raise ValueError("Categories and topics must be lists")
    try:
        categories = tuple(
            CategoryPreference(
                name=str(item["name"]).strip(),
                weight=float(item["weight"]),
            )
            for item in categories_raw
            if isinstance(item, dict)
        )
        topics = tuple(
            TopicPreference(
                name=str(item["name"]).strip(),
                weight=float(item["weight"]),
                phrases=tuple(
                    str(value).strip()
                    for value in item["phrases"]
                    if str(value).strip()
                ),
            )
            for item in topics_raw
            if isinstance(item, dict)
        )
        updated = replace(
            current,
            record_dir=Path(str(payload["record_dir"])).expanduser(),
            active_library_dir=Path(
                str(payload["active_library_dir"])
            ).expanduser(),
            archive_library_dir=Path(
                str(payload["archive_library_dir"])
            ).expanduser(),
            seed_library_dir=(
                Path(seed_text).expanduser() if seed_text else None
            ),
            seed_library_limit=int(payload.get("seed_library_limit", 100)),
            timezone=str(payload["timezone"]),
            search_time=str(payload["search_time"]),
            categories=categories,
            topics=topics,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Profile editor payload is incomplete") from exc
    _validate_editor_state(updated)
    return updated


def save_profile_with_backup(
    path: Path,
    text: str,
    *,
    now: datetime | None = None,
) -> Path:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Profile must be an existing regular file")
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.backup-{timestamp}")
    if backup.exists():
        raise FileExistsError(f"Profile backup already exists: {backup}")
    shutil.copy2(path, backup)
    try:
        write_profile_atomic(path, text)
    except Exception:
        backup.replace(path)
        raise
    return backup


def _validate_editor_state(state: ProfileEditorState) -> None:
    values = SetupValues(
        record_dir=state.record_dir,
        active_library_dir=state.active_library_dir,
        archive_library_dir=state.archive_library_dir,
        timezone=state.timezone,
        search_time=state.search_time,
        contact_email="profile@example.invalid",
    )
    validate_setup(values)
    if state.seed_library_dir is not None:
        seed = state.seed_library_dir.expanduser()
        if not seed.is_absolute():
            raise ValueError("The seed PDF folder must be an absolute path")
    if not 25 <= state.seed_library_limit <= 500:
        raise ValueError("Seed library limit must be between 25 and 500")
    if not state.categories:
        raise ValueError("Select at least one arXiv category")
    if not state.topics:
        raise ValueError("Add at least one interest topic")
    for category in state.categories:
        if not category.name.strip() or not 0 <= category.weight <= 1:
            raise ValueError("Category names and weights must be valid")
    for topic in state.topics:
        if (
            not topic.name.strip()
            or not 0 <= topic.weight <= 1
            or not topic.phrases
            or any(not phrase.strip() for phrase in topic.phrases)
        ):
            raise ValueError("Each topic needs a name, weight, and keywords")


def _render_mapping(values: dict[str, object]) -> list[str]:
    return [
        f"{key} = {_toml_value(value)}"
        for key, value in values.items()
    ]


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return f'"{_toml_string(value)}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _toml_number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"Unsupported profile value: {value!r}")


def _toml_number(value: int | float) -> str:
    return str(value) if isinstance(value, int) else repr(float(value))


def _toml_string(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')
