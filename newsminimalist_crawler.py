import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests
import trafilatura
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


LIST_URL = "https://www.newsminimalist.com/?category=all&from=5&to=10&sort=latest"

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state" / "last_run.json"
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "scheduler.log"

DATE_LABEL_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), [A-Z][a-z]{2} \d{1,2}$")
NEWS_TIME_RE = re.compile(
    r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4},\s+"
    r"\d{1,2}(?::\d{2})?\s*(?:AM|PM)",
    re.IGNORECASE,
)
RELATIVE_TIME_RE = re.compile(r"^(?:<\s*)?\d+\s*(?:m|h|d)(?:\s+\d+\s*h)?$", re.IGNORECASE)

BLOCKER_SIGNALS = [
    "just a moment",
    "checking your browser",
    "enable javascript and cookies",
    "access denied",
    "verify you are human",
    "failed to verify your browser",
    "security checkpoint",
    "too many requests",
    "vercel security checkpoint",
    "website owner? click here to fix",
]

PAYWALL_SIGNALS = [
    "already a subscriber",
    "digital access",
    "for subscribers",
    "premium content",
    "register to continue",
    "sign in to continue",
    "subscribe to continue",
    "subscribe to continue reading",
    "subscribe to read",
    "subscription required",
    "to continue reading",
    "unlock this article",
    "you have reached your limit",
]

BOILERPLATE_SIGNALS = [
    "already a subscriber",
    "subscribe now",
    "subscription",
    "sign in to continue",
    "register for free",
    "get unlimited access",
    "accept cookies",
    "advertisement",
]

CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
BROWSER_FALLBACK_WAIT_SECONDS = 8
LIST_RENDER_WAIT_SECONDS = 45
LIST_POST_RENDER_WAIT_SECONDS = 2


def headless_browser_enabled() -> bool:
    return os.environ.get("NEWSMINIMALIST_HEADLESS", "1").lower() not in {
        "0",
        "false",
        "no",
    }


def configure_chrome_options(options) -> None:
    if headless_browser_enabled():
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--log-level=3")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--lang=en-US,en")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )


def chrome_options() -> Options:
    options = Options()
    configure_chrome_options(options)
    return options


