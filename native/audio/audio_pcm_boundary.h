/* PCM-boundary provenance. Apache-2.0. Shared ring cursor/epoch with samples. */
#ifndef BLUEHEAD_AUDIO_PCM_BOUNDARY_H
#define BLUEHEAD_AUDIO_PCM_BOUNDARY_H
#define BH_PCM_BOUNDARY_UNCERTAIN 1u
#define BH_PCM_BOUNDARY_REPAIR 2u
/* Decoder repair is also an uncertainty/deadline barrier. Ordinary
 * uncertainty alone never requests waveform treatment. */
#define BH_PCM_BOUNDARY_DECODER_REPAIR (BH_PCM_BOUNDARY_UNCERTAIN | BH_PCM_BOUNDARY_REPAIR)
#endif
