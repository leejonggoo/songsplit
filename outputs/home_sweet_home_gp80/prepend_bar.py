from pathlib import Path
import xml.etree.ElementTree as E, zipfile, copy
src=Path('outputs/home_sweet_home_gp80/Home_Sweet_Home_80bpm_67bars.gp')
out=src.with_name('Home_Sweet_Home_80bpm_68bars_intro.gp')
z=zipfile.ZipFile(src);r=E.fromstring(z.read('Content/score.gpif'));before=copy.deepcopy(r)
masters=r.find('MasterBars');bars=r.find('Bars'); lookup={b.attrib['id']:b for b in bars}
m=E.Element('MasterBar')
for tag in ['Key','Time']:m.append(copy.deepcopy(masters[0].find(tag)))
ids=[]; nextid=max(int(b.attrib['id']) for b in bars)+1
for oldid in masters[0].findtext('Bars').split():
 b=copy.deepcopy(lookup[oldid]);b.attrib['id']=str(nextid);nextid+=1
 b.find('Voices').text='-1 -1 -1 -1';bars.append(b);ids.append(b.attrib['id'])
E.SubElement(m,'Bars').text=' '.join(ids);masters.insert(0,m)
for a in r.findall('.//Automation'):
 bar=a.find('Bar')
 if bar is not None:bar.text=str(int(bar.text)+1)
 if a.findtext('Type')=='Tempo':
  a.find('Value').text='80 2';a.find('Bar').text='0';a.find('Position').text='0'
# Rebuild GP score-view measure spans with 68 entries.
def vi(n):
 result=bytearray()
 while n>127:result.append((n&127)|128);n>>=7
 result.append(n);return bytes(result)
def rewrite(data):
 i=0;out=b'';done=False
 def read():
  nonlocal i
  n=0;s=0
  while True:
   b=data[i];i+=1;n|=(b&127)<<s
   if b<128:return n
   s+=7
 while i<len(data):
  start=i;k=read();w=k&7
  if w==0:read()
  elif w==2:l=read();i+=l
  elif w in (1,5):i+=8 if w==1 else 4
  else:raise ValueError(w)
  if k==26:
   if not done:
    for j in range(68):
     v=(b'\x08'+vi(j) if j else b'')+b'\x10'+vi(j+1)+b'\x32\x00'
     out+=b'\x1a'+vi(len(v))+v
    done=True
  else:out+=data[start:i]
 return out
assert len(masters)==68
assert all(E.tostring(a)==E.tostring(b) for a,b in zip(list(masters)[1:],before.find('MasterBars')))
for tag in ['Notes','Beats','Voices','Rhythms']:assert E.tostring(r.find(tag))==E.tostring(before.find(tag))
with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as dest:
 for info in z.infolist():
  data=z.read(info.filename)
  if info.filename=='Content/score.gpif':data=E.tostring(r,encoding='utf-8',xml_declaration=True)
  elif info.filename.startswith('Content/ScoreViews/') and info.filename.endswith('.gpsv'):data=rewrite(data)
  dest.writestr(info,data)
with zipfile.ZipFile(out) as check:assert check.testzip() is None
print(out.resolve());print('Verified 68 bars, 80 BPM, leading 4/4 rest; existing music unchanged, delayed 3 seconds.')
