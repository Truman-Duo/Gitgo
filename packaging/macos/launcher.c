/* Finder entry only. The existing terminal executable owns the product UI. */
#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <spawn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

int main(void) {
    char executable[PATH_MAX], resolved[PATH_MAX], command[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0 ||
        realpath(executable, resolved) == NULL) return 1;
    char *slash = strrchr(resolved, '/');
    if (slash == NULL) return 1;
    *slash = '\0';
    int written = snprintf(command, sizeof(command), "%s/../Resources/launch.command", resolved);
    if (written < 0 || (size_t)written >= sizeof(command) || access(command, R_OK) != 0) return 1;
    char *arguments[] = {"/usr/bin/open", "-a", "Terminal", command, NULL};
    pid_t child;
    int error = posix_spawn(&child, arguments[0], NULL, NULL, arguments, environ);
    if (error != 0) return error;
    int status;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) return 1;
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
