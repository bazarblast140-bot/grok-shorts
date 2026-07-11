# Grok Shorts

A zero-cost, memory-lean YouTube Shorts pipeline that runs on a free GitHub
Actions runner.

```
trending topic -> Grok (script + image prompts) -> edge-tts voiceover
    -> free Pollinations stills (or a looping background) -> FFmpeg
    -> out/short_<id>.mp4  +  out/short_<id>.json
```

## Cost

| Step | Service | Cost |
| --- | --- | --- |
| Script + image prompts | xAI Grok | pay-per-token (the only paid part) |
| Voiceover | edge-tts | free |
| Images | Pollinations (Flux) | free, no key |
| Assembly + hosting | FFmpeg on GitHub Actions | free |

Set `BG_VIDEO` to a looping mp4 and the image step is skipped entirely — the
run then costs one Grok call and nothing else.

## Setup

```bash
pip install -r requirements.txt

# Prove ffmpeg and the assembly path work — no API calls, no spend.
python main.py selftest

# Model ids drift; confirm what your key can actually use.
export XAI_API_KEY=xai-...
python main.py models

python main.py run
```

On GitHub: add `XAI_API_KEY` under **Settings -> Secrets and variables ->
Actions**. Anything else (`XAI_MODEL`, `TTS_VOICE`, `NUM_SCENES`,
`TARGET_SECONDS`) is an optional repository **variable** with a sane default.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `XAI_API_KEY` | — | Required. Secret. |
| `XAI_MODEL` | `grok-4` | Verify with `python main.py models`. |
| `TTS_VOICE` | `hi-IN-SwaraNeural` | Any edge-tts voice. |
| `TTS_RATE` | `+12%` | Faster reads hold retention. |
| `NUM_SCENES` | `5` | 3–8. |
| `TARGET_SECONDS` | `30` | 15–60. |
| `BG_VIDEO` | — | Path to a looping mp4. Skips image generation. |

## Schedule

`cron-pipeline.yml` runs at 08:00 / 14:00 / 20:00 IST. GitHub's scheduler is
best-effort and routinely runs late or skips under load. For punctual runs,
delete the `schedule:` block and POST to the dispatch endpoint from an external
cron (e.g. cron-job.org) with a classic PAT scoped `repo` + `workflow`:

```
POST https://api.github.com/repos/<owner>/<repo>/actions/workflows/cron-pipeline.yml/dispatches
Authorization: Bearer ghp_...
Accept: application/vnd.github+json

{"ref":"main"}
```

Keep exactly one scheduler. Running both GitHub's cron and an external one
double-posts.

## Output

Each run writes a vertical mp4 and a JSON sidecar carrying the title,
description and tags, uploaded together as a build artifact. Point an upload
step at the sidecar and no second model call is needed.

## Notes on the build

The assembly is deliberately raw FFmpeg rather than MoviePy: each scene is
encoded on its own and the segments are joined with the concat demuxer under
`-c:v copy`, so only one segment is ever in memory and peak usage stays flat.
`libx264` runs at `-threads 1` — its per-thread frame buffers are what tip a
small runner into OOM.
