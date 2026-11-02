"""Read an individual part / assembly drawing PDF (no STEP needed): title block, notes, views and BOM table.

Extracts material, thickness, units, scale, finish, hardware callouts, a BOM table (if any) and the blank size
measured from the heavy object-line outline (snapped to the dimension text).
"""
import collections, math, re
import fitz

PT_MM = 25.4 / 72.0
MAT_RE = re.compile(r"STEEL|STAINLESS|\bSUS|\bSST\b|\bSS ?\d{3}|ALUM|\bAL ?\d{4}|\b\d{4}-?[HT]\d|A\d{3,4}\b|ASTM|CRS|HRS|SECC|SPCC|SGCC|\bEG\b|BRASS|COPPER|BRONZE|TITANIUM|DELRIN|\bPOM\b|PEEK|UHMW|NYLON|ABS\b|PVC|POLYCARB|\bMS\b|MILD|CARBON|1018|6061|5052|304|316", re.I)
LABELS = {"MATERIAL", "SCALE", "SIZE", "DRAWING", "NUMBER", "TITLE", "FINISH", "REV", "SHEET", "DATE", "WEIGHT", "DESCRIPTION", "APPROVED", "DRAWN", "CHECKED", "UNLESS", "OTHERWISE", "SPECIFIED", "TOLERANCES", "NOTES", "DO", "NOT", "SIGNATURE"}
HW_RE = re.compile(r"\b(PEM|CLINCH|SELF[- ]?CLINCH\w*|STUD|STANDOFF|STAND-OFF|NUT|RIVET|SCREW|WASHER|BOLT|INSERT|HELICOIL|SPACER|PIN|GROMMET|CASTER|HINGE|LATCH|HANDLE|LABEL)\b", re.I)
HW_SKIP = re.compile(r"CLEAN|BAG|MARK|ENGRAV|DEBURR|BURR|PLATING|FINISH|DIMENSION|INTERPRET|BEND|RADII|PACKAG|FREE OF|STANDARD|SPECIFICATION", re.I)
PN_TOKEN = re.compile(r"\b(\d{6,7}-\d{3}|\d{4}-\d{5}|[A-Z]{1,4}-?[A-Z0-9]{1,5}-\d{1,4}[A-Z0-9-]*)\b")


def _spans(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                t = s["text"].strip()
                if t: out.append((t, fitz.Rect(s["bbox"]), s["size"]))
    return out


def _num(t):
    try: return float(t)
    except Exception: return None


def _scale(spans):
    for t, r, _ in spans:
        if t.upper() == "SCALE":
            cands = [(abs(r2.x0 - r.x1) + abs(r2.y0 - r.y0) * 0.5, t2) for t2, r2, _ in spans
                     if t2 != t and abs(r2.x0 - r.x1) < 110 and abs(r2.y0 - r.y0) < 25]
            for _, t2 in sorted(cands):
                m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)", t2)
                if m: return float(m.group(1)) / float(m.group(2)) if "/" in t2 else float(m.group(2)) / float(m.group(1)) if float(m.group(1)) == 1 else float(m.group(1)) / float(m.group(2))
                if t2.upper() in ("FULL", "1X"): return 1.0
                if t2.upper() == "HALF": return 0.5
                v = _num(t2)
                if v and 0.01 < v < 100: return v
    return 1.0


def _units(text):
    u = text.upper()
    if re.search(r"DIMENSIONS ARE IN (MM|MILLIMET)|ALL DIMENSIONS IN MM|\bIN MM\b", u): return "mm"
    if re.search(r"DIMENSIONS ARE IN INCH|IN INCHES|INCH(ES)? UNLESS", u): return "in"
    return "mm"


def _near(spans, label_rect, below=65, right=190, above=0):
    """Spans sitting below / right of a label (title-block cells), nearest first."""
    c = []
    for t, r, sz in spans:
        if r == label_rect or t.upper() in LABELS: continue
        dy = r.y0 - label_rect.y1
        if -above <= dy <= below and r.x1 >= label_rect.x0 - 25 and r.x0 <= label_rect.x1 + right:
            c.append((abs(dy) + abs(r.x0 - label_rect.x0) * 0.3, t, r))
    return sorted(c, key=lambda x: x[0])


