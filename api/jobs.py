"""In-memory job-manager для рассылки. Без персистентности — задачи живут пока живёт процесс."""
import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class TargetState:
    target: str
    status: str = "pending"   # pending | sent | skipped | failed
    error: str | None = None
    used_account: str | None = None
    processed_at: datetime | None = None


@dataclass
class Job:
    id: str
    kind: str
    message: str
    parallel: int
    targets: list[TargetState]
    status: str = "pending"   # pending | running | stopping | done | stopped | failed
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    log: deque = field(default_factory=lambda: deque(maxlen=500))
    error: str | None = None
    continuous: bool = False    # крутиться вечно, пока не остановят
    current_round: int = 0      # номер текущего раунда (только для continuous)

    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    def add_log(self, **fields: Any) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), **fields}
        self.log.append(entry)

    @property
    def total(self) -> int:
        return len(self.targets)

    def to_dict(self, include_targets: bool = False, include_log: bool = False) -> dict:
        d = {
            "id": self.id,
            "kind": self.kind,
            "message": self.message,
            "parallel": self.parallel,
            "status": self.status,
            "sent": self.sent,
            "failed": self.failed,
            "skipped": self.skipped,
            "total": self.total,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error,
            "continuous": self.continuous,
            "current_round": self.current_round,
        }
        if include_targets:
            d["targets"] = [
                {
                    "target": t.target,
                    "status": t.status,
                    "error": t.error,
                    "used_account": t.used_account,
                    "processed_at": t.processed_at.isoformat() if t.processed_at else None,
                }
                for t in self.targets
            ]
        if include_log:
            d["log"] = list(self.log)
        return d


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def create(self, *, kind: str, message: str, targets: list[str], parallel: int) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            message=message,
            parallel=parallel,
            targets=[TargetState(target=t) for t in targets],
        )
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def delete(self, job_id: str) -> bool:
        return self._jobs.pop(job_id, None) is not None


jobs = JobManager()
