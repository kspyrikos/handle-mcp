# Build your own skill on top

The server finds and counts. Your skill or agent picks. Taste, favourites, budget and organic-product rules belong in your skill, never in this server.

`compare_basket` gives each list line up to 5 `candidates`. The first has `suggested: true`: the server's guess by word rules, nothing more. When the search found an organic product, one candidate is organic.

| Field | Meaning |
|---|---|
| `id` | Product id. Use it in `set_cart_quantities` |
| `name`, `brand`, `size`, `category` | As the store shows them, e.g. category `Kolonial > Konserves` |
| `quantity` | Packs needed for the amount on the list |
| `line_total` | Cost for that amount, offers included. Compare on this |
| `price`, `unit_price`, `unit` | Price per pack, and per kg, l or stk |
| `offer_text` | The store's offer, e.g. `2 for 30` |
| `name_match` | `full`, `partial` or `none` (a guess: no word on the list matched) |
| `size_fits` | `false`: the packs give too much or too little. Left out when sizes cannot be compared |
| `prepared` | Cold cuts, smoked, breaded |
| `processed` | `frost` or `konserves` |
| `blend` | A mix, e.g. minced meat with vegetables |
| `oeko` | Organic |
| `note` | Why the line is unsure, in English |

Flags that are false and facts that are unknown are left out, to keep the answer small. `sure: false` on a line means: look, or ask the user. When no candidate fits, call `search_products` with other words.
