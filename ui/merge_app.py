"""SongSplit 악보 병합 UI.

같은 곡의 MIDI / MuseScore / MusicXML / Guitar Pro 파일을 올리면 트랙을 분석해서(악기 종류, 기준 악보와의
위치 정렬, 중복 여부) 기준 .gp 악보에 새 트랙으로 추가한다. 실행: `streamlit run ui/merge_app.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import streamlit as st

from songsplit.stages.guitarpro import GuitarProError, inspect_gp
from songsplit.stages.lyrics import add_lyrics_to_gp, list_tracks
from songsplit.stages.score_merge import (
    GP_EXTS,
    SUPPORTED_EXTS,
    analyze_sources,
    apply_plan,
    load_tracks,
)

st.set_page_config(page_title="SongSplit 악보 병합", layout="wide")
st.title("악보 병합")
st.caption(
    "같은 곡의 MIDI / MuseScore / MusicXML / Guitar Pro 파일을 올리면 트랙을 분석해 기준 .gp 악보에 추가합니다. "
    "제목·가수·템포·페이지/마디 배치 같은 문서 설정은 모두 기준 .gp 그대로 유지되고, 다른 .gp의 트랙은 튜닝·악기·사운드까지 함께 가져옵니다."
)


@st.cache_data(show_spinner=False)
def _load(data: bytes, filename: str):
    return load_tracks(data, filename)


@st.cache_data(show_spinner=False)
def _info(data: bytes):
    return inspect_gp(data)


def lyrics_panel(gp_bytes: bytes, out_name: str, key: str) -> None:
    """가사를 입력받아 보컬 트랙의 음표에 맞춰 넣는다. gp_bytes는 병합 결과(없으면 기준 악보)."""
    st.markdown("### 가사")
    st.caption(
        "가사를 붙여 넣으면 보컬 트랙의 멜로디에서 가사 줄마다 알맞은 음표 구간을 찾아 음절을 한 음표씩 맞춥니다. "
        "한글은 글자 단위, 영어는 직접 하이픈(gi-ve)을 넣거나 자동 분리를 씁니다. [mm:ss.xx] 시각이 있는 LRC도 됩니다. "
        "Guitar Pro에서는 가사를 넣은 트랙을 선택해야 가사 패널에 보이고, 트랙 목록의 눈 아이콘이 꺼져 있으면 악보에 나오지 않습니다."
    )
    tracks = [t for t in list_tracks(gp_bytes) if not t["is_drum"] and t["notes"]]
    if not tracks:
        st.info("가사를 넣을 수 있는 트랙이 없습니다.")
        return
    default = next((i for i, t in enumerate(tracks) if t["looks_vocal"]), 0)
    col_track, col_mode, col_start = st.columns([2, 2, 1])
    target = col_track.selectbox(
        "가사를 넣을 트랙", tracks, index=default, format_func=lambda t: f"{t['index'] + 1}. {t['name']} (음표 {t['notes']}개)", key=f"lyr-track-{key}"
    )
    mode = col_mode.radio(
        "맞춤 방식",
        ["smart", "sequential"],
        format_func=lambda m: "멜로디에 맞춤 (권장)" if m == "smart" else "첫 음표부터 순서대로",
        horizontal=True,
        key=f"lyr-mode-{key}",
        help="멜로디에 맞춤: 가사 줄마다 음표 수가 비슷한 구간을 찾고 잡음 음표는 건너뜁니다. 순서대로: Guitar Pro 기본 방식.",
    )
    start_bar = col_start.number_input("시작 마디", min_value=1, value=1, step=1, key=f"lyr-start-{key}")
    uploaded = st.file_uploader("가사 파일(.txt/.lrc) — 또는 아래에 직접 입력", type=["txt", "lrc"], key=f"lyr-file-{key}")
    initial = uploaded.getvalue().decode("utf-8", errors="replace") if uploaded else ""
    text = st.text_area("가사", value=initial, height=220, key=f"lyr-text-{key}-{bool(uploaded)}")
    split_english = st.checkbox("영어 단어를 음절로 자동 분리 (간이 규칙, 하이픈을 직접 넣으면 그대로 따름)", value=True, key=f"lyr-eng-{key}")

    if st.button("가사 맞추기", type="primary", disabled=not text.strip(), key=f"lyr-run-{key}"):
        try:
            result = add_lyrics_to_gp(
                gp_bytes,
                target["index"],
                text,
                mode=mode,
                start_bar=int(start_bar) if start_bar > 1 else None,
                split_english=split_english,
            )
        except GuitarProError as exc:
            st.error(str(exc))
            st.session_state.pop(f"lyr-result-{key}", None)
        else:
            st.session_state[f"lyr-result-{key}"] = (out_name, target["name"], result)

    saved = st.session_state.get(f"lyr-result-{key}")
    if saved:
        name, track_name, result = saved
        st.success(f"'{track_name}' 트랙에 음절 {result.placed}개를 배치했습니다 (시작 마디 {result.offset_bar + 1}).")
        for warning in result.warnings:
            st.warning(warning)
        st.dataframe(
            [
                {
                    "가사": r.text,
                    "마디": f"{r.first_bar}~{r.last_bar}" if r.first_bar else "맞춤 실패",
                    "음절": r.syllables,
                    "음표": r.slots,
                }
                for r in result.lines
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.download_button(
            "가사가 들어간 .gp 다운로드",
            data=result.data,
            file_name=name.replace(".gp", "_lyrics.gp"),
            mime="application/octet-stream",
            key=f"lyr-dl-{key}",
        )


uploads = st.file_uploader(
    "같은 곡의 파일들을 모두 올리세요 (.mid, .mscz/.mscx, .musicxml/.mxl, .gp)",
    type=[e.lstrip(".") for e in sorted(SUPPORTED_EXTS)],
    accept_multiple_files=True,
)
files = {u.name: u.getvalue() for u in uploads}
gp_names = [n for n in files if Path(n).suffix.lower() in GP_EXTS]

if not files:
    st.info("파일을 올리면 분석이 시작됩니다.")
    st.stop()
if not gp_names:
    st.error("기준이 될 Guitar Pro(.gp) 파일이 없습니다. .gp 파일을 하나 이상 함께 올려 주세요.")
    st.stop()

base_name = st.selectbox(
    "기준 악보 (여기에 트랙이 추가됩니다)",
    gp_names,
    help="다른 .gp 파일을 같이 올렸다면 그 파일의 트랙은 기준 악보에 추가할 소스로 쓰입니다.",
)
base_bytes = files[base_name]
try:
    info = _info(base_bytes)
except GuitarProError as exc:
    st.error(str(exc))
    st.stop()
st.write(
    f"**{base_name}** — 트랙 {len(info.track_names)}개 ({', '.join(info.track_names)}) · "
    f"{info.bar_count}마디 · 박자 {', '.join(info.time_signatures)} · {info.tempo_bpm:g} BPM"
)

sources = []
for name, data in files.items():
    if name == base_name:
        continue
    try:
        with st.spinner(f"{name} 읽는 중..."):
            tracks = _load(data, name)
    except Exception as exc:  # noqa: BLE001 - UI에 원인 표시용
        st.error(f"{name}: 읽지 못했습니다 — {exc}")
        continue
    if not tracks:
        st.warning(f"{name}: 음표가 있는 트랙이 없습니다")
    sources.extend(tracks)

if not sources:
    st.info("기준 악보에 추가할 트랙이 있는 파일을 함께 올리면 병합할 수 있습니다. 가사만 넣을 수도 있습니다.")
    lyrics_panel(base_bytes, f"{Path(base_name).stem}.gp", f"base-{base_name}-{len(base_bytes)}")
    st.stop()

plan = analyze_sources(base_bytes, sources)
for warning in plan.warnings:
    st.warning(warning)

st.markdown("### 분석 결과")
st.caption(
    "정렬은 기준 악보의 음과 가장 잘 겹치는 박 위치로 자동 추정한 값입니다. 틀렸다면 파일별로 직접 고치세요. "
    "기준 악보에 이미 있는 트랙과 거의 같은 트랙은 기본으로 제외됩니다."
)

by_origin: dict[str, list] = {}
for item in plan.items:
    by_origin.setdefault(item.track.origin, []).append(item)

for origin, items in by_origin.items():
    al = items[0].alignment
    with st.container(border=True):
        st.markdown(f"**{origin}**")
        source_key = plan.source_keys.get(origin)
        st.caption(
            f"기준 악보 조: {plan.base_key}" + (f" · 이 파일에서 추정한 조: {source_key}" if source_key else "")
        )
        col_scale, col_shift, col_transpose, col_score = st.columns(4)
        scale = col_scale.selectbox(
            "배속",
            [1.0, 2.0, 0.5],
            index=[1.0, 2.0, 0.5].index(al.scale),
            format_func=lambda s: {1.0: "1배 (같은 박자)", 2.0: "2배 (원본이 절반 길이로 쓰임)", 0.5: "0.5배"}[s],
            key=f"scale-{origin}",
        )
        shift = col_shift.number_input(
            "오프셋(박)",
            step=0.25,
            value=float(al.shift_beats),
            help="+면 뒤로, -면 앞으로 이동. 1박 = 4분음표",
            key=f"shift-{origin}",
        )
        transpose = col_transpose.number_input(
            "조옮김(반음)",
            step=1,
            min_value=-12,
            max_value=12,
            value=int(al.transpose),
            help="소스가 기준 악보와 다른 조일 때 반음 단위로 옮깁니다. 드럼은 옮기지 않습니다. 자동 감지값이 기본입니다.",
            key=f"transpose-{origin}",
        )
        col_score.metric("자동 정렬 일치도", f"{al.score:.2f}", "신뢰" if al.confident else "낮음", delta_color="normal" if al.confident else "off")
        for i, item in enumerate(items):
            item.alignment.scale = scale
            item.alignment.shift_beats = shift
            item.alignment.transpose = int(transpose)
            t = item.track
            dup = f" · ⚠ '{item.duplicate_of}'와 {item.duplicate_score:.0%} 일치 (중복)" if item.duplicate_of else ""
            item.include = st.checkbox(
                f"{t.name} — {t.role}, 음 {len(t.notes)}개{dup}",
                value=item.include,
                key=f"include-{origin}-{i}-{t.name}",
            )

selected = sum(item.include for item in plan.items)
if st.button(f"선택한 {selected}개 트랙을 기준 악보에 추가", type="primary", disabled=not selected):
    try:
        result = apply_plan(base_bytes, plan)
    except GuitarProError as exc:
        st.error(str(exc))
    else:
        st.session_state["merge_result"] = (f"{Path(base_name).stem}_merged.gp", result, (base_name, len(base_bytes)))

saved_merge = st.session_state.get("merge_result")
if saved_merge and saved_merge[2] == (base_name, len(base_bytes)):
    out_name, result, _ = saved_merge
    st.success(f"추가된 트랙: {', '.join(result.added_tracks)}")
    for key, count in result.notes_written.items():
        dropped = result.notes_dropped[key]
        st.caption(f"{key}: 음 {count}개 배치" + (f", 범위 밖 {dropped}개 제외" if dropped else ""))
    for warning in result.warnings:
        st.warning(warning)
    st.download_button("병합된 .gp 다운로드", data=result.data, file_name=out_name, mime="application/octet-stream")

    lyrics_panel(result.data, out_name, f"merged-{base_name}-{len(base_bytes)}")
else:
    lyrics_panel(base_bytes, f"{Path(base_name).stem}.gp", f"base-{base_name}-{len(base_bytes)}")
