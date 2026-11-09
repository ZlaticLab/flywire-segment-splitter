"""READ-ONLY: try several terminal layouts on one root with preview_split and report which the server accepts. Never edits.
Usage: python diagnose.py <sample> <root> [--config config.json]"""
import argparse, json, time
import networkx as nx
import app, splitter as bs

ap = argparse.ArgumentParser(); ap.add_argument("sample"); ap.add_argument("root", type=int)
ap.add_argument("--config", default=app.CONFIG_PATH); a = ap.parse_args()
app.load_config(a.config); cg = app.connect(a.sample); root = a.root
bs.require_latest(cg, root)

geo = bs.geometry(cg)
old = dict(bits=bs.BITS, layer_bits=8, chunk=[bs.CHUNK_VOXELS] * 3, res=[float(bs.NM_PER_VOXEL)] * 3, offset=[0, 0, 0], size=None)
print("geometry:", json.dumps(geo))
try:
    info = cg.segmentation_info; info = info() if callable(info) else info
    print("segmentation_info graph:", json.dumps(info.get("graph"))[:600])
    print("segmentation_info scale 0:", json.dumps({k: v for k, v in info["scales"][0].items() if k in ("resolution", "voxel_offset", "size", "chunk_sizes")}))
except Exception as e:
    print("segmentation_info unavailable:", e)

ids, g = bs.l2_graph(cg, root)
leaves = {int(k): [int(s) for s in v] for k, v in cg.get_leaves_many(ids, stop_layer=1).items()}
multi = {x for x in ids if len(leaves[x]) > 1}
print(f"root {root}: {len(ids)} L2 nodes, {g.number_of_edges()} L2 edges, {len(multi)} with 2+ supervoxels, "
      f"{sum(map(len, leaves.values()))} supervoxels, {nx.number_connected_components(g)} L2 components")
P = bs.chunk_points(ids, geo); lo, hi = [min(p[k] for p in P) for k in range(3)], [max(p[k] for p in P) for k in range(3)]
print(f"L2 chunk centres span (nm): {[round(x) for x in lo]} to {[round(x) for x in hi]}")
print(f"first L2 node {ids[0]}: old point {[round(x) for x in bs.chunk_points([ids[0]], old)[0]]} nm, new point {[round(x) for x in P[0]]} nm\n")


def run(name, S, T, gm=geo, with_sv=True):
    t0 = time.time()
    try:
        src, snk, ok, other = cg.preview_split(bs.chunk_points(S, gm), bs.chunk_points(T, gm), root, return_additional_ccs=True,
                                               **(dict(source_supervoxels=[leaves[x][0] for x in S], sink_supervoxels=[leaves[x][0] for x in T]) if with_sv else {}))
        res = f"OK   legal={ok} source_sv={len(src)} sink_sv={len(snk)} other_ccs={len(other)}"
    except Exception as e:
        msg = str(e); i = msg.find('"message"'); res = "FAIL " + (msg[i:i + 160] if i >= 0 else msg[:160]).replace("\n", " ")
    print(f"{name:<34} {len(S):>3}+{len(T):<3} {time.time() - t0:5.0f}s  {res}", flush=True)


xyz = dict(zip(ids, P))
S3, T3, c3 = bs.choose_terminals(g, xyz, k=3, ok=multi)
run("OLD geometry, 1 per side, no sv", S3[:1], T3[:1], gm=old, with_sv=False)
run("1 per side, no supervoxels", S3[:1], T3[:1], with_sv=False)
run("1 per side", S3[:1], T3[:1])
run("core, 3 per side", S3[:c3[0]], T3[:c3[1]])
S10, T10, c10 = bs.choose_terminals(g, xyz, k=10, ok=multi); run("core, 10 per side", S10[:c10[0]], T10[:c10[1]])
S, T, (nS, nT) = bs.choose_terminals(g, xyz, ok=multi)
run("core only", S[:nS], T[:nT])
run("current (core + outermost)", S, T)
