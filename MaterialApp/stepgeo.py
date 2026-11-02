"""STEP geometry helpers: part extraction, sheet-metal unfolding, bar/plate blank sizing."""
import math, re
import numpy as np
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.TDF import TDF_LabelSequence, TDF_Label
from OCP.TDataStd import TDataStd_Name
from OCP.IFSelect import IFSelect_RetDone
from OCP.BRepGProp import BRepGProp, BRepGProp_Face
from OCP.GProp import GProp_GProps
from OCP.XCAFApp import XCAFApp_Application
from OCP.TopLoc import TopLoc_Location
from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder
from OCP.TopExp import TopExp_Explorer, TopExp
from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE, TopAbs_REVERSED
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape
from OCP.gp import gp_Trsf, gp_Ax3, gp_Pnt, gp_Dir, gp_Vec
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.BRepTools import BRepTools

IN = 25.4
TC_RE = re.compile(r"(TC\d-\d+)_\d+")


def _name(l):
    a = TDataStd_Name()
    return a.Get().ToExtString() if l.FindAttribute(TDataStd_Name.GetID_s(), a) else ""


def _V(p):
    return np.array([p.X(), p.Y(), p.Z()])


def leaf_parts(path):
    """Return {key: {name, tc, count, shape}} for every distinct leaf solid in a STEP file.
    key = TcENG number when present in the name, else the full name."""
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    app.NewDocument(TCollection_ExtendedString("XmlOcaf"), doc)
    r = STEPCAFControl_Reader()
    r.SetNameMode(True)
    if r.ReadFile(path) != IFSelect_RetDone:
        raise RuntimeError("cannot read STEP " + path)
    r.Transfer(doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    out = {}

    def rec(label, loc):
        ref = TDF_Label()
        if st.IsReference_s(label):
            st.GetReferredShape_s(label, ref)
            rec(ref, loc.Multiplied(st.GetLocation_s(label)))
            return
        if st.IsAssembly_s(label):
            ch = TDF_LabelSequence()
            st.GetComponents_s(label, ch)
            for i in range(1, ch.Length() + 1):
                rec(ch.Value(i), loc)
            return
        n = _name(label)
        m = TC_RE.match(n)
        key = m.group(1) if m else n
        if key in out:
            out[key]["count"] += 1
        else:
            out[key] = {"name": n, "tc": m.group(1) if m else None, "count": 1,
                        "shape": st.GetShape_s(label).Moved(loc)}

    fl = TDF_LabelSequence()
    st.GetFreeShapes(fl)
    for i in range(1, fl.Length() + 1):
        rec(fl.Value(i), TopLoc_Location())
    return out


def volume(shape):
    p = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, p)
    return p.Mass()


def _area(face):
    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, g)
    return g.Mass()


def _fnorm(f, pl):
    d = _V(pl.Axis().Direction())
    return -d if f.Orientation() == TopAbs_REVERSED else d


def _hull_minrect(P):
    pts = sorted(set(map(tuple, np.round(P, 4))))
    if len(pts) < 3:
        return 0.0, 0.0
    cr = lambda o, a, b: (a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0])
    lo, up = [], []
    for p in pts:
        while len(lo) >= 2 and cr(lo[-2], lo[-1], p) <= 0: lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(up) >= 2 and cr(up[-2], up[-1], p) <= 0: up.pop()
        up.append(p)
    H = np.array(lo[:-1] + up[:-1])
    best = None
    for i in range(len(H)):
        e = H[(i+1) % len(H)] - H[i]
        a = math.atan2(e[1], e[0]); c, s = math.cos(-a), math.sin(-a)
        Q = H @ np.array([[c, -s], [s, c]]).T
        w, h = np.ptp(Q[:, 0]), np.ptp(Q[:, 1])
        if best is None or w*h < best[0]:
            best = (w*h, max(w, h), min(w, h))
    return best[1], best[2]


def _edges(f):
    e = TopExp_Explorer(f, TopAbs_EDGE); L = []
    while e.More():
        L.append(TopoDS.Edge_s(e.Current())); e.Next()
    return L


