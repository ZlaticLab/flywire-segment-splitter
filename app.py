"""Web page for splitting segments. The Split action makes REAL chunkedgraph edits. See README.md."""
import argparse, concurrent.futures, hmac, json, os, re, secrets, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import splitter as bs

HERE = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(HERE, "config.json")  # samples for the menu and the CAVE server: see README.md
SAMPLES, SERVER = {}, ""  # sample name -> datastack name, and the CAVE global server, both set by load_config()
SAMPLE = None  # the one sample this run serves, chosen before the server starts: see pick_sample()
RUNNING = {"queued", "previewing", "splitting"}
MAX_ROUNDS = 6  # 63 edits at most from one segment
TOKEN = secrets.token_urlsafe(32)
STATE_PATH = os.path.join(HERE, "outputs", "gui_jobs.json")
GUIDE_DIR = os.path.join(HERE, "end_user")  # guide.html and its screenshots, also served at /help
jobs = []  # root IDs are strings: they don't fit in a JS number
lock = threading.RLock()
pool = concurrent.futures.ThreadPoolExecutor(4)


def load_config(path):
    """Read the samples and server from a JSON config. Fails at startup, with the reason, if the file is unusable."""
    global SERVER
    try:
        with open(path) as f: c = json.load(f)
    except (OSError, ValueError) as e: raise SystemExit(f"Can't read config {path}: {e}")
    s, srv = c.get("samples") if isinstance(c, dict) else None, c.get("server_address") if isinstance(c, dict) else None
    if not (isinstance(s, dict) and s and all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in s.items())):
        raise SystemExit(f"Config {path}: \"samples\" must map at least one sample name to a datastack name")
    if not (isinstance(srv, str) and srv.startswith("https://")):
        raise SystemExit(f"Config {path}: \"server_address\" must be an https:// URL")
    SAMPLES.clear(); SAMPLES.update(s); SERVER = srv


def connect(sample):
    from caveclient import CAVEclient
    return CAVEclient(datastack_name=SAMPLES[sample], server_address=SERVER).chunkedgraph


def pick_sample(requested, ask=input):
    """Choose the one sample this run will serve. --sample picks it non-interactively; otherwise a lone sample
    is used without asking, and two or more are offered as a numbered menu. Raises SystemExit on a bad --sample."""
    names = list(SAMPLES)
    if requested is not None:
        if requested not in SAMPLES: raise SystemExit(f"Unknown sample {requested!r}. Choices: {', '.join(names)}")
        return requested
    if len(names) == 1:
        print(f"Using the only configured sample: {names[0]} ({SAMPLES[names[0]]})", flush=True)
        return names[0]
    print("Samples available:")
    for i, n in enumerate(names, 1): print(f"  {i}. {n} ({SAMPLES[n]})")
    while True:
        choice = ask("Choose a sample (number or name): ").strip()
        if choice in SAMPLES: return choice
        if choice.isdigit() and 1 <= int(choice) <= len(names): return names[int(choice) - 1]
        print(f"Not a valid choice: {choice!r}. Enter a number from 1-{len(names)}, or one of: {', '.join(names)}")


def verify_connection(sample):
    """Try to open this sample on CAVE, the same way a real job would. Returns None on success, else a readable error."""
    try: connect(sample)
    except Exception as e: return brief(e)
    return None


def save():
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f: json.dump(jobs, f)
    os.replace(tmp, STATE_PATH)


def load():
    if not os.path.exists(STATE_PATH): return
    with open(STATE_PATH) as f: jobs[:] = json.load(f)
    for j in jobs:  # jobs did not survive the restart
        if j["status"] == "splitting":
            j.update(status="unknown", message="The page stopped during this edit, which may still have applied. Wait a few minutes, then re-check.")
        elif j["status"] in RUNNING:
            j.update(status="failed", message="The page stopped before this finished. Nothing was edited. Start it again.")


