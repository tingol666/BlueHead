/* 2.0.1 opt-in renderer. Included after the original rendering helpers.
 * The unchanged legacy renderer remains an offline comparison oracle.
 */
API int bh_mix_continuity(Mixer *m, int index, int enabled, uint32_t minimum, uint32_t maximum) {
  if (!m || index < 0 || index >= INPUTS || minimum > maximum || maximum > m->input[index].maximum)
    return -1;
  Input *s = &m->input[index];
  atomic_store(&s->continuity_min, minimum);
  atomic_store(&s->continuity_max, maximum);
  atomic_store_explicit(&s->continuity_mode, !!enabled, memory_order_release);
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
    s->gate = fminf(target, s->gate + 1.0f / 480);
  else if (s->gate > target)
    s->gate = fmaxf(target, s->gate - 1.0f / 480);
  return s->gate;
}
static int similar_front(Input *s, uint32_t offset, uint32_t lag) {
  double xy = 0, xx = 0, yy = 0;
  for (uint32_t j = 0; j < 64; j++)
    for (int ch = 0; ch < 2; ch++) {
      double a = sample(s, (int)(offset + j), ch), b = sample(s, (int)(offset + j + lag), ch);
      xy += a * b;
      xx += a * a;
      yy += b * b;
    }
  return xx > 1e-12 && yy > 1e-12 && xy / sqrt(xx * yy) > .85 && xx / yy > .5 && xx / yy < 2;
}
API int bh_mix_render(Mixer *m, float *output, uint32_t count, float master) {
  if (!m || !output || count > CAPACITY || !isfinite(master))
    return -1;
  if (!atomic_load(&m->enhanced))
    return render_legacy(m, output, count, master);
  memset(output, 0, count * 2 * sizeof(float));
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
    int enhanced = atomic_load_explicit(&s->continuity_mode, memory_order_acquire);
    uint64_t w = atomic_load_explicit(&s->write, memory_order_acquire),
             trim = atomic_load_explicit(&s->trim, memory_order_acquire);
    if (trim > w)
      trim = w;
    unsigned epoch = atomic_load_explicit(&s->epoch, memory_order_acquire);
    if (trim > s->cursor || epoch != s->seen_epoch) {
      if (trim > s->cursor) {
        if (epoch == s->seen_epoch)
          atomic_fetch_add(&s->dropped, trim - s->cursor);
        s->cursor = trim;
      }
      s->history_count = 0;
      s->phase = s->drift = 0;
      s->buffering = 1;
      s->seen_epoch = epoch;
      s->starve_frames = s->deficit_frames = 0;
      uint64_t concealed = s->continuity.concealed, silent = s->continuity.silent,
               merged = s->continuity.merged;
      bhc_reset(&s->continuity);
      s->continuity.concealed = concealed;
      s->continuity.silent = silent;
      s->continuity.merged = merged;
    }
    s->available = w - s->cursor;
    uint32_t target = s->target;
    uint32_t old_target = s->adaptive_target;
    if (enhanced) {
      uint32_t floor = atomic_load(&s->continuity_min), ceiling = atomic_load(&s->continuity_max);
      uint64_t desired = (uint64_t)floor + atomic_load(&s->jitter_view) + count / 2;
      if (desired > ceiling)
        desired = ceiling;
      if (!s->adaptive_target)
        s->adaptive_target = target;
      if (desired > s->adaptive_target)
        s->adaptive_target = (uint32_t)desired;
      else if (s->rendered > 240000 && s->adaptive_target > desired) {
        uint32_t step = count / 480;
        if (!step)
          step = 1;
        s->adaptive_target =
            s->adaptive_target - desired > step ? s->adaptive_target - step : (uint32_t)desired;
      }
      target = s->adaptive_target;
      atomic_store(&s->target_view, target);
      if (old_target && old_target != target && s->rendered) {
        s->retarget_remaining += (int32_t)old_target - (int32_t)target;
        s->target_changed_at = s->rendered;
      }
      if (s->rendered > s->target_changed_at + 240000)
        s->retarget_remaining = 0;
    }
    int smooth = target && (enhanced || period > 0 || call == i);
    uint32_t observed_target = target;
    if (smooth) {
      uint64_t centre = (uint64_t)target + count / 2;
      observed_target = (uint32_t)(centre > s->maximum ? s->maximum : centre);
    }
    if (s->buffering && s->available < observed_target) {
      atomic_fetch_add(&s->prefill, count);
      atomic_store_explicit(&s->read, s->cursor, memory_order_release);
      atomic_fetch_sub(&s->readers, 1);
      continue;
    }
    if (s->buffering) {
      s->clock_average = observed_target;
      s->clock_correction = 1;
      s->drift = 0;
      if (enhanced && s->rendered) {
        s->continuity.gap = 960;
        s->continuity.merge = s->continuity.merge_length = 192;
      }
    }
    s->buffering = 0;
    float gain = number(atomic_load(&s->gain)) * ((call >= 0 && call != i) ? .15f : 1);
    float gate_target = atomic_load(&s->enabled) ? 1 : 0;
    uint32_t prefix = 0;
    /* Bounded, correlated whole-period overlap is used only for buffer
     * retargeting. It preserves pitch and shares timing across channels.
     * Transients with poor waveform similarity are left untouched. */
    if (enhanced && abs(s->retarget_remaining) >= 32 && s->rendered > s->last_stretch + 9600 &&
        s->continuity.count >= 512 && s->available > count + 320) {
      uint32_t lag = bhc_period(&s->continuity, 240);
      if (lag && lag < count / 2 && similar_front(s, 0, lag)) {
        if (s->retarget_remaining >= (int32_t)lag &&
            s->available > (uint64_t)observed_target + count + lag) {
          save_history(s, lag);
          s->cursor += lag;
          s->available -= lag;
          atomic_fetch_add(&s->accelerated, lag);
          s->retarget_remaining -= lag;
          s->clock_average -= lag;
          s->continuity.lag = lag;
          s->continuity.gap = lag;
          s->continuity.merge = s->continuity.merge_length = 64;
          s->last_stretch = s->rendered;
        } else if (s->retarget_remaining <= -(int32_t)lag && s->available + lag < observed_target &&
                   lag < count) {
          s->continuity.lag = lag;
          for (uint32_t j = 0; j < lag; j++) {
            float p[2] = {s->continuity.period[j * 2], s->continuity.period[j * 2 + 1]}, merged[2];
            bhc_real(&s->continuity, p, merged);
            float g = gain * continuity_gate(s, gate_target);
            output[j * 2] += merged[0] * g;
            output[j * 2 + 1] += merged[1] * g;
          }
          prefix = lag;
          atomic_fetch_add(&s->expanded, lag);
          s->retarget_remaining += lag;
          s->clock_average += lag;
          s->continuity.gap = lag;
          s->continuity.merge = s->continuity.merge_length = 64;
          s->last_stretch = s->rendered;
        }
      }
    }
    double ratio = 1;
    if (smooth) {
      /* The supported +/-500ppm source mismatch needs correction headroom.
       * A clamp at the source limit rectifies jitter and slowly drains/fills
       * the queue even after the rate estimate converges to that limit. */
      ratio = clock_update(s, count, observed_target, (period > 0 ? period : 3) * 48000,
                           period > 0 ? limit : .001);
    } else if (s->target && s->rendered > 96000) {
      double wanted = ((double)s->available - s->target) / 48000 * .02;
      if (wanted > .0005)
        wanted = .0005;
      if (wanted < -.0005)
        wanted = -.0005;
      s->drift += .005 * (wanted - s->drift);
      ratio += s->drift;
    }
    uint32_t produced = prefix;
    double position = s->phase;
    for (; produced < count && (uint64_t)position < s->available; produced++, position += ratio) {
      int offset = (int)position;
      float fraction = (float)(position - offset), in[2], value[2];
      for (int ch = 0; ch < 2; ch++) {
        in[ch] = fraction == 0 ? sample(s, offset, ch) : interpolate(m, s, offset, ch, fraction);
        if (!isfinite(in[ch]))
          in[ch] = 0;
      }
      if (enhanced)
        bhc_real(&s->continuity, in, value);
      else {
        value[0] = in[0];
        value[1] = in[1];
      }
      float g = gain * continuity_gate(s, gate_target);
      output[produced * 2] += value[0] * g;
      output[produced * 2 + 1] += value[1] * g;
    }
    uint32_t consumed = (uint32_t)position;
    if (consumed > s->available)
      consumed = (uint32_t)s->available;
    save_history(s, consumed);
    s->cursor += consumed;
    s->available -= consumed;
    s->phase = s->available ? position - consumed : 0;
    s->rendered += count;
    if (produced < count) {
      atomic_fetch_add(&m->underruns, 1);
      atomic_fetch_add(&s->underruns, 1);
      uint32_t missing = count - produced;
      if (produced > prefix)
        s->starve_frames = 0;
      s->starve_frames += missing;
      s->deficit_frames += missing;
      if (enhanced && s->continuity.count) {
        for (uint32_t j = produced; j < count; j++) {
          float value[2];
          bhc_missing(&s->continuity, value);
          float g = gain * continuity_gate(s, gate_target);
          output[j * 2] += value[0] * g;
          output[j * 2 + 1] += value[1] * g;
        }
        atomic_store(&s->concealed, s->continuity.concealed);
        atomic_store(&s->concealed_silence, s->continuity.silent);
      }
      if (!enhanced || s->starve_frames >= 960 || s->deficit_frames >= 960 ||
          !s->continuity.count) {
        s->buffering = 1;
        atomic_fetch_add(&s->rebuffers, 1);
      }
    } else {
      s->starve_frames = 0;
      uint32_t recovery = count / 20 + 1;
      s->deficit_frames = s->deficit_frames > recovery ? s->deficit_frames - recovery : 0;
    }
    atomic_store(&s->merged, s->continuity.merged);
    double ppm = s->drift * 1000000;
    if (ppm < -1000)
      ppm = -1000;
    if (ppm > 1000)
      ppm = 1000;
    atomic_store(&s->drift_view, (unsigned)llround(ppm + 1000));
    atomic_store_explicit(&s->read, s->cursor, memory_order_release);
    atomic_fetch_sub(&s->readers, 1);
  }
  float peak = 0;
  for (uint32_t j = 0; j < count * 2; j++) {
    output[j] *= master;
    float value = fabsf(output[j]);
    if (value > peak)
      peak = value;
  }
  if (peak > .98f) {
    float scale = .98f / peak;
    for (uint32_t j = 0; j < count * 2; j++)
      output[j] *= scale;
  }
  return 1;
}
