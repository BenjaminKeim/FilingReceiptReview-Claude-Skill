#!/usr/bin/env python3
"""
Filing Receipt Review
---------------------
Compares a USPTO ADS (XFA PDF, PTO/AIA/14) against the corresponding
Filing Receipt and produces a Markdown comparison table.

Usage:
    python review_filing_receipt.py <ads.pdf> <filing_receipt.pdf>

Exit codes:
    0  — comparison table printed to stdout
    2  — filing receipt is image-only; ADS data printed + receipt images
         saved to --image-dir (default: system temp); Claude should read
         the images visually and complete the comparison
    1  — fatal error
"""

import io
import json
import re
import sys
import tempfile
import argparse
from pathlib import Path
from typing import Optional, List, Dict, Any

# UTF-8 stdout (needed on Windows where default is cp1252)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# ── required dependency ──────────────────────────────────────────────────────────
try:
    import PyPDF2
except ImportError:
    sys.exit(
        "[filing-receipt-review] PyPDF2 is not installed.\n"
        "  Install with: pip install PyPDF2 --break-system-packages\n"
    )

try:
    import pdfplumber
    _PDFPLUMBER = True
except ImportError:
    _PDFPLUMBER = False

try:
    import pypdfium2 as pdfium
    _PDFIUM = True
except ImportError:
    _PDFIUM = False

import xml.etree.ElementTree as ET


# ════════════════════════════════════════════════════════════════════════════════
# XFA helpers (ADS extraction)
# ════════════════════════════════════════════════════════════════════════════════

def _is_xfa(pdf_path: Path) -> bool:
    try:
        with open(pdf_path, 'rb') as f:
            r = PyPDF2.PdfReader(f)
            root = r.trailer['/Root']
            if hasattr(root, 'get_object'):
                root = root.get_object()
            if '/AcroForm' not in root:
                return False
            af = root['/AcroForm']
            if hasattr(af, 'get_object'):
                af = af.get_object()
            return '/XFA' in af
    except Exception:
        return False


def _extract_xfa_datasets(pdf_path: Path) -> Optional[str]:
    try:
        with open(pdf_path, 'rb') as f:
            r = PyPDF2.PdfReader(f)
            af = r.trailer['/Root']['/AcroForm']
            if hasattr(af, 'get_object'):
                af = af.get_object()
            xfa = af.get('/XFA')
            if xfa is None:
                return None
            if hasattr(xfa, 'get_object'):
                xfa = xfa.get_object()
            items = list(xfa)
            for i in range(0, len(items), 2):
                if str(items[i]) == 'datasets' and i + 1 < len(items):
                    stream = items[i + 1]
                    if hasattr(stream, 'get_object'):
                        stream = stream.get_object()
                    return stream.get_data().decode('utf-8', errors='replace')
    except Exception as e:
        print(f"  WARNING: XFA extraction failed: {e}", file=sys.stderr)
    return None


def _ln(elem) -> str:
    t = elem.tag
    return t.split('}', 1)[1] if '}' in t else t


def _find(elem, name) -> Optional[ET.Element]:
    for child in elem.iter():
        if _ln(child) == name:
            return child
    return None


def _txt(elem, name) -> str:
    child = _find(elem, name) if elem is not None else None
    return (child.text or '').strip() if child is not None else ''


# ════════════════════════════════════════════════════════════════════════════════
# ADS data extraction
# ════════════════════════════════════════════════════════════════════════════════

