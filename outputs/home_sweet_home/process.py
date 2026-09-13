from pathlib import Path
import json
from songsplit.pipeline.job import Job, Stage, StageStatus
from songsplit.stages.ingest import ingest
from songsplit.stages.separate import separate_stems
from songsplit.stages.separation.demucs_engine import DemucsEngine
from songsplit.stages.analyze import run_analysis, load_analysis

out = Path(__file__).resolve().parent
marker = out / 'job_id.txt'
source = next(Path('/Users/noone/Downloads').glob('*Home Sweet Home*TJ Karaoke.mp3'))
job = Job.load(marker.read_text().strip()) if marker.exists() else Job.create(source.name)
marker.write_text(job.job_id)
print('JOB', job.base_dir, flush=True)
wav = Path(job.get_stage(Stage.INGEST)['outputs']['wav_path']) if job.is_stage_done(Stage.INGEST) else ingest(job, source)
print('INGEST DONE', flush=True)
engine = DemucsEngine(model='htdemucs_6s')
stems = {k: Path(v) for k,v in job.get_stage(Stage.SEPARATION)['outputs'].items()} if job.is_stage_done(Stage.SEPARATION) else separate_stems(job, wav, engine, engine.available_stems())
print('SEPARATION DONE', flush=True)
analysis = load_analysis(job) or run_analysis(job, wav)
print('ANALYSIS', analysis, flush=True)
from songsplit.stages.transcribe import transcribe_stem
job.update_stage(Stage.MIDI, StageStatus.RUNNING, params={'tempo_bpm': analysis.tempo_bpm})
mids = {}
for stem, path in stems.items():
    target = job.stage_dir(Stage.MIDI) / f'{stem}.mid'
    if not target.exists():
        print('TRANSCRIBE', stem, flush=True)
        transcribe_stem(job, stem, path, analysis.tempo_bpm)
    mids[stem] = str(target)
    print('MIDI DONE', stem, flush=True)
job.update_stage(Stage.MIDI, StageStatus.DONE, outputs=mids)
from songsplit.stages.export import build_export
print('EXPORT', build_export(job), flush=True)
