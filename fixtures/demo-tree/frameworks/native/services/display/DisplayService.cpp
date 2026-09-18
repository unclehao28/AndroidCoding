// 示例代码：Native 服务
#include "DisplayService.h"

void DisplayService::setBrightness(int level) {
    if (brightnessHal == nullptr) {
        return;
    }
    brightnessHal->setBrightness(level);
}
