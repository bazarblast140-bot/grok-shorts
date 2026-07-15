"""
Grok Shorts — a zero-cost, memory-lean YouTube Shorts pipeline.

Pipeline:
    trending topic -> xAI Grok (script + image prompts) -> edge-tts voiceover
    -> visuals (free Pollinations images, or a looping stock background)
    -> FFmpeg assembly -> out/short_<id>.mp4 (+ .json sidecar for upload)

Designed to run on a GitHub Actions free runner: no GPU, no paid media APIs,
and a strictly streaming FFmpeg assembly so it never OOMs.

Usage:
    python main.py run       # build one Short (and upload, if configured)
    python main.py reauth    # LOCAL ONLY: redo Google consent -> token.json
    python main.py models    # list model ids your XAI_API_KEY can use
    python main.py selftest  # offline check: ffmpeg + assembly, no API calls

Environment:
    XAI_API_KEY   (required)  xAI API key
    XAI_MODEL     (optional)  default "grok-4.5"
    TTS_VOICE     (optional)  default "hi-IN-SwaraNeural"
    TTS_RATE      (optional)  default "+12%"
    NUM_SCENES    (optional)  default 5
    TARGET_SECONDS(optional)  default 30
    BG_VIDEO      (optional)  path to a looping background mp4; if set, images
                              are skipped entirely (cheapest + fastest path)

    VISUAL_MODE   (optional)  what a run is worth, in one dial:
                    free_image  Pollinations stills + Ken Burns   free  (default)
                    grok_image  Grok stills + Ken Burns           ~$0.10/video
                    video       Grok clips, real motion           ~$0.05/second
                  A 25s Short is therefore ~$1.25 in video mode and ~$0.10 in
                  grok_image mode. Motion is ~60x the cost of stills; nothing
                  else in the pipeline comes close.

    UPLOAD_YOUTUBE   (optional) "false" to build only. Default: upload if a
                                token.json is present.
    YOUTUBE_PRIVACY  (optional) public | unlisted | private. Default public.
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (optional)
                                Success and failure notifications.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "out"
WORK_DIR = ROOT / "work"

XAI_BASE_URL = "https://api.x.ai/v1"
POLLINATIONS = "https://image.pollinations.ai/prompt/"

VIDEO_W, VIDEO_H, FPS = 1080, 1920, 30

# What the visuals cost, per xAI's published rates. Motion is the expensive
# part by two orders of magnitude — the mode is a dial, not a detail.
#   video       $0.05/second   -> a 25s Short costs ~$1.25
#   grok_image  $0.02/image    -> a 5-scene Short costs ~$0.10
#   free_image  free
VISUAL_MODES = ("video", "grok_image", "free_image")
VIDEO_COST_PER_SECOND = 0.05
GROK_IMAGE_COST = 0.02

# grok-imagine-video accepts 1–15s per clip, so a Short is stitched from one
# clip per scene.
CLIP_MIN_S, CLIP_MAX_S = 1, 15

# YouTube OAuth. token.json is written by `reauth` locally; on the runner the
# workflow materialises it from the TOKEN_JSON secret.
TOKEN_PATH = ROOT / "token.json"
CLIENT_SECRET = ROOT / "client_secret.json"
YOUTUBE_SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_CATEGORY_ID = "1"  # Film & Animation


@dataclass(frozen=True)
class Config:
    xai_api_key: str
    xai_model: str = "grok-4.5"
    tts_voice: str = "hi-IN-SwaraNeural"
    tts_rate: str = "+12%"
    tts_provider: str = "edge"
    grok_voice: str = "rex"
    grok_speed: float = 1.0
    num_scenes: int = 5
    target_seconds: int = 30
    bg_video: str = ""
    visual_mode: str = "free_image"
    video_model: str = "grok-imagine-video"
    video_resolution: str = "720p"
    image_model: str = "grok-imagine-image"
    upload_youtube: bool = False
    youtube_privacy: str = "public"
    http_timeout: int = 90
    poll_timeout_s: int = 15 * 60
    poll_interval_s: int = 5
    retries: int = 3

    @classmethod
    def from_env(cls) -> "Config":
        key = os.environ.get("XAI_API_KEY", "").strip()
        if not key:
            raise ConfigError(
                "XAI_API_KEY is not set. Locally: set it in your shell. "
                "On GitHub Actions: add it under "
                "Settings -> Secrets and variables -> Actions."
            )

        # Upload by default when a token exists, so a run that has credentials
        # never silently builds a video and sits on it. UPLOAD_YOUTUBE=false
        # forces build-only.
        raw_upload = os.environ.get("UPLOAD_YOUTUBE", "").strip().lower()
        if raw_upload in ("false", "0", "no"):
            upload = False
        elif raw_upload in ("true", "1", "yes"):
            upload = True
        else:
            upload = TOKEN_PATH.exists()

        privacy = os.environ.get("YOUTUBE_PRIVACY", "public").strip().lower()
        if privacy not in ("public", "unlisted", "private"):
            log.warning("YOUTUBE_PRIVACY=%r is invalid; using 'public'.", privacy)
            privacy = "public"

        # Defaults to the free path: an unset variable must never quietly start
        # spending money.
        mode = os.environ.get("VISUAL_MODE", "free_image").strip().lower()
        if mode not in VISUAL_MODES:
            log.warning(
                "VISUAL_MODE=%r is not one of %s; using 'free_image'.",
                mode, ", ".join(VISUAL_MODES),
            )
            mode = "free_image"

        provider = os.environ.get("TTS_PROVIDER", "edge").strip().lower()
        if provider not in ("edge", "grok"):
            log.warning("TTS_PROVIDER=%r is not 'edge' or 'grok'; using 'edge'.",
                        provider)
            provider = "edge"

        return cls(
            xai_api_key=key,
            xai_model=os.environ.get("XAI_MODEL", "grok-4.5").strip(),
            tts_provider=provider,
            grok_voice=os.environ.get("GROK_VOICE", "rex").strip().lower(),
            grok_speed=_env_float("GROK_SPEED", 1.0, lo=0.7, hi=1.5),
            tts_voice=os.environ.get("TTS_VOICE", "hi-IN-SwaraNeural").strip(),
            tts_rate=os.environ.get("TTS_RATE", "+12%").strip(),
            num_scenes=_env_int("NUM_SCENES", 5, lo=3, hi=8),
            target_seconds=_env_int("TARGET_SECONDS", 30, lo=15, hi=60),
            bg_video=os.environ.get("BG_VIDEO", "").strip(),
            visual_mode=mode,
            video_model=os.environ.get(
                "VIDEO_MODEL", "grok-imagine-video").strip(),
            video_resolution=os.environ.get(
                "VIDEO_RESOLUTION", "720p").strip(),
            image_model=os.environ.get(
                "IMAGE_MODEL", "grok-imagine-image").strip(),
            upload_youtube=upload,
            youtube_privacy=privacy,
        )


class ConfigError(RuntimeError):
    """Something the operator must fix (missing key, bad value)."""


class OutOfCredits(ConfigError):
    """The xAI team's balance ran out. Distinct from ConfigError because a run
    that has already paid for some clips should still ship a video from them."""


class PipelineError(RuntimeError):
    """A step failed after its retries were exhausted."""


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        log.warning("%s=%r is not a number; using %d.", name, raw, default)
        return default


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(hi, float(raw)))
    except ValueError:
        log.warning("%s=%r is not a number; using %s.", name, raw, default)
        return default


# --------------------------------------------------------------------------- #
#  Logging
# --------------------------------------------------------------------------- #
def build_logger() -> logging.Logger:
    # Hindi + emoji must survive a cp1252 Windows console and the Actions log.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    logger = logging.getLogger("grok-shorts")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S"
    )
    if sys.stdout is not None:  # None under some headless schedulers
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


log = build_logger()


# --------------------------------------------------------------------------- #
#  FFmpeg discovery
# --------------------------------------------------------------------------- #
def ffmpeg_bin() -> str:
    """System ffmpeg if present (the runner apt-installs it), else the wheel."""
    for candidate in ("ffmpeg", "ffmpeg.exe"):
        try:
            subprocess.run([candidate, "-version"], capture_output=True, check=True)
            return candidate
        except (OSError, subprocess.CalledProcessError):
            continue
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(
            "No ffmpeg found. Install it (apt-get install ffmpeg) or "
            f"pip install imageio-ffmpeg. ({exc})"
        ) from exc


def run_ffmpeg(args: list[str], what: str) -> None:
    """Run one ffmpeg command, surfacing only the tail of stderr on failure."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        raise PipelineError(f"ffmpeg failed ({what}): " + " / ".join(tail))


