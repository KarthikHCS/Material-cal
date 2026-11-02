"""PDF parsing: BOM tables, OEM part-definition forms, Excel BOM cross-check. All lookups work on file lists."""
import re, os, glob
import fitz

PN_RE = re.compile(r"^(\d{4}-\d{5}|\d{3}-\d{2}-\d{3})$")
PN_PREFIX = re.compile(r"^(\d{4}-\d{5}|\d{3}-\d{2}-\d{3}|\d{6,7}-\d{3})(?![\d-])")
TC_END = re.compile(r"\((TC\d-\d+)\)\s*$")
_cache = {}


def pdf_lines(path):
    if path in _cache: return _cache[path]
    try:
        d = fitz.open(path)
        L = [l.strip() for p in d for l in p.get_text().splitlines() if l.strip()]
    except Exception:
        L = []
    _cache[path] = L
    return L


def index_files(root):
    """{part number: [paths]} - part number taken from the start of each file name."""
    idx = {}
    for dp, _, fs in os.walk(root):
        for f in fs:
            m = PN_PREFIX.match(f)
            if m: idx.setdefault(m.group(1), []).append(os.path.join(dp, f))
    return idx


def parse_bom(path):
    """Parse the Applied Materials style BOM ('ITEM / QTY / PART NO / DESCRIPTION / (TcENG)').
    Returns list of dicts(item, qty, pn, desc, tc) or [] when the PDF carries no readable BOM."""
    L = pdf_lines(path); items = []; i = 0
    isint = lambda s: s.isdigit() and len(s) <= 3
    while i < len(L) - 3:
        if isint(L[i]) and isint(L[i+1]) and not isint(L[i+2]):
            j = i + 2; buf = []
            while j < len(L) and j < i + 12:
                buf.append(L[j])
                if TC_END.search(L[j]): break
                j += 1
            if j >= len(L) or j >= i + 12 or not TC_END.search(L[j]):
                i += 1; continue
            tc = TC_END.search(buf[-1]).group(1)
            buf[-1] = TC_END.sub("", buf[-1]).strip()
            buf = [b for b in buf if b]
            pn = ""
            if buf and PN_RE.match(buf[0]): pn, buf = buf[0], buf[1:]
            elif buf and buf[0] in ("-", "–", "—", "N/A"): buf = buf[1:]      # '-' in the PART NO. column = no part number
            items.append({"item": int(L[i]), "qty": int(L[i+1]), "pn": pn, "desc": " ".join(buf), "tc": tc})
            i = j + 1
        else:
            i += 1
    return sorted(items, key=lambda x: x["item"])


def parse_form(files):
    """OEM Part Definition Form (-001.pdf): list of (manufacturer, mfr part number)."""
    for f in sorted(files):
        if not re.search(r"-001\.pdf$", f, re.I): continue
        t = "\n".join(pdf_lines(f))
        if "Manufacturer Part Number" not in t: continue
        tail = t.split("Manufacturer Part Number", 1)[1]
        tail = re.split(r"There is No Intellectual|IP Statement|Shall comply", tail)[0]
        v = [l.strip(" |") for l in tail.splitlines() if l.strip(" |-")]
        v = [x for x in v if x and not set(x) <= set("|- ")]
        return [(v[k], v[k+1]) for k in range(0, len(v) - 1, 2)]
    return []


def form_desc(files):
    for f in sorted(files):
        if not re.search(r"-001\.pdf$", f, re.I): continue
        t = "\n".join(pdf_lines(f))
        m = re.search(r"Description:\s*(.*?)\n(?:Requestor|Division|UOM)", t, re.S)
        if m: return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def main_pdf(files):
    c = [f for f in files if f.lower().endswith(".pdf")
         and not re.search(r"-00\d(\(\d\))?\.pdf$", f, re.I) and not re.search(r"\(\d\)\.pdf$", f)]
    return sorted(c)[0] if c else None


def excel_bom(root):
    """Find any .xlsx BOM below root; return {part_no: qty}. Empty if none."""
    out = {}
    try:
        import openpyxl
    except Exception:
        return out
    for f in glob.glob(os.path.join(root, "**", "*.xlsx"), recursive=True):
        if "Material_List" in f or "_DRAFT" in f: continue
        try:
            wb = openpyxl.load_workbook(f, data_only=True)
        except Exception:
            continue
        for ws in wb:
            for row in ws.iter_rows(values_only=True):
                pn = next((str(c).strip() for c in row if c and PN_RE.match(str(c).strip())), None)
                q = next((c for c in row if isinstance(c, (int, float))), None)
                if pn and q is not None: out[pn] = out.get(pn, 0) + q
    return out
