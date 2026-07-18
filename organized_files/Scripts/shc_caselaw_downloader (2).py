"""
Sindh High Court Caselaw PDF Downloader
=========================================

Downloads judgement/order PDFs from https://caselaw.shc.gov.pk for every
case year from START_YEAR to END_YEAR, saving them into per-year
subfolders. Fully resumable: if the script crashes or is stopped, just
re-run it and it will pick up where it left off (it will not
re-download files it already has).

------------------------------------------------------------------
HOW IT WORKS
------------------------------------------------------------------
1. Opens the search page in a real Chrome browser (Selenium).
2. Types the year into the "Case Year" field.
3. Clicks the Search button (xpath you supplied).
4. Waits for the results table to finish loading.
5. Walks every page of results (if the table is paginated).
6. For every row, reads the PDF link out of column 16
   (td[16]/a[1], as you specified).
7. Downloads each PDF (via requests, using the browser's session
   cookies) into:  <OUTPUT_DIR>\\<year>\\<filename>.pdf
8. Records progress in progress.json after every single file, and
   writes a human-readable log to download_log.txt.

------------------------------------------------------------------
INSTALL (run once, in Command Prompt / PowerShell)
------------------------------------------------------------------
    pip install selenium webdriver-manager requests

You also need Google Chrome installed. webdriver-manager will
automatically download the matching ChromeDriver for you, so you do
NOT need to manually download chromedriver.exe.

------------------------------------------------------------------
RUN
------------------------------------------------------------------
    python shc_caselaw_downloader.py

Optional flags:
    python shc_caselaw_downloader.py --start 2000 --end 2010
    python shc_caselaw_downloader.py --headless
    python shc_caselaw_downloader.py --output "D:\\years count sindh"

------------------------------------------------------------------
IF THE "CASE YEAR" FIELD ISN'T FOUND
------------------------------------------------------------------
The script tries several common ways to find the "Case Year" input
automatically (see CASE_YEAR_LOCATORS below). If it still can't find
it, press F12 on the search page in Chrome, click the little
arrow/cursor icon top-left of DevTools, click on the "Case Year" box,
and look at the highlighted <input ...> tag. Find its "name" or "id"
attribute and add it to CASE_YEAR_LOCATORS near the top of this file.
"""

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from urllib.parse import urljoin

import requests
import urllib3
from urllib.parse import urlparse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    StaleElementReferenceException,
)

# ============================================================
# CONFIG - edit these if needed
# ============================================================

SEARCH_URL = "https://caselaw.shc.gov.pk/caselaw/search-all/search"
BASE_DOMAIN = "https://caselaw.shc.gov.pk"

DEFAULT_START_YEAR = 2009
DEFAULT_END_YEAR = 2026
DEFAULT_OUTPUT_DIR = r"D:\sindh-high-cort-all-records"

# xpath of the Search button, exactly as you gave it
SEARCH_BUTTON_XPATH = "/html/body/div/div[2]/div[2]/form/button"

# xpath of the results table.
#
# NOTE: this used to be the absolute path copied from DevTools
# ("/html/body/div/div[3]/div[2]/div/table"). Absolute paths count
# every sibling <div> from the document root, so they break the
# instant the page's DOM differs by even one wrapper element (a
# banner, a collapsed panel, timing of AJAX rendering, etc). That
# was the actual bug: the "Search Results" header text appeared
# (satisfying half of the wait condition below) while the absolute
# table path matched nothing, so every year with real results was
# being logged as "no results found" and marked complete without
# downloading anything.
#
# Fixed by anchoring on the table's own content instead of its
# position in the DOM: find whichever <table> has a header/cell
# containing "Case No." - stable no matter what wraps it.
RESULTS_TABLE_XPATH = "//table[.//*[contains(normalize-space(text()),'Case No.')]]"

# which <td> (1-indexed) holds the PDF link, and which <a> inside it
PDF_TD_INDEX = 16
PDF_A_INDEX = 1  # a[1]

