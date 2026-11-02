"""Material Usage Calculator - local web app.

Run:  python app.py            (opens http://127.0.0.1:8765)
CLI:  python app.py --folder "D:\\path\\0011-19758" --out result.xlsx [--assemblies 2]

Data (uploaded files + saved results) lives in ./data  (runs/<id>/{result.json,files/}, history.json).
"""
import argparse, json, os, re, shutil, sys, threading, time, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

FROZEN = getattr(sys, "frozen", False)
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))   # bundled resources (index.html)
sys.path.insert(0, HERE)
import engine, excel_out, inventory

if FROZEN:   # installed .exe: keep settings and default records in the user's profile, not in the program folder
    SETTINGS_FILE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "MaterialUsageCalculator", "settings.json")
    DEFAULT_SAVE = os.path.join(os.path.expanduser("~"), "Documents", "MaterialUsageCalculator")
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
else:
    SETTINGS_FILE = os.path.join(HERE, "settings.json")
    DEFAULT_SAVE = os.path.join(HERE, "data")
LOCK = threading.Lock()


class P:
    """Save-folder paths; everything (uploads, runs, history) lives under the chosen folder."""
    save = DEFAULT_SAVE

    @classmethod
    def set(cls, path):
        path = os.path.abspath(path.strip().strip(chr(34)))
        os.makedirs(os.path.join(path, "runs"), exist_ok=True); os.makedirs(os.path.join(path, "uploads"), exist_ok=True)
        cls.save = path
        json.dump({"save_dir": path}, open(SETTINGS_FILE, "w", encoding="utf-8"))

    runs = classmethod(lambda c: os.path.join(c.save, "runs"))
    upl = classmethod(lambda c: os.path.join(c.save, "uploads"))
    hist = classmethod(lambda c: os.path.join(c.save, "history.json"))


try: P.set(json.load(open(SETTINGS_FILE, encoding="utf-8"))["save_dir"])
except Exception: P.set(P.save)
MERGED = {"xlsx": None, "name": "Merged"}


# ------------------------------------------------------------------ history store
def hist_load():
    try: return json.load(open(P.hist(), encoding="utf-8"))
    except Exception: return []


def hist_save(h):
    json.dump(h, open(P.hist(), "w", encoding="utf-8"), indent=1)


def safe_rel(p):
    parts = [x for x in re.split(r"[\\/]+", unquote(p)) if x not in ("", ".", "..") and ":" not in x]
    return os.path.join(*parts) if parts else None


def options_from(o):
    return dict(assemblies=int(o.get("assemblies", 1) or 1), sheet_max_mm=float(o.get("sheet_max_mm", 6.0)),
                plate_max_mm=float(o.get("plate_max_mm", 10.0)), allowance_mm=float(o.get("allowance_mm", 2.0)),
                tube_allow_mm=float(o.get("tube_allow_mm", 5.0)), nest_gap_mm=float(o.get("nest_gap_mm", 5.0)),
                stock_len_mm=float(o.get("stock_len_m", 3.0)) * 1000, kerf_mm=float(o.get("kerf_mm", 0) or 0),
                allowance_thickness=bool(o.get("allowance_thickness", True)))


