# Segment splitter

A small web app that cuts large, fused CAVE segments into smaller pieces, to make manual proofreading easier. Each split is a **real chunkedgraph edit**.

| File | Purpose |
|---|---|
| `app.py` | Web server and page (Python standard library only) |
| `splitter.py` | Chooses terminals, previews, and executes one cut |
| `setup-google-credentials.py` | Sets up and checks your CAVE token |
| `config.json` | The samples to choose from and the CAVE server |
| `diagnose.py` | Read-only check of which terminal layouts the server accepts for one segment |
| `end_user/guide.html` | User guide with screenshots, also shown in the app under **How to use** |
| `requirements.txt` | Python packages (`pip install -r requirements.txt`) |

## Setup

Needs Python 3.9+, on Mac, Linux, or Windows.

```
pip install -r requirements.txt
```

On Windows, run these with `python` from an ordinary Command Prompt or PowerShell (the [python.org](https://python.org) installer adds it to `PATH`) — no other setup is needed, and nothing here needs WSL or Git Bash.

### CAVE credentials

```
python setup-google-credentials.py
```

It reads the server from `config.json`, says whether a token is already saved and still works, and otherwise prints the page to make one on (opening it in a browser if it can), takes the pasted token, saves it where `caveclient` looks, and then opens every sample in `config.json` the way the app does. It never prints your token.

| Option | Use |
|---|---|
| `--check` | Only report on the saved token. Writes nothing. |
| `--force` | Replace a token that already works. |
| `--token T` | Save a token you already have, with no prompts. |
| `--no-browser` | Don't try to open the token page (headless servers). |
| `--server URL` | Set a token up for another CAVE server than the config's. |

Tokens go to `~/.cloudvolume/secrets/` (on Windows, `C:\Users\<you>\.cloudvolume\secrets\`), in both `cave-secret.json` and `<server>-cave-secret.json`, restricted to your account — an NTFS permission on Windows, a Unix file mode elsewhere. An old token being replaced is kept alongside as `.bak`. Making a new token invalidates the previous one, so anything else using the old one stops working.

To do it by hand instead: open `https://global.connectomics.braininbrain.org/auth/api/v1/create_token`, log in with Google, and save the token shown as `~/.cloudvolume/secrets/cave-secret.json`:

```
{"token": "YOUR_TOKEN"}
```

**If the token later expires or is revoked:** jobs fail with "401 … Token is Invalid or Expired" and nothing is edited. Run this on the server with the app's Python, as the Linux user that runs the app (the app uses whichever token is saved for that user):

```
python setup-google-credentials.py --force --no-browser
```

It prints a URL: open it, log in, and paste the token back. No restart is needed. If a job was marked "Outcome unknown" because of this, click **Re-check** once the token works.

### Samples and server

List the datasets to choose from, and the CAVE server they're on, in `config.json`:

```json
{
  "server_address": "https://global.connectomics.braininbrain.org",
  "samples": {
    "Parker-Goog": "zlatic_parker_google_segmentation_datastack",
    "Parker_LSD": "zlatic_parker_v1b_datastack"
  }
}
```

Each entry maps a menu name to a datastack name. The app refuses to start, and says why, if the file is missing or malformed. To use a config file kept elsewhere, add `--config /path/to/config.json` when running `app.py` (or `setup-google-credentials.py`).

## Run

```
python app.py --selftest   # fake server, no CAVE calls
python app.py              # picks a sample, checks CAVE, then prints a link containing the access token
```

Before it opens the port, `app.py`:
1. **Picks which sample this run will serve.** With one sample in `config.json` it's used automatically; with more than one you're shown a numbered menu to choose from. Pass `--sample NAME` to skip the prompt (handy in `screen`/`tmux`, or scripts).
2. **Opens that sample on CAVE**, the same way a real job would. If this fails — an expired token, a wrong datastack, no network — the app prints the error and **exits without starting**, so a broken connection shows up immediately instead of on your first edit. It tells you to run `python setup-google-credentials.py` to fix the token, then try again.

Only then does it print the link. The chosen sample can't be changed from the page — it's shown in a badge next to the app's title, so you always know which one you're editing. To work on a different sample, stop the app and start it again (optionally with a different `--sample`).

The app listens on `localhost:8765` (change with `--port`). To open it from your own machine, run `ssh -L 8765:localhost:8765 <host>`, then open the printed link (Windows has `ssh` built in since Windows 10; PuTTY's `plink -L 8765:localhost:8765 <host>` also works). Start it in `screen` or `tmux` to keep it running after you disconnect. Windows has no equivalent, so either leave the console window open, or start it minimized so it stays out of the way without losing the printed link: `start /min python app.py` (Command Prompt) or `Start-Process python -ArgumentList "app.py" -WindowStyle Minimized` (PowerShell) — don't use `-WindowStyle Hidden` or `pythonw.exe` here, since both discard the console output, and the link is only ever printed, never saved anywhere. Each start picks a sample afresh and prints a new link.

## Use

1. Check the sample badge next to the title — that's the dataset every job below will run against for this run of the app.
2. Enter one segment ID and click **Start**.
   - **Preview first** (default) runs a read-only preview, then makes the cut without asking if it passes. A failed preview edits nothing.
   - Unticked, it goes straight to the real edit after a confirmation prompt.
3. Set **Rounds** to keep cutting automatically (up to 6). Each round cuts the two large pieces from the previous one, so A → B + C, then B → B0 + B1 and C → C0 + C1. N rounds is at most 2^N − 1 edits.
   - With **Preview first**, every cut, including the first, runs its own preview and splits without asking if that passes.
   - Unticked, you confirm once and no cut in the tree is previewed.
4. Follow the jobs under **Progress**, where each piece is nested under its parent. A finished split lists its op number and new root IDs. **Copy** next to a result copies it with the sample, segment, status and, for errors, the full server response (the page shows only the server's message).

A branch stops, while the others continue, if its preview fails, its cut can't be verified or doesn't separate the sides, or its outcome is unknown. Pieces under 10 L2 nodes and small fragments aren't cut. **Re-check** doesn't resume a stopped branch; paste a new ID in to continue it.

To stop a running tree, stop the app: running edits still finish, and queued jobs are marked failed on restart.

Each cut gives two large pieces and sometimes a few small fragments. Previews don't show those fragments.

## How a cut works

1. Read the root's L2 graph and bisect it spectrally.
2. Grow a connected ball (20% of nodes) from the deepest node of each half, and take about 40 spread points from each ball as terminals.
3. Add the outermost L2 node on each axis to its nearest side. Without these, the sides can stay joined around the box the terminals span (op 1581 failed this way; op 1594 worked with them).
4. Prefer L2 nodes with 2+ supervoxels as terminals. A lone supervoxel only has edges across chunk borders, which can fall outside the server's cut graph ("Invalid vertex index").
5. Send the terminals to `preview_split` or `execute_split`. If the preview fails with "Invalid vertex index" or "paths … irreparably overlap", it retries up to 3 times: first without the outermost nodes (overlap only), then with a random half of the terminals. Execute never retries, and uses the exact terminals of the passed preview, or fresh ones if there was no preview.

A preview passes if the split is legal, every terminal is on its own side, at most 1% of supervoxels are missing, no L2 links remain between the sides, and the smaller side has at least 10% of L2 nodes. Passing doesn't guarantee a real split, but it has predicted the sizes well so far.

## Practical notes

- **Edits are permanent, but can be undone:** use `cg.undo_operation(op)`, newest first. Root IDs change with every edit, so anything keyed on an old root is stale.
- **Don't kill a running edit:** the server still applies it, and the log row is lost. Stopping the app doesn't cancel it either.
- **"Outcome unknown":** the request failed, but the edit may have applied. The segment is blocked until you click **Re-check**.
- **Skipping the preview:** a failed cut only shows up after the edit, so keep the preview on for large segments.
- **Limits:** segments under 10 L2 nodes are refused, and up to 4 jobs run at once across all trees. Time per cut grows with segment size, from about 15 s at a few hundred L2 to about 3 min at 6,000 L2.
- **Access:** the token in the link allows edits with your CAVE credentials, so don't share it. Using `--host` for anything but localhost sends it unencrypted.
- **One sample per run:** jobs always use the sample chosen at startup — there's no way to switch it from the page. Restart the app to work on a different one.

## Outputs

Written to `outputs/`, which isn't gitignored:

- `gui_jobs.json`: progress, reloaded on restart
- `<root>/edit_log.csv`: one row per edit, with no header: op, root, new roots, L2 sizes, source and sink terminals
- `<root>/preview_terminals.json`: the latest terminals for that root
