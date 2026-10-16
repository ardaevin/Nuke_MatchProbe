"""Match Probe - Pixel-Analyzer-style target vs live source comparison.

  1. Ctrl+Shift+drag a region in the Viewer, press "Lock target"
     (the exact average is frozen and saved with the script).
  2. Ctrl+Shift+drag the source region. Its average updates live while you
     change colours upstream.
  3. Read target / source / difference for rgba, Nuke-style hsvl and
     perceptual OKLab LCh; "Make Grade" inserts the multiply that matches
     the source average to the target.

Sampling follows the Pixel Analyzer: a Viewer (Active Viewer or a named
one) and a layer (current layer or any layer of the viewed node). Range is
native floating point by default (8/10/16-bit scaling optional).

Engine functions (region_avg, exact_avg, hsvl, oklch, compare, suggest,
make_grade) work without the panel; the headless tests use them.
"""
import base64
import json
import math
import os
import re
import time
import zlib

import nuke

import colorMath as cm

ROOT_KNOB = "matchprobe_target_b64"
# Old storage: raw JSON in a Multiline_Eval_String_Knob. Nuke evaluates that
# knob type as TCL, and JSON's [...] are TCL commands - a large target made
# Nuke throw regex_error(error_complexity) or die outright. Never read it;
# just remove it from any script that still has it.
OLD_ROOT_KNOBS = ("matchprobe_target",)
CRASH_LOG = os.path.join(os.path.expanduser("~"), ".nuke", "matchprobe_crash.log")
TRACE_LOG = os.path.join(os.path.expanduser("~"), ".nuke", "matchprobe_trace.log")
_FAULT_FILE = None
_QUIET = {"until": time.time() + 5.0}   # no sampling for 5 s after startup


def trace(msg):
    """Breadcrumb written straight to disk, so the last line survives a crash."""
    try:
        if os.path.exists(TRACE_LOG) and os.path.getsize(TRACE_LOG) > 512 * 1024:
            os.replace(TRACE_LOG, TRACE_LOG + ".old")
        with open(TRACE_LOG, "a") as f:
            f.write("%s.%03d %s\n" % (time.strftime("%H:%M:%S"), int(time.time() * 1000) % 1000, msg))
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass


def quiet_for(seconds, why=""):
    """Pause live sampling (script loading, nodes being created, ...)."""
    until = time.time() + seconds
    if until > _QUIET["until"]:
        _QUIET["until"] = until
    if why:
        _QUIET["why"] = why


def engine_quiet():
    """True while sampling the node graph is not safe/wanted."""
    if time.time() < _QUIET["until"]:
        return True
    try:
        if nuke.executing():
            return True
    except Exception:
        pass
    return False


def _on_create():
    quiet_for(1.5)


def _on_script_close():
    quiet_for(5.0, "script close")
    trace("script close -> live sampling paused")


def _on_script_load():
    quiet_for(2.0, "script load")
    trace("script loaded: %s" % nuke.root().name())
    try:
        remove_old_target_knobs()
    except Exception:
        pass


