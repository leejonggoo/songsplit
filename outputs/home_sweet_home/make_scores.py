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
measures = math.ceil(duration*bpm/60/4)
score = stream.Score()
score.metadata = metadata.Metadata(title='Home Sweet Home — automatic transcription', composer='Car, the garden / TJ Karaoke source')
names = {'vocals':'Vocals / guide', 'drums':'Drums', 'bass':'Bass', 'guitar':'Guitar', 'piano':'Piano', 'other':'Other instruments'}
instruments = {'vocals':instrument.Vocalist, 'drums':instrument.Percussion, 'bass':instrument.ElectricBass, 'guitar':instrument.ElectricGuitar, 'piano':instrument.Piano, 'other':instrument.Instrument}
summary = {}
for stem, midpath in job.get_stage(Stage.MIDI)['outputs'].items():
    midi = pretty_midi.PrettyMIDI(midpath)
    notes = [n for inst in midi.instruments for n in inst.notes]
    summary[stem] = len(notes)
    # Sixteenth-note score grid; preserve pitches without scale snapping.
    active = [set() for _ in range(measures*16)]
    for n in notes:
        start = max(0, round(n.start*bpm/60*4))
        end = min(len(active), max(start+1, round(n.end*bpm/60*4)))
        if stem == 'drums': end = min(len(active), start+1)
        for i in range(start,end): active[i].add(n.pitch)
    part = stream.Part(id=stem)
    part.partName = names[stem]
    part.insert(0, instruments[stem]())
    for bar in range(measures):
        m = stream.Measure(number=bar+1)
        if bar == 0:
            m.insert(0,meter.TimeSignature('4/4'))
            m.insert(0,tempo.MetronomeMark(number=round(bpm,3)))
            m.insert(0,clef.PercussionClef() if stem=='drums' else clef.BassClef() if stem=='bass' else clef.TrebleClef())
            if stem != 'drums': m.insert(0,key.Key(analysis['key'],analysis['scale']))
        if stem == 'drums':
            for pitch, display in [(36,('F',4)),(38,('C',5)),(42,('G',5))]:
                v = stream.Voice(id=str(pitch))
                for t in range(16):
                    event = note.Unpitched() if pitch in active[bar*16+t] else note.Rest()
                    if isinstance(event,note.Unpitched):
                        event.displayStep,event.displayOctave=display
                        event.storedInstrument={36:instrument.BassDrum,38:instrument.SnareDrum,42:instrument.HiHatCymbal}[pitch]()
                    event.quarterLength=.25
                    v.insert(t/4,event)
                m.insert(0,v)
        else:
            t=bar*16
            while t<(bar+1)*16:
                pitches=active[t]; end=t+1
                while end<(bar+1)*16 and active[end]==pitches: end+=1
                event=chord.Chord(sorted(pitches)) if pitches else note.Rest()
                event.quarterLength=(end-t)/4
                if pitches:
                    for n in event.notes:
                        before=t>0 and n.pitch.midi in active[t-1]
                        after=end<len(active) and n.pitch.midi in active[end]
                        if before or after: n.tie=tie.Tie('continue' if before and after else 'stop' if before else 'start')
                m.insert((t-bar*16)/4,event)
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
shutil.copy2(job.stage_dir(Stage.EXPORT)/'merged.mid',out/'Home_Sweet_Home_all_tracks.mid')
readme=f'''Home Sweet Home — SongSplit automatic transcription
Source: {job.read_manifest()['source_filename']}
Duration: {duration:.2f} seconds
Estimated tempo: {bpm:.3f} BPM; estimated key: {analysis['key']} {analysis['scale']}
Separation: Demucs htdemucs_6s, six stems.
MIDI: original SongSplit Basic Pitch transcription; drums use onset heuristics.
MusicXML: 1/16 grid, assumed 4/4 meter, audio time zero retained; no pitch snapping.
This is an automatically transcribed draft, not a verified published score.
Check meter, beat alignment, tempo, note lengths and pitch errors in a score editor.
Vocals in this karaoke recording may contain guide melody or separation leakage.
Drum notation is limited to kick, snare and closed hi-hat.
Detected note counts: {json.dumps(summary)}
'''
(out/'README.txt').write_text(readme)
with zipfile.ZipFile(out/'Home_Sweet_Home_tracks_midi_musicxml.zip','w',zipfile.ZIP_DEFLATED) as z:
    for stem,path in job.get_stage(Stage.SEPARATION)['outputs'].items(): z.write(path,f'stems/{stem}.wav')
    for stem,path in job.get_stage(Stage.MIDI)['outputs'].items(): z.write(path,f'midi/{stem}.mid')
    z.write(out/'Home_Sweet_Home_all_tracks.mid','midi/all_tracks.mid')
    for p in out.glob('*.musicxml'): z.write(p,'scores/'+p.name)
    z.write(out/'README.txt','README.txt')
print('COMPLETE',json.dumps(summary),flush=True)
