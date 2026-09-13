from pathlib import Path
import copy,zipfile,xml.etree.ElementTree as E
from music21 import converter,chord
src=Path('outputs/home_sweet_home_gp80/Home_Sweet_Home_add_bar_before_80bpm.gp')
z=zipfile.ZipFile(src);r=E.fromstring(z.read('Content/score.gpif'));old=copy.deepcopy(r)
p=next(p for p in converter.parse('outputs/home_sweet_home_chords/Home_Sweet_Home_full_score.musicxml').parts if p.partName=='Piano')
grid=[set() for _ in range(68*16)]
for m in p.getElementsByClass('Measure'):
 for n in m.recurse().notes:
  if isinstance(n,chord.Chord): pitches=n.pitches
  else:pitches=[n.pitch]
  # Match numbered measures, retaining pickup notes inside the existing intro.
  start=n.getOffsetInHierarchy(m)+(max(0,m.number-1)*4)
  a=round(float(start)*4);b=a+round(float(n.quarterLength)*4)
  assert 0<=a<b<=len(grid)
  for j in range(a,b):grid[j].update(int(x.midi) for x in pitches)
tracks=r.find('Tracks');track=copy.deepcopy(tracks[2]);tid=str(max(int(x.get('id')) for x in tracks)+1);track.set('id',tid)
track.find('Name').text='Piano — MusicXML';track.find('ShortName').text='Pno.XML';track.find('Color').text='150 180 235'
track.find('Transpose/Octave').text='0';track.find('InstrumentSet/Name').text='Acoustic Grand Piano';track.find('InstrumentSet/Type').text='piano';track.find('IconId').text='1';track.find('AudioEngineState').text='MIDI'
for tag in ['UseOneChannelPerString','RSE','Sounds','Automations','Staves','Lyrics']:
 x=track.find(tag)
 if x is not None:track.remove(x)
sounds=E.SubElement(track,'Sounds');sound=E.SubElement(sounds,'Sound')
for k,v in [('Name','Acoustic Grand Piano'),('Label','Piano'),('Path','Keyboards/Piano'),('Role','User')]:E.SubElement(sound,k).text=v
mi=E.SubElement(sound,'MIDI')
for k,v in [('LSB','0'),('MSB','0'),('Program','0')]:E.SubElement(mi,k).text=v
st=E.SubElement(E.SubElement(track,'Staves'),'Staff');props=E.SubElement(st,'Properties');E.SubElement(props,'Name').text='Standard'
track.find('MidiConnection/PrimaryChannel').text='12';track.find('MidiConnection/SecondaryChannel').text='13'
tracks.append(track);r.find('MasterTrack/Tracks').text+=' '+tid
ids={tag:max(int(x.get('id')) for x in r.find(tag))+1 for tag in ['Bars','Voices','Beats','Notes','Rhythms']}
def new(tag,element):
 i=str(ids[tag]);ids[tag]+=1;x=E.SubElement(r.find(tag),element,id=i);return x,i
rh={}
for size,name in [(1,'16th'),(2,'Eighth'),(4,'Quarter'),(8,'Half'),(16,'Whole')]:
 x,i=new('Rhythms','Rhythm');E.SubElement(x,'NoteValue').text=name;rh[size]=i
for bar,m in enumerate(r.find('MasterBars')):
 b,bid=new('Bars','Bar');E.SubElement(b,'Clef').text='G2'
 v,vid=new('Voices','Voice');E.SubElement(b,'Voices').text=vid+' -1 -1 -1';beatids=[]
 t=bar*16
 while t<(bar+1)*16:
  end=t+1
  while end<(bar+1)*16 and grid[end]==grid[t]:end+=1
  # Dyadic segmentation, with ties for held pitches across segments and bars.
  size=max(s for s in rh if s<=end-t and (t%16)%s==0)
  finish=t+size;beat,beatid=new('Beats','Beat');beatids.append(beatid)
  E.SubElement(beat,'Dynamic').text='MF';E.SubElement(beat,'Rhythm',ref=rh[size]);nids=[]
  for midi in sorted(grid[t]):
   n,nid=new('Notes','Note');nids.append(nid)
   before=t>0 and midi in grid[t-1];after=finish<len(grid) and midi in grid[finish]
   if before or after:E.SubElement(n,'Tie',origin=str(after).lower(),destination=str(before).lower())
   E.SubElement(n,'InstrumentArticulation').text='0';props=E.SubElement(n,'Properties')
   prop=E.SubElement(props,'Property',name='Midi');E.SubElement(prop,'Number').text=str(midi)
   name=['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'][midi%12]
   for key in ['ConcertPitch','TransposedPitch']:
    pitch=E.SubElement(E.SubElement(props,'Property',name=key),'Pitch');E.SubElement(pitch,'Step').text=name[0];E.SubElement(pitch,'Accidental').text='Sharp' if '#' in name else '';E.SubElement(pitch,'Octave').text=str(midi//12)
  if nids:E.SubElement(beat,'Notes').text=' '.join(nids)
  t=finish
 E.SubElement(v,'Beats').text=' '.join(beatids);m.find('Bars').text+=' '+bid
# All pre-existing tracks, beat definitions and notes are byte-for-byte equivalent at XML level.
for tag in ['Tracks','Bars','Voices','Beats','Notes','Rhythms']:
 for a,b in zip(old.find(tag),r.find(tag)):assert E.tostring(a)==E.tostring(b),(tag,a.get('id'))
for a,b in zip(old.find('MasterBars'),r.find('MasterBars')):assert b.findtext('Bars').split()[:-1]==a.findtext('Bars').split()
# Validate appended piano pitch occupancy against the source score's aligned grid.
lookup={tag:{x.get('id'):x for x in r.find(tag)} for tag in ['Bars','Voices','Beats','Notes']};rev={v:k for k,v in rh.items()};actual=[]
for m in r.find('MasterBars'):
 b=lookup['Bars'][m.findtext('Bars').split()[-1]];v=lookup['Voices'][b.findtext('Voices').split()[0]]
 for bid in v.findtext('Beats').split():
  beat=lookup['Beats'][bid];pitches={int(lookup['Notes'][nid].find("Properties/Property[@name='Midi']/Number").text) for nid in beat.findtext('Notes','').split()}
  actual.extend([pitches]*rev[beat.find('Rhythm').get('ref')])
assert actual==grid
out=Path('outputs/home_sweet_home_merged/Home_Sweet_Home_80bpm_with_MusicXML_Piano.gp')
with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as dest:
 for info in z.infolist():
  # Optional GP8 precomputed score views describe the old track set; regenerate on load.
  if info.filename.startswith('Content/ScoreViews/'):continue
  data=z.read(info.filename)
  if info.filename=='Content/score.gpif':data=E.tostring(r,encoding='utf-8',xml_declaration=True)
  if info.filename=='Content/PartConfiguration':
   # Full-score view: add standard-notation display for track 7.
   assert data[8]==6
   data=data[:8]+bytes([7])+data[9:15]+bytes([1])+data[15:]
  dest.writestr(info,data)
with zipfile.ZipFile(out) as check:assert check.testzip() is None
print(out.resolve());print('7 tracks / 68 bars; piano grid verified; all existing musical content preserved.')
