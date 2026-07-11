"""
Grok Shorts â€” a zero-cost, memory-lean YouTube Shorts pipeline.

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

    UPLOAD_YOUTUBE   (optional) "false" to build only. Default: upload if a
                                token.json is present.
    YOUTUBE_PRIVACY  (optional) public | unlisted | private. Default public.
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (optional)
                                Success and failure notifications.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
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
TRENDS_RSS = "https://trends.google.com/trending/rss?geo=IN"
POLLINATIONS = "https://image.pollinations.ai/prompt/"

VIDEO_W, VIDEO_H, FPS = 1080, 1920, 30

# YouTube OAuth. token.json is written by `reauth` locally; on the runner the
# workflow materialises it from the TOKEN_JSON secret.
TOKEN_PATH = ROOT / "token.json"
CLIENT_SECRET = ROOT / "client_secret.json"
YOUTUBE_SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_CATEGORY_ID = "27"  # Education

# Trend titles that are queries, not story material.
TREND_SKIP = ("weather", "result", "score", "vs", "live", "rate", "login")


@dataclass(frozen=True)
class Config:
    xai_api_key: str
    xai_model: str = "grok-4.5"
    tts_voice: str = "hi-IN-SwaraNeural"
    tts_rate: str = "+12%"
    num_scenes: int = 5
    target_seconds: int = 30
    bg_video: str = ""
    upload_youtube: bool = False
    youtube_privacy: str = "public"
    http_timeout: int = 90
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

        return cls(
            xai_api_key=key,
            xai_model=os.environ.get("XAI_MODEL", "grok-4.5").strip(),
            tts_voice=os.environ.get("TTS_VOICE", "hi-IN-SwaraNeural").strip(),
            tts_rate=os.environ.get("TTS_RATE", "+12%").strip(),
            num_scenes=_env_int("NUM_SCENES", 5, lo=3, hi=8),
            target_seconds=_env_int("TARGET_SECONDS", 30, lo=15, hi=60),
            bg_video=os.environ.get("BG_VIDEO", "").strip(),
            upload_youtube=upload,
            youtube_privacy=privacy,
        )


class ConfigError(RuntimeError):
    """Something the operator must fix (missing key, bad value)."""


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
#  Step 1 â€” trending topic
# --------------------------------------------------------------------------- #
FALLBACK_TOPICS = [
    "Why the ocean looks blue but water is clear",
    "How honeybees decide where to build a hive",
    "The reason your phone battery drains faster in winter",
    "What actually happens when a plane hits turbulence",
    "Why some people never seem to feel cold",
    "How ancient builders moved stones without machines",
]


def fetch_topic() -> str:
    """A live Google Trends (IN) title, or an evergreen fallback."""
    try:
        resp = requests.get(TRENDS_RSS, timeout=20)
        resp.raise_for_status()
        titles = [
            (item.findtext("title") or "").strip()
            for item in ET.fromstring(resp.content).iter("item")
        ]
        usable = [
            t
            for t in titles
            if t and not any(bad in t.lower() for bad in TREND_SKIP)
        ]
        if usable:
            topic = random.choice(usable[:8])  # top-of-list, but varied
            log.info("Topic (google-trends): %s", topic)
            return topic
        log.warning("Trends returned nothing usable; using fallback bank.")
    except Exception as exc:  # noqa: BLE001
        log.warning("Trends fetch failed (%s); using fallback bank.", str(exc)[:120])

    topic = random.choice(FALLBACK_TOPICS)
    log.info("Topic (fallback): %s", topic)
    return topic


# --------------------------------------------------------------------------- #
#  Step 2 â€” Grok writes the script
# --------------------------------------------------------------------------- #
@dataclass
class Script:
    title: str
    description: str
    tags: list[str]
    narration: str
    image_prompts: list[str] = field(default_factory=list)


SYSTEM_PROMPT = (
    "You are a viral short-form video writer. You reply with JSON only â€” "
    "no prose, no markdown fences."
)

SCRIPT_PROMPT = """Write a {seconds}-second vertical YouTube Short about: {topic}

Structure the narration as HOOK -> BODY -> CTA:
- HOOK: first 3 seconds. A surprising claim or question that stops the scroll.
- BODY: the payoff. Concrete, specific, one clear idea.
- CTA: a short line inviting a follow or comment.

Rules:
- Narration must be ONE flowing spoken paragraph (no labels, no stage
  directions, no emoji) of roughly {words} words so it reads aloud in about
  {seconds} seconds.
- Write the narration in natural conversational Hindi using Devanagari script,
  keeping common English words as-is where a Hindi speaker would use them.
- Write exactly {scenes} image_prompts, one per scene, in ENGLISH. Each is a
  vivid, self-contained description of a cinematic 3D-animated frame with no
  text, letters, or logos in the image. Keep any recurring character described
  identically across scenes so the video looks consistent.
- title: under 70 characters, curiosity-driven, may use one emoji.
- description: 2 sentences, then a line of relevant hashtags.
- tags: 8 to 12 lowercase keyword strings.

