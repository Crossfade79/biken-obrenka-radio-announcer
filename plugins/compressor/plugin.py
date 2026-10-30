#!/usr/bin/env python3
import math
import numpy as np
PLUGIN_API_VERSION=1
PLUGIN_NAME='compressor'
ENGINE='FAST_NUMPY_FRAME_RMS'

def process(samples,sr,cfg):
    x=np.asarray(samples,dtype=np.float32)
    n=x.size
    if n==0:return x
    th=float(cfg.get('threshold_db',-18.0));ratio=max(1.0,float(cfg.get('ratio',2.5)))
    attack=max(0.001,float(cfg.get('attack_ms',15.0))/1000.0)
    release=max(0.001,float(cfg.get('release_ms',180.0))/1000.0)
    makeup=float(cfg.get('makeup_db',0.0));soft=bool(cfg.get('soft_knee',True));knee=6.0 if soft else 0.0
    frame=max(64,int(sr*0.010))
    pad=(-n)%frame
    xp=np.pad(x,(0,pad)) if pad else x
    frames=xp.reshape(-1,frame)
    rms=np.sqrt(np.mean(frames.astype(np.float64)**2,axis=1)+1e-12)
    level=20.0*np.log10(np.maximum(rms,1e-9))
    reduction=np.zeros_like(level)
    if knee>0:
        lo=th-knee/2.0;hi=th+knee/2.0
        mid=(level>lo)&(level<hi);above=level>=hi
        d=level[mid]-lo
        reduction[mid]=(1.0/ratio-1.0)*(d*d)/(2.0*knee)
        reduction[above]=(th+(level[above]-th)/ratio)-level[above]
    else:
        above=level>th
        reduction[above]=(th+(level[above]-th)/ratio)-level[above]
    # Smooth only frame-rate control data (~100 values/s), not every audio sample.
    aA=math.exp(-frame/(sr*attack));aR=math.exp(-frame/(sr*release))
    sm=np.empty_like(reduction);g=0.0
    for i,r in enumerate(reduction):
        coeff=aA if r<g else aR
        g=coeff*g+(1.0-coeff)*float(r)
        sm[i]=g
    sm += makeup
    frame_gain=np.power(10.0,sm/20.0)
    centers=np.arange(frame_gain.size,dtype=np.float64)*frame + frame*0.5
    pos=np.arange(n,dtype=np.float64)
    gain=np.interp(pos,centers,frame_gain,left=frame_gain[0],right=frame_gain[-1]).astype(np.float32)
    return x*gain
