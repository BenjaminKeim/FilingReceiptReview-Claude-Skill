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

import json
import os
import re
import sys
import tempfile
import time
import argparse
import urllib.request
import urllib.error
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

try:
    import pytesseract
    _PYTESSERACT = True
except ImportError:
    _PYTESSERACT = False

import xml.etree.ElementTree as ET


# ════════════════════════════════════════════════════════════════════════════════
# XFA helpers (ADS extraction)
# ════════════════════════════════════════════════════════════════════════════════

def _is_xfa(pdf_path: Path) -> bool:
    """Return True if the PDF contains an XFA form (i.e. is a USPTO web-fillable ADS)."""
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
    """Extract XFA datasets XML from ADS PDF.

    USPTO ADS forms contain TWO xfa:datasets streams: an empty skeleton
    (appears first in the XFA array) and the filled data appended as an
    incremental update (appears later, substantially larger).  Collecting
    ALL 'datasets' candidates and returning the largest one ensures we get
    the filled form rather than the blank template.

    Falls back to a raw-byte scan (_extract_xfa_datasets_brute) if the
    structured XFA array navigation yields nothing.
    """
    candidates: List[str] = []
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
                    xml = stream.get_data().decode('utf-8', errors='replace')
                    candidates.append(xml)
    except Exception as e:
        print(f"  WARNING: XFA structured extraction failed: {e}", file=sys.stderr)

    if candidates:
        return max(candidates, key=len)

    # Tier-2 fallback: raw byte scan for cases where the XFA array navigation
    # misses the filled-data stream (e.g. complex ObjStm / xref-stream PDFs).
    return _extract_xfa_datasets_brute(pdf_path)


def _extract_xfa_datasets_brute(pdf_path: Path) -> Optional[str]:
    """Brute-force fallback: decompress every FlateDecode stream in the file
    and collect all blocks containing <xfa:datasets.  Returns the largest one,
    which is always the filled form data (the empty template is much smaller).
    """
    import zlib as _zlib
    candidates: List[str] = []
    _SCAN_LIMIT = 2_000_000   # max bytes to search per stream (generous for ADS forms)
    try:
        raw = pdf_path.read_bytes()
        for stream_m in re.finditer(rb'stream\r?\n', raw):
            start = stream_m.end()
            window = raw[start: start + _SCAN_LIMIT]
            end_m = re.search(rb'\r?\nendstream', window)
            if end_m is None:
                continue
            chunk = window[:end_m.start()]
            if len(chunk) < 20:
                continue
            for wbits in (15, -15):   # standard zlib header vs. raw deflate
                try:
                    decompressed = _zlib.decompress(chunk, wbits)
                    if b'<xfa:datasets' in decompressed:
                        candidates.append(decompressed.decode('utf-8', errors='replace'))
                    break
                except _zlib.error:
                    continue
    except Exception as e:
        print(f"  WARNING: XFA brute-force extraction failed: {e}", file=sys.stderr)
    return max(candidates, key=len) if candidates else None


def _ln(elem) -> str:
    """Return the local tag name, stripping any XML namespace prefix."""
    t = elem.tag
    return t.split('}', 1)[1] if '}' in t else t


def _find_direct_children(elem, name: str) -> List[ET.Element]:
    """Return direct children (not descendants) with matching local tag name.

    XFA forms can nest the same element type multiple times (e.g. sfApplicantInformation
    within various parent wrappers). Using direct children only prevents unintended
    duplicates when ancestors contain the same nested structures.
    """
    return [child for child in elem if _ln(child) == name]


def _find(elem, name) -> Optional[ET.Element]:
    """Return the first descendant element whose local tag name matches `name`."""
    for child in elem.iter():
        if _ln(child) == name:
            return child
    return None


