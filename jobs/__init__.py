"""Job registry + runner for external farm automations."""

from .registry import JOBS, JobDef, get_job, list_jobs
from .runner import RunResult, run_job

__all__ = ["JOBS", "JobDef", "get_job", "list_jobs", "RunResult", "run_job"]
