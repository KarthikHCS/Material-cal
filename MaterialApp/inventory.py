"""Inventory reference (material.MD): read-only lookup of the Inventory Dashboard v1.7 CSV exports.

The dashboard / CSVs are never edited. refresh() only runs the user's refresh_materials.ps1, which works on a temp copy
of the dashboard; if it fails nothing is exported and the previous CSVs stay (flagged as stale, never as fresh).
"""
import csv, io, json, os, re, subprocess, sys, time

IN = 25.4
BASE = os.path.join(os.path.expanduser("~"), "Documents", "MaterialUsageCalculator")
MAT_DIR = os.path.join(BASE, "materials")
SCRIPT = os.path.join(BASE, "refresh_materials.ps1")
GAUGE = {7: 4.55, 8: 4.18, 10: 3.43, 11: 3.04, 12: 2.67, 13: 2.28, 14: 1.9, 16: 1.52, 18: 1.2}
BAD = re.compile(r"CHECKER|PERFORAT|GASKET|FOAM|PVC|POLYC|CANVAS|LASER CUT|CUTTING|MACHIN|OIL PLATE|WELD|ASSY|ASSEMBLY|BRACKET", re.I)
_cache = {"mtime": None, "inv": None}


# ------------------------------------------------------------------ parsing helpers
def _num(s):
    s = s.strip()
    if "/" in s:
        a, b = s.split("/"); return float(a) / float(b)
    return float(s)


def _unit(u, default=1.0):
    u = (u or "").upper().replace('"', "IN")
    return {"MM": 1.0, "M": 1000.0, "FT": 304.8, "IN": 25.4, "CM": 10.0}.get(u, default)


def sheet_size(d):
    d = d.upper()
    if re.search(r"(?<![\d.])4\s*(?:FT|')?\s*X\s*8\s*(?:FT|')?(?!\d)|\b1219\s*[X-]\s*2438|\b122\d\s*X\s*24[34]\d", d): return "4x8"
    if re.search(r"(?<![\d.])5\s*(?:FT|')?\s*X\s*10\s*(?:FT|')?(?!\d)|\b1524\s*[X-]\s*3048|\b1500\s*X\s*3000", d): return "5x10"
    return None


