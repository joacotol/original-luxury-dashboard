import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SECRET_KEY"))

resp = supabase.table("products").select("*").eq("msku", "TEST-MSKU-001").execute()
print(resp.data)
