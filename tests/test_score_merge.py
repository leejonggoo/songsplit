from pathlib import Path

import pretty_midi
import pytest

from songsplit.merge_cli import main as cli_main
from songsplit.stages.guitarpro import GuitarProError, TrackSpec, add_tracks_to_gp, inspect_gp
from songsplit.stages.score_merge import (
    SourceTrack,
    analyze_sources,
    apply_plan,
    classify_role,
    load_gp,
    load_midi,
    load_tracks,
)
from tests.test_guitarpro import BARS, _make_gp

# 마디 하나 = 16칸. 서로 다른 음이 박마다 바뀌는 멜로디 (정렬을 식별할 수 있을 만큼 비주기적)
MELODY = [(60, 0, 4), (62, 4, 8), (64, 8, 10), (67, 10, 12), (65, 12, 16), (69, 16, 20), (71, 24, 28), (60, 32, 36), (72, 40, 48)]


def _gp_with_melody() -> bytes:
    spec = TrackSpec(key="mel", name="Melody", style="piano", notes=MELODY)
    return add_tracks_to_gp(_make_gp(), [spec]).data


def _melody_midi(path: Path, shift_beats: float = 0.0, bpm: float = 100.0) -> Path:
    """MELODY(칸 단위)를 bpm 템포의 MIDI로 쓰되 shift_beats만큼 늦춘다."""
    sec_per_beat = 60.0 / bpm
    midi = pretty_midi.PrettyMIDI(initial_tempo=bpm)
    inst = pretty_midi.Instrument(program=0, name="Lead")
    for pitch, a, b in MELODY:
        inst.notes.append(
            pretty_midi.Note(
                velocity=90,
                pitch=pitch,
                start=(a / 4 + shift_beats) * sec_per_beat,
                end=(b / 4 + shift_beats) * sec_per_beat,
            )
        )
    midi.instruments.append(inst)
    midi.write(str(path))
    return path


def test_midi_positions_are_in_beats_regardless_of_tempo(tmp_path: Path):
    for bpm in (60.0, 120.0):
        midi = pretty_midi.PrettyMIDI(initial_tempo=bpm)
        inst = pretty_midi.Instrument(program=33, name="Bass")
        inst.notes.append(pretty_midi.Note(velocity=90, pitch=40, start=60.0 / bpm * 2, end=60.0 / bpm * 3))
        midi.instruments.append(inst)
        path = tmp_path / "b.mid"
        midi.write(str(path))
        (track,) = load_midi(path.read_bytes(), "b.mid")
        pitch, start, end = track.notes[0]
        assert (pitch, round(start, 2), round(end, 2)) == (40, 2.0, 3.0)


def test_classify_role_from_name_and_program():
    mk = lambda name, program=None, drum=False, hint="": SourceTrack("f", name, drum, [], program, hint)  # noqa: E731
    assert classify_role(mk("x", drum=True)) == "drums"
    assert classify_role(mk("Lead Vocal")) == "vocals"
    assert classify_role(mk("x", program=33)) == "bass"
    assert classify_role(mk("x", program=29)) == "guitar"
    assert classify_role(mk("x", program=0)) == "piano"
    assert classify_role(mk("x", hint="electricBass")) == "bass"
    assert classify_role(mk("x", program=81)) == "other"


def test_load_gp_reads_back_what_was_written_including_ties():
    # 한 마디 끝 2칸(14~16)과 다음 마디 처음 2칸(16~18)을 잇는 음
    gp = add_tracks_to_gp(_make_gp(), [TrackSpec("a", "A", "bass", [(40, 14, 18), (43, 20, 21)])]).data
    tracks = load_gp(gp, "x.gp")
    assert [t.name for t in tracks] == ["A"]
    assert sorted(tracks[0].notes) == [(40, 3.5, 4.5), (43, 5.0, 5.25)]


def test_alignment_recovers_offset_and_ignores_tempo(tmp_path: Path):
    base = _gp_with_melody()
    for shift in (0.0, 2.0, -1.0):
        midi = _melody_midi(tmp_path / "m.mid", shift_beats=shift, bpm=100.0)  # 악보는 80 BPM이어도 박 기준이라 무관
        plan = analyze_sources(base, load_midi(midi.read_bytes(), "m.mid"))
        al = plan.items[0].alignment
        assert al.confident
        assert (al.scale, al.shift_beats) == (1.0, -shift)


