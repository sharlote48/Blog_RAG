#!/usr/bin/env python3
"""Scrape claude.com/blog — save every post as markdown and track new posts over time.

Usage:
  uv run main.py                   # download all posts (skip already saved)
  uv run main.py --check-updates   # print new posts since last run, no download
  uv run main.py --refresh         # re-download every post
  uv run main.py --list            # list posts tracked in the manifest
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import markdownify
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_URL = "https://claude.com/blog/"
MANIFEST_FILE = Path("manifest.json")
OUTPUT_DIR = Path("blogs")


# ── manifest helpers ──────────────────────────────────────────────────────────

def load_manifest() -> dict:
    if MANIFEST_FILE.exists():
        return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    return {"last_checked": None, "posts": {}}


def save_manifest(manifest: dict):
    manifest["last_checked"] = datetime.now().isoformat()
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


# ── URL / filename helpers ────────────────────────────────────────────────────

def slug_from_url(url: str) -> str:
    return urlparse(url).path.strip("/").split("/")[-1]


def safe_filename(slug: str) -> str:
    return re.sub(r"[^\w\-]", "-", slug)


# ── scraping ──────────────────────────────────────────────────────────────────

def fetch_blog_index(page) -> list[dict]:
    """Return all post stubs from the blog listing, clicking 'View more' until done."""
    page.goto(BASE_URL, wait_until="networkidle")
    seen: set[str] = set()
    posts: list[dict] = []

    while True:
        soup = BeautifulSoup(page.content(), "html.parser")

        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            # Only accept paths like /blog/<slug> (not /blog/ itself)
            if not re.match(r"^/blog/[^/]+/?$", href):
                continue
            full_url = urljoin("https://claude.com", href)
            slug = slug_from_url(full_url)
            if not slug or slug in seen:
                continue
            seen.add(slug)

            title_el = a.find(["h2", "h3", "h4", "p"])
            title = title_el.get_text(strip=True) if title_el else ""

            date_el = a.find("time") or a.find(attrs={"datetime": True})
            date = (
                date_el.get("datetime") or date_el.get_text(strip=True)
                if date_el else ""
            )

            posts.append({"url": full_url, "slug": slug, "title": title, "date": date})

        # Scroll to bottom to trigger any lazy-load, then look for "View more"
        prev_count = len(seen)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)

        btn = page.query_selector(
            "button:has-text('View more'), a:has-text('View more'), "
            "button:has-text('Load more'), a:has-text('Load more')"
        )
        if not btn:
            break
        try:
            btn.scroll_into_view_if_needed(timeout=5000)
            btn.click(timeout=10000)
            page.wait_for_load_state("networkidle")
        except Exception:
            # If click fails, check if scroll already loaded new posts
            soup2 = BeautifulSoup(page.content(), "html.parser")
            new_slugs = {
                slug_from_url(urljoin("https://claude.com", a["href"]))
                for a in soup2.find_all("a", href=True)
                if re.match(r"^/blog/[^/]+/?$", a["href"])
            }
            if new_slugs - seen:
                continue  # new posts appeared via scroll, keep going
            break

    return posts


def fetch_post_content(page, url: str) -> dict:
    """Fetch a single post and return title, date, and body as markdown."""
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception:
        # Fall back to domcontentloaded if networkidle times out
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
    soup = BeautifulSoup(page.content(), "html.parser")

    # ── title ────────────────────────────────────────────────────────────────
    h1 = soup.find("h1")
    title = h1.get_text(separator=" ", strip=True) if h1 else ""

    # ── date ─────────────────────────────────────────────────────────────────
    date = ""
    date_el = soup.find("time") or soup.find(attrs={"datetime": True})
    if date_el:
        date = date_el.get("datetime") or date_el.get_text(strip=True)
    if not date:
        for el in soup.find_all(["span", "p", "div"]):
            text = el.get_text(strip=True)
            if re.search(
                r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}\b",
                text,
            ):
                date = text
                break

    # ── body ─────────────────────────────────────────────────────────────────
    # Remove all style/script tags globally before any further processing
    for tag in soup.find_all(["style", "script"]):
        tag.decompose()

    body = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.body
    )

    # Remove chrome that isn't part of the article
    for junk in body.find_all(["nav", "footer", "aside", "header"], recursive=True):
        junk.decompose()
    for junk in body.find_all(
        class_=re.compile(r"\b(related|share|comment|sidebar|nav|cookie|banner)\b", re.I)
    ):
        junk.decompose()

    # Find the deepest block that contains the real prose (h2 + p pattern)
    # The blog posts wrap the article body in a div with lots of p/h2 children
    prose_candidates = body.find_all(
        lambda tag: tag.name == "div"
        and len(tag.find_all(["p", "h2", "h3", "ul", "ol"], recursive=False)) >= 3
    )
    if prose_candidates:
        # Pick the deepest / largest candidate
        body = max(prose_candidates, key=lambda t: len(t.get_text()))

    md_body = markdownify.markdownify(
        str(body),
        heading_style="ATX",
        strip=["img"],
        newline_style="backslash",
    ).strip()

    # Clean up repeated blank lines left by stripped elements
    md_body = re.sub(r"\n{3,}", "\n\n", md_body)

    front_matter = (
        f"---\n"
        f"title: {json.dumps(title)}\n"
        f"url: {url}\n"
        f"date: {date}\n"
        f"scraped_at: {datetime.now().isoformat()}\n"
        f"---\n\n"
    )
    return {"title": title, "date": date, "markdown": front_matter + md_body}


def save_post(post_data: dict, slug: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    filepath = OUTPUT_DIR / f"{safe_filename(slug)}.md"
    filepath.write_text(post_data["markdown"], encoding="utf-8")
    return filepath


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_list(manifest: dict):
    posts = manifest.get("posts", {})
    if not posts:
        print("No posts tracked yet. Run without flags to download.")
        return
    print(f"{'Slug':<55} {'Date':<15} Saved")
    print("-" * 80)
    for slug, info in sorted(posts.items(), key=lambda x: x[1].get("date", ""), reverse=True):
        saved = "yes" if info.get("saved") else "no"
        print(f"{slug:<55} {info.get('date', ''):<15} {saved}")
    last = manifest.get("last_checked")
    print(f"\n{len(posts)} total  |  last checked: {last or 'never'}")


def cmd_check_updates(manifest: dict, posts: list[dict]):
    known = set(manifest.get("posts", {}).keys())
    new_posts = [p for p in posts if p["slug"] not in known]

    # Record newly seen slugs without downloading
    for p in posts:
        if p["slug"] not in manifest["posts"]:
            manifest["posts"][p["slug"]] = {
                "url": p["url"],
                "title": p["title"],
                "date": p["date"],
                "saved": False,
            }
    save_manifest(manifest)

    if new_posts:
        print(f"{len(new_posts)} new post(s) found:\n")
        for p in new_posts:
            print(f"  {(p['date'] or '?'):>15}  {p['title'] or p['slug']}")
            print(f"  {'':>15}  {p['url']}\n")
    else:
        print("No new posts since last check.")


def cmd_download(manifest: dict, posts: list[dict], page, force: bool):
    already_saved = {
        slug for slug, info in manifest.get("posts", {}).items() if info.get("saved")
    }
    to_fetch = posts if force else [p for p in posts if p["slug"] not in already_saved]

    if not to_fetch:
        print(
            f"All {len(already_saved)} posts already saved. "
            f"Use --refresh to re-download."
        )
        save_manifest(manifest)
        return

    print(f"Downloading {len(to_fetch)} post(s)...\n")
    errors = 0
    for i, post in enumerate(to_fetch, 1):
        label = post["title"] or post["slug"]
        print(f"[{i}/{len(to_fetch)}] {label}")
        try:
            content = fetch_post_content(page, post["url"])
            filepath = save_post(content, post["slug"])
            manifest["posts"][post["slug"]] = {
                "url": post["url"],
                "title": content["title"],
                "date": content["date"],
                "saved": True,
                "file": str(filepath),
            }
            print(f"         -> {filepath}")
        except Exception as exc:
            errors += 1
            print(f"         ERROR: {exc}", file=sys.stderr)
            manifest["posts"][post["slug"]] = {
                "url": post["url"],
                "title": post["title"],
                "date": post["date"],
                "saved": False,
                "error": str(exc),
            }

    save_manifest(manifest)
    saved = sum(1 for v in manifest["posts"].values() if v.get("saved"))
    print(f"\nDone. {saved} post(s) saved to '{OUTPUT_DIR}/'. ({errors} error(s))")


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scrape claude.com/blog and save posts as markdown files."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check-updates",
        action="store_true",
        help="Show new posts since last run without downloading.",
    )
    group.add_argument(
        "--refresh",
        action="store_true",
        help="Re-download all posts, even ones already saved.",
    )
    group.add_argument(
        "--list",
        action="store_true",
        help="List all tracked posts from the local manifest.",
    )
    args = parser.parse_args()

    manifest = load_manifest()

    if args.list:
        cmd_list(manifest)
        return

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        print("Fetching blog index...")
        posts = fetch_blog_index(page)
        print(f"Found {len(posts)} posts on the site.\n")

        if args.check_updates:
            cmd_check_updates(manifest, posts)
        else:
            cmd_download(manifest, posts, page, force=args.refresh)

        browser.close()


if __name__ == "__main__":
    main()
