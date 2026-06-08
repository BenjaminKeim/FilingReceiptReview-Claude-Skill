# Filing Receipt Review — Claude Code Skill

A [Claude Code](https://claude.ai/claude-code) skill that compares a USPTO **Filing Receipt** against the corresponding **Application Data Sheet (ADS)** and produces a line-by-line comparison table flagging any discrepancies.

## What It Does

After a U.S. patent application is filed, the USPTO issues a Filing Receipt recording the inventors, title, docket number, priority claims, and other bibliographic data it captured from the ADS. Errors on the Filing Receipt must be caught early and corrected promptly — especially for inventor names and priority claims.

This skill automates that review by:

1. Extracting all structured data from the ADS using its embedded **XFA datasets stream** (no Adobe Acrobat required)
2. Extracting data from the Filing Receipt — handles both **text-based** and **image-only (scanned)** receipts
3. Producing a **Markdown comparison table** with OK/DISCREPANCY/CRITICAL DISCREPANCY status for every field

### Fields Compared

| Field | Notes |
|---|---|
| Title | Case-insensitive exact match |
| Docket Number | Exact match; handles `ATTY DOCKET NO`, `Attorney Docket Number`, and `Docket Number` heading variants |
| Applicant / Assignee | Org name containment match (receipt appends city/state) |
| Customer Number | Correspondence customer number |
| ADS Signature Date vs. Filing Date | Should match; mismatch may indicate a re-dating issue |
| Inventor Count | Total inventors |
| Inventor N — Name | Levenshtein edit-distance comparison; 1–2 char differences flagged with OCR-artifact note |
| Inventor N — City, Country | ADS ISO country code expanded to full name before comparing |
| Domestic Benefit Claim | None / application number(s) — **Critical** |
| Foreign Priority Claim | None / application number(s) + country — **Critical** |
| Non-Publication Request | Yes/No (only compared if receipt explicitly records it) |

Critical fields (benefit/priority) are flagged as **[CRITICAL DISCREPANCY]** and grouped separately in the output with MPEP 503 remediation instructions.

## Requirements

- Python 3.8+
- [Claude Code](https://claude.ai/claude-code) with this skill installed

**Python dependencies:**

```bash
pip install PyPDF2 pdfplumber pypdfium2
```

| Package | Purpose |
|---|---|
| `PyPDF2` | XFA stream extraction from the ADS |
| `pdfplumber` | Text extraction from text-based Filing Receipts (handles column table layout) |
| `pypdfium2` | Renders image-only Filing Receipts to PNG for OCR or visual review |

Optional (improves image-only receipt handling significantly):

```bash
pip install pytesseract
# Windows: also install Tesseract-OCR from https://github.com/UB-Mannheim/tesseract/wiki
```

## Installation

1. Clone this repository into your Claude Code skills directory:

   **macOS / Linux:**
   ```bash
   git clone https://github.com/BenjaminKeim/Filing-receipt-validation.git \
       ~/.claude/skills/filing_receipt_review
   ```

   **Windows:**
   ```powershell
   git clone https://github.com/BenjaminKeim/Filing-receipt-validation.git `
       "$env:USERPROFILE\.claude\skills\filing_receipt_review"
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Claude Code will automatically detect the skill via `SKILL.md` and make it available in all sessions.

## Usage

In Claude Code, supply the two PDF files and ask for a comparison:

```
@ads.pdf @filing_receipt.pdf  compare these
```

or just:

```
review the filing receipt against the ADS
```

Claude Code invokes the skill automatically, runs the script, and displays the comparison table inline.

### Text-Based Filing Receipts

When the USPTO Filing Receipt contains extractable text, the script performs the full comparison and prints the Markdown table automatically (exit code 0).

### Image-Only (Scanned) Filing Receipts

USPTO Filing Receipts are frequently issued as scanned image PDFs with no extractable text. In this case the script:

1. Extracts and prints all ADS data (always works — reads the XFA stream directly)
2. Renders the receipt pages to PNG images at high resolution using `pypdfium2` (no Poppler or Ghostscript required)
3. **Attempts Tesseract OCR** on the rendered images; if successful, runs the full automated comparison
4. If Tesseract is unavailable or yields insufficient text, exits with code 2 — Claude Code reads the rendered images with its vision capability and completes the comparison manually

No manual intervention needed in either case — Claude Code handles the OCR/vision step automatically per the `SKILL.md` workflow.

## Technical Background: XFA Dynamic PDFs

USPTO web-fillable forms — including the ADS (PTO/AIA/14), declarations (PTO/AIA/01, /02), and the Power of Attorney (PTO/AIA/82) — are **XFA dynamic PDFs**. Standard PDF text extractors return only a "Please wait..." placeholder. You must read the embedded XFA datasets XML stream directly.

### The Two-Datasets Problem

A filled ADS contains **two** `xfa:datasets` streams: an empty skeleton written when the form was first loaded, and the actual submitted data appended as an incremental PDF update. The filled data stream is always substantially larger. Extracting the first match gives you the empty template; you must collect all matches and take the largest:

```python
import PyPDF2
import xml.etree.ElementTree as ET

reader = PyPDF2.PdfReader(ads_path)
xfa    = reader.trailer['/Root']['/AcroForm']['/XFA']
items  = list(xfa)   # alternating [name, stream_ref, name, stream_ref, ...]

# Collect ALL 'datasets' entries — the filled form is in an incremental update
# appended after the blank template. Return the largest (the filled data).
candidates = []
for i in range(0, len(items), 2):
    if str(items[i]) == 'datasets':
        xml_bytes = items[i + 1].get_object().get_data()
        candidates.append(xml_bytes.decode('utf-8', errors='replace'))

xml_str = max(candidates, key=len) if candidates else None
root = ET.fromstring(xml_str)
```

If structured XFA navigation yields nothing (e.g. compressed ObjStm/xref-stream PDFs), the script falls back to scanning every FlateDecode stream in the raw file bytes and returning the largest block containing `<xfa:datasets`.

### Traversal Helper

All ADS field lookups use local tag names (ignoring XML namespace prefixes) to stay robust across namespace variations:

```python
def find_first(root, name):
    for elem in root.iter():
        tag = elem.tag
        local = tag.split('}', 1)[1] if '}' in tag else tag
        if local == name:
            return elem
    return None
```

See `scripts/review_filing_receipt.py` for the full implementation including all ADS field locations within the XFA schema.

## Repository Structure

```
Filing-receipt-validation/
├── README.md                       # This file
├── LICENSE                         # MIT license
├── SKILL.md                        # Claude Code skill definition (trigger + workflow)
├── requirements.txt                # Python dependencies
└── scripts/
    └── review_filing_receipt.py    # Main comparison script
```

## Standalone Usage

The script can be run directly without Claude Code:

```bash
python scripts/review_filing_receipt.py <ads.pdf> <filing_receipt.pdf>
```

File order doesn't matter — the script auto-detects which is the ADS (XFA form) and which is the Filing Receipt.

**Exit codes:**
- `0` — Comparison table printed to stdout (Markdown)
- `2` — Image-only receipt; ADS summary + rendered image paths output; vision fallback needed
- `1` — Fatal error (file not found, wrong file types, etc.)

## Changelog

### v1.3.0 — Code quality, security, and performance

- **Security:** Rendered PNG images of filing receipts (written during the image-only OCR path) now contain a privacy warning in output and are automatically deleted after successful Tesseract OCR. Previously they were left on disk indefinitely.
- **Security:** Fixed inconsistent error sentinel in ODP API lookup — `error` is now consistently `None` on success, non-empty string on failure.
- **Performance:** Fixed `pdfplumber` calling `extract_text()` twice per page (once in the `if` condition, once to capture the value). Text extraction now calls it once per page.
- **Performance:** Document-type detection patterns (`_ADDITIONAL_DOC_PATTERNS`) are now pre-compiled `re.Pattern` objects at module load rather than raw strings re-compiled on every call to `detect_additional_documents()`.
- **Performance:** Replaced `any([list])` constructs with direct boolean short-circuit expressions throughout `parse_ads()`.
- **Comments:** Added explanatory comments for non-obvious behaviors: why `pdfplumber` is preferred over `PyPDF2` for receipt text extraction (column table layout), Python `for/else` semantics in the ODP retry loop, and the PII sensitivity of rendered temp images.

### v1.2.0 — Robustness improvements from real-world testing

- **XFA extraction fix (critical):** The script previously returned blank ADS data for any filled ADS form. USPTO ADS forms contain two `xfa:datasets` streams — an empty skeleton and the filled data appended as an incremental PDF update. The script now collects all `datasets` entries and returns the largest, ensuring the filled form data is used. A raw-byte brute-force fallback handles edge-case PDFs where structured XFA navigation misses the filled stream.
- **Inventor name fuzzy matching:** Name comparison now uses Levenshtein edit distance. Mismatches with an edit distance of 1–2 characters are still flagged as `[DISCREPANCY]` but include an explanatory note: *"could be OCR artifact (e.g. i/l/1 substitution)"*. This is common when filing receipts are scanned — OCR frequently confuses lowercase `i`, `l`, and digit `1`.
- **Docket number parsing:** Added `Docket Number` / `Docket No.` as a third fallback heading pattern, in addition to the existing `ATTY DOCKET NO` and `Attorney Docket Number` patterns.

### v1.1.0 — Initial public release

- XFA data extraction from ADS (PTO/AIA/14)
- Text and image-only filing receipt handling
- Full field comparison: title, docket, applicant, customer number, inventors, priority claims
- USPTO ODP API integration for priority chain date/patent-number verification
- Tesseract OCR fallback for image-only receipts
- Severity tiers: `[DISCREPANCY]` vs `[CRITICAL DISCREPANCY]` for benefit/priority fields

## License

MIT — see [LICENSE](LICENSE).

## Author

[Benjamin Keim](https://github.com/BenjaminKeim) · Newport IP, LLC
