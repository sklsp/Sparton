"""Tests for app.research.extraction: structured-first page extraction.

``extract_page`` is the public surface; every test feeds it a fixed HTML string
and asserts literal values, so any change to what the parser keeps, drops or
falls back on shows up as a failure here. The JSON-LD path is primary (method
"json-ld"), the OpenGraph fallback covers sites without JSON-LD (method
"opengraph"), and link plus metadata handling applies to both.

No network: everything is an in-memory string, same convention as the other
research tests.
"""

from __future__ import annotations

from app.research.extraction import extract_page

BASE = "https://shop.example/p/acme-widget-3000"


def page(body: str, head: str = "") -> str:
    """A minimal HTML document around the given body and head fragments."""
    return f"<html><head>{head}</head><body>{body}</body></html>"


# --- json-ld path ------------------------------------------------------------------


def test_json_ld_product_keeps_every_field():
    html = page(
        "<p>Buy now</p>",
        """<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "Acme Widget 3000",
  "brand": {"@type": "Brand", "name": "Acme"},
  "category": "Tools > Widgets",
  "sku": "SKU-1",
  "mpn": "MPN-2",
  "image": "/img/widget.png",
  "url": "/p/acme-widget-3000",
  "description": "A sturdy widget.",
  "aggregateRating": {"@type": "AggregateRating", "reviewCount": 42},
  "offers": {
    "@type": "Offer",
    "price": "19.99",
    "priceCurrency": "EUR",
    "availability": "https://schema.org/InStock"
  }
}
</script>""",
    )
    products, _, meta = extract_page(html, BASE)

    assert len(products) == 1
    p = products[0]
    assert p.name == "Acme Widget 3000"
    assert p.brand == "Acme"
    assert p.category == "Tools > Widgets"
    assert p.price == 19.99
    assert p.currency == "EUR"
    assert p.availability == "instock"
    assert p.description == "A sturdy widget."
    assert p.image_url == "https://shop.example/img/widget.png"
    assert p.url == BASE
    assert p.review_count == 42
    assert p.attributes == {"sku": "SKU-1", "mpn": "MPN-2"}
    assert p.method == "json-ld"
    assert p.confidence == 0.95
    assert meta["title"] == ""


def test_json_ld_graph_list_and_numeric_prices():
    # Products nested in a list value, and prices given as numbers instead of
    # strings: both must come back as floats. The full schema.org URL form of
    # @type is accepted too.
    html = page(
        "",
        """<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {"@type": "Product", "name": "Int Price", "offers": {"price": 25}},
    {"@type": "http://schema.org/Product", "name": "Float Price", "offers": {"price": 12.75}}
  ]
}
</script>""",
    )
    products, _, _ = extract_page(html, BASE)

    assert [p.name for p in products] == ["Int Price", "Float Price"]
    assert products[0].price == 25.0
    assert isinstance(products[0].price, float)
    assert products[1].price == 12.75


def test_broken_json_ld_script_is_skipped_not_fatal():
    html = page(
        "",
        '<script type="application/ld+json">{"@type": "Product", "name": oops</script>'
        '<script type="application/ld+json">{"@type": "Product", "name": "Survivor"}</script>',
    )
    products, _, _ = extract_page(html, BASE)

    assert [p.name for p in products] == ["Survivor"]


def test_only_broken_json_ld_gives_no_products():
    html = page("", '<script type="application/ld+json">{not json at all</script>')
    products, _, _ = extract_page(html, BASE)

    assert products == []


def test_list_valued_fields_take_the_first_entry():
    html = page(
        "",
        """<script type="application/ld+json">
{
  "@type": "Product",
  "name": ["Acme Widget", "Legacy Name"],
  "brand": [{"@type": "Brand", "name": "Acme"}],
  "image": ["/img/a.png", "/img/b.png"],
  "category": []
}
</script>""",
    )
    products, _, _ = extract_page(html, BASE)

    p = products[0]
    assert p.name == "Acme Widget"
    assert p.brand == "Acme"
    assert p.image_url == "https://shop.example/img/a.png"
    assert p.category == ""