def _edge_pts(ed, T=None, defl=0.2):
    c = BRepAdaptor_Curve(ed)
    d = GCPnts_QuasiUniformDeflection(c, defl)
    pts = []
    if d.IsDone():
        for k in range(1, d.NbPoints() + 1):
            p = d.Value(k)
            pts.append(_V(p.Transformed(T) if T else p))
    else:
        for u in (c.FirstParameter(), c.LastParameter()):
            p = c.Value(u)
            pts.append(_V(p.Transformed(T) if T else p))
    return pts


def unfold(shape, t):
    """Unfold a constant-thickness bent sheet solid. Neutral axis at mid-thickness (K=0.5).
    Returns dict(L,W,area,visited,planes,bends) or None when no planar face exists."""
    fm = TopTools_IndexedMapOfShape(); TopExp.MapShapes_s(shape, TopAbs_FACE, fm)
    em = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, em)
    info = {}
    for i in range(1, fm.Extent() + 1):
        f = TopoDS.Face_s(fm.FindKey(i)); a = BRepAdaptor_Surface(f); ty = a.GetType()
        if ty == GeomAbs_Plane:
            pl = a.Plane(); info[i] = ("P", f, pl, _fnorm(f, pl))
        elif ty == GeomAbs_Cylinder:
            info[i] = ("C", f, a.Cylinder(), None)
    nplanes = sum(1 for v in info.values() if v[0] == "P")
    if not nplanes:
        return None

    def nbrs(f):
        out = []
        for ed in _edges(f):
            for g in em.FindFromKey(ed):
                if not g.IsSame(f): out.append((ed, TopoDS.Face_s(g)))
        return out

    def is_bend(c, p):
        cy = info[c][2]; n = info[p][3]; pl = info[p][2]
        if abs(np.dot(_V(cy.Axis().Direction()), n)) > 1e-3: return False
        d = abs(np.dot(_V(cy.Axis().Location()) - _V(pl.Location()), n))
        return abs(d - cy.Radius()) < 1e-2 * max(1, cy.Radius())

    def inner(cf, cy):
        u0, u1, v0, v1 = BRepTools.UVBounds_s(cf)
        pnt = gp_Pnt(); vec = gp_Vec()
        BRepGProp_Face(cf).Normal((u0+u1)/2, (v0+v1)/2, pnt, vec)
        loc = _V(cy.Axis().Location()); ax = _V(cy.Axis().Direction()); p = _V(pnt)
        rad = p - loc - np.dot(p - loc, ax) * ax
        return np.dot(_V(vec), rad) < 0

    i0 = max((i for i, v in info.items() if v[0] == "P"), key=lambda i: _area(info[i][1]))
    pl0, n0 = info[i0][2], info[i0][3]
    T0 = gp_Trsf(); T0.SetTransformation(gp_Ax3(pl0.Location(), gp_Dir(*n0)))
    Tf = {i0: T0}; stack = [i0]; strips = []; strip_area = 0.0; nb = 0
    while stack:
        i1 = stack.pop(); T1 = Tf[i1]; f1 = info[i1][1]; n1 = info[i1][3]
        for ed, g in nbrs(f1):
            c = fm.FindIndex(g)
            if c not in info or info[c][0] != "C" or not is_bend(c, i1): continue
            cf, cy = info[c][1], info[c][2]
            for _, g2 in nbrs(cf):
                j = fm.FindIndex(g2)
                if j == i1 or j in Tf or j not in info or info[j][0] != "P" or not is_bend(c, j): continue
                n2 = info[j][3]
                th = math.acos(max(-1, min(1, float(np.dot(n1, n2)))))
                if th < 1e-3: continue
                ax = cy.Axis(); axv = _V(ax.Direction())
                for sgn in (1, -1):
                    r = gp_Trsf(); r.SetRotation(ax, sgn * th)
                    if np.dot(_V(gp_Dir(*n2).Transformed(r)), n1) > 0.999: break
                rn = cy.Radius() + (t / 2 if inner(cf, cy) else -t / 2)
                Lb = th * rn
                c1 = BRepAdaptor_Curve(ed)
                pa, pb = _V(c1.Value(c1.FirstParameter())), _V(c1.Value(c1.LastParameter()))
                u = np.cross(axv, n1); u /= np.linalg.norm(u)
                cen = np.mean([_V(BRepAdaptor_Curve(e).Value(BRepAdaptor_Curve(e).FirstParameter())) for e in _edges(f1)], axis=0)
                if np.dot(u, (pa + pb) / 2 - cen) < 0: u = -u
                tr = gp_Trsf(); tr.SetTranslation(gp_Vec(*(u * Lb)))
                Tf[j] = T1.Multiplied(tr).Multiplied(r); stack.append(j)
                for p in (pa, pb):
                    for dd in (0, Lb):
                        strips.append(_V(gp_Pnt(*(p + u * dd)).Transformed(T1)))
                strip_area += Lb * np.linalg.norm(pb - pa); nb += 1
    pts = list(strips); area = strip_area
    for i, T in Tf.items():
        f = info[i][1]; area += _area(f)
        for ed in _edges(f): pts += _edge_pts(ed, T)
    L, W = _hull_minrect(np.array(pts)[:, :2])
    return {"L": L, "W": W, "area": area, "visited": len(Tf), "planes": nplanes, "bends": nb}


