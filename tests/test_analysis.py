from pathlib import Path

import pytest

from songsplit.pipeline.config import AppConfig
from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.analysis import PITCH_CLASSES, analyze
from songsplit.stages.analyze import apply_user_edit, load_analysis, run_analysis

FIXTURE = Path(__file__).parent / "fixtures" / "tone.wav"


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(jobs_dir=tmp_path / "jobs")


def test_analyze_returns_plausible_result():
    result = analyze(FIXTURE)

    assert 20.0 <= result.tempo_bpm <= 300.0
    assert 0.0 <= result.tempo_confidence <= 1.0
    assert result.key in PITCH_CLASSES
    assert result.scale in {"major", "minor"}
    assert 0.0 <= result.key_confidence <= 1.0
    assert result.is_user_edited is False


def test_run_analysis_writes_manifest_and_json(config: AppConfig):
    job = Job.create("tone.wav", config=config)

    result = run_analysis(job, FIXTURE)

    entry = job.get_stage(Stage.ANALYSIS)
    assert entry["status"] == StageStatus.DONE.value
    assert entry["outputs"]["tempo_bpm"] == result.tempo_bpm
    assert entry["outputs"]["key"] == result.key

    loaded = load_analysis(job)
    assert loaded == result


def test_apply_user_edit_overrides_and_marks_edited(config: AppConfig):
    job = Job.create("tone.wav", config=config)
    run_analysis(job, FIXTURE)

    edited = apply_user_edit(job, tempo_bpm=128.0, key="A", scale="minor")

    assert edited.tempo_bpm == 128.0
    assert edited.key == "A"
    assert edited.scale == "minor"
    assert edited.is_user_edited is True

    entry = job.get_stage(Stage.ANALYSIS)
    assert entry["outputs"]["tempo_bpm"] == 128.0
    assert entry["outputs"]["is_user_edited"] is True

    loaded = load_analysis(job)
    assert loaded.tempo_bpm == 128.0
    assert loaded.is_user_edited is True
