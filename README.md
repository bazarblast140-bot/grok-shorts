# Grok Shorts

Daily Hinglish science Shorts for children aged 6–10. The GitHub Actions workflow runs at **08:00 IST** and uses DeepSeek for the script, edge-tts for narration, FFmpeg for a vertical captioned video, and the YouTube Data API to upload it.

The video is a typography-led 9:16 short with Hindi captions and voiceover. Uploads are marked **Made for Kids** and are **public** by default. GitHub Actions scheduling is best-effort, so a run may start late.

## GitHub Actions secrets

Set these in **Settings → Secrets and variables → Actions → Secrets**:

| Secret | Required | Purpose |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | Yes | Generates the daily script. |
| `TOKEN_JSON` | Yes | Refreshed YouTube OAuth token used for uploads. |
| `TELEGRAM_BOT_TOKEN` | No | Sends success/failure notifications. |
| `TELEGRAM_CHAT_ID` | No | Destination for Telegram notifications. |

## YouTube OAuth setup

1. In Google Cloud Console, create a project and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen as External and publish the app to Production. Apps left in Testing can have refresh tokens expire after seven days.
3. Create an OAuth client ID of type **Desktop app** and save the downloaded file as `client_secret.json` beside `main.py`.
4. Install the dependencies with `pip install -r requirements.txt`, then run `python main.py reauth` on your computer and sign in to the YouTube channel.
5. Copy the full contents of the generated `token.json` into the GitHub Actions secret named `TOKEN_JSON`.

The app requests only the YouTube upload scope. Keep `client_secret.json` and `token.json` private; both are gitignored.

## Run

- Automatic: one upload daily at 08:00 IST.
- Manual: open **Actions → Daily Grok Shorts Pipeline → Run workflow**.
- Optional: set `YOUTUBE_PRIVACY` to `unlisted` or `private` in the workflow to change the default visibility.
- Each workflow run also keeps the generated MP4 and upload metadata as an artifact for seven days.

Topics rotate by date from `story_topics.json`. The generated MP4 is kept under 60 seconds so it is eligible for Shorts.
