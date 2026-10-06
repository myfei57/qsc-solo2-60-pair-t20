"""Calibration and concentration versions in effect when a dose happened.

Both registries are append only: a meter recalibration or a new solution
concentration is a new version with an ``effective_from`` stamp, never an edit
of an old version. Settlement looks up the version in effect at the dose time,
so a later recalibration cannot change how an older shift was collected.

Fallbacks (used before any version exists) are pinned into every run
fingerprint, which is how a change to the live flow calibration marks older
settlement runs stale even though the raw audit entries never move.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

from .timeutil import epoch_of, stamp_now

CALIBRATION_KEY = "settle:calibration-versions"
CONCENTRATION_KEY = "settle:concentration-versions"

DEFAULT_CALIBRATION = 1.0
DEFAULT_CONCENTRATION = 1.0


@dataclass(frozen=True)
class BasisVersion:
    """One effective dated calibration or concentration value."""

    id: str
    chemical: str
    value: float
    effective_from: str
    recorded_at: str
    note: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "chemical": self.chemical,
            "value": self.value,
            "effective_from": self.effective_from,
            "recorded_at": self.recorded_at,
            "note": self.note,
        }


def validate_basis_value(value: float) -> None:
    if value <= 0:
        raise ValueError("calibration and concentration values must be positive")
    if value > 1_000_000:
        raise ValueError("basis value exceeds the supported range")


class _VersionRegistry:
    def __init__(self, store: Store, key: str, kind: str) -> None:
        self._store = store
        self._key = key
        self._kind = kind

    def register(
        self,
        chemical: str,
        value: float,
        effective_from: str | None = None,
        note: str = "",
    ) -> BasisVersion:
        if not chemical:
            raise ValueError("chemical is required")
        validate_basis_value(value)
        effective = effective_from or stamp_now()
        # Parse up front so an invalid stamp is rejected before appending.
        epoch_of(effective)
        version = BasisVersion(
            id=uuid.uuid4().hex,
            chemical=chemical,
            value=float(value),
            effective_from=effective,
            recorded_at=stamp_now(),
            note=note,
        )
        append_command(self._store, self._key, json.dumps(version.as_dict(), ensure_ascii=False))
        return version

    def versions(self, chemical: str | None = None) -> list[BasisVersion]:
        versions: list[BasisVersion] = []
        for item in load_commands(self._store, self._key):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            name = str(payload.get("chemical", ""))
            if chemical is not None and name != chemical:
                continue
            try:
                value = float(payload.get("value", ""))
            except ValueError:
                continue
            versions.append(
                BasisVersion(
                    id=str(payload.get("id", "")),
                    chemical=name,
                    value=value,
                    effective_from=str(payload.get("effective_from", "")),
                    recorded_at=str(payload.get("recorded_at", "")),
                    note=str(payload.get("note", "")),
                )
            )
        return versions

    def effective(
        self, chemical: str, when: str, fallback: float
    ) -> tuple[float, str | None]:
        """Return the value in effect at ``when`` plus its version id."""

        target = epoch_of(when)
        chosen: BasisVersion | None = None
        for version in self.versions(chemical):
            try:
                effective = epoch_of(version.effective_from)
            except ValueError:
                continue
            if effective <= target and (
                chosen is None or effective > epoch_of(chosen.effective_from)
            ):
                chosen = version
        if chosen is None:
            return fallback, None
        return chosen.value, chosen.id

    def describe(self) -> str:
        return f"settle {self._kind} versions={len(self.versions())}"


class CalibrationRegistry(_VersionRegistry):
    """Effective dated meter calibration factors per chemical."""

    def __init__(self, store: Store) -> None:
        super().__init__(store, CALIBRATION_KEY, "calibration")

    def effective_for(self, chemical: str, when: str, live_factor: float) -> tuple[float, str | None]:
        # Coagulant doses are computed from the live flow meter factor; other
        # chemicals (e.g. chlorine residual) have no meter calibration.
        fallback = live_factor if chemical == "coagulant" else DEFAULT_CALIBRATION
        return self.effective(chemical, when, fallback)


class ConcentrationRegistry(_VersionRegistry):
    """Effective dated delivered solution concentrations per chemical."""

    def __init__(self, store: Store) -> None:
        super().__init__(store, CONCENTRATION_KEY, "concentration")

    def effective_for(self, chemical: str, when: str) -> tuple[float, str | None]:
        return self.effective(chemical, when, DEFAULT_CONCENTRATION)
