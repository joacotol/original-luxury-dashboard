import asyncio
import json
import os
import random
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://originalluxury.ca"
ALL_COLLECTION_URL = f"{BASE_URL}/collections/all"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# -------------------- Helpers --------------------

def clean_invisible(s: Any) -> str:
    if s is None:
        return ""
    s = str(s).replace("\ufeff", "")
    # Remove control/format chars
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cf"))
    s = re.sub(r"\s+", " ", s).strip()
    return s


def uniq_sorted(values: List[str]) -> List[str]:
    cleaned = [clean_invisible(v) for v in values if clean_invisible(v)]
    seen = {}
    for v in cleaned:
        key = v.lower()
        if key not in seen:
            seen[key] = v
    return sorted(seen.values(), key=lambda x: x.lower())


def normalize_url(u: str) -> str:
    u = clean_invisible(u)
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    return u


def extract_text_lines(soup: BeautifulSoup) -> List[str]:
    text = soup.get_text("\n")
    lines = [clean_invisible(ln) for ln in text.split("\n")]
    return [ln for ln in lines if ln]


def find_after_label(lines: List[str], label: str) -> Optional[str]:
    """
    Finds a line like 'MSKU:' and returns inline content 'MSKU: XYZ'
    or returns the next line if in next-line format.
    """
    label_lower = label.lower()
    for i, ln in enumerate(lines):
        low = ln.lower()
        if low.startswith(label_lower):
            parts = ln.split(":", 1)
            if len(parts) == 2 and clean_invisible(parts[1]):
                return clean_invisible(parts[1])
            if i + 1 < len(lines):
                return clean_invisible(lines[i + 1])
    return None


def handle_from_product_url(url: str) -> Optional[str]:
    try:
        path = urllib.parse.urlparse(url).path
        m = re.search(r"/products/([^/?#]+)", path)
        return m.group(1) if m else None
    except Exception:
        return None

def shopify_money_to_float(x: Any) -> Optional[float]:
    """
    Shopify product .js often returns money in cents (e.g., 850000 == 8500.00).
    Sometimes it returns decimal strings like "2100.00".
    Normalize both safely.
    """
    if x is None:
        return None

    # If already numeric
    if isinstance(x, (int, float)):
        # Heuristic: very large whole numbers are almost certainly cents
        if isinstance(x, int) and x >= 10000:
            return round(x / 100.0, 2)
        # if float like 2100.00 already dollars
        return round(float(x), 2)

    s = str(x).strip().replace(",", "")
    if not s:
        return None

    # Digits only => cents most of the time in product.js
    if re.fullmatch(r"\d+", s):
        n = int(s)
        # Heuristic: >= 10000 cents => >= $100.00
        if n >= 10000:
            return round(n / 100.0, 2)
        # Could be a small cents number or actual dollars; prefer dollars for tiny values
        return round(float(n), 2)

    # Decimal string like "2100.00"
    try:
        return round(float(s), 2)
    except Exception:
        return None


# -------------------- Robust fetch (429-aware) --------------------

async def _fetch(client: httpx.AsyncClient, url: str, retries: int = 8) -> httpx.Response:
    backoff = 1.5
    for attempt in range(retries + 1):
        try:
            r = await client.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-CA,en;q=0.9",
                },
                follow_redirects=True,
                timeout=30,
            )

            if r.status_code == 429:
                if attempt == retries:
                    raise httpx.HTTPStatusError("rate_limited", request=r.request, response=r)

                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except Exception:
                        wait = backoff
                else:
                    wait = backoff

                await asyncio.sleep(wait + random.uniform(0.2, 0.9))
                backoff = min(backoff * 2, 90)
                continue

            if r.status_code in (500, 502, 503, 504):
                if attempt == retries:
                    raise httpx.HTTPStatusError("server_error", request=r.request, response=r)
                raise httpx.HTTPStatusError("retryable", request=r.request, response=r)

            r.raise_for_status()
            return r

        except Exception:
            if attempt == retries:
                raise
            await asyncio.sleep(backoff + random.uniform(0.2, 0.9))
            backoff = min(backoff * 2, 90)

    raise RuntimeError(f"Fetch failed unexpectedly for {url}")


