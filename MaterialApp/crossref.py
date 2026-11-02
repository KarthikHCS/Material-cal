"""Applied Materials 0250-46767 Rev 06 - Global Comparable Raw Materials Cross Reference Standard.

Appendix A, Table 1 (sheet metal quick pick) and Table 2 (plate quick pick): imperial thickness -> stocked metric thickness.
Only sheet and plate are covered here (bars / tubes / shapes are not converted).
Values transcribed from the table images in the Rev 06 document; the live Excel (0250-46767.xlsx) was not available.
"""

IN = 25.4
TOL_IN = 0.0016     # imperial thickness must match a table row within this (0.04 mm)

# (imperial nominal in, gauge / label, stocked metric mm, delta in [steel thk vs metric], note)
SHEET_CARBON = [   # CRS ASTM A1008 / HRS ASTM A1011
    (0.015, "28 GA", 0.4, -0.0008, ""), (0.018, "26 GA", 0.5, -0.0018, ""), (0.024, "24 GA", 0.6, 0.0003, ""),
    (0.030, "22 GA", 0.7, 0.0023, ""), (0.036, "20 GA", 1.0, -0.0035, ""), (0.048, "18 GA", 1.2, 0.0006, ""),
    (0.060, "16 GA", 1.6, -0.0032, ""), (0.075, "14 GA", 2.0, -0.0040, ""),
    (0.090, "13 GA", None, None, "0.090 in carbon steel has no metric equivalent and should not be used in designs (Table 1 note 2)"),
    (0.105, "12 GA", 2.5, 0.0062, ""), (0.120, "11 GA", 3.0, 0.0015, ""), (0.135, "10 GA", 3.5, -0.0033, ""),
    (0.164, "8 GA", 4.0, 0.0069, ""), (0.179, "7 GA", 4.5, 0.0021, "4.5 mm may not always be available locally (Table 1 note 6)"),
    (0.1875, '3/16"', 5.0, -0.0094, "HRS 3/16 in may be classed as plate by some suppliers (Table 1 note 4)"),
    (0.250, '1/4"', 6.0, 0.0138, "HRS 0.250 in plate: ASTM A1018 (Table 1 note 4)"),
]
SHEET_SS304 = [    # Stainless type 304, ASTM A240
    (0.012, "", 0.3, None, ""), (0.016, "", 0.4, None, ""), (0.020, "", 0.5, None, ""), (0.024, "", 0.6, None, ""),
    (0.032, "", 0.8, None, ""), (0.040, "", 1.0, None, ""), (0.047, "", 1.2, None, ""), (0.059, "", 1.5, None, ""),
    (0.079, "", 2.0, None, ""), (0.098, "", 2.5, None, ""), (0.118, "", 3.0, None, ""), (0.157, "", 4.0, None, ""),
    (0.179, "", 4.5, None, "4.5 mm may not always be available locally (Table 1 note 6)"),
    (0.187, "", 5.0, None, "use 0.197 in / 5.0 mm for new designs (Table 1 note 7)"), (0.1875, '3/16"', 5.0, None, ""),
    (0.197, "", 5.0, None, ""), (0.236, "", 6.0, None, ""), (0.250, '1/4"', 6.0, None, "use 0.236 in / 6.0 mm for new designs (Table 1 note 7)"),
    (0.3125, '5/16"', 8.0, None, ""),
]
SHEET_AL = [       # Aluminum ASTM B209 5052-H32
    (0.020, "", 0.5, None, ""), (0.025, "", 0.6, None, ""), (0.032, "", 0.8, None, ""), (0.040, "", 1.0, None, ""),
    (0.050, "", 1.2, None, ""), (0.063, "", 1.6, None, ""), (0.080, "", 2.0, None, ""),
    (0.090, "", None, None, ".090 in aluminum has no close metric stock thickness (Table 1 note 3)"),
    (0.100, "", 2.5, None, ""), (0.125, "", 3.0, None, ""), (0.160, "", 4.0, None, ""), (0.190, "", 5.0, None, ""), (0.250, '1/4"', 6.0, None, ""),
]
PLATE = [          # Table 2: carbon HRS (A1011 / A1018 / A36 / A283) and stainless 304 (A240)
    (0.1875, '3/16"', 5.0, ""), (0.250, '1/4"', 6.0, ""), (0.3125, '5/16"', 8.0, ""), (0.375, '3/8"', 10.0, ""),
    (0.500, '1/2"', 12.5, ""), (0.5625, '9/16"', 15.0, "9/16 in is not always commercially available - avoid if possible (Table 2 note 2)"),
    (0.625, '5/8"', 16.0, ""), (0.750, '3/4"', 20.0, ""), (0.875, '7/8"', 22.5, ""), (1.000, '1"', 25.0, ""),
    (1.250, '1.25"', 30.0, ""), (1.500, '1.5"', 40.0, ""), (2.000, '2"', 50.0, ""), (2.500, '2.5"', 60.0, "2.5 in is not always commercially available (Table 2 note 2)"),
    (3.000, '3"', 75.0, ""), (3.500, '3.5"', 80.0, "3.5 in is not always commercially available (Table 2 note 2)"), (4.000, '4"', 100.0, ""), (6.000, '6"', 150.0, ""),
]


def family(spec):
    s = (spec or "").upper()
    if any(k in s for k in ("SUS", "SST", "STAINLESS", "304", "316", "301")): return "ss304"
    if any(k in s for k in ("ALU", "6061", "5052", "B209")): return "al"
    return "carbon"


def alt_metric(t_mm, kind, spec=""):
    """Imperial thickness t_mm (already converted to mm) -> dict(metric, text, note) per 0250-46767, or None if no table row.
    kind: 'sheet' (Table 1) or 'plate' (Table 2). metric is None when the table says there is no metric equivalent."""
    t_in = t_mm / IN
    fam = family(spec)
    if kind == "plate":
        rows = [(i, lab, mm, None, n) for i, lab, mm, n in PLATE]; tab = "Table 2 (plate)"
    else:
        rows = {"carbon": SHEET_CARBON, "ss304": SHEET_SS304, "al": SHEET_AL}[fam]; tab = "Table 1 (sheet)"
    hit = min(rows, key=lambda r: abs(r[0] - t_in))
    if abs(hit[0] - t_in) > TOL_IN: return None
    imp, lab, mm, delta, note = hit
    imp_txt = f"{imp:g} in" + (f" ({lab})" if lab else "")
    if mm is None:
        return dict(metric=None, text=f"{imp_txt}: no metric equivalent in 0250-46767 {tab}", note=note)
    d = f", delta {delta:+.4f} in" if delta is not None else ""
    return dict(metric=mm, text=f"{imp_txt} -> {mm:g} mm (0250-46767 {tab}{d})", note=note)
