#include "state.h"

int gBrightness = 128; // NAV_DEF:global

void DisplayState::setBrightness(int brightness) { // NAV_DEF:parameter
    int next = brightness; // NAV_USE:parameter
    this->brightness = next; // NAV_USE:member
    gBrightness = next; // NAV_USE:global
}

int DisplayState::getBrightness() const {
    return brightness; // NAV_USE:member-read
}
