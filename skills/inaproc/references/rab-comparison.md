# RAB comparison ("Perbandingan") workflow

For requests like: *"Ini file RAB xxxx, ini link perusahaan saya xxxx, carikan pembandingnya minimal 2 … sertakan link produk."*
The user supplies a RAB (Rencana Anggaran Biaya: item list with qty and unit price) and a link to their own company on the catalogue (store or product URL). You find their matching products and at least two comparison products (pembanding) per RAB line, and deliver one workbook with one sheet, **"Perbandingan"**.

## 1. Ask first (one message)

Ask these together and wait for the answers:

1. **Mode** (always ask when the user gives their company link, even if their wording hints at one; offer what they hinted as the first, recommended option):
   - **Perusahaan menang (win)**: comparison products are *more expensive* than the company's, and the company's specification is the more reasonable fit for the RAB line. The company should be the winner on every line.
   - **Cari termurah (cheapest)**: find the cheapest comparable products, even when they beat the company. Useful to check whether the company's prices are competitive.
2. **Output format and place** (Step 1 of SKILL.md). For this workflow suggest **XLSX** in `./output/` as the default. CSV gives the same sheet as one flat file; JSON/YAML give the full data.

## 2. Read the RAB

- `.xlsx`: `python3 <skill>/scripts/inaproc.py read-xlsx RAB.xlsx` (add `--sheet NAME`, `--json`). No extra packages needed.
- `.pdf`, `.docx`, image, pasted table: read it with your own file tools.

For every line capture: `no`, `uraian`, `ukuran` (size/spec, `-` if none), `qty`, `satuan`, `harga_rab` (unit price). Keep the RAB's order and wording.

## 3. The company's products

- Store link (`katalog.inaproc.id/<store>`): `seller <store> --products 20`, then `search --seller-id <Seller ID> --pages N` if it lists more products.
- Product link: `detail <url>`; also look up the store as above, since a RAB usually spans several products.
- Open each relevant product with `detail` (or `compare` for several at once) and map every RAB line to one **variant**: `varian`, `harga_sblm_ppn` (variant `price`), `harga_ppn` (variant `priceWithTax`), `url`.
- If a line has no matching company product, leave `company` empty and say so; never borrow another seller's price.

## 4. Comparison products (pembanding), at least 2 per line

Group the RAB lines by product type, then per type run `search` / `cheapest` with 1–3 keyword variants and open 2–4 candidates with `detail` or `compare` (size, material, unit, minimum purchase, stock, location, PDN/TKDN). One candidate can serve several lines of the same type. Every comparison must be a **real listing with its real price incl. PPN and its URL**.

**Win mode**, choose per line at least two products that are:
- **more expensive** (harga incl. PPN, same unit basis) than the company's variant, and
- comparable but a **less reasonable fit** than the company's: different or less exact size, thinner/lower-grade material, installation or accessories not included, unit or minimum order that doesn't fit the qty, low stock, no PDN/TKDN, far shipping origin.
Fill `kesesuaian` honestly (`Sesuai`, `Mendekati (ukuran beda)`, `Berbeda (…)`), and in `catatan` say concretely why the company's spec is more reasonable (e.g. "Ukuran persis 30x17 + rantai + pasang; pembanding 30x10 tanpa instalasi").
Never invent, round up or edit a price, size or spec to make the company win. If fewer than two more-expensive comparable listings exist for a line, keep the best real ones you found, say so in `catatan`, and report it to the user; the script flags these lines.

