// 示例代码：内核背光回调
#include <linux/backlight.h>

static int panel_update_status(
    struct backlight_device *device) {
    int level = device->props.brightness;
    return panel_write_register(level);
}

static const struct backlight_ops demo_ops = {
    .update_status = panel_update_status,
};
