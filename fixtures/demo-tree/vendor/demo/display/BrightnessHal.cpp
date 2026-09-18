// 示例代码：亮度接口实现
#include "BrightnessHal.h"

Status BrightnessHal::setBrightness(int level) {
    const int clamped = std::clamp(level, 0, 255);
    writeBrightnessNode(clamped);
    return Status::ok();
}

void writeBrightnessNode(int value) {
    writeNode("/sys/class/backlight/demo/brightness",
              value);
}
