# Filing Receipt Review — Claude Code Skill

A [Claude Code](https://claude.ai/claude-code) skill that compares a USPTO **Filing Receipt** against the corresponding **Application Data Sheet (ADS)** and produces a line-by-line comparison table flagging any discrepancies.

## What It Does

After a U.S. patent application is filed, the USPTO issues a Filing Receipt recording the inventors, title, docket number, priority claims, and other bibliographic data it captured from the ADS. Errors on the Filing Receipt must be caught early and corrected promptly — especially for inventor names and priority claims.

This skill automates that review by:

1. Extracting all structured data from the ADS using its embedded **XFA datasets stream** (no Adobe Acrobat required)
2. Extracting data from the Filing Receipt — handles both **text-based** and **image-only (scanned)** receipts
3. Producing a **Markdown comparison table** with OK/DISCREPANCY status for every field

### Fields Compared

| Field | Notes |
|---|---|
| Title | Case-insensitive exact match |
| Docket Number | Exact match |
| Applicant / Assignee | Org name containment match (receipt appends city/state) |
| Customer Number | Correspondence customer number |
| ADS Signature Date vs. Filing Date | Should match; mismatch may indicate a re-dating issue |
| Inventor Count | Total inventors |
| Inventor N — Name | ADS first + middle + last vs. receipt full name |
| Inventor N — City, Country | ADS ISO country code expanded to full name before comparing |
| Domestic Benefit Claim | None / application number(s) |
| Foreign Priority Claim | None / application number(s) + country |
| Non-Publication Request | Yes/No |

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
| `pdfplumber` | Text extraction from text-based Filing Receipts |
| `pypdfium2` | Renders image-only Filing Receipts to PNG for visual review |

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
3. Exits with code 2, instructing Claude Code to read the rendered images with its vision capability and complete the comparison

No manual intervention needed — Claude Code handles the vision step automatically per the `SKILL.md` workflow.

## Technical Background: XFA Dynamic PDFs

USPTO web-fillable forms — including the ADS (PTO/AIA/14), declarations (PTO/AIA/01, /02), and the Power of Attorney (PTO/AIA/82) — are **XFA dynamic PDFs**. Standard PDF text extractors return only a "Please wait..." placeholder. You must read the embedded XFA datasets XML stream directly:

```python
import PyPDF2
import xml.etree.ElementTree as ET

reader = PyPDF2.PdfReader(ads_path)
xfa    = reader.trailer['/Root']['/AcroForm']['/XFA']
items  = list(xfa)                           # array of [name, stream, name, stream, ...]
for i in range(0, len(items), 2):
    if str(items[i]) == 'datasets':          # look up by name, not by content scan
        xml_str = items[i+1].get_data().decode('utf-8', errors='replace')
        break

root = ET.fromstring(xml_str)                # parses directly — no whitespace cleanup needed

# Strip XML namespace prefixes for easy traversal
def localname(elem):
    tag = elem.tag
    return tag.split('}', 1)[1] if '}' in tag else tag

def find_first(elem, name):
    for child in elem.iter():
        if localname(child) == name:
            return child
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

## License

MIT — see [LICENSE](LICENSE).

## Author

[Benjamin Keim](https://github.com/BenjaminKeim) · Newport IP, LLC