def audio_duration(path: Path) -> float:
    """Duration in seconds, read via ffmpeg (avoids pulling in MoviePy)."""
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr or "")
    if not match:
        raise PipelineError(f"Could not read duration of {path.name}.")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


# --------------------------------------------------------------------------- #
#  Step 1 — story topic
# --------------------------------------------------------------------------- #
# Topics come from a bank of invented vegetable characters, NOT from Google
# Trends. Trends surfaces real people and live news, and a model asked for a
# viral hook about a real person will simply invent one — which is how this
# pipeline once published a fabricated "shocking truth" about a named actor.
# Fictional characters make that failure impossible by construction.
TOPICS_FILE = ROOT / "story_topics.json"

FALLBACK_TOPICS = [
    "आलू ने मिर्ची पर भरोसा किया, मिर्ची ने पीठ पीछे धोखा दिया",
    "गरीब आलू की नौकरी छूट गई, घर चलाना मुश्किल हो गया",
    "खीरा और मूली की अनोखी प्रेम कहानी जिसे किसी ने नहीं समझा",
    "घमंडी शिमला मिर्च को उसके घमंड ने ही गिरा दिया",
]


def pick_topic() -> str:
    """One story seed from the bank."""
    try:
        bank = json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
        pool = [
            topic
            for key, topics in bank.items()
            if not key.startswith("_")
            for topic in topics
        ]
        if pool:
            topic = random.choice(pool)
            log.info("Topic: %s", topic)
            return topic
        log.warning("%s has no topics; using the built-in bank.", TOPICS_FILE.name)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read %s (%s); using the built-in bank.",
                    TOPICS_FILE.name, str(exc)[:120])

    topic = random.choice(FALLBACK_TOPICS)
    log.info("Topic (fallback): %s", topic)
    return topic


# --------------------------------------------------------------------------- #
#  Step 2 — Grok writes the script
# --------------------------------------------------------------------------- #
@dataclass
class Script:
    title: str
    description: str
    tags: list[str]
    narration: str
    image_prompts: list[str] = field(default_factory=list)
    mood: str = "sad"


# Background music, chosen to match the story. Drop tracks into
# assets/music/<mood>/ and they are picked up automatically; with the folders
# empty the pipeline simply runs voiceover-only, exactly as it does today.
#
# Source the tracks from the YouTube Audio Library (studio.youtube.com ->
# Audio Library). It is the only library that carries no Content ID risk on the
# channel that uploads them.
MUSIC_DIR = ROOT / "assets" / "music"
MOODS = ("sad", "hopeful", "tense", "happy")
MUSIC_VOLUME = 0.05   # well under the narration — a faint bed, never competing
MUSIC_FADE_S = 2.0


SYSTEM_PROMPT = (
    "You write emotional Hindi cartoon stories about invented vegetable "
    "characters. You reply with JSON only — no prose, no markdown fences."
)