def brief(e, n=600):
    """Readable error: an HTTP error's raw response body is replaced by the server's own message. The Copy button gives the full text."""
    s = str(e); m = re.search(r"\s*for url: \S+ content:\s*b'(.*)", s, re.S)
    if m:
        inner = re.search(r'"message":\s*"((?:[^"\\]|\\.)*)"', m.group(1))
        s = s[:m.start()] + (f" ({inner.group(1)})" if inner and inner.group(1) not in s[:m.start()] else "")
    s = s if len(s) <= n else s[:n] + "… (Copy for the full text)"
    if "401" in s and "token" in s.lower(): s += " The CAVE token on the server is missing or expired: renew it (README, \"CAVE token\")."
    return s


def failure(e):
    """update() fields for an error: the full text, for the Copy button."""
    return dict(detail=str(e)[:20000])


def help_page():
    """end_user/guide.html with its screenshots inlined, so /help needs no other routes (or tokens on image URLs)."""
    import base64
    with open(os.path.join(GUIDE_DIR, "guide.html"), encoding="utf-8") as f: html = f.read()
    def inline(m):
        path = os.path.join(GUIDE_DIR, "screenshots", os.path.basename(m.group(1)))
        if not os.path.exists(path): return m.group(0)
        with open(path, "rb") as f: return 'src="data:image/png;base64,' + base64.b64encode(f.read()).decode() + '"'
    return re.sub(r'src="screenshots/([\w.-]+\.png)"', inline, html)


def update(job, **kw):
    if "message" in kw: kw.setdefault("detail", "")  # a new message replaces the old error detail
    with lock:
        job.update(kw); save()


def paths(root):
    out = os.path.join(HERE, "outputs", root); os.makedirs(out, exist_ok=True)
    return os.path.join(out, "preview_terminals.json"), os.path.join(out, "edit_log.csv")


def add(sample, root, rounds, parent, preview):
    """Record a job and queue it. A previewed job splits without asking if its preview passes."""
    with lock:
        j = dict(id=len(jobs), sample=sample, root=root, status="queued", message="", started=time.time(), rounds=rounds, parent=parent)
        jobs.append(j); save()
    if preview: pool.submit(run_preview, j)
    else: pool.submit(run_split, j, True)


def not_run(j):
    left = j.get("rounds", 1) - 1
    return f" {left} more round(s) not run on this branch." if left else ""


def run_preview(j):
    update(j, status="previewing", started=time.time())
    try:
        p = bs.preview(connect(j["sample"]), int(j["root"]), paths(j["root"])[0])
        msg = (f"Would give {p['source_l2']} / {p['sink_l2']} L2 ({p['both_l2']} on both sides, {p['neither_l2']} on neither). "
               f"Missing supervoxels {p['missing_sv']} of {p['total_sv']}, links between sides {p['links']}, {p['seconds']} s.")
        update(j, status="preview_passed" if p["passed"] else "preview_failed", message=msg)
    except Exception as e:
        update(j, status="failed", message=f"Preview failed, nothing was edited: {brief(e)}", **failure(e))
    else:
        if p["passed"]: run_split(j, False)  # a passed preview is the approval: the cut uses its exact terminals


def run_split(j, no_preview):
    update(j, status="splitting", started=time.time(), message="")
    try: cg = connect(j["sample"])  # a login or connection failure happens before any edit is sent
    except Exception as e: return update(j, status="failed", message=f"Nothing was edited, couldn't connect to CAVE: {brief(e)}", **failure(e))
    try:
        r = bs.execute(cg, int(j["root"]), *paths(j["root"]), no_preview)
    except bs.BeforeEdit as e:
        return update(j, status="failed", message=f"Nothing was edited: {brief(e)}", **failure(e))
    except Exception as e:  # the edit may have applied even though the request failed
        return update(j, status="unknown", message=f"The edit may still have applied: {brief(e)}. Wait a few minutes, then re-check.", **failure(e))
    sized = len(r["sizes"]) == len(r["new"])
    pieces = sorted(zip(r["new"], r["sizes"]), key=lambda x: -x[1]) if sized else [(x, None) for x in r["new"]]
    msg = f"Op {r['op']}, {r['seconds']} s: " + ", ".join(f"{x} ({n} L2)" if n is not None else str(x) for x, n in pieces) + "."
    if r["verify_error"]: msg += f" The edit applied, but its checks failed: {r['verify_error']}"
    clean = sized and r["separated"] is True and not r["verify_error"]  # only a verified cut is cut again
    if not clean: msg += not_run(j)
    update(j, status="not_separated" if r["separated"] is False else "split", message=msg)
    if clean and j.get("rounds", 1) > 1:
        for root, n in pieces[:2]:  # the two large pieces; fragments and too-small pieces stop here
            if n >= bs.MIN_L2: add(j["sample"], str(root), j["rounds"] - 1, j["id"], not no_preview)


