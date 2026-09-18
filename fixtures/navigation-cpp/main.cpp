#include "state.h"

int main() {
    int level = 12; // NAV_DEF:outer
    DisplayState state;
    state.setBrightness(level); // NAV_USE:outer-before
    {
        int level = 20; // NAV_DEF:inner
        state.setBrightness(level); // NAV_USE:inner
    }
    int value = level; // NAV_USE:outer-after
    return value + kDefaultBrightness; // NAV_USE:constant
}
