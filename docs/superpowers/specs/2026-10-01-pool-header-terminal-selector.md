# Pool header terminal selector

For sound-impact-72, pools with more than one live instance expose the existing
native Instance picker directly in their single-row terminal header. The header
picker has a pool-specific accessible label and shows the selected instance;
options retain the session name, project, task and idle-time context. Compact
options lead with their ordinal so the selection remains distinguishable when
long generated session names must truncate.

The header and details pickers share the URL-backed selection and action. Changing
either switches the live terminal without requiring the disclosure to open.
Added instances preserve an existing selection. A removed selection falls back to
the oldest remaining live instance, matching the existing terminal behavior. Pools
with zero or one live instance retain their existing header and details behavior.

The picker shrinks within a bounded width, leaving transport, details and close
controls usable. The header remains one row, including at 320 px and at 200% zoom.
Native keyboard selection and visible focus styling remain available.

Verify pool selection and session churn with component tests; verify keyboard
switching, selected-instance removal and header geometry against the built
dashboard in the existing terminal-headers browser check. Rebuild dashboard assets
before delivery; the integration owner publishes the delivered build.
