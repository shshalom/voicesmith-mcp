/*
 * audio_service.c — Unix-socket mic streaming daemon for VoiceSmithMCP
 *
 * Runs as a LaunchAgent (launchd spawns it, ppid=1).  Because it is spawned
 * by launchd — not by a terminal app — macOS TCC attributes microphone
 * access to VoiceSmithMCP.app (com.voicesmith-mcp.launcher) rather than to
 * the user's terminal, which typically lacks NSMicrophoneUsageDescription.
 *
 * Protocol (Unix domain socket at SOCKET_PATH):
 *   - Service creates the socket file, then loops accepting connections.
 *   - On connection: start CoreAudio recording, stream raw little-endian
 *     float32 samples in exactly CHUNK_SAMPLES-sample (2048-byte) blocks
 *     until the client closes the connection or SIGTERM is received.
 *   - On disconnect: stop AudioQueue, loop back to accept().
 *   - On SIGTERM/SIGINT: stop cleanly and exit 0.
 *   - On CoreAudio error: write message to stderr and exit 1.
 *
 * Build (handled by install.sh):
 *   clang -framework AudioToolbox -framework CoreFoundation \
 *         audio_service.c -o audio-service
 */

#include <AudioToolbox/AudioToolbox.h>
#include <CoreFoundation/CoreFoundation.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define SAMPLE_RATE      16000
#define CHANNELS         1
#define BYTES_PER_SAMPLE 4          /* float32 */
#define CHUNK_SAMPLES    512        /* Silero VAD requires 512-sample chunks */
#define CHUNK_BYTES      (CHUNK_SAMPLES * BYTES_PER_SAMPLE)  /* 2048 */
#define NUM_BUFFERS      3
#define BUFFER_FRAMES    (CHUNK_SAMPLES * 4)
#define BUFFER_BYTES     (BUFFER_FRAMES * BYTES_PER_SAMPLE)

#define SOCKET_PATH      "/tmp/voicesmith-audio.sock"

/* ── Global state ────────────────────────────────────────────────────────── */

static volatile sig_atomic_t g_running   = 1;  /* set to 0 by signal */
static volatile sig_atomic_t g_recording = 0;  /* set to 0 when client gone */
static AudioQueueRef         g_queue     = NULL;
static int                   g_client_fd = -1;

static void handle_signal(int sig)
{
    (void)sig;
    g_running   = 0;
    g_recording = 0;
    if (g_queue)
        AudioQueueStop(g_queue, /*inImmediate=*/false);
}

/* ── Per-callback accumulation state ─────────────────────────────────────── */

typedef struct {
    float staging[CHUNK_SAMPLES];
    int   pos;
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

    State       *state   = (State *)user_data;
    const float *samples = (const float *)buffer->mAudioData;
    UInt32       n       = buffer->mAudioDataByteSize / BYTES_PER_SAMPLE;

    for (UInt32 i = 0; i < n && g_recording; i++) {
        state->staging[state->pos++] = samples[i];
        if (state->pos == CHUNK_SAMPLES) {
            /* Write one 512-sample chunk to the socket client */
            const char *p   = (const char *)state->staging;
            ssize_t     rem = (ssize_t)CHUNK_BYTES;
            while (rem > 0 && g_recording) {
                ssize_t r = write(g_client_fd, p, (size_t)rem);
                if (r < 0) {
                    if (errno == EINTR) continue;
                    /* Client disconnected — stop recording this session */
                    g_recording = 0;
                    break;
                }
                p   += r;
                rem -= r;
            }
            state->pos = 0;
        }
    }

    if (g_recording)
        AudioQueueEnqueueBuffer(queue, buffer, 0, NULL);
}

/* ── start_queue: create and start an AudioQueue, return noErr on success ── */

static OSStatus start_queue(AudioQueueRef *out_queue, State *state)
{
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

    memset(state, 0, sizeof(*state));

    AudioQueueRef queue;
    OSStatus err = AudioQueueNewInput(&fmt, audio_callback, state,
                                      NULL, NULL, 0, &queue);
    if (err != noErr) return err;

    for (int i = 0; i < NUM_BUFFERS; i++) {
        AudioQueueBufferRef buf;
        err = AudioQueueAllocateBuffer(queue, BUFFER_BYTES, &buf);
        if (err != noErr) { AudioQueueDispose(queue, true); return err; }
        AudioQueueEnqueueBuffer(queue, buf, 0, NULL);
    }

    err = AudioQueueStart(queue, NULL);
    if (err != noErr) { AudioQueueDispose(queue, true); return err; }

    *out_queue = queue;
    return noErr;
}

/* ── main ────────────────────────────────────────────────────────────────── */

int main(void)
{
    signal(SIGTERM, handle_signal);
    signal(SIGINT,  handle_signal);
    signal(SIGPIPE, SIG_IGN);

    /* Create Unix socket */
    int server_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (server_fd < 0) {
        fprintf(stderr, "audio-service: socket: %s\n", strerror(errno));
        return 1;
    }

    unlink(SOCKET_PATH);  /* remove stale socket if present */

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path) - 1);

    if (bind(server_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        fprintf(stderr, "audio-service: bind: %s\n", strerror(errno));
        return 1;
    }
    if (listen(server_fd, 1) < 0) {
        fprintf(stderr, "audio-service: listen: %s\n", strerror(errno));
        return 1;
    }

    /* ── Main accept loop: serve one client at a time ─────────────────── */
    while (g_running) {
        /* Accept a connection (blocks until client arrives or signal) */
        g_client_fd = accept(server_fd, NULL, NULL);
        if (g_client_fd < 0) {
            if (errno == EINTR) continue;  /* interrupted by signal */
            break;
        }

        /* Start recording for this client */
        State        state;
        AudioQueueRef queue = NULL;
        OSStatus err = start_queue(&queue, &state);
        if (err != noErr) {
            fprintf(stderr, "audio-service: AudioQueue error (%d)\n", (int)err);
            close(g_client_fd);
            g_client_fd = -1;
            continue;
        }
        g_queue     = queue;
        g_recording = 1;

        /* Run CoreAudio run loop until client disconnects or signal */
        while (g_running && g_recording)
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.1, false);

        /* Tear down AudioQueue for this session */
        g_recording = 0;
        AudioQueueStop(queue, /*inImmediate=*/true);
        AudioQueueDispose(queue, true);
        g_queue = NULL;

        close(g_client_fd);
        g_client_fd = -1;
    }

    close(server_fd);
    unlink(SOCKET_PATH);
    return 0;
}
