import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pretty_midi
import pytest

from songsplit.pipeline.config import AppConfig
from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.guitarpro import (
    GuitarProError,
    _load_template,
    add_stems_to_gp,
    build_guitarpro,
    inspect_gp,
)

BARS = 4  # 4/4 x 4마디, 80 BPM → 한 마디 = 3초, 16분음표 = 0.1875초


def _make_gp(part_config: bytes | None = None, key: tuple[int, str, str] = (0, "Major", "Sharps")) -> bytes:
    """트랙 1개(비어 있는 피아노), 4/4 4마디짜리 최소 .gp."""
    track = _load_template("piano")
    track.set("id", "0")
    root = ET.Element("GPIF")
    score = ET.SubElement(root, "Score")
    ET.SubElement(score, "Title").text = "Base Song"
    ET.SubElement(score, "Artist").text = "Base Artist"
    master_track = ET.SubElement(root, "MasterTrack")
    ET.SubElement(master_track, "Tracks").text = "0"
    auto = ET.SubElement(ET.SubElement(master_track, "Automations"), "Automation")
    ET.SubElement(auto, "Type").text = "Tempo"
    ET.SubElement(auto, "Value").text = "80 2"
    ET.SubElement(root, "Tracks").append(track)
    masters = ET.SubElement(root, "MasterBars")
    bars = ET.SubElement(root, "Bars")
    voices = ET.SubElement(root, "Voices")
    beats = ET.SubElement(root, "Beats")
    ET.SubElement(root, "Notes")
    rhythms = ET.SubElement(root, "Rhythms")
    ET.SubElement(ET.SubElement(rhythms, "Rhythm", id="0"), "NoteValue").text = "Whole"
    for i in range(BARS):
        master = ET.SubElement(masters, "MasterBar")
        key_el = ET.SubElement(master, "Key")
        ET.SubElement(key_el, "AccidentalCount").text = str(key[0])
        ET.SubElement(key_el, "Mode").text = key[1]
        ET.SubElement(key_el, "TransposeAs").text = key[2]
        ET.SubElement(master, "Time").text = "4/4"
        ET.SubElement(master, "Bars").text = str(i)
        bar = ET.SubElement(bars, "Bar", id=str(i))
        ET.SubElement(bar, "Voices").text = str(i) + " -1 -1 -1"
        ET.SubElement(ET.SubElement(voices, "Voice", id=str(i)), "Beats").text = str(i)
        beat = ET.SubElement(beats, "Beat", id=str(i))
        ET.SubElement(beat, "Rhythm", ref="0")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Content/score.gpif", ET.tostring(root, encoding="utf-8", xml_declaration=True))
        zf.writestr("Content/ScoreViews/0.gpsv", b"full-score-view-layout")
        zf.writestr("Content/Stylesheets/score.gpss", b"page-style")
        if part_config is not None:
            zf.writestr("Content/PartConfiguration", part_config)
    return buf.getvalue()


def _make_midi(path: Path, notes: list[tuple[int, float, float]], is_drum: bool = False) -> Path:
    midi = pretty_midi.PrettyMIDI(initial_tempo=80.0)
    inst = pretty_midi.Instrument(program=0, is_drum=is_drum)
    for pitch, start, end in notes:
        inst.notes.append(pretty_midi.Note(velocity=100, pitch=pitch, start=start, end=end))
    midi.instruments.append(inst)
    midi.write(str(path))
    return path


def _score(gp_bytes: bytes) -> ET.Element:
    return ET.fromstring(zipfile.ZipFile(io.BytesIO(gp_bytes)).read("Content/score.gpif"))


