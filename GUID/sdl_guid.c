#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dirent.h>
#include <limits.h>
#include <errno.h>
#include <unistd.h>
#include <SDL2/SDL.h>

static int read_file(const char *path, char *buf, size_t sz) {
    FILE *fp = fopen(path, "r");
    if (!fp) return -1;
    size_t n = fread(buf, 1, sz - 1, fp);
    fclose(fp);
    if (n == 0) return -1;
    buf[n] = '\0';
    char *p = strchr(buf, '\n');
    if (p) *p = '\0';
    return 0;
}

static int realpath_of(const char *path, char *out, size_t outsz) {
    char *rp = realpath(path, NULL);
    if (!rp) return -1;
    strncpy(out, rp, outsz - 1);
    out[outsz - 1] = '\0';
    free(rp);
    return 0;
}

// SDL が返した /dev/input/eventX から、対応する /dev/input/jsN を探す
static int find_js_from_event(const char *event_dev_path, char *out_js, size_t out_sz) {
    if (!event_dev_path || !*event_dev_path) return -1;

    // /dev/input/event28 -> event28
    const char *base = strrchr(event_dev_path, '/');
    base = base ? base + 1 : event_dev_path;
    if (strncmp(base, "event", 5) != 0) return -1;

    char ev_sys[PATH_MAX];
    snprintf(ev_sys, sizeof(ev_sys), "/sys/class/input/%s/device", base);

    char ev_real[PATH_MAX];
    if (realpath_of(ev_sys, ev_real, sizeof(ev_real)) != 0) return -1;

    // /sys/class/input/js*/device を総当りで realpath 比較
    DIR *d = opendir("/sys/class/input");
    if (!d) return -1;

    struct dirent *de;
    int found = -1;
    while ((de = readdir(d))) {
        if (strncmp(de->d_name, "js", 2) != 0) continue; // js0, js1, ...
        char js_sys[PATH_MAX];
        snprintf(js_sys, sizeof(js_sys), "/sys/class/input/%s/device", de->d_name);

        char js_real[PATH_MAX];
        if (realpath_of(js_sys, js_real, sizeof(js_real)) != 0) continue;

        if (strcmp(ev_real, js_real) == 0) {
            snprintf(out_js, out_sz, "/dev/input/%s", de->d_name);
            found = 0;
            break;
        }
    }
    closedir(d);
    return found;
}

int main(void) {
    if (SDL_Init(SDL_INIT_JOYSTICK) != 0) {
        fprintf(stderr, "SDL_Init error: %s\n", SDL_GetError());
        return 1;
    }

    int n = SDL_NumJoysticks();
    printf("Joysticks: %d\n", n);

    for (int i = 0; i < n; i++) {
        const char *name = SDL_JoystickNameForIndex(i);

        SDL_JoystickGUID g = SDL_JoystickGetDeviceGUID(i);
        char gstr[64];
        SDL_JoystickGetGUIDString(g, gstr, sizeof(gstr));

        int vend = SDL_JoystickGetDeviceVendor(i);
        int prod = SDL_JoystickGetDeviceProduct(i);
        int vers = SDL_JoystickGetDeviceProductVersion(i);

        const char *sdl_path = NULL;
        #if SDL_VERSION_ATLEAST(2,26,0)
        sdl_path = SDL_JoystickPathForIndex(i);  // 多くの環境で /dev/input/eventX が返る
        #endif

        char js_path[PATH_MAX] = "";
        char show_path[PATH_MAX] = "";

        if (sdl_path && *sdl_path) {
            // eventX -> jsN へマップ
            if (find_js_from_event(sdl_path, js_path, sizeof(js_path)) == 0) {
                snprintf(show_path, sizeof(show_path), "%s (event: %s)", js_path, sdl_path);
            } else {
                // 見つからなければ event のまま表示
                snprintf(show_path, sizeof(show_path), "%s", sdl_path);
            }
        } else {
            // SDL がパスを返せない古い版でも、とりあえず js? 不明扱い
            snprintf(show_path, sizeof(show_path), "UNKNOWN(js?)");
        }

        printf("#%d path: %s name: \"%s\" guid: %s vendor: 0x%04x product: 0x%04x version: 0x%04x\n",
               i, show_path, name ? name : "(null)", gstr,
               vend & 0xffff, prod & 0xffff, vers & 0xffff);
    }

    SDL_Quit();
    return 0;
}
