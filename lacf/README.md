# LACF codec (vendored)

These files are the C reference implementation of the LACF frame (spec 11),
copied unchanged from [radiohound/lora-aprs-ideas](https://github.com/radiohound/lora-aprs-ideas)
except for the `#include` paths in `hbi.c` and `hbc.c`, which point at this
folder instead of the original repository layout.

| File | Origin in lora-aprs-ideas |
|---|---|
| `hb.c`, `hb.h`, `hb_payload.h`, `hbi.c`, `hbc.c` | `tools/hb/` |
| `aprs_ppm.c`, `aprs_ppm.h`, `aprs_ppm_model.h` | `reference/` |
| `model_hybridB_info_native.c`, `model_hybridB_comment.c`, `tocall_set1.txt` | `tools/hybrid/tables/` |

Copied from lora-aprs-ideas commit `607ea92` (2 November 2026), LACF 2.0.0
(protocol version 2, table version 0).

`make` in the repository root builds these into `liblacf.so`, which the
station loads with Python's `ctypes`. Keeping the C reference, rather than
rewriting the codec in Python, means the frames this station sends and
accepts are byte-for-byte the frames the firmware sends and accepts.

To update: copy the same files again from a newer lora-aprs-ideas commit,
restore the two include-path edits, update the commit above, and run
`make test`.