def _read_back(root: ET.Element, track_index: int) -> list[tuple[int, set[int]]]:
    """track_index번째 트랙의 (칸 길이, 음 집합) 목록을 마디 순서대로 복원."""
    sizes = {r.get("id"): {"16th": 1, "Eighth": 2, "Quarter": 4, "Half": 8, "Whole": 16}[r.findtext("NoteValue")]
             for r in root.find("Rhythms")}
    lookup = {tag: {x.get("id"): x for x in root.find(tag)} for tag in ("Bars", "Voices", "Beats", "Notes")}
    out = []
    for master in root.find("MasterBars"):
        bar = lookup["Bars"][master.findtext("Bars").split()[track_index]]
        voice = lookup["Voices"][bar.findtext("Voices").split()[0]]
        for beat_id in voice.findtext("Beats").split():
            beat = lookup["Beats"][beat_id]
            pitches = {
                int(lookup["Notes"][n].find("Properties/Property[@name='Midi']/Number").text)
                for n in beat.findtext("Notes", "").split()
            }
            out.append((sizes[beat.find("Rhythm").get("ref")], pitches))
    return out


def test_inspect_reads_tracks_tempo_and_bars():
    info = inspect_gp(_make_gp())
    assert info.bar_count == BARS
    assert info.tempo_bpm == 80.0
    assert info.time_signatures == ["4/4"]
    assert not info.has_drum_track


def test_rejects_non_gp7_files():
    with pytest.raises(GuitarProError, match=r"\.gp"):
        inspect_gp(b"FICHIER GUITAR PRO v5.00")


def test_adds_track_and_keeps_existing_content(tmp_path: Path):
    original = _make_gp()
    midi = _make_midi(tmp_path / "bass.mid", [(40, 0.0, 0.75)])
    result = add_stems_to_gp(original, {"bass": midi})

    before, after = _score(original), _score(result.data)
    assert len(after.find("Tracks")) == 2
    assert after.find("MasterTrack/Tracks").text == "0 1"
    assert result.added_tracks == ["bass — SongSplit"]
    assert result.notes_written == {"bass": 1}
    # 기존 마디는 그대로, 마디마다 새 Bar가 뒤에 하나씩 붙는다
    for old_master, new_master in zip(before.find("MasterBars"), after.find("MasterBars")):
        assert new_master.findtext("Bars").split()[:1] == old_master.findtext("Bars").split()
        assert len(new_master.findtext("Bars").split()) == 2


def test_document_settings_stay_exactly_as_in_base_gp(tmp_path: Path):
    original = _make_gp()
    midi = _make_midi(tmp_path / "bass.mid", [(40, 0.0, 0.75)])
    result = add_stems_to_gp(original, {"bass": midi}, tempo_bpm=123.0)  # 변환 BPM이 달라도 악보 템포는 그대로
    before, after = zipfile.ZipFile(io.BytesIO(original)), zipfile.ZipFile(io.BytesIO(result.data))

    # score.gpif 말고는 (보기 레이아웃·스타일시트 포함) 모든 파일이 바이트 단위로 같다
    assert before.namelist() == after.namelist()
    for name in before.namelist():
        if name != "Content/score.gpif":
            assert before.read(name) == after.read(name), name

    old_root, new_root = _score(original), _score(result.data)
    assert ET.tostring(old_root.find("Score")) == ET.tostring(new_root.find("Score"))
    assert new_root.findtext("Score/Title") == "Base Song" and new_root.findtext("Score/Artist") == "Base Artist"
    assert new_root.findtext("MasterTrack/Automations/Automation/Value") == "80 2"


def test_notes_land_on_16th_grid_and_round_trip(tmp_path: Path):
    # 80 BPM: 16분음표 = 0.1875초. 칸 0~3(한 박) C4, 칸 8~9 E4+G4 동시
    cell = 0.1875
    midi = _make_midi(
        tmp_path / "piano.mid",
        [(60, 0, 4 * cell), (64, 8 * cell, 10 * cell), (67, 8 * cell, 10 * cell)],
    )
    result = add_stems_to_gp(_make_gp(), {"piano": midi})
    beats = _read_back(_score(result.data), track_index=1)

    first_bar = []
    total = 0
    for size, pitches in beats:
        first_bar.append((size, pitches))
        total += size
        if total == 16:
            break
    assert first_bar == [
        (4, {60}),  # 한 박 C4
        (4, set()),  # 쉼표
        (2, {64, 67}),
        (2, set()),
        (4, set()),
    ]
    # 마디마다 정확히 16칸을 채운다
    assert sum(size for size, _ in beats) == 16 * BARS


