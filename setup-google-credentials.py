#!/usr/bin/env python3
"""Set up (or check) the CAVE token this app needs, interactively. Standard library only; caveclient is used for the final check if it is installed.

    python setup-google-credentials.py            # guided setup, then checks the token and every sample
    python setup-google-credentials.py --check    # only report on the token already saved, change nothing
    python setup-google-credentials.py --token T  # save a token you already have, no prompts

See README.md, "Setting Google credentials".
"""
import argparse, json, os, re, subprocess, sys, urllib.error, urllib.parse, urllib.request, webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SECRET_DIR = os.path.expanduser("~/.cloudvolume/secrets")  # where caveclient looks
TOKEN_KEY = "token"
# Files caveclient will read, newest convention first. The server file wins when both exist, so write both.
OTHER_NAMES = ["chunkedgraph-secret.json", "cave_token.json"]  # older names, only read, never written
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{20,}$")


def say(*a): print(*a, flush=True)


def read_config(path):
    """The server address and samples the app itself uses, so this script checks exactly what the app will do."""
    try:
        with open(path) as f: c = json.load(f)
        return c["server_address"], dict(c.get("samples") or {})
    except (OSError, ValueError, KeyError, TypeError) as e:
        say(f"Can't read \"server_address\" from config {path}: {e}")
        say("Pass the server yourself with --server https://global.connectomics.braininbrain.org")
        raise SystemExit(2)


def server_file(server):
    return os.path.join(SECRET_DIR, urllib.parse.urlparse(server).netloc + "-cave-secret.json")


def token_files(server):
    return [server_file(server), os.path.join(SECRET_DIR, "cave-secret.json")]


def read_token(path):
    try:
        with open(path) as f: t = json.load(f).get(TOKEN_KEY)
        return t if isinstance(t, str) and t else None
    except (OSError, ValueError, AttributeError):
        return None


def saved_token(server):
    """The token caveclient would use, and where it came from."""
    for p in token_files(server) + [os.path.join(SECRET_DIR, n) for n in OTHER_NAMES]:
        t = read_token(p)
        if t: return t, p
    return None, None


def check_token(server, token):
    """Ask the auth server whether the token is good. Returns (ok, message)."""
    url = f"{server.rstrip('/')}/auth/api/v1/user/token"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: body = json.loads(r.read().decode() or "[]")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403): return False, "the server rejected it: it is wrong, expired, or was replaced by a newer one"
        return False, f"the server answered {e.code} {e.reason}"
    except (urllib.error.URLError, ValueError, OSError) as e:
        return False, f"couldn't reach {url}: {e}"
    users = {d.get("user_id") for d in body if isinstance(d, dict)} if isinstance(body, list) else set()
    who = f", user id {sorted(users)[0]}" if len(users) == 1 else ""
    return True, f"accepted by {urllib.parse.urlparse(server).netloc}{who}"


def check_samples(server, samples):
    """Open each sample the way app.py does. Needs caveclient; skipped with a note if it is missing."""
    if not samples: return True
    try:
        from caveclient import CAVEclient
    except ImportError:
        say("\nSkipping the sample check: caveclient isn't installed here. Install it with")
        say(f"    pip install -r {os.path.join(HERE, 'requirements.txt')}")
        return True
    say("\nOpening each sample from config.json, as the app does:")
    ok = True
    for name, datastack in sorted(samples.items()):
        try:
            CAVEclient(datastack_name=datastack, server_address=server).chunkedgraph
            say(f"    OK      {name} ({datastack})")
        except Exception as e:
            ok = False
            say(f"    FAILED  {name} ({datastack}): {str(e)[:300]}")
    return ok


def clean(pasted):
    """Accept a bare token, a quoted one, or the whole {"token": "..."} line people often copy."""
    s = pasted.strip().strip(",")
    if s.startswith("{"):
        try: s = str(json.loads(s).get(TOKEN_KEY, "")).strip()
        except ValueError: pass
    return s.strip().strip('"').strip("'").strip()


def lock_down(path, dir=False):
    """Best-effort: restrict `path` to the current user. On Windows, chmod only toggles the read-only
    attribute and doesn't stop other accounts on the machine reading it, so an ACL is set with icacls instead."""
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME")
            if user:
                rights = f"{user}:(OI)(CI)F" if dir else f"{user}:F"
                subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", rights], capture_output=True, check=False)
        else:
            os.chmod(path, 0o700 if dir else 0o600)
    except OSError:
        pass


