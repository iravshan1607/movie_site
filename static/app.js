let BOT = '';
let curType = 'all', curGenre = 'all', curSort = 'new';
let allMovies = [];
const typeLabel = { movie: 'Kino', series: 'Serial', anime: 'Anime', cartoon: 'Multfilm' };
// Fake poster ranglari (poster bo'lmasa)
const palettes = ['c1','c2','c3','c4','c5','c6','c7','c8','c9','c10'];

function esc(s){ return String(s==null?'':s).replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c])); }
function pal(id){ return palettes[id % palettes.length]; }

// Mobil menyu
function toggleMenu(){ document.body.classList.toggle('menu-open'); }
function closeMenu(){ document.body.classList.remove('menu-open'); }

// Hero strip kataklari
document.getElementById('heroStrip').innerHTML = Array(42).fill('<div class="hero-strip-cell"></div>').join('');

// Navbar scroll
window.addEventListener('scroll', () => {
  document.getElementById('navbar').classList.toggle('scrolled', window.scrollY > 50);
});

// Bot username
fetch('/api/botlink').then(r=>r.json()).then(d=>{ BOT = d.bot || ''; }).catch(()=>{});

// Janrlar
fetch('/api/genres').then(r=>r.json()).then(d=>{
  if (d.genres && d.genres.length) {
    const box = document.getElementById('genres');
    box.innerHTML = '<div class="genre-pill active" onclick="setGenre(\'all\', this)">Barchasi</div>' +
      d.genres.map(g => `<div class="genre-pill" onclick="setGenre('${esc(g)}', this)">${esc(g)}</div>`).join('');
  }
}).catch(()=>{});

// Poster URL
function posterUrl(m){ return m.poster_url ? m.poster_url : (m.has_poster ? `/api/poster/${m.id}` : ''); }

// Oddiy karta (16:9)
function cardHtml(m){
  const p = posterUrl(m);
  const bg = p ? `<img src="${p}" loading="lazy" onerror="this.remove()">` : '';
  return `<a href="/kino/${m.id}" class="card ${pal(m.id)}" onclick="event.preventDefault();openMovie(${m.id})">
    <div class="card-img">${bg}<span class="card-name">${esc(m.title)}</span></div>
    <div class="card-overlay"><div class="card-play"><svg viewBox="0 0 24 24" fill="#000"><path d="M8 5v14l11-7z"/></svg></div></div>
  </a>`;
}
// Top 10 karta (2:3 + raqam)
function top10Html(m, rank){
  const p = posterUrl(m);
  const bg = p ? `<img src="${p}" loading="lazy" onerror="this.remove()">` : '';
  return `<a href="/kino/${m.id}" class="card-top10" onclick="event.preventDefault();openMovie(${m.id})">
    <div class="card-top10-img ${pal(m.id)}">${bg}
      <div style="padding:8px;font-size:11px;font-weight:500;color:#fff;position:absolute;bottom:8px;left:8px;text-shadow:0 1px 4px #000;z-index:2;">${esc(m.title)}</div>
    </div>
    <div class="top10-number">${rank}</div>
  </a>`;
}

function rowHtml(title, cardsHtml, isTop10){
  return `<div class="row">
    <div class="row-header"><span class="row-title">${title}</span></div>
    <div class="${isTop10?'cards-top10':'cards'}">${cardsHtml}</div>
  </div>`;
}

async function fetchMovies(params){
  const q = new URLSearchParams(params);
  const r = await fetch('/api/movies?' + q);
  return await r.json();
}

