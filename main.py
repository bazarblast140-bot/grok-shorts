import asyncio
import datetime as dt
import json
import os
import re
import subprocess
import sys
import textwrap
import time

import edge_tts
import requests

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
VOICE = os.environ.get("TTS_VOICE", "hi-IN-SwaraNeural")
RATE = os.environ.get("TTS_RATE", "+12%")
RUN_DIR = "run"
VIDEO_PATH = os.path.join(RUN_DIR, "daily-short.mp4")


def run(command):
    subprocess.run(command, check=True)


def call_deepseek(prompt, system_prompt):
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GitHub Actions secret DEEPSEEK_API_KEY is missing.")
    response = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.6,
        },
        timeout=120,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"DeepSeek API returned {response.status_code}: {response.text[:500]}"
        )
    return response.json()["choices"][0]["message"]["content"].strip()


def select_topic():
    with open("story_topics.json", "r", encoding="utf-8") as f:
        topics = json.load(f)["topics"]
    if not topics:
        raise RuntimeError("story_topics.json has no topics.")
    # Stable daily rotation works on GitHub's fresh runner without committing state.
    day_number = (dt.date.today() - dt.date(2020, 1, 1)).days
    return topics[day_number % len(topics)]["topic"]


def split_sentences(script):
    cleaned = re.sub(r"^\s*[-*]\s*", "", script.strip(), flags=re.M)
    sentences = [part.strip() for part in re.split(r"(?<=[.!?।])\s+", cleaned) if part.strip()]
    return sentences or [cleaned]


async def make_voiceover(sentences):
    os.makedirs(RUN_DIR, exist_ok=True)
    durations, files = [], []
    for index, sentence in enumerate(sentences):
        path = os.path.join(RUN_DIR, f"voice-{index:02d}.mp3")
        await edge_tts.Communicate(sentence, VOICE, rate=RATE).save(path)
        files.append(path)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            check=True, capture_output=True, text=True,
        )
        durations.append(float(probe.stdout.strip()))

    concat_path = os.path.join(RUN_DIR, "audio-list.txt")
    with open(concat_path, "w", encoding="utf-8") as f:
        for path in files:
            f.write(f"file '{os.path.abspath(path)}'\n")
    audio_path = os.path.join(RUN_DIR, "voiceover.mp3")
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", concat_path,
        "-c:a", "libmp3lame", "-q:a", "3", audio_path,
    ])
    return audio_path, durations


def ass_time(seconds):
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{whole_seconds:02}.{fraction:02}"


def ass_escape(value):
    return value.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def write_captions(sentences, durations):
    path = os.path.join(RUN_DIR, "captions.ass")
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans Devanagari,50,&H00FFFFFF,&H000000FF,&H00101A35,&HAA101A35,0,0,0,0,100,100,0,0,3,2,0,5,55,55,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    current = 0.0
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        for sentence, duration in zip(sentences, durations):
            wrapped = textwrap.fill(sentence, width=25, break_long_words=False)
            caption = ass_escape(wrapped).replace("\n", r"\N")
            f.write(
                f"Dialogue: 0,{ass_time(current)},{ass_time(current + duration)},Default,,0,0,0,,"
                f"{caption}\n"
            )
            current += duration
    return current


def build_video(topic, audio_path, duration):
    captions_path = os.path.abspath(os.path.join(RUN_DIR, "captions.ass"))
    topic_path = os.path.abspath(os.path.join(RUN_DIR, "topic.txt"))
    filter_graph = (
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        "text='KIDS SCIENCE FACT':fontcolor=0xFFE082:fontsize=42:x=(w-text_w)/2:y=150,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
        f"textfile={topic_path}:fontcolor=white:fontsize=30:x=(w-text_w)/2:y=240,"
        f"subtitles={captions_path}:fontsdir=/usr/share/fonts/truetype/noto"
    )
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=#101a35:s=720x1280:r=30:d={duration:.3f}",
        "-i", audio_path, "-vf", filter_graph,
        "-c:v", "libx264", "-preset", "veryfast", "-threads", "1",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart", VIDEO_PATH,
    ])