def write_token(path, token):
    """Save the token, keeping any other keys already in the file, and keep it readable only by this user."""
    os.makedirs(SECRET_DIR, exist_ok=True)
    lock_down(SECRET_DIR, dir=True)
    data = {}
    if os.path.exists(path):
        try:
            with open(path) as f: data = json.load(f)
            if not isinstance(data, dict): data = {}
        except (OSError, ValueError): data = {}
        if data.get(TOKEN_KEY) and data[TOKEN_KEY] != token:
            os.replace(path, path + ".bak")
            say(f"    kept the old token in {path}.bak")
    data[TOKEN_KEY] = token
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f, indent=2)
    lock_down(tmp)
    os.replace(tmp, path)
    say(f"    wrote   {path}")


def ask_for_token(server, open_browser):
    """Walk the person through making a token on this server and paste it back."""
    url = f"{server.rstrip('/')}/auth/api/v1/create_token"
    say("\nA token has to be made by hand, in a browser. Three steps:")
    say(f"  1. Open  {url}")
    say("  2. Log in with the Google account that has access to this dataset.")
    say("  3. Copy the token it shows, and paste it below.")
    say("\nThe page shows the token once, and making a new one invalidates the old one,")
    say("so anything else already using your old token will stop working.")
    if open_browser:
        try:
            if webbrowser.open(url): say("\nTrying to open that page in your browser.")
        except Exception: pass
    for attempt in range(3):
        try:
            t = clean(input("\nPaste the token (or press Enter to give up): "))
        except EOFError:
            say("\nNo token given. Nothing was changed.")
            return None
        if not t:
            say("Nothing was changed.")
            return None
        if TOKEN_RE.match(t): return t
        say("That doesn't look like a token (it should be one long word, no spaces)." +
            (" Try again." if attempt < 2 else " Giving up; nothing was changed."))
    return None


def main():
    ap = argparse.ArgumentParser(description="Set up the CAVE token for the segment splitter.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--config", default=CONFIG_PATH, help="JSON config with the CAVE server and samples (default: config.json here)")
    ap.add_argument("--server", help="CAVE global server to set the token up for (default: the one in the config)")
    ap.add_argument("--token", help="save this token instead of asking for one")
    ap.add_argument("--check", action="store_true", help="only report on the saved token, write nothing")
    ap.add_argument("--force", action="store_true", help="ask for a new token even if the saved one works")
    ap.add_argument("--no-browser", action="store_true", help="don't try to open the token page")
    a = ap.parse_args()

    given = clean(a.token) if a.token else None
    if a.token and not TOKEN_RE.match(given or ""): raise SystemExit("That --token doesn't look like a token: one long word, no spaces.")
    if a.server and not os.path.exists(a.config): server, samples = a.server, {}  # --server alone needs no config
    else: server, samples = read_config(a.config)
    if a.server: server = a.server
    if not server.startswith("https://"): raise SystemExit(f"The server must be an https:// URL, not {server!r}")
    say(f"CAVE server:   {server}")
    say(f"Token files:   {SECRET_DIR}")

    if not given:  # is a token already saved, and does it still work?
        token, where = saved_token(server)
        if not token:
            say("\nNo token is saved yet." + (" Nothing to check." if a.check else ""))
            if a.check: return 1
        else:
            say(f"\nFound a saved token in {where}. Checking it:")
            ok, why = check_token(server, token)
            say(f"    {'OK     ' if ok else 'FAILED '} {why}")
            if a.check: return 0 if ok and check_samples(server, samples) else 1
            if ok and not a.force:
                if where not in token_files(server):
                    say("\nThat file is an old name caveclient may stop reading. Copying the token to the current ones:")
                    for p in token_files(server): write_token(p, token)
                say("\nNothing to do. Run with --force to replace this token anyway.")
                return 0 if check_samples(server, samples) else 1

    new = given or ask_for_token(server, not a.no_browser)
    if not new: return 1

    say("\nSaving it:")
    for p in token_files(server): write_token(p, new)

    say("\nChecking the saved token:")
    ok, why = check_token(server, new)
    say(f"    {'OK     ' if ok else 'FAILED '} {why}")
    if not ok:
        say("\nThe token was saved but the server won't accept it. Make a fresh one and run this again.")
        return 1
    good = check_samples(server, samples)
    say("\nDone. " + ("You can start the app with: python app.py" if good else
                      "The token works, but a sample above didn't open: check \"samples\" in your config."))
    return 0 if good else 1


if __name__ == "__main__":
    try: sys.exit(main())
    except KeyboardInterrupt: say("\nStopped. Nothing was changed."); sys.exit(130)