def parse_ads(pdf_path: Path) -> Dict[str, Any]:
    xml_str = _extract_xfa_datasets(pdf_path)
    if not xml_str:
        sys.exit(f"ERROR: Could not extract XFA data from {pdf_path.name}.\n"
                 "       Is this the ADS (PTO/AIA/14) file?")

    root = ET.fromstring(xml_str)
    req = _find(root, 'us-request')
    if req is None:
        sys.exit("ERROR: ADS XML does not contain a <us-request> element.")

    data: Dict[str, Any] = {
        'title':               _txt(req, 'invention-title'),
        'docket_number':       _txt(req, 'attorney-docket-number'),
        'inventors':           [],
        'assignee_org':        '',
        'customer_number':     '',
        'small_entity':        None,
        'application_type':    '',
        'drawing_sheets':      '',
        'non_publication':     None,
        'domestic_continuity': [],
        'foreign_priority':    [],
        'signature_date':      '',
    }

    # Inventors
    for block in req.iter():
        if _ln(block) != 'sfApplicantInformation':
            continue
        name_el = _find(block, 'sfApplicantName')
        if name_el is None:
            continue
        first  = _txt(name_el, 'firstName')
        middle = _txt(name_el, 'middleName')
        last   = _txt(name_el, 'lastName')
        if not first and not last:
            continue

        res_type = res_city = res_country = ''
        res_block = _find(block, 'sfAppResChk')
        if res_block is not None:
            res_type = _txt(res_block, 'ResidencyRadio')
            if res_type == 'us-residency':
                us = _find(res_block, 'sfUSres')
                res_city    = _txt(us, 'rsCityTxt')
                res_country = _txt(res_block, 'rsCtryTxt')
            else:
                non_us = _find(res_block, 'sfNonUSRes')
                if non_us is not None:
                    res_city    = _txt(non_us, 'nonresCity')
                    res_country = _txt(non_us, 'nonresCtryList')

        data['inventors'].append({
            'first': first, 'middle': middle, 'last': last,
            'city': res_city, 'country': res_country,
        })

    # Customer number
    cust = _find(req, 'sfCorrCustNo')
    if cust is not None:
        data['customer_number'] = _txt(cust, 'customerNumber')
    if not data['customer_number']:
        atty = _find(req, 'sfAttorny')
        if atty is not None:
            data['customer_number'] = _txt(atty, 'customerNumberTxt')

    # App info
    app_pos = _find(req, 'sfAppPos')
    if app_pos is not None:
        small = _txt(app_pos, 'chkSmallEntity')
        data['small_entity']     = (small == '1') if small in ('0', '1') else None
        data['application_type'] = _txt(app_pos, 'application_type')
        data['drawing_sheets']   = _txt(app_pos, 'us-total_number_of_drawing-sheets')

    pub = _find(req, 'sfPub')
    if pub is not None:
        np = _txt(pub, 'nonPublication')
        data['non_publication'] = (np == '1') if np in ('0', '1') else None

    # Assignee
    assignee = _find(req, 'sfAssigneeInformation')
    if assignee is not None:
        data['assignee_org'] = _txt(assignee, 'orgName')

    # Domestic continuity
    for cont in req.iter():
        if _ln(cont) != 'sfDomesticContinuity':
            continue
        info = _find(cont, 'sfDomesContInfo')
        if info is not None:
            app_num   = _txt(info, 'domappNumber')
            cont_type = _txt(info, 'domesContList')
            date      = _txt(info, 'DateTimeField1')
            if any([app_num, cont_type, date]):
                data['domestic_continuity'].append(
                    {'app_number': app_num, 'type': cont_type, 'date': date}
                )

    # Foreign priority
    for fpr in req.iter():
        if _ln(fpr) != 'sfForeignPriorityInfo':
            continue
        app_num = _txt(fpr, 'frprAppNum')
        country = _txt(fpr, 'frprctryList')
        date    = _txt(fpr, 'frprParentDate')
        if any([app_num, country, date]):
            data['foreign_priority'].append(
                {'app_number': app_num, 'country': country, 'date': date}
            )

    # Signature date
    sig = _find(req, 'sfSignature')
    if sig is not None:
        sfSig = _find(sig, 'sfSig')
        if sfSig is not None:
            data['signature_date'] = _txt(sfSig, 'date')

    return data


# ════════════════════════════════════════════════════════════════════════════════
# Filing receipt: text extraction + parsing
# ════════════════════════════════════════════════════════════════════════════════

def _extract_receipt_text(pdf_path: Path) -> str:
    """Try to extract text from the filing receipt. Returns '' if image-only."""
    if _PDFPLUMBER:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                parts = [p.extract_text() for p in pdf.pages if p.extract_text()]
                text = '\n'.join(parts)
                if text.strip():
                    return text
        except Exception:
            pass
    # Fallback: PyPDF2
    with open(pdf_path, 'rb') as f:
        reader = PyPDF2.PdfReader(f)
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
    return text.strip()


