# Tools

| Tool | Writes? | Stores | What |
|---|---|---|---|
| `search_products(query, stores?)` | no | nemlig, BilkaToGo, REMA 1000 | Price, price per kg/l, offers, EAN when known. nemlig and BilkaToGo need a login; REMA 1000 does not |
| `search_recipes(query, stores?)` | no | nemlig | The store's own recipes: name and link. `get_recipe` gives time, persons and products. BilkaToGo has no recipe search: use `list_recipes` with `category` |
| `list_recipes(stores?, tags?, category?, limit?)` | no | nemlig, BilkaToGo | This week's featured recipes (nemlig "Ugens udvalgte"), or with `tags` all recipes that have every tag, e.g. `["Familievenlig", "Budgetvenlig"]` or `["Under 30 min", "Kylling"]`. `category`: aftensmad (default), frokost, madpakke, ... An unknown tag answers with nemlig's list. BilkaToGo has no tags: without `category` it is this week's "Madplan" (7 dinners, Monday first), with `category` every recipe in that part of bilkatogo.dk/inspiration/opskrifter (`aftensmad`, `aftensmad/pastaretter`, `boern-i-koekkenet` ...), mixed across sub-categories. An unknown category answers with BilkaToGo's list |
| `get_recipe(store, recipe, persons?)` | no | nemlig, BilkaToGo | The products behind the store's "Se varer og læg i kurv": per ingredient the product, packs for `persons`, price and alternatives. `have_at_home` items (salt, oil) are left out of `total`. Also the dish's photo (`image`), `description`, `method` (sections with steps) and `tip`. The `ingredients` text is for `ingredients_persons`, not `persons`. Fill the cart with `add_recipe_to_cart` after a yes. BilkaToGo: one pack of each product on the page's "Det skal du bruge" (no amounts, no alternatives, no `have_at_home`); `persons` is not used, the recipe's own persons come back (`null` when written for pieces, e.g. "16 stk."); `missing` lists product ids not sold at your address; prices need a login |
| `list_offers(stores?, limit?)` | no | nemlig, BilkaToGo, REMA 1000 | This week's offers, also items you would not search for: nemlig Skarp pris, Priskup, Dagstilbud, Undgå madspild, gifts; BilkaToGo offers; REMA 1000's leaflet (Avisvarer), no login, a max per customer in `offer_text`. Free gifts have price 0 and a max per order. nemlig needs no login (with one, the offers fit your delivery time); BilkaToGo does, because the store follows your address |
| `get_nutrition(store, product_ids)` | no | nemlig, BilkaToGo, REMA 1000 | Nutrition per 100 g or 100 ml from the store's label: `energy_kj`, `energy_kcal`, `fat`, `saturated_fat`, `carbohydrate`, `sugar`, `protein`, `salt`, `fibre`. Values are strings as on the label (`"0.13"`, `"<0.5"`), a missing line is `null` ([ADR 0010](adr/0010-nutrition-values-are-label-strings.md)). No label (often fresh meat, fruit and vegetables): `nutrition: null` with a `reason`. Up to 50 ids from `search_products`, `list_offers` or `compare_basket`; duplicates are removed, order kept. One failing product gets its error as `reason`, the others still answer. No login needed. REMA 1000 writes "pr. 100 g/ml": `per` follows the product's price unit (kg → `100 g`, l → `100 ml`), else `null` |
| `compare_basket(items, stores?)` | no | nemlig, BilkaToGo, REMA 1000 | Prices a shopping list in each store: up to 5 candidates per item with facts and flags, fees, minimum order, unsure and missing items marked, cheapest store. REMA 1000 is pickup or in the store, and its packing fee is not known: a note says so |
| `get_cart(store?)` | no | nemlig, BilkaToGo, REMA 1000 | Cart lines, fees, total, minimum order, `url` (the cart page with UTM tags). Without `store`: every store's cart, keyed by store name. REMA 1000: the shopping list "handle-mcp" in REMA's app ([ADR 0007](adr/0007-rema-cart-is-a-shopping-list.md)); lines ticked off in the app are left out, `pant` is a fee, the packing fee is not known |
| `set_cart_quantities(store, items)` | **yes** | nemlig, BilkaToGo, REMA 1000 | Absolute quantities (0 removes). Only the cart. Returns the new cart with `url`. REMA 1000: only the list "handle-mcp", made on the first write and never primary; lines ticked off in the app are removed |
| `add_recipe_to_cart(store, recipe, persons?, skip?, swap?, include?)` | **yes** | nemlig, BilkaToGo | A recipe's products from `get_recipe` into the cart, added to what is there (twice adds twice). Ask first. `have_at_home` items only with `include`; `skip` and `swap` (to an alternative's id) by ingredient or product id. Answers before/added/after per product and what was skipped (sold out, have at home, left out) |
| `get_delivery_info(store)` | no | nemlig, BilkaToGo, REMA 1000 | Chosen delivery time, next free day, fees, `delivery_mode` (`home`, `pickup` or `null`) and `pickup_store`. BilkaToGo can be pickup, see [ADR 0006](adr/0006-pickup-is-a-delivery-mode.md). REMA 1000 is always `pickup`; with a login and a chosen store, `pickup_store` and the next free day |
| `list_delivery_slots(stores?, days?)` | no | nemlig, BilkaToGo, REMA 1000 | Free delivery times with the fee per slot, the 3 cheapest first. BilkaToGo pickup carts: pickup times, `kind: "Pickup"`. REMA 1000: Køb & Hent times in the first store you chose at `handle-mcp login rema1000` that has Køb & Hent, `kind: "Køb & Hent"`, `price: null` (REMA does not say) |
| `get_orders(store)` / `get_order(store, id)` | no | nemlig, BilkaToGo | Past orders, for "what we usually buy" |

Money is a string in kroner with two decimals (`"12.95"`, `"39.00"`) in every store. Dates are ISO. A fact the store does not give is `null`, never left out.

Every product from `search_products` and `list_offers` has the same fields. Two of them are often `null`:

| Field | nemlig | BilkaToGo | REMA 1000 |
|---|---|---|---|
| `before_price` (the price without the offer) | when nemlig shows a crossed-out price; a "Skarp pris" without one has `null` | when the offer has a normal price | the price after the offer week |
| `offer_until` (last day of the offer, ISO date) | from `search_products`; nemlig's offer lists (`list_offers`) do not say, so `null` | always `null`: BilkaToGo does not say | always, from the price period |

`compare_basket` candidates list only the facts that are known, to stay short.

What it can **never** do: place an order, pay, read cards, choose a delivery time or change the address. That code does not exist, and a test fails if it shows up. See [ADR 0001](adr/0001-the-human-always-pays.md).


Fields in each `compare_basket` candidate: [skills.md](skills.md).
