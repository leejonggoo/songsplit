"""Job 단위 실행 관리 + manifest.json 읽기/쓰기.

각 처리 요청은 jobs/<job_id>/ 폴더 하나로 표현된다. 파이프라인 단계는
Job.stage_dir(stage)가 반환하는 폴더에 산출물을 쓰고, Job.update_stage()로
진행 상태와 파라미터를 manifest.json에 기록한다. UI는 manifest만 읽어서
이미 끝난 단계는 재실행 없이 결과를 표시한다.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from songsplit.pipeline.config import AppConfig, DEFAULT_CONFIG

MANIFEST_FILENAME = "manifest.json"


class Stage(StrEnum):
    INGEST = "00_input"
    SEPARATION = "01_separation"
    ANALYSIS = "02_analysis"
    MIDI = "03_midi"
    EDITED_MIDI = "04_edited_midi"
    EXPORT = "05_export"
    GUITARPRO = "06_guitarpro"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_job_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


@dataclass
class Job:
    job_id: str
    base_dir: Path
    config: AppConfig = field(default_factory=lambda: DEFAULT_CONFIG, repr=False)

    # ---- lifecycle -----------------------------------------------------

    @classmethod
    def create(cls, source_filename: str, config: AppConfig = DEFAULT_CONFIG) -> "Job":
        config.ensure_dirs()
        job_id = _new_job_id()
        base_dir = config.jobs_dir / job_id
        base_dir.mkdir(parents=True, exist_ok=False)
        for stage in Stage:
            (base_dir / stage.value).mkdir(parents=True, exist_ok=True)

        job = cls(job_id=job_id, base_dir=base_dir, config=config)
        job._write_manifest(
            {
                "job_id": job_id,
                "created_at": _now_iso(),
                "source_filename": source_filename,
                "stages": {},
            }
        )
        return job

    @classmethod
    def load(cls, job_id: str, config: AppConfig = DEFAULT_CONFIG) -> "Job":
        base_dir = config.jobs_dir / job_id
        if not (base_dir / MANIFEST_FILENAME).exists():
            raise FileNotFoundError(f"job not found: {job_id}")
        return cls(job_id=job_id, base_dir=base_dir, config=config)

    @classmethod
    def list_jobs(cls, config: AppConfig = DEFAULT_CONFIG) -> list[str]:
        config.ensure_dirs()
        return sorted(
            (p.name for p in config.jobs_dir.iterdir() if (p / MANIFEST_FILENAME).exists()),
            reverse=True,
        )

    # ---- paths -----------------------------------------------------------

    def stage_dir(self, stage: Stage) -> Path:
        path = self.base_dir / stage.value
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def manifest_path(self) -> Path:
        return self.base_dir / MANIFEST_FILENAME

    # ---- manifest read/write ---------------------------------------------

    def read_manifest(self) -> dict[str, Any]:
        with self.manifest_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _write_manifest(self, data: dict[str, Any]) -> None:
        # 원자적 쓰기: 임시 파일에 쓴 뒤 교체 (중간에 중단되어도 manifest 손상 방지)
        fd, tmp_path = tempfile.mkstemp(dir=self.base_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.manifest_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def get_stage(self, stage: Stage) -> dict[str, Any] | None:
        return self.read_manifest()["stages"].get(stage.value)

    def update_stage(
        self,
        stage: Stage,
        status: StageStatus,
        params: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        manifest = self.read_manifest()
        entry = manifest["stages"].setdefault(stage.value, {})
        entry["status"] = status.value
        entry["updated_at"] = _now_iso()
        if params is not None:
            entry["params"] = params
        if outputs is not None:
            entry["outputs"] = outputs
        entry["error"] = error
        self._write_manifest(manifest)

    def is_stage_done(self, stage: Stage) -> bool:
        entry = self.get_stage(stage)
        return bool(entry and entry.get("status") == StageStatus.DONE.value)