def has_newsminimalist_list(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return bool(soup.find("details")) or any(is_date_marker(tag) for tag in soup.find_all("div"))


def has_blocker_html(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    text = normalize_space(soup.get_text(" ", strip=True)).lower()
    page_title = normalize_space(soup.title.get_text(" ", strip=True)).lower() if soup.title else ""
    haystack = f"{page_title} {text}"
    return any(signal in haystack for signal in BLOCKER_SIGNALS)


def wait_for_newsminimalist_list(driver, source_name: str) -> str:
    driver.get(LIST_URL)
    try:
        WebDriverWait(driver, LIST_RENDER_WAIT_SECONDS).until(
            lambda browser: has_newsminimalist_list(browser.page_source)
            or has_blocker_html(browser.page_source)
        )
    except Exception as exc:
        print(
            f"Timed out waiting for News Minimalist list render via {source_name}: "
            f"{type(exc).__name__}; returning current page source.",
            flush=True,
        )
    driver.execute_script(
        "document.querySelectorAll('details > summary').forEach((summary) => summary.click());"
    )
    time.sleep(LIST_POST_RENDER_WAIT_SECONDS)
    return driver.page_source


def fetch_with_stealth_browser() -> tuple[str | None, str]:
    try:
        import undetected_chromedriver as uc
    except ImportError as exc:
        return None, f"unavailable: {type(exc).__name__}"

    options = uc.ChromeOptions()
    configure_chrome_options(options)
    try:
        driver = uc.Chrome(options=options, use_subprocess=True)
    except Exception as exc:
        return None, f"launch failed: {type(exc).__name__}"

    try:
        html = wait_for_newsminimalist_list(driver, "stealth browser")
        if has_blocker_html(html):
            return html, "blocked by browser verification"
        return html, "fetched"
    except Exception as exc:
        return None, f"fetch failed: {type(exc).__name__}"
    finally:
        driver.quit()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def date_label(day: dt.date) -> str:
    return f"{day.strftime('%a')}, {day.strftime('%b')} {day.day}"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    completed_dates = state.pop("completed_utc_dates", [])
    if completed_dates and not state.get("last_completed_utc_date"):
        state["last_completed_utc_date"] = max(completed_dates)
    state.pop("last_log_cleaned_iso_week", None)
    state.pop("last_log_cleaned_at_utc", None)
    return state


def save_state(state: dict) -> None:
    state.pop("completed_utc_dates", None)
    state.pop("last_log_cleaned_iso_week", None)
    state.pop("last_log_cleaned_at_utc", None)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def due_target_date(now: dt.datetime, force: bool = False) -> dt.date | None:
    today = now.date()
    if force:
        return today

    state = load_state()
    if state.get("last_completed_utc_date") == today.isoformat():
        return None
    return today


def fetch_newsminimalist_html() -> str:
    html, fetch_status = request_text_with_status(LIST_URL)
    if html and has_newsminimalist_list(html):
        print("Fetched News Minimalist list from server-rendered HTML.", flush=True)
        return html
    if html:
        print(
            "Server-rendered HTML did not contain news items "
            f"({fetch_status}); falling back to browser render.",
            flush=True,
        )
    else:
        print(
            f"Server-rendered HTML fetch failed ({fetch_status}); falling back to browser render.",
            flush=True,
        )

    html, stealth_status = fetch_with_stealth_browser()
    if html and has_newsminimalist_list(html):
        print("Fetched News Minimalist list with stealth browser.", flush=True)
        return html
    print(f"Stealth browser did not return news items ({stealth_status}); falling back to Selenium.", flush=True)

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options())
    try:
        return wait_for_newsminimalist_list(driver, "Selenium")
    finally:
        driver.quit()


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_date_marker(tag) -> bool:
    if tag.name != "div":
        return False
    text = normalize_space(tag.get_text(" ", strip=True))
    return DATE_LABEL_RE.fullmatch(text) is not None


def clean_title(raw: str) -> str:
    raw = normalize_space(raw)
    raw = re.sub(r"\b\d+(?:\.\d+)?\s*$", "", raw).strip()
    raw = re.sub(r"[\s(-]*$", "", raw).strip()
    return raw


def unique_urls(urls: list[str]) -> list[str]:
    seen = set()
    unique = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique


def parse_news_item(details) -> dict | None:
    summary = details.find("summary")
    if summary is None:
        return None

    time_tag = summary.select_one(".last-article-date")
    time_text = normalize_space(time_tag.get("title", "")) if time_tag else ""
    if not NEWS_TIME_RE.fullmatch(time_text):
        text = normalize_space(summary.get_text(" ", strip=True))
        time_match = NEWS_TIME_RE.search(text)
        time_text = normalize_space(time_match.group(0)) if time_match else ""
    if not time_text and time_tag:
        visible_time = normalize_space(time_tag.get_text(" ", strip=True))
        if RELATIVE_TIME_RE.fullmatch(visible_time):
            time_text = visible_time

    title_tag = summary.select_one("div.mr-auto > span")
    title = clean_title(title_tag.get_text(" ", strip=True)) if title_tag else ""
    if not title:
        text = normalize_space(summary.get_text(" ", strip=True))
        title_candidates = [
            clean_title(span.get_text(" ", strip=True))
            for span in summary.find_all("span")
            if len(normalize_space(span.get_text(" ", strip=True))) > 20
        ]
        title = max(title_candidates, key=len, default=clean_title(text))

    links = [a.get("href") for a in details.find_all("a", href=True)]
    source_urls = unique_urls([href for href in links if href.startswith("http")])
    if not title and not source_urls:
        return None

    return {
        "title": title,
        "time": time_text or "N/A",
        "source_url": source_urls[0] if source_urls else None,
        "source_urls": source_urls,
    }


def parse_newsminimalist(html: str, target_date: dt.date) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    sections = [soup, *soup.select("main section")]
    section = max(sections, key=lambda tag: len(tag.find_all("details")))
    target_label = date_label(target_date)
    stop_label = date_label(target_date - dt.timedelta(days=1))
    include_undated_lead = target_date == utc_now().date()

    current_label = None
    items: list[dict] = []

    for node in section.find_all(["div", "details"]):
        if is_date_marker(node):
            label = normalize_space(node.get_text(" ", strip=True))
            if label == stop_label and current_label == target_label:
                break
            current_label = label
            continue

        if node.name != "details":
            continue

        if node.find_parent("details") is not None:
            continue

        in_target_section = current_label == target_label or (
            current_label is None and include_undated_lead
        )
        if not in_target_section:
            continue

        item = parse_news_item(node)
        if item:
            items.append(item)

    return items


def write_parse_debug_html(html: str, target_date: dt.date) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    debug_file = LOG_DIR / f"newsminimalist-debug-{target_date.isoformat()}.html"
    debug_file.write_text(html, encoding="utf-8")
    return debug_file


def print_parse_diagnostics(html: str) -> None:
    soup = BeautifulSoup(html, "html.parser")
    text = normalize_space(soup.get_text(" ", strip=True))
    labels = [
        normalize_space(tag.get_text(" ", strip=True))
        for tag in soup.find_all("div")
        if is_date_marker(tag)
    ]
    lower_text = text.lower()
    blockers = [
        signal
        for signal in [*BLOCKER_SIGNALS, "too many requests"]
        if signal in lower_text
    ]
    print(
        "Parse diagnostics: "
        f"html_bytes={len(html.encode('utf-8'))}, "
        f"details={len(soup.find_all('details'))}, "
        f"date_markers={labels[:8]}, "
        f"blockers={blockers[:5]}",
        flush=True,
    )


def decode_response_text(response: requests.Response) -> str:
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        response.encoding = response.apparent_encoding
        return response.text


def request_text_with_status(url: str) -> tuple[str | None, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        response = requests.get(url, headers=headers, timeout=25)
    except requests.RequestException as exc:
        return None, type(exc).__name__
    if response.status_code >= 400:
        return None, f"HTTP {response.status_code}"
    return decode_response_text(response), "fetched"


def request_text(url: str) -> str | None:
    text, _ = request_text_with_status(url)
    return text


def render_article_text(url: str) -> tuple[str, str]:
    try:
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options())
    except Exception as exc:
        return "", f"browser unavailable: {type(exc).__name__}"

    try:
        driver.get(url)
        time.sleep(BROWSER_FALLBACK_WAIT_SECONDS)
        html = driver.page_source
    except Exception as exc:
        return "", f"browser fetch failed: {type(exc).__name__}"
    finally:
        driver.quit()

    article_text = extract_article_text(html)
    if article_text:
        return article_text, "browser rendered"
    return "", "browser rendered no extractable article text"


def has_enough_article_text(text: str) -> bool:
    if len(text.split()) >= 80:
        return True
    cjk_chars = len(CJK_RE.findall(text))
    return cjk_chars >= 250 and len(text) >= 500


def extract_article_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    page_title = normalize_space(soup.title.get_text(" ", strip=True)) if soup.title else ""
    page_text = normalize_space(soup.get_text(" ", strip=True))[:2000].lower()
    if is_blocked_or_paywalled(page_title, page_text):
        return ""

    extracted = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        favor_recall=True,
        output_format="txt",
    )
    if extracted:
        text = clean_article_text(extracted)
        if has_enough_article_text(text):
            return text

    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside"]):
        tag.decompose()

    selectors = ["article p", "main p", "[class*=article] p", "p"]
    for selector in selectors:
        paragraphs = [
            normalize_space(p.get_text(" ", strip=True))
            for p in soup.select(selector)
            if len(normalize_space(p.get_text(" ", strip=True))) > 40
        ]
        text = clean_article_text("\n".join(paragraphs))
        if has_enough_article_text(text):
            return text
    return ""


