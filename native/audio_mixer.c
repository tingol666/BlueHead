/* BlueHead bounded PCM mixer. Apache-2.0.
 * Serialized producers, one render consumer. No locks/allocation/Python in render.
 * Ring history is consumer-owned: released slots may immediately be reused.
 */
#include <windows.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdatomic.h>
#define API __declspec(dllexport)
#define INPUTS 8
#define CAPACITY 48064u
#define TAPS 64
#define PHASES 1024
#if ATOMIC_LLONG_LOCK_FREE != 2 || ATOMIC_INT_LOCK_FREE != 2
#error BlueHead requires lock-free atomics
#endif
typedef struct {
    CRITICAL_SECTION producer;
    float *pcm;
    atomic_ullong read, write, trim, dropped, underruns, prefill;
    atomic_uint active, readers, gain, enabled, epoch, drift_view;
    uint32_t target, maximum;
    /* Render-only state. */
    uint32_t seen_epoch, history_count;
    uint64_t cursor, available, rendered;
    float history[64], gate;
    int buffering;
    double phase, drift, clock_average, clock_correction;
} Input;
typedef struct Mixer {
    CRITICAL_SECTION lifecycle;
    Input input[INPUTS];
    atomic_ullong underruns;
    atomic_uint master, clock_period, clock_limit;
    atomic_int call;
    float sinc[PHASES+1][TAPS];
} Mixer;
static unsigned bits(float f) { unsigned u; memcpy(&u,&f,4); return u; }
static float number(unsigned u) { float f; memcpy(&f,&u,4); return f; }
API void bh_mix_master(Mixer *m,float gain) { if(m && isfinite(gain)) atomic_store(&m->master,bits(gain)); }
API float bh_mix_master_value(Mixer *m) { return m ? number(atomic_load(&m->master)) : 0; }
/* period=0 preserves the media controller. A nonzero period enables the smooth
 * integral controller for independent capture clocks. Configure off render. */
