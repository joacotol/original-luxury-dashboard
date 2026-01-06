import asyncio
import hashlib
import io
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from supabase import create_client
from PIL import Image

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors as rl_colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, PageBreak
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY")
if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SECRET_KEY in .env")
supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

LOCAL_DIR = os.getenv("LOCAL_BRAND_PDF_DIR", "./exports/brand_catalogs")
os.makedirs(LOCAL_DIR, exist_ok=True)

CACHE_DIR = os.getenv("IMAGE_CACHE_DIR", "./cache/images")
os.makedirs(CACHE_DIR, exist_ok=True)

BOOTSTRAP_SENTINEL = os.path.join(LOCAL_DIR, ".bootstrap_done")
MANIFEST_PATH = os.path.join(LOCAL_DIR, ".brand_manifest.json")

CONCURRENCY = int(os.getenv("PDF_CONCURRENCY", "2"))
UPLOAD_TO_SUPABASE = os.getenv("UPLOAD_TO_SUPABASE", "0") == "1"
BUCKET = os.getenv("SUPABASE_BRAND_PDF_BUCKET", "brand-pdfs")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PAGE_W, PAGE_H = letter
MARGIN = 0.6 * inch
IMAGE_H = 4.2 * inch
QR_SIZE = 1.2 * inch

TARGET_MAX_PX = int(os.getenv("PDF_IMAGE_MAX_PX", "1400"))
JPEG_QUALITY = int(os.getenv("PDF_IMAGE_JPEG_QUALITY", "82"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "1000"))

