"""[3] Analysis 스테이지 오케스트레이션: 템포/키를 추정해 analysis.json에 저장하고
job manifest를 갱신한다. 사용자가 웹 UI에서 값을 수정하면 apply_user_edit()로
덮어써 이후 단계(transcription/postprocess)가 수정된 값을 쓰게 한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.analysis import AnalysisResult, analyze

ANALYSIS_FILENAME = "analysis.json"


def _analysis_path(job: Job) -> Path:
    return job.stage_dir(Stage.ANALYSIS) / ANALYSIS_FILENAME


def _save_and_record(job: Job, result: AnalysisResult) -> None:
    _analysis_path(job).write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    job.update_stage(
        Stage.ANALYSIS,
        StageStatus.DONE,
        outputs={"analysis_path": str(_analysis_path(job)), **result.to_dict()},
    )


def run_analysis(job: Job, wav_path: Path) -> AnalysisResult:
    job.update_stage(Stage.ANALYSIS, StageStatus.RUNNING, params={"source_wav": str(wav_path)})
    try:
        result = analyze(wav_path)
    except Exception as exc:
        job.update_stage(Stage.ANALYSIS, StageStatus.ERROR, error=str(exc))
        raise

    _save_and_record(job, result)
    return result


def apply_user_edit(job: Job, tempo_bpm: float, key: str, scale: str) -> AnalysisResult:
    """사용자가 UI에서 템포/키를 직접 수정했을 때 호출. is_user_edited=True로 저장한다."""
    result = AnalysisResult(
        tempo_bpm=tempo_bpm,
        tempo_confidence=1.0,
        key=key,
        scale=scale,
        key_confidence=1.0,
        is_user_edited=True,
    )
    _save_and_record(job, result)
    return result


def load_analysis(job: Job) -> AnalysisResult | None:
    path = _analysis_path(job)
    if not path.exists():
        return None
    return AnalysisResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