def _material(spans, text):
    for t, r, _ in spans:
        if t.upper() == "MATERIAL":
            for _, t2, r2 in _near(spans, r, below=70, right=190, above=8):
                if MAT_RE.search(t2) and len(t2) >= 4: return t2
    for line in text.splitlines():
        if re.search(r"MATERIAL\s*[:=]", line, re.I):
            return line.split(":", 1)[-1].strip() or line
    for t, r, _ in spans:
        if re.fullmatch(r"(STEEL|ALUMINUM|STAINLESS)[\w ,._/-]*", t, re.I): return t
    return None


def _thickness(spans, text, unit, mat):
    """-> (mm, source) from THICK/THK callouts in the views, notes, title block or the material string."""
    f = 25.4 if unit == "in" else 1.0
    for t, r, _ in spans:
        if re.fullmatch(r"(THICK|THK|THICKNESS)\.?:?", t, re.I):
            c = []
            for t2, r2, _ in spans:
                v = _num(t2)
                if v and 0 < v < 60 and abs(r2.y0 - r.y0) < 14 and (abs(r2.x1 - r.x0) < 60 or abs(r2.x0 - r.x1) < 60):
                    c.append((min(abs(r2.x1 - r.x0), abs(r2.x0 - r.x1)), v))
            if c: return min(c)[1] * f, "view callout THICK"
    for pat, src in ((r"(?:THICKNESS|THK|THICK)\s*[:=]?\s*(\d*\.?\d+)\s*(MM|IN|\")?", "note / title block"),
                     (r"(\d*\.?\d+)\s*(MM|IN|\")?\s*(?:THK|THICK|THICKNESS)", "note / title block")):
        m = re.search(pat, text, re.I)
        if m:
            v = float(m.group(1)); un = (m.group(2) or "").upper()
            return v * (25.4 if un in ("IN", '"') else 1.0 if un == "MM" else f), src
    if mat:
        m = re.search(r"(\d*\.?\d+)\s*(MM|IN|\")", mat, re.I)
        if m: return float(m.group(1)) * (25.4 if m.group(2).upper() in ("IN", '"') else 1.0), "material string"
        m = re.search(r"(\d{1,2})\s*GA", mat, re.I)
        if m:
            g = {7: 4.55, 8: 4.18, 10: 3.43, 11: 3.04, 12: 2.67, 14: 1.9, 16: 1.52, 18: 1.2, 20: 0.91}.get(int(m.group(1)))
            if g: return g, "gauge in material string"
    return None, None