def enable_crash_log():
    """faulthandler writes every thread's Python stack to CRASH_LOG on a hard
    crash (access violation etc.), so a crash inside Match Probe leaves a trace."""
    global _FAULT_FILE
    if _FAULT_FILE is not None:
        return
    try:
        import faulthandler
        _FAULT_FILE = open(CRASH_LOG, "a")
        _FAULT_FILE.write("\n=== Nuke session started %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        _FAULT_FILE.flush()
        faulthandler.enable(file=_FAULT_FILE, all_threads=True)
    except Exception:
        _FAULT_FILE = None
PANEL_ID = "uk.co.arda.MatchProbe"
PREFS_PATH = os.path.join(os.path.expanduser("~"), ".nuke", "matchprobe_prefs.json")
RANGES = [("native floating point", 1.0), ("8-bit", 255.0), ("10-bit", 1023.0),
          ("16-bit", 65535.0)]
# Sampling a node while the viewer is still rendering it can crash Nuke
# (seen: Defocus.dll access violation on the render threads while
# nuke.sample() re-validated the same tree from the main thread). So Match
# Probe only pulls pixels once box, node, frame, layer and knobs have been
# unchanged for SETTLE_SECONDS - by then the viewer is normally done.
SETTLE_SECONDS = 2.0
LIVE_GRID = 64           # full sampling up to 64x64 px, strided above
SUFFIX_ORDER = ("red", "green", "blue", "alpha", "r", "g", "b", "a", "x", "y", "z", "w", "u", "v")


def load_prefs():
    p = {"range": RANGES[0][0]}
    try:
        with open(PREFS_PATH) as f:
            p.update(json.load(f))
    except (IOError, OSError, ValueError):
        pass
    return p


def save_prefs(p):
    try:
        with open(PREFS_PATH, "w") as f:
            json.dump(p, f, indent=2)
    except (IOError, OSError):
        pass


# --------------------------------------------------------------------------
#  viewers, layers, regions
# --------------------------------------------------------------------------
def viewer_nodes():
    return sorted(n.name() for n in nuke.allNodes("Viewer"))


def _viewer(name=None):
    """(viewer node, viewed node) for 'Active Viewer' (None) or a named Viewer."""
    if name:
        vn = nuke.toNode(name)
        if vn is None:
            return None, None
        av = nuke.activeViewer()
        if av is not None and av.node() is vn:
            idx = av.activeInput()
        else:
            idx = int(vn["input_number"].value()) if "input_number" in vn.knobs() else 0
    else:
        av = nuke.activeViewer()
        if av is None:
            return None, None
        vn, idx = av.node(), av.activeInput()
    if idx is None:
        return vn, None
    return vn, vn.input(int(idx))


def layers_of(node):
    seen = []
    try:
        for ch in node.channels():
            lay = ch.split(".")[0]
            if lay not in seen:
                seen.append(lay)
    except Exception:
        pass
    return seen


def layer_channels(node, layer):
    """Up to 4 full channel names of a layer, in r g b a order (None = missing)."""
    chans = [c for c in node.channels() if c.split(".")[0] == layer]
    chans.sort(key=lambda c: SUFFIX_ORDER.index(c.split(".")[1])
               if c.split(".")[1] in SUFFIX_ORDER else 99)
    chans = chans[:4]
    if layer == "rgba" or len(chans) < 4:
        by_suffix = {c.split(".")[1]: c for c in chans}
        if layer == "rgba":
            return [by_suffix.get(s) for s in ("red", "green", "blue", "alpha")]
        return chans + [None] * (4 - len(chans))
    return chans


def viewer_layer(vn):
    """The layer the viewer shows ('current layer' in the Pixel Analyzer)."""
    try:
        v = vn["channels"].value()
        return v.split(".")[0] if v else "rgba"
    except Exception:
        return "rgba"


def viewer_region(viewer_name=None):
    """(node, [x, y, r, t] px, frame, viewer node) of the Ctrl+Shift sample box.
    Mapping as used by NST_GradMagic."""
    vn, node = _viewer(viewer_name)
    if node is None:
        return None
    bb = vn["colour_sample_bbox"].value()
    w, h, pa = node.width(), node.height(), node.pixelAspect()
    xs = [(bb[0] * 0.5 + 0.5) * w, (bb[2] * 0.5 + 0.5) * w]
    ys = [bb[1] * 0.5 * w * pa + 0.5 * h, bb[3] * 0.5 * w * pa + 0.5 * h]
    box = [min(xs), min(ys), max(xs), max(ys)]
    box[2] = max(box[2], box[0] + 1)
    box[3] = max(box[3], box[1] + 1)
    return node, [int(math.floor(box[0])), int(math.floor(box[1])),
                  int(math.ceil(box[2])), int(math.ceil(box[3]))], int(nuke.frame()), vn


# --------------------------------------------------------------------------
#  averages
# --------------------------------------------------------------------------
def region_avg(node, box, frame, channels, grid=LIVE_GRID, keep=False):
    """Average of each channel over box. Every pixel when the box is at most
    grid x grid, otherwise an evenly strided grid. Missing channels -> None.
    keep=True also returns the sampled rgb triplets (for the vectorscope)."""
    x0, y0, x1, y1 = [float(v) for v in box]
    w, h = max(int(round(x1 - x0)), 1), max(int(round(y1 - y0)), 1)
    nx, ny = min(grid, w), min(grid, h)
    sums = [0.0] * len(channels)
    points = []
    for j in range(ny):
        y = y0 + (j + 0.5) * h / float(ny)
        for i in range(nx):
            x = x0 + (i + 0.5) * w / float(nx)
            px = [node.sample(c, x, y, frame=frame) if c else 0.0 for c in channels]
            for k in range(len(channels)):
                sums[k] += px[k]
            if keep:
                points.append(px[:3])
    n = float(nx * ny)
    return {"avg": [(s / n) if c else None for s, c in zip(sums, channels)],
            "exact": nx == w and ny == h, "grid": (nx, ny), "points": points}


def exact_avg(node, box, frame, channels):
    """Exact average via a temporary CurveTool (layer shuffled into rgb)."""
    out = []
    with nuke.root():
        tmp = []
        try:
            for group in (channels[:3], [channels[3]] * 3):
                if not any(group):
                    out.extend([None] * 3)
                    continue
                e = nuke.nodes.Expression(inputs=[node])
                tmp.append(e)
                for i, c in enumerate(group):
                    e["channel%d" % i].fromScript(("rgba.red", "rgba.green", "rgba.blue")[i])
                    e["expr%d" % i].setValue(c if c else "0")
                ct = nuke.nodes.CurveTool(inputs=[e])
                tmp.append(ct)
                ct["operation"].setValue("Avg Intensities")
                ct["ROI"].setValue(list(box))
                nuke.execute(ct, frame, frame)
                out.extend(ct["intensitydata"].getValueAt(frame, i) for i in range(3))
        finally:
            for n in reversed(tmp):
                nuke.delete(n)
    avg = out[:3] + [out[3]]
    return [v if c else None for v, c in zip(avg, channels)]


# --------------------------------------------------------------------------
#  value rows
# --------------------------------------------------------------------------
def hsvl(rgb):
    """Nuke Pixel Analyzer hsvl: HSV hue deg, (max-min)/max, max, Nuke luminance."""
    r, g, b = [v or 0.0 for v in rgb[:3]]
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d <= 0:
        h = 0.0
    elif mx == r:
        h = 60.0 * (((g - b) / d) % 6.0)
    elif mx == g:
        h = 60.0 * ((b - r) / d + 2.0)
    else:
        h = 60.0 * ((r - g) / d + 4.0)
    s = d / mx if mx > 0 else 0.0
    # Nuke's luminance weights (match the Pixel Analyzer exactly, not 0.2126/0.7152/0.0722)
    return [h, s, mx, 0.2125 * r + 0.7154 * g + 0.0721 * b]


def oklch(rgb, ws=0):
    """Perceptual row: OKLab lightness, chroma, hue deg, saturation (C/L)."""
    L, a, b = cm.to_oklab([v or 0.0 for v in rgb[:3]], ws)
    C = math.hypot(a, b)
    return [L, C, math.degrees(math.atan2(b, a)) % 360.0, C / L if L > 1e-6 else 0.0]


def delta(t, s, hue_index=None):
    """[(abs, pct or None)] source - target; hue wraps to +-180 and has no %."""
    out = []
    for i, (a, b) in enumerate(zip(t, s)):
        if a is None or b is None:
            out.append((None, None))
            continue
        d = b - a
        if i == hue_index:
            d = (d + 180.0) % 360.0 - 180.0
            out.append((d, None))
            continue
        out.append((d, 100.0 * d / a if abs(a) > 1e-12 else None))
    return out


def compare(tgt, src, ws=0, scale=1.0):
    """All three rows for target, source and difference."""
    rows = {}
    for name, fn, hue_i, scaled in (("rgba", None, None, (0, 1, 2, 3)),
                                    ("hsvl", hsvl, 0, (2, 3)),
                                    ("oklch", lambda v: oklch(v, ws), 2, ())):
        t = list(tgt) if fn is None else fn(tgt)
        s = list(src) if fn is None else fn(src)
        t = [v * scale if (v is not None and i in scaled) else v for i, v in enumerate(t)]
        s = [v * scale if (v is not None and i in scaled) else v for i, v in enumerate(s)]
        rows[name] = (t, s, delta(t, s, hue_i))
    return rows


# --------------------------------------------------------------------------
#  correction
# --------------------------------------------------------------------------
def suggest(tgt_rgb, src_rgb):
    """Per-channel Grade multiply mapping the source average onto the target."""
    return [(t / s) if abs(s) > 1e-12 else 1.0 for t, s in zip(tgt_rgb[:3], src_rgb[:3])]


def make_grade(src_node, multiply, target_label=""):
    """Insert a Grade right after src_node and reconnect its outputs."""
    nuke.Undo.begin("Match Probe: Make Grade")
    try:
        deps = [(d, i) for d in src_node.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS, False)
                for i in range(d.inputs()) if d.input(i) is src_node and d.Class() != "Viewer"]
        g = nuke.nodes.Grade(inputs=[src_node])
        g["multiply"].setValue(list(multiply) + [1.0])
        g["label"].setValue("MatchProbe → %s" % target_label if target_label else "MatchProbe")
        g.setXYpos(src_node.xpos(), src_node.ypos() + 60)
        for d, i in deps:
            d.setInput(i, g)
    finally:
        nuke.Undo.end()
    return g