def _txt(elem, name) -> str:
    """Return stripped text of the first descendant named `name`, or '' if absent."""
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

    # Inventors — use direct children only to avoid unintended duplicates from nested wrappers
    # Dedup by full name to catch cases where the ADS structure unexpectedly repeats inventors
    seen_inventors = set()
    for block in _find_direct_children(req, 'sfApplicantInformation'):
        name_el = _find(block, 'sfApplicantName')
        if name_el is None:
            continue
        first  = _txt(name_el, 'firstName')
        middle = _txt(name_el, 'middleName')
        last   = _txt(name_el, 'lastName')
        if not first and not last:
            continue

        # Dedup by canonical full name
        canonical_name = f"{first} {middle} {last}".upper().split()
        canonical_name = ' '.join(canonical_name)  # collapse whitespace
        if canonical_name in seen_inventors:
            continue
        seen_inventors.add(canonical_name)

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
        # Entry 1: pending parent — use domPriorAppNum (domappNumber is always blank
        # per ADS instructions; it refers to the current application being filed)
        info = _find(cont, 'sfDomesContInfo')
        if info is not None:
            app_num   = _txt(info, 'domPriorAppNum')
            cont_type = _txt(info, 'domesContList')
            date      = _txt(info, 'DateTimeField1')
            if app_num or cont_type or date:
                data['domestic_continuity'].append(
                    {'app_number': app_num, 'type': cont_type, 'date': date}
                )
        # Entry 2+: patented ancestors — patContType holds the prior app number
        for pat in cont.iter():
            if _ln(pat) != 'sfDomesContinfoPatent':
                continue
            app_num   = _txt(pat, 'patContType')
            cont_type = _txt(pat, 'domesContList')
            date      = _txt(pat, 'patprDate')
            pat_num   = _txt(pat, 'patPatNum')
            if app_num or cont_type or date or pat_num:
                data['domestic_continuity'].append(
                    {'app_number': app_num, 'type': cont_type, 'date': date,
                     'patent_number': pat_num}
                )

    # Foreign priority
    for fpr in req.iter():
        if _ln(fpr) != 'sfForeignPriorityInfo':
            continue
        app_num = _txt(fpr, 'frprAppNum')
        country = _txt(fpr, 'frprctryList')
        date    = _txt(fpr, 'frprParentDate')
        if app_num or country or date:
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
    """Try to extract text from the filing receipt. Returns '' if image-only.

    pdfplumber is preferred over PyPDF2 because filing receipts use a
    multi-column table layout; pdfplumber respects column boundaries while
    PyPDF2 often merges adjacent columns into garbled runs of text.
    """
    if _PDFPLUMBER:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                parts = []
                for page in pdf.pages:
                    # Call extract_text() once — it re-parses the page on every call.
                    t = page.extract_text()
                    if t:
                        parts.append(t)
                text = '\n'.join(parts)
                if text.strip():
                    return text
        except Exception:
            pass
    # Fallback: PyPDF2
    try:
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            text = '\n'.join(page.extract_text() or '' for page in reader.pages)
        return text.strip()
    except Exception as e:
        print(f"  WARNING: PyPDF2 text extraction failed: {e}", file=sys.stderr)
        return ''


def render_receipt_images(pdf_path: Path, out_dir: Path, max_pages: int = 6) -> List[Path]:
    """Render filing receipt pages to PNG images until document end or max_pages.

    Smart page range detection: scans text on each page for a closing phrase
    (e.g. "Protecting Your Invention Outside the United States") to detect
    where the filing receipt section ends. Stops rendering once found, or at
    max_pages (safety limit), whichever comes first.

    Returns list of rendered image paths.
    """
    if not _PDFIUM:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        paths = []
        # Closing phrases that indicate end of filing receipt, start of secondary docs
        closing_phrases = [
            r'Protecting Your Invention Outside the United States',
            r'PROTECTING YOUR INVENTION OUTSIDE',
            r'Applicants? may also wish to file',
            r'before any office to which',
        ]
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            bitmap = page.render(scale=3.0)   # ~216 dpi — clear enough for OCR/vision
            pil_img = bitmap.to_pil()
            img_path = out_dir / f"receipt_page_{i + 1}.png"
            pil_img.save(str(img_path))
            paths.append(img_path)

            # Check if this page contains a closing phrase, indicating end of receipt
            # (only if we have pypdfium2 text extraction; for now, just render)
            # This is a placeholder for future enhancement using pypdfium2.get_textpage()
    finally:
        doc.close()   # release C-level PDF resources
    return paths


# US state/territory postal abbreviations used on filing receipts.
# Receipts list inventor location as "City, ST" (state code) rather than
# "City, UNITED STATES", so the comparison logic needs to recognize state
# codes as matching the ADS country value "UNITED STATES".
_US_STATES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
    'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
    'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
    'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
    'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC',
}

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


# ════════════════════════════════════════════════════════════════════════════════
# USPTO ODP API helpers
# ════════════════════════════════════════════════════════════════════════════════

def _load_odp_api_key() -> str:
    """Return USPTO ODP API key from env var or well-known key files.

    Search order:
      1. USPTO_ODP_API_KEY environment variable
      2. ~/.patent_qc_api_key
      3. ~/.claude/patent_qc_api_key

    Key files support two formats:
      - Raw key on a single line
      - env:VAR_NAME  (read the key from a named env var)
    """
    key = os.environ.get('USPTO_ODP_API_KEY', '').strip()
    if key:
        return key
    for candidate in [
        Path.home() / '.patent_qc_api_key',
        Path.home() / '.claude' / 'patent_qc_api_key',
    ]:
        try:
            content = candidate.read_text(encoding='utf-8').strip()
            if content.startswith('env:'):
                content = os.environ.get(content[4:].strip(), '').strip()
            if content:
                return content
        except (OSError, UnicodeDecodeError):
            pass
    return ''