def generate(root, parent_pn, opt, label, move_from=None, refresh=False):
    """Run the engine on root; save the record (result + the input files) to history; return the result.
    Inventory (material.MD): refreshed first when asked; if the refresh fails the previous export is used and flagged as not fresh."""
    rr = inventory.refresh() if refresh else None
    inv = inventory.load(fresh=bool(rr and rr["ok"]))
    res = engine.Engine(root, opt, inv).run(parent_pn or None)
    if rr and not rr["ok"] and res.get("inventory") is not None:
        res["inventory"]["refresh_error"] = rr["error"]
        res["questions"].insert(0, dict(topic="Inventory refresh failed", text=rr["error"] + " Matches use the export from " + str(res["inventory"].get("refreshed_at")) + "."))
    rid = time.strftime("%Y%m%d-%H%M%S") + "-" + res["top"]
    rdir = os.path.join(P.runs(), rid); os.makedirs(rdir, exist_ok=True)
    res["id"] = rid; res["label"] = label or res["top"]; res["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if move_from:
        shutil.move(move_from, os.path.join(rdir, "files"))          # upload area is cleared
        nfiles = sum(len(f) for _, _, f in os.walk(os.path.join(rdir, "files")))
    else:
        nfiles = 0; res["source"] = root
    json.dump(res, open(os.path.join(rdir, "result.json"), "w", encoding="utf-8"), default=str)
    open(os.path.join(rdir, "result.xlsx"), "wb").write(excel_out.to_xlsx(res))
    with LOCK:
        h = hist_load()
        h.insert(0, dict(id=rid, label=res["label"], top=res["top"], created=res["created"], files=nfiles,
                         assemblies=opt["assemblies"], parts=len(res["parts"]), cons=len(res["consolidated"]),
                         questions=len(res["questions"]), missing=sum(1 for c in res["coverage"] if c["status"].startswith("MISSING"))))
        hist_save(h)
    return res


def load_run(rid):
    if not re.fullmatch(r"[\w.-]+", rid or ""): raise ValueError("bad id")
    return json.load(open(os.path.join(P.runs(), rid, "result.json"), encoding="utf-8"))


def merge(ids):
    parts, qs, cov, labels = [], [], [], []
    seen_q = set()
    for rid in ids:
        r = load_run(rid); lab = r.get("label") or r["top"]; labels.append(lab)
        lab = f"{lab} ({rid[:8]})" if labels.count(lab) > 1 else lab
        for p in r["parts"]:
            p = dict(p); p["run"] = lab; p["parent"] = f"[{lab}] {p['parent']}"; parts.append(p)
        for c in r.get("coverage", []):
            c = dict(c); c["parent"] = f"[{lab}] {c['parent']}"; cov.append(c)
        for q in r["questions"]:
            k = (q["topic"], q["text"])
            if k not in seen_q: seen_q.add(k); qs.append(q)
    o1 = load_run(ids[0]).get("options", {})
    res = dict(top="MERGED", label=" + ".join(labels), options=dict(assemblies=1), parts=parts, coverage=cov,
               consolidated=engine.consolidate(parts, "", o1.get("stock_len_mm", 3000.0), o1.get("kerf_mm", 0.0), o1.get("nest_gap_mm", 5.0), inventory.load()),
               questions=qs, sources=labels, inventory=load_run(ids[0]).get("inventory"))
    MERGED["xlsx"] = excel_out.to_xlsx(res); MERGED["name"] = "Merged_" + "_".join(re.sub(r"\W+", "", l)[:12] for l in labels)
    return res


# ------------------------------------------------------------------ http
HTML_PATH = os.path.join(HERE, "index.html")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body, ctype="application/json", extra=None):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)

    def _json(self, obj, code=200): self._send(code, json.dumps(obj, default=str))

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        try:
            if u.path == "/": self._send(200, open(HTML_PATH, encoding="utf-8").read(), "text/html; charset=utf-8")
            elif u.path == "/api/history": self._json(hist_load())
            elif u.path == "/api/ping": self._json({"ok": True, "save_dir": P.save})
            elif u.path == "/api/inventory": self._json(inventory.info())
            elif u.path == "/api/run": self._json(load_run(q["id"][0]))
            elif u.path == "/api/xlsx":
                rid = q["id"][0]
                if rid == "merged":
                    data, name = MERGED["xlsx"], MERGED["name"]
                else:
                    load_run(rid); data = open(os.path.join(P.runs(), rid, "result.xlsx"), "rb").read(); name = rid
                if not data: return self._send(404, "nothing", "text/plain")
                self._send(200, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           {"Content-Disposition": f'attachment; filename="{name}_Material_List.xlsx"'})
            else: self._send(404, "not found", "text/plain")
        except Exception as e: self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        try:
            n = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(n)
            if u.path == "/api/upload":                       # one file into the staging area
                sid = re.sub(r"\W", "", q["sid"][0]); rel = safe_rel(q["path"][0])
                if not rel: return self._json({"error": "bad path"}, 400)
                dst = os.path.join(P.upl(), sid, rel); os.makedirs(os.path.dirname(dst), exist_ok=True)
                open(dst, "wb").write(raw); return self._json({"ok": True})
            req = json.loads(raw or b"{}")
            if u.path == "/api/generate":
                opt = options_from(req.get("options", {})); sid = re.sub(r"\W", "", req.get("sid", ""))
                stage = os.path.join(P.upl(), sid) if sid else None
                if stage and os.path.isdir(stage):
                    try: res = generate(stage, req.get("parent", "").strip(), opt, req.get("label", "").strip(), move_from=stage, refresh=bool(req.get("refresh")))
                    finally:
                        if os.path.isdir(stage) and not os.path.exists(os.path.join(P.runs(), "x")): pass
                elif req.get("folder", "").strip():
                    f = req["folder"].strip().strip('"')
                    if not os.path.isdir(f): return self._json({"error": "Folder not found: " + f}, 400)
                    res = generate(f, req.get("parent", "").strip(), opt, req.get("label", "").strip(), refresh=bool(req.get("refresh")))
                else: return self._json({"error": "Add files (or a folder path) first."}, 400)
                self._json(res)
            elif u.path == "/api/settings":
                path = req.get("save_dir", "").strip()
                if not path: return self._json({"error": "Enter a folder path."}, 400)
                P.set(path); self._json({"ok": True, "save_dir": P.save})
            elif u.path == "/api/browse":
                try:
                    import tkinter as tk; from tkinter import filedialog
                    r = tk.Tk(); r.withdraw(); r.attributes("-topmost", True)
                    p = filedialog.askdirectory(title="Select save folder", initialdir=P.save); r.destroy()
                except Exception as e: return self._json({"error": "Folder dialog unavailable: " + str(e)}, 500)
                self._json({"path": os.path.normpath(p) if p else ""})
            elif u.path == "/api/inventory/refresh":          # runs refresh_materials.ps1 (temp copy of the dashboard; source never saved)
                self._json(inventory.refresh())
            elif u.path == "/api/discard":                    # clear a staging area without generating
                sid = re.sub(r"\W", "", req.get("sid", "")); shutil.rmtree(os.path.join(P.upl(), sid), ignore_errors=True); self._json({"ok": True})
            elif u.path == "/api/delete":
                rid = req["id"]; load_run(rid)
                shutil.rmtree(os.path.join(P.runs(), rid), ignore_errors=True)
                with LOCK: hist_save([h for h in hist_load() if h["id"] != rid])
                self._json({"ok": True})
            elif u.path == "/api/merge":
                ids = req.get("ids", [])
                if len(ids) < 2: return self._json({"error": "Select at least 2 history records."}, 400)
                self._json(merge(ids))
            else: self._json({"error": "unknown"}, 404)
        except Exception as e:
            import traceback; traceback.print_exc()
            self._json({"error": str(e)}, 500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder"); ap.add_argument("--out"); ap.add_argument("--assemblies", type=int, default=1)
    ap.add_argument("--parent", default=""); ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.folder:
        res = generate(a.folder, a.parent, options_from(dict(assemblies=a.assemblies)), "")
        out = a.out or os.path.join(P.runs(), res["id"], "result.xlsx")
        open(out, "wb").write(excel_out.to_xlsx(res))
        print("Saved", out, "|", len(res["parts"]), "part rows,", len(res["consolidated"]), "consolidated rows,", len(res["questions"]), "open questions")
        return
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    url = f"http://127.0.0.1:{a.port}"
    print("Material Usage Calculator running at", url, "(Ctrl+C to stop)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass


if __name__ == "__main__":
    main()

