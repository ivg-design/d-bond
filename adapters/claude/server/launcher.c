/* Native macOS MCPB entry point. The shared script selects the installed app's
 * bundled Python first, and keeps standalone plugin fallback support. */
#include <mach-o/dyld.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

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
    char launch[PATH_MAX];
    if (snprintf(launch, sizeof(launch), "%s/launch.sh", executable) >= (int)sizeof(launch)) {
        fputs("D-Bond server path is too long.\n", stderr);
        return 1;
    }
    char *argv[] = {"/bin/sh", launch, NULL};
    execv("/bin/sh", argv);
    perror("Could not start D-Bond server");
    return 1;
}
