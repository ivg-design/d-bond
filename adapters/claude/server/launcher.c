/* Native macOS entry point for the MCPB. It selects a working local Python. */
#include <mach-o/dyld.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static int supported(const char *python) {
    if (access(python, X_OK) != 0) return 0;
    pid_t child = fork();
    if (child < 0) return 0;
    if (child == 0) {
        FILE *null = fopen("/dev/null", "w");
        if (null) {
            dup2(fileno(null), STDOUT_FILENO);
            dup2(fileno(null), STDERR_FILENO);
            fclose(null);
        }
        char *argv[] = {(char *)python, "-c",
            "import sys; raise SystemExit(not ((3, 9) <= sys.version_info[:2] < (4, 0)))", NULL};
        execv(python, argv);
        _exit(127);
    }
    int status = 0;
    return waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

int main(void) {
    char executable[PATH_MAX];
    uint32_t length = sizeof(executable);
    if (_NSGetExecutablePath(executable, &length) != 0) {
        fputs("D-Bond launcher path is too long.\n", stderr);
        return 1;
    }
    char *slash = strrchr(executable, '/');
    if (!slash) {
        fputs("D-Bond launcher path is invalid.\n", stderr);
        return 1;
    }
    *slash = '\0';
    char server[PATH_MAX];
    if (snprintf(server, sizeof(server), "%s/mcp.py", executable) >= (int)sizeof(server)) {
        fputs("D-Bond server path is too long.\n", stderr);
        return 1;
    }
    const char *home = getenv("HOME");
    char bridge[PATH_MAX] = "";
    char local[PATH_MAX] = "";
    if (home && *home) {
        snprintf(bridge, sizeof(bridge), "%s/Library/Application Support/DotBridge/venv/bin/python", home);
        snprintf(local, sizeof(local), "%s/.local/bin/python3", home);
    }
    const char *candidates[] = {
        "/opt/homebrew/bin/python3", "/usr/local/bin/python3", bridge, local, "/usr/bin/python3", NULL
    };
    for (const char **candidate = candidates; *candidate; ++candidate) {
        if (**candidate && supported(*candidate)) {
            char *argv[] = {(char *)*candidate, server, NULL};
            execv(*candidate, argv);
        }
    }
    fputs("D-Bond requires an installed Python 3.9 or newer; no supported interpreter was found.\n", stderr);
    return 1;
}