API int bh_mix_clock_config(Mixer *m,float period_seconds,float maximum_ppm) {
    if(!m || !isfinite(period_seconds) || !isfinite(maximum_ppm) || period_seconds<0 ||
       (period_seconds>0 && (period_seconds<.25f || period_seconds>10)) || maximum_ppm<=0 || maximum_ppm>1000) return -1;
    atomic_store(&m->clock_period,bits(period_seconds)); atomic_store(&m->clock_limit,bits(maximum_ppm/1000000)); return 0;
}
API Mixer *bh_mix_create(void) {
    Mixer *m=calloc(1,sizeof(*m)); if(!m) return NULL;
    InitializeCriticalSection(&m->lifecycle);
    atomic_init(&m->underruns,0); atomic_init(&m->master,0x3f800000u); atomic_init(&m->call,-1);
    atomic_init(&m->clock_period,bits(0)); atomic_init(&m->clock_limit,bits(.0005f));
    const double pi=3.14159265358979323846;
    for(int p=0;p<=PHASES;p++) {
        double sum=0;
        for(int t=0;t<TAPS;t++) {
            double x=t-31-(double)p/PHASES;
            double win=fabs(x)>=32 ? 0 : .35875+.48829*cos(pi*x/32)+.14128*cos(2*pi*x/32)+.01168*cos(3*pi*x/32);
            double value=fabs(x)<1e-12 ? 1 : sin(pi*x)/(pi*x);
            m->sinc[p][t]=(float)(value*win); sum+=value*win;
        }
        for(int t=0;t<TAPS;t++) m->sinc[p][t]/=(float)sum;
    }
    for(int i=0;i<INPUTS;i++) {
        Input *s=&m->input[i]; InitializeCriticalSection(&s->producer);
        atomic_init(&s->read,0); atomic_init(&s->write,0); atomic_init(&s->trim,0); atomic_init(&s->dropped,0);
        atomic_init(&s->underruns,0); atomic_init(&s->prefill,0);
        atomic_init(&s->active,0); atomic_init(&s->readers,0); atomic_init(&s->gain,0x3f800000u);
        atomic_init(&s->enabled,1); atomic_init(&s->epoch,0); atomic_init(&s->drift_view,1000);
        s->pcm=calloc(CAPACITY*2,sizeof(float));
        if(!s->pcm) {
            for(int j=0;j<=i;j++) { free(m->input[j].pcm); DeleteCriticalSection(&m->input[j].producer); }
            DeleteCriticalSection(&m->lifecycle); free(m); return NULL;
        }
    }
    return m;
}
/* Owner must stop/join its renderer and producers before freeing. */
API void bh_mix_free(Mixer *m) {
    if(!m) return;
    for(int i=0;i<INPUTS;i++) { free(m->input[i].pcm); DeleteCriticalSection(&m->input[i].producer); }
    DeleteCriticalSection(&m->lifecycle); free(m);
}
API int bh_mix_add(Mixer *m,uint32_t target,uint32_t maximum) {
    if(!m || !maximum || maximum>CAPACITY-TAPS || target>maximum) return -1;
    EnterCriticalSection(&m->lifecycle); int index=-1;
    for(int i=0;i<INPUTS;i++) if(!atomic_load(&m->input[i].active)) {
        Input *s=&m->input[i]; EnterCriticalSection(&s->producer);
        while(atomic_load(&s->readers)) Sleep(0);
        atomic_store(&s->read,0); atomic_store(&s->write,0); atomic_store(&s->trim,0); atomic_store(&s->dropped,0);
        atomic_store(&s->underruns,0); atomic_store(&s->prefill,0);
        atomic_store(&s->gain,0x3f800000u); atomic_store(&s->enabled,1); atomic_store(&s->epoch,0); atomic_store(&s->drift_view,1000);
        s->target=target; s->maximum=maximum; s->seen_epoch=s->history_count=0;
        s->cursor=s->available=s->rendered=0; s->gate=1; s->buffering=1; s->phase=s->drift=0;
        s->clock_average=target; s->clock_correction=1;
        atomic_store(&s->active,1); LeaveCriticalSection(&s->producer); index=i; break;
    }
    LeaveCriticalSection(&m->lifecycle); return index;
}
API void bh_mix_remove(Mixer *m,int index) {
    if(!m || index<0 || index>=INPUTS) return;
    EnterCriticalSection(&m->lifecycle); Input *s=&m->input[index]; EnterCriticalSection(&s->producer);
    atomic_store(&s->active,0);
    /* Only control waits; already acquired callbacks complete normally. */
    while(atomic_load(&s->readers)) Sleep(0);
    int expected=index; atomic_compare_exchange_strong(&m->call,&expected,-1);
    LeaveCriticalSection(&s->producer); LeaveCriticalSection(&m->lifecycle);
}
API void bh_mix_clear(Mixer *m,int index) {
    if(!m || index<0 || index>=INPUTS) return;
    Input *s=&m->input[index]; EnterCriticalSection(&s->producer);
    atomic_store(&s->trim,atomic_load(&s->write)); atomic_fetch_add(&s->epoch,1);
    LeaveCriticalSection(&s->producer);
}
API void bh_mix_gain(Mixer *m,int index,float gain) {
    if(m && index>=0 && index<INPUTS && isfinite(gain)) atomic_store(&m->input[index].gain,bits(gain));
}
API void bh_mix_gate(Mixer *m,int index,int enabled) { if(m && index>=0 && index<INPUTS) atomic_store(&m->input[index].enabled,!!enabled); }
API void bh_mix_call(Mixer *m,int index) { if(m) atomic_store(&m->call,index); }
API void bh_mix_write(Mixer *m,int index,const float *pcm,uint32_t frames) {
    if(!m || !pcm || index<0 || index>=INPUTS || !frames) return;
    Input *s=&m->input[index]; EnterCriticalSection(&s->producer);
    if(!atomic_load(&s->active)) { LeaveCriticalSection(&s->producer); return; }
    if(frames>s->maximum) { atomic_fetch_add(&s->dropped,frames-s->maximum); pcm+=(frames-s->maximum)*2; frames=s->maximum; }
    uint64_t w=atomic_load_explicit(&s->write,memory_order_relaxed),r=atomic_load_explicit(&s->read,memory_order_acquire);
    if(w-r+frames>CAPACITY) {
        /* Never overwrite a sample a callback may currently be reading. */
        atomic_fetch_add(&s->dropped,frames); atomic_store(&s->trim,w); LeaveCriticalSection(&s->producer); return;
    }
    uint32_t at=(uint32_t)(w%CAPACITY),first=frames<CAPACITY-at ? frames : CAPACITY-at;
    memcpy(s->pcm+at*2,pcm,first*2*sizeof(float)); memcpy(s->pcm,pcm+first*2,(frames-first)*2*sizeof(float));
    if(w-r+frames>s->maximum) atomic_store_explicit(&s->trim,w+frames-s->maximum,memory_order_release);
    atomic_store_explicit(&s->write,w+frames,memory_order_release); LeaveCriticalSection(&s->producer);
}
API void bh_mix_write_s16_mono(Mixer *m,int index,const int16_t *pcm,uint32_t frames) {
    if(!m || !pcm || frames>CAPACITY) return;
    /* Called on the voice worker. Native stack chunks avoid Python arrays. */
    float stereo[480*2];
    while(frames) {
        uint32_t take=frames<480 ? frames : 480;
        for(uint32_t i=0;i<take;i++) stereo[i*2]=stereo[i*2+1]=pcm[i]/32768.0f;
        bh_mix_write(m,index,stereo,take); pcm+=take; frames-=take;
    }
}
static float sample(Input *s,int offset,int channel) {
    if(offset<0) {
        if(!s->history_count) offset=0;
        else { if(offset<-(int)s->history_count) offset=-(int)s->history_count;
            return s->history[((int)s->history_count+offset)*2+channel]; }
    }
    if((uint64_t)offset>=s->available) offset=(int)s->available-1;
    return s->pcm[((s->cursor+(uint32_t)offset)%CAPACITY)*2+channel];
}
static float interpolate(Mixer *m,Input *s,int offset,int channel,float fraction) {
    float phase=fraction*PHASES; int index=(int)phase; float blend=phase-index,value=0;
    if(index>=PHASES) return sample(s,offset+1,channel);
    for(int t=0;t<TAPS;t++) value+=sample(s,offset+t-31,channel)*(m->sinc[index][t]+blend*(m->sinc[index+1][t]-m->sinc[index][t]));
    return value;
}
static void save_history(Input *s,uint32_t consumed) {
    uint32_t keep=consumed<32 ? consumed : 32,old=s->history_count<32-keep ? s->history_count : 32-keep;
    if(old) memmove(s->history,s->history+(s->history_count-old)*2,old*2*sizeof(float));
    for(uint32_t j=0;j<keep;j++) for(int ch=0;ch<2;ch++) s->history[(old+j)*2+ch]=sample(s,(int)(consumed-keep+j),ch);
    s->history_count=old+keep;
}
/* Adapted from PipeWire spa/plugins/bluez5/rate-control.h, MIT.
 * Copyright (c) 2022 Pauli Virtanen. Original revision/source/license retained
 * in vendor/pipewire-rate-control. The low-pass average plus accumulated rate
 * term matches clocks without the permanent queue displacement of pure P.
 * Units are frames; duration and period use the same 48kHz clock. */
