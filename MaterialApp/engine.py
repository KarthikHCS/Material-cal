"""Rules engine: scan an assembly folder, classify materials, size blanks, consolidate, raise questions."""
import os, re, glob
from collections import OrderedDict, defaultdict
import pdfparse as pp
import stepgeo as sg
import inventory as invmod
import drawing as dr
import crossref as xref

IN = 25.4
DEFAULTS = dict(
    assemblies=1,            # number of top-level assemblies to build
    sheet_max_mm=6.0,        # < this = sheet
    plate_max_mm=10.0,       # sheet_max..plate_max = plate (>10 = machined solid: plate / bar / rod)
    allowance_mm=2.0,        # per side, machined parts (solid plate / bar / rod)
    allowance_thickness=True,  # allowance on every side incl. thickness / diameter
    tube_allow_mm=5.0,       # per end, hollow tube cut pieces
    stock_len_mm=3000.0,     # standard bar / rod / hollow tube length (3.0 m)
    kerf_mm=0.0,             # saw kerf between pieces on a bar (0 = not included)
    nest_gap_mm=5.0,         # spacing between nested sheet / plate blanks
    plate_ratio=6.0,         # solid blank section W/T >= this -> solid plate, below -> bar
    fill_min=0.85,           # non-bent plate with volume/box-volume below this = irregular outline = machined
)
SHEET_SIZES = {"4x8 ft": (1219.2, 2438.4), "5x10 ft": (1524.0, 3048.0)}


def is_imperial_t(desc):
    """True when the thickness in a description is imperial: a bare decimal such as '.09 THK', inches, a quote mark or a gauge -
    anything without an explicit mm value."""
    if thickness_mm(desc) is None: return False
    return not re.search(r"\d\s*MM\b|\bT\s*\d+(?:\.\d+)?\s*MM", desc, re.I)