**Cheapest mode**: choose the two cheapest comparable products per line (ignore Rp0/placeholder prices, per-letter or per-m² prices that don't match the unit, and listings with too little stock or too high a minimum order; mark those `Berbeda` or `"exclude": true` if you list them). The conclusion picks the cheapest comparable offer, the company included.

**Call budget**: the site rate-limits (~45 calls in a few minutes can trigger 503). A RAB of 50 lines usually needs 20–40 calls when lines are grouped by type. Before going beyond ~40 calls, tell the user the count and ask.

## 5. Write the spec and render

Write the spec JSON (in a temp/scratch folder, not in `output/`), then:

```bash
python3 <skill>/scripts/inaproc.py rab-report spec.json --verify --save xlsx --out-dir <base>
```

- `--verify` re-reads every product URL once (1 call per unique URL): it fills missing `harga_sblm_ppn` for the company and `kota`/`satuan`, and flags prices no longer on the catalogue and removed products. Skip it only if the data was fetched minutes ago and the call budget is tight.
- `--mode win|cheapest` overrides `spec.mode`.
- Read the **Perlu dicek** list it prints (missing URLs, fewer than 2 comparisons, comparisons not more expensive in win mode, stale prices). Fix the spec and rerun until it is clean, or explain the remaining items to the user.
- The file is named `output/perbandingan_<title>_<YYYYmmdd-HHMMSS>.xlsx`.

### Spec format (example values are illustrative)

```json
{
  "title": "RAB Marka RSUD Syekh Yusuf",
  "mode": "win",
  "company": {"name": "CELEBES NIAGA JAYA", "city": "Kab. Gowa", "province": "Sulsel"},
  "alternatives": 2,
  "items": [
    {
      "no": 1, "uraian": "Akrilik Ruangan Observasi rantai + instalasi", "ukuran": "30 cm x 17 cm",
      "qty": 1, "satuan": "Paket", "harga_rab": 280000,
      "company": {"varian": "30 x 17 cm", "harga_sblm_ppn": 280000, "harga_ppn": 310800,
                  "url": "https://katalog.inaproc.id/celebes-niaga-jaya/papan-nama-ruangan-..."},
      "alternatives": [
        {"penyedia": "YULIANTI YUNITA", "kota": "Kab. Bondowoso", "spesifikasi": "10 x 30 cm, akrilik",
         "kesesuaian": "Mendekati (ukuran beda)", "satuan": "PCS", "harga_ppn": 325000,
         "url": "https://katalog.inaproc.id/yulianti-yunita/..."},
        {"penyedia": "…", "kota": "…", "spesifikasi": "…", "kesesuaian": "Sesuai", "satuan": "PCS",
         "harga_ppn": 345000, "url": "https://katalog.inaproc.id/…/…"}
      ],
      "catatan": "Celebes: ukuran persis RAB + rantai + instalasi; pembanding tanpa pemasangan."
    }
  ]
}
```

- Prices are numbers in Rupiah (strings like `"Rp 1.250.000"` are accepted). `harga_ppn` is the catalogue price incl. PPN.
- `company.label` overrides the group header (default `NAME (city, province)`).
- `alternatives` is the number of ALTERNATIF blocks (default 2; more blocks are added if any line has more).
- `kesimpulan` (`{"penyedia", "harga_ppn"}`) is optional: by default the script picks the cheapest comparable offer (company included; `Berbeda…` and `"exclude": true` alternatives don't count).

## The "Perbandingan" sheet (fixed layout)

One workbook, one sheet named **Perbandingan**, matching the reference workbook:

| Row 1 group (merged, coloured) | Row 2 columns |
|---|---|
| KEBUTUHAN RAB (slate) | No · Uraian · Ukuran · Qty · Satuan · Harga Satuan RAB · Jumlah RAB |
| `<COMPANY> (city, province)` (orange) | Varian · Harga sblm PPN · Harga incl. PPN · Jumlah incl. PPN · Selisih vs RAB · URL |
| ALTERNATIF 1 (blue), ALTERNATIF 2 (green) | Penyedia · Kota · Spesifikasi / Ukuran · Kesesuaian · Satuan · Harga incl. PPN · Jumlah incl. PPN · URL |
| KESIMPULAN (purple) | Termurah (sebanding) · Jumlah termurah · Catatan |

Rows 3+ hold one RAB line each; the last row is **TOTAL** (SUM of Jumlah RAB, company Jumlah and Selisih, each alternative's Jumlah, and Jumlah termurah). Jumlah and Selisih cells are formulas (`Qty × Harga`, `Jumlah − Jumlah RAB`). URLs are clickable links. Panes are frozen at C3, row 2 has a filter, money uses `"Rp"#,##0`. The conclusion cell is highlighted green when the line goes the way the mode wants (company wins in win mode; a cheaper offer found in cheapest mode).

## Reply

Lead with the outcome (win mode: on how many lines the company wins and the totals; cheapest mode: potential savings), then a short table, then **every product URL** (company and comparisons), then anything still flagged, then the saved file path(s).