static double clock_update(Input *s,uint32_t duration,double target,double period,double limit) {
    double beta=duration/period; if(beta>.5) beta=.5;
    double old=s->clock_average;
    double average=beta*((double)s->available-s->phase)+(1-beta)*old;
    s->clock_correction+=(average-old)/(3*period)+beta*(old-target)/(27*period);
    if(s->clock_correction>1+limit) s->clock_correction=1+limit;
    if(s->clock_correction<1-limit) s->clock_correction=1-limit;
    s->clock_average=average; s->drift=s->clock_correction-1;
    return s->clock_correction;
}
API int bh_mix_render(Mixer *m,float *output,uint32_t count,float master) {
    if(!m || !output || count>CAPACITY || !isfinite(master)) return -1;
    memset(output,0,count*2*sizeof(float)); int call=atomic_load(&m->call);
    double clock_period=number(atomic_load(&m->clock_period)),clock_limit=number(atomic_load(&m->clock_limit));
    for(int i=0;i<INPUTS;i++) {
        Input *s=&m->input[i]; atomic_fetch_add(&s->readers,1);
        if(!atomic_load(&s->active)) { atomic_fetch_sub(&s->readers,1); continue; }
        uint64_t w=atomic_load_explicit(&s->write,memory_order_acquire),trim=atomic_load_explicit(&s->trim,memory_order_acquire);
        if(trim>w) trim=w;
        unsigned epoch=atomic_load_explicit(&s->epoch,memory_order_acquire);
        if(trim>s->cursor || epoch!=s->seen_epoch) {
            if(trim>s->cursor) { if(epoch==s->seen_epoch) atomic_fetch_add(&s->dropped,trim-s->cursor); s->cursor=trim; }
            s->history_count=0; s->phase=s->drift=0; s->buffering=1; s->seen_epoch=epoch;
        }
        s->available=w-s->cursor;
        /* Observe the queue at the centre of this render quantum. Comparing a
         * pre-render queue directly with the requested buffer otherwise costs
         * half a callback of safety margin, especially for 20ms HFP + jitter.
         * Legacy media keeps its original queue semantics and controller. */
        int smooth_clock=s->target && (clock_period>0 || call==i);
        uint32_t clock_target=s->target;
        if(smooth_clock) {
            uint64_t centred=(uint64_t)s->target+count/2;
            clock_target=(uint32_t)(centred>s->maximum ? s->maximum : centred);
        }
        if(s->buffering && s->available<clock_target) {
            atomic_fetch_add(&s->prefill,count);
            atomic_store_explicit(&s->read,s->cursor,memory_order_release); atomic_fetch_sub(&s->readers,1); continue;
        }
        if(s->buffering) { s->clock_average=clock_target; s->clock_correction=1; s->drift=0; }
        s->buffering=0;
        float gain=number(atomic_load(&s->gain))*((call>=0 && call!=i) ? .15f : 1);
        float target=atomic_load(&s->enabled) ? 1 : 0; double ratio=1;
        if(smooth_clock) {
            ratio=clock_update(s,count,clock_target,(clock_period>0 ? clock_period : 3)*48000,clock_period>0 ? clock_limit : .001);
        } else if(s->target && s->rendered>96000) {
            double wanted=((double)s->available-s->target)/48000*.02;
            if(wanted>.0005) wanted=.0005;
            if(wanted<-.0005) wanted=-.0005;
            s->drift+=.005*(wanted-s->drift); ratio+=s->drift;
        }
        uint32_t produced=0; double position=s->phase;
        for(;produced<count && (uint64_t)position<s->available;produced++,position+=ratio) {
            int offset=(int)position; float fraction=(float)(position-offset);
            if(s->gate<target) s->gate=fminf(target,s->gate+1.0f/480);
            else if(s->gate>target) s->gate=fmaxf(target,s->gate-1.0f/480);
            for(int ch=0;ch<2;ch++) {
                float value=fraction==0 ? sample(s,offset,ch) : interpolate(m,s,offset,ch,fraction);
                if(isfinite(value)) output[produced*2+ch]+=value*gain*s->gate;
            }
        }
        uint32_t consumed=(uint32_t)position; if(consumed>s->available) consumed=(uint32_t)s->available;
        save_history(s,consumed); s->cursor+=consumed; s->available-=consumed;
        s->phase=s->available ? position-consumed : 0; s->rendered+=produced;
        if(produced<count) { s->buffering=1; atomic_fetch_add(&m->underruns,1); atomic_fetch_add(&s->underruns,1); }
        double ppm=s->drift*1000000;
        if(ppm<-1000) ppm=-1000;
        if(ppm>1000) ppm=1000;
        atomic_store(&s->drift_view,(unsigned)llround(ppm+1000));
        atomic_store_explicit(&s->read,s->cursor,memory_order_release); atomic_fetch_sub(&s->readers,1);
    }
    float peak=0;
    for(uint32_t j=0;j<count*2;j++) { output[j]*=master; float value=fabsf(output[j]); if(value>peak) peak=value; }
    if(peak>.98f) { float scale=.98f/peak; for(uint32_t j=0;j<count*2;j++) output[j]*=scale; }
    return 1;
}
API uint64_t bh_mix_value(Mixer *m,int index,int field) {
    if(!m) return 0;
    if(index<0) return field==0 ? atomic_load(&m->underruns) : 0;
    if(index>=INPUTS) return 0;
    Input *s=&m->input[index];
    if(field==0) {
        uint64_t r=atomic_load_explicit(&s->read,memory_order_acquire),w=atomic_load_explicit(&s->write,memory_order_acquire),trim=atomic_load(&s->trim);
        if(trim>r) r=trim;
        return w>r ? w-r : 0;
    }
    if(field==1) return atomic_load(&s->dropped);
    if(field==3) return atomic_load(&s->underruns);
    if(field==4) return atomic_load(&s->prefill);
    return atomic_load(&s->drift_view);
}
