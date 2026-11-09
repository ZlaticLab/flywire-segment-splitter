"""Cut one chunkedgraph root into two large pieces. See README.md."""
import csv, json, time
import numpy as np, networkx as nx
import scipy.sparse as sp
from scipy.sparse.csgraph import laplacian
from scipy.sparse.linalg import eigsh

MIN_SMALLER_SIDE, MAX_MISSING = 0.10, 0.01  # preview pass criteria, fractions of L2 nodes and supervoxels
MIN_L2 = 10  # too few nodes to choose terminals
BAD_TERMINAL, RETRIES = "Invalid vertex index", 3  # server error when a terminal supervoxel is missing from its cut graph
OVERLAP = "irreparably overlap"  # server error when paths joining one side's terminals cross the other side's paths
BITS, CHUNK_VOXELS, NM_PER_VOXEL = 7, 256, 8  # NGC defaults, used only when the chunkedgraph doesn't report its geometry


class BeforeEdit(Exception):
    """execute() failed before sending the edit, so nothing can have changed."""


def bisect(g):
    """Split connected graph g into two equal halves along the Fiedler vector."""
    nodes = list(g)
    L = laplacian(nx.to_scipy_sparse_array(g, nodelist=nodes, dtype=float))
    f = eigsh(sp.csr_matrix(L), k=2, sigma=-1e-3, which="LM")[1][:, 1]  # ponytail: shift-invert fine to ~1e5 nodes
    order = np.argsort(f); k = min(int(np.searchsorted(np.arange(1, len(nodes) + 1), len(nodes) / 2)) + 1, len(nodes) - 1)
    return [nodes[i] for i in order[:k]], [nodes[i] for i in order[k:]]


def chunk_xyz(ids, bits=BITS, layer_bits=8):
    """L2 node or supervoxel IDs -> chunk coordinates. ID layout: [layer | x | y | z | segment].
    bits varies by volume (NGC-13 uses 6): see geometry()."""
    ids = np.asarray(ids, dtype=np.uint64); m = np.uint64(2 ** bits - 1); top = 64 - layer_bits
    return np.stack([(ids >> np.uint64(top - (k + 1) * bits)) & m for k in range(3)], 1).astype(float)


def geometry(cg):
    """The volume's ID layout, chunk size, voxel size (nm), voxel offset and size, read from the chunkedgraph's segmentation_info.
    These differ between datastacks; wrong values put terminal points in the wrong place or outside the volume."""
    try:
        info = cg.segmentation_info; info = info() if callable(info) else info
        g, sc = info["graph"], info["scales"][0]; masks = g["spatial_bit_masks"]
        return dict(bits=int(masks.get("2", masks.get(2))), layer_bits=int(g.get("n_bits_for_layer_id", 8)),
                    chunk=[int(x) for x in g["chunk_size"][:3]], res=[float(x) for x in sc["resolution"][:3]],
                    offset=[int(x) for x in sc.get("voxel_offset", [0, 0, 0])[:3]], size=[int(x) for x in sc["size"][:3]], source="segmentation_info")
    except Exception as e:
        return dict(bits=BITS, layer_bits=8, chunk=[CHUNK_VOXELS] * 3, res=[float(NM_PER_VOXEL)] * 3, offset=[0, 0, 0], size=None,
                    source=f"NGC defaults ({type(e).__name__}: {e})")


def chunk_points(L, geo):
    """L2 node IDs -> their chunk centres in nm, clamped inside the volume."""
    c = chunk_xyz(L, geo["bits"], geo["layer_bits"]); ch, off = np.array(geo["chunk"]), np.array(geo["offset"])
    v = off + c * ch + ch // 2
    if geo["size"]: v = np.clip(v, off, off + np.array(geo["size"]) - 1)
    return (v * np.array(geo["res"])).tolist()


