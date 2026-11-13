# Compression benchmark and A/B viewer

Tools for comparing image encoders against the hosted display files, and a standalone
browser page for judging the results by eye. Nothing here touches R2 or the pipeline.

## Requirements
- A Python venv with `numpy pillow ssimulacra2 imagecodecs pillow-jxl-plugin`.
- Homebrew `libavif` (`avifenc`) and `webp` (`cwebp`, `webpinfo`).
- The source drive mounted, so masters can be read.

## Reproduce the A/B test
Pick any empty work directory (outside the repo) and run from the repo root:

```bash
W=/path/to/work
python tools/compression_bench/make_sample.py $W 60 7   # 60 random photos, seed 7: resized masters as PNG
python tools/compression_bench/bench.py $W              # encode every variant on a q grid, score, cache in results.jsonl
python tools/compression_bench/analyze.py $W            # size vs quality summary per encoder
python tools/compression_bench/search.py $W             # per-photo q search for the AVIF targets, writes picks.json
python tools/compression_bench/build_ab.py $W /path/to/ab-out   # standalone viewer; open ab-out/index.html
```

- Reference for every score: the master resized exactly as `process_trip.py` does.
- Scores: SSIMULACRA2 plus luma and chroma PSNR. `bench.py` and `search.py` are resumable
  (results cached per image and variant). AVIF at speed 4 is slow, so a full run takes hours.
- `search.py` targets: `strict` (no measure below the hosted file) and `score` (SSIMULACRA2 only).
- The viewer opens via `file://`: keys `1`-`4` switch version, `0` master, `Space` flips the last
  two, `Z` fit/100%, wheel zoom, drag to pan, blind mode with votes kept in localStorage.
- The A/B output contains copies of real photos. Keep it local; do not commit or deploy it.

## Re-encode leftover q90 display files at q85
`requeue_q90.py` finds hosted display files still encoded at WebP q90 (`webpinfo` Base Q 12),
verifies each against its master (same size, PSNR >= 25 dB) and re-encodes from the master
with the pipeline's own settings. Dry run by default:

```bash
python tools/compression_bench/requeue_q90.py <slug> [<slug> ...]          # report only
python tools/compression_bench/requeue_q90.py --apply <slug> [<slug> ...]  # write in place
```

Then upload with `deploy.py --skip-pages --trip <slug>` (dry run first).