# Candidate ways to locate the "Case Year" input field.
# The script tries these in order until one is found.
# Each entry is (By.<STRATEGY>, "value")
CASE_YEAR_LOCATORS = [
    # confirmed working locator (from your test run) - tried first so
    # we don't waste 5s x 6 failed attempts on every single year
    (By.XPATH, "//label[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'case year')]"
               "/following::input[1]"),
    (By.NAME, "case_year"),
    (By.NAME, "caseyear"),
    (By.NAME, "year"),
    (By.ID, "case_year"),
    (By.ID, "caseyear"),
    (By.ID, "year"),
    (By.XPATH, "//label[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'case year')]"
               "/following::select[1]"),
]

PAGE_LOAD_TIMEOUT = 40
RESULTS_WAIT_TIMEOUT = 90  # results can take a while to load - extended after
                           # observing the site sometimes needs well over a
                           # minute to populate the AJAX table
BETWEEN_YEARS_DELAY = 1.5  # be polite to the server
DOWNLOAD_RETRIES = 6
DOWNLOAD_RETRY_DELAY = 5  # base delay; grows with each attempt (backoff)
DELAY_BETWEEN_DOWNLOADS = 0.6  # seconds - be gentle after big years like 1998 (120 files)

# The site's own SSL certificate chain is misconfigured (self-signed
# cert in the chain), which makes `requests` refuse the connection.
# We disable verification for this specific, known-public government
# site and silence the resulting warning.
VERIFY_SSL = False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Only URLs on this host are real, publicly downloadable PDFs. Some
# older case rows link out to an internal case-management system on a
# private IP (43.245.130.107) that is unreachable from outside the
# court network - we skip those instantly instead of retrying/timing
# out on them.
PUBLIC_PDF_HOST = "caselaw.shc.gov.pk"

DOWNLOAD_TIMEOUT = (10, 30)  # (connect timeout, read timeout) seconds

# Some case rows serve their document as plain HTML instead of a PDF
# (older cases especially). Rather than treating that as a failed
# download, we render it to a real PDF using headless Chrome's
# built-in print-to-PDF (Page.printToPDF via CDP) - no extra
# dependencies needed since Selenium/Chrome is already required.
CONVERT_HTML_TO_PDF = True

# ============================================================
# END CONFIG
# ============================================================


def setup_logging(output_dir):
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, "download_looog.txt")
    logger = logging.getLogger("shc")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(ch)

    return logger


# ---------------- progress (resume) handling ----------------

def load_progress(output_dir):
    path = os.path.join(output_dir, "progress.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("completed_years", [])
                data.setdefault("downloaded", {})
                data.setdefault("skipped", {})  # for progress.json files from older runs
                data.setdefault("failed", {})   # url -> last error, retried every run
                return data
        except Exception:
            pass
    return {"completed_years": [], "downloaded": {}, "skipped": {}, "failed": {}}


def save_progress(output_dir, progress, logger=None):
    path = os.path.join(output_dir, "progress.json")
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)

    # On Windows, antivirus / OneDrive / search-indexer can transiently
    # lock progress.json for a few hundred ms right after it's written,
    # which makes os.replace() raise "WinError 5: Access is denied".
    # That's not a real failure - retry briefly instead of blowing up
    # the whole year's progress.
    last_err = None
    for attempt in range(6):
        try:
            os.replace(tmp_path, path)  # atomic on both Windows and Linux
            return
        except PermissionError as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    # Give up gracefully: leave the .tmp file (data isn't lost, it's
    # just not been swapped in yet) and log instead of crashing the run.
    if logger:
        logger.warning(
            f"Could not update progress.json after retries ({last_err}). "
            f"Progress for this step is saved in {tmp_path} and will be "
            f"picked up on the next successful save."
        )


# ---------------- selenium helpers ----------------

def make_driver(headless):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
    # "eager" returns control once the DOM is ready, without waiting for
    # every image/font on this heavy page to finish loading - this is
    # the single biggest speedup available, since we only need the form
    # fields, not the full rendered page.
    options.page_load_strategy = "eager"
    options.add_experimental_option(
        "prefs", {"profile.managed_default_content_settings.images": 2}
    )

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
    except Exception:
        # fall back to whatever chromedriver is on PATH
        driver = webdriver.Chrome(options=options)

    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver


