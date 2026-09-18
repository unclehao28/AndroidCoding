package demo;

public final class ScopeDemo {
    private int level = 1; // NAV_DEF:java-field

    public int adjust(int level) { // NAV_DEF:java-parameter
        int result = level; // NAV_USE:java-parameter
        this.level = result; // NAV_USE:java-field
        return result; // NAV_USE:java-local
    }

    public int read() {
        return level; // NAV_USE:java-field-read
    }

    public int siblingBlocks() {
        int sum = 0;
        {
            int value = 2; // NAV_DEF:java-first-local
            sum += value; // NAV_USE:java-first-local
        }
        {
            int value = 3; // NAV_DEF:java-second-local
            sum += value; // NAV_USE:java-second-local
        }
        return sum;
    }
}