def is_blocked_or_paywalled(page_title: str, page_text: str) -> bool:
    haystack = f"{page_title.lower()} {page_text.lower()}"
    return any(signal in haystack for signal in BLOCKER_SIGNALS + PAYWALL_SIGNALS)


def clean_article_lines(text: str) -> list[str]:
    lines = []
    seen_lines = set()
    for line in text.splitlines():
        line = normalize_space(line)
        if not line:
            continue
        lower = line.lower()
        if any(signal in lower for signal in BOILERPLATE_SIGNALS) and len(line.split()) < 30:
            continue
        if lower in seen_lines:
            continue
        seen_lines.add(lower)
        lines.append(line)
    return lines


def clean_article_text(text: str) -> str:
    lines = clean_article_lines(text)
    return normalize_space(" ".join(lines))


def translate_to_english(text: str) -> str:
    if not text:
        return ""
    query = text[:1800]
    url = (
        "https://translate.googleapis.com/translate_a/single?client=gtx"
        f"&sl=auto&tl=en&dt=t&q={urllib.parse.quote(query)}"
    )
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        data = response.json()
        translated = "".join(part[0] for part in data[0] if part and part[0])
        return normalize_space(translated)
    except Exception:
        return text


def first_words(text: str, limit: int = 100) -> str:
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]).rstrip(" ,;:") + "."


def first_paragraph_extract(text: str, limit: int = 100) -> str:
    lines = clean_article_lines(text)
    if not lines:
        return ""
    paragraph = next((line for line in lines if len(line.split()) >= 30), lines[0])
    return first_words(paragraph, limit)


def summarize_extracted_text(text: str) -> str:
    return first_paragraph_extract(text, 100)