def render_receipt_images(pdf_path: Path, out_dir: Path) -> List[Path]:
    """Render the filing receipt to PNG images using pypdfium2. Returns image paths."""
    if not _PDFIUM:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pdfium.PdfDocument(str(pdf_path))
    paths = []
    for i, page in enumerate(doc):
        bitmap = page.render(scale=3.0)   # ~216 dpi — clear enough for OCR/vision
        pil_img = bitmap.to_pil()
        img_path = out_dir / f"receipt_page_{i + 1}.png"
        pil_img.save(str(img_path))
        paths.append(img_path)
    return paths


_COUNTRY_MAP = {
    'IL': 'ISRAEL',   'US': 'UNITED STATES',  'GB': 'UNITED KINGDOM',
    'DE': 'GERMANY',  'FR': 'FRANCE',          'JP': 'JAPAN',
    'CN': 'CHINA',    'KR': 'SOUTH KOREA',     'CA': 'CANADA',
    'AU': 'AUSTRALIA','IN': 'INDIA',            'SE': 'SWEDEN',
    'FI': 'FINLAND',  'NL': 'NETHERLANDS',     'CH': 'SWITZERLAND',
    'EP': 'EUROPEAN PATENT OFFICE',
    'TW': 'TAIWAN',   'SG': 'SINGAPORE',       'IE': 'IRELAND',
    'IT': 'ITALY',    'ES': 'SPAIN',            'BR': 'BRAZIL',
    'MX': 'MEXICO',   'RU': 'RUSSIA',           'SA': 'SAUDI ARABIA',
    'AE': 'UNITED ARAB EMIRATES',               'TR': 'TURKEY',
}


def _expand_country(code_or_name: str) -> str:
    s = code_or_name.strip().upper()
    return _COUNTRY_MAP.get(s, s)


def parse_receipt(text: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        'application_number':  '',
        'filing_date':         '',
        'confirmation_number': '',
        'title':               '',
        'docket_number':       '',
        'total_claims':        '',
        'independent_claims':  '',
        'inventors':           [],
        'applicant':           '',
        'customer_number':     '',
        'domestic_benefit':    [],
        'foreign_priority':    [],
        'non_publication':     None,
        'early_publication':   None,
        'domestic_none':       False,
        'foreign_none':        False,
    }

    m = re.search(r'\b(\d{2}/\d{3},\d{3})\b', text)
    if m:
        data['application_number'] = m.group(1)

    # Filing date: labeled column, or date that immediately follows the app number
    m = re.search(
        r'FILING\s+or\s+\n?\s*371\(c\)\s+DATE[^\d]*?(\d{2}/\d{2}/\d{4})',
        text, re.I | re.DOTALL
    )
    if not m:
        m = re.search(
            r'\b\d{2}/\d{3},\d{3}\b[^\d\n]*?(\d{2}/\d{2}/\d{4})',
            text, re.DOTALL
        )
    if m:
        data['filing_date'] = m.group(1)

    m = re.search(r'CONFIRMATION\s+NO\.?\s*(\d+)', text, re.I)
    if m:
        data['confirmation_number'] = m.group(1)

    m = re.search(r'ATTY\s+DOCKET\s+NO\.?\s+(\S+)', text, re.I)
    if not m:
        m = re.search(r'Attorney\s+Docket\s+(?:Number|No)\.?\s*:?\s*(\S+)', text, re.I)
    if m:
        data['docket_number'] = m.group(1)

    m = re.search(r'TOT\s+CLAIMS\s+(\d+)', text, re.I)
    if m:
        data['total_claims'] = m.group(1)
    m = re.search(r'IND\s+CLAIMS\s+(\d+)', text, re.I)
    if m:
        data['independent_claims'] = m.group(1)

    m = re.search(
        r'(?:^|\n)\s*Title\s*\n\s*(.*?)(?:\n\s*(?:Preliminary Class|Statement under|$))',
        text, re.I | re.DOTALL | re.MULTILINE
    )
    if m:
        data['title'] = re.sub(r'\s+', ' ', m.group(1)).strip()

    # Inventor(s) block
    inv_m = re.search(
        r'Inventor\(s\)\s*\n(.*?)(?=\n\s*Applicant\(s\)|\n\s*Power of Attorney)',
        text, re.DOTALL | re.I
    )
    if inv_m:
        block = inv_m.group(1)
        entries = re.split(r';\s*\n', block)
        for entry in entries:
            entry = re.sub(r'\s+', ' ', entry).strip().rstrip(';').strip()
            if not entry:
                continue
            parts = [p.strip() for p in entry.split(',')]
            if len(parts) >= 3:
                data['inventors'].append(
                    {'name': parts[0], 'city': parts[1], 'country': parts[2].upper()}
                )
            elif len(parts) == 2:
                data['inventors'].append(
                    {'name': parts[0], 'city': parts[1], 'country': ''}
                )
            elif len(parts) == 1 and parts[0]:
                data['inventors'].append({'name': parts[0], 'city': '', 'country': ''})

    app_m = re.search(
        r'Applicant\(s\)\s*\n\s*(.*?)(?=\n\s*(?:Power of Attorney|Domestic|Foreign|$))',
        text, re.DOTALL | re.I
    )
    if app_m:
        data['applicant'] = re.sub(r'\s+', ' ', app_m.group(1)).strip().rstrip(';').strip()

    m = re.search(r'Customer\s+Number\s+(\d+)', text, re.I)
    if m:
        data['customer_number'] = m.group(1)

    dom_m = re.search(
        r'Domestic Applications for which benefit is claimed.*?'
        r'(?=\n\s*(?:Foreign|$))',
        text, re.DOTALL | re.I
    )
    if dom_m:
        dom_text = dom_m.group(0)
        if re.search(r'[-–]\s*None', dom_text, re.I):
            data['domestic_none'] = True
        else:
            nums = re.findall(r'\b(\d{2}/\d{3},\d{3})\b', dom_text)
            if nums:
                data['domestic_benefit'] = nums

    for_m = re.search(
        r'Foreign Applications for which priority is claimed.*?'
        r'(?=\n\s*(?:Permission|$))',
        text, re.DOTALL | re.I
    )
    if for_m:
        for_text = for_m.group(0)
        if re.search(r'[-–]\s*None', for_text, re.I):
            data['foreign_none'] = True
        else:
            entries = re.findall(r'(\S+)\s+([A-Z]{2})\s+(\d{2}/\d{2}/\d{4})', for_text)
            if entries:
                data['foreign_priority'] = [
                    {'app_number': e[0], 'country': e[1], 'date': e[2]}
                    for e in entries
                ]

    m = re.search(r'Non-Publication Request:\s+(Yes|No)', text, re.I)
    if m:
        data['non_publication'] = m.group(1).strip().lower() == 'yes'

    m = re.search(r'Early Publication Request:\s+(Yes|No)', text, re.I)
    if m:
        data['early_publication'] = m.group(1).strip().lower() == 'yes'

    return data