def make_pdf_converter_driver():
    """A second, always-headless Chrome instance used only to render
    HTML case documents to PDF (Page.printToPDF via CDP). Kept
    completely separate from the main scraping driver so converting a
    file never disturbs the search-results page or pagination state."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.page_load_strategy = "eager"
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
    except Exception:
        driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver


def html_bytes_to_pdf(pdf_driver, html_bytes, dest_pdf_path, logger):
    """Render raw HTML bytes to a real PDF file at dest_pdf_path using
    headless Chrome's native print-to-PDF. Returns True on success."""
    tmp_html = dest_pdf_path + ".src.html"
    try:
        with open(tmp_html, "wb") as f:
            f.write(html_bytes)

        file_url = "file:///" + os.path.abspath(tmp_html).replace("\\", "/")
        pdf_driver.get(file_url)
        time.sleep(0.5)  # let any late-loading content settle

        result = pdf_driver.execute_cdp_cmd(
            "Page.printToPDF",
            {
                "printBackground": True,
                "preferCSSPageSize": True,
                "marginTop": 0.4,
                "marginBottom": 0.4,
                "marginLeft": 0.4,
                "marginRight": 0.4,
            },
        )
        pdf_bytes = base64.b64decode(result["data"])
        tmp_pdf = dest_pdf_path + ".part"
        with open(tmp_pdf, "wb") as f:
            f.write(pdf_bytes)

        if not _is_valid_pdf(tmp_pdf):
            os.remove(tmp_pdf)
            raise ValueError("Chrome print-to-PDF did not produce a valid PDF")

        os.replace(tmp_pdf, dest_pdf_path)
        return True
    except Exception as e:
        logger.warning(f"HTML->PDF conversion failed for {dest_pdf_path}: {e}")
        return False
    finally:
        try:
            os.remove(tmp_html)
        except OSError:
            pass


def set_case_year(driver, year, logger):
    """Try each candidate locator until the Case Year field is found
    and filled in."""
    last_err = None
    for by, value in CASE_YEAR_LOCATORS:
        try:
            el = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((by, value))
            )
            tag = el.tag_name.lower()
            if tag == "select":
                Select(el).select_by_visible_text(str(year))
            else:
                el.clear()
                el.send_keys(str(year))
            logger.info(f"Case Year field located via ({by}, {value})")
            return True
        except Exception as e:
            last_err = e
            continue
    logger.error(
        "Could not locate the 'Case Year' field automatically. "
        "See the docstring at the top of this script for how to fix "
        f"CASE_YEAR_LOCATORS. Last error: {last_err}"
    )
    return False


def click_search(driver, logger):
    try:
        btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.XPATH, SEARCH_BUTTON_XPATH))
        )
        btn.click()
        return True
    except Exception as e:
        logger.error(f"Could not click the Search button: {e}")
        return False


# Scoped explicitly to inside the results table itself (not the whole
# page) - a genuine "no matching records" message from the table's own
# empty-state, not an unrelated dropdown widget's placeholder text
# (see the old bug note below).
NO_DATA_IN_TABLE_XPATH = (
    RESULTS_TABLE_XPATH + "//*["
    "contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'no matching record')"
    " or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'no data available')"
    " or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'no record found')"
    "]"
)


