"""Explicit public storefronts; no guessed inventory or prices.

Regions are pinned when the shop supports them. A registered source is not a
claim that automated access is available: the last scan result is shown in UI.
"""
NEW_IDS = ['prm', 'bstn', 'asics', 'footshop', 'adidas', 'reebok',
           'nanostudio', 'camperlab', 'aelfric', 'wss', 'oluolin', 'kiikio']


def extra_stores(Store):
    return {
        'prm': Store('prm', 'PRM', 'prm.com',
            ('https://prm.com/pl/on', 'https://prm.com/pl/on/obuwie', 'https://prm.com/pl/on/odziez'),
            r'/p/[^/?]+', 'Польская витрина: обувь, дизайнерский streetwear и одежда.'),
        'bstn': Store('bstn', 'BSTN', 'bstn.com',
            ('https://www.bstn.com/eu_en/men/new-arrivals/footwear',
             'https://www.bstn.com/eu_en/men/new-arrivals/apparel'),
            r'/(?:p|r)/[^/?]+', 'EU-витрина: sneakers, верх и брюки. Цена остаётся в исходной валюте.'),
        'asics': Store('asics', 'ASICS', 'asics.com',
            ('https://www.asics.com/pl/pl-pl/men-sportstyle-shoes/c/as10200000/',),
            r'/p/[A-Za-z0-9._-]+\.html', 'Польская витрина ASICS SportStyle. При запрете доступа сбор останавливается.', 'ASICS'),
        'footshop': Store('footshop', 'Footshop', 'footshop.pl',
            ('https://www.footshop.pl/pl/5-buty-meskie', 'https://www.footshop.pl/pl/2-odziez-meska'),
            r'/\d+-[^/?]+\.html', 'Польский мультибренд: обувь и streetwear.'),
        'adidas': Store('adidas', 'adidas', 'adidas.pl',
            ('https://www.adidas.pl/mezczyzni-originals-buty', 'https://www.adidas.pl/mezczyzni-originals-odziez'),
            r'/[A-Z0-9]{6,10}\.html', 'Польская витрина Originals. Блокировка сайта отображается, не обходится.', 'adidas'),
        'reebok': Store('reebok', 'Reebok', 'reebok.eu',
            ('https://www.reebok.eu/collections/mens-new-arrivals', 'https://www.reebok.eu/'),
            r'/products/[^/?]+', 'EU-витрина: Club C, Classic Leather, кроссовки и одежда.', 'Reebok'),
        'nanostudio': Store('nanostudio', 'Nanostudio', 'nanostudio-official.com',
            ('https://nanostudio-official.com/en-po', 'https://nanostudio-official.com/en-po/collections/knit'),
            r'/products/[^/?]+', 'Корейские силуэты, объёмные брюки и ботинки. Регион Poland / English.', 'Nanostudio'),
        'camperlab': Store('camperlab', 'CAMPERLAB', 'camperlab.com',
            ('https://www.camperlab.com/en_PL/men/shoes/all_shoes_lab_men', 'https://www.camperlab.com/en_PL/men/all'),
            r'/[^/?]+-[A-Za-z]*\d{4,}[A-Za-z0-9]*-\d{2,}',
            'Польская витрина: выразительная обувь, ботинки и ready-to-wear.', 'CAMPERLAB'),
        'aelfric': Store('aelfric', 'Aelfric Eden', 'aelfriceden.com',
            ('https://www.aelfriceden.com/collections/new-in', 'https://www.aelfriceden.com/collections/jeans'),
            r'/products/[^/?]+', 'Графика, необычный трикотаж и свободный деним. Валюта самой витрины.', 'Aelfric Eden'),
        'wss': Store('wss', 'Warsaw Sneaker Store', 'warsawsneakerstore.com',
            ('https://warsawsneakerstore.com/menu/obuwie/meskie', 'https://warsawsneakerstore.com/'),
            r'^/[^/?]+\.html$', 'Польский sneaker/streetwear-магазин: кроссовки и одежда.'),
        'oluolin': Store('oluolin', 'OLUOLIN', 'oluolin.com',
            ('https://oluolin.com/en-eu/collections/new-arrival', 'https://oluolin.com/en-eu/collections/pants-jeans'),
            r'/products/[^/?]+', 'EU-витрина: baggy, многослойность, графика и утилитарные детали.', 'OLUOLIN'),
        'kiikio': Store('kiikio', 'KIIKIO', 'kiikio.com',
            ('https://www.kiikio.com/collections/new-in', 'https://www.kiikio.com/collections/best-sellings'),
            r'/products/[^/?]+', 'Выразительный деним, washed-фактуры, объём и тёмный streetwear.', 'KIIKIO'),
    }
