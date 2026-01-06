import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple
import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://originalluxury.ca"
ALL_COLLECTIONS_URL = f"{BASE_URL}/collections/all"
USER_AGENT = "OriginalLuxuryScraper/1.0 (contact: admin@originalluxury.ca)"

@dataclass
class ScrapeStats:
    pages_scanned: int = 0
    handles_found: int = 0
    products_fetched: int = 0
    products_failed: int = 0

def _normalize_url(u: str) -> str:
    if not u:
        return u
    if u.startswith("//"):
        return "https:" + u
    return u

def _uniq_sorted(values: List[str]) -> List[str]:
    cleaned = [v.strip() for v in values if v and v.strip()]
    return sorted(set(cleaned), key=lambda x: x.lower())

def _pick_msku(product_js: Dict[str, Any]) -> str:
    """
    Shopify product .js typically includes variants[].sku
    MSKU is the same regardless of size/color, so:
    - pick the first non-empty sku
    - if all empty, fall back to handle (not ideal, but prevents null key)
    """

    for v in product_js.get("variants", []) or []:
        sku = (v.get("sku") or "").strip()
        if sku:
            return sku
    return (product_js.get("handle") or "").strip()

def _extract_sizes_colors(product_js: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """
    Extract size/color by reading the option names and variant option values.
    Many products have options like: Size/Material/Color, etc.
    """
    # e.g. ["Size", "Material", "Color"]
    option_names = product_js.get("options", []) or [] 
    variants = product_js.get("variants", []) or []

    size_idx = None
    color_idx = None
    for i, name in enumerate(option_names):
        n = (name or "").lower()
        if "size" in n:
            size_idx = i + 1 # option 1/2/3 are 1-based
        if "color" in n or "colour" in n:
            color_idx = i + 1

    sizes: List[str] = []
    colors: List[str] = []

    for v in variants:
        if size_idx:
            sizes.append((v.get(f"option{size_idx}") or "").strip())
        if color_idx:
            colors.append((v.get(f"option{color_idx}") or "").strip())

    return _uniq_sorted(sizes), _uniq_sorted(colors)


def _prices_from_variants(product_js: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """
    Shopify product .js returns prices in cents.
    - current_price: minimum variant price
    - original_price: minimum compare_at_price if present
    """

    prices = []
    compares = []
    for v in product_js.get("variants", []) or []:
        p = v.get("price")
        c = v.get("compare_at_price")
        if isinstance(p, int):
            prices.append(p)
        if isinstance(c, int):
            compares.append(c)
    
    current = (min(prices) / 100.0) if prices else None
    original = (min(compares) / 100.0) if compares else None
    return current, original

async def _fetch(client: httpx.AsyncClient, url: str, retries: int = 4) -> httpx.Response:
    backoff = 1.0
    for attempt in range(retries + 1):
        try:
            r = await client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30)
            
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"retryable status {r.status_code}", request=r.request, response=r)
            
            return r
        except Exception:
            if attempt == retries:
                raise
            
            await asyncio.sleep(backoff)
            backoff *= 2

    raise RuntimeError("unreachable")

async def get_all_product_handles(max_pages: Optional[int] = None) -> Tuple[List[str], ScrapeStats]:
    """
    Scrape handles from /collections/all?page=N
    Stops when a page yields no /products/ links.
    """
    stats = ScrapeStats()
    handles: Set[str] = set()

    async with httpx.AsyncClient() as client:
        page = 1
        while True:
            if max_pages and page > max_pages:
                break
            
            url = f"{ALL_COLLECTIONS_URL}?page={page}"
            r = await _fetch(client, url)
            stats.pages_scanned += 1

            soup = BeautifulSoup(r.text, "lxml")

            # collect all links to /products/{handle}
            page_handles: Set[str] = set()
            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                m = re.match(r"^/products/([^/?#]+)", href)
                if m:
                    page_handles.add(m.group(1))

            if not page_handles:
                break # no products => end pagination

            handles |= page_handles
            page += 1

    out = sorted(handles)
    stats.handles_found = len(out)
    return out, stats

async def fetch_product_from_js(client: httpx.AsyncClient, handle: str) -> Dict[str, Any]:
    """
    Primary: /products/{handle}.js
    This gives structured JSON (title, vendor, varitants, images, etc.)
    """
    url = f"{BASE_URL}/products/{handle}.js"
    r = await _fetch(client, url)
    return r.json()

def transform_product(product_js: Dict[str, Any]) -> Dict[str, Any]:
    """
    Output schema requested:
      MSKU, product name, brand, current price, original price, size, color, url, image
    Store sizes/colors as arrays (Postgres text[]).        
    """
    handle = (product_js.get("handle") or "").strip()
    title = (product_js.get("title") or "").strip()
    brand = (product_js.get("vendor") or "").strip()

    msku = _pick_msku(product_js)
    sizes, colors = _extract_sizes_colors(product_js)
    current_price, original_price = _prices_from_variants(product_js)

    images = product_js.get("images") or []
    image = _normalize_url(images[0]) if images else None

    return {
        "msku": msku,
        "product_name": title,
        "brand": brand,
        "current_price": current_price,
        "original_price": original_price,
        "sizes": sizes, # List[str]
        "colors": colors, # List[str]
        "url": f"{BASE_URL}/products/{handle}",
        "image": image
    }

async def scrape_all_products(concurrency: int = 8, max_pages: Optional[int] = None) -> Tuple[List[Dict[str, Any]], ScrapeStats]:

    handles, stats = await get_all_product_handles(max_pages=max_pages)

    results: List[Dict[str, Any]] = []
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:

        async def worker(h: str):
            nonlocal results, stats
            async with sem:
                try:
                    js = await fetch_product_from_js(client, h)
                    results.append(transform_product(js))
                    stats.products_fetched += 1
                except Exception:
                    stats.products_failed += 1

        await asyncio.gather(*[worker(h) for h in handles])

    return results, stats

if __name__ == "__main__":
    # Quick sanity run (first few pages only) to avoid heavy load while testing.
    t0= time.time()
    products, st = asyncio.run(scrape_all_products(concurrency=6, max_pages=2))
    dt = time.time() - t0
    print(f"Pages scanned: {st.pages_scanned}")
    print(f"Handles found: {st.handles_found}")
    print(f"Fetched: {st.products_fetched}  Failed: {st.products_failed}")
    print(f"Time: {dt:.1f}s")
    print("Sample:", products[:2])




        
