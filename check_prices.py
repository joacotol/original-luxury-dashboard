import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SECRET_KEY"))

# Put a real MSKU here (copy from your products table)
MSKU = "AL6"

resp = (
    supabase.table("products")
    .select("msku,brand,product_name,current_price,original_price,status,updated_at")
    .eq("msku", MSKU)
    .limit(1)
    .execute()
)

print(resp.data)