def test_long_note_is_tied_across_bar_line(tmp_path: Path):
    cell = 0.1875
    midi = _make_midi(tmp_path / "bass.mid", [(40, 14 * cell, 18 * cell)])  # 1마디 끝 2칸 + 2마디 처음 2칸
    result = add_stems_to_gp(_make_gp(), {"bass": midi})
    root = _score(result.data)
    ties = [(n.find("Tie").get("origin"), n.find("Tie").get("destination")) for n in root.find("Notes") if n.find("Tie") is not None]
    assert ties == [("true", "false"), ("false", "true")]


def test_repeated_same_pitch_is_not_merged(tmp_path: Path):
    cell = 0.1875
    midi = _make_midi(tmp_path / "x.mid", [(60, 0, 2 * cell), (60, 2 * cell, 4 * cell)])
    beats = _read_back(_score(add_stems_to_gp(_make_gp(), {"piano": midi}).data), 1)
    assert beats[:2] == [(2, {60}), (2, {60})]
    root = _score(add_stems_to_gp(_make_gp(), {"piano": midi}).data)
    assert all(n.find("Tie") is None for n in root.find("Notes"))


def test_offset_and_out_of_range_notes(tmp_path: Path):
    cell = 0.1875
    midi = _make_midi(tmp_path / "x.mid", [(60, 1.0, 1.0 + cell), (62, 100.0, 101.0), (64, 0.0, cell)])
    result = add_stems_to_gp(_make_gp(), {"piano": midi}, offset_sec=1.0)
    assert result.notes_written == {"piano": 1}  # 오프셋 1초 이후 시작하는 60만 들어감... 64는 음수 칸
    assert result.notes_dropped == {"piano": 2}
    assert any("벗어난" in w for w in result.warnings)
    beats = _read_back(_score(result.data), 1)
    assert beats[0] == (1, {60})


def test_tempo_override_changes_grid(tmp_path: Path):
    midi = _make_midi(tmp_path / "x.mid", [(60, 0.5, 0.75)])  # 120 BPM: 칸 = 0.125초 → 칸 4
    beats = _read_back(_score(add_stems_to_gp(_make_gp(), {"piano": midi}, tempo_bpm=120.0).data), 1)
    assert beats[0] == (4, set())
    assert beats[1][1] == {60}


def test_drum_track_uses_kit_articulations(tmp_path: Path):
    midi = _make_midi(tmp_path / "drums.mid", [(36, 0.0, 0.1), (38, 0.1875 * 4, 0.1875 * 4 + 0.1), (20, 0.1875 * 8, 0.1875 * 8 + 0.1)], is_drum=True)
    result = add_stems_to_gp(_make_gp(), {"drums": midi})
    root = _score(result.data)
    drum_track = root.find("Tracks")[1]
    assert drum_track.findtext("InstrumentSet/Type") == "drumKit"
    assert any("20" in w for w in result.warnings)
    midi_numbers = [int(n.find("Properties/Property[@name='Midi']/Number").text) for n in root.find("Notes")]
    assert midi_numbers == [36, 38]
    assert all(n.find("Tie") is None for n in root.find("Notes"))


def test_part_configuration_is_left_untouched(tmp_path: Path):
    # 파트 수·보기 수와 개수가 맞물려 있어서 트랙 수만 바꾸면 Guitar Pro가 파일을 열지 못한다
    config = bytes([0, 0, 0, 2, 0, 0, 0, 0, 1, 3, 0, 0, 0, 0, 1, 3, 0, 0, 0, 1])
    midi = _make_midi(tmp_path / "a.mid", [(60, 0, 0.2)])
    out = zipfile.ZipFile(io.BytesIO(add_stems_to_gp(_make_gp(part_config=config), {"a": midi, "b": midi}).data))
    assert out.read("Content/PartConfiguration") == config