# ════════════════════════════════════════════════════════════════════════════════
# Comparison logic
# ════════════════════════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or '').strip().upper())


def _fmt_ads_name(inv: Dict) -> str:
    return ' '.join(p for p in [
        inv.get('first', ''), inv.get('middle', ''), inv.get('last', '')
    ] if p)


def _fmt_ads_location(inv: Dict) -> str:
    city    = (inv.get('city') or '').strip()
    country = _expand_country(inv.get('country') or '')
    if city and country:
        return f"{city}, {country}"
    return city or country


Row = Dict[str, Any]


def _row(label: str, ads_val: str, receipt_val: str,
         note: str = '', match_override: Optional[bool] = None) -> Row:
    match = (match_override if match_override is not None
             else _norm(ads_val) == _norm(receipt_val))
    return {
        'label':   label,
        'ads':     ads_val or '*(blank)*',
        'receipt': receipt_val or '*(blank)*',
        'match':   match,
        'note':    note,
    }


def compare(ads: Dict, receipt: Dict) -> List[Row]:
    rows: List[Row] = []

    rows.append(_row('Title', ads['title'], receipt['title']))
    rows.append(_row('Docket Number', ads['docket_number'], receipt['docket_number']))

    # Applicant: receipt includes address suffix after org name → containment match
    ads_org = _norm(ads['assignee_org'])
    rec_app = _norm(receipt['applicant'])
    app_match = bool(ads_org) and (ads_org in rec_app or rec_app.startswith(ads_org))
    rows.append(_row(
        'Applicant / Assignee',
        ads['assignee_org'], receipt['applicant'],
        match_override=app_match,
    ))

    rows.append(_row('Customer Number', ads['customer_number'], receipt['customer_number']))

    # ADS signature date (YYYY-MM-DD) → reformat to MM/DD/YYYY before comparing
    ads_sig = ads.get('signature_date', '')
    if re.match(r'\d{4}-\d{2}-\d{2}', ads_sig):
        y, mo, d = ads_sig.split('-')
        ads_sig_disp = f"{mo}/{d}/{y}"
    else:
        ads_sig_disp = ads_sig
    rows.append(_row(
        'ADS Signature Date vs. Filing Date',
        ads_sig_disp, receipt['filing_date'],
        note='ADS signature date should equal the USPTO filing date',
    ))

    # Inventor count
    rows.append(_row(
        'Inventor Count',
        str(len(ads['inventors'])), str(len(receipt['inventors'])),
    ))

    # Per-inventor
    max_i = max(len(ads['inventors']), len(receipt['inventors']))
    for i in range(1, max_i + 1):
        if i <= len(ads['inventors']) and i <= len(receipt['inventors']):
            ai = ads['inventors'][i - 1]
            ri = receipt['inventors'][i - 1]
            rows.append(_row(f'Inventor {i} — Name',
                             _fmt_ads_name(ai), ri['name']))
            ads_loc = _fmt_ads_location(ai)
            rec_city = ri.get('city', '').strip()
            rec_ctry = ri.get('country', '').strip()
            rec_loc = f"{rec_city}, {rec_ctry}" if rec_city and rec_ctry else rec_city or rec_ctry
            rows.append(_row(
                f'Inventor {i} — City, Country',
                ads_loc, rec_loc,
                match_override=(_norm(ads_loc) == _norm(rec_loc)),
            ))
        elif i <= len(ads['inventors']):
            ai = ads['inventors'][i - 1]
            rows.append({
                'label':   f'Inventor {i} — Name',
                'ads':     _fmt_ads_name(ai),
                'receipt': '*(missing from receipt)*',
                'match':   False,
                'note':    'Present in ADS, absent from Filing Receipt',
            })
        else:
            ri = receipt['inventors'][i - 1]
            rows.append({
                'label':   f'Inventor {i} — Name',
                'ads':     '*(missing from ADS)*',
                'receipt': ri['name'],
                'match':   False,
                'note':    'Present in Filing Receipt, absent from ADS',
            })

    # Domestic benefit
    ads_dom = bool(ads['domestic_continuity'])
    rec_dom = bool(receipt['domestic_benefit'])
    rec_dom_none = receipt.get('domestic_none', False)
    no_dom_both  = not ads_dom and (not rec_dom or rec_dom_none)
    ads_dom_str  = ('None' if not ads_dom
                    else '; '.join(e.get('app_number') or e.get('type') or '?'
                                   for e in ads['domestic_continuity']))
    rec_dom_str  = ('None' if (not rec_dom or rec_dom_none)
                    else '; '.join(receipt['domestic_benefit']))
    rows.append(_row('Domestic Benefit Claim', ads_dom_str, rec_dom_str,
                     match_override=(True if no_dom_both else None)))

    # Foreign priority
    ads_for = bool(ads['foreign_priority'])
    rec_for = bool(receipt['foreign_priority'])
    rec_for_none = receipt.get('foreign_none', False)
    no_for_both  = not ads_for and (not rec_for or rec_for_none)
    ads_for_str  = ('None' if not ads_for
                    else '; '.join(
                        f"{e.get('app_number','')} "
                        f"({_expand_country(e.get('country',''))})"
                        for e in ads['foreign_priority']))
    rec_for_str  = ('None' if (not rec_for or rec_for_none)
                    else '; '.join(
                        f"{e.get('app_number','')} "
                        f"({_expand_country(e.get('country',''))})"
                        for e in receipt['foreign_priority']))
    rows.append(_row('Foreign Priority Claim', ads_for_str, rec_for_str,
                     match_override=(True if no_for_both else None)))

    # Non-publication (only if receipt recorded it)
    if receipt['non_publication'] is not None:
        ads_np = ('Yes' if ads['non_publication']
                  else ('No' if ads['non_publication'] is False else '*(not set)*'))
        rec_np = 'Yes' if receipt['non_publication'] else 'No'
        rows.append(_row('Non-Publication Request', ads_np, rec_np))

    return rows


