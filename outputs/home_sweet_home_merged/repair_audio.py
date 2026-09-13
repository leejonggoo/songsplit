from pathlib import Path
import zipfile,copy,xml.etree.ElementTree as E
src=Path('outputs/home_sweet_home_merged/Home_Sweet_Home_80bpm_with_MusicXML_Piano.gp');z=zipfile.ZipFile(src);r=E.fromstring(z.read('Content/score.gpif'));old=copy.deepcopy(r)
t=r.find('Tracks')[-1];base=r.find('Tracks')[0]
t.find('InstrumentSet/Type').text='acousticPiano'
t.insert(list(t).index(t.find('ForcedSound')),copy.deepcopy(base.find('RSE')))
s=t.find('Sounds/Sound');rs=copy.deepcopy(base.find('Sounds/Sound/RSE'));rs.find('SoundbankPatch').text='German-APiano';rs.find('EffectChain').clear();s.append(rs)
t.find('AudioEngineState').text='RSE'
auto=E.SubElement(E.SubElement(t,'Automations'),'Automation')
for k,v in [('Type','Sound'),('Linear','false'),('Bar','0'),('Position','0'),('Visible','true'),('Value','Keyboards/Piano;Acoustic Grand Piano;User')]:E.SubElement(auto,k).text=v
for tag in ['Notes','Beats','Bars','Voices','Rhythms','MasterBars']:assert E.tostring(old.find(tag))==E.tostring(r.find(tag))
for tr in r.find('Tracks'):
 assert tr.find('RSE/ChannelStrip/Parameters') is not None
 for sound in tr.findall('Sounds/Sound'):assert sound.find('RSE/SoundbankPatch') is not None
out=src.with_name('Home_Sweet_Home_80bpm_Piano_repaired.gp')
with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as dest:
 for info in z.infolist():dest.writestr(info,E.tostring(r,encoding='utf-8',xml_declaration=True) if info.filename=='Content/score.gpif' else z.read(info.filename))
with zipfile.ZipFile(out) as check:assert check.testzip() is None
print(out.resolve())
