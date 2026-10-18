# GrantRunner

Run grant-application browser work on your desktop PC. Use a laptop dashboard to see progress, answer questions, approve decisions, and supply documents. The worker continues other applications while one waits for you.

This first version works with ordinary HTML forms. It includes a local fictional grant portal and real-browser integration tests. It has **not been validated against your actual grant sites yet**. Complex portals, embedded forms, custom widgets, login flows, signatures, and unusual submission controls may need a site-specific handler.

## Start on your PC

Install Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/), then open a terminal in the GrantRunner folder:

```sh
uv sync --frozen --extra dev
uv run playwright install chromium
uv run grantrunner --demo
```

On Linux, Playwright may also require system browser libraries: `uv run playwright install --with-deps chromium` installs them through the system package manager. On Windows and macOS the normal browser installation is usually sufficient.

Open the dashboard at loopback address `127.0.0.1`, port `8765` in your browser. In a second terminal in the same folder, get your private sign-in token:

```sh
uv run grantrunner --show-token
```

Do not share the token or commit the `.grantrunner` folder. The dashboard token grants access to team data, documents, browser screenshots, and application decisions. The server binds only to the local PC by default.

### Try a complete local application

1. Open **Team profile**, choose **Load example facts**, and save. These facts are fictional; replace them before using real grants.
2. Choose **Try the demo**, enter an amount, and add the opportunity.
3. In the action center, confirm eligibility, provide the missing community-impact response, and upload a small PDF, image, or text file as the sponsorship packet.
4. Inspect the filled form screenshot and approve submission. A fictional confirmation appears in the application receipt.

The demo sends nothing to a sponsor. Reusing the same URL is blocked to prevent accidental duplicate applications. For another demo run, use a separate data folder rather than deleting real application history.

For real use, restart without `--demo`:

```sh
uv run grantrunner
```

## Connect your laptop

The Python worker, browser, database, and uploads run on the **PC**. The laptop only renders the dashboard. The PC must stay awake, connected, and running GrantRunner.

