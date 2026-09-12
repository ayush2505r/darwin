"""Media and text input handling module for Darwin.

Handles validation, text file reading, and audio/video transcription.
Per Section 5.2: If an external speech-to-text service is unavailable,
transcription is stubbed behind a clearly marked function so it never blocks
the rest of the pipeline.
"""

import os
import logging
from typing import Optional, Tuple
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# Permitted extensions and maximum allowed file size (16MB default)
ALLOWED_TEXT_EXTENSIONS = {".txt", ".md"}
ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg"}
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi", ".mkv"}
ALLOWED_EXTENSIONS = ALLOWED_TEXT_EXTENSIONS | ALLOWED_AUDIO_EXTENSIONS | ALLOWED_VIDEO_EXTENSIONS

MAX_FILE_SIZE_BYTES = 16 * 1024 * 1024  # 16 MB


def validate_file(filename: str, file_size: Optional[int] = None) -> Tuple[bool, Optional[str]]:
    """Validate uploaded file type and size.
    
    Returns (is_valid, error_message).
    """
    if not filename:
        return False, "No file uploaded."

    _, ext = os.path.splitext(filename.lower())
    if ext not in ALLOWED_EXTENSIONS:
        allowed_list = ", ".join(sorted(ALLOWED_EXTENSIONS))
        return False, f"Unsupported file type '{ext}'. Allowed types: {allowed_list}"

    if file_size is not None and file_size > MAX_FILE_SIZE_BYTES:
        max_mb = MAX_FILE_SIZE_BYTES // (1024 * 1024)
        return False, f"File size exceeds maximum permitted limit of {max_mb}MB."

    return True, None


def extract_or_transcribe(
    file_path: Optional[str],
    original_filename: Optional[str] = None,
    raw_text: Optional[str] = None
) -> str:
    """Extract text from text files/notes or transcribe audio/video.
    
    If both file and text are provided, combines them coherently.
    """
    extracted_parts = []

    # 1. Direct text notes
    if raw_text and raw_text.strip():
        extracted_parts.append(raw_text.strip())

    # 2. File handling
    if file_path and os.path.exists(file_path):
        filename = original_filename or os.path.basename(file_path)
        _, ext = os.path.splitext(filename.lower())

        if ext in ALLOWED_TEXT_EXTENSIONS:
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read().strip()
                    if content:
                        extracted_parts.append(f"[Uploaded Text Notes ({filename})]:\n{content}")
            except Exception as e:
                logger.error(f"Failed to read text file {file_path}: {e}")
                extracted_parts.append(f"[Failed to read uploaded text file: {e}]")

        elif ext in ALLOWED_AUDIO_EXTENSIONS or ext in ALLOWED_VIDEO_EXTENSIONS:
            transcription = transcribe_media(file_path, filename)
            extracted_parts.append(f"[Transcribed Media ({filename})]:\n{transcription}")

    return "\n\n".join(extracted_parts).strip()


def transcribe_media(file_path: str, filename: str) -> str:
    """Transcribe audio or video media.
    
    Attempts Groq Whisper API if GROQ_API_KEY is available; otherwise falls back
    to a clearly marked stub so the rest of the pipeline is never blocked.
    """
    groq_api_key = os.getenv("GROQ_API_KEY")

    if groq_api_key and groq_api_key.strip():
        try:
            from groq import Groq
            client = Groq(api_key=groq_api_key)
            with open(file_path, "rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    file=(filename, audio_file.read()),
                    model="whisper-large-v3",
                    response_format="text"
                )
                logger.info(f"Successfully transcribed {filename} with Groq Whisper.")
                return str(transcription).strip()
        except Exception as e:
            logger.warning(
                f"Groq Whisper transcription failed for {filename} ({e}). "
                f"Falling back to speech-to-text stub."
            )

    # Clean stub fallback as mandated by Section 5.2
    return stub_transcription(filename)


def stub_transcription(filename: str) -> str:
    """Clearly marked transcription stub for environments without STT service."""
    return (
        f"Student Discussion & Classroom Notes (simulated from media '{filename}'):\n"
        "Students discussed fundamental concepts with varying degrees of confidence. "
        "Several students asked clarifying questions regarding core principles, while others "
        "were able to solve introductory examples but struggled with real-world applications and edge cases."
    )
