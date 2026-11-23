# INAPROC public API reference

## Contents
1. Request format
2. Bundled operations (all public, no login)
3. Search input and enums
4. Key response fields
5. Errors and limits

---

## 1. Request format

```
POST https://katalog.inaproc.id/graphql
content-type: application/json
referer: https://katalog.inaproc.id/
user-agent: <a normal browser UA>

{"operationName": "<name>", "query": "<text from scripts/queries.json>", "variables": {...}}
```

- **Every result is a union with `GenericError { code message reqId }`.** The script turns that into an error message.
- **Introspection is disabled.** Inputs are checked against a JSON schema, so a wrong key returns HTTP 400 with `additionalProperties 'x' not allowed` or `missing properties: 'y'`. That error is the fastest way to learn an input's shape.
- **Any bundled operation can be called directly:**
  `python3 scripts/inaproc.py raw getSellerScore --vars '{"input":{"sellerId":"01K2KD1FCK4CX04NRKXJ29F7E9"}}'`

## 2. Bundled operations

| Operation | Variables (working example) | Returns |
|---|---|---|
| `searchProducts` | `{"input": <SearchProductInput, §3>}` | `total perPage currentPage lastPage items[]` |
| `searchProductsCategoryAggregations` | same input with `filter.strategy:"AGGREGATION"`, `aggregation:["CATEGORY_ALL_LEVEL"]`, `pagination:{page:0,perPage:0}` | category facet counts |
| `getAutoCompleteProduct` | `{"keyword":"kertas"}` | `items{text}` |
| `getProductBySlug` | `{"username":"anak-terusan","slug":"kertas-hvs","regionCode":null}` | full product (§4) |
| `getProductSlugById` | `{"id":"<productId>"}` | `id name slug status` |
| `getProductInformationByIds` | `{"input":{"ids":["<productId>", …]}}` | `id name status actionReasons` |
| `getSpecialProductRegionPrice` | `{"productId":"<id>"}` | regional prices (often `null`) |
| `getOrderProductRatingList` | `{"input":{"filter":{"productId":"<id>"},"pagination":{"page":1,"perPage":10}}}` | reviews with buyer KLPD/Satker |
| `getOrderRatingAggregation` | `{"input":{"filter":{"productId":"<id>"},"aggregationType":"RATING"}}` | `ratingAvg ratingCount reviewCount rating[]` |
| `getSearchSeller` | `{"input":{"filter":{"keyword":"x","strategy":"DEFAULT","labels":[],"sellerRegionCodes":[""]},"pagination":{"page":1,"perPage":10}}}` | `name slug isUMKK transactionCount location` |
| `getSellerInfoBySlug` | `{"filter":{"slugs":["anak-terusan"]}}` | company: `name institusiId isUmkk bentukUsaha status website companyAddresses shippingAddresses` |
| `getSellerGeneralData` | `{"filter":{"slugs":["…"]}}` | company plus district/village of shipping addresses |
| `getSellerScore` | `{"input":{"sellerId":"<institusiId>"}}` | `finalScore orderSignedCount details[]` |
| `getAssessmentSummaryDashboard` | `{"sellerId":"<institusiId>"}` | `avgScore totalAssessment totalScore byCategory` |
| `getAllMinifiedCategory` | `{"input":{"level":1,"productType":"PHYSICAL"}}` | 3-level category tree |
| `AllProductCategoryList` | `{"input":{"filter":{"level":1},"pagination":{"page":1,"perPage":20}}}` | flat categories with KBLI codes |
| `getProductCategory` | `{"id":"<categoryId>"}` | category plus parent chain |
| `getAllRegionProvince` | `{"regionLevel":"PROVINCE"}` | 38 provinces (`areaCode` like `31`) |
| `getAllRegionCity` | `{"regionLevel":"CITY","parentAreaCodes":["31"]}` | cities (`areaCode` like `31.73`) |
| `GetSearchRegion` | `{"level":"CITY","search":"bandung","pagination":{"page":1,"perPage":10}}` | region search |
| `getKBLIList` | `{"page":1,"perPage":50}` | KBLI codes |
| `ppnList` | `{}` | PPN rates `12, 1.2, 0` |

