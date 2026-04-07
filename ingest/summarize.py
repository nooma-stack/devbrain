"""Session summarization using local Ollama model."""

from __future__ import annotations

import json
import urllib.request

from config import OLLAMA_URL, SUMMARIZE_MODEL


def summarize_text(text: str) -> str | None:
    """Summarize a session transcript using the local Ollama model."""
    # Limit input to ~12K chars for 7B model context
    truncated = text[:12000]

    prompt = f"""Summarize this AI coding session transcript concisely. Focus on:
- What was accomplished
- Key decisions made and why
- Files created or modified
- Issues encountered and how they were resolved
- Important patterns or lessons learned

Keep the summary under 300 words. Be specific about file names and technical details.

Transcript:
{truncated}"""

    data = json.dumps({
        "model": SUMMARIZE_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.3,
            "num_predict": 800,
        },
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())

    return result.get("response", "").strip() or None
