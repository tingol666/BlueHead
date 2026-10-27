"""Import pinned MIT decoder, add bounds checks, and build a local Windows DLL.

Source repository must be provided explicitly. The encoder is built for tests only.
"""
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
upstream = Path(sys.argv[1]).resolve()
vendor = root / "vendor" / "libldacdec"
vendor.mkdir(parents=True, exist_ok=True)
for path in upstream.glob("*"):
    if path.suffix in (".c", ".h") or path.name == "LICENSE":
        shutil.copy2(path, vendor / path.name)

def patch(file, old, new):
    path = vendor / file
    text = path.read_text()
    if old not in text:
        raise RuntimeError(f"Upstream layout changed: {file}: {old[:50]}")
    path.write_text(text.replace(old, new), encoding="utf-8")

patch("ldacdec.h", '#include "log.h"', '#include "log.h"\n#include "guard.h"')
patch("ldacdec.h", 'int gradient[MAX_QUANT_UNITS];', 'int gradient[50];')
patch("bit_reader.c", '#include "bit_reader.h"', '#include "bit_reader.h"\n#include "guard.h"')
patch("huffCodes.c", '#include "huffCodes.h"', '#include "huffCodes.h"\n#include "guard.h"')
patch("bit_reader.c", 'uint32_t PeekInt(BitReaderCxt* br, const int bits)\n{', '''uint32_t PeekInt(BitReaderCxt* br, const int bits)
{
    LDAC_REQUIRE(bits >= 0 && bits <= 25);
    LDAC_REQUIRE(br->Position >= 0 && br->Position + bits <= ldac_input_length * 8);
    if (bits == 0) return 0;''')
patch("libldacdec.c", 'this->quantizationUnitCount = ga_nqus_ldac[this->nbrBands];', '''LDAC_REQUIRE(this->nbrBands <= LDAC_MAXNBANDS);
    this->quantizationUnitCount = ga_nqus_ldac[this->nbrBands];
    LDAC_REQUIRE(this->quantizationUnitCount <= (this->frameSamples == 128 ? 26 : 34));''')
patch("libldacdec.c", 'this->gradientBoundary = ReadInt( br, LDAC_NADJQUBITS );', '''this->gradientBoundary = ReadInt( br, LDAC_NADJQUBITS );
    LDAC_REQUIRE(this->gradientStartUnit <= 49 && this->gradientEndUnit <= 50);
    LDAC_REQUIRE(this->gradientStartUnit <= this->gradientEndUnit);
    LDAC_REQUIRE(this->gradientBoundary <= this->quantizationUnitCount);''')
patch("libldacdec.c", 'this->channelCount = channelConfigIdToChannelCount[this->channelConfigId];', '''LDAC_REQUIRE(this->sampleRateId < 4 && this->channelConfigId == 2);
    LDAC_REQUIRE(this->frameLength + 3 == ldac_input_length);
    this->channelCount = channelConfigIdToChannelCount[this->channelConfigId];''')
patch("libldacdec.c", 'this->precisionsFine[i] = this->precisions[i] - LDAC_MAXIDWL1;', '''this->precisionsFine[i] = this->precisions[i] - LDAC_MAXIDWL1;
            LDAC_REQUIRE(this->precisionsFine[i] <= LDAC_MAXIDWL2);''')
patch("libldacdec.c", 'calculatePrecisionMask( channel );', '''for (int q = 0; q < frame->quantizationUnitCount; ++q)
                LDAC_REQUIRE(channel->scaleFactors[q] >= 0 && channel->scaleFactors[q] < 32);
            calculatePrecisionMask( channel );''')
patch("spectrum.c", 'int value = decode4DSpectrum[ReadInt( br, LDAC_4DIMSPECBITS )];', '''int value = decode4DSpectrum[ReadInt( br, LDAC_4DIMSPECBITS )];
                    LDAC_REQUIRE(value >= 0);''')
