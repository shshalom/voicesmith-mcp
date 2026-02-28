/*
 * VoiceSmithMCP Launcher
 *
 * A minimal macOS app bundle wrapper that launches the Python MCP server.
 * Being inside an app bundle with NSMicrophoneUsageDescription ensures macOS
 * TCC correctly attributes microphone permission requests to this bundle
 * rather than to the parent terminal app (e.g. Commander, iTerm2).
 *
 * The Python server inherits stdin/stdout/stderr directly via posix_spawn with
 * NULL file_actions (no proxying), so stdio MCP transport works transparently.
 *
 * Paths are baked in at compile time by install.sh. Environment variable
 * overrides are also supported for development.
 *
 * Build: see install.sh (runs automatically during `./install.sh`)
 */

#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

/* Baked in by install.sh: -DVOICESMITH_PYTHON='"..."' -DVOICESMITH_SERVER='"..."' */
#ifndef VOICESMITH_PYTHON
#define VOICESMITH_PYTHON "/usr/bin/python3"
#endif

#ifndef VOICESMITH_SERVER
#define VOICESMITH_SERVER "server.py"
#endif

extern char **environ;

int main(void) {
    const char *python = getenv("VOICESMITH_PYTHON") ? getenv("VOICESMITH_PYTHON") : VOICESMITH_PYTHON;
    const char *server = getenv("VOICESMITH_SERVER") ? getenv("VOICESMITH_SERVER") : VOICESMITH_SERVER;

    char *const args[] = {(char *)python, (char *)server, NULL};

    pid_t pid;
    /* NULL file_actions → child inherits all open fds (stdin/stdout/stderr) */
    int ret = posix_spawn(&pid, python, NULL, NULL, args, environ);
    if (ret != 0) {
        perror("voicesmith-launcher: posix_spawn");
        return 1;
    }

    int status;
    if (waitpid(pid, &status, 0) < 0) {
        perror("voicesmith-launcher: waitpid");
        return 1;
    }

    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
