from pathlib import Path

import pytest

from songsplit.pipeline.config import AppConfig
from songsplit.pipeline.job import Job, Stage, StageStatus


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(jobs_dir=tmp_path / "jobs")


def test_create_and_load_job(config: AppConfig):
    job = Job.create("song.mp3", config=config)

    assert job.base_dir.exists()
    for stage in Stage:
        assert (job.base_dir / stage.value).exists()

    manifest = job.read_manifest()
    assert manifest["source_filename"] == "song.mp3"
    assert manifest["stages"] == {}

    loaded = Job.load(job.job_id, config=config)
    assert loaded.read_manifest() == manifest


def test_list_jobs(config: AppConfig):
    a = Job.create("a.mp3", config=config)
    b = Job.create("b.mp3", config=config)

    job_ids = Job.list_jobs(config=config)
    assert set(job_ids) == {a.job_id, b.job_id}


def test_update_stage_roundtrip(config: AppConfig):
    job = Job.create("song.mp3", config=config)

    job.update_stage(Stage.INGEST, StageStatus.RUNNING)
    entry = job.get_stage(Stage.INGEST)
    assert entry["status"] == StageStatus.RUNNING.value
    assert not job.is_stage_done(Stage.INGEST)

    job.update_stage(
        Stage.INGEST,
        StageStatus.DONE,
        params={"source_filename": "song.mp3"},
        outputs={"wav_path": "00_input/source.wav"},
    )
    entry = job.get_stage(Stage.INGEST)
    assert entry["status"] == StageStatus.DONE.value
    assert entry["params"]["source_filename"] == "song.mp3"
    assert entry["outputs"]["wav_path"] == "00_input/source.wav"
    assert job.is_stage_done(Stage.INGEST)


def test_update_stage_error(config: AppConfig):
    job = Job.create("song.mp3", config=config)

    job.update_stage(Stage.INGEST, StageStatus.ERROR, error="boom")
    entry = job.get_stage(Stage.INGEST)
    assert entry["status"] == StageStatus.ERROR.value
    assert entry["error"] == "boom"