def match_params(tgt_rgb, src_rgb, ws=0, hue=True, sat=True, light=True):
    """Knob values for the LCh node that move the source average onto the target.

    The node rotates hue, then scales chroma by `saturation`, then scales L, a
    and b together by `lightness` (= an exposure change of lightness**3, which
    keeps hue and saturation). With all three on, the source average lands
    exactly on the target. Returns (params, notes)."""
    tL, ta, tb = cm.to_oklab([v or 0.0 for v in tgt_rgb[:3]], ws)
    sL, sa, sb = cm.to_oklab([v or 0.0 for v in src_rgb[:3]], ws)
    tC, sC = math.hypot(ta, tb), math.hypot(sa, sb)
    out = {"hue_rotate": 0.0, "saturation": 1.0, "lightness": 1.0}
    notes = []
    grey = 0.002
    if hue:
        if tC < grey or sC < grey:
            notes.append("hue skipped: %s is (almost) grey" % ("target" if tC < grey else "source"))
        else:
            d = math.degrees(math.atan2(tb, ta) - math.atan2(sb, sa))
            out["hue_rotate"] = (d + 180.0) % 360.0 - 180.0
    if sat:
        if sC < grey or sL <= 1e-6 or tL <= 1e-6:
            notes.append("saturation skipped: source is (almost) grey")
        else:
            out["saturation"] = (tC / tL) / (sC / sL)
    if light:
        if sL <= 1e-6:
            notes.append("lightness skipped: source is black")
        else:
            out["lightness"] = tL / sL
    return out, notes


def _ws_select(knob, tables, ws_expr="parent.working_space"):
    """Matrix knob picks its 3x3 from `tables` by the group's working_space."""
    for i in range(9):
        e = "%.12g" % tables[-1][i]
        for w in range(len(tables) - 2, -1, -1):
            e = "%s==%d ? %.12g : (%s)" % (ws_expr, w, tables[w][i], e)
        knob.setExpression(e, i)


def _expr_rgb(src, exprs, temps=(), name=None):
    e = nuke.nodes.Expression(inputs=[src])
    if name:
        e.setName(name)
    for i, (tn, te) in enumerate(temps):
        e["temp_name%d" % i].setValue(tn)
        e["temp_expr%d" % i].setValue(te)
    for i, ch in enumerate(("rgba.red", "rgba.green", "rgba.blue")):
        e["channel%d" % i].fromScript(ch)
        e["expr%d" % i].setValue(exprs[i])
    e["channel3"].fromScript("none")
    return e


def create_lch_node(ws=0):
    """MatchProbe_LCh: hue rotate / saturation x / lightness x in OKLab, plus
    mask + mix. A plain Group of native nodes (no Python needed to render)."""
    g = nuke.nodes.Group()
    g.setName("MatchProbe_LCh", uncollide=True)
    K = nuke
    g.addKnob(K.Tab_Knob("lch", "Match LCh"))
    g.addKnob(K.Enumeration_Knob("working_space", "working space", cm.WS_NAMES))
    k = K.Double_Knob("hue_rotate", "hue rotate")
    k.setRange(-180, 180)
    k.setTooltip("Degrees around the OKLab hue circle; lightness and saturation stay put.")
    g.addKnob(k)
    k = K.Double_Knob("saturation", "saturation")
    k.setRange(0, 2)
    k.setTooltip("Multiplies OKLab chroma; hue and lightness stay put.")
    g.addKnob(k)
    k = K.Double_Knob("lightness", "lightness")
    k.setRange(0, 2)
    k.setTooltip("Multiplies OKLab lightness (= exposure of lightness^3); hue and "
                 "saturation stay put.")
    g.addKnob(k)
    k = K.Text_Knob("stops", "", "")
    g.addKnob(k)
    k = K.Double_Knob("mix", "mix")
    g.addKnob(k)
    g["working_space"].setValue(ws)
    g["saturation"].setValue(1.0)
    g["lightness"].setValue(1.0)
    g["mix"].setValue(1.0)
    g["stops"].setValue("lightness as exposure: [format %+.3f [expr 3*log([value lightness])/log(2)]] stops")
    g["tile_color"].setValue(0x3E6FA8FF)
    inv_lms = [cm._inv(m) for m in cm.WS_TO_LMS]
    with g:
        src = nuke.nodes.Input(name="Input")
        mask = nuke.nodes.Input(name="mask")
        mask["number"].setValue(1)
        lms = nuke.nodes.ColorMatrix(inputs=[src], name="to_lms")
        _ws_select(lms["matrix"], cm.WS_TO_LMS)
        cb = _expr_rgb(lms, ["r<0 ? -pow(-r,1/3.0) : pow(r,1/3.0)",
                             "g<0 ? -pow(-g,1/3.0) : pow(g,1/3.0)",
                             "b<0 ? -pow(-b,1/3.0) : pow(b,1/3.0)"], name="cbrt")
        lab = nuke.nodes.ColorMatrix(inputs=[cb], name="to_lab")
        lab["matrix"].setValue(cm.LMS_TO_LAB)
        adj = _expr_rgb(lab, ["r*m", "(g*c - b*s)*k*m", "(g*s + b*c)*k*m"],
                        temps=[("c", "cos(radians(parent.hue_rotate))"),
                               ("s", "sin(radians(parent.hue_rotate))"),
                               ("k", "parent.saturation"), ("m", "parent.lightness")],
                        name="adjust_lch")
        back = nuke.nodes.ColorMatrix(inputs=[adj], name="from_lab")
        back["matrix"].setValue(cm._inv(cm.LMS_TO_LAB))
        cube = _expr_rgb(back, ["r*r*r", "g*g*g", "b*b*b"], name="cube")
        rgb = nuke.nodes.ColorMatrix(inputs=[cube], name="to_ws")
        _ws_select(rgb["matrix"], inv_lms)
        mrg = nuke.nodes.Merge2(inputs=[src, rgb, mask], name="mask_mix")
        mrg["operation"].setValue("copy")
        mrg["output"].fromScript("rgb")
        mrg["mix"].setExpression("parent.mix")
        nuke.nodes.Output(inputs=[mrg])
    return g


def make_lch(src_node, params, ws=0, target_label=""):
    """Insert a MatchProbe_LCh node right after src_node and reconnect its outputs."""
    nuke.Undo.begin("Match Probe: Make Grade (LCh)")
    try:
        deps = [(d, i) for d in src_node.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS, False)
                for i in range(d.inputs()) if d.input(i) is src_node and d.Class() != "Viewer"]
        with nuke.root():
            g = create_lch_node(ws)
        for k, v in params.items():
            g[k].setValue(v)
        g.setInput(0, src_node)
        g["label"].setValue("\u2192 %s" % target_label if target_label else "")
        g.setXYpos(src_node.xpos(), src_node.ypos() + 60)
        for d, i in deps:
            d.setInput(i, g)
    finally:
        nuke.Undo.end()
    return g


# --------------------------------------------------------------------------
#  target persisted on the Root
# --------------------------------------------------------------------------
def encode_target(t):
    """JSON -> zlib -> base64: only A-Z a-z 0-9 + / =, nothing TCL can evaluate."""
    return base64.b64encode(zlib.compress(json.dumps(t).encode("utf-8"), 9)).decode("ascii")


def decode_target(text):
    return json.loads(zlib.decompress(base64.b64decode(text.encode("ascii"))).decode("utf-8"))


