# Review comment editor positioning

Section and selection comments keep their existing quote, heading path and viewed
revision. Opening the editor must preserve the document and browser scroll offsets.
The editor and selection action use a body portal and viewport coordinates, so pane
overflow and transformed ancestors cannot clip or relocate them.

Measure the live heading or cloned selection Range on captured scroll events,
window/visual viewport resize and scroll, and document/editor resize. Prefer a
placement beside the referenced context or below/above it without overlap. Clamp
the editor to the visible viewport, including the mobile keyboard viewport. When
no non-overlapping placement fits, minimize overlap and keep controls reachable
with an internally scrollable editor. Repositioning does not recreate the editor
or discard its draft. Pin submission to the revision on which the editor opened.
Document companions respond to the review container's width, so a narrow pane or
landscape/zoom view keeps room for the paragraph even on a wide browser window.

Focus the labeled textarea with preventScroll. Escape, Cancel and outside pointer
press dismiss the editor; return focus to the originating control without scrolling.
Submission errors retain the draft and expose an accessible error. Tab navigation
stays within the editor while it is open.

Regression evidence uses the built dashboard in headless Chrome against the layout
stub, with a long review scrolled to a nested section. Assert real bounds, unchanged
scroll, visible context, correct submission, pane/browser scrolling, resizing, zoom,
narrow viewports, selection geometry and focus/dismissal; save screenshots. Building
this worktree verifies the candidate. The operator/integration owner must build the
delivered dashboard before describing the live UI as updated.

## Verification evidence

The original built dashboard changed the document scrollTop from 7888 to 0 when
opening a nested section comment. The new check passes all five layout profiles
in Chrome 150: both phones, landscape, desktop and 200% zoom. It also verifies a
480 px review pane inside a desktop window. In a short landscape pane the paragraph
is already partly clipped by the existing reader layout; opening preserves its
visible portion and scroll offset. Drafts survive pane/browser scroll and resize,
the editor retains focus, and both section and quote submissions retain revision 2
and the nested heading path. Escape and outside pointer dismissal are exercised.

- `npm -w dashboard run build` — passed; candidate assets built in this worktree.
- `npm -w dashboard run test -- src/panes/review/__tests__ src/pages/reviews/__tests__ src/ws/__tests__/useEventStream.review.test.tsx src/shell/__tests__/ActivityDrawer.review.test.tsx` — 30 tests passed in 6 files.
- `npm -w dashboard run check:layout -- --only review-comments --out /tmp/brisk-cascade-90-browser` — 5/5 profiles passed.
- `POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres aq test tests/test_dashboard_browser_storage.py tests/test_pane_registry_parity.py` — 5 tests passed.
- Changed review TypeScript files passed ESLint; selection catalogue drift check passed.

Committed screenshots: [original jump](../../reports/review-comment-positioning/before-desktop.png),
[desktop section](../../reports/review-comment-positioning/desktop-section.png),
[phone section](../../reports/review-comment-positioning/phone-section.png),
[landscape selection](../../reports/review-comment-positioning/landscape-selection.png), and
[narrow pane draft](../../reports/review-comment-positioning/narrow-pane-draft.png).
The [browser report](../../reports/review-comment-positioning/report.json) records all five profiles.

Deployment state: this is a tested branch checkpoint. The live dashboard has not
been changed by this worker. Integration/operator delivery and a build from the
delivered source are still required before claiming the fix is live.