def thickness_mm(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*MM\b", text, re.I)
    if m: return float(m.group(1))
    m = re.search(r"(?<!\d)(\d*\.\d+)\s*(?:INCH|IN\b|\"|THK)", text, re.I)
    if m: return float(m.group(1)) * IN
    m = re.search(r"(?<![\d./])(\d+)\s*/\s*(\d+)\s*(?:INCH|IN\b|\"|THK|THICK)", text, re.I)          # 1/4 THK, 3/16", 1/2 IN
    if m and int(m.group(2)): return int(m.group(1)) / int(m.group(2)) * IN
    m = re.search(r"\b(\d{1,2})\s*GA(?:UGE)?\b", text, re.I)              # sheet gauge -> imperial gauge equivalent (0250-46767 Table 1)
    g = {28: .0149, 26: .0179, 24: .0239, 22: .0299, 20: .0359, 18: .0478, 16: .0598, 14: .0747, 13: .0897, 12: .1046, 11: .1196, 10: .1345, 8: .1644, 7: .1793}
    if m and re.search(r"SUS|SST|STAINLESS|\b30[14]\b|\b316", text, re.I):    # stainless gauges differ from carbon steel gauges
        g = {10: .1406, 11: .125, 12: .1094, 13: .0938, 14: .0781, 16: .0625, 18: .05, 20: .0375, 22: .0312, 24: .025}
    return g[int(m.group(1))] * IN if m and int(m.group(1)) in g else None


def canon_spec(desc, t):
    """Canonical material text so equivalent descriptions consolidate (SHEET/PLATE wording, inch/THK variants)."""
    u = re.sub(r"\s+", " ", desc.upper())
    m = re.match(r"SHEET, MATERIAL: (.*?)\s*([\d.]+) MM THK$", u)
    if m: return f"{m.group(1).title().replace('_', ' ')} {float(m.group(2)):.2f} mm (from drawing)"
    if "HRS" in u and "A1011" in u and t:
        return f'HRS {t/IN:.3f}" ({t:.2f} mm) ASTM A1011 CS Type B, P&O'
    return re.sub(r"\s+", " ", desc.strip())


def is_raw(desc, pn):
    return (not pn) and bool(re.match(r"(SHEET|PLATE|PL\b|BAR|FLAT|ROUND|TUBE|TUBING|PIPE|ANGLE|CHANNEL)", desc.strip(), re.I))


def tube_origin(desc):
    for b in re.findall(r"\(([^)]*)\)", desc):
        if re.search(r"\d\s*(\"|IN\b|INCH)", b, re.I): return "US"
        if re.search(r"\d\s*MM\b", b, re.I): return "Local"
    return "?"


def _place(free, w, h):
    best = None
    for i, (x, y, fw, fh) in enumerate(free):
        for pw, ph in ((w, h), (h, w)):
            if pw <= fw + 1e-9 and ph <= fh + 1e-9:
                sc = (min(fw - pw, fh - ph), max(fw - pw, fh - ph))
                if best is None or sc < best[0]: best = (sc, x, y, pw, ph)
    if not best: return False
    _, px, py, pw, ph = best
    new = []
    for fx, fy, fw, fh in free:
        if px < fx + fw and px + pw > fx and py < fy + fh and py + ph > fy:
            if px > fx: new.append((fx, fy, px - fx, fh))
            if px + pw < fx + fw: new.append((px + pw, fy, fx + fw - px - pw, fh))
            if py > fy: new.append((fx, fy, fw, py - fy))
            if py + ph < fy + fh: new.append((fx, py + ph, fw, fy + fh - py - ph))
        else:
            new.append((fx, fy, fw, fh))
    keep = [r for i, r in enumerate(new) if not any(j != i and o[0] <= r[0] + 1e-9 and o[1] <= r[1] + 1e-9 and
            o[0] + o[2] >= r[0] + r[2] - 1e-9 and o[1] + o[3] >= r[1] + r[3] - 1e-9 and (o != r or j < i) for j, o in enumerate(new))]
    free[:] = keep
    return True


def nest_rects(rects, sheet, gap=0.0):
    """MaxRects (best short side fit, rotation allowed) nesting of rectangles [(a, b)] onto sheets (W, L).
    Returns (sheets used, pieces that do not fit, total rectangle area placed)."""
    SW, SL = sheet; bins = []; over = 0; used = 0.0
    for a, b in sorted(rects, key=lambda r: -(r[0] * r[1])):
        w, h = a + gap, b + gap
        if not ((w <= SW + gap and h <= SL + gap) or (h <= SW + gap and w <= SL + gap)):
            over += 1; continue
        if not any(_place(f, w, h) for f in bins):
            f = [(0.0, 0.0, SW + gap, SL + gap)]; _place(f, w, h); bins.append(f)
        used += a * b
    return len(bins), over, used


def fits_sheet(l, w):
    """Smallest standard sheet size (4x8 ft, then 5x10 ft) that can hold one blank l x w."""
    for nm, (SW, SL) in SHEET_SIZES.items():
        if (l <= SL and w <= SW) or (w <= SL and l <= SW): return nm
    return None


def sheet_label(nm):
    SW, SL = SHEET_SIZES[nm]
    return f"{nm.replace('x', ' x ')} sheet ({SW:.0f} x {SL:.0f} mm)"


class Engine:
    def __init__(self, root, opt=None, inv=None):
        self.root = root
        self.inv = inv
        self.o = dict(DEFAULTS); self.o.update(opt or {})
        self.parts = []          # part-level rows
        self.q = []              # questions
        self.tcmap = {}          # TcENG -> (pn, desc)
        self.forms = {}
        self.cache = {}
        self.parts_from_drawing = 0
        self.index = pp.index_files(root)   # part number -> files
        self.coverage = []                  # child-level review rows
        self.seen_cov = set()

    # ---------------------------------------------------------- helpers
    def ask(self, topic, text):
        if not any(t == topic and x == text for t, x in self.q): self.q.append((topic, text))

    def folder_for(self, pn):
        return pn if pn in self.index else None

    def files(self, pn):
        return self.index.get(pn, [])

    def cover(self, parent, pn, desc, kind, drawing, form, step, status):
        k = (parent, pn, desc)
        if k in self.seen_cov: return
        self.seen_cov.add(k)
        self.coverage.append(dict(parent=parent, pn=pn, desc=desc, kind=kind, drawing=drawing, form=form, step=step, status=status))

    def mfr(self, pn):
        if pn in self.forms: return self.forms[pn]
        f = pp.parse_form(self.files(pn))
        self.forms[pn] = f; return f

    def mfr_text(self, pn, tc):
        f = self.mfr(pn)
        real = [(m, n) for m, n in f if m.upper() != "ANY VENDORS"]
        if not f:
            self.ask("Manufacturer missing", f"{pn}: no OEM part definition form found. Which manufacturer / mfr part number? (TcENG {tc})")
            return f"not available (TcENG {tc})", ""
        mf = [m for m, _ in real]
        distinct = list(OrderedDict.fromkeys(mf))
        if not real:
            return f"{f[0][1]} (Any vendors)", ""
        first = real[0]
        alts = [f"{n} ({m})" for m, n in real[1:]] + [f"{n} (Any vendors)" for m, n in f if m.upper() == "ANY VENDORS"]
        note = ""
        if len(distinct) > 1:
            self.ask("Multiple manufacturers", f"{pn}: form lists several manufacturers ({'; '.join(f'{m} {n}' for m, n in real)}). Which one to buy? Shown: {first[1]} ({first[0]}).")
            note = "Alternates: " + "; ".join(alts)
        elif alts:
            note = "Alternates: " + "; ".join(alts)
        if first[1] == pn:
            self.ask("Manufacturer P/N", f"{pn}: form shows the Applied Materials number as the manufacturer P/N ({first[0]}). Confirm the real manufacturer part number.")
        return f"{first[1]} ({first[0]})", note

    # ---------------------------------------------------------- raw material
    def raw_part(self, parent, it, qty_parent, mult, stp):
        desc, tc = it["desc"], it["tc"]
        t = thickness_mm(desc)
        row = dict(parent=parent, item=it["item"], part=f"{tc}", mnum=f"{tc} (AMAT TcENG; no mfr no.)", spec=canon_spec(desc, t),
                   qty=qty_parent, ext=qty_parent * mult, note=[])
        if re.match(r"(TUBE|TUBING|PIPE)", desc, re.I):
            a = self.o["tube_allow_mm"]; SL = self.o["stock_len_mm"]
            row.update(cat="Raw material - hollow tube", form="Hollow tube", size=re.sub(r"\s+", " ", desc.strip()),
                       stock=f"{SL/1000:g} m standard length", mach="Cut to length",
                       tube=tube_origin(desc), area=None, blank=None, key=("tube", re.sub(r"\s+", " ", desc.upper())))
            if row["tube"] == "?": self.ask("Tube size", f"{tc}: tube size not in brackets with units - US or Local?")
            s = stp.get(tc)
            if s:
                try:
                    _, _, Lg, _ = sg.box_blank(s["shape"])
                    cut = round(Lg + 2 * a, 1)
                    row.update(blank=(cut, None, None), mach=f"Cut to length: finished {Lg:.1f} mm + {a:g} mm per side = {cut} mm",
                               stock=f"{SL/1000:g} m standard length; cut piece {cut} mm")
                except Exception as e: self.ask("Tube length", f"{tc}: cannot read tube length from STEP ({e}).")
            else:
                self.ask("Tube length", f"{tc} ({desc}): no STEP model, tube cut length unknown.")
            return row
        s = stp.get(tc)
        if t is None and s:
            try:
                t = sg.box_blank(s["shape"])[0]
                row["spec"] = canon_spec(desc, t)
                self.ask("Thickness inferred", f"{tc} ({desc}): no thickness in the description; {t:.2f} mm taken from the STEP model. Confirm.")
            except Exception: t = None
        if t is None:
            self.ask("Thickness", f"{tc} ({desc}): thickness not found. Please give it.")
            row.update(cat="Raw material - unclassified", form="?", size="?", stock="?", mach="?", tube="n/a", area=None, blank=None, key=("?", tc))
            return row
        geo = None; ratio = None
        if s:
            try:
                if t < self.o["plate_max_mm"] + 1e-9:
                    geo = sg.unfold(s["shape"], t)
                    if geo and geo["bends"]:
                        ratio = geo["area"] / (sg.volume(s["shape"]) / t)
                        if abs(ratio - 1) > 0.05:
                            self.ask("Flat pattern check", f"{tc}: unfolded area is {ratio:.2f} x (volume / thickness); review the flat size manually.")
                        if geo["visited"] < geo["planes"] * 0.1 and False: pass
                    else:
                        geo = None
            except Exception as e:
                self.ask("Flat pattern failed", f"{tc}: unfold failed ({e}).")
        elif not it.get("blank_hint"):
            self.ask("No 3D model", f"{tc} ({desc}): not found in STEP, flat blank size cannot be calculated.")
        a = self.o["allowance_mm"]; SL = self.o["stock_len_mm"]
        steps = 0; T = W = Lg = None; fill = 1.0; rod = None
        hint = it.get("blank_hint")
        if not s and hint:                                   # no STEP: blank measured from the drawing view
            T, Lg, W = t, max(hint["L"], hint["W"]), min(hint["L"], hint["W"])
            row["note"].append(f"Blank {Lg:g} x {W:g} mm from the drawing ({hint['source']})" + ("" if hint["dimensioned"] else " - NOT dimensioned, confirm"))
            if not hint["dimensioned"]: self.ask("Blank size", f"{tc}: blank {Lg:g} x {W:g} mm was measured from the drawing outline only (no matching dimension text). Confirm.")
        if s and not geo:
            try:
                T, W, Lg, steps = sg.box_blank(s["shape"])
                fill = sg.volume(s["shape"]) / (T * W * Lg) if T * W * Lg else 1.0
                rod = sg.round_stock(s["shape"])
            except Exception as e: self.ask("Blank failed", f"{tc}: {e}")
        dim_txt = f'{t/IN:.3f}" ({t:.2f} mm) thk'
        # --- machined solid (plate / bar / rod): stepped, irregular, round, or thicker than the plate limit
        irregular = (not geo and T is not None and t >= self.o["sheet_max_mm"] and fill < self.o["fill_min"])
        machined = (not geo and T is not None and (t > self.o["plate_max_mm"] or steps > 2 or irregular or rod))
        if machined:
            why = ("round section" if rod else f"stepped ({steps} depth levels)" if steps > 2 else
                   f"irregular outline (fill {fill:.2f})" if irregular else f"thickness {t:.1f} mm > {self.o['plate_max_mm']:g} mm")
            at = 2 * a if self.o["allowance_thickness"] else 0
            if rod:
                D, Lr = rod
                blank = (round(Lr + 2 * a, 1), round(D + 2 * a, 1))
                row.update(cat="Raw material - rod (machined)", form="Round rod / bar", size=f"finished dia {D:.1f} x {Lr:.1f} mm",
                           stock=f"{SL/1000:g} m round rod, dia {blank[1]} mm; cut piece {blank[0]} mm",
                           mach=f"Machined ({why}); +{a:g} mm per side", tube="n/a", area=None, blank=blank,
                           key=("rod", canon_spec(desc, t), blank[1]))
                return row
            Tm = max(T, t) if t <= T * 1.5 else T
            blank = (round(Lg + 2 * a, 1), round(W + 2 * a, 1), round(Tm + at, 1))     # (length, width, thickness)
            ratio_s = W / Tm
            if ratio_s >= self.o["plate_ratio"]:
                row.update(cat="Raw material - solid plate (machined)", form="Solid plate, cut to size",
                           size=f"finished {Lg:.1f} x {W:.1f} x {Tm:.1f} mm; blank {blank[0]} x {blank[1]} x {blank[2]} mm",
                           stock=f"{fits_sheet(blank[0], blank[1]) or 'no standard sheet fits'} plate, thickness {blank[2]} mm",
                           mach=f"Machined ({why}); +{a:g} mm per side" + (" incl. thickness" if at else " on L & W"),
                           tube="n/a", area=blank[0] * blank[1], blank=(blank[0], blank[1]),
                           key=("splate", canon_spec(desc, t), blank[2]))
                if not fits_sheet(blank[0], blank[1]): self.ask("Oversize", f"{tc}: blank {blank[0]} x {blank[1]} mm does not fit a 4x8 or 5x10 ft plate.")
                row["imperial"] = bool(it.get("imperial")) or is_imperial_t(desc)
                if row["imperial"]:                                  # 0250-46767 Table 2: imperial plate -> stocked metric plate that covers the blank thickness
                    cover = [mm for _, _, mm, _ in xref.PLATE if mm >= blank[2] - 0.01]
                    if cover:
                        row["alt_mm"] = min(cover)
                        row["alt_text"] = f"{t/IN:.3f} in plate, blank {blank[2]:g} mm -> next stocked metric plate {min(cover):g} mm (0250-46767 Table 2)"
                        row["size"] += f"; metric alternate {min(cover):g} mm"
                    else:
                        row["alt_mm"] = None; row["alt_text"] = f"blank {blank[2]:g} mm is above the Table 2 plate range in 0250-46767"
            else:
                row.update(cat="Raw material - bar (machined)", form="Flat / square bar, cut to length",
                           size=f"finished {Lg:.1f} x {W:.1f} x {Tm:.1f} mm; section {blank[1]} x {blank[2]} mm",
                           stock=f"{SL/1000:g} m bar, section {blank[1]} x {blank[2]} mm; cut piece {blank[0]} mm",
                           mach=f"Machined ({why}); +{a:g} mm per side" + (" incl. thickness" if at else " on L & W"),
                           tube="n/a", area=None, blank=(blank[0], blank[1], blank[2]),
                           key=("bar", canon_spec(desc, t), blank[1], blank[2]))
            return row
        # --- sheet / plate (laser-cut, bent)
        imperial = bool(it.get("imperial")) or is_imperial_t(desc)
        if t < self.o["sheet_max_mm"]:
            cat, form = "Raw material - sheet metal", "Sheet"
        else:
            cat, form = "Raw material - plate (simple cut / bend)", "Plate"
        key = ("sheet", canon_spec(desc, t))
        if geo:
            blank = (round(geo["L"], 1), round(geo["W"], 1))
            row.update(cat=cat, form=form + ", formed", size=f"{dim_txt}; flat {blank[0]} x {blank[1]} mm",
                       mach=f"Cut + bend ({geo['bends']} bends)", tube="n/a", area=geo["area"], blank=blank, key=key)
            row["note"].append(f"Flat pattern from STEP (neutral axis K=0.5); area/volume check {ratio:.2f}")
        elif T is not None:
            blank = (round(max(Lg, W), 1), round(min(Lg, W), 1))
            row.update(cat=cat, form=form, size=f"{dim_txt}; flat {blank[0]} x {blank[1]} mm", mach="Cut",
                       tube="n/a", area=blank[0] * blank[1], blank=blank, key=key)
        else:
            row.update(cat=cat, form=form, size=dim_txt, mach="?", tube="n/a", area=None, blank=None, key=key)
            blank = None
        row["imperial"] = imperial
        if imperial:                                                 # 0250-46767: imperial sheet / plate -> alternate stocked metric thickness
            a_ = xref.alt_metric(t, "sheet" if t < self.o["sheet_max_mm"] else "plate", row["spec"])
            if a_:
                row["alt_mm"] = a_["metric"]; row["alt_text"] = a_["text"] + (f"; {a_['note']}" if a_["note"] else "")
                if a_["metric"] is not None: row["size"] += f"; metric alternate {a_['metric']:g} mm"
                if a_["metric"] is None: self.ask("No metric equivalent", f"{tc}: {a_['text']}. {a_['note']}")
            else:
                row["alt_mm"] = None; row["alt_text"] = "imperial size not listed in 0250-46767 Tables 1 / 2"
                self.ask("Metric alternate", f"{tc}: {t/IN:.4f} in ({t:.2f} mm) is not listed in 0250-46767 Tables 1 and 2 - choose the metric size manually.")
        if blank:
            nm = fits_sheet(*blank)
            row["stock"] = sheet_label(nm) if nm else "no standard sheet fits"
            if not nm: self.ask("Oversize", f"{tc}: flat blank {blank[0]} x {blank[1]} mm does not fit a 4x8 or 5x10 ft sheet.")
        else:
            row["stock"] = "?"
        return row

    # ---------------------------------------------------------- hardware
    def hw_part(self, parent, it, qty_parent, mult, step_cnt):
        pn, tc = it["pn"], it["tc"]
        if re.match(r"TC\d-\d+_", it["desc"]):
            fd = pp.form_desc(self.files(pn))
            if fd: it = dict(it, desc=fd)
        mt, note = self.mfr_text(pn, tc)
        row = dict(parent=parent, item=it["item"], part=f"{it['desc']} [{pn}]", mnum=mt, cat="Accessory / purchased hardware",
                   spec=it["desc"], form="Purchased", size="-", stock="-", mach="-", qty=qty_parent, ext=qty_parent * mult,
                   tube="n/a", area=None, blank=None, key=("hw", pn), note=[note] if note else [], pn=pn, tc=tc,
                   mfr_pns=[n for _, n in self.mfr(pn)])
        if step_cnt is not None and step_cnt != qty_parent:
            self.ask("PDF vs STEP quantity", f"{pn}: BOM qty {qty_parent} but STEP has {step_cnt} instances in {parent}. Which is right?")
        elif step_cnt is not None:
            row["note"].append(f"STEP count {step_cnt} matches BOM")
        return row

    # ---------------------------------------------------------- walk
    def process(self, pn, qty_parent, mult, label=None, depth=0):
        parent = label or pn
        files = self.files(pn)
        pdf = pp.main_pdf(files)
        stp = {}
        for f in files:
            if f.lower().endswith((".stp", ".step")):
                try: stp.update(sg.leaf_parts(f))
                except Exception as e: self.ask("STEP read", f"{os.path.basename(f)}: {e}")
        by_pn = {}
        for k, v in stp.items():
            m = re.search(r"-(\d{4}-\d{5}|\d{3}-\d{2}-\d{3})\b", v["name"])
            if m: by_pn[m.group(1)] = v
        bom = pp.parse_bom(pdf) if pdf else []
        for b in bom: self.tcmap[b["tc"]] = b
        if not bom and pdf and (not stp or len(stp) == 1):
            bom = self.from_drawing(pn, pdf, parent, mult, stp, depth)        # individual part drawing: title block / notes / views / table BOM
        if not bom and stp:
            self.ask("BOM not readable", f"{pn}: PDF has no readable BOM (3D/MBD drawing). Items taken from the STEP solids and instance counts - please confirm.")
            tcmap = self.tcmap
            for k, v in stp.items():
                nm = v["name"]; m = re.search(r"-(\d{4}-\d{5}|\d{3}-\d{2}-\d{3})\b", nm)
                if m and m.group(1) in {x["pn"] for x in tcmap.values()}: b = next(x for x in tcmap.values() if x["pn"] == m.group(1)); pnn = m.group(1)
                elif v["tc"] in tcmap and tcmap[v["tc"]]["pn"]: b = tcmap[v["tc"]]; pnn = b["pn"]
                elif m: pnn = m.group(1); b = {"pn": pnn, "desc": nm, "tc": v["tc"] or pnn}
                else: pnn = ""; b = {"pn": "", "desc": re.sub(r"^TC\d-\d+_\d+-", "", nm).replace("_", ","), "tc": v["tc"] or nm}
                bom.append(dict(item="STEP", qty=v["count"], pn=pnn, desc=b["desc"], tc=b["tc"]))
        if not bom and not stp and depth == 0 and not self.parts_from_drawing:
            self.ask("Parent BOM", f"{pn}: no readable BOM or STEP found in the uploaded files.")
        for it in bom:
            q = it["qty"]; ipn = it["pn"]; ifiles = self.files(ipn) if ipn else []
            if ipn and ifiles and self._is_assembly(ipn):
                ext = mult * q
                self.cover(parent, ipn, it["desc"], "Assembly", bool(pp.main_pdf(ifiles)), bool(pp.parse_form(ifiles)),
                           any(f.lower().endswith((".stp", ".step")) for f in ifiles), "Reviewed (components below)")
                self.parts.append(dict(parent=parent, item=it["item"], part=f"{it['desc']} {ipn}", mnum=f"{it['tc']} (AMAT TcENG)",
                    cat="Sub-assembly", spec=it["desc"], form="Fabricated assembly", size="-", stock="-", mach="See components below",
                    qty=q, ext=ext, tube="n/a", area=None, blank=None, key=("asm", ipn), note=["Components listed below, multiplied by qty"]))
                self.process(ipn, q, ext, label=f"{ipn} {it['desc']}", depth=depth + 1)
            elif ipn:
                cnt = None
                if stp: cnt = (by_pn.get(ipn) or stp.get(it["tc"]) or {}).get("count")
                has_form = bool(pp.parse_form(ifiles)); has_pdf = bool(pp.main_pdf(ifiles)) or any(f.lower().endswith(".pdf") for f in ifiles)
                has_step = any(f.lower().endswith((".stp", ".step")) for f in ifiles) or cnt is not None
                miss = [n for n, ok in (("supplier form / drawing", has_form or has_pdf), ("manufacturer data", has_form)) if not ok]
                self.cover(parent, ipn, it["desc"], "Purchased", has_pdf, has_form, has_step,
                           "OK" if not miss else "MISSING: " + ", ".join(miss))
                if not ifiles:
                    self.ask("Missing child files", f"{ipn} ({it['desc']}): no files uploaded for this child part.")
                self.parts.append(self.hw_part(parent, it, q, mult, cnt))
            else:
                r = self.raw_part(parent, it, q, mult, stp)
                has = it["tc"] in stp
                self.cover(parent, it["tc"], it["desc"], "Raw material part", False, False, has,
                           "OK (flat pattern from STEP)" if has else "MISSING: STEP model")
                if stp and has and stp[it["tc"]]["count"] != q:
                    self.ask("PDF vs STEP quantity", f"{it['tc']}: BOM qty {q} but STEP has {stp[it['tc']]['count']}.")
                self.parts.append(r)

    def from_drawing(self, pn, pdf, parent, mult, stp, depth):
        """Read material / thickness / blank / accessories from a single part drawing. Returns BOM-style items found in a table."""
        info = self.dwg(pdf)
        mat, t = info["material"], info["thickness"]
        table = [dict(item=b["item"], qty=b["qty"], pn=b["pn"] if b["pn"] != pn else "", desc=b["desc"], tc="") for b in info["bom"] if b["pn"] != pn]
        if not mat and not t and not info["hardware"] and not table:
            return []                                                          # nothing useful on this PDF (e.g. drawing is an image)
        self.parts_from_drawing += 1
        title = info["title"] or pn
        if not any(tuple(r["key"]) == ("top", pn) for r in self.parts): self.parts.append(dict(parent=parent, item=0, part=f"{title} {pn}", mnum=f"{pn} (drawing)", cat="Sub-assembly" if depth == 0 else "Sub-assembly",
                               spec=title, form="Part from drawing", size="-", stock="-", mach="Read from drawing: " + "; ".join(
                                   x for x in (f"material {mat}" if mat else "", f"thickness {t:g} mm ({info['thickness_src']})" if t else "", f"units {info['units']}, scale {info['scale']:g}") if x),
                               qty=1, ext=mult, tube="n/a", area=None, blank=None, key=("asm", pn),
                               note=[f"Finish: {info['finish']}"] if info["finish"] else []))
        if mat or t:
            if not mat: self.ask("Material", f"{pn}: no material found in the title block, notes or views. Please give it.")
            if not t: self.ask("Thickness", f"{pn}: no thickness found in the title block, notes or views. Please give it.")
            tc = next(iter(stp)) if len(stp) == 1 else pn
            desc = f"SHEET, MATERIAL: {mat or 'UNKNOWN'}" + (f" {t:.2f} MM THK" if t else "")
            it = dict(item="DWG", qty=1, pn="", desc=desc, tc=tc, blank_hint=info["blank"], imperial=(info["units"] == "in"))
            r = self.raw_part(parent if depth == 0 else f"{pn} {title}", it, 1, mult, stp)
            r["part"] = f"{title} [{pn}]"; r["mnum"] = f"{pn} (drawing; no mfr no.)"
            r["note"].append(f"Material / thickness from the drawing ({info['thickness_src'] or 'n/a'}); drawing units {info['units']}, scale {info['scale']:g}")
            self.parts.append(r)
            self.cover(parent, pn, title, "Part drawing", True, False, bool(stp), "OK (material from drawing)" if mat and t else "CHECK: material or thickness missing")
        for h in info["hardware"]:
            ex = h["pn"]
            row = dict(parent=parent, item="note", part=(f"{h['text']} [{ex}]" if ex else h["text"]), mnum=(f"{ex} (from drawing note)" if ex else "not specified on drawing"),
                       cat="Accessory / purchased hardware", spec=h["text"], form="Purchased", size="-", stock="-", mach="-", qty=h["qty"], ext=h["qty"] * mult,
                       tube="n/a", area=None, blank=None, key=("hw", ex or h["text"][:40]), note=["From a drawing note - confirm"] + ([] if h["qty_found"] else ["quantity not stated, 1 assumed"]),
                       pn=ex, tc="", mfr_pns=[ex] if ex else [])
            if not h["qty_found"]: self.ask("Accessory quantity", f"{pn}: '{h['text'][:70]}' - quantity not stated on the drawing; 1 assumed. Confirm.")
            self.parts.append(row)
        return table

    def dwg(self, pdf):
        if pdf not in self.cache: self.cache[pdf] = dr.parse_part_drawing(pdf)
        return self.cache[pdf]

    def _is_assembly(self, pn):
        files = self.files(pn)
        pdf = pp.main_pdf(files)
        if pdf and pp.parse_bom(pdf): return True
        if pdf and not any(f.lower().endswith((".stp", ".step")) for f in files):
            try:
                i = self.dwg(pdf)                      # a part drawing with its own material / table BOM is a part, not purchased hardware
                if i["material"] and i["thickness"] or i["bom"]: return True
            except Exception: pass
        for f in files:
            if f.lower().endswith((".stp", ".step")):
                try:
                    if len(sg.leaf_parts(f)) > 2: return True
                except Exception: pass
        return False

    def detect_top(self):
        """Parent = a part number whose drawing has a BOM and that is not a child of any other BOM."""
        kids = set(); parents = []
        for pn, fs in self.index.items():
            pdf = pp.main_pdf(fs)
            b = pp.parse_bom(pdf) if pdf else []
            if b:
                parents.append(pn); kids |= {x["pn"] for x in b if x["pn"]}
        tops = [p for p in parents if p not in kids]
        return tops

    # ---------------------------------------------------------- run
    def run(self, top_pn=None):
        if not top_pn:
            tops = self.detect_top()
            if not tops:
                loose = [p for p, fs in self.index.items() if pp.main_pdf(fs)]
                if not loose: raise ValueError("No drawing PDF with a recognisable part number in the file name was found.")
                if len(loose) > 1:
                    self.parts.append(dict(parent="(top level)", item=0, part="INDIVIDUAL PARTS", mnum="-", cat="Top-level assembly", spec="parts", form="Part list", size="-", stock="-",
                                           mach="-", qty=1, ext=self.o["assemblies"], tube="n/a", area=None, blank=None, key=("top", "PARTS"), note=[f"{len(loose)} part drawings uploaded"]))
                    for p in loose: self.process(p, 1, self.o["assemblies"], label=p)
                    return self._finish("PARTS")
                tops = loose
            if len(tops) > 1: self.ask("Several parents", f"More than one top-level assembly found ({', '.join(tops)}); using {tops[0]}. Enter the part number to choose another.")
            top_pn = tops[0]
        if top_pn not in self.index: raise ValueError(f"No files for parent part number {top_pn}.")
        self.process_top(top_pn)
        return self._finish(top_pn)

    def _finish(self, top_pn):
        self.apply_usage()
        self.apply_inventory()
        xl = pp.excel_bom(self.root)
        if not xl:
            self.ask("Excel BOM", "No Excel BOM uploaded - accessories were cross-checked against the PDF BOMs and STEP counts only.")
        else:
            for r in self.parts:
                if r.get("pn") in xl and xl[r["pn"]] != r["qty"]:
                    self.ask("Excel vs PDF BOM", f"{r['pn']}: Excel qty {xl[r['pn']]} vs PDF qty {r['qty']}.")
        if self.inv:
            i = self.inv_info()
            rows = [r for r in self.parts if r["key"][0] not in ("top", "asm")]
            hw = [r for r in rows if r["key"][0] == "hw"]; raw = [r for r in rows if r["key"][0] != "hw"]
            nm_hw = sorted({r["key"][1] for r in hw if not r.get("inv")})
            nm_raw = sorted({r["spec"] for r in raw if not r.get("inv")})
            self.ask("Inventory reference", f"Matched against Inventory Dashboard data refreshed {i.get('refreshed_at')} ({'fresh' if i['fresh'] else 'NOT refreshed in this run - use Refresh inventory'}). "
                     f"Accessories matched: {len({r['key'][1] for r in hw if r.get('inv')})} of {len({r['key'][1] for r in hw})}. "
                     f"Raw material rows matched: {len([r for r in raw if r.get('inv')])} of {len(raw)}. "
                     f"Not found (current specification kept): accessories {', '.join(nm_hw) or 'none'}; raw material {'; '.join(s[:40] for s in nm_raw) or 'none'}.")
        else:
            self.ask("Inventory reference", "No inventory export found (material.MD source) - materials were not matched.")
        if any(r.get("alt_text") for r in self.parts):
            self.ask("Metric alternates (0250-46767)", "Sheet and plate sizes given in imperial were converted to the alternate stocked metric thickness from 0250-46767 Rev 06 Appendix A Tables 1 and 2 (transcribed from the table images; the live 0250-46767.xlsx was not available). Bars, tubes and machined solids are not converted. Add 'ALTERNATE MATERIALS ALLOWED PER 0250-46767' to the drawing notes where the supplier may use the alternate. Confirm.")
        self.ask("Machined parts", f"Machined parts (stepped, irregular outline, round, or thicker than {self.o['plate_max_mm']:g} mm) get {self.o['allowance_mm']:g} mm on every side and are bought as solid plate (W/T >= {self.o['plate_ratio']:g}), bar or rod. Bars and rods use {self.o['stock_len_mm']/1000:g} m stock lengths with pieces nested. Confirm, and tell me the standard bar / rod sections you stock if the blank should snap to them.")
        self.ask("Hollow tube", f"Hollow tube is cut from {self.o['stock_len_mm']/1000:g} m standard lengths with {self.o['tube_allow_mm']:g} mm added each end; saw kerf {'%g mm' % self.o['kerf_mm'] if self.o['kerf_mm'] else 'not included'}. Confirm.")
        self.ask("Sheet nesting", f"Sheets and plates of the same material and thickness are nested together ({self.o['nest_gap_mm']:g} mm between parts) on the standard size (4x8 or 5x10 ft) that uses the least material. Confirm the gap.")
        self.ask("Boundary rule", f"Sheet: t < {self.o['sheet_max_mm']} mm; plate: {self.o['sheet_max_mm']}-{self.o['plate_max_mm']} mm (exactly {self.o['sheet_max_mm']} mm treated as plate); bar/solid above {self.o['plate_max_mm']} mm or when stepped/machined. Confirm.")
        res = dict(top=top_pn, options=self.o, parts=self.parts, coverage=self.coverage, inventory=self.inv_info(),
                   consolidated=consolidate(self.parts, top_pn, self.o["stock_len_mm"], self.o["kerf_mm"], self.o["nest_gap_mm"], self.inv), questions=[dict(topic=t, text=x) for t, x in self.q])
        return res

    # ---------------------------------------------------------- inventory reference (material.MD)
    def inv_info(self):
        if not self.inv: return dict(available=False, note="No inventory export loaded - nothing matched.")
        i = dict(self.inv.info); i["fresh"] = self.inv.fresh
        return i

    def apply_usage(self):
        """Exact decimal material usage per part row (area in m2 / fraction of a sheet, length in m / fraction of a stock length)."""
        SL = self.o["stock_len_mm"]
        for r in self.parts:
            k = r["key"][0]; q = r["ext"]
            if k in ("top", "asm"): continue
            if k == "hw": r["usage"] = f"{q:g}"
            elif k in ("sheet", "splate") and r.get("area") and r.get("blank"):
                nm = fits_sheet(*r["blank"][:2]); a = r["area"] * q
                r["usage"] = f"{a / 1e6:.4f} m2" + (f" = {a / (SHEET_SIZES[nm][0] * SHEET_SIZES[nm][1]):.4f} x {nm} sheet" if nm else "")
            elif k in ("bar", "rod", "tube") and r.get("blank"):
                L = r["blank"][0] * q
                r["usage"] = f"{L / 1000:.4f} m = {L / SL:.4f} x {SL / 1000:g} m length"
            else: r["usage"] = ""

    def apply_inventory(self):
        """Match every part row to the Inventory Dashboard export; unmatched rows keep their current specification."""
        inv = self.inv
        if not inv: return
        for r in self.parts:
            k = r["key"][0]
            if k in ("top", "asm"): continue
            m = match_group(inv, k, r["key"], r["spec"], r.get("blank"), r.get("mfr_pns"), r.get("stock", ""), r.get("alt_mm"), r.get("imperial"))
            r["inv"] = m; r["inv_text"] = inv_text_for(inv, m, k, r["key"], r["spec"], r.get("stock", ""), r.get("alt_mm"), r.get("imperial")); r["inv_stock"] = inv.stock(m)

    def process_top(self, top_pn):
        top_row = dict(parent="(top level)", item=0, part=f"ASSEMBLY {top_pn}", mnum="-", cat="Top-level assembly", spec=top_pn,
                       form="Assembly", size="-", stock="-", mach="-", qty=1, ext=self.o["assemblies"], tube="n/a", area=None, blank=None,
                       key=("top", top_pn), note=[f"{self.o['assemblies']} assembly(ies) requested"])
        self.parts.append(top_row)
        self.process(top_pn, 1, self.o["assemblies"], label=top_pn)


def inv_text_for(inv, m, kind, key, spec, stock_txt, alt_mm, imperial):
    """Text shown for the inventory match. Imperial sheet / plate whose 0250-46767 metric alternate is not stocked says so."""
    if m or kind not in ("sheet", "splate") or not imperial or not alt_mm: return inv.text(m)
    txt = f"Not in inventory - current specification kept (0250-46767 alternate {alt_mm:g} mm not stocked"
    try:
        t = thickness_mm(spec)
        nm = re.match(r"(\d+) x (\d+) ft", (stock_txt or "").lower()); size = f"{nm.group(1)}x{nm.group(2)}" if nm else "4x8"
        hint = inv.imperial_hint(spec, t, size) if kind == "sheet" and t else None
        if hint: txt += f"; the drawing's imperial size is stocked as {hint}"
    except Exception: pass
    return txt + ")"


def match_group(inv, kind, key, spec, blank, mfr_pns=None, stock_txt="", alt_mm=None, imperial=False):
    """One inventory lookup for a part row / consolidated group."""
    try:
        if kind == "hw": return inv.match_part(key[1], mfr_pns or [])
        if kind == "sheet":
            t = thickness_mm(spec); nm = (re.match(r"(\d+) x (\d+) ft", stock_txt.lower()) or None)
            size = f"{nm.group(1)}x{nm.group(2)}" if nm else None
            if not t: return None
            for sz in ([size] if size else ["4x8", "5x10"]):        # blank unknown (no STEP): try the standard sizes
                m = inv.match_sheet(spec, t, sz, True, alt_mm, bool(imperial))
                if m and m["quality"] != "Different size":
                    if not size: m["note"] = (m["note"] + "; " if m["note"] else "") + f"sheet size not known (no 3D model) - {sz} assumed"
                    return m
            return None
        if kind == "splate":
            nm = re.match(r"(\d+) x (\d+) ft", stock_txt.lower()); size = f"{nm.group(1)}x{nm.group(2)}" if nm else None
            return inv.match_sheet(spec, alt_mm or key[2], size, False, alt_mm, bool(imperial)) if size else None
        if kind == "bar" and blank: return inv.match_bar(spec, blank[2], blank[1])
        if kind == "rod" and blank: return inv.match_rod(spec, blank[1])
        if kind == "tube": return inv.match_tube(spec)
    except Exception:
        return None
    return None


def bar_nest(cuts, stock_len, kerf=0.0):
    """First-fit-decreasing 1-D nesting of cut lengths onto standard bars/tubes.
    Returns (bars used, pieces longer than stock, total cut length used, per-bar piece lists)."""
    bars = []; over = 0; used = 0.0
    for c in sorted(cuts, reverse=True):
        if c > stock_len: over += 1; continue
        for b in bars:
            if b["free"] >= c + (kerf if b["pcs"] else 0):
                b["free"] -= c + (kerf if b["pcs"] else 0); b["pcs"].append(c); break
        else:
            bars.append({"free": stock_len - c, "pcs": [c]})
        used += c
    return len(bars), over, used, [b["pcs"] for b in bars]


def consolidate(parts, default_run="", stock_len=3000.0, kerf=0.0, gap=5.0, inv=None):
    """Group equal materials / hardware across all part rows (and across runs when rows carry a 'run' label)."""
    cons = OrderedDict()
    for r in parts:
        k = tuple(r["key"])
        if k[0] in ("top", "asm"): continue
        run = r.get("run") or default_run
        c = cons.setdefault(k, dict(cat=r["cat"], spec=r["spec"], form=r["form"], size=r["size"], stock=r["stock"], tube=r["tube"],
                                   total=0, rel=[], notes=[], blanks=[], area=0.0, byrun=OrderedDict(), mfr=[], alt_mm=None, alt_text="", imperial=False))
        c["mfr"] += [x for x in (r.get("mfr_pns") or []) if x not in c["mfr"]]
        if r.get("alt_text") and not c["alt_text"]: c["alt_mm"], c["alt_text"] = r.get("alt_mm"), r["alt_text"]
        c["imperial"] = c["imperial"] or bool(r.get("imperial"))
        c["total"] += r["ext"]; c["byrun"][run] = c["byrun"].get(run, 0) + r["ext"]
        tag = f"[{r['run']}] " if r.get("run") else ""
        c["rel"].append(f"{tag}{r['mnum'].split(' (')[0] if r['cat'].startswith('Raw') else r['mnum']} x{r['ext']}")
        if r.get("blank") is not None: c["blanks"] += [tuple(r["blank"])] * r["ext"]
        if r.get("area"): c["area"] += r["area"] * r["ext"]
        for n in r["note"]:
            if n and n not in c["notes"] and not n.startswith("STEP count") and not n.startswith("Flat pattern"): c["notes"].append(n)
    rows = []
    for k, c in cons.items():
        qty = c["total"]; util = []; usage = ""; need = c["total"] if k[0] == "hw" else None; blank0 = next(iter(c["blanks"]), None)
        if k[0] in ("sheet", "splate"):
            bl = [(b[0], b[1]) for b in c["blanks"]]
            area = c["area"] or sum(a * b for a, b in bl)
            best = None
            for nm, sz in SHEET_SIZES.items():
                n, un, _ = nest_rects(bl, sz, gap)
                if un == len(bl): continue
                cand = (un > 0, n * sz[0] * sz[1], nm, n, un)       # fewer problems, then least material area
                if best is None or cand < best: best = cand
            if best:
                _, tot, nm, n, un = best; sz = SHEET_SIZES[nm]; need = n if n > 1 else min(1.0, area / (sz[0] * sz[1]))   # one sheet: need only the area used
                c["stock"] = sheet_label(nm)
                qty = f"{n} x {nm.replace('x', ' x ')} sheet(s) for {len(bl)} blank(s); part area {area/(IN*IN):,.0f} in² ({area/1e6:.2f} m²)"
                util.append(f"{area / tot * 100:.2f} % ({area/1e6:.4f} m² of {tot/1e6:.4f} m²)")
                usage = f"{area/1e6:.4f} m² = {area / (sz[0] * sz[1]):.4f} x {nm.replace('x', ' x ')} sheet"
                c["notes"].append(f"All blanks of the same material and thickness nested together (MaxRects, {gap:g} mm between parts); "
                                  f"{sheet_label(nm)} chosen as the least material. Final nesting in CAM.")
                if un: c["notes"].append(f"{un} blank(s) do not fit this sheet size.")
            else:
                qty = f"{len(bl)} blank(s) - none fit a standard sheet"; util.append("n/a")
        elif k[0] in ("bar", "rod", "tube"):
            cuts = [b[0] for b in c["blanks"]]
            std = f"{stock_len / 1000:g} m"
            if k[0] == "bar":
                sec = next(iter(c["blanks"]), (0, 0, 0))
                c["size"] = f"section {sec[1]} x {sec[2]} mm"; c["stock"] = f"{std} bar, section {sec[1]} x {sec[2]} mm"
            elif k[0] == "rod":
                sec = next(iter(c["blanks"]), (0, 0))
                c["size"] = f"dia {sec[1]} mm"; c["stock"] = f"{std} round rod, dia {sec[1]} mm"
            else:
                c["stock"] = f"{std} standard tube length"
            if cuts:
                nb, over, used, bars = bar_nest(cuts, stock_len, kerf)
                cutlist = "; ".join(f"{cuts.count(x)} x {x:g} mm" for x in sorted(set(cuts), reverse=True))
                qty = f"{nb} x {std} length(s) for {len(cuts)} pcs ({cutlist})" + (f"; {over} piece(s) longer than {std}" if over else "")
                util.append(f"{used / (nb * stock_len) * 100:.2f} % ({used/1000:.4f} m of {nb * stock_len/1000:.4f} m)" if nb else "-")
                usage = f"{used/1000:.4f} m = {used / stock_len:.4f} x {std} length"
                c["notes"].append(f"Pieces of the same section nested along the {std} length; unused {nb * stock_len - used:,.0f} mm."
                                  + (f" Kerf {kerf:g} mm per cut included." if kerf else " Saw kerf not included."))
                if over: c["notes"].append(f"{over} piece(s) exceed the standard length.")
            else:
                qty = f"{c['total']} pcs (cut length unknown - no STEP)"; util.append("n/a")
        m = match_group(inv, k[0], k, c["spec"], blank0, c["mfr"], c["stock"], c["alt_mm"], c["imperial"]) if inv else None
        itxt = inv_text_for(inv, m, k[0], k, c["spec"], c["stock"], c["alt_mm"], c["imperial"]) if inv else "Inventory not loaded"; istk = inv.stock(m) if inv else ""
        if m and need is not None:
            short = need - m["available"]
            istk += f" | need {need:.4f}{' sheet(s)' if k[0] in ('sheet', 'splate') else ''}: " + ("SHORT by %.4f" % short if short > 1e-9 else "covered by available stock")
        rows.append(dict(cat=c["cat"], spec=c["spec"], form=c["form"], size=c["size"], stock=c["stock"], tube=c["tube"], total=qty,
                         inv=m, inv_text=itxt, inv_stock=istk, alt_text=c["alt_text"],
                         util="; ".join(util), usage=usage or (f"{c['total']:g}" if k[0] == "hw" else ""),
                         byrun="; ".join(f"{a or '-'}: {b}" for a, b in c["byrun"].items()),
                         rel="; ".join(c["rel"]), notes="; ".join(c["notes"])))
    if not any(r["cat"].endswith("hollow tube") for r in rows):
        rows.append(dict(cat="Raw material - hollow tube", spec="-", form="-", size="-", stock=f"{stock_len / 1000:g} m standard length", tube="none found",
                         total=0, util="-", usage="0", byrun="-", rel="-", inv=None, inv_text="-", inv_stock="", alt_text="", notes="No hollow tube in BOM/STEP; US/Local rule not applicable."))
    order = lambda r: (0 if "sheet metal" in r["cat"] else 1 if "plate (simple" in r["cat"] else 2 if "solid plate" in r["cat"] else 3 if "bar" in r["cat"] else 4 if "rod" in r["cat"] else 5 if "tube" in r["cat"] else 6)
    rows.sort(key=order)
    return rows
