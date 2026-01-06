import os
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from urllib.parse import quote
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY")
SUPABASE_BRAND_PDF_BUCKET = os.getenv("SUPABASE_BRAND_PDF_BUCKET", "brand-pdfs")

if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SECRET_KEY in .env")

supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

app = FastAPI(title="Original Luxury Catalog Dashboard")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/catalog", response_class=HTMLResponse)
def catalog(request: Request):
    return templates.TemplateResponse("catalog.html", {"request": request})

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/products")
def list_products(
    brand: str | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=500),
):
    query = supabase.table("products").select(
        "msku,product_name,brand,current_price,original_price,url,image,sizes,colors,variants,updated_at,status,change_type"
    )
    if brand:
        query = query.eq("brand", brand)
    if q:
        query = query.or_(f"msku.ilike.%{q}%,product_name.ilike.%{q}%")

    resp = query.order("updated_at", desc=True).limit(limit).execute()
    return {"count": len(resp.data or []), "items": resp.data or []}

@app.get("/brand-pdfs")
def brand_pdfs(include_unknown: bool = False):
    """
    Returns a list of brands + their public PDF URLs.
    Assumes Supabase Storage bucket is PUBLIC and keys are:
      brands/<Brand>.pdf
    """
    rows = supabase.table("products").select("brand").execute().data or []

    brands = []
    for r in rows:
        b = (r.get("brand") or "").strip()
        if not b:
            continue
        if (not include_unknown) and b.lower() == "unknown brand":
            continue
        brands.append(b)

    brands = sorted(set(brands), key=lambda x: x.lower())

    base_public = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BRAND_PDF_BUCKET}/brands"

    items = []
    for b in brands:
        # URL-encode brand because it may include spaces/special chars
        url = f"{base_public}/{quote(b, safe='')}.pdf"
        items.append({"brand": b, "pdf_url": url})

    return {"count": len(items), "items": items}