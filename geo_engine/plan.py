"""The test plan file: one YAML file per test, the single source of truth.

Every agent reads and writes it; it doubles as the pre-registration and the audit trail.
Each section belongs to one step of the workflow (see workflow.py). Four gates record
your approvals. At gate G3 (launch) the brief, data and design sections are locked: a
hash of them is stored, and saving a plan whose locked sections changed is refused
unless the change was logged with record_deviation().
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

STATUSES = ("draft", "brief_approved", "data_ready", "design_approved", "locked", "live",
            "analysed", "results_approved", "closed")
GATES = {"G1": "brief", "G2": "design", "G3": "launch", "G4": "results"}
LOCKED_SECTIONS = ("brief", "data", "design")
BRIEF_REQUIRED = ("question", "channel", "test_type", "kpi", "kpi_source", "success_metric",
                  "decision_rule")


class PlanError(ValueError):
    """The plan cannot do what was asked. The message says why and what to do instead."""


class LockedError(PlanError):
    """A locked section was changed without logging a deviation."""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _empty(test_id: str) -> dict:
    return {
        "test_id": test_id, "status": "draft", "created": now(), "updated": now(),
        "brief": {}, "data": {}, "design": {}, "launch": {}, "monitoring": [],
        "deviations": [], "results": {}, "readout": {},
        "gates": {g: None for g in GATES}, "lock": None, "history": [],
    }


class TestPlan:
    """A test plan held in memory; save() writes it back, checking the lock."""

    __test__ = False  # not a pytest test class

    def __init__(self, data: dict, path: Path | None = None):
        self.d = data
        self.path = path

    # ---- files ---------------------------------------------------------------------
    @classmethod
    def new(cls, test_id: str, folder: str | Path) -> "TestPlan":
        if not test_id.replace("_", "").replace("-", "").isalnum():
            raise PlanError("test_id may use letters, digits, - and _ only")
        path = Path(folder) / test_id / "test_plan.yaml"
        if path.exists():
            raise PlanError(f"{path} already exists; load it instead")
        plan = cls(_empty(test_id), path)
        plan.log("created")
        plan.save()
        return plan

    @classmethod
    def load(cls, path: str | Path) -> "TestPlan":
        path = Path(path)
        if path.is_dir():
            path = path / "test_plan.yaml"
        data = yaml.safe_load(path.read_text())
        plan = cls(data, path)
        plan._check_lock()
        return plan

    def save(self) -> Path:
        if self.path is None:
            raise PlanError("this plan has no file path")
        self._check_lock()
        self.d["updated"] = now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(self.d, sort_keys=False, allow_unicode=True, width=100))
        return self.path

    @property
    def folder(self) -> Path:
        return self.path.parent

    # ---- state -----------------------------------------------------------------------
    @property
    def status(self) -> str:
        return self.d["status"]

    def at_least(self, status: str) -> bool:
        return STATUSES.index(self.status) >= STATUSES.index(status)

    def set_status(self, status: str) -> None:
        if status not in STATUSES:
            raise PlanError(f"unknown status {status}")
        self.d["status"] = status
        self.log(f"status -> {status}")

    def log(self, event: str) -> None:
        self.d.setdefault("history", []).append({"at": now(), "event": event})

    def section(self, name: str) -> dict:
        return self.d[name]

    def update(self, name: str, values: dict) -> None:
        """Write fields into a section. Locked sections refuse: use record_deviation()."""
        if self.d.get("lock") and name in LOCKED_SECTIONS:
            raise LockedError(f"'{name}' is locked since {self.d['lock']['at']}; "
                              "use record_deviation() with a reason")
        self.d[name].update(copy.deepcopy(values))

    # ---- lock and deviations -----------------------------------------------------------
    def _locked_hash(self) -> str:
        blob = json.dumps({k: self.d[k] for k in LOCKED_SECTIONS}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def lock(self, by: str) -> None:
        self.d["lock"] = {"at": now(), "by": by, "hash": self._locked_hash()}
        self.log(f"brief, data and design locked by {by}")

    def _check_lock(self) -> None:
        lk = self.d.get("lock")
        if lk and lk["hash"] != self._locked_hash():
            raise LockedError("the brief, data or design changed after the lock without a logged "
                              "deviation; undo the edit or use record_deviation()")

    def record_deviation(self, section: str, field: str, value, reason: str, by: str) -> None:
        """Change a locked field after launch, keeping the old value and the reason on record."""
        if not reason.strip():
            raise PlanError("a deviation needs a reason")
        if section not in LOCKED_SECTIONS:
            raise PlanError(f"only locked sections need deviations; just update '{section}'")
        old = self.d[section].get(field)
        self.d[section][field] = copy.deepcopy(value)
        self.d["deviations"].append({"at": now(), "by": by, "section": section, "field": field,
                                     "from": old, "to": value, "reason": reason})
        if self.d.get("lock"):
            self.d["lock"]["hash"] = self._locked_hash()
        self.log(f"deviation: {section}.{field} changed by {by}")

    # ---- gates -----------------------------------------------------------------------
    def record_gate(self, gate: str, by: str, note: str = "") -> None:
        self.d["gates"][gate] = {"approved_by": by, "at": now(), "note": note}
        self.log(f"{gate} ({GATES[gate]}) approved by {by}")