def ball(g, half, n):
    """Connected BFS ball of up to n nodes, grown from the deepest node of half's largest component."""
    h = g.subgraph(max(nx.connected_components(g.subgraph(half)), key=len))
    edge = [x for x in h if any(m not in h for m in g[x])] or list(h)[:1]
    d = nx.multi_source_dijkstra_path_length(h, edge); seed = max(d, key=d.get)
    return ([seed] + [v for _, v in nx.bfs_edges(h, seed)])[:n]


def spread(g, nodes, k, ok=None):
    """Farthest-point sample of k nodes by hops within nodes, picking only from ok nodes when the ball has any."""
    h = g.subgraph(nodes); cand = [n for n in nodes if ok is None or n in ok] or nodes
    pick = [cand[0]]; dist = nx.single_source_shortest_path_length(h, cand[0])
    for _ in range(min(k, len(cand)) - 1):
        i = max(cand, key=lambda n: dist.get(n, -1)); pick.append(i)
        di = nx.single_source_shortest_path_length(h, i); dist = {n: min(dist[n], di.get(n, dist[n])) for n in dist}
    return pick


def choose_terminals(g, xyz, k=40, frac=0.2, ok=None):
    """-> (source L2 nodes, sink L2 nodes, (n, m)). The first n sources and m sinks are the core terminals, drawn from two
    compact balls at opposite ends; the rest are outermost nodes, added to widen the server's cut box. xyz: node -> position.
    ok: preferred terminal nodes (L2 nodes with 2+ supervoxels, whose supervoxels all have edges inside the chunk)."""
    A, B = bisect(g); n = int(frac * len(g))
    ballA, ballB = ball(g, A, n), ball(g, B, n)
    S, T = spread(g, ballA, k, ok), spread(g, ballB, k, ok); core = (len(S), len(T))
    dA, dB = nx.multi_source_dijkstra_path_length(g, ballA), nx.multi_source_dijkstra_path_length(g, ballB)
    nodes = list(g); p = np.array([xyz[x] for x in nodes]); good = np.array([ok is None or x in ok for x in nodes])
    def extreme(ax, f):  # an outermost node on this axis, preferring an ok one at the same coordinate
        at = p[:, ax] == f(p[:, ax]); return nodes[int(np.flatnonzero(at & good)[0] if (at & good).any() else np.flatnonzero(at)[0])]
    for x in {extreme(ax, f) for ax in range(3) if np.ptp(p[:, ax]) for f in (np.min, np.max)}:  # flat axes bound nothing
        side = S if dA[x] <= dB[x] else T
        if x not in S and x not in T: side.append(x)
    return S, T, core


def l2_graph(cg, root):
    ids = [int(i) for i in cg.get_leaves(root, stop_layer=2)]
    g = nx.Graph(); g.add_nodes_from(ids); g.add_edges_from(map(tuple, np.asarray(cg.level2_chunk_graph(root)).tolist()))
    return ids, g


def require_latest(cg, root):
    if not cg.is_latest_roots([root])[0]:
        raise ValueError(f"not the latest root; latest: {', '.join(str(int(x)) for x in cg.get_latest_roots(root))}")


def terminals(cg, root, path):
    """Choose terminals and save them to path. Points are chunk centres in nm, which the server accepts as approximate."""
    ids, g = l2_graph(cg, root); geo = geometry(cg)
    if len(ids) < MIN_L2: raise ValueError(f"only {len(ids)} L2 nodes: too small to split (minimum {MIN_L2})")
    leaves = {int(k): [int(s) for s in v] for k, v in cg.get_leaves_many(ids, stop_layer=1).items()}
    S, T, (nS, nT) = choose_terminals(g, dict(zip(ids, chunk_points(ids, geo))), ok={x for x in ids if len(leaves[x]) > 1})
    t = dict(root=root, geometry=geo, source_points=chunk_points(S, geo), sink_points=chunk_points(T, geo), source_supervoxels=[leaves[x][0] for x in S],
             sink_supervoxels=[leaves[x][0] for x in T], source_core=nS, sink_core=nT, passed=False)
    with open(path, "w") as f: json.dump(t, f)
    return t, ids, g, leaves


