"""Speech queue for serialized TTS playback."""

import asyncio
import time

from shared import SpeakResult, MAX_CHUNK_LENGTH, get_logger
from tts.kokoro_engine import KokoroEngine
from tts.audio_player import AudioPlayer

logger = get_logger("tts.speech_queue")


class SpeechQueue:
    """Manages sequential speech synthesis and playback."""

    def __init__(self, engine: KokoroEngine, player: AudioPlayer) -> None:
        self._engine = engine
        self._player = player
        self._queue: asyncio.Queue = asyncio.Queue()
        self._speaking = False

    async def speak(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        block: bool = True,
    ) -> SpeakResult:
        """Synthesize and play text.

        Args:
            text: The text to speak.
            voice_id: Kokoro voice ID.
            speed: Speech speed multiplier.
            block: If True, wait for playback to finish. If False, queue and return immediately.

        Returns:
            SpeakResult with timing and status info.
        """
        if not block:
            # Fire-and-forget: schedule as a background task
            asyncio.create_task(self._speak_blocking(text, voice_id, speed))
            return SpeakResult(
                success=True,
                voice=voice_id,
                queued=True,
            )

        return await self._speak_blocking(text, voice_id, speed)

    async def _speak_blocking(
        self,
        text: str,
        voice_id: str,
        speed: float,
    ) -> SpeakResult:
        """Internal: synthesize and play text, blocking until done."""
        loop = asyncio.get_event_loop()
        self._speaking = True
        total_duration_ms = 0.0
        total_synthesis_ms = 0.0

        try:
            chunks = self.chunk_text(text)

            for chunk in chunks:
                # Run sync synthesis in executor to avoid blocking the event loop
                synthesis_result = await loop.run_in_executor(
                    None, self._engine.synthesize, chunk, voice_id, speed
                )
                total_synthesis_ms += synthesis_result.synthesis_ms

                # Run sync playback in executor
                playback_result = await loop.run_in_executor(
                    None,
                    self._player.play,
                    synthesis_result.samples,
                    synthesis_result.sample_rate,
                )
                total_duration_ms += playback_result.duration_ms

                if not playback_result.success:
                    return SpeakResult(
                        success=False,
                        voice=voice_id,
                        duration_ms=total_duration_ms,
                        synthesis_ms=total_synthesis_ms,
                        error=playback_result.error,
                    )

            return SpeakResult(
                success=True,
                voice=voice_id,
                duration_ms=total_duration_ms,
                synthesis_ms=total_synthesis_ms,
            )

        except Exception as e:
            logger.error(f"Speech failed: {e}")
            return SpeakResult(
                success=False,
                voice=voice_id,
                duration_ms=total_duration_ms,
                synthesis_ms=total_synthesis_ms,
                error=str(e),
            )
        finally:
            self._speaking = False

    def stop(self) -> bool:
        """Stop current playback.

        Returns:
            True if something was stopped.
        """
        return self._player.stop()

    @property
    def depth(self) -> int:
        """Return the number of items in the queue."""
        return self._queue.qsize()

    @staticmethod
    def chunk_text(text: str, max_length: int = MAX_CHUNK_LENGTH) -> list[str]:
        """Split text into chunks by sentence boundaries.

        Splits on '. ', '! ', '? ' and their end-of-string variants.
        Keeps chunks under max_length. Single sentences exceeding
        max_length are included as-is (never broken mid-sentence).

        Args:
            text: The text to chunk.
            max_length: Maximum characters per chunk.

        Returns:
            List of text chunks.
        """
        if not text:
            return []

        if len(text) <= max_length:
            return [text]

        # Split into sentences
        sentences: list[str] = []
        current = ""
        i = 0
        while i < len(text):
            current += text[i]
            # Check for sentence-ending punctuation followed by space or end of string
            if text[i] in ".!?" and (i + 1 >= len(text) or text[i + 1] == " "):
                sentences.append(current.strip())
                current = ""
                # Skip the space after punctuation
                if i + 1 < len(text) and text[i + 1] == " ":
                    i += 1
            i += 1

        # Add any remaining text
        if current.strip():
            sentences.append(current.strip())

        # Group sentences into chunks under max_length
        chunks: list[str] = []
        current_chunk = ""

        for sentence in sentences:
            if not current_chunk:
                current_chunk = sentence
            elif len(current_chunk) + 1 + len(sentence) <= max_length:
                current_chunk += " " + sentence
            else:
                chunks.append(current_chunk)
                current_chunk = sentence

        if current_chunk:
            chunks.append(current_chunk)

        return chunks