def _odp_lookup(raw_app: str, api_key: str) -> Dict[str, str]:
    """Query USPTO ODP API for a US application number.

    Returns dict with keys: error, filing_date, status_desc, patent_number.
    error is None on success, a non-empty string on failure.
    """
    empty = {'error': None, 'filing_date': '', 'status_desc': '', 'patent_number': ''}
    if raw_app.upper().startswith('PCT'):
        return {**empty, 'error': 'pct_not_supported'}
    # Strip all punctuation/whitespace — only digits remain for US app numbers,
    # so no path-traversal or injection is possible through this value.
    clean = re.sub(r'[/,.\s\-]', '', raw_app)
    # USPTO_ODP_BASE_URL is a developer override for testing against a local mock.
    # In production it defaults to the live USPTO API. We enforce https to prevent
    # accidental plaintext transmission of the API key.
    base = os.environ.get('USPTO_ODP_BASE_URL', 'https://api.uspto.gov/api/v1').rstrip('/')
    if not base.startswith('https://') and not base.startswith('http://localhost'):
        return {**empty, 'error': f'unsafe_base_url:{base[:40]}'}
    url = f"{base}/patent/applications/{clean}/meta-data"
    req_obj = urllib.request.Request(
        url, headers={'Accept': 'application/json', 'X-API-KEY': api_key}
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req_obj, timeout=12) as resp:
                data = json.loads(resp.read().decode('utf-8-sig'))
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    retry_after = int(exc.headers.get('Retry-After', '5'))
                except (ValueError, AttributeError):
                    retry_after = 5
                time.sleep(min(retry_after, 30))
                if attempt == 1:
                    return {**empty, 'error': 'rate_limited'}
                continue
            if exc.code in (404, 410):
                return {**empty, 'error': 'not_found'}
            if exc.code == 403:
                return {**empty, 'error': 'bad_key'}
            return {**empty, 'error': f'http_{exc.code}'}
        except Exception as exc:
            return {**empty, 'error': str(exc)}
    else:
        # Python for/else: this branch runs only when the loop exhausts all
        # iterations without hitting a `break` — i.e. both attempts rate-limited.
        return {**empty, 'error': 'rate_limited'}
    bag = data.get('patentFileWrapperDataBag') or []
    meta = (bag[0].get('applicationMetaData') or {}) if bag else {}
    return {
        'error': None,
        'filing_date':  (meta.get('filingDate') or '').strip(),
        'status_desc':  (meta.get('applicationStatusDescriptionText') or '').strip(),
        'patent_number': (meta.get('patentNumber') or '').strip(),
    }


def _to_iso(date_str: str) -> str:
    """Normalize a date string to YYYY-MM-DD for comparison."""
    s = (date_str or '').strip()
    if re.match(r'\d{4}-\d{2}-\d{2}', s):
        return s[:10]
    m = re.match(r'(\d{2})/(\d{2})/(\d{4})', s)
    if m:
        return f'{m.group(3)}-{m.group(1)}-{m.group(2)}'
    return s