# Hard content boundary. The bank only holds fictional characters, but a model
# reaching for a viral hook will drift toward real names and real news unless
# it is told not to. Stated once, plainly, at the top of the prompt.
CONTENT_RULES = """ABSOLUTE RULES — breaking any of these makes the video unusable:
- Every character is an INVENTED VEGETABLE. Never mention, imply, or build on a
  real person, celebrity, actor, politician, brand, company or news event.
- Never present anything as real news, a real fact, or a "shocking truth".
  This is an openly fictional cartoon story and nothing else.
- Keep it family-friendly: sad and emotional is good; violence, death, romance
  beyond innocent affection, politics and religion are not."""

# One of these is drawn at random per run and handed to the model as the shape
# of the opening line. Left to itself the model settles into one favourite
# opening and every Short starts the same way, which is what makes a feed of
# them feel machine-made.
HOOK_STYLES = [
    "a blunt statement of the injustice, naming the character who did it",
    "a question fired straight at the viewer about what they would have done",
    "the last line of the story, told first — then rewind into it",
    "a single line of raw dialogue shouted by a character mid-scene",
    "a countdown of what the character lost, one thing at a time",
    "an innocent, ordinary moment — with one detail that is clearly wrong",
    "a promise to the viewer that they will not see the ending coming",
    "the villain's cruel line, quoted with no introduction at all",
    "a number or a time that sounds small but lands hard",
    "a warm, happy opening that turns cold in the very next sentence",
]

SCRIPT_PROMPT = """{rules}

Write a {seconds}-second vertical Hindi YouTube Short — an emotional 3D-cartoon
vegetable story, in the style of channels like "Veggie Shorts TV".

STORY SEED: {topic}

Structure the narration as HOOK -> STORY -> TWIST -> FEELING:
- HOOK (first 3 seconds): a line that stops the scroll. For THIS video the hook
  must take this shape: {hook_style}
  Never open with "रुको". Do not reuse a stock opener — this hook must feel
  written for this story alone.
- STORY: what happens, told simply and concretely.
- TWIST: the betrayal, the sacrifice, or the moment that turns it.
- FEELING: a one-line emotional landing or moral. End with a short question to
  the viewer so they comment.

Rules:
- Narration: ONE flowing spoken paragraph in natural conversational Hindi
  (Devanagari), roughly {words} words so it reads aloud in about {seconds}
  seconds. No labels, no stage directions, no emoji.
- Pick 2 or 3 vegetable characters and give them simple Hindi names. Decide
  their look ONCE (colour, clothes, eyes, expression) and describe them with
  those SAME words in every image_prompt, so the character stays recognisable
  across the video.
- Write exactly {scenes} image_prompts, one per scene, in ENGLISH. Each: a
  cinematic 3D Pixar-style animated frame of expressive vegetable characters
  with big emotional eyes, in an Indian village or market setting, dramatic
  lighting, vertical 9:16 composition. Absolutely NO text, letters, words,
  numbers or logos inside the image.
- Each image_prompt must carry the emotion of its beat (sad, betrayed, hopeful).

- image_prompts[0] IS THE VISUAL HOOK and follows different rules from the rest.
  It is what the viewer sees in the first 2-3 seconds and it decides whether they
  stay, so it must be FULL OF MOTION AND ACTION from the very first frame. It
  must NOT be a calm establishing shot, a wide village view, or a character
  sitting still or crying quietly — those get scrolled past.
  Make it the most DYNAMIC, high-energy instant of the whole story, caught
  mid-movement: a character running, falling, being shoved, spinning around,
  something crashing or flying, a sudden reveal — with a strong fast camera
  push-in. Describe the movement explicitly (e.g. "sprinting toward camera",
  "stumbling backward", "whirling around in shock") so the animation moves
  immediately. Take this peak moment even though the narration has not reached it
  yet — it only pulls the viewer in. The spoken hook and this motion hit together.

SEO — this decides whether anyone sees the video:
- title: under 90 characters. Hindi + a couple of English keywords, 1-2 emoji,
  and a curiosity gap. Example shape:
  "😢 गरीब आलू की कहानी | Emotional Vegetable Story | Moral Cartoon"
- description: 2-3 sentences that retell the hook and invite a comment.
- tags: 12 lowercase keyword strings mixing Hindi-in-English and English
  (e.g. "veggie story", "emotional cartoon", "hindi kahani", "moral story").
- mood: the ONE word that best fits the background music this story wants —
  exactly one of: sad, hopeful, tense, happy.

Return exactly this JSON shape:
{{"title": "...", "description": "...", "tags": ["..."], "mood": "sad",
  "narration": "...", "image_prompts": ["...", "..."]}}"""

# Appended to whatever the model writes, so every upload carries the channel's
# core keywords even on a run where the model was stingy with tags.
SEO_BASE_TAGS = [
    "veggie shorts", "vegetable story", "emotional story", "hindi kahani",
    "moral story", "cartoon story", "3d animation", "sabzi cartoon",
    "hindi story", "veggie cartoon",
]

SEO_HASHTAGS = (
    "#shorts #veggieshorts #vegetablestory #emotionalstory #hindikahani "
    "#moralstory #cartoon #3danimation #sabzi #hindistory #kahani #viral"
)


