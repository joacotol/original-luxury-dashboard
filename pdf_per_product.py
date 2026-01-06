import asyncio
import hashlib
import io
import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from supabase import create_client
from PIL import Image

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors as rl_colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, Table, TableStyle
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY")
if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SECRET_KEY in .env")
supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

LOCAL_DIR = os.getenv("LOCAL_PRODUCT_PDF_DIR", "./exports/product_pdfs")
os.makedirs(LOCAL_DIR, exist_ok=True)

CACHE_DIR = os.getenv("IMAGE_CACHE_DIR", "./cache/images")
os.makedirs(CACHE_DIR, exist_ok=True)

BOOTSTRAP_SENTINEL = os.path.join(LOCAL_DIR, ".bootstrap_done")

CONCURRENCY = int(os.getenv("PDF_CONCURRENCY", "2"))
UPLOAD_TO_SUPABASE = os.getenv("UPLOAD_TO_SUPABASE", "0") == "1"
BUCKET = os.getenv("SUPABASE_PRODUCT_PDF_BUCKET", "product-pdfs")

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


def clean_invisible(s: Any) -> str:
    if s is None:
        return ""
    s = str(s).replace("\ufeff", "")
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cf"))
    s = re.sub(r"\s+", " ", s).strip()
    return s


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def safe_segment(s: str, max_len: int = 80) -> str:
    s = clean_invisible(s)
    s = re.sub(r"[\/\\:*?\"<>|]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:max_len] or "").strip() or "Unknown"


def make_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="Brand",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=20,
        alignment=TA_CENTER,
        spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        name="ProductName",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        alignment=TA_CENTER,
        spaceAfter=10,
    ))
    styles.add(ParagraphStyle(
        name="Detail",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        leading=11,
        alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        name="Muted",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8,
        leading=10,
        textColor=rl_colors.grey,
        alignment=TA_CENTER,
    ))
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
        t.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        return t

    with Image.open(io.BytesIO(image_bytes)) as im:
        w, h = im.size

    scale = min(box_w / w, box_h / h)
    draw_w = max(1, int(w * scale))
    draw_h = max(1, int(h * scale))

    rl_img = RLImage(io.BytesIO(image_bytes), width=draw_w, height=draw_h)
    t = Table([[rl_img]], colWidths=[box_w], rowHeights=[box_h], hAlign="CENTER")
    t.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def local_pdf_path(brand: str, product_name: str, msku: str) -> str:
    brand_dir = os.path.join(LOCAL_DIR, safe_segment(brand) or "Unknown Brand")
    os.makedirs(brand_dir, exist_ok=True)
    filename = f"{safe_segment(brand)} - {safe_segment(product_name, 90)} - {safe_segment(msku, 60)}.pdf"
    return os.path.join(brand_dir, filename)


def storage_key_for_product(brand: str, msku: str) -> str:
    # Use safe folder and a hash filename to avoid invalid-key errors forever
    b = safe_segment(brand, 60)
    h = sha256_text(clean_invisible(msku))[:16]
    return f"products/{b}/{h}.pdf"