Return exactly this JSON shape:
{{"title": "...", "description": "...", "tags": ["..."],
  "narration": "...", "image_prompts": ["...", "..."]}}"""


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
    prompt = SCRIPT_PROMPT.format(
        topic=topic,
        seconds=cfg.target_seconds,
        words=words,
        scenes=cfg.num_scenes,
    )
    raw = call_grok(
        cfg,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
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

    script = Script(
        title=str(data.get("title", topic)).strip()[:100],
        description=str(data.get("description", "")).strip(),
        tags=[str(t).strip() for t in data.get("tags", []) if str(t).strip()][:12],
        narration=narration,
        image_prompts=prompts,
    )
    log.info(
        "Script ready: %r | %d chars narration | %d scenes",
        script.title, len(script.narration), len(script.image_prompts),
    )
    return script


# --------------------------------------------------------------------------- #
#  Step 3 â€” voiceover (edge-tts, free)
# --------------------------------------------------------------------------- #
def synthesize_voice(cfg: Config, narration: str, out_path: Path) -> float:
    import asyncio

    import edge_tts

    log.info("TTS: %s (rate %s)...", cfg.tts_voice, cfg.tts_rate)

    async def _run() -> None:
        await edge_tts.Communicate(
            narration, cfg.tts_voice, rate=cfg.tts_rate
        ).save(str(out_path))

    asyncio.run(_run())
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise PipelineError("edge-tts produced an empty audio file.")

    duration = audio_duration(out_path)
    log.info("TTS: voiceover ready (%.1fs)", duration)
    return duration


# --------------------------------------------------------------------------- #
#  Step 4 â€” visuals (free)
# --------------------------------------------------------------------------- #
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


def render_images(cfg: Config, prompts: list[str], run_id: str) -> list[Path]:
    """One still per scene. Survives individual misses; fails only if all miss."""
    paths: list[Path] = []
    for index, prompt in enumerate(prompts, 1):
        target = WORK_DIR / f"img_{run_id}_{index}.jpg"
        log.info("Image %d/%d...", index, len(prompts))
        for attempt in range(1, 3):
            if fetch_image(prompt, target, cfg.http_timeout):
                paths.append(target)
                break
            time.sleep(3 * attempt)  # the free tier rate-limits; back off
        else:
            log.warning("Scene %d has no image; it will be skipped.", index)

    if not paths:
        raise PipelineError(
            "Every scene image failed. Pollinations may be down â€” set BG_VIDEO "
            "to a looping mp4 to build the Short without generated stills."
        )
    log.info("Images: %d/%d rendered.", len(paths), len(prompts))
    return paths


# --------------------------------------------------------------------------- #
#  Step 5 â€” assembly (streaming; never holds frames in RAM)
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


def assemble(
    cfg: Config, images: list[Path], voice: Path, duration: float, out_path: Path
) -> Path:
    """
    Build the final Short.

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
    else:
        if bg:
            log.warning("BG_VIDEO %s not found; using generated stills.", bg)
        per_scene = duration / len(images)
        log.info(
            "Assembling %d scenes x %.1fs at %dx%d...",
            len(images), per_scene, VIDEO_W, VIDEO_H,
        )
        for index, image in enumerate(images, 1):
            segment = WORK_DIR / f"seg_{index}.mp4"
            ken_burns_segment(image, per_scene, segment)
            segments.append(segment)

    listing = WORK_DIR / "concat.txt"
    listing.write_text(
        "".join(f"file '{s.as_posix()}'\n" for s in segments), encoding="utf-8"
    )

    run_ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", str(listing),
         "-i", str(voice),
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "copy",            # segments already encoded â€” no re-encode
         "-c:a", "aac", "-b:a", "128k",
         "-shortest", "-movflags", "+faststart",
         str(out_path)],
        "final mux",
    )

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise PipelineError("Assembly produced an empty file.")

    size_mb = out_path.stat().st_size / 1_048_576
    log.info("Video ready -> %s (%.1f MB)", out_path.name, size_mb)
    return out_path


# --------------------------------------------------------------------------- #
#  Step 6 â€” YouTube upload
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
                    "'In production' â€” while it is in 'Testing', Google expires "
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
    """Best-effort status ping. Never raises â€” a dead bot must not fail a run."""
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
        topic = fetch_topic()
        script = write_script(cfg, topic)

        voice = WORK_DIR / f"voice_{run_id}.mp3"
        duration = synthesize_voice(cfg, script.narration, voice)

        images: list[Path] = []
        if not (cfg.bg_video and Path(cfg.bg_video).exists()):
            images = render_images(cfg, script.image_prompts, run_id)

        video = OUT_DIR / f"short_{run_id}.mp4"
        assemble(cfg, images, voice, duration, video)

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
            "âœ… Grok Short ready\n"
            f"{script.title}\n"
            f"Topic: {topic}\n"
            f"Length: {duration:.0f}s\n"
            + (watch_url or "Not uploaded â€” download it from the Actions artifact.")
        )
        return video

    except Exception as exc:
        send_telegram(
            f"âŒ Grok Short FAILED (id={run_id})\n{type(exc).__name__}: "
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
    assemble(cfg, [still, still], tone, audio_duration(tone), video)
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
