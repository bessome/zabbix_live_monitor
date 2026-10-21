# Project guidance for code changes

## Numeric values and charts

- Apply this display rule to every existing and new numeric measurement throughout the app: at most two decimal places, with trailing fractional zeros removed (`-19.400000000000002` → `-19.4`, `12.00` → `12`, `1.236` → `1.24`). Display rounded negative zero as `0`.
- Use the same formatting for lists, device cards, live readings, chart axes, tooltips, legends, overlays, and latest/min/max/average summaries. Keep units visible and preserve integer counters and status labels. Missing or non-finite readings must retain the appropriate `n/a` or `—` state, never become zero.
- Round only for presentation. Preserve source values and precision for history, chart coordinates, rate calculations, aggregation, and alarm threshold comparisons. Do not round identifiers, MAC/IP addresses, dates, or timestamps as measurements.
- When modifying a measurement or chart, apply the existing display formatter consistently to its current and historical views. Consult `.codex/skills/zabbix-device-metrics/SKILL.md` for the device-metrics workflow and formatting checks.

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
