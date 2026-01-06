let allItems = [];
let activeButton = null;

function setStatus(text) {
  document.getElementById("statusLabel").textContent = text || "";
}

function setCount(n, total) {
  const label = total != null ? `${n} / ${total} brands` : `${n} brands`;
  document.getElementById("countLabel").textContent = label;
}

function renderList(items) {
  const list = document.getElementById("brandList");
  list.innerHTML = "";

  items.forEach(item => {
    const li = document.createElement("li");
    li.className = "brand-item";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = item.brand;

    btn.addEventListener("click", () => {
      if (activeButton) activeButton.classList.remove("active");
      btn.classList.add("active");
      activeButton = btn;

      const url = item.pdf_url;

      document.getElementById("viewerTitle").textContent = item.brand;
      document.getElementById("pdfFrame").src = url;

      const open = document.getElementById("openNewTab");
      open.href = url;
      open.classList.remove("disabled");

      const copy = document.getElementById("copyLink");
      copy.classList.remove("disabled");
      copy.onclick = async () => {
        try {
          await navigator.clipboard.writeText(url);
          setStatus("Link copied.");
          setTimeout(() => setStatus(""), 1200);
        } catch {
          setStatus("Could not copy link.");
          setTimeout(() => setStatus(""), 1200);
        }
      };
    });

    li.appendChild(btn);
    list.appendChild(li);
  });
}

function applySearch() {
  const q = (document.getElementById("brandSearch").value || "").trim().toLowerCase();
  const filtered = q
    ? allItems.filter(x => (x.brand || "").toLowerCase().includes(q))
    : allItems;

  setCount(filtered.length, allItems.length);
  renderList(filtered);
}

async function loadBrands() {
  setStatus("Loading…");
  document.getElementById("viewerTitle").textContent = "Select a brand";
  document.getElementById("pdfFrame").src = "";
  document.getElementById("openNewTab").classList.add("disabled");
  document.getElementById("copyLink").classList.add("disabled");

  const res = await fetch("/brand-pdfs");
  const data = await res.json();

  allItems = data.items || [];
  setCount(allItems.length, null);
  renderList(allItems);
  setStatus("");
}

window.addEventListener("DOMContentLoaded", () => {
  document.getElementById("brandSearch").addEventListener("input", applySearch);
  document.getElementById("refreshBtn").addEventListener("click", async () => {
    await loadBrands();
    applySearch();
  });

  loadBrands().then(applySearch);
});
