"""Build the pinned LHDC V5 math with a verified, isolated float adapter."""

from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import tempfile
from native_build import compiler, includes

ROOT = Path(__file__).resolve().parents[1]


def replace_function(source, signature, body):
    if source.count(signature) != 1:
        raise RuntimeError("Pinned function signature changed: " + signature)
    start = source.index("{", source.index(signature))
    level = 1
    end = start + 1
    while level:
        level += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[:start] + "{" + body + "}" + source[end:]


def adapt_decoder(source):
    source = replace_function(
        source,
        "static const float *lhdc_dec_get_window(int mdct_size)",
        "\n    return bh_lhdc_window(mdct_size);\n",
    )
    source = replace_function(
        source, "static float lhdc_dec_rate_norm(int mdct_size)", "\n    return (float)mdct_size / 512.0f;\n"
    )
    source = replace_function(
        source,
        "static float lhdc_dec_level_cal(uint32_t sample_rate, int bit_depth)",
        "\n    (void)sample_rate; (void)bit_depth; return 1.0f;\n",
    )
    old = "/* Fall back to silence on this channel if ch1 fails. */\n                for (int n = 0; n < samples_per_ch; n++) out_r[n] = 0.0f;"
    if source.count(old) != 1:
        raise RuntimeError("Pinned channel error path changed")
    source = source.replace(old, "return ret;")
    # Use defined modulo-32 bit patterns, preserving the codec's fixed-point arithmetic.
    old = "int mag = ((int)coeff[k] << s) | mant;"
    if source.count(old) != 1:
        raise RuntimeError("Pinned mantissa expression changed")
    source = source.replace(old, "int mag = (int32_t)(((uint32_t)coeff[k] << s) | (uint32_t)mant);")
    start = source.index("    /* Interleave to output (l/r already include the bit-depth scale `sc`). */")
    end = source.index("    #undef LHDC_OUT_SAMPLE", start) + len("    #undef LHDC_OUT_SAMPLE")
    replacement = """    /* BlueHead private ABI publishes complete, validated normalized float. */
    if (conceal) return LHDC_DEC_BITSTREAM_ERROR;
    float normalized = ldexpf(sc, 1 - out_bit_depth);
    float *output = (float *)out_pcm;
    for (int n = 0; n < samples_per_ch; ++n) {
        output[n * channels] = out_l[n] * normalized;
        if (channels >= 2) output[n * channels + 1] = (do_stereo ? out_r[n] : out_l[n]) * normalized;
    }
"""
    return source[:start] + replacement + source[end:]


def adapt_entropy(source):
    replacements = [
        (
            "while (s2_at(z, j) == 2) {\n            j++;",
            "while (s2_at(z, j) == 2) {\n            if (shift > 28) { z->eos = 1; *jp = j; return -1; }\n            j++;",
            1,
        ),
        (
            "return (int)(low | ((uint32_t)(ones + 1) << shift));",
            "if ((uint32_t)(ones + 1) > (0x7fffffffu >> shift)) { z->eos = 1; return -1; }\n        return (int)(low | ((uint32_t)(ones + 1) << shift));",
            1,
        ),
        (
            "int q = rice_read_quotient(z, &j);",
            "int q = rice_read_quotient(z, &j);\n                if (q < 0 || q > 0x3ffffffd || z->eos) { z->eos = 1; return; }",
            2,
        ),
        ("(q << 1) | s", "(int32_t)(((uint32_t)q << 1) | (uint32_t)s)", 1),
        (
            "rice_decode_coeffs_used(s1, &z, count, split, quant_spectrum, &s2_used);",
            "rice_decode_coeffs_used(s1, &z, count, split, quant_spectrum, &s2_used);\n    if (z.eos) return LHDC_DEC_BITSTREAM_ERROR;",
            1,
        ),
    ]
    for before, after, count in replacements:
        if source.count(before) != count:
            raise RuntimeError("Pinned Rice safety boundary changed: " + before)
        source = source.replace(before, after)
    return source


def build(output):
    vendor = ROOT / "vendor/lhdc5-decoder"
    record = json.loads((vendor / "origin.json").read_text("utf-8"))
    for name, expected in record["files_sha256"].items():
        if hashlib.sha256((vendor / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Modified pinned LHDC source: " + name)
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bluehead-lhdc5-") as temporary:
        temporary = Path(temporary)
        adapted = temporary / "lhdc_dec.c"
        adapted.write_text(adapt_decoder((vendor / "lhdc_dec.c").read_text("utf-8")), "utf-8")
        entropy = temporary / "lhdc_entropy_dec.c"
        entropy.write_text(adapt_entropy((vendor / "lhdc_entropy_dec.c").read_text("utf-8")), "utf-8")
        objects = []
        sources = [
            adapted,
            entropy,
            *(
                vendor / name
                for name in (
                    "lhdc_bit_reader.c",
                    "lhdc_diag_config.c",
                    "lhdc_imdct.c",
                    "lhdc_sns_synth.c",
                    "lhdc_tables.c",
                )
            ),
            ROOT / "native/codecs/lhdc5_stream.c",
            ROOT / "native/codecs/lhdc5_media.c",
        ]
        for source in sources:
            obj = temporary / (source.stem + ".o")
            own = source.is_relative_to(ROOT / "native")
            subprocess.run(
                [
                    compiler(),
                    "-std=c11",
                    "-O3",
                    "-Wall",
                    "-Wextra",
                    "-fwrapv",
                    *(
                        ["-Werror"]
                        if own
                        else [
                            "-Wno-unused-variable",
                            "-Wno-unused-parameter",
                            "-Wno-unused-value",
                            "-Wno-unused-function",
                            "-Wno-unused-but-set-variable",
                            "-Wno-misleading-indentation",
                            "-Wno-sign-compare",
                        ]
                    ),
                    "-DLHDC_HOST_BUILD",
                    *includes(vendor),
                    *([] if own else ["-include", str(ROOT / "native/codecs/lhdc5_compat.h")]),
                    "-c",
                    str(source),
                    "-o",
                    str(obj),
                ],
                check=True,
            )
            objects.append(str(obj))
        subprocess.run(
            [
                compiler(),
                "-shared",
                "-static-libgcc",
                "-s",
                "-Wl,--no-insert-timestamp",
                "-Wl,--exclude-all-symbols",
                *objects,
                "-o",
                str(output),
                "-lm",
            ],
            check=True,
        )
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "bluehead/assets/bluehead_lhdc5.dll")
    print(build(parser.parse_args().output))