def extract_json(text: str) -> dict:
    """Parse a JSON object out of a model reply, fences or prose notwithstanding."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise PipelineError(f"Grok returned unparseable JSON: {exc}") from exc
    raise PipelineError("Grok returned no JSON object.")


def call_grok(cfg: Config, messages: list[dict], temperature: float = 0.8) -> str:
    """One chat completion. The xAI API is OpenAI-compatible."""
    last: Exception | None = None
    for attempt in range(1, cfg.retries + 1):
        try:
            resp = requests.post(
                f"{XAI_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg.xai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg.xai_model,
                    "messages": messages,
                    "temperature": temperature,
                },
                timeout=cfg.http_timeout,
            )
            if resp.status_code == 401:
                raise ConfigError("xAI rejected the key (401). Check XAI_API_KEY.")
            if resp.status_code == 404:
                raise ConfigError(
                    f"xAI does not know model {cfg.xai_model!r} (404). "
                    "Run `python main.py models` to list valid ids, then set XAI_MODEL."
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                raise PipelineError(f"xAI {resp.status_code}: {resp.text[:160]}")
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except ConfigError:
            raise  # operator must fix; retrying is pointless
        except Exception as exc:  # noqa: BLE001
            last = exc
            wait = 2**attempt
            log.warning(
                "Grok call failed (try %d/%d: %s); retrying in %ds.",
                attempt, cfg.retries, str(exc)[:120], wait,
            )
            time.sleep(wait)
    raise PipelineError(f"Grok call failed after {cfg.retries} tries: {last}")


def write_script(cfg: Config, topic: str) -> Script:
    words = int(cfg.target_seconds * 2.6)  # ~2.6 spoken words/sec
    hook_style = random.choice(HOOK_STYLES)
    log.info("Hook style: %s", hook_style)

    prompt = SCRIPT_PROMPT.format(
        rules=CONTENT_RULES,
        topic=topic,
        seconds=cfg.target_seconds,
        words=words,
        scenes=cfg.num_scenes,
        hook_style=hook_style,
    )
    raw = call_grok(
        cfg,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        # A fresh hook every run matters more here than a safe one.
        temperature=1.0,
    )
    data = extract_json(raw)

    narration = str(data.get("narration", "")).strip()
    if not narration:
        raise PipelineError("Grok returned an empty narration.")

    prompts = [str(p).strip() for p in data.get("image_prompts", []) if str(p).strip()]
    if not prompts:
        raise PipelineError("Grok returned no image prompts.")
    # Tolerate the model over/under-shooting the scene count.
    while len(prompts) < cfg.num_scenes:
        prompts.append(prompts[-1])
    prompts = prompts[: cfg.num_scenes]

    # Fold in the channel's own keywords, so SEO does not depend on the model
    # being generous on any given run. Model tags lead; base tags fill.
    tags = [str(t).strip().lower() for t in data.get("tags", []) if str(t).strip()]
    for base in SEO_BASE_TAGS:
        if base not in tags:
            tags.append(base)

    description = str(data.get("description", "")).strip()
    if SEO_HASHTAGS.split()[0] not in description:
        description = f"{description}\n\n{SEO_HASHTAGS}"

    mood = str(data.get("mood", "")).strip().lower()
    if mood not in MOODS:
        mood = "sad"  # the bank leans emotional; this is the safe default

    script = Script(
        title=str(data.get("title", topic)).strip()[:100],
        description=description,
        tags=tags[:15],  # YouTube ignores the tail; 15 is plenty
        narration=narration,
        image_prompts=prompts,
        mood=mood,
    )
    log.info(
        "Script ready: %r | %d chars narration | %d scenes",
        script.title, len(script.narration), len(script.image_prompts),
    )
    return script


# --------------------------------------------------------------------------- #
#  Step 3 — voiceover
# --------------------------------------------------------------------------- #
# Grok's TTS is warmer than edge-tts on Hindi and costs $15 per million
# characters — about a rupee a month at one video a day. Voices: ara, eve, leo,
# rex, sal.
GROK_VOICES = ("ara", "eve", "leo", "rex", "sal")


def edge_voice(cfg: Config, narration: str, out_path: Path) -> None:
    import asyncio

    import edge_tts

    async def _run() -> None:
        await edge_tts.Communicate(
            narration, cfg.tts_voice, rate=cfg.tts_rate
        ).save(str(out_path))

    asyncio.run(_run())


def grok_voice_tts(cfg: Config, narration: str, out_path: Path) -> None:
    """xAI TTS. Returns raw audio bytes, so write them straight to disk."""
    voice = cfg.grok_voice if cfg.grok_voice in GROK_VOICES else "rex"
    resp = requests.post(
        f"{XAI_BASE_URL}/tts",
        headers={
            "Authorization": f"Bearer {cfg.xai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "text": narration,
            "language": "hi",
            "voice_id": voice,
            "speed": cfg.grok_speed,
        },
        timeout=cfg.http_timeout,
    )
    if resp.status_code in (402, 403) and "credit" in resp.text.lower():
        raise OutOfCredits("xAI has no credits left for the voiceover.")
    resp.raise_for_status()
    out_path.write_bytes(resp.content)


def synthesize_voice(cfg: Config, narration: str, out_path: Path) -> float:
    if cfg.tts_provider == "grok":
        log.info("TTS: Grok voice %r...", cfg.grok_voice)
        try:
            grok_voice_tts(cfg, narration, out_path)
        except Exception as exc:  # noqa: BLE001
            # A voice is not worth losing the video over: edge-tts is free and
            # always available, so fall back rather than fail the run.
            log.warning("Grok TTS failed (%s); falling back to edge-tts.",
                        str(exc)[:140])
            edge_voice(cfg, narration, out_path)
    else:
        log.info("TTS: edge %s (rate %s)...", cfg.tts_voice, cfg.tts_rate)
        edge_voice(cfg, narration, out_path)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise PipelineError("TTS produced an empty audio file.")

    duration = audio_duration(out_path)
    log.info("TTS: voiceover ready (%.1fs)", duration)
    return duration


# --------------------------------------------------------------------------- #
#  Step 4 — visuals
# --------------------------------------------------------------------------- #
def download(url: str, out_path: Path, timeout: int) -> None:
    """Stream a remote file to disk."""
    with requests.get(url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        with open(out_path, "wb") as handle:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                handle.write(chunk)


def xai_post(cfg: Config, path: str, payload: dict) -> dict:
    """POST to the xAI API, turning the errors that matter into clear ones."""
    resp = requests.post(
        f"{XAI_BASE_URL}{path}",
        headers={
            "Authorization": f"Bearer {cfg.xai_api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=cfg.http_timeout,
    )
    if resp.status_code in (402, 403) and "credit" in resp.text.lower():
        raise OutOfCredits(
            "xAI reports no credits. Top up the SAME team this API key belongs "
            "to — console.x.ai -> that team -> Billing -> Add credits. Credits "
            "are per team, not per account."
        )
    if resp.status_code == 401:
        raise ConfigError("xAI rejected the key (401). Check XAI_API_KEY.")
    if resp.status_code >= 400:
        raise PipelineError(f"xAI {path} [{resp.status_code}]: {resp.text[:200]}")
    return resp.json()


def grok_image(cfg: Config, prompt: str, out_path: Path) -> bool:
    """One Grok-generated vertical still (~$0.02)."""
    try:
        data = xai_post(
            cfg,
            "/images/generations",
            {"model": cfg.image_model, "prompt": prompt, "aspect_ratio": "9:16"},
        )
        download(data["data"][0]["url"], out_path, cfg.http_timeout)
        return out_path.exists() and out_path.stat().st_size > 5000
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Grok image failed (%s).", str(exc)[:140])
        return False


def grok_video(cfg: Config, prompt: str, seconds: int, out_path: Path) -> bool:
    """
    One Grok-generated vertical clip with real motion (~$0.05 per second).

    Submit, then poll until done. The model returns no audio — the edge-tts
    voiceover is laid over the stitched clips at mux time.
    """
    try:
        submitted = xai_post(
            cfg,
            "/videos/generations",
            {
                "model": cfg.video_model,
                "prompt": prompt,
                "duration": seconds,
                "aspect_ratio": "9:16",
                "resolution": cfg.video_resolution,
            },
        )
        request_id = submitted.get("request_id") or submitted.get("id")
        if not request_id:
            raise PipelineError(f"No request_id: {str(submitted)[:160]}")

        deadline = time.time() + cfg.poll_timeout_s
        while time.time() < deadline:
            time.sleep(cfg.poll_interval_s)
            poll = requests.get(
                f"{XAI_BASE_URL}/videos/{request_id}",
                headers={"Authorization": f"Bearer {cfg.xai_api_key}"},
                timeout=cfg.http_timeout,
            )
            # Credits can run dry mid-generation, and then every poll 403s.
            # Retrying that for the full timeout burns 15 minutes to learn
            # something the first reply already said.
            if poll.status_code in (402, 403) and "credit" in poll.text.lower():
                raise OutOfCredits(
                    "xAI ran out of credits partway through this video."
                )
            if poll.status_code >= 400:
                log.warning("Poll [%s]: %s", poll.status_code, poll.text[:120])
                continue

            data = poll.json()
            status = str(data.get("status", "")).lower()
            if status == "done":
                download(data["video"]["url"], out_path, cfg.http_timeout)
                return out_path.exists() and out_path.stat().st_size > 5000
            if status in ("failed", "expired"):
                raise PipelineError(f"Video {status}: {str(data)[:160]}")

        raise PipelineError(f"Video timed out after {cfg.poll_timeout_s}s.")
    except ConfigError:
        raise  # no credits — retrying only wastes time
    except Exception as exc:  # noqa: BLE001
        log.warning("Grok video failed (%s).", str(exc)[:140])
        return False


def fetch_image(prompt: str, out_path: Path, timeout: int) -> bool:
    """One free Pollinations (Flux) still. Returns False if it doesn't arrive."""
    url = (
        POLLINATIONS
        + quote(prompt)
        + f"?width={VIDEO_W}&height={VIDEO_H}&nologo=true&model=flux"
        f"&seed={random.randint(1, 9_000_000)}"
    )
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200 and len(resp.content) > 5000:
            out_path.write_bytes(resp.content)
            return True
        log.warning("Image HTTP %s / %d bytes.", resp.status_code, len(resp.content))
    except Exception as exc:  # noqa: BLE001
        log.warning("Image fetch failed (%s).", str(exc)[:120])
    return False


