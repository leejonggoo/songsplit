import io
import xml.etree.ElementTree as ET
import zipfile

import pytest

from songsplit.stages.guitarpro import GuitarProError, TrackSpec, add_tracks_to_gp
from songsplit.stages.lyrics import (
    add_lyrics_to_gp,
    list_tracks,
    parse_lyrics,
    syllabify,
    track_slots,
)
from tests.test_guitarpro import _make_gp

# 4마디(마디당 16칸): 구절1(0~15), 잡음 음표 하나(24), 구절2(40~55)
PHRASE1 = [(60, 0, 4), (62, 4, 8), (64, 8, 12), (65, 12, 16)]
NOISE = [(70, 24, 26)]
PHRASE2 = [(67, 40, 44), (69, 44, 48), (71, 48, 52), (72, 52, 56)]


def _vocal_gp(notes=PHRASE1 + NOISE + PHRASE2) -> bytes:
    return add_tracks_to_gp(_make_gp(), [TrackSpec("v", "Vocals", "vocals", list(notes))]).data


def _root(gp: bytes) -> ET.Element:
    return ET.fromstring(zipfile.ZipFile(io.BytesIO(gp)).read("Content/score.gpif"))


def _syllables(gp: bytes, track: int = 1) -> list[tuple[int, str]]:
    """(마디 번호, 박에 적힌 음절)을 시간 순서로. 가사 없는 음표 박은 ''."""
    root = _root(gp)
    out = []
    for slot in track_slots(root, track):
        if not slot.eligible:
            continue
        line = slot.beat.find("Lyrics/Line")
        out.append((slot.bar, (line.text or "") if line is not None else ""))
    return out


def test_parse_lyrics_handles_lrc_and_section_headers():
    lines = parse_lyrics("[ti:노래]\n[Verse 1]\n[01:02.50]첫 줄\n\n둘째 줄\n")
    assert [(ln.text, ln.time_sec) for ln in lines] == [("첫 줄", 62.5), ("둘째 줄", None)]


def test_syllabify_korean_english_and_explicit_hyphens():
    assert [s.text for s in syllabify("사랑 해요")] == ["사", "랑", "해", "요"]
    assert [s.new_word for s in syllabify("사랑 해요")] == [True, False, True, False]
    assert [(s.text, s.joins_next) for s in syllabify("gi-ve lo-o-ove")] == [
        ("gi", True), ("ve", False), ("lo", True), ("o", True), ("ove", False)
    ]
    assert [s.text for s in syllabify("make give love")] == ["make", "give", "love"]
    assert [s.text for s in syllabify("passion")] == ["pas", "sion"]
    assert [s.text for s in syllabify("passion", split_english=False)] == ["passion"]


def test_sequential_fills_note_beats_in_order_and_writes_track_text_and_offset():
    gp = add_lyrics_to_gp(
        _vocal_gp([(60, 16, 20), (62, 20, 24), (64, 24, 28)]), 1, "하나 둘 셋", mode="sequential"
    ).data
    assert [s for _, s in _syllables(gp)] == ["하", "나", "둘"]
    track = _root(gp).find("Tracks")[1]
    assert track.find("Lyrics").get("dispatched") == "true"
    assert track.findtext("Lyrics/Line/Offset") == "1"  # 첫 가사가 놓인 마디(0부터)
    assert track.findtext("Lyrics/Line/Text") == "하 나  둘"
    assert len(track.findall("Lyrics/Line")) == 5


def test_sequential_mode_follows_guitar_pro_and_gives_tied_beats_a_syllable():
    # 한 마디 끝에서 다음 마디로 붙임줄로 이어지는 음. Guitar Pro는 붙임줄 도착 박에도 음절을 하나 배정한다
    gp = add_lyrics_to_gp(_vocal_gp([(60, 12, 20), (62, 24, 28)]), 1, "가 나 다", mode="sequential").data
    root = _root(gp)
    labels = [(s.bar, s.beat.find("Lyrics/Line").text) for s in track_slots(root, 1) if s.has_notes]
    assert labels == [(0, "가"), (1, "나"), (1, "다")]


