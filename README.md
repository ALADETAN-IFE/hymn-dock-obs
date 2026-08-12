# Hymn Dock for OBS — Render Edition

This version runs on Render, so **Python does not need to be installed on the OBS computer**.

## Files to upload to GitHub

Upload these files to the root of your repository:

- `server.py`
- `dock.html`
- `display.html`
- `requirements.txt`
- `render.yaml`
- `README.md`

Do NOT upload the old `start_hymn_dock.bat` for the Render deployment.

## Render

Create a Render Web Service from the GitHub repository.

Recommended settings:

- Language/Runtime: Python
- Build Command: leave blank
- Start Command: `python server.py`
- Plan: Free

Render automatically supplies the `PORT` environment variable. `server.py` listens on `0.0.0.0`.

After deployment, your service URL will look like:

`https://YOUR-SERVICE-NAME.onrender.com`

## OBS URLs

Custom Browser Dock:

`https://YOUR-SERVICE-NAME.onrender.com/dock`

Browser Source:

`https://YOUR-SERVICE-NAME.onrender.com/display`

Recommended Browser Source size:

- Width: 1920
- Height: 1080
- FPS: 30

## Test

Open:

`https://YOUR-SERVICE-NAME.onrender.com/dock`

Enter `235` and press LOAD.

Then click a stanza.

Open:

`https://YOUR-SERVICE-NAME.onrender.com/display`

The selected stanza should appear.

## Important

The app fetches hymn pages from Treasure Hymns from the Render server. Your OBS computer therefore does not need Python.

The app stores the current selected hymn/stanza in the running web service process. For a single OBS computer and normal church streaming this is sufficient. Do not run multiple instances of the service unless state synchronization is added.

If the Render service has been sleeping, the first request can take longer while it wakes.

If Treasure Hymns changes its HTML structure, the parser may need an update.

## Local fallback

If you later install Python locally, the original local package can be used instead.
