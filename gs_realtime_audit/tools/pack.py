"""Pack the whole audit deliverable into one zip, preserving directory structure.

Also writes CHECKLIST.md (file count, sizes, SHA256 of every file) inside the zip
and beside it. Excludes caches / temp / spill artifacts.
"""
import os, zipfile, hashlib, datetime, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.abspath(os.path.join(ROOT, "..", "gs_realtime_audit_deliverable.zip"))
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}
SKIP_EXT = {".pyc", ".pyo", ".tmp", ".log.bak"}

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def main():
    files = []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            if any(f.endswith(e) for e in SKIP_EXT):
                continue
            p = os.path.join(dp, f)
            files.append(p)
    files.sort()
    lines = ["# GameScaling 实时推理审计 · 交付校验清单", "",
             "生成时间: %s" % datetime.datetime.now().isoformat(),
             "根目录: %s" % ROOT,
             "文件总数: %d" % len(files), ""]
    lines.append("| 相对路径 | 字节 | SHA256 |")
    lines.append("|---|---|---|")
    for p in files:
        rel = os.path.relpath(p, os.path.dirname(ROOT)).replace(os.sep, "/")
        lines.append("| %s | %d | %s |" % (rel, os.path.getsize(p), sha(p)))
    chk = os.path.join(ROOT, "CHECKLIST.md")
    open(chk, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    if chk not in files:
        files.append(chk)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            arc = os.path.join("gs_realtime_audit", os.path.relpath(p, ROOT)).replace(os.sep, "/")
            z.write(p, arc)
    print("ZIP", OUT, os.path.getsize(OUT), "bytes,", len(files), "files")

if __name__ == "__main__":
    main()