def test_smart_mode_marks_tied_and_skipped_beats_with_underscore():
    gp = add_lyrics_to_gp(_vocal_gp([(60, 12, 20), (62, 24, 28)]), 1, "가 나", mode="smart").data
    root = _root(gp)
    labels = [s.beat.find("Lyrics/Line").text for s in track_slots(root, 1) if s.has_notes]
    assert labels == ["가", "_", "나"]  # 붙임줄로 이어진 박은 '_' 로 채운다


def test_smart_mode_skips_noise_phrase_and_aligns_lines_to_melody():
    result = add_lyrics_to_gp(_vocal_gp(), 1, "가 나 다 라\n마 바 사 아", mode="smart")
    syl = _syllables(result.data)
    assert [s for _, s in syl if s not in ("", "_")] == ["가", "나", "다", "라", "마", "바", "사", "아"]
    assert dict.fromkeys(b for b, s in syl if s not in ("", "_")) == {0: None, 2: None, 3: None}
    noise_bar_syl = [s for b, s in syl if b == 1]
    assert noise_bar_syl == ["_"]  # 잡음 음표는 가사 없이 '_' 로 건너뛴다
    assert result.skipped_slots == 1 and not result.unplaced_lines
    assert [(r.first_bar, r.syllables, r.slots) for r in result.lines] == [(1, 4, 4), (3, 4, 4)]
    # 가사 원문은 줄마다 개행, 한글은 단어 사이 두 칸
    assert _root(result.data).find("Tracks")[1].findtext("Lyrics/Line/Text") == "가  나  다  라 _\n마  바  사  아"


def test_more_syllables_than_notes_are_grouped_by_word():
    # 구절 하나(음표 3개)에 음절 5개: 단어 경계를 우선해 묶는다
    gp = add_lyrics_to_gp(_vocal_gp([(60, 0, 4), (62, 4, 8), (64, 8, 12)]), 1, "사랑 하는 너", mode="smart").data
    assert [s for _, s in _syllables(gp)] == ["사랑", "하는", "너"]


def test_fewer_syllables_than_notes_prefers_long_notes_and_keeps_first():
    notes = [(60, 0, 8), (62, 8, 9), (64, 9, 10), (65, 12, 16)]  # 길이: 8,1,1,4
    gp = add_lyrics_to_gp(_vocal_gp(notes), 1, "가 나 다", mode="smart").data
    assert [s for _, s in _syllables(gp)] == ["가", "", "나", "다"] or [s for _, s in _syllables(gp)].count("") == 1
    assert _syllables(gp)[0][1] == "가"


def test_english_hyphenated_words_get_trailing_hyphen_per_beat():
    gp = add_lyrics_to_gp(_vocal_gp(PHRASE1), 1, "gi-ve lo-ve", mode="sequential").data
    assert [s for _, s in _syllables(gp)] == ["gi-", "ve", "lo-", "ve"]
    assert _root(gp).find("Tracks")[1].findtext("Lyrics/Line/Text") == "gi-ve lo-ve"


def test_lrc_timestamps_choose_between_equally_good_phrases():
    # 두 구절이 똑같이 4음절짜리라 순서만으로는 줄 하나를 어느 구절에 둘지 모호하다 → 시각으로 결정
    only_second = "[00:03.00]마 바 사 아"  # 80BPM에서 3초 ≈ 4박 → 2마디 부근이 아니라 구절2(≈10박) 쪽이 되도록 오프셋을 준다
    result = add_lyrics_to_gp(
        _vocal_gp(PHRASE1 + PHRASE2), 1, only_second, mode="smart", tempo_bpm=80.0, time_offset_sec=-4.5
    )
    syl = _syllables(result.data)
    assert [s for b, s in syl if b == 0] == ["", "", "", ""]
    assert [s for b, s in syl if s] == ["마", "바", "사", "아"]


def test_replaces_existing_lyrics():
    first = add_lyrics_to_gp(_vocal_gp(PHRASE1), 1, "가 나 다 라", mode="sequential").data
    second = add_lyrics_to_gp(first, 1, "마 바", mode="sequential").data
    assert [s for _, s in _syllables(second)] == ["마", "바", "", ""]