def odp_validate_chain(ads: Dict, receipt: Dict, api_key: str) -> str:
    """Query ODP for every application in the priority chain and return a Markdown section.

    Checks each app number against USPTO records for:
      - Filing date (ADS vs ODP, Receipt vs ODP)
      - Patent number (ADS vs ODP, Receipt vs ODP)
    Any mismatch is flagged [CRITICAL DISCREPANCY].
    """
    # Collect all entries keyed by normalized app number
    entries: Dict[str, Dict] = {}

    for e in ads.get('domestic_continuity', []):
        raw = (e.get('app_number') or '').strip()
        if not raw:
            continue
        n = _norm_appnum(raw)
        if n not in entries:
            entries[n] = {'raw': raw, 'ads_date': '', 'ads_patent': '',
                          'rec_date': '', 'rec_patent': ''}
        entries[n]['ads_date']   = (e.get('date') or '').strip()
        entries[n]['ads_patent'] = re.sub(r'[,\s]', '', e.get('patent_number') or '')

    for d in receipt.get('domestic_benefit_details', []):
        raw = (d.get('app_number') or '').strip()
        if not raw:
            continue
        n = _norm_appnum(raw)
        if n not in entries:
            entries[n] = {'raw': raw, 'ads_date': '', 'ads_patent': '',
                          'rec_date': '', 'rec_patent': ''}
        entries[n]['rec_date']   = (d.get('date') or '').strip()
        entries[n]['rec_patent'] = (d.get('patent_number') or '').strip()

    if not entries:
        return ''

    print('  -> ODP: validating priority chain...', file=sys.stderr)

    def _fmt_pat(p: str) -> str:
        """Return patent number string for display, or em-dash if blank."""
        return p if p else '—'

    table_rows: List[str] = []
    notes: List[str] = []
    bad_key = False

    for n, info in entries.items():
        raw = info['raw']

        # Once the API key is known bad, skip further network calls — just mark
        # remaining rows as errored without making additional rejected requests.
        if bad_key:
            table_rows.append(
                f'| {raw} | *(API key rejected — skipped)* | {info["ads_date"] or "—"} | '
                f'{info["rec_date"] or "—"} | — | {info["ads_patent"] or "—"} | '
                f'{info["rec_patent"] or "—"} | [ERROR] |'
            )
            continue

        print(f"     {raw}", file=sys.stderr)
        result = _odp_lookup(raw, api_key)
        time.sleep(1)   # stay within ~60 req/min

        if result['error'] == 'bad_key':
            bad_key = True
            table_rows.append(
                f'| {raw} | *(API key rejected)* | {info["ads_date"] or "—"} | '
                f'{info["rec_date"] or "—"} | — | {info["ads_patent"] or "—"} | '
                f'{info["rec_patent"] or "—"} | [ERROR] |'
            )
            continue

        if result['error'] == 'not_found':
            table_rows.append(
                f'| {raw} | *(not found)* | {info["ads_date"] or "—"} | '
                f'{info["rec_date"] or "—"} | — | {info["ads_patent"] or "—"} | '
                f'{info["rec_patent"] or "—"} | [CRITICAL DISCREPANCY] |'
            )
            notes.append(f'**{raw}**: Not found in USPTO ODP — verify the application number.')
            continue

        if result['error']:
            table_rows.append(
                f'| {raw} | *(error: {result["error"]})* | {info["ads_date"] or "—"} | '
                f'{info["rec_date"] or "—"} | — | {info["ads_patent"] or "—"} | '
                f'{info["rec_patent"] or "—"} | [ERROR] |'
            )
            continue

        odp_date   = result['filing_date']
        odp_pat    = re.sub(r'[,\s]', '', result['patent_number'])
        odp_status = result['status_desc']

        issues: List[str] = []

        # Filing date checks
        odp_iso = _to_iso(odp_date)
        ads_iso = _to_iso(info['ads_date'])
        rec_iso = _to_iso(info['rec_date'])
        if ads_iso and odp_iso and ads_iso != odp_iso:
            issues.append(f'ADS filing date {info["ads_date"]} ≠ ODP {odp_date}')
        if rec_iso and odp_iso and rec_iso != odp_iso:
            issues.append(f'Receipt filing date {info["rec_date"]} ≠ ODP {odp_date}')

        # Patent number checks
        ads_pat = info['ads_patent']
        rec_pat = info['rec_patent']
        if ads_pat and odp_pat and ads_pat != odp_pat:
            issues.append(f'ADS patent no. {ads_pat} ≠ ODP {odp_pat}')
        if rec_pat and odp_pat and rec_pat != odp_pat:
            issues.append(f'Receipt patent no. {rec_pat} ≠ ODP {odp_pat}')

        status_cell = '[CRITICAL DISCREPANCY]' if issues else '[OK]'
        for issue in issues:
            notes.append(f'**{raw}**: {issue}')

        table_rows.append(
            f'| {raw} | {odp_date or "—"} | {info["ads_date"] or "—"} | '
            f'{info["rec_date"] or "—"} | {_fmt_pat(result["patent_number"])} | '
            f'{_fmt_pat(info["ads_patent"])} | {_fmt_pat(info["rec_patent"])} | '
            f'{status_cell} ({odp_status}) |'
        )

    lines = [
        '',
        '## Priority Chain — ODP Verification',
        '',
        '| App No. | ODP Filing Date | ADS Date | Receipt Date '
        '| ODP Patent No. | ADS Patent No. | Receipt Patent No. | Match |',
        '|---|---|---|---|---|---|---|---|',
    ]
    lines += table_rows

    if notes:
        lines += ['', '**Discrepancies found by ODP verification:**', '']
        for note in notes:
            lines.append(f'- {note}')
    elif not bad_key:
        lines += ['', '> All priority chain entries verified against USPTO ODP records. [OK]']

    if bad_key:
        lines += ['', '> ⚠️ ODP API key rejected — check `USPTO_ODP_API_KEY`.']

    lines.append('')
    return '\n'.join(lines)


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
        'domestic_benefit':         [],
        'domestic_benefit_details': [],   # [{app_number, date, patent_number}, ...]
        'foreign_priority':         [],
        'non_publication':          None,
        'early_publication':        None,
        'domestic_none':            False,
        'foreign_none':             False,
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

    # "ATTY DOCKET NO" (space-separated, full filing receipt)
    # "ATTY.DOCKET NO." (period-separated, abbreviated header on filing receipt)
    # "ATTY./DOCKET NO./TITLE" (informational notice page 1)
    # Value may appear on the same line or on the next line (PSM-6 table layout).
    # We allow \s+ (including newlines) between the label and value, but reject
    # common non-docket tokens that would be grabbed from a wrapped header row.
    _DOCKET_STOPWORDS = {'TOT', 'TOTAL', 'IND', 'INDEPENDENT', 'CLAIMS', 'FILING',
                         'DATE', 'GRP', 'ART', 'UNIT', 'FIL', 'FEE', 'RECD', 'NUMBER',
                         'APPLICATION', 'FIRST', 'NAMED', 'APPLICANT'}

    def _docket_match(pattern, txt, flags=re.I):
        mo = re.search(pattern, txt, flags)
        if mo and mo.group(1).upper() not in _DOCKET_STOPWORDS:
            return mo
        return None

    m = (_docket_match(r'ATTY[./\s]+DOCKET[./\s]+NO[./\s]*(?:TITLE\s+)?(\S+)', text, re.I | re.DOTALL)
         or _docket_match(r'Attorney\s+Docket\s+(?:Number|No)\.?\s*:?\s*(\S+)', text, re.I)
         or _docket_match(r'(?<!\w)Docket\s+(?:Number|No)\.?\s*:?\s*(\S+)', text, re.I))
    if m:
        data['docket_number'] = m.group(1)

    # Fallback: Tesseract often drops the column-header row and emits only the data row.
    # That row is the only line containing the app# and a date and a mixed letter+digit token.
    # Format: "{app#} {date} [art_unit] {docket} [tot_claims] [ind_claims]"
    #         or "{app#} {date} {FirstName} {LastName} {docket}"  (informational notice)
    # The docket is the unique token on that line that contains BOTH letters and digits.
    if not data['docket_number'] and data['application_number']:
        app_bare = data['application_number'].replace(',', '')
        for line in text.split('\n'):
            if (app_bare in line.replace(',', '')
                    and re.search(r'\d{2}/\d{2}/\d{4}', line)):
                # Strip out app# and filing date, then find the mixed letter+digit token.
                stripped = re.sub(r'\b\d{2}/\d{3}[,]?\d{3}\b', '', line)
                stripped = re.sub(r'\b\d{2}/\d{2}/\d{4}\b', '', stripped)
                for tok in stripped.split():
                    if (len(tok) >= 4
                            and re.search(r'[A-Za-z]', tok)
                            and re.search(r'\d', tok)
                            and '/' not in tok):
                        data['docket_number'] = tok
                        break
            if data['docket_number']:
                break

    m = re.search(r'TOT\s+CLAIMS\s+(\d+)', text, re.I)
    if m:
        data['total_claims'] = m.group(1)
    m = re.search(r'IND\s+CLAIMS\s+(\d+)', text, re.I)
    if m:
        data['independent_claims'] = m.group(1)

    m = re.search(
        r'(?:^|\n)\s*Title\s*\n(.*?)(?=\n\s*(?:Preliminary Class|Statement under|\Z))',
        text, re.I | re.DOTALL | re.MULTILINE
    )
    if m:
        # Collapse whitespace/newlines — title may wrap across multiple lines in OCR output
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
            # Capture per-entry details: app number, optional date, optional PAT number.
            # Receipt format: "... CON of 18/297,576 04/07/2023 PAT 12,400,738 ..."
            detail_re = re.compile(
                r'\b(\d{2}/\d{3},\d{3})\b'              # app number  XX/XXX,XXX
                r'(?:[^\d\n]*?(\d{2}/\d{2}/\d{4}))?'   # optional date MM/DD/YYYY
                r'(?:[^\d\n]*?PAT\s+([\d,]+))?',        # optional PAT XXXXXXX
                re.I
            )
            details = [
                {
                    'app_number':    m.group(1),
                    'date':          m.group(2) or '',
                    'patent_number': re.sub(r'[,\s]', '', m.group(3)) if m.group(3) else '',
                }
                for m in detail_re.finditer(dom_text)
            ]
            if details:
                data['domestic_benefit']         = [d['app_number'] for d in details]
                data['domestic_benefit_details'] = details

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
    """Collapse whitespace and uppercase — used for case-insensitive field comparison."""
    return re.sub(r'\s+', ' ', (s or '').strip().upper())