def thickness_mm(d):
    d = d.upper()
    m = re.search(r"(?<![\d.])(\d*\.\d+)\s*(?:INCH|IN\b|\")", d)
    if m: return float(m.group(1)) * IN
    m = re.search(r"\bT\s*(\d+/\d+|\d+(?:\.\d+)?)\s*(MM|IN|\")?(?=\s*[X*]|\s*$|\s)", d)
    if m: return _num(m.group(1)) * _unit(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*MM\s*(?:THK|THICK)|THK\.?\s*(\d+(?:\.\d+)?)\s*MM", d)
    if m: return float(m.group(1) or m.group(2))
    m = re.search(r"\b(\d{1,2})\s*GA\b", d)
    if m and int(m.group(1)) in GAUGE: return GAUGE[int(m.group(1))]
    return None


def flat_dims(d):
    """(T, W, L) mm of a flat/square bar from 'T25MM X 150MM X 3657.6MM', 'T1INx4INx12FT', 'T1/2"x 1"x 6M' ..."""
    m = re.search(r"T\s*(\d+/\d+|\d+(?:\.\d+)?)\s*(MM|IN|\")?\s*[X*]\s*W?\s*(\d+/\d+|\d+(?:\.\d+)?)\s*(MM|IN|\")?\s*[X*]\s*L?\s*(\d+(?:\.\d+)?)\s*(MM|M|FT|IN|\")?", d.upper())
    if not m: return None
    u3 = _unit(m.group(6), 1.0)
    u2 = _unit(m.group(4), _unit(m.group(2), 1.0))
    u1 = _unit(m.group(2), u2)
    return _num(m.group(1)) * u1, _num(m.group(3)) * u2, float(m.group(5)) * u3


def rod_dims(d):
    d = d.upper().replace("�", "D").replace("Ø", "D").replace("⌀", "D")
    m = re.search(r"(?:\bD|DIA\.?|\bN)\s*(\d+(?:\.\d+)?)\s*(MM|IN|\")?", d)
    if not m: return None
    dia = float(m.group(1)) * _unit(m.group(2))
    l = re.search(r"[XL]\s*(\d+(?:\.\d+)?)\s*(MM|M|FT)\b", d[m.end():])
    return dia, (float(l.group(1)) * _unit(l.group(2)) if l else None)


def tags(d):
    d = d.upper(); t = set()
    if re.search(r"HRS|HOT ROLL|A1011|A569|\bHR\b", d): t.add("HRS")
    if re.search(r"\bMS\b|MILD|A36|S275|SS400|Q235|\bMSP", d): t.add("MS")
    if re.search(r"CRS|COLD|1018|SPCC|S45C|C45|CARBON", d): t.add("CRS")
    if re.search(r"SUS|SS ?30|SS ?31|SST|STAINLESS|SS304|SS316", d): t.add("SS")
    if re.search(r"ALU|AL ?6|AL ?5|5052|6061", d): t.add("AL")
    if re.search(r"\bEG\b|EGP|SECC|ELECTRO ?GALV", d): t.add("EG")
    if re.search(r"GALV|\bGI\b|\bGIS\b|ZINC", d) and "EG" not in t: t.add("COATED")
    return t


def accepted(spec, thin=False):
    """Which inventory material tags satisfy a required spec -> {tag: 'Exact'|'Equivalent'}.
    Stainless -> SUS 304 only. Steel sheet (< 6 mm) -> CRS or EG. Thicker steel plate / bar -> CRS, A36 / mild steel."""
    s = spec.upper(); a = {}
    if re.search(r"SUS|SST|STAINLESS|\bSS ?3\d\d|\b30[14]\b|\b316\b|18-8", s): return {"SS": "Exact"}
    if re.search(r"ALU|\bAL ?\d{4}|6061|5052", s): return {"AL": "Exact"}
    if thin or not re.search(r"A36|1018|S275|HRS|A1011|A569", s):
        return {"CRS": "Exact", "EG": "Exact"}
    if re.search(r"HRS|A1011|A569|HOT ROLL", s): a["MS"] = "Equivalent"; a["HRS"] = "Exact"
    if "A36" in s: a["MS"] = "Exact"
    if "1018" in s or re.search(r"\bCR\b", s): a["CRS"] = "Exact"
    return a or {"CRS": "Exact", "MS": "Equivalent"}


# ------------------------------------------------------------------ inventory object
class Inventory:
    def __init__(self, rows, xref, info, fresh):
        self.rows = rows; self.info = info; self.fresh = fresh
        self.by_gss = {}; self.by_pn = {}
        for r in rows:
            self.by_gss.setdefault(r["GSS_PART"].strip(), r); self.by_pn.setdefault(r["PART_NUMBER"].strip(), r)
        self.xref = {}
        for x in xref: self.xref.setdefault(x["CUSTOMER_PART"].strip(), []).append(x["INVENTORY_PART"].strip())
        self._sheets = None; self._bars = None; self._rods = None

    # -- summaries
    def view(self, r, method, quality, note=""):
        def f(k):
            try: return float(r[k] or 0)
            except Exception: return 0.0
        return dict(gss=r["GSS_PART"].strip(), pn=(r["PART_NUMBER"].strip() or r["GSS_PART"].strip()), desc=" ".join(r["DESCRIPTION"].split()), uom=r["UM_INV"], onhand=f("QTY_ONHAND"),
                    available=f("QTY_AVAILABLE"), onpo=f("QTY_ONPO"), inactive=r["INACTIVE"].strip().upper() == "Y",
                    bin=r["BIN"], lead=r["LEAD_TIME"], method=method, quality=quality, note=note, fresh=self.fresh)

    @staticmethod
    def text(m):
        if not m: return "Not in inventory - current specification kept"
        s = f"{m['pn']} - {m['desc'][:90]} [{m['quality']}; {m['method']}]"
        return s + (f" - {m['note']}" if m.get("note") else "")

    @staticmethod
    def stock(m):
        if not m: return ""
        return f"{m['uom']}: on hand {m['onhand']:g} / available {m['available']:g} / on PO {m['onpo']:g}" + (" / INACTIVE" if m["inactive"] else "")

    # -- accessories
    def match_part(self, pn, mfr_pns=()):
        for p in (pn,):
            r = self.by_gss.get(p) or self.by_pn.get(p)
            if r: return self.view(r, "AMAT part no. = inventory part", "Exact")
        for g in self.xref.get(pn, []):
            r = self.by_gss.get(g) or self.by_pn.get(g)
            if r:
                alts = len(self.xref[pn]) - 1
                return self.view(r, "customer-part cross-reference", "Exact", f"{alts} other cross-referenced item(s)" if alts else "")
        for mp in mfr_pns:
            mp = mp.strip()
            if len(mp) < 4: continue
            r = self.by_gss.get(mp) or self.by_pn.get(mp)
            if r: return self.view(r, f"manufacturer part {mp}", "Exact")
            pat = re.compile(r"(?<![A-Z0-9])" + re.escape(mp.upper()) + r"(?![A-Z0-9])")
            c = [x for x in self.rows if pat.search(x["GSS_PART"].upper()) or pat.search(x["PART_NUMBER"].upper())]
            if c:
                c.sort(key=lambda x: (x["INACTIVE"].strip().upper() == "Y", -float(x["QTY_ONHAND"] or 0)))
                return self.view(c[0], f"manufacturer part {mp} (contained in inventory part)", "Equivalent")
        return None

    # -- raw material
    def _pool(self, kind):
        if self._sheets is None:
            self._sheets, self._bars, self._rods = [], [], []
            for r in self.rows:
                d = r["DESCRIPTION"]
                if r["PROD_LINE"] not in ("RM", "TU") or BAD.search(d): continue
                tg = tags(d)
                if r["UM_INV"] == "SH" and (sz := sheet_size(d)) and (t := thickness_mm(d)): self._sheets.append((r, tg, sz, t))
                if re.search(r"FLAT BAR|SQ(UARE)?\.? BAR|\bFB\b", d, re.I) and (fd := flat_dims(d)): self._bars.append((r, tg, fd))
                if re.search(r"ROD|ROUND BAR|RND BAR", d, re.I) and (rd := rod_dims(d)): self._rods.append((r, tg, rd))
        return {"sheet": self._sheets, "bar": self._bars, "rod": self._rods}[kind]

    @staticmethod
    def _rank(r, q):
        return (q != "Exact", r["INACTIVE"].strip().upper() == "Y", -float(r["QTY_ONHAND"] or 0))

    def match_sheet(self, spec, t, size, exact_t=True, alt_mm=None, imperial=False):
        """t in mm. Imperial sheet / plate sizes follow 0250-46767: only the stocked METRIC ALTERNATE is matched (no nearest-thickness
        substitution). If the document lists no metric size for it, only the drawing's own size is matched.
        Sizes given in metric: nearest stocked thickness for sheet (+/-0.6 mm)."""
        if not imperial:
            return self._match_sheet(spec, t, size, exact_t, False)
        if alt_mm:
            m = self._match_sheet(spec, alt_mm, size, exact_t, True)
            if m: m["note"] = (m["note"] + "; " if m["note"] else "") + f"metric alternate {alt_mm:g} mm per 0250-46767 (drawing {t / 25.4:.3f} in)"
            return m
        m = self._match_sheet(spec, t, size, exact_t, True)
        if m: m["note"] = (m["note"] + "; " if m["note"] else "") + "no metric alternate in 0250-46767 - drawing size stocked"
        return m

    def imperial_hint(self, spec, t, size, exact_t=True):
        """Item stocked in the drawing's own imperial size (reported only as information when the metric alternate is not stocked)."""
        m = self._match_sheet(spec, t, size, exact_t, True)
        return m["pn"] if m else None

    def _match_sheet(self, spec, t, size, exact_t, strict):
        thin = exact_t and t < 6.0
        acc = accepted(spec, thin); best = None; other = []
        for r, tg, sz, ti in self._pool("sheet"):
            q = next((acc[x] for x in tg if x in acc), None)
            if not q: continue
            if "SS" in acc and "304" not in r["DESCRIPTION"]: continue
            dt = abs(ti - t)
            ok = (dt <= 0.12) if strict else (dt <= 0.6) if thin else (dt <= max(0.12, 0.03 * t) if exact_t else (ti >= t - 0.01 and ti <= t + 8))
            if not ok: continue
            if sz != size: other.append((r, sz)); continue
            pref = 0 if "CRS" in tg or "SS" in tg else 1
            key = (round(dt, 1) if thin else 0,) + self._rank(r, q) + (pref, dt)
            if best is None or key < best[0]: best = (key, r, q, ti, dt)
        if best:
            _, r, q, ti, dt = best
            fam = "SUS 304" if "SS" in acc else "aluminium" if "AL" in acc else "steel (CRS/EG)" if thin else "material"
            qual = ("Exact" if dt <= 0.12 else "Equivalent") if thin else q
            note = (f"required {t:.2f} mm -> stocked {ti:g} mm (difference {ti - t:+.2f} mm)" if thin and dt > 0.12 else
                    "" if exact_t else f"stock thickness {ti:g} mm (blank needs {t:g} mm)")
            return self.view(r, f"{fam}, {ti:.2f} mm, {size} sheet", qual, note)
        if other:
            r, sz = other[0]
            return dict(self.view(r, "same material/thickness, other sheet size", "Different size", f"stocked as {sz}, required {size}"))
        return None

    def match_bar(self, spec, T, W):
        acc = accepted(spec); best = None
        if "SS" in acc: acc = {"SS": "Exact"}
        for r, tg, (t, w, l) in self._pool("bar"):
            q = next((acc[x] for x in tg if x in acc), None)
            if not q: continue
            a, b = sorted((t, w)); ra, rb = sorted((T, W))
            if a + 0.01 >= ra and b + 0.01 >= rb:
                key = self._rank(r, q)[:1] + (l < 2990, a * b, r["INACTIVE"].strip().upper() == "Y")
                if best is None or key < best[0]: best = (key, r, q, t, w, l)
        if not best: return None
        _, r, q, t, w, l = best
        return self.view(r, "smallest stocked flat bar that fits", "Nearest larger" if (abs(t - T) > 0.5 or abs(w - W) > 0.5) else q,
                         f"stock section {t:g} x {w:g} mm, length {l:g} mm" + ("" if l >= 2990 else " (shorter than 3 m)"))

    def match_rod(self, spec, D):
        acc = accepted(spec); best = None
        for r, tg, (d, l) in self._pool("rod"):
            q = next((acc[x] for x in tg if x in acc), None)
            if q and d + 0.01 >= D and (best is None or (d, self._rank(r, q)) < best[0]): best = ((d, self._rank(r, q)), r, q, d, l)
        if not best: return None
        _, r, q, d, l = best
        return self.view(r, "smallest stocked rod that fits", "Nearest larger" if abs(d - D) > 0.5 else q, f"stock dia {d:g} mm" + (f", length {l:g} mm" if l else ""))

    def match_tube(self, desc):
        nums = [round(float(x) * (25.4 if '"' in desc or " IN" in desc.upper() else 1), 1) for x in re.findall(r"\d+(?:\.\d+)?", desc)][:4]
        if not nums: return None
        for r in self.rows:
            d = r["DESCRIPTION"].upper()
            if r["PROD_LINE"] in ("RM", "TU") and "TUBE" in d and not BAD.search(d):
                found = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", d)]
                if all(any(abs(n - f * k) <= 0.6 for f in found for k in (1, 25.4)) for n in nums):
                    return self.view(r, "tube size match", "Equivalent")
        return None


# ------------------------------------------------------------------ loading / refresh
def info(mat_dir=MAT_DIR):
    try:
        i = json.load(open(os.path.join(mat_dir, "refresh_info.json"), encoding="utf-8-sig"))
    except Exception:
        return dict(available=False, error="No inventory export found in " + mat_dir)
    try: age_h = (time.time() - time.mktime(time.strptime(i["refreshed_at"], "%Y-%m-%d %H:%M:%S"))) / 3600
    except Exception: age_h = None
    return dict(available=True, refreshed_at=i.get("refreshed_at"), rows=i.get("rows", {}), source=i.get("source"), age_hours=age_h, dir=mat_dir)


def _read(path):
    return list(csv.DictReader(io.StringIO(open(path, encoding="utf-8-sig", errors="replace").read().replace("\0", ""))))


def load(mat_dir=MAT_DIR, fresh=False):
    """Load the CSV export (cached; reloaded when the file changes). Returns None if no export exists."""
    p = os.path.join(mat_dir, "Inventory_All.csv")
    if not os.path.exists(p): return None
    mt = (os.path.getmtime(p), fresh)
    if _cache["mtime"] != mt:
        cp = os.path.join(mat_dir, "Customer_Part.csv")
        _cache["inv"] = Inventory(_read(p), _read(cp) if os.path.exists(cp) else [], info(mat_dir), fresh)
        _cache["mtime"] = mt
    return _cache["inv"]


def refresh(source=None, script=SCRIPT, timeout=900):
    """Run refresh_materials.ps1 (uses a temp copy of the dashboard; the dashboard itself is never saved)."""
    before = info().get("refreshed_at")
    if not os.path.exists(script): return dict(ok=False, error="Refresh script not found: " + script)
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
    if source: cmd += ["-Source", source]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return dict(ok=False, error=f"Refresh timed out after {timeout}s; previous export kept.")
    after = info()
    ok = p.returncode == 0 and after.get("refreshed_at") and after["refreshed_at"] != before and after.get("rows", {}).get("Inventory_All", 0) > 0
    err = [l.strip() for l in (p.stderr or p.stdout or "").splitlines()
           if l.strip() and not l.strip().startswith(("+", "At ", "CategoryInfo", "FullyQualifiedErrorId"))]
    return dict(ok=bool(ok), output=(p.stdout or "").strip()[-600:], error=None if ok else ("Refresh failed: " + (err[0] if err else "no new export produced")) + " - previous export kept (not fresh).",
                info=after)
