"""Offline regression coverage for source adapters and the local AI workflow."""
import io
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from freshhead.app import create_app
from freshhead.catalog import STORES, extract, store_for, enrich
from freshhead.db import DB
from freshhead.models import Product, now
from freshhead.stores_extra import NEW_IDS
from freshhead.style import Ranker
from freshhead.vision import (MODEL, REVISION, PROMPT_VERSION, apply_attributes, index_images,
    load_records, load_index, select_pending, valid_vector, vision_products)
from freshhead.ai import AIJobs


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path)


def product(db, name='p', store='reebok', **kwargs):
    p=enrich(Product(store=store, url=f'https://{STORES[store].domain}/products/{name}',
                     title='Regular white trainers', image=f'https://cdn.example.com/{name}.jpg', **kwargs))
    for key,value in kwargs.items(): setattr(p,key,value)
    db.upsert(p)
    return p


def vector(n=0):
    v=[0.0]*512; v[n]=1.0
    return v


def record(db,p,v=None,attrs=None,revision=REVISION):
    with db.connect() as c:
        c.execute('INSERT OR REPLACE INTO image_features VALUES(?,?,?,?,?,?,?,?)',
            (p.id,p.image,MODEL,revision,PROMPT_VERSION,json.dumps(v if v is not None else vector()),json.dumps(attrs or {}),now()))


def test_all_12_sources_and_domain_validation():
    assert len(NEW_IDS)==12 and len(STORES)==16
    for sid in NEW_IDS:
        assert store_for(STORES[sid].seeds[0]).id==sid
    with pytest.raises(ValueError): store_for('https://www.adidas.pl.evil.example/ABCD12.html')


def test_settings_migrate_once_without_changing_likes(db):
    p=product(db); db.rate(p.id,1)
    db.set('settings',{'enabled_stores':['walk'],'budgets':{'PLN':777}})
    with db.connect() as c: c.execute("DELETE FROM kv WHERE key='catalog_migration_v2'")
    migrated=DB(db.path)
    assert migrated.settings().enabled_stores==['walk']+NEW_IDS
    assert migrated.settings().budgets=={'PLN':777}
    assert migrated.ratings()=={p.id:1}
    s=migrated.settings(); s.enabled_stores=['walk']; migrated.set('settings',s.model_dump())
    assert DB(db.path).settings().enabled_stores==['walk']