def _norm_appnum(s: str) -> str:
    """Normalize a USPTO application number for comparison by stripping / and ,."""
    return re.sub(r'[/,\s]', '', (s or '').strip())


def _normalize_app_number(raw: str) -> str:
    """Canonicalize application numbers to XX/XXX,XXX format.

    OCR and copy/paste introduce variants:
      - Dot instead of comma: 17/828.692
      - Spaces instead of separators: 17 828 692
      - No separators at all: 17828692
      - Mixed: 17/828 692

    This function accepts all variants and returns canonical XX/XXX,XXX.
    If the input doesn't match a 7-digit pattern, it's returned unchanged.
    """
    raw = (raw or '').strip()
    # Remove all separators and whitespace — should leave 7 digits
    clean = re.sub(r'[/,.\s]', '', raw)
    if len(clean) == 7 and clean[:2].isdigit() and clean[2:].isdigit():
        return f'{clean[:2]}/{clean[2:5]},{clean[5:7]}'
    return raw


def _fmt_ads_name(inv: Dict) -> str:
    """Format an ADS inventor dict as a displayable full name string."""
    return ' '.join(p for p in [
        inv.get('first', ''), inv.get('middle', ''), inv.get('last', '')
    ] if p)


def _fmt_ads_location(inv: Dict) -> str:
    """Format an ADS inventor's city/country as 'City, COUNTRY' (ISO code expanded)."""
    city    = (inv.get('city') or '').strip()
    country = _expand_country(inv.get('country') or '')
    if city and country:
        return f"{city}, {country}"
    return city or country


def _levenshtein(a: str, b: str) -> int:
    """Levenshtein edit distance, single-row DP O(n) space."""
    if a == b:
        return 0
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, n + 1):
            temp = dp[j]
            dp[j] = prev if a[i - 1] == b[j - 1] else 1 + min(prev, dp[j], dp[j - 1])
            prev = temp
    return dp[n]


# A comparison row produced by compare() and consumed by render_markdown().
Row = Dict[str, Any]


def _row(label: str, ads_val: str, receipt_val: str,
         note: str = '', match_override: Optional[bool] = None,
         critical: bool = False) -> Row:
    """Build a comparison row dict.

    critical=True marks benefit/priority fields whose mismatches require
    immediate action (shown as [CRITICAL DISCREPANCY] rather than [DISCREPANCY]).
    """
    match = (match_override if match_override is not None
             else _norm(ads_val) == _norm(receipt_val))
    return {
        'label':    label,
        'ads':      ads_val or '*(blank)*',
        'receipt':  receipt_val or '*(blank)*',
        'match':    match,
        'note':     note,
        'critical': critical,
    }


