---
name: filing_receipt_review
description: Compare a USPTO Filing Receipt against the ADS for the same application. Use when the user asks to compare, check, or review a filing receipt against the ADS, or wants to verify that the USPTO recorded the correct inventors, title, docket number, applicant, priority claims, or other data.
---

# Filing Receipt Review

## Overview

This skill compares a USPTO Filing Receipt against the corresponding ADS (Application Data Sheet, PTO/AIA/14) and produces a line-by-line comparison table. It pays particular attention to inventor names, residency, priority claims, and applicant. The ADS is always an XFA dynamic PDF; the filing receipt may be either text-based or image-only (scanned) — the skill handles both.

## When to Use

Trigger whenever the user:
- Asks to compare a filing receipt to the ADS
- Asks to "check" or "review" a filing receipt
- Wants to confirm what the USPTO recorded (inventors, title, docket, priority)
- Uses phrases like "verify the receipt", "does the receipt match the ADS", "any discrepancies on the receipt"

## Input Files

Two PDF files are required:
1. **The ADS** — USPTO web-fillable PTO/AIA/14 (XFA dynamic PDF). Auto-detected by checking for an embedded XFA stream.
2. **The Filing Receipt** — USPTO Filing Receipt PDF (typically 3–6 pages). May be text-searchable or image-only. The script handles both.

Files may be passed in either order — the script auto-detects which is which.

## Workflow

### Step 1: Identify the Two Files

Confirm both file paths before running. The user typically @-mentions them.

### Step 2: Install Dependencies (if needed)

```
pip install PyPDF2 pdfplumber pypdfium2 --break-system-packages
```

If the script exits with `ModuleNotFoundError`, install the missing package and re-run.

### Step 3: Run the Script

```bash
python "C:\Users\Newpo\.claude\skills\filing_receipt_review\scripts\review_filing_receipt.py" "<ads_path>" "<receipt_path>"
```

Optionally specify where to save rendered receipt images (default: system temp dir):
```bash
python "..." "<ads_path>" "<receipt_path>" --image-dir "<output_folder>"
```

### Step 4A: Text-Based Filing Receipt (exit code 0)

If the filing receipt contains extractable text, the script prints the full Markdown comparison table to stdout. **Display the table directly in the conversation** — do not paraphrase it. Add one sentence noting the overall result and, if discrepancies exist, the corrective action.

### Step 4B: Image-Only Filing Receipt (exit code 2)

USPTO filing receipts are frequently issued as image-only (scanned) PDFs with no extractable text. When this happens the script:

1. Extracts all ADS data (always works — XFA)
2. Renders the receipt pages to PNG images using pypdfium2 (no poppler needed)
3. Prints the ADS data summary and the image file paths, then exits with code 2

**When you see exit code 2, do the following:**

a. Read the rendered receipt images using the Read tool — focus on pages 1-2 (fee determination table + main receipt page with inventor list).

b. From the images, extract:
   - Application number and filing date (header table, page 2)
   - Confirmation number (page 2)
   - Total claims / independent claims (header table, page 2)
   - Title (near bottom of page 2, before "Preliminary Class")
   - Docket number (header table, page 2 "ATTY DOCKET NO" column)
   - Inventor list — for each: full name, city, country (page 2 under "Inventor(s)")
   - Applicant / assignee (page 2 under "Applicant(s)")
   - Customer number (page 2 under "Power of Attorney")
   - Domestic benefit claims — "None" or application numbers (page 2)
   - Foreign priority claims — "None" or app numbers + countries (page 2)
   - Non-publication request: Yes/No (page 2/3)

c. Compare the extracted receipt data against the ADS summary printed by the script and produce the comparison table using the format below.

### Step 5: Discrepancy Guidance

If the table contains discrepancies, advise:

> To correct a discrepancy, file a request for a corrected Filing Receipt (no fee) with a marked-up ADS showing changes by strike-through (deletions) and underlining (additions), per **MPEP 503**. Submit via USPTO Patent Center using the "Filing Receipt Correction" document code.

For **foreign priority discrepancies** specifically:
> Foreign priority claims under 35 U.S.C. §119(a) must be made within the later of 16 months from the priority date or 4 months from the U.S. filing date. If a priority claim was in the ADS but is absent from the receipt, contact the USPTO promptly — this is time-sensitive.

---

## Comparison Table Format

Use this structure for both the script-generated and the manually-produced tables:

```
## Filing Receipt Review

| | |
|---|---|
| **Application No.** | XX/XXX,XXX |
| **Filing Date** | MM/DD/YYYY |
| **Confirmation No.** | XXXX |
| **Total / Independent Claims** | XX / X |
| **ADS** | `filename.pdf` |
| **Filing Receipt** | `filename.pdf` |

> **N discrepancies found — including M CRITICAL discrepancies in benefit/priority information** (see [CRITICAL DISCREPANCY] rows below)
  — OR —
> **N discrepancies found** — see rows marked [DISCREPANCY] below.
  — OR —
> **All checked fields are consistent with the Filing Receipt.** [OK]

**Match symbols:** `[OK]` / `[DISCREPANCY]` / `[CRITICAL DISCREPANCY]`
Domestic Benefit and Foreign Priority rows use `[CRITICAL DISCREPANCY]` when they do not match; `[DISCREPANCY]` is used when partial signals are detected but the entry could not be fully parsed (manual review required).

| Field | ADS | Filing Receipt | Match |
|---|---|---|:---:|
| Title | ... | ... | [OK] |
| Docket Number | ... | ... | [OK] |
| Applicant / Assignee | ... | ... | [OK] |
| Customer Number | ... | ... | [OK] |
| ADS Signature Date vs. Filing Date | ... | ... | [OK] |
| Inventor Count | ... | ... | [OK] |
| Inventor 1 — Name | ... | ... | [OK] |
| Inventor 1 — City, Country | ... | ... | [OK] |
| ... | ... | ... | ... |
| Domestic Benefit Claim | None | None | [OK] |
| Foreign Priority Claim | None | None | [OK] |
| Non-Publication Request | No | No | [OK] |
```

---

## Fields Compared

| Field | Notes |
|---|---|
| Title | Case-insensitive exact match |
| Docket Number | Exact match |
| Applicant / Assignee | ADS org name contained-in receipt applicant line (receipt appends city/state) |
| Customer Number | From ADS correspondence field; from receipt "Power of Attorney" line |
| ADS Signature Date vs. Filing Date | ADS date (YYYY-MM-DD) reformatted to MM/DD/YYYY for comparison; note indicates whether ADS was signed before or after the filing date |
| Inventor Count | Total inventors |
| Inventor N — Name | Levenshtein edit distance; 1–2 char differences flagged as `[DISCREPANCY]` with note that it may be an OCR artifact (i/l/1 confusion) |
| Inventor N — City, Country | ADS ISO country code expanded to full name before comparing |
| Domestic Benefit Claim | None / app number(s) |
| Foreign Priority Claim | None / app number(s) + country |
| Non-Publication Request | Yes/No (only shown if found on receipt) |

---

## Known Limitations

- **Image-only receipts**: Handled via pypdfium2 rendering + Claude vision. See Step 4B.
- **Applicant field**: Receipt includes city/state after org name — the script uses containment matching, not exact equality.
- **Country codes**: ADS stores ISO 2-letter codes (e.g., `IL`); script expands to full names (e.g., `ISRAEL`) before comparing.
- **Foreign priority parsing**: Complex multi-application foreign priority chains may not parse cleanly from receipt text. Always verify foreign priority rows manually.
- **Claims counts**: Total and independent claims come only from the receipt — the ADS does not store them. They appear in the table header for reference only.
