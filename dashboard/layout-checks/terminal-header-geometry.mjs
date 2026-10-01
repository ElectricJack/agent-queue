export async function terminalHeaderGeometry(page) {
  return page.evaluate(() => [...document.querySelectorAll('section[aria-label$="agent window"]')].map((pane) => {
    const header = pane.querySelector("header");
    const screen = pane.querySelector("[data-interactive-terminal], [data-allow-overflow-x]");
    const height = (el) => el?.getBoundingClientRect().height ?? 0;
    return { name: pane.getAttribute("aria-label"), pane: height(pane), header: height(header), terminal: height(screen) };
  }));
}