def round_stock(shape):
    """(diameter, length) when the part is a plain round bar (one outer cylinder spanning the whole width), else None."""
    fm = TopTools_IndexedMapOfShape(); TopExp.MapShapes_s(shape, TopAbs_FACE, fm)
    rmax = 0.0; axis = None
    for i in range(1, fm.Extent() + 1):
        a = BRepAdaptor_Surface(TopoDS.Face_s(fm.FindKey(i)))
        if a.GetType() == GeomAbs_Cylinder and a.Cylinder().Radius() > rmax:
            rmax = a.Cylinder().Radius(); axis = _V(a.Cylinder().Axis().Direction())
    if not rmax: return None
    T, W, L, _ = box_blank(shape)
    D = 2 * rmax
    if abs(D - W) <= 0.02 * W and abs(D - T) <= 0.02 * T and L > D:
        return D, L
    return None


def box_blank(shape):
    """Min-volume bounding box aligned to a planar face normal: (T, W, L, steps).
    steps = number of distinct depth levels of faces parallel to the main face (>2 => stepped)."""
    fm = TopTools_IndexedMapOfShape(); TopExp.MapShapes_s(shape, TopAbs_FACE, fm)
    faces = []
    for i in range(1, fm.Extent() + 1):
        f = TopoDS.Face_s(fm.FindKey(i)); a = BRepAdaptor_Surface(f)
        if a.GetType() == GeomAbs_Plane: faces.append((f, a.Plane()))
    pts = []
    for i in range(1, fm.Extent() + 1):
        for ed in _edges(TopoDS.Face_s(fm.FindKey(i))): pts += _edge_pts(ed, None, 0.3)
    P = np.array(pts); cands = []; seen = []
    for f, pl in faces:
        n = _fnorm(f, pl)
        if any(abs(abs(np.dot(n, s)) - 1) < 1e-4 for s in seen): continue
        seen.append(n)
        a = np.cross(n, [1, 0, 0] if abs(n[0]) < .9 else [0, 1, 0]); a /= np.linalg.norm(a); b = np.cross(n, a)
        T = np.ptp(P @ n)
        L, W = _hull_minrect(np.c_[P @ a, P @ b])
        cands.append((T * L * W, T, W, L, n))
    vmin = min(c[0] for c in cands)
    best = min((c for c in cands if c[0] <= vmin * 1.01), key=lambda c: c[1])
    _, T, W, L, n = best
    offs = sorted({round(float(np.dot(_V(pl.Location()), n)), 1) for f, pl in faces
                   if abs(abs(np.dot(_fnorm(f, pl), n)) - 1) < 1e-4})
    return T, W, L, len(offs)