# -------------------- Shopify Product JS fallback --------------------

async def fetch_product_js(client: httpx.AsyncClient, handle: str) -> Optional[Dict[str, Any]]:
    """
    Shopify endpoint that returns product JSON reliably, including vendor.
    https://originalluxury.ca/products/<handle>.js
    """
    if not handle:
        return None
    url = f"{BASE_URL}/products/{handle}.js"
    try:
        r = await _fetch(client, url, retries=8)
        return r.json()
    except Exception:
        return None


def sizes_colors_variants_from_product_js(pj: Dict[str, Any]) -> Tuple[List[str], List[str], List[Dict[str, Any]]]:
    if not isinstance(pj, dict):
        return [], [], []

    options = pj.get("options")
    variants = pj.get("variants")
    if not isinstance(options, list) or not isinstance(variants, list):
        return [], [], []

    option_names: List[str] = []
    for o in options:
        # In .js it can be list of strings or list of dicts depending on theme/apps
        if isinstance(o, dict):
            option_names.append(clean_invisible(o.get("name")))
        else:
            option_names.append(clean_invisible(o))

    size_idx = None
    color_idx = None
    for i, nm in enumerate(option_names):
        low = (nm or "").lower()
        if low in ("size", "sizes"):
            size_idx = i
        if low in ("color", "colour", "colors", "colours"):
            color_idx = i

    sizes: List[str] = []
    colors: List[str] = []
    parsed_variants: List[Dict[str, Any]] = []

    for v in variants:
        if not isinstance(v, dict):
            continue
        opts = [
            clean_invisible(v.get("option1")),
            clean_invisible(v.get("option2")),
            clean_invisible(v.get("option3")),
        ]

        if size_idx is not None and size_idx < len(opts) and opts[size_idx]:
            sizes.append(opts[size_idx])
        if color_idx is not None and color_idx < len(opts) and opts[color_idx]:
            colors.append(opts[color_idx])

        parsed_variants.append({
            "id": v.get("id"),
            "sku": clean_invisible(v.get("sku")),
            "price": shopify_money_to_float(v.get("price")),
            "compare_at_price": shopify_money_to_float(v.get("compare_at_price")),
            "available": v.get("available"),
            "option1": opts[0],
            "option2": opts[1],
            "option3": opts[2],
        })


    return uniq_sorted(sizes), uniq_sorted(colors), parsed_variants