def remove_old_target_knobs():
    """Drop old-format target knobs WITHOUT reading their value. Returns count."""
    root = nuke.root()
    n = 0
    for name in OLD_ROOT_KNOBS:
        k = root.knobs().get(name)
        if k is not None:
            try:
                root.removeKnob(k)
                n += 1
            except Exception:
                pass
    if n:
        trace("removed %d old-format Match Probe target knob(s) from %s" % (n, root.name()))
    return n


def save_target(t):
    root = nuke.root()
    remove_old_target_knobs()
    if ROOT_KNOB not in root.knobs():
        k = nuke.String_Knob(ROOT_KNOB, "Match Probe target")
        k.setFlag(nuke.INVISIBLE)
        root.addKnob(k)
    root[ROOT_KNOB].setValue(encode_target(t) if t else "")


def load_target():
    root = nuke.root()
    remove_old_target_knobs()
    k = root.knobs().get(ROOT_KNOB)
    if k is None:
        return None
    try:
        text = k.toScript().strip().strip('"')   # raw text, never TCL-evaluated
        if not text:
            return None
        t = decode_target(text)
        return t if "avg" in t else None
    except Exception:
        return None


# --------------------------------------------------------------------------
#  swatch colour through the viewer's OCIO display/view
# --------------------------------------------------------------------------
_PROC_CACHE = {}


def _ocio_config_path():
    root = nuke.root()
    name = root["OCIO_config"].value()
    if name == "custom":
        return root["customOCIOConfigPath"].evaluate()
    base = os.path.join(os.path.dirname(nuke.EXE_PATH), "plugins", "OCIOConfigs", "configs")
    for cand in (os.path.join(base, name, "config.ocio"), os.path.join(base, name),
                 os.path.join(base, name + ".ocio")):
        if os.path.isfile(cand):
            return cand
    return name  # may be an ocio:// builtin URI


def _display_processor(vn):
    try:
        import PyOpenColorIO as OCIO
        vp = vn["viewerProcess"].value()
        m = re.match(r"^(.*) \((.*)\)$", vp)
        view, display = (m.group(1), m.group(2)) if m else (vp, None)
        path = _ocio_config_path()
        key = (path, display, view)
        if key not in _PROC_CACHE:
            cfg = OCIO.Config.CreateFromFile(path)
            display = display or cfg.getDefaultDisplay()
            t = OCIO.DisplayViewTransform(src="scene_linear", display=display, view=view)
            _PROC_CACHE[key] = cfg.getProcessor(t).getDefaultCPUProcessor()
        return _PROC_CACHE[key]
    except Exception:
        return None


def display_rgb(rgb, vn=None, ws=0):
    """8-bit display colour of a scene-linear value, like the viewer shows it."""
    rgb = [v or 0.0 for v in rgb[:3]]
    proc = _display_processor(vn) if vn is not None else None
    if proc is not None:
        out = proc.applyRGB(list(rgb))
    else:  # rough fallback: ws -> Rec.709, soft shoulder, sRGB curve
        out = []
        for v in cm._vec(cm.WS_TO_709[ws], rgb):
            v = max(v, 0.0)
            v = v / (1.0 + v) * 1.6 if v > 0.6 else v
            v = min(v, 1.0)
            out.append(12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055)
    return [int(round(255 * min(max(v, 0.0), 1.0))) for v in out]


def display_floats(rgbs, vn=None, ws=0):
    """Scene-linear rgb triplets -> display-referred 0-1 floats (viewer OCIO)."""
    rgbs = [[v or 0.0 for v in q[:3]] for q in rgbs]
    proc = _display_processor(vn) if vn is not None else None
    if proc is not None and rgbs:
        flat = [v for q in rgbs for v in q]
        out = list(proc.applyRGB(flat))
        return [out[i:i + 3] for i in range(0, len(out), 3)]
    res = []
    for q in rgbs:
        o = []
        for v in cm._vec(cm.WS_TO_709[ws], q):
            v = max(v, 0.0)
            v = v / (1.0 + v) * 1.6 if v > 0.6 else v
            v = min(v, 1.0)
            o.append(12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055)
        res.append(o)
    return res


def cbcr(disp):
    """Display-referred R'G'B' -> Rec.709 (Cb, Cr), each in -0.5..0.5."""
    r, g, b = disp[:3]
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return ((b - y) / 1.8556, (r - y) / 1.5748)


SCOPE_TARGETS = [("R", (0.75, 0, 0)), ("Yl", (0.75, 0.75, 0)), ("G", (0, 0.75, 0)),
                 ("Cy", (0, 0.75, 0.75)), ("B", (0, 0, 0.75)), ("Mg", (0.75, 0, 0.75))]
SKIN_LINE_DEG = 123.0   # I-line, counter-clockwise from +Cb
SCOPE_POINTS = 4000     # max dots drawn per scope


def thin(points, n=SCOPE_POINTS):
    if len(points) <= n:
        return list(points)
    step = len(points) / float(n)
    return [points[int(i * step)] for i in range(n)]


def auto_zoom(radii):
    """Largest of 16/8/4/2/1x that keeps 95% of the dots inside the scope."""
    if not radii:
        return 1.0
    radii = sorted(radii)
    r95 = radii[min(int(len(radii) * 0.95), len(radii) - 1)]
    for z in (16.0, 8.0, 4.0, 2.0):
        if r95 * z <= 0.42:
            return z
    return 1.0


# --------------------------------------------------------------------------
#  panel
# --------------------------------------------------------------------------
try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    try:
        from PySide2 import QtCore, QtGui, QtWidgets
    except ImportError:
        QtWidgets = None


def _decimals(row, col, scale):
    """Pixel-Analyzer-like precision: 8 decimals in float, 3 when scaled to
    integer ranges, 2 for hue angles."""
    if (row == "hsvl" and col == 0) or (row == "oklch" and col == 2):
        return 2
    if scale > 1.0 and (row == "rgba" or (row == "hsvl" and col in (2, 3))):
        return 3
    return 8


def _fmt(v, dec=8):
    return "" if v is None else "%.*f" % (dec, v)


def _fmt_delta(a, pct, dec=8):
    if a is None:
        return ""
    return ("%+.*f" % (dec, a)) + ("  (%+.1f%%)" % pct if pct is not None else "")


