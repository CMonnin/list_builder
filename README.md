# WH40K 11th Edition List Builder

A simple army list builder for Warhammer 40K 11th Edition. No army composition rules — just pick units and track your points.

## Local Development

The app is a single static HTML file — no build step or server needed.

```bash
# Serve from the project root (for the points.json fetch to work)
cd list_builder_pi
python -m http.server 8000
```

Then open http://localhost:8000 in your browser.

> **Note:** Opening `index.html` directly from the filesystem (`file://`) will **not** work — the browser blocks `fetch()` to local files. Use a local HTTP server.

## Updating Points Data

The `points.json` file is generated from the official GW Munitorum Field Manual PDF.

### Via GitHub Actions (recommended)

1. Go to the **Actions** tab on GitHub.
2. Select the **Update Points** workflow.
3. Click **Run workflow**.
4. Enter the URL to the latest Munitorum Field Manual PDF.
5. The script will parse the PDF, write a new `points.json`, and commit it.

### Manually (local)

```bash
# Install dependencies
uv pip install -r requirements.txt

# Run the script
uv run python scripts/update_points.py --url "https://example.com/mfm.pdf" --output points.json
```

## Deploying to GitHub Pages

1. Push this repository to GitHub.
2. Go to **Settings → Pages**.
3. Set the source to the `main` branch, root (`/`) directory.
4. Your site will be live at `https://<username>.github.io/<repo>/`.

The points data is embedded in the repo — whenever `points.json` is updated via the GitHub Action, the live site updates automatically.
