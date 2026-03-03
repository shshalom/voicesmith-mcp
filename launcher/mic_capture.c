/*
 * mic_capture.c — CoreAudio microphone recorder for VoiceSmithMCP
 *
 * Lives inside VoiceSmithMCP.app/Contents/MacOS/audio-capture so macOS TCC
 * attributes microphone permission to VoiceSmithMCP.app rather than to
 * Homebrew's Python.app (which lacks NSMicrophoneUsageDescription).
 *
 * Protocol:
 *   stdout  — raw little-endian float32 samples, exactly 512 samples
 *             (2048 bytes) per flush, 16 kHz mono, until terminated
 *   stderr  — error messages only
 *   stdin   — ignored; terminate the process with SIGTERM to stop recording
 *   exit 0  — clean shutdown (SIGTERM/SIGINT)
 *   exit 1  — CoreAudio error
 *
 * Build:
 *   clang -framework AudioToolbox -framework CoreFoundation \
 *         mic_capture.c -o audio-capture
 */

#include <AudioToolbox/AudioToolbox.h>
#include <CoreFoundation/CoreFoundation.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define SAMPLE_RATE      16000
#define CHANNELS         1
#define BYTES_PER_SAMPLE 4          /* float32 */
#define CHUNK_SAMPLES    512        /* Silero VAD requires 512-sample chunks */
#define CHUNK_BYTES      (CHUNK_SAMPLES * BYTES_PER_SAMPLE)  /* 2048 */
#define NUM_BUFFERS      3
#define BUFFER_FRAMES    (CHUNK_SAMPLES * 4)                 /* 4 chunks per buffer */
#define BUFFER_BYTES     (BUFFER_FRAMES * BYTES_PER_SAMPLE)  /* 8192 */

/* ── Global state for signal handler ─────────────────────────────────────── */

static volatile sig_atomic_t g_running = 1;
static AudioQueueRef         g_queue   = NULL;

static void handle_signal(int sig)
{
    (void)sig;
    g_running = 0;
    if (g_queue)
        AudioQueueStop(g_queue, /*inImmediate=*/false);
}

/* ── Per-callback accumulation state ─────────────────────────────────────── */

typedef struct {
    float staging[CHUNK_SAMPLES];  /* accumulates samples until a full chunk */
    int   pos;                     /* write position in staging[] */
} State;

/* ── AudioQueue input callback ───────────────────────────────────────────── */

static void audio_callback(
    void                               *user_data,
    AudioQueueRef                       queue,
    AudioQueueBufferRef                 buffer,
    const AudioTimeStamp               *start_time,
    UInt32                              num_packets,
    const AudioStreamPacketDescription *packet_desc)
{
    (void)start_time; (void)num_packets; (void)packet_desc;

    State        *state   = (State *)user_data;
    const float  *samples = (const float *)buffer->mAudioData;
    UInt32        n       = buffer->mAudioDataByteSize / BYTES_PER_SAMPLE;

    for (UInt32 i = 0; i < n && g_running; i++) {
        state->staging[state->pos++] = samples[i];
        if (state->pos == CHUNK_SAMPLES) {
            /* Flush exactly one 512-sample chunk to stdout */
            fwrite(state->staging, BYTES_PER_SAMPLE, CHUNK_SAMPLES, stdout);
            state->pos = 0;
        }
    }

    /* Re-enqueue the buffer so CoreAudio can refill it */
    if (g_running)
        AudioQueueEnqueueBuffer(queue, buffer, 0, NULL);
}

/* ── main ────────────────────────────────────────────────────────────────── */

int main(void)
{
    /* Unbuffered stdout — Python must see each 2048-byte chunk immediately */
    setvbuf(stdout, NULL, _IONBF, 0);

    /* Forward SIGTERM/SIGINT to a clean shutdown; ignore SIGPIPE */
    signal(SIGTERM, handle_signal);
    signal(SIGINT,  handle_signal);
    signal(SIGPIPE, SIG_IGN);

    /* ── Stream format: 16 kHz, mono, float32 PCM ─────────────────────── */
    AudioStreamBasicDescription fmt;
    memset(&fmt, 0, sizeof(fmt));
    fmt.mSampleRate       = SAMPLE_RATE;
    fmt.mFormatID         = kAudioFormatLinearPCM;
    fmt.mFormatFlags      = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked;
    fmt.mBitsPerChannel   = 32;
    fmt.mChannelsPerFrame = CHANNELS;
    fmt.mBytesPerFrame    = BYTES_PER_SAMPLE * CHANNELS;
    fmt.mFramesPerPacket  = 1;
    fmt.mBytesPerPacket   = fmt.mBytesPerFrame;

    /* ── Create input queue ────────────────────────────────────────────── */
    State state;
    memset(&state, 0, sizeof(state));

    AudioQueueRef queue;
    OSStatus err = AudioQueueNewInput(
        &fmt,
        audio_callback,
        &state,
        /*inCallbackRunLoop=*/NULL,     /* use AudioQueue's internal thread */
        /*inCallbackRunLoopMode=*/NULL,
        /*inFlags=*/0,
        &queue);

    if (err != noErr) {
        fprintf(stderr, "audio-capture: AudioQueueNewInput failed (%d)\n", (int)err);
        return 1;
    }
    g_queue = queue;

    /* ── Allocate and enqueue rotate buffers ──────────────────────────── */
    for (int i = 0; i < NUM_BUFFERS; i++) {
        AudioQueueBufferRef buf;
        err = AudioQueueAllocateBuffer(queue, BUFFER_BYTES, &buf);
        if (err != noErr) {
            fprintf(stderr, "audio-capture: AudioQueueAllocateBuffer failed (%d)\n", (int)err);
            AudioQueueDispose(queue, true);
            return 1;
        }
        AudioQueueEnqueueBuffer(queue, buf, 0, NULL);
    }

    /* ── Start recording ──────────────────────────────────────────────── */
    err = AudioQueueStart(queue, NULL);
    if (err != noErr) {
        fprintf(stderr, "audio-capture: AudioQueueStart failed (%d)\n", (int)err);
        AudioQueueDispose(queue, true);
        return 1;
    }

    /* ── Run until SIGTERM/SIGINT ─────────────────────────────────────── */
    while (g_running)
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.1, false);

    /* ── Clean teardown ───────────────────────────────────────────────── */
    AudioQueueStop(queue, /*inImmediate=*/true);
    AudioQueueDispose(queue, true);

    return 0;
}