def core_terminals(t):
    """Only the core terminals, dropping the outermost nodes whose paths to their side are long and may cross the other."""
    return {f"{s}_{k}": t[f"{s}_{k}"][:t[f"{s}_core"]] for s in ("source", "sink") for k in ("points", "supervoxels")}


def half_terminals(t, rng):
    """A random half of each side's terminals (points and supervoxels kept paired), at least one per side."""
    out = {}
    for side in ("source", "sink"):
        n = len(t[f"{side}_points"]); keep = sorted(rng.choice(n, max(1, n // 2), replace=False))
        out[f"{side}_points"] = [t[f"{side}_points"][i] for i in keep]
        out[f"{side}_supervoxels"] = [t[f"{side}_supervoxels"][i] for i in keep]
    return out


def preview(cg, root, path):
    """Read-only server preview. Saves the terminals, and marks them passed only if the cut looks real."""
    require_latest(cg, root)
    t, ids, g, leaves = terminals(cg, root, path)
    sv2l2 = {s: l2 for l2, l in leaves.items() for s in l}; t0 = time.time(); rng = np.random.default_rng(0)
    tried = []  # terminal counts per attempt, reported if every attempt fails
    for attempt in range(RETRIES + 1):
        tried.append(f"{len(t['source_points'])}+{len(t['sink_points'])}")
        try:
            src, snk, ok, other = cg.preview_split(t["source_points"], t["sink_points"], root, source_supervoxels=t["source_supervoxels"],
                                                   sink_supervoxels=t["sink_supervoxels"], return_additional_ccs=True)
            break
        except Exception as e:  # retry fewer terminals: core only on a path overlap, then random halves of what is left
            if not (BAD_TERMINAL in str(e) or OVERLAP in str(e)): raise
            if attempt == RETRIES: raise RuntimeError(f"{len(tried)} attempts, source+sink terminals {', '.join(tried)}; last error: {e}") from e
            if not attempt: base = dict(t)
            extra = len(base["source_points"]) > base["source_core"] or len(base["sink_points"]) > base["sink_core"]
            if OVERLAP in str(e) and extra: base = dict(base, **core_terminals(base)); t = dict(base)
            else: t = dict(base, **half_terminals(base, rng))
    src, snk = set(map(int, src)), set(map(int, snk)); oth = {int(s) for c in other for s in c}
    missing = set(sv2l2) - src - snk - oth
    a, b = {sv2l2[s] for s in src}, {sv2l2[s] for s in snk}; both = a & b; neither = set(ids) - a - b
    links = [c for c in nx.connected_components(g.subgraph(set(ids) - both)) if c & (a - b) and c & (b - a)]
    right = all(s in src for s in t["source_supervoxels"]) and all(s in snk for s in t["sink_supervoxels"])
    t["passed"] = bool(ok and not links and right and len(missing) <= MAX_MISSING * len(sv2l2)
                       and min(len(a), len(b)) >= MIN_SMALLER_SIDE * len(ids))
    with open(path, "w") as f: json.dump(t, f)
    return dict(passed=t["passed"], legal=bool(ok), source_l2=len(a - b), sink_l2=len(b - a), both_l2=len(both), neither_l2=len(neither),
                missing_sv=len(missing), total_sv=len(sv2l2), links=len(links), seconds=round(time.time() - t0))


def execute(cg, root, path, log, no_preview=False):
    """REAL EDIT, no retries. Uses the terminals of a passed preview, or fresh ones if no_preview."""
    try:
        require_latest(cg, root)
        if no_preview: t = terminals(cg, root, path)[0]
        else:
            with open(path) as f: t = json.load(f)
        assert t["root"] == root and (no_preview or t["passed"]), "no passing preview saved for this root"
        n_sv = len(cg.get_leaves(root)); t0 = time.time()
    except Exception as e:
        raise BeforeEdit(str(e)) from e
    op, new = cg.execute_split(t["source_points"], t["sink_points"], root,
                               source_supervoxels=t["source_supervoxels"], sink_supervoxels=t["sink_supervoxels"])
    new = [int(r) for r in new]
    sizes, separated, verify_error = [], True, None
    try:  # the log row must survive a failed verification read, and the edit has already applied
        time.sleep(2); total = 0
        src, snk = set(t["source_supervoxels"]), set(t["sink_supervoxels"])
        for r in new:
            ids, g = l2_graph(cg, r); svs = set(cg.get_leaves(r).tolist()); total += len(svs); sizes.append(len(ids))
            separated = separated and not (svs & src and svs & snk)
            if not nx.is_connected(g): raise ValueError(f"piece {r} is disconnected")
        if total != n_sv: raise ValueError(f"supervoxels changed: {n_sv} before, {total} after")
    except Exception as e:
        verify_error = str(e)[:500]; separated = separated if len(sizes) == len(new) else None  # None = not fully checked
    finally:
        res = np.array(t.get("geometry", {}).get("res", [NM_PER_VOXEL] * 3))
        vox = lambda P, V: [(int(s), (np.array(p) / res).astype(int).tolist()) for s, p in zip(V, P)]
        with open(log, "a", newline="") as f:
            csv.writer(f).writerow([op, root, *new, *sizes, str(vox(t["source_points"], t["source_supervoxels"])), str(vox(t["sink_points"], t["sink_supervoxels"]))])
    return dict(op=int(op), new=new, sizes=sizes, separated=separated, verify_error=verify_error, seconds=round(time.time() - t0))


def selftest():
    """Terminals, preview and execute on a two-lobe graph with a fake chunkedgraph. Makes no CAVE calls."""
    import os, tempfile
    node = lambda x, y: (2 << 56) | (x << 49) | (y << 42) | 1
    g = nx.grid_2d_graph(10, 10); lobe2 = nx.grid_2d_graph(10, 10)
    full = nx.Graph(); full.add_edges_from((node(*u), node(*v)) for u, v in g.edges)
    full.add_edges_from((node(u[0] + 20, u[1]), node(v[0] + 20, v[1])) for u, v in lobe2.edges)
    X = node(15, 5); full.add_edge(node(9, 5), X); full.add_edge(X, node(20, 5))  # the cut passes through X: one SV per side
    left = [n for n in full if chunk_xyz([n])[0, 0] < 15]; right = [n for n in full if chunk_xyz([n])[0, 0] > 15]
    leaves = {n: [n + 1] for n in full}; leaves[X] = [X + 1, X + 2]
    left_sv, right_sv = [n + 1 for n in left] + [X + 1], [n + 1 for n in right] + [X + 2]
    ROOT, A, B = 1, 2, 3; parts = {ROOT: list(full), A: left + [X], B: right + [X], 4: left[:3]}
    svs = {ROOT: left_sv + right_sv, A: left_sv, B: right_sv, 4: [n + 1 for n in left[:3]]}

    class FakeCG:
        latest = True
        def is_latest_roots(self, r): return [self.latest]
        def get_latest_roots(self, r): return [A, B]
        def get_leaves(self, r, stop_layer=1): return np.array(parts[r] if stop_layer == 2 else svs[r])
        def level2_chunk_graph(self, r): return [list(e) for e in full.subgraph(parts[r]).edges]
        def get_leaves_many(self, ids, stop_layer=1): return {n: leaves[n] for n in ids}
        fail, msg = 0, BAD_TERMINAL
        def preview_split(self, sp_, tp, r, source_supervoxels, sink_supervoxels, return_additional_ccs):
            self.sent = len(sp_) + len(tp)
            if self.fail: self.fail -= 1; raise RuntimeError(f"Server Error: Paths {self.msg} labels" if self.msg == OVERLAP else f"{self.msg}: 9939")
            on_left = bool(set(source_supervoxels) & set(left_sv))
            return (left_sv, right_sv, True, []) if on_left else (right_sv, left_sv, True, [])
        def execute_split(self, sp_, tp, r, source_supervoxels, sink_supervoxels): return 7, [A, B]

    class Info: segmentation_info = {"graph": {"chunk_size": [128, 128, 64], "n_bits_for_layer_id": 8, "spatial_bit_masks": {"2": 9}},
                                     "scales": [{"resolution": [4, 4, 40], "voxel_offset": [100, 0, 10], "size": [1000, 1000, 100]}]}
    geo = geometry(Info()); assert geo["bits"] == 9 and geo["res"] == [4, 4, 40] and geo["source"] == "segmentation_info", geo
    n9 = (2 << 56) | (3 << 47) | (1 << 38) | (5 << 29) | 1  # chunk (3, 1, 5) with 9-bit coordinates
    assert chunk_points([n9], geo) == [[(100 + 3 * 128 + 64) * 4, (128 + 64) * 4, (10 + 99) * 40]], "z clamps inside the volume"
    assert geometry(object())["source"].startswith("NGC defaults")
    d = tempfile.mkdtemp(); path, log = os.path.join(d, "t.json"), os.path.join(d, "log.csv"); cg = FakeCG()
    S, T, core = choose_terminals(full, dict(zip(full, chunk_xyz(list(full)).tolist())), k=5)
    assert core == (5, 5) and len(S) + len(T) > 10, "outermost nodes must come after the core terminals"
    assert not set(S) & set(T) and {n in left for n in S} != {n in left for n in T}, "sides should be the two lobes"
    p = chunk_xyz(S + T); q = chunk_xyz(list(full))
    assert (p.min(0) == q.min(0)).all() and (p.max(0) == q.max(0)).all(), "terminal box must cover the graph"
    try: execute(cg, ROOT, path, log); raise AssertionError("no preview saved")
    except BeforeEdit: pass
    r = preview(cg, ROOT, path); assert r["passed"] and r["missing_sv"] == 0 and r["links"] == 0, r
    r = execute(cg, ROOT, path, log); assert r["separated"] and r["sizes"] == [101, 101] and r["verify_error"] is None, r
    assert open(log).read().startswith("7,1,2,3,101,101,"), "edit must be logged"
    r = execute(cg, ROOT, path, log, no_preview=True); assert r["op"] == 7
    try: preview(cg, 4, path); raise AssertionError("small root must be refused")
    except ValueError as e: assert "too small" in str(e)
    n0 = len(json.load(open(path))["source_points"]); cg.fail = RETRIES
    r = preview(cg, ROOT, path); t = json.load(open(path))
    assert r["passed"] and 1 <= len(t["source_points"]) < n0 and len(t["source_points"]) == len(t["source_supervoxels"]), (r, t)
    cg.fail, cg.msg = 1, OVERLAP; preview(cg, ROOT, path); t = json.load(open(path))
    assert cg.sent == t["source_core"] + t["sink_core"] == len(t["source_points"]) + len(t["sink_points"]), "overlap must drop to core"
    cg.fail = 2; preview(cg, ROOT, path); assert cg.sent < len(t["source_points"]) + len(t["sink_points"]), "then halve the core"
    cg.fail, cg.msg = RETRIES + 1, BAD_TERMINAL
    try: preview(cg, ROOT, path); raise AssertionError("must give up after RETRIES")
    except RuntimeError as e: assert BAD_TERMINAL in str(e) and str(e).startswith(f"{RETRIES + 1} attempts"), e
    cg.latest = False
    try: execute(cg, ROOT, path, log); raise AssertionError("stale root must be refused")
    except BeforeEdit as e: assert "latest" in str(e)
