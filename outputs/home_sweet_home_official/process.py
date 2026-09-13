from pathlib import Path
import json
import numpy as np
import soundfile as sf
from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.ingest import ingest
from songsplit.stages.separate import separate_stems
from songsplit.stages.separation.demucs_engine import DemucsEngine
from songsplit.stages.analyze import apply_user_edit

out = Path(__file__).resolve().parent
marker = out/'job_id.txt'
source = next(p for p in Path('/Users/noone/Downloads').glob('*Home Sweet Home.mp3') if p.name.startswith('[Official Audio]'))
job = Job.load(marker.read_text().strip()) if marker.exists() else Job.create(source.name)
marker.write_text(job.job_id)
print('JOB',job.base_dir,flush=True)
wav = Path(job.get_stage(Stage.INGEST)['outputs']['wav_path']) if job.is_stage_done(Stage.INGEST) else ingest(job,source)
engine = DemucsEngine(model='htdemucs_6s')
stems = {k:Path(v) for k,v in job.get_stage(Stage.SEPARATION)['outputs'].items()} if job.is_stage_done(Stage.SEPARATION) else separate_stems(job,wav,engine,engine.available_stems())
print('SEPARATION DONE',flush=True)
apply_user_edit(job,80.0,'G','major')
from basic_pitch.inference import predict
from songsplit.stages.transcription.drums import transcribe_drums
from songsplit.stages.export import build_export
job.update_stage(Stage.MIDI,StageStatus.RUNNING,params={'tempo_bpm':80,'key':'G major','onset_threshold':0.65,'frame_threshold':0.4,'minimum_note_ms':150,'minimum_velocity':35,'rms_floor_dbfs':-42,'relative_floor_db':-30})
mids={}; stats={}
for stem,path in stems.items():
    target=job.stage_dir(Stage.MIDI)/f'{stem}.mid'
    if target.exists():
        mids[stem]=str(target)
        continue
    print('TRANSCRIBE',stem,flush=True)
    y,sr=sf.read(path,dtype='float32',always_2d=True)
    hop=max(1,int(sr*.02))
    power=np.mean(y*y,axis=1)
    rms=np.sqrt(np.array([np.mean(power[i:i+hop]) for i in range(0,len(power),hop)]))
    floor=max(10**(-42/20),float(np.percentile(rms,95))*10**(-30/20))
    midi=transcribe_drums(path,80) if stem=='drums' else predict(str(path),midi_tempo=80,onset_threshold=.65,frame_threshold=.4,minimum_note_length=150,melodia_trick=False)[1]
    before=sum(len(i.notes) for i in midi.instruments)
    for inst in midi.instruments:
        inst.name=stem
        inst.program={'vocals':53,'drums':0,'bass':33,'guitar':27,'piano':0,'other':48}[stem]
        kept=[]
        for n in inst.notes:
            a=max(0,int(n.start*sr/hop)); b=min(len(rms),max(a+1,int(np.ceil(n.end*sr/hop))))
            level=float(np.max(rms[a:b])) if b>a else 0
            if level>=floor and (stem=='drums' or n.velocity>=35): kept.append(n)
        inst.notes=kept
    midi.write(str(target))
    mids[stem]=str(target)
    stats[stem]={'detected':before,'kept':sum(len(i.notes) for i in midi.instruments),'rms_floor_dbfs':20*np.log10(floor)}
    (out/'filter_stats.json').write_text(json.dumps(stats,indent=2))
    print('MIDI DONE',stem,stats[stem],flush=True)
job.update_stage(Stage.MIDI,StageStatus.DONE,outputs=mids)
print('EXPORT',build_export(job),flush=True)