def test_build_guitarpro_records_stage(tmp_path: Path):
    config = AppConfig(jobs_dir=tmp_path / "jobs")
    job = Job.create("song.mp3", config)
    midi = _make_midi(tmp_path / "bass.mid", [(40, 0.0, 0.5)])
    job.update_stage(Stage.MIDI, StageStatus.DONE, outputs={"bass": str(midi)})

    out = build_guitarpro(job, _make_gp(), "My Song.gp", ["bass"])
    assert out.exists() and out.name == "My Song_songsplit.gp"
    entry = job.get_stage(Stage.GUITARPRO)
    assert entry["status"] == "done"
    assert entry["outputs"]["added_tracks"] == ["bass — SongSplit"]


def test_build_guitarpro_prefers_edited_midi_and_rejects_missing_stem(tmp_path: Path):
    config = AppConfig(jobs_dir=tmp_path / "jobs")
    job = Job.create("song.mp3", config)
    raw = _make_midi(tmp_path / "raw.mid", [(40, 0.0, 0.5)])
    edited = _make_midi(tmp_path / "edited.mid", [(43, 0.0, 0.5)])
    job.update_stage(Stage.MIDI, StageStatus.DONE, outputs={"bass": str(raw)})
    job.update_stage(Stage.EDITED_MIDI, StageStatus.DONE, outputs={"bass": str(edited)})

    out = build_guitarpro(job, _make_gp(), "s.gp", ["bass"])
    pitches = [int(n.find("Properties/Property[@name='Midi']/Number").text) for n in _score(out.read_bytes()).find("Notes")]
    assert set(pitches) == {43}

    with pytest.raises(GuitarProError, match="vocals"):
        build_guitarpro(job, _make_gp(), "s.gp", ["vocals"])
    assert job.get_stage(Stage.GUITARPRO)["status"] == "error"


# ---- 튜닝 / 줄·프렛 -----------------------------------------------------------


def _notes_info(root: ET.Element) -> list[dict]:
    out = []
    for n in root.find("Notes"):
        props = {p.get("name"): p for p in n.findall("Properties/Property")}
        out.append(
            {
                "midi": int(props["Midi"].findtext("Number")),
                "string": int(props["String"].findtext("String")) if "String" in props else None,
                "fret": int(props["Fret"].findtext("Fret")) if "Fret" in props else None,
                "tie": n.find("Tie") is not None,
            }
        )
    return out


def _track_notes(root: ET.Element, track_index: int) -> list[dict]:
    """한 트랙의 음을 시간 순서대로 (Notes 컬렉션의 순서에 의존하지 않는다)."""
    lookup = {t: {x.get("id"): x for x in root.find(t)} for t in ("Bars", "Voices", "Beats", "Notes")}
    out = []
    for master in root.find("MasterBars"):
        bar = lookup["Bars"][master.findtext("Bars").split()[track_index]]
        for vid in bar.findtext("Voices").split():
            if vid == "-1":
                continue
            for bid in lookup["Voices"][vid].findtext("Beats").split():
                for nid in lookup["Beats"][bid].findtext("Notes", "").split():
                    n = lookup["Notes"][nid]
                    props = {p.get("name"): p for p in n.findall("Properties/Property")}
                    out.append(
                        {
                            "midi": int(props["Midi"].findtext("Number")),
                            "string": int(props["String"].findtext("String")) if "String" in props else None,
                            "fret": int(props["Fret"].findtext("Fret")) if "Fret" in props else None,
                            "tie": n.find("Tie") is not None,
                        }
                    )
    return out