def _blank(page, scale, nums_mm, thick_label):
    """Flat blank (L, W) mm from the heavy-line outline of the view whose size matches dimension text."""
    dr = page.get_drawings()
    if not dr: return None
    PW, PH = page.rect.width, page.rect.height
    byw = collections.defaultdict(list)
    for x in dr:
        w = round(x.get("width") or 0, 2)
        r = fitz.Rect(x["rect"])
        if w >= 0.5 and (r.width > 0.05 or r.height > 0.05): byw[w].append(r)
    best = None
    for w, rects in byw.items():
        par = list(range(len(rects)))

        def find(i):
            while par[i] != i: par[i] = par[par[i]]; i = par[i]
            return i
        G = 25; grid = collections.defaultdict(list); E = 0.4
        for i, r in enumerate(rects):
            for gx in range(int((r.x0 - E) // G), int((r.x1 + E) // G) + 1):
                for gy in range(int((r.y0 - E) // G), int((r.y1 + E) // G) + 1):
                    for j in grid[(gx, gy)]:
                        a = rects[j]
                        if a.x0 - E <= r.x1 and r.x0 - E <= a.x1 and a.y0 - E <= r.y1 and r.y0 - E <= a.y1: par[find(i)] = find(j)
                    grid[(gx, gy)].append(i)
        cl = {}
        for i, r in enumerate(rects):
            k = find(i); cl[k] = (cl[k] | r) if k in cl else fitz.Rect(r)
        for c in cl.values():
            if c.width > 0.7 * PW or c.height > 0.7 * PH: continue          # frame / title block
            if c.width < 12 or c.height < 12: continue
            L, W = sorted((c.width * PT_MM / scale, c.height * PT_MM / scale), reverse=True)
            snap = []
            for v in (L, W):
                hit = [n for n in nums_mm if abs(n - v) <= max(0.35, 0.012 * v)]
                snap.append(min(hit, key=lambda n: abs(n - v)) if hit else None)
            matched = all(s is not None for s in snap)
            dist = 0.0
            if thick_label is not None:
                dist = math.hypot((c.x0 + c.x1) / 2 - thick_label[0], (c.y0 + c.y1) / 2 - thick_label[1])
            key = (not matched, round(dist / 80), -(L * W))
            if best is None or key < best[0]:
                best = (key, (snap[0] or round(L, 1), snap[1] or round(W, 1)), matched, c)
    if not best: return None
    return dict(L=best[1][0], W=best[1][1], dimensioned=best[2], source=("heavy outline snapped to dimension text" if best[2] else "heavy outline geometry (not dimensioned)"))


def _hardware(lines):
    out = []
    for ln in lines:
        s = " ".join(ln.split())
        if len(s) < 6 or len(s) > 160 or HW_SKIP.search(s) or not HW_RE.search(s): continue
        if not (re.match(r"\d+[.)]\s", s) or re.search(r"\bINSTALL\b|\bQTY\b|(?<![\d.])\d+\s*[Xx]\s+[A-Za-z]", s)): continue   # note / callout, not a table cell
        q = re.search(r"(?<![\d.])(\d+)\s*[Xx]\s+(?=[A-Za-z])", s)
        pn = next((m for m in PN_TOKEN.findall(s) if re.search(r"\d", m)), "")
        out.append(dict(qty=int(q.group(1)) if q else 1, text=s, pn=pn, qty_found=bool(q)))
    return out


def parse_part_drawing(path):
    d = fitz.open(path)
    info = dict(pages=len(d), material=None, thickness=None, thickness_src=None, units="mm", scale=1.0, title=None, finish=None,
                notes=[], hardware=[], blank=None, bom=[], rev=None)
    for pg in d:
        spans = _spans(pg)
        text = pg.get_text()
        if len(text) < 80 and not pg.get_drawings(): continue
        unit = _units(text); info["units"] = unit
        info["scale"] = _scale(spans)
        mat = _material(spans, text)
        if mat and not info["material"]: info["material"] = mat
        th, src = _thickness(spans, text, unit, mat)
        if th and not info["thickness"]: info["thickness"], info["thickness_src"] = round(th, 3), src
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        m = next((re.search(r"FINISH\s*[:=]\s*(.+)", l, re.I) for l in lines if re.search(r"FINISH\s*[:=]", l, re.I)), None)
        if m: info["finish"] = m.group(1).strip()
        info["notes"] = [l for l in lines if re.match(r"\d+[.\s]", l)][:20]
        info["hardware"] += _hardware(lines)
        for t, r, _ in spans:                                   # description: cell above DRAWING NUMBER
            if re.fullmatch(r"(DRAWING|DWG)\.? ?(NUMBER|NO\.?)?:?", t, re.I) and not info["title"]:
                c = [(r.y0 - r2.y1, t2) for t2, r2, _ in spans if 0 <= r.y0 - r2.y1 < 60 and abs(r2.x0 - r.x0) < 70 and len(t2) > 8 and t2.upper() not in LABELS and not re.fullmatch(r"[\d.\s-]+", t2)]
                if c: info["title"] = min(c)[1]
        nums = set()
        for t, r, _ in spans:
            v = _num(re.sub(r"^[0-9]+X\s*", "", t))
            if v and v > 1: nums.add(v * (25.4 if unit == "in" else 1.0))
        tl = next(((r.x0, r.y0) for t, r, _ in spans if re.fullmatch(r"(THICK|THK|THICKNESS)\.?:?", t, re.I)), None)
        if not info["title"]:
            c = [(-r.y0, t) for t, r, _ in spans if r.x0 > pg.rect.width * 0.45 and r.y0 > pg.rect.height * 0.7 and "," in t and len(t) > 8 and re.search(r"[A-Za-z]{3}", t) and not MAT_RE.search(t)]
            if c: info["title"] = min(c)[1]
        if not info["blank"]:
            try: info["blank"] = _blank(pg, info["scale"], sorted(nums), tl)
            except Exception: info["blank"] = None
        info["bom"] += parse_table_bom(pg, spans)
    return info


def parse_table_bom(page, spans=None):
    """BOM table found by its header (ITEM / PART NUMBER / DESCRIPTION / QTY) -> [{item, pn, desc, qty}]."""
    words = page.get_text("words")
    hdr = {}
    for w in words:
        t = w[4].upper().strip(".")
        if t in ("ITEM", "NO", "QTY", "QUANTITY", "DESCRIPTION", "DESC") or t in ("PART", "P/N", "PN"):
            hdr.setdefault(t, []).append(w)
    if not (("QTY" in hdr or "QUANTITY" in hdr) and ("DESCRIPTION" in hdr or "DESC" in hdr) and ("PART" in hdr or "P/N" in hdr or "PN" in hdr)): return []
    for q in hdr.get("QTY", []) + hdr.get("QUANTITY", []):
        row = [w for k in hdr.values() for w in k if abs(w[1] - q[1]) < 6]
        names = {w[4].upper().strip(".") for w in row}
        if not ({"DESCRIPTION", "DESC"} & names and {"PART", "P/N", "PN"} & names): continue
        cols = {}
        for w in row:
            t = w[4].upper().strip(".")
            cols["qty" if t in ("QTY", "QUANTITY") else "desc" if t in ("DESCRIPTION", "DESC") else "pn" if t in ("PART", "P/N", "PN") else "item"] = w[0]
        if "item" not in cols: cols["item"] = min(w[0] for w in row) - 5 if min(w[0] for w in row) not in cols.values() else min(cols.values()) - 40
        order = sorted(cols.items(), key=lambda kv: kv[1])
        bounds = [(k, (x - 4)) for k, x in order]
        def col(x):
            c = bounds[0][0]
            for k, bx in bounds:
                if x >= bx: c = k
            return c
        below = [w for w in words if w[1] > q[3] - 1 and abs(w[0] - q[0]) < 400 and w[0] >= order[0][1] - 40]
        above = [w for w in words if w[3] < q[1] + 1 and abs(w[0] - q[0]) < 400 and w[0] >= order[0][1] - 40]
        for region, rev in ((below, False), (above, True)):
            rows = collections.defaultdict(list)
            for w in sorted(region, key=lambda w: w[1], reverse=rev):
                rows[round(w[1] / 5)].append(w)
            items = []; last_y = None
            for key in sorted(rows, reverse=rev):
                ws = sorted(rows[key], key=lambda w: w[0]); y = ws[0][1]
                if last_y is not None and abs(y - last_y) > 28: break
                cells = collections.defaultdict(list)
                for w in ws: cells[col(w[0])].append(w[4])
                item = " ".join(cells.get("item", [])); qty = " ".join(cells.get("qty", []))
                if item.isdigit() and qty.replace(".", "").isdigit():
                    items.append(dict(item=int(item), pn=" ".join(cells.get("pn", [])), desc=" ".join(cells.get("desc", [])), qty=int(float(qty))))
                    last_y = y
                elif items and not item and not qty: items[-1]["desc"] += " " + " ".join(cells.get("desc", [])); last_y = y
                elif items: break
            if len(items) >= 1: return sorted(items, key=lambda x: x["item"])
    return []
