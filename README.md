# Vice City Intel Clipping Workflow

Windows workflow for finding and editing highlights from authorized Twitch VODs.
The PowerShell launchers call the Python application. Discovery downloads and
transcribes the VOD in chunks, extracts visual and audio signals, and uses
Gemini to rank candidates. FFmpeg renders vertical edits from explicit plans
with captions, reaction effects, overlays, and synthesized sound cues.

## Restore or set up

Clone this private repository, put FFmpeg and ffprobe on PATH, and open the
cloned folder in PowerShell. Install Python 3.12 and run:

    py -3.12 -m venv .\AI-Youtube-Shorts-Generator\.venv
    .\AI-Youtube-Shorts-Generator\.venv\Scripts\python.exe -m pip install -r .\AI-Youtube-Shorts-Generator\requirements-local.txt

Copy the application .env.example to .env locally and supply GEMINI_API_KEY
for discovery. GEMINI_MODEL, LOCAL_WHISPER_MODEL, LOCAL_WHISPER_DEVICE and
LOCAL_OUTPUT_DIR control model selection and local output. Place the authorized
campaign banner at assets/SkiiMaskTimmy Twitch Overlay.png or set the campaign
profile to your authorized asset. The official image is distributed separately.

## Run and review

    .\clip.ps1 discover 'https://www.twitch.tv/videos/VOD_ID' --campaign skiimasktimmy --top 15
    .\clip.ps1 review --run RUN_ID
    .\clip.ps1 polish --run RUN_ID --candidates 1
    .\clip.ps1 preflight --help
    .\clip.ps1 approve --help
    .\clip.ps1 clean --help

Review the original footage, contact sheet and dialogue before selecting a
candidate. Put editorial.json in the run folder with candidate IDs as keys.
A selected entry needs decision=select and source_window matching the
candidate's clip_start and clip_end. Add nonempty hook, context, escalation,
visible_payoff, reaction, ending, dialogue, and contact_sheet fields. The
timestamps use seconds in the VOD. Field values explain the actual beat and
identify the supporting contact sheet. See AGENTS.md and WORKFLOW.md.

The private review gallery and run data are under work/runs. Use the exact
export for preflight and approval, tied to its SHA-256 and intended platforms.
Use the cleanup preview before confirming file removal. Keep shared models,
campaign assets and source rights records outside generated run data.
Videos, local publishing history, credentials, caches, and model weights are
excluded from this backup. Restore those separately where needed.

## Checks

    .\AI-Youtube-Shorts-Generator\.venv\Scripts\python.exe .\AI-Youtube-Shorts-Generator\tests\test_clip_logic.py

Based on Anil Chandra Naidu Matcha's AI-Youtube-Shorts-Generator. Its MIT
license and attribution are preserved in LICENSE.
