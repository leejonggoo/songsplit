from pathlib import Path

import pytest

from songsplit.pipeline.config import AppConfig
from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.separate import separate_stems
from songsplit.stages.separation.base import SeparationEngine
from songsplit.stages.separation.demucs_engine import (
    FOUR_STEM_MODEL,
    FOUR_STEMS,
    SIX_STEM_MODEL,
    SIX_STEMS,
    DemucsEngine,
)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(jobs_dir=tmp_path / "jobs")


def test_demucs_engine_available_stems_by_model():
    assert DemucsEngine(model=FOUR_STEM_MODEL).available_stems() == FOUR_STEMS
    assert DemucsEngine(model=SIX_STEM_MODEL).available_stems() == SIX_STEMS


def test_demucs_engine_rejects_unsupported_stem(tmp_path: Path):
    engine = DemucsEngine(model=FOUR_STEM_MODEL)
    with pytest.raises(ValueError, match="guitar"):
        engine.separate(tmp_path / "in.wav", ["guitar"], tmp_path / "out")


class DummyEngine(SeparationEngine):
    """실제 demucs 없이 오케스트레이션(separate_stems)만 검증하기 위한 가짜 엔진."""

    name = "dummy"

    def __init__(self, fail: bool = False):
        self.fail = fail

    def available_stems(self) -> list[str]:
        return ["vocals", "drums"]

    def separate(self, wav_path: Path, targets: list[str], output_dir: Path) -> dict[str, Path]:
        if self.fail:
            raise RuntimeError("boom")
        output_dir.mkdir(parents=True, exist_ok=True)
        produced = {}
        for stem in targets:
            p = output_dir / f"{stem}.wav"
            p.write_bytes(b"fake-wav")
            produced[stem] = p
        return produced


def test_separate_stems_updates_manifest_on_success(config: AppConfig, tmp_path: Path):
    job = Job.create("song.mp3", config=config)
    wav_path = tmp_path / "source.wav"
    wav_path.write_bytes(b"fake")

    result = separate_stems(job, wav_path, DummyEngine(), ["vocals", "drums"])

    assert set(result) == {"vocals", "drums"}
    entry = job.get_stage(Stage.SEPARATION)
    assert entry["status"] == StageStatus.DONE.value
    assert entry["params"]["engine"] == "dummy"
    assert entry["params"]["targets"] == ["vocals", "drums"]
    assert Path(entry["outputs"]["vocals"]).exists()


def test_separate_stems_records_error(config: AppConfig, tmp_path: Path):
    job = Job.create("song.mp3", config=config)
    wav_path = tmp_path / "source.wav"
    wav_path.write_bytes(b"fake")

    with pytest.raises(RuntimeError, match="boom"):
        separate_stems(job, wav_path, DummyEngine(fail=True), ["vocals"])

    entry = job.get_stage(Stage.SEPARATION)
    assert entry["status"] == StageStatus.ERROR.value
    assert entry["error"] == "boom"
