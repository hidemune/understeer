#include <stdio.h>
#include <SDL2/SDL.h>

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
        printf("#%d name=\"%s\" guid=%s\n", i, name ? name : "(null)", gstr);
    }
    SDL_Quit();
    return 0;
}