# ════════════════════════════════════════════════════════════════════════════════
# Markdown rendering
# ════════════════════════════════════════════════════════════════════════════════

def render_markdown(
        rows: List[Row],
        ads_path: str, receipt_path: str,
        app_number: str, filing_date: str,
        confirmation: str,
        total_claims: str, ind_claims: str,
) -> str:
    lines = [
        '## Filing Receipt Review',
        '',
        '| | |',
        '|---|---|',
        f'| **Application No.** | {app_number or "—"} |',
        f'| **Filing Date** | {filing_date or "—"} |',
        f'| **Confirmation No.** | {confirmation or "—"} |',
        f'| **Total / Independent Claims** | {total_claims or "—"} / {ind_claims or "—"} |',
        f'| **ADS** | `{Path(ads_path).name}` |',
        f'| **Filing Receipt** | `{Path(receipt_path).name}` |',
        '',
    ]

    discrepancies = [r for r in rows if not r['match']]
    if discrepancies:
        n = len(discrepancies)
        lines.append(
            f'> **{n} discrepanc{"y" if n == 1 else "ies"} found** — '
            f'see rows marked [X] below.'
        )
    else:
        lines.append('> **All checked fields are consistent with the Filing Receipt.** [OK]')
    lines.append('')

    lines += [
        '| Field | ADS | Filing Receipt | Match |',
        '|---|---|---|:---:|',
    ]

    for row in rows:
        mark = '[OK]' if row['match'] else '[DISCREPANCY]'
        note_md = f'<br>*{row["note"]}*' if row.get('note') else ''
        ads_cell = str(row['ads']).replace('|', '\\|')
        rec_cell = str(row['receipt']).replace('|', '\\|')
        lines.append(f'| {row["label"]}{note_md} | {ads_cell} | {rec_cell} | {mark} |')

    lines.append('')

    if discrepancies:
        lines += ['### Discrepancies', '']
        for i, r in enumerate(discrepancies, 1):
            lines.append(
                f'{i}. **{r["label"]}** — '
                f'ADS: `{r["ads"]}` | Filing Receipt: `{r["receipt"]}`'
            )
        lines += [
            '',
            '> To correct a discrepancy, file a request for a corrected Filing Receipt '
            'with a marked-up ADS (strike-through deletions, underline additions) per MPEP 503.',
        ]
    lines.append('')
    return '\n'.join(lines)