def test_alignment_detects_double_time(tmp_path: Path):
    base = _gp_with_melody()
    midi = pretty_midi.PrettyMIDI(initial_tempo=120.0)
    inst = pretty_midi.Instrument(program=0)
    for pitch, a, b in MELODY:  # 절반 길이로 압축된 사본
        inst.notes.append(pretty_midi.Note(velocity=90, pitch=pitch, start=a / 8 * 0.5, end=b / 8 * 0.5))
    midi.instruments.append(inst)
    path = tmp_path / "half.mid"
    midi.write(str(path))
    al = analyze_sources(base, load_midi(path.read_bytes(), "half.mid")).items[0].alignment
    assert al.scale == 2.0 and al.shift_beats == 0.0


def test_duplicate_of_existing_track_is_excluded_by_default(tmp_path: Path):
    base = _gp_with_melody()
    midi = _melody_midi(tmp_path / "dup.mid")
    item = analyze_sources(base, load_midi(midi.read_bytes(), "dup.mid")).items[0]
    assert item.duplicate_of == "Melody"
    assert item.include is False


def test_apply_plan_adds_only_included_tracks_aligned(tmp_path: Path):
    base = _make_gp()
    midi = _melody_midi(tmp_path / "m.mid", shift_beats=1.0)
    other = _melody_midi(tmp_path / "o.mid")
    tracks = load_midi(midi.read_bytes(), "m.mid") + load_midi(other.read_bytes(), "o.mid")
    plan = analyze_sources(base, tracks)
    plan.items[1].include = False
    result = apply_plan(base, plan)
    assert result.added_tracks == ["Lead (m)"]
    assert inspect_gp(result.data).track_names[-1] == "Lead (m)"
    plan.items[0].include = False
    with pytest.raises(GuitarProError):
        apply_plan(base, plan)


def test_unsupported_extension_is_rejected():
    with pytest.raises(GuitarProError, match="지원"):
        load_tracks(b"", "song.gp5")


def test_cli_analyze_and_merge(tmp_path: Path, capsys):
    base_path = tmp_path / "song.gp"
    base_path.write_bytes(_make_gp())
    midi = _melody_midi(tmp_path / "lead.mid", shift_beats=1.0)

    assert cli_main(["--base", str(base_path), str(midi), "--analyze-only"]) == 0
    assert "Lead (lead)" in capsys.readouterr().out

    out = tmp_path / "merged.gp"
    assert cli_main(["--base", str(base_path), str(midi), "-o", str(out)]) == 0
    assert inspect_gp(out.read_bytes()).track_names == [inspect_gp(_make_gp()).track_names[0], "Lead (lead)"]
    assert BARS == inspect_gp(out.read_bytes()).bar_count

    assert cli_main(["--base", str(base_path), str(tmp_path / "missing.mid")]) == 1


# ---- GP 소스: 튜닝·악기·줄/프렛 보존 --------------------------------------------


def _tuning(gp: bytes, index: int) -> str | None:
    from tests.test_guitarpro import _tuning_of, _score

    return _tuning_of(_score(gp), index)


def test_gp_source_track_keeps_tuning_instrument_and_string_fret(tmp_path: Path):
    from tests.test_guitarpro import _notes_info, _retune, _score

    cell = 4 * 0.5  # 칸 단위가 아니라 GP 소스를 직접 만든다
    src = add_tracks_to_gp(
        _make_gp(),
        [
            TrackSpec("a", "Acoustic", "acoustic", [(52, 0, 4), (59, 8, 12)]),
            TrackSpec("b", "Bass", "bass", [(33, 0, 4), (40, 4, 8)]),
        ],
    ).data
    src = _retune(src, "28 33 38 43", "26 33 38 43")  # 바스를 드롭 D로 바꾼 소스
    tracks = load_gp(src, "friend.gp")
    by_name = {t.name: t for t in tracks}
    assert by_name["Acoustic"].style == "acoustic"

    base = _make_gp()
    plan = analyze_sources(base, tracks)
    for item in plan.items:
        item.include = True
        item.alignment.scale, item.alignment.shift_beats = 1.0, 0.0
    result = apply_plan(base, plan)
    root = _score(result.data)
    merged = {t.findtext("Name"): t for t in root.find("Tracks")}

    acoustic = merged["Acoustic (friend)"]
    assert acoustic.findtext("InstrumentSet/Type") == "steelGuitar"
    assert acoustic.findtext("Sounds/Sound/MIDI/Program") == "25"
    bass = merged["Bass (friend)"]
    assert bass.findtext("Staves/Staff/Properties/Property[@name='Tuning']/Pitches") == "26 33 38 43"
    assert bass.findtext("InstrumentSet/Type") == "electricBass"

    src_notes = _notes_info(_score(src))
    new_notes = _notes_info(root)[-len(src_notes):]
    assert [(n["midi"], n["string"], n["fret"]) for n in new_notes] == [
        (n["midi"], n["string"], n["fret"]) for n in src_notes
    ]