if QtWidgets is not None:

    class _Swatch(QtWidgets.QFrame):
        """Pixel-Analyzer-like box: title, colour, small caption."""

        def __init__(self, title, parent=None):
            super(_Swatch, self).__init__(parent)
            self.setFrameShape(QtWidgets.QFrame.StyledPanel)
            self.setFixedWidth(150)
            lay = QtWidgets.QVBoxLayout(self)
            lay.setContentsMargins(6, 4, 6, 6)
            self.title = QtWidgets.QLabel(title)
            self.title.setAlignment(QtCore.Qt.AlignCenter)
            lay.addWidget(self.title)
            self.colour = QtWidgets.QLabel()
            self.colour.setFixedHeight(70)
            self.colour.setStyleSheet("background: #2a2a2a;")
            lay.addWidget(self.colour)
            self.caption = QtWidgets.QLabel("-")
            self.caption.setAlignment(QtCore.Qt.AlignCenter)
            self.caption.setWordWrap(True)
            f = self.caption.font()
            f.setPointSize(max(f.pointSize() - 1, 7))
            self.caption.setFont(f)
            lay.addWidget(self.caption)

        def set(self, rgb8, caption):
            if rgb8 is None:
                self.colour.setStyleSheet("background: #2a2a2a;")
            else:
                self.colour.setStyleSheet("background: rgb(%d,%d,%d);" % tuple(rgb8))
            self.caption.setText(caption)

    class _Scope(QtWidgets.QWidget):
        """Rec.709 Cb/Cr vectorscope: dot cloud of the region, filled marker =
        this region's average, dashed ring = the other region's average."""

        def __init__(self, title, parent=None):
            super(_Scope, self).__init__(parent)
            self.title = title
            self.points, self.avg, self.other, self.zoom = [], None, None, 1.0
            self.setMinimumSize(220, 220)

        def set_data(self, points, avg, other, zoom):
            self.points, self.avg, self.other, self.zoom = points, avg, other, zoom
            self.update()

        def paintEvent(self, e):
            p = QtGui.QPainter(self)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            w, h = self.width(), self.height()
            p.fillRect(0, 0, w, h, QtGui.QColor(24, 24, 24))
            side = min(w, h - 16) - 16
            cx, cy = w / 2.0, (h + 16) / 2.0
            sc = side / 2.0 / 0.5 * self.zoom   # radius 0.5 = scope edge at 1x

            def pt(cb, cr):
                return QtCore.QPointF(cx + cb * sc, cy - cr * sc)

            p.setPen(QtGui.QPen(QtGui.QColor(90, 90, 90)))
            for rad in (0.025, 0.05, 0.1, 0.25, 0.5):   # finer rings matter when zoomed in
                if rad * sc > 6 and rad * sc < side:
                    p.drawEllipse(QtCore.QPointF(cx, cy), rad * sc, rad * sc)
            p.drawLine(pt(-0.5, 0), pt(0.5, 0))
            p.drawLine(pt(0, -0.5), pt(0, 0.5))
            a = math.radians(SKIN_LINE_DEG)
            sk = QtGui.QPen(QtGui.QColor(150, 150, 150))
            sk.setStyle(QtCore.Qt.DashLine)
            p.setPen(sk)
            p.drawLine(pt(0, 0), pt(0.5 * math.cos(a), 0.5 * math.sin(a)))
            p.setPen(QtGui.QColor(170, 170, 170))
            f = p.font()
            f.setPointSize(8)
            p.setFont(f)
            for name, rgb in SCOPE_TARGETS:
                q = pt(*cbcr(rgb))
                p.drawRect(QtCore.QRectF(q.x() - 5, q.y() - 5, 10, 10))
                p.drawText(QtCore.QPointF(q.x() + 7, q.y() - 6), name)
            p.drawText(QtCore.QPointF(6, 13), "%s   %gx" % (self.title, self.zoom))

            if self.points:
                p.setPen(QtGui.QPen(QtGui.QColor(235, 235, 235, 110), 1.6))
                p.drawPoints(QtGui.QPolygonF([pt(b_, r_) for b_, r_ in self.points]))
            if self.other is not None:
                pen = QtGui.QPen(QtGui.QColor(255, 255, 255), 1.5)
                pen.setStyle(QtCore.Qt.DashLine)
                p.setPen(pen)
                p.setBrush(QtCore.Qt.NoBrush)
                p.drawEllipse(pt(*self.other), 7, 7)
            if self.avg is not None:
                q = pt(*self.avg)
                p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0), 3))
                p.setBrush(QtCore.Qt.NoBrush)
                p.drawEllipse(q, 5, 5)
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255), 1.5))
                p.setBrush(QtGui.QColor(255, 255, 255))
                p.drawEllipse(q, 4, 4)
                p.drawLine(QtCore.QPointF(q.x() - 10, q.y()), QtCore.QPointF(q.x() + 10, q.y()))
                p.drawLine(QtCore.QPointF(q.x(), q.y() - 10), QtCore.QPointF(q.x(), q.y() + 10))
            p.end()

    class MatchProbePanel(QtWidgets.QWidget):
        ROWS = (("rgba", ("r", "g", "b", "a")),
                ("hsvl", ("h", "s", "v", "l")),
                ("oklch", ("L", "C", "h", "sat")))

        def __init__(self, parent=None):
            super(MatchProbePanel, self).__init__(parent)
            self.prefs = load_prefs()
            self.target = load_target()
            self.ws = cm.detect_working_space()
            self.last_key = None
            self.dirty = True
            self._src = None            # (node, box, frame, channels, layer, vn)
            self._build()
            # single-shot chain: the next poll is scheduled only after the
            # previous one finished, so polls can never overlap
            self.timer = QtCore.QTimer(self)
            self.timer.setSingleShot(True)
            self.timer.setInterval(200)
            self.timer.timeout.connect(self._tick)
            self._busy = False          # Lock / Make Grade running nuke.execute
            self._seen_key = None
            self._stale = False         # viewer selection moved since the last sample
            self._last_change = time.time()
            self._in_poll = False       # guards against re-entry via processEvents
            self._live = False          # panel visible, callbacks registered
            self.debounce = QtCore.QTimer(self)
            self.debounce.setSingleShot(True)
            self.debounce.setInterval(120)
            self.debounce.timeout.connect(self._mark_dirty)
            self._show_target()

        # ---- layout -------------------------------------------------------
        def _build(self):
            L = QtWidgets.QVBoxLayout(self)
            # [Lock target / Clear target] [target] [source] [Sample source]
            top = QtWidgets.QHBoxLayout()
            tbtns = QtWidgets.QVBoxLayout()
            b = QtWidgets.QPushButton("Lock target")
            b.setToolTip("Ctrl+Shift+drag a region in the Viewer, then lock its exact "
                         "average as the target (saved with the script).")
            b.clicked.connect(self.lock_target)
            tbtns.addWidget(b)
            b = QtWidgets.QPushButton("Clear target")
            b.clicked.connect(self.clear_target)
            tbtns.addWidget(b)
            tbtns.addStretch(1)
            top.addLayout(tbtns)
            self.sw_t = _Swatch("target\naverage")
            self.sw_s = _Swatch("source\naverage")
            top.addWidget(self.sw_t)
            top.addWidget(self.sw_s)
            sbtns = QtWidgets.QVBoxLayout()
            self.btn_sample = QtWidgets.QPushButton("Sample source")
            self.btn_sample.setToolTip(
                "Ctrl+Shift+drag the source region in the Viewer, wait until the Viewer "
                "has finished rendering, then press. Match Probe never samples on its own: "
                "pulling pixels while the Viewer is still rendering the same nodes can crash Nuke.")
            self.btn_sample.clicked.connect(self.sample_source)
            sbtns.addWidget(self.btn_sample)
            self.chk_live = QtWidgets.QCheckBox("live update")
            self.chk_live.setChecked(False)          # always starts off
            self.chk_live.setToolTip(
                "WARNING: this can crash Nuke.\n\n"
                "Samples the source automatically once the box, viewed node, frame and "
                "knobs have been still for %g s. If the Viewer is still rendering the same "
                "nodes at that moment (slow trees, Defocus, heavy comps), Nuke can crash - "
                "this is what caused the earlier crashes.\n\n"
                "Safer: leave this off and press 'Sample source' after the Viewer has "
                "finished. Always off when Nuke starts." % SETTLE_SECONDS)
            self.chk_live.toggled.connect(self._live_toggled)
            sbtns.addWidget(self.chk_live)
            sbtns.addStretch(1)
            top.addLayout(sbtns)
            top.addStretch(1)
            L.addLayout(top)

            form = QtWidgets.QGridLayout()
            form.addWidget(QtWidgets.QLabel("sample"), 0, 0, QtCore.Qt.AlignRight)
            self.cmb_viewer = QtWidgets.QComboBox()
            self.cmb_viewer.setMinimumWidth(220)
            self.cmb_viewer.currentIndexChanged.connect(self._mark_dirty)
            form.addWidget(self.cmb_viewer, 0, 1)
            self.cmb_layer = QtWidgets.QComboBox()
            self.cmb_layer.setMinimumWidth(220)
            self.cmb_layer.currentIndexChanged.connect(self._mark_dirty)
            form.addWidget(self.cmb_layer, 0, 2)
            form.addWidget(QtWidgets.QLabel("range"), 1, 0, QtCore.Qt.AlignRight)
            self.cmb_range = QtWidgets.QComboBox()
            self.cmb_range.addItems([r[0] for r in RANGES])
            self.cmb_range.setCurrentText(self.prefs.get("range", RANGES[0][0]))
            self.cmb_range.currentTextChanged.connect(self._range_changed)
            form.addWidget(self.cmb_range, 1, 1)
            form.setColumnStretch(3, 1)
            L.addLayout(form)

            grid = QtWidgets.QGridLayout()
            grid.setHorizontalSpacing(4)
            grid.setVerticalSpacing(2)
            self.fields = {}
            self.section_widgets = {}    # name -> widgets hidden when folded
            self.section_buttons = {}
            r = 0
            for name, cols in self.ROWS:
                # disclosure triangle + section name; folds the rows below it
                btn = QtWidgets.QToolButton()
                btn.setText(name)
                btn.setCheckable(True)
                btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
                btn.setAutoRaise(True)
                f = btn.font()
                f.setBold(True)
                btn.setFont(f)
                btn.setToolTip("Show / hide the %s rows" % name)
                grid.addWidget(btn, r, 0, QtCore.Qt.AlignLeft)
                widgets = []
                for c, col in enumerate(cols):
                    lab = QtWidgets.QLabel(col)
                    lab.setAlignment(QtCore.Qt.AlignCenter)
                    grid.addWidget(lab, r, 1 + c)
                    widgets.append(lab)
                r += 1
                for which in ("target", "source", "Δ"):
                    lab = QtWidgets.QLabel(which)
                    grid.addWidget(lab, r, 0, QtCore.Qt.AlignRight)
                    widgets.append(lab)
                    for c in range(4):
                        e = QtWidgets.QLineEdit()
                        e.setReadOnly(True)
                        e.setMinimumWidth(130)
                        grid.addWidget(e, r, 1 + c)
                        self.fields[(name, which, c)] = e
                        widgets.append(e)
                    r += 1
                grid.setRowMinimumHeight(r, 4)
                r += 1
                self.section_widgets[name] = widgets
                self.section_buttons[name] = btn
                btn.toggled.connect(lambda on, n=name: self._fold(n, on))
                btn.setChecked(bool(self.prefs.get("open_" + name, name == "rgba")))
                self._fold(name, btn.isChecked(), save=False)
            grid.setColumnStretch(5, 1)
            L.addLayout(grid)
            self.lbl_note = QtWidgets.QLabel("")
            L.addWidget(self.lbl_note)

            zr = QtWidgets.QHBoxLayout()
            zr.addWidget(QtWidgets.QLabel("vectorscope zoom"))
            self.cmb_zoom = QtWidgets.QComboBox()
            self.cmb_zoom.addItems(["auto", "1x", "2x", "4x", "8x", "16x"])
            self.cmb_zoom.setCurrentText(self.prefs.get("zoom", "auto"))
            self.cmb_zoom.currentTextChanged.connect(self._zoom_changed)
            zr.addWidget(self.cmb_zoom)
            zr.addWidget(QtWidgets.QLabel("   filled = this average,  dashed ring = the other "
                                          "average,  dashed line = skin tone"))
            zr.addStretch(1)
            L.addLayout(zr)
            sr = QtWidgets.QHBoxLayout()
            self.scope_t = _Scope("target")
            self.scope_s = _Scope("source")
            sr.addWidget(self.scope_t)
            sr.addWidget(self.scope_s)
            L.addLayout(sr, 1)

            br = QtWidgets.QHBoxLayout()
            self.chk_match = {}
            for key, label, tip in (
                    ("hue", "match hue", "Rotate hue so the source average's hue equals the target's."),
                    ("sat", "match saturation", "Scale saturation (chroma / lightness) to the target's."),
                    ("light", "match lightness", "Scale lightness to the target's - an exposure "
                                                 "change that keeps hue and saturation.")):
                c = QtWidgets.QCheckBox(label)
                c.setToolTip(tip)
                c.setChecked(bool(self.prefs.get("match_" + key, True)))
                c.toggled.connect(lambda v, k=key: self._match_pref(k, v))
                br.addWidget(c)
                self.chk_match[key] = c
            br.addStretch(1)
            self.btn_grade = QtWidgets.QPushButton("Make Grade")
            self.btn_grade.setMinimumWidth(140)
            self.btn_grade.setToolTip(
                "Insert a MatchProbe_LCh node after the source node. Ticked parts get the "
                "measured values (OKLab hue rotate / saturation x / lightness x), unticked "
                "ones stay neutral. Has mask + mix; knobs stay editable.")
            self.btn_grade.clicked.connect(self.make_grade)
            br.addWidget(self.btn_grade)
            L.addLayout(br)

        # ---- combos -----------------------------------------------------
        def _refresh_viewers(self):
            items = ["Active Viewer"] + viewer_nodes()
            cur = self.cmb_viewer.currentText()
            if [self.cmb_viewer.itemText(i) for i in range(self.cmb_viewer.count())] != items:
                self.cmb_viewer.blockSignals(True)
                self.cmb_viewer.clear()
                self.cmb_viewer.addItems(items)
                self.cmb_viewer.setCurrentText(cur if cur in items else "Active Viewer")
                self.cmb_viewer.blockSignals(False)

        def _refresh_layers(self, node, vn):
            cur_name = "current layer (%s)" % viewer_layer(vn)
            items = [cur_name] + layers_of(node)
            have = [self.cmb_layer.itemText(i) for i in range(self.cmb_layer.count())]
            if have != items:
                cur = self.cmb_layer.currentText()
                self.cmb_layer.blockSignals(True)
                self.cmb_layer.clear()
                self.cmb_layer.addItems(items)
                if cur.startswith("current layer") or cur not in items:
                    self.cmb_layer.setCurrentIndex(0)
                else:
                    self.cmb_layer.setCurrentText(cur)
                self.cmb_layer.blockSignals(False)

        def _layer(self, vn):
            t = self.cmb_layer.currentText()
            return viewer_layer(vn) if (not t or t.startswith("current layer")) else t

        def _range_changed(self, v):
            self.prefs["range"] = v
            save_prefs(self.prefs)
            self._redraw()

        def _fold(self, name, open_, save=True):
            """Disclosure triangle: open = rows visible (down arrow)."""
            btn = self.section_buttons[name]
            btn.setArrowType(QtCore.Qt.DownArrow if open_ else QtCore.Qt.RightArrow)
            for w in self.section_widgets[name]:
                w.setVisible(open_)
            if save:
                self.prefs["open_" + name] = bool(open_)
                save_prefs(self.prefs)

        def _match_pref(self, key, v):
            self.prefs["match_" + key] = bool(v)
            save_prefs(self.prefs)

        def _zoom_changed(self, v):
            self.prefs["zoom"] = v
            save_prefs(self.prefs)
            self._redraw()

        def _scale(self):
            return dict(RANGES).get(self.cmb_range.currentText(), 1.0)

        # ---- actions ------------------------------------------------------
        def _region(self):
            name = self.cmb_viewer.currentText()
            return viewer_region(None if name in ("", "Active Viewer") else name)

        def _settled_wait(self):
            return max(0.0, SETTLE_SECONDS - (time.time() - self._last_change))

        def _when_settled(self, fn, what):
            """Run fn once nothing has changed for SETTLE_SECONDS."""
            wait = self._settled_wait()
            if wait > 0:
                self.lbl_note.setText("%s: waiting for the viewer to settle (%.1f s) ..." % (what, wait))
                QtCore.QTimer.singleShot(int(wait * 1000) + 50, lambda: self._when_settled(fn, what))
                return
            if self._busy or self._in_poll:
                QtCore.QTimer.singleShot(100, lambda: self._when_settled(fn, what))
                return
            self._engine_job(fn)

        def lock_target(self):
            if self._busy:
                return
            self._engine_job(self._lock_target)

        def _lock_target(self):
            reg = self._region()
            if reg is None:
                nuke.message("Match Probe: view a node and Ctrl+Shift+drag a region first.")
                return
            node, box, f, vn = reg
            layer = self._layer(vn)
            chans = layer_channels(node, layer)
            avg = exact_avg(node, box, f, chans)
            cloud = region_avg(node, box, f, chans, grid=64, keep=True)["points"]
            self.target = {"node": node.fullName(), "box": box, "frame": f, "layer": layer,
                           "avg": avg,
                           "cloud": [[round(v, 5) for v in q] for q in thin(cloud, 512)]}
            save_target(self.target)
            self._show_target()
            self.dirty = True

        def clear_target(self):
            self.target = None
            save_target(None)
            self._show_target()
            self._redraw()

        def make_grade(self):
            if self._busy:
                return
            self._engine_job(self._make_grade)

        def _make_grade(self):
            if not self.target or self._src is None:
                nuke.message("Match Probe: lock a target and select a source first.")
                return
            node, box, f, chans, layer, vn = self._src
            if layer != "rgba" or self.target.get("layer") != "rgba":
                nuke.message("Match Probe: Make Grade works on the rgba layer only.")
                return
            flags = {k: c.isChecked() for k, c in self.chk_match.items()}
            if not any(flags.values()):
                nuke.message("Match Probe: tick at least one of hue / saturation / lightness.")
                return
            src = exact_avg(node, box, f, chans)
            params, notes = match_params(self.target["avg"], src, self.ws,
                                         flags["hue"], flags["sat"], flags["light"])
            make_lch(node, params, self.ws, self.target["node"])
            if notes:
                nuke.message("Match Probe:\n" + "\n".join(notes))
            self.dirty = True

        # ---- live loop ----------------------------------------------------
        def showEvent(self, e):
            super(MatchProbePanel, self).showEvent(e)
            self._live = True
            self._set_poke(self.chk_live.isChecked())
            self.timer.start()

        def hideEvent(self, e):
            self._stop_live()
            super(MatchProbePanel, self).hideEvent(e)

        def closeEvent(self, e):
            self._stop_live()
            super(MatchProbePanel, self).closeEvent(e)

        def _stop_live(self):
            self.timer.stop()
            self.debounce.stop()
            self._live = False
            self._set_poke(False)

        def _set_poke(self, on):
            """knobChanged hook, only while live update is ticked."""
            if on and not getattr(self, "_poke_on", False):
                nuke.addKnobChanged(self._knob_poke, nodeClass="*")
                self._poke_on = True
            elif not on and getattr(self, "_poke_on", False):
                try:
                    nuke.removeKnobChanged(self._knob_poke, nodeClass="*")
                except Exception:
                    pass
                self._poke_on = False

        def _live_toggled(self, on):
            self._set_poke(on and self._live)
            self._last_change = time.time()
            self.dirty = True
            trace("live update %s" % ("ON" if on else "off"))
            self.lbl_note.setText("live update on - samples %g s after things stop changing "
                                  "(can crash Nuke)" % SETTLE_SECONDS if on else "live update off")

        def _live_tick(self):
            """Auto-sample (live update ticked): only after SETTLE_SECONDS of no change."""
            if not self.chk_live.isChecked() or self._busy or engine_quiet():
                return
            reg = self._region()
            if reg is None:
                return
            node, box, f, vn = reg
            key = (node.fullName(), tuple(box), f)
            now = time.time()
            if key != self._seen_key:
                self._seen_key = key
                self._last_change = now
            if not (self._src is None or self._stale or self.dirty):
                return
            wait = SETTLE_SECONDS - (now - self._last_change)
            if wait > 0:
                self.lbl_note.setText("live update: sampling in %.1f s ..." % wait)
                return
            self.dirty = False
            self.sample_source()

        def _tick(self):
            try:
                self._check_stale()
                self._live_tick()
            except Exception as err:
                self.lbl_note.setText("Match Probe: %s" % err)
            finally:
                if self._live and not self._busy:
                    self.timer.start()

        def _check_stale(self):
            """Only reads the Viewer's selection box / viewed node / frame - no
            pixels, no channels(), nothing that makes Nuke render."""
            if self._busy or self._src is None or not self.isVisible():
                return
            try:
                reg = self._region()
                if reg is None:
                    return
                node, box, f, vn = reg
                cur = (node.fullName(), tuple(box), f)
                was = (self._src[0].fullName(), tuple(self._src[1]), self._src[2])
                stale = cur != was
            except Exception:
                stale = True
            if stale != self._stale:
                self._stale = stale
                self._show_stale()

        def _show_stale(self):
            if self._stale:
                self.sw_s.title.setText("source average\n(stale)")
                self.sw_s.setStyleSheet("QLabel { color: #8a8a8a; }")
                self.lbl_note.setText("selection / node / frame changed - press Sample source")
            else:
                self.sw_s.title.setText("source\naverage")
                self.sw_s.setStyleSheet("")

        def sample_source(self):
            if self._busy:
                return
            self._engine_job(self._sample_now)

        def _engine_job(self, fn):
            """Run something that calls nuke.execute / creates nodes with the
            live loop fully paused, so sample() can't run in the middle of it."""
            trace("engine job start: %s" % getattr(fn, "__name__", fn))
            self._busy = True
            self.timer.stop()
            self.debounce.stop()
            self.btn_sample.setEnabled(False)
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            try:
                return fn()
            except Exception as err:
                self.lbl_note.setText("Match Probe: %s" % err)
            finally:
                QtWidgets.QApplication.restoreOverrideCursor()
                trace("engine job end")
                self._busy = False
                self.btn_sample.setEnabled(True)
                if self._live:
                    self.timer.start()

        def _knob_poke(self):
            try:
                if nuke.thisNode().Class() == "Viewer" or \
                        nuke.thisKnob().name() in ("xpos", "ypos", "selected", "label"):
                    return
            except Exception:
                return
            self._last_change = time.time()
            self.dirty = True

        def _mark_dirty(self, *a):
            self.dirty = True

        def _sample_now(self):
            """Manual 'Sample source' (runs inside _engine_job)."""
            self._in_poll = True
            try:
                self._refresh_viewers()
                reg = self._region()
                if reg is None:
                    nuke.message("Match Probe: view a node and Ctrl+Shift+drag the source region first.")
                    return
                node, box, f, vn = reg
                self._refresh_layers(node, vn)
                layer = self._layer(vn)
                chans = layer_channels(node, layer)
                t0 = time.time()
                trace("sample %s %s frame %d layer %s" % (node.fullName(), box, f, layer))
                res = region_avg(node, box, f, chans, keep=True)
                trace("sample done %.0f ms" % (1000 * (time.time() - t0)))
                self._src = (node, box, f, chans, layer, vn)
                self._src_avg = res
                self._src_time = time.time() - t0
                self._stale = False
                self._show_stale()
                self._redraw()
                self.lbl_note.setText(self.lbl_note.text() + "   |   sampled %s" % time.strftime("%H:%M:%S"))
            finally:
                self._in_poll = False

        # ---- drawing ------------------------------------------------------
        def _caption(self, node, box, f, layer):
            return "%s\n%s  frame %d\nxywh %d %d %d %d" % (
                node, layer, f, box[0], box[1], box[2] - box[0], box[3] - box[1])

        def _vn(self):
            return self._src[5] if self._src else (nuke.activeViewer().node()
                                                   if nuke.activeViewer() else None)

        def _show_target(self):
            t = self.target
            if not t:
                self.sw_t.set(None, "Ctrl+Shift+drag,\nthen Lock target")
            else:
                self.sw_t.set(display_rgb(t["avg"], self._vn(), self.ws),
                              self._caption(t["node"], t["box"], t["frame"], t["layer"]))
            self._redraw()

        def _redraw(self):
            if not hasattr(self, "fields"):
                return
            src = getattr(self, "_src_avg", None)
            if self._src and src:
                node, box, f, chans, layer, vn = self._src
                self.sw_s.set(display_rgb(src["avg"], vn, self.ws),
                              self._caption(node.fullName(), box, f, layer))
                note = "source: every pixel" if src["exact"] else \
                    "source: sampled %dx%d (target is exact)" % src["grid"]
                if self.target and self.target.get("layer") != layer:
                    note += "   |   layers differ: target %s, source %s" % (
                        self.target["layer"], layer)
                self.lbl_note.setText(note)
            tavg = self.target["avg"] if self.target else None
            savg = src["avg"] if src else None
            rows = compare(tavg or [None] * 4, savg or [None] * 4, self.ws, self._scale()) \
                if (tavg and savg) else None
            for name, _ in self.ROWS:
                for which in ("target", "source", "Δ"):
                    for c in range(4):
                        txt = ""
                        dec = _decimals(name, c, self._scale())
                        if rows:
                            t, s, d = rows[name]
                            if which == "target":
                                txt = _fmt(t[c], dec)
                            elif which == "source":
                                txt = _fmt(s[c], dec)
                            else:
                                txt = _fmt_delta(d[c][0], d[c][1], dec)
                        else:
                            single = tavg if which == "target" else (savg if which == "source" else None)
                            if single:
                                one = compare(single, single, self.ws, self._scale())[name][0]
                                txt = _fmt(one[c], dec)
                        self.fields[(name, which, c)].setText(txt)
            trace("draw scopes")
            self._draw_scopes(tavg, savg, src)
            trace("draw done")

        def _draw_scopes(self, tavg, savg, src):
            if not hasattr(self, "scope_t"):
                return
            vn = self._vn()
            t_pts, s_pts, t_avg, s_avg = [], [], None, None
            if tavg:
                t_avg = cbcr(display_floats([tavg], vn, self.ws)[0])
                t_pts = [cbcr(d) for d in display_floats(self.target.get("cloud") or [], vn, self.ws)]
            if savg and src:
                s_avg = cbcr(display_floats([savg], vn, self.ws)[0])
                s_pts = [cbcr(d) for d in display_floats(thin(src.get("points") or []), vn, self.ws)]
            z = self.cmb_zoom.currentText()
            if z == "auto":
                zoom = auto_zoom([math.hypot(*q) for q in t_pts + s_pts +
                                  [q for q in (t_avg, s_avg) if q]])
            else:
                zoom = float(z.rstrip("x"))
            self.scope_t.set_data(t_pts, t_avg, s_avg, zoom)
            self.scope_s.set_data(s_pts, s_avg, t_avg, zoom)

    def show():
        from nukescripts import panels
        p = panels.registerWidgetAsPanel("matchProbe.MatchProbePanel", "Match Probe", PANEL_ID, True)
        p.addToPane(nuke.getPaneFor("Properties.1"))
        return p

else:

    def show():
        nuke.message("Match Probe needs PySide2 or PySide6.")


def register():
    """Called from menu.py: Pane menu entry + layout restore."""
    if QtWidgets is None:
        return
    import sys
    import __main__
    __main__.matchProbe = sys.modules[__name__]  # factory string is evaluated in __main__
    enable_crash_log()
    trace("---- Nuke %s started, Match Probe registered" % nuke.NUKE_VERSION_STRING)
    nuke.addOnCreate(_on_create, nodeClass="*")
    nuke.addOnScriptClose(_on_script_close)
    nuke.addOnScriptLoad(_on_script_load)
    from nukescripts import panels
    panels.registerWidgetAsPanel("matchProbe.MatchProbePanel", "Match Probe", PANEL_ID)
