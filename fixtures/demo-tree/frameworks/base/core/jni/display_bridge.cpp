// 示例代码：JNI 静态注册关系
#include <jni.h>
#include "DisplayService.h"

static void nativeSetBrightness(
    JNIEnv* env, jclass clazz, jint level) {
    DisplayService::instance()
        .setBrightness(level);
}

static const JNINativeMethod METHODS[] = {
    {"nativeSetBrightness", "(I)V",
     reinterpret_cast<void*>(nativeSetBrightness)}
};