**IDs and how they link:**
- A product's `sellerId` is the same value as the company's `institusiId`, and that's what `getSellerScore` and the search `sellerId` filter take.
- A product URL is `https://katalog.inaproc.id/{username}/{slug}`, and `username` is also the store slug.

## 3. Search input (`SearchProductInput`)

```json
{
  "sort": [{"field": "RELEVANCE", "order": "DESC"}],
  "filter": {
    "strategy": "SRP",
    "keyword": "kertas hvs",
    "categoryIds": ["<categoryId>"],
    "sellerId": "<institusiId>",
    "labels": ["PDN"],
    "sellerTypes": ["UMKK"],
    "sellerRegionCodes": [""],
    "minPrice": 40000, "maxPrice": 100000,
    "rateTypes": [],
    "productTypes": ["PHYSICAL"],
    "ratingAvgGte": null
  },
  "pagination": {"page": 1, "perPage": 60}
}
```

- **`sort.field`:** `RELEVANCE`, `PRICE`, `UNIT_SOLD`, `TKDN_BMP`, `STOCK_ACCUMULATION`, `UPDATED_AT`. **`order`:** `ASC` or `DESC`.
- **`productTypes`:** `PHYSICAL` (barang), `SERVICE` (jasa), `DIGITAL`.
- **`sellerTypes`:** `UMKK`, `OFFICIAL_VENDOR`, `OAP`.
- **`labels`:** `PDN` is confirmed. The UI also shows Import, Verified and Inovasi, but their wire values are unconfirmed.
- **`sellerRegionCodes`:** the format is unconfirmed. Keep it `[""]`.
- **Limits:** `total` is capped at 10,000, and `perPage` goes up to 60.

## 4. Key response fields

**Search item:**

| Field | Meaning |
|---|---|
| `minPriceWithTax`, `maxPriceWithTax` | Price range across variants, including PPN |
| `minPrice`, `maxPrice` | The same range before tax |
| `tkdn{tkdnBmp value bmpValue status}` | Local-content figures (TKDN, BMP and the combined value) |
| `labels` | e.g. `PDN`, `PPN`, `Konsolidasi` |
| `stockAccumulation` | Total stock |
| `unitSold` | Units sold |
| `rating{average count}` | Rating summary |
| `location` | Nested region; the deepest `name` is the seller's area |
| `sellerName`, `username`, `sellerId` | Seller identity |
| `variants[]{price priceWithTax stock options}` | Per-variant prices and stock |

**Product detail (`getProductBySlug`):**

| Field | Meaning |
|---|---|
| `prices{minPurchase minPriceWithTax maxPriceWithTax productWholesalePrices selectedRegionPrice}` | Prices, minimum order, wholesale tiers |
| `stockUnit.primaryUnit` | Unit: RIM, PCS, UNIT, PAKET… |
| `variants[]{optionValues price taxablePrice priceWithTax stock sku}` | Variant prices and stock |
| `tkdn{value bmpValue number expirationDate producerName url}` | TKDN certificate |
| `pdn{type countryName}` | Origin |
| `tax{ppnPercentage taxableRate}` | PPN |
| `productInformations{mainInformations additionalInformations documents}` | Specifications and documents |
| `shipping{weight weightUnit length width height}` | Shipping weight and size |
| `sellerLocation{cityName isFTZ}` | Seller city; `isFTZ` = free trade zone |
| `unitSold`, `rating` | Sales and rating |
| `kbki` | KBKI code |
| `category{name allowedTransactionMethod}` | Category |
| `isAllowedBuyer` | Always `false` without a login, so ignore it |

## 5. Errors and limits

| Response | Meaning | What to do |
|---|---|---|
| HTTP 400 | Wrong variables | Read the message, fix the keys |
| HTTP 401 | Operation needs a login | Out of scope for this skill |
| HTTP 503 / 429 | Cloudflare rate limit | Wait, then call less often (≥1 s apart) |
| `GenericError` with `EntityNotFound` | Bad id or slug | Check the id or slug |
