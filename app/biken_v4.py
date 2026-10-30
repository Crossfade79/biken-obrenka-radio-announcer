#!/usr/bin/env python3
import base64, json, math, os, re, signal, subprocess, sys, threading, time, wave
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import urlopen
import importlib.util

APP=Path('/opt/biken-v4'); RUNTIME=APP/'runtime'; LOGS=APP/'logs'; VP=APP/'voice-packs'
MODE_FILE=APP/'mode.json'; ANN_CFG=APP/'announcer_config.json'; ANN_STATE=RUNTIME/'announcer_state.json'
LEGACY_PATH=APP/'biken_v37_locked.py'; FINAL_WAV=RUNTIME/'announcer_message.wav'; PRESETS=APP/'presets'; PRESET_FILE=PRESETS/'presets.json'; WEATHER_CACHE=RUNTIME/'weather_cache.json'
PLUGIN_DIR=APP/'plugins'; PLUGIN_HOST_PATH=PLUGIN_DIR/'plugin_host.py'; DSP_WAV=RUNTIME/'announcer_message.wav.dsp.wav'
MODE_TDOA='TDOA_BIKEN'; MODE_ANN='RADIO_ANNOUNCER'
lock=threading.RLock(); tx_lock=threading.Lock(); stop_event=threading.Event(); scheduler_stop=threading.Event()
ann_worker=None; scheduler_thread=None; legacy=None
manual_ptt_active=False; manual_ptt_lock_held=False
pps_diag_cache={'checked_monotonic':0.0,'data':{}}


def atomic_json(path,obj):
    with lock:
        path.parent.mkdir(parents=True,exist_ok=True)
        tmp=path.with_name(path.name+f'.{os.getpid()}.{threading.get_ident()}.tmp')
        tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
        os.replace(tmp,path)
def read_json(path,default):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except Exception:return dict(default)
def now_iso(): return datetime.now().astimezone().isoformat(timespec='seconds')

plugin_host=None
def load_plugin_host():
    global plugin_host
    if plugin_host is not None:return plugin_host
    if not PLUGIN_HOST_PATH.exists():return None
    try:
        spec=importlib.util.spec_from_file_location('biken_audio_plugin_host',PLUGIN_HOST_PATH)
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        plugin_host=mod.PluginHost(APP)
        return plugin_host
    except Exception as e:
        print('Audio Plugin Host load failed:',e,flush=True)
        plugin_host=None
        return None

def plugin_state():
    h=load_plugin_host()
    if h is None:return {'ok':False,'available':False,'master_enabled':False,'status':'NOT_INSTALLED','error':None}
    try:return h.state()
    except Exception as e:return {'ok':False,'available':True,'master_enabled':False,'status':'ERROR','error':str(e)}

def plugin_save_config(data):
    h=load_plugin_host()
    if h is None:return {'ok':False,'error':'PLUGIN_HOST_NOT_INSTALLED'}
    return h.save_config(data if isinstance(data,dict) else {})

def plugin_reset():
    h=load_plugin_host()
    if h is None:return {'ok':False,'error':'PLUGIN_HOST_NOT_INSTALLED'}
    return h.reset_config()

def plugin_bypass_all():
    h=load_plugin_host()
    if h is None:return {'ok':False,'error':'PLUGIN_HOST_NOT_INSTALLED'}
    return h.bypass_all()

def plugin_presets():
    h=load_plugin_host()
    if h is None:return {'ok':False,'error':'PLUGIN_HOST_NOT_INSTALLED','presets':[]}
    return h.preset_list()

def plugin_preset_load(data):
    h=load_plugin_host()
    if h is None:return {'ok':False,'error':'PLUGIN_HOST_NOT_INSTALLED'}
    return h.load_preset((data or {}).get('name'))

def plugin_preset_save(data):
    h=load_plugin_host()
    if h is None:return {'ok':False,'error':'PLUGIN_HOST_NOT_INSTALLED'}
    return h.save_custom_preset((data or {}).get('name'))

def plugin_preset_delete(data):
    h=load_plugin_host()
    if h is None:return {'ok':False,'error':'PLUGIN_HOST_NOT_INSTALLED'}
    return h.delete_custom_preset((data or {}).get('name'))

def process_output_audio(source_path):
    # Backward-compatible wrapper only. TX and RPi preview use LIVE streaming DSP.
    h=load_plugin_host()
    if h is None:return Path(source_path),{'ok':True,'bypassed':True,'reason':'PLUGIN_HOST_NOT_INSTALLED'}
    try:
        r=h.process_file(Path(source_path),DSP_WAV)
        if r.get('ok') and r.get('path'):return Path(r['path']),r
        return Path(source_path),r
    except Exception as e:
        return Path(source_path),{'ok':False,'bypassed':True,'reason':'PLUGIN_EXCEPTION','error':str(e)}

def prepare_stream_audio(source_path):
    source=Path(source_path);h=load_plugin_host()
    if h is not None and hasattr(h,'prepare_pcm'):
        prepared=h.prepare_pcm(source)
        st=h.state();return h,prepared,{'ok':True,'prepared':True,'mode':'LIVE_STREAM','engine':st.get('engine'),'chain':st.get('enabled_chain',[]),'bypassed':not bool((st.get('config') or {}).get('master_enabled'))}
    with wave.open(str(source),'rb') as w:
        if (w.getnchannels(),w.getsampwidth(),w.getframerate())!=(1,2,48000):raise RuntimeError('stream audio requires mono PCM16 48000 Hz')
        n=w.getnframes();raw=w.readframes(n)
    return None,{'pcm':raw,'frames':n,'duration_s':n/48000.0,'source':str(source)},{'ok':True,'prepared':True,'mode':'LIVE_STREAM','engine':'RAW_FALLBACK','chain':[],'bypassed':True}

def play_prepared_stream(host,prepared,audio_device):
    if host is not None and hasattr(host,'play_prepared'):
        return host.play_prepared(prepared,audio_device=audio_device,stop_event=stop_event,timeout_s=float(prepared.get('duration_s',0))+5.0)
    raw=prepared.get('pcm',b'');dur=float(prepared.get('duration_s') or (len(raw)/96000.0));cmd=['aplay','-q','-D',str(audio_device),'-t','raw','-f','S16_LE','-c','1','-r','48000']
    proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE);deadline=time.monotonic()+dur+5.0
    try:
        mv=memoryview(raw);step=1920*2
        for off in range(0,len(raw),step):
            if stop_event.is_set():raise RuntimeError('audio stopped')
            if time.monotonic()>deadline:raise RuntimeError('live audio timeout')
            proc.stdin.write(mv[off:off+step])
        proc.stdin.close();proc.stdin=None
        while proc.poll() is None:
            if stop_event.is_set() or time.monotonic()>deadline:
                proc.terminate();raise RuntimeError('audio stopped or timed out')
            time.sleep(.02)
        if proc.returncode!=0:
            err=(proc.stderr.read().decode(errors='replace') if proc.stderr else '').strip();raise RuntimeError('aplay failed: '+err[-300:])
        return {'ok':True,'bypassed':True,'chain':[],'engine':'RAW_FALLBACK','mode':'LIVE_STREAM','wall_ms':round(dur*1000,1)}
    finally:
        try:
            if proc.stdin:proc.stdin.close()
        except Exception:pass

def load_legacy():
    global legacy
    spec=importlib.util.spec_from_file_location('biken_v37_locked',LEGACY_PATH); legacy=importlib.util.module_from_spec(spec); spec.loader.exec_module(legacy)
    legacy.APP_DIR=APP; legacy.CONFIG_PATH=APP/'config.json'; legacy.STATE_PATH=RUNTIME/'tdoa_state.json'; legacy.AUDIO_DIR=APP/'audio'
    legacy.config=legacy.load_config(); legacy.gpio_init(); legacy.ptt(False)

def mode(): return read_json(MODE_FILE,{'active_mode':MODE_TDOA}).get('active_mode',MODE_TDOA)
def ann_cfg(): return read_json(ANN_CFG,{})
def ann_state():
    d={'state':'IDLE','automation':False,'voice_engine':'IDLE','ptt':False,'next_tx':None,'last_tx':None,'message_length_s':None,'error':None,'last_event':None,'updated':now_iso()}
    d.update(read_json(ANN_STATE,{})); return d
def set_ann(**kw):
    s=ann_state(); s.update(kw); s['updated']=now_iso(); atomic_json(ANN_STATE,s); return s

def _manual_ptt_release_lock():
    global manual_ptt_lock_held
    if manual_ptt_lock_held:
        try: tx_lock.release()
        except Exception: pass
        manual_ptt_lock_held=False

def manual_ptt_on():
    global manual_ptt_active, manual_ptt_lock_held
    if mode()!=MODE_ANN:return {'ok':False,'error':'wrong_mode'}
    if manual_ptt_active:return {'ok':True,'ptt':True,'manual':True,'already_on':True}
    if ann_worker and ann_worker.is_alive():return {'ok':False,'error':'audio_or_tx_busy'}
    st=ann_state()
    if bool(st.get('ptt')):return {'ok':False,'error':'ptt_already_active'}
    if not tx_lock.acquire(False):return {'ok':False,'error':'tx_busy'}
    manual_ptt_lock_held=True
    try:
        legacy.ptt(True)
        manual_ptt_active=True
        set_ann(state='MANUAL_PTT',voice_engine='IDLE',ptt=True,error=None,last_event='MANUAL_PTT_ON',manual_ptt_since=now_iso())
        return {'ok':True,'ptt':True,'manual':True}
    except Exception as e:
        try: legacy.ptt(False)
        except Exception: pass
        manual_ptt_active=False
        _manual_ptt_release_lock()
        set_ann(state='IDLE',voice_engine='IDLE',ptt=False,error=str(e),last_event='MANUAL_PTT_FAILED',manual_ptt_since=None)
        return {'ok':False,'error':str(e)}

def manual_ptt_off():
    global manual_ptt_active
    try: legacy.ptt(False)
    except Exception as e:
        manual_ptt_active=False
        _manual_ptt_release_lock()
        set_ann(state='IDLE',voice_engine='IDLE',ptt=False,error=str(e),last_event='MANUAL_PTT_OFF_ERROR',manual_ptt_since=None)
        return {'ok':False,'error':str(e)}
    was_manual=manual_ptt_active
    manual_ptt_active=False
    _manual_ptt_release_lock()
    set_ann(state='IDLE',voice_engine='IDLE',ptt=False,error=None,last_event='MANUAL_PTT_OFF' if was_manual else 'PTT_FORCED_OFF',manual_ptt_since=None)
    return {'ok':True,'ptt':False,'manual_was_active':was_manual}