def render_visuals(
    cfg: Config, prompts: list[str], run_id: str, duration: float
) -> tuple[str, list[Path]]:
    """
    One asset per scene, in whichever mode is configured. Returns ("clip"|
    "image", paths).

    Motion costs about 60x what stills cost, so the mode is the single dial
    that decides what a run is worth; each mode logs its own estimate before
    spending anything.

    Resilient per scene: a scene that will not render is dropped, and only an
    empty result is fatal.
    """
    scenes = len(prompts)

    if cfg.visual_mode == "video":
        # Round up so the stitched video is never shorter than the voiceover —
        # the final mux trims the tail with -shortest.
        clip_s = max(CLIP_MIN_S, min(CLIP_MAX_S, math.ceil(duration / scenes)))
        log.info(
            "Visuals: Grok VIDEO — %d clips x %ds, about $%.2f this run.",
            scenes, clip_s, scenes * clip_s * VIDEO_COST_PER_SECOND,
        )
        paths = []
        for index, prompt in enumerate(prompts, 1):
            target = WORK_DIR / f"clip_{run_id}_{index}.mp4"
            log.info("Clip %d/%d (%ds)...", index, scenes, clip_s)
            try:
                if grok_video(cfg, prompt, clip_s, target):
                    paths.append(target)
                else:
                    log.warning("Scene %d has no clip; skipping it.", index)
            except OutOfCredits:
                # The clips already rendered are already paid for. Throwing the
                # run away here would bin that money and post nothing; a shorter
                # video is strictly better than no video.
                if not paths:
                    raise
                log.warning(
                    "Out of credits after %d/%d clips — building the video from "
                    "the clips already paid for. Top up to get full-length "
                    "videos again.",
                    len(paths), scenes,
                )
                break
        if not paths:
            raise PipelineError("Every scene clip failed to render.")
        log.info("Clips: %d/%d rendered.", len(paths), scenes)
        return "clip", paths

    if cfg.visual_mode == "grok_image":
        log.info(
            "Visuals: Grok IMAGES — %d stills, about $%.2f this run.",
            scenes, scenes * GROK_IMAGE_COST,
        )
    else:
        log.info("Visuals: FREE images (Pollinations) — %d stills.", scenes)

    paths = []
    for index, prompt in enumerate(prompts, 1):
        target = WORK_DIR / f"img_{run_id}_{index}.jpg"
        log.info("Image %d/%d...", index, scenes)
        for attempt in range(1, 3):
            ok = (
                grok_image(cfg, prompt, target)
                if cfg.visual_mode == "grok_image"
                else fetch_image(prompt, target, cfg.http_timeout)
            )
            if ok:
                paths.append(target)
                break
            time.sleep(3 * attempt)  # the free tier rate-limits; back off
        else:
            log.warning("Scene %d has no image; skipping it.", index)

    if not paths:
        raise PipelineError(
            "Every scene image failed. Set BG_VIDEO to a looping mp4 to build "
            "the Short without generated stills."
        )
    log.info("Images: %d/%d rendered.", len(paths), scenes)
    return "image", paths


