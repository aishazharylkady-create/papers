---
name: paper-orchestra
description: Write a complete, submission-ready LaTeX research paper (CVPR/ICLR or custom template) from raw materials — an idea/method description and an experimental log, optionally figures — using the PaperOrchestra multi-agent pipeline (outline → literature review → section writing → plotting → peer-review refinement → format check), with Claude playing every agent itself; no Gemini/OpenAI keys needed. Use when the user asks to write, draft or generate a paper/article/manuscript from their idea, notes, results or experiment logs, or mentions PaperOrchestra. Triggers: "напиши статью", "сгенерируй статью", "write a paper", "draft a CVPR/ICLR paper", "turn my results into a paper".
---

# PaperOrchestra (self-contained skill)

Port of [google-research/paper-orchestra](https://github.com/google-research/paper-orchestra) (Apache-2.0).
The original pipeline calls Gemini/OpenAI for each agent. Here **you are every agent**: follow the
original prompts in `references/agent_prompts.md` step by step, write each artifact to disk, and
use the bundled scripts for the parts that need real tools (literature search, LaTeX compile, plots).

Paths below: `SKILL_DIR` = this skill's folder (`.claude/skills/paper-orchestra`),
`REPO` = repository root. LaTeX templates live in `REPO/paper-orchestra/templates/<venue>/`
(`template.tex`, `guidelines.md`, `.sty`, `.bst`). If that folder is missing, ask the user for a template.

## 0. Setup check (do this first, silently fix what you can)

```bash
which latexmk pdflatex bibtex || apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  texlive-latex-base texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended \
  texlive-science texlive-bibtex-extra latexmk
python3 -c "import pymupdf, matplotlib, numpy" || pip install -q PyMuPDF matplotlib numpy
```
Only install what `which`/import reports missing. If LaTeX cannot be installed, still produce
`paper.tex` + `references.bib` and tell the user it was not compiled.

## 1. Gather inputs

Required (ask in one message if missing; accept pasted text or file paths):
- **idea** — problem, hypothesis, method, equations, contributions → save as `raw_materials/idea.md`
- **experimental log** — setups, datasets, baselines, all numbers, ablations, observations → `raw_materials/experimental_log.md`

Optional: `raw_materials/figures/*.png` + captions (`figures/info.json`: `[{"name": "x.png", "caption": "..."}]`),
target venue (default `cvpr2025`; also `iclr2025`, or a user template dir), research cutoff date
(default: 1 month before today, `YYYY-MM`), whether to generate plots (default yes), paper language
(default English — the venue guidelines require English).

A worked example of raw materials: `REPO/paper-orchestra/frontend/examples/cvpr_example.json`
(keys `idea`, `log`). Use it for a demo run if the user just wants to try the skill.

Create the run directory (default `REPO/papers_out/<short_slug>/`):
```
<run>/raw_materials/{idea.md, experimental_log.md, figures/}
<run>/work/          # outline.json, lit_pool.json, citation_map.json, reviews, worklogs
<run>/paper/         # copy of the template + figures/ ; final paper.tex, references.bib, paper.pdf
```
```bash
cp -r REPO/paper-orchestra/templates/<venue>/. <run>/paper/ && mv <run>/paper/template.tex <run>/paper/paper.tex
mkdir -p <run>/paper/figures && cp -r <run>/raw_materials/figures/. <run>/paper/figures/ 2>/dev/null || true
```

**Hard rules for every step** (from the original prompts — read `## 0` of agent_prompts.md):
- Use ONLY the provided materials. Never import facts, numbers or claims from your memory of any real paper, even if you recognise the work.
- Every number in the paper must appear in `experimental_log.md`. Never invent experiments, baselines, or results.
- Fully anonymous (double-blind): no author names, affiliations, emails, acknowledgements. Replace the template's placeholder author block with `Anonymous Authors` / the venue's anonymous form.
- Cite ONLY keys present in `work/citation_map.json` / `paper/references.bib`, which come exclusively from real search results (step 3). Never fabricate a reference.

Keep the user posted with one short line per step (e.g. "Outline ready: 4 figures, 38 citation hints. Searching literature…").

## 2. Outline Agent → `work/outline.json`

Read `idea.md`, `experimental_log.md`, `paper/paper.tex`, `paper/guidelines.md`.
Follow prompt **§1 Outline Agent** exactly (`{cutoff_date}` = cutoff). Output one JSON object with
`plotting_plan`, `intro_related_work_plan`, `section_plan`. Validate it parses (`python3 -m json.tool`).
If plotting is off, set `plotting_plan: []` and rely on the user's figures only.

## 3. Literature Review Agent → `work/citation_map.json`, `paper/references.bib`, Intro + Related Work

**3a. Search** (real APIs; no key needed; set `SEMANTIC_SCHOLAR_API_KEY` env var if available for better results):
```bash
S=SKILL_DIR/scripts/lit_search.py
python3 $S search --pool <run>/work/lit_pool.json --cutoff <YYYY-MM> --tag intro  "<query>" "<query>" ...
python3 $S search --pool <run>/work/lit_pool.json --cutoff <YYYY-MM> --tag related_<n> "<query>" ...
python3 $S title  --pool <run>/work/lit_pool.json --cutoff <YYYY-MM> "Exact Paper Title" ...
```
- Run every `search_directions` query (tag `intro`), every `sota_investigation_mission` (turn into 1–2 queries)
  and `limitation_search_queries` (tag `related_<subsection#>`).
- For every `citation_hints` entry in `section_plan`: if it names an exact title, use `title`; if it is
  "research paper or technical report introducing 'X'", search `"X"` and pick the canonical original paper.
  `title` prints NOT FOUND when no confident match exists — then drop that citation, never invent it.
- Note: foundational works (e.g. a dataset from 2009) are allowed before the cutoff; papers after the cutoff are filtered out automatically.
- Reformulate weak queries and search again until you have roughly 10–20 intro papers and 30–50 related-work papers (fewer is fine for niche topics; quality over count).

**3b. Select.** Read the printed titles/abstracts. Write `<run>/work/selected_keys.txt` (one key per line)
with only clearly relevant papers + every confirmed citation-hint paper. Then:
```bash
python3 $S bib --pool <run>/work/lit_pool.json --keys <run>/work/selected_keys.txt \
   --bib <run>/paper/references.bib --map <run>/work/citation_map.json
```
Add a `citation_candidates: [keys]` list to each matching subsection in `outline.json` (this is what the
original agent's `outline_v1.json` does).

**3c. Write Intro + Related Work** following prompt **§2** (`{paper_count}` = number of selected papers,
`{min_cite_paper_count}` = ~90% of them, `{cutoff_date}`). Edit only those two sections of `paper/paper.tex`;
base every sentence about a cited work on its abstract in `citation_map.json`.

## 4. Plotting Agent (if enabled) → `paper/figures/*.png` + `paper/figures/info.json`

For each `plotting_plan` item:
1. `plot` type: write `work/plots/<figure_id>.py` (matplotlib, data copied verbatim from `experimental_log.md`,
   figsize matching `aspect_ratio`, readable fonts ≥ 8pt at column width, colour-blind-safe palette, no title
   inside the image — the caption carries it), run it, save `paper/figures/<figure_id>.png` at 300 dpi.
   `diagram` type (method overview etc.): draw it with matplotlib patches/arrows (or TikZ in the paper), boxes
   labelled with the method's actual component names from `idea.md`.
2. **Critic loop (max 3 rounds):** open the PNG with the Read tool, check: data matches the log exactly, labels/
   legend legible, nothing clipped/overlapping, supports the figure's `objective`. If anything is off, fix and re-render.
3. Write a factual caption (no "Figure N:" prefix). Append `{"name": "<figure_id>.png", "caption": "..."}` to `info.json`.
Also include user-supplied figures in `info.json`.

## 5. Section Writing Agent → complete `paper/paper.tex`

Follow prompt **§3** with `outline.json`, `idea.md`, `experimental_log.md`, `citation_map.json`,
`guidelines.md`, and the figures list (look at each PNG with Read before describing it). Fill title,
abstract, method, experiments (booktabs tables with exact numbers), conclusion, optional appendix after
references. Keep the already-written Intro/Related Work, the preamble and the template style.
Use every figure exactly once, each before the Conclusion, referenced in the text.

Then compile:
```bash
python3 SKILL_DIR/scripts/compile_check.py <run>/paper --page-limit <N from guidelines.md> --png
```
Fix every reported PROBLEM (errors, undefined/missing cite keys, missing figures, figures after Conclusion,
blank pages) and recompile until it prints `OK`. Keep a copy: `cp paper.tex ../work/draft_v0.tex`.

## 6. Content Refinement loop (max 3 iterations) — prompts §5 then §4a

For iteration i = 1..3:
1. **Review** the current PDF as the reviewer in prompt **§5** (be genuinely critical; judge only what is
   written). Base it on the compiled paper text (`python3 -c "import pymupdf;print(''.join(p.get_text() for p in pymupdf.open('<run>/paper/paper.pdf')))"`)
   and the page images. Save `work/review_v{i-1}.json` with all fields incl. `Overall` (1–10).
2. **Revise** following prompt **§4a**: address weaknesses, weave answers to questions into the text, ignore
   requests for experiments not in the log, keep strengths. Save `work/worklog_v{i}.json`
   (`addressed_weaknesses`, `integrated_answers`, `actions_taken`). Save the previous tex as `work/draft_v{i-1}.tex`.
3. Recompile with `compile_check.py` and fix problems.
4. **Re-review** the new version (fresh, without anchoring to the old score) → `work/review_v{i}.json`.
   Accept if `Overall` rose; if it fell, or the sum of sub-score drops exceeds the sum of gains, revert to
   `draft_v{i-1}.tex`. Stop early when the score stops improving or there is nothing substantive left.

## 7. Format Agent — prompt §4b

Run `compile_check.py --png`, look at every page image, and fix layout per prompt **§4b**: page limit
(main content up to References ≤ guideline limit — condense prose, never cut contributions/results/abstract/
conclusion), overflowing tables (`\small`, `tabularx`, `\resizebox`), figure/table placement, orphaned headings,
typos, cite keys. Repeat until compile is clean and the page limit holds (up to 3 rounds). Save `work/format_worklog.json`.

## 8. Deliver

- Final files: `<run>/paper/paper.tex`, `references.bib`, `figures/`, `paper.pdf`. Copy the PDF to `<run>/final_paper.pdf`.
- Clean LaTeX aux files (`latexmk -c` in `<run>/paper`); delete `<run>/paper/pages/`.
- Send the PDF to the user (SendUserFile, display render) and summarise in a few lines: title, page count,
  #citations, figures, reviewer score trajectory (e.g. 5 → 6 → 7), and anything from the log you could not
  support or any citation hints that were not found.
- Offer to commit `<run>/` to the repository (do not commit unasked).

## Original CLI (optional)

If the user explicitly wants the original Google implementation and has API keys, it still works:
`cd REPO/paper-orchestra && python paper_writing_cli.py --raw_materials_dir ... --latex_template_dir templates/cvpr2025 --output_dir ... --use_plotting true`
with `GEMINI_API_KEY` (or Vertex) / `OPENAI_API_KEY` and `SEMANTIC_SCHOLAR_API_KEY` in `paper-orchestra/.env`.
The skill above does not need any of these.