patch("spectrum.c", 'const float stepSizeFine = QuantizerFineStepSize[this->precisions[band]];', '''/* Residual word length is independent of the saturated coarse precision.
     * AOSP quant_residual_ldac quantizes with a_idwl2, not a_idwl1. */
    const float stepSizeFine = QuantizerFineStepSize[this->precisionsFine[band]];''')
patch("spectrum.c", 'if( this->scaleFactors[i] > 0 )', '''/* AOSP norm_spectrum_ldac writes a -0.75 placeholder for ID zero.
         * It denotes an uncoded/zero spectral unit, not unity-scale audio.
         * Clear this unit before IMDCT while retaining its valid overlap. */
        if( this->scaleFactors[i] == 0 )
        {
            memset(spectra + startSubBand, 0,
                   (endSubBand - startSubBand) * sizeof(*spectra));
        }
        else''')
patch("huffCodes.c", 'br->Position += bits;', '''LDAC_REQUIRE(bits > 0);
    br->Position += bits;
    LDAC_REQUIRE(br->Position <= ldac_input_length * 8);''')
(vendor / "guard.h").write_text('''#pragma once
#include <setjmp.h>
extern _Thread_local jmp_buf ldac_exception;
extern _Thread_local int ldac_input_length;
#define LDAC_REQUIRE(condition) do { if (!(condition)) longjmp(ldac_exception, 1); } while (0)
''', encoding="utf-8")
(vendor / "bridge.c").write_text('''/* BlueHead bounded float32 bridge. Upstream decoder: MIT, see LICENSE. */
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdint.h>
#include "ldacdec.h"
_Thread_local jmp_buf ldac_exception;
_Thread_local int ldac_input_length;
__declspec(dllexport) void *bh_ldac_create(void) {
    ldacdec_t *decoder = calloc(1, sizeof(ldacdec_t));
    if (decoder) ldacdecInit(decoder);
    return decoder;
}
__declspec(dllexport) void bh_ldac_free(void *decoder) { free(decoder); }
__declspec(dllexport) int bh_ldac_decode(void *handle, const uint8_t *input,
        int length, float *output, int capacity) {
    if (!handle || !input || !output || length < 4 || length > 515 || capacity < 512)
        return -1;
    uint8_t padded[520] = {0};
    int16_t temporary[512] = {0};
    memcpy(padded, input, length);
    ldac_input_length = length;
    if (setjmp(ldac_exception)) return -2;
    ldacdec_t *decoder = handle;
    int used = 0;
    if (ldacDecode(decoder, padded, temporary, &used) < 0 || used != length) return -3;
    frame_t *frame = &decoder->frame;
    for (int sample = 0; sample < frame->frameSamples; ++sample) {
        for (int channel = 0; channel < 2; ++channel) {
            float value = frame->channels[channel].pcm[sample] / 32768.0f;
            if (!isfinite(value)) return -4;
            output[sample * 2 + channel] = value;
        }
    }
    return frame->frameSamples;
}
''', encoding="utf-8")
assets = root / "bluehead" / "assets"
assets.mkdir(exist_ok=True)
sources = ["bridge.c", "libldacdec.c", "bit_allocation.c", "huffCodes.c", "bit_reader.c", "utility.c", "imdct.c", "spectrum.c"]
subprocess.run(["gcc", "-shared", "-std=gnu11", "-O2", "-static", "-static-libgcc", "-Wl,--no-insert-timestamp", "-o", str(assets / "bluehead_ldac.dll"), *[str(vendor / f) for f in sources], "-lm"], check=True)
encoder = upstream / "libldac"
(root / "tests").mkdir(exist_ok=True)
subprocess.run(["gcc", "-shared", "-std=gnu11", "-O2", "-static-libgcc", "-DLDACBT_API=__declspec(dllexport)", "-I" + str(encoder / "inc"), "-I" + str(encoder / "src"), "-o", str(root / "tests" / "ldac_encoder.dll"), str(encoder / "src" / "ldaclib.c"), str(encoder / "src" / "ldacBT.c"), "-lm"], check=True)
print("LDAC decoder and test encoder built")
