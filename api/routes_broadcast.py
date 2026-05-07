import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_token
from .chat_runner import run_chat_job
from .jobs import jobs

router = APIRouter(prefix="/broadcast", tags=["broadcast"], dependencies=[Depends(require_token)])


class StartChatJobIn(BaseModel):
    message: str = Field(min_length=1)
    targets: list[str] = Field(min_length=1)
    parallel: int = Field(default=1, ge=1, le=20)


@router.post("/chat/start")
async def start_chat_job(body: StartChatJobIn) -> dict:
    # дедуп + чистка
    seen: list[str] = []
    seen_set: set[str] = set()
    for t in body.targets:
        s = t.strip()
        if s and s not in seen_set:
            seen_set.add(s)
            seen.append(s)
    if not seen:
        raise HTTPException(status_code=400, detail="targets is empty after cleanup")

    job = jobs.create(kind="chat", message=body.message, targets=seen, parallel=body.parallel)
    job.task = asyncio.create_task(run_chat_job(job))
    return job.to_dict()


@router.get("/jobs")
def list_jobs() -> list[dict]:
    return [j.to_dict() for j in jobs.list()]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, include_targets: bool = True, include_log: bool = False) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_dict(include_targets=include_targets, include_log=include_log)


@router.post("/jobs/{job_id}/stop")
async def stop_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("running", "pending"):
        return {"ok": True, "status": job.status}
    job.cancel.set()
    job.status = "stopping"
    return {"ok": True, "status": job.status}


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in ("running", "pending", "stopping"):
        raise HTTPException(status_code=400, detail="stop the job first")
    jobs.delete(job_id)
    return {"ok": True}
