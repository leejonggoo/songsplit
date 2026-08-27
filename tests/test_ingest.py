from pathlib import Path

import pytest

from songsplit.pipeline.config import AppConfig
from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.ingest import ingest

FIXTURE = Path(__file__).parent / "fixtures" / "tone.wav"


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(jobs_dir=tmp_path / "jobs")


def test_ingest_writes_wav_and_updates_manifest(config: AppConfig):
    job = Job.create("tone.wav", config=config)

    output_path = ingest(job, FIXTURE)

    assert output_path.exists()
    entry = job.get_stage(Stage.INGEST)
    assert entry["status"] == StageStatus.DONE.value
    assert entry["outputs"]["sample_rate"] == config.sample_rate
    assert entry["outputs"]["channels"] == 2
    assert entry["outputs"]["duration_sec"] == pytest.approx(1.0, abs=0.05)