def build_product_pdf_bytes(p: Dict[str, Any], image_bytes: Optional[bytes]) -> bytes:
    styles = make_styles()
    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
    )

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

    story: List[Any] = []
    story.append(Paragraph(brand, styles["Brand"]))
    story.append(Paragraph(name, styles["ProductName"]))

    story.append(_image_block(image_bytes, box_w=box_w, box_h=IMAGE_H))
    story.append(Spacer(1, 12))

    story.append(Paragraph(f"<b>MSKU:</b> {msku or '—'}", styles["Detail"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"<b>Current Price:</b> {fmt_price(p.get('current_price'))}", styles["Detail"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"<b>Original Price:</b> {fmt_price(p.get('original_price'))}", styles["Detail"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"<b>Sizes:</b> {sizes_str}", styles["Detail"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"<b>Colors:</b> {colors_str}", styles["Detail"]))
    story.append(Spacer(1, 12))

    qr_draw = _qr_drawing(url)
    qr_table = Table([[qr_draw]], colWidths=[QR_SIZE], rowHeights=[QR_SIZE], hAlign="CENTER")
    qr_table.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(qr_table)
    story.append(Spacer(1, 6))
    story.append(Paragraph("Scan to open product page", styles["Muted"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f'<link href="{url}">{url}</link>', styles["Detail"]))
    story.append(Spacer(1, 10))
    story.append(Paragraph(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", styles["Muted"]))

    def on_page(canv, _doc):
        if status == "deleted":
            canv.saveState()
            canv.setFillColor(rl_colors.lightgrey)
            canv.translate(PAGE_W / 2, PAGE_H / 2)
            canv.rotate(35)
            canv.setFont("Helvetica-Bold", 60)
            canv.drawCentredString(0, 0, "REMOVED")
            canv.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


def fetch_products_all(page_size: int = 1000) -> List[Dict[str, Any]]:
    out = []
    offset = 0
    while True:
        resp = (
            supabase.table("products")
            .select("msku,brand,product_name,url,image,sizes,colors,status,current_price,original_price,change_type")
            .order("msku")
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = resp.data or []
        if not batch:
            break
        out.extend(batch)
        offset += page_size
    return out


def fetch_products_changed(page_size: int = 1000) -> List[Dict[str, Any]]:
    out = []
    offset = 0
    while True:
        resp = (
            supabase.table("products")
            .select("msku,brand,product_name,url,image,sizes,colors,status,current_price,original_price,change_type")
            .in_("change_type", ["new", "updated", "deleted"])
            .order("msku")
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = resp.data or []
        if not batch:
            break
        out.extend(batch)
        offset += page_size
    return out


async def generate_pdfs():
    first_run = not os.path.exists(BOOTSTRAP_SENTINEL)

    items = fetch_products_all(PAGE_SIZE) if first_run else fetch_products_changed(PAGE_SIZE)
    mode = "ALL" if first_run else "CHANGED"
    print(f"[pdf] mode={mode} products={len(items)}", flush=True)

    sem = asyncio.Semaphore(CONCURRENCY)
    built = skipped = failed = 0

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:

        async def worker(p: Dict[str, Any]):
            nonlocal built, skipped, failed
            async with sem:
                try:
                    msku = clean_invisible(p.get("msku"))
                    if not msku:
                        skipped += 1
                        return

                    brand = clean_invisible(p.get("brand") or "Unknown Brand")
                    name = clean_invisible(p.get("product_name") or "Unknown Product")

                    local_path = local_pdf_path(brand, name, msku)

                    # Skip unchanged on incremental if file exists
                    if (not first_run) and os.path.exists(local_path):
                        skipped += 1
                        return

                    img = await fetch_image_jpeg(client, p.get("image") or "")
                    pdf_bytes = build_product_pdf_bytes(p, img)

                    with open(local_path, "wb") as f:
                        f.write(pdf_bytes)

                    if UPLOAD_TO_SUPABASE:
                        key = storage_key_for_product(brand, msku)
                        supabase.storage.from_(BUCKET).upload(
                            path=key,
                            file=pdf_bytes,
                            file_options={"content-type": "application/pdf", "upsert": "true"},
                        )

                    built += 1
                    if built % 50 == 0:
                        print(f"[pdf] built={built} skipped={skipped} failed={failed}", flush=True)

                except Exception as e:
                    failed += 1
                    print(f"[ERROR] product PDF failed MSKU={p.get('msku')!r}: {repr(e)}", flush=True)

        await asyncio.gather(*[worker(p) for p in items])

    if first_run:
        with open(BOOTSTRAP_SENTINEL, "w", encoding="utf-8") as f:
            f.write(datetime.now(timezone.utc).isoformat())

    print(f"[pdf] done built={built} skipped={skipped} failed={failed}", flush=True)


if __name__ == "__main__":
    asyncio.run(generate_pdfs())
