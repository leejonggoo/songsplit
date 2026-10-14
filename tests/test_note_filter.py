import pretty_midi

from songsplit.stages.transcription.note_filter import filter_notes


def _midi(notes):
    m = pretty_midi.PrettyMIDI(initial_tempo=120)
    inst = pretty_midi.Instrument(program=0)
    inst.notes = [pretty_midi.Note(velocity=v, pitch=p, start=s, end=e) for p, s, e, v in notes]
    m.instruments.append(inst)
    return m


def _pitches(m):
    return sorted(n.pitch for n in m.instruments[0].notes)


def test_removes_weak_short_and_harmonic_ghost_notes():
    m = _midi(
        [
            (60, 0.0, 1.0, 90),  # 기준음
            (72, 0.0, 1.0, 50),  # 옥타브 위 배음 유령음
            (64, 2.0, 3.0, 20),  # 너무 약함
            (67, 4.0, 4.05, 90),  # 너무 짧음
            (65, 5.0, 6.0, 80),  # 정상
        ]
    )
    assert _pitches(filter_notes(m, "guitar", 0.5)) == [60, 65]


def test_merges_fragmented_same_pitch():
    m = _midi([(60, 0.0, 0.5, 80), (60, 0.55, 1.2, 80)])
    notes = filter_notes(m, "guitar", 0.5).instruments[0].notes
    assert len(notes) == 1 and notes[0].end == 1.2


def test_monophonic_stem_keeps_one_note_at_a_time():
    m = _midi([(60, 0.0, 1.0, 100), (62, 0.2, 0.9, 60)])
    assert _pitches(filter_notes(m, "vocals", 0.5)) == [60]
    assert _pitches(filter_notes(m, "piano", 0.5)) == [60, 62]


def test_drums_untouched_and_input_not_mutated():
    m = _midi([(36, 0.0, 0.1, 25)])
    m.instruments[0].is_drum = True
    assert _pitches(filter_notes(m, "drums", 1.0)) == [36]
    m2 = _midi([(60, 0.0, 0.5, 80), (60, 0.55, 1.2, 80)])
    filter_notes(m2, "guitar")
    assert len(m2.instruments[0].notes) == 2