def summarize_article(source_urls: list[str] | str | None) -> tuple[str, str, list[dict[str, str]]]:
    if isinstance(source_urls, str):
        tried_urls = [source_urls]
    else:
        tried_urls = source_urls or []

    attempts = []
    for url in tried_urls:
        html, fetch_status = request_text_with_status(url)
        if not html:
            attempts.append({"url": url, "status": f"fetch failed: {fetch_status}"})
            article_text, browser_status = render_article_text(url)
            if not article_text:
                attempts.append({"url": url, "status": browser_status})
                continue
            summary = summarize_extracted_text(article_text)
            english_summary = translate_to_english(summary)
            attempts.append({"url": url, "status": "summarized from browser render"})
            return english_summary, url, attempts
        article_text = extract_article_text(html)
        if not article_text:
            attempts.append({"url": url, "status": "no extractable article text"})
            article_text, browser_status = render_article_text(url)
            if not article_text:
                attempts.append({"url": url, "status": browser_status})
                continue
            summary = summarize_extracted_text(article_text)
            english_summary = translate_to_english(summary)
            attempts.append({"url": url, "status": "summarized from browser render"})
            return english_summary, url, attempts
        summary = summarize_extracted_text(article_text)
        english_summary = translate_to_english(summary)
        attempts.append({"url": url, "status": "summarized"})
        return english_summary, url, attempts
    return "Summary unavailable: article content could not be fetched from any source URL for this item.", "", attempts


def enrich_items(items: list[dict]) -> list[dict]:
    enriched = []
    for index, item in enumerate(items, start=1):
        print(f"Summarizing {index}/{len(items)}: {item['title']}", flush=True)
        summary, summarized_url, summary_attempts = summarize_article(
            item.get("source_urls") or item.get("source_url")
        )
        enriched.append(
            {
                **item,
                "summary": summary,
                "summarized_url": summarized_url,
                "summary_attempts": summary_attempts,
            }
        )
        time.sleep(1)
    return enriched


def write_markdown(items: list[dict], target_date: dt.date) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = OUTPUT_DIR / f"newsminimalist-{target_date.isoformat()}.md"

    lines = [f"# News Minimalist - {target_date.isoformat()} UTC", ""]
    if not items:
        lines.append("No news items found for the target date.")
    for item in items:
        attempt_lines = []
        if not item.get("summarized_url"):
            attempts = item.get("summary_attempts") or []
            if attempts:
                attempt_lines = ["", "Attempted summary sources:"]
                attempt_lines.extend(
                    f"- {attempt['url']} ({attempt['status']})" for attempt in attempts
                )
            else:
                attempt_lines = ["", "Attempted summary sources: none captured"]

        lines.extend(
            [
                f"## {item['title']}",
                "",
                f"- Time: {item['time']}",
                f"- Source: {item.get('source_url') or 'N/A'}",
                f"- Summarized from: {item.get('summarized_url') or 'N/A'}",
                "",
                f"Summary: {item['summary']}",
                *attempt_lines,
                "",
            ]
        )

    output_file.write_text("\n".join(lines), encoding="utf-8")
    return output_file


def mark_completed(target_date: dt.date, output_file: Path) -> None:
    state = load_state()
    state["last_completed_utc_date"] = target_date.isoformat()
    state["last_output"] = str(output_file)
    state["updated_at_utc"] = utc_now().isoformat()
    save_state(state)


def run_once(force: bool = False, target_date: dt.date | None = None) -> int:
    now = utc_now()
    run_date = target_date or due_target_date(now, force=force)
    if run_date is None:
        print("No UTC date is due. Nothing to do.")
        return 0

    print(f"Running News Minimalist crawl for UTC date {run_date.isoformat()}.")
    html = fetch_newsminimalist_html()
    items = parse_newsminimalist(html, run_date)
    print(f"Found {len(items)} item(s) for {date_label(run_date)}.")
    if not items:
        print_parse_diagnostics(html)
        debug_file = write_parse_debug_html(html, run_date)
        print(f"Wrote parse debug HTML to {debug_file}", flush=True)
        raise RuntimeError("No items were parsed for the target date; leaving the run uncompleted for retry.")
    enriched = enrich_items(items)
    output_file = write_markdown(enriched, run_date)
    mark_completed(run_date, output_file)
    print(f"Wrote {output_file}")
    return 0


def run_daemon() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = LOG_FILE.open("a", encoding="utf-8", buffering=1)
    sys.stdout = log_handle
    sys.stderr = log_handle
    print("News Minimalist scheduler started. Checking every 10 minutes.", flush=True)
    while True:
        try:
            run_once(force=False)
        except Exception as exc:
            print(f"Run failed: {exc}", file=sys.stderr, flush=True)
        time.sleep(600)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and summarize News Minimalist items.")
    parser.add_argument("--once", action="store_true", help="Run one due check and exit.")
    parser.add_argument("--force", action="store_true", help="Run for the current UTC date even if it is already completed.")
    parser.add_argument("--date", help="Run for a specific UTC date in YYYY-MM-DD format.")
    parser.add_argument("--daemon", action="store_true", help="Keep checking for due runs.")
    args = parser.parse_args()

    if STATE_FILE.exists():
        save_state(load_state())

    if args.daemon:
        return run_daemon()
    target_date = dt.date.fromisoformat(args.date) if args.date else None
    return run_once(force=args.force, target_date=target_date)


if __name__ == "__main__":
    raise SystemExit(main())
