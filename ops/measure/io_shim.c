/* LD_PRELOAD counter for the shadow-tick benchmark: counts fsync/fdatasync calls and bytes/calls written to regular
 * files by this process (SQLite WAL commits show up as pwrite + fdatasync/fsync). Read via ctypes: shim_counts(out[4]).
 * Build: gcc -O2 -shared -fPIC -o io_shim.so io_shim.c -ldl      (measurement tool only; never loaded by the app) */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <stdlib.h>
#include <unistd.h>

static volatile uint64_t n_fsync, n_fdatasync, n_write_calls, n_write_bytes;

/* AUU_SHIM_FSYNC_DELAY_US=N: sleep N microseconds inside every fsync/fdatasync to EMULATE a slower disk (cloud volume).
 * Emulation only; documented as such wherever its numbers are used. */
static long fsync_delay_us(void) {
    static long v = -1;
    if (v < 0) { const char *e = getenv("AUU_SHIM_FSYNC_DELAY_US"); v = e ? atol(e) : 0; }
    return v;
}
static int is_reg(int fd) {
    struct stat st;
    return fstat(fd, &st) == 0 && S_ISREG(st.st_mode);
}
typedef int (*fn_sync)(int);
typedef ssize_t (*fn_write)(int, const void *, size_t);
typedef ssize_t (*fn_pwrite)(int, const void *, size_t, off_t);
#define REAL(name, type) static type real = 0; if (!real) real = (type)dlsym(RTLD_NEXT, name)

int fsync(int fd) {
    REAL("fsync", fn_sync);
    __sync_fetch_and_add(&n_fsync, 1);
    if (fsync_delay_us() > 0) usleep((useconds_t)fsync_delay_us());
    return real(fd);
}
int fdatasync(int fd) {
    REAL("fdatasync", fn_sync);
    __sync_fetch_and_add(&n_fdatasync, 1);
    if (fsync_delay_us() > 0) usleep((useconds_t)fsync_delay_us());
    return real(fd);
}
ssize_t write(int fd, const void *b, size_t n) {
    REAL("write", fn_write);
    ssize_t r = real(fd, b, n);
    if (r > 0 && is_reg(fd)) { __sync_fetch_and_add(&n_write_calls, 1); __sync_fetch_and_add(&n_write_bytes, (uint64_t)r); }
    return r;
}
ssize_t pwrite(int fd, const void *b, size_t n, off_t o) {
    REAL("pwrite", fn_pwrite);
    ssize_t r = real(fd, b, n, o);
    if (r > 0 && is_reg(fd)) { __sync_fetch_and_add(&n_write_calls, 1); __sync_fetch_and_add(&n_write_bytes, (uint64_t)r); }
    return r;
}
ssize_t pwrite64(int fd, const void *b, size_t n, off_t o) {
    REAL("pwrite64", fn_pwrite);
    ssize_t r = real(fd, b, n, o);
    if (r > 0 && is_reg(fd)) { __sync_fetch_and_add(&n_write_calls, 1); __sync_fetch_and_add(&n_write_bytes, (uint64_t)r); }
    return r;
}
void shim_counts(uint64_t *out) {
    out[0] = n_fsync + n_fdatasync; out[1] = n_write_calls; out[2] = n_write_bytes; out[3] = n_fdatasync;
}
