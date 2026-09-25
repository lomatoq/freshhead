"""Opt-in synthetic catalog for exercising UX offline; never sent as real inventory."""
from .catalog import enrich
from .models import Product, Variant

DEMO_ITEMS = [
    ('top-olive', 'Olive cropped knit polo', 'top', ['knit'], 'cropped'),
    ('bottom-black', 'Washed black wide-leg denim', 'bottom', ['denim'], 'wide'),
    ('shoe-brown', 'Brown suede chunky runner', 'footwear', ['suede'], 'unknown'),
    ('top-cream', 'Ecru oversized graphic tee', 'top', ['graphic'], 'oversized'),
    ('bottom-blue', 'Blue balloon carpenter jeans', 'bottom', ['denim', 'workwear'], 'wide'),
    ('shoe-black', 'Black leather lug sole boots', 'footwear', ['leather'], 'unknown'),
    ('top-red', 'Burgundy cropped cardigan', 'top', ['knit'], 'cropped'),
    ('bottom-beige', 'Sand double-knee straight pants', 'bottom', ['workwear'], 'regular'),
    ('shoe-white', 'Cream low-profile suede sneaker', 'footwear', ['suede'], 'unknown'),
    ('top-black', 'Black boxy striped knit', 'top', ['knit', 'graphic'], 'oversized'),
    ('bottom-olive', 'Olive loose cargo pants', 'bottom', ['workwear'], 'wide'),
    ('shoe-blue', 'Blue and grey technical runner', 'footwear', ['sport'], 'unknown'),
    ('top-blue', 'Blue oversized denim shirt', 'top', ['denim'], 'oversized'),
    ('top-beige', 'Beige regular cotton tee', 'top', [], 'regular'),
    ('bottom-brown', 'Brown wide corduroy trousers', 'bottom', [], 'wide'),
    ('shoe-cream', 'Cream slender low-profile sneaker', 'footwear', [], 'unknown'),
    ('outer-olive', 'Olive cropped canvas jacket', 'outerwear', ['workwear'], 'cropped'),
    ('outer-black', 'Black oversized leather jacket', 'outerwear', ['leather'], 'oversized'),
    ('top-white', 'White fitted cotton tee', 'top', [], 'slim'),
    ('bottom-grey', 'Grey straight pleated trousers', 'bottom', [], 'regular'),
    ('shoe-red', 'Burgundy leather penny loafer', 'footwear', ['leather'], 'unknown'),
    ('top-pink', 'Pink oversized mohair sweater', 'top', ['knit', 'cool'], 'oversized'),
    ('top-grey', 'Grey boxy fleece sweatshirt', 'top', ['cool'], 'oversized'),
    ('bottom-cream', 'Cream wide canvas carpenter pants', 'bottom', ['workwear'], 'wide'),
]


def seed_demo(db):
    count = 0
    for i, (slug, title, category, tags, fit) in enumerate(DEMO_ITEMS):
        p = Product(store='demo', url=f'https://example.com/freshhead-demo/{slug}',
                    title=title, brand='FRESHHEAD CONCEPT', category=category, tags=tags, fit=fit,
                    image=f'/static/demo/{slug}.svg', demo=True, price=150 + (i % 6)*70,
                    currency='PLN', available=True, gender='unisex', extraction='synthetic-demo',
                    description='Вымышленная вещь для проверки приложения. Иллюстрация не изображает товар магазина. Не продаётся.',
                    variants=[Variant(size=x, available=True) for x in (['EU 42', 'EU 43'] if category == 'footwear' else ['M', 'L'])])
        p = enrich(p)
        p.category = category  # "denim shirt" in synthetic data is a known top
        p.fit = fit
        count += db.upsert(p)
    return count
