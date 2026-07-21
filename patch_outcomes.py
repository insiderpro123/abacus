"""One-off content patch: refine the outcome wording for sub-points 1.3, 4.1, 4.2
(review feedback). Updates ONLY those three rows - nothing else is touched, and it
is safe to run more than once (idempotent).

Run against the target database via the DATABASE_URL env var (same as import_data.py):

  # Local SQLite (default - no DATABASE_URL set)
  python patch_outcomes.py

  # Production Postgres (use the Render "External Database URL")
  #   Windows PowerShell:
  #     $env:DATABASE_URL="postgresql://...external..."; python patch_outcomes.py
  #   macOS / Linux:
  #     DATABASE_URL="postgresql://...external..." python patch_outcomes.py
"""

from models import SessionLocal, Subprocess

OUTCOMES = {
    "1.3": "Create a summary on Confluence of the data received to date, making the "
           "current contractual status of all 3rd-party suppliers clear.",
    "4.1": "Document a clear strategic view of what good looks like for this operational "
           "and/or supply chain area, evidencing our hypothesis and a milestone plan to "
           "deliver value.",
    "4.2": "Ensure the proposed strategy is fully backed by the evidence and metrics "
           "revealed in the data analytics phase (Step 2).",
}


def main():
    with SessionLocal.begin() as s:
        for code, text in OUTCOMES.items():
            sp = s.get(Subprocess, code)
            if not sp:
                print(f"  {code}: NOT FOUND - skipped")
                continue
            old = (sp.outcomes or "").strip()
            sp.outcomes = text
            print(f"  {code}: updated")
            print(f"      was: {old[:90] or '(blank)'}")
            print(f"      now: {text[:90]}")
    print("Done.")


if __name__ == "__main__":
    main()