def hard_stop(reason='STOP'):
    global manual_ptt_active
    stop_event.set(); scheduler_stop.set()
    try: legacy.stop_now(); legacy.stop_continuous_audio(); legacy.ptt(False)
    except Exception: pass
    manual_ptt_active=False; _manual_ptt_release_lock()
    subprocess.run(['pkill','-f','aplay.*announcer_message.wav'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    set_ann(state='IDLE',voice_engine='IDLE',ptt=False,error=None,last_event=reason,manual_ptt_since=None)

def switch_mode(new_mode):
    if new_mode not in (MODE_TDOA,MODE_ANN): return {'ok':False,'error':'invalid_mode'}
    with lock:
        hard_stop('MODE_CHANGE'); time.sleep(.1); stop_event.clear(); scheduler_stop.clear()
        atomic_json(MODE_FILE,{'active_mode':new_mode});
        if new_mode==MODE_ANN: ensure_scheduler()
        return {'ok':True,'active_mode':new_mode}

def wav_info(p):
    with wave.open(str(p),'rb') as w:
        return w.getnchannels(),w.getsampwidth(),w.getframerate(),w.getnframes(),w.getnframes()/w.getframerate()
def validate_fragment(p):
    if not p.exists(): return False,'missing '+p.name
    try:
        ch,sw,sr,n,d=wav_info(p)
        if (ch,sw,sr)!=(1,2,48000): return False,f'{p.name}: expected mono PCM16 48000 Hz'
        if n<=0:return False,f'{p.name}: empty'
        return True,None
    except Exception as e:return False,f'{p.name}: {e}'

def greeting_name(hour,cfg):
    g=cfg.get('greeting','AUTO')
    if g=='NONE':return None
    if g!='AUTO':return {'MORNING':'greeting_morning.wav','DAY':'greeting_day.wav','EVENING':'greeting_evening.wav'}.get(g)
    return 'greeting_morning.wav' if 5<=hour<11 else ('greeting_day.wav' if 11<=hour<18 else 'greeting_evening.wav')
def condition_file(c):
    m={'vedro':'weather_clear.wav','pretežno vedro':'weather_mostly_clear.wav','delimično oblačno':'weather_partly_cloudy.wav','pretežno oblačno':'weather_mostly_cloudy.wav','oblačno':'weather_cloudy.wav','kiša':'weather_rain.wav','pljuskovi':'weather_showers.wav','sneg':'weather_snow.wav','magla':'weather_fog.wav','grmljavina':'weather_thunder.wav','vetrovito':'weather_windy.wav'}
    return m.get(str(c or '').lower())
def weather_tokens(cfg):
    if not cfg.get('include_weather'):return []
    out=[]; t=cfg.get('manual_temperature_c'); c=cfg.get('manual_condition')
    if t is not None:
        v=max(-45,min(45,int(round(float(t))))); out+=['temperature_is.wav']
        if v<0:out+=['minus.wav']; v=abs(v)
        out += [f'number_{v:02d}.wav', 'degree_1.wav' if v%10==1 and v%100!=11 else ('degree_2_4.wav' if v%10 in (2,3,4) and v%100 not in (12,13,14) else 'degree_other.wav')]
    f=condition_file(c)
    if f:out.append(f)
    return out


def voice_manifest(pack):
    return read_json(pack/'manifest.json',{})



def _library_pack():
    cfg=ann_cfg()
    return VP/cfg.get('voice_pack','sr_female_01')

def _library_safe():
    st=read_json(ANN_STATE,{})
    if bool(st.get('ptt')): return False,'PTT_ACTIVE'
    if ann_worker and ann_worker.is_alive(): return False,'AUDIO_BUSY'
    state=str(st.get('state') or 'IDLE').upper()
    if state not in ('IDLE','READY'): return False,'ANNOUNCER_BUSY_'+state
    return True,None

def _manifest_backup(pack,man,tag='library'):
    bdir=pack/'manifest-backups'; bdir.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    p=bdir/f'manifest.{tag}.{stamp}.json'
    p.write_text(json.dumps(man,ensure_ascii=False,indent=2),encoding='utf-8')
    return p

def _sample_slug(text):
    x=str(text or '').strip().lower()
    for a,b in [('đ','dj'),('č','c'),('ć','c'),('š','s'),('ž','z')]: x=x.replace(a,b)
    x=re.sub(r'[^a-z0-9]+','_',x).strip('_')
    return (x[:52].rstrip('_') or 'sample')

def sample_library_snapshot():
    pack=_library_pack(); man=voice_manifest(pack); rows=[]
    for row in man.get('phrases',[]):
        try: idx=int(row.get('index') or 0)
        except Exception: idx=0
        fn=str(row.get('file') or '').strip(); p=pack/fn
        rows.append({
          'index':idx,'text':str(row.get('text') or '').strip(),'file':fn,
          'category':str(row.get('category') or sample_category(idx,str(row.get('text') or ''))),
          'managed':str(row.get('managed_by') or '')=='SAMPLE_LIBRARY_V1',
          'exists':bool(fn and p.exists()),'size_kb':round(p.stat().st_size/1024,1) if fn and p.exists() else None
        })
    rows.sort(key=lambda x:(x['index'],x['text']))
    cats=sorted(set(r['category'] for r in rows if r['category']))
    next_index=max([r['index'] for r in rows]+[0])+1
    return {'ok':True,'voice_pack':pack.name,'path':str(pack),'count':len(rows),'next_index':next_index,'categories':cats,'samples':rows}

def sample_library_add(data):
    ok,err=_library_safe()
    if not ok:return {'ok':False,'error':err}
    text=str(data.get('text') or '').strip(); category=str(data.get('category') or '').strip() or 'Kompletne gotove poruke'
    b64=str(data.get('wav_base64') or '')
    if not text:return {'ok':False,'error':'TEXT_REQUIRED'}
    if not b64:return {'ok':False,'error':'WAV_REQUIRED'}
    pack=_library_pack(); man=voice_manifest(pack); phrases=list(man.get('phrases') or [])
    if any(str(r.get('text') or '').strip().casefold()==text.casefold() for r in phrases):return {'ok':False,'error':'DUPLICATE_TEXT'}
    try: raw=base64.b64decode(b64,validate=True)
    except Exception:return {'ok':False,'error':'INVALID_BASE64'}
    if len(raw)>10*1024*1024:return {'ok':False,'error':'WAV_TOO_LARGE_MAX_10MB'}
    used=[]
    for r in phrases:
        try:used.append(int(r.get('index') or 0))
        except Exception:pass
    idx=max(used+[0])+1
    slug=_sample_slug(text)
    while True:
        fn=f'phrase_{idx}_{slug}.wav'
        if not (pack/fn).exists() and not any(str(r.get('file') or '')==fn for r in phrases):break
        idx+=1
    tmp=pack/(fn+f'.{os.getpid()}.uploadtmp'); final=pack/fn
    try:
        tmp.write_bytes(raw)
        valid,why=validate_fragment(tmp)
        if not valid:
            try:tmp.unlink()
            except Exception:pass
            return {'ok':False,'error':why}
        _manifest_backup(pack,man,'before-add')
        os.replace(tmp,final)
        newrow={'index':idx,'file':fn,'text':text,'category':category,'managed_by':'SAMPLE_LIBRARY_V1'}
        phrases.append(newrow); man['phrases']=phrases
        try: atomic_json(pack/'manifest.json',man)
        except Exception:
            try: final.unlink()
            except Exception:pass
            raise
        return {'ok':True,'sample':newrow,**sample_library_snapshot()}
    except Exception as e:
        try:
            if tmp.exists():tmp.unlink()
        except Exception:pass
        return {'ok':False,'error':str(e)}

def sample_library_edit(data):
    ok,err=_library_safe()
    if not ok:return {'ok':False,'error':err}
    try:idx=int(data.get('index'))
    except Exception:return {'ok':False,'error':'INVALID_INDEX'}
    category=str(data.get('category') or '').strip()
    text=str(data.get('text') or '').strip()
    if not category:return {'ok':False,'error':'CATEGORY_REQUIRED'}
    pack=_library_pack(); man=voice_manifest(pack); phrases=list(man.get('phrases') or [])
    hit=None
    for r in phrases:
        try:
            if int(r.get('index') or 0)==idx:hit=r;break
        except Exception:pass
    if hit is None:return {'ok':False,'error':'SAMPLE_NOT_FOUND'}
    managed=str(hit.get('managed_by') or '')=='SAMPLE_LIBRARY_V1'
    # Legacy samples may change category only; their text may be used by dynamic resolver logic.
    if managed:
        if not text:return {'ok':False,'error':'TEXT_REQUIRED'}
        if any(r is not hit and str(r.get('text') or '').strip().casefold()==text.casefold() for r in phrases):return {'ok':False,'error':'DUPLICATE_TEXT'}
        hit['text']=text
    hit['category']=category
    _manifest_backup(pack,man,'before-edit')
    atomic_json(pack/'manifest.json',man)
    return {'ok':True,'managed':managed,**sample_library_snapshot()}

def sample_library_delete(data):
    ok,err=_library_safe()
    if not ok:return {'ok':False,'error':err}
    try:idx=int(data.get('index'))
    except Exception:return {'ok':False,'error':'INVALID_INDEX'}
    pack=_library_pack(); man=voice_manifest(pack); phrases=list(man.get('phrases') or [])
    hit=None
    for r in phrases:
        try:
            if int(r.get('index') or 0)==idx:hit=r;break
        except Exception:pass
    if hit is None:return {'ok':False,'error':'SAMPLE_NOT_FOUND'}
    if str(hit.get('managed_by') or '')!='SAMPLE_LIBRARY_V1':return {'ok':False,'error':'LEGACY_SAMPLE_PROTECTED'}
    fn=str(hit.get('file') or ''); text=str(hit.get('text') or '')
    d=load_presets();refs=[]
    for pname,p in d.get('presets',{}).items():
        for b in p.get('blocks',[]):
            if str(b.get('file') or '')==fn or (str(b.get('type') or '').upper()=='PHRASE' and str(b.get('text') or '')==text):refs.append(pname);break
    if refs:return {'ok':False,'error':'SAMPLE_USED_BY_PRESET','presets':refs}
    _manifest_backup(pack,man,'before-delete')
    trash=pack/'library-trash'/datetime.now().strftime('%Y%m%d-%H%M%S-%f');trash.mkdir(parents=True,exist_ok=True)
    p=pack/fn
    if p.exists():os.replace(p,trash/fn)
    man['phrases']=[r for r in phrases if r is not hit]
    atomic_json(pack/'manifest.json',man)
    return {'ok':True,'trashed_to':str(trash),**sample_library_snapshot()}

def phrase_index(pack):
    idx={}
    for row in voice_manifest(pack).get('phrases',[]):
        text=str(row.get('text','')).strip()
        fn=str(row.get('file','')).strip()
        if text and fn:
            idx[text]=fn
    return idx

def default_weather():
    return {
      'source':'OPEN_METEO_FIXED',
      'location_name':'Košutnjak',
      'latitude':44.754,
      'longitude':20.421,
      'max_cache_minutes':180
    }

def default_presets():
    w=default_weather()
    return {
      'active_preset':'BIKEN sat i pozdrav',
      'presets':{
        'BIKEN sat i pozdrav':{
          'name':'BIKEN sat i pozdrav','weather':dict(w),
          'blocks':[{'type':'GREETING_AUTO'},{'type':'STATION_BIKEN'},{'type':'TIME_CURRENT'}]
        },
        'PMR 13 Obrenovac':{
          'name':'PMR 13 Obrenovac',
          'weather':{'source':'OPEN_METEO_FIXED','location_name':'Obrenovac','latitude':44.6549,'longitude':20.2002,'max_cache_minutes':180},
          'blocks':[{'type':'GREETING_AUTO'},{'type':'PHRASE','text':'Vi ste na kanalu PMR trinaest ekipe Obrenovac.'},{'type':'TIME_CURRENT'},{'type':'TEMPERATURE_CURRENT'},{'type':'WEATHER_CURRENT'},{'type':'GOODBYE_AUTO'}]
        },
        'PMR 10+ CRM Srbije':{
          'name':'PMR 10+ CRM Srbije','weather':dict(w),
          'blocks':[{'type':'GREETING_AUTO'},{'type':'PHRASE','text':'Vi ste na kanalu PMR deset plus Civilne radio mreže Srbije.'},{'type':'TIME_CURRENT'},{'type':'TEMPERATURE_CURRENT'},{'type':'WEATHER_CURRENT'},{'type':'GOODBYE_AUTO'}]
        },
        'SDR Košutnjak PMR 6':{
          'name':'SDR Košutnjak PMR 6','weather':dict(w),
          'blocks':[{'type':'GREETING_AUTO'},{'type':'PHRASE','text':'Ovo je SDR Košutnjak. Vi ste na kanalu PMR šest.'},{'type':'TIME_CURRENT'},{'type':'TEMPERATURE_CURRENT'},{'type':'WEATHER_CURRENT'},{'type':'SDR_GOODBYE_AUTO'}]
        }
      }
    }

def load_presets():
    d=read_json(PRESET_FILE,{})
    changed=False
    if not isinstance(d.get('presets'),dict) or not d.get('presets'):
        d=default_presets(); changed=True
    for p in d.get('presets',{}).values():
        if not isinstance(p.get('weather'),dict):
            p['weather']=default_weather(); changed=True
        else:
            for k,v in default_weather().items():
                if k not in p['weather']: p['weather'][k]=v; changed=True
    if d.get('active_preset') not in d['presets']:
        d['active_preset']=next(iter(d['presets'])); changed=True
    if changed: atomic_json(PRESET_FILE,d)
    return d

def save_presets(d):
    PRESETS.mkdir(parents=True,exist_ok=True)
    atomic_json(PRESET_FILE,d)

def active_preset():
    d=load_presets()
    return d['presets'][d['active_preset']]

def phrase_file(pack,text):
    return phrase_index(pack).get(text)

def weather_code_text(code,wind_kmh=0):
    c=int(code)
    if wind_kmh is not None and float(wind_kmh)>=45:
        return 'Vreme je vetrovito.'
    if c==0:return 'Vreme je vedro.'
    if c==1:return 'Vreme je pretežno vedro.'
    if c==2:return 'Vreme je delimično oblačno.'
    if c==3:return 'Vreme je oblačno.'
    if c in (45,48):return 'Ima magle.'
    if c in (51,53,55,56,57,61,63,66,67):return 'Pada kiša.'
    if c==65:return 'Pada jaka kiša.'
    if c in (71,73,77,85):return 'Pada sneg.'
    if c in (75,86):return 'Pada jak sneg.'
    if c in (80,81,82):return 'Mogući su pljuskovi.'
    if c in (95,96,99):return 'Moguća je grmljavina.'
    return 'Vreme je oblačno.'

def cache_age_minutes(cache):
    try:
        ts=datetime.fromisoformat(cache.get('fetched_at'))
        return max(0,(datetime.now().astimezone()-ts).total_seconds()/60)
    except Exception:return 10**9

def weather_coordinates(weather_cfg):
    source=str(weather_cfg.get('source','OFF')).upper()
    if source=='OPEN_METEO_GPS':
        try:
            g=legacy.get_gps_status_cached()
            if g.get('lock') and g.get('lat') is not None and g.get('lon') is not None:
                return float(g['lat']),float(g['lon']),str(weather_cfg.get('location_name') or 'GPS lokacija')
        except Exception:pass
        return None,None,'GPS lokacija'
    if source=='OPEN_METEO_FIXED':
        try:return float(weather_cfg['latitude']),float(weather_cfg['longitude']),str(weather_cfg.get('location_name') or 'Fiksna lokacija')
        except Exception:return None,None,str(weather_cfg.get('location_name') or 'Fiksna lokacija')
    return None,None,'Off'

def fetch_weather(weather_cfg,force=False):
    source=str(weather_cfg.get('source','OFF')).upper()
    if source=='OFF':
        return {'ok':False,'error':'WEATHER_OFF','source':'OFF'}
    lat,lon,name=weather_coordinates(weather_cfg)
    if lat is None or lon is None:
        return {'ok':False,'error':'WEATHER_LOCATION_UNAVAILABLE','source':source,'location_name':name}
    cache=read_json(WEATHER_CACHE,{})
    max_age=max(5,int(weather_cfg.get('max_cache_minutes',180)))
    same=(abs(float(cache.get('latitude',999))-lat)<0.0001 and abs(float(cache.get('longitude',999))-lon)<0.0001)
    if not force and cache.get('ok') and same and cache_age_minutes(cache)<=15:
        cache['age_minutes']=round(cache_age_minutes(cache),1);return cache
    params=urlencode({
      'latitude':lat,'longitude':lon,
      'current':'temperature_2m,weather_code,wind_speed_10m',
      'timezone':'auto','forecast_days':1
    })
    url='https://api.open-meteo.com/v1/forecast?'+params
    try:
        with urlopen(url,timeout=8) as r:
            data=json.loads(r.read().decode('utf-8'))
        cur=data.get('current') or {}
        temp=float(cur['temperature_2m']); code=int(cur['weather_code']); wind=float(cur.get('wind_speed_10m') or 0)
        result={
          'ok':True,'source':source,'provider':'Open-Meteo','location_name':name,
          'latitude':lat,'longitude':lon,'temperature_c':temp,'temperature_rounded_c':int(round(temp)),
          'weather_code':code,'condition_text':weather_code_text(code,wind),'wind_speed_kmh':wind,
          'observation_time':cur.get('time'),'fetched_at':now_iso(),'stale':False,'error':None
        }
        atomic_json(WEATHER_CACHE,result);result['age_minutes']=0.0;return result
    except Exception as e:
        if cache.get('ok') and same and cache_age_minutes(cache)<=max_age:
            cache['stale']=True;cache['error']='ONLINE_FETCH_FAILED_USING_CACHE: '+str(e);cache['age_minutes']=round(cache_age_minutes(cache),1);return cache
        return {'ok':False,'source':source,'location_name':name,'latitude':lat,'longitude':lon,'error':'ONLINE_FETCH_FAILED: '+str(e)}

def temp_tokens(pack,weather):
    if not weather.get('ok'):return []
    v=max(-45,min(45,int(weather.get('temperature_rounded_c',0))))
    idx=phrase_index(pack);out=[]
    intro=idx.get('Trenutna temperatura je.') or idx.get('Temperatura je.')
    if intro:out.append(intro)
    if v<0:
        f=idx.get('Minus.')
        if f:out.append(f)
    n=abs(v)
    names=['Nula','Jedan','Dva','Tri','Četiri','Pet','Šest','Sedam','Osam','Devet','Deset',
    'Jedanaest','Dvanaest','Trinaest','Četrnaest','Petnaest','Šesnaest','Sedamnaest',
    'Osamnaest','Devetnaest','Dvadeset','Dvadeset jedan','Dvadeset dva','Dvadeset tri',
    'Dvadeset četiri','Dvadeset pet','Dvadeset šest','Dvadeset sedam','Dvadeset osam',
    'Dvadeset devet','Trideset','Trideset jedan','Trideset dva','Trideset tri',
    'Trideset četiri','Trideset pet','Trideset šest','Trideset sedam','Trideset osam',
    'Trideset devet','Četrdeset','Četrdeset jedan','Četrdeset dva','Četrdeset tri',
    'Četrdeset četiri','Četrdeset pet']
    f=idx.get(names[n]+'.')
    if f:out.append(f)
    form='Stepen.' if n%10==1 and n%100!=11 else ('Stepena.' if n%10 in (2,3,4) and n%100 not in (12,13,14) else 'Stepeni.')
    f=idx.get(form)
    if f:out.append(f)
    return out

def weather_token(pack,weather):
    if not weather.get('ok'):return None
    return phrase_file(pack,str(weather.get('condition_text') or ''))

def goodbye_auto_text(hour):
    if 5<=hour<18:return 'Hvala na pažnji i prijatan dan.'
    if 18<=hour<23:return 'Hvala na pažnji i prijatno veče.'
    return 'Hvala na pažnji i prijatnu noć.'

def sdr_goodbye_auto_text(hour):
    if 5<=hour<18:return 'SDR Košutnjak vam želi prijatan dan.'
    if 18<=hour<23:return 'SDR Košutnjak vam želi prijatno veče.'
    return 'SDR Košutnjak vam želi prijatnu noć.'


# OBRENKA ROTATION V1
_OBRENKA_GREETING = {
    'morning': [
        ('phrase_208_dobro_jutro_dragi_obrenovcani_obrenka_je_uz_vas.wav', 'Dobro jutro, dragi Obrenovčani. Obrenka je uz vas.'),
        ('phrase_211_dragi_prijatelji_javlja_vam_se_obrenka.wav', 'Dragi prijatelji, javlja vam se Obrenka.'),
        ('phrase_212_jedan_veliki_pozdrav_za_obrenovac_i_sve_dobre_ljude_na_vezi.wav', 'Jedan veliki pozdrav za Obrenovac i sve dobre ljude na vezi.'),
        ('phrase_213_evo_mene_ponovo_da_vas_podsetim_koliko_je_sati.wav', 'Evo mene ponovo, da vas podsetim koliko je sati.'),
        ('phrase_214_da_ne_gledate_na_sat_obrenka_je_tu.wav', 'Da ne gledate na sat, Obrenka je tu.'),
        ('phrase_215_samo_na_trenutak_da_vam_se_javim.wav', 'Samo na trenutak da vam se javim.'),
        ('phrase_216_za_komsije_na_kanalu_trinaest_stizu_tacno_vreme_i_prognoza.wav', 'Za komšije na kanalu trinaest, stižu tačno vreme i prognoza.'),
    ],
    'day': [
        ('phrase_209_dobar_dan_komsije_pozdravlja_vas_vasa_obrenka.wav', 'Dobar dan, komšije. Pozdravlja vas vaša Obrenka.'),
        ('phrase_211_dragi_prijatelji_javlja_vam_se_obrenka.wav', 'Dragi prijatelji, javlja vam se Obrenka.'),
        ('phrase_212_jedan_veliki_pozdrav_za_obrenovac_i_sve_dobre_ljude_na_vezi.wav', 'Jedan veliki pozdrav za Obrenovac i sve dobre ljude na vezi.'),
        ('phrase_213_evo_mene_ponovo_da_vas_podsetim_koliko_je_sati.wav', 'Evo mene ponovo, da vas podsetim koliko je sati.'),
        ('phrase_214_da_ne_gledate_na_sat_obrenka_je_tu.wav', 'Da ne gledate na sat, Obrenka je tu.'),
        ('phrase_215_samo_na_trenutak_da_vam_se_javim.wav', 'Samo na trenutak da vam se javim.'),
        ('phrase_216_za_komsije_na_kanalu_trinaest_stizu_tacno_vreme_i_prognoza.wav', 'Za komšije na kanalu trinaest, stižu tačno vreme i prognoza.'),
    ],
    'evening': [
        ('phrase_210_dobro_vece_obrenovce_uz_vas_je_obrenka.wav', 'Dobro veče, Obrenovče. Uz vas je Obrenka.'),
        ('phrase_211_dragi_prijatelji_javlja_vam_se_obrenka.wav', 'Dragi prijatelji, javlja vam se Obrenka.'),
        ('phrase_212_jedan_veliki_pozdrav_za_obrenovac_i_sve_dobre_ljude_na_vezi.wav', 'Jedan veliki pozdrav za Obrenovac i sve dobre ljude na vezi.'),
        ('phrase_213_evo_mene_ponovo_da_vas_podsetim_koliko_je_sati.wav', 'Evo mene ponovo, da vas podsetim koliko je sati.'),
        ('phrase_214_da_ne_gledate_na_sat_obrenka_je_tu.wav', 'Da ne gledate na sat, Obrenka je tu.'),
        ('phrase_215_samo_na_trenutak_da_vam_se_javim.wav', 'Samo na trenutak da vam se javim.'),
        ('phrase_216_za_komsije_na_kanalu_trinaest_stizu_tacno_vreme_i_prognoza.wav', 'Za komšije na kanalu trinaest, stižu tačno vreme i prognoza.'),
    ],
}
_OBRENKA_GOODBYE = [
    ('phrase_217_cuvajte_se_i_budite_dobro_vasa_obrenka.wav', 'Čuvajte se i budite dobro. Vaša Obrenka.'),
    ('phrase_218_hvala_vam_sto_ste_bili_uz_mene_cujemo_se_na_sledeci_puni_sat.wav', 'Hvala vam što ste bili uz mene. Čujemo se na sledeći puni sat.'),
    ('phrase_219_budite_mi_vedri_zdravi_i_nasmejani.wav', 'Budite mi vedri, zdravi i nasmejani.'),
    ('phrase_220_hvala_na_paznji_komsije_obrenka_vas_srdacno_pozdravlja.wav', 'Hvala na pažnji, komšije. Obrenka vas srdačno pozdravlja.'),
    ('phrase_221_za_obrenovac_sa_osmehom_vasa_obrenka.wav', 'Za Obrenovac, sa osmehom. Vaša Obrenka.'),
    ('phrase_222_obrenka_kaze_osmeh_na_lice_i_polako_kroz_dan.wav', 'Obrenka kaže, osmeh na lice i polako kroz dan.'),
    ('phrase_223_komsije_cuvajte_jedni_druge_pozdravlja_vas_obrenka.wav', 'Komšije, čuvajte jedni druge. Pozdravlja vas Obrenka.'),
]

def _obrenka_rotation_pick(kind, dt):
    # Stable for a scheduled target, but changes every hour.
    # Preview and actual scheduled emission therefore select the same sample.
    slot = int(dt.timestamp() // 3600)
    if kind == 'greeting':
        group = 'morning' if 5 <= dt.hour < 12 else ('day' if 12 <= dt.hour < 18 else 'evening')
        pool = _OBRENKA_GREETING[group]
        return pool[slot % len(pool)]
    return _OBRENKA_GOODBYE[slot % len(_OBRENKA_GOODBYE)]


def resolve_blocks(blocks,pack,cfg,trigger='MANUAL',at=None,preset=None):
    dt=at or datetime.now().astimezone();hour=dt.hour;names=[];preview=[]
    idx=phrase_index(pack);preset=preset or active_preset()
    needs_weather=any(str(b.get('type','')).upper() in ('TEMPERATURE_CURRENT','WEATHER_CURRENT') for b in blocks)
    weather=fetch_weather(preset.get('weather') or default_weather()) if needs_weather else {'ok':False,'error':'NOT_REQUESTED'}
    for block in blocks:
        typ=str(block.get('type','')).upper()
        if typ=='CHIME':
            names.append('hourly_chime.wav');preview.append('[BIP]')
        elif typ=='GREETING_AUTO':
            fn=greeting_name(hour,{'greeting':'AUTO'})
            names.append(fn)
            preview.append('Dobro jutro.' if fn=='greeting_morning.wav' else ('Dobar dan.' if fn=='greeting_day.wav' else 'Dobro veče.'))
        elif typ=='OBRENKA_GREETING_RANDOM':
            fn,text=_obrenka_rotation_pick('greeting',dt)
            names.append(fn);preview.append(text)
        elif typ=='STATION_BIKEN':
            names.append('station_name.wav');preview.append('Ovo je radio automatika BIKEN.')
        elif typ=='TIME_CURRENT':
            names += ['time_is.wav',f'hour_{hour:02d}.wav']
            htexts=['Ponoć.','Jedan čas.','Dva časa.','Tri časa.','Četiri časa.','Pet časova.','Šest časova.','Sedam časova.','Osam časova.','Devet časova.','Deset časova.','Jedanaest časova.','Dvanaest časova.','Trinaest časova.','Četrnaest časova.','Petnaest časova.','Šesnaest časova.','Sedamnaest časova.','Osamnaest časova.','Devetnaest časova.','Dvadeset časova.','Dvadeset jedan čas.','Dvadeset dva časa.','Dvadeset tri časa.']
            preview += ['Tačno je.',htexts[hour]]
        elif typ=='TEMPERATURE_CURRENT':
            toks=temp_tokens(pack,weather);names+=toks
            if toks:preview.append('Trenutna temperatura je '+str(weather.get('temperature_rounded_c'))+' °C.')
            else:preview.append('[TEMPERATURA NIJE PODEŠENA]')
        elif typ=='WEATHER_CURRENT':
            fn=weather_token(pack,weather)
            if fn:names.append(fn);preview.append(str(weather.get('condition_text') or 'Vreme'))
            else:preview.append('[VREME NIJE PODEŠENO]')
        elif typ=='GOODBYE_AUTO':
            text=goodbye_auto_text(hour);fn=idx.get(text)
            if fn:names.append(fn);preview.append(text)
        elif typ=='OBRENKA_GOODBYE_RANDOM':
            fn,text=_obrenka_rotation_pick('goodbye',dt)
            names.append(fn);preview.append(text)
        elif typ=='SDR_GOODBYE_AUTO':
            text=sdr_goodbye_auto_text(hour);fn=idx.get(text)
            if fn:names.append(fn);preview.append(text)
        elif typ=='PHRASE':
            text=str(block.get('text','')).strip()
            fn=str(block.get('file','')).strip() or idx.get(text)
            if fn:names.append(fn);preview.append(text or fn)
        elif typ=='FILE':
            fn=str(block.get('file','')).strip()
            if fn:names.append(fn);preview.append(str(block.get('label') or fn))
    return names,preview

def sample_category(index,text):
    i=int(index or 0)
    if i<=5:return 'Pozdravi'
    if i<=12:return 'Identifikacija i uvodi'
    if i<=37:return 'Sati'
    if i<=90:return 'Brojevi i temperatura'
    if i<=110:return 'Vremenski uslovi'
    if i<=120:return 'Odjave'
    if i<=133:return 'Testne i servisne poruke'
    if i<=154:return 'PMR kanali i plus oznake'
    if i<=168:return 'Organizacije i SDR identifikacija'
    if i<=183:return 'Pojedinačne reči'
    return 'Kompletne gotove poruke'

def catalog(pack):
    out=[
      {'label':'Pozdrav – automatski','type':'GREETING_AUTO','category':'Dinamički blokovi'},
      {'label':'Obrenka – promenljivi pozdrav','type':'OBRENKA_GREETING_RANDOM','category':'Dinamički blokovi'},
      {'label':'Identifikacija – BIKEN','type':'STATION_BIKEN','category':'Dinamički blokovi'},
      {'label':'Tačno vreme – trenutno','type':'TIME_CURRENT','category':'Dinamički blokovi'},
      {'label':'Temperatura – trenutna','type':'TEMPERATURE_CURRENT','category':'Dinamički blokovi'},
      {'label':'Vremenski uslovi – trenutni','type':'WEATHER_CURRENT','category':'Dinamički blokovi'},
      {'label':'Odjava – automatska','type':'GOODBYE_AUTO','category':'Dinamički blokovi'},
      {'label':'Obrenka – promenljiva odjava','type':'OBRENKA_GOODBYE_RANDOM','category':'Dinamički blokovi'},
      {'label':'SDR Košutnjak odjava – automatska','type':'SDR_GOODBYE_AUTO','category':'Dinamički blokovi'},
      {'label':'Zvučni signal punog sata','type':'CHIME','category':'Dinamički blokovi'},
    ]
    for row in voice_manifest(pack).get('phrases',[]):
        text=str(row.get('text','')).strip(); fn=str(row.get('file','')).strip(); idx=int(row.get('index') or 0)
        if text and fn:
            out.append({'label':text,'type':'PHRASE','text':text,'file':fn,'index':idx,'category':str(row.get('category') or sample_category(idx,text))})
    return out

def preset_snapshot():
    cfg=ann_cfg();pack=VP/cfg.get('voice_pack','sr_female_01');d=load_presets()
    items=[]
    for name,p in d['presets'].items():
        preview=[]
        for b in p.get('blocks',[]):
            typ=str(b.get('type','')).upper()
            if typ=='TEMPERATURE_CURRENT':preview.append('[AKTUELNA TEMPERATURA]')
            elif typ=='WEATHER_CURRENT':preview.append('[AKTUELNO VREME]')
            else:preview.append(str(b.get('text') or b.get('label') or typ))
        items.append({'name':name,'active':name==d['active_preset'],'blocks':p.get('blocks',[]),'weather':p.get('weather') or default_weather(),'preview':' '.join(preview)})
    cat=catalog(pack)
    return {'active_preset':d['active_preset'],'presets':items,'catalog':cat,'catalog_count':len(cat),'categories':sorted(set(str(x.get('category') or 'Ostalo') for x in cat))}

def build_message(trigger='MANUAL',target_time=None):
    cfg=ann_cfg(); pack=VP/cfg.get('voice_pack','sr_female_01')
    preset=active_preset()
    target_dt=datetime.fromtimestamp(float(target_time)).astimezone() if target_time else None
    names,preview=resolve_blocks(preset.get('blocks',[]),pack,cfg,trigger,at=target_dt,preset=preset)
    if trigger=='SCHEDULE' and cfg.get('hourly_chime') and (not names or names[0]!='hourly_chime.wav'):
        names.insert(0,'hourly_chime.wav');preview.insert(0,'[BIP]')
    if not names:return {'ok':False,'error':'empty_message','preview':preview}
    parts=[]
    for n in names:
        p=pack/n; ok,err=validate_fragment(p)
        if not ok:return {'ok':False,'error':err,'components':names,'preview':preview}
        parts.append(p)
    with wave.open(str(FINAL_WAV),'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(48000)
        for i,p in enumerate(parts):
            with wave.open(str(p),'rb') as w:out.writeframes(w.readframes(w.getnframes()))
            if i < len(parts)-1:
                out.writeframes(b'\x00\x00'*int(0.08*48000))
    d=wav_info(FINAL_WAV)[4]
    return {'ok':True,'path':str(FINAL_WAV),'duration_s':d,'components':names,'preview':preview,'preset':preset.get('name')}

def log_tx(rec):
    LOGS.mkdir(parents=True,exist_ok=True)
    with (LOGS/'transmissions.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(rec,ensure_ascii=False)+'\n')


def parse_chrony_tracking():
    out={
      'ok':False,'reference_name':None,'ref_time':None,'ref_age_s':None,
      'system_time_offset_s':None,'rms_offset_s':None,
      'leap_status':None,'error':None
    }
    try:
        p=subprocess.run(['chronyc','tracking'],capture_output=True,text=True,timeout=2,check=False)
        if p.returncode!=0:
            out['error']='chronyc tracking rc='+str(p.returncode)
            return out
        fields={}
        for line in p.stdout.splitlines():
            if ':' in line:
                k,v=line.split(':',1)
                fields[k.strip()]=v.strip()

        ref=fields.get('Reference ID','')
        m=re.search(r'^([0-9A-Fa-f]+)\s+\(([^)]+)\)',ref)
        out['reference_name']=m.group(2) if m else (ref or None)
        out['leap_status']=fields.get('Leap status')

        st=fields.get('System time','')
        m=re.search(r'([+-]?[0-9.]+)\s+seconds\s+(fast|slow)',st)
        if m:
            val=float(m.group(1))
            out['system_time_offset_s']=val if m.group(2)=='fast' else -val

        rms=fields.get('RMS offset','')
        m=re.search(r'([+-]?[0-9.]+)\s+seconds',rms)
        if m:
            out['rms_offset_s']=float(m.group(1))

        rt=fields.get('Ref time (UTC)')
        if rt:
            try:
                dt=datetime.strptime(rt,'%a %b %d %H:%M:%S %Y').replace(tzinfo=timezone.utc)
                out['ref_time']=dt.isoformat()
                out['ref_age_s']=max(0.0,(datetime.now(timezone.utc)-dt).total_seconds())
            except Exception:
                out['ref_time']=rt

        out['ok']=True
        return out
    except Exception as e:
        out['error']=str(e)
        return out

def probe_kernel_pps():
    dev=Path('/dev/pps0')
    sys_assert=Path('/sys/class/pps/pps0/assert')
    out={
      'device':str(dev),'sysfs_assert':str(sys_assert),
      'exists':dev.exists(),'fresh':False,
      'pulse_epoch':None,'pulse_age_s':None,
      'sequence':None,'error':None
    }
    if not dev.exists():
        out['error']='PPS_DEVICE_MISSING'
        return out
    if not sys_assert.exists():
        out['error']='PPS_SYSFS_ASSERT_MISSING'
        return out
    try:
        raw=sys_assert.read_text(encoding='ascii',errors='replace').strip()
        # Typical format: 1784550258.999949123#3592
        m=re.search(r'([0-9]+\.[0-9]+)#([0-9]+)',raw)
        if not m:
            out['error']='PPS_ASSERT_PARSE_FAILED'
            out['raw']=raw
            return out
        ts=float(m.group(1))
        seq=int(m.group(2))
        age=max(0.0,time.time()-ts)
        out['pulse_epoch']=ts
        out['sequence']=seq
        out['pulse_age_s']=age
        out['fresh']=age<=2.5
        if not out['fresh']:
            out['error']='PPS_PULSE_STALE'
        return out
    except Exception as e:
        out['error']=str(e)
        return out

def timing_diagnostics(force=False):
    """
    Radio Announcer timing source.

    AUTO:
      - Prefer GPS/PPS when fresh.
      - Otherwise use synchronized system clock (chrony/NTP).

    GPS_PPS:
      - Require fresh kernel PPS and synchronized chrony clock.

    SYSTEM_NTP:
      - PPS/GPS not required.
      - Require synchronized system clock via chrony.

    This gate belongs to RADIO ANNOUNCER only.
    TDoA timing requirements remain in the locked legacy layer.
    """
    global pps_diag_cache

    nowm=time.monotonic()
    if not force and nowm-float(pps_diag_cache.get('checked_monotonic',0))<3.0:
        return pps_diag_cache.get('data') or {}

    cfg=ann_cfg()
    requested=str(cfg.get('time_source','AUTO') or 'AUTO').strip().upper()

    if requested not in ('AUTO','GPS_PPS','SYSTEM_NTP'):
        requested='AUTO'

    kernel=probe_kernel_pps()
    chrony=parse_chrony_tracking()

    warnings=[]
    reasons=[]

    # System clock is considered trustworthy when chrony itself is healthy,
    # leap status is Normal and reported system offset is <= 50 ms.
    chrony_ready=True

    if not chrony.get('ok'):
        chrony_ready=False
    else:
        if chrony.get('leap_status')!='Normal':
            chrony_ready=False

        off=chrony.get('system_time_offset_s')
        if off is None:
            chrony_ready=False
        else:
            try:
                if abs(float(off))>0.050:
                    chrony_ready=False
            except Exception:
                chrony_ready=False

    pps_ready=bool(kernel.get('exists') and kernel.get('fresh'))

    if requested=='GPS_PPS':
        selected='GPS_PPS'
    elif requested=='SYSTEM_NTP':
        selected='SYSTEM_NTP'
    else:
        selected='GPS_PPS' if (pps_ready and chrony_ready) else 'SYSTEM_NTP'

    if selected=='GPS_PPS':
        if not kernel.get('exists'):
            reasons.append('PPS_DEVICE_MISSING')
        elif not kernel.get('fresh'):
            reasons.append(kernel.get('error') or 'PPS_NOT_FRESH')

        if not chrony.get('ok'):
            reasons.append('CHRONY_UNAVAILABLE')
        else:
            if chrony.get('leap_status')!='Normal':
                reasons.append('CHRONY_NOT_SYNCHRONIZED')

            off=chrony.get('system_time_offset_s')
            if off is None:
                reasons.append('CHRONY_OFFSET_UNKNOWN')
            else:
                try:
                    if abs(float(off))>0.050:
                        reasons.append('SYSTEM_CLOCK_OFFSET_TOO_LARGE')
                except Exception:
                    reasons.append('CHRONY_OFFSET_INVALID')

            age=chrony.get('ref_age_s')
            if age is None:
                warnings.append('CHRONY_REF_AGE_UNKNOWN')
            elif float(age)>600.0:
                warnings.append('CHRONY_REFERENCE_STALE_BUT_KERNEL_PPS_FRESH')

    else:
        # SYSTEM_NTP: GPS/PPS is optional.
        if not chrony.get('ok'):
            reasons.append('CHRONY_UNAVAILABLE')
        else:
            if chrony.get('leap_status')!='Normal':
                reasons.append('SYSTEM_CLOCK_NOT_SYNCHRONIZED')

            off=chrony.get('system_time_offset_s')
            if off is None:
                reasons.append('SYSTEM_CLOCK_OFFSET_UNKNOWN')
            else:
                try:
                    if abs(float(off))>0.050:
                        reasons.append('SYSTEM_CLOCK_OFFSET_TOO_LARGE')
                except Exception:
                    reasons.append('SYSTEM_CLOCK_OFFSET_INVALID')

        if not pps_ready:
            warnings.append('GPS_PPS_NOT_AVAILABLE_USING_SYSTEM_CLOCK')

    data={
        'ready':not reasons,
        'status':'READY' if not reasons else 'BLOCKED',
        'requested_source':requested,
        'selected_source':selected,
        'reasons':reasons,
        'warnings':warnings,
        'kernel_pps':kernel,
        'chrony':chrony,
        'checked_at':now_iso()
    }

    pps_diag_cache={
        'checked_monotonic':nowm,
        'data':data
    }

    return data

def transmit(trigger='MANUAL',ptt_enabled=True,target_time=None):
    if mode()!=MODE_ANN:return {'ok':False,'error':'wrong_mode'}
    cfg=ann_cfg();max_late_s=10.0
    if trigger=='SCHEDULE':
        td=timing_diagnostics(force=True)
        if not td.get('ready'):
            reason=';'.join(td.get('reasons') or ['TIMING_NOT_READY'])
            set_ann(state='IDLE',ptt=False,error=reason,last_event='SCHEDULE_BLOCKED',timing=td)
            return {'ok':False,'error':reason,'timing':td}
        set_ann(error=None,last_event='SCHEDULE_TIMING_READY',timing=td)
    if ptt_enabled and not cfg.get('master_enable'):
        set_ann(state='IDLE',ptt=False,error='MASTER_ENABLE_REQUIRED',last_event='RF_TX_BLOCKED')
        return {'ok':False,'error':'MASTER_ENABLE_REQUIRED'}
    if trigger=='SCHEDULE' and not cfg.get('automation'):
        return {'ok':False,'error':'automation_disabled'}
    if not tx_lock.acquire(False):return {'ok':False,'error':'tx_busy'}
    txid='BIKEN-TX-'+datetime.now().strftime('%Y%m%d-%H%M%S')
    ptt_guard_stop=threading.Event()
    def late_skip(stage,b=None):
        if trigger!='SCHEDULE' or target_time is None:return None
        late=time.time()-float(target_time)
        if late<=max_late_s:return None
        try:legacy.ptt(False)
        except Exception:pass
        rec={'transmission_id':txid,'time':now_iso(),'trigger':trigger,'result':'SKIPPED_LATE','lateness_s':round(late,3),'stage':stage,'scheduled_target':datetime.fromtimestamp(float(target_time)).astimezone().isoformat()}
        if isinstance(b,dict):
            rec.update({'preset':b.get('preset'),'preview':b.get('preview'),'components':b.get('components')})
        log_tx(rec);set_ann(state='IDLE',voice_engine='IDLE',ptt=False,last_tx=rec,error=None,last_event='SCHEDULE_SKIPPED_LATE',scheduled_target=rec['scheduled_target'])
        return {'ok':False,'skipped':True,'reason':'SCHEDULE_TOO_LATE',**rec}
    try:
        stop_event.clear(); legacy.stop_continuous_audio(); legacy.stop_now(); legacy.ptt(False)
        if (r:=late_skip('BEFORE_BUILD')) is not None:return r
        set_ann(state='BUILDING_MESSAGE',voice_engine='BUILDING',error=None)
        b=build_message(trigger,target_time=target_time)
        if not b.get('ok'): set_ann(state='ERROR',voice_engine='ERROR',error=b.get('error')); return b
        if (r:=late_skip('AFTER_BUILD',b)) is not None:return r
        pre=float(cfg.get('pre_roll_s',1)); post=float(cfg.get('post_roll_s',.5)); max_tx=float(cfg.get('max_tx_s',60))
        # Load the completed WAV into a few MB of RAM while PTT is still OFF.
        # DSP itself is NOT pre-rendered: it runs block-by-block during playback.
        set_ann(state='PREPARING_STREAM',voice_engine='PREPARING')
        host,prepared,dsp=prepare_stream_audio(FINAL_WAV)
        dur=float(prepared.get('duration_s') or wav_info(FINAL_WAV)[4]);b['duration_s']=dur;b['audio_processing']=dsp
        if pre+dur+post>max_tx: raise RuntimeError('PTT timeout limit exceeded')
        if (r:=late_skip('AFTER_STREAM_PREPARE',b)) is not None:return r
        set_ann(state='READY',voice_engine='READY',message_length_s=round(dur,3),components=b['components'],audio_processing=dsp)
        if ptt_enabled:
            if target_time:
                target_ts=float(target_time);ptt_on_ts=target_ts-pre
                while time.time()<ptt_on_ts:
                    if stop_event.is_set():raise RuntimeError('stopped')
                    time.sleep(min(.05,max(.002,ptt_on_ts-time.time())))
                if (r:=late_skip('BEFORE_PTT',b)) is not None:return r
                legacy.ptt(True);set_ann(state='PRE_ROLL',ptt=True,scheduled_target=datetime.fromtimestamp(target_ts).astimezone().isoformat())
                def ptt_safety():
                    if not ptt_guard_stop.wait(max_tx):
                        try:legacy.ptt(False)
                        except Exception:pass
                        stop_event.set();set_ann(state='ERROR',ptt=False,error='PTT_SAFETY_TIMEOUT',last_event='PTT_SAFETY_TIMEOUT')
                threading.Thread(target=ptt_safety,daemon=True).start()
                while time.time()<target_ts:
                    if stop_event.is_set():raise RuntimeError('stopped')
                    time.sleep(min(.01,max(.001,target_ts-time.time())))
                if (r:=late_skip('BEFORE_AUDIO',b)) is not None:return r
            else:
                legacy.ptt(True);set_ann(state='PRE_ROLL',ptt=True)
                def ptt_safety():
                    if not ptt_guard_stop.wait(max_tx):
                        try:legacy.ptt(False)
                        except Exception:pass
                        stop_event.set();set_ann(state='ERROR',ptt=False,error='PTT_SAFETY_TIMEOUT',last_event='PTT_SAFETY_TIMEOUT')
                threading.Thread(target=ptt_safety,daemon=True).start()
                t=time.monotonic()
                while time.monotonic()-t<pre:
                    if stop_event.is_set():raise RuntimeError('stopped')
                    time.sleep(.02)
        set_ann(state='TRANSMITTING',ptt=ptt_enabled,voice_engine='PLAYING',actual_audio_start=now_iso())
        dsp_result=play_prepared_stream(host,prepared,cfg.get('audio_device','default'))
        b['audio_processing']=dsp_result
        if ptt_enabled:
            set_ann(state='POST_ROLL',ptt=True)
            t=time.monotonic()
            while time.monotonic()-t<post:
                if stop_event.is_set():break
                time.sleep(.02)
        legacy.ptt(False);ptt_guard_stop.set()
        rec={'transmission_id':txid,'time':now_iso(),'trigger':trigger,'duration_s':dur,'ptt':ptt_enabled,'preset':b.get('preset'),'preview':b.get('preview'),'components':b['components'],'audio_processing':b.get('audio_processing'),'scheduled_target':datetime.fromtimestamp(float(target_time)).astimezone().isoformat() if target_time else None,'result':'SUCCESS'}
        log_tx(rec)
        set_ann(state='IDLE',voice_engine='IDLE',ptt=False,last_tx=rec,error=None,last_event='SCHEDULE_SUCCESS' if trigger=='SCHEDULE' else f'{trigger}_SUCCESS',audio_processing=b.get('audio_processing'))
        return {'ok':True,**rec}
    except Exception as e:
        try:legacy.ptt(False)
        except Exception:pass
        ptt_guard_stop.set()
        rec={'transmission_id':txid,'time':now_iso(),'trigger':trigger,'result':'ERROR','error':str(e)};log_tx(rec);set_ann(state='ERROR',voice_engine='ERROR',ptt=False,last_tx=rec,error=str(e));return {'ok':False,'error':str(e)}
    finally:
        ptt_guard_stop.set()
        try:legacy.ptt(False)
        except Exception:pass
        tx_lock.release()

def play_single_sample(filename):
    if mode()!=MODE_ANN:return {'ok':False,'error':'wrong_mode'}
    if ann_worker and ann_worker.is_alive():return {'ok':False,'error':'audio_busy'}
    cfg=ann_cfg();pack=VP/cfg.get('voice_pack','sr_female_01');fn=Path(str(filename)).name;p=pack/fn
    ok,err=validate_fragment(p)
    if not ok:return {'ok':False,'error':err}
    def worker():
        set_ann(state='AUDIO_PREVIEW',voice_engine='PREPARING',ptt=False,error=None,last_event='SAMPLE_PREVIEW')
        try:
            host,prepared,dsp=prepare_stream_audio(p)
            set_ann(state='AUDIO_PREVIEW',voice_engine='PLAYING',ptt=False,error=None,audio_processing=dsp)
            r=play_prepared_stream(host,prepared,str(cfg.get('audio_device','default')))
            set_ann(state='IDLE',voice_engine='IDLE',ptt=False,error=None,last_event='SAMPLE_PREVIEW_SUCCESS',audio_processing=r)
        except Exception as e:
            set_ann(state='IDLE',voice_engine='ERROR',ptt=False,error=str(e),last_event='SAMPLE_PREVIEW_FAILED')
    threading.Thread(target=worker,daemon=True).start()
    return {'ok':True,'file':fn,'mode':'LIVE_STREAM'}

def request_full_test(confirmed=False):
    if not confirmed:
        set_ann(state='IDLE',ptt=False,error='FULL_TEST_CONFIRMATION_REQUIRED',last_event='RF_TX_BLOCKED')
        return {'ok':False,'error':'FULL_TEST_CONFIRMATION_REQUIRED'}
    set_ann(error=None,last_event='RF_FULL_TEST_CONFIRMED')
    return start_tx('FULL_TEST',True)

def start_tx(trigger='MANUAL',ptt=True,target_time=None):
    global ann_worker
    if manual_ptt_active:return {'ok':False,'error':'manual_ptt_active'}
    if ann_worker and ann_worker.is_alive():return {'ok':False,'error':'tx_busy'}
    ann_worker=threading.Thread(target=transmit,args=(trigger,ptt,target_time),daemon=True);ann_worker.start();return {'ok':True,'queued':trigger,'target_time':target_time}

def scheduled_hours(cfg):
    sm=cfg.get('schedule_mode','MANUAL_HOURS'); h=datetime.now().hour
    if sm=='EVERY_HOUR':return list(range(24))
    if sm=='EVERY_2_HOURS':return list(range(0,24,2))
    if sm=='EVERY_3_HOURS':return list(range(0,24,3))
    if sm=='EVERY_6_HOURS':return list(range(0,24,6))
    return sorted(set(int(x) for x in cfg.get('manual_hours',[]) if 0<=int(x)<=23))
def next_tx_time(cfg,now=None,grace_s=3.0,exclude_key=None):
    hs=scheduled_hours(cfg)
    now=now or datetime.now().astimezone()
    floor=now-timedelta(seconds=float(grace_s))
    for d in range(0,2):
        day=(now+timedelta(days=d)).date()
        for h in hs:
            dt=datetime.combine(day,datetime.min.time(),tzinfo=now.tzinfo).replace(hour=h)
            key=dt.strftime('%Y-%m-%d-%H')
            if dt>=floor and key!=exclude_key:
                return dt
    return None

def scheduler_loop():
    fired_key=None
    while not scheduler_stop.is_set():
        try:
            cfg=ann_cfg()
            enabled=bool(cfg.get('master_enable') and cfg.get('automation') and mode()==MODE_ANN)
            now=datetime.now().astimezone()
            nxt=next_tx_time(cfg,now=now,grace_s=3.0,exclude_key=fired_key) if enabled else None
            set_ann(automation=enabled,next_tx=nxt.isoformat() if nxt else None,scheduler_alive=True)
            if enabled and nxt:
                key=nxt.strftime('%Y-%m-%d-%H')
                seconds=(nxt-now).total_seconds()
                if -3.0 <= seconds <= 45.0 and fired_key!=key:
                    queued=start_tx('SCHEDULE',True,nxt.timestamp())
                    if queued.get('ok'):
                        fired_key=key
                        set_ann(last_event='SCHEDULE_QUEUED',error=None,scheduled_target=nxt.isoformat())
            time.sleep(.2)
        except Exception as e:
            set_ann(state='IDLE',ptt=False,scheduler_alive=False,last_event='SCHEDULER_ERROR',error='SCHEDULER: '+str(e))
            time.sleep(1.0)

def ensure_scheduler():
    global scheduler_thread
    if scheduler_thread and scheduler_thread.is_alive():return
    scheduler_stop.clear(); scheduler_thread=threading.Thread(target=scheduler_loop,daemon=True);scheduler_thread.start()

def combined_state():
    gps=legacy.get_gps_status_cached() if hasattr(legacy,'get_gps_status_cached') else {}
    try:tdoa=legacy.read_state()
    except Exception:tdoa={}
    if not gps and isinstance(tdoa,dict): gps=tdoa.get('gps') or {}
    cfg=ann_cfg(); s=ann_state(); pack=VP/cfg.get('voice_pack','sr_female_01'); missing=[]
    try:
        man=read_json(pack/'manifest.json',{}); missing=[x for x in man.get('required_core',[]) if not (pack/x).exists()]
    except Exception:missing=['manifest.json']
    weather=read_json(WEATHER_CACHE,{})
    if weather:weather['age_minutes']=round(cache_age_minutes(weather),1)
    timing=timing_diagnostics()
    return {'ok':True,'version':'BIKEN V4.0 FINAL LOCKED','active_mode':mode(),'announcer':s,'announcer_config':cfg,'tdoa':tdoa,'tdoa_config':legacy.config,'gps':gps,'pps':bool(timing.get('kernel_pps',{}).get('fresh')),'timing':timing,'voice_pack':{'path':str(pack),'missing_required':missing,'ready':not missing},'presets':preset_snapshot(),'weather':weather,'audio_plugins':plugin_state()}


def legacy_page():
    bar = """<style>
#bikenV4ModeBar{position:fixed;right:14px;top:14px;z-index:99999;background:#111c27;border:1px solid #355267;border-radius:10px;padding:8px 10px;color:#e7f3fa;font-family:Arial,sans-serif;box-shadow:0 4px 18px #0008}
#bikenV4ModeBar button{border:1px solid #4b687d;border-radius:7px;padding:7px 10px;margin-left:6px;background:#183249;color:white;cursor:pointer}
#bikenV4ModeBar button.active{border-color:#2de38a;box-shadow:0 0 10px #2de38a66}
</style><div id="bikenV4ModeBar"><b>OPERATING MODE</b><button class="active">TDoA BIKEN</button><button onclick="fetch('/api/v4/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active_mode:'RADIO_ANNOUNCER'})}).then(()=>location.reload())">RADIO ANNOUNCER</button></div>"""
    return legacy.HTML.replace('<body>', '<body>'+bar, 1)

ANN_HTML="""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>BIKEN V4.0</title><style>
:root{--bg:#06101a;--panel:#0d1b28;--panel2:#0a1722;--line:#244359;--line2:#315a73;--text:#e8f2f7;--muted:#87a4b8;--cyan:#49c7ef;--green:#26e58a;--yellow:#ffd34e;--red:#ff5364;--shadow:0 18px 55px rgba(0,0,0,.28)}
*{box-sizing:border-box}html{background:var(--bg)}body{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Arial,sans-serif;background:radial-gradient(circle at 50% -10%,#12304a 0,#08131f 32%,#050d15 75%);color:var(--text);margin:0;min-height:100vh}.wrap{max-width:1320px;margin:auto;padding:18px}.top,.card{background:linear-gradient(145deg,rgba(17,37,53,.96),rgba(9,24,36,.96));border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:14px;box-shadow:var(--shadow)}
.top{padding:24px 28px}.brand{display:flex;justify-content:space-between;align-items:flex-start;gap:18px}.brand h1{font-size:30px;line-height:1.15;margin:0 0 7px;letter-spacing:.1px}.subtitle{color:#9cb2c1;font-size:14px}.radio-mark{color:var(--green);font-weight:700;letter-spacing:1px;font-size:12px;text-align:center}.radio-mark .icon{font-size:30px;line-height:1}.modes{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:22px}.mode{position:relative;padding:18px 20px;border:1px solid var(--line2);border-radius:11px;cursor:pointer;background:linear-gradient(145deg,#102230,#0b1823);transition:.18s ease}.mode:hover{transform:translateY(-1px);border-color:#4a7894}.mode b{font-size:16px}.mode small{display:block;color:#a7bbc8;margin-top:4px}.mode.active{border-color:var(--green);box-shadow:0 0 0 1px rgba(38,229,138,.18),0 0 24px rgba(38,229,138,.18);background:linear-gradient(145deg,#102a2a,#0b1d22)}.mode.active:after{content:'ACTIVE';position:absolute;right:14px;top:13px;font-size:10px;font-weight:800;color:#9affca;background:#153d2d;border:1px solid #27764f;border-radius:999px;padding:4px 8px}.mode-icon{display:inline-flex;width:34px;height:34px;align-items:center;justify-content:center;margin-right:11px;border-radius:9px;color:var(--cyan);background:#0a2332;border:1px solid #24526b}.mode.active .mode-icon{color:var(--green);border-color:#24784f;background:#0c2d21}
.section-title{display:flex;align-items:center;gap:10px;margin:0 0 16px;font-size:20px}.section-title .sicon{color:var(--cyan);font-size:23px}.collapse-toggle{margin-left:auto;display:inline-flex;align-items:center;justify-content:center;width:34px;height:30px;min-height:30px;padding:0;border:1px solid #315a73;border-radius:8px;background:linear-gradient(180deg,#10283a,#0b1d2a);color:#aee8ff;font-size:18px;line-height:1;box-shadow:none}.collapse-toggle:hover{border-color:var(--cyan);filter:brightness(1.12)}.card.is-collapsed .section-title{margin-bottom:0}.card-collapse-body.is-collapsed{display:none}.status-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.status-grid>div{min-height:94px;padding:14px 16px;border-radius:10px;border:1px solid #244154;background:linear-gradient(145deg,#102332,#0a1722)}.status-tile{display:flex;flex-direction:column;justify-content:flex-start;height:100%}.status-head{display:flex;align-items:center;gap:12px;min-height:34px}.status-copy{display:flex;align-items:center;min-width:0}.status-icon{display:inline-flex;align-items:center;justify-content:center;flex:0 0 32px;width:32px;height:32px;border-radius:9px;border:1px solid #315166;background:linear-gradient(180deg,#102231,#0a1620);color:#7fd3ff;box-shadow:inset 0 0 0 1px rgba(255,255,255,.025)}.status-icon svg{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:1.85;stroke-linecap:round;stroke-linejoin:round;vector-effect:non-scaling-stroke}.status-icon.tone-green{color:var(--green);border-color:#265c46;background:linear-gradient(180deg,#0d2b20,#091a14)}.status-icon.tone-cyan{color:#7fd3ff;border-color:#355264;background:linear-gradient(180deg,#102231,#0a1620)}.status-icon.tone-yellow{color:var(--yellow);border-color:#6f5a25;background:linear-gradient(180deg,#2b2412,#1a150b)}.status-icon.tone-red{color:#ff8f99;border-color:#6a3340;background:linear-gradient(180deg,#2a171b,#190e11)}.status-icon.tone-neutral{color:#c7d2db;border-color:#365062;background:linear-gradient(180deg,#11212d,#0b151d)}.label{font-size:11px;color:var(--muted);letter-spacing:.45px;text-transform:uppercase;line-height:1.1}.value{font-size:17px;font-weight:800;margin-top:12px;overflow-wrap:anywhere;line-height:1.22}.green{color:var(--green)}.red{color:var(--red)}.yellow{color:var(--yellow)}
.form-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.field{display:flex;flex-direction:column;gap:7px}.field>span,.field-label{font-size:12px;color:#bcd0dc;letter-spacing:.25px;text-transform:uppercase}input,select{appearance:auto;background:#07141e;color:#f4f8fb;border:1px solid #31536a;border-radius:7px;padding:10px 11px;width:100%;min-height:40px;outline:none}input:focus,select:focus{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(73,199,239,.1)}button{font:inherit;background:linear-gradient(180deg,#163750,#112d43);color:white;border:1px solid #356887;border-radius:8px;padding:10px 14px;cursor:pointer;transition:.15s ease}button:hover{filter:brightness(1.12);transform:translateY(-1px)}button:active{transform:none}button.danger{background:linear-gradient(180deg,#7a2732,#5e1c26);border-color:#ab4653}.btn-green{background:linear-gradient(180deg,#115a3b,#0d452f);border-color:#25875c;color:#caffdf}.save-row{display:flex;justify-content:flex-end;margin-top:14px}
.builder-shell{display:grid;grid-template-columns:minmax(330px,.9fr) minmax(440px,1.3fr);gap:14px}.builder-panel{border:1px solid #244459;border-radius:11px;background:linear-gradient(145deg,#0d202e,#091620);padding:14px}.builder-left .form-grid{grid-template-columns:1fr 1fr}.catalog-title,.sequence-title{font-size:13px;font-weight:800;color:#d9e7ef;letter-spacing:.35px;text-transform:uppercase;margin:16px 0 9px}.catalog-layout{display:grid;grid-template-columns:1fr 145px;gap:10px;align-items:stretch}#block_catalog{min-height:260px;padding:7px}#block_catalog option{padding:6px}.catalog-actions{display:flex;flex-direction:column;gap:9px}.catalog-actions button{margin:0;min-height:54px}.weather-test{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:12px}.weather-test .label{text-transform:none}.builder-row{display:grid;grid-template-columns:1fr auto auto auto;gap:7px;align-items:center;padding:11px 10px;border:1px solid #29495e;border-radius:8px;margin:7px 0;background:#081722}.builder-row button{padding:7px 10px;margin:0}.builder-row button:last-child{font-size:11px}.catalog-info{margin-top:14px;padding:12px 14px;border:1px solid #6d5827;border-radius:9px 9px 0 0;background:#171b18;color:var(--yellow);font-size:12px;font-weight:800}.preview-wrap{border:1px solid #6d5827;border-top:0;border-radius:0 0 9px 9px;padding:13px;background:#0a1721}.preview-wrap .field-label{margin-bottom:8px}.preview-box{padding:13px;border:1px solid #31546a;border-radius:8px;background:#06131d;min-height:62px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;line-height:1.45;overflow-wrap:anywhere}

.sample-lib-grid{display:grid;grid-template-columns:minmax(310px,.75fr) minmax(520px,1.25fr);gap:14px}.sample-lib-box{border:1px solid #29495e;border-radius:11px;background:linear-gradient(145deg,#0d202e,#091620);padding:14px}.sample-lib-box h3{margin:0 0 12px;font-size:14px;color:#d9e7ef}.sample-lib-toolbar{display:grid;grid-template-columns:1fr 220px;gap:10px;margin-bottom:10px}.sample-lib-scroll{max-height:340px;overflow:auto;border:1px solid #29495e;border-radius:8px;background:#06131d}.sample-table{width:100%;border-collapse:collapse;font-size:12px}.sample-table th,.sample-table td{padding:8px 9px;border-bottom:1px solid #1e3545;text-align:left;vertical-align:top}.sample-table th{position:sticky;top:0;background:#102331;color:#bcd0dc;z-index:1}.sample-table tr{cursor:pointer}.sample-table tr:hover,.sample-table tr.selected{background:#10283a}.sample-table .mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;color:#9ec2d6}.sample-note{margin-top:10px;color:#8facbf;font-size:12px;line-height:1.45}.sample-editor-actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px}.sample-editor-actions button{min-width:120px}.sample-count{color:var(--yellow);font-weight:800}

.actions{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.actions.secondary{grid-template-columns:1.05fr 1fr 1fr .9fr .9fr .9fr;margin-top:10px}.actions button{min-height:48px}.btn-ptt{background:linear-gradient(180deg,#6b5511,#4c3c0b);border-color:#a88b2d;color:#fff2a8}.btn-ptt.ptt-live{background:linear-gradient(180deg,#8a6810,#664b08);border-color:#ffd34e;box-shadow:0 0 0 1px rgba(255,211,78,.22),0 0 18px rgba(255,211,78,.18)}.full-confirm{display:none;margin-top:14px;padding:14px;border:1px solid #f0ad4e;border-radius:8px;background:#2a2115}.full-warning{font-weight:700;color:#ffd166;margin-bottom:10px}
.plugin-rack{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.plugin-box{border:1px solid #29495e;border-radius:11px;background:linear-gradient(145deg,#0d202e,#091620);padding:14px}.plugin-box h3{margin:0 0 12px;font-size:14px;color:#d9e7ef}.plugin-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px}.plugin-head select{width:auto;min-width:90px}.slider-row{display:grid;grid-template-columns:128px 1fr 76px;gap:10px;align-items:center;margin:9px 0}.slider-row input[type=range]{padding:0;min-height:24px}.slider-value{text-align:right;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;color:#dff5ff}.plugin-status{padding:10px 12px;border:1px solid #31546a;border-radius:8px;background:#06131d;margin-top:12px;color:#a9c1d0;font-size:12px}.plugin-actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px}.plugin-actions button{min-width:140px}
@media(max-width:980px){.sample-lib-grid{grid-template-columns:1fr}.status-grid{grid-template-columns:repeat(2,1fr)}.form-grid{grid-template-columns:repeat(2,1fr)}.builder-shell{grid-template-columns:1fr}.actions,.actions.secondary{grid-template-columns:repeat(2,1fr)}.plugin-rack{grid-template-columns:1fr}}
@media(max-width:620px){.wrap{padding:9px}.top,.card{padding:13px;border-radius:11px}.brand h1{font-size:23px}.radio-mark{display:none}.modes{grid-template-columns:1fr}.status-grid{grid-template-columns:1fr 1fr}.status-grid>div{min-height:76px;padding:12px}.value{font-size:15px}.form-grid,.builder-left .form-grid{grid-template-columns:1fr}.catalog-layout{grid-template-columns:1fr}.catalog-actions{display:grid;grid-template-columns:1fr 1fr}.actions,.actions.secondary{grid-template-columns:1fr 1fr}.mode.active:after{position:static;display:inline-block;margin-left:9px}.builder-panel{padding:10px}}
</style></head><body><div class='wrap'>
<div class='top'><div class='brand'><div><h1>BIKEN V4.0 – Dual Mode Automation Platform</h1><div class='subtitle'>Final Locked &nbsp;•&nbsp; Radio Automation Control</div></div><div class='radio-mark'><div class='icon'>⌁</div>RADIO</div></div><div class='modes'><div class='mode' onclick="setMode('TDOA_BIKEN')"><span class='mode-icon'>◎</span><b>TDoA BIKEN</b><small>Original V3.7 FINAL LOCKED panel</small></div><div class='mode active'><span class='mode-icon'>〽</span><b>RADIO ANNOUNCER</b><small>Offline WAV automation</small></div></div></div>
<div class='card'><h2 class='section-title'><span class='sicon'>⌁</span>System Status</h2><div class='status-grid' id='status'></div></div>
<div class='card'><h2 class='section-title'><span class='sicon'>⌁</span>Radio Announcer</h2><div class='form-grid'>
<label class='field'><span>MASTER ENABLE</span><select id='master_enable'><option value='false'>OFF</option><option value='true'>ON</option></select></label>
<label class='field'><span>AUTOMATION</span><select id='automation'><option value='false'>OFF</option><option value='true'>ON</option></select></label>
<label class='field'><span>INTERVAL</span><select id='schedule_mode'><option>EVERY_HOUR</option><option>EVERY_2_HOURS</option><option>EVERY_3_HOURS</option><option>EVERY_6_HOURS</option><option>MANUAL_HOURS</option></select></label>
<label class='field'><span>MANUAL HOURS</span><input id='manual_hours' placeholder='8,10,12,14,16,18,20,22'></label>
<label class='field'><span>VOICE PACK</span><input id='voice_pack' value='sr_female_01' readonly></label>
<label class='field'><span>HOURLY CHIME</span><select id='hourly_chime'><option value='true'>ON</option><option value='false'>OFF</option></select></label>
</div><div class='save-row'><button onclick='saveAnn()'>▣ &nbsp; SAVE AUTOMATION</button></div></div>
<div class='card'><h2 class='section-title'><span class='sicon'>◫</span>Audio Plugin Rack</h2>
<div class='plugin-rack'>
<div class='plugin-box'><div class='plugin-head'><h3>PLUGIN HOST</h3><select id='plugin_master'><option value='false'>BYPASS</option><option value='true'>ON</option></select></div>
<div class='slider-row'><span>ALSA Output Gain</span><input id='alsa_gain' type='range' min='-20' max='4' step='0.5' oninput="rangeOut('alsa_gain','alsa_gain_v',' dB')"><span id='alsa_gain_v' class='slider-value'>—</span></div>
<label class='field' style='margin-top:10px'><span>DSP PRESET</span><select id='audio_preset_select'><option value=''>— SELECT —</option></select></label>
<div class='plugin-actions'><button onclick='loadAudioPreset()'>LOAD PRESET</button><button onclick='saveCustomAudioPreset()'>SAVE CUSTOM</button><button class='danger' onclick='deleteCustomAudioPreset()'>DELETE CUSTOM</button></div>
<div class='plugin-status'>Host: <b id='plugin_host_status'>—</b><br>PCM: <span id='pcm_actual'>—</span><br>Last DSP: <span id='plugin_last'>—</span></div>
<div class='plugin-actions'><button onclick='savePlugins()' class='btn-green'>▣ SAVE AUDIO</button><button onclick='bypassPlugins()'>BYPASS ALL</button><button onclick='resetPlugins()'>RESET FLAT</button></div></div>
<div class='plugin-box'><div class='plugin-head'><h3>EQUALIZER</h3><select id='eq_enabled'><option value='false'>OFF</option><option value='true'>ON</option></select></div>
<div class='slider-row'><span>High-pass</span><input id='eq_hpf' type='range' min='60' max='300' step='10' oninput="rangeOut('eq_hpf','eq_hpf_v',' Hz')"><span id='eq_hpf_v' class='slider-value'>—</span></div>
<div class='slider-row'><span>Bass 200 Hz</span><input id='eq_bass' type='range' min='-12' max='12' step='0.5' oninput="rangeOut('eq_bass','eq_bass_v',' dB')"><span id='eq_bass_v' class='slider-value'>—</span></div>
<div class='slider-row'><span>Low-mid 500 Hz</span><input id='eq_lowmid' type='range' min='-12' max='12' step='0.5' oninput="rangeOut('eq_lowmid','eq_lowmid_v',' dB')"><span id='eq_lowmid_v' class='slider-value'>—</span></div>
<div class='slider-row'><span>Presence 2.2 kHz</span><input id='eq_presence' type='range' min='-12' max='12' step='0.5' oninput="rangeOut('eq_presence','eq_presence_v',' dB')"><span id='eq_presence_v' class='slider-value'>—</span></div>
<div class='slider-row'><span>Treble 3.5 kHz</span><input id='eq_treble' type='range' min='-12' max='12' step='0.5' oninput="rangeOut('eq_treble','eq_treble_v',' dB')"><span id='eq_treble_v' class='slider-value'>—</span></div>
<div class='slider-row'><span>Low-pass</span><input id='eq_lpf' type='range' min='3000' max='12000' step='250' oninput="rangeOut('eq_lpf','eq_lpf_v',' Hz')"><span id='eq_lpf_v' class='slider-value'>—</span></div></div>
<div class='plugin-box'><div class='plugin-head'><h3>COMPRESSOR + SAFETY</h3><select id='comp_enabled'><option value='false'>OFF</option><option value='true'>ON</option></select></div>
<div class='slider-row'><span>Threshold</span><input id='comp_threshold' type='range' min='-40' max='0' step='1' oninput="rangeOut('comp_threshold','comp_threshold_v',' dBFS')"><span id='comp_threshold_v' class='slider-value'>—</span></div>
<div class='slider-row'><span>Ratio</span><input id='comp_ratio' type='range' min='1' max='10' step='0.5' oninput="rangeOut('comp_ratio','comp_ratio_v',':1')"><span id='comp_ratio_v' class='slider-value'>—</span></div>
<div class='slider-row'><span>Attack</span><input id='comp_attack' type='range' min='1' max='100' step='1' oninput="rangeOut('comp_attack','comp_attack_v',' ms')"><span id='comp_attack_v' class='slider-value'>—</span></div>
<div class='slider-row'><span>Release</span><input id='comp_release' type='range' min='20' max='1000' step='10' oninput="rangeOut('comp_release','comp_release_v',' ms')"><span id='comp_release_v' class='slider-value'>—</span></div>
<div class='slider-row'><span>Make-up</span><input id='comp_makeup' type='range' min='0' max='12' step='0.5' oninput="rangeOut('comp_makeup','comp_makeup_v',' dB')"><span id='comp_makeup_v' class='slider-value'>—</span></div>
<label class='field'><span>SOFT KNEE</span><select id='comp_knee'><option value='true'>ON</option><option value='false'>OFF</option></select></label>
<label class='field' style='margin-top:10px'><span>SAFETY LIMITER</span><select id='limiter_enabled'><option value='true'>ON</option><option value='false'>OFF</option></select></label>
<div class='slider-row'><span>Ceiling</span><input id='limiter_ceiling' type='range' min='-6' max='-0.2' step='0.1' oninput="rangeOut('limiter_ceiling','limiter_ceiling_v',' dBFS')"><span id='limiter_ceiling_v' class='slider-value'>—</span></div>
<div class='plugin-actions'><button onclick="get('/api/announcer/audio_test')">▶ PREVIEW ACTIVE PRESET</button></div></div>
</div></div>

<div class='card'><h2 class='section-title'><span class='sicon'>♫</span>Voice Pack / Sample Library</h2>
<div class='sample-lib-grid'>
<div class='sample-lib-box'><h3>ADD NEW SAMPLE</h3>
<div class='form-grid' style='grid-template-columns:1fr 1fr'>
<label class='field'><span>WAV FILE</span><input id='lib_file' type='file' accept='.wav,audio/wav'></label>
<label class='field'><span>NEXT INDEX</span><input id='lib_next_index' readonly value='—'></label>
<label class='field' style='grid-column:1/-1'><span>TEXT / TRANSCRIPT</span><input id='lib_text' placeholder='Tačan tekst koji sempl izgovara'></label>
<label class='field'><span>EXISTING GROUP</span><select id='lib_category_select'><option value=''>— SELECT —</option></select></label>
<label class='field'><span>NEW / CUSTOM GROUP</span><input id='lib_category_new' placeholder='Ako je novo, upiši naziv grupe'></label>
</div>
<div class='sample-editor-actions'><button class='btn-green' onclick='addLibrarySample()'>＋ ADD TO LIBRARY</button><button onclick='clearLibraryAdd()'>CLEAR</button></div>
<div class='sample-note'>Prihvaća samo <b>mono PCM16 / 48 kHz WAV</b>. Novi uzorak dobija sledeći slobodan <code>phrase_N_...</code> broj. Originalni/legacy semplovi su zaštićeni od brisanja i promene teksta.</div>
</div>
<div class='sample-lib-box'><h3>LIBRARY — <span id='lib_count' class='sample-count'>0</span> SAMPLES</h3>
<div class='sample-lib-toolbar'><input id='lib_search' placeholder='Pretraga teksta ili fajla...' oninput='renderSampleLibrary()'><select id='lib_filter_category' onchange='renderSampleLibrary()'><option value='ALL'>ALL GROUPS</option></select></div>
<div class='sample-lib-scroll'><table class='sample-table'><thead><tr><th>#</th><th>TEXT</th><th>GROUP</th><th>FILE</th></tr></thead><tbody id='lib_rows'></tbody></table></div>
<div class='form-grid' style='grid-template-columns:110px 1fr 1fr;margin-top:12px'>
<label class='field'><span>INDEX</span><input id='lib_edit_index' readonly value='—'></label>
<label class='field'><span>TEXT</span><input id='lib_edit_text' placeholder='Izaberi sempl iz liste'></label>
<label class='field'><span>GROUP</span><input id='lib_edit_category' placeholder='Grupa'></label>
</div>
<div class='sample-editor-actions'><button onclick='playLibrarySelected()'>▶ PLAY — RPi + BROWSER</button><button class='btn-green' onclick='saveLibraryMeta()'>▣ SAVE META</button><button class='danger' id='lib_delete_btn' onclick='deleteLibrarySelected()'>REMOVE</button></div>
<div id='browser_preview_status' class='sample-note'>Preview reprodukuje isti sempl na RPi audio izlazu i u ovom browseru. PTT ostaje OFF.</div>
<div id='lib_selected_info' class='sample-note'>Izaberi sempl iz liste. Za legacy semplove možeš menjati grupu; tekst i brisanje su zaključani radi kompatibilnosti.</div>
</div></div></div>
<div class='card'><h2 class='section-title'><span class='sicon'>▱</span>Message Builder &amp; Presets</h2><div class='builder-shell'>
<div class='builder-panel builder-left'><div class='form-grid'>
<label class='field'><span>PRESET</span><select id='preset_select' onchange='loadSelectedPreset()'></select></label>
<label class='field'><span>PRESET NAME</span><input id='preset_name' placeholder='Naziv preseta'></label>
<label class='field'><span>SAMPLE CATEGORY</span><select id='sample_category' onchange='renderCatalog()'></select></label>
<label class='field'><span>SEARCH SAMPLES</span><input id='sample_search' placeholder='Ukucaj reč ili deo rečenice...' oninput='renderCatalog()'></label>
<label class='field'><span>WEATHER SOURCE</span><select id='weather_source'><option value='OFF'>OFF</option><option value='OPEN_METEO_FIXED'>OPEN-METEO – FIXED LOCATION</option><option value='OPEN_METEO_GPS'>OPEN-METEO – DEVICE GPS</option></select></label>
<label class='field'><span>WEATHER LOCATION</span><input id='weather_location_name' placeholder='Košutnjak'></label>
<label class='field'><span>LATITUDE</span><input id='weather_latitude' type='number' step='0.000001'></label>
<label class='field'><span>LONGITUDE</span><input id='weather_longitude' type='number' step='0.000001'></label>
</div><div class='catalog-title'>Available Samples / Blocks</div><div class='catalog-layout'><select id='block_catalog' size='8'></select><div class='catalog-actions'><button class='btn-green' onclick='addCatalogBlock()'>＋ ADD SELECTED SAMPLE</button><button onclick='previewSelectedSample()'>▶ PLAY — RPi + BROWSER</button></div></div><div class='weather-test'><button onclick='testWeather()'>☁ &nbsp; TEST WEATHER SOURCE</button><span id='weather_test_result' class='label'></span></div></div>
<div class='builder-panel'><div class='sequence-title'>Current Message Sequence</div><div id='builder_rows'></div><div class='label' style='margin-top:12px;text-transform:none'>Use arrows to reorder items</div></div>
</div><div class='catalog-info'>▤ &nbsp; VOICE PACK CATALOG: <span id='catalog_count'>0</span> AVAILABLE ITEMS</div><div class='preview-wrap'><div class='field-label'>PREVIEW TEXT</div><div id='preview_text' class='preview-box'></div></div></div>
<div class='card'><h2 class='section-title'><span class='sicon'>ϟ</span>Actions</h2><div class='actions'><button onclick='savePreset()'>▣ &nbsp; SAVE PRESET</button><button onclick='duplicatePreset()'>▣ &nbsp; DUPLICATE</button><button onclick='activatePreset()'>◉ &nbsp; SET ACTIVE</button><button class='danger' onclick='deletePreset()'>⌫ &nbsp; DELETE PRESET</button></div><div class='actions secondary'><button id='btn_tx' class='btn-green' onclick="rfTransmitNow()">⌁ &nbsp; TRANSMIT NOW</button><button id='btn_audio' onclick="get('/api/announcer/audio_test')">◉ &nbsp; AUDIO PREVIEW</button><button id='btn_full' onclick="showFullTestConfirm()">⌁ &nbsp; FULL TEST</button><button id='btn_ptt_on' class='btn-ptt' onclick="manualPttOn()">● &nbsp; PTT ON</button><button class='danger' onclick="get('/api/v4/stop')">■ &nbsp; STOP</button><button id='btn_ptt_off' class='danger' onclick="manualPttOff()">⊘ &nbsp; PTT OFF</button></div>
<div id='full_test_confirm' class='full-confirm'><div class='full-warning'>FULL TEST ĆE UKLJUČITI PTT I EMITOVATI AKTIVNI PRESET.</div><button id='btn_full_confirm' style='border-color:#ffb347;color:#ffd166' onclick="confirmFullTest()">CONFIRM RF FULL TEST</button><button onclick="hideFullTestConfirm()">CANCEL</button></div></div>
<script>
let first=true;
const el=id=>document.getElementById(id);
function collapseKey(title,index){return 'biken.v4.collapse.'+String(title||('panel-'+index)).toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'')}
function setCardCollapsed(card,collapsed,persist=true){
 let body=card.querySelector(':scope > .card-collapse-body'),btn=card.querySelector(':scope > .section-title .collapse-toggle');
 if(!body||!btn)return;
 card.classList.toggle('is-collapsed',collapsed);body.classList.toggle('is-collapsed',collapsed);
 btn.textContent=collapsed?'▸':'▾';btn.title=collapsed?'Expand panel':'Collapse panel';btn.setAttribute('aria-expanded',collapsed?'false':'true');
 if(persist){try{localStorage.setItem(card.dataset.collapseKey,collapsed?'1':'0')}catch(e){}}
}
function initCollapsibleCards(){
 document.querySelectorAll('.card').forEach((card,index)=>{
  if(card.dataset.collapseReady==='1')return;
  let title=card.querySelector(':scope > .section-title');if(!title)return;
  let titleText=(title.textContent||'Panel').trim();
  let body=document.createElement('div');body.className='card-collapse-body';
  Array.from(card.children).slice(1).forEach(n=>body.appendChild(n));card.appendChild(body);
  let btn=document.createElement('button');btn.type='button';btn.className='collapse-toggle';btn.setAttribute('aria-label','Collapse or expand '+titleText);title.appendChild(btn);
  card.dataset.collapseKey=collapseKey(titleText,index);card.dataset.collapseReady='1';
  let collapsed=false;try{collapsed=localStorage.getItem(card.dataset.collapseKey)==='1'}catch(e){}
  setCardCollapsed(card,collapsed,false);
  btn.addEventListener('click',ev=>{ev.stopPropagation();setCardCollapsed(card,!card.classList.contains('is-collapsed'))});
  title.addEventListener('dblclick',()=>setCardCollapsed(card,!card.classList.contains('is-collapsed')));
 });
}

async function api(p,o){let r=await fetch(p,o);if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}
async function get(p){await api(p);setTimeout(refresh,200)}
async function setMode(m){await api('/api/v4/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active_mode:m})});location.reload()}
async function rfTransmitNow(){
 if(el('master_enable').value!=='true'){alert('MASTER ENABLE mora biti ON pre RF emisije.');return}
 if(!confirm('Pokrenuti stvarnu RF emisiju preko PTT-a?'))return;
 await get('/api/announcer/transmit_now')
}
function showFullTestConfirm(){
 if(el('master_enable').value!=='true'){
   alert('MASTER ENABLE mora biti ON pre RF FULL TEST-a.');
   return;
 }
 el('full_test_confirm').style.display='block';
}
function hideFullTestConfirm(){
 el('full_test_confirm').style.display='none';
}
async function confirmFullTest(){
 let b=el('btn_full_confirm');
 b.disabled=true;
 b.textContent='STARTING...';
 try{
   await get('/api/announcer/full_test?confirm=YES');
 }finally{
   hideFullTestConfirm();
   b.disabled=false;
   b.textContent='CONFIRM RF FULL TEST';
 }
}

let pluginFirst=true;
function rangeOut(id,out,suffix){let e=el(id),o=el(out);if(e&&o)o.textContent=String(e.value)+suffix}
function setRange(id,value,out,suffix){let e=el(id);if(!e)return;e.value=value;rangeOut(id,out,suffix)}
async function loadPlugins(){
 try{
  let r=await api('/api/plugins/audio/state'),c=r.config||{},eq=c.eq||{},co=c.compressor||{},li=c.limiter||{};
  el('plugin_host_status').textContent=(r.status||'UNKNOWN')+(r.last_error?' / '+r.last_error:'');
  el('pcm_actual').textContent=(r.alsa&&r.alsa.db!==null&&r.alsa.db!==undefined)?(Number(r.alsa.db).toFixed(2)+' dB'):'—';
  el('plugin_last').textContent=r.last_process||'—';
  if(pluginFirst){
   el('plugin_master').value=String(!!c.master_enabled);el('eq_enabled').value=String(!!eq.enabled);el('comp_enabled').value=String(!!co.enabled);el('comp_knee').value=String(co.soft_knee!==false);el('limiter_enabled').value=String(li.enabled!==false);
   setRange('alsa_gain',c.output_gain_db===undefined?4:c.output_gain_db,'alsa_gain_v',' dB');setRange('eq_hpf',eq.hpf_hz||120,'eq_hpf_v',' Hz');setRange('eq_bass',eq.bass_db||0,'eq_bass_v',' dB');setRange('eq_lowmid',eq.lowmid_db||0,'eq_lowmid_v',' dB');setRange('eq_presence',eq.presence_db||0,'eq_presence_v',' dB');setRange('eq_treble',eq.treble_db||0,'eq_treble_v',' dB');setRange('eq_lpf',eq.lpf_hz||5000,'eq_lpf_v',' Hz');
   setRange('comp_threshold',co.threshold_db===undefined?-18:co.threshold_db,'comp_threshold_v',' dBFS');setRange('comp_ratio',co.ratio||2.5,'comp_ratio_v',':1');setRange('comp_attack',co.attack_ms||15,'comp_attack_v',' ms');setRange('comp_release',co.release_ms||180,'comp_release_v',' ms');setRange('comp_makeup',co.makeup_db||0,'comp_makeup_v',' dB');setRange('limiter_ceiling',li.ceiling_db===undefined?-1:li.ceiling_db,'limiter_ceiling_v',' dBFS');pluginFirst=false;
  }
 }catch(e){if(el('plugin_host_status'))el('plugin_host_status').textContent='ERROR: '+e.message}
}
async function savePlugins(){
 let b={master_enabled:el('plugin_master').value==='true',output_gain_db:Number(el('alsa_gain').value),eq:{enabled:el('eq_enabled').value==='true',hpf_hz:Number(el('eq_hpf').value),bass_db:Number(el('eq_bass').value),lowmid_db:Number(el('eq_lowmid').value),presence_db:Number(el('eq_presence').value),treble_db:Number(el('eq_treble').value),lpf_hz:Number(el('eq_lpf').value)},compressor:{enabled:el('comp_enabled').value==='true',threshold_db:Number(el('comp_threshold').value),ratio:Number(el('comp_ratio').value),attack_ms:Number(el('comp_attack').value),release_ms:Number(el('comp_release').value),makeup_db:Number(el('comp_makeup').value),soft_knee:el('comp_knee').value==='true'},limiter:{enabled:el('limiter_enabled').value==='true',ceiling_db:Number(el('limiter_ceiling').value)}};
 let r=await api('/api/plugins/audio/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});if(!r.ok){alert(r.error||'Audio plugin save failed');return false}pluginFirst=true;await loadPlugins();return true;
}
async function loadAudioPresetList(selectName){
 try{
  let r=await api('/api/plugins/audio/presets'),sel=el('audio_preset_select');if(!sel)return;
  sel.innerHTML='<option value="">— SELECT —</option>'+((r.presets||[]).map(p=>`<option value="${esc(p.name)}">[${esc(p.type)}] ${esc(p.name)}</option>`).join(''));
  if(selectName)sel.value=selectName;
 }catch(e){console.log('Audio preset list:',e)}
}
async function loadAudioPreset(){
 let name=el('audio_preset_select').value;if(!name){alert('Izaberi DSP preset.');return}
 let r=await api('/api/plugins/audio/preset/load',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});if(!r.ok){alert(r.error||'Preset load failed');return}
 pluginFirst=true;await loadPlugins();await loadAudioPresetList(name);
}
async function saveCustomAudioPreset(){
 let ok=await savePlugins();if(ok===false)return;
 let name=prompt('Naziv za CUSTOM DSP preset:','');if(!name)return;
 let r=await api('/api/plugins/audio/preset/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});if(!r.ok){alert(r.error||'Preset save failed');return}
 await loadAudioPresetList(r.name);
}
async function deleteCustomAudioPreset(){
 let name=el('audio_preset_select').value;if(!name){alert('Izaberi CUSTOM preset.');return}
 if(!confirm('Obrisati CUSTOM DSP preset '+name+'?'))return;
 let r=await api('/api/plugins/audio/preset/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});if(!r.ok){alert(r.error||'Može se brisati samo CUSTOM preset.');return}
 await loadAudioPresetList();
}
async function bypassPlugins(){let r=await api('/api/plugins/audio/bypass',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(!r.ok)alert(r.error||'Bypass failed');pluginFirst=true;await loadPlugins()}
async function resetPlugins(){if(!confirm('Vratiti Audio Plugin Rack na bezbedne početne vrednosti?'))return;let r=await api('/api/plugins/audio/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(!r.ok)alert(r.error||'Reset failed');pluginFirst=true;await loadPlugins()}



let sampleLibrary=null;
let selectedLibraryIndex=null;
async function loadSampleLibrary(keepSelection=true){
 try{
  let r=await api('/api/announcer/library');if(!r.ok)throw new Error(r.error||'Library load failed');sampleLibrary=r;
  el('lib_count').textContent=String(r.count||0);el('lib_next_index').value=String(r.next_index||'—');
  let opts=(r.categories||[]).map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');
  el('lib_category_select').innerHTML='<option value="">— SELECT —</option>'+opts;
  let f=el('lib_filter_category'),old=f.value;f.innerHTML='<option value="ALL">ALL GROUPS</option>'+opts;if([...f.options].some(o=>o.value===old))f.value=old;
  if(!keepSelection)selectedLibraryIndex=null;renderSampleLibrary();
  if(selectedLibraryIndex!==null){let row=(r.samples||[]).find(x=>Number(x.index)===Number(selectedLibraryIndex));if(row)selectLibrarySample(row.index);else selectedLibraryIndex=null}
 }catch(e){if(el('lib_selected_info'))el('lib_selected_info').textContent='ERROR: '+e.message}
}
function renderSampleLibrary(){
 if(!sampleLibrary)return;let q=(el('lib_search').value||'').trim().toLocaleLowerCase('sr'),cat=el('lib_filter_category').value||'ALL';
 let rows=(sampleLibrary.samples||[]).filter(x=>(cat==='ALL'||x.category===cat)&&(!q||(`${x.text} ${x.file} ${x.category}`).toLocaleLowerCase('sr').includes(q)));
 el('lib_rows').innerHTML=rows.map(x=>`<tr class="${Number(x.index)===Number(selectedLibraryIndex)?'selected':''}" onclick="selectLibrarySample(${Number(x.index)})"><td>${x.index}</td><td>${esc(x.text)}</td><td>${esc(x.category)}</td><td class='mono'>${esc(x.file)}${x.exists?'':' ⚠'}</td></tr>`).join('')||"<tr><td colspan='4' class='label'>No matching samples.</td></tr>";
}
function selectLibrarySample(idx){
 if(!sampleLibrary)return;let x=(sampleLibrary.samples||[]).find(r=>Number(r.index)===Number(idx));if(!x)return;selectedLibraryIndex=Number(idx);
 el('lib_edit_index').value=String(x.index);el('lib_edit_text').value=x.text;el('lib_edit_category').value=x.category;el('lib_edit_text').readOnly=!x.managed;el('lib_delete_btn').disabled=!x.managed;
 el('lib_selected_info').textContent=(x.managed?'Panel-managed sample — text/group can be edited and sample can be removed safely.':'Protected legacy sample — group can be changed; text and deletion are locked.')+' File: '+x.file+(x.size_kb!==null?' / '+x.size_kb+' kB':'');renderSampleLibrary();
}
function clearLibraryAdd(){el('lib_file').value='';el('lib_text').value='';el('lib_category_select').value='';el('lib_category_new').value=''}
function fileBase64(file){return new Promise((resolve,reject)=>{let r=new FileReader();r.onload=()=>resolve(String(r.result).split(',')[1]||'');r.onerror=()=>reject(r.error||new Error('File read failed'));r.readAsDataURL(file)})}
async function addLibrarySample(){
 let f=el('lib_file').files[0],text=(el('lib_text').value||'').trim(),category=(el('lib_category_new').value||'').trim()||el('lib_category_select').value;
 if(!f){alert('Izaberi WAV fajl.');return}if(!text){alert('Upiši tačan tekst sempla.');return}if(!category){alert('Izaberi ili upiši grupu.');return}if(f.size>10*1024*1024){alert('WAV je veći od 10 MB.');return}
 let b64=await fileBase64(f),r=await api('/api/announcer/library/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,category,wav_base64:b64})});
 if(!r.ok){alert('Dodavanje nije uspelo: '+(r.error||'unknown'));return}clearLibraryAdd();sampleLibrary=r;selectedLibraryIndex=Number(r.sample.index);await loadSampleLibrary();await reloadPresets();alert('Sample dodat: '+r.sample.file);
}
async function saveLibraryMeta(){
 if(selectedLibraryIndex===null){alert('Izaberi sempl.');return}let text=(el('lib_edit_text').value||'').trim(),category=(el('lib_edit_category').value||'').trim();if(!category){alert('Grupa je obavezna.');return}
 let r=await api('/api/announcer/library/edit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:selectedLibraryIndex,text,category})});if(!r.ok){alert('Čuvanje nije uspelo: '+(r.error||'unknown'));return}sampleLibrary=r;await loadSampleLibrary();await reloadPresets();
}
let browserSampleAudio=null;
function playSampleInBrowser(file){
 try{
  if(browserSampleAudio){try{browserSampleAudio.pause()}catch(e){} browserSampleAudio=null}
  let a=new Audio('/api/announcer/sample/audio?file='+encodeURIComponent(file)+'&_='+Date.now());
  a.preload='auto';browserSampleAudio=a;
  let st=el('browser_preview_status');if(st)st.textContent='Browser preview: loading…';
  a.onplaying=()=>{let x=el('browser_preview_status');if(x)x.textContent='Browser preview: PLAYING • RPi preview started in parallel';};
  a.onended=()=>{let x=el('browser_preview_status');if(x)x.textContent='Preview finished.';};
  a.onerror=()=>{let x=el('browser_preview_status');if(x)x.textContent='Browser preview failed; RPi preview may still be playing.';};
  let pr=a.play();if(pr&&pr.catch)pr.catch(e=>{let x=el('browser_preview_status');if(x)x.textContent='Browser blocked audio autoplay — click PLAY again or allow audio for this site.';});
 }catch(e){let x=el('browser_preview_status');if(x)x.textContent='Browser preview error: '+e.message}
}
async function playLibrarySelected(){
 if(selectedLibraryIndex===null||!sampleLibrary){alert('Izaberi sempl.');return}
 let x=(sampleLibrary.samples||[]).find(r=>Number(r.index)===Number(selectedLibraryIndex));if(!x)return;
 playSampleInBrowser(x.file);
 let r=await api('/api/announcer/sample/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:x.file})});
 if(!r.ok)alert(r.error||'RPi preview failed');
}
async function deleteLibrarySelected(){
 if(selectedLibraryIndex===null||!sampleLibrary){alert('Izaberi sempl.');return}let x=(sampleLibrary.samples||[]).find(r=>Number(r.index)===Number(selectedLibraryIndex));if(!x||!x.managed){alert('Legacy sempl je zaštićen.');return}if(!confirm('Ukloniti sample #'+x.index+' iz biblioteke? Fajl će biti premešten u library-trash, ne trajno obrisan.'))return;
 let r=await api('/api/announcer/library/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:selectedLibraryIndex})});if(!r.ok){alert('Brisanje nije uspelo: '+(r.error||'unknown')+(r.presets?' / Presets: '+r.presets.join(', '):''));return}selectedLibraryIndex=null;sampleLibrary=r;await loadSampleLibrary(false);await reloadPresets();
}

let presetData=null;
let builderBlocks=[];
function esc(s){return String(s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function iconSvg(name){
 const icons={
  mode:`<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M12 9v7'></path><path d='M9 20h6'></path><circle cx='12' cy='6' r='1.5'></circle><path d='M6.5 11.5a8 8 0 0 1 11 0'></path><path d='M9 14a4.5 4.5 0 0 1 6 0'></path></svg>`,
  automation:`<svg viewBox='0 0 24 24' aria-hidden='true'><circle cx='12' cy='12' r='2.8'></circle><path d='M19 12a1.5 1.5 0 0 0 1.2 1.47l.32.07v2.92l-.32.07A1.5 1.5 0 0 0 19 18l-.15.33-2.52 1.46-.22-.24a1.5 1.5 0 0 0-2.06-.18L13.8 19.6l-.6.27-1.4-.8-.13-.33a1.5 1.5 0 0 0-1.77-.93l-.35.08-2.05-2.06.08-.35a1.5 1.5 0 0 0-.93-1.77l-.33-.13-.8-1.4.27-.6.23-.25a1.5 1.5 0 0 0-.18-2.06l-.24-.22 1.46-2.52.33-.15A1.5 1.5 0 0 0 9.54 5.8l.07-.32h2.92l.07.32A1.5 1.5 0 0 0 14 7l.33.15 2.52 1.46-.24.22a1.5 1.5 0 0 0-.18 2.06l.23.25.27.6'></path></svg>`,
  voice:`<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M3.5 12h2.5l1.8-3.5 2.8 7 2.6-5 1.6 3h2.2'></path><path d='M19 9v6'></path></svg>`,
  ptt:`<svg viewBox='0 0 24 24' aria-hidden='true'><rect x='9' y='3' width='6' height='11' rx='3'></rect><path d='M6 11a6 6 0 0 0 12 0'></path><path d='M12 17v4'></path><path d='M9 21h6'></path></svg>`,
  gps:`<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M12 20s5.5-4.7 5.5-10a5.5 5.5 0 1 0-11 0c0 5.3 5.5 10 5.5 10z'></path><circle cx='12' cy='10' r='2'></circle></svg>`,
  pps:`<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M3 12h4l2-4 3 8 2.2-5H21'></path></svg>`,
  schedule:`<svg viewBox='0 0 24 24' aria-hidden='true'><rect x='4' y='5' width='16' height='15' rx='2'></rect><path d='M8 3v4'></path><path d='M16 3v4'></path><path d='M4 10h16'></path><path d='M9 14l2 2 4-4'></path></svg>`,
  weather:`<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M7 18a4 4 0 1 1 .9-7.9A5 5 0 0 1 17.4 12H18a3 3 0 0 1 0 6H7z'></path></svg>`,
  next:`<svg viewBox='0 0 24 24' aria-hidden='true'><circle cx='12' cy='12' r='8'></circle><path d='M12 8v4l3 2'></path></svg>`,
  last:`<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M8 8H4v4'></path><path d='M4.8 9.2A8 8 0 1 1 6 17.5'></path><path d='M12 8v4l2.6 1.6'></path></svg>`,
  length:`<svg viewBox='0 0 24 24' aria-hidden='true'><circle cx='12' cy='13' r='6'></circle><path d='M12 13l2.6-1.5'></path><path d='M9.5 3h5'></path><path d='M12 3v3'></path></svg>`,
  pack:`<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M4 14h2.8l1.7-3.5 2.8 7 2.1-4H16'></path><path d='M18 9v6'></path></svg>`,
  event:`<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M9 7h9'></path><path d='M9 12h9'></path><path d='M9 17h9'></path><circle cx='5.5' cy='7' r='1'></circle><circle cx='5.5' cy='12' r='1'></circle><circle cx='5.5' cy='17' r='1'></circle></svg>`,
  error:`<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M12 4.5l7 13H5l7-13z'></path><path d='M12 9v4'></path><path d='M12 15.8h.01'></path></svg>`
 };
 return icons[name]||icons.mode;
}
function statusCard(label,value,cls,icon,tone){
 return `<div class='status-tile'><div class='status-head'><span class='status-icon ${tone||'tone-cyan'}'>${iconSvg(icon)}</span><div class='status-copy'><div class='label'>${esc(label)}</div></div></div><div class="value ${cls||''}">${esc(value)}</div></div>`;
}
function blockLabel(b){
 let c=(presetData&&presetData.catalog)||[];
 let hit=c.find(x=>x.type===b.type && (!b.text||x.text===b.text));
 return hit?hit.label:(b.text||b.label||b.type||b.file);
}
function renderBuilder(){
 let box=el('builder_rows');
 box.innerHTML=builderBlocks.map((b,i)=>`<div class='builder-row'><div>${i+1}. ${esc(blockLabel(b))}</div><button onclick='moveBlock(${i},-1)'>↑</button><button onclick='moveBlock(${i},1)'>↓</button><button class='danger' onclick='removeBlock(${i})'>REMOVE</button></div>`).join('')||'<div class=label>No blocks. Add a block from the list.</div>';
 updateLocalPreview();
}
function updateLocalPreview(){
 let texts=builderBlocks.map(blockLabel);
 el('preview_text').textContent=texts.join(' ')||'—';
}
function moveBlock(i,d){let j=i+d;if(j<0||j>=builderBlocks.length)return;[builderBlocks[i],builderBlocks[j]]=[builderBlocks[j],builderBlocks[i]];renderBuilder()}
function removeBlock(i){builderBlocks.splice(i,1);renderBuilder()}
function renderCatalog(){
 let category=(el('sample_category')&&el('sample_category').value)||'ALL';
 let q=((el('sample_search')&&el('sample_search').value)||'').trim().toLocaleLowerCase('sr');
 let cat=el('block_catalog'),rows=[];
 (presetData.catalog||[]).forEach((x,i)=>{
   let xc=String(x.category||'Ostalo');
   let hay=(String(x.label||'')+' '+String(x.file||'')+' '+xc).toLocaleLowerCase('sr');
   if(category!=='ALL'&&xc!==category)return;
   if(q&&!hay.includes(q))return;
   rows.push(`<option value="${i}">[${esc(xc)}] ${esc(x.label)}</option>`);
 });
 cat.innerHTML=rows.join('');
 if(rows.length)cat.selectedIndex=0;
}
async function previewSelectedSample(){
 let sel=el('block_catalog');
 if(!sel||sel.value===''){alert('Izaberi sample.');return}
 let item=(presetData.catalog||[])[Number(sel.value)];
 if(!item||!item.file){alert('Dinamički blok nema jedan statički WAV. Dodaj ga u preset i koristi AUDIO PREVIEW.');return}
 playSampleInBrowser(item.file);
 let r=await api('/api/announcer/sample/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:item.file})});
 if(!r.ok)alert(r.error||'RPi sample play failed');
}
function addCatalogBlock(){
 let sel=el('block_catalog');
 if(!sel||sel.value===''){alert('Izaberi sample ili blok.');return}
 let item=(presetData.catalog||[])[Number(sel.value)];
 if(!item)return;
 let b={type:item.type};
 if(item.text)b.text=item.text;
 if(item.file)b.file=item.file;
 if(item.label)b.label=item.label;
 builderBlocks.push(b);renderBuilder();
}
function loadSelectedPreset(){
 let name=el('preset_select').value;
 let p=(presetData.presets||[]).find(x=>x.name===name);
 if(!p)return;
 el('preset_name').value=p.name;
 builderBlocks=JSON.parse(JSON.stringify(p.blocks||[]));
 let w=p.weather||{};
 el('weather_source').value=w.source||'OFF';
 el('weather_location_name').value=w.location_name||'';
 el('weather_latitude').value=w.latitude===undefined?'':w.latitude;
 el('weather_longitude').value=w.longitude===undefined?'':w.longitude;
 renderBuilder();
 el('preview_text').textContent=p.preview||'—';
}
async function reloadPresets(selectName){
 let r=await api('/api/announcer/presets');
 presetData=r;
 let ps=el('preset_select');
 ps.innerHTML=(r.presets||[]).map(p=>`<option value="${esc(p.name)}">${p.active?'★ ':''}${esc(p.name)}</option>`).join('');
 let sc=el('sample_category');
 sc.innerHTML='<option value="ALL">ALL CATEGORIES</option>'+((r.categories||[]).map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join(''));
 el('catalog_count').textContent=String(r.catalog_count||0);
 renderCatalog();
 ps.value=selectName||r.active_preset;
 loadSelectedPreset();
}
async function savePreset(){
 let name=el('preset_name').value.trim();
 if(!name){alert('Unesi naziv preseta.');return}
 if(!builderBlocks.length){alert('Preset mora imati bar jedan blok.');return}
 let r=await api('/api/announcer/preset/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,blocks:builderBlocks,weather:{
 source:el('weather_source').value,
 location_name:el('weather_location_name').value.trim(),
 latitude:el('weather_latitude').value===''?null:Number(el('weather_latitude').value),
 longitude:el('weather_longitude').value===''?null:Number(el('weather_longitude').value),
 max_cache_minutes:180
 }})});
 if(!r.ok){alert(r.error||'Save failed');return}
 await reloadPresets(name);
}
async function duplicatePreset(){
 let base=el('preset_name').value.trim()||'Preset';
 el('preset_name').value=base+' COPY';
}
async function activatePreset(){
 let name=el('preset_select').value;
 let r=await api('/api/announcer/preset/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
 if(!r.ok){alert(r.error||'Activate failed');return}
 await reloadPresets(name);refresh();
}
async function testWeather(){
 let name=el('preset_name').value.trim();
 if(name)await savePreset();
 let r=await api('/api/announcer/weather/test');
 let out=el('weather_test_result');
 if(r.ok)out.textContent=`${r.location_name}: ${Math.round(r.temperature_c)} °C, ${r.condition_text}`;
 else out.textContent=r.error||'WEATHER ERROR';
}
async function deletePreset(){
 let name=el('preset_select').value;
 if(!confirm('Obrisati preset '+name+'?'))return;
 let r=await api('/api/announcer/preset/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
 if(!r.ok){alert(r.error||'Delete failed');return}
 await reloadPresets();
}
function v(x){return x===null||x===undefined||x===''?'—':x}
async function saveAnn(){let b={master_enable:el('master_enable').value==='true',automation:el('automation').value==='true',schedule_mode:el('schedule_mode').value,manual_hours:el('manual_hours').value.split(',').map(x=>Number(x.trim())).filter(x=>Number.isInteger(x)&&x>=0&&x<=23),voice_pack:el('voice_pack').value,hourly_chime:el('hourly_chime').value==='true'};await api('/api/announcer/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});refresh()}
async function manualPttOn(){
 let r=await api('/api/v4/ptt/on');
 if(!r.ok)alert('PTT ON nije uključen: '+(r.error||'unknown error'));
 refresh();
}
async function manualPttOff(){
 let r=await api('/api/v4/ptt/off');
 if(!r.ok)alert('PTT OFF greška: '+(r.error||'unknown error'));
 refresh();
}
async function refresh(){
 try{
  let s=await api('/api/v4/state'),a=s.announcer||{},g=s.gps||{},vp=s.voice_pack||{missing_required:[]};
  loadPlugins();
  let gpsText=g.lock?'LOCKED':(g.available?'NO FIX':'OFFLINE');
  let gpsClass=g.lock?'green':(g.available?'yellow':'red');
  let activePreset=((s.presets&&s.presets.presets)||[]).find(p=>p.active)||{};
  let weatherCfg=activePreset.weather||{};
  let weatherUsed=(activePreset.blocks||[]).some(b=>['TEMPERATURE_CURRENT','WEATHER_CURRENT'].includes(b.type));
  let weatherEnabled=weatherUsed&&weatherCfg.source&&weatherCfg.source!=='OFF';
  let weather=s.weather&&s.weather.ok
    ? `${Math.round(s.weather.temperature_c)} °C / ${s.weather.condition_text}`
    : (weatherEnabled?'WAITING / ERROR':'OFF');
  let busy=['BUILDING_MESSAGE','READY','PRE_ROLL','TRANSMITTING','POST_ROLL'].includes(a.state);
  let manualPtt=(a.state==='MANUAL_PTT'&&a.ptt);
  for(let id of ['btn_tx','btn_audio','btn_full']){let b=el(id);if(b){b.disabled=busy||manualPtt;b.style.opacity=(busy||manualPtt)?'.55':'1'}}
  if(el('btn_ptt_on')){el('btn_ptt_on').disabled=busy||a.ptt;el('btn_ptt_on').style.opacity=(busy||a.ptt)?'.55':'1';el('btn_ptt_on').classList.toggle('ptt-live',manualPtt);}
  if(el('btn_tx'))el('btn_tx').style.borderColor=a.ptt?'#ffd166':'#42647d';
  if(el('btn_audio'))el('btn_audio').style.borderColor=(a.voice_engine==='PLAYING'&&!a.ptt)?'#ffd166':'#42647d';
  if(el('btn_full'))el('btn_full').style.borderColor=(a.state!=='IDLE'&&a.ptt)?'#ffd166':'#42647d';
  el('status').innerHTML=[
   ['ACTIVE MODE',s.active_mode,'green','mode','tone-green'],['AUTOMATION',a.automation?'ACTIVE':'OFF',a.automation?'green':'red','automation',a.automation?'tone-green':'tone-neutral'],
   ['VOICE ENGINE',v(a.voice_engine),a.voice_engine==='ERROR'?'red':(a.voice_engine==='PLAYING'?'yellow':'green'),'voice',a.voice_engine==='ERROR'?'tone-red':(a.voice_engine==='PLAYING'?'tone-yellow':'tone-cyan')],
   ['PTT',a.ptt?'ON':'OFF',a.ptt?'yellow':'green','ptt',a.ptt?'tone-yellow':'tone-neutral'],['GPS DATA',gpsText,gpsClass,'gps',g.lock?'tone-green':(g.available?'tone-yellow':'tone-neutral')],
   ['PPS PULSES',s.timing&&s.timing.kernel_pps&&s.timing.kernel_pps.fresh?'FRESH':'NOT FRESH',s.timing&&s.timing.kernel_pps&&s.timing.kernel_pps.fresh?'green':'red','pps',s.timing&&s.timing.kernel_pps&&s.timing.kernel_pps.fresh?'tone-green':'tone-red'],
   ['SCHEDULE READY',s.timing&&s.timing.ready?'YES':'NO',s.timing&&s.timing.ready?'green':'red','schedule',s.timing&&s.timing.ready?'tone-green':'tone-red'],
   ['WEATHER',weather,weatherEnabled&&s.weather&&s.weather.ok?'green':(weatherEnabled?'yellow':'red'),'weather',weatherEnabled&&s.weather&&s.weather.ok?'tone-cyan':(weatherEnabled?'tone-yellow':'tone-neutral')],
   ['NEXT TX',v(a.next_tx),'','next','tone-cyan'],['LAST TX',a.last_tx?v(a.last_tx.time):'—','','last','tone-cyan'],
   ['MESSAGE LENGTH',a.message_length_s?v(a.message_length_s)+' s':'—','','length','tone-cyan'],
   ['VOICE PACK',vp.ready?'READY':'MISSING '+(vp.missing_required||[]).length,vp.ready?'green':'red','pack',vp.ready?'tone-green':'tone-red'],
   ['LAST EVENT',v(a.last_event),'','event','tone-cyan'],['ERROR',v(a.error),a.error?'red':'green','error',a.error?'tone-red':'tone-neutral']
  ].map(x=>statusCard(x[0],x[1],x[2],x[3],x[4])).join('');
  if(first){let c=s.announcer_config;for(let k of ['master_enable','automation','schedule_mode','voice_pack','hourly_chime']){let e=el(k);if(e)e.value=c[k]===null?'':String(c[k])}el('manual_hours').value=(c.manual_hours||[]).join(',');await reloadPresets(s.presets?s.presets.active_preset:null);first=false}
 }catch(e){el('status').innerHTML=`<div><div class=label>ERROR</div><div class="value red">${e.message}</div></div>`}
}
initCollapsibleCards();setInterval(refresh,1000);refresh();loadAudioPresetList();loadSampleLibrary(false);
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def sendj(self,o):
        b=json.dumps(o,ensure_ascii=False,indent=2).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
    def sendhtml(self,h):
        b=h.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
    def send_sample_wav(self,filename):
        cfg=ann_cfg();pack=VP/cfg.get('voice_pack','sr_female_01');fn=Path(str(filename)).name;p=pack/fn
        ok,err=validate_fragment(p)
        if not ok:return self.send_error(404,err)
        b=p.read_bytes();self.send_response(200);self.send_header('Content-Type','audio/wav');self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store, max-age=0');self.send_header('Content-Disposition','inline; filename=\"'+fn.replace('\"','')+'\"');self.end_headers();self.wfile.write(b)
    def do_GET(self):
        u=urlparse(self.path);q=parse_qs(u.query)
        if u.path=='/': return self.sendhtml(legacy_page() if mode()==MODE_TDOA else ANN_HTML)
        if u.path=='/api/v4/state': return self.sendj(combined_state())
        if u.path=='/api/plugins/audio/state': return self.sendj(plugin_state())
        if u.path=='/api/plugins/audio/presets': return self.sendj(plugin_presets())
        if u.path=='/api/announcer/presets': return self.sendj({'ok':True,**preset_snapshot()})
        if u.path=='/api/announcer/library': return self.sendj(sample_library_snapshot())
        if u.path=='/api/announcer/sample/audio': return self.send_sample_wav(q.get('file',[''])[0])
        if u.path=='/api/announcer/weather/test': return self.sendj(fetch_weather(active_preset().get('weather') or default_weather(),force=True))
        if u.path=='/api/v4/timing': return self.sendj({'ok':True,**timing_diagnostics(force=True)})
        if u.path=='/api/v4/stop': hard_stop();stop_event.clear();scheduler_stop.clear();ensure_scheduler();return self.sendj({'ok':True})
        if u.path=='/api/v4/ptt/on': return self.sendj(manual_ptt_on())
        if u.path=='/api/v4/ptt/off': return self.sendj(manual_ptt_off())
        if u.path=='/api/state': return self.sendj(legacy.read_state() if mode()==MODE_TDOA else combined_state())
        if u.path in ('/api/start','/api/stop','/api/audio_test','/api/ptt_test','/api/ptt/on','/api/ptt/off','/api/audio_stream/start','/api/audio_stream/stop','/api/audio_stream/toggle'):
            if mode()!=MODE_TDOA: return self.sendj({'ok':False,'error':'wrong_mode'})
            if u.path=='/api/start': return self.sendj(legacy.start_async(float(q.get('duration',[legacy.config['default_duration_s']])[0]),'web'))
            if u.path=='/api/stop': return self.sendj(legacy.stop_now())
            if u.path=='/api/audio_test': return self.sendj(legacy.audio_test_async(float(q.get('duration',[5])[0])))
            if u.path=='/api/ptt_test': return self.sendj(legacy.ptt_test_async(float(q.get('seconds',[2])[0])))
            if u.path=='/api/ptt/on': return self.sendj(legacy.ptt_manual_on())
            if u.path=='/api/ptt/off': return self.sendj(legacy.ptt_manual_off())
            if u.path=='/api/audio_stream/start': return self.sendj(legacy.start_continuous_audio())
            if u.path=='/api/audio_stream/stop': return self.sendj(legacy.stop_continuous_audio())
            if u.path=='/api/audio_stream/toggle': return self.sendj(legacy.stop_continuous_audio() if legacy.is_continuous_audio_running() else legacy.start_continuous_audio())
        if u.path=='/api/announcer/transmit_now': return self.sendj(start_tx('MANUAL',True))
        if u.path=='/api/announcer/audio_test': return self.sendj(start_tx('AUDIO_TEST',False))
        if u.path=='/api/announcer/full_test': return self.sendj(request_full_test(q.get('confirm',[''])[0]=='YES'))
        self.send_error(404)
    def do_POST(self):
        n=int(self.headers.get('Content-Length','0'));body=self.rfile.read(n)
        try:data=json.loads(body or b'{}')
        except Exception as e:return self.sendj({'ok':False,'error':str(e)})
        if self.path=='/api/v4/mode': return self.sendj(switch_mode(data.get('active_mode')))
        if self.path=='/api/plugins/audio/config': return self.sendj(plugin_save_config(data))
        if self.path=='/api/plugins/audio/reset': return self.sendj(plugin_reset())
        if self.path=='/api/plugins/audio/bypass': return self.sendj(plugin_bypass_all())
        if self.path=='/api/plugins/audio/preset/load': return self.sendj(plugin_preset_load(data))
        if self.path=='/api/plugins/audio/preset/save': return self.sendj(plugin_preset_save(data))
        if self.path=='/api/plugins/audio/preset/delete': return self.sendj(plugin_preset_delete(data))
        if self.path=='/api/config':
            if mode()!=MODE_TDOA:return self.sendj({'ok':False,'error':'wrong_mode'})
            legacy.config.update(data);legacy.save_config(legacy.config);return self.sendj({'ok':True,'config':legacy.config})
        if self.path=='/api/announcer/config':
            c=ann_cfg();c.update(data);atomic_json(ANN_CFG,c);return self.sendj({'ok':True,'config':c})
        if self.path=='/api/announcer/library/add': return self.sendj(sample_library_add(data))
        if self.path=='/api/announcer/library/edit': return self.sendj(sample_library_edit(data))
        if self.path=='/api/announcer/library/delete': return self.sendj(sample_library_delete(data))
        if self.path=='/api/announcer/sample/play':
            return self.sendj(play_single_sample(data.get('file','')))
        if self.path=='/api/announcer/preset/save':
            name=str(data.get('name','')).strip();blocks=data.get('blocks')
            if not name or not isinstance(blocks,list) or not blocks:return self.sendj({'ok':False,'error':'invalid_preset'})
            weather=data.get('weather') if isinstance(data.get('weather'),dict) else default_weather()
            d=load_presets();d['presets'][name]={'name':name,'blocks':blocks,'weather':weather};save_presets(d)
            return self.sendj({'ok':True,'name':name})
        if self.path=='/api/announcer/preset/activate':
            name=str(data.get('name','')).strip();d=load_presets()
            if name not in d['presets']:return self.sendj({'ok':False,'error':'preset_not_found'})
            d['active_preset']=name;save_presets(d);set_ann(last_event='PRESET_ACTIVATED',error=None)
            return self.sendj({'ok':True,'active_preset':name})
        if self.path=='/api/announcer/preset/delete':
            name=str(data.get('name','')).strip();d=load_presets()
            if name not in d['presets']:return self.sendj({'ok':False,'error':'preset_not_found'})
            if len(d['presets'])<=1:return self.sendj({'ok':False,'error':'cannot_delete_last_preset'})
            del d['presets'][name]
            if d['active_preset']==name:d['active_preset']=next(iter(d['presets']))
            save_presets(d);return self.sendj({'ok':True,'active_preset':d['active_preset']})
        self.send_error(404)

def cleanup(*_):
    hard_stop('SERVICE_STOP')
    try:legacy.gpio_cleanup()
    except Exception:pass
    raise SystemExit(0)
def main():
    RUNTIME.mkdir(parents=True,exist_ok=True)
    LOGS.mkdir(parents=True,exist_ok=True)
    PRESETS.mkdir(parents=True,exist_ok=True)
    load_presets()
    load_legacy()
    signal.signal(signal.SIGTERM,cleanup)
    signal.signal(signal.SIGINT,cleanup)
    ensure_scheduler()
    set_ann(state='IDLE',voice_engine='IDLE',ptt=False,error=None,last_event='SERVICE_STARTED')
    threading.Timer(0.5, lambda: set_ann(last_event='SERVICE_STARTED',error=None)).start()
    ThreadingHTTPServer(('0.0.0.0',8891),H).serve_forever()
if __name__=='__main__':main()
