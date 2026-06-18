let BOT = '';
let curType = 'all', curGenre = 'all', curSort = 'new';
let allMovies = [];
const typeLabel = { movie: 'Kino', series: 'Serial', anime: 'Anime', cartoon: 'Multfilm' };
// Fake poster ranglari (poster bo'lmasa)
const palettes = ['c1','c2','c3','c4','c5','c6','c7','c8','c9','c10'];

function esc(s){ return String(s==null?'':s).replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c])); }
function pal(id){ return palettes[id % palettes.length]; }

// Sevimlilar — kirgan bo'lsa serverda (bot bilan umumiy), aks holda qurilmada
let ME = { logged_in: false };
let SERVER_FAVS = null; // kirgan foydalanuvchi sevimlilari (id massivi)

function getFavs(){
  if (ME.logged_in && SERVER_FAVS) return SERVER_FAVS.slice();
  try { return JSON.parse(localStorage.getItem('astra_favs')||'[]'); } catch(e){ return []; }
}
function isFav(id){ return getFavs().indexOf(id) >= 0; }
function toggleFav(id, el){
  const adding = !isFav(id);
  if (ME.logged_in){
    if (!SERVER_FAVS) SERVER_FAVS = [];
    const i = SERVER_FAVS.indexOf(id);
    if (adding && i<0) SERVER_FAVS.push(id);
    if (!adding && i>=0) SERVER_FAVS.splice(i,1);
    fetch('/api/favorites', {
      method: adding ? 'POST' : 'DELETE',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ id: id })
    }).catch(()=>{});
  } else {
    var f = getFavs(); var j = f.indexOf(id);
    if (j>=0) f.splice(j,1); else f.push(id);
    try { localStorage.setItem('astra_favs', JSON.stringify(f)); } catch(e){}
  }
  if (el) el.classList.toggle('faved', adding);
  if (curType === 'fav') loadHome();
}

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
fetch('/api/botlink').then(r=>r.json()).then(d=>{ BOT = d.bot || ''; var tg=document.getElementById('tgLink'); if(tg && BOT) tg.href='https://t.me/'+BOT; }).catch(()=>{});

// ── Telegram orqali kirish / profil ───────────────────────────────────────────
function openLogin(){
  document.getElementById('loginModalBg').classList.add('show');
  document.getElementById('loginModal').classList.add('show');
}
function closeLogin(){
  document.getElementById('loginModalBg').classList.remove('show');
  document.getElementById('loginModal').classList.remove('show');
}
function toggleUserMenu(){ document.getElementById('userMenu').classList.toggle('show'); }
function closeUserMenu(){ document.getElementById('userMenu').classList.remove('show'); }

window.onTelegramAuth = function(user){
  fetch('/api/tg-login', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(user)
  }).then(r=>r.json()).then(d=>{
    if (d.ok){ closeLogin(); applyMe({logged_in:true, id:d.id, name:d.name, photo:d.photo}); loadServerFavs(true); }
    else { var n=document.getElementById('loginNote'); if(n) n.textContent='Kirishda xato. Qayta urinib ko\'ring.'; }
  }).catch(()=>{ var n=document.getElementById('loginNote'); if(n) n.textContent='Server bilan aloqa yo\'q.'; });
};

function applyMe(me){
  ME = me || {logged_in:false};
  var login = document.getElementById('navLogin');
  var av = document.getElementById('navAvatar');
  if (ME.logged_in){
    login.style.display='none';
    av.style.display='';
    var ph = ME.photo || '';
    var img = document.getElementById('navAvatarImg');
    var umImg = document.getElementById('userMenuImg');
    if (ph){ img.src=ph; umImg.src=ph; }
    else { img.src='/static/logo.svg'; umImg.src='/static/logo.svg'; }
    document.getElementById('userMenuName').textContent = ME.name || 'Foydalanuvchi';
  } else {
    login.style.display = BOT ? '' : 'none';
    av.style.display='none';
    closeUserMenu();
  }
}

function loadServerFavs(refresh){
  if (!ME.logged_in) return;
  fetch('/api/favorites').then(r=>r.json()).then(d=>{
    SERVER_FAVS = (d.ids||[]);
    if (refresh) loadHome();
  }).catch(()=>{ SERVER_FAVS = []; });
}

function doLogout(){
  fetch('/api/logout', {method:'POST'}).then(()=>{
    applyMe({logged_in:false}); SERVER_FAVS=null; closeUserMenu();
    if (curType==='fav') loadHome();
  }).catch(()=>{});
}

