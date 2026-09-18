// 示例代码：屏幕亮度控制
package demo.display;

public final class DisplayController {
    private int lastBrightness = -1;

    public void setBrightness(int level) {
        int safeLevel = Math.max(0, level);
        if (safeLevel == lastBrightness) {
            return;
        }
        nativeSetBrightness(safeLevel);
        lastBrightness = safeLevel;
    }

    private static native void
        nativeSetBrightness(int level);
}
