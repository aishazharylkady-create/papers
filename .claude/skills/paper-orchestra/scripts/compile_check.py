#!/usr/bin/env python3
"""Compile a LaTeX paper and report everything the refinement loop needs.

    python compile_check.py <paper_dir> [--tex paper.tex] [--page-limit 8] [--png]

Reports: compile errors, undefined citations/references, cite keys missing from
references.bib, overfull boxes, total pages, the page where References begins
(= main-content length), figures referenced but missing, and figures placed
after the Conclusion. With --png it renders every page to <paper_dir>/pages/
so the pages can be inspected visually (Read tool on the PNGs).
Exit code 0 = PDF produced and no blocking problems, 1 = problems found.
"""
import argparse
import os
import re
import subprocess
import sys


def run_latex(d, tex):
    cmd = ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-f", tex]
    p = subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=600)
    return p.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paper_dir")
    ap.add_argument("--tex", default="paper.tex")
    ap.add_argument("--page-limit", type=int, default=0)
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()

    d = os.path.abspath(a.paper_dir)
    tex_path = os.path.join(d, a.tex)
    stem = os.path.splitext(a.tex)[0]
    pdf_path = os.path.join(d, stem + ".pdf")
    log_path = os.path.join(d, stem + ".log")
    if os.path.exists(pdf_path):
        os.remove(pdf_path)

    problems = []
    rc = run_latex(d, a.tex)
    log = open(log_path, errors="replace").read() if os.path.exists(log_path) else ""
    src = open(tex_path, errors="replace").read()

    lines = log.splitlines()
    n_err = 0
    for i, ln in enumerate(lines):
        if ln.startswith("! ") and "==> Fatal error" not in ln and n_err < 10:
            msg = ln[2:] + (lines[i + 1] if i + 1 < len(lines) and lines[i + 1] and not lines[i + 1].startswith(("l.", "See ", "Type ")) else "")
            m = next((re.match(r"l\.(\d+)", x) for x in lines[i + 1:i + 15] if re.match(r"l\.(\d+)", x)), None)
            problems.append(f"LaTeX error{' at line ' + m.group(1) if m else ''}: {msg.strip()}")
            n_err += 1
    if not os.path.exists(pdf_path):
        problems.append("PDF was NOT produced.")

    undef_c = sorted(set(re.findall(r"Citation `([^']+)' .*undefined", log)))
    undef_r = sorted(set(re.findall(r"Reference `([^']+)' .*undefined", log)))
    if undef_c:
        problems.append("Undefined citations: " + ", ".join(undef_c))
    if undef_r:
        problems.append("Undefined references (\\ref/\\cref): " + ", ".join(undef_r))

    body = re.sub(r"(?<!\\)%.*", "", src)
    cited = set()
    for grp in re.findall(r"\\cite[a-zA-Z]*\*?(?:\[[^\]]*\])*\{([^}]*)\}", body):
        cited.update(k.strip() for k in grp.split(",") if k.strip())
    bib_keys = set()
    for bib in re.findall(r"\\bibliography\{([^}]*)\}", body):
        for b in bib.split(","):
            bp = os.path.join(d, b.strip() + ("" if b.strip().endswith(".bib") else ".bib"))
            if os.path.exists(bp):
                bib_keys |= set(re.findall(r"@\w+\s*\{\s*([^,\s]+)\s*,", open(bp, errors="replace").read()))
    missing = sorted(cited - bib_keys)
    if missing:
        problems.append("Cite keys not in references.bib: " + ", ".join(missing))

    for f in re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}", body):
        if not os.path.exists(os.path.join(d, f)):
            problems.append(f"Missing figure file: {f}")

    concl = re.search(r"\\section\*?\{[^}]*(Conclusion|Discussion and Conclusion)[^}]*\}", body)
    bibpos = re.search(r"\\bibliography\{|\\begin\{thebibliography\}", body)
    appx = re.search(r"\\appendix|\\section\*?\{Appendix", body)
    if concl:
        end = min([m.start() for m in (bibpos, appx) if m] or [len(body)])
        tail = body[concl.end():end]
        if re.search(r"\\begin\{(figure|table)\*?\}", tail):
            problems.append("A figure/table is placed after the Conclusion (move it earlier).")

    overfull = re.findall(r"Overfull \\hbox \(([\d.]+)pt too wide\)[^\n]*lines? (\d+)", log)
    big = [(float(w), l) for w, l in overfull if float(w) > 5]

    info = []
    if os.path.exists(pdf_path):
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz  # older PyMuPDF
        doc = fitz.open(pdf_path)
        info.append(f"PDF: {pdf_path}")
        info.append(f"Total pages: {doc.page_count}")
        ref_page = None
        for i, page in enumerate(doc):
            if re.search(r"^\s*(References|Bibliography)\s*$", page.get_text(), re.M):
                ref_page = i + 1
                break
        if ref_page:
            info.append(f"References start on page {ref_page} (main content ~{ref_page} pages incl. that partial page)")
            if a.page_limit and ref_page > a.page_limit + 1:
                problems.append(f"Over page limit: references start on page {ref_page}, limit is {a.page_limit} pages of main content.")
            elif a.page_limit and ref_page == a.page_limit + 1:
                info.append("  (check visually that main text ends on page "
                            f"{a.page_limit} and only References spill onto page {ref_page})")
        for i, page in enumerate(doc):
            if len(page.get_text().strip()) < 40 and not page.get_images():
                problems.append(f"Page {i+1} is (almost) blank.")
        if a.png:
            out = os.path.join(d, "pages")
            os.makedirs(out, exist_ok=True)
            for i, page in enumerate(doc):
                page.get_pixmap(dpi=80).save(os.path.join(out, f"page_{i+1:02d}.png"))
            info.append(f"Page images: {out}/page_XX.png")

    if big:
        info.append("Overfull boxes > 5pt (source line: width): " +
                    ", ".join(f"l.{l}: {w:.0f}pt" for w, l in big[:15]))
    info.append(f"Citations used: {len(cited)} unique keys")

    print("\n".join(info))
    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(" -", p)
        sys.exit(1)
    print("\nOK: compiled cleanly.")


if __name__ == "__main__":
    main()
