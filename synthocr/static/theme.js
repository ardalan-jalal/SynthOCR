"use strict";

// Runs before CSS so a saved preference is applied before the first paint.
const themePreference = (() => {
    const key = "synthocr.theme";
    function apply(value) {
        const theme = value === "light" ? "light" : "dark";
        document.documentElement.dataset.theme = theme;
        document
            .querySelector('meta[name="theme-color"]')
            ?.setAttribute("content", theme === "dark" ? "#101116" : "#f5f5fa");
        return theme;
    }
    let saved = null;
    try {
        saved = localStorage.getItem(key);
    } catch {}
    apply(saved);
    return {
        set(value) {
            const theme = apply(value);
            try {
                localStorage.setItem(key, theme);
            } catch {}
            return theme;
        },
    };
})();
