"""Known Pokémon TCG: 30th Celebration products and search phrases.

Discovery adds later Target and Best Buy listings whose titles match this set.
Pokémon Center's exclusive Elite Trainer Box is not sold on Target.
"""

SEED_PRODUCTS = [
    {
        "retailer": "target",
        "sku": "1010892076",
        "name": "Pokémon TCG: 30th Celebration Elite Trainer Box",
        "url": "https://www.target.com/p/-/A-1010892076",
    },
    {
        "retailer": "target",
        "sku": "1010892067",
        "name": "Pokémon TCG: 30th Celebration Poster Collection",
        "url": "https://www.target.com/p/-/A-1010892067",
    },
    {
        "retailer": "target",
        "sku": "1010892070",
        "name": "Pokémon TCG: 30th Celebration Knock Out Collection",
        "url": "https://www.target.com/p/-/A-1010892070",
    },
    {
        "retailer": "target",
        "sku": "1010892078",
        "name": "Pokémon TCG: 30th Celebration Tech Sticker Collection",
        "url": "https://www.target.com/p/-/A-1010892078",
    },
    {
        "retailer": "target",
        "sku": "1010892069",
        "name": "Pokémon TCG: 30th Celebration Tin (Sylveon or Greninja)",
        "url": "https://www.target.com/p/-/A-1010892069",
    },
    {
        "retailer": "target",
        "sku": "1011407490",
        "name": "Pokémon TCG: 30th Celebration Booster Bundle Box",
        "url": "https://www.target.com/p/-/A-1011407490",
    },
    {
        "retailer": "target",
        "sku": "1012422107",
        "name": "Pokémon TCG: 30th Celebration Mini Tin",
        "url": "https://www.target.com/p/-/A-1012422107",
    },
    {
        "retailer": "bestbuy",
        "sku": "6685559",
        "name": "Pokémon TCG: 30th Celebration Elite Trainer Box",
        "url": "https://www.bestbuy.com/site/searchpage.jsp?id=pcat17071&st=sku%3A6685559",
    },
]

DISCOVERY_QUERIES = [
    "pokemon 30th celebration elite trainer box",
    "pokemon 30th celebration poster collection",
    "pokemon 30th celebration knock out collection",
    "pokemon 30th celebration tech sticker",
    "pokemon 30th celebration booster bundle",
    "pokemon 30th celebration mini tin",
    "pokemon 30th celebration battle deck",
    "pokemon 30th celebration ultra premium",
    "pokemon 30th celebration ditto",
    "pokemon 30th celebration binder collection",
    "pokemon 30th celebration figure collection",
    "pokemon 30th celebration blister",
    "pokemon 30th celebration ex box",
]


def is_30th_celebration(title: str) -> bool:
    text = title.lower().replace("é", "e")
    return "30th" in text and "celebration" in text
