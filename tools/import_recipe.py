#!/usr/bin/env python3
"""
Fetch a recipe from a URL and save it as a Dinner Plan JSON file.

Most recipe sites (AllRecipes, NYT Cooking, Serious Eats, Food Network,
Bon Appétit, etc.) embed schema.org/Recipe data as JSON-LD. This script
reads that, parses ingredients into amount/unit/name, categorizes them,
and drops the result into recipes/.

Usage:
  python3 tools/import_recipe.py <URL>
  python3 tools/import_recipe.py <URL> <slug>   # override the filename

Examples:
  python3 tools/import_recipe.py https://www.allrecipes.com/recipe/8728/
  python3 tools/import_recipe.py https://www.allrecipes.com/recipe/8728/ beef-stew

After running, add the slug to recipes/index.json so the app picks it up.
No external dependencies — uses only the Python standard library.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser


# ── Units (longest first so regex prefers "tablespoon" over "table") ──────────
UNITS = sorted([
    "tablespoon", "tablespoons", "tbsp", "tbs",
    "teaspoon",   "teaspoons",   "tsp",
    "cup",        "cups",
    "fluid ounce","fluid ounces","fl oz",
    "ounce",      "ounces",      "oz",
    "pound",      "pounds",      "lb",  "lbs",
    "kilogram",   "kilograms",   "kg",
    "gram",       "grams",       "g",
    "milliliter", "milliliters", "ml",
    "liter",      "liters",      "l",
    "quart",      "quarts",      "qt",
    "pint",       "pints",       "pt",
    "gallon",     "gallons",
    "bunch",      "bunches",
    "clove",      "cloves",
    "sprig",      "sprigs",
    "slice",      "slices",
    "can",        "cans",
    "package",    "packages",    "pkg",
    "head",       "heads",
    "stalk",      "stalks",
    "inch",       "inches",
    "large",      "medium",      "small",
    "pinch",      "dash",
], key=len, reverse=True)

# ── Category keyword map ───────────────────────────────────────────────────────
PRODUCE_KW = {
    "onion", "shallot", "leek", "scallion", "green onion", "chive",
    "garlic", "ginger", "tomato", "pepper", "jalapeño", "serrano", "habanero",
    "carrot", "celery", "lettuce", "spinach", "kale", "chard", "arugula",
    "cabbage", "broccoli", "cauliflower", "brussels", "fennel",
    "cucumber", "zucchini", "squash", "pumpkin", "butternut",
    "potato", "sweet potato", "yam", "turnip", "parsnip",
    "lemon", "lime", "orange", "grapefruit", "mandarin",
    "apple", "pear", "peach", "plum", "apricot", "mango", "papaya",
    "banana", "strawberry", "blueberry", "raspberry", "blackberry",
    "avocado", "mushroom", "asparagus", "artichoke", "beet",
    "eggplant", "radish", "snap pea", "edamame", "corn", "okra",
    "pineapple", "cherry", "grape", "pomegranate",
    "chili", "chile", "guajillo", "ancho", "pasilla",
    "shallot", "bok choy", "daikon", "tomatillo",
}
MEAT_KW = {
    "chicken", "turkey", "duck", "game hen", "quail",
    "beef", "steak", "brisket", "chuck", "short rib", "ground beef",
    "pork", "bacon", "pancetta", "guanciale", "prosciutto", "ham", "lard",
    "lamb", "mutton", "veal", "rabbit",
    "salmon", "tuna", "cod", "halibut", "tilapia", "mahi",
    "shrimp", "prawn", "scallop", "lobster", "crab", "clam", "mussel",
    "anchovy", "sardine", "mackerel", "bass", "snapper", "fish",
    "sausage", "chorizo", "pepperoni", "salami", "mortadella",
    "ground meat", "minced", "filet", "fillet",
}
DAIRY_KW = {
    "milk", "cream", "half and half", "heavy cream", "whipping cream",
    "butter", "ghee", "margarine",
    "cheese", "parmesan", "parmigiano", "pecorino", "mozzarella",
    "feta", "cheddar", "gruyere", "brie", "gouda", "ricotta", "cottage cheese",
    "cream cheese", "goat cheese", "cotija", "queso",
    "yogurt", "sour cream", "crème fraîche", "buttermilk",
    "egg", "eggs",
}
HERB_KW = {
    "basil", "oregano", "thyme", "rosemary", "parsley", "cilantro",
    "dill", "mint", "sage", "tarragon", "chives", "bay leaf", "bay leaves",
    "cumin", "paprika", "smoked paprika", "turmeric", "coriander",
    "chili powder", "cayenne", "red pepper flake", "crushed red pepper",
    "cinnamon", "nutmeg", "cardamom", "cloves", "allspice", "star anise",
    "black pepper", "white pepper", "pepper", "salt", "sea salt",
    "kosher salt", "fleur de sel", "flake salt",
    "curry", "garam masala", "za'atar", "sumac", "harissa", "ras el hanout",
    "saffron", "vanilla", "anise", "fennel seed", "celery seed",
    "mustard seed", "caraway", "sesame seed", "poppy seed",
    "garlic powder", "onion powder", "smoked salt",
}


def categorize(name: str) -> str:
    n = name.lower()
    for kw in MEAT_KW:
        if kw in n:
            return "meat"
    for kw in DAIRY_KW:
        if kw in n:
            return "dairy"
    for kw in HERB_KW:
        if kw in n:
            return "herbs"
    for kw in PRODUCE_KW:
        if kw in n:
            return "produce"
    return "pantry"


# ── Unicode fraction normalisation ────────────────────────────────────────────
_FRACTIONS = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4",
              "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8"}


def norm_fractions(s: str) -> str:
    for uc, f in _FRACTIONS.items():
        s = s.replace(uc, f)
    return s


# ── Ingredient string → {name, amount, unit} ──────────────────────────────────
_UNIT_PAT = "(?:" + "|".join(re.escape(u) for u in UNITS) + ")"
_NUM_PAT = r"(\d+(?:\s+\d+/\d+|\.\d+|/\d+)?)"
_ING_RE  = re.compile(
    rf"^{_NUM_PAT}\s+{_UNIT_PAT}s?\.?\s+(.+)$",
    re.IGNORECASE,
)
_ING_NO_UNIT_RE = re.compile(rf"^{_NUM_PAT}\s+(.+)$", re.IGNORECASE)


def parse_ingredient(raw: str) -> dict:
    text = norm_fractions(re.sub(r"\s+", " ", raw.strip()))

    # "to taste", "as needed" etc. → no amount
    if re.match(r"^(salt|pepper|salt and pepper)", text, re.I):
        name = re.sub(r"\s*(,.*|to taste.*|as needed.*)$", "", text, flags=re.I).strip()
        return {"name": name, "amount": "", "unit": "to taste", "category": "herbs"}

    m = _ING_RE.match(text)
    if m:
        amount, name = m.group(1).strip(), m.group(2).strip()
        # extract unit from between amount and name
        mid = text[len(amount):len(text) - len(name)].strip().rstrip(".")
        unit = mid if mid else ""
        name = re.sub(r"\s*\([^)]*\)", "", name).strip()
        return {"name": _cap(name), "amount": amount, "unit": unit, "category": categorize(name)}

    m = _ING_NO_UNIT_RE.match(text)
    if m:
        amount, name = m.group(1).strip(), m.group(2).strip()
        name = re.sub(r"\s*\([^)]*\)", "", name).strip()
        return {"name": _cap(name), "amount": amount, "unit": "", "category": categorize(name)}

    name = re.sub(r"\s*\([^)]*\)", "", text).strip()
    return {"name": _cap(name), "amount": "", "unit": "", "category": categorize(name)}


def _cap(s: str) -> str:
    return s[0].upper() + s[1:] if s else s


# ── JSON-LD extraction via a simple HTMLParser ─────────────────────────────────
class _LDParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._in = False
        self._buf = []
        self.results = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self._in = True
            self._buf = []

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._in:
            self._in = False
            try:
                self.results.append(json.loads("".join(self._buf)))
            except json.JSONDecodeError:
                pass


def _find_recipe(obj):
    """Recursively find the first @type:Recipe object in JSON-LD."""
    if isinstance(obj, dict):
        t = obj.get("@type", "")
        types = t if isinstance(t, list) else [t]
        if "Recipe" in types:
            return obj
        for v in obj.values():
            r = _find_recipe(v)
            if r:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _find_recipe(item)
            if r:
                return r
    return None


# ── Duration ───────────────────────────────────────────────────────────────────
def _parse_duration(iso: str) -> str:
    if not iso:
        return ""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", iso)
    if not m:
        return ""
    h, mn = m.group(1), m.group(2)
    parts = []
    if h:  parts.append(f"{h} hr")
    if mn: parts.append(f"{mn} min")
    return " ".join(parts)


# ── Slug ───────────────────────────────────────────────────────────────────────
def slugify(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", name.lower())
    s = re.sub(r"[\s_]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    url  = sys.argv[1]
    slug = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"Fetching {url} …")
    req = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    parser = _LDParser()
    parser.feed(html)
    schema = _find_recipe(parser.results)

    if not schema:
        print(
            "\nNo Recipe schema found on that page.\n"
            "The site may require JavaScript or block scrapers.\n"
            "Sites known to work: AllRecipes, Serious Eats, NYT Cooking,\n"
            "Bon Appétit, Food Network, The Kitchn, Simply Recipes.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Extract fields ─────────────────────────────────────────────────────────
    name = schema.get("name", "Untitled Recipe").strip()

    desc = schema.get("description", "")
    desc = re.sub(r"<[^>]+>", "", desc).strip()          # strip HTML
    desc = re.sub(r"\s+", " ", desc)
    if len(desc) > 200:
        desc = desc[:197] + "…"

    yield_raw = schema.get("recipeYield", "")
    if isinstance(yield_raw, list):
        yield_raw = yield_raw[0] if yield_raw else ""
    m = re.search(r"\d+", str(yield_raw))
    servings = int(m.group()) if m else 4

    total_time = (
        _parse_duration(schema.get("totalTime"))
        or _parse_duration(schema.get("cookTime"))
        or _parse_duration(schema.get("prepTime"))
        or ""
    )

    keywords = schema.get("keywords", "") or ""
    if isinstance(keywords, list):
        keywords = ", ".join(keywords)
    tags = [t.strip().lower() for t in re.split(r"[,;]", keywords) if t.strip()][:6]

    raw_ings = schema.get("recipeIngredient", [])
    ingredients = [parse_ingredient(s) for s in raw_ings if s.strip()]

    if not slug:
        slug = slugify(name)

    recipe = {
        "name":        name,
        "description": desc,
        "servings":    servings,
        "prepTime":    total_time,
        "tags":        tags,
        "ingredients": ingredients,
    }

    # ── Save recipe JSON ───────────────────────────────────────────────────────
    recipes_dir = os.path.join(os.path.dirname(__file__), "..", "recipes")
    out_path    = os.path.normpath(os.path.join(recipes_dir, f"{slug}.json"))

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(recipe, f, indent=2, ensure_ascii=False)

    # ── Update index.json ──────────────────────────────────────────────────────
    index_path = os.path.normpath(os.path.join(recipes_dir, "index.json"))
    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    if slug not in index:
        index.append(slug)
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, ensure_ascii=False)
        index_note = "added to index.json ✓"
    else:
        index_note = "already in index.json"

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n✓  recipes/{slug}.json  ({index_note})")
    print(f"   Name:        {name}")
    print(f"   Servings:    {servings}")
    print(f"   Time:        {total_time or '(not found)'}")
    print(f"   Tags:        {', '.join(tags) or '(none)'}")
    print(f"   Ingredients: {len(ingredients)}")
    if ingredients:
        for ing in ingredients[:4]:
            amt = " ".join(filter(None, [ing["amount"], ing["unit"]]))
            print(f"      {ing['name']}  {amt}  [{ing['category']}]")
        if len(ingredients) > 4:
            print(f"      … and {len(ingredients) - 4} more")


if __name__ == "__main__":
    main()
