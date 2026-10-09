// The spec's viewports (mobile dashboard §5). Zoom 200% on a 1440×900 window is
// half the CSS pixels at twice the density; it also runs with reduced motion.
// android-412 is a common Android size (Pixel 7 class); only the terminal checks
// run it, so ALL stays the original five.
export const PROFILES = {
  "phone-320": { viewport: { width: 320, height: 568, deviceScaleFactor: 2, isMobile: true, hasTouch: true }, phone: true },
  "phone-390": { viewport: { width: 390, height: 844, deviceScaleFactor: 3, isMobile: true, hasTouch: true }, phone: true },
  "phone-landscape": { viewport: { width: 844, height: 390, deviceScaleFactor: 3, isMobile: true, hasTouch: true, isLandscape: true }, phone: true },
  "android-412": { viewport: { width: 412, height: 915, deviceScaleFactor: 2.625, isMobile: true, hasTouch: true }, phone: true },
  "desktop": { viewport: { width: 1440, height: 900, deviceScaleFactor: 1 }, phone: false },
  "desktop-zoom200": { viewport: { width: 720, height: 450, deviceScaleFactor: 2 }, phone: false, reducedMotion: true },
};
export const PHONES = ["phone-320", "phone-390", "phone-landscape"];
/** The phone terminal's sizes: iPhone SE and iPhone 14 class, an Android, and landscape. */
export const TERMINAL_PHONES = ["phone-320", "phone-390", "android-412", "phone-landscape"];
export const ALL = ["phone-320", "phone-390", "phone-landscape", "desktop", "desktop-zoom200"];
