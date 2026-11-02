"""Excel export of the purchasing tables, child review and open questions."""
import io
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

HF = PatternFill("solid", fgColor="1F3864")
THIN = Side(style="thin", color="BBBBBB")


def _sheet(wb, title, hdr, rows, widths):
    ws = wb.create_sheet(title)
    ws.append(hdr)
    for c in ws[1]:
        c.fill = HF; c.font = Font(bold=True, color="FFFFFF"); c.alignment = Alignment(wrap_text=True, vertical="center")
    for r in rows: ws.append(r)
    for i, w in enumerate(widths, 1): ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top"); c.border = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
    ws.freeze_panes = "A2"
    if rows: ws.auto_filter.ref = ws.dimensions


def to_xlsx(res):
    wb = Workbook(); wb.remove(wb.active)
    n = res.get("options", {}).get("assemblies", 1)
    P = [[r["parent"], r["item"], r["part"], r["mnum"], r["cat"], r["spec"], r["form"], r["size"], r["stock"], r["mach"],
          r["qty"], r["ext"], round(r["area"] / 1e6, 4) if r.get("area") else "", r.get("usage", ""),
          r.get("alt_text", ""), r.get("inv_text", ""), r.get("inv_stock", ""), "; ".join(x for x in r["note"] if x)] for r in res["parts"]]
    _sheet(wb, "Part-level detail",
           ["Parent assembly", "BOM item", "Part name / number", "Manufacturing number (manufacturer)", "Component / material type",
            "Material specification", "Shape / form", "Thickness / size", "Standard stock size / blank", "Machining requirement",
            "Qty per parent", "Total qty (all assemblies)", "Flat area / piece (m²)", "Material usage (exact decimal)",
            "Metric alternate (0250-46767)", "Inventory match (part number)", "Inventory stock", "Notes"],
           P, [22, 8, 40, 34, 24, 34, 18, 34, 34, 34, 9, 12, 12, 34, 44, 60, 36, 46])
    C = [[r["cat"], r["spec"], r["form"], r["size"], r["stock"], r["tube"], r["total"], r.get("usage", ""), r.get("util", ""), r.get("alt_text", ""), r.get("inv_text", ""), r.get("inv_stock", ""),
          r.get("byrun", ""), r["rel"], r["notes"]] for r in res["consolidated"]]
    _sheet(wb, "Consolidated list",
           ["Material type", "Material specification", "Shape / form", "Size / thickness", "Standard stock size", "US / Local (tube)",
            "Total required quantity", "Material usage (exact decimal)", "Utilization of standard stock", "Metric alternate (0250-46767)", "Inventory match (part number)", "Inventory stock vs need",
            "Qty by parent / history record", "Related parts / manufacturing numbers", "Notes"],
           C, [26, 44, 20, 30, 28, 12, 52, 34, 40, 44, 60, 40, 34, 60, 50])
    if res.get("coverage"):
        _sheet(wb, "Child review", ["Parent", "Part no.", "Description", "Kind", "Drawing PDF", "Supplier form", "STEP", "Status"],
               [[c["parent"], c["pn"], c["desc"], c["kind"], "yes" if c["drawing"] else "no", "yes" if c["form"] else "no",
                 "yes" if c["step"] else "no", c["status"]] for c in res["coverage"]], [22, 14, 50, 16, 11, 12, 8, 36])
    _sheet(wb, "Open questions", ["#", "Topic", "Question"],
           [[i + 1, q["topic"], q["text"]] for i, q in enumerate(res["questions"])], [5, 26, 130])
    b = io.BytesIO(); wb.save(b); return b.getvalue()