def test_gp_source_drum_articulations_are_preserved():
    from tests.test_guitarpro import _score

    src = add_tracks_to_gp(_make_gp(), [TrackSpec("d", "Kit", "drums", [(36, 0, 1), (38, 4, 5), (42, 8, 9)], True)]).data
    (kit,) = [t for t in load_gp(src, "d.gp") if t.is_drum]
    assert set(kit.extras) and all(v["art"] is not None for v in kit.extras.values())
    base = _make_gp()
    plan = analyze_sources(base, [kit])
    plan.items[0].include = True
    root = _score(apply_plan(base, plan).data)
    arts = [n.findtext("InstrumentArticulation") for n in root.find("Notes")][-3:]
    assert arts == [str(kit.extras[k]["art"]) for k in sorted(kit.extras, key=lambda k: k[1])]


def test_other_gp_never_overrides_base_title_tempo_or_settings():
    from tests.test_guitarpro import _score, _retune

    src = _retune(_gp_with_melody(), "Base Song", "Other Song")
    src = _retune(src, "80 2", "150 2")
    base = _make_gp()
    plan = analyze_sources(base, load_gp(src, "other.gp"))
    for item in plan.items:
        item.include = True
    out = apply_plan(base, plan).data
    root = _score(out)
    assert root.findtext("Score/Title") == "Base Song"
    assert root.findtext("MasterTrack/Automations/Automation/Value") == "80 2"


def test_transposed_copy_is_detected_and_moved_back_to_base_key(tmp_path: Path):
    from tests.test_guitarpro import _score, _notes_info

    base = _gp_with_melody()
    midi = pretty_midi.PrettyMIDI(initial_tempo=100.0)
    inst = pretty_midi.Instrument(program=0, name="Lead in other key")
    spb = 60.0 / 100.0
    for pitch, a, b in MELODY:  # 같은 곡을 +6반음 높은 조로 옮겨 쓴 사본, 1박 늦게 시작
        inst.notes.append(pretty_midi.Note(velocity=90, pitch=pitch + 6, start=(a / 4 + 1) * spb, end=(b / 4 + 1) * spb))
    midi.instruments.append(inst)
    path = tmp_path / "other_key.mid"
    midi.write(str(path))

    plan = analyze_sources(base, load_midi(path.read_bytes(), "other_key.mid"))
    al = plan.items[0].alignment
    assert (al.transpose, al.shift_beats, al.confident) in {(-6, -1.0, True), (6, -1.0, True)}
    plan.items[0].include = True  # 원곡 멜로디와 같으니 중복으로 제외되지 않게 한다
    notes = _notes_info(_score(apply_plan(base, plan).data))[-len(MELODY):]
    assert sorted(n["midi"] for n in notes) == sorted(p for p, _, _ in MELODY)


def test_user_can_override_transposition(tmp_path: Path):
    from tests.test_guitarpro import _score, _notes_info

    base = _make_gp()
    midi = _melody_midi(tmp_path / "m.mid")
    plan = analyze_sources(base, load_midi(midi.read_bytes(), "m.mid"))
    plan.items[0].include = True
    plan.items[0].alignment.transpose = 2
    notes = _notes_info(_score(apply_plan(base, plan).data))
    assert sorted(n["midi"] for n in notes) == sorted(p + 2 for p, _, _ in MELODY)


def test_key_estimation_and_base_key_in_plan(tmp_path: Path):
    from songsplit.stages.score_merge import estimate_key

    c_major = [(p, i * 0.5, i * 0.5 + 0.5) for i, p in enumerate([60, 62, 64, 65, 67, 69, 71, 72, 67, 64, 60, 60, 67, 60])]
    assert estimate_key(c_major)[0] == "C major"
    plan = analyze_sources(_make_gp(key=(-3, "Major", "Flats")), load_midi(_melody_midi(tmp_path / "m.mid").read_bytes(), "m.mid"))
    assert plan.base_key == "Eb major" and plan.source_keys["m.mid"].endswith("major")
