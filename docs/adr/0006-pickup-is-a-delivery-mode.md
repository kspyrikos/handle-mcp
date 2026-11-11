# 0006: Pickup is a delivery mode

A BilkaToGo cart can be set to pickup in a Bilka store. handle-mcp reads pickup carts and their pickup times. It never chooses a store, a time or a mode.

## Modes

`delivery_mode` is `"home"`, `"pickup"` or `null` on `get_cart`, both parts of `get_delivery_info` and each store in `compare_basket`. `pickup_store` is the store's name for pickup, else `null`. nemlig is always `"home"`.

BilkaToGo decides from the cart's `deliveryAddress`, in one place (`_mode()` in `stores/bilkatogo.py`). It fails closed:

- **home:** `dawa_uid` or `recipient_name` is set.
- **pickup:** no home marker, a `sid`, and `name` starts with `Bilka`.
- **unknown (`null`):** anything else. The answer says `BilkaToGo does not indicate whether the cart is set to home delivery or pickup. Choose on bilkatogo.dk.` and `list_delivery_slots` refuses.

On a home cart `name` is the customer's name. So `name` is read only when nothing says home, and only a name that starts with `Bilka` becomes `pickup_store`.

## Answers

- A pickup cart has no warning. `home_delivery` stays `false`.
- The pickup fee is the cart's `delivery_price`. `0` is a known fee (`"0.00"`), not a missing one.
- `list_delivery_slots` lists pickup times with `kind: "Pickup"`.
- `compare_basket` adds `Pickup at <store>: you collect the products yourself.` The trip to the store is not priced.

## Rule 1 still holds

Choosing a pickup store, a pickup time or the mode counts as changing the address (ADR 0001). No code does it. The human chooses on bilkatogo.dk.

## Why

Many families pick up at Bilka. Before, a pickup cart only got "Choose home delivery", which is wrong advice for them. Idea and facts from the Go port [McAndersDK/handle-mcp-go](https://github.com/McAndersDK/handle-mcp-go) (MIT).

## Not proven live

The pickup cart shape is a real capture (`cart_logged_in.json`). The pickup slot list is assumed to have the same shape as the home one: the one live capture we know of had no pickup intervals.