# --------------------------------------------------------------------------- #
#  Step 5 — assembly (streaming; never holds frames in RAM)
# --------------------------------------------------------------------------- #
# One thread: libx264 allocates per-thread frame buffers, and on a small runner
# that is what tips an encode into OOM. Encoding 1080x1920 single-threaded is
# still far faster than realtime here.
ENCODE = [
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "23",
    "-pix_fmt", "yuv420p",
    "-threads", "1",
    "-r", str(FPS),
]


def ken_burns_segment(image: Path, seconds: float, out_path: Path) -> None:
    """Encode one still into a slowly-zooming clip."""
    frames = max(1, int(round(seconds * FPS)))
    zoom_step = 0.15 / frames  # ends at ~1.15x, regardless of segment length
    chain = (
        f"scale={VIDEO_W * 5 // 4}:{VIDEO_H * 5 // 4}"
        ":force_original_aspect_ratio=increase,"
        f"crop={VIDEO_W * 5 // 4}:{VIDEO_H * 5 // 4},"
        f"zoompan=z='min(zoom+{zoom_step:.6f},1.15)':d={frames}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={VIDEO_W}x{VIDEO_H}:fps={FPS},"
        "format=yuv420p"
    )
    run_ffmpeg(
        ["-loop", "1", "-i", str(image), "-t", f"{seconds:.3f}",
         "-vf", chain, "-an", *ENCODE, str(out_path)],
        f"ken-burns {image.name}",
    )


def background_segment(bg_video: Path, seconds: float, out_path: Path) -> None:
    """Loop a stock background to length, cropped to vertical."""
    chain = (
        f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_W}:{VIDEO_H},format=yuv420p"
    )
    run_ffmpeg(
        ["-stream_loop", "-1", "-i", str(bg_video), "-t", f"{seconds:.3f}",
         "-vf", chain, "-an", *ENCODE, str(out_path)],
        "background loop",
    )


def pick_music(mood: str) -> Path | None:
    """A track matching the story's mood, or None to run voiceover-only."""
    for folder in (MUSIC_DIR / mood, MUSIC_DIR):
        if not folder.is_dir():
            continue
        tracks = sorted(
            p for p in folder.glob("*")
            if p.is_file() and p.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg")
        )
        if tracks:
            track = random.choice(tracks)
            where = "mood" if folder.name == mood else "shared"
            log.info("Music (%s/%s): %s", where, mood, track.name)
            return track

    log.info("No music in %s; voiceover only.", MUSIC_DIR)
    return None


def clip_segment(clip: Path, out_path: Path) -> None:
    """
    Re-encode a Grok clip to the pipeline's exact format.

    Grok returns 9:16 already, but at the requested resolution (720p by
    default) — scaling every clip to identical dimensions and frame rate is
    what lets the concat demuxer stream-copy them together afterwards.
    """
    chain = (
        f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_W}:{VIDEO_H},format=yuv420p"
    )
    run_ffmpeg(
        ["-i", str(clip), "-vf", chain, "-an", *ENCODE, str(out_path)],
        f"normalise {clip.name}",
    )