def _tuning_of(root: ET.Element, index: int) -> str | None:
    return root.find("Tracks")[index].findtext("Staves/Staff/Properties/Property[@name='Tuning']/Pitches")


def test_guitar_and_bass_get_standard_tuning_and_string_fret(tmp_path: Path):
    cell = 0.1875
    gtr = _make_midi(tmp_path / "g.mid", [(64, 0, cell * 2), (40, cell * 4, cell * 6), (52, cell * 8, cell * 10)])
    bass = _make_midi(tmp_path / "b.mid", [(28, 0, cell * 2), (43, cell * 4, cell * 6)])
    result = add_stems_to_gp(_make_gp(), {"guitar": gtr, "bass": bass})
    root = _score(result.data)
    assert _tuning_of(root, 1) == "40 45 50 55 59 64"
    assert _tuning_of(root, 2) == "28 33 38 43"
    tunings = {1: [40, 45, 50, 55, 59, 64], 2: [28, 33, 38, 43]}
    notes = _notes_info(root)
    assert len(notes) == 5
    for note, track in zip(notes, [1, 1, 1, 2, 2]):
        assert note["string"] is not None and 0 <= note["fret"] <= 24
        assert tunings[track][note["string"]] + note["fret"] == note["midi"]


def test_chord_uses_distinct_strings_and_tie_keeps_same_string(tmp_path: Path):
    cell = 0.1875
    midi = _make_midi(
        tmp_path / "g.mid",
        [(40, 0, cell * 2), (47, 0, cell * 2), (52, 0, cell * 2), (55, 14 * cell, 18 * cell)],  # 마지막 음은 마디선을 넘는다
    )
    root = _score(add_stems_to_gp(_make_gp(), {"guitar": midi}).data)
    notes = _notes_info(root)
    chord = notes[:3]
    assert len({n["string"] for n in chord}) == 3
    tied = [n for n in notes if n["tie"]]
    assert len(tied) == 2 and tied[0]["string"] == tied[1]["string"] and tied[0]["fret"] == tied[1]["fret"]


def test_out_of_range_notes_are_folded_into_instrument_range(tmp_path: Path):
    midi = _make_midi(tmp_path / "b.mid", [(16, 0, 0.2)])  # 4현 베이스 최저음(28)보다 낮다
    result = add_stems_to_gp(_make_gp(), {"bass": midi})
    (note,) = _notes_info(_score(result.data))
    assert note["midi"] == 28 and note["string"] == 0 and note["fret"] == 0
    assert any("옥타브" in w for w in result.warnings)


def _retune(gp: bytes, old: str, new: str) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(gp))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dest:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "Content/score.gpif":
                data = data.replace(old.encode(), new.encode())
            dest.writestr(info, data)
    return out.getvalue()


def test_new_guitar_follows_tuning_of_guitar_already_in_score(tmp_path: Path):
    cell = 0.1875
    seed = add_stems_to_gp(_make_gp(), {"guitar": _make_midi(tmp_path / "s.mid", [(40, 0, cell)])}).data
    drop_d = _retune(seed, "40 45 50 55 59 64", "38 45 50 55 59 64")
    midi = _make_midi(tmp_path / "g.mid", [(38, 0, cell * 2)])  # 드롭 D 개방현
    root = _score(add_stems_to_gp(drop_d, {"guitar": midi}).data)
    assert _tuning_of(root, 2) == "38 45 50 55 59 64"
    (note,) = _notes_info(root)[-1:]
    assert (note["string"], note["fret"]) == (0, 0)


def _pitch_spelling(root: ET.Element) -> list[tuple[str, str]]:
    out = []
    for n in root.find("Notes"):
        pitch = n.find("Properties/Property[@name='ConcertPitch']/Pitch")
        out.append((pitch.findtext("Step"), pitch.findtext("Accidental") or ""))
    return out


