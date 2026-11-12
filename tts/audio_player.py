"""Audio playback via external player (mpv, afplay, aplay)."""

import fcntl
import os
import platform
import subprocess
import tempfile
import time

import numpy as np
import soundfile as sf

from shared import PlaybackResult, AudioPlayerError, AUDIO_LOCK_PATH, get_logger

logger = get_logger("tts.audio_player")


def apply_gain(samples, gain: float = 1.0):
    """Scale samples by gain (0.0-1.0), clamping to full scale.

    Player-agnostic volume: works for mpv, afplay, and aplay alike, and
    applies to VoiceSmith's own output without touching the system
    device volume.
    """
    if gain is None or gain == 1.0:
        return samples
    arr = np.asarray(samples)
    if arr.dtype != np.float32:
        arr = arr.astype(np.float32)
    return np.clip(arr * np.float32(gain), -1.0, 1.0)


class AudioPlayer:
    """Plays audio samples through an external player process.

    With mpv, playback is LIVE-CONTROLLABLE: a mute_provider is polled
    ~10x/s (Mute All / Mute Subagents engaging mid-utterance kills the
    player within ~150 ms) and a volume_provider feeds mpv's volume over
    IPC, so volume-slider changes apply to the audio that is currently
    playing — not just the next utterance. With the afplay/aplay
    fallbacks, gain is baked into the samples per chunk and only the
    mute kill reacts live.
    """

    def __init__(
        self,
        player_command: str = "mpv",
        audio_output_device: str | None = None,
        mute_provider=None,
        volume_provider=None,
    ) -> None:
        self._player_command = player_command
        self._audio_output_device = audio_output_device
        self._mute_provider = mute_provider      # Callable[[], bool] — True = kill playback
        self._volume_provider = volume_provider  # Callable[[], float] — 0.0-1.0
        self._process: subprocess.Popen | None = None
        self._ipc_seq = 0

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

    _available_devices_cache: list[str] | None = None
    _available_devices_ts: float = 0.0
    _DEVICE_CACHE_TTL: float = 10.0  # seconds

    def _get_available_devices(self) -> list[str]:
        """Return list of available mpv audio device IDs, cached briefly."""
        now = time.time()
        if (
            self._available_devices_cache is not None
            and now - self._available_devices_ts < self._DEVICE_CACHE_TTL
        ):
            return self._available_devices_cache

        try:
            result = subprocess.run(
                ["mpv", "--audio-device=help"],
                capture_output=True, text=True, timeout=5,
            )
            devices = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("'") and "'" in line[1:]:
                    dev_id = line[1:line.index("'", 1)]
                    devices.append(dev_id)
            self._available_devices_cache = devices
            self._available_devices_ts = now
            return devices
        except Exception as e:
            logger.warning(f"Failed to query audio devices: {e}")
            return []

    def _build_command(self, path: str) -> list[str]:
        """Build the player command list for the given audio file path.

        Re-reads audio_output_device from config on each call so device
        changes take effect immediately without restarting the server.
        Falls back to system default if the configured device is unavailable.
        """
        if self._player_command == "mpv":
            cmd = ["mpv", "--no-terminal", "--no-video"]
            # Check live config for device (picks up menu bar changes)
            device = self._get_live_output_device()
            if device:
                available = self._get_available_devices()
                if available and device not in available:
                    logger.warning(
                        f"Audio device '{device}' not available, falling back to system default"
                    )
                    device = None
            if device:
                cmd.extend([f"--audio-device={device}"])
            cmd.append(path)
            return cmd
        elif self._player_command == "afplay":
            return ["afplay", path]
        elif self._player_command == "aplay":
            return ["aplay", path]
        else:
            return [self._player_command, path]

    _config_path_override: str | None = None  # For testing: set to skip live config reads

    def _get_live_output_device(self) -> str | None:
        """Read audio_output_device from config.json on each call.

        Allows the menu bar to change the output device without restarting
        the server. Falls back to the constructor value if config read fails.
        """
        if self._config_path_override == "":
            return self._audio_output_device  # Testing mode: skip config read

        try:
            import json
            from pathlib import Path
            config_path = Path(self._config_path_override) if self._config_path_override else (
                Path.home() / ".local" / "share" / "voicesmith-mcp" / "config.json"
            )
            if config_path.exists():
                with open(config_path) as f:
                    cfg = json.load(f)
                device = cfg.get("tts", {}).get("audio_output_device")
                if device is not None:
                    return device
        except Exception:
            pass
        return self._audio_output_device

    def play(self, samples, sample_rate: int, gain: float | None = None) -> PlaybackResult:
        """Play audio samples through the configured player.

        Args:
            samples: Audio samples (numpy array).
            sample_rate: Sample rate in Hz.
            gain: Playback volume 0.0-1.0. When None (default), the
                volume_provider is consulted — once for the initial level,
                then live (mpv only) while playback runs, so slider changes
                apply to the audio that is currently playing.

        Returns:
            PlaybackResult with success status and timing. stopped=True
            means playback was killed mid-flight (mute engaged) — callers
            should skip any remaining chunks.

        Raises:
            AudioPlayerError: If playback fails.
        """
        use_mpv = self._player_command == "mpv"
        initial_gain = self._resolve_gain(gain)

        if use_mpv:
            # mpv path: raw samples + mpv's own volume knob (live-adjustable
            # over IPC). Do NOT bake gain into samples here or it would
            # double-attenuate.
            play_samples = samples
        else:
            # afplay/aplay have no live volume — bake the initial gain in.
            play_samples = apply_gain(samples, initial_gain)

        tmp_path = None
        sock_path = None
        try:
            # Pre-spawn mute check: if a mute engaged while we were
            # synthesizing, never make a sound (the in-loop check only
            # helps once the player is already running).
            if self._mute_provider is not None:
                try:
                    if bool(self._mute_provider()):
                        return PlaybackResult(success=True, duration_ms=0.0, stopped=True)
                except Exception:
                    pass

            # Write samples to a temporary WAV file
            fd, tmp_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            sf.write(tmp_path, play_samples, sample_rate)

            cmd = self._build_command(tmp_path)
            ipc_sock = None
            if use_mpv:
                # Volume knob + IPC for live adjustments, inserted before
                # the file path (mpv wants options first).
                self._ipc_seq += 1
                sock_path = f"/tmp/voicesmith-mpv-{os.getpid()}-{self._ipc_seq}.sock"
                try:
                    os.unlink(sock_path)
                except OSError:
                    pass
                pct = max(0, min(100, round(initial_gain * 100)))
                cmd = cmd[:-1] + [
                    f"--volume={pct}",
                    f"--input-ipc-server={sock_path}",
                    cmd[-1],
                ]
                ipc_sock = sock_path
            logger.debug(f"Playing audio: {' '.join(cmd)}")

            # Cross-session audio lock: prevents overlapping playback
            # flock is kernel-managed — auto-released on crash, no stale locks
            with open(AUDIO_LOCK_PATH, "w") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)

                start = time.perf_counter()
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                conn = self._open_mpv_ipc(ipc_sock) if ipc_sock else None
                stopped = False
                returncode = 0
                try:
                    stopped = self._wait_with_controls(conn)
                finally:
                    returncode = self._process.returncode if self._process is not None else 0
                    duration_ms = (time.perf_counter() - start) * 1000
                    if conn is not None:
                        try:
                            conn.close()
                        except OSError:
                            pass
                    self._process = None

            # Lock released when lock_file closes

            if stopped:
                return PlaybackResult(success=True, duration_ms=duration_ms, stopped=True)

            if returncode != 0:
                return PlaybackResult(
                    success=False,
                    duration_ms=duration_ms,
                    error=f"Player exited with code {returncode}",
                )

            return PlaybackResult(success=True, duration_ms=duration_ms)

        except Exception as e:
            raise AudioPlayerError(f"Playback failed: {e}") from e
        finally:
            self._process = None
            if sock_path:
                try:
                    os.unlink(sock_path)
                except OSError:
                    pass
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # ─── Live control ─────────────────────────────────────────────────────────

    def _resolve_gain(self, gain: float | None) -> float:
        """Initial gain: explicit arg, else the volume provider, else 1.0."""
        if gain is not None:
            try:
                return min(1.0, max(0.0, float(gain)))
            except (TypeError, ValueError):
                return 1.0
        if self._volume_provider is not None:
            try:
                return min(1.0, max(0.0, float(self._volume_provider())))
            except Exception:
                return 1.0
        return 1.0

    def _wait_with_controls(self, ipc_conn) -> bool:
        """Block until the player exits, reacting to live state changes.

        Polls ~10x/s: if the mute provider turns on, kills the player
        immediately (returns True = stopped). If the volume provider moved,
        pushes the new level to mpv over IPC. Returns False for normal exit.
        """
        assert self._process is not None
        last_volume = None  # lazily seeded from the initial level
        while True:
            if self._process.poll() is not None:
                return False
            if self._mute_provider is not None:
                try:
                    muted = bool(self._mute_provider())
                except Exception:
                    muted = False
                if muted:
                    logger.info("Mute engaged mid-playback — stopping audio")
                    try:
                        self._process.kill()
                        self._process.wait(timeout=2)
                    except Exception:
                        pass
                    return True
            if ipc_conn is not None and self._volume_provider is not None:
                try:
                    v = min(1.0, max(0.0, float(self._volume_provider())))
                except Exception:
                    v = None
                if v is not None:
                    if last_volume is None or abs(v - last_volume) >= 0.01:
                        if self._mpv_set_volume(ipc_conn, v):
                            last_volume = v
            time.sleep(0.1)

    @staticmethod
    def _open_mpv_ipc(sock_path: str | None):
        """Connect to mpv's IPC socket (mpv creates it shortly after start)."""
        if not sock_path:
            return None
        import socket as socket_mod
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                conn = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
                conn.connect(sock_path)
                return conn
            except OSError:
                try:
                    conn.close()
                except Exception:
                    pass
                time.sleep(0.05)
        logger.debug("mpv IPC socket not available — live volume disabled for this chunk")
        return None

    @staticmethod
    def _mpv_set_volume(ipc_conn, volume: float) -> bool:
        """Push a volume change to mpv over IPC. Fire-and-forget."""
        try:
            pct = max(0, min(100, round(volume * 100)))
            ipc_conn.sendall(f"set volume {pct}\n".encode())
            return True
        except OSError:
            return False

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