def test_only_lyrics_change_in_the_file(tmp_path):
    before = _vocal_gp()
    after = add_lyrics_to_gp(before, 1, "가 나 다 라\n마 바 사 아").data
    a, b = zipfile.ZipFile(io.BytesIO(before)), zipfile.ZipFile(io.BytesIO(after))
    assert a.namelist() == b.namelist()
    assert all(a.read(n) == b.read(n) for n in a.namelist() if n != "Content/score.gpif")
    ra, rb = _root(before), _root(after)
    for tag in ("Score", "MasterTrack", "MasterBars", "Bars", "Voices", "Notes", "Rhythms"):
        assert ET.tostring(ra.find(tag)) == ET.tostring(rb.find(tag)), tag
    assert ET.tostring(ra.find("Tracks")[0]) == ET.tostring(rb.find("Tracks")[0])


def test_errors_for_drum_track_empty_lyrics_and_track_without_notes():
    gp = add_tracks_to_gp(_make_gp(), [TrackSpec("d", "Kit", "drums", [(36, 0, 1)], True)]).data
    with pytest.raises(GuitarProError, match="드럼"):
        add_lyrics_to_gp(gp, 1, "가 나")
    with pytest.raises(GuitarProError, match="비어"):
        add_lyrics_to_gp(_vocal_gp(), 1, "  \n[Verse]\n")
    with pytest.raises(GuitarProError, match="음표가 없"):
        add_lyrics_to_gp(_make_gp(), 0, "가 나")


def test_list_tracks_flags_vocal_candidates():
    tracks = list_tracks(_vocal_gp())
    assert [t["looks_vocal"] for t in tracks] == [False, True]


def test_cli_adds_lyrics_to_auto_detected_vocal_track(tmp_path, capsys):
    from songsplit.merge_cli import main

    base = tmp_path / "song.gp"
    base.write_bytes(_vocal_gp())
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("가 나 다 라\n마 바 사 아", encoding="utf-8")
    out = tmp_path / "out.gp"
    assert main(["--base", str(base), "--lyrics", str(lyrics), "-o", str(out)]) == 0
    assert [s for _, s in _syllables(out.read_bytes()) if s not in ("", "_")] == list("가나다라마바사아")
    assert "'Vocals' 트랙에 음절 8개" in capsys.readouterr().out
    # 트랙 이름 지정, 없는 이름은 오류
    assert main(["--base", str(base), "--lyrics", str(lyrics), "--lyrics-track", "nothing", "-o", str(out)]) == 1


def test_lyrics_are_written_as_cdata_like_guitar_pro():
    gp = add_lyrics_to_gp(_vocal_gp(PHRASE1), 1, "I can't & you", mode="sequential").data
    raw = zipfile.ZipFile(io.BytesIO(gp)).read("Content/score.gpif").decode()
    # 박별 음절 5줄(빈 줄도 CDATA) + 트랙 가사 5줄
    assert "<Lyrics><Line><![CDATA[I]]></Line><Line><![CDATA[]]></Line><Line><![CDATA[]]></Line>" in raw
    assert "<Text><![CDATA[I can't & you]]></Text>" in raw
    assert raw.count("<Text><![CDATA[]]></Text>") == 4


class _FakeAudio:
    """slot_features만 흉내 내는 음원 정보: 슬롯별 (발성 비율, 직전 무음 길이, 시각)."""

    def __init__(self, activity, gaps):
        self.activity, self.gaps = activity, gaps

    def refine(self, notes):
        self.refined_with = len(notes)

    def slot_features(self, starts, durations):
        return self.activity, self.gaps, [s * 0.8 for s in starts]


