# Project guidance for code changes

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
