"""BilkaToGo recipes: the pages under bilkatogo.dk/inspiration/opskrifter/.

No recipe API: the website is a static Nuxt build, and every page has its data in
/_nuxt/static/<build>/<path>/payload.js (Magnolia CMS). The build id changes on each deploy,
so it is read from the website's HTML and read again when a payload stops answering.
A recipe has its text as HTML and "Det skal du bruge": a list of Bilka product ids
(`curatedSkuList`), one pack each. No amounts per product, no persons to scale, no search.
Found 2026-11-06, see tests/fixtures/bilkatogo/README.md. Read only.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from decimal import Decimal
from itertools import zip_longest
from typing import Any

import httpx

from handle_mcp.models import Recipe, RecipeDetails, RecipeItem, RecipeProduct, RecipeSection
from handle_mcp.stores import StoreError

SITE = "https://www.bilkatogo.dk"
ROOT = "/inspiration/opskrifter/"
IMAGES = (
    "https://digitalassets.sallinggroup.com/image/upload/w_1000,dpr_1,c_fill,q_auto,f_auto/"
    "t_DigitalAiLogo/external_content_providers/magnolia/next/"
)
BUILD = re.compile(r"/_nuxt/static/(\d+)")
SLUG = re.compile(r"^[a-z0-9-]+(/[a-z0-9-]+)*$")
IDENT = re.compile(r"[A-Za-z_$][\w$]*")
NUMBER = re.compile(r"\d+")
FEATURED = "madplaner"  # "Ugens madplan": 7 dinners, Monday to Sunday
METHOD = ("Fremgangsm", "Sådan gør du")  # the heading above the steps
MAX_DEPTH = 2
PARALLEL = 5  # pages read at once: be polite to the website
SKU = re.compile(r"^\d+$")


class NoPage(StoreError):
    """bilkatogo.dk has no page at that path."""  # aftensmad → sandwiches-pita-og-wraps → sandwiches → recipes


def recipe_path(recipe: str) -> str:
    """'https://www.bilkatogo.dk/inspiration/opskrifter/aftensmad/pizza/flammkuchen/?x=1',
    '/inspiration/opskrifter/aftensmad/pizza/flammkuchen' or 'aftensmad/pizza/flammkuchen'
    → 'aftensmad/pizza/flammkuchen'."""
    path = recipe.strip().split("?")[0].split("#")[0].lower()
    path = path.split("bilkatogo.dk", 1)[-1].strip("/")
    path = path.removeprefix(ROOT.strip("/")).strip("/")
    if not path or not SLUG.match(path):
        raise StoreError(f"Unknown recipe {recipe!r}. Use the link from bilkatogo.dk/inspiration.")
    return path


def category_path(category: str) -> str:
    """'Aftensmad', 'aftensmad/danske-retter' or 'børn i køkkenet' → BilkaToGo's path."""
    path = category.strip().lower().replace(" ", "-")
    for a, b in (("æ", "ae"), ("ø", "oe"), ("å", "aa")):
        path = path.replace(a, b)
    return recipe_path(path)


def js_object(src: str) -> dict:
    """payload.js is `__NUXT_JSONP__("/path", {data: ...})`: a JavaScript object, not JSON.
    Keys have no quotes and undefined is `void 0`. Strings are already JSON strings."""
    try:
        s = src[src.index("{") : src.rindex("}") + 1]
    except ValueError:
        raise StoreError("BilkaToGo page format has changed.") from None
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == '"':
            j = i + 1
            while s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            out.append(s[i : j + 1])
            i = j + 1
        elif s.startswith("void 0", i):
            out.append("null")
            i += 6
        elif m := IDENT.match(s, i):
            word, i = m.group(), m.end()
            out.append(word if word in ("true", "false", "null") else json.dumps(word))
        else:
            out.append(c)
            i += 1
    try:
        data = json.loads("".join(out))
    except ValueError:
        raise StoreError("BilkaToGo page format has changed.") from None
    if not isinstance(data, dict):
        raise StoreError("BilkaToGo page format has changed.")
    return data


def _nodes(node: Any, *kinds: str) -> list[dict]:
    """Every Magnolia component of these kinds, in page order."""
    found = []
    if isinstance(node, dict):
        if (node.get("componentType") or "").removeprefix("MagnoliaComponents") in kinds:
            found.append(node)
        for value in node.values():
            found += _nodes(value, *kinds)
    elif isinstance(node, list):
        for value in node:
            found += _nodes(value, *kinds)
    return found


