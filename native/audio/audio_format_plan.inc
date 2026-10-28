/* Control transactions; included after Mixer and its resource helpers. */
typedef struct {
  MixRoute *identity;
  uint32_t rate;
  BhRateKernel *prepared, *retired;
} BhFormatRoute;
struct BhMixFormatPlan {
  Mixer *owner;
  int index, committed;
  uint32_t rate, old_rate, flags, capacity, history_need;
  uint64_t source_revision, route_revision;
  size_t route_count;
  BhFormatRoute *routes;
  float *pcm, *retired_pcm;
  uint8_t *fence, *retired_fence;
};
API void bh_mix_format_plan_free(BhMixFormatPlan *p) {
  if (!p) return;
  Mixer *m=p->owner;
  /* Prepared pointers are private; transferred old pointers retain the exact
   * registry reference formerly held by the source. Release outside bridge. */
  if (m) EnterCriticalSection(&m->lifecycle);
  for(size_t i=0;i<p->route_count;++i) {
    release_kernel(p->routes[i].prepared);
    release_kernel(p->routes[i].retired);
  }
  if (m) LeaveCriticalSection(&m->lifecycle);
  free(p->routes);free(p->pcm);free(p->fence);
  free(p->retired_pcm);free(p->retired_fence);
  free(p);
  if(m) release_mixer(m);
}
API BhMixFormatPlan *bh_mix_format_prepare(void *opaque,int index,uint32_t rate,
                                           uint32_t flags,int *error) {
  if(error) *error=BH_FORMAT_INVALID;
  Mixer *m=opaque;
  if(!m || index<0 || index>=INPUTS || rate<8000 || rate>384000 ||
     (flags&~BH_FORMAT_RETIRE_SOURCE)) return NULL;
  BhMixFormatPlan *p=calloc(1,sizeof(*p));
  if(!p) {if(error)*error=BH_FORMAT_MEMORY;return NULL;}
  atomic_fetch_add_explicit(&m->references,1,memory_order_relaxed);
  p->owner=m;p->index=index;p->rate=rate;p->flags=flags;p->history_need=32;
  Input *s=&m->input[index];
  int failure=BH_FORMAT_STALE, captured=0;
  for(unsigned attempt=0;attempt<8 && !captured;++attempt) {
    EnterCriticalSection(&m->lifecycle);
    EnterCriticalSection(&s->producer);
    int valid=!atomic_load(&m->closing) && (atomic_load(&s->active)||s->reserved);
    p->source_revision=atomic_load(&s->lifetime_revision);
    p->old_rate=atomic_load(&s->source_rate);
    p->route_revision=m->route_revision;
    size_t count=0;
    if(valid) for(MixRoute *r=m->routes;r;r=r->next) count++;
    LeaveCriticalSection(&s->producer);
    LeaveCriticalSection(&m->lifecycle);
    if(!valid) break;
    /* No allocation, kernel construction or destruction holds a live lock. */
    BhFormatRoute *routes=count?calloc(count,sizeof(*routes)):NULL;
    if(count && !routes) {failure=BH_FORMAT_MEMORY;break;}
    EnterCriticalSection(&m->lifecycle);
    EnterCriticalSection(&s->producer);
    valid=!atomic_load(&m->closing) && (atomic_load(&s->active)||s->reserved) &&
          p->source_revision==atomic_load(&s->lifetime_revision) &&
          p->old_rate==atomic_load(&s->source_rate) && p->route_revision==m->route_revision;
    if(valid) {
      size_t n=0;
      for(MixRoute *r=m->routes;r;r=r->next) {
        routes[n].identity=r;routes[n++].rate=r->rate;
      }
      p->routes=routes;p->route_count=count;captured=1;
    }
    LeaveCriticalSection(&s->producer);
    LeaveCriticalSection(&m->lifecycle);
    if(!captured) free(routes);
  }
  if(!captured) {if(error)*error=failure;bh_mix_format_plan_free(p);return NULL;}
  if(rate!=p->old_rate) {
    p->capacity=rate+BHR_MAX_TAPS;
    p->pcm=calloc((size_t)p->capacity*2,sizeof(float));
    p->fence=calloc(p->capacity,1);
    if(!p->pcm || !p->fence) failure=BH_FORMAT_MEMORY;
    else {
      failure=BH_FORMAT_COMMITTED;
      for(size_t i=0;i<p->route_count;++i) {
        /* Reuse identical private kernels within this plan. Live registry
         * sharing is selected only during the short validated commit. */
        BhRateKernel *kernel=NULL;
        for(size_t j=0;j<i;++j)
          if(p->routes[j].rate==p->routes[i].rate) {kernel=p->routes[j].prepared;break;}
        if(kernel) kernel->references++;
        else {
          kernel=bhr_create(rate,p->routes[i].rate);
          if(kernel) kernel->references=1;
        }
        p->routes[i].prepared=kernel;
        if(!kernel) {failure=BH_FORMAT_MEMORY;break;}
        if(kernel->centre+1>p->history_need) p->history_need=kernel->centre+1;
      }
    }
    if(failure!=BH_FORMAT_COMMITTED) {
      if(error)*error=failure;
      bh_mix_format_plan_free(p);return NULL;
    }
  }
  if(error)*error=BH_FORMAT_COMMITTED;
  return p;
}
API int bh_mix_format_try_commit(BhMixFormatPlan *p) {
  if(!p || p->committed) return BH_FORMAT_INVALID;
  Mixer *m=p->owner;
  Input *s=&m->input[p->index];
  if(!TryEnterCriticalSection(&m->lifecycle)) return BH_FORMAT_BUSY;
  if(!TryEnterCriticalSection(&s->producer)) {
    LeaveCriticalSection(&m->lifecycle);return BH_FORMAT_BUSY;
  }
  int result=BH_FORMAT_STALE;
  if(atomic_load(&m->closing) || (!atomic_load(&s->active)&&!s->reserved) ||
     p->source_revision!=atomic_load(&s->lifetime_revision) ||
     p->old_rate!=atomic_load(&s->source_rate) || p->route_revision!=m->route_revision)
    goto done;
  if(p->rate==p->old_rate && !p->flags) {
    p->committed=1;result=BH_FORMAT_COMMITTED;goto done;
  }
  unsigned active=atomic_exchange(&s->active,0);
  if(atomic_load(&s->readers)) {
    atomic_store(&s->active,active);result=BH_FORMAT_BUSY;goto done;
  }
  atomic_store(&s->consumer_ready,0);
  if(p->rate==p->old_rate) {
    /* Same-format endpoint replacement is a real source boundary, while an
     * ordinary same-rate request above remains sample-exact and a true no-op. */
    uint64_t w=atomic_load(&s->write);
    atomic_store(&s->trim,w);
    atomic_store(&s->repair_highwater,w);
    s->last_fence=UINT64_MAX;
    s->repair_checked=w;s->repair_remaining=s->repair_total=s->repair_waiting=0;
    s->last_arrival=s->previous_frames=0;s->jitter_peak=0;
    memset(&s->arrival,0,sizeof(s->arrival));atomic_store(&s->jitter_view,0);
  } else {
    for(size_t i=0;i<p->route_count;++i) {
      MixRoute *route=p->routes[i].identity;
      BhRateKernel *kernel=NULL;
      for(int j=0;j<INPUTS;++j)
        if(j!=p->index && route->kernel[j] && route->kernel[j]->input_rate==p->rate) {
          kernel=route->kernel[j];break;
        }
      p->routes[i].retired=route->kernel[p->index];
      if(kernel) kernel->references++;
      else {kernel=p->routes[i].prepared;p->routes[i].prepared=NULL;}
      route->kernel[p->index]=kernel;
    }
    atomic_store(&s->history_need,p->history_need);
    p->retired_pcm=s->pcm;p->retired_fence=s->fence;
    s->pcm=p->pcm;s->fence=p->fence;s->capacity=p->capacity;
    p->pcm=NULL;p->fence=NULL;
    uint32_t old=p->old_rate,rate=p->rate;
    s->target=(uint32_t)bht_scale(s->target,old,rate);
    s->maximum=(uint32_t)bht_scale(s->maximum,old,rate);
    atomic_store(&s->continuity_min,(uint32_t)bht_scale(atomic_load(&s->continuity_min),old,rate));
    atomic_store(&s->continuity_max,(uint32_t)bht_scale(atomic_load(&s->continuity_max),old,rate));
    atomic_store(&s->dropped,bht_scale(atomic_load(&s->dropped),old,rate));
    atomic_store(&s->expired,bht_scale(atomic_load(&s->expired),old,rate));
    atomic_store(&s->recovery_discarded,bht_scale(atomic_load(&s->recovery_discarded),old,rate));
    atomic_store(&s->target_view,s->target);
    atomic_store(&s->read,0);atomic_store(&s->write,0);atomic_store(&s->trim,0);
    s->cursor=s->available=s->history_count=0;s->last_fence=UINT64_MAX;
    s->repair_checked=0;s->repair_remaining=s->repair_total=s->repair_waiting=0;
    atomic_store(&s->repair_highwater,0);
    s->render_rate=0;s->adaptive_target=s->target;
    s->last_arrival=s->previous_frames=0;
    memset(&s->arrival,0,sizeof(s->arrival));atomic_store(&s->jitter_view,0);
    if(old==BHT_RATE && rate!=BHT_RATE) atomic_fetch_add(&m->native_formats,1);
    else if(old!=BHT_RATE && rate==BHT_RATE) atomic_fetch_sub(&m->native_formats,1);
    atomic_store(&s->source_rate,rate);
  }
  atomic_fetch_add(&s->epoch,1);
  atomic_fetch_add(&s->lifetime_revision,1);
  atomic_store(&s->active,active);
  p->committed=1;result=BH_FORMAT_COMMITTED;
done:
  LeaveCriticalSection(&s->producer);LeaveCriticalSection(&m->lifecycle);
  return result;
}
/* Blocking compatibility helper is control-only. Endpoint publication always
 * uses try_commit and releases its bridge before retrying or freeing a plan. */
API int bh_mix_format(Mixer *m,int index,uint32_t rate) {
  if(!m) return -1;
  atomic_fetch_add(&m->references,1);
  int output=-1;
  for(unsigned attempt=0;attempt<8;++attempt) {
    int error=BH_FORMAT_INVALID;
    BhMixFormatPlan *p=bh_mix_format_prepare(m,index,rate,0,&error);
    if(!p) break;
    int result;
    while((result=bh_mix_format_try_commit(p))==BH_FORMAT_BUSY) Sleep(0);
    bh_mix_format_plan_free(p);
    if(result==BH_FORMAT_COMMITTED) {output=0;break;}
    if(result!=BH_FORMAT_STALE || atomic_load(&m->closing)) break;
  }
  release_mixer(m);
  return output;
}
