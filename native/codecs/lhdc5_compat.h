/* Host-only adapter, force-included before unchanged vendor headers. Apache-2.0. */
#pragma once
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
/* Production behavior must not depend on inherited diagnostic environment
 * variables, and diagnostics must never block the codec's frame deadline. */
static inline char *bh_lhdc_no_environment(const char *name) { (void)name; return NULL; }
#define getenv bh_lhdc_no_environment
#define printf(...) (0)
#define fprintf(...) (0)

const float *bh_lhdc_window(int mdct_size);
