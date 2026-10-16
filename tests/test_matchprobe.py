"""Headless tests for Match Probe.

    Nuke --nukex -i -t tests/test_matchprobe.py <folder with matchProbe.py>

Runs in terminal mode (needs an interactive licence: -i).
"""
import math
import os
import sys

MOD_DIR = sys.argv[1]
sys.path.insert(0, MOD_DIR)
import nuke  # noqa: E402
import colorMath as cm  # noqa: E402
import matchProbe as mp  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def expr_node(src, exprs, chans=("rgba.red", "rgba.green", "rgba.blue", "rgba.alpha")):
    e = nuke.nodes.Expression(inputs=[src])
    for i, ch in enumerate(chans):
        e["channel%d" % i].fromScript(ch)
        e["expr%d" % i].setValue(exprs[i])
    return e


def const(rgba, fmt="mptest"):
    c = nuke.nodes.Constant()
    c["color"].setValue(list(rgba))
    c["format"].setValue(fmt)
    return c


root = nuke.root()
root["colorManagement"].setValue("OCIO")
# ACES 1.2 config bundled with this Nuke (Nuke 14/15). Newer Nukes may not ship
# it: then the OCIO swatch checks fall back to Match Probe's approximation.
ACES12 = os.path.join(os.path.dirname(nuke.EXE_PATH), "plugins", "OCIOConfigs",
                      "configs", "aces_1.2", "config.ocio").replace("\\", "/")
HAVE_ACES12 = os.path.isfile(ACES12)
if HAVE_ACES12:
    root["OCIO_config"].setValue("custom")
    root["customOCIOConfigPath"].setValue(ACES12)
root["workingSpaceLUT"].setValue("scene_linear")
nuke.addFormat("200 100 1 mptest")
root["format"].setValue("mptest")
base = const([0, 0, 0, 1])
RGBA = ["rgba.red", "rgba.green", "rgba.blue", "rgba.alpha"]

print("== averages ==")
img = expr_node(base, ["x*0.005", "y*0.01", "fmod(x*3+y*5,11)*0.02", "0.5+x*0.001"])


def ref_avg(box):
    px = [(x * 0.005, y * 0.01, math.fmod(x * 3 + y * 5, 11) * 0.02, 0.5 + x * 0.001)
          for x in range(box[0], box[2]) for y in range(box[1], box[3])]
    return [sum(p[k] for p in px) / len(px) for k in range(4)]


box = [20, 10, 60, 40]
ref = ref_avg(box)
ra = mp.region_avg(img, box, 1, RGBA)
check(ra["exact"] and all(abs(a - b) < 1e-6 for a, b in zip(ra["avg"], ref)),
      "live average, every pixel %s" % [round(v, 6) for v in ra["avg"]])
ea = mp.exact_avg(img, box, 1, RGBA)
check(all(abs(a - b) < 1e-5 for a, b in zip(ea, ref)), "exact (CurveTool) average incl. alpha")
bb = [0, 0, 200, 100]
big = mp.region_avg(img, bb, 1, RGBA, grid=32)
check(not big["exact"] and all(abs(a - b) < 0.01 for a, b in zip(big["avg"], ref_avg(bb))),
      "strided grid close to exact on a large box")

print("\n== layers ==")
nuke.Layer("diffuse", ["diffuse.red", "diffuse.green", "diffuse.blue", "diffuse.alpha"])
lay = expr_node(img, ["r*2", "g*2", "b*2", "a"], ["diffuse.red", "diffuse.green",
                                                  "diffuse.blue", "rgba.alpha"])
one = const([0, 0, 0, 1])
lay = nuke.nodes.Copy(inputs=[lay, one])
lay["from0"].fromScript("rgba.alpha")
lay["to0"].fromScript("diffuse.alpha")
check("diffuse" in mp.layers_of(lay), "layers_of lists 'diffuse'")
ch = mp.layer_channels(lay, "diffuse")
check(ch == ["diffuse.red", "diffuse.green", "diffuse.blue", "diffuse.alpha"], "layer_channels %s" % ch)
la = mp.region_avg(lay, box, 1, ch)
le = mp.exact_avg(lay, box, 1, ch)
check(all(abs(la["avg"][k] - 2 * ref[k]) < 1e-6 for k in range(3)), "live average of a layer")
check(all(abs(le[k] - 2 * ref[k]) < 1e-5 for k in range(3)) and abs(le[3] - 1) < 1e-6,
      "exact average of a layer %s" % [round(v, 5) for v in le])
noalpha = mp.layer_channels(const([1, 1, 1, 1]), "rgba")
check(len(noalpha) == 4, "rgba channels resolved %s" % noalpha)

print("\n== hsvl = Pixel Analyzer (values from the user's screenshot) ==")
h = mp.hsvl([0.04086406, 0.02271588, 0.01230701])
check(abs(h[0] - 22) < 0.5 and abs(h[1] - 0.69883054) < 1e-6 and abs(h[2] - 0.04086406) < 1e-9
      and abs(h[3] - 0.02582189) < 1e-8, "hsvl %s" % [round(v, 8) for v in h])
