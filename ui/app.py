"""SongSplit 웹 UI 진입점.

전체 흐름: 업로드 → Ingestion → 악기 선택 → Separation(Demucs) →
템포/키 분석(+수정) → MIDI 변환(+피아노롤 미리보기) → 후처리(퀀타이즈/스케일 스냅) → 내보내기(zip).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# `streamlit run ui/app.py`로 실행할 때 src 레이아웃 패키지를 찾을 수 있도록 경로 추가
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import plotly.graph_objects as go
import pretty_midi
import streamlit as st

from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.analysis import PITCH_CLASSES
from songsplit.stages.analyze import apply_user_edit, run_analysis
from songsplit.stages.ingest import ingest
from songsplit.stages.separate import separate_stems
from songsplit.stages.separation.demucs_engine import (
    FOUR_STEM_MODEL,
    SIX_STEM_MODEL,
    DemucsEngine,
)
from songsplit.stages.separation.spleeter_engine import SpleeterEngine
from songsplit.stages.export import build_export
from songsplit.stages.postprocess import GRID_OPTIONS, apply_postprocess
from songsplit.stages.transcribe import transcribe_all

st.set_page_config(page_title="SongSplit", layout="wide")
st.title("SongSplit")
st.caption("음원 분리 + MIDI 변환 파이프라인")

if "job_id" not in st.session_state:
    st.session_state.job_id = None


def _stage_entry(manifest: dict, stage: Stage) -> dict | None:
    return manifest["stages"].get(stage.value)


def _piano_roll_figure(midi_path: str) -> go.Figure:
    midi = pretty_midi.PrettyMIDI(midi_path)
    xs: list[float | None] = []
    ys: list[int | None] = []
    for inst in midi.instruments:
        for note in inst.notes:
            xs += [note.start, note.end, None]
            ys += [note.pitch, note.pitch, None]

    fig = go.Figure(go.Scatter(x=xs, y=ys, mode="lines", line=dict(width=6)))
    fig.update_layout(
        height=260,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis_title="시간(초)",
        yaxis_title="MIDI pitch",
    )
    return fig


# ---- 1. 업로드 ---------------------------------------------------------

uploaded = st.file_uploader("오디오 파일 업로드 (mp3/wav/m4a)", type=["mp3", "wav", "m4a"])
if uploaded is not None and st.button("업로드하고 새 작업 시작"):
    job = Job.create(uploaded.name)
    ingest_ok = False
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / uploaded.name
        tmp_path.write_bytes(uploaded.getvalue())
        with st.spinner("업로드한 파일을 표준 형식으로 변환하는 중..."):
            try:
                ingest(job, tmp_path)
                ingest_ok = True
            except Exception as exc:  # noqa: BLE001 - UI에 원인 표시용
                st.error(
                    f"파일을 읽는 데 실패했습니다: {exc}\n\n"
                    "손상된 파일이거나 지원하지 않는 형식일 수 있습니다. "
                    "다른 파일로 다시 시도해 주세요."
                )
    if ingest_ok:
        st.session_state.job_id = job.job_id
        st.rerun()

st.divider()

# ---- 2~3. 현재 작업: 악기 선택 → 분리 결과 ------------------------------

if st.session_state.job_id:
    job = Job.load(st.session_state.job_id)
    manifest = job.read_manifest()
    st.subheader(f"현재 작업: {job.job_id} ({manifest.get('source_filename', '?')})")

    # 이후 섹션들이 어떤 조합으로 보이든(예: 분리 전에 새로고침) 안전하게 참조할 수 있도록
    # 모든 스테이지 엔트리를 미리 한 번에 읽어둔다.
    ingest_entry = _stage_entry(manifest, Stage.INGEST)
    sep_entry = _stage_entry(manifest, Stage.SEPARATION)
    analysis_entry = _stage_entry(manifest, Stage.ANALYSIS)
    midi_entry = _stage_entry(manifest, Stage.MIDI)
    edited_entry = _stage_entry(manifest, Stage.EDITED_MIDI)
    export_entry = _stage_entry(manifest, Stage.EXPORT)

    if ingest_entry and ingest_entry["status"] == StageStatus.DONE.value:
        st.audio(ingest_entry["outputs"]["wav_path"])

        st.markdown("### 악기 선택")
        engine_name = st.radio(
            "분리 엔진",
            options=["demucs", "spleeter"],
            format_func=lambda e: "Demucs (고품질, 기본)" if e == "demucs" else "Spleeter (경량, 대안)",
        )

        if engine_name == "demucs":
            model_label = st.radio(
                "분리 모델",
                options=[FOUR_STEM_MODEL, SIX_STEM_MODEL],
                format_func=lambda m: (
                    "htdemucs — 4종 (보컬 / 드럼 / 베이스 / 그 외)"
                    if m == FOUR_STEM_MODEL
                    else "htdemucs_6s — 6종 (보컬 / 드럼 / 베이스 / 기타 / 피아노 / 그 외)"
                ),
            )
            engine = DemucsEngine(model=model_label)
            st.caption("악기를 하나만 선택하면 demucs의 two-stems 모드로 더 빠르게 처리합니다.")
        else:
            spleeter_config = st.radio(
                "분리 설정",
                options=["2stems", "4stems", "5stems"],
                index=1,
                format_func=lambda c: {
                    "2stems": "2stems — 보컬 / 반주",
                    "4stems": "4stems — 보컬 / 드럼 / 베이스 / 그 외",
                    "5stems": "5stems — 보컬 / 드럼 / 베이스 / 피아노 / 그 외",
                }[c],
            )
            engine = SpleeterEngine(config=spleeter_config)
            st.caption(
                "⚠️ Spleeter는 tensorflow==2.12.1을 요구하는데 Windows용 wheel이 없어 "
                "이 프로젝트의 기본(Windows) 환경에서는 실행이 실패합니다. "
                "Linux/WSL/macOS에서 `pip install -e \".[spleeter]\"`로 설치한 환경에서만 동작합니다."
            )

        stem_options = engine.available_stems()
        selected = st.multiselect("분리할 악기를 선택하세요", stem_options, default=stem_options)

        if st.button("분리 실행", disabled=not selected, type="primary"):
            wav_path = Path(ingest_entry["outputs"]["wav_path"])
            with st.spinner(
                "분리 중입니다... 모델을 처음 사용하는 경우 가중치 다운로드로 몇 분 더 걸릴 수 있습니다."
            ):
                try:
                    separate_stems(job, wav_path, engine, selected)
                except Exception as exc:  # noqa: BLE001 - UI에 원인 표시용
                    st.error(f"분리 실패: {exc}")
            st.rerun()

    if sep_entry:
        st.markdown("### 분리 결과")
        if sep_entry["status"] == StageStatus.DONE.value:
            cols = st.columns(min(3, len(sep_entry["outputs"])) or 1)
            for i, (stem, path) in enumerate(sep_entry["outputs"].items()):
                with cols[i % len(cols)]:
                    st.write(f"**{stem}**")
                    st.audio(path)
        elif sep_entry["status"] == StageStatus.ERROR.value:
            st.error(f"분리 실패: {sep_entry.get('error')}")
        elif sep_entry["status"] == StageStatus.RUNNING.value:
            st.info("분리 진행 중...")

    # ---- 4. 템포/키 분석 -------------------------------------------------

    if ingest_entry and ingest_entry["status"] == StageStatus.DONE.value:
        st.markdown("### 템포/키 분석")

        source_options = {"원곡 (전체 믹스)": ingest_entry["outputs"]["wav_path"]}
        if sep_entry and sep_entry["status"] == StageStatus.DONE.value:
            for stem, path in sep_entry["outputs"].items():
                source_options[f"스템: {stem}"] = path
        source_label = st.selectbox("분석할 오디오", list(source_options.keys()))

        if st.button("템포/키 분석 실행"):
            with st.spinner("템포/키 분석 중..."):
                try:
                    run_analysis(job, Path(source_options[source_label]))
                except Exception as exc:  # noqa: BLE001 - UI에 원인 표시용
                    st.error(f"분석 실패: {exc}")
            st.rerun()

        if analysis_entry and analysis_entry["status"] == StageStatus.DONE.value:
            outputs = analysis_entry["outputs"]
            if outputs.get("is_user_edited"):
                st.caption("사용자가 값을 직접 수정했습니다.")
            col1, col2 = st.columns(2)
            col1.metric("템포", f"{outputs['tempo_bpm']} BPM", f"신뢰도 {outputs['tempo_confidence']:.2f}")
            col2.metric(
                "키",
                f"{outputs['key']} {outputs['scale']}",
                f"신뢰도 {outputs['key_confidence']:.2f}",
            )

            with st.form("edit_analysis_form"):
                st.write("값이 틀렸다면 직접 수정할 수 있습니다.")
                edited_tempo = st.number_input(
                    "BPM", min_value=20.0, max_value=300.0, step=0.5, value=float(outputs["tempo_bpm"])
                )
                edited_key = st.selectbox(
                    "키", PITCH_CLASSES, index=PITCH_CLASSES.index(outputs["key"])
                )
                edited_scale = st.selectbox(
                    "스케일", ["major", "minor"], index=0 if outputs["scale"] == "major" else 1
                )
                if st.form_submit_button("수정값 저장"):
                    apply_user_edit(job, edited_tempo, edited_key, edited_scale)
                    st.rerun()
        elif analysis_entry and analysis_entry["status"] == StageStatus.ERROR.value:
            st.error(f"분석 실패: {analysis_entry.get('error')}")

    # ---- 5. MIDI 변환 -----------------------------------------------------

    if (
        sep_entry
        and sep_entry["status"] == StageStatus.DONE.value
        and analysis_entry
        and analysis_entry["status"] == StageStatus.DONE.value
    ):
        st.markdown("### MIDI 변환")
        tempo_bpm = analysis_entry["outputs"]["tempo_bpm"]
        st.caption(f"현재 확정된 템포({tempo_bpm} BPM)를 기준으로 변환합니다. drums 스템은 onset 기반 분류를 사용합니다.")

        if st.button("MIDI 변환 실행", type="primary"):
            stem_paths = {stem: Path(path) for stem, path in sep_entry["outputs"].items()}
            with st.spinner("MIDI로 변환 중입니다..."):
                try:
                    transcribe_all(job, stem_paths, tempo_bpm=tempo_bpm)
                except Exception as exc:  # noqa: BLE001 - UI에 원인 표시용
                    st.error(f"MIDI 변환 실패: {exc}")
            st.rerun()

        if midi_entry and midi_entry["status"] == StageStatus.DONE.value:
            for stem, path in midi_entry["outputs"].items():
                st.write(f"**{stem}**")
                st.plotly_chart(_piano_roll_figure(path), use_container_width=True, key=f"pianoroll-{stem}")
                st.download_button(
                    f"{stem}.mid 다운로드",
                    data=Path(path).read_bytes(),
                    file_name=f"{stem}.mid",
                    mime="audio/midi",
                    key=f"download-{stem}",
                )
        elif midi_entry and midi_entry["status"] == StageStatus.ERROR.value:
            st.error(f"MIDI 변환 실패: {midi_entry.get('error')}")

    # ---- 6. Post-process (퀀타이즈 / 스케일 스냅) ---------------------------

    if midi_entry and midi_entry["status"] == StageStatus.DONE.value:
        st.markdown("### 후처리 (퀀타이즈 / 스케일 스냅)")
        st.caption("이미 생성된 MIDI를 다시 추론하지 않고 빠르게 재조정합니다.")

        grid = st.selectbox("퀀타이즈 그리드", GRID_OPTIONS, index=GRID_OPTIONS.index("1/16"))
        snap_scale = st.checkbox(
            f"스케일 밖 음을 {analysis_entry['outputs']['key']} {analysis_entry['outputs']['scale']} 스케일로 스냅"
        )

        if st.button("후처리 적용", type="primary"):
            with st.spinner("퀀타이즈 중입니다..."):
                try:
                    apply_postprocess(
                        job,
                        tempo_bpm=analysis_entry["outputs"]["tempo_bpm"],
                        key=analysis_entry["outputs"]["key"],
                        scale=analysis_entry["outputs"]["scale"],
                        grid=grid,
                        snap_scale=snap_scale,
                    )
                except Exception as exc:  # noqa: BLE001 - UI에 원인 표시용
                    st.error(f"후처리 실패: {exc}")
            st.rerun()

        if edited_entry and edited_entry["status"] == StageStatus.DONE.value:
            for stem, path in edited_entry["outputs"].items():
                st.write(f"**{stem}** — 적용 전 / 적용 후")
                col_before, col_after = st.columns(2)
                with col_before:
                    st.plotly_chart(
                        _piano_roll_figure(midi_entry["outputs"][stem]),
                        use_container_width=True,
                        key=f"pianoroll-before-{stem}",
                    )
                with col_after:
                    st.plotly_chart(
                        _piano_roll_figure(path), use_container_width=True, key=f"pianoroll-after-{stem}"
                    )
                st.download_button(
                    f"{stem}.mid 다운로드 (후처리 적용됨)",
                    data=Path(path).read_bytes(),
                    file_name=f"{stem}.mid",
                    mime="audio/midi",
                    key=f"download-edited-{stem}",
                )
        elif edited_entry and edited_entry["status"] == StageStatus.ERROR.value:
            st.error(f"후처리 실패: {edited_entry.get('error')}")

    # ---- 7. 내보내기 -------------------------------------------------------

    if (sep_entry and sep_entry["status"] == StageStatus.DONE.value) or (
        midi_entry and midi_entry["status"] == StageStatus.DONE.value
    ):
        st.markdown("### 내보내기")
        st.caption(
            "분리된 스템(wav)과 MIDI를 zip 하나로 묶고, 모든 스템의 MIDI를 트랙별로 유지한 채 "
            "하나로 합친 merged.mid도 함께 만듭니다. 후처리를 적용한 스템은 후처리 버전이 사용됩니다."
        )

        if st.button("ZIP 만들기", type="primary"):
            with st.spinner("압축하는 중..."):
                try:
                    build_export(job)
                except Exception as exc:  # noqa: BLE001 - UI에 원인 표시용
                    st.error(f"내보내기 실패: {exc}")
            st.rerun()

        if export_entry and export_entry["status"] == StageStatus.DONE.value:
            outputs = export_entry["outputs"]
            st.write(f"포함된 스템: {', '.join(outputs['stems'])}")
            if outputs["used_edited_midi"]:
                st.caption(f"후처리 버전 MIDI 사용: {', '.join(outputs['used_edited_midi'])}")
            col_zip, col_merged = st.columns(2)
            with col_zip:
                st.download_button(
                    "전체 zip 다운로드",
                    data=Path(outputs["zip_path"]).read_bytes(),
                    file_name=f"{job.job_id}.zip",
                    mime="application/zip",
                )
            if outputs.get("merged_midi_path"):
                with col_merged:
                    st.download_button(
                        "합쳐진 MIDI만 다운로드 (merged.mid)",
                        data=Path(outputs["merged_midi_path"]).read_bytes(),
                        file_name="merged.mid",
                        mime="audio/midi",
                    )
        elif export_entry and export_entry["status"] == StageStatus.ERROR.value:
            st.error(f"내보내기 실패: {export_entry.get('error')}")

    if st.button("새 작업 시작하기"):
        st.session_state.job_id = None
        st.rerun()

st.divider()

# ---- 기존 작업 목록 -----------------------------------------------------

st.subheader("기존 작업 목록")
job_ids = Job.list_jobs()
if not job_ids:
    st.write("아직 생성된 job이 없습니다.")
else:
    for job_id in job_ids:
        job = Job.load(job_id)
        manifest = job.read_manifest()
        with st.expander(f"{job_id} — {manifest.get('source_filename', '?')}"):
            st.json(manifest)
            if st.button("이 작업 불러오기", key=f"load-{job_id}"):
                st.session_state.job_id = job_id
                st.rerun()