def test_worker_database_handle_does_not_interrupt_live_scan(db):
    recent=db.begin_run('reebok'); old=db.begin_run('bstn')
    with db.connect() as c:
        c.execute('UPDATE runs SET started=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(hours=3)).isoformat(),old))
    reopened=DB(db.path)
    runs={r['id']:r['status'] for r in reopened.runs()}
    assert runs[recent]=='running' and runs[old]=='interrupted'


def test_sibling_images_and_adjacent_product_prices():
    html='''<script>Shopify.currency={"active":"EUR"};</script>
    <div class="product-card"><a href="/products/one">One black jeans</a><img alt="One black jeans" src="https://img.example/one.jpg"><div class="price"><s>199,00 €</s><span>59,00 €</span></div></div>
    <div class="product-card"><a href="/products/two">Two knit sweater</a><img alt="Two knit sweater" src="https://img.example/two.jpg"><div class="price">89,00 €</div></div>'''
    ps,_=extract(html,'https://oluolin.com/en-eu/collections/new-arrival',STORES['oluolin'])
    assert sorted((p.title,p.price,p.currency) for p in ps)==[('One black jeans',59,'EUR'),('Two knit sweater',89,'EUR')]


def test_collection_url_deduplicated_and_service_excluded():
    html='''<div class="product-card"><a href="/collections/new/products/jeans">Jeans</a><a href="/products/jeans?variant=1"><img alt="Jeans" src="https://img.example/p.jpg"></a><span>$89.00</span></div>
    <div class="product-card"><a href="/products/worry-free-purchase"><img alt="Worry-free purchase" src="https://img.example/s.jpg"></a><span>$2.00</span></div>'''
    ps,_=extract(html,'https://www.kiikio.com/',STORES['kiikio'])
    assert len(ps)==1 and ps[0].url=='https://www.kiikio.com/products/jeans'


def test_price_without_currency_is_not_invented():
    html='<div class="product-card"><a href="/products/model-2002"><img alt="Model 2002 jeans" src="https://img.example/p.jpg"></a><span class="price">89.00</span></div>'
    ps,_=extract(html,'https://oluolin.com/',STORES['oluolin'])
    assert len(ps)==1 and ps[0].price is None


@pytest.mark.parametrize('bad',[[1], [float('nan')]+[0]*511,[True]+[0]*511,[0]*512,'invalid'])
def test_corrupt_embeddings_rejected(bad):
    assert not valid_vector(bad)


def test_index_invalidation_and_disable(db):
    p=product(db); record(db,p)
    assert p.id in load_index(db)
    s=db.settings(); s.ai_enabled=False; db.set('settings',s.model_dump())
    assert load_index(db)=={} and p.id in load_records(db)
    p.image='https://cdn.example.com/changed.jpg'; db.upsert(p)
    assert load_records(db)=={}
    record(db,p,revision='outdated'); assert load_records(db)=={}


def test_visual_guesses_only_fill_missing_attributes(db):
    p=product(db,colors=['black'],fit='wide',material='cotton')
    attrs={'category':{'label':'bottom','accepted':True},'colors':{'label':'pink','accepted':True},
           'fit':{'label':'slim','accepted':True},'shoe_mass':{'label':'heavy','accepted':True},
           'material':{'label':'leather','accepted':True}}
    before=p.model_dump(); q=apply_attributes(p,attrs)
    assert q.category=='footwear' and q.colors==['black'] and q.fit=='wide' and q.material=='cotton'
    assert q.shoe_mass=='heavy' and p.model_dump()==before
    attrs['shoe_mass']['accepted']=False
    assert apply_attributes(p,attrs).shoe_mass=='unknown'


def test_selection_prioritizes_votes_and_balances_stores(db):
    many=[product(db,f'r{i}') for i in range(8)]
    other=product(db,'k1',store='kiikio')
    db.rate(many[-1].id,-1)
    pending=select_pending(db,{},3)
    assert pending[0].id==many[-1].id and other.id in [p.id for p in pending]
    assert many[-1].id not in [p.id for p in select_pending(db,{many[-1].id:{}},20)]


def test_real_index_pipeline_with_fake_encoder_and_no_network(db):
    ps=[product(db,f'img{i}') for i in range(3)]
    buf=io.BytesIO(); Image.new('RGB',(64,64)).save(buf,'PNG')
    class FakeDownloader:
        def fetch(self,url):
            if url.endswith('img2.jpg'): raise ValueError('Image path disallowed by robots.txt')
            return buf.getvalue()
    class FakeEncoder:
        device='test'; device_name='test only, not a model benchmark'
        def encode(self,images,products): return [(vector(),{}) for _ in images]
    assert index_images(db,limit=3,encoder_factory=FakeEncoder,downloader=FakeDownloader())==2
    assert len(load_records(db))==2
    job=db.get('ai_job'); assert job['phase']=='completed' and job['failed']==1 and not job['running']
    assert len(select_pending(db,load_records(db),20))==1


def test_visual_similarity_changes_taste_not_model_weights(db):
    liked=product(db,'liked'); similar=product(db,'similar'); different=product(db,'different')
    db.rate(liked.id,1)
    r=Ranker(db.products(),db.ratings(),{liked.id:vector(),similar.id:vector(),different.id:vector(1)})
    assert r.taste(similar)[0]>r.taste(different)[0]
    assert r.taste(similar)==r.taste(similar)
    assert r.similarity(liked,similar)>r.similarity(liked,different)


def test_ai_api_missing_dependencies_and_same_category_neighbors(db,monkeypatch):
    anchor=product(db,'a'); similar=product(db,'b'); other=product(db,'c',category='bottom'); disliked=product(db,'d')
    for p in (anchor,similar,other,disliked): record(db,p)
    db.rate(disliked.id,-1)
    app=create_app(db)
    monkeypatch.setattr(AIJobs,'dependencies',staticmethod(lambda:{'filelock':True,'torch':False}))
    with TestClient(app) as c:
        assert c.post('/api/ai/index',headers={'X-Freshhead':'1'}).status_code==422
        assert c.post('/api/ai/stop').status_code==403
        results=c.get(f'/api/products/{anchor.id}/similar').json()
        assert [p['id'] for p in results['items']]==[similar.id]
        assert c.get('/api/ai/status').json()['indexed']==4
        assert len(c.get('/api/state').json()['stores'])==16


def test_missing_embedding_and_disabled_ai_do_not_claim_photo_search(db):
    p=product(db)
    with TestClient(create_app(db)) as c:
        assert c.get(f'/api/products/{p.id}/similar').status_code==422
        record(db,p)
        s=db.settings();s.ai_enabled=False;db.set('settings',s.model_dump())
        assert c.get(f'/api/products/{p.id}/similar').status_code==422


def test_wss_sku_metadata_not_neighbour_price():
    ga=json.dumps({'item_id':'1','item_name':'Reebok Premier Road Ultra','currency':'PLN',
        'item_brand':'Reebok','item_category':'Mężczyzna','item_category2':'Obuwie','price':569.99})
    html=f'''<div class="listing-product"><div class="listing-product__image-block"><a href="/reebok-chalk.html" data-ga='{ga}'></a><img src="https://img.example/chalk.jpg"></div>
    <div class="colors"><a href="/reebok-blue.html"><img alt="Reebok Blue" src="https://img.example/blue.jpg"></a></div><span>569,99 zł</span></div>
    <a href="/blog/fashion.html">Blog</a>'''
    ps,links=extract(html,'https://warsawsneakerstore.com/',STORES['wss'])
    assert len(ps)==1 and ps[0].price==569.99 and ps[0].category=='footwear' and ps[0].gender=='men'
    assert not any('/blog/' in x for x in links)


def test_corrupt_visual_attribute_does_not_break_feed(db):
    p=product(db);record(db,p,attrs={'shoe_mass':'heavy','colors':123})
    assert len(vision_products(db))==1


def test_bstn_public_embedded_json_prices_and_sizes():
    hit={'objectID':'1','name':'MIND 001','brand':'Nike','image_url':'https://img.bstn.com/one.jpg',
         'url':'r/nike-mind-001','price':{'EUR':{'default':89.99}},'categories.level1':['Men /// Footwear'],
         'gender':['men','women'],'color':['Brown'],'swatches_conf':{'size':{'label':'EU','items':[
             {'label':'42,5','in_stock':1,'sku':'1'},{'label':'44','in_stock':0,'sku':'2'}]}}}
    html='<script id="__NEXT_DATA__" type="application/json">'+json.dumps({'props':{'hits':[hit]}})+'</script>'
    ps,_=extract(html,'https://www.bstn.com/eu_en/men/new-arrivals/footwear',STORES['bstn'])
    assert len(ps)==1 and ps[0].price==89.99 and ps[0].currency=='EUR'
    assert ps[0].url=='https://www.bstn.com/eu_en/r/nike-mind-001'
    assert ps[0].category=='footwear' and ps[0].gender=='unisex'
    assert ps[0].variants[0].size=='EU 42,5' and ps[0].variants[1].available is False


def test_tracking_pixel_is_skipped_without_breaking_other_photos(db):
    good=product(db,'good'); tiny=product(db,'tracking')
    buf=io.BytesIO(); Image.new('RGB',(64,64)).save(buf,'PNG')
    pixel=io.BytesIO(); Image.new('RGB',(1,1)).save(pixel,'PNG')
    class Downloader:
        def fetch(self,url): return pixel.getvalue() if 'tracking' in url else buf.getvalue()
    class Encoder:
        device='test'; device_name='test'
        def encode(self,images,products):
            assert all(min(im.size)>=32 for im in images)
            return [(vector(),{}) for im in images]
    assert index_images(db,2,encoder_factory=Encoder,downloader=Downloader())==1
    assert set(load_records(db))=={good.id}
    assert db.get('ai_job')['failed']==1 and db.get('ai_job')['phase']=='completed'


@pytest.mark.parametrize('sid', ['kiikio','aelfric'])
def test_t4s_width_one_uses_explicit_responsive_width(sid):
    domain=STORES[sid].domain
    html='<div class="product-card"><a href="/products/baggy-jeans"><img alt="Baggy jeans" data-src="//'+domain+'/cdn/shop/files/jeans.jpg?v=123&amp;width=1" data-widths="[100,200,600,1000]" src="data:image/gif;base64,R0lGODlhAQABAAAA"></a><span>$89.00</span></div>'
    ps,_=extract(html,'https://'+domain+'/',STORES[sid])
    assert len(ps)==1 and ps[0].image.endswith('v=123&width=600')
    assert ps[0].price==89
