import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from freshhead.app import create_app
from freshhead.catalog import (CrawlError, PublicClient, Robots, STORES, availability,
                              enrich, extract, money, price_text, store_for)
from freshhead.db import DB
from freshhead.demo import seed_demo
from freshhead.models import Product, Settings, Variant, canonical, product_id
from freshhead.service import Service, send_telegram
from freshhead.style import Ranker, allowed, build_outfits, compatibility


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path)


def product(title='Black wide jeans', **kw):
    return enrich(Product(store='supersklep', url='https://supersklep.pl/i123-black-jeans', title=title, **kw))


@pytest.mark.parametrize('raw,want', [('1 249,90 zł',1249.9),('129,90',129.9),('1,249.90',1249.9),
                                    ('1.249,90',1249.9),('',None),('bad',None),(-1,None),(0,0)])
def test_money(raw,want):
    assert money(raw) == want


def test_model_number_not_price():
    assert price_text('Buty New Balance 9060') == (None,'')
    assert price_text('Old 999,90 PLN Now 499,90 PLN') == (499.9,'PLN')


@pytest.mark.parametrize('url', ['http://supersklep.pl','https://127.0.0.1', 'file:///etc/passwd',
    'https://supersklep.pl.evil.test/x','https://evil.test@supersklep.pl/x','https://supersklep.pl:444/x',
    'https://supersklep.pl@evil.test/x'])
def test_external_urls_rejected(url):
    with pytest.raises(ValueError):
        store_for(url)


def test_robots_patterns():
    rb=Robots('User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: */filters/*\nDisallow: /cart*\nAllow: /cart-public$\nCrawl-delay: 4')
    assert rb.allowed('https://emestudios.com/pl/en/shop')
    assert not rb.allowed('https://emestudios.com/api/products')
    assert not rb.allowed('https://supersklep.pl/x/filters/male')
    assert rb.allowed('https://supersklep.pl/cart-public')
    assert not rb.allowed('https://supersklep.pl/cart-public/more')
    assert rb.delay==4


def test_specific_agent_rules():
    rb=Robots('User-agent: *\nAllow: /\nUser-agent: Freshhead\nDisallow: /')
    assert not rb.allowed('https://supersklep.pl/spodnie')


def test_percent_encoded_disallow():
    assert not Robots('User-agent: *\nDisallow: /api/').allowed('https://emestudios.com/%61pi/products')


def test_ld_extract():
    data={'@context':'https://schema.org','@type':'Product','name':'Black wide jeans',
          'url':'https://supersklep.pl/i123-black-jeans','brand':{'name':'Example'},
          'image':['https://supersklep.pl/image.jpg'], 'offers':[
              {'@type':'Offer','price':'299.90','priceCurrency':'PLN','size':'M','availability':'https://schema.org/OutOfStock'},
              {'@type':'Offer','price':'349.90','priceCurrency':'PLN','size':'L','availability':'https://schema.org/InStock'}]}
    ps,_=extract('<script type="application/ld+json">'+json.dumps(data)+'</script>',data['url'],STORES['supersklep'],True)
    p=ps[0]
    assert p.category=='bottom' and p.fit=='wide'
    assert p.price==349.9 and p.currency=='PLN' and p.available is True
    assert p.variants[0].available is False
    assert p.brand=='Example'
    assert p.colors==['black']


def test_catalog_cards():
    markup='''<article class="product"><a href="/i128-buty"><img alt="Buty New Balance 9060" src="https://supersklep.pl/shoe.webp"></a>
    <h3>Buty New Balance 9060</h3><span>849,90 PLN</span></article>'''
    ps,links=extract(markup,'https://supersklep.pl/buty',STORES['supersklep'])
    assert len(ps)==1 and ps[0].price==849.9
    assert ps[0].shoe_mass=='heavy'
    assert ps[0].available is None and ps[0].variants==[]
    assert links==['https://supersklep.pl/i128-buty']


def test_bad_json_does_not_kill_extract():
    ps,_=extract('<script type="application/ld+json">oops</script>','https://supersklep.pl/',STORES['supersklep'])
    assert ps==[]


def test_id_and_first_seen_stable(db):
    p=product();p.first_seen='2024-01-01T00:00:00+00:00'
    assert db.upsert(p)
    db.rate(p.id,1)
    p2=product(price=90,currency='PLN')
    assert not db.upsert(p2)
    assert len(db.products())==1
    assert db.product(p.id).first_seen==p.first_seen
    assert db.ratings()[p.id]==1
    db.rate(p.id,None)
    assert not db.ratings()


def test_budget_and_stock():
    s=Settings(budgets={'PLN':300,'EUR':100})
    assert not allowed(product(price=350,currency='PLN'),s)
    assert allowed(product(price=90,currency='EUR'),s)
    assert not allowed(product(price=90,currency='GBP'),s)
    assert not allowed(product(available=False),s)
    assert allowed(product(price=None),s)


def test_size_requirements():
    s=Settings(sizes={'bottom':['M']},strict_sizes=True)
    assert not allowed(product(),s)
    assert not allowed(product(variants=[Variant(size='M',available=False)]),s)
    assert allowed(product(variants=[Variant(size='M',available=True)]),s)
    assert not allowed(product(variants=[Variant(size='L',available=True)]),s)


def test_gender_kids():
    assert enrich(product(title='Buty New Balance 9060 JR')).gender=='kids'
    assert not allowed(enrich(product(title='Buty New Balance 9060 JR')),Settings())
    assert not allowed(enrich(product(title='Bluza Volcom Wmn')),Settings(gender='men'))


