// The spec's viewports (mobile dashboard §5). Zoom 200% on a 1440×900 window is
// half the CSS pixels at twice the density; it also runs with reduced motion.
export const PROFILES = {
  "phone-320": { viewport: { width: 320, height: 568, deviceScaleFactor: 2, isMobile: true, hasTouch: true }, phone: true },
  "phone-390": { viewport: { width: 390, height: 844, deviceScaleFactor: 3, isMobile: true, hasTouch: true }, phone: true },
  "phone-landscape": { viewport: { width: 844, height: 390, deviceScaleFactor: 3, isMobile: true, hasTouch: true, isLandscape: true }, phone: true },
  "desktop": { viewport: { width: 1440, height: 900, deviceScaleFactor: 1 }, phone: false },
  "desktop-zoom200": { viewport: { width: 720, height: 450, deviceScaleFactor: 2 }, phone: false, reducedMotion: true },
};
export const PHONES = ["phone-320", "phone-390", "phone-landscape"];
export const ALL = Object.keys(PROFILES);