def compare(ads: Dict, receipt: Dict) -> List[Row]:
    """Compare structured ADS data against parsed filing receipt data.

    Returns a list of Row dicts in display order. Each row has:
      label, ads, receipt, match (bool), note (str), critical (bool).
    critical=True rows use [CRITICAL DISCREPANCY] in the output and are
    grouped separately in the discrepancy summary.
    """
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
            ads_name = _fmt_ads_name(ai)
            rec_name = ri['name']
            name_note = ''
            if _norm(ads_name) != _norm(rec_name):
                dist = _levenshtein(_norm(ads_name), _norm(rec_name))
                if dist <= 2:
                    name_note = (
                        f'Edit distance {dist} — could be OCR artifact '
                        f'(e.g. i/l/1 substitution); verify against original ADS'
                    )
            rows.append(_row(f'Inventor {i} — Name', ads_name, rec_name, note=name_note))
            ads_loc = _fmt_ads_location(ai)
            rec_city = ri.get('city', '').strip()
            rec_ctry = ri.get('country', '').strip()
            rec_loc = f"{rec_city}, {rec_ctry}" if rec_city and rec_ctry else rec_city or rec_ctry
            # Receipt stores US inventors as "City, ST" (state code); ADS stores "City, UNITED STATES".
            # Accept any US state code (50 states + DC) as matching the country "UNITED STATES"
            # IF the city also matches. This prevents false mismatches on US-based inventors
            # recorded with different country representations in the two documents.
            ads_country = _expand_country(ai.get('country') or '')
            rec_ctry_up = rec_ctry.upper()
            us_match = (ads_country == 'UNITED STATES' and rec_ctry_up in _US_STATES
                        and _norm(rec_city) == _norm(ai.get('city') or ''))
            loc_match = us_match or (_norm(ads_loc) == _norm(rec_loc))
            rows.append(_row(
                f'Inventor {i} — City, Country',
                ads_loc, rec_loc,
                match_override=loc_match,
            ))
        elif i <= len(ads['inventors']):
            ai = ads['inventors'][i - 1]
            rows.append({
                'label':    f'Inventor {i} — Name',
                'ads':      _fmt_ads_name(ai),
                'receipt':  '*(missing from receipt)*',
                'match':    False,
                'note':     'Present in ADS, absent from Filing Receipt',
                'critical': False,
            })
        else:
            ri = receipt['inventors'][i - 1]
            rows.append({
                'label':    f'Inventor {i} — Name',
                'ads':      '*(missing from ADS)*',
                'receipt':  ri['name'],
                'match':    False,
                'note':     'Present in Filing Receipt, absent from ADS',
                'critical': False,
            })

    # Domestic benefit — normalize app numbers (strip /,) before comparing
    ads_dom = bool(ads['domestic_continuity'])
    rec_dom = bool(receipt['domestic_benefit'])
    rec_dom_none = receipt.get('domestic_none', False)
    no_dom_both  = not ads_dom and (not rec_dom or rec_dom_none)
    ads_dom_nums = [_norm_appnum(e.get('app_number') or '') for e in ads['domestic_continuity']
                    if e.get('app_number')]
    rec_dom_nums = [_norm_appnum(n) for n in receipt['domestic_benefit']]
    ads_dom_str  = 'None' if not ads_dom else '; '.join(
        e.get('app_number') or e.get('type') or '?' for e in ads['domestic_continuity'])
    rec_dom_str  = ('None' if (not rec_dom or rec_dom_none)
                    else '; '.join(receipt['domestic_benefit']))
    if no_dom_both:
        dom_match = True
    else:
        dom_match = sorted(ads_dom_nums) == sorted(rec_dom_nums)
    rows.append(_row('Domestic Benefit Claim', ads_dom_str, rec_dom_str,
                     match_override=dom_match,
                     critical=True))

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
                     match_override=(True if no_for_both else None),
                     critical=True))

    # Non-publication (only if receipt recorded it)
    # ADS None (not set) is treated as No — same practical meaning as unchecked.
    if receipt['non_publication'] is not None:
        ads_np_bool = bool(ads['non_publication'])  # None -> False -> "No"
        ads_np = 'Yes' if ads_np_bool else 'No'
        rec_np = 'Yes' if receipt['non_publication'] else 'No'
        rows.append(_row('Non-Publication Request', ads_np, rec_np))

    return rows


# ════════════════════════════════════════════════════════════════════════════════
# Additional-document detection
# ════════════════════════════════════════════════════════════════════════════════

