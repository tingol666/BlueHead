/* Minimal Windows scalar port for the isolated SpanDSP PLC component.
 * Copyright 2026 BlueHead contributors.
 * SPDX-License-Identifier: LGPL-2.1-only
 * No algorithm changes: supply only the original platform helper operations.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <limits.h>
#ifdef _WIN32
#define SPAN_DECLARE(type) __declspec(dllexport) type
#else
#define SPAN_DECLARE(type) __attribute__((visibility("default"))) type
#endif
#include "src/spandsp/plc.h"
#include "src/spandsp/private/plc.h"
#define span_alloc malloc
#define span_free free
#define vec_copyi16(dst, src, len) memcpy((dst), (src), (size_t)(len) * sizeof(int16_t))
#define vec_movei16(dst, src, len) memmove((dst), (src), (size_t)(len) * sizeof(int16_t))
static inline int16_t fsaturate(float value) {
    if (value > 32767.0f) return 32767;
    if (value < -32768.0f) return -32768;
    return (int16_t)lrintf(value);
}