function initTelegramLogin(botUser){
  if (!botUser) return;
  var holder = document.getElementById('tgLoginBtn');
  if (!holder || holder.dataset.loaded) return;
  holder.dataset.loaded='1';
  var s = document.createElement('script');
  s.async = true;
  s.src = 'https://telegram.org/js/telegram-widget.js?22';
  s.setAttribute('data-telegram-login', botUser);
  s.setAttribute('data-size', 'large');
  s.setAttribute('data-userpic', 'true');
  s.setAttribute('data-request-access', 'write');
  s.setAttribute('data-onauth', 'onTelegramAuth(user)');
  holder.appendChild(s);
}

// Boshlang'ich holat
fetch('/api/me').then(r=>r.json()).then(d=>{
  if (d.bot){ BOT = d.bot; initTelegramLogin(d.bot); }
  applyMe(d);
  if (d.logged_in) loadServerFavs(false);
}).catch(()=>{});

// Profil menyusi tashqariga bosilsa yopilsin
document.addEventListener('click', function(e){
  var um = document.getElementById('userMenu');
  var av = document.getElementById('navAvatar');
  if (um && um.classList.contains('show') && !um.contains(e.target) && av && !av.contains(e.target)) {
    um.classList.remove('show');
  }
});

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
  const fav = isFav(m.id) ? ' faved' : '';
  return `<a href="/kino/${m.id}" class="card ${pal(m.id)}" onclick="event.preventDefault();openMovie(${m.id})">
    <div class="card-img">${bg}<span class="card-name">${esc(m.title)}</span></div>
    <button class="fav-btn${fav}" onclick="event.stopPropagation();event.preventDefault();toggleFav(${m.id},this)" aria-label="Sevimlilarga qo'shish"><svg viewBox="0 0 24 24"><path d="M12 21s-8-5.3-8-11a4.5 4.5 0 0 1 8-2.8A4.5 4.5 0 0 1 20 10c0 5.7-8 11-8 11z"/></svg></button>
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
    // Sevimlilar rejimi
    if (curType === 'fav') {
      const favs = getFavs();
      if (!favs.length) {
        rows.innerHTML = '<div class="state-msg">Sevimlilar ro\'yxati bo\'sh. Kino kartasidagi yurakcha ❤ tugmasini bosing.</div>';
        if (typeof setHero==='function') setHero(null);
        return;
      }
      const res = await fetchMovies({ ids: favs.join(',') });
      const movies = (res.movies||[]);
      allMovies = movies;
      if (typeof setHero==='function') setHero(null);
      rows.innerHTML = rowHtml('❤ Sevimlilar', movies.map(cardHtml).join(''));
      return;
    }
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
  document.querySelectorAll('[data-nav]').forEach(a=>a.classList.toggle('nav-active', a.getAttribute('data-nav')===t));
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

// ── Galaxy click effekti — bosilganda yulduzchalar sachraydi ──
document.addEventListener('click', e => {
  const colors = ['#9d7bff', '#5ad1ff', '#ff6ec7', '#ffffff'];
  const count = 8;
  for (let i = 0; i < count; i++) {
    const spark = document.createElement('div');
    const angle = (Math.PI * 2 * i) / count + Math.random() * 0.5;
    const dist = 30 + Math.random() * 30;
    const dx = Math.cos(angle) * dist;
    const dy = Math.sin(angle) * dist;
    spark.style.cssText = `position:fixed;left:${e.clientX}px;top:${e.clientY}px;`
      + `width:6px;height:6px;border-radius:50%;pointer-events:none;z-index:9999;`
      + `background:${colors[i % colors.length]};box-shadow:0 0 8px currentColor;color:${colors[i % colors.length]};`
      + `--dx:${dx}px;--dy:${dy}px;animation:spark-burst 0.6s ease-out forwards;`;
    document.body.appendChild(spark);
    setTimeout(() => spark.remove(), 650);
  }
});
(function(){
  const s = document.createElement('style');
  s.textContent = '@keyframes spark-burst{0%{transform:translate(-50%,-50%) scale(1);opacity:1}'
    + '100%{transform:translate(calc(-50% + var(--dx)),calc(-50% + var(--dy))) scale(0);opacity:0}}';
  document.head.appendChild(s);
})();

// Boshlash
document.querySelectorAll('[data-nav]').forEach(a=>a.classList.toggle('nav-active', a.getAttribute('data-nav')==='all'));
loadHome();
