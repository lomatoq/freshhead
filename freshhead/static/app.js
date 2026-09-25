'use strict';
const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = p => p.price == null ? 'Цена не указана' : `${p.price_from ? 'от ' : ''}${new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(p.price)} ${esc(p.currency || 'валюта не указана')}`;
const colors = {black:'#30332e',white:'#fff',cream:'#e7e1ce',grey:'#90988d',blue:'#506b86',brown:'#695241',beige:'#c4b598',olive:'#767d50',green:'#54795b',red:'#b15d4b',pink:'#d1a0ac',yellow:'#d1bf69',orange:'#c08853',purple:'#8d7499'};
const categories = {top:'Верх',bottom:'Брюки',footwear:'Обувь',outerwear:'Куртки',accessory:'Аксессуары',unknown:'Не определено'};
const pages = {feed:'Находки',train:'Твой вкус',outfits:'Образы',saved:'Сохранённое',digest:'На сегодня',sources:'Магазины',settings:'Настройки',ai:'AI-зрение'};
const modeNames = {balanced:'Баланс объёмов',contrast:'Контраст пропорций',relaxed:'Свободный силуэт'};
let aiSimilar=null;
let state, current='feed', category='', search='', cached=[], looks=[], anchor='', mode='balanced', undo=[], requestId=0, toastTimer, pollTimer;

