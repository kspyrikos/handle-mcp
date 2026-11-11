# Store API research: nemlig.com, BilkaToGo and REMA 1000

Version 1 research notes. Written 2026-11-01; updated through 2026-11-11.

**Sources:** code, documentation, tests and fixtures in
[eisbaw/nemlig_cli](https://github.com/eisbaw/nemlig_cli) (`nemlig_api.md`),
[kraenhansen/nemlig-mcp](https://github.com/kraenhansen/nemlig-mcp),
[nickknissen/bilkatogo](https://github.com/nickknissen/bilkatogo) (fixtures are real, edited responses), and
[stigvoss/bilkatogo-mcp](https://github.com/stigvoss/bilkatogo-mcp) (no license; consulted for facts only).

These notes were written before the first live calls. Items marked **⚠ live** have not yet been confirmed with a real account; they are collected under "Open questions". Items confirmed since then are listed under "Confirmed live".

## At a glance

| Question | nemlig.com | BilkaToGo |
|---|---|---|
| Login | 3 calls: XSRF → Bearer → `/webapi/login` | 3 calls: Gigya login → Gigya JWT → `LoginJWT` |
| Session lifetime | `.ASPXAUTH` cookie: 1 year. Bearer token: 5 min | Bilka JWT: 24 h. Log in again with email/password |
| Read available slots | `GET /webapi/v2/Delivery/GetDeliveryDays`, including a price per slot | `GET shop/v3/AvailableDeliveryDates`. No price per slot; fixed fee in the cart |
| Selected slot | `GET /webapi/Order/DeliverySpot` → `TimeSlot` | Cart's `deliveryDate` → `intervalStart/End` |
| Fees from API | Yes: `DeliveryPrice` in the cart | Yes: `stat.delivery_price`, `stat.packing` (packing service) |
| Minimum order from API | Yes: `MinimumOrderTotal`, `IsMinTotalValid` | Yes: `stat.minimum_left`, `deliveryAddress.minimum_buy` |
| Home delivery or pickup | Home delivery only | Cart has `hd_available`, `missing_hd_address`. Home: `dawa_uid` or `recipient_name`. Pickup: `sid` and `name` ("Bilka …") (ADR 0006) |
| EAN/GTIN in search | **No** in known responses | **Yes:** `gtin` in Algolia hit |

## nemlig.com

### Login and session
1. `GET /webapi/AntiForgery` → `X-XSRF-TOKEN` + cookie.
2. `GET /webapi/Token` → Bearer JWT, `expires_in: 300` (5 min).
3. `POST /webapi/login` with XSRF + Bearer → `.ASPXAUTH` cookie (1 year).

Implication: save cookies locally only (`~/.config/handle-mcp/nemlig.json`, chmod 600). Fetch a new Bearer token after a 401. Log in again only when the cookie has expired.

Required headers: `Platform: web`, `Device-Size: desktop`, `Version: <app-version>`, `X-Correlation-Id: <uuid4>`. The version can change. **⚠ live:** what happens when an old `Version` is sent?

### Search
`GET https://webapi.prod.knl.nemlig.it/searchgateway/api/search?query=…&take=…&timeslotUtc=…&deliveryZoneId=…`

- `timeslotUtc` and `deliveryZoneId` come from page settings. Price and stock may therefore depend on the delivery time and zone.
- The response contains `Id` (nemlig product number), name, brand, price, `UnitPriceLabel` (for example, "kr./Kg.") and campaign data (`ProductCampaignBuyXForY`, `ProductCampaignMixOffer`).
- **No EAN/GTIN** in known responses. **⚠ live:** check the product page (`/webapi/…/product`) for an EAN. Otherwise, v1 matches by name + quantity + brand. EAN matching is currently possible only for Bilka.

**Offers (observed live 2026-11-01):**
- `DiscountItem: true` without `Campaign` is nemlig's permanent "Discount" label (also present in `Labels`). It is not an offer.
- For `ProductCampaignDiscountPercent` and `ProductCampaignDiscount`, `Price` is the pre-discount price. The customer pays `CampaignPrice`; the unit price is `CampaignUnitPrice`.
- For `ProductCampaignMixOffer` and `ProductCampaignBuyXForY`, `Price` is the unit price. The offer is `MinQuantity` items for `TotalPrice`.
- Campaigns appear the evening before `IntervalStart`.

### Cart
- Read: `GET /webapi/basket/GetBasket`. The response is large (~1.5 KB per line plus analytics). Return selected fields only by default (kraenhansen: `BASKET_FIELDS`).
- Useful fields: `TotalPrice`, `DeliveryPrice`, `TotalBagsPrice`, `TotalDepositsPrice`, `MinimumOrderTotal`, `IsMinTotalValid`, `FormattedDeliveryTime`, `ValidationFailures`.
- Write: `POST /webapi/basket/AddToBasket` with `ProductId`, `quantity`. Quantity is **absolute**. 0 removes the line.
- Personal data in the cart: `InvoiceAddress`, `DeliveryAddress`, `CustomerName`, `MobileNumber`, `Email`, `DoorCode`, `UnattendedNotes`. These must be removed.

### Delivery slots
- Only `GET /webapi/Order/DeliverySpot` was initially known. It shows the **selected** slot (`TimeSlot`, `EditDeadline`) for the open order. It also contains `CustomerName` → privacy filter.
- Available slots (found 2026-11-02 in `main.js`, GET only): `GET /webapi/v2/Delivery/GetDeliveryDays?days=3&showForSubscriptions=false` (optional `startDate`; next page = response's `NextRangeStart`). Works without login, but then uses standard-zone prices. Logged-in users get household prices.
- Each slot in `DayRangeHours[].DayHours[]`: `Date` + `StartHour`/`EndHour` (local time), `DeliveryPrice` (DKK), `Type` (0 = Personlig, 1 = Fleksibel), `Availability` (0 available, 1 past deadline, 2 sold out, 3 inactive), `Deadline`. Confirmed examples: Fleksibel 14-21 = 25 DKK, Personlig Tue 14-21 = 19 DKK, Wed 14-21 = 0 DKK.
- The same class has `UpdateDeliveryTime` / `TryUpdateDeliveryTime` (POST, selects a slot). These must never be called; see `tests/test_safety.py`.
- `list_delivery_slots` displays the list. `get_delivery_info` displays the selected slot + `EditDeadline`. No slot selection.

### Recipes and meal plans (found 2026-11-06, GET only, no login)
- There is no recipe API. bilkatogo.dk is a static Nuxt build: each page has its data in `/_nuxt/static/<build>/inspiration/opskrifter/<path>/payload.js` (`__NUXT_JSONP__`, Magnolia CMS). It is a JavaScript object: keys are unquoted, and `void 0` represents undefined.
- The build ID changes on every deployment. It is present in each HTML page (`staticAssetsBase:"/_nuxt/static/<build>"`). An unknown page or old build returns HTTP 200 with the website HTML, not 404.
- A recipe has a title (Heading h1), facts ("4 personer", "35 minutter"; some say "16 stk." or "1 glas"), HTML text under h2 headings "Ingredienser" and "Fremgangsmåde" (or "Sådan gør du"), tips under "Alternativer og justeringer", and an image (`imageJCR` on digitalassets.sallinggroup.com).
- "Det skal du bruge": `CuratedSlider.sourceProperties.curatedSkuList`, Bilka product numbers (same as Algolia `objectID` and the cart). No quantity per product, no mapping from ingredient line → product number, and no "have at home" field. Some entries may be missing (`void 0`).
- Categories: `ContentCard.hierarchy` with its own subpages (or a subcategory, e.g. "Pita"), and `CarouselContentCard` under a heading. `aftensmad` has 15 subcategories; some have another level (`sandwiches-pita-og-wraps/sandwiches`).
- Meal plan: `/inspiration/opskrifter/madplaner/`: 7 dinners (Mon-Sun) in a carousel under "Madplan til uge NN", plus "Køb alt" (all product numbers) and a text shopping list.
- No search and no tags.

### Orders
Known in `nemlig_cli` (order history and order details). Contains address and name → privacy filter.

### Endpoints that must never be in the code
`/webapi/Order/PlaceOrderLoggedIn`, `/webapi/Checkout/GetCreditCards`, `/webapi/Checkout/GetCardsFees`. A test must grep for them.

## BilkaToGo

### Login and session
1. `POST https://accounts.eu1.gigya.com/accounts.login` (public `GIGYA_API_KEY`) with email/password → `login_token`.
2. `POST accounts.eu1.gigya.com/accounts.getJWT` → Gigya JWT.
3. `POST https://api.bilkatogo.dk/api/auth/LoginJWT` → Bilka session (cookies + JWT, 24 h).

- Renew 5 minutes before expiry (nickknissen: `JWT_RENEWAL_MARGIN`).
- Expired session: error body `{"code": 1, "msg": "Du er ikke logget ind"}`. **Only** this means "log in again". Other error bodies are genuine errors.
- **Trap:** `LoginJWT` replaces the account's cart with the cart behind the `cart` cookie. Never send a `cart` cookie when logging in.

### Search
Algolia, not Bilka's own API. App `F9VBJLR1BK`, index `prod_BILKATOGO_PRODUCTS`, public search key. Hardcode them.

Useful fields per hit: `objectID` (product number for the cart), `gtin` (**EAN is available**), `name`, `brand`, `netcontent`, `units`, `uom`, `unitOfMeasurePrice` (price per kg/l), `storeData[<sid>]` (price, offer, stock per store), `isInOffer`, `cpOfferPrice`, `is_multibuy`.

- Price and stock depend on store ID (`sid`). For home delivery, use `sid` from the cart's `deliveryAddress`.
- Use `objectID`, not `article`, as the product number (nickknissen v0.3.0 breaking change).

**Offers (observed live 2026-11-01, amounts in øre):**
- `storeData.<store>.price` is what the customer pays. Top-level `price` is the pre-offer price. `beforePrice` is always 0.
- `unitsOfMeasurePrice` is the unit price before the offer. `unitsOfMeasureShowPrice` is the unit price after the offer.
- `offerDescription` "Se Her" is a leaflet offer without its own text. "Skarp pris" is also an offer with a lower price.
- `offerFrom` and `offerUntil` are always empty. Therefore, we do not know when a Bilka offer ends, and an offer is counted even if it ends before delivery.
- Multi-buy offers across products ("Mix 10 for 25" on bananas and pears) are counted only within one line.

### Cart
- Read: `GET shop/v6/Cart?u=w&extra=deliveryAddress,deliveryDate`. Add `without_cache=1` when checking a change.
- Write: `GET shop/v6/ChangeLineCount?u=w&productId=<objectID>&count=<n>&fullCart=0`. Quantity is **absolute**. 0 removes the line.
- The response is HTTP 200 even for failures. A rejected update is indicated by an error body (e.g. `eid: 410`) **or** `productAmount` not matching the requested quantity. `message`/`offerLimitMessage` on an accepted update is a warning.
- Amounts are in **øre**.
- Fees in `stat`: `delivery_price`, `packing` (packing service, 3900 øre = 39 DKK in the fixture), `packaging_price`, `deposit`. "Papirpose" and "Pakkeservice" lines are fees, not products.
- Minimum order: `stat.minimum_left` (+ text) and `deliveryAddress.minimum_buy`.
- Home delivery: the cart has `hd_available` and `missing_hd_address`. The fixture is pickup (`hd_available: false`). **Confirmed live 2026-11-01:** `delivery_price` = 39 DKK + packing service 39 DKK, minimum order 500 DKK, `deliveryAddress` = home address (filtered).
- Switching to home delivery changes state (`SetDeliveryAddress`). v1 does **not** do this. If the cart is set to pickup, the tool clearly says: "Choose home delivery on bilkatogo.dk".

### Delivery slots
- Selected slot: cart's `deliveryDate` (`deliveryDate`, `intervalStart`, `intervalEnd`, `delivery_message`).
- Next available day: `deliveryAddress.nextAvailableDeliveryDate`. This is a read-only field.
- Available slots (found 2026-11-02, GET only): `GET https://api.bilkatogo.dk/api/shop/v3/AvailableDeliveryDates?u=w`, authenticated. Follows the cart's delivery address. `availableIntervals[].intervals[]`: `intervalStart`/`intervalEnd`, `closed` (1 = closed), `availability_bit` (1 = available, 2 = sold out), `orderBeforeTime`, `info[].message` (HTML). The day has `closed_text` ("sold out").
- No price per slot: the fee is fixed (`stat.delivery_price` in the cart, 39 DKK). Home delivery here is night-only (00-06, arrives between 20 and 06).
- `SetDeliveryDate` (selects a slot) must never be called; see `tests/test_safety.py`.

### Orders
`POST shop/v7/OrderHistory`, `GET shop/v7/OrderDetails`. Status codes (`Bestilt`, `Leveret`, `Faktureret`) are mapped to the website's wording (nickknissen ADR 0003). Orders can contain `home_delivery` and an address → privacy filter.

## REMA 1000 (from the Go port, 2026-11-07)

Source: [McAndersDK/handle-mcp-go](https://github.com/McAndersDK/handle-mcp-go) (MIT), `docs/research.md` and ADR 0008. GET requests only and one login. We have not called REMA 1000 ourselves yet; these points will be confirmed when we capture our own fixtures.

- **Network and API:** website `shop.rema1000.dk`, API `api.digital.rema1000.dk/api`. No queue or bot check observed. Headers: `Accept: application/json`, `X-Device: web`, `X-Locale: da`, `X-Timezone: Copenhagen/Europe`, and, when logged in, `Authorization: Bearer <access_token>`.
- **Delivery:** Køb & Hent pickup only, and only at some stores (`is_click_and_collect_active`). No home delivery.
- **Search (public):** `GET search/products?query=&per_page=&page=`, same when logged in and out. `prices` is a list of periods, current one first: `price`, `compare_unit`/`compare_unit_price`, `is_campaign`, `is_advertised`, `starting_at`, `ending_at`, `max_quantity`, `price_over_max_quantity`. `underline` is "size / brand". `barcodes` contains EAN. REMA writes `HK.` for minced.
- **Offers (public):** leaflet products use `search/products?filter[labels]=9001&sort=department` (label Avisvare). 285 products observed 2026-11-07.
- **Recipes (public):** `GET search/recipes?query=` and `GET rema1000dk/entities?filter[type]=recipe&filter[slug]=...`. Ingredients have `digitalProduct` with `amount` = packs for the recipe's own serving count.
- **Login:** REMA's own OAuth: `POST cphapp.rema1000.dk/api/oauth2/login` with email and password, followed by redirects that set cookies `access_token` (JWT, about 1 hour), `refresh_token`, `api_session`, `AWSALB`, `AWSALBCORS`. Renewal: `POST shop.rema1000.dk/api/auth/refresh`; without `api_session` and `AWSALB` it returns 500. `refresh_token` lifetime is unknown.
- **Stores and slots:** `GET v3/stores` (paginated) and `GET v3/stores/{id}/time-slots` (day → `{from, to, is_available}` in UTC, 2 hours per slot, no price). `v3/stores` works without login (443 stores, 5 pages of 100, 2026-11-09); `time-slots` returns 401 without login. A store has `id`, `name`, `postal_code`, `city`, `is_click_and_collect_active`, but also a phone number and merchant name; handle-mcp does not read those.
- **Shopping lists:** `GET v3/users/{id}/shopping-lists`, `GET v2/shoppinglists/{id}?with=items` (never `with=users`). Lines have `amount`, `bought`, `store_item_id` and `store_item.pricing` (`price`, `normal_price`, `max_quantity`, `price_over_max`, `deposit`). Writes follow the website SDK: `POST v2/shoppinglists`, `POST .../items`, `PATCH .../items/{id}`, `PATCH v2/shoppinglists/{id}` with `{items:[{id, delete:true}]}`. **Not tested live.** See ADR 0007.
- **Cart and payment:** a real cart is tied to a store, time and coupon, and has payment jobs. We never touch it (ADR 0001, 0007).
- **Unknowns:** no order endpoint, fee or minimum order was found in the known calls. Køb & Hent has a packing and packaging fee based on order size ([terms](https://rema1000.dk/information/handelsbetingelser-scan-selv), section 7.1).
- **Terms (read 2026-11-08):** a Køb & Hent order starts from the shopping list (section 3). No prohibition on automated access was found; REMA may block suspected misuse. `rema1000.dk/robots.txt` excludes `/api/*` and `/konto/*` (website paths, not `api.digital.rema1000.dk`).

## Implications for v1

1. **Product matching:** EAN is available only at Bilka. `compare_basket` matches by normalized name + quantity + unit, and uses EAN when both sides have one. Uncertain matches are flagged.
2. **Fees and minimum order** come from the store APIs where available. No configuration is needed except as a fallback.
3. **Delivery slots:** selected slot (both), next available day (Bilka), and the list of available slots with prices (`list_delivery_slots`). No slot selection.
4. **Privacy filter** must cover: `InvoiceAddress`, `DeliveryAddress`/`deliveryAddress` (for home delivery only), name, email and phone fields, `DoorCode`, `UnattendedNotes`, `uid`, `tid`, `CustomerName`.
5. **Forbidden-endpoint test:** grep for `PlaceOrder`, `Checkout/`, `SetDeliveryAddress`, `EmptyCart` in `src/`.

## Nutrition (2026-11-11)

- **BilkaToGo:** the Algolia index has `infos` with code `nutritional_100`: rows `{type: 2, title, value}`, first row "Næringsindhold pr." = "100 g", and two "Energi" rows ("233 kJ", "55 kcal"). It is also included in an ordinary search when `infos` is in `attributesToRetrieve`. Lookup by `objectID` works without login. `energyInfo` exists but is empty.
- **nemlig:** `GET productbff/api/web/productDetails/{id}` with the anonymous token: `product.detailDeclaration[].tableHeader` ("Næringsindhold pr. 100 ml") and `tableItems` ([label, value], "329 kJ / 78 Kcal"). One call per product. No EAN, either here or in search.
- **REMA 1000:** `GET api/v3/products/{id}?include=nutrition_info` without login, as used by the product page: `nutrition_info` = `[{name, value}]` ("Energi" = "318 KJ / 75 kcal", "Fedt" = "< 0,5", no unit). The table says "Pr. 100 g/ml"; the price's `compare_unit` (kg/ltr) identifies which. Search does not include it (`declaration` contains ingredients). Unknown ID: 404. The website also has `POST api/v3/batch`, which we do not use.
- **Fresh meat** (minced beef, flank steak) and bananas have no nutrition declaration in any of the stores. REMA does have values for minced white cabbage.
- **Open Food Facts** has the same numbers as Bilka for skyr (EAN 5711953011399), but lacks fresh meat. Not used: nemlig has no EAN, and Bilka has the values itself.

## Confirmed live (2026-11-01, home delivery selected)

- **BilkaToGo `Cart`:** `stat.delivery_price` = 39 DKK (delivery) + 39 DKK (packing service), minimum order 500 DKK. Home delivery is indicated by `deliveryAddress.dawa_uid` / `recipient_name`.
- **nemlig:** the delivery fee depends on the selected slot (19-51 DKK). Read from the website's time picker, not an API field. Slots can be viewed without login.
- **Privacy:** the entire `deliveryAddress` block is removed by `privacy.scrub()`, so `dawa_uid` and `recipient_name` never reach the model.

## Open questions

If you find the answer in DevTools → Network (Fetch/XHR), copy only the URL and response fields, never cookies, and add an edited response to `tests/fixtures/`.

1. **nemlig:** is an EAN/GTIN available on the product page (`/webapi/…/product`)? The search response does not include it.
2. **nemlig:** what does the API return for an outdated `Version` header? (Currently, `HANDLE_NEMLIG_APP_VERSION` can be set as an environment variable.)

Until then, v1 matches nemlig products by name + quantity instead of EAN.