def _text(fragment: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _own_path(page: dict) -> str:
    return (page.get("path") or "").removeprefix("/bilka-to-go").rstrip("/") + "/"


def _cards(page: dict) -> list[tuple[str, str, str | None]]:
    """The pages a category shows, as (path, name, group), in page order. Two kinds: the
    grids of its own or a sub-category's pages ("Pita"), and carousels under a heading
    ("Lav aftensmad med børnene"). A recipe's "Lignende opskrifter" is its parent's grid."""
    own, group, found = _own_path(page), None, {}
    for node in _nodes(page, "Heading", "ContentCard", "CarouselContentCard"):
        kind = node["componentType"].removeprefix("MagnoliaComponents")
        if kind == "Heading":
            group = (node.get("header") or "").strip() or group
        elif kind == "ContentCard" and ((node.get("hierarchy") or {}).get("path") or "").startswith(
            own
        ):
            grid = node["hierarchy"]  # its own sub-pages, or a sub-category's ("Pita")
            for child in grid.get("children") or []:
                found.setdefault(child.get("path") or "", (_name(child), _name(grid)))
        elif kind == "CarouselContentCard":
            for card in node.get("contentCards") or []:
                target = card.get("page") if isinstance(card.get("page"), dict) else {}
                name = (card.get("header") or _name(target)).strip()
                found.setdefault(target.get("path") or "", (name, group))
    return [
        (path.removeprefix(ROOT).strip("/"), name, grp)
        for path, (name, grp) in found.items()
        if path.startswith(ROOT) and path != ROOT
    ]


def _is_recipe(page: dict) -> bool:
    return any(k.startswith("Ingrediens") for k in _sections(page))


def _name(page: dict) -> str:
    return (page.get("displayName") or page.get("title") or "").strip()


def _recipe(store: str, path: str, name: str, group: str | None) -> Recipe:
    rid = path.removeprefix(ROOT).strip("/")  # 'aftensmad/pizza/flammkuchen'
    return Recipe(store=store, id=rid, name=name, url=f"{SITE}{ROOT}{rid}/", group=group)


def _sections(page: dict) -> dict[str, list[str]]:
    """The recipe's text by heading: 'Ingredienser', 'Fremgangsmåde', ... → lines."""
    sections: dict[str, list[str]] = {"": []}
    heading = ""
    for para in _nodes(page, "Paragraph"):
        for part in re.split(r"(<h2>.*?</h2>)", para.get("htmlContent") or "", flags=re.DOTALL):
            if part.startswith("<h2>"):
                heading = _text(part)
                sections.setdefault(heading, [])
                continue
            if re.search(r"<li", part):
                lines = re.findall(r"<li[^>]*>(.*?)</li>", part, flags=re.DOTALL)
            else:  # <p> lines split by <br />; a <strong> line is a sub-heading, <em> a note
                lines = [
                    ln
                    for p in re.findall(r"<p>(.*?)</p>", part, flags=re.DOTALL)
                    if not p.lstrip().startswith("<em>")
                    for ln in re.split(r"<br\s*/?>", p)
                    if not re.fullmatch(r"\s*<strong>.*</strong>\s*", ln, flags=re.DOTALL)
                ]
            sections[heading] += [t for ln in lines if (t := _text(ln))]
    return sections


def _facts(page: dict) -> tuple[int | None, str | None]:
    """'4 personer' and '35 minutter' from the row of facts under the title."""
    persons = minutes = None
    for usp in _nodes(page, "FrontpageUsps"):
        for el in usp.get("contentElements") or []:
            title = (el.get("title") or "").strip().lower()
            if title.endswith("personer") and (n := NUMBER.search(title)):
                persons = int(n.group())
            elif "minut" in title or "time" in title:  # "35 minutter", "2,5 timer"
                minutes = title.replace("minutter", "min").replace("minut", "min")
    return persons, minutes


def _image(page: dict) -> str | None:
    banner = next(iter(_nodes(page, "Banner")), {})
    jcr = banner.get("imageJCR") or ""
    return f"{IMAGES}{jcr}" if jcr.startswith("jcr:") else None


class BilkaRecipesMixin:
    """Recipe methods for BilkaStore. Pages need no login; prices do (the address's store)."""

    name: str
    _send: Any
    _algolia: Any
    _build: str | None = None
    _pages_at_once: asyncio.Semaphore | None = None

    async def _page(self, path: str) -> dict:
        """The page's data. A stale build id answers with the website's HTML: read it again."""
        if not getattr(self, "_pages_at_once", None):
            self._pages_at_once = asyncio.Semaphore(PARALLEL)
        async with self._pages_at_once:
            return await self._payload(path)

    async def _payload(self, path: str) -> dict:
        for fresh in (False, True):
            if fresh or not self._build:
                await self._read_build()
            url = f"{SITE}/_nuxt/static/{self._build}{(ROOT + path).rstrip('/')}/payload.js"
            resp: httpx.Response = await self._send("GET", url)
            if resp.status_code == 200 and resp.text.startswith("__NUXT_JSONP__"):
                page = ((js_object(resp.text).get("data") or [{}])[0] or {}).get("page")
                if not isinstance(page, dict):
                    raise StoreError("BilkaToGo page format has changed.")
                return page
        raise NoPage(f"BilkaToGo has no page at {ROOT}{path}/.")

    async def _read_build(self) -> None:
        resp = await self._send("GET", f"{SITE}{ROOT}")
        if resp.status_code != 200 or not (m := BUILD.search(resp.text)):
            raise StoreError("BilkaToGo recipe data format has changed: no build ID was found.")
        self._build = m.group(1)

    async def recipes(self, query: str, limit: int = 5) -> list[Recipe]:
        raise StoreError(
            "BilkaToGo does not support recipe search. Use list_recipes with category, "
            "fx aftensmad eller aftensmad/danske-retter."
        )

    async def featured_recipes(self, limit: int = 20) -> list[Recipe]:
        """This week's "Madplan": 7 dinners, Monday to Sunday, in that order."""
        page = await self._page(FEATURED)
        found = [_recipe(self.name, path, name, group) for path, name, group in _cards(page)]
        if not found:
            raise StoreError("No recipes were found in the BilkaToGo meal plan.")
        return found[:limit]

    async def recipes_by_tag(
        self, tags: list[str], category: str | None = None, limit: int = 20
    ) -> list[Recipe]:
        """All recipes in one of BilkaToGo's categories, e.g. "aftensmad" or
        "aftensmad/danske-retter". A category with sub-categories gives their recipes, mixed."""
        if tags:
            raise StoreError(
                "BilkaToGo does not support tags. Use category, e.g. aftensmad, aftensmad/pastaretter, "
                "boern-i-koekkenet eller nemme."
            )
        path = category_path(category or "aftensmad")
        try:
            groups = await self._collect(path, MAX_DEPTH, {})
        except NoPage:
            top = [c[0] for c in _cards(await self._page(""))]
            raise StoreError(
                f"BilkaToGo does not recognize category {category!r}. Try: {', '.join(top)}."
            ) from None
        mixed = [r for row in zip_longest(*groups) for r in row if r]  # one from each, in turn
        if not mixed:
            raise StoreError(f"No recipes were found in BilkaToGo category {category!r}.")
        return mixed[:limit]

    async def _collect(self, path: str, depth: int, seen: dict[str, dict]) -> list[list[Recipe]]:
        """Recipes per group below `path`. The first card shows if the cards are recipes or
        sub-categories (aftensmad → pizza → recipes)."""
        page = seen.get(path) or await self._page(path)
        cards = _cards(page)
        if not cards:
            return []
        seen[cards[0][0]] = await self._page(cards[0][0])
        if depth == 0 or _is_recipe(seen[cards[0][0]]):
            groups: dict[str | None, list[Recipe]] = {}
            for sub, name, group in cards:
                groups.setdefault(group, []).append(_recipe(self.name, sub, name, group))
            return list(groups.values())
        found = await asyncio.gather(*(self._collect(c[0], depth - 1, seen) for c in cards))
        return [group for groups in found for group in groups]

    async def recipe(self, recipe_id: str, persons: int | None = None) -> RecipeDetails:
        """One recipe and its products, one pack each. `persons` is not used: BilkaToGo's
        recipes are written for a fixed number of persons and have no amounts per product."""
        path = recipe_path(recipe_id)
        page = await self._page(path)
        sections = _sections(page)
        ingredients = next((v for k, v in sections.items() if k.startswith("Ingrediens")), [])
        skus = [
            str(s)
            for slider in _nodes(page, "CuratedSlider")
            for s in (slider.get("sourceProperties") or {}).get("curatedSkuList") or []
            if isinstance(s, str) and SKU.match(s)  # item numbers only: they go in a filter
        ]
        if not ingredients and not skus:
            raise StoreError(f"{ROOT}{path}/ is not a BilkaToGo recipe.")
        skus = list(dict.fromkeys(skus))
        products = {p.id: p for p in await self._algolia("", len(skus), ids=skus)} if skus else {}
        if skus and not products:  # the lookup by item number itself failed, or nothing is sold
            raise StoreError("BilkaToGo did not find any of the recipe products at your store.")
        items = [
            RecipeItem(
                ingredient=p.name,
                product=RecipeProduct(
                    id=p.id,
                    name=p.name,
                    price=p.price,
                    quantity=1,
                    size=p.size,
                    available=p.available,
                    offer_text=p.offer_text,
                ),
            )
            for sku in skus
            if (p := products.get(sku))
        ]
        own, minutes = _facts(page)
        heading = next((h.get("header") for h in _nodes(page, "Heading")), None)
        method = next((v for k, v in sections.items() if k.startswith(METHOD)), [])
        tips = next((v for k, v in sections.items() if k.startswith("Alternativer")), [])
        return RecipeDetails(
            store=self.name,
            id=path,
            name=(heading or _name(page)).strip(),
            url=f"{SITE}{ROOT}{path}/",
            persons=own,  # None: written for "6 stk" or "1 glas", not persons
            time=minutes,
            description=" ".join(sections[""]) or None,
            image=_image(page),
            ingredients=ingredients,
            ingredients_persons=own,
            method=[RecipeSection(title=None, steps=method)] if method else [],
            tip=" ".join(tips) or None,
            items=items,
            total=sum((i.product.price for i in items), Decimal(0)),
            missing=[s for s in skus if s not in products],
        )