def render_ads_summary(ads: Dict) -> str:
    """Human-readable ADS data block for the vision-fallback path."""
    lines = ['### ADS Data Extracted (for manual comparison)', '']
    lines.append(f'- **Title**: {ads["title"]}')
    lines.append(f'- **Docket**: {ads["docket_number"]}')
    lines.append(f'- **Assignee**: {ads["assignee_org"]}')
    lines.append(f'- **Customer No.**: {ads["customer_number"]}')
    sig = ads.get('signature_date', '')
    if re.match(r'\d{4}-\d{2}-\d{2}', sig):
        y, mo, d = sig.split('-')
        sig = f'{mo}/{d}/{y}'
    lines.append(f'- **ADS Signature Date**: {sig or "—"}')
    lines.append(f'- **Inventors ({len(ads["inventors"])}):**')
    for i, inv in enumerate(ads['inventors'], 1):
        name = _fmt_ads_name(inv)
        loc  = _fmt_ads_location(inv)
        lines.append(f'  {i}. {name} — {loc}')
    lines.append(f'- **Domestic continuity**: '
                 f'{"None" if not ads["domestic_continuity"] else str(ads["domestic_continuity"])}')
    lines.append(f'- **Foreign priority**: '
                 f'{"None" if not ads["foreign_priority"] else str(ads["foreign_priority"])}')
    lines.append(f'- **Non-publication**: '
                 f'{"Yes" if ads["non_publication"] else ("No" if ads["non_publication"] is False else "not set")}')
    return '\n'.join(lines)