Use [Tailscale](https://tailscale.com/kb/1312/serve) on both machines in the same private tailnet. On the PC, with GrantRunner running on its default local port:

```sh
tailscale serve --bg http://127.0.0.1:8765
```

Follow Tailscale's HTTPS setup if prompted. Use the private HTTPS address that Tailscale prints on your laptop and sign in with your PC's dashboard token. GrantRunner does not configure Tailscale or expose a public tunnel. Leave the application bound to loopback; do not port-forward it to the Internet.

Choose **Enable alerts** and leave the dashboard tab open. Browser notifications require permission and a secure context (HTTPS, or localhost on the PC). Questions also appear in the action center and tab title. This version does **not** provide push delivery after the tab/browser closes, or while the laptop sleeps. Tailscale connectivity and laptop notification delivery must be checked on your own devices; the cloud tests cannot validate them.

If a portal requires interactive login or CAPTCHA, restart the worker with `uv run grantrunner --headed` on the PC. Use the PC directly or your preferred remote-desktop tool. Complete the challenge in the worker's browser, then choose **Retry after correction** in the dashboard. The dashboard screenshot itself is not remote browser control. Up to three application browser contexts are retained; older waiting pages may reopen from the original URL using saved answers and cookies.

## Your team profile and grant list

Use **Team profile** for verified facts and writing preferences. Additional approved responses map an **exact form question** to an answer. Uploaded documents have IDs; reusable attachments map an exact form question to one of those IDs. Answers provided in the action center can be remembered automatically.

Add direct application links individually, paste one URL per line, or import CSV with these columns:

```csv
name,url,request_amount,eligibility_confirmed
Example sponsor,https://example.org/apply,1500,false
```

See [examples/team-profile.json](examples/team-profile.json) for the API profile format. Set `eligibility_confirmed` to true only after checking that your team satisfies that grant's requirements. This version does not independently assess eligibility, grant deadlines, or nonprofit status. Use one currency consistently; there is no currency conversion.

### Automatic submission

Initially, GrantRunner asks before final submission. To permit unattended submissions, enable **Allow automatic submissions**, set the maximum request amount, and mark each selected opportunity eligible. The application must have a request amount within that limit. Unknown required fields, documents, and certifications still generate questions. Empty optional fields are left blank unless an answer is explicitly available.

For supported forms with all facts and attachments available, the worker fills and submits automatically. Ordinary public websites are supported; private/local network URLs are blocked. Only the deliberately enabled local demo is exempt. The browser honors TLS validation. There is no CAPTCHA bypass or automatic signing on someone else's behalf.

The worker records a durable boundary **before** clicking final Submit. If it crashes, loses the connection, or cannot identify a receipt after that point, it asks you to check the portal or confirmation email. It never automatically clicks Submit again. The URL history also prevents queueing the same URL twice; it cannot identify duplicate grants represented by unrelated URLs or different query strings.

The generic handler recognizes common text inputs, numbers, select controls, checkboxes, file uploads, Next/Continue buttons, and final submission buttons. It pauses for unsupported flows. A portal receipt is evidence of receipt, not grant approval. Nonstandard sites need targeted testing and adapters before unattended use.

### Optional AI writing

No API key is needed for known facts, approved answers, file uploads, dashboard responses, or the local demo. To enable AI suggestions for unknown required narrative fields, set `GRANTRUNNER_LLM_API_KEY` securely in the PC process environment. `GRANTRUNNER_LLM_MODEL` defaults to `gpt-4.1-mini`. Requests go to OpenAI's HTTPS chat-completions endpoint.

With a key present, the provider receives the form question, team facts, and approved reference answers to suggest a response. Suggestions appear in questions for review. **Allow generated narrative answers** additionally authorizes using those drafts without review. Generation is instructed to use only supplied facts, but can still be wrong; leave that option off until you trust the source profile and results. This provider integration is covered with mocked HTTP tests, not a live paid API call.

Do not put passwords or API keys in your team profile or approved answers. Profile text may be sent to the writing provider. Browser sessions, screenshots, documents, and application answers contain sensitive data: protect the PC account and backups.

## Development

```sh
uv sync --frozen --extra dev
uv run playwright install chromium
uv run --extra dev pytest -q
```

To use an existing compatible Chromium installation, set `GRANTRUNNER_BROWSER_EXECUTABLE` to its executable path. In the supplied cloud machine, `/usr/bin/chromium` is available and was used for validation:

```sh
GRANTRUNNER_BROWSER_EXECUTABLE=/usr/bin/chromium uv run --extra dev pytest -q
GRANTRUNNER_BROWSER_EXECUTABLE=/usr/bin/chromium uv run grantrunner --demo
```

On Windows PowerShell, set process variables using `$env:GRANTRUNNER_BROWSER_EXECUTABLE = 'C:\path\to\chrome.exe'`; omit the variable to use Playwright's installed browser. `GRANTRUNNER_DATA_DIR` selects a persistent data directory (default: `.grantrunner` in the working directory). `GRANTRUNNER_TOKEN` can override the generated token; use at least 24 characters. `--port` changes the listening port. `--headed` shows the browser on a machine with a graphical session.

Tests use temporary data and a local HTTP portal. They cover authentication, cross-origin writes, bounded uploads, URL filtering, remembered answers, duplicate imports, submission crash recovery, an actual browser fill/pause/upload/approval/receipt cycle, standing authorization, uncertain outcomes, and dashboard desktop/mobile behavior. Real grant portals, AI-provider availability, and the private PC/laptop network require separate validation.

The backend lives in `src/grantrunner`: `app.py` provides the authenticated API, `store.py` stores progress in SQLite, and `browser.py` performs browser work. The dashboard uses plain HTML, CSS, and JavaScript in `static/`. Only one application runs at a time to keep PC resource use modest. Waiting applications keep their own answers and documents. Browser processes restart on launch; SQLite, uploads, screenshots, and session cookies persist in the data directory.

To back up, stop GrantRunner and copy the entire data directory securely. Restoring only the database omits documents and browser sessions. To rotate dashboard access, stop the server, remove `dashboard-token` from the data directory (or change `GRANTRUNNER_TOKEN`), and restart. Existing dashboard cookies will no longer authenticate.
