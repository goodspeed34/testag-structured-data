# Structured Data Instructions: TESTAG

This repository is the compact contest-results archive and PostgreSQL builder for TESTAG. When checked out inside the combined workspace, also follow the workspace-level `AGENTS.md`.

## Project contract

- Keep the committed score archive under `data/` as the source of truth. PostgreSQL is derived.
- Use the layout `data/<contest>/<year>/<mode>/{scores.csv,meta.json}`. Keep only score/result fields and the eight rank fields documented in `ARCHITECTURE.md`.
- Each `meta.json` must contain only `retrieved_at_utc` and `source`.
- Preserve source provenance, source and normalized callsigns, score details, eligibility, QSOs, multipliers, work time, operator memberships, and explicit DXCC/entity values. Do not guess at scores, categories, eligibility, corrections, omitted operators, or DXCC entities.
- Calculate category worldwide and in-DXCC ranks using eligible score rows. Calculate overlay ranks only for an explicit overlay label/category. Keep source-reported values separate from calculated ranks.
- Preserve the data license and attribution: CC BY-NC-ND 4.0 International; Gong BI1XJT <BI1XJT@rad1o.cn>.
- Do not add source collectors, scrapers, or refresh automation. The only executable project script is `build_db.py`.

## Database builder

- Use `python build_db.py --validate-only` for offline archive validation.
- Build PostgreSQL only when explicitly requested and only with a disposable or approved database URL. The builder drops and recreates the TESTAG tables defined by `db/schema.sql` in the selected database.
- Never point tests or ad hoc imports at production. This repository has no live source checks or scraping tests.

## Documentation and continuity

For changes to data fields, ranking, schema, validation, or database behavior, update both `ARCHITECTURE.md` and `README.md`.
Use `.vault-notes.md` for short-lived decisions and remaining work; keep durable contract decisions in the architecture and README.
