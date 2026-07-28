from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ExperimentArm(StrEnum):
    control = "control"  # the live pipeline, untouched
    treatment = "treatment"  # the candidate change, run in shadow


class ExperimentStatus(StrEnum):
    running = "running"
    promoted = "promoted"
    rejected = "rejected"


class ImprovementExperiment(BaseModel):
    """One shadow A/B: a candidate change to a component, tested *beside* the live
    pipeline before anything is promoted.

    The candidate (``treatment``) runs on the same real inputs as the live pipeline
    (``control``) but its output is **quarantined** — never written to the knowledge
    base. Each arm's outputs are graded and the scores accumulate here (running
    sums). Once both arms have ``min_sample`` graded samples, the decision rule
    promotes the candidate only if treatment beats control by ``margin``. Nothing
    goes live until it has won on real inputs over a window.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    field_id: str
    component: str  # e.g. "extraction"
    target: str  # the actuator handle, e.g. "extract-source"
    # The candidate under test — an opaque config blob the component's actuator
    # interprets: {"prompt": "..."} for a prompt, {"attack_weight": ..} for a config.
    treatment: dict[str, Any] = Field(default_factory=dict)
    # The concerns this experiment set out to fix — resolved iff it promotes.
    concern_ids: list[str] = Field(default_factory=list)

    status: ExperimentStatus = ExperimentStatus.running
    # Running per-arm score aggregates (mean = sum / n).
    control_n: int = 0
    control_score_sum: float = 0.0
    treatment_n: int = 0
    treatment_score_sum: float = 0.0

    min_sample: int = 20  # per arm before a decision may be made
    margin: float = 0.02  # treatment must beat control by at least this

    rationale: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None

    @property
    def control_score(self) -> float | None:
        return self.control_score_sum / self.control_n if self.control_n else None

    @property
    def treatment_score(self) -> float | None:
        return self.treatment_score_sum / self.treatment_n if self.treatment_n else None

    @property
    def ready(self) -> bool:
        """Both arms have enough samples to decide."""
        return self.control_n >= self.min_sample and self.treatment_n >= self.min_sample

    @property
    def treatment_wins(self) -> bool:
        """Ready AND the candidate beats the live arm by the margin."""
        c, t = self.control_score, self.treatment_score
        return self.ready and c is not None and t is not None and t > c + self.margin