def test_missing_fields_fall_back_to_defaults():
    html = page("", '<script type="application/ld+json">{"@type": "Product", "name": "Bare"}</script>')
    products, _, _ = extract_page(html, BASE)

    p = products[0]
    assert p.name == "Bare"
    assert p.brand == ""
    assert p.category == ""
    assert p.price is None
    assert p.currency == ""
    assert p.availability == "unknown"
    assert p.description == ""
    # urljoin of an empty relative URL is the page URL itself.
    assert p.image_url == BASE
    assert p.url == BASE
    assert p.review_count is None
    assert p.attributes == {"sku": None, "mpn": None}


def test_odd_price_text_is_parsed_as_the_first_number_run():
    # The parser takes the first run of digits with at most one separator group
    # and treats both "," and "." as a decimal point. That is the ported Ares
    # behaviour, pinned here on purpose: "EUR 19,90" reads as 19.90, "$1,299"
    # reads as 1.299 (a comma is not a thousands separator), and text without
    # digits gives no price at all.
    html = page(
        "",
        """<script type="application/ld+json">
{"@graph": [
  {"@type": "Product", "name": "Comma Decimal", "offers": {"price": "EUR 19,90"}},
  {"@type": "Product", "name": "Dollar Comma", "offers": {"price": "$1,299"}},
  {"@type": "Product", "name": "No Digits", "offers": {"price": "on request"}}
]}
</script>""",
    )
    products, _, _ = extract_page(html, BASE)

    by_name = {p.name: p for p in products}
    assert by_name["Comma Decimal"].price == 19.90
    assert by_name["Dollar Comma"].price == 1.299
    assert by_name["No Digits"].price is None


# --- opengraph fallback --------------------------------------------------------------


def test_opengraph_fallback_when_no_json_ld():
    html = page(
        "<p>Just a listing</p>",
        """<title>Fallback Mug</title>
<meta property="og:type" content="product">
<meta property="og:title" content="Fallback Mug">
<meta property="og:description" content="A mug.">
<meta property="og:image" content="/img/mug.png">
<meta property="product:price:amount" content="14.50">
<meta property="product:price:currency" content="USD">""",
    )
    products, _, meta = extract_page(html, BASE)

    assert len(products) == 1
    p = products[0]
    assert p.name == "Fallback Mug"
    assert p.description == "A mug."
    assert p.price == 14.5
    assert p.currency == "USD"
    assert p.availability == "unknown"
    assert p.image_url == "https://shop.example/img/mug.png"
    assert p.url == BASE
    assert p.method == "opengraph"
    assert p.confidence == 0.65
    assert meta["title"] == "Fallback Mug"


def test_listing_page_without_price_is_not_a_product():
    # The fallback signal must be a PRICE, not the word "product": every listing
    # page contains "product" in its URLs, and matching on that turned category
    # pages into phantom products named after the site.
    html = page(
        '<p>See our product range</p><a href="/products/widget">product</a>',
        '<title>All Products</title>'
        '<meta property="og:type" content="website">'
        '<meta name="description" content="Catalogue">',
    )
    products, links, meta = extract_page(html, BASE)

    assert products == []
    assert links == ["https://shop.example/products/widget"]
    assert meta["title"] == "All Products"
    assert meta["description"] == "Catalogue"


def test_og_type_product_without_price_is_not_a_product():
    html = page("", '<title>Some Product</title><meta property="og:type" content="product">')
    products, _, _ = extract_page(html, BASE)

    assert products == []


# --- links and metadata ----------------------------------------------------------------


def test_whitespace_only_text_is_ignored():
    # Whitespace-only data nodes produce no title text, no link and no body
    # text: an anchor whose label is only spaces must not become a link.
    html = page(
        '<p>   \n\t </p><a href="/ghost">   </a><a href="/real">Real</a>',
        "<title>Padded  Title</title>",
    )
    products, links, meta = extract_page(html, BASE)

    assert products == []
    assert links == ["https://shop.example/real"]
    # Inner runs of whitespace collapse to single spaces.
    assert meta["title"] == "Padded Title"


def test_links_are_filtered_and_deduplicated():
    html = page(
        '<a href="javascript:void(0)">js</a>'
        '<a href="mailto:a@b.example">mail</a>'
        '<a href="#top">anchor</a>'
        '<a href="/real">one</a>'
        '<a href="/real">two</a>'
        '<a href="https://other.example/x">ext</a>',
    )
    _, links, _ = extract_page(html, BASE)

    assert links == [
        "https://shop.example/real",
        "https://other.example/x",
    ]

