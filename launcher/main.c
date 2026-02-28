/*
 * VoiceSmithMCP Launcher
 *
 * A minimal macOS app bundle wrapper that launches the Python MCP server.
 * Being inside an app bundle with NSMicrophoneUsageDescription ensures macOS
 * TCC correctly attributes microphone permission requests to this bundle
 * rather than to the parent terminal app (e.g. Commander, iTerm2).
 *
 * The Python server inherits stdin/stdout/stderr directly (no proxying),
 * so stdio MCP transport works transparently.
 *
 * Signal handling: SIGTERM/SIGINT/SIGHUP are forwarded to the child so that
 * when Claude Code terminates the MCP connection the Python server shuts down
 * gracefully and the launcher exits cleanly rather than leaving an orphan.
 *
 * Paths are baked in at compile time by install.sh. Environment variable
 * overrides are also supported for development.
 *
 * Build: see install.sh (runs automatically during `./install.sh`)
 */

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

/* Baked in by install.sh: -DVOICESMITH_PYTHON='"..."' -DVOICESMITH_SERVER='"..."' */
#ifndef VOICESMITH_PYTHON
#define VOICESMITH_PYTHON "/usr/bin/python3"
#endif

#ifndef VOICESMITH_SERVER
#define VOICESMITH_SERVER "server.py"
#endif

static pid_t child_pid = 0;

static void forward_signal(int sig) {
    if (child_pid > 0) {
        kill(child_pid, sig);
    }
}

extern char **environ;

int main(void) {
    const char *python = getenv("VOICESMITH_PYTHON") ? getenv("VOICESMITH_PYTHON") : VOICESMITH_PYTHON;
    const char *server = getenv("VOICESMITH_SERVER") ? getenv("VOICESMITH_SERVER") : VOICESMITH_SERVER;

    /* Forward termination signals to the child so it can shut down cleanly.
     * Without this, SIGTERM kills the launcher immediately and leaves the
     * Python server orphaned with ppid=1. */
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = forward_signal;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT,  &sa, NULL);
    sigaction(SIGHUP,  &sa, NULL);

    /* Ignore SIGPIPE — the child manages its own I/O */
    signal(SIGPIPE, SIG_IGN);

    child_pid = fork();
    if (child_pid < 0) {
        perror("voicesmith-launcher: fork");
        return 1;
    }

    if (child_pid == 0) {
        /* Child: exec into Python server. Inherits stdin/stdout/stderr. */
        char *const args[] = {(char *)python, (char *)server, NULL};
        execv(python, args);
        perror("voicesmith-launcher: execv");
        _exit(1);
    }

    /* Parent: wait for child, retrying if interrupted by a forwarded signal */
    int status;
    while (waitpid(child_pid, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("voicesmith-launcher: waitpid");
            return 1;
        }
    }

    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