for rgb, hue in (([1, 0, 0], 0), ([1, 1, 0], 60), ([0, 1, 0], 120), ([0, 0, 1], 240), ([1, 0, 1], 300)):
    check(abs(mp.hsvl(rgb)[0] - hue) < 1e-9, "hue of %s = %d" % (rgb, hue))

print("\n== compare / delta / range ==")
T = [0.30, 0.19, 0.13, 1.0]
S = [0.33, 0.19, 0.117, 1.0]
rows = mp.compare(T, S)
t, s, d = rows["rgba"]
check(abs(d[0][0] - 0.03) < 1e-12 and abs(d[0][1] - 10.0) < 1e-9 and abs(d[2][1] + 10.0) < 1e-9,
      "rgba delta abs + pct (r +0.03 = +10%%, b -10%%)")
d_h = mp.delta([350, 0.5, 1, 1], [10, 0.5, 1, 1], hue_index=0)
check(abs(d_h[0][0] - 20) < 1e-9 and d_h[0][1] is None, "hue delta wraps 350 -> 10 = +20, no %")
r8 = mp.compare(T, S, scale=255.0)
check(abs(r8["rgba"][0][0] - 0.30 * 255) < 1e-9 and abs(r8["hsvl"][0][0] - rows["hsvl"][0][0]) < 1e-9
      and abs(r8["hsvl"][0][1] - rows["hsvl"][0][1]) < 1e-9
      and abs(r8["hsvl"][0][2] - rows["hsvl"][0][2] * 255) < 1e-9
      and r8["oklch"] == rows["oklch"], "8-bit scales rgba, v, l only")
ok = mp.oklch(T)
check(abs(ok[0] - cm.to_oklab(T[:3])[0]) < 1e-12 and 0 <= ok[2] < 360, "oklch row %s" % [round(v, 4) for v in ok])

print("\n== make_grade ==")
Sv = [0.25, 0.21, 0.11]
src = const(Sv + [1])
down = nuke.nodes.Blur(inputs=[src])
g = mp.make_grade(src, mp.suggest(T, Sv), target_label="plate")
check(down.input(0) is g, "downstream reconnected to the Grade")
after = mp.exact_avg(g, box, 1, RGBA)
check(all(abs(after[k] - T[k]) < 1e-6 for k in range(3)), "after Make Grade source avg == target %s" % after[:3])

print("\n== target persistence ==")
tgt = {"node": "Read1", "box": box, "frame": 1, "layer": "rgba", "avg": ea}
mp.save_target(tgt)
check(mp.load_target() == tgt, "target saved on Root")
mp.save_target(None)
check(mp.load_target() is None, "clear target")

print("\n== swatch through viewer OCIO ==")
v = nuke.nodes.Viewer()
v["viewerProcess"].setValue("sRGB (ACES)")
proc = mp._display_processor(v)
if HAVE_ACES12:
    check(proc is not None, "OCIO display processor for %r" % v["viewerProcess"].value())
else:
    print("  skip OCIO processor check (no bundled ACES 1.2 config in this Nuke)")
grey = mp.display_rgb([0.18, 0.18, 0.18], v)
check(grey[0] == grey[1] == grey[2] and 60 < grey[0] < 160, "mid grey -> neutral %s" % grey)
warm = mp.display_rgb([0.04086406, 0.02271588, 0.01230701], v)
check(warm[0] > warm[1] > warm[2], "screenshot average -> warm brown swatch %s" % warm)

print("\n== vectorscope ==")
cb, cr = mp.cbcr([1, 0, 0])
check(abs(cb + 0.114572) < 1e-5 and abs(cr - 0.5) < 1e-9, "Rec.709 Cb/Cr of red (%.4f, %.4f)" % (cb, cr))
check(mp.cbcr([0.5, 0.5, 0.5]) == (0.0, 0.0), "grey sits in the centre")
ang = {n: math.degrees(math.atan2(mp.cbcr(c)[1], mp.cbcr(c)[0])) % 360 for n, c in mp.SCOPE_TARGETS}
check(abs(ang["R"] - 102.9) < 0.1 and abs(ang["B"] - 354.8) < 0.1 and abs(ang["G"] - 229.7) < 0.1,
      "target angles R %.1f B %.1f G %.1f (Rec.709 HD layout)" % (ang["R"], ang["B"], ang["G"]))
pts = [[0.04, 0.02, 0.01], [0.2, 0.1, 0.05], [0.18, 0.18, 0.18]]
batch = mp.display_floats(pts, v)
single = [mp.display_floats([q], v)[0] for q in pts]
check(all(abs(a - b) < 1e-6 for x, y in zip(batch, single) for a, b in zip(x, y)),
      "batched OCIO display transform == one-by-one")
