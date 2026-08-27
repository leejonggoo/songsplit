"""[2] Separation 스테이지 오케스트레이션: 엔진을 호출해 선택한 악기만 분리하고
결과 경로/파라미터를 job manifest에 기록한다."""

from __future__ import annotations

from pathlib import Path

from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.separation.base import SeparationEngine


def separate_stems(
    job: Job, wav_path: Path, engine: SeparationEngine, targets: list[str]
) -> dict[str, Path]:
    job.update_stage(
        Stage.SEPARATION,
        StageStatus.RUNNING,
        params={"engine": engine.name, "model": getattr(engine, "model", None), "targets": targets},
    )
    try:
        output_dir = job.stage_dir(Stage.SEPARATION)
        stems = engine.separate(wav_path, targets, output_dir)
    except Exception as exc:
        job.update_stage(Stage.SEPARATION, StageStatus.ERROR, error=str(exc))
        raise

    job.update_stage(
        Stage.SEPARATION,
        StageStatus.DONE,
        params={"engine": engine.name, "model": getattr(engine, "model", None), "targets": targets},
        outputs={stem: str(path) for stem, path in stems.items()},
    )
    return stems
