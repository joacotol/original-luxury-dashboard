import os
import hashlib
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_SECRET_KEY")
if not url or not key:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SECRET_KEY in .env")

supabase = create_client(url, key)

product = {
    "msku": "TEST-MSKU-001",
    "product_name": "Test Product Name",
    "brand": "Test Brand",
    "current_price": 100.00,
    "original_price": 150.00,
    "url": "https://originalluxury.com/products/test-product",
    "image": "https://via.placeholder.com/600x600.png?text=Original+Luxury",

    # NEW: multiple values
    "sizes": ["S", "M", "L", "XL"],
    "colors": ["Black", "Navy"],

    # OPTIONAL: exact combos (can leave out for now)
    "variants": [
        {"size": "S", "color": "Black"},
        {"size": "M", "color": "Black"},
        {"size": "L", "color": "Navy"},
    ],
}

stable = "|".join(str(product.get(k) or "") for k in [
    "msku","product_name","brand","current_price","original_price",
    "url","image",
    "sizes","colors","variants",
])
product["data_hash"] = hashlib.sha256(stable.encode("utf-8")).hexdigest()

resp = supabase.table("products").upsert(product).execute()
print("Upsert OK. Returned:", resp.data)