def prices_from_product_js(pj: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """
    Derive a reasonable current/original price from variants.
    Normalizes cents vs dollars using shopify_money_to_float().
    """
    variants = pj.get("variants")
    if not isinstance(variants, list) or not variants:
        return None, None

    prices: List[float] = []
    compares: List[float] = []

    for v in variants:
        if not isinstance(v, dict):
            continue

        p = shopify_money_to_float(v.get("price"))
        c = shopify_money_to_float(v.get("compare_at_price"))

        if p is not None and p > 0:
            prices.append(p)
        if c is not None and c > 0:
            compares.append(c)

    current = min(prices) if prices else None
    original = max(compares) if compares else None
    return current, original


# -------------------- HTML parsing (good when it works; kept as base) --------------------

def parse_json_ld_product(soup: BeautifulSoup) -> Dict[str, Any]:
    for tag in soup.select('script[type="application/ld+json"]'):
        raw = tag.get_text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        candidates = data if isinstance(data, list) else [data]
        # Support @graph
        expanded: List[Dict[str, Any]] = []
        for c in candidates:
            if isinstance(c, dict) and isinstance(c.get("@graph"), list):
                expanded.extend([g for g in c["@graph"] if isinstance(g, dict)])
        candidates = candidates + expanded

        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type")
            if t == "Product" or (isinstance(t, list) and "Product" in t):
                return obj
    return {}


def brand_from_ld(ld: Dict[str, Any]) -> Optional[str]:
    b = ld.get("brand")
    if isinstance(b, dict):
        return clean_invisible(b.get("name"))
    if isinstance(b, str):
        return clean_invisible(b)
    return None


def image_from_page(soup: BeautifulSoup) -> Optional[str]:
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        return normalize_url(og["content"])
    img = soup.select_one("img[src]")
    if img and img.get("src"):
        return normalize_url(img["src"])
    return None


def parse_product_page(html: str, product_url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    lines = extract_text_lines(soup)

    # Product name
    h1 = soup.find("h1")
    product_name = clean_invisible(h1.get_text(strip=True)) if h1 else None

    # MSKU
    msku = find_after_label(lines, "MSKU")

    # Try JSON-LD brand as primary HTML signal (often correct)
    ld = parse_json_ld_product(soup)
    brand = brand_from_ld(ld)

    # Fallback heuristic: a vendor link near title
    if not brand:
        for a in soup.select("a[href*='/collections/']"):
            t = clean_invisible(a.get_text(strip=True))
            if t and len(t) <= 40 and t.lower() not in ("men", "women", "sale", "new"):
                brand = t
                break

    # Size / Color from visible lines (kept, but JS fallback is more reliable)
    sizes: List[str] = []
    colors: List[str] = []

    size_block = find_after_label(lines, "Size")
    if size_block:
        sizes = uniq_sorted(re.split(r"\s+", clean_invisible(size_block)))

    for i, ln in enumerate(lines):
        if ln.lower().startswith("color:"):
            val = clean_invisible(ln.split(":", 1)[1]) if ":" in ln else ""
            if val:
                colors.append(val)
            elif i + 1 < len(lines):
                colors.append(clean_invisible(lines[i + 1]))
    colors = uniq_sorted(colors)

    image = image_from_page(soup)

    return {
        "msku": clean_invisible(msku),
        "product_name": clean_invisible(product_name),
        "brand": clean_invisible(brand),
        "current_price": None,
        "original_price": None,
        "sizes": sizes,
        "colors": colors,
        "variants": None,
        "url": product_url,
        "image": image,
    }


# -------------------- Handle discovery --------------------

@dataclass
class ScrapeStats:
    pages_scanned: int = 0
    handles_found: int = 0
    products_fetched: int = 0
    products_failed: int = 0


def _extract_total_products_from_collection(html: str) -> Optional[int]:
    """
    Best-effort: attempt to find total products count from collection page text.
    If not found, returns None.
    """
    # Common patterns: "1715 products", "Products (1715)", etc.
    m = re.search(r"(\d{2,6})\s+products\b", html, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    m = re.search(r"products\s*\((\d{2,6})\)", html, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None


async def get_all_product_handles(max_pages: Optional[int] = None) -> Tuple[List[str], ScrapeStats, Optional[int]]:
    stats = ScrapeStats()
    handles: Set[str] = set()

    limits = httpx.Limits(max_connections=8, max_keepalive_connections=8)
    async with httpx.AsyncClient(limits=limits) as client:
        # page 1 to estimate total products
        r1 = await _fetch(client, f"{ALL_COLLECTION_URL}?page=1")
        total_products = _extract_total_products_from_collection(r1.text)

        page = 1
        while True:
            if max_pages and page > max_pages:
                break

            url = f"{ALL_COLLECTION_URL}?page={page}"
            r = r1 if page == 1 else await _fetch(client, url)
            stats.pages_scanned += 1

            soup = BeautifulSoup(r.text, "lxml")
            page_handles: Set[str] = set()

            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                m = re.match(r"^/products/([^/?#]+)", href)
                if m:
                    page_handles.add(m.group(1))

            if not page_handles:
                break

            handles |= page_handles

            # progress
            if page == 1:
                per_page = len(page_handles)
                est_pages = None
                if total_products and per_page:
                    est_pages = (total_products + per_page - 1) // per_page
                print(
                    f"[handles] page=1 handles={per_page} total_products={total_products} "
                    f"per_page={per_page} est_pages={est_pages} scanning…",
                    flush=True,
                )

            if page % 5 == 0:
                print(f"[handles] page={page} +{len(page_handles)} total_unique={len(handles)}", flush=True)

            page += 1
            await asyncio.sleep(0.25)

    out = sorted(handles)
    stats.handles_found = len(out)
    return out, stats, total_products


# -------------------- Full scrape (product pages) --------------------

async def scrape_all_products(
    concurrency: int = 3,
    max_pages: Optional[int] = None,
    debug_errors: int = 8,
) -> Tuple[List[Dict[str, Any]], ScrapeStats, Optional[int]]:

    handles, stats, total_products = await get_all_product_handles(max_pages=max_pages)
    results: List[Dict[str, Any]] = []

    total = len(handles)
    print(f"[fetch] starting product fetch for {total} handles (concurrency={concurrency})", flush=True)

    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    done = 0
    error_printed = 0

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits) as client:

        async def worker(handle: str):
            nonlocal done, error_printed
            async with sem:
                product_url = f"{BASE_URL}/products/{handle}"
                try:
                    r = await _fetch(client, product_url)
                    data = parse_product_page(r.text, product_url)

                    # Fallback to product.js for vendor + options (ONLY when needed)
                    brand = clean_invisible(data.get("brand"))
                    need_vendor = (not brand) or brand.lower() in ("unknown", "unknown brand", "men", "women")
                    need_opts = (not (data.get("sizes") or [])) or (not (data.get("colors") or []))

                    if need_vendor or need_opts or (data.get("current_price") is None):
                        pj = await fetch_product_js(client, handle)
                        if pj:
                            vendor = clean_invisible(pj.get("vendor"))
                            if vendor:
                                data["brand"] = vendor

                            # Title sometimes more reliable
                            title = clean_invisible(pj.get("title"))
                            if title and not data.get("product_name"):
                                data["product_name"] = title

                            # Prices
                            cp, op = prices_from_product_js(pj)
                            if cp is not None:
                                data["current_price"] = cp
                            if op is not None:
                                data["original_price"] = op

                            # Image
                            imgs = pj.get("images")
                            if isinstance(imgs, list) and imgs:
                                data["image"] = normalize_url(str(imgs[0]))

                            # Options -> sizes/colors/variants
                            s2, c2, v2 = sizes_colors_variants_from_product_js(pj)
                            if s2:
                                data["sizes"] = s2
                            if c2:
                                data["colors"] = c2
                            if v2:
                                data["variants"] = v2

                    # Final normalization
                    if not clean_invisible(data.get("brand")):
                        data["brand"] = "Unknown Brand"

                    if not clean_invisible(data.get("product_name")):
                        raise ValueError("Missing product_name after parse")

                    results.append(data)
                    stats.products_fetched += 1

                except Exception as e:
                    stats.products_failed += 1
                    if error_printed < debug_errors:
                        error_printed += 1
                        status = getattr(getattr(e, "response", None), "status_code", None)
                        print(f"[ERROR] handle={handle} status={status} err={repr(e)}", flush=True)

                await asyncio.sleep(0.08)  # gentle pacing

                async with lock:
                    done += 1
                    if done % 25 == 0 or done == total:
                        print(
                            f"[fetch] done={done}/{total} fetched={stats.products_fetched} failed={stats.products_failed}",
                            flush=True,
                        )

        await asyncio.gather(*[worker(h) for h in handles])

    return results, stats, total_products


# -------------------- CLI test --------------------

if __name__ == "__main__":
    # Test scrape quickly
    max_pages = int(os.getenv("MAX_PAGES", "2")) if os.getenv("MAX_PAGES") else 2
    concurrency = int(os.getenv("SCRAPE_CONCURRENCY", "3"))

    t0 = time.time()
    products, st, total = asyncio.run(scrape_all_products(concurrency=concurrency, max_pages=max_pages))
    dt = time.time() - t0

    print(f"Pages scanned: {st.pages_scanned}")
    print(f"Handles found: {st.handles_found} (site reported: {total})")
    print(f"Fetched: {st.products_fetched}  Failed: {st.products_failed}")
    print(f"Time: {dt:.1f}s")
    print("Sample:", products[:2])