def wait_for_results(driver, logger, timeout=RESULTS_WAIT_TIMEOUT):
    """Poll for up to `timeout` seconds for the results table to show
    real rows.

    Returns a tuple (found_rows, confirmed_empty):
      - (True,  False) -> rows are present, go scrape them.
      - (False, True)  -> the table itself displayed an explicit
        "no matching records" message - genuinely nothing to
        download, safe to mark the year complete.
      - (False, False) -> we could not confirm either way within the
        timeout (slow site, changed markup, etc). The caller must NOT
        mark this year complete - it should be retried on the next
        run instead of being silently skipped forever.

    NOTE: earlier versions of this function short-circuited the wait
    as soon as generic "Search Results" text appeared anywhere on the
    page. That was itself a bug: that heading is a static part of the
    page layout and is present even before a real search runs, so the
    wait was effectively only running for a few extra seconds of
    polling instead of the full timeout - which silently produced
    false "no results" verdicts on slow-loading pages. We now poll
    continuously for the real table content for the whole timeout
    window instead of trusting any early page-text signal.

    An even older version treated the generic phrase "not found"
    anywhere on the page as a "no results" signal - also wrong, since
    this site's dropdown widgets (Advocate Name, Judges, Topics) use a
    library whose own default placeholder text is "No results found",
    unrelated to search results.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = driver.find_elements(By.XPATH, RESULTS_TABLE_XPATH + "//tbody/tr")
        if rows:
            return True, False
        if driver.find_elements(By.XPATH, NO_DATA_IN_TABLE_XPATH):
            return False, True
        time.sleep(1)

    # Timed out with neither real rows nor an explicit empty-state
    # message from the table. Dump the live page HTML so this can be
    # diagnosed from the file instead of needing to reproduce it live.
    try:
        debug_path = os.path.join(
            os.getcwd(), f"debug_no_results_{int(time.time())}.html"
        )
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        logger.warning(
            f"Timed out after {timeout}s with neither result rows nor an "
            f"explicit 'no records' message; dumped the live page HTML to "
            f"{debug_path} for inspection. This year will NOT be marked "
            f"complete and will be retried on the next run."
        )
    except Exception as e:
        logger.warning(f"Could not write results-debug HTML dump: {e}")

    return False, False


def get_pdf_links_on_page(driver, logger):
    """Return list of (row_label, absolute_pdf_url) for the current
    results page."""
    links = []
    rows = driver.find_elements(By.XPATH, RESULTS_TABLE_XPATH + "//tbody/tr")
    for idx, row in enumerate(rows, start=1):
        try:
            a = row.find_element(By.XPATH, f".//td[{PDF_TD_INDEX}]/a[{PDF_A_INDEX}]")
            href = a.get_attribute("href")
            if not href:
                continue
            abs_url = urljoin(BASE_DOMAIN, href)

            # try to build a friendly label out of the first couple of
            # cells (usually case no / case year / party names etc.)
            cells = row.find_elements(By.XPATH, "./td")
            label_parts = []
            for c in cells[:4]:
                t = c.text.strip()
                if t:
                    label_parts.append(t)
            label = "_".join(label_parts) if label_parts else f"row{idx}"
            links.append((label, abs_url))
        except NoSuchElementException:
            continue
        except StaleElementReferenceException:
            logger.warning("Stale row while reading links, skipping one row.")
            continue
    return links


def has_next_page_and_click(driver, logger):
    """Best-effort generic pagination handling (Bootstrap/DataTables
    style). Returns True if it successfully moved to a new page."""
    candidates = [
        "//ul[contains(@class,'pagination')]//a[contains(@aria-label,'Next') and not(ancestor::li[contains(@class,'disabled')])]",
        "//ul[contains(@class,'pagination')]//a[normalize-space(text())='Next' and not(ancestor::li[contains(@class,'disabled')])]",
        "//a[contains(@class,'page-link') and (contains(text(),'Next') or contains(@aria-label,'Next'))]",
        "//li[contains(@class,'next') and not(contains(@class,'disabled'))]//a",
    ]
    for xp in candidates:
        try:
            next_btns = driver.find_elements(By.XPATH, xp)
            if not next_btns:
                continue
            next_btn = next_btns[0]

            # remember first row text so we can detect the page changed
            try:
                old_first_row = driver.find_element(
                    By.XPATH, RESULTS_TABLE_XPATH + "//tbody/tr[1]"
                ).text
            except NoSuchElementException:
                old_first_row = ""

            driver.execute_script("arguments[0].scrollIntoView(true);", next_btn)
            next_btn.click()

            WebDriverWait(driver, RESULTS_WAIT_TIMEOUT).until(
                lambda d: d.find_element(
                    By.XPATH, RESULTS_TABLE_XPATH + "//tbody/tr[1]"
                ).text
                != old_first_row
            )
            return True
        except Exception:
            continue
    return False  # no more pages / no pagination found


# ---------------- download helpers ----------------

def sanitize_filename(name, max_len=120):
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", "_", name).strip("_")
    if not name:
        name = "file"
    return name[:max_len]


def make_requests_session_from_driver(driver):
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
            "Referer": SEARCH_URL,
        }
    )
    for c in driver.get_cookies():
        session.cookies.set(c["name"], c["value"])
    session.verify = VERIFY_SSL  # site's cert chain is misconfigured
    return session


def is_public_pdf_url(url):
    """True only for links on the real public caselaw host. Some rows
    link out to an internal case-management system on a private IP
    that is unreachable from outside - we must not waste time
    retrying/timing out on those."""
    try:
        return urlparse(url).hostname == PUBLIC_PDF_HOST
    except Exception:
        return False


def _is_valid_pdf(path):
    """Guards against the 'We can't open this file' problem: a
    download can return HTTP 200 with a small HTML error/maintenance
    page instead of the real PDF (this happens a lot on 522s and
    server hiccups). A real PDF always starts with the %PDF- magic
    bytes and is more than a trivial number of bytes."""
    try:
        size = os.path.getsize(path)
        if size < 200:  # a real judgement PDF is never this small
            return False
        with open(path, "rb") as f:
            head = f.read(5)
        return head == b"%PDF-"
    except Exception:
        return False


def download_pdf(session, url, dest_path, logger, pdf_driver=None):
    last_err = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            with session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
                r.raise_for_status()
                content = r.content
                content_type = r.headers.get("Content-Type", "").lower()

            # Case 1: it's already a real PDF - just save it.
            if content[:5] == b"%PDF-" and len(content) >= 200:
                tmp_path = dest_path + ".part"
                with open(tmp_path, "wb") as f:
                    f.write(content)
                os.replace(tmp_path, dest_path)
                return True

            # Case 2: server returned the case document as HTML
            # instead of a PDF (common for older cases) - convert it.
            stripped = content.strip()[:20].lower()
            looks_like_html = (
                "html" in content_type
                or stripped.startswith(b"<!doctype")
                or stripped.startswith(b"<html")
                or stripped.startswith(b"<head")
                or stripped.startswith(b"<body")
            )
            if looks_like_html and CONVERT_HTML_TO_PDF and pdf_driver is not None:
                logger.info(f"{url} returned HTML (not a PDF) - converting to PDF via Chrome...")
                if html_bytes_to_pdf(pdf_driver, content, dest_path, logger):
                    return True
                raise ValueError("HTML response could not be converted to a valid PDF")

            # Case 3: neither a real PDF nor usable/convertible HTML
            # (error page, empty body, truncated stream, etc) - retry.
            raise ValueError(
                f"response was not a PDF and not convertible HTML "
                f"(content-type={content_type!r}, first bytes={content[:20]!r})"
            )

        except Exception as e:
            last_err = e
            logger.warning(f"Download attempt {attempt}/{DOWNLOAD_RETRIES} failed for {url}: {e}")
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(DOWNLOAD_RETRY_DELAY * attempt)
    return False


# ---------------- main ----------------

def process_year(driver, year, year_dir, progress, output_dir, logger, pdf_driver=None):
    """Returns True if every downloadable link for this year was
    successfully saved as a valid PDF (safe to mark the year
    'complete' and never revisit it), False if anything failed and
    should be retried on a future run."""
    os.makedirs(year_dir, exist_ok=True)

    driver.get(SEARCH_URL)
    session = make_requests_session_from_driver(driver)

    if not set_case_year(driver, year, logger):
        raise RuntimeError("Case Year field not found - see log / docstring.")

    if not click_search(driver, logger):
        raise RuntimeError("Could not click Search button.")

    has_results, confirmed_empty = wait_for_results(driver, logger)
    if not has_results:
        if confirmed_empty:
            logger.info(f"Year {year}: confirmed no results.")
            return True  # nothing to download, year really is "complete"
        logger.warning(
            f"Year {year}: could not confirm results within the timeout "
            f"(neither rows nor an explicit empty message appeared). Not "
            f"marking complete - will retry on the next run."
        )
        return False

    page_num = 1
    seen_this_year = 0
    year_had_failures = [False]  # mutable cell so the inner loop can set it
    while True:
        logger.info(f"Year {year}: reading page {page_num} of results...")
        links = get_pdf_links_on_page(driver, logger)
        logger.info(f"Year {year}, page {page_num}: found {len(links)} PDF link(s).")

        for i, (label, url) in enumerate(links, start=1):
            seen_this_year += 1

            if (
                url in progress["downloaded"]
                and os.path.exists(progress["downloaded"][url])
                and _is_valid_pdf(progress["downloaded"][url])
            ):
                continue  # already have a genuine PDF, resume-skip

            if url in progress["downloaded"] and not _is_valid_pdf(
                progress["downloaded"].get(url, "")
            ):
                # We have a record of downloading this, but the file on
                # disk is missing/corrupted (this is the "We can't open
                # this file" case) - drop the stale record so it gets
                # re-downloaded below instead of being skipped forever.
                logger.warning(
                    f"Year {year}: previously-downloaded file for {url} is "
                    f"missing or corrupt, re-downloading."
                )
                del progress["downloaded"][url]

            if url in progress["skipped"]:
                continue  # previously identified as unreachable, don't recheck

            if not is_public_pdf_url(url):
                # points at the internal case-management system
                # (private IP) instead of a real public PDF - not
                # downloadable from outside, skip instantly
                progress["skipped"][url] = "non-public host (internal system link)"
                save_progress(output_dir, progress, logger)
                logger.info(f"Year {year}: skipping non-public link (row {i}, page {page_num}): {url}")
                continue

            fname = sanitize_filename(f"{year}_{page_num:03d}_{i:03d}_{label}")
            if not fname.lower().endswith(".pdf"):
                fname += ".pdf"
            dest_path = os.path.join(year_dir, fname)

            ok = download_pdf(session, url, dest_path, logger, pdf_driver)
            if ok:
                progress["downloaded"][url] = dest_path
                progress["failed"].pop(url, None)
                save_progress(output_dir, progress, logger)
                logger.info(f"Downloaded: {dest_path}")
            else:
                logger.error(f"FAILED to download after retries: {url}")
                progress["failed"][url] = {
                    "year": year,
                    "label": label,
                    "dest_path": dest_path,
                }
                year_had_failures[0] = True
                save_progress(output_dir, progress, logger)

            time.sleep(DELAY_BETWEEN_DOWNLOADS)

        moved = has_next_page_and_click(driver, logger)
        if not moved:
            break
        page_num += 1

    if year_had_failures[0]:
        logger.warning(
            f"Year {year}: done, but {sum(1 for v in progress['failed'].values() if v.get('year') == year)} "
            f"file(s) failed after retries. Year will be revisited on the next run."
        )
    else:
        logger.info(f"Year {year}: done. Total PDF links seen: {seen_this_year}.")

    return not year_had_failures[0]


def main():
    parser = argparse.ArgumentParser(description="Download SHC caselaw PDFs by year.")
    parser.add_argument("--start", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--end", type=int, default=DEFAULT_END_YEAR)
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--no-html-to-pdf",
        action="store_true",
        help="Disable converting HTML case documents to PDF; they will "
        "instead be retried/skipped like a failed download.",
    )
    parser.add_argument(
        "--retry-years",
        type=str,
        default="",
        help="Comma-separated years/ranges to re-check even if already marked "
        "complete, e.g. '1999-2006,2010'. Use this after a bug fix to "
        "re-run years that may have been wrongly recorded as having no "
        "results.",
    )
    args = parser.parse_args()

    global CONVERT_HTML_TO_PDF
    if args.no_html_to_pdf:
        CONVERT_HTML_TO_PDF = False

    output_dir = args.output
    os.makedirs(output_dir, exist_ok=True)
    logger = setup_logging(output_dir)
    progress = load_progress(output_dir)

    logger.info(f"Starting run: years {args.start}-{args.end}, output={output_dir}")

    if args.retry_years.strip():
        retry_set = set()
        for part in args.retry_years.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, hi = part.split("-")
                retry_set.update(range(int(lo), int(hi) + 1))
            else:
                retry_set.add(int(part))
        before = set(progress["completed_years"])
        progress["completed_years"] = [y for y in progress["completed_years"] if y not in retry_set]
        removed = before - set(progress["completed_years"])
        if removed:
            logger.info(f"--retry-years: will re-check {sorted(removed)}")
        save_progress(output_dir, progress, logger)

    logger.info(f"Already completed years: {sorted(progress['completed_years'])}")

    driver = make_driver(args.headless)
    pdf_driver = None
    if CONVERT_HTML_TO_PDF:
        logger.info("Starting a second headless Chrome instance for HTML->PDF conversion...")
        try:
            pdf_driver = make_pdf_converter_driver()
        except Exception as e:
            logger.warning(
                f"Could not start the HTML->PDF converter driver ({e}). "
                f"Any case rows that return HTML instead of a PDF will be "
                f"skipped/retried instead of converted."
            )

    try:
        for year in range(args.start, args.end + 1):
            if year in progress["completed_years"]:
                logger.info(f"Year {year}: already completed, skipping.")
                continue

            year_dir = os.path.join(output_dir, str(year))
            logger.info(f"=== Processing year {year} ===")

            try:
                fully_succeeded = process_year(
                    driver, year, year_dir, progress, output_dir, logger, pdf_driver
                )

                if fully_succeeded:
                    progress["completed_years"].append(year)
                    save_progress(output_dir, progress, logger)
                    logger.info(f"Year {year}: marked complete.")
                else:
                    # Do NOT mark complete - there are files in
                    # progress["failed"] for this year. Leaving the
                    # year off completed_years means it will be
                    # re-scraped and those specific files retried on
                    # the very next run, instead of being lost forever.
                    save_progress(output_dir, progress, logger)
                    logger.warning(f"Year {year}: NOT marked complete (has pending failed downloads).")

            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"Year {year}: FAILED with error, will retry on next run. {e}")
                save_progress(output_dir, progress, logger)

            time.sleep(BETWEEN_YEARS_DELAY)

    except KeyboardInterrupt:
        logger.warning("Interrupted by user. Progress has been saved; re-run the script to resume.")
    finally:
        save_progress(output_dir, progress, logger)
        try:
            driver.quit()
        except Exception:
            pass
        if pdf_driver is not None:
            try:
                pdf_driver.quit()
            except Exception:
                pass

    logger.info(
        f"Run finished. Downloaded: {len(progress['downloaded'])}, "
        f"Skipped (unreachable/internal-only records): {len(progress['skipped'])}, "
        f"Still failing after retries: {len(progress['failed'])}, "
        f"Years completed: {len(progress['completed_years'])}"
    )
    if progress["skipped"]:
        skipped_path = os.path.join(output_dir, "skipped_links.txt")
        with open(skipped_path, "w", encoding="utf-8") as f:
            for url, reason in progress["skipped"].items():
                f.write(f"{url}\t{reason}\n")
        logger.info(f"List of skipped/unreachable records written to {skipped_path}")

    if progress["failed"]:
        failed_path = os.path.join(output_dir, "failed_links.txt")
        with open(failed_path, "w", encoding="utf-8") as f:
            for url, info in progress["failed"].items():
                f.write(f"{info.get('year')}\t{url}\t{info.get('dest_path')}\n")
        logger.info(
            f"{len(progress['failed'])} file(s) still failing after retries - written to "
            f"{failed_path}. Their years were left incomplete, so simply re-running the "
            f"script will retry them."
        )


if __name__ == "__main__":
    main()
