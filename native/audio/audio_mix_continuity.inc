/* 2.0.1 opt-in renderer. Included after the original rendering helpers.
 * The unchanged legacy renderer remains an offline comparison oracle.
 */
API int bh_mix_continuity(Mixer *m, int index, int enabled, uint32_t minimum, uint32_t maximum) {
  if (!m || index < 0 || index >= INPUTS || minimum > maximum)
    return -1;
  Input *s = &m->input[index];
  EnterCriticalSection(&s->producer);
  uint32_t rate = atomic_load(&s->source_rate);
  if ((uint64_t)maximum * rate > (uint64_t)s->maximum * BHT_RATE) {
    LeaveCriticalSection(&s->producer);
    return -1;
  }
  minimum = (uint32_t)bht_scale(minimum, BHT_RATE, rate);
  maximum = (uint32_t)bht_scale(maximum, BHT_RATE, rate);
  atomic_store(&s->continuity_min, minimum);
  atomic_store(&s->continuity_max, maximum);
  atomic_store_explicit(&s->continuity_mode, !!enabled, memory_order_release);
  LeaveCriticalSection(&s->producer);
  if (enabled)
    atomic_store(&m->enhanced, 1);
  else {
    int any = 0;
    for (int i = 0; i < INPUTS; i++)
      if (atomic_load(&m->input[i].continuity_mode))
        any = 1;
    atomic_store(&m->enhanced, any);
  }
  return 0;
}
static float continuity_gate(Input *s, float target) {
  if (s->gate < target)
    s->gate = fminf(target, s->gate + 100.0f / (s->render_rate ? s->render_rate : 48000));
  else if (s->gate > target)
    s->gate = fmaxf(target, s->gate - 100.0f / (s->render_rate ? s->render_rate : 48000));
  return s->gate;
}
static int render_continuity(Mixer *m, MixRoute *route, float *output, uint32_t count,
                             float master) {
  if (!m || !output || count > CAPACITY || !isfinite(master))
    return -1;
  if (m->quality.rate != route->rate) {
    m->quality.rate = route->rate;
    m->quality.lookahead = route->lookahead;
    m->quality.ramp = route->ramp;
    m->quality.attack = route->attack;
    m->quality.release = route->release;
    bhq_reset_envelope(&m->quality);
  }
  memset(output, 0, count * 2 * sizeof(float));
  memset(m->delayed, 0, count * 2 * sizeof(double));
  memset(m->accumulated, 0, count * 2 * sizeof(double));
  int flush_muted = master == 0 && m->quality.master.value != 0;
  if (flush_muted)
    bhq_reset_envelope(&m->quality);
  int call = atomic_load(&m->call);
  double period = number(atomic_load(&m->clock_period)),
         limit = number(atomic_load(&m->clock_limit));
  for (int i = 0; i < INPUTS; i++) {
    Input *s = &m->input[i];
    atomic_fetch_add(&s->readers, 1);
    if (!atomic_load(&s->active)) {
      atomic_fetch_sub(&s->readers, 1);
      continue;
    }
    BhRateKernel *kernel = route->kernel[i];
    uint32_t source_rate = atomic_load(&s->source_rate);
    double native_count = count * kernel->ratio;
    double canonical_count = (double)count * BHT_RATE / route->rate;
    if (s->render_rate != route->rate) {
      atomic_store_explicit(&s->consumer_ready, 0, memory_order_release);
      s->render_rate = route->rate;
      s->quality.rate = route->rate;
      s->quality.lookahead = route->lookahead;
      s->quality.ramp = route->ramp;
      bhq_source_clear(&s->quality);
      bhc_reset(&s->continuity);
      bhc_rate(&s->continuity, route->rate);
      s->repair_remaining=s->repair_total=0;
    }
    s->presentation_fraction += canonical_count;
    uint64_t presented = (uint64_t)s->presentation_fraction;
    s->presentation_fraction -= presented;
    memset(m->scratch, 0, count * 2 * sizeof(float));
    if (flush_muted)
      bhq_source_clear(&s->quality);
    float gain = number(atomic_load(&s->gain)) * ((call >= 0 && call != i) ? .15f : 1);
    int enhanced = atomic_load_explicit(&s->continuity_mode, memory_order_acquire);
    int framed = atomic_load_explicit(&s->gap_owner, memory_order_acquire) != BHT_GENERIC;
    uint64_t w = atomic_load_explicit(&s->write, memory_order_acquire),
             trim = atomic_load_explicit(&s->trim, memory_order_acquire);
    if (trim > w)
      trim = w;
    unsigned epoch = atomic_load_explicit(&s->epoch, memory_order_acquire);
    unsigned control_generation = atomic_load(&s->control_generation);
    if (trim > s->cursor || epoch != s->seen_epoch) {
      if (trim > s->cursor) {
        if (epoch == s->seen_epoch)
          atomic_fetch_add(&s->dropped, trim - s->cursor);
        s->cursor = trim;
      }
      s->history_count = 0;
      s->phase = s->drift = 0;
      s->deadline_debt = 0;
      s->buffering = 1;
      s->recover_late = framed && epoch == s->seen_epoch;
      s->adaptive_target = s->target;
      s->adaptive_decay = 0;
      s->seen_epoch = epoch;
      s->repair_checked=s->cursor;
      s->repair_remaining=s->repair_total=s->repair_waiting=0;
      s->starve_frames = s->deficit_frames = 0;
      uint64_t concealed = s->continuity.concealed, silent = s->continuity.silent,
               merged = s->continuity.merged;
      bhc_reset(&s->continuity);
      bhc_rate(&s->continuity, route->rate);
      bhq_source_clear(&s->quality);
      s->continuity.concealed = concealed;
      s->continuity.silent = silent;
      s->continuity.merged = merged;
    }
    uint64_t before_concealed = s->continuity.concealed, before_silent = s->continuity.silent,
             before_merged = s->continuity.merged;
    s->available = w - s->cursor;
    if (framed && s->deadline_debt > 0) {
      uint64_t expired = (uint64_t)s->deadline_debt;
      if (expired > s->available)
        expired = s->available;
      /* Only a missing presentation interval owns this debt. An estimated or
       * omitted compressed duration cannot prove real PCM beyond its fence is
       * obsolete. Scan only the bounded expiry range, never the normal path.
       * Include its endpoint to avoid carrying fractional debt past a fence. */
      uint64_t boundary=first_pcm_boundary(s,s->cursor,expired+(expired<s->available));
      int barrier=boundary!=UINT64_MAX;
      if (barrier) expired=boundary-s->cursor;
      s->cursor += expired;
      s->available -= expired;
      s->deadline_debt -= expired;
      atomic_fetch_add(&s->expired, expired);
      if (expired)
        s->history_count = 0;
      if (barrier) {
        s->phase=s->deadline_debt=0;
        if (s->last_fence!=s->cursor) {
          atomic_fetch_add(&s->fence_reanchors,1);
          s->last_fence=s->cursor;
        }
      } else if (s->deadline_debt < 1) {
        s->phase = s->deadline_debt;
        s->deadline_debt = 0;
      }
    }
    uint32_t target = s->target;
    if (enhanced) {
      uint32_t floor = atomic_load(&s->continuity_min), ceiling = atomic_load(&s->continuity_max);
      uint64_t desired =
          (uint64_t)floor + atomic_load(&s->jitter_view) + (uint64_t)ceil(native_count / 2);
      if (desired > ceiling)
        desired = ceiling;
      if (!s->adaptive_target)
        s->adaptive_target = target;
      if (desired > s->adaptive_target)
        s->adaptive_target = (uint32_t)desired;
      else if (s->rendered > 240000 && s->adaptive_target > desired) {
        /* Decay per rendered frame, not per callback. Small/irregular device
         * quanta must not shrink the safety buffer several times faster. */
        s->adaptive_decay += native_count;
        uint32_t step = (uint32_t)(s->adaptive_decay / 480);
        s->adaptive_decay -= step * 480;
        s->adaptive_target =
            s->adaptive_target - desired > step ? s->adaptive_target - step : (uint32_t)desired;
      }
      target = s->adaptive_target;
      atomic_store(&s->target_view, target);
    }
    int smooth = target && (enhanced || period > 0 || call == i);
    uint32_t observed_target = target;
    if (smooth) {
      uint64_t centre = (uint64_t)target + (uint64_t)ceil(native_count / 2);
      observed_target = (uint32_t)(centre > s->maximum ? s->maximum : centre);
    }
    if (kernel->ratio != 1) {
      uint32_t support = kernel->taps - kernel->centre + (uint32_t)ceil(native_count);
      if (observed_target < support)
        observed_target = support;
    }
    if (s->buffering && s->available < observed_target) {
      atomic_fetch_add(&s->prefill, presented);
      atomic_fetch_add(&s->presentation, presented);
      consumer_publish(s, epoch, control_generation, 0, 0);
      bhq_source(&s->quality, m->scratch, m->accumulated, m->delayed, count, gain);
      atomic_fetch_sub(&s->readers, 1);
      continue;
    }
    if (s->buffering) {
      if (framed && s->recover_late && s->available > observed_target + native_count) {
        uint64_t expired = s->available - observed_target;
        /* A recovery target is not evidence that newly fenced genuine PCM
         * is obsolete. Retire only the old interval before the same barrier
         * respected by ordinary presentation-debt repayment above. */
        uint64_t boundary=first_pcm_boundary(s,s->cursor,expired+(expired<s->available));
        if(boundary!=UINT64_MAX) {
          expired=boundary-s->cursor;
          if(s->last_fence!=boundary) {
            atomic_fetch_add(&s->fence_reanchors,1);
            s->last_fence=boundary;
          }
        }
        s->cursor += expired;
        s->available -= expired;
        s->history_count = 0;
        atomic_fetch_add(&s->expired, expired);
        atomic_fetch_add(&s->recovery_discarded, expired);
      }
      s->deadline_debt = 0;
      s->recover_late = 0;
      bht_clock_reset(&s->clock, observed_target);
      s->drift = 0;
      if (enhanced && s->rendered) {
        s->continuity.gap = bhc_frames(&s->continuity, 960);
        s->continuity.merge = s->continuity.merge_length = bhc_frames(&s->continuity, 192);
      }
    }
    s->buffering = 0;
    float gate_target = (atomic_load(&s->enabled) && atomic_load(&s->connected)) ? 1 : 0;
    uint32_t prefix = 0;
    /* Buffer targets are followed by the smooth clock controller below.
     * Periodic waveform copies are unsafe for general music: low-frequency
     * energy can correlate while other content is not periodic, producing
     * backward/forward sample seams despite a full queue. Concealment remains
     * reserved for genuinely missing audio, followed by its bounded merge. */
    double ratio = 1;
    if (smooth) {
      /* The supported +/-500ppm source mismatch needs correction headroom.
       * A clamp at the source limit rectifies jitter and slowly drains/fills
       * the queue even after the rate estimate converges to that limit. */
      ratio = clock_update(s, native_count, observed_target,
                           (period > 0 ? period : 3) * source_rate, period > 0 ? limit : .001);
    } else if (s->target && s->rendered > 96000) {
      double wanted = ((double)s->available - s->target) / source_rate * .02;
      if (wanted > .0005)
        wanted = .0005;
      if (wanted < -.0005)
        wanted = -.0005;
      s->drift += .005 * (wanted - s->drift);
      ratio += s->drift;
    }
    ratio *= kernel->ratio;
    s->boundary = NULL;
    if (s->available && (ratio != 1 || s->phase != 0))
      prepare_boundary(m, s, kernel);
    uint32_t produced = prefix;
    double position = s->phase;
    /* Quality is applied in a batched pass below. Mirror only its tiny control
     * trajectory so an event later in this callback sees its own predecessor,
     * rather than the gain left at the start of a large callback. The actual
     * gain state, delay and PCM pass remain unchanged. */
    uint64_t repair_limit=atomic_load_explicit(&s->repair_highwater,memory_order_relaxed);
    if(repair_limit>w) repair_limit=w;
    int repair_work=s->repair_remaining || s->repair_waiting || repair_limit>s->repair_checked;
    BhGainRamp audible_gain={0}, audible_master={0};
    if(repair_work) {
      audible_gain=s->quality.gain;audible_master=m->quality.master;
      bhq_target(&audible_gain,gain,route->ramp);
      bhq_target(&audible_master,master,route->ramp);
    }
    for (; produced < count && (uint64_t)position < s->available; produced++, position += ratio) {
      int offset = (int)position;
      double fraction = position - offset;
      float in[2], value[2];
      int filtered = kernel->ratio > 1 || fraction != 0;
      if (filtered && (uint64_t)offset + kernel->taps - kernel->centre - 1 >= s->available)
        break; /* Missing future support is a deadline gap, never a repeated edge sample. */
      /* Event time is the actual source centre, never future filter support.
       * A muted/closed route has no audible predecessor to bridge from. */
      /* Repeated rational steps can land a few ulps before an integer in a
       * large callback. Normalize only event identity using the same dust
       * tolerance as the existing end-of-callback cursor; PCM interpolation
       * continues to use its unchanged fraction and filter coefficients. */
      if(repair_work) {
        int audible=enhanced && gain>0 && master>0 && s->gate>0 &&
                    audible_gain.value>0 && audible_master.value>0;
        double event_position=position, event_nearest=nearbyint(event_position);
        if(fabs(event_position-event_nearest)<1e-9) event_position=event_nearest;
        repair_boundaries(s,route,s->cursor+(uint64_t)event_position,audible);
      }
      if (filtered)
        interpolate_stereo(kernel, s, offset, fraction, in);
      else {
        in[0] = sample(s, offset, 0);
        in[1] = sample(s, offset, 1);
      }
      if (enhanced) {
        if(repair_work) repair_sample(s,route,in);
        bhc_real(&s->continuity, in, value);
      } else {
        value[0] = in[0];
        value[1] = in[1];
      }
      float g = continuity_gate(s, gate_target);
      m->scratch[produced * 2] = value[0] * g;
      m->scratch[produced * 2 + 1] = value[1] * g;
      if(repair_work) {bhq_gain(&audible_gain);bhq_gain(&audible_master);}
    }
    double nearest = nearbyint(position);
    if (fabs(position - nearest) < 1e-9)
      position = nearest; // Remove only floating accumulation dust at exact rational boundaries.
    uint32_t consumed = (uint32_t)position;
    if (consumed > s->available)
      consumed = (uint32_t)s->available;
    if(repair_work) repair_released(s,s->cursor+consumed);
    else if(s->repair_checked<s->cursor+consumed) s->repair_checked=s->cursor+consumed;
    save_history(s, consumed);
    s->cursor += consumed;
    s->available -= consumed;
    s->phase = (framed || s->available) ? position - consumed : 0;
    s->rendered += canonical_count;
    atomic_fetch_add(&s->presentation, presented);
    if (produced < count) {
      /* The deadline owner takes over after a real underflow. Never resume an
       * unfinished repair window on top of that owner's recovery merge. */
      s->repair_remaining=s->repair_total=0;
      atomic_fetch_add(&m->underruns, 1);
      atomic_fetch_add(&s->underruns, 1);
      uint32_t missing = count - produced;
      if (framed) {
        s->deadline_debt += position - consumed + missing * ratio;
        s->phase = 0;
      }
      if (produced > prefix)
        s->starve_frames = 0;
      s->starve_frames += missing;
      s->deficit_frames += missing;
      if (enhanced && s->continuity.count) {
        for (uint32_t j = produced; j < count; j++) {
          float value[2];
          if (framed && !s->continuity.gap)
            s->continuity.lag = 0;
          if (framed) {
            // Codec/media workers own waveform prediction. A missed software
            // presentation deadline only receives a short bounded envelope;
            // no period search or second prediction runs on this callback.
            bhc_prediction(&s->continuity, s->continuity.gap, value);
            s->continuity.gap++;
            s->continuity.concealed++;
            if (fabsf(value[0]) + fabsf(value[1]) < 1e-10f)
              s->continuity.silent++;
          } else {
            if (!s->continuity.gap)
              atomic_fetch_add(&s->deadline_searches, 1);
            bhc_missing(&s->continuity, value);
          }
          float g = continuity_gate(s, gate_target);
          m->scratch[j * 2] = value[0] * g;
          m->scratch[j * 2 + 1] = value[1] * g;
        }
      }
      if (!enhanced || s->starve_frames >= route->rate / 50 ||
          s->deficit_frames >= route->rate / 50 || !s->continuity.count) {
        s->buffering = 1;
        s->recover_late = 1;
        s->deadline_debt = 0;
        atomic_fetch_add(&s->rebuffers, 1);
      }
    } else {
      s->starve_frames = 0;
      uint32_t recovery = count / 20 + 1;
      s->deficit_frames = s->deficit_frames > recovery ? s->deficit_frames - recovery : 0;
    }
    s->concealed_total +=
        (s->continuity.concealed - before_concealed) * (double)BHT_RATE / route->rate;
    s->silent_total += (s->continuity.silent - before_silent) * (double)BHT_RATE / route->rate;
    s->merged_total += (s->continuity.merged - before_merged) * (double)BHT_RATE / route->rate;
    atomic_store(&s->concealed, (uint64_t)s->concealed_total);
    atomic_store(&s->concealed_silence, (uint64_t)s->silent_total);
    atomic_store(&s->merged, (uint64_t)s->merged_total);
    double ppm = s->drift * 1000000;
    if (ppm < -1000)
      ppm = -1000;
    if (ppm > 1000)
      ppm = 1000;
    atomic_store(&s->drift_view, (unsigned)llround(ppm + 1000));
    int was_ready = atomic_load(&s->consumer_ready) &&
                    atomic_load(&s->consumer_epoch) == epoch &&
                    atomic_load(&s->consumer_generation) == control_generation;
    bhq_source(&s->quality, m->scratch, m->accumulated, m->delayed, count, gain);
    uint32_t support = ratio != 1 || s->phase != 0 || kernel->ratio > 1
                           ? kernel->taps - kernel->centre - 1 : 0;
    consumer_publish(s, epoch, control_generation, support,
                     !s->buffering && (consumed != 0 || was_ready) &&
                     atomic_load(&s->enabled) && atomic_load(&s->connected));
    atomic_fetch_sub(&s->readers, 1);
  }
  uint64_t before = m->quality.limited;
  bhq_process(&m->quality, m->delayed, m->accumulated, output, count, master);
  m->limited_total += (m->quality.limited - before) * (double)BHT_RATE / route->rate;
  atomic_store(&m->limited, (uint64_t)m->limited_total);
  return 1;
}
API int bh_mix_render(Mixer *m, float *output, uint32_t count, float master) {
  unsigned environment = bh_float_enter();
  int result = m && (atomic_load(&m->enhanced) || atomic_load(&m->native_formats))
                   ? render_continuity(m, m->default_route, output, count, master)
                   : render_legacy(m, output, count, master);
  bh_float_leave(environment);
  return result;
}

API int bh_mix_render_route(MixRoute *route, float *output, uint32_t count, float master) {
  if (!route)
    return -1;
  unsigned environment = bh_float_enter();
  Mixer *m = route->owner;
  if (atomic_load_explicit(&m->closing, memory_order_acquire)) {
    bh_float_leave(environment);
    if (!output || count > CAPACITY)
      return -1;
    memset(output, 0, count * 2 * sizeof(float));
    return 0;
  }
  int result =
      route->rate == BHT_RATE && !atomic_load(&m->enhanced) && !atomic_load(&m->native_formats)
          ? render_legacy(m, output, count, master)
          : render_continuity(m, route, output, count, master);
  bh_float_leave(environment);
  return result;
}