def run_recheck(j):
    try:
        cg = connect(j["sample"])
        if cg.is_latest_roots([int(j["root"])])[0]:
            update(j, status="failed", message="The root hasn't changed. If the server is still working, the edit could still apply.")
        else:
            update(j, status="split", message="The edit applied. Latest roots: " + ", ".join(str(int(x)) for x in cg.get_latest_roots(int(j["root"]))) + "." + not_run(j))
    except Exception as e:
        update(j, status="unknown", message=f"Re-check failed: {brief(e)}", **failure(e))


def start(root, preview, rounds=1):
    """-> (http code, error message). Every rule is enforced here, not just in the page. Always uses the one
    sample chosen for this run (SAMPLE): there is no per-request sample, so a job can't land on the wrong one."""
    if not (isinstance(root, str) and root.isascii() and root.isdigit() and 0 < int(root) < 2 ** 64):
        return 400, "Enter one segment ID, as a number"
    if not isinstance(preview, bool): return 400, "Bad request"
    if type(rounds) is not int or not 1 <= rounds <= MAX_ROUNDS: return 400, f"Rounds must be a whole number from 1 to {MAX_ROUNDS}"
    root = str(int(root))
    with lock:
        for j in jobs:
            if j["root"] == root and j["status"] in RUNNING: return 409, "This segment is already running"
            if j["root"] == root and j["status"] == "unknown": return 409, "This segment's last edit has an unknown outcome. Re-check it first."
        add(SAMPLE, root, rounds, None, preview)
    return 200, ""


def act(job_id, action):
    with lock:
        j = next((x for x in jobs if x["id"] == job_id), None)
        if not j: return 404, "Unknown job"
        if action == "split" and j["status"] == "preview_passed":
            update(j, status="queued", started=time.time()); pool.submit(run_split, j, False)
        elif action == "recheck" and j["status"] == "unknown":
            pool.submit(run_recheck, j)
        else:
            return 409, f"Can't {action} a job in this state"
    return 200, ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass  # default logging would print the token

    def reply(self, code, body, ctype="application/json"):
        body = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer"); self.send_header("Cache-Control", "no-store"); self.end_headers()
        self.wfile.write(body)

    def authorised(self, given):
        if given and hmac.compare_digest(given.encode(), TOKEN.encode()): return True
        self.reply(403, {"error": "Missing or wrong token"}); return False

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            if self.authorised(parse_qs(url.query).get("token", [""])[0]): self.reply(200, PAGE, "text/html; charset=utf-8")
        elif url.path == "/help":
            if self.authorised(parse_qs(url.query).get("token", [""])[0]):
                try: self.reply(200, help_page(), "text/html; charset=utf-8")
                except OSError: self.reply(404, {"error": "The guide (end_user/guide.html) is missing"})
        elif url.path == "/api/state":
            if self.authorised(self.headers.get("X-Token", "")):
                with lock: self.reply(200, {"jobs": jobs, "sample": SAMPLE, "max_rounds": MAX_ROUNDS, "now": time.time()})
        else:
            self.reply(404, {"error": "Not found"})

    def do_POST(self):
        if not self.authorised(self.headers.get("X-Token", "")): return
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 10000: raise ValueError
            d = json.loads(self.rfile.read(n))
        except ValueError:
            return self.reply(400, {"error": "Bad request"})
        path = urlparse(self.path).path
        if path == "/api/start": code, err = start(d.get("root"), d.get("preview"), d.get("rounds", 1))
        elif path == "/api/act": code, err = act(d.get("id"), d.get("action"))
        else: code, err = 404, "Not found"
        self.reply(code, {"error": err})


PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Segment splitter</title><style>
*{box-sizing:border-box}body{font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;background:#f4f5f7;color:#1c2330}
main{max-width:1200px;margin:auto;padding:32px 24px}h1{font-size:22px;font-weight:600;margin:0}h2{font-size:15px;font-weight:600;margin:28px 0 10px}
.sub,.hint{color:#5b6472}.sub{margin:2px 0 20px}.hint{margin:12px 0 0;font-size:13px}
.card{background:#fff;border:1px solid #e1e4e9;border-radius:10px;box-shadow:0 1px 2px #1018280d}form.card{padding:18px 20px}.scroll{overflow-x:auto}
.fields{display:flex;gap:16px;align-items:flex-end;flex-wrap:wrap}label{display:grid;gap:4px;font-size:12px;font-weight:600;color:#5b6472}
label.check{display:flex;align-items:center;gap:6px;font-size:14px;font-weight:400;color:inherit;height:34px}
input:not([type=checkbox]),select{font:inherit;font-weight:400;color:inherit;height:34px;padding:0 10px;border:1px solid #cdd2da;border-radius:6px;background:#fff}#root{width:220px}#rounds{width:76px}
button{font:inherit;font-weight:500;white-space:nowrap;height:30px;padding:0 12px;border:1px solid #cdd2da;border-radius:6px;background:#fff;color:inherit;cursor:pointer}button:hover{background:#f1f3f5}
.primary{height:34px;padding:0 18px;background:#2b59c3;border-color:#2b59c3;color:#fff}.primary:hover{background:#234aa3}.danger{background:#b3261e;border-color:#b3261e;color:#fff}.danger:hover{background:#94201a}
table{border-collapse:collapse;width:100%}th{font-size:12px;font-weight:600;color:#5b6472;background:#fafbfc}th,td{border-bottom:1px solid #eceef1;padding:10px 14px;text-align:left;vertical-align:top}tr:last-child td{border-bottom:0}
.id{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;white-space:nowrap}.round{display:block;font:12px system-ui,sans-serif;color:#5b6472}.msg{color:#3d4654;font-size:13px;overflow-wrap:anywhere}.time{color:#5b6472;font-size:12px;white-space:nowrap}
.s{display:inline-block;padding:1px 9px;border-radius:999px;background:#eceef1;font-size:12px;font-weight:500;white-space:nowrap}.good{background:#dcf3e3;color:#17663a}.bad{background:#fbe2e0;color:#98211a}.busy{background:#fdf0c8;color:#7a5600}
.copy{height:22px;padding:0 8px;margin-left:8px;font-size:12px;color:#5b6472;vertical-align:baseline}.msg{user-select:text}
.top{display:flex;align-items:baseline;justify-content:space-between;gap:16px}.titlewrap{display:flex;align-items:center;gap:10px}
.helplink{color:#2b59c3;font-weight:500;text-decoration:none}.helplink:hover{text-decoration:underline}
.sample{display:inline-block;padding:2px 10px;border-radius:999px;background:#e4ebfb;color:#2b59c3;font-size:13px;font-weight:600;white-space:nowrap}
#err{color:#b3261e;margin-top:10px}#err:empty{display:none}td:first-child{white-space:nowrap}
</style></head><body><main>
<div class="top"><div class="titlewrap"><h1>Segment splitter</h1><span id="sampleBadge" class="sample"></span></div><a id="help" class="helplink" target="_blank" rel="noopener">How to use</a></div><p class="sub">Each split is a real chunkedgraph edit.</p>
<form id="f" class="card"><div class="fields">
<label>Segment <input id="root" required inputmode="numeric" autocomplete="off"></label>
<label>Rounds <input id="rounds" type="number" min="1" value="1" required></label>
<label class="check"><input id="preview" type="checkbox" checked> Preview first</label><button class="primary">Start</button></div>
<p id="hint" class="hint"></p><div id="err" role="alert"></div></form>
<h2>Progress</h2><div class="card scroll"><table id="jobs"></table></div>
<script>
const T=new URLSearchParams(location.search).get('token'),$=i=>document.getElementById(i);$('help').href='help?token='+encodeURIComponent(T);
const api=async(p,b)=>{const r=await fetch(p,{method:b?'POST':'GET',headers:{'X-Token':T},body:b&&JSON.stringify(b)});const j=await r.json();
 if(!r.ok)throw new Error(j.error||r.status);return j};
const LABEL={queued:'Queued',previewing:'Previewing',splitting:'Splitting',preview_passed:'Preview passed',preview_failed:'Preview failed',split:'Split',
 not_separated:'Split, sides not separated',failed:'Failed',unknown:'Outcome unknown'};
const CLS={preview_passed:'good',split:'good',preview_failed:'bad',failed:'bad',unknown:'bad',not_separated:'bad',queued:'busy',previewing:'busy',splitting:'busy'};
const el=(t,txt,c)=>{const e=document.createElement(t);if(txt!=null)e.textContent=txt;if(c)e.className=c;return e};
const safe=f=>async()=>{try{$('err').textContent='';await f();await draw()}catch(e){$('err').textContent=e.message}};
const btn=(txt,fn,c)=>{const b=el('button',txt,c);b.onclick=safe(fn);return b};
function copyText(text,b){const done=()=>{b.textContent='Copied';setTimeout(()=>b.textContent='Copy',1500)};
 if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(text).then(done,()=>fallback());fallback();
 function fallback(){const a=el('textarea');a.value=text;a.style.position='fixed';a.style.opacity='0';document.body.append(a);a.select();
  try{document.execCommand('copy');done()}catch(e){prompt('Copy with Ctrl+C / Cmd+C:',text)}a.remove()}}
const edit=(r,n)=>confirm(n>1?`Up to ${2**n-1} real edits over ${n} rounds, starting with ${r}. Later rounds start without asking. Continue?`:`Real edit on ${r}. It can't be cancelled. Continue?`);
function hint(){const n=+$('rounds').value||1;$('hint').textContent=(n>1?`Up to ${2**n-1} edits and ${2**n} large pieces. `:'')+($('preview').checked
 ?(n>1?'Each cut is previewed first and made without asking if its preview passes.':'The cut is made without asking if its preview passes.')
 :(n>1?'No previews: every cut is made without asking, after one confirmation.':'No preview: the cut is made after one confirmation.'))}
function row(j,now,depth){const r=el('tr'),seg=el('td',null,'id'),st=el('td'),a=el('td'),n=j.rounds||1;seg.style.paddingLeft=14+depth*20+'px';seg.append((depth?'└ ':'')+j.root);
 if(depth+n>1)seg.append(el('span',`Round ${depth+1} of ${depth+n}`,'round'));
 st.append(el('span',LABEL[j.status]||j.status,'s '+(CLS[j.status]||'')));
 if(CLS[j.status]=='busy')st.append(el('div',Math.round(now-j.started)+' s','time'));
 if(j.status=='preview_passed')a.append(btn('Split',()=>edit(j.root,n)?api('/api/act',{id:j.id,action:'split'}):0,'danger'));
 if(j.status=='unknown')a.append(btn('Re-check',()=>api('/api/act',{id:j.id,action:'recheck'})));
 const m=el('td',null,'msg');m.append(el('span',j.message));
 if(j.message){const b=el('button','Copy','copy');b.title='Copy the full result, including any raw server error';
  b.onclick=()=>copyText(`${j.sample} ${j.root} ${LABEL[j.status]||j.status}\n${j.message}`+(j.detail?`\n\nFull error:\n${j.detail}`:''),b);m.append(b)}
 r.append(el('td',j.sample),seg,st,m,a);return r}
const selecting=()=>{const s=getSelection();return s&&!s.isCollapsed&&$('jobs').contains(s.anchorNode)};
async function draw(){const d=await api('/api/state');if(selecting())return;/* a redraw would drop the text being selected */$('sampleBadge').textContent=d.sample;$('rounds').max=d.max_rounds;
 const t=$('jobs'),h=el('tr');for(const x of['Sample','Segment','Status','Result',''])h.append(el('th',x));t.replaceChildren(h);
 if(!d.jobs.length){const r=el('tr'),c=el('td','Nothing yet.','msg');c.colSpan=5;r.append(c);t.append(r)}
 const kids={};for(const j of d.jobs)(kids[j.parent]??=[]).push(j);
 const walk=(j,depth)=>{t.append(row(j,d.now,depth));(kids[j.id]||[]).forEach(c=>walk(c,depth+1))};
 d.jobs.filter(j=>j.parent==null).reverse().forEach(j=>walk(j,0))}
$('f').oninput=hint;hint();
$('f').onsubmit=e=>{e.preventDefault();safe(async()=>{const root=$('root').value.trim(),preview=$('preview').checked,rounds=+$('rounds').value;if(!preview&&!edit(root,rounds))return;
 await api('/api/start',{root,preview,rounds});$('root').value=''})()};
draw().catch(e=>$('err').textContent=e.message);setInterval(()=>draw().catch(e=>$('err').textContent='Lost contact with the server: '+e.message),3000);
</script></main></body></html>"""


def selftest():
    """Whole flow over real HTTP against a fake chunkedgraph. Makes no CAVE calls."""
    import tempfile, urllib.error, urllib.request
    bs.selftest()
    global STATE_PATH, connect, paths, SAMPLE
    d = tempfile.mkdtemp(); STATE_PATH = os.path.join(d, "jobs.json")
    cfg = os.path.join(d, "config.json")
    for bad in ("{", '{"samples": {}, "server_address": "https://x"}', '{"samples": {"A": "a"}, "server_address": "http://x"}'):
        with open(cfg, "w") as f: f.write(bad)
        try: load_config(cfg); raise AssertionError(f"bad config must be refused: {bad}")
        except SystemExit: pass
    with open(cfg, "w") as f: json.dump({"server_address": "https://x", "samples": {"NGC-11": "ngc11_datastack", "B": "b"}}, f)
    load_config(cfg); assert SERVER == "https://x" and SAMPLES == {"NGC-11": "ngc11_datastack", "B": "b"}

    assert pick_sample("B", ask=None) == "B", "a valid --sample must be used without asking"
    try: pick_sample("nope", ask=None); raise AssertionError("an unknown --sample must be refused")
    except SystemExit: pass
    answers = iter(["", "x", "2"])
    assert pick_sample(None, ask=lambda _: next(answers)) == "B", "two samples must prompt, retry on junk, and accept a number"
    assert pick_sample(None, ask=lambda _: "NGC-11") == "NGC-11", "the prompt must also accept a name directly"
    both = dict(SAMPLES); SAMPLES.clear(); SAMPLES["Solo"] = "solo_ds"
    assert pick_sample(None, ask=lambda _: (_ for _ in ()).throw(AssertionError("must not ask with only one sample"))) == "Solo"
    SAMPLES.clear(); SAMPLES.update(both)
    connect = lambda sample: "ok"  # verify_connection makes no CAVE call itself; this stands in for a working connect()
    assert verify_connection("NGC-11") is None, "a working connection must report no error"
    def fails(sample): raise RuntimeError("boom")
    connect = fails
    assert "boom" in verify_connection("NGC-11"), "a broken connection must report the error, not raise"
    SAMPLE = "NGC-11"

    class FakeCG:
        def is_latest_roots(self, r): return [False]
        def get_latest_roots(self, r): return [99]
    calls = []
    connect = lambda sample: (calls.append(("connect", sample)), FakeCG())[1]
    paths = lambda root: ("p", "l")
    bs.preview = lambda cg, r, p: (calls.append(("preview", r)), dict(passed=r != 3, legal=True, source_l2=1, sink_l2=1, both_l2=0, neither_l2=0, missing_sv=0, total_sv=9, links=0, seconds=0))[1]
    bs.execute = lambda cg, r, p, l, np: (calls.append(("execute", r, np)), dict(op=7, new=[r * 10, r * 10 + 1], sizes=[5, 50], separated=True, verify_error=None, seconds=0))[1]
    s = ThreadingHTTPServer(("127.0.0.1", 0), Handler); threading.Thread(target=s.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{s.server_address[1]}"

    def req(path, body=None, token=TOKEN):
        r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers={"X-Token": token})
        try:
            with urllib.request.urlopen(r) as x: return x.status, json.loads(x.read() or b"{}")
        except urllib.error.HTTPError as e: return e.code, json.loads(e.read())

    def wait(job_id, status):
        for _ in range(200):
            st = req("/api/state")[1]["jobs"][job_id]["status"]
            if st == status: return
            time.sleep(.02)
        raise AssertionError(f"job {job_id} never reached {status}, is {st}")

    begin = lambda root, preview: req("/api/start", dict(root=root, preview=preview))
    assert req("/api/state", token="wrong")[0] == 403 and req("/api/start", {}, token="")[0] == 403, "token must be required"
    with urllib.request.urlopen(f"{base}/?token={TOKEN}") as x: assert b"Segment splitter" in x.read()
    with urllib.request.urlopen(f"{base}/help?token={TOKEN}") as x: h = x.read().decode()
    assert "How to use the segment splitter" in h and 'src="screenshots/' not in h and "data:image/png;base64," in h, "help must inline its screenshots"
    try: urllib.request.urlopen(f"{base}/help?token=wrong"); raise AssertionError("help must need the token")
    except urllib.error.HTTPError as e: assert e.code == 403
    assert "//" not in PAGE.split("<script>")[1], "no // comments in the page script: one hid the rest of its line (the sample menu) once"
    assert req("/api/state")[1]["sample"] == "NGC-11", "the page must be told which one sample this run serves"
    assert begin("abc", True)[0] == 400 and begin("1 2", True)[0] == 400 and begin("1", "yes")[0] == 400, "bad input must be refused"
    assert not calls

    assert begin("1", True)[0] == 200; wait(0, "split")  # a passed preview splits without asking
    assert calls.index(("preview", 1)) < calls.index(("execute", 1, False)), "preview first, then the edit with its terminals"
    msg = req("/api/state")[1]["jobs"][0]["message"]
    assert msg.index("11 (50 L2)") < msg.index("10 (5 L2)"), msg
    assert req("/api/act", {"id": 0, "action": "split"})[0] == 409, "a finished job must not split again"
    assert begin("2", False)[0] == 200; wait(1, "split")
    assert ("execute", 2, True) in calls, "no-preview goes straight to the edit"
    assert begin("3", True)[0] == 200; wait(2, "preview_failed")
    assert req("/api/act", {"id": 2, "action": "split"})[0] == 409, "a failed preview offers no split"

    def before(*a): raise bs.BeforeEdit("too small")
    bs.execute = before
    assert begin("4", False)[0] == 200; wait(3, "failed")

    def boom(*a): raise RuntimeError("timeout")
    bs.execute = boom
    assert begin("5", False)[0] == 200; wait(4, "unknown")
    assert begin("5", False)[0] == 409, "unknown outcome must block another edit"
    assert req("/api/act", {"id": 4, "action": "recheck"})[0] == 200; wait(4, "split")
    assert "99" in req("/api/state")[1]["jobs"][4]["message"]

    def idle():
        for _ in range(500):
            if not any(j["status"] in RUNNING for j in req("/api/state")[1]["jobs"]): return time.sleep(.05)  # let a just-finished job add its children
            time.sleep(.02)
        raise AssertionError("jobs never finished")

    def tree(root, preview, rounds):
        n = len(jobs); assert req("/api/start", dict(root=root, preview=preview, rounds=rounds))[0] == 200
        for _ in range(3): idle()
        return jobs[n:]

    for bad in (0, MAX_ROUNDS + 1, "2", True, 1.5): assert req("/api/start", dict(root="6", preview=True, rounds=bad))[0] == 400, bad
    cuts = lambda fn: setattr(bs, "execute", lambda cg, r, p, l, np: (calls.append(("execute", r, np)), dict(op=7, verify_error=None, seconds=0, separated=True) | fn(r))[1])
    two = lambda r: dict(new=[r * 10, r * 10 + 1], sizes=[50, 60])
    cuts(two)

    t = tree("6", True, 2); assert t[0]["status"] == "split" and len(t) == 3, "the first cut splits unasked too"
    assert ("preview", 60) in calls and ("execute", 60, False) in calls and ("execute", 61, False) in calls, "children preview, then split unasked"
    assert not any(c[0] == "execute" and c[1] >= 600 for c in calls), "the last round must not cut again"
    assert [j["parent"] for j in jobs[-2:]] == [t[0]["id"]] * 2 and all(j["rounds"] == 1 for j in jobs[-2:])

    t = tree("7", False, 3); assert len(t) == 7 and all(j["status"] == "split" for j in t), "3 rounds = 7 cuts"
    assert ("execute", 700, True) in calls and ("preview", 70) not in calls, "no-preview trees never preview"

    cuts(lambda r: dict(new=[3, 30], sizes=[50, 60]) if r == 9 else two(r))  # the fake preview fails root 3
    tree("9", True, 2)
    assert ("execute", 3, False) not in calls and ("execute", 30, False) in calls, "a failed preview stops only its branch"

    cuts(lambda r: dict(new=[r * 10, r * 10 + 1, r * 10 + 2], sizes=[5, 50, 2]))
    assert [j["root"] for j in tree("11", False, 2)] == ["11", "111"], "fragments and too-small pieces are not cut"
    cuts(lambda r: dict(two(r), separated=False)); t = tree("12", False, 2)
    assert len(t) == 1 and "not run" in t[0]["message"], "a failed separation stops the branch"
    cuts(lambda r: dict(two(r), verify_error="boom")); assert len(tree("13", False, 2)) == 1, "an unverified cut is not cut again"

    jobs[0]["status"] = "splitting"; jobs[1]["status"] = "previewing"; save(); jobs.clear(); load()
    assert jobs[0]["status"] == "unknown" and jobs[1]["status"] == "failed", "a restart must flag interrupted jobs"
    e400 = RuntimeError("4 attempts, source+sink terminals 46+44; last error: 400 Client Error: BAD REQUEST for url: https://x/split_preview content: "
                        "b'{\\n \"code\": 400,\\n \"message\": \"[400]: Paths irreparably overlap.\",\\n \"timestamp\": \"t\"\\n}\\n'")
    assert brief(e400) == "4 attempts, source+sink terminals 46+44; last error: 400 Client Error: BAD REQUEST ([400]: Paths irreparably overlap.)", brief(e400)
    e500 = RuntimeError("500 Server Error: Invalid vertex index: 9939 for url: https://x content:b'{\\n \"message\": \"Invalid vertex index: 9939\", \"traceback\": [\"...\"]'")
    assert brief(e500) == "500 Server Error: Invalid vertex index: 9939", brief(e500)
    assert brief(RuntimeError("x" * 900)).endswith("(Copy for the full text)")
    bs.preview = lambda cg, r, p: (_ for _ in ()).throw(e500)
    assert begin("20", True)[0] == 200; jid = len(jobs) - 1; wait(jid, "failed")
    j = req("/api/state")[1]["jobs"][jid]; assert j["message"].endswith("9939") and "traceback" in j["detail"], j
    def no_login(sample): raise RuntimeError("401 Client Error: UNAUTHORIZED for url: https://x/info content: b'{\\n  \"error\": \"invalid_token\",\\n  \"message\": \"Unauthorized - Token is Invalid or Expired\"\\n}'")
    connect = no_login
    assert begin("21", False)[0] == 200; jid = len(jobs) - 1; wait(jid, "failed")
    j = req("/api/state")[1]["jobs"][jid]; assert j["message"].startswith("Nothing was edited") and "CAVE token" in j["message"], j
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="anything but localhost sends the token and edits unencrypted")
    ap.add_argument("--port", type=int, default=8765); ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--config", default=CONFIG_PATH, help="JSON file listing the samples and the CAVE server (default: config.json here)")
    ap.add_argument("--sample", help="which sample to serve, by its name in config.json. Skips the prompt; still checked against CAVE.")
    a = ap.parse_args()
    if a.selftest: selftest(); raise SystemExit
    load_config(a.config)
    try:
        SAMPLE = pick_sample(a.sample)
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\nStopped. Nothing was started.")
    print(f"Checking the CAVE connection for '{SAMPLE}' ({SAMPLES[SAMPLE]})...", flush=True)
    error = verify_connection(SAMPLE)
    if error:
        raise SystemExit(f"Can't connect to CAVE for '{SAMPLE}': {error}\n"
                          f"Run `python setup-google-credentials.py` to fix your CAVE token, then try again.")
    print("Connected.", flush=True)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True); load()
    s = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Sample: {SAMPLE}", flush=True)
    print(f"Open: http://{a.host}:{s.server_address[1]}/?token={TOKEN}", flush=True)
    s.serve_forever()