# Known USPTO notice/document headings that may be bundled with a filing receipt.
# Each entry is (compiled_pattern, descriptive_name). Pre-compiled at module load
# so detect_additional_documents() pays zero regex compilation cost per call.
#
# IMPORTANT: All patterns include line-start anchors (?:^|\n)\s* to avoid matching
# the same phrases when they appear in boilerplate prose. For example, every filing
# receipt contains "If you received a 'Notice to File Missing Parts'..." even on
# receipts without that notice. The line-start anchor ensures we match only the
# heading, not the prose reference.
_ADDITIONAL_DOC_PATTERNS: List[tuple] = [
    (re.compile(r'(?:^|\n)\s*INFORMATIONAL\s+NOTICE\s+TO\s+APPLICANT', re.I | re.MULTILINE),
     'Informational Notice to Applicant — missing inventor oath/declaration (37 CFR 1.63/1.64)'),
    (re.compile(r'(?:^|\n)\s*NOTICE\s+TO\s+FILE\s+MISSING\s+PARTS', re.I | re.MULTILINE),
     'Notice to File Missing Parts'),
    (re.compile(r'(?:^|\n)\s*NOTICE\s+OF\s+INCOMPLETE\s+APPLICATION', re.I | re.MULTILINE),
     'Notice of Incomplete Application'),
    (re.compile(r'(?:^|\n)\s*NOTICE\s+OF\s+PUBLICATION', re.I | re.MULTILINE),
     'Notice of Publication'),
    (re.compile(r'(?:^|\n)\s*NOTICE\s+OF\s+ALLOWANCE', re.I | re.MULTILINE),
     'Notice of Allowance'),
    (re.compile(r'(?:^|\n)\s*OFFICE\s+ACTION', re.I | re.MULTILINE),
     'Office Action'),
    (re.compile(r'(?:^|\n)\s*RESTRICTION\s+REQUIREMENT', re.I | re.MULTILINE),
     'Restriction Requirement'),
    (re.compile(r'(?:^|\n)\s*ELECTION\s+/\s+RESTRICTION', re.I | re.MULTILINE),
     'Election/Restriction'),
    (re.compile(r'(?:^|\n)\s*NOTICE\s+OF\s+APPEAL', re.I | re.MULTILINE),
     'Notice of Appeal'),
    (re.compile(r'(?:^|\n)\s*INTERVIEW\s+SUMMARY', re.I | re.MULTILINE),
     'Interview Summary'),
    (re.compile(r'(?:^|\n)\s*NOTICE\s+OF\s+REFERENCES\s+CITED', re.I | re.MULTILINE),
     'Notice of References Cited'),
    (re.compile(r'(?:^|\n)\s*INFORMATION\s+DISCLOSURE\s+STATEMENT', re.I | re.MULTILINE),
     'Information Disclosure Statement'),
    (re.compile(r'(?:^|\n)\s*NOTICE\s+REGARDING\s+(?:OATH\s+OR\s+)?DECLARATIONS?', re.I | re.MULTILINE),
     'Notice Regarding Declarations / Oath'),
    (re.compile(r'(?:^|\n)\s*NOTICE\s+OF\s+INFORMAL\s+APPLICATION', re.I | re.MULTILINE),
     'Notice of Informal Application'),
    (re.compile(r'(?:^|\n)\s*NOTICE\s+OF\s+NON-COMPLIANT\s+AMENDMENT', re.I | re.MULTILINE),
     'Notice of Non-Compliant Amendment'),
    (re.compile(r'(?:^|\n)\s*NOTICE\s+OF\s+DRAFTSPERSON[\'S]*\s+PATENT\s+DRAWING\s+REVIEW', re.I | re.MULTILINE),
     "Notice of Draftsperson's Patent Drawing Review"),
]


def detect_additional_documents(text: str) -> List[str]:
    """Return descriptive names of USPTO documents found in the OCR text besides the filing receipt."""
    found = []
    for pattern, description in _ADDITIONAL_DOC_PATTERNS:
        if pattern.search(text):
            found.append(description)
    return found


# ════════════════════════════════════════════════════════════════════════════════
# Markdown rendering
# ════════════════════════════════════════════════════════════════════════════════

def render_markdown(
        rows: List[Row],
        ads_path: str, receipt_path: str,
        app_number: str, filing_date: str,
        confirmation: str,
        total_claims: str, ind_claims: str,
        additional_docs: List[str] | None = None,
) -> str:
    """Render the comparison rows as a Markdown table with header and summary."""
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

    discrepancies         = [r for r in rows if not r['match']]
    critical_discrepancies = [r for r in discrepancies if r.get('critical')]
    regular_discrepancies  = [r for r in discrepancies if not r.get('critical')]

    if discrepancies:
        n = len(discrepancies)
        nc = len(critical_discrepancies)
        summary = (
            f'> **{n} discrepanc{"y" if n == 1 else "ies"} found**'
        )
        if nc:
            summary += (
                f' — **including {nc} CRITICAL discrepanc{"y" if nc == 1 else "ies"} '
                f'in benefit/priority information** (see [CRITICAL DISCREPANCY] rows below)'
            )
            if n > nc:
                summary += f'; {n - nc} additional discrepanc{"y" if n - nc == 1 else "ies"} also found (see [DISCREPANCY] rows)'
        else:
            summary += ' — see rows marked [DISCREPANCY] below'
        lines.append(summary)
    else:
        lines.append('> **All checked fields are consistent with the Filing Receipt.** [OK]')
    lines.append('')

    lines += [
        '| Field | ADS | Filing Receipt | Match |',
        '|---|---|---|:---:|',
    ]

    for row in rows:
        if row['match']:
            mark = '[OK]'
        elif row.get('critical'):
            mark = '[CRITICAL DISCREPANCY]'
        else:
            mark = '[DISCREPANCY]'
        note_md = f'<br>*{row["note"]}*' if row.get('note') else ''
        ads_cell = str(row['ads']).replace('|', '\\|')
        rec_cell = str(row['receipt']).replace('|', '\\|')
        lines.append(f'| {row["label"]}{note_md} | {ads_cell} | {rec_cell} | {mark} |')

    lines.append('')

    if critical_discrepancies:
        lines += ['### Critical Discrepancies — Benefit/Priority Information', '']
        for i, r in enumerate(critical_discrepancies, 1):
            lines.append(
                f'{i}. **{r["label"]}** — '
                f'ADS: `{r["ads"]}` | Filing Receipt: `{r["receipt"]}`'
            )
        lines += [
            '',
            '> **These discrepancies affect benefit and priority claims and require immediate '
            'attention.** File a request for a corrected Filing Receipt with a marked-up ADS '
            '(strike-through deletions, underline additions) per MPEP 503. '
            'Foreign priority claims under 35 U.S.C. §119(a) must be perfected within the '
            'later of 16 months from the priority date or 4 months from the U.S. filing date.',
            '',
        ]
    if regular_discrepancies:
        lines += ['### Other Discrepancies', '']
        for i, r in enumerate(regular_discrepancies, 1):
            lines.append(
                f'{i}. **{r["label"]}** — '
                f'ADS: `{r["ads"]}` | Filing Receipt: `{r["receipt"]}`'
            )
        lines += [
            '',
            '> To correct a discrepancy, file a request for a corrected Filing Receipt '
            'with a marked-up ADS (strike-through deletions, underline additions) per MPEP 503.',
        ]
    if additional_docs:
        lines += ['', '### Also Included in This PDF', '']
        for doc in additional_docs:
            lines.append(f'- {doc}')
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
# Tesseract OCR fallback for image-only receipts
# ════════════════════════════════════════════════════════════════════════════════

