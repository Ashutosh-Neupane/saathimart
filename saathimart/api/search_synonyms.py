"""Query expansion for storefront search — Nepali↔English synonyms.

The storefront's customers search in the language they speak: "chiya",
"dudh", "chamal". The catalog is (mostly) English: "Tea Leaves", "Fresh
Milk", "Jeera Masino Rice". A plain LIKE matches none of them.

expand_terms() turns one query into an ordered OR-list of LIKE terms:
the original query first (exact wins), then expansions of the whole
query, then expansions of individual tokens — so "chiya" gains "tea"
and "dudh pani" still hits products whose names contain "milk".

The map is deliberately small and curated; every entry is a real Nepali
grocery term with an unambiguous English counterpart, symmetric so
English queries can find Nepali-named products too (rare in this
catalog, free to support).
"""

# word → list of alternatives (never includes the word itself)
_ALTERNATES = {
    # beverages
    "chiya": ["tea"],
    "tea": ["chiya"],
    "dudh": ["milk"],
    "milk": ["dudh"],
    "coffee": ["kafi"],
    "kafi": ["coffee"],
    "juice": ["ras"],
    "ras": ["juice"],
    "pani": ["water"],
    "water": ["pani"],
    # staples
    "chamal": ["rice"],
    "rice": ["chamal"],
    "dal": ["lentil", "pulses"],
    "lentil": ["dal"],
    "atta": ["flour"],
    "flour": ["atta"],
    "chini": ["sugar"],
    "sugar": ["chini"],
    "nun": ["salt"],
    "salt": ["nun"],
    "tel": ["oil"],
    "oil": ["tel"],
    "gheu": ["ghee"],
    "ghee": ["gheu"],
    # fresh & packaged food
    "kera": ["banana"],
    "banana": ["kera"],
    "syau": ["apple"],
    "apple": ["syau"],
    "khursani": ["chili", "chilli"],
    "chili": ["khursani"],
    "anda": ["egg"],
    "egg": ["anda"],
    "masu": ["meat", "chicken"],
    "meat": ["masu"],
    "tarkari": ["vegetable"],
    "vegetable": ["tarkari"],
    "sabun": ["soap"],
    "soap": ["sabun"],
    # transliterated/brand-adjacent food words the catalog names in English
    "chowmein": ["noodles", "noodle"],
    "chow mein": ["noodles", "noodle"],
    "noodles": ["chowmein"],
    "noodle": ["chowmein"],
    "bhujiya": ["snacks"],
    "momo": ["dumpling"],
    "sukuti": ["dried fish"],
}


def expand_terms(query: str) -> list:
    """Ordered LIKE-term list for a query: [query, whole-phrase expansions,
    per-token expansions]. Deduped, non-empty, original first."""
    q = (query or "").strip()
    if not q:
        return []
    terms = [q]
    lowered = q.lower()

    def _alts(word):
        alts = _ALTERNATES.get(word.lower())
        return alts or []

    # whole-phrase expansion ("green chiya" → whole phrase has no entry,
    # but a phrase like "chow mein" does)
    for alt in _alts(lowered):
        terms.append(alt)

    # per-token expansion ("dudh pani" → milk, water)
    for token in lowered.split():
        for alt in _alts(token):
            if alt not in terms:
                terms.append(alt)

    # dedupe, preserve order, drop empties
    seen, out = set(), []
    for t in terms:
        t = t.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def or_like_filters(fields, terms):
    """Frappe or_filters covering every term across every field.

    fields: e.g. ["product_name", "tags", "short_description"]
    terms:  expand_terms() output.
    """
    filters = []
    for term in terms:
        for f in fields:
            filters.append([f, "like", f"%{term}%"])
    return filters


def sql_like_clause(fields, terms):
    """Raw-SQL variant of or_like_filters: returns (clause_sql, params).

    clause_sql is a parenthesized OR-group with one %s placeholder per
    (field, term) pair; params is the matching %-pattern list.
    """
    likes, params = [], []
    for term in terms:
        for f in fields:
            likes.append(f"{f} LIKE %s")
            params.append(f"%{term}%")
    if not likes:
        return "1=1", []
    return "(" + " OR ".join(likes) + ")", params


def fuzzy_matches(query, names, threshold=0.75, limit=20):
    """Typo fallback: names whose similarity to the query (or any token)
    clears `threshold`, best first. Pure Python — no DB dependency —
    so it runs identically everywhere; callers only invoke it when the
    exact/synonym passes returned nothing."""
    from difflib import SequenceMatcher

    q = (query or "").strip().lower()
    if not q or not names:
        return []
    tokens = q.split()

    def best_ratio(name):
        n = (name or "").lower()
        whole = SequenceMatcher(None, q, n).ratio()
        word = max(
            (SequenceMatcher(None, t, w).ratio()
             for t in tokens for w in n.split()),
            default=0.0,
        )
        return max(whole, word)

    scored = [(best_ratio(n), n) for n in names]
    scored = [s for s in scored if s[0] >= threshold]
    scored.sort(key=lambda s: -s[0])
    return [n for _, n in scored[:limit]]