# ════════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Compare a USPTO ADS (XFA PDF) against the Filing Receipt.'
    )
    parser.add_argument('file1', help='ADS PDF or Filing Receipt (auto-detected)')
    parser.add_argument('file2', help='Filing Receipt or ADS PDF (auto-detected)')
    parser.add_argument(
        '--image-dir',
        help='Directory to save rendered receipt images when receipt is image-only '
             '(default: system temp dir)',
        default=None,
    )
    args = parser.parse_args()

    p1, p2 = Path(args.file1), Path(args.file2)
    for p in (p1, p2):
        if not p.exists():
            sys.exit(f"ERROR: File not found: {p}")

    # Auto-detect ADS vs. filing receipt
    p1_xfa = _is_xfa(p1)
    p2_xfa = _is_xfa(p2)
    if p1_xfa and not p2_xfa:
        ads_path, receipt_path = p1, p2
    elif p2_xfa and not p1_xfa:
        ads_path, receipt_path = p2, p1
    elif p1_xfa and p2_xfa:
        sys.exit("ERROR: Both files are XFA forms. Supply one ADS and one Filing Receipt.")
    else:
        sys.exit(
            "ERROR: Neither file is an XFA form. "
            "The ADS (PTO/AIA/14) must be an XFA PDF."
        )

    print(f"ADS:            {ads_path.name}", file=sys.stderr)
    print(f"Filing Receipt: {receipt_path.name}", file=sys.stderr)
    print("", file=sys.stderr)

    # Extract ADS
    print("Extracting ADS (XFA) data...", file=sys.stderr)
    ads_data = parse_ads(ads_path)
    print(
        f"  -> {len(ads_data['inventors'])} inventor(s), "
        f"title='{ads_data['title'][:60]}'",
        file=sys.stderr
    )

    # Extract filing receipt text
    print("Extracting filing receipt text...", file=sys.stderr)
    receipt_text = _extract_receipt_text(receipt_path)

    if not receipt_text or len(receipt_text.strip()) < 100:
        # Image-only receipt — render to PNGs for Claude's vision fallback
        print("  -> Filing receipt appears to be image-only (no extractable text).",
              file=sys.stderr)

        img_dir = Path(args.image_dir) if args.image_dir else Path(tempfile.mkdtemp(prefix='receipt_review_'))
        print(f"  -> Rendering receipt pages to: {img_dir}", file=sys.stderr)

        image_paths: List[Path] = []
        if _PDFIUM:
            image_paths = render_receipt_images(receipt_path, img_dir)
            print(f"  -> Rendered {len(image_paths)} page(s).", file=sys.stderr)
        else:
            print("  -> pypdfium2 not available; cannot render images.", file=sys.stderr)
            print("     Install with: pip install pypdfium2 --break-system-packages",
                  file=sys.stderr)

        # Output ADS summary + image paths for Claude's vision path
        print()
        print("## Filing Receipt Review — Image-Only Receipt Detected")
        print()
        print("The Filing Receipt PDF contains no extractable text (scanned/image-only).")
        print("ADS data has been extracted. **Claude: please read the receipt images below**")
        print("using your vision capability, extract the fields listed in the ADS summary,")
        print("and produce the comparison table manually.")
        print()
        print(render_ads_summary(ads_data))
        print()
        if image_paths:
            print("### Receipt Image Files (read these with the Read tool)")
            print()
            for p in image_paths:
                print(f"- `{p}`")
            print()
            print("**Fields to extract from the receipt images:**")
            print("Application number, filing date, confirmation number,")
            print("total claims, independent claims, title, docket number,")
            print("inventor list (name, city, country for each),")
            print("applicant/assignee, customer number,")
            print("domestic benefit claims (None or app numbers),")
            print("foreign priority claims (None or app numbers + countries),")
            print("non-publication request (Yes/No).")
        else:
            print("*(No images rendered — pypdfium2 not available.)*")
            print("Please read the receipt PDF directly using the Read tool.")
        sys.exit(2)

    # Text extraction succeeded — run full comparison
    receipt_data = parse_receipt(receipt_text)
    print(
        f"  -> {len(receipt_data['inventors'])} inventor(s), "
        f"app# {receipt_data['application_number']}",
        file=sys.stderr
    )
    print("", file=sys.stderr)

    rows = compare(ads_data, receipt_data)
    print(render_markdown(
        rows,
        str(ads_path), str(receipt_path),
        receipt_data['application_number'],
        receipt_data['filing_date'],
        receipt_data['confirmation_number'],
        receipt_data['total_claims'],
        receipt_data['independent_claims'],
    ))


if __name__ == '__main__':
    main()