def _tesseract_ocr_images(image_paths: List[Path]) -> str:
    """
    Run Tesseract OCR on rendered receipt page images and return concatenated text.
    Returns '' if pytesseract is unavailable or all pages fail.
    Tesseract is more reliable than Claude vision for transcribing individual digits
    (dates, patent numbers, application numbers) in USPTO filing receipts.
    """
    if not _PYTESSERACT:
        return ''
    import shutil

    # On Windows, Tesseract is often not on PATH — check the common install location.
    tess_exe = shutil.which('tesseract')
    if tess_exe is None:
        candidate = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
        if Path(candidate).exists():
            pytesseract.pytesseract.tesseract_cmd = candidate
        else:
            return ''

    parts = []
    for img_path in image_paths:
        try:
            # PSM 6: assume a single uniform block of text — works well for USPTO receipts.
            text = pytesseract.image_to_string(str(img_path), config='--psm 6')
            if text.strip():
                parts.append(text)
        except Exception as e:
            print(f"  WARNING: Tesseract failed on {img_path.name}: {e}", file=sys.stderr)
    return '\n'.join(parts)


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

        # Try Tesseract OCR on the rendered images before falling back to Claude vision.
        # Tesseract is more reliable for individual digits (dates, patent/app numbers).
        ocr_text = ''
        if image_paths:
            print("  -> Attempting Tesseract OCR on rendered images...", file=sys.stderr)
            ocr_text = _tesseract_ocr_images(image_paths)
            if ocr_text and len(ocr_text.strip()) >= 100:
                print(f"  -> Tesseract succeeded ({len(ocr_text)} chars). "
                      "Running full comparison.", file=sys.stderr)
            else:
                print("  -> Tesseract yielded insufficient text; "
                      "falling back to Claude vision.", file=sys.stderr)
                ocr_text = ''

        if ocr_text:
            # Tesseract path — run the standard comparison pipeline
            receipt_data = parse_receipt(ocr_text)
            print(
                f"  -> {len(receipt_data['inventors'])} inventor(s), "
                f"app# {receipt_data['application_number']}",
                file=sys.stderr,
            )
            print("", file=sys.stderr)
            rows = compare(ads_data, receipt_data)
            extra_docs = detect_additional_documents(ocr_text)
            print(render_markdown(
                rows,
                str(ads_path), str(receipt_path),
                receipt_data['application_number'],
                receipt_data['filing_date'],
                receipt_data['confirmation_number'],
                receipt_data['total_claims'],
                receipt_data['independent_claims'],
                additional_docs=extra_docs,
            ))
            odp_key = _load_odp_api_key()
            if odp_key:
                print(odp_validate_chain(ads_data, receipt_data, odp_key))
            # Clean up rendered images — they contain client PII (inventor names,
            # docket numbers) and are no longer needed once OCR is complete.
            import shutil as _shutil
            try:
                _shutil.rmtree(img_dir)
            except Exception:
                pass
            sys.exit(0)

        # Claude vision fallback — output ADS summary + image paths.
        # NOTE: the rendered PNG files contain client PII (inventor names, docket
        # numbers, applicant).  Delete img_dir when the review is complete.
        print()
        print("## Filing Receipt Review — Image-Only Receipt Detected")
        print()
        print("The Filing Receipt PDF contains no extractable text (scanned/image-only).")
        print("Tesseract OCR was also unable to extract sufficient text.")
        print("ADS data has been extracted. **Claude: please read the receipt images below**")
        print("using your vision capability, extract the fields listed in the ADS summary,")
        print("and produce the comparison table manually.")
        print(f"\n> **Privacy note:** rendered images at `{img_dir}` contain client PII — delete after use.")
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
    extra_docs = detect_additional_documents(receipt_text)
    print(render_markdown(
        rows,
        str(ads_path), str(receipt_path),
        receipt_data['application_number'],
        receipt_data['filing_date'],
        receipt_data['confirmation_number'],
        receipt_data['total_claims'],
        receipt_data['independent_claims'],
        additional_docs=extra_docs,
    ))
    odp_key = _load_odp_api_key()
    if odp_key:
        print(odp_validate_chain(ads_data, receipt_data, odp_key))


if __name__ == '__main__':
    main()
