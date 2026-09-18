#pragma once

extern int gBrightness; // NAV_DECL:global
inline constexpr int kDefaultBrightness = 128; // NAV_DEF:constant

struct DisplayState {
    int brightness = 0; // NAV_DEF:member
    void setBrightness(int brightness);
    int getBrightness() const;
};