def test_note_spelling_follows_the_key_signature_of_the_base(tmp_path: Path):
    midi = _make_midi(tmp_path / "p.mid", [(70, 0, 0.2), (66, 0.3, 0.5)])  # A#/Bb 와 F#/Gb
    sharp = add_stems_to_gp(_make_gp(key=(2, "Major", "Sharps")), {"piano": midi})  # D장조
    flat = add_stems_to_gp(_make_gp(key=(-1, "Major", "Flats")), {"piano": midi})  # F장조
    assert sorted(_pitch_spelling(_score(sharp.data))) == [("A", "#"), ("F", "#")]
    assert sorted(_pitch_spelling(_score(flat.data))) == [("B", "b"), ("G", "b")]


def test_gp_key_reads_first_bar_key_signature():
    from songsplit.stages.guitarpro import gp_key

    assert gp_key(_make_gp(key=(-3, "Major", "Flats")))[:2] == ("Eb major", 3)
    assert gp_key(_make_gp(key=(1, "Minor", "Sharps")))[:2] == ("E minor", 4)
    assert gp_key(_make_gp())[0] == "C major"


def test_cdata_is_preserved_and_written_like_guitar_pro():
    from songsplit.stages.guitarpro import parse_gpif, serialize_gpif

    raw = (
        '<?xml version="1.0" encoding="utf-8"?>\n<GPIF>\n<Score>\n<Title><![CDATA[A & B <live>]]></Title>\n'
        "<Artist><![CDATA[]]></Artist>\n</Score>\n<Tracks>\n<Track id=\"0\">\n<Name><![CDATA[Vocals]]></Name>\n"
        "<ShortName><![CDATA[voc.]]></ShortName>\n<Lyrics dispatched=\"true\">\n<Line>\n<Text><![CDATA[]]></Text>\n"
        "<Offset>0</Offset>\n</Line>\n</Lyrics>\n</Track>\n</Tracks>\n<MasterBars>\n<MasterBar>\n<Section>\n<Letter>\n"
        "<![CDATA[]]>\n</Letter>\n<Text>\n<![CDATA[Verse 1]]>\n</Text>\n</Section>\n</MasterBar>\n</MasterBars>\n</GPIF>"
    )
    root = parse_gpif(raw.encode())
    assert root.findtext("Score/Title") == "A & B <live>"  # 코드에서는 평범한 문자열로 읽힌다
    out = serialize_gpif(root).decode()
    assert out == raw
    # CDATA가 풀려 있던 파일도 Guitar Pro 방식으로 되돌려 쓴다
    plain = ET.fromstring(raw.replace("<![CDATA[", "").replace("]]>", "").replace("A & B <live>", "A B").encode())
    again = serialize_gpif(plain).decode()
    assert "<Title><![CDATA[A B]]></Title>" in again and "<Name><![CDATA[Vocals]]></Name>" in again
    assert "<Text><![CDATA[]]></Text>" in again and "<Letter>\n<![CDATA[]]>\n</Letter>" in again


def test_new_track_names_are_written_as_cdata(tmp_path: Path):
    midi = _make_midi(tmp_path / "b.mid", [(40, 0.0, 0.5)])
    out = zipfile.ZipFile(io.BytesIO(add_stems_to_gp(_make_gp(), {"bass": midi}).data)).read("Content/score.gpif").decode()
    assert "<Name><![CDATA[bass — SongSplit]]></Name>" in out


def test_non_fretted_tracks_get_a_wide_tuning_so_every_note_has_string_and_fret(tmp_path: Path):
    # 건반/보컬 트랙: 음을 옥타브 옮기지 않고 튜닝을 음역(+조옮김 여유)에 맞춰 넓힌다.
    # 줄·프렛이 없는 음은 Guitar Pro가 프렛을 -2147483648로 채워 오디오 엔진을 죽이기 때문이다
    cell = 0.1875
    midi = _make_midi(tmp_path / "p.mid", [(60, 0, cell * 2), (100, cell * 4, cell * 6), (30, cell * 8, cell * 10)])
    result = add_stems_to_gp(_make_gp(), {"piano": midi})
    root = _score(result.data)
    tuning = [int(x) for x in _tuning_of(root, 1).split()]
    assert tuning != [40, 45, 50, 55, 59, 64]
    assert min(tuning) <= 30 - 12 and max(tuning) + 24 >= 100 + 12  # 조옮김 ±12 여유
    notes = _notes_info(root)
    assert sorted(n["midi"] for n in notes) == [30, 60, 100]  # 옥타브 이동 없음
    for n in notes:
        assert n["string"] is not None and 0 <= n["fret"] <= 24
        assert tuning[n["string"]] + n["fret"] == n["midi"]


