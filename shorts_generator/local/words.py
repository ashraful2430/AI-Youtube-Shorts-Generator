"""Word-level transcription of a short source window (for captions and edit plans).

Cuts [start, end] out of the source as 16 kHz mono WAV with ffmpeg and runs
faster-whisper with word timestamps. Results are cached as JSON next to the
other outputs, keyed by source name + window + model, so re-edits are instant.
Times in the result are absolute source-video seconds.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from ..config import LOCAL_OUTPUT_DIR, LOCAL_WHISPER_DEVICE


def word_transcript(
    source_path: str,
    start: float,
    end: float,
    model_name: Optional[str] = None,
    language: str = "en",
) -> List[Dict]:
    """Return [{"start", "end", "word", "prob"}] for words spoken in the window."""
    model_name = model_name or os.getenv("CAPTION_WHISPER_MODEL", "small")
    cache_dir = Path(LOCAL_OUTPUT_DIR) / "word_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = f"{Path(source_path).stem}_{start:.2f}_{end:.2f}_{model_name}.json"
    cache = cache_dir / key
    if cache.exists() and cache.stat().st_mtime >= os.path.getmtime(source_path):
        return json.loads(cache.read_text(encoding="utf-8"))

    from faster_whisper import WhisperModel  # type: ignore

    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "window.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
             "-i", source_path, "-vn", "-ac", "1", "-ar", "16000", wav],
            check=True,
        )
        device = "cpu" if LOCAL_WHISPER_DEVICE in ("auto", "cpu") else LOCAL_WHISPER_DEVICE
        print(f"[words] faster-whisper {model_name} on {end - start:.0f}s window", flush=True)
        model = WhisperModel(model_name, device=device, compute_type="int8")
        segments, _ = model.transcribe(
            wav, language=language, beam_size=5, word_timestamps=True,
            condition_on_previous_text=False, vad_filter=False,
        )
        words = []
        for seg in segments:
            for w in seg.words or []:
                text = (w.word or "").strip()
                if text:
                    words.append({
                        "start": round(start + float(w.start), 3),
                        "end": round(start + float(w.end), 3),
                        "word": text,
                        "prob": round(float(w.probability), 3),
                    })
    cache.write_text(json.dumps(words, indent=1, ensure_ascii=False), encoding="utf-8")
    return words