kp = mp.region_avg(img, box, 1, RGBA, keep=True)
check(len(kp["points"]) == 40 * 30 and len(kp["points"][0]) == 3, "keep=True returns every sampled pixel")
check(mp.auto_zoom([0.01, 0.02, 0.03]) == 8.0 and mp.auto_zoom([0.3, 0.4]) == 1.0 and mp.auto_zoom([]) == 1.0,
      "auto zoom picks 8x for a tight cluster, 1x for a wide one")
check(len(mp.thin(list(range(10000)))) == mp.SCOPE_POINTS, "dot count capped")

print("\n== Make Grade: match hue / saturation / lightness (OKLab LCh node) ==")


def lch_of(rgb):
    L, a, b = cm.to_oklab(rgb[:3])
    return L, math.hypot(a, b) / L, math.degrees(math.atan2(b, a)) % 360


def hdiff(a, b):
    return (a - b + 180) % 360 - 180


Tm, Sm = [0.30, 0.19, 0.13], [0.22, 0.20, 0.08]
tL, tS, tH = lch_of(Tm)
sL, sS, sH = lch_of(Sm)
cases = {"all": (True, True, True), "hue": (True, False, False),
         "sat": (False, True, False), "light": (False, False, True)}
for name, (fh, fs, fl) in cases.items():
    srcn = const(Sm + [1])
    par, notes = mp.match_params(Tm, Sm, 0, fh, fs, fl)
    nd = mp.make_lch(srcn, par, 0, "plate")
    o = mp.exact_avg(nd, box, 1, RGBA)
    oL, oS, oH = lch_of(o)
    want = (tL if fl else sL, tS if fs else sS, tH if fh else sH)
    ok = abs(oL - want[0]) < 1e-5 and abs(oS - want[1]) < 1e-5 and abs(hdiff(oH, want[2])) < 1e-3
    check(ok, "%-5s -> L %.5f (want %.5f)  sat %.5f (want %.5f)  hue %.3f (want %.3f)  params %s" % (
        name, oL, want[0], oS, want[1], oH, want[2], {k: round(v, 4) for k, v in par.items()}))
    check(abs(o[3] - 1.0) < 1e-6, "%s: alpha untouched" % name)

srcn = const(Sm + [1])
nd = mp.make_lch(srcn, {"hue_rotate": 0.0, "saturation": 1.0, "lightness": 1.0}, 0)
o = mp.exact_avg(nd, box, 1, RGBA)
check(all(abs(o[k] - Sm[k]) < 1e-6 for k in range(3)), "neutral knobs = identity %s" % [round(v, 7) for v in o[:3]])
greyn = const([0.18, 0.18, 0.18, 1])
nd = mp.make_lch(greyn, {"hue_rotate": 40.0, "saturation": 1.7, "lightness": 1.0}, 0)
o = mp.exact_avg(nd, box, 1, RGBA)
check(all(abs(v - 0.18) < 1e-6 for v in o[:3]), "grey stays grey under hue/sat changes")
nd = mp.make_lch(greyn, {"hue_rotate": 0.0, "saturation": 1.0, "lightness": 2 ** (1 / 3.0)}, 0)
o = mp.exact_avg(nd, box, 1, RGBA)
check(all(abs(v - 0.36) < 1e-5 for v in o[:3]), "lightness x 2^(1/3) = +1 stop (%.5f)" % o[0])
_, notes = mp.match_params(Tm, [0.2, 0.2, 0.2], 0, True, True, False)
check(len(notes) == 2, "grey source: hue + saturation skipped with notes %s" % notes)

srcn = const(Sm + [1])
downn = nuke.nodes.Blur(inputs=[srcn])
mk = expr_node(base, ["0", "0", "0", "x < 100"])
par, _ = mp.match_params(Tm, Sm, 0)
nd = mp.make_lch(srcn, par, 0, "plate")
check(downn.input(0) is nd, "downstream node reconnected to the LCh node")
nd.setInput(1, mk)
left, right = mp.exact_avg(nd, [0, 0, 100, 100], 1, RGBA), mp.exact_avg(nd, [100, 0, 200, 100], 1, RGBA)
check(all(abs(left[k] - Tm[k]) < 1e-5 for k in range(3)) and all(abs(right[k] - Sm[k]) < 1e-6 for k in range(3)),
      "mask: matched inside, untouched outside")
nd.setInput(1, None)
nd["mix"].setValue(0.5)
o = mp.exact_avg(nd, box, 1, RGBA)
check(all(abs(o[k] - 0.5 * (Tm[k] + Sm[k])) < 1e-5 for k in range(3)), "mix 0.5 = halfway")
nd["mix"].setValue(1.0)
nd["working_space"].setValue(2)
o = mp.exact_avg(nd, box, 1, RGBA)
p709, _ = mp.match_params(Tm, Sm, 2)
for k_, v_ in p709.items():
    nd[k_].setValue(v_)
o = mp.exact_avg(nd, box, 1, RGBA)
check(all(abs(o[k] - Tm[k]) < 1e-5 for k in range(3)), "Linear Rec.709 working space matches too")

print("\nRESULT:", "ALL PASS" if not FAILS else "%d FAILURES" % len(FAILS))
for f in FAILS:
    print("  -", f)
