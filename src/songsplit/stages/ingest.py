"""[1] Ingestion: 입력 파일을 표준 wav로 통일하고 메타데이터를 manifest에 기록한다."""

from __future__ import annotations

from pathlib import Path

from songsplit.core.audio_io import to_wav
from songsplit.pipeline.job import Job, Stage, StageStatus

INGESTED_FILENAME = "source.wav"


def ingest(job: Job, input_path: Path) -> Path:
    """input_path를 job의 00_input 폴더에 표준 wav로 저장하고 manifest를 갱신한다."""
    job.update_stage(Stage.INGEST, StageStatus.RUNNING)
    try:
        output_path = job.stage_dir(Stage.INGEST) / INGESTED_FILENAME
        meta = to_wav(input_path, output_path)
    except Exception as exc:
        job.update_stage(Stage.INGEST, StageStatus.ERROR, error=str(exc))
        raise

    job.update_stage(
        Stage.INGEST,
        StageStatus.DONE,
        params={"source_filename": input_path.name},
        outputs={
            "wav_path": str(output_path),
            "duration_sec": meta.duration_sec,
            "sample_rate": meta.sample_rate,
            "channels": meta.channels,
        },
    )
    return output_path