def test_transposing_a_non_fretted_track_by_an_octave_keeps_every_note_playable(tmp_path: Path):
    cell = 0.1875
    midi = _make_midi(tmp_path / "p.mid", [(38, 0, cell * 2), (64, cell * 4, cell * 6), (88, cell * 8, cell * 10)])
    root = _score(add_stems_to_gp(_make_gp(), {"piano": midi}).data)
    tuning = [int(x) for x in _tuning_of(root, 1).split()]
    for shift in (-12, -6, 6, 12):
        for n in _notes_info(root):
            assert any(0 <= n["midi"] + shift - open_pitch <= 24 for open_pitch in tuning), (shift, n["midi"])


def _break_track(gp: bytes, track_index: int) -> bytes:
    """예전 버전이 만들던 문제 상태를 재현: 기타 기본 튜닝 + 범위 밖 음은 줄·프렛 없음, 일부는 프렛 -2147483648."""
    from songsplit.stages.guitarpro import parse_gpif, serialize_gpif

    src = zipfile.ZipFile(io.BytesIO(gp))
    root = parse_gpif(src.read("Content/score.gpif"))
    track = root.find("Tracks")[track_index]
    track.find("Staves/Staff/Properties/Property[@name='Tuning']/Pitches").text = "40 45 50 55 59 64"
    notes = {n.get("id"): n for n in root.find("Notes")}
    lookup = {t: {x.get("id"): x for x in root.find(t)} for t in ("Bars", "Voices", "Beats")}
    for master in root.find("MasterBars"):
        bar = lookup["Bars"][master.findtext("Bars").split()[track_index]]
        for vid in bar.findtext("Voices").split():
            if vid == "-1":
                continue
            for bid in lookup["Voices"][vid].findtext("Beats").split():
                for nid in lookup["Beats"][bid].findtext("Notes", "").split():
                    note = notes[nid]
                    midi = int(note.find("Properties/Property[@name='Midi']/Number").text)
                    for prop in list(note.find("Properties")):
                        if prop.get("name") in ("String", "Fret"):
                            note.find("Properties").remove(prop)
                    if midi < 40:
                        for name, tag, val in (("String", "String", "0"), ("Fret", "Fret", "-2147483648")):
                            ET.SubElement(ET.SubElement(note.find("Properties"), "Property", name=name), tag).text = val
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dest:
        for info in src.infolist():
            data = src.read(info.filename)
            dest.writestr(info, serialize_gpif(root) if info.filename == "Content/score.gpif" else data)
    return out.getvalue()