// Asosiy yuklash — bir nechta qator
async function loadHome(){
  const rows = document.getElementById('rows');
  rows.innerHTML = '<div class="loader"><div class="spin"></div>Kinolar yuklanmoqda...</div>';
  try {
    const typeFilter = curType !== 'all' ? { type: curType } : {};
    const genreFilter = curGenre !== 'all' ? { genre: curGenre } : {};
    const base = { ...typeFilter, ...genreFilter };

    // Hammasini olamiz (1-sahifa, ko'proq)
    const all = await fetchMovies({ ...base, page: 1 });
    if (all.error) { rows.innerHTML = `<div class="state-msg">⚠️ Xato: ${esc(all.error)}</div>`; return; }
    if (!all.movies || !all.movies.length) {
      rows.innerHTML = '<div class="state-msg">😕 Hech narsa topilmadi.</div>';
      setHero(null);
      return;
    }
    const movies = all.movies;
    allMovies = movies;

    // Hero uchun — eng ko'p ko'rilgan 5 ta (yoki birinchi 5) aylanadi
    const heroPool = [...movies].sort((a,b)=>(b.views||0)-(a.views||0)).slice(0,5);
    startHeroRotation(heroPool.length ? heroPool : movies.slice(0,5));

    // Saralash
    let sorted = [...movies];
    if (curSort === 'views') sorted.sort((a,b)=>(b.views||0)-(a.views||0));
    else if (curSort === 'year') sorted.sort((a,b)=>(b.year||0)-(a.year||0));

    let html = '';
    // Saralash + tasodifiy tugma
    html += `<div class="sort-bar">
      <span class="sort-label">Saralash:</span>
      <button class="sort-btn ${curSort==='new'?'active':''}" onclick="setSort('new')">🆕 Yangi</button>
      <button class="sort-btn ${curSort==='views'?'active':''}" onclick="setSort('views')">🔥 Ko'p ko'rilgan</button>
      <button class="sort-btn ${curSort==='year'?'active':''}" onclick="setSort('year')">📅 Yil</button>
      <button class="random-btn" onclick="randomMovie()">🎲 Tasodifiy</button>
    </div>`;

    const listTitle = (curType==='all' && curGenre==='all')
      ? {new:'🆕 Yangi qo\'shildi', views:'🔥 Eng ko\'p ko\'rilgan', year:'📅 Yil bo\'yicha'}[curSort]
      : 'Natijalar';
    html += rowHtml(listTitle, sorted.slice(0, 18).map(cardHtml).join(''));

    // Qo'shimcha qatorlar — faqat "Yangi" saralash + Barchasi rejimida
    if (curSort === 'new' && curType === 'all' && curGenre === 'all') {
      const top = [...movies].sort((a,b)=>(b.views||0)-(a.views||0)).slice(0,10);
      if (top.some(m=>m.views>0)) {
        html += rowHtml('🔥 Eng ko\'p ko\'rilgan', top.map((m,i)=>top10Html(m,i+1)).join(''), true);
      }
      for (const [t, label] of [['movie','🎬 Kinolar'],['series','📺 Seriallar'],['anime','🌸 Anime'],['cartoon','🧸 Multfilmlar']]) {
        const sub = movies.filter(m=>m.type===t);
        if (sub.length) html += rowHtml(label, sub.slice(0,12).map(cardHtml).join(''));
      }
    }
    rows.innerHTML = html;
  } catch(e){
    rows.innerHTML = '<div class="state-msg">❌ Yuklashda xatolik.</div>';
  }
}

function setSort(s){ curSort = s; loadHome(); }
function randomMovie(){
  if (!allMovies.length) return;
  const m = allMovies[Math.floor(Math.random()*allMovies.length)];
  openMovie(m.id);
}

// Hero avtomatik almashinishi
let heroTimer = null, heroIdx = 0, heroList = [];
function startHeroRotation(list){
  heroList = list || [];
  heroIdx = 0;
  if (heroTimer) clearInterval(heroTimer);
  if (!heroList.length) { setHero(null); return; }
  setHero(heroList[0]);
  if (heroList.length > 1) {
    heroTimer = setInterval(() => {
      heroIdx = (heroIdx + 1) % heroList.length;
      setHero(heroList[heroIdx], true);
    }, 6000);
  }
}

function setHero(m, animate){
  const h = document.getElementById('heroContent');
  const bgEl = document.getElementById('heroPosterBg') || document.querySelector('.hero-poster-bg');
  if (!m) { return; }
  const p = posterUrl(m);

  const apply = () => {
    if (p && bgEl) {
      bgEl.style.backgroundImage = `url('${p}')`;
      bgEl.style.backgroundSize = 'cover';
      bgEl.style.backgroundPosition = 'center';
      bgEl.style.backgroundRepeat = 'no-repeat';
    }
    const badge = typeLabel[m.type] || 'Kino';
    h.innerHTML = `
      <div class="hero-badge">${badge}${m.year?' · '+m.year:''}</div>
      <div class="hero-title">${esc(m.title)}</div>
      ${m.genre?`<div class="hero-subtitle">${esc(m.genre)}</div>`:''}
      <div class="hero-meta">
        ${m.rating?`<span class="match">★ ${m.rating}</span>`:''}
        ${m.year?`<span>${m.year}</span>`:''}
        ${m.quality?`<span class="age">${esc(m.quality)}</span>`:''}
        ${m.language?`<span>${esc(m.language)}</span>`:''}
      </div>
      <div class="hero-btns">
        <a class="btn btn-play" onclick="openMovie(${m.id})"><svg viewBox="0 0 24 24" fill="#000"><path d="M8 5v14l11-7z"/></svg> Ko'rish</a>
        <button class="btn btn-info" onclick="openMovie(${m.id})">ℹ Batafsil</button>
      </div>`;
  };

  if (animate) {
    // Eskisini chapga surib chiqaramiz, yangisini o'ngdan kiritamiz
    h.classList.add('hero-slide-out');
    if (bgEl) bgEl.style.opacity = '0';
    setTimeout(() => {
      apply();
      h.classList.remove('hero-slide-out');
      h.classList.add('hero-slide-in');
      if (bgEl) bgEl.style.opacity = '1';
      setTimeout(() => h.classList.remove('hero-slide-in'), 600);
    }, 400);
  } else {
    apply();
  }
}

function setType(t){
  curType = t; curGenre = 'all';
  document.querySelectorAll('.genre-pill').forEach((p,i)=>p.classList.toggle('active', i===0));
  window.scrollTo({top:0,behavior:'smooth'});
  loadHome();
}
function setGenre(g, el){
  curGenre = g;
  document.querySelectorAll('.genre-pill').forEach(p=>p.classList.remove('active'));
  if (el) el.classList.add('active');
  loadHome();
}

