# Project guidance for code changes

## Numeric values and charts

- UPSs displays separate `Output Load L1`, `Battery Capacity`, and `Autonomy` numeric items in lists, trees, and cards, with sorting for each column. Battery Capacity < 50 is critical (red), 50 <= capacity < 100 is warning (orange), and capacity >= 100 is normal, using original values. Other UPS readings have no alarm thresholds. The UPS card uses the shared modem layout with an Output Load L1 history chart in place of the ping chart, readings on the right, and device details below the chart.

- EDFA shows separate `Optical output level` and `Optical input level` columns in lists and tree branches, and both readings beside the ping chart in its card. Read enabled numeric Zabbix items with those exact names; use their units and the existing numeric history. Missing readings stay `n/a`. Do not apply Amplifires alarm thresholds to EDFA without explicit EDFA thresholds.
- EDFA thresholds use original values: Output <= 20 is critical (red), 20 < Output < 22.7 is warning (orange), Output >= 22.7 is normal; Input < -1 is critical (red), Input >= -1 is normal. Reuse the server-provided state in lists, trees, and cards. Color the entire row only when both readings are abnormal; use orange if either is a warning, and keep a critical cell red.

- Apply this display rule to every existing and new numeric measurement throughout the app: at most two decimal places, with trailing fractional zeros removed (`-19.400000000000002` → `-19.4`, `12.00` → `12`, `1.236` → `1.24`). Display rounded negative zero as `0`.
- Use the same formatting for lists, device cards, live readings, chart axes, tooltips, legends, overlays, and latest/min/max/average summaries. Keep units visible and preserve integer counters and status labels. Missing or non-finite readings must retain the appropriate `n/a` or `—` state, never become zero.
- Round only for presentation. Preserve source values and precision for history, chart coordinates, rate calculations, aggregation, and alarm threshold comparisons. Do not round identifiers, MAC/IP addresses, dates, or timestamps as measurements.
- When modifying a measurement or chart, apply the existing display formatter consistently to its current and historical views. Consult `.codex/skills/zabbix-device-metrics/SKILL.md` for the device-metrics workflow and formatting checks.

## Device card layout

- Use the modem layout and shared `metricColumns` implementation as the template for every current and future device category with displayed parameters. Every numeric list/tree column must support ascending and descending sorting. Build headers and sort controls from the shared column definitions; sort lists before pagination using source precision, keep missing values last, and keep tree sorting local to each branch.

- In TV_Amplifires lists and trees, append the Zabbix `Device description` to the visible device name in parentheses. For descriptions containing `Lambda PRO 71`, `ECM01`, `V4.7`, or any of `V6.2.3`, `V6.2.4`, `V5.2.4`, display respectively `Vector L PRO 71`, `Vector B PRO 51`, `OPR-2108G`, or `ASCENT AON1110` as that suffix. Other descriptions remain as supplied; omit empty parentheses when no description is available. Keep the original description in the device card and preserve the suffix during direct SNMP metric updates.

- Use the Amplifires list table as the common layout for all device categories, in both list and tree views: device name on the left, one separate column on the right for each displayed value, and the favorite action last. Never combine two measurements into one cell or place summaries below the device name. Modems have separate US Level and DS Level columns; Amplifires have an optical-level column. Text descriptions also occupy their own column when displayed in a list.
- In mobile list cards, keep numeric measurements side by side in a single row of separate cells. Use compact labels (US/DS), smaller text, and no wrapping of a measurement and its units. The device name remains above that row, and the favorite action stays at the top right. Text descriptions may wrap.
- Sorting a tree branch must reorder only that branch's direct device rows. Keep sibling and closed branches, group hierarchy, expansion state, and list-mode sorting unchanged. Store the sort independently per branch and reapply it when that branch's readings refresh. Do not apply a global API sort to the entire tree when the user clicks a branch header.
- Highlight the whole device row as abnormal only when every displayed numeric measurement is present and outside its normal range. If some measurements are normal or missing, highlight only the abnormal measurement cells. Missing/non-finite values remain `n/a` and never count as abnormal. Do not color an entire row as normal: normal measurements may be highlighted individually. Apply thresholds to original values or authoritative `alert` flags before display rounding. Reuse the shared `metricColumns`, `updateRowSummary`, and `.device-metric-table` implementation for new device types and metrics.

- Device lists and trees do not have a separate address column. Use `device_display_name` for visible names: append ` - address` only when that complete address is absent from the name. Keep the original Zabbix name and address unchanged. Use the same display name in card titles and saved-device lists.

- Place the favorite star at the right edge of a device row, in a dedicated narrow final column in desktop lists and trees. In mobile cards, place it at the top right. Keep it out of the device name and measurement columns; device titles and saved-device lists also keep this action on the right.

- Always show `Device description` in the "Устройство" block for every Zabbix device category, including TV_Amplifires and EDFA. Use `—` when it is missing. Additional values such as optical power must not replace the description in this block.

- Use the modem card layout for every device type whenever a desktop card has measurements beside the ping chart: ping chart on the left, additional measurements on the right, and the "Устройство" details directly below the ping chart at the same width. Never reverse these columns for a particular device type.
- Reuse `.device-monitor-grid` with `.ping-panel`, `.device-side-panel`, and `.device-details` in `templates/device.html` and `static/ping.css`. Put the ping panel before the measurements in document order. On narrow screens and in forced mobile mode, stack the chart, measurements, and details in that order.
- Cards without measurements beside the chart (currently Switches, VOIP, and Routers) keep their existing full-width layout. If such a card gains a side panel, adopt the same shared layout; do not create a device-specific column order.

## HTTPS and page assets

- Production traffic reaches Caddy over HTTPS; Caddy connects to FastAPI/Uvicorn over HTTP inside the Compose network. Do not assume `request.url.scheme` inside the app is the browser's scheme.
- Links to this app's CSS, JavaScript, images, and API endpoints must be origin-relative (for example `/static/style.css` or `url_for('static', path='/style.css').path`). Do not render an absolute `http://` URL for a page resource. An intentional link that opens a device's own HTTP interface in a new tab is separate from page resources.
- When changing a template, static mount, proxy configuration, or URL generation, check the rendered login page and a device page for asset URLs. A page that loads while CSS or JavaScript fails is still broken.
- Keep the regression test in `tests/test_app.py` that checks `/login` emits `/static/style.css` and that the stylesheet is served. Extend it when a new type of asset or URL generation is introduced. Run the app tests with PyCharm's configured Python interpreter; do not use Docker as the test runner for routine changes.

## Deployment and secrets

- `compose.yaml` runs the app, while `compose.https.yaml` selects Caddy's local certificate and `compose.cert.yaml` selects the supplied certificate. Use only one HTTPS overlay at a time.
- Never commit `.env`, certificates, private keys, or files under `certs/`. Keep certificate mounts read-only and check ignored paths before committing deployment changes.

## Password forms

- Every new password entered in the web UI needs a confirmation field and a server-side equality check before any database mutation. This includes user creation, Admin changes to another user's password, Admin's own password, and self-service changes in `/profile`.
- Changing a user's other settings must still work when both optional password fields are empty. Require the current password for self-service password changes and keep the minimum length at 9 characters.
