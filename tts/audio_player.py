"""Audio playback via external player (mpv, afplay, aplay)."""

import os
import platform
import subprocess
import tempfile
import time

import soundfile as sf

from shared import PlaybackResult, AudioPlayerError, get_logger

logger = get_logger("tts.audio_player")


class AudioPlayer:
    """Plays audio samples through an external player process."""

    def __init__(self, player_command: str = "mpv") -> None:
        self._player_command = player_command
        self._process: subprocess.Popen | None = None

        # Detect platform fallback if player_command is not available
        if not self._command_exists(player_command):
            system = platform.system()
            if system == "Darwin":
                self._player_command = "afplay"
                logger.info(f"'{player_command}' not found, falling back to afplay")
            elif system == "Linux":
                self._player_command = "aplay"
                logger.info(f"'{player_command}' not found, falling back to aplay")
            else:
                logger.warning(f"'{player_command}' not found and no fallback for {system}")

    @staticmethod
    def _command_exists(cmd: str) -> bool:
        """Check if a command is available on the system."""
        try:
            subprocess.run(
                ["which", cmd],
                capture_output=True,
                check=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _build_command(self, path: str) -> list[str]:
        """Build the player command list for the given audio file path."""
        if self._player_command == "mpv":
            return ["mpv", "--no-terminal", "--no-video", path]
        elif self._player_command == "afplay":
            return ["afplay", path]
        elif self._player_command == "aplay":
            return ["aplay", path]
        else:
            return [self._player_command, path]

    def play(self, samples, sample_rate: int) -> PlaybackResult:
        """Play audio samples through the configured player.

        Args:
            samples: Audio samples (numpy array).
            sample_rate: Sample rate in Hz.

        Returns:
            PlaybackResult with success status and timing.

        Raises:
            AudioPlayerError: If playback fails.
        """
        tmp_path = None
        try:
            # Write samples to a temporary WAV file
            fd, tmp_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            sf.write(tmp_path, samples, sample_rate)

            cmd = self._build_command(tmp_path)
            logger.debug(f"Playing audio: {' '.join(cmd)}")

            start = time.perf_counter()
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._process.wait()
            duration_ms = (time.perf_counter() - start) * 1000

            if self._process.returncode != 0:
                return PlaybackResult(
                    success=False,
                    duration_ms=duration_ms,
                    error=f"Player exited with code {self._process.returncode}",
                )

            return PlaybackResult(success=True, duration_ms=duration_ms)

        except Exception as e:
            raise AudioPlayerError(f"Playback failed: {e}") from e
        finally:
            self._process = None
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def stop(self) -> bool:
        """Stop any currently playing audio.

        Returns:
            True if a process was stopped, False if nothing was playing.
        """
        if self._process is not None:
            try:
                self._process.kill()
                self._process.wait(timeout=2)
                logger.info("Stopped audio playback")
                return True
            except Exception:
                return False
            finally:
                self._process = None
        return False

    @property
    def is_playing(self) -> bool:
        """Return True if audio is currently playing."""
        return self._process is not None and self._process.poll() is None
