#!/usr/bin/env python3
import json, subprocess, wave, time, re, math
from pathlib import Path
try:
    import numpy as np
    NUMPY_OK=True
    NUMPY_VERSION=np.__version__
except Exception as e:
    np=None;NUMPY_OK=False;NUMPY_VERSION=None;NUMPY_ERROR=str(e)

class PluginHost:
    API_VERSION=1
    ENGINE='LIVE_STREAM_NUMPY_V1_5'
    SR=48000
    BLOCK_FRAMES=1920          # 40 ms at 48 kHz
    FIR_TAPS=513               # ~10.7 ms linear-phase FIR
    FIR_FFT=4096

    def __init__(self, app_dir):
        self.app=Path(app_dir);self.dir=self.app/'plugins';self.cfg_path=self.dir/'plugin_config.json';self.presets_path=self.dir/'audio_presets.json'
        self.last_error=None;self.last_process='never';self.modules={}
        if not self.cfg_path.exists():self._write_json(self.cfg_path,self.defaults())

    def defaults(self):
        return {'api_version':1,'master_enabled':False,'output_gain_db':4.0,'order':['audio_eq','compressor'],
          'eq':{'enabled':False,'hpf_hz':120.0,'bass_db':0.0,'lowmid_db':0.0,'presence_db':0.0,'treble_db':0.0,'lpf_hz':5000.0},
          'compressor':{'enabled':False,'threshold_db':-18.0,'ratio':2.5,'attack_ms':15.0,'release_ms':180.0,'makeup_db':0.0,'soft_knee':True},
          'limiter':{'enabled':True,'ceiling_db':-1.0}}

    def factory_presets(self):
        return {
          'OBRENKA NATURAL':{'master_enabled':True,'eq':{'enabled':True,'hpf_hz':100.0,'bass_db':0.0,'lowmid_db':0.0,'presence_db':0.5,'treble_db':0.0,'lpf_hz':6000.0},'compressor':{'enabled':True,'threshold_db':-16.0,'ratio':2.0,'attack_ms':20.0,'release_ms':220.0,'makeup_db':0.5,'soft_knee':True},'limiter':{'enabled':True,'ceiling_db':-1.0}},
          'OBRENKA BROADCAST':{'master_enabled':True,'eq':{'enabled':True,'hpf_hz':120.0,'bass_db':-1.0,'lowmid_db':0.5,'presence_db':1.5,'treble_db':0.5,'lpf_hz':5250.0},'compressor':{'enabled':True,'threshold_db':-18.0,'ratio':2.5,'attack_ms':15.0,'release_ms':180.0,'makeup_db':1.5,'soft_knee':True},'limiter':{'enabled':True,'ceiling_db':-1.0}},
          'OBRENKA PMR CLEAR':{'master_enabled':True,'eq':{'enabled':True,'hpf_hz':180.0,'bass_db':-2.0,'lowmid_db':-1.0,'presence_db':2.5,'treble_db':1.0,'lpf_hz':4250.0},'compressor':{'enabled':True,'threshold_db':-18.0,'ratio':3.0,'attack_ms':12.0,'release_ms':160.0,'makeup_db':1.5,'soft_knee':True},'limiter':{'enabled':True,'ceiling_db':-1.0}}
        }

    def _read_custom_presets(self):
        try:
            d=json.loads(self.presets_path.read_text(encoding='utf-8'));return d if isinstance(d,dict) else {}
        except Exception:return {}
    def preset_list(self):
        rows=[]
        for name in self.factory_presets():rows.append({'name':name,'type':'FACTORY','deletable':False})
        for name in sorted(self._read_custom_presets()):rows.append({'name':name,'type':'CUSTOM','deletable':True})
        return {'ok':True,'presets':rows}
    def _preset_payload(self,c):
        return {'master_enabled':bool(c.get('master_enabled',True)),'eq':dict(c.get('eq') or {}),'compressor':dict(c.get('compressor') or {}),'limiter':dict(c.get('limiter') or {})}
    def load_preset(self,name):
        name=str(name or '').strip();factory=self.factory_presets();custom=self._read_custom_presets();src=factory.get(name) if name in factory else custom.get(name)
        if not isinstance(src,dict):return {'ok':False,'error':'AUDIO_PRESET_NOT_FOUND'}
        current=self._read_cfg();gain=current.get('output_gain_db',4.0);current['master_enabled']=bool(src.get('master_enabled',True))
        for sec in ('eq','compressor','limiter'):
            if isinstance(src.get(sec),dict):current.setdefault(sec,{}).update(src[sec])
        current['output_gain_db']=gain;current=self._sanitize(current);self._write_json(self.cfg_path,current);self.last_error=None
        return {'ok':True,'name':name,'type':'FACTORY' if name in factory else 'CUSTOM','config':current}
    def save_custom_preset(self,name):
        name=' '.join(str(name or '').strip().split())
        if not name or len(name)>48:return {'ok':False,'error':'INVALID_AUDIO_PRESET_NAME'}
        if name.upper() in {x.upper() for x in self.factory_presets()}:return {'ok':False,'error':'FACTORY_PRESET_NAME_RESERVED'}
        d=self._read_custom_presets();d[name]=self._preset_payload(self._read_cfg());self._write_json(self.presets_path,d);return {'ok':True,'name':name,'type':'CUSTOM'}
    def delete_custom_preset(self,name):
        name=str(name or '').strip();d=self._read_custom_presets()
        if name not in d:return {'ok':False,'error':'CUSTOM_AUDIO_PRESET_NOT_FOUND'}
        del d[name];self._write_json(self.presets_path,d);return {'ok':True,'name':name}
    def _write_json(self,p,obj):
        p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_name(p.name+'.tmp');tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');tmp.replace(p)
    def _read_cfg(self):
        d=self.defaults()
        try:
            raw=json.loads(self.cfg_path.read_text(encoding='utf-8'))
            for k,v in raw.items():
                if isinstance(v,dict) and isinstance(d.get(k),dict):d[k].update(v)
                else:d[k]=v
        except Exception:pass
        return self._sanitize(d)
    def _sanitize(self,c):
        clamp=lambda v,a,b:max(a,min(b,float(v)));c['api_version']=1;c['master_enabled']=bool(c.get('master_enabled',False));c['output_gain_db']=clamp(c.get('output_gain_db',4),-20,4)
        e=c.setdefault('eq',{});e['enabled']=bool(e.get('enabled',False));e['hpf_hz']=clamp(e.get('hpf_hz',120),60,300);e['bass_db']=clamp(e.get('bass_db',0),-12,12);e['lowmid_db']=clamp(e.get('lowmid_db',0),-12,12);e['presence_db']=clamp(e.get('presence_db',0),-12,12);e['treble_db']=clamp(e.get('treble_db',0),-12,12);e['lpf_hz']=clamp(e.get('lpf_hz',5000),3000,12000)
        co=c.setdefault('compressor',{});co['enabled']=bool(co.get('enabled',False));co['threshold_db']=clamp(co.get('threshold_db',-18),-40,0);co['ratio']=clamp(co.get('ratio',2.5),1,10);co['attack_ms']=clamp(co.get('attack_ms',15),1,100);co['release_ms']=clamp(co.get('release_ms',180),20,1000);co['makeup_db']=clamp(co.get('makeup_db',0),0,12);co['soft_knee']=bool(co.get('soft_knee',True))
        li=c.setdefault('limiter',{});li['enabled']=bool(li.get('enabled',True));li['ceiling_db']=clamp(li.get('ceiling_db',-1),-6,-0.2);c['order']=['audio_eq','compressor'];return c
    def _alsa(self):
        out={'db':None,'raw':None,'ok':False}
        try:
            p=subprocess.run(['amixer','get','PCM'],capture_output=True,text=True,timeout=2,check=False);out['raw']=p.stdout.strip();vals=re.findall(r'\[([+-]?[0-9.]+)dB\]',p.stdout)
            if vals:out['db']=float(vals[-1])
            out['ok']=p.returncode==0
        except Exception as e:out['error']=str(e)
        return out
    def _set_alsa(self,db):
        db=max(-20.0,min(4.0,float(db)));p=subprocess.run(['amixer','set','PCM',f'{db:.1f}dB'],capture_output=True,text=True,timeout=3,check=False)
        if p.returncode!=0:raise RuntimeError((p.stderr or p.stdout or 'amixer failed').strip())
        return self._alsa()
    def state(self):
        c=self._read_cfg();enabled=[]
        if c['master_enabled']:
            if c['eq']['enabled']:enabled.append('audio_eq')
            if c['compressor']['enabled']:enabled.append('compressor')
            if c['limiter']['enabled']:enabled.append('safety_limiter')
        dep={'numpy':NUMPY_OK,'numpy_version':NUMPY_VERSION,'streaming':True,'block_ms':40.0}
        if not NUMPY_OK:dep['error']=globals().get('NUMPY_ERROR','numpy unavailable')
        status='READY' if NUMPY_OK and not self.last_error else 'DEGRADED'
        return {'ok':True,'available':True,'api_version':1,'status':status,'engine':self.ENGINE,'dependencies':dep,'config':c,'enabled_chain':enabled,'modules':['audio_eq','compressor'],'alsa':self._alsa(),'last_error':self.last_error,'last_process':self.last_process}
    def save_config(self,data):
        current=self._read_cfg()
        for k in ('master_enabled','output_gain_db'):
            if k in data:current[k]=data[k]
        for sec in ('eq','compressor','limiter'):
            if isinstance(data.get(sec),dict):current.setdefault(sec,{}).update(data[sec])
        current=self._sanitize(current)
        if current['master_enabled'] and not NUMPY_OK:return {'ok':False,'error':'LIVE_DSP_REQUIRES_NUMPY'}
        try:alsa=self._set_alsa(current['output_gain_db'])
        except Exception as e:return {'ok':False,'error':'ALSA_GAIN_FAILED: '+str(e)}
        self._write_json(self.cfg_path,current);self.last_error=None;return {'ok':True,'config':current,'alsa':alsa}
    def reset_config(self):
        c=self.defaults();self._write_json(self.cfg_path,c)
        try:alsa=self._set_alsa(c['output_gain_db'])
        except Exception as e:return {'ok':False,'error':'ALSA_GAIN_FAILED: '+str(e)}
        self.last_error=None;return {'ok':True,'config':c,'alsa':alsa}
    def bypass_all(self):
        c=self._read_cfg();c['master_enabled']=False;self._write_json(self.cfg_path,c);return {'ok':True,'config':c}

    # --- LIVE STREAM DSP ---
    @staticmethod
    def _gauss_oct(freq,center,width_oct):
        f=np.maximum(freq,1.0);x=np.log2(f/float(center))/float(width_oct);return np.exp(-0.5*x*x)
    def _make_eq_fir(self,cfg):
        nfft=self.FIR_FFT;nt=self.FIR_TAPS;sr=self.SR
        freq=np.fft.rfftfreq(nfft,d=1.0/sr).astype(np.float64);fr=np.maximum(freq,1e-6)
        hpf=max(20.0,float(cfg.get('hpf_hz',120.0)));lpf=min(sr*0.45,float(cfg.get('lpf_hz',5000.0)))
        hp=1.0/np.sqrt(1.0+(hpf/fr)**4);lp=1.0/np.sqrt(1.0+(fr/lpf)**4);hp[0]=0.0
        db=np.zeros(freq.shape,dtype=np.float64)
        bass=float(cfg.get('bass_db',0.0));lowmid=float(cfg.get('lowmid_db',0.0));presence=float(cfg.get('presence_db',0.0));treble=float(cfg.get('treble_db',0.0))
        if abs(bass)>=0.01:db += bass/(1.0+(fr/260.0)**4)
        if abs(lowmid)>=0.01:db += lowmid*self._gauss_oct(fr,520.0,0.72)
        if abs(presence)>=0.01:db += presence*self._gauss_oct(fr,2200.0,0.78)
        if abs(treble)>=0.01:db += treble/(1.0+(3000.0/fr)**4)
        mag=hp*lp*np.power(10.0,db/20.0)
        h0=np.fft.irfft(mag,n=nfft);hs=np.fft.fftshift(h0);mid=nfft//2;half=nt//2
        taps=hs[mid-half:mid+half+1].copy();taps*=np.hanning(nt)
        return taps.astype(np.float32),np.fft.rfft(np.pad(taps,(0,nfft-nt))).astype(np.complex64)
    @staticmethod
    def _compress_gain_db(level,th,ratio,knee):
        if knee>0:
            lo=th-knee/2.0;hi=th+knee/2.0
            if level<=lo:return 0.0
            if level>=hi:return (th+(level-th)/ratio)-level
            d=level-lo;return (1.0/ratio-1.0)*(d*d)/(2.0*knee)
        if level<=th:return 0.0
        return (th+(level-th)/ratio)-level
    def _process_stream_block(self,x,state,c):
        y=x.astype(np.float32,copy=False)
        if c['eq']['enabled']:
            ov=state['eq_overlap'];ext=np.concatenate((ov,y));spec=np.fft.rfft(ext,n=self.FIR_FFT);z=np.fft.irfft(spec*state['eq_H'],n=self.FIR_FFT).astype(np.float32)
            y=z[self.FIR_TAPS-1:self.FIR_TAPS-1+len(y)];state['eq_overlap']=ext[-(self.FIR_TAPS-1):].copy()
        if c['compressor']['enabled'] and y.size:
            co=c['compressor'];th=float(co['threshold_db']);ratio=max(1.0,float(co['ratio']));knee=6.0 if co.get('soft_knee',True) else 0.0
            attack=max(.001,float(co['attack_ms'])/1000.0);release=max(.001,float(co['release_ms'])/1000.0);makeup=float(co['makeup_db']);frame=480
            out=np.empty_like(y);g=state.get('comp_gr_db',0.0)
            for i in range(0,len(y),frame):
                f=y[i:i+frame];rms=float(np.sqrt(np.mean(f.astype(np.float64)*f.astype(np.float64))+1e-12));level=20.0*math.log10(max(rms,1e-9));target=self._compress_gain_db(level,th,ratio,knee)
                dt=len(f)/float(self.SR);coeff=math.exp(-dt/(attack if target<g else release));g=coeff*g+(1.0-coeff)*target;gain=10.0**((g+makeup)/20.0);out[i:i+len(f)]=f*gain
            state['comp_gr_db']=g;y=out
        if c['limiter']['enabled']:
            ceiling=np.float32(10.0**(float(c['limiter']['ceiling_db'])/20.0));y=np.clip(y,-ceiling,ceiling)
        return y
    def prepare_pcm(self,source):
        source=Path(source)
        with wave.open(str(source),'rb') as w:
            if (w.getnchannels(),w.getsampwidth(),w.getframerate())!=(1,2,self.SR):raise RuntimeError('live DSP requires mono PCM16 48000 Hz')
            frames=w.getnframes();raw=w.readframes(frames)
        return {'pcm':raw,'frames':frames,'duration_s':frames/float(self.SR),'source':str(source)}
    def play_prepared(self,prepared,audio_device='default',stop_event=None,timeout_s=None):
        if not isinstance(prepared,dict) or 'pcm' not in prepared:raise RuntimeError('invalid prepared audio')
        raw=prepared['pcm'];duration=float(prepared.get('duration_s') or (len(raw)/2.0/self.SR));c=self._read_cfg();chain=[]
        if c['master_enabled']:
            if not NUMPY_OK:raise RuntimeError('numpy unavailable')
            if c['eq']['enabled']:chain.append('audio_eq')
            if c['compressor']['enabled']:chain.append('compressor')
            if c['limiter']['enabled']:chain.append('safety_limiter')
        state={'comp_gr_db':0.0}
        if c['master_enabled'] and c['eq']['enabled']:
            _,H=self._make_eq_fir(c['eq']);state['eq_H']=H;state['eq_overlap']=np.zeros(self.FIR_TAPS-1,dtype=np.float32)
        cmd=['aplay','-q','-D',str(audio_device),'-t','raw','-f','S16_LE','-c','1','-r',str(self.SR)]
        proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        wall0=time.monotonic();cpu0=time.process_time();deadline=wall0+(float(timeout_s) if timeout_s else duration+5.0);blocks=0;max_block_ms=0.0
        try:
            mv=memoryview(raw);bytes_per_block=self.BLOCK_FRAMES*2
            for off in range(0,len(raw),bytes_per_block):
                if stop_event is not None and stop_event.is_set():raise RuntimeError('audio stopped')
                if time.monotonic()>deadline:raise RuntimeError('live audio timeout')
                chunk=mv[off:off+bytes_per_block]
                t=time.monotonic()
                if c['master_enabled'] and chain:
                    x=np.frombuffer(chunk,dtype='<i2').astype(np.float32)/32768.0;y=self._process_stream_block(x,state,c);pcm=np.rint(np.clip(y,-1.0,32767.0/32768.0)*32768.0).astype('<i2').tobytes()
                else:pcm=chunk.tobytes()
                max_block_ms=max(max_block_ms,(time.monotonic()-t)*1000.0);blocks+=1
                proc.stdin.write(pcm)
            proc.stdin.close();proc.stdin=None
            while proc.poll() is None:
                if stop_event is not None and stop_event.is_set():proc.terminate();raise RuntimeError('audio stopped')
                if time.monotonic()>deadline:proc.terminate();raise RuntimeError('live audio timeout')
                time.sleep(.02)
            err=(proc.stderr.read().decode(errors='replace') if proc.stderr else '').strip()
            if proc.returncode!=0:raise RuntimeError('aplay failed: '+err[-300:])
            wall_ms=round((time.monotonic()-wall0)*1000.0,1);cpu_ms=round((time.process_time()-cpu0)*1000.0,1);self.last_error=None
            if c['master_enabled']:
                self.last_process=f"LIVE {cpu_ms} ms CPU / {wall_ms} ms wall / {' > '.join(chain) if chain else 'NO ACTIVE DSP'}"
                return {'ok':True,'bypassed':False,'chain':chain,'engine':self.ENGINE,'mode':'LIVE_STREAM','cpu_ms':cpu_ms,'wall_ms':wall_ms,'max_block_ms':round(max_block_ms,2),'block_ms':40.0}
            self.last_process=f'LIVE BYPASS / {wall_ms} ms wall'
            return {'ok':True,'bypassed':True,'chain':[],'engine':self.ENGINE,'mode':'LIVE_STREAM','wall_ms':wall_ms,'max_block_ms':round(max_block_ms,2),'block_ms':40.0}
        except Exception as e:
            self.last_error=str(e);self.last_process='LIVE ERROR'
            try:
                if proc.stdin:proc.stdin.close()
            except Exception:pass
            try:proc.terminate();proc.wait(timeout=1)
            except Exception:
                try:proc.kill()
                except Exception:pass
            raise

    # Kept only for backward API compatibility; TX/preview no longer use offline rendering.
    def process_file(self,source,dest):
        c=self._read_cfg();self.last_process='OFFLINE_DISABLED -> LIVE_STREAM_REQUIRED'
        return {'ok':True,'bypassed':True,'path':str(source),'chain':[],'engine':self.ENGINE,'reason':'OFFLINE_DSP_DISABLED'}
