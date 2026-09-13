from pathlib import Path
import copy, json, math, shutil, zipfile
import numpy as np
import pretty_midi
from music21 import stream, note, chord, meter, tempo, key, metadata, instrument, clef, tie, converter
from songsplit.pipeline.job import Job, Stage

out = Path(__file__).resolve().parent
job = Job.load((out/'job_id.txt').read_text().strip())
analysis = json.loads((job.stage_dir(Stage.ANALYSIS)/'analysis.json').read_text())
bpm = analysis['tempo_bpm']
duration = job.get_stage(Stage.INGEST)['outputs']['duration_sec']
measures = math.ceil((duration*bpm/60-1)/4)
bounds = [(0,4)] + [(4+i*16,4+(i+1)*16) for i in range(measures)]
(out/"midi").mkdir(exist_ok=True)
aligned_mids = {}
score = stream.Score()
score.metadata = metadata.Metadata(title='Home Sweet Home — automatic transcription', composer='Car, the garden / Official Audio source')
names = {'vocals':'Vocals / guide', 'drums':'Drums', 'bass':'Bass', 'guitar':'Guitar', 'piano':'Piano', 'other':'Other instruments'}
instruments = {'vocals':instrument.Vocalist, 'drums':instrument.Percussion, 'bass':instrument.ElectricBass, 'guitar':instrument.ElectricGuitar, 'piano':instrument.Piano, 'other':instrument.Instrument}
summary = {}
for stem, midpath in job.get_stage(Stage.MIDI)['outputs'].items():
    midi = pretty_midi.PrettyMIDI(midpath)
    midi.time_signature_changes = [pretty_midi.TimeSignature(1,4,0), pretty_midi.TimeSignature(4,4,60/bpm)]
    midi.key_signature_changes = [pretty_midi.KeySignature(7,0)]
    aligned_mids[stem] = out/'midi'/f'{stem}.mid'
    midi.write(str(aligned_mids[stem]))
    notes = [n for inst in midi.instruments for n in inst.notes]
    summary[stem] = len(notes)
    # Sixteenth-note score grid; preserve pitches without scale snapping.
    active = [set() for _ in range(bounds[-1][1])]
    for n in notes:
        start = max(0, round(n.start*bpm/60*4))
        end = min(len(active), max(start+1, round(n.end*bpm/60*4)))
        if stem == 'drums': end = min(len(active), start+1)
        for i in range(start,end): active[i].add(n.pitch)
    part = stream.Part(id=stem)
    part.partName = names[stem]
    part.insert(0, instruments[stem]())
    for bar,(lo,hi) in enumerate(bounds):
        m = stream.Measure(number=bar)
        if bar == 0: m.paddingLeft = 3
        if bar == 0:
            m.insert(0,meter.TimeSignature('4/4'))
            m.insert(0,tempo.MetronomeMark(number=round(bpm,3)))
            m.insert(0,clef.PercussionClef() if stem=='drums' else clef.BassClef() if stem=='bass' else clef.TrebleClef())
            if stem != 'drums': m.insert(0,key.Key(analysis['key'],analysis['scale']))
        if stem == 'drums':
            for pitch, display in [(36,('F',4)),(38,('C',5)),(42,('G',5))]:
                v = stream.Voice(id=str(pitch))
                for t in range(hi-lo):
                    event = note.Unpitched() if pitch in active[lo+t] else note.Rest()
                    if isinstance(event,note.Unpitched):
                        event.displayStep,event.displayOctave=display
                        event.storedInstrument={36:instrument.BassDrum,38:instrument.SnareDrum,42:instrument.HiHatCymbal}[pitch]()
                    event.quarterLength=.25
                    v.insert(t/4,event)
                m.insert(0,v)
        else:
            t=lo
            while t<hi:
                pitches=active[t]; end=t+1
                while end<hi and active[end]==pitches: end+=1
                event=chord.Chord(sorted(pitches)) if pitches else note.Rest()
                event.quarterLength=(end-t)/4
                if pitches:
                    for n in event.notes:
                        before=t>0 and n.pitch.midi in active[t-1]
                        after=end<len(active) and n.pitch.midi in active[end]
                        if before or after: n.tie=tie.Tie('continue' if before and after else 'stop' if before else 'start')
                m.insert((t-lo)/4,event)
                t=end
        part.append(m)
    score.insert(0,part)
    single=stream.Score(); single.metadata=copy.deepcopy(score.metadata); single.insert(0,copy.deepcopy(part))
    path=out/f'{stem}.musicxml'
    single.write('musicxml',fp=path)
    check=converter.parse(path)
    assert len(check.parts)==1
    print('SCORE',stem,len(notes),'notes',flush=True)
score.write('musicxml',fp=out/'Home_Sweet_Home_full_score.musicxml')
assert len(converter.parse(out/'Home_Sweet_Home_full_score.musicxml').parts)==6
merged = pretty_midi.PrettyMIDI(str(job.stage_dir(Stage.EXPORT)/'merged.mid'))
merged.time_signature_changes = [pretty_midi.TimeSignature(1,4,0),pretty_midi.TimeSignature(4,4,60/bpm)]
merged.key_signature_changes = [pretty_midi.KeySignature(7,0)]
merged.write(str(out/'Home_Sweet_Home_all_tracks.mid'))
readme=f'''Home Sweet Home — SongSplit automatic transcription
Source: {job.read_manifest()['source_filename']}
Duration: {duration:.2f} seconds
User-specified tempo: {bpm:.3f} BPM; user-specified key: {analysis['key']} {analysis['scale']}
Separation: Demucs htdemucs_6s, six stems.
MIDI: SongSplit Basic Pitch, onset threshold 0.65, frame threshold 0.40, minimum length 150 ms, melodia trick disabled.
Filter: reject MIDI velocity below 35 (pitched notes) and events below max(-42 dBFS, track 95th-percentile 20ms RMS minus 30 dB).
Drums use onset heuristics with the same RMS filter.
MusicXML: 1/16 grid, assumed 4/4 meter, audio time zero retained; one-quarter pickup (measure 0), then full 4/4 measures. Old bar 6 beat 2 = new bar 6 beat 1 at 15.75 seconds. MIDI uses 1/4 at time zero then 4/4 at 0.75 seconds to preserve pickup; some DAWs number the pickup as bar 1. No pitches or note timings changed.
This is an automatically transcribed draft, not a verified published score.
Check meter, beat alignment, tempo, note lengths and pitch errors in a score editor.
Source: Official Audio. Separated tracks can still contain leakage.
Drum notation is limited to kick, snare and closed hi-hat.
Detected note counts: {json.dumps(summary)}
'''
(out/'README.txt').write_text(readme)
with zipfile.ZipFile(out/'Home_Sweet_Home_tracks_midi_musicxml.zip','w',zipfile.ZIP_DEFLATED) as z:
    for stem,path in job.get_stage(Stage.SEPARATION)['outputs'].items(): z.write(path,f'stems/{stem}.wav')
    for stem,path in aligned_mids.items(): z.write(path,f'midi/{stem}.mid')
    z.write(out/'Home_Sweet_Home_all_tracks.mid','midi/all_tracks.mid')
    for p in out.glob('*.musicxml'): z.write(p,'scores/'+p.name)
    z.write(out/'README.txt','README.txt')
    z.write(out/'filter_stats.json','filter_stats.json')
print('COMPLETE',json.dumps(summary),flush=True)
