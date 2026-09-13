from pathlib import Path
import json, csv, zipfile, copy
import numpy as np
import librosa
import xml.etree.ElementTree as ET

root=Path(__file__).resolve().parent
source=root.parent/'home_sweet_home_aligned'
job=Path('jobs')/(source/'job_id.txt').read_text().strip()
stems=job/'01_separation'/'htdemucs_6s'
sr=22050; hop=512
y=None
for stem in ['guitar','piano','other']:
    a,_=librosa.load(stems/f'{stem}.wav',sr=sr)
    y=a if y is None else y+a
print('Loaded accompaniment',flush=True)
chroma=librosa.feature.chroma_stft(y=y,sr=sr,hop_length=hop,n_fft=4096,tuning=0,norm=None)
b,_=librosa.load(stems/'bass.wav',sr=sr)
bc=librosa.feature.chroma_stft(y=b,sr=sr,hop_length=hop,n_fft=8192,tuning=0,norm=None)
times=librosa.frames_to_time(np.arange(chroma.shape[1]),sr=sr,hop_length=hop)
tree=ET.parse(source/'Home_Sweet_Home_full_score.musicxml')
bars=[int(m.attrib['number']) for m in tree.findall('./part')[0].findall('measure')]
names=['C','C#','D','E-','E','F','F#','G','A-','A','B-','B']
qualities=[('major',[0,4,7],''),('minor',[0,3,7],'m'),('dominant',[0,4,7,10],'7'),('major-seventh',[0,4,7,11],'maj7'),('minor-seventh',[0,3,7,10],'m7'),('suspended-fourth',[0,5,7],'sus4'),('diminished',[0,3,6],'dim')]
candidates=[(r,k,intervals,suffix) for r in range(12) for k,intervals,suffix in qualities]
vectors=[]; energies=[]; ranges=[]; bassvec=[]
for bar in bars:
    start=0 if bar==0 else .75+(bar-1)*3
    end=.75 if bar==0 else start+3
    mask=(times>=start)&(times<end)
    v=np.mean(chroma[:,mask],axis=1) if mask.any() else np.zeros(12)
    bass=np.mean(bc[:,mask],axis=1) if mask.any() else np.zeros(12)
    vectors.append(v/(np.linalg.norm(v)+1e-12)); bassvec.append(bass/(bass.max()+1e-12))
    clip=y[int(start*sr):int(end*sr)]
    energies.append(float(np.sqrt(np.mean(clip**2))) if len(clip) else 0)
    ranges.append((start,end))
scores=np.zeros((len(bars),len(candidates)))
for j,(r,kind,intervals,suffix) in enumerate(candidates):
    pcs=[(r+i)%12 for i in intervals]
    template=np.zeros(12); template[pcs]=1; template/=np.linalg.norm(template)
    for i,v in enumerate(vectors):
        scores[i,j]=np.dot(v,template)+.12*bassvec[i][r]
        if len(intervals)>3: scores[i,j]-=.065
        if kind in ['suspended-fourth','diminished']: scores[i,j]-=.035
        if set(pcs).issubset({7,9,11,0,2,4,6}): scores[i,j]+=.025
# Small transition penalty reduces unstable frame-level chord changes.
dp=scores[0].copy(); back=[]
for row in scores[1:]:
    transitions=dp[:,None]-.055*(1-np.eye(len(candidates)))
    arg=transitions.argmax(axis=0); back.append(arg)
    dp=row+transitions[arg,np.arange(len(candidates))]
chosen=[int(dp.argmax())]
for arg in reversed(back): chosen.append(int(arg[chosen[-1]]))
chosen.reverse()
rows=[]
for i,idx in enumerate(chosen):
    r,kind,intervals,suffix=candidates[idx]
    symbol=names[r].replace('-','b')+suffix
    if energies[i]<10**(-48/20): symbol='N.C.'
    rows.append({'bar':bars[i],'start_seconds':ranges[i][0],'chord':symbol,'root':r,'kind':kind,'score_margin':round(float(scores[i,idx]-np.partition(scores[i],-2)[-2]),4)})
(root/'chords.json').write_text(json.dumps(rows,indent=2))
with (root/'chords.csv').open('w') as f:
    w=csv.DictWriter(f,fieldnames=['bar','start_seconds','chord']); w.writeheader(); w.writerows({k:r[k] for k in w.fieldnames} for r in rows)
for path in source.glob('*.musicxml'):
    t=ET.parse(path)
    # Full score: one shared chord row above the top staff. Individual parts: chord row on each score.
    part=t.findall('./part')[0]
    for measure,row in zip(part.findall('measure'),rows):
        for old in list(measure.findall('harmony')): measure.remove(old)
        h=ET.Element('harmony',{'placement':'above'})
        if row['chord']=='N.C.':
            rt=ET.SubElement(h,'root'); ET.SubElement(rt,'root-step',{'text':''}).text='C'
            ET.SubElement(h,'kind',{'text':'N.C.'}).text='none'
        else:
            rt=ET.SubElement(h,'root'); name=names[row['root']]
            ET.SubElement(rt,'root-step').text=name[0]
            if len(name)>1: ET.SubElement(rt,'root-alter').text='1' if name[1]=='#' else '-1'
            ET.SubElement(h,'kind').text=row['kind']
        pos=next((j for j,e in enumerate(measure) if e.tag in ('note','backup','forward')),len(measure))
        measure.insert(pos,h)
    dest=root/path.name
    t.write(dest,encoding='utf-8',xml_declaration=True)
    parsed=ET.parse(dest)
    assert len(parsed.findall('.//harmony'))==len(rows)
    assert [ET.tostring(n) for n in ET.parse(path).findall('.//note')]==[ET.tostring(n) for n in parsed.findall('.//note')]
readme=(source/'README.txt').read_text()+'\nChord symbols: automatically estimated per aligned measure from separated guitar/piano/other audio with bass support. G-major preference, not hard restriction. Chords require musical review. Full score shows shared chords on top staff; individual scores each include chords. MIDI note content is unchanged.\n'
(root/'README.txt').write_text(readme)
with zipfile.ZipFile(source/'Home_Sweet_Home_tracks_midi_musicxml.zip') as zin, zipfile.ZipFile(root/'Home_Sweet_Home_with_chords.zip','w',zipfile.ZIP_DEFLATED) as zout:
    for name in zin.namelist():
        if name.startswith('scores/') or name=='README.txt': continue
        zout.writestr(name,zin.read(name))
    for p in root.glob('*.musicxml'): zout.write(p,'scores/'+p.name)
    for name in ['README.txt','chords.csv','chords.json']: zout.write(root/name,name)
print('COMPLETE',[(r['bar'],r['chord']) for r in rows],flush=True)