def assemble(
    cfg: Config,
    kind: str,
    assets: list[Path],
    voice: Path,
    duration: float,
    out_path: Path,
    music: Path | None = None,
) -> Path:
    """
    Build the final Short from `assets` — Grok clips ("clip") or stills
    ("image") — with `music` bedded under the voiceover if given.

    Encode each segment separately, then join with the concat demuxer and
    stream-copy the video. Only ever one segment is in flight, so peak memory
    stays flat no matter how many scenes there are.
    """
    segments: list[Path] = []
    bg = Path(cfg.bg_video) if cfg.bg_video else None

    if bg and bg.exists():
        log.info("Assembling from background loop (%s)...", bg.name)
        segment = WORK_DIR / "seg_bg.mp4"
        background_segment(bg, duration, segment)
        segments.append(segment)
    elif kind == "clip":
        log.info(
            "Assembling %d Grok clips at %dx%d...", len(assets), VIDEO_W, VIDEO_H
        )
        for index, clip in enumerate(assets, 1):
            segment = WORK_DIR / f"seg_{index}.mp4"
            clip_segment(clip, segment)
            segments.append(segment)

        # A run that ran out of credits ships fewer clips than the voiceover
        # needs. Without this the -shortest mux would cut the story off
        # mid-sentence; hold the last frame instead so the story is heard out.
        shot = sum(audio_duration(s) for s in segments)
        gap = duration - shot
        if gap > 0.3:
            log.info("Video is %.1fs short of the voiceover; holding the last "
                     "frame to cover it.", gap)
            held = WORK_DIR / "seg_held.mp4"
            run_ffmpeg(
                ["-i", str(segments[-1]),
                 "-vf", f"tpad=stop_mode=clone:stop_duration={gap + 0.2:.2f}",
                 "-an", *ENCODE, str(held)],
                "hold last frame",
            )
            segments[-1] = held
    else:
        if bg:
            log.warning("BG_VIDEO %s not found; using generated stills.", bg)
        per_scene = duration / len(assets)
        log.info(
            "Assembling %d scenes x %.1fs at %dx%d...",
            len(assets), per_scene, VIDEO_W, VIDEO_H,
        )
        for index, image in enumerate(assets, 1):
            segment = WORK_DIR / f"seg_{index}.mp4"
            ken_burns_segment(image, per_scene, segment)
            segments.append(segment)

    listing = WORK_DIR / "concat.txt"
    listing.write_text(
        "".join(f"file '{s.as_posix()}'\n" for s in segments), encoding="utf-8"
    )

    mux = [
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-i", str(voice),
    ]

    if music is None:
        mux += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        # Loop the track to cover the video, duck it well under the voice, and
        # fade it out at the end. `duration=first` keeps the mix as long as the
        # voiceover, not as long as the (looping) music.
        fade_at = max(0.0, duration - MUSIC_FADE_S)
        mux += [
            "-stream_loop", "-1", "-i", str(music),
            "-filter_complex",
            (
                f"[2:a]volume={MUSIC_VOLUME},"
                f"afade=t=out:st={fade_at:.2f}:d={MUSIC_FADE_S}[bed];"
                "[1:a][bed]amix=inputs=2:duration=first:dropout_transition=0"
                "[aout]"
            ),
            "-map", "0:v:0", "-map", "[aout]",
        ]

    mux += [
        "-c:v", "copy",            # segments already encoded — no re-encode
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]
    run_ffmpeg(mux, "final mux")

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise PipelineError("Assembly produced an empty file.")

    size_mb = out_path.stat().st_size / 1_048_576
    log.info("Video ready -> %s (%.1f MB)", out_path.name, size_mb)
    return out_path


# --------------------------------------------------------------------------- #
#  Step 6 — YouTube upload
# --------------------------------------------------------------------------- #
def youtube_service():
    """An authorized YouTube client, refreshing token.json when it has expired."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), YOUTUBE_SCOPE)

    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                log.info("YouTube: refreshing expired token...")
                creds.refresh(Request())
                refreshed = True
            except Exception as exc:  # noqa: BLE001
                log.warning("YouTube: refresh failed (%s).", str(exc)[:120])
                creds = None

        if not refreshed:
            # Consent needs a browser, which a runner does not have. Say what to
            # do instead of dying on a missing client_secret.json three frames
            # later.
            if os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"):
                raise ConfigError(
                    "The YouTube token is expired or revoked and cannot be "
                    "refreshed on a headless runner. Fix it in three steps: "
                    "(1) in Google Cloud Console set the OAuth consent screen to "
                    "'In production' — while it is in 'Testing', Google expires "
                    "refresh tokens after 7 days and the bot breaks every week; "
                    "(2) run `python main.py reauth` on your laptop; "
                    "(3) paste the new token.json into the TOKEN_JSON secret."
                )
            if not CLIENT_SECRET.exists():
                raise ConfigError(
                    f"client_secret.json not found at {CLIENT_SECRET}. Download "
                    "the OAuth 'Desktop app' credentials from Google Cloud "
                    "Console and save them there."
                )
            log.info("YouTube: opening the browser for consent...")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET), YOUTUBE_SCOPE
            )
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)


def set_thumbnail(youtube, video_id: str, video: Path) -> None:
    """Use a frame of the video as the thumbnail. Never breaks the run."""
    from googleapiclient.http import MediaFileUpload

    thumb = WORK_DIR / f"thumb_{video_id}.jpg"
    try:
        run_ffmpeg(
            ["-ss", "1.5", "-i", str(video), "-frames:v", "1", "-q:v", "2",
             str(thumb)],
            "thumbnail frame",
        )
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumb), mimetype="image/jpeg"),
        ).execute()
        log.info("YouTube: thumbnail set.")
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Thumbnail skipped (%s). Custom thumbnails need a phone-verified "
            "channel: youtube.com/verify",
            str(exc)[:120],
        )


def upload_to_youtube(cfg: Config, video: Path, script: Script) -> str:
    """Upload the Short and return its watch URL."""
    from googleapiclient.http import MediaFileUpload

    youtube = youtube_service()
    body = {
        "snippet": {
            "title": script.title,
            "description": script.description,
            "tags": script.tags,
            "categoryId": YOUTUBE_CATEGORY_ID,
        },
        "status": {
            "privacyStatus": cfg.youtube_privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    log.info("YouTube: uploading (%s)...", cfg.youtube_privacy)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(
            str(video), chunksize=-1, resumable=True, mimetype="video/mp4"
        ),
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("YouTube: %d%% uploaded", int(status.progress() * 100))

    video_id = response["id"]
    url = f"https://www.youtube.com/watch?v={video_id}"
    log.info("YouTube: done -> %s", url)

    set_thumbnail(youtube, video_id, video)
    return url


def reauth() -> None:
    """Redo Google consent locally and rewrite token.json."""
    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"):
        raise ConfigError("reauth needs a browser; run it on your laptop.")

    WORK_DIR.mkdir(exist_ok=True)
    if TOKEN_PATH.exists():
        backup = TOKEN_PATH.with_suffix(".json.old")
        TOKEN_PATH.replace(backup)
        log.info("Stale token backed up -> %s", backup.name)

    youtube_service()  # opens the browser, writes a fresh token.json

    log.info("=" * 62)
    log.info("NEW TOKEN -> %s", TOKEN_PATH)
    log.info("Now paste the ENTIRE contents of token.json into the GitHub")
    log.info("secret TOKEN_JSON (Settings -> Secrets and variables -> Actions).")
    log.info("=" * 62)


# --------------------------------------------------------------------------- #
#  Telegram
# --------------------------------------------------------------------------- #
def send_telegram(message: str) -> None:
    """Best-effort status ping. Never raises — a dead bot must not fail a run."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.info("Telegram not configured; skipping notification.")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=30,
        )
        resp.raise_for_status()
        log.info("Telegram: notified.")
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram notification failed (%s).", str(exc)[:120])