def upload_to_youtube(topic, script):
    token_json = os.environ.get("TOKEN_JSON", "").strip()
    if not token_json:
        raise RuntimeError(
            "GitHub Actions secret TOKEN_JSON is missing. Add the refreshed "
            "YouTube token.json contents to enable daily uploads."
        )
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError("YouTube upload dependencies are not installed.") from exc
    try:
        token_info = json.loads(token_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError("TOKEN_JSON is not valid JSON.") from exc

    credentials = Credentials.from_authorized_user_info(token_info, SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials.valid:
        raise RuntimeError(
            "YouTube OAuth token is expired or invalid. Re-run the README reauth "
            "steps and replace TOKEN_JSON."
        )
    privacy = os.environ.get("YOUTUBE_PRIVACY", "public").strip().lower()
    if privacy not in {"public", "unlisted", "private"}:
        raise RuntimeError("YOUTUBE_PRIVACY must be public, unlisted, or private.")

    metadata = {
        "snippet": {
            "title": f"Amazing Fact for Kids: {topic}"[:100],
            "description": (
                f"{script}\n\nAaj ka science fact, simple Hinglish mein! "
                "Follow for a new fun learning short every day.\n\n"
                "#Shorts #KidsLearning #ScienceFacts #Hinglish"
            )[:5000],
            "tags": ["Shorts", "Kids Learning", "Science Facts", "Hinglish", "Education"],
            "categoryId": "27",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": True},
    }
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    request = youtube.videos().insert(
        part="snippet,status", body=metadata,
        media_body=MediaFileUpload(VIDEO_PATH, mimetype="video/mp4", resumable=True),
    )
    response = None
    while response is None:
        try:
            _, response = request.next_chunk()
        except HttpError as exc:
            if exc.resp.status not in (500, 502, 503, 504):
                raise RuntimeError(f"YouTube upload failed: {exc}") from exc
            time.sleep(2)
    video_id = response.get("id")
    if not video_id:
        raise RuntimeError("YouTube accepted the upload but returned no video ID.")
    return video_id


def send_telegram_notification(message):
    token = os.environ.get("TG_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT", "").strip()
    if not token or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": message}, timeout=10,
        )
    except requests.RequestException:
        pass


def main():
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        topic = select_topic()
        print(f"[TOPIC] {topic}")
        prompt = f"""Write one 35–45 second educational YouTube Shorts script for children aged 6–10 about: "{topic}".

Requirements:
- Simple Hinglish (Hindi in Devanagari mixed with easy English words)
- Begin with a surprising question
- Explain accurately in 4–6 short spoken sentences
- Include one memorable fact
- End with a cheerful line inviting viewers to learn again tomorrow
- 65–85 words total
- Return only the voiceover script, with no title, labels, bullets, or stage directions."""
        script = call_deepseek(
            prompt,
            "You write accurate, warm, age-appropriate educational voiceovers for children.",
        )
        sentences = split_sentences(script)
        word_count = len(script.split())
        if not 35 <= word_count <= 100:
            raise RuntimeError(
                f"Generated script has {word_count} words; expected 35–100. "
                "Run again to avoid uploading an unsuitable Short."
            )

        with open(os.path.join(RUN_DIR, "script.txt"), "w", encoding="utf-8") as f:
            f.write(script)
        with open(os.path.join(RUN_DIR, "topic.txt"), "w", encoding="utf-8") as f:
            f.write(topic)

        audio_path, sentence_durations = asyncio.run(make_voiceover(sentences))
        duration = write_captions(sentences, sentence_durations)
        if duration > 58:
            raise RuntimeError(f"Voiceover is {duration:.1f} seconds; keep Shorts under 60 seconds.")
        build_video(topic, audio_path, duration)

        video_id = upload_to_youtube(topic, script)
        result = {
            "topic": topic,
            "title": f"Amazing Fact for Kids: {topic}"[:100],
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "privacy": os.environ.get("YOUTUBE_PRIVACY", "public"),
            "made_for_kids": True,
        }
        with open(os.path.join(RUN_DIR, "video.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[SUCCESS] Uploaded: {result['url']}")
        send_telegram_notification(f"🎬 Daily Kids Short uploaded!\n{result['url']}")
    except Exception as exc:
        message = f"🎬 Daily Kids Short FAILED\n{exc}"
        print(message, file=sys.stderr)
        send_telegram_notification(message)
        sys.exit(1)


if __name__ == "__main__":
    main()
