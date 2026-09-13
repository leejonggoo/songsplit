from pathlib import Path
import xml.etree.ElementTree as E, zipfile,copy
from fractions import Fraction
import sys
if len(sys.argv) != 2: raise SystemExit('Usage: python '+sys.argv[0]+' /path/to/original.gp')
src=Path(sys.argv[1]); z=zipfile.ZipFile(src)
r=E.fromstring(z.read('Content/score.gpif')); original=copy.deepcopy(r)
bs={x.attrib['id']:x for x in r.find('Bars')}; vs={x.attrib['id']:x for x in r.find('Voices')}; beats={x.attrib['id']:x for x in r.find('Beats')}
values={'Whole':4,'Half':2,'Quarter':1,'Eighth':Fraction(1,2),'16th':Fraction(1,4),'32nd':Fraction(1,8)}
rh={x.attrib['id']:values[x.findtext('NoteValue')]*(sum(Fraction(1,2**i) for i in range(int(x.find('AugmentationDot').attrib['count'])+1)) if x.find('AugmentationDot') is not None else 1) for x in r.find('Rhythms')}
def duration(ids):return sum(rh[beats[b].find('Rhythm').attrib['ref']] for b in ids)
# Keep exact beat/note references, halving rhythmic values once globally.
seq=['Whole','Half','Quarter','Eighth','16th','32nd','64th']
for x in r.find('Rhythms'): x.find('NoteValue').text=seq[seq.index(x.findtext('NoteValue'))+1]
restid=str(max(map(int,beats))+1); rest=E.SubElement(r.find('Beats'),'Beat',id=restid);E.SubElement(rest,'Dynamic').text='MF';E.SubElement(rest,'Rhythm',ref='2')
newbars=[];newvoices=[];newmasters=[]
masters=list(r.find('MasterBars'))
for i in range(0,len(masters),2):
 a,b=masters[i:i+2]; m=copy.deepcopy(a)
 if b.find('Section') is not None and m.find('Section') is None:
  s=copy.deepcopy(b.find('Section'));s.find('Text').text=s.findtext('Text').strip()+' (beat 3)';m.append(s)
 ids=[]
 for left,right in zip(a.findtext('Bars').split(),b.findtext('Bars').split()):
  dest=copy.deepcopy(bs[left]); dest.attrib['id']=str(len(newbars));ids.append(dest.attrib['id']); voiceids=[]
  for v1,v2 in zip(bs[left].findtext('Voices').split(),bs[right].findtext('Voices').split()):
   if v1==v2=='-1': voiceids.append('-1');continue
   combined=[]
   for v in [v1,v2]:
    bb=vs[v].findtext('Beats').split() if v!='-1' else [restid]
    if v!='-1': assert duration(bb)==4,(i,v,duration(bb))
    combined+=bb
   voice=E.Element('Voice',id=str(len(newvoices)));E.SubElement(voice,'Beats').text=' '.join(combined);voiceids.append(voice.attrib['id']);newvoices.append(voice)
  dest.find('Voices').text=' '.join(voiceids);newbars.append(dest)
 m.find('Bars').text=' '.join(ids);newmasters.append(m)
for name,items in [('MasterBars',newmasters),('Bars',newbars),('Voices',newvoices)]:
 container=r.find(name);container.clear();container.extend(items)
for a in r.findall('.//Automation'):
 b=a.find('Bar');p=a.find('Position')
 if b is not None:
  old=int(b.text); b.text=str(old//2)
  if p is not None:p.text=str((old%2+float(p.text))/2)
 if a.findtext('Type')=='Tempo':a.find('Value').text='80 2';a.find('Visible').text='true'
# Validate sounded note timeline at the two tempos, including every occurrence.
def timeline(doc,bpm):
 bd={x.attrib['id']:x for x in doc.find('Bars')};vd={x.attrib['id']:x for x in doc.find('Voices')}; bt={x.attrib['id']:x for x in doc.find('Beats')};rr={}
 for x in doc.find('Rhythms'):
  d=Fraction(values[x.findtext('NoteValue')]); dot=x.find('AugmentationDot')
  if dot is not None:d*=sum(Fraction(1,2**i) for i in range(int(dot.attrib['count'])+1))
  rr[x.attrib['id']]=d
 result=[]
 for n,m in enumerate(doc.find('MasterBars')):
  for staff,b in enumerate(m.findtext('Bars').split()):
   for voice,v in enumerate(bd[b].findtext('Voices').split()):
    if v=='-1':continue
    t=Fraction(n*4)
    for bid in vd[v].findtext('Beats').split():
     beat=bt[bid];d=rr[beat.find('Rhythm').attrib['ref']]
     if beat.find('Notes') is not None:result.append((staff,voice,t*60/bpm,d*60/bpm,beat.findtext('Notes')))
     t+=d
 return sorted(result)
assert timeline(original,160)==timeline(r,80)
assert E.tostring(original.find('Notes'))==E.tostring(r.find('Notes'))
# GP8 score-view bar spans are protobuf field 3. Rebuild their index ranges.
def varint(n):
 out=bytearray()
 while n>127:out.append((n&127)|128);n>>=7
 out.append(n);return bytes(out)
def fields(data):
 i=0
 def read():
  nonlocal i
  n=0;s=0
  while True:
   b=data[i];i+=1;n|=(b&127)<<s
   if b<128:return n
   s+=7
 while i<len(data):
  start=i;k=read();w=k&7
  if w==0:v=read()
  elif w==2:l=read();v=data[i:i+l];i+=l
  elif w in (1,5):l=8 if w==1 else 4;v=data[i:i+l];i+=l
  else:raise ValueError(w)
  yield k,v,data[start:i]
def view(data):
 out=b'';done=False
 for k,v,raw in fields(data):
  if k==26:
   if not done:
    for i in range(67):
     body=(b'\x08'+varint(i) if i else b'')+b'\x10'+varint(i+1)+b'\x32\x00'
     out+=b'\x1a'+varint(len(body))+body
    done=True
  else:out+=raw
 return out
out=Path('outputs/home_sweet_home_gp80/Home_Sweet_Home_80bpm_67bars.gp')
with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as dest:
 for info in z.infolist():
  data=z.read(info.filename)
  if info.filename=='Content/score.gpif':data=E.tostring(r,encoding='utf-8',xml_declaration=True)
  elif info.filename.startswith('Content/ScoreViews/') and info.filename.endswith('.gpsv'):data=view(data)
  dest.writestr(info,data)
with zipfile.ZipFile(out) as check:assert check.testzip() is None
print(out.resolve());print('Validated: 134 -> 67 bars, 160 -> 80 BPM; all sounded notes retain exact timing and duration.')
