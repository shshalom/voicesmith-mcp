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
 *   - Supports MULTIPLE concurrent clients — audio is broadcast to all.
 *   - On connection: add client to the list, start recording if not already.
 *   - On client disconnect: remove from list, stop recording if no clients.
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
#include <fcntl.h>
#include <pthread.h>
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
#define MAX_CLIENTS      8

/* ── Global state ────────────────────────────────────────────────────────── */

static volatile sig_atomic_t g_running = 1;
static AudioQueueRef         g_queue   = NULL;

/* Client tracking */
static int             g_clients[MAX_CLIENTS];
static int             g_num_clients = 0;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;

static void handle_signal(int sig)
{
    (void)sig;
    g_running = 0;
    if (g_queue)
        AudioQueueStop(g_queue, /*inImmediate=*/true);
}

/* ── Client management ───────────────────────────────────────────────────── */

static void add_client(int fd)
{
    pthread_mutex_lock(&g_lock);
    if (g_num_clients < MAX_CLIENTS) {
        g_clients[g_num_clients++] = fd;
        fprintf(stderr, "audio-service: client added (fd=%d, total=%d)\n", fd, g_num_clients);
    } else {
        fprintf(stderr, "audio-service: max clients reached, rejecting fd=%d\n", fd);
        close(fd);
    }
    pthread_mutex_unlock(&g_lock);
}

static void remove_client(int fd)
{
    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < g_num_clients; i++) {
        if (g_clients[i] == fd) {
            g_clients[i] = g_clients[--g_num_clients];
            fprintf(stderr, "audio-service: client removed (fd=%d, total=%d)\n", fd, g_num_clients);
            break;
        }
    }
    pthread_mutex_unlock(&g_lock);
    close(fd);
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

    for (UInt32 i = 0; i < n && g_running; i++) {
        state->staging[state->pos++] = samples[i];
        if (state->pos == CHUNK_SAMPLES) {
            /* Broadcast chunk to all connected clients */
            pthread_mutex_lock(&g_lock);
            for (int c = 0; c < g_num_clients; /* no increment */) {
                const char *p   = (const char *)state->staging;
                ssize_t     rem = (ssize_t)CHUNK_BYTES;
                int         ok  = 1;
                while (rem > 0) {
                    ssize_t r = write(g_clients[c], p, (size_t)rem);
                    if (r < 0) {
                        if (errno == EINTR) continue;
                        /* Client disconnected — remove it */
                        int dead_fd = g_clients[c];
                        g_clients[c] = g_clients[--g_num_clients];
                        close(dead_fd);
                        fprintf(stderr, "audio-service: client dropped (fd=%d, total=%d)\n",
                                dead_fd, g_num_clients);
                        ok = 0;
                        break;
                    }
                    p   += r;
                    rem -= r;
                }
                if (ok) c++;  /* only advance if we didn't remove */
            }
            pthread_mutex_unlock(&g_lock);
            state->pos = 0;
        }
    }

    if (g_running)
        AudioQueueEnqueueBuffer(queue, buffer, 0, NULL);
}

/* ── start_queue: create and start an AudioQueue ─────────────────────────── */

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

/* ── Accept thread: accepts new clients in the background ────────────────── */

static int g_server_fd = -1;

static void *accept_thread(void *arg)
{
    (void)arg;
    while (g_running) {
        int client_fd = accept(g_server_fd, NULL, NULL);
        if (client_fd < 0) {
            if (errno == EINTR) continue;
            break;
        }
        add_client(client_fd);
    }
    return NULL;
}

/* ── main ────────────────────────────────────────────────────────────────── */

int main(void)
{
    signal(SIGTERM, handle_signal);
    signal(SIGINT,  handle_signal);
    signal(SIGPIPE, SIG_IGN);

    /* Create Unix socket */
    g_server_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (g_server_fd < 0) {
        fprintf(stderr, "audio-service: socket: %s\n", strerror(errno));
        return 1;
    }

    unlink(SOCKET_PATH);

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path) - 1);

    if (bind(g_server_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        fprintf(stderr, "audio-service: bind: %s\n", strerror(errno));
        return 1;
    }
    if (listen(g_server_fd, MAX_CLIENTS) < 0) {
        fprintf(stderr, "audio-service: listen: %s\n", strerror(errno));
        return 1;
    }

    /* Start AudioQueue — always recording, broadcasting to all clients */
    State state;
    OSStatus err = start_queue(&g_queue, &state);
    if (err != noErr) {
        fprintf(stderr, "audio-service: AudioQueue error (%d)\n", (int)err);
        return 1;
    }

    /* Start accept thread for new connections */
    pthread_t tid;
    pthread_create(&tid, NULL, accept_thread, NULL);

    /* Run CoreAudio run loop */
    while (g_running)
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.1, false);

    /* Cleanup */
    AudioQueueStop(g_queue, true);
    AudioQueueDispose(g_queue, true);

    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < g_num_clients; i++)
        close(g_clients[i]);
    g_num_clients = 0;
    pthread_mutex_unlock(&g_lock);

    close(g_server_fd);
    unlink(SOCKET_PATH);
    return 0;
}