def test_taste_responds_to_feedback(db):
    seed_demo(db)
    ps=db.products()
    liked=next(p for p in ps if p.title=='Washed black wide-leg denim')
    similar=next(p for p in ps if p.title=='Blue balloon carpenter jeans')
    baseline=Ranker(ps,{}).taste(similar)[0]
    assert Ranker(ps,{liked.id:1}).taste(similar)[0]>baseline
    assert Ranker(ps,{liked.id:-1}).taste(similar)[0]<baseline


def test_demo_and_real_taste_separate(db):
    seed_demo(db);p=product();db.upsert(p)
    ratings={x.id:1 for x in db.products() if x.demo}
    assert Ranker(db.products(),ratings).taste(p)[0]==48


def test_outfits_and_distinct_modes(db):
    seed_demo(db)
    data=build_outfits(db.products(),{},Settings(),mode='contrast')
    assert len(data['outfits'])>=2
    for look in data['outfits']:
        assert {p['category'] for p in look['items']}=={'top','bottom','footwear'}
        assert len({p['id'] for p in look['items']})==3
        assert look['reasons']
    ids=[{p['id'] for p in x['items']} for x in data['outfits']]
    assert all(len(a&b)<2 for i,a in enumerate(ids) for b in ids[i+1:])


def test_anchor_locked(db):
    seed_demo(db);ps=db.products();a=next(p for p in ps if p.category=='bottom')
    result=build_outfits(ps,{},Settings(),a.id)
    assert result['outfits']
    assert all(a.id in [p['id'] for p in x['items']] for x in result['outfits'])


def test_disliked_never_enters_outfit(db):
    seed_demo(db);ps=db.products();shoes=[p for p in ps if p.category=='footwear']
    result=build_outfits(ps,{p.id:-1 for p in shoes},Settings())
    assert result['outfits']==[] and 'footwear' in result['missing']


def test_demo_cleanup(db):
    seed_demo(db);p=db.products()[0];db.rate(p.id,1)
    db.clear_demo();assert not db.products() and not db.ratings()


def test_dedup_ratings_state_api(db):
    with TestClient(create_app(db)) as c:
        h={'X-Freshhead':'1'}
        assert c.get('/health').json()=={'ok':True}
        assert c.post('/api/demo').status_code==403
        assert c.post('/api/demo',headers=h).status_code==200
        p=c.get('/api/products').json()['items'][0]
        assert c.post(f"/api/products/{p['id']}/rating",json={'value':1},headers=h).status_code==200
        assert c.get('/api/state').json()['counts']['liked']==1
        assert c.post(f"/api/products/{p['id']}/rating",json={'value':100},headers=h).status_code==422
        assert c.post('/api/demo',headers={**h,'Origin':'https://evil.example'}).status_code==403
        result=c.post('/api/outfits',json={'mode':'balanced'},headers=h).json()['outfits'][0]
        response=c.post('/api/outfits/save',json={'outfit_id':result['id'],'mode':'balanced'},headers=h)
        assert response.status_code==200,response.text
        assert len(c.get('/api/state').json()['saved_outfits'])==1
        assert c.get('/').status_code==200
        assert c.get('/static/app.js').status_code==200


def test_daily_digest_excludes_demo(db):
    seed_demo(db)
    body=asyncio.run(Service(db).make_digest())
    assert body['items']==[] and body['outfits']==[]
    assert body['status']=='empty'


def test_telegram_missing_credentials(db,monkeypatch):
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN',raising=False)
    monkeypatch.delenv('TELEGRAM_CHAT_ID',raising=False)
    result=asyncio.run(send_telegram(db,{'items':[]}))
    assert not result['sent']


def test_telegram_marks_only_success(db,monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','testtoken')
    monkeypatch.setenv('TELEGRAM_CHAT_ID','123')
    p=product(price=199,currency='PLN');db.upsert(p)
    original=httpx.AsyncClient
    def handler(request):
        return httpx.Response(403,json={'ok':False})
    monkeypatch.setattr('freshhead.service.httpx.AsyncClient',lambda **kw: original(transport=httpx.MockTransport(handler),**kw))
    result=asyncio.run(send_telegram(db,{'day':'2026-09-25','items':[p.model_dump()]}))
    assert not result['sent'] and not db.sent()


def test_http_does_not_follow_unapproved_redirect():
    async def run():
        client=PublicClient(STORES['supersklep'])
        client.robots=Robots('User-agent: *\nAllow: /')
        await client.client.aclose()
        client.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(302,headers={'location':'http://127.0.0.1/secret'})))
        with pytest.raises(CrawlError):
            await client.html('https://supersklep.pl/spodnie')
        await client.client.aclose()
    asyncio.run(run())


def test_browser_not_triggered_on_403(db,monkeypatch):
    calls=[]
    class Stub:
        requests=0
        def __init__(self,*a):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def html(self,url):raise CrawlError('HTTP 403','blocked')
        async def render(self,url):calls.append(url)
    monkeypatch.setattr('freshhead.service.PublicClient',Stub)
    s=db.settings();s.browser_fallback=True;db.set('settings',s.model_dump())
    asyncio.run(Service(db).scan_store('eme'))
    assert not calls and db.runs()[0]['status']=='blocked'

@pytest.mark.parametrize('text,want', [('$65.00 $45.00',(45.0,'USD')), ('100 €',(100.0,'EUR')),('£75.00',(75.0,'GBP')),('1 249,90 PLN',(1249.9,'PLN'))])
def test_symbol_prices(text,want):
    assert price_text(text)==want


@pytest.mark.parametrize('title,category', [('Blue denim shirt','top'), ('Black denim jacket','outerwear'), ('Baggy denim jeans','bottom')])
def test_material_does_not_override_garment_category(title, category):
    from freshhead.catalog import enrich
    p = Product(store='eme', url='https://emestudios.com/product/test-denim', title=title)
    assert enrich(p).category == category