def test_repair_playable_fixes_missing_and_invalid_string_fret_without_touching_other_tracks(tmp_path: Path):
    from songsplit.stages.guitarpro import repair_playable

    cell = 0.1875
    piano = _make_midi(tmp_path / "p.mid", [(38, 0, cell * 2), (64, cell * 4, cell * 6), (67, cell * 4, cell * 6), (88, cell * 8, cell * 10)])
    guitar = _make_midi(tmp_path / "g.mid", [(52, 0, cell * 2), (40, cell * 4, cell * 6)])
    gp = add_stems_to_gp(_make_gp(), {"piano": piano, "guitar": guitar}).data
    broken = _break_track(gp, 1)  # 1번 트랙 = 피아노
    assert any(n["string"] is None or n["fret"] == -2147483648 for n in _track_notes(_score(broken), 1))

    fixed, report = repair_playable(broken)
    root = _score(fixed)
    assert len(report) == 1 and "1번" not in report[0] or "2번" in report[0]  # 피아노 트랙만 보고
    tuning = [int(x) for x in _tuning_of(root, 1).split()]
    piano_notes = _track_notes(root, 1)
    assert len(piano_notes) == 4
    for n in piano_notes:
        assert n["string"] is not None and n["fret"] != -2147483648 and 0 <= n["fret"] <= 24
        assert tuning[n["string"]] + n["fret"] == n["midi"]
    assert sorted(n["midi"] for n in piano_notes) == [38, 64, 67, 88]  # 음높이는 그대로
    # 같은 화음의 두 음은 서로 다른 줄
    chord = [n for n in piano_notes if n["midi"] in (64, 67)]
    assert chord[0]["string"] != chord[1]["string"]
    # 기타 트랙은 그대로
    assert _track_notes(_score(gp), 2) == _track_notes(root, 2)
    assert _tuning_of(_score(gp), 2) == _tuning_of(root, 2)


def test_repair_keeps_tied_notes_on_the_same_string(tmp_path: Path):
    from songsplit.stages.guitarpro import repair_playable

    cell = 0.1875
    piano = _make_midi(tmp_path / "p.mid", [(50, 14 * cell, 18 * cell)])  # 마디선을 넘는 붙임줄
    fixed, _ = repair_playable(_break_track(add_stems_to_gp(_make_gp(), {"piano": piano}).data, 1))
    tied = [n for n in _track_notes(_score(fixed), 1) if n["tie"]]
    assert len(tied) == 2 and (tied[0]["string"], tied[0]["fret"]) == (tied[1]["string"], tied[1]["fret"])


def test_repair_does_not_corrupt_tracks_that_share_note_elements(tmp_path: Path):
    """Guitar Pro는 똑같은 음이 여러 트랙에 있으면 Note 요소를 공유해 저장한다. 한 트랙을 고쳐도 다른 트랙이 틀어지면 안 된다."""
    from songsplit.stages.guitarpro import parse_gpif, repair_playable, serialize_gpif

    cell = 0.1875
    low = _make_midi(tmp_path / "a.mid", [(38, 0, cell * 2), (60, cell * 4, cell * 6)])
    high = _make_midi(tmp_path / "b.mid", [(38, 0, cell * 2), (60, cell * 4, cell * 6), (96, cell * 8, cell * 10)])
    gp = add_stems_to_gp(_make_gp(), {"piano": low, "other": high}).data
    src = zipfile.ZipFile(io.BytesIO(gp))
    root = parse_gpif(src.read("Content/score.gpif"))
    # 2번 트랙(other)이 1번 트랙(piano)의 마디를 그대로 가리키게 해서 음 요소를 공유시킨다 (두 번째 트랙도 같은 음만 갖는 경우)
    for master in root.find("MasterBars"):
        ids = master.findtext("Bars").split()
        ids[2] = ids[1]
        master.find("Bars").text = " ".join(ids)
    shared = io.BytesIO()
    with zipfile.ZipFile(shared, "w") as dest:
        for info in src.infolist():
            dest.writestr(info, serialize_gpif(root) if info.filename == "Content/score.gpif" else src.read(info.filename))
    broken = _break_track(shared.getvalue(), 1)

    fixed, report = repair_playable(broken)
    fixed_root = _score(fixed)
    assert len(report) == 2
    for track in (1, 2):
        tuning = [int(x) for x in _tuning_of(fixed_root, track).split()]
        notes = _track_notes(fixed_root, track)
        assert [n["midi"] for n in notes] == [38, 60]
        for n in notes:
            assert n["string"] is not None and n["fret"] != -2147483648
            assert tuning[n["string"]] + n["fret"] == n["midi"], (track, n)
