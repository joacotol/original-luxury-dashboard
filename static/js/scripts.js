function fmtPrice(v) {
  if (v === null || v === undefined || v === "") return "";
  return Number(v).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function pillList(arr) {
  if (!arr || !Array.isArray(arr) || arr.length === 0) return "";
  return arr.map((x) => `<span class="pill">${String(x)}</span>`).join("");
}

async function loadProducts() {
  const q = document.getElementById("q").value.trim();
  const brand = document.getElementById("brand").value.trim();

  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (brand) params.set("brand", brand);
  params.set("limit", "200");

  const status = document.getElementById("status");
  status.textContent = "Loading…";

  const res = await fetch(`/products?${params.toString()}`);
  const data = await res.json();

  const tbody = document.getElementById("tbody");
  tbody.innerHTML = "";

  const items = data.items || [];
  status.textContent = `Showing ${items.length} item(s)`;

  for (const p of items) {
    const img = p.image
      ? `<img class="img" src="${p.image}" alt="product image" />`
      : `<div class="img"></div>`;

    const price = `
      <div><strong>${fmtPrice(p.current_price)}</strong></div>
      <div class="muted">${p.original_price ? "Original: " + fmtPrice(p.original_price) : ""}</div>
    `;

    const sizesColors = `
      <div><div class="muted">Sizes</div>${pillList(p.sizes)}</div>
      <div style="margin-top:8px;"><div class="muted">Colors</div>${pillList(p.colors)}</div>
    `;

    const links = p.url
      ? `<a href="${p.url}" target="_blank" rel="noopener">View on site</a>`
      : "";

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${img}</td>
      <td>${p.brand || ""}</td>
      <td>
        <div><strong>${p.product_name || ""}</strong></div>
        <div class="muted">${p.updated_at ? "Updated: " + p.updated_at : ""}</div>
      </td>
      <td>${p.msku || ""}</td>
      <td>${price}</td>
      <td>${sizesColors}</td>
      <td>${links}</td>
    `;
    tbody.appendChild(tr);
  }
}

function resetFilters() {
  document.getElementById("q").value = "";
  document.getElementById("brand").value = "";
  loadProducts();
}

function bindEvents() {
  document.getElementById("btnSearch").addEventListener("click", loadProducts);
  document.getElementById("btnReset").addEventListener("click", resetFilters);

  // Enter key triggers search
  document.getElementById("q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadProducts();
  });
  document.getElementById("brand").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadProducts();
  });
}

document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  loadProducts();
});