# Sorting rules based on product name keywords
CATEGORY_ORDER = {"Footwear": 0, "Apparel": 1, "Accessories": 2, "Watches": 3, "Other": 9}
SUBTYPE_ORDER = {
    "Sneakers": 0, "Loafers": 1, "Moccasins": 2, "Drivers": 3, "Derbies": 4, "Oxfords": 5, "Boots": 6, "Slides": 7, "Sandals": 8, "Other Footwear": 99,
    "Coats": 0, "Outerwear": 1, "Jackets": 2, "Blazers & Sport Jackets": 3, "Suits": 4, "Vests & Gilets": 5,
    "Knitwear": 10, "Sweaters": 11, "Cardigans": 12, "Hoodies & Sweatshirts": 13, "Shirts": 14, "Polos": 15, "T-Shirts": 16,
    "Trousers": 20, "Chinos": 21, "Jeans": 22, "Joggers & Track": 23, "Shorts": 24,
    "Swimwear": 30, "Underwear": 31, "Other Apparel": 99,
    "Scarves": 0, "Ties": 1, "Belts": 2, "Hats & Caps": 3, "Gloves": 4, "Socks": 5, "Bags": 6, "Wallets & Cardholders": 7, "Cases": 8, "Sunglasses": 9, "Jewelry": 10, "Other Accessories": 99,
    "Watches": 0, "Watch Winders": 1, "Other Watches": 99,
    "Other": 999,
}
NAME_RULES = [
    (r"\bwatch\s*winder(s)?\b|\bwinder(s)?\b", "Watches", "Watch Winders"),
    (r"\bwatch(es)?\b|\btimepiece(s)?\b", "Watches", "Watches"),

    (r"\bsneaker(s)?\b|\btrainer(s)?\b", "Footwear", "Sneakers"),
    (r"\bloaf(er|ers)\b", "Footwear", "Loafers"),
    (r"\bmoccasin(s)?\b", "Footwear", "Moccasins"),
    (r"\bdriving\b|\bdriver(s)?\b", "Footwear", "Drivers"),
    (r"\bderby(s)?\b", "Footwear", "Derbies"),
    (r"\boxford(s)?\b", "Footwear", "Oxfords"),
    (r"\bboot(s)?\b", "Footwear", "Boots"),
    (r"\bslide(s)?\b", "Footwear", "Slides"),
    (r"\bsandal(s)?\b", "Footwear", "Sandals"),
    (r"\bshoe(s)?\b|\bfootwear\b", "Footwear", "Other Footwear"),

    (r"\bwaist\s*coat(s)?\b|\bwaistcoat(s)?\b|\bgilet(s)?\b|\bvest(s)?\b|\bsleeveless\b", "Apparel", "Vests & Gilets"),
    (r"\btrench\b|\bovercoat(s)?\b|\bcoat(s)?\b", "Apparel", "Coats"),
    (r"\bparka\b|\bpuffer\b|\bdown\b|\bwindbreaker\b|\bouterwear\b", "Apparel", "Outerwear"),
    (r"\bbomber\b|\bblouson\b|\bjacket(s)?\b", "Apparel", "Jackets"),
    (r"\bsport\s*jacket(s)?\b|\bblazer(s)?\b|\bsport\s*coat(s)?\b", "Apparel", "Blazers & Sport Jackets"),
    (r"\bsuit(s)?\b", "Apparel", "Suits"),

    (r"\bcardigan(s)?\b", "Apparel", "Cardigans"),
    (r"\bknitwear\b|\bknit(ting)?\b", "Apparel", "Knitwear"),
    (r"\bturtleneck(s)?\b|\broll\s*neck(s)?\b", "Apparel", "Sweaters"),
    (r"\bsweater(s)?\b|\bpullover(s)?\b", "Apparel", "Sweaters"),
    (r"\bhoodie(s)?\b|\bsweatshirt(s)?\b", "Apparel", "Hoodies & Sweatshirts"),

    (r"\bpolo(s)?\b", "Apparel", "Polos"),
    (r"\bt[\-\s]?shirt(s)?\b|\btee(s)?\b", "Apparel", "T-Shirts"),
    (r"\bshirt(s)?\b|\bovershirt(s)?\b", "Apparel", "Shirts"),

    (r"\bjean(s)?\b|\bdenim\b", "Apparel", "Jeans"),
    (r"\bchino(s)?\b", "Apparel", "Chinos"),
    (r"\bjogger(s)?\b|\bjogging\b|\btrack\b", "Apparel", "Joggers & Track"),
    (r"\btrouser(s)?\b|\bpant(s)?\b", "Apparel", "Trousers"),
    (r"\bshort(s)?\b", "Apparel", "Shorts"),

    (r"\bswim\b|\bswimwear\b|\btrunk(s)?\b", "Apparel", "Swimwear"),
    (r"\bunderwear\b|\bboxer(s)?\b|\bbrief(s)?\b", "Apparel", "Underwear"),

    (r"\bscarf(s)?\b|\bstole(s)?\b", "Accessories", "Scarves"),
    (r"\btie(s)?\b|\bbow\s*tie(s)?\b", "Accessories", "Ties"),
    (r"\bbelt(s)?\b", "Accessories", "Belts"),
    (r"\bbaseball\s*cap(s)?\b|\bcap(s)?\b|\bhat(s)?\b|\bbucket\s*hat(s)?\b|\bvisor(s)?\b|\bbeanie(s)?\b", "Accessories", "Hats & Caps"),
    (r"\bglove(s)?\b", "Accessories", "Gloves"),
    (r"\bsock(s)?\b", "Accessories", "Socks"),
    (r"\blaptop\s*bag(s)?\b|\btravel\s*bag(s)?\b|\bshoulder\s*bag(s)?\b|\bcross\s*body\b|\bcrossbody\b|\bfanny\s*pack\b|\bbackpack(s)?\b|\btote(s)?\b|\bbriefcase(s)?\b|\bbag(s)?\b", "Accessories", "Bags"),
    (r"\bwallet(s)?\b|\bcard\s*holder(s)?\b|\bcardholder(s)?\b", "Accessories", "Wallets & Cardholders"),
    (r"\bcase(s)?\b", "Accessories", "Cases"),
    (r"\bsunglass(es)?\b|\beyewear\b", "Accessories", "Sunglasses"),
    (r"\bracelet(s)?\b|\bring(s)?\b|\bnecklace(s)?\b|\bjewelry\b", "Accessories", "Jewelry"),
]
_COMPILED_RULES = [(re.compile(pat, re.IGNORECASE), cat, sub) for pat, cat, sub in NAME_RULES]


