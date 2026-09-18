// 示例代码：应用层调用
package demo.display;

public class BrightnessPanel {
    private DisplayController controller;

    public void onSliderChanged(int value) {
        controller.setBrightness(value);
    }
}
