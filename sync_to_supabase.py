import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from supabase import create_client

from scraper_originalluxury import scrape_all_products, clean_invisible

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY")
if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SECRET_KEY in .env")

supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

UPSERT_CHUNK = int(os.getenv("UPSERT_CHUNK", "150"))
SCRAPE_CONCURRENCY = int(os.getenv("SCRAPE_CONCURRENCY", "2"))
PAGE_SIZE = int(os.getenv("DB_PAGE_SIZE", "1000"))


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def stable_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def product_hash(p: Dict[str, Any]) -> str:
    # hash only the fields you care about for "changed"
    payload = {
        "msku": clean_invisible(p.get("msku")),
        "product_name": clean_invisible(p.get("product_name")),
        "brand": clean_invisible(p.get("brand")),
        "current_price": p.get("current_price"),
        "original_price": p.get("original_price"),
        "sizes": p.get("sizes") or [],
        "colors": p.get("colors") or [],
        "url": clean_invisible(p.get("url")),
        "image": clean_invisible(p.get("image")),
        # variants can be large; keep if you want change detection across options
        "variants": p.get("variants") or None,
    }
    return sha256_text(stable_json(payload))


def insert_scrape_run() -> int:
    resp = supabase.table("scrape_runs").insert(
        {"started_at": now_utc_iso(), "status": "running"}
    ).execute()
    run_id = resp.data[0]["id"]
    return int(run_id)


def finalize_scrape_run(
    run_id: int,
    pages_scanned: int,
    handles_found: int,
    fetched: int,
    failed: int,
    new_count: int,
    updated_count: int,
    unchanged_count: int,
    deleted_count: int,
):
    supabase.table("scrape_runs").update(
        {
            "finished_at": now_utc_iso(),
            "status": "done",
            "pages_scanned": pages_scanned,
            "handles_found": handles_found,
            "fetched": fetched,
            "failed": failed,
            "new_count": new_count,
            "updated_count": updated_count,
            "unchanged_count": unchanged_count,
            "deleted_count": deleted_count,
        }
    ).eq("id", run_id).execute()


def fetch_existing_index() -> Dict[str, Dict[str, Any]]:
    """
    Returns: { msku: {data_hash, status, brand} }
    """
    out: Dict[str, Dict[str, Any]] = {}
    offset = 0
    while True:
        resp = (
            supabase.table("products")
            .select("msku,data_hash,status,brand")
            .order("msku")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            break
        for row in rows:
            msku = clean_invisible(row.get("msku"))
            if not msku:
                continue
            out[msku] = {
                "data_hash": clean_invisible(row.get("data_hash")),
                "status": clean_invisible(row.get("status") or "active").lower(),
                "brand": clean_invisible(row.get("brand") or ""),
            }
        offset += PAGE_SIZE
    return out


def dedupe_by_msku(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        msku = clean_invisible(r.get("msku"))
        if not msku:
            continue
        seen[msku] = r
    return list(seen.values())


async def main(max_pages: Optional[int] = None):
    run_id = insert_scrape_run()
    print("[sync] scraping site…", flush=True)

    scraped, stats, total_reported = await scrape_all_products(
        concurrency=SCRAPE_CONCURRENCY,
        max_pages=max_pages,
    )

    print(
        f"[sync] scrape complete handles_found={stats.handles_found} fetched={stats.products_fetched} "
        f"failed={stats.products_failed} site_reported={total_reported}",
        flush=True,
    )

    existing = fetch_existing_index()

    # Build upserts
    upserts: List[Dict[str, Any]] = []
    seen_mskus: set[str] = set()

    new_count = 0
    updated_count = 0
    unchanged_count = 0

    for p in scraped:
        msku = clean_invisible(p.get("msku"))
        if not msku:
            continue

        seen_mskus.add(msku)

        # BRAND PRESERVATION: if scraped brand is Unknown, keep existing brand if available
        scraped_brand = clean_invisible(p.get("brand"))
        prev_brand = clean_invisible((existing.get(msku) or {}).get("brand", ""))

        brand_to_write = scraped_brand
        if (not scraped_brand) or scraped_brand.lower() in ("unknown", "unknown brand"):
            if prev_brand:
                brand_to_write = prev_brand
            else:
                brand_to_write = "Unknown Brand"

        # Compute data hash (use brand_to_write, not scraped_brand)
        p_for_hash = dict(p)
        p_for_hash["brand"] = brand_to_write
        data_hash = product_hash(p_for_hash)

        prev = existing.get(msku)
        if not prev:
            change_type = "new"
            status = "active"
            new_count += 1
        else:
            status = prev.get("status", "active") or "active"
            if prev.get("data_hash") == data_hash and status != "deleted":
                change_type = "unchanged"
                unchanged_count += 1
            else:
                # If it was deleted but now present again, treat as updated + active
                change_type = "updated"
                status = "active"
                updated_count += 1

        upserts.append(
            {
                "msku": msku,
                "product_name": clean_invisible(p.get("product_name")),
                "brand": brand_to_write,
                "current_price": p.get("current_price"),
                "original_price": p.get("original_price"),
                "url": clean_invisible(p.get("url")),
                "image": clean_invisible(p.get("image")),
                "sizes": p.get("sizes") or [],
                "colors": p.get("colors") or [],
                "variants": p.get("variants"),
                "data_hash": data_hash,
                "status": status,
                "change_type": change_type,
                "updated_at": now_utc_iso(),
            }
        )

    # Mark deletions (present in DB but not in this scrape)
    to_delete = [msku for msku in existing.keys() if msku not in seen_mskus]
    deleted_count = 0
    if to_delete:
        print(f"[sync] marking deleted={len(to_delete)}", flush=True)
        deleted_count = len(to_delete)

        # Mark as deleted in chunks
        for i in range(0, len(to_delete), 500):
            chunk = to_delete[i : i + 500]
            supabase.table("products").update(
                {"status": "deleted", "change_type": "deleted", "updated_at": now_utc_iso()}
            ).in_("msku", chunk).execute()

    # Upsert in chunks (dedup per chunk to avoid ON CONFLICT row twice)
    upserts = dedupe_by_msku(upserts)

    print(f"[sync] upserting rows={len(upserts)} chunk={UPSERT_CHUNK}", flush=True)
    for i in range(0, len(upserts), UPSERT_CHUNK):
        batch = dedupe_by_msku(upserts[i : i + UPSERT_CHUNK])
        supabase.table("products").upsert(batch).execute()
        if (i // UPSERT_CHUNK + 1) % 5 == 0:
            print(f"[sync] upsert progress {min(i+UPSERT_CHUNK, len(upserts))}/{len(upserts)}", flush=True)

    finalize_scrape_run(
        run_id=run_id,
        pages_scanned=stats.pages_scanned,
        handles_found=stats.handles_found,
        fetched=stats.products_fetched,
        failed=stats.products_failed,
        new_count=new_count,
        updated_count=updated_count,
        unchanged_count=unchanged_count,
        deleted_count=deleted_count,
    )

    print(
        f"[sync DONE] new={new_count} updated={updated_count} unchanged={unchanged_count} deleted={deleted_count}",
        flush=True,
    )


if __name__ == "__main__":
    # Set max_pages=None for full run. For testing, set MAX_PAGES in env.
    mp = os.getenv("MAX_PAGES")
    max_pages = int(mp) if mp else None
    asyncio.run(main(max_pages=max_pages))
