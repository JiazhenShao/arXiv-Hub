from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class PreferenceEvidence:
    source: str
    weight: float
    explicit: bool
    fingerprint: str
    fallback_changed_at: datetime


@dataclass(frozen=True)
class ResolvedPreference:
    weight: float
    source: str
    changed_at: datetime


class PreferenceSignalStore:
    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._signals: dict[str, dict[str, dict[str, str]]] = {}
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                signals = payload.get("signals", {})
                if isinstance(signals, dict):
                    self._signals = signals
            except (OSError, TypeError, ValueError):
                self._signals = {}

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _observe(
        self,
        arxiv_id: str,
        evidence: PreferenceEvidence,
    ) -> ResolvedPreference:
        paper_signals = self._signals.setdefault(arxiv_id, {})
        previous = paper_signals.get(evidence.source)
        if previous is None:
            changed_at = self._aware(evidence.fallback_changed_at)
        elif previous.get("fingerprint") != evidence.fingerprint:
            changed_at = self._aware(self._now())
        else:
            try:
                changed_at = self._aware(
                    datetime.fromisoformat(str(previous["changed_at"]))
                )
            except (KeyError, TypeError, ValueError):
                changed_at = self._aware(evidence.fallback_changed_at)
        paper_signals[evidence.source] = {
            "fingerprint": evidence.fingerprint,
            "changed_at": changed_at.isoformat(),
        }
        return ResolvedPreference(evidence.weight, evidence.source, changed_at)

    def resolve(
        self,
        arxiv_id: str,
        *,
        report: PreferenceEvidence | None,
        library: PreferenceEvidence | None,
    ) -> ResolvedPreference:
        report_signal = self._observe(arxiv_id, report) if report else None
        library_signal = self._observe(arxiv_id, library) if library else None
        if report_signal is None and library_signal is None:
            raise ValueError("At least one preference signal is required")
        if report_signal is None:
            return library_signal  # type: ignore[return-value]
        if library_signal is None or not library.explicit:  # type: ignore[union-attr]
            return report_signal
        if library_signal.changed_at > report_signal.changed_at:
            return library_signal
        return report_signal

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {"version": 1, "signals": self._signals},
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