// Qidiruv
function openSearch(){ document.getElementById('searchOverlay').classList.add('open'); setTimeout(()=>document.getElementById('searchInput').focus(),100); }
function closeSearch(){ document.getElementById('searchOverlay').classList.remove('open'); document.getElementById('searchResults').innerHTML=''; document.getElementById('searchInput').value=''; }
async function doSearch(){
  const q = document.getElementById('searchInput').value.trim();
  const box = document.getElementById('searchResults');
  if (!q) return;
  box.innerHTML = '<div class="loader"><div class="spin"></div></div>';
  try {
    const d = await fetchMovies({ q, page: 1 });
    if (!d.movies || !d.movies.length) { box.innerHTML = '<div class="state-msg">Topilmadi</div>'; return; }
    box.innerHTML = `<div class="cards">${d.movies.map(cardHtml).join('')}</div>`;
  } catch(e){ box.innerHTML = '<div class="state-msg">Xato</div>'; }
}

// Modal
async function openMovie(id){
  const modal = document.getElementById('movieModal');
  const box = document.getElementById('movieBox');
  box.innerHTML = '<div class="loader"><div class="spin"></div></div>';
  modal.classList.add('open'); document.body.style.overflow='hidden';
  try {
    const r = await fetch('/api/movie/'+id);
    const d = await r.json();
    if (!d.found) { box.innerHTML = '<div class="state-msg">Topilmadi</div>'; return; }
    const m = d.movie;
    const p = posterUrl(m);
    const isSeries = m.type==='series' || m.type==='anime';
    const botLink = BOT ? `https://t.me/${BOT}?start=movie_${m.id}` : '#';
    box.innerHTML = `
      <div class="mm-hero" style="${p?`background-image:url('${p}')`:''}">
        <button class="mm-close" onclick="closeMovie()">×</button>
        <div class="mm-hero-in">
          ${p?`<img class="mm-poster" src="${p}" onerror="this.outerHTML='<div class=\\'mm-poster ph\\'>🎬</div>'">`:`<div class="mm-poster ph">🎬</div>`}
          <div class="mm-info">
            <h2>${esc(m.title)}</h2>
            <div class="mm-tags">
              <span class="mm-tag r">${typeLabel[m.type]||'Kino'}</span>
              ${m.year?`<span class="mm-tag">${m.year}</span>`:''}
              ${m.quality?`<span class="mm-tag">${esc(m.quality)}</span>`:''}
              ${m.language?`<span class="mm-tag">${esc(m.language)}</span>`:''}
              ${m.rating?`<span class="mm-tag">★ ${m.rating}</span>`:''}
            </div>
          </div>
        </div>
      </div>
      <div class="mm-body">
        ${m.genre?`<div style="color:var(--text-muted);font-size:13px;margin-bottom:14px;">${esc(m.genre)}</div>`:''}
        ${m.description?`<p class="mm-desc">${esc(m.description)}</p>`:'<p class="mm-desc" style="opacity:0.5">Tavsif yo\'q.</p>'}
        <a href="${botLink}" target="_blank" class="mm-watch">
          ${isSeries?'📺 Botda qismlarni ko\'rish':'▶ Botda ko\'rish / yuklab olish'}
        </a>
        <p class="mm-note">👁 ${m.views} marta ko'rilgan · Telegram bot orqali ochiladi</p>
        ${similarHtml(m)}
      </div>`;
  } catch(e){ box.innerHTML = '<div class="state-msg">❌ Xatolik</div>'; }
}

function similarHtml(m){
  // Shu janr yoki turdagi boshqa kinolar (o'zidan tashqari)
  let pool = allMovies.filter(x => x.id !== m.id && m.genre && x.genre && x.genre.toLowerCase().includes(m.genre.split(',')[0].trim().toLowerCase()));
  if (pool.length < 4) {
    // janr kam bo'lsa — shu turdan to'ldiramiz
    const more = allMovies.filter(x => x.id !== m.id && x.type === m.type && !pool.includes(x));
    pool = pool.concat(more);
  }
  pool = pool.slice(0, 8);
  if (!pool.length) return '';
  const cards = pool.map(x => {
    const p = posterUrl(x);
    const bg = p ? `<img src="${p}" loading="lazy" onerror="this.remove()">` : '<div style="display:flex;align-items:center;justify-content:center;height:100%;font-size:24px;">🎬</div>';
    return `<div class="similar-card" onclick="openMovie(${x.id})">
      <div class="similar-poster">${bg}</div>
      <div class="st">${esc(x.title)}</div>
    </div>`;
  }).join('');
  return `<div class="similar-row"><h4>🎯 O'xshash kinolar</h4><div class="similar-cards">${cards}</div></div>`;
}
function closeMovie(){ document.getElementById('movieModal').classList.remove('open'); document.body.style.overflow=''; }
document.addEventListener('keydown', e=>{ if(e.key==='Escape'){ closeMovie(); closeSearch(); closeMenu(); } });

// Boshlash
loadHome();