async function api(path, options={}) {
  const response = await fetch('/api'+path, {...options,headers:{'Content-Type':'application/json','X-Freshhead':'1',...options.headers}});
  const data = await response.json().catch(()=>({detail:'Не удалось прочитать ответ сервера'}));
  if (!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'Проверь введённые значения');
  return data;
}
const post = (path, body={}) => api(path,{method:'POST',body:JSON.stringify(body)});
function toast(message) {const el=$('#toast');el.textContent=message;el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,5000);}
function heading(title,sub='',actions='',eyebrow='CURATED BY YOU') {
  return `<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1>${sub?`<p class="heading-sub">${sub}</p>`:''}</div>${actions?`<div class="heading-buttons">${actions}</div>`:''}</div>`;
}
const syncButton = `<button class="button primary" data-action="refresh">Обновить ↗</button>`;
function syncState() {
  $('#sourceCount').textContent=String(state.stores.length).padStart(2,'0');
  $('#navCount').textContent=String(state.counts.products).padStart(2,'0');
  $('#tasteN').textContent=`${state.counts.liked}/20`;
  $('#tasteBar').style.width=Math.min(100,state.counts.liked/20*100)+'%';
  $('#demoBanner').hidden=!state.counts.demo;
  $('#scanBanner').hidden=!state.progress.running;
  $('#scanText').textContent=`${state.progress.store} · ${state.progress.message}`;
  $('#connection').textContent=state.progress.running?'◌ Проверяю магазины':'● Локальная база';
}
async function loadState() {state=await api('/state');syncState();}
function hero() {
  return `<section class="hero"><div class="hero-copy"><div class="eyebrow">GOOD PIECES. BETTER TOGETHER.</div><h2>Не просто вещи.<br>Твой следующий образ.</h2><p>Находим то, что нравится тебе.<br>И то, что классно работает вместе.</p><button class="button primary" data-page="outfits">Собрать образ <span>↗</span></button></div><div class="hero-art" aria-hidden="true"><div class="orb"></div><div class="hero-serial">01</div>${['top','bottom','footwear'].map((k,i)=>`<div class="style-ticket ${['one','two','three'][i]}"><img src="/static/demo/hero-${k}.svg" alt="" style="width:100%;height:${i===2?'90':'130'}px"><div class="ticket-label"><span>${['THE TOP','THE FIT','THE FINISH'][i]}</span><span>0${i+1}</span></div></div>`).join('')}<span class="art-caption">ILLUSTRATED COMBINATIONS / YOUR RULES</span></div></section>`;
}
function card(p, training=false) {
  const colorHtml=(p.colors||[]).map(c=>`<span class="color-dot" title="${esc(c)}" style="background:${colors[c]||'#bbb'}"></span>`).join('');
  return `<article class="product-card${training?' train-card':''}" data-id="${esc(p.id)}"><button class="product-photo" data-action="detail" data-id="${esc(p.id)}" aria-label="Подробнее: ${esc(p.title)}">${p.image?`<img src="${esc(p.image)}" loading="lazy" alt="${esc(p.title)}">`:`<span class="product-placeholder">${p.category==='footwear'?'↗':'✳'}</span>`}<span class="card-tag">${p.demo?'ДЕМО':p.extraction==='catalog-card'?'КАТАЛОГ':'НАЙДЕНО'}</span></button>${training?'':`<button class="like-button ${p.rating===1?'liked':''}" data-action="like" data-id="${esc(p.id)}" aria-label="${p.rating===1?'Убрать лайк':'Нравится'}">${p.rating===1?'♥':'♡'}</button>`}<div class="product-info"><div class="brand-line"><span>${esc(p.brand||state.stores.find(s=>s.id===p.store)?.name||p.store)}</span><span title="Индекс сходства, не вероятность">${p.score!=null?`${p.score} / 100`:''}</span></div><h3>${esc(p.title)}</h3><div class="product-info-bottom"><span class="price">${p.demo?'Концепт, не продаётся':money(p)}</span><span class="color-dots">${colorHtml}</span></div>${training?'':`<div class="card-actions"><button data-action="anchor" data-id="${esc(p.id)}">С чем носить ↗</button><button class="dismiss" data-action="dislike" data-id="${esc(p.id)}" aria-label="Не нравится">×</button></div>`}</div></article>`;
}
function emptyCatalog() {
  return `<div class="empty"><span class="empty-icon">◎</span><h2>Первый шаг — твои находки</h2><p>Настроено ${state.stores.length} источников. Доступность каждого проверяется отдельно. Проверь магазины или добавь прямые ссылки на любимые вещи.</p><div class="heading-buttons">${syncButton}<button class="button" data-action="import">Добавить ссылку +</button><button class="button" data-action="demo">Посмотреть демо</button></div><p class="tiny-note">Демо использует вымышленные вещи. Реальные данные появятся только после успешной проверки магазинов.</p></div>`;
}
async function feed(saved=false) {
  const content=$('#content');
  content.innerHTML=heading(saved?'Оставить себе.':'Хороший вкус.<br>Свежие находки.',saved?'То, к чему хочется возвращаться.':'Твой личный отбор из независимых брендов и streetwear-магазинов.',`<button class="button" data-action="import">Добавить +</button>${syncButton}`)+(!saved?hero():'')+`<div class="section-row"><h2>${saved?'Понравившиеся вещи':'Твой радар'}</h2><span class="muted">${esc(state.vision)}</span></div><div class="filters"><div class="chips">${[['','Всё'],...Object.entries(categories).slice(0,4)].map(([k,v])=>`<button class="chip ${category===k?'active':''}" data-category="${k}">${v}</button>`).join('')}</div><input class="search" id="searchInput" placeholder="Найти вещь или бренд ↗" value="${esc(search)}" aria-label="Поиск по каталогу"></div><div id="gridContent"><div class="product-grid">${Array(4).fill('<div class="skeleton"></div>').join('')}</div></div>${saved?`<div id="savedLooks"></div>`:''}<p class="tiny-note">Индекс /100 — оценка алгоритма, не вероятность. Размер, посадка и доставка проверяются в магазине. Цены не конвертируются между валютами.</p>`;
  await loadFeed(saved);
  if(saved && state.saved_outfits.length) $('#savedLooks').innerHTML=`<div class="section-row" style="margin-top:40px"><h2>Сохранённые образы</h2></div><div class="outfit-grid">${state.saved_outfits.map((o,i)=>outfitCard(o,i,true)).join('')}</div>`;
  let timer;
  $('#searchInput').addEventListener('input',e=>{search=e.target.value;clearTimeout(timer);timer=setTimeout(()=>loadFeed(saved),250);});
}
async function loadFeed(saved=false) {
  const id=++requestId;
  const data=await api(`/products?view=${saved?'saved':'feed'}&category=${encodeURIComponent(category)}&q=${encodeURIComponent(search)}`);
  if(id!==requestId||!$('#gridContent'))return;
  cached=data.items;
  $('#gridContent').innerHTML=cached.length?`<div class="product-grid">${cached.map(p=>card(p)).join('')}</div>`:(!state.counts.products&&!state.counts.demo?emptyCatalog():`<div class="empty"><span class="empty-icon">✳</span><h2>${saved?'Здесь будут твои фавориты':'Пока ничего не нашлось'}</h2><p>${saved?'Нажми на сердечко у понравившейся вещи.':'Попробуй другую категорию или смягчи бюджет и ограничения по размерам.'}</p><button class="button primary" data-page="${saved?'feed':'settings'}">${saved?'К находкам':'Открыть настройки'} ↗</button></div>`);
}
async function train() {
  const data=await api('/products?view=train');cached=data.items;
  $('#content').innerHTML=heading('Покажи, что твоё.','Лайкай вещи, а не фотографии. Пропуск не считается дизлайком.',`<button class="button" data-action="import">Добавить своё +</button>`,'TASTE IS PERSONAL')+`<div class="train-layout"><div id="trainStage"></div><div class="train-notes"><div class="eyebrow">${state.counts.rated} ОЦЕНОК В БАЗЕ</div><h2>Алгоритм знакомится<br>с тобой, не наоборот.</h2><p>Не нужно выбирать один стиль. Тебе могут нравиться и спокойный трикотаж, и выразительный деним.</p><div class="step-line"><span class="num">01</span><div><h3>20–50 любимых вещей</h3><p>Добавь несколько примеров верха, брюк и обуви.</p></div></div><div class="step-line"><span class="num">02</span><div><h3>«Нет» тоже помогает</h3><p>Дизлайк отделяет похожее от действительно твоего.</p></div></div><div class="step-line"><span class="num">03</span><div><h3>Вещь ≠ целый образ</h3><p>Личный вкус и сочетаемость считаются отдельно.</p></div></div><p class="tiny-note">Сейчас: ${esc(state.vision)}. Прогресс на демо не обучает отбор реальных товаров.</p><button class="button ghost" data-action="undo">↶ Отменить последнюю оценку</button></div></div>`;
  renderTrain();
}
function renderTrain() {
  const p=cached[0],el=$('#trainStage');if(!el)return;
  el.innerHTML=p?`${card(p,true)}<div class="train-actions"><button class="vote" data-action="trainNo" aria-label="Не нравится">×</button><button class="vote" data-action="trainSkip" aria-label="Пропустить">→</button><button class="vote yes" data-action="trainYes" aria-label="Нравится">♡</button></div><div class="keyboard-hint"><kbd>←</kbd> нет &nbsp; <kbd>↓</kbd> пропуск &nbsp; <kbd>→</kbd> да</div>`:`<div class="empty"><span class="empty-icon">✳</span><h2>${state.counts.rated?'На сегодня всё оценено':'Начни с каталога'}</h2><p>Добавь новые вещи или обнови магазины, чтобы продолжить обучение.</p>${syncButton}<button class="button" data-action="demo">Демо</button></div>`;
}
async function vote(pid,value,advance=false) {
  const old=cached.find(p=>p.id===pid)?.rating??null;
  await post(`/products/${encodeURIComponent(pid)}/rating`,{value});
  undo.push({id:pid,value:old});
  await loadState();
  if(advance){cached=cached.filter(p=>p.id!==pid);renderTrain();}
  else if(current==='feed'||current==='saved')await loadFeed(current==='saved');
}
async function outfits() {
  const data=await api('/products');cached=data.items;
  const anchors=cached.filter(p=>['top','bottom','footwear'].includes(p.category));
  $('#content').innerHTML=heading('Всё складывается.','Начни с одной вещи. Найди брюки, верх и обувь, которые работают вместе.','','BETTER TOGETHER')+`<div class="outfit-toolbar"><label class="field">Вещь, вокруг которой собираем<select id="anchorSelect"><option value="">Подобрать весь образ</option>${anchors.map(p=>`<option value="${esc(p.id)}" ${p.id===anchor?'selected':''}>${p.demo?'ДЕМО · ':''}${esc(p.title)}</option>`).join('')}</select></label><div class="chips">${Object.entries(modeNames).map(([k,v])=>`<button class="chip ${mode===k?'active':''}" data-mode="${k}">${v}</button>`).join('')}</div><button class="button primary" data-action="build">Собрать ↗</button></div><div id="outfitResults"></div><div class="sources-note">Не «модно / немодно», а несколько возможных решений. Контраст пропорций вдохновлён разбором MR PORTER; баланс цвета и фактур — гипотезы этого прототипа. <button class="button ghost" data-action="showSources">Откуда правила ↗</button></div>`;
  $('#anchorSelect').addEventListener('change',e=>anchor=e.target.value);
  await generateLooks();
}
async function generateLooks() {
  const target=$('#outfitResults');if(!target)return;
  target.innerHTML='<p class="muted">Собираю варианты…</p>';
  const data=await post('/outfits',{anchor_id:anchor,mode});looks=data.outfits;
  if(!$('#outfitResults'))return;
  target.innerHTML=looks.length?`<div class="outfit-grid">${looks.map((o,i)=>outfitCard(o,i)).join('')}</div>`:`<div class="empty"><span class="empty-icon">⌘</span><h2>Нужно ещё несколько вещей</h2><p>Для образа нужны верх, брюки и обувь. ${data.missing?.length?'Не хватает: '+data.missing.map(k=>categories[k]).join(', ')+'.':''} Проверь фильтры и магазины.</p><button class="button primary" data-page="feed">К находкам ↗</button><button class="button" data-action="demo">Попробовать на демо</button></div>`;
}
function outfitCard(o,i,saved=false) {
  const total=Object.entries(o.totals||{}).map(([c,p])=>money({price:p,currency:c})).join(' + ')||'Цены не указаны';
  const isDemo=o.items.some(p=>p.demo);
  return `<article class="outfit-card"><div class="outfit-top"><span>${isDemo?'ДЕМО / ':''}LOOK ${String(i+1).padStart(2,'0')}</span><span class="outfit-score" title="Рейтинг, не вероятность">Сочетание ${o.compatibility}/100</span></div><div class="outfit-items">${o.items.map(p=>`<button class="outfit-piece" data-action="detail" data-id="${esc(p.id)}">${p.image?`<img src="${esc(p.image)}" loading="lazy" alt="${esc(p.title)}">`:'<span class="product-placeholder">✳</span>'}<span>${esc(p.title)}</span></button>`).join('')}</div><div class="outfit-body"><h3>${modeNames[o.mode]||'Твой образ'}</h3>${o.reasons.map(r=>`<p class="reason">${esc(r)}</p>`).join('')}<div class="outfit-bottom"><span class="outfit-total">${isDemo?'Иллюстративный комплект':total}</span><button class="button small" data-action="${saved?'deleteLook':'saveLook'}" data-id="${esc(o.id)}">${saved?'Убрать':'Сохранить ♡'}</button></div><details><summary>Как это оценено · уверенность ${esc(o.confidence)}</summary><p>${esc(o.note)}<br>Личный вкус: ${o.taste}/100. Сочетание: ${o.compatibility}/100.</p>${state.sources.filter(s=>o.source_ids.includes(s.id)).map(s=>s.url?`<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.author)} · ${esc(s.title)} ↗</a><br>`:`${esc(s.title)}<br>`).join('')}</details></div></article>`;
}
function sourcePage() {
  $('#content').innerHTML=heading(`${state.stores.length} источников.<br>Один твой стиль.`,'Независимые марки, кроссовки и выразительные силуэты. Статус ниже — результат реальной проверки.',syncButton,'THE STORES')+`<div class="source-grid">${state.stores.map((s,i)=>{
    const run=state.runs.find(r=>r.store===s.id);
    const good=run&&['ok','running'].includes(run.status);
    return `<article class="source-card"><span class="source-index">SOURCE / ${String(i+1).padStart(2,'0')}</span><h2>${esc(s.name)}</h2><div class="source-total">${s.products||0} вещей в локальной базе</div><p>${esc(s.description)}</p><div class="source-state ${run&&!good?'error':''}">${run?`${esc(run.status.toUpperCase())} · ${esc(run.message)}`:'НЕ ПРОВЕРЕН · Доступность подтвердится после первого успешного сбора.'}</div><div class="source-actions"><label class="switch-label"><input type="checkbox" data-source="${s.id}" ${state.settings.enabled_stores.includes(s.id)?'checked':''}>Включён</label><a class="button small" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">Сайт ↗</a><button class="button small" data-action="refreshOne" data-id="${s.id}">Проверить</button></div></article>`;
  }).join('')}</div><div class="sources-note">Сборщик читает публичные страницы и JSON-LD, соблюдает robots.txt и делает паузы между запросами. На блокировке или CAPTCHA останавливается. Лимит одного прохода: ${state.settings.page_budget} запросов на магазин. Ошибка магазина не означает, что новых вещей нет.</div><div class="section-row" style="margin-top:35px"><h2>Откуда идеи сочетаний</h2></div>${styleReferences()}`;
}
function styleReferences(){return `<div class="panel">${state.sources.map(s=>`<p><b>${esc(s.author)}</b> · ${esc(s.date)}<br>${s.url?`<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)} ↗</a>`:esc(s.title)}<br>${esc(s.summary)}</p>`).join('')}</div>`;}

function aiPage(){
  const a=state.ai||{},j=a.job||{};
  const pct=j.total?Math.round(100*(j.done||0)/j.total):0;
  const phase={loading:'Загрузка модели',indexing:'Анализ фотографий',completed:'Проход завершён',stopped:'Остановлено',interrupted:'Прервано',error:'Ошибка анализа',empty:'Фото не обработаны'}[j.phase]||'Готов к работе';
  const missing=Object.entries(a.dependencies||{}).filter(([,ok])=>!ok).map(([name])=>name);
  const errors=(j.errors||[]).reduce((acc,e)=>{const key=`${e.store}: ${e.reason}`;acc[key]=(acc[key]||0)+1;return acc;},{});
  const cards=aiSimilar?.items||[];
  cached=cards;
  $('#content').innerHTML=heading('Увидеть твой вкус.','Фотография → визуальные признаки → твои предпочтения. Всё на этом компьютере.','','LOCAL FASHION INTELLIGENCE')+`
  <section class="ai-hero panel"><div><div class="eyebrow">FASHIONCLIP · LOCAL</div><h2>${a.indexed?`${a.indexed} фото уже в индексе`:'Начни с первого анализа'}</h2><p>${a.enabled?'AI включён в рекомендации.':'AI выключен в настройках. Индекс сохранён, но в подборе не используется.'}</p><div class="ai-metrics"><div><b>${a.indexed||0}</b><span>проанализировано</span></div><div><b>${a.pending||0}</b><span>ожидают анализа</span></div><div><b>${a.rated_indexed||0}</b><span>с твоей оценкой</span></div></div></div><div class="ai-status"><span class="status-pill">${a.running?'◌ В работе':a.ready?'● Модель доступна для запуска':'○ Нужна установка'}</span><p><b>${esc(j.device_name||'GPU / CPU определяется при первом запуске')}</b></p><p>Лимит: ${state.settings.ai_batch_limit} фото за проход.<br>Без API-ключа и оплаты за запрос.</p></div></section>
  <section class="panel ai-job"><div class="section-row"><h2>${esc(phase)}</h2><span>${a.running?`${j.done||0} / ${j.total||0}`:j.total?`${j.indexed||0} готовы · ${j.failed||0} пропущены`:''}</span></div>${a.running?`<progress class="ai-progress" value="${pct}" max="100" aria-label="Прогресс анализа"></progress>`:''}<p>${esc(j.message||'Новые фото обрабатываются порциями. Лайки и дизлайки идут первыми, остальные вещи чередуются по магазинам и категориям.')}</p><div class="heading-buttons"><button class="button primary" data-action="aiStart" ${a.running||!a.ready?'disabled':''}>${a.running?'Анализирую…':'Проанализировать новые фото ↗'}</button>${a.can_stop?'<button class="button" data-action="aiStop">Остановить</button>':''}<button class="button" data-page="settings">Настройки</button><button class="button" data-page="train">Показать свой вкус ♡</button></div>${!a.ready?`<p>Не установлены: ${esc(missing.join(', '))}. Запусти <code>install-ai.bat</code> на Windows или <code>bash install-ai.sh</code> на macOS/Linux. Первый анализ скачивает веса модели.</p>`:''}${Object.keys(errors).length?`<details><summary>Почему часть фото пропущена</summary><p>Защита сайта, robots.txt, недоступная или повреждённая фотография не обходятся. Здесь последние ошибки прохода.</p>${Object.entries(errors).map(([k,n])=>`<p class="tiny-note">${esc(k)} · ${n}</p>`).join('')}</details>`:''}</section>
  <div class="ai-explain"><section class="panel"><div class="eyebrow">01 / SEE</div><h2>Не только название</h2><p>Модель превращает фото в 512 чисел. По ним находятся похожие вещи, даже когда в названии только артикул.</p><p>Цвет, крой и визуальная масса обуви могут дополнять пропуски в описании. Догадки AI не заменяют явные характеристики магазина.</p></section><section class="panel"><div class="eyebrow">02 / LEARN YOUR TASTE</div><h2>Лайки меняют отбор</h2><p>Сравниваем вещи внутри одной категории с твоими лайками и дизлайками. Визуальная часть — 70%, признаки из описания — 30%, когда обе фотографии уже в индексе.</p><p>Это персональный поиск ближайших примеров, не дообучение весов нейросети. Несколько разных направлений вкуса могут сосуществовать.</p></section><section class="panel"><div class="eyebrow">03 / BUILD A LOOK</div><h2>Сходство ≠ сочетание</h2><p>Верх, брюки и обувь соединяются отдельными правилами цвета, объёмов и фактур. Рейтинг сочетания — эвристика, не мнение обученного стилиста.</p><p>Фон, поза модели и освещение могут влиять на анализ. AI не знает точную посадку на тебе, размер и качество ткани.</p></section></div>
  ${aiSimilar?`<div class="section-row"><div><div class="eyebrow">VISUAL NEIGHBOURS</div><h2>Похожие на ${esc(aiSimilar.anchor.title)}</h2></div></div><p class="muted">Та же категория, только уже проанализированные фото. Учитываются фильтры магазинов, бюджета и твои дизлайки.</p>${cards.length?`<div class="product-grid">${cards.map(p=>card(p)).join('')}</div>`:'<div class="panel">Пока нет подходящих соседей. Проанализируй ещё фото или смягчи фильтры.</div>'}`:'<p class="sources-note">В карточке проанализированной вещи появится кнопка «Похожие по фото». Фото загружаются с сайтов магазинов, веса — из Hugging Face. Вычисления и история твоих оценок остаются локально.</p>'}`;
}

function settingsPage() {
  const s=state.settings;
  $('#content').innerHTML=heading('Под твои правила.','Размеры, бюджет, режим отбора. Всё сохраняется на твоём компьютере.','','MAKE IT YOURS')+`<form id="settingsForm"><div class="settings-grid"><section class="panel"><h2>Твои размеры</h2><div class="field-grid">${['top','bottom','footwear','outerwear'].map(k=>`<label>${categories[k]}<input name="size_${k}" value="${esc((s.sizes[k]||[]).join(', '))}" placeholder="${k==='footwear'?'EU 42, EU 42.5':k==='bottom'?'W32, 32, M':'M, L'}"></label>`).join('')}</div><p>Вводи обозначения как в магазине: EU, UK, US и W — разные системы. Они не конвертируются автоматически. Несколько вариантов — через запятую.</p><label><input type="checkbox" name="strict_sizes" ${s.strict_sizes?'checked':''}>Только подтверждённое наличие моего размера</label><label>Раздел<select name="gender">${[['men','Мужское + унисекс'],['women','Женское + унисекс'],['all','Все взрослые вещи']].map(([k,v])=>`<option value="${k}" ${s.gender===k?'selected':''}>${v}</option>`).join('')}</select></label><label>Сезон<select name="season">${[['any','Любой'],['warm','Тёплый'],['cool','Прохладный']].map(([k,v])=>`<option value="${k}" ${s.season===k?'selected':''}>${v}</option>`).join('')}</select></label></section><section class="panel"><h2>Бюджет на одну вещь</h2><div class="field-grid">${['PLN','EUR','GBP','USD'].map(c=>`<label>Максимум, ${c}<input type="number" min="1" max="1000000" name="budget_${c}" value="${s.budgets[c]||''}" required></label>`).join('')}</div><p>Каждая валюта фильтруется отдельно. Это не конвертация; стоимость доставки не включена.</p><label><input name="hide_unknown_prices" type="checkbox" ${s.hide_unknown_prices?'checked':''}>Не показывать вещи без цены</label><label>Режим образов<select name="outfit_mode">${Object.entries(modeNames).map(([k,v])=>`<option value="${k}" ${s.outfit_mode===k?'selected':''}>${v}</option>`).join('')}</select></label></section><section class="panel"><h2>Каждый день — новое</h2><label><input name="daily_enabled" type="checkbox" ${s.daily_enabled?'checked':''}>Ежедневный сбор и подборка</label><div class="field-grid"><label>Время · Europe/Warsaw<input type="time" name="daily_time" value="${s.daily_time}" required></label><label>Количество вещей<input type="number" name="digest_count" min="1" max="20" value="${s.digest_count}"></label></div><p>Работает, пока приложение запущено и компьютер не спит. Пропущенные дни не рассылаются пачкой.</p><p>Telegram: <b>${state.telegram_ready?'настроен':'не настроен'}</b>. Без него подборка остаётся во вкладке «На сегодня».</p><div class="code-hint"># Только в локальном файле .env\nTELEGRAM_BOT_TOKEN=...\nTELEGRAM_CHAT_ID=...</div></section><section class="panel"><h2>Каталог и зрение</h2><label>Лимит запросов на магазин<input name="page_budget" type="number" min="4" max="120" value="${s.page_budget}"></label><label><input name="browser_fallback" type="checkbox" ${s.browser_fallback?'checked':''}>Playwright для пустых JS-страниц</label><p>Требует requirements-browser.txt и установленный Chromium. Не обходит блокировки и не читает запрещённые API.</p><p>Сейчас: <b>${esc(state.vision)}</b><br>Проиндексировано фото: ${state.counts.images_indexed}.</p><div class="code-hint">pip install -r requirements-vision.txt\npython -m freshhead index-images</div><label><input name="ai_enabled" type="checkbox" ${s.ai_enabled?'checked':''}>Использовать AI в рекомендациях</label><label><input name="ai_auto_index" type="checkbox" ${s.ai_auto_index?'checked':''}>Анализировать новые фото после обновления магазинов</label><label>Фото за один AI-проход<input name="ai_batch_limit" type="number" min="1" max="1000" value="${s.ai_batch_limit}"></label><p>Локальный FashionCLIP. Лимит бережёт ресурсы; оставшиеся фото обрабатываются следующим проходом. Это не виртуальная примерка.</p><button class="button" data-page="ai" type="button">Открыть AI-зрение ↗</button></section></div><div class="settings-actions"><button class="button primary" type="submit">Сохранить настройки ↗</button><button class="button" data-page="sources" type="button">Управлять магазинами</button><span class="muted">Нет внешнего аккаунта. Нет облачной истории лайков.</span></div></form>`;
  $('#settingsForm').addEventListener('submit', async e=>{
    e.preventDefault();const f=new FormData(e.target);const updated={...s,sizes:{},budgets:{}};
    for(const k of ['top','bottom','footwear','outerwear'])updated.sizes[k]=f.get('size_'+k).split(',').map(x=>x.trim()).filter(Boolean);
    for(const c of ['PLN','EUR','GBP','USD'])updated.budgets[c]=Number(f.get('budget_'+c));
    for(const k of ['daily_enabled','strict_sizes','hide_unknown_prices','browser_fallback','ai_enabled','ai_auto_index'])updated[k]=f.has(k);
    for(const k of ['daily_time','gender','season','outfit_mode'])updated[k]=f.get(k);
    for(const k of ['digest_count','page_budget','ai_batch_limit'])updated[k]=Number(f.get(k));
    try{await api('/settings',{method:'PUT',body:JSON.stringify(updated)});await loadState();mode=updated.outfit_mode;toast('Настройки сохранены');}catch(err){toast(err.message);}
  });
}
function digestPage() {
  const d=state.digest;
  $('#content').innerHTML=heading('Твой ежедневный edit.','Новые вещи. Перепроверенные карточки. Без бесконечных повторов.','','FRESH TODAY')+`<div class="digest-head"><div><h2>${d?'Подборка · '+esc(d.day):'Радар ещё не запускался'}</h2><p>${d?esc(d.message):'Сначала загрузи каталог и отметь понравившиеся вещи.'}<br>${d?.delivery_message?esc(d.delivery_message):state.telegram_ready?'Telegram подключён. Отправка — отдельной кнопкой.':'Без Telegram подборка сохраняется здесь.'}</p></div><div class="heading-buttons"><button class="button primary" data-action="digest">Собрать сейчас ↗</button>${state.telegram_ready?'<button class="button" data-action="sendDigest">Отправить в Telegram</button>':''}</div></div><div id="digestBody">${d?.items.length?`<div class="product-grid">${d.items.map(p=>card(p)).join('')}</div>`:`<div class="empty"><span class="empty-icon">↗</span><h2>Только реальные находки</h2><p>Демонстрационные вещи никогда не входят в ежедневную подборку. Перед добавлением карточки повторно проверяются.</p><button class="button" data-page="sources">Проверить магазины ↗</button></div>`}${d?.outfits?.length?`<div class="section-row" style="margin-top:35px"><h2>Идеи образов из каталога</h2></div><p class="muted">Наличие всех составляющих этих образов отдельно не перепроверялось.</p><div class="outfit-grid">${d.outfits.map((o,i)=>outfitCard(o,i)).join('')}</div>`:''}</div>`;
  cached=d?.items||[];
}
function detail(pid) {
  let p=cached.find(p=>p.id===pid)||looks.flatMap(o=>o.items).find(p=>p.id===pid)||state.saved_outfits.flatMap(o=>o.items).find(p=>p.id===pid)||state.digest?.outfits.flatMap(o=>o.items).find(p=>p.id===pid);
  if(!p)return;
  const stock=p.available===true?'Есть в наличии по последней проверке':p.available===false?'Нет в наличии':'Наличие не подтверждено';
  const variants=(p.variants||[]).filter(v=>v.size);
  $('#productDetail').innerHTML=`<div class="detail-layout"><div class="detail-image">${p.image?`<img src="${esc(p.image)}" alt="${esc(p.title)}">`:''}</div><div class="detail-info"><div class="eyebrow">${p.demo?'ИЛЛЮСТРАТИВНОЕ ДЕМО':esc(p.brand||p.store)}</div><h2>${esc(p.title)}</h2><div class="price">${p.demo?'Вымышленная вещь':money(p)}</div><p>${esc(stock)}</p><div class="chips">${variants.length?variants.map(v=>`<span class="chip">${esc(v.size)} ${v.available===true?'✓':v.available===false?'×':'?'}</span>`).join(''):'<span class="chip">Размеры не подтверждены</span>'}</div><p>${esc((p.description||'').slice(0,650))}</p><p>${esc(p.reason||'Признаки кроя и цвета получены из текста карточки.')}</p><div class="heading-buttons">${p.demo?'':`<a class="button primary" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">В магазин ↗</a>`}<button class="button lime" data-action="anchor" data-id="${p.id}">С чем носить</button>${p.vision||Object.keys(p.visual_attributes||{}).length?`<button class="button" data-action="similar" data-id="${esc(p.id)}">Похожие по фото ↗</button>`:state.settings.ai_enabled?'<span class="tiny-note">Фото ещё не проанализировано. Запусти AI-зрение.</span>':''}</div><div class="metadata">${esc(categories[p.category])} · крой: ${esc(p.fit)}<br>Источник: ${esc(p.extraction)}<br>Обновлено: ${esc(new Date(p.fetched_at).toLocaleString('ru-RU'))}<br>${esc(p.attributes_source)}<br>Доставку в Польшу и окончательную цену проверь при покупке.</div></div></div>`;
  $('#productDialog').showModal();
}
async function navigate(page) {
  $('#mobileMenu').hidden=true;$('#mobileMenuButton').setAttribute('aria-expanded','false');
  if(!pages[page])page='feed';current=page;requestId++;location.hash=page;
  $$('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.page===page));
  $('#sectionName').textContent=pages[page].toUpperCase();
  try {
    if(page==='feed'||page==='saved')await feed(page==='saved');
    if(page==='train')await train();
    if(page==='outfits')await outfits();
    if(page==='sources')sourcePage();
    if(page==='settings')settingsPage();
    if(page==='digest')digestPage();
    if(page==='ai')aiPage();
    window.scrollTo({top:0,behavior:'instant'});
  } catch(err){toast(err.message);}
}
document.addEventListener('click',async e=>{
  const b=e.target.closest('button,[data-page]');if(!b)return;
  if(b.dataset.page){e.preventDefault();await navigate(b.dataset.page);return;}
  if(b.dataset.category!==undefined){category=b.dataset.category;await feed(current==='saved');return;}
  if(b.dataset.mode){mode=b.dataset.mode;$$('[data-mode]').forEach(x=>x.classList.toggle('active',x.dataset.mode===mode));await generateLooks();return;}
  const action=b.dataset.action,pid=b.dataset.id;
  if(!action)return;
  try{
    if(action==='refresh'||action==='refreshOne'){await post('/refresh'+(pid?'?store='+encodeURIComponent(pid):''));await loadState();toast('Проверка запущена. Прогресс виден сверху.');}
    if(action==='import'){$('#importStatus').textContent='';$('#importDialog').showModal();}
    if(action==='demo'){await post('/demo');await loadState();await navigate(current);toast('Демо добавлено. Это вымышленные вещи, не предложения магазинов.');}
    if(action==='detail')detail(pid);
    if(action==='aiStart'){b.disabled=true;const r=await post('/ai/index');toast(`AI: начинаю проход, до ${r.limit} фото`);await loadState();aiPage();}
    if(action==='aiStop'){await post('/ai/stop');await loadState();aiPage();toast('AI остановлен. Результаты сохранены.');}
    if(action==='similar'){aiSimilar=await api(`/products/${encodeURIComponent(pid)}/similar`);$('#productDialog').close();await navigate('ai');}
    if(action==='like'){const p=cached.find(p=>p.id===pid);await vote(pid,p?.rating===1?null:1);}
    if(action==='dislike'){await vote(pid,-1);toast('Учёл дизлайк. Отмена доступна во вкладке «Твой вкус».');}
    if(action==='anchor'){anchor=pid;$('#productDialog').close();await navigate('outfits');}
    if(action==='build')await generateLooks();
    if(action==='trainYes'||action==='trainNo'||action==='trainSkip'){if(cached[0])await vote(cached[0].id,action==='trainYes'?1:action==='trainNo'?-1:0,true);}
    if(action==='undo'){const last=undo.pop();if(last){await post(`/products/${last.id}/rating`,{value:last.value});await loadState();await navigate(current);}else toast('Пока нечего отменять');}
    if(action==='showSources'){await navigate('sources');}
    if(action==='saveLook'){await post('/outfits/save',{outfit_id:pid,anchor_id:anchor,mode});await loadState();toast('Образ сохранён');}
    if(action==='deleteLook'){await api('/outfits/'+pid,{method:'DELETE'});await loadState();await navigate('saved');}
    if(action==='digest'||action==='sendDigest'){b.disabled=true;b.textContent='Перепроверяю вещи…';toast('Перепроверяю карточки. При недоступном магазине вещь будет пропущена.');await post('/digest',{send:action==='sendDigest'});await loadState();if(current==='digest')digestPage();toast('Подборка обновлена');}
  }catch(err){toast(err.message);}finally{b.disabled=false;}
});
document.addEventListener('change',async e=>{
  const sid=e.target.dataset.source;if(!sid)return;
  const ids=new Set(state.settings.enabled_stores);e.target.checked?ids.add(sid):ids.delete(sid);
  try{await api('/settings',{method:'PUT',body:JSON.stringify({...state.settings,enabled_stores:[...ids]})});await loadState();toast('Список магазинов сохранён');}catch(err){e.target.checked=!e.target.checked;toast(err.message);}
});
document.addEventListener('keydown',async e=>{
  if(current!=='train'||$('dialog[open]')||['INPUT','SELECT','TEXTAREA'].includes(document.activeElement?.tagName))return;
  const values={ArrowLeft:-1,ArrowDown:0,ArrowRight:1};if(e.key in values&&cached[0]){e.preventDefault();try{await vote(cached[0].id,values[e.key],true);}catch(err){toast(err.message);}}
});
$$('.dialog-close').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));
$$('dialog').forEach(d=>d.addEventListener('click',e=>{if(e.target===d){const r=d.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)d.close();}}));
$('#mobileMenuButton').addEventListener('click',()=>{const menu=$('#mobileMenu');menu.hidden=!menu.hidden;$('#mobileMenuButton').setAttribute('aria-expanded',String(!menu.hidden));});
$('#settingsShortcut').addEventListener('click',()=>navigate('settings'));
$('#removeDemo').addEventListener('click',async()=>{try{await api('/demo',{method:'DELETE'});await loadState();await navigate(current);}catch(err){toast(err.message);}});
$('#importForm').addEventListener('submit',async e=>{
  e.preventDefault();const button=$('button[type=submit]',e.target);button.disabled=true;$('#importStatus').textContent='Читаю карточку магазина…';
  try{const p=await post('/import',{url:new FormData(e.target).get('url')});$('#importDialog').close();await loadState();await navigate('feed');toast('Добавлено: '+p.title);}catch(err){$('#importStatus').textContent=err.message;}finally{button.disabled=false;}
});
// Image failures don't turn the entire card into a broken UI.
document.addEventListener('error',e=>{if(e.target.tagName==='IMG'&&!e.target.dataset.failed){e.target.dataset.failed='1';e.target.src='/static/demo/unavailable.svg';}},true);
async function init(){
  try{await loadState();mode=state.settings.outfit_mode;await navigate(location.hash.slice(1)||'feed');
    pollTimer=setInterval(async()=>{try{const was=state.progress.running;await loadState();if(was&&!state.progress.running&&current!=='settings'){await navigate(current);toast('Проверка завершена. Подробности — в магазинах.');}else if(current==='sources'&&state.progress.running)sourcePage();else if(current==='ai')aiPage();}catch{/* keep a useful existing UI during a temporary connection failure */}},3000);
  }catch(err){$('#content').innerHTML=`<div class="empty"><h2>Нет соединения с Freshhead</h2><p>Запусти start.bat или python -m freshhead, затем обнови страницу.<br>${esc(err.message)}</p></div>`;}
}
init();