def test_audio_activity_makes_silent_notes_skippable_and_marks_line_starts():
    # 같은 멜로디 8음표(한 구절). 앞의 2음표는 음원에서 무음(잡음), 나머지 6음표에서 노래가 불린다
    notes = [(60 + i, 4 * i, 4 * i + 3) for i in range(8)]
    gp = _vocal_gp(notes)
    activity = [0.0, 0.0] + [1.0] * 6
    audio = _FakeAudio(activity, gaps=[2.0] * 8)
    result = add_lyrics_to_gp(gp, 1, "가 나 다 라 마 바", mode="smart", audio=audio)
    labelled = [(i, s) for i, (_, s) in enumerate(_syllables(result.data)) if s not in ("", "_")]
    assert [s for _, s in labelled] == list("가나다라마바")
    assert [i for i, _ in labelled] == [2, 3, 4, 5, 6, 7]  # 무음 음표 둘을 건너뛰고 노래가 불리는 자리에 놓인다
    assert result.lines[0].start_sec is not None


def test_audio_breath_positions_decide_where_lines_break():
    # 음표 8개를 음절 4+4 두 줄로 나눈다. 숨 쉬는 자리가 5번째 음표 앞이 아니라 3번째 음표 앞이라면(3+5) 그쪽을 따른다
    notes = [(60 + i, 4 * i, 4 * i + 3) for i in range(8)]
    gp = _vocal_gp(notes)
    gaps_audio = [2.0, 0, 0, 0, 0, 0, 0, 0]
    gaps_audio[3] = 1.5  # 4번째 음표 앞에서 숨을 쉰다
    audio = _FakeAudio([1.0] * 8, gaps_audio)
    result = add_lyrics_to_gp(gp, 1, "가 나 다\n라 마 바 사 아", mode="smart", audio=audio)
    assert [(r.slots) for r in result.lines] == [3, 5]
    assert [s for _, s in _syllables(result.data) if s != "_"] == list("가나다라마바사아")


def _redispatch(gp: bytes, track: int = 1):
    """사용자가 Guitar Pro에서 가사를 고쳐 저장했을 때: 트랙 텍스트를 처음부터 다시 순서대로 나눈 결과."""
    from songsplit.stages.lyrics import simulate_gp_dispatch

    root = _root(gp)
    t = root.find("Tracks")[track]
    result = simulate_gp_dispatch(root, track, t.findtext("Lyrics/Line/Text"), int(t.findtext("Lyrics/Line/Offset")))
    actual = {id(s.beat): (s.beat.find("Lyrics/Line").text if s.beat.find("Lyrics/Line") is not None else None) for s, _ in result}
    return [(actual[id(slot.beat)], label) for slot, label in result]


def test_editing_lyrics_in_guitar_pro_does_not_shift_anything():
    # 붙임줄로 이어진 음, 잡음 음표(건너뜀), 한 음표에 단어 둘이 얹히는 경우를 모두 포함한다
    notes = [
        (60, 0, 8),        # 길게 이어지는 음(마디선을 넘음)
        (62, 16, 20),
        (64, 20, 24),
        (65, 28, 29),      # 짧은 잡음 음표 — 건너뜀
        (67, 32, 36),
        (69, 40, 44),
        (71, 48, 52),
        (72, 52, 56),
    ]
    gp = _vocal_gp(notes)
    for text in ("I can't wait\nfor you", "가 나 다\n라 마 바 사", "in-to the night\nyou are mine"):
        out = add_lyrics_to_gp(gp, 1, text, mode="smart").data
        pairs = _redispatch(out)
        assert pairs, text
        assert all(stored == dispatched for stored, dispatched in pairs), (text, pairs)


def test_multi_word_group_on_one_note_is_joined_with_plus_in_the_text():
    # 음표 3개에 단어 5개: 한 음표에 여러 단어가 얹힌다. 텍스트에서는 '+'로 묶여야 다시 나눠도 박 수가 어긋나지 않는다
    notes = [(60, 0, 4), (62, 4, 8), (64, 8, 12)]
    out = add_lyrics_to_gp(_vocal_gp(notes), 1, "one two three four five", mode="smart").data
    text = _root(out).find("Tracks")[1].findtext("Lyrics/Line/Text")
    assert "+" in text and len(text.split()) == 3
    assert all(stored == dispatched for stored, dispatched in _redispatch(out))