def clean_invisible(s: Any) -> str:
    if s is None:
        return ""
    s = str(s).replace("\ufeff", "")
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cf"))
    s = re.sub(r"\s+", " ", s).strip()
    return s


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def safe_segment(s: str, max_len: int = 90) -> str:
    s = clean_invisible(s)
    s = re.sub(r"[\/\\:*?\"<>|]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:max_len] or "").strip() or "Unknown"


def normalize_name(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def infer_category_subtype(product_name: str) -> Tuple[str, str]:
    n = normalize_name(product_name or "")
    for rx, cat, sub in _COMPILED_RULES:
        if rx.search(n):
            return cat, sub
    return "Other", "Other"


def load_manifest() -> Dict[str, Any]:
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_manifest(manifest: Dict[str, Any]) -> None:
    tmp = MANIFEST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MANIFEST_PATH)


def brand_local_path(brand: str) -> str:
    return os.path.join(LOCAL_DIR, f"{safe_segment(brand, 120)}.pdf")


def brand_storage_key(brand: str) -> str:
    return f"brands/{safe_segment(brand, 80)}.pdf"


def compute_brand_hash(products: List[Dict[str, Any]]) -> str:
    parts = []
    for p in sorted(products, key=lambda x: clean_invisible(x.get("msku") or "").lower()):
        parts.append(
            f"{clean_invisible(p.get('msku'))}|{clean_invisible(p.get('data_hash'))}|{clean_invisible(p.get('status'))}"
        )
    return sha256_text("\n".join(parts))


def make_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Brand", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=18, leading=20, alignment=TA_CENTER, spaceAfter=4))
    styles.add(ParagraphStyle(name="ProductName", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=13, leading=16, alignment=TA_CENTER, spaceAfter=10))
    styles.add(ParagraphStyle(name="Detail", parent=styles["BodyText"], fontName="Helvetica", fontSize=9, leading=11, alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="Muted", parent=styles["BodyText"], fontName="Helvetica", fontSize=8, leading=10, textColor=rl_colors.grey, alignment=TA_CENTER))
    return styles


def cache_path_for_url(url: str) -> str:
    return os.path.join(CACHE_DIR, f"{sha256_text(url)}.jpg")


def optimize_image_to_jpeg(image_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(image_bytes)) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = min(TARGET_MAX_PX / max(w, h), 1.0)
        if scale < 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
        return out.getvalue()


async def fetch_image_jpeg(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
    url = clean_invisible(url)
    if not url:
        return None

    path = cache_path_for_url(url)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with open(path, "rb") as f:
                return f.read()
        except Exception:
            pass

    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        jpg = optimize_image_to_jpeg(r.content)
        with open(path, "wb") as f:
            f.write(jpg)
        return jpg
    except Exception:
        return None


def _qr_drawing(url: str):
    widget = qr.QrCodeWidget(clean_invisible(url))
    bounds = widget.getBounds()
    w = bounds[2] - bounds[0]
    h = bounds[3] - bounds[1]
    d = renderPDF.Drawing(QR_SIZE, QR_SIZE, transform=[QR_SIZE / w, 0, 0, QR_SIZE / h, 0, 0])
    d.add(widget)
    return d


def _image_block(image_bytes: Optional[bytes], box_w: float, box_h: float):
    if not image_bytes:
        t = Table([[""]], colWidths=[box_w], rowHeights=[box_h], hAlign="CENTER")
        t.setStyle(TableStyle([("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0),("ALIGN",(0,0),(-1,-1),"CENTER"),("VALIGN",(0,0),(-1,-1),"MIDDLE")]))
        return t

    with Image.open(io.BytesIO(image_bytes)) as im:
        w, h = im.size

    scale = min(box_w / w, box_h / h)
    draw_w = max(1, int(w * scale))
    draw_h = max(1, int(h * scale))

    rl_img = RLImage(io.BytesIO(image_bytes), width=draw_w, height=draw_h)
    t = Table([[rl_img]], colWidths=[box_w], rowHeights=[box_h], hAlign="CENTER")
    t.setStyle(TableStyle([("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0),("ALIGN",(0,0),(-1,-1),"CENTER"),("VALIGN",(0,0),(-1,-1),"MIDDLE")]))
    return t


def build_product_page(p: Dict[str, Any], styles, image_bytes: Optional[bytes]) -> List[Any]:
    brand = clean_invisible(p.get("brand") or "Unknown Brand")
    name = clean_invisible(p.get("product_name") or "Unknown Product")
    msku = clean_invisible(p.get("msku") or "")
    url = clean_invisible(p.get("url") or "")
    status = (p.get("status") or "active").lower()

    def fmt_price(x):
        if isinstance(x, (int, float)):
            return f"CAD {x:,.2f}"
        return clean_invisible(x) or "—"

    sizes = p.get("sizes") or []
    colors = p.get("colors") or []
    sizes_str = ", ".join([clean_invisible(s) for s in sizes if clean_invisible(s)]) or "—"
    colors_str = ", ".join([clean_invisible(c) for c in colors if clean_invisible(c)]) or "—"

    box_w = PAGE_W - 2 * MARGIN

    flow: List[Any] = []
    flow.append(Paragraph(brand, styles["Brand"]))
    flow.append(Paragraph(name, styles["ProductName"]))
    flow.append(_image_block(image_bytes, box_w=box_w, box_h=IMAGE_H))
    flow.append(Spacer(1, 12))

    flow.append(Paragraph(f"<b>MSKU:</b> {msku or '—'}", styles["Detail"]))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(f"<b>Current Price:</b> {fmt_price(p.get('current_price'))}", styles["Detail"]))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(f"<b>Original Price:</b> {fmt_price(p.get('original_price'))}", styles["Detail"]))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(f"<b>Sizes:</b> {sizes_str}", styles["Detail"]))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(f"<b>Colors:</b> {colors_str}", styles["Detail"]))
    flow.append(Spacer(1, 12))

    qr_draw = _qr_drawing(url)
    qr_table = Table([[qr_draw]], colWidths=[QR_SIZE], rowHeights=[QR_SIZE], hAlign="CENTER")
    qr_table.setStyle(TableStyle([("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0),("ALIGN",(0,0),(-1,-1),"CENTER"),("VALIGN",(0,0),(-1,-1),"MIDDLE")]))
    flow.append(qr_table)
    flow.append(Spacer(1, 6))
    flow.append(Paragraph("Scan to open product page", styles["Muted"]))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(f'<link href="{url}">{url}</link>', styles["Detail"]))
    flow.append(Spacer(1, 10))
    flow.append(Paragraph(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", styles["Muted"]))

    # watermark handled by on_page using page_statuses
    return flow


def fetch_all_products() -> List[Dict[str, Any]]:
    out = []
    offset = 0
    while True:
        resp = (
            supabase.table("products")
            .select("msku,brand,product_name,url,image,sizes,colors,status,current_price,original_price,data_hash,change_type")
            .order("msku")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = resp.data or []
        if not batch:
            break
        out.extend(batch)
        offset += PAGE_SIZE
    return out


def fetch_changed_brands() -> Set[str]:
    brands: Set[str] = set()
    offset = 0
    while True:
        resp = (
            supabase.table("products")
            .select("brand,change_type")
            .in_("change_type", ["new", "updated", "deleted"])
            .order("brand")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = resp.data or []
        if not batch:
            break
        for r in batch:
            b = clean_invisible(r.get("brand") or "")
            if b:
                brands.add(b)
        offset += PAGE_SIZE
    return brands


def fetch_products_for_brand(brand: str) -> List[Dict[str, Any]]:
    out = []
    offset = 0
    while True:
        resp = (
            supabase.table("products")
            .select("msku,brand,product_name,url,image,sizes,colors,status,current_price,original_price,data_hash")
            .eq("brand", brand)
            .order("msku")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = resp.data or []
        if not batch:
            break
        out.extend(batch)
        offset += PAGE_SIZE
    return out


async def build_brand_pdf(brand: str, products: List[Dict[str, Any]], client: httpx.AsyncClient) -> Tuple[str, str, Optional[str]]:
    styles = make_styles()

    def sort_key(p: Dict[str, Any]):
        cat, sub = infer_category_subtype(p.get("product_name") or "")
        return (
            CATEGORY_ORDER.get(cat, 9),
            SUBTYPE_ORDER.get(sub, 999),
            normalize_name(p.get("product_name") or ""),
            clean_invisible(p.get("msku") or "").lower(),
        )

    products_sorted = sorted(products, key=sort_key)
    brand_hash = compute_brand_hash(products_sorted)

    sem = asyncio.Semaphore(CONCURRENCY)

    async def get_img(p: Dict[str, Any]) -> Optional[bytes]:
        async with sem:
            return await fetch_image_jpeg(client, p.get("image") or "")

    images = await asyncio.gather(*[get_img(p) for p in products_sorted])

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN, title=f"{brand} Catalog")

    story: List[Any] = []
    page_statuses: List[str] = []

    for i, p in enumerate(products_sorted):
        story.extend(build_product_page(p, styles, images[i]))
        page_statuses.append((p.get("status") or "active").lower())
        if i < len(products_sorted) - 1:
            story.append(PageBreak())

    def on_page(canv, _doc):
        idx = canv.getPageNumber() - 1
        if 0 <= idx < len(page_statuses) and page_statuses[idx] == "deleted":
            canv.saveState()
            canv.setFillColor(rl_colors.lightgrey)
            canv.translate(PAGE_W / 2, PAGE_H / 2)
            canv.rotate(35)
            canv.setFont("Helvetica-Bold", 60)
            canv.drawCentredString(0, 0, "REMOVED")
            canv.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    pdf_bytes = buf.getvalue()

    local_path = brand_local_path(brand)
    with open(local_path, "wb") as f:
        f.write(pdf_bytes)

    storage_path = None
    if UPLOAD_TO_SUPABASE:
        storage_path = brand_storage_key(brand)
        supabase.storage.from_(BUCKET).upload(
            path=storage_path,
            file=pdf_bytes,
            file_options={"content-type": "application/pdf", "upsert": "true"},
        )

    return local_path, brand_hash, storage_path


async def bootstrap_all():
    all_products = fetch_all_products()
    by_brand: Dict[str, List[Dict[str, Any]]] = {}
    for p in all_products:
        b = clean_invisible(p.get("brand") or "") or "Unknown Brand"
        by_brand.setdefault(b, []).append(p)

    manifest = load_manifest()
    built = skipped = failed = 0

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        for brand in sorted(by_brand.keys(), key=lambda x: x.lower()):
            try:
                products = by_brand[brand]
                current_hash = compute_brand_hash(products)
                local_path = brand_local_path(brand)

                if os.path.exists(local_path) and manifest.get(brand, {}).get("brand_hash") == current_hash:
                    skipped += 1
                    continue

                lp, bh, sp = await build_brand_pdf(brand, products, client)
                manifest[brand] = {
                    "brand_hash": bh,
                    "built_at": datetime.now(timezone.utc).isoformat(),
                    "local_path": lp,
                    "storage_path": sp,
                    "product_count": len(products),
                }
                save_manifest(manifest)

                built += 1
                if built % 10 == 0:
                    print(f"[brand bootstrap] built={built} skipped={skipped} failed={failed}", flush=True)

            except Exception as e:
                failed += 1
                print(f"[ERROR bootstrap] brand={brand!r} err={repr(e)}", flush=True)

    with open(BOOTSTRAP_SENTINEL, "w", encoding="utf-8") as f:
        f.write(datetime.now(timezone.utc).isoformat())

    print(f"[brand bootstrap] done built={built} skipped={skipped} failed={failed}", flush=True)


async def incremental_changed():
    manifest = load_manifest()
    changed = fetch_changed_brands()
    missing_local = {b for b, meta in manifest.items() if not os.path.exists(meta.get("local_path") or "")}
    targets = set(changed) | set(missing_local)

    if not targets:
        print("[brand incremental] no changed brands", flush=True)
        return

    built = skipped = failed = 0

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        for brand in sorted(targets, key=lambda x: x.lower()):
            try:
                products = fetch_products_for_brand(brand)
                if not products:
                    skipped += 1
                    continue

                current_hash = compute_brand_hash(products)
                local_path = brand_local_path(brand)

                if os.path.exists(local_path) and manifest.get(brand, {}).get("brand_hash") == current_hash and brand not in missing_local:
                    skipped += 1
                    continue

                lp, bh, sp = await build_brand_pdf(brand, products, client)
                manifest[brand] = {
                    "brand_hash": bh,
                    "built_at": datetime.now(timezone.utc).isoformat(),
                    "local_path": lp,
                    "storage_path": sp,
                    "product_count": len(products),
                }
                save_manifest(manifest)

                built += 1
                if built % 10 == 0:
                    print(f"[brand incremental] built={built} skipped={skipped} failed={failed}", flush=True)

            except Exception as e:
                failed += 1
                print(f"[ERROR incremental] brand={brand!r} err={repr(e)}", flush=True)

    print(f"[brand incremental] done built={built} skipped={skipped} failed={failed}", flush=True)


async def main():
    first_run = not os.path.exists(BOOTSTRAP_SENTINEL)
    if first_run:
        print("[brand] first run: bootstrap all brands", flush=True)
        await bootstrap_all()
    else:
        print("[brand] bootstrap exists: incremental only", flush=True)
    await incremental_changed()


if __name__ == "__main__":
    asyncio.run(main())
