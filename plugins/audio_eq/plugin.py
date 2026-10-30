#!/usr/bin/env python3
import numpy as np
PLUGIN_API_VERSION=1
PLUGIN_NAME='audio_eq'
ENGINE='FAST_NUMPY_FFT'

def _gauss_oct(freq, center, width_oct):
    f=np.maximum(freq,1.0)
    x=np.log2(f/float(center))/float(width_oct)
    return np.exp(-0.5*x*x)

def process(samples,sr,cfg):
    x=np.asarray(samples,dtype=np.float32)
    n=x.size
    if n==0:return x
    freq=np.fft.rfftfreq(n,d=1.0/float(sr)).astype(np.float32)
    eps=np.float32(1e-6)
    hpf=max(20.0,float(cfg.get('hpf_hz',120.0)))
    lpf=min(float(sr)*0.45,float(cfg.get('lpf_hz',5000.0)))
    # Smooth 2nd-order magnitude high/low cuts, plus broad voice bands.
    fr=np.maximum(freq,eps)
    hp=1.0/np.sqrt(1.0+(hpf/fr)**4)
    lp=1.0/np.sqrt(1.0+(fr/lpf)**4)
    hp[0]=0.0
    db=np.zeros(freq.shape,dtype=np.float32)
    bass=float(cfg.get('bass_db',0.0))
    lowmid=float(cfg.get('lowmid_db',0.0))
    presence=float(cfg.get('presence_db',0.0))
    treble=float(cfg.get('treble_db',0.0))
    if abs(bass)>=0.01:
        db += bass/(1.0+(fr/260.0)**4)
    if abs(lowmid)>=0.01:
        db += lowmid*_gauss_oct(fr,520.0,0.72)
    if abs(presence)>=0.01:
        db += presence*_gauss_oct(fr,2200.0,0.78)
    if abs(treble)>=0.01:
        db += treble/(1.0+(3000.0/fr)**4)
    gain=(hp*lp*np.power(10.0,db/20.0)).astype(np.float32)
    spec=np.fft.rfft(x)
    y=np.fft.irfft(spec*gain,n=n).astype(np.float32)
    return y