# --------------------------------------------------------------------------- #
#  Orchestration
# --------------------------------------------------------------------------- #
def cleanup(run_id: str) -> None:
    """Drop intermediates so the runner's disk (and the repo) stay small."""
    for leftover in WORK_DIR.glob("*"):
        if leftover.is_file():
            try:
                leftover.unlink()
            except OSError:
                pass


def run() -> Path:
    cfg = Config.from_env()
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    OUT_DIR.mkdir(exist_ok=True)
    WORK_DIR.mkdir(exist_ok=True)

    log.info("=" * 62)
    log.info("RUN START (id=%s, model=%s)", run_id, cfg.xai_model)
    log.info("=" * 62)

    try:
        topic = pick_topic()
        script = write_script(cfg, topic)

        voice = WORK_DIR / f"voice_{run_id}.mp3"
        duration = synthesize_voice(cfg, script.narration, voice)

        kind, assets = "image", []
        if not (cfg.bg_video and Path(cfg.bg_video).exists()):
            kind, assets = render_visuals(
                cfg, script.image_prompts, run_id, duration
            )

        video = OUT_DIR / f"short_{run_id}.mp4"
        assemble(
            cfg, kind, assets, voice, duration, video,
            music=pick_music(script.mood),
        )

        watch_url = ""
        if cfg.upload_youtube:
            watch_url = upload_to_youtube(cfg, video, script)
        else:
            log.info(
                "Upload skipped (no token.json, or UPLOAD_YOUTUBE=false). "
                "The video is in out/."
            )

        # Sidecar: everything an uploader step needs, without re-calling Grok.
        meta = OUT_DIR / f"short_{run_id}.json"
        meta.write_text(
            json.dumps(
                {
                    "video": video.name,
                    "title": script.title,
                    "description": script.description,
                    "tags": script.tags,
                    "topic": topic,
                    "duration_seconds": round(duration, 1),
                    "youtube_url": watch_url,
                    "created_utc": datetime.utcnow().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        log.info("RUN SUCCESS (id=%s) -> %s", run_id, video.name)
        send_telegram(
            "✅ Grok Short ready\n"
            f"{script.title}\n"
            f"Topic: {topic}\n"
            f"Length: {duration:.0f}s\n"
            + (watch_url or "Not uploaded — download it from the Actions artifact.")
        )
        return video

    except Exception as exc:
        send_telegram(
            f"❌ Grok Short FAILED (id={run_id})\n{type(exc).__name__}: "
            f"{str(exc)[:400]}"
        )
        raise
    finally:
        cleanup(run_id)


def list_models() -> None:
    """Ask xAI which model ids this key may use (model names drift)."""
    cfg = Config.from_env()
    resp = requests.get(
        f"{XAI_BASE_URL}/models",
        headers={"Authorization": f"Bearer {cfg.xai_api_key}"},
        timeout=30,
    )
    resp.raise_for_status()
    for model in resp.json().get("data", []):
        marker = " <- current XAI_MODEL" if model.get("id") == cfg.xai_model else ""
        log.info("  %s%s", model.get("id"), marker)


def selftest() -> None:
    """Prove ffmpeg + the assembly path work, without spending a single API call."""
    WORK_DIR.mkdir(exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)
    log.info("ffmpeg: %s", ffmpeg_bin())

    still = WORK_DIR / "selftest.jpg"
    run_ffmpeg(
        ["-f", "lavfi", "-i", f"testsrc=size={VIDEO_W}x{VIDEO_H}:duration=1:rate=1",
         "-frames:v", "1", str(still)],
        "selftest still",
    )
    tone = WORK_DIR / "selftest.mp3"
    run_ffmpeg(
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=4", str(tone)],
        "selftest tone",
    )

    cfg = Config(xai_api_key="unused")
    video = OUT_DIR / "selftest.mp4"
    assemble(cfg, "image", [still, still], tone, audio_duration(tone), video)
    log.info("SELFTEST PASSED -> %s", video)


COMMANDS = {
    "run": run,
    "reauth": reauth,
    "models": list_models,
    "selftest": selftest,
}


def main() -> int:
    command = sys.argv[1].lower() if len(sys.argv) > 1 else "run"
    if command not in COMMANDS:
        log.error("Unknown command %r. Use one of: %s",
                  command, ", ".join(COMMANDS))
        return 2
    try:
        COMMANDS[command]()
        return 0
    except ConfigError as exc:
        log.error("CONFIG: %s", exc)
        return 2
    except PipelineError as exc:
        log.error("FAILED: %s", exc)
        return 1
    except KeyboardInterrupt:
        log.warning("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
