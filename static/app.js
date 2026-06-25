let BOT = '';
let curType = 'all', curGenre = 'all', curSort = 'new';
let gridPage = 1, gridSort = 'new';      // kategoriya/janr to'ri uchun sahifalash
let gridYear = 'all', gridLang = 'all', gridQuality = 'all';   // qo'shimcha filtrlar
let FILTERS = null;                       // /api/filters keshi
let searchPage = 1, searchQ = '';        // qidiruv sahifalashi

// Ko'rishni davom ettirish — ochilgan kinolar (localStorage)
function getRecent(){
  try { return JSON.parse(localStorage.getItem('astra_recent') || '[]'); }
  catch(e){ return []; }
}
function pushRecent(m){
  if (!m || !m.id) return;
  try {
    let list = getRecent().filter(x => x.id !== m.id);
    list.unshift({ id:m.id, title:m.title, poster_id:m.poster_id, poster_url:m.poster_url,
                   type:m.type, year:m.year, rating:m.rating, genre:m.genre });
    list = list.slice(0, 12);
    localStorage.setItem('astra_recent', JSON.stringify(list));
  } catch(e){}
}
let allMovies = [];
const typeLabel = { movie: 'Kino', series: 'Serial', anime: 'Anime', cartoon: 'Multfilm' };
// Fake poster ranglari (poster bo'lmasa)
const palettes = ['c1','c2','c3','c4','c5','c6','c7','c8','c9','c10'];

function esc(s){ return String(s==null?'':s).replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c])); }
function pal(id){ return palettes[id % palettes.length]; }

// Qisqa suzuvchi xabar (toast) — masalan spam himoyasi ogohlantirishi
function toast(msg, isErr){
  var t = document.getElementById('astraToast');
  if (!t){
    t = document.createElement('div');
    t.id = 'astraToast';
    t.style.cssText = 'position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(20px);'
      + 'background:#1b1840;color:#fff;border:1px solid #2a2750;padding:13px 20px;border-radius:12px;'
      + 'font-size:14px;z-index:9000;box-shadow:0 10px 40px rgba(0,0,0,.5);opacity:0;'
      + 'transition:opacity .2s,transform .2s;max-width:90vw;text-align:center;pointer-events:none;';
    document.body.appendChild(t);
  }
  t.style.borderColor = isErr ? 'rgba(255,128,136,.55)' : '#2a2750';
  t.textContent = msg;
  requestAnimationFrame(function(){ t.style.opacity='1'; t.style.transform='translateX(-50%) translateY(0)'; });
  clearTimeout(t._timer);
  t._timer = setTimeout(function(){ t.style.opacity='0'; t.style.transform='translateX(-50%) translateY(20px)'; }, 3200);
}

// Kontent almashganda yumshoq paydo bo'lish animatsiyasi
function fadeIn(el){
  if (!el) return;
  el.classList.remove('view-fade');
  void el.offsetWidth;      // animatsiyani qayta ishga tushiramiz
  el.classList.add('view-fade');
}

// ── Bildirishnomalar (qo'ng'iroq) ────────────────────────────────────────────
var _notifItems = [], _notifPoll = null;function startNotifPoll(){
  if (_notifPoll) return;
  _notifPoll = setInterval(function(){ if (ME.logged_in) loadNotif(); }, 60000);
}
function loadNotif(){
  fetch('/api/notifications').then(function(r){return r.json();}).then(function(d){
    if (!d || d.logged_in === false) return;
    _notifItems = d.items || [];
    var badge = document.getElementById('navBellBadge');
    if (badge){
      var n = d.unread || 0;
      badge.textContent = n > 9 ? '9+' : n;
      badge.style.display = n > 0 ? '' : 'none';
    }
    var panel = document.getElementById('notifPanel');
    if (panel && panel.classList.contains('open')) renderNotif();
  }).catch(function(){});
}
function renderNotif(){
  var list = document.getElementById('notifList');
  if (!list) return;
  if (!_notifItems.length){
    list.innerHTML = '<div class="notif-empty">Hozircha bildirishnoma yo\'q 🔕</div>';
    return;
  }
  list.innerHTML = _notifItems.map(function(n){
    var icon = n.type === 'release' ? '🎉' : '💬';
    var clickable = n.movie_id ? (' onclick="openNotif('+n.movie_id+')"') : '';
    return '<div class="notif-item'+(n.read?'':' unread')+(n.movie_id?' clickable':'')+'"'+clickable+'>'
      + '<span class="notif-ic">'+icon+'</span>'
      + '<div class="notif-body"><div class="notif-text">'+esc(n.text)+'</div>'
      + '<div class="notif-date">'+esc(n.date)+'</div></div></div>';
  }).join('');
}
function toggleNotif(){
  var panel = document.getElementById('notifPanel');
  if (!panel) return;
  var open = panel.classList.contains('open');
  if (open){ panel.classList.remove('open'); return; }
  closeUserMenu();
  renderNotif();
  panel.classList.add('open');
  // ochilganda — o'qilgan deb belgilaymiz
  if (_notifItems.some(function(n){ return !n.read; })){
    fetch('/api/notifications/read', { method:'POST' }).then(function(){
      var badge = document.getElementById('navBellBadge');
      if (badge) badge.style.display = 'none';
      _notifItems.forEach(function(n){ n.read = true; });
    }).catch(function(){});
  }
}
function closeNotif(){ var p = document.getElementById('notifPanel'); if (p) p.classList.remove('open'); }
function openNotif(movieId){ closeNotif(); openMovie(movieId); }
document.addEventListener('click', function(e){
  var panel = document.getElementById('notifPanel');
  var bell = document.getElementById('navBell');
  if (panel && panel.classList.contains('open') && !panel.contains(e.target) && bell && !bell.contains(e.target)){
    closeNotif();
  }
});

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
  toast(adding ? '❤️ Sevimlilarga qo\'shildi' : 'Sevimlilardan olib tashlandi');
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

// Reklama banner (bosh sahifa) — /api/ad dan oladi, bo'lmasa hech narsa ko'rsatmaydi
(function loadSiteAd(){
  try {
    fetch('/api/ad').then(function(r){return r.json();}).then(function(d){
      var ad = d && d.ad; var box = document.getElementById('adBanner');
      if (!ad || !box) return;
      function esc(s){ return String(s==null?'':s).replace(/[<>&"]/g,function(c){return ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'})[c];}); }
      var inner;
      if (ad.image_url) {
        inner = '<div style="display:flex;gap:13px;align-items:center;">'
          + '<img src="'+esc(ad.image_url)+'" alt="" loading="lazy" style="width:96px;height:68px;object-fit:cover;border-radius:8px;flex:0 0 auto;">'
          + '<div style="min-width:0;"><div style="font-size:15px;color:#fff;font-weight:500;line-height:1.45;">'+esc(ad.title)+'</div>'
          + (ad.link ? '<div style="font-size:13px;color:#9b93c4;margin-top:4px;">Batafsil →</div>' : '')
          + '</div></div>';
      } else {
        inner = '<div style="display:flex;gap:12px;align-items:center;">'
          + '<div style="width:38px;height:38px;flex:0 0 auto;border-radius:9px;background:rgba(124,92,255,0.25);display:flex;align-items:center;justify-content:center;font-size:19px;">📢</div>'
          + '<div style="font-size:15px;color:#fff;font-weight:500;line-height:1.45;">'+esc(ad.title)+'</div></div>';
      }
      var style = 'display:block;text-decoration:none;background:rgba(124,92,255,0.10);border:1px solid rgba(124,92,255,0.4);border-radius:12px;padding:16px;margin:20px 0 6px;position:relative;';
      var label = '<span style="position:absolute;top:8px;right:10px;font-size:10px;color:#8a82b8;text-transform:uppercase;letter-spacing:0.5px;">Reklama</span>';
      if (ad.link) box.innerHTML = '<a href="'+esc(ad.link)+'" target="_blank" rel="noopener nofollow sponsored" style="'+style+'">'+label+inner+'</a>';
      else box.innerHTML = '<div style="'+style+'">'+label+inner+'</div>';
    }).catch(function(){});
  } catch(e) {}
})();

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

function setAvatar(photo, name){
  var btn = document.getElementById('navAvatar');
  var um = document.getElementById('userMenuImg');
  var initial = ((name || '?').trim().charAt(0) || '?').toUpperCase();
  btn.classList.remove('av-initial');
  btn.innerHTML = '';
  if (photo){
    var im = document.createElement('img');
    im.id = 'navAvatarImg';
    im.alt = '';
    im.onerror = function(){ btn.classList.add('av-initial'); btn.textContent = initial; };
    im.src = photo;
    btn.appendChild(im);
  } else {
    btn.classList.add('av-initial');
    btn.textContent = initial;
  }
  if (um){
    if (photo){ um.src = photo; um.style.display=''; um.onerror=function(){ um.style.display='none'; }; }
    else { um.src = '/static/logo.svg'; }
  }
}

function applyMe(me){
  ME = me || {logged_in:false};
  var login = document.getElementById('navLogin');
  var av = document.getElementById('navAvatar');
  var bell = document.getElementById('navBell');
  if (ME.logged_in){
    login.style.display='none';
    av.style.display='';
    setAvatar(ME.photo || '', ME.name || 'Foydalanuvchi');
    document.getElementById('userMenuName').textContent = ME.name || 'Foydalanuvchi';
    if (bell){ bell.style.display=''; loadNotif(); startNotifPoll(); }
  } else {
    login.style.display = BOT ? '' : 'none';
    av.style.display='none';
    if (bell) bell.style.display='none';
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
  const rate = m.rating ? `<span class="card-rate">⭐ ${(+m.rating).toFixed(1)}</span>` : '';
  const meta = [m.year, ({movie:'Kino',series:'Serial',anime:'Anime',cartoon:'Multfilm'}[m.type]||''), m.quality].filter(Boolean).join(' · ');
  return `<a href="/kino/${m.id}" class="card ${pal(m.id)}" onclick="event.preventDefault();openMovie(${m.id})">
    <div class="card-img">${bg}${rate}<span class="card-name">${esc(m.title)}</span>${meta?`<span class="card-meta">${esc(meta)}</span>`:''}</div>
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

// ── Skeleton (yuklanish paytidagi "miltillovchi" bo'sh kartalar) ─────────────
function skeletonHome(){
  var cards = '<div class="cards">' + Array(6).fill('<div class="skel skel-card"></div>').join('') + '</div>';
  var row = '<div class="skel-row"><div class="skel skel-row-title"></div>' + cards + '</div>';
  return row + row;
}
function skeletonGrid(n){
  return '<div class="cards">' + Array(n||12).fill('<div class="skel skel-card"></div>').join('') + '</div>';
}
function skeletonSoon(){
  return '<div class="soon-grid">' + Array(8).fill('<div class="skel skel-card tall skel-soon"></div>').join('') + '</div>';
}
function skeletonModal(){
  return '<div class="skel skel-modal-hero"></div>'
    + '<div style="padding:22px;">'
    + '<div class="skel skel-line" style="width:58%;height:26px;margin-bottom:14px;"></div>'
    + '<div class="skel skel-line" style="width:38%;"></div>'
    + '<div class="skel skel-line" style="width:92%;margin-top:18px;"></div>'
    + '<div class="skel skel-line" style="width:86%;"></div>'
    + '<div class="skel skel-line" style="width:74%;"></div>'
    + '<div class="skel skel-line" style="width:100%;height:48px;margin-top:22px;border-radius:8px;"></div>'
    + '</div>';
}

// ── Sahifalash (page raqamlari) ──────────────────────────────────────────────
function pageNumbers(cur, total){
  var set = {};
  [1, total, cur, cur-1, cur-2, cur+1, cur+2].forEach(function(p){ if (p>=1 && p<=total) set[p]=1; });
  var arr = Object.keys(set).map(Number).sort(function(a,b){ return a-b; });
  var out = [], prev = 0;
  arr.forEach(function(p){
    if (prev && p - prev > 1) out.push('...');
    out.push(p); prev = p;
  });
  return out;
}
function paginationHtml(page, pages, fn){
  if (!pages || pages <= 1) return '';
  var nums = pageNumbers(page, pages).map(function(p){
    if (p === '...') return '<span class="pg-gap">…</span>';
    return '<button class="pg-num'+(p===page?' active':'')+'" onclick="'+fn+'('+p+')">'+p+'</button>';
  }).join('');
  var prev = '<button class="pg-arrow"'+(page<=1?' disabled':'')+' onclick="'+fn+'('+(page-1)+')" aria-label="Oldingi">‹</button>';
  var next = '<button class="pg-arrow"'+(page>=pages?' disabled':'')+' onclick="'+fn+'('+(page+1)+')" aria-label="Keyingi">›</button>';
  return '<div class="pagination">'+prev+nums+next+'</div>';
}

// ── Kategoriya / janr ko'rinishi (sahifalangan to'r) ─────────────────────────
function loadFilters(){
  if (FILTERS) return Promise.resolve(FILTERS);
  return fetch('/api/filters').then(function(r){return r.json();})
    .then(function(d){ FILTERS = d || {years:[],languages:[],qualities:[]}; return FILTERS; })
    .catch(function(){ FILTERS = {years:[],languages:[],qualities:[]}; return FILTERS; });
}
function filterBarHtml(){
  var f = FILTERS || {years:[],languages:[],qualities:[]};
  function sel(id, label, opts, cur){
    var o = '<option value="all">'+label+'</option>' + (opts||[]).map(function(v){
      return '<option value="'+esc(String(v))+'"'+(String(v)===String(cur)?' selected':'')+'>'+esc(String(v))+'</option>';
    }).join('');
    return '<select class="filter-sel" onchange="setGridFilter(\''+id+'\',this.value)">'+o+'</select>';
  }
  var active = (gridYear!=='all'||gridLang!=='all'||gridQuality!=='all');
  return '<div class="filter-bar">'
    + sel('year','📅 Yil', f.years, gridYear)
    + sel('language','🌐 Til', f.languages, gridLang)
    + sel('quality','🎞 Sifat', f.qualities, gridQuality)
    + (active ? '<button class="filter-clear" onclick="clearGridFilters()">✕ Tozalash</button>' : '')
    + '</div>';
}
function setGridFilter(key, val){
  if (key==='year') gridYear = val;
  else if (key==='language') gridLang = val;
  else if (key==='quality') gridQuality = val;
  gridPage = 1; loadGrid();
}
function clearGridFilters(){ gridYear='all'; gridLang='all'; gridQuality='all'; gridPage=1; loadGrid(); }

async function loadGrid(){
  const rows = document.getElementById('rows');
  if (heroTimer){ clearInterval(heroTimer); heroTimer = null; }
  if (typeof setHero === 'function') setHero(null);
  rows.innerHTML = skeletonGrid(18);
  await loadFilters();
  var params = { page: gridPage, sort: gridSort };
  if (curType === 'top'){ params.rated = 1; }            // Eng yaxshilar — faqat reytingli kinolar
  else if (curType !== 'all'){ params.type = curType; }
  if (curGenre !== 'all') params.genre = curGenre;
  if (gridYear !== 'all') params.year = gridYear;
  if (gridLang !== 'all') params.language = gridLang;
  if (gridQuality !== 'all') params.quality = gridQuality;
  try {
    const d = await fetchMovies(params);
    if (d.error){ rows.innerHTML = '<div class="state-msg">⚠️ Xato: '+esc(d.error)+'</div>'; return; }
    const movies = d.movies || [];
    allMovies = movies;
    if (!movies.length){ rows.innerHTML = '<div class="state-msg">😕 Hech narsa topilmadi.</div>'; return; }
    var title;
    if (curType === 'top') title = '⭐ Eng yaxshilar' + (curGenre !== 'all' ? (' · ' + esc(curGenre)) : '');
    else if (curGenre !== 'all') title = '🎭 ' + esc(curGenre);
    else title = ({movie:'🎬 Kinolar', series:'📺 Seriallar', anime:'🌸 Anime', cartoon:'🧸 Multfilmlar'}[curType] || 'Natijalar');
    var html = '<div class="sort-bar">'
      + '<span class="sort-label">Saralash:</span>'
      + '<button class="sort-btn '+(gridSort==='new'?'active':'')+'" onclick="setGridSort(\'new\')">🆕 Yangi</button>'
      + '<button class="sort-btn '+(gridSort==='popular'?'active':'')+'" onclick="setGridSort(\'popular\')">🔥 Mashhur</button>'
      + '<button class="sort-btn '+(gridSort==='rating'?'active':'')+'" onclick="setGridSort(\'rating\')">⭐ Reyting</button>'
      + '</div>';
    html += filterBarHtml();
    html += '<div class="row-header" style="margin-bottom:14px;"><span class="row-title">'+title
      + ' <span class="row-count">'+(d.total||movies.length)+'</span></span></div>';
    html += '<div class="cards">' + movies.map(cardHtml).join('') + '</div>';
    html += paginationHtml(d.page || gridPage, d.pages || 1, 'gotoGridPage');
    rows.innerHTML = html;
    fadeIn(rows);
  } catch(e){
    rows.innerHTML = '<div class="state-msg">❌ Yuklashda xatolik.</div>';
  }
}
function setGridSort(s){ gridSort = s; gridPage = 1; loadGrid(); }
function gotoGridPage(p){
  if (p < 1) return;
  gridPage = p;
  loadGrid();
  var g = document.getElementById('genres');
  var y = g ? (g.getBoundingClientRect().top + window.scrollY - 80) : 0;
  window.scrollTo({ top: Math.max(0, y), behavior: 'smooth' });
}

// Asosiy yuklash — bir nechta qator
async function loadHome(){
  const rows = document.getElementById('rows');
  // Janr panelini "Tez orada" rejimida yashiramiz
  var gbar = document.getElementById('genres');
  if (gbar) gbar.style.display = (curType === 'soon') ? 'none' : '';
  // Hero bannerni faqat bosh sahifada ko'rsatamiz (kategoriya/janr/top/soon/fav'da yashiramiz)
  var heroEl = document.querySelector('.hero');
  if (heroEl) heroEl.style.display = (curType === 'all' && curGenre === 'all') ? '' : 'none';
  // "Tez orada" rejimi — alohida ko'rinish
  if (curType === 'soon'){
    if (heroTimer){ clearInterval(heroTimer); heroTimer = null; }
    if (typeof setHero === 'function') setHero(null);
    rows.innerHTML = skeletonSoon();
    await loadUpcoming(rows);
    return;
  }
  rows.innerHTML = skeletonHome();
  try {
    // Sevimlilar rejimi
    if (curType === 'fav') {
      const favs = getFavs();
      if (!favs.length) {
        const hint = ME.logged_in
          ? 'Kino kartasidagi yurakcha ❤ tugmasini bosing — botda saqlaganlaringiz ham shu yerda ko\'rinadi.'
          : (BOT ? 'Kino kartasidagi yurakcha ❤ tugmasini bosing. Telegram orqali kirsangiz, botdagi saqlanganlar ham shu yerga qo\'shiladi.'
                 : 'Kino kartasidagi yurakcha ❤ tugmasini bosing.');
        rows.innerHTML =
          '<div class="fav-empty">' +
            '<div class="fav-empty-ic">💛</div>' +
            '<div class="fav-empty-t">Sevimlilar ro\'yxati bo\'sh</div>' +
            '<div class="fav-empty-s">' + hint + '</div>' +
            '<button class="fav-empty-btn" onclick="setType(\'all\')">Kinolarni ko\'rish</button>' +
          '</div>';
        if (typeof setHero==='function') setHero(null);
        return;
      }
      const res = await fetchMovies({ ids: favs.join(',') });
      const movies = (res.movies||[]);
      allMovies = movies;
      if (typeof setHero==='function') setHero(null);
      const title = '❤ Sevimlilar <span class="row-count">' + movies.length + '</span>';
      rows.innerHTML = rowHtml(title, movies.map(cardHtml).join(''));
      return;
    }
    // Kategoriya yoki janr tanlangan bo'lsa — sahifalangan to'r (page raqamlari bilan)
    if (curType !== 'all' || curGenre !== 'all'){
      await loadGrid();
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

    // #1 Ko'rishni davom ettiring (faqat "Yangi" + Barchasi rejimida, tepada)
    if (curSort === 'new' && curType === 'all' && curGenre === 'all') {
      const recent = getRecent();
      if (recent.length) {
        html += rowHtml('▶ Ko\'rishni davom ettiring', recent.map(cardHtml).join(''));
      }
    }

    html += rowHtml(listTitle, sorted.slice(0, 10).map(cardHtml).join(''));

    // Qo'shimcha qatorlar — faqat "Yangi" saralash + Barchasi rejimida
    if (curSort === 'new' && curType === 'all' && curGenre === 'all') {
      // 🔥 Trend — butun katalogdan eng ko'p ko'rilganlar (faqat yuklangan sahifadan emas)
      const trendData = await fetchMovies({ sort: 'popular', page: 1 });
      const trend = (trendData.movies || []).filter(m => (m.views||0) > 0).slice(0, 10);
      if (trend.length) {
        html += rowHtml('🔥 Trend — eng ko\'p ko\'rilgan', trend.map((m,i)=>top10Html(m,i+1)).join(''), true);
      }
      for (const [t, label] of [['movie','🎬 Kinolar'],['series','📺 Seriallar'],['anime','🌸 Anime'],['cartoon','🧸 Multfilmlar']]) {
        const sub = movies.filter(m=>m.type===t);
        if (sub.length) html += rowHtml(label, sub.slice(0,12).map(cardHtml).join(''));
      }
      // #2 Janr bo'yicha qatorlar (eng ko'p uchragan 4 ta janr)
      const gcount = {};
      movies.forEach(m => (m.genre||'').split(',').map(s=>s.trim()).filter(Boolean)
        .forEach(g => { gcount[g] = (gcount[g]||0) + 1; }));
      const topGenres = Object.keys(gcount).sort((a,b)=>gcount[b]-gcount[a]).slice(0,4);
      for (const g of topGenres) {
        const sub = movies.filter(m => (m.genre||'').toLowerCase().includes(g.toLowerCase()));
        if (sub.length >= 2) html += rowHtml('🎭 ' + g, sub.slice(0,12).map(cardHtml).join(''));
      }
    }
    rows.innerHTML = html;
    fadeIn(rows);
    injectInlineAd();
    // Bosh sahifada "Tez orada" qatorini qo'shamiz (faqat Yangi + Barchasi rejimida)
    if (curSort === 'new' && curType === 'all' && curGenre === 'all') {
      injectUpcomingTeaser();
    }
  } catch(e){
    rows.innerHTML = '<div class="state-msg">❌ Yuklashda xatolik.</div>';
  }
}

// ── "Tez orada" (kutilayotgan kinolar) ──────────────────────────────────────
async function loadUpcoming(rows){
  try {
    const r = await fetch('/api/upcoming');    const d = await r.json();
    const items = d.items || [];
    var html = '<div class="soon-head">'
      + '<h2 class="soon-title">🔜 Tez orada</h2>'
      + '<p class="soon-sub">Hali qo\'shilmagan, lekin tez orada chiqadigan kinolar. '
      + '<b>🔔 Xabar ber</b> tugmasini bossangiz — kino qo\'shilishi bilan Telegram botdan birinchi bo\'lib xabar olasiz.</p>'
      + (BOT ? ('<p style="font-size:12.5px;color:#9c97c8;margin:8px 0 0;">⚠️ Xabar kelishi uchun avval <a href="https://t.me/'+BOT+'" target="_blank" rel="noopener" style="color:#5ad1ff;">botni ishga tushiring</a> (Telegramда «Start» bosing).</p>') : '')
      + '</div>';
    html += '<div class="soon-request">'
      + '<input id="soonReqInput" class="soon-req-input" maxlength="200" '
      + 'placeholder="Qaysi kinoni qo\'shishimizni xohlaysiz?" onkeydown="if(event.key===\'Enter\')requestUpcoming()">'
      + '<button class="soon-req-btn" onclick="requestUpcoming()">So\'rash</button>'
      + '</div><div id="soonReqMsg" class="soon-req-msg"></div>';
    if (!items.length){
      html += '<div class="state-msg">Hozircha kutilayotgan kino yo\'q. Birinchi bo\'lib so\'rang! 👆</div>';
    } else {
      html += '<div class="soon-grid">';
      items.forEach(function(it){ html += upcomingCard(it); });
      html += '</div>';
    }
    rows.innerHTML = html;
    fadeIn(rows);
  } catch(e){
    rows.innerHTML = '<div class="state-msg">❌ Yuklashda xatolik.</div>';
  }
}
function upcomingCard(it){
  var p = it.poster_url ? '<img src="'+esc(it.poster_url)+'" loading="lazy" onerror="this.parentNode.innerHTML=\'<div class=\\\'soon-ph\\\'>🎬</div>\'">' : '<div class="soon-ph">🎬</div>';
  var subbed = !!it.subscribed;
  var btn = '<button class="soon-bell'+(subbed?' on':'')+'" onclick="toggleUpcoming('+it.id+',this)">'
    + (subbed ? '✅ Kuzatilmoqda' : '🔔 Xabar ber') + '</button>';
  var cnt = it.subs ? '<span class="soon-cnt">👥 '+it.subs+' kishi kutmoqda</span>' : '<span class="soon-cnt soon-cnt-0">Birinchi bo\'lib kuting</span>';
  return '<div class="soon-card">'
    + '<div class="soon-poster">'+p+'<span class="soon-badge">Tez orada</span></div>'
    + '<div class="soon-body"><div class="soon-name">'+esc(it.title)+'</div>'
    + (it.note?'<div class="soon-note">'+esc(it.note)+'</div>':'')
    + '<div class="soon-foot">'+cnt+btn+'</div>'
    + '</div></div>';
}
function toggleUpcoming(id, el){
  if (!ME.logged_in){ openLogin(); return; }
  var on = el.classList.contains('on');
  el.disabled = true;
  fetch('/api/upcoming/'+id+'/subscribe', { method: on ? 'DELETE' : 'POST' })
    .then(function(r){return r.json();}).then(function(d){
      el.disabled = false;
      if (d.logged_in === false){ openLogin(); return; }
      if (d.ok){
        el.classList.toggle('on', d.subscribed);
        el.textContent = d.subscribed ? '✅ Kuzatilmoqda' : '🔔 Xabar ber';
      }
    }).catch(function(){ el.disabled = false; });
}
function requestUpcoming(){
  if (!ME.logged_in){ openLogin(); return; }
  var inp = document.getElementById('soonReqInput');
  var msg = document.getElementById('soonReqMsg');
  var t = ((inp && inp.value) || '').trim();
  if(!t){ if(inp) inp.focus(); return; }
  fetch('/api/upcoming/request', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ title: t })
  }).then(function(r){return r.json();}).then(function(d){
    if (d.logged_in === false){ openLogin(); return; }
    if (d.ok){
      if(inp) inp.value='';
      if(msg){ msg.textContent='✅ So\'rovingiz qabul qilindi! Kino qo\'shilganda botdan xabar beramiz.'; msg.className='soon-req-msg ok'; }
    } else if (msg){ msg.textContent='❌ '+(d.error||'Xato'); msg.className='soon-req-msg err'; }
  }).catch(function(){ if(msg){ msg.textContent='❌ Server bilan aloqa yo\'q.'; msg.className='soon-req-msg err'; } });
}
function injectUpcomingTeaser(){
  fetch('/api/upcoming').then(function(r){return r.json();}).then(function(d){
    var items = (d.items||[]).slice(0,12);
    if (!items.length) return;
    if (curType!=='all' || curGenre!=='all' || curSort!=='new') return;
    var rowsEl = document.getElementById('rows');
    if (!rowsEl) return;
    var cards = items.map(function(it){
      var p = it.poster_url ? '<img src="'+esc(it.poster_url)+'" loading="lazy" onerror="this.remove()">' : '';
      return '<div class="card soon-mini" onclick="setType(\'soon\')">'
        + '<div class="card-img">'+p+'<span class="soon-badge">Tez orada</span>'
        + '<span class="card-name">'+esc(it.title)+'</span></div></div>';
    }).join('');
    var row = document.createElement('div');
    row.className = 'row';
    row.innerHTML = '<div class="row-header"><span class="row-title">🔜 Tez orada</span>'
      + '<a class="row-more" href="javascript:void(0)" onclick="setType(\'soon\')">Barchasi →</a></div>'
      + '<div class="cards">'+cards+'</div>';
    var sb = rowsEl.querySelector('.sort-bar');
    if (sb && sb.nextSibling) rowsEl.insertBefore(row, sb.nextSibling);
    else if (sb) rowsEl.appendChild(row);
    else rowsEl.insertBefore(row, rowsEl.firstChild);
  }).catch(function(){});
}

function setSort(s){ curSort = s; loadHome(); }

// Bilinar-bilinmas inline reklama — qatorlar orasiga nozik banner qo'yadi
function injectInlineAd(){
  try {
    fetch('/api/ad').then(function(r){return r.json();}).then(function(d){
      var ad = d && d.ad; var rowsEl = document.getElementById('rows');
      if (!ad || !rowsEl) return;
      if (rowsEl.querySelector('.inline-ad-row')) return;  // ikki marta qo'ymaymiz
      var inner;
      if (ad.image_url) {
        inner = '<img src="'+esc(ad.image_url)+'" alt="" loading="lazy" style="width:58px;height:40px;object-fit:cover;border-radius:6px;flex:0 0 auto;">'
          + '<div style="min-width:0;font-size:13.5px;color:#cfcae8;line-height:1.4;">'+esc(ad.title)+(ad.link?' <span style="color:#9b93c4;">— Batafsil →</span>':'')+'</div>';
      } else {
        inner = '<div style="width:30px;height:30px;flex:0 0 auto;border-radius:7px;background:rgba(124,92,255,0.18);display:flex;align-items:center;justify-content:center;font-size:15px;">📢</div>'
          + '<div style="min-width:0;font-size:13.5px;color:#cfcae8;line-height:1.4;">'+esc(ad.title)+(ad.link?' <span style="color:#9b93c4;">— Batafsil →</span>':'')+'</div>';
      }
      var style = 'display:flex;align-items:center;gap:12px;background:rgba(124,92,255,0.06);border:1px solid rgba(124,92,255,0.18);border-radius:10px;padding:10px 14px;text-decoration:none;position:relative;';
      var label = '<span style="position:absolute;top:5px;right:9px;font-size:9px;color:#7a7398;text-transform:uppercase;letter-spacing:0.5px;">Reklama</span>';
      var box = ad.link
        ? '<a href="'+esc(ad.link)+'" target="_blank" rel="noopener nofollow sponsored" style="'+style+'">'+label+inner+'</a>'
        : '<div style="'+style+'">'+label+inner+'</div>';
      var row = document.createElement('div');
      row.className = 'row inline-ad-row';
      row.style.cssText = 'margin:18px 0;';
      row.innerHTML = box;
      // 2-qatordan keyin joylashtiramiz (kontent ichida, bilinar-bilinmas)
      var allRows = rowsEl.querySelectorAll(':scope > .row');
      if (allRows.length >= 2) rowsEl.insertBefore(row, allRows[1].nextSibling);
      else rowsEl.appendChild(row);
    }).catch(function(){});
  } catch(e) {}
}

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
  gridPage = 1; gridSort = (t === 'top') ? 'rating' : 'new';
  gridYear = 'all'; gridLang = 'all'; gridQuality = 'all';
  document.querySelectorAll('.genre-pill').forEach((p,i)=>p.classList.toggle('active', i===0));
  document.querySelectorAll('[data-nav]').forEach(a=>a.classList.toggle('nav-active', a.getAttribute('data-nav')===t));
  window.scrollTo({top:0,behavior:'smooth'});
  loadHome();
}
function setGenre(g, el){
  curGenre = g;
  gridPage = 1;
  document.querySelectorAll('.genre-pill').forEach(p=>p.classList.remove('active'));
  if (el) el.classList.add('active');
  loadHome();
}

// Qidiruv
function openSearch(){ document.getElementById('searchOverlay').classList.add('open'); setTimeout(()=>document.getElementById('searchInput').focus(),100); }
function closeSearch(){ document.getElementById('searchOverlay').classList.remove('open'); document.getElementById('searchResults').innerHTML=''; document.getElementById('searchInput').value=''; }
async function doSearch(){
  const q = document.getElementById('searchInput').value.trim();
  if (!q) return;
  searchQ = q;
  searchPage = 1;
  renderSearch(false);
}
function gotoSearchPage(p){
  if (p < 1) return;
  searchPage = p;
  renderSearch(true);
}
async function renderSearch(scroll){
  const box = document.getElementById('searchResults');
  if (!searchQ) return;
  box.innerHTML = skeletonGrid(12);
  try {
    const d = await fetchMovies({ q: searchQ, page: searchPage });
    if (!d.movies || !d.movies.length){ box.innerHTML = '<div class="state-msg">Topilmadi</div>'; return; }
    box.innerHTML = '<div class="cards">' + d.movies.map(cardHtml).join('') + '</div>'
      + paginationHtml(d.page || searchPage, d.pages || 1, 'gotoSearchPage');
    fadeIn(box);
    if (scroll){
      var so = document.getElementById('searchOverlay');
      if (so) so.scrollTo({ top: 0, behavior: 'smooth' });
    }
  } catch(e){ box.innerHTML = '<div class="state-msg">Xato</div>'; }
}

// Modal
function ytId(s){
  if(!s) return '';
  s=String(s).trim();
  if(/^[A-Za-z0-9_-]{11}$/.test(s)) return s;
  var m=s.match(/(?:youtu\.be\/|youtube\.com\/(?:watch\?v=|embed\/|v\/|shorts\/))([A-Za-z0-9_-]{11})/);
  return m?m[1]:'';
}
function playTrailer(el){
  var id=el.getAttribute('data-yt'); if(!id) return;
  el.innerHTML='<iframe src="https://www.youtube-nocookie.com/embed/'+id+'?autoplay=1&rel=0" title="Treyler" frameborder="0" allow="autoplay; encrypted-media; fullscreen" allowfullscreen></iframe>';
  el.onclick=null;
}
// ── Izohlar (fikr bildirish) ──
var _revStars = 0;
function loadReviews(mid){
  var box = document.getElementById('mmReviews');
  if(!box) return;
  box.innerHTML = '<div style="color:#777;font-size:13px;padding:10px 0;">Izohlar yuklanmoqda...</div>';
  fetch('/api/reviews/'+mid).then(function(r){return r.json();}).then(function(d){
    renderReviews(box, mid, d);
  }).catch(function(){ box.innerHTML=''; });
}
function renderReviews(box, mid, d){
  _revStars = 0;
  var html = '<div class="rev-head">💬 Izohlar ('+(d.count||0)+')</div>';
  if (d.logged_in){
    html += '<div class="rev-form">'
      + '<div class="rev-stars" id="revStars">'
      + [1,2,3,4,5].map(function(n){return '<span data-n="'+n+'" onclick="setRevStar('+n+')">☆</span>';}).join('')
      + '</div>'
      + '<textarea id="revText" maxlength="1000" placeholder="Bu kino haqida fikringiz..."></textarea>'
      + '<button class="rev-send" onclick="submitReview('+mid+')">Yuborish</button>'
      + '</div>';
  } else {
    html += '<div class="rev-login">Izoh qoldirish uchun <a href="#" onclick="openLogin();return false;">Telegram orqali kiring</a>.</div>';
  }
  if (d.reviews && d.reviews.length){
    html += '<div class="rev-list">';
    d.reviews.forEach(function(rv){ html += revItemHtml(rv, mid, false); });
    html += '</div>';
  } else {
    html += '<div class="rev-empty">Hali izoh yo\'q. Birinchi bo\'lib fikr bildiring!</div>';
  }
  box.innerHTML = html;
}
// Bitta izoh (yoki javob) HTML'i
function revItemHtml(rv, mid, isReply){
  var stars = (rv.rating && !isReply) ? '<span class="rev-rate">'+'★'.repeat(rv.rating)+'☆'.repeat(5-rv.rating)+'</span>' : '';
  var av = rv.photo ? '<img src="'+esc(rv.photo)+'" onerror="this.style.display=\'none\'">' : '<div class="rev-av-ph">'+esc((rv.name||'?').charAt(0).toUpperCase())+'</div>';
  var del = rv.mine ? '<button class="rev-del" onclick="delReview('+rv.id+','+mid+')">🗑</button>' : '';
  var replyTo = (isReply && rv.reply_to) ? '<span class="rev-replyto">↪ '+esc(rv.reply_to)+'</span>' : '';
  var h = '<div class="rev-item'+(isReply?' rev-reply':'')+'"><div class="rev-av">'+av+'</div>'
    + '<div class="rev-main"><div class="rev-top"><b>'+esc(rv.name)+'</b> '+stars+replyTo
    + '<span class="rev-date">'+esc(rv.date)+'</span>'+del+'</div>'
    + '<div class="rev-text">'+esc(rv.text)+'</div>';
  if (!isReply){
    if (ME.logged_in){
      h += '<div class="rev-actions"><button class="rev-reply-btn" onclick="toggleReplyForm('+rv.id+')">↩ Javob berish</button></div>'
        + '<div class="rev-reply-form" id="replyForm'+rv.id+'" style="display:none;">'
        + '<textarea id="replyText'+rv.id+'" maxlength="1000" placeholder="Javobingiz..."></textarea>'
        + '<div class="rev-reply-row">'
        + '<button class="rev-send sm" onclick="submitReply('+rv.id+','+mid+')">Yuborish</button>'
        + '<button class="rev-cancel" onclick="toggleReplyForm('+rv.id+')">Bekor</button>'
        + '</div></div>';
    }
    if (rv.replies && rv.replies.length){
      h += '<div class="rev-replies">'
        + rv.replies.map(function(rp){ return revItemHtml(rp, mid, true); }).join('')
        + '</div>';
    }
  }
  h += '</div></div>';
  return h;
}
function toggleReplyForm(rid){
  var f = document.getElementById('replyForm'+rid);
  if(!f) return;
  var open = (f.style.display === 'none' || !f.style.display);
  f.style.display = open ? 'block' : 'none';
  if (open){ var t = document.getElementById('replyText'+rid); if(t) t.focus(); }
}
function submitReply(parentId, mid){
  var el = document.getElementById('replyText'+parentId);
  var t = ((el && el.value) || '').trim();
  if(!t) return;
  fetch('/api/reviews', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ movie_id: mid, text: t, parent_id: parentId })
  }).then(function(r){return r.json();}).then(function(d){
    if(d.ok){ loadReviews(mid); }
    else if(d.logged_in===false){ openLogin(); }
    else if(d.error){ toast(d.error, true); }
  }).catch(function(){});
}
function setRevStar(n){
  _revStars = n;
  var el = document.getElementById('revStars');
  if(!el) return;
  el.querySelectorAll('span').forEach(function(s){
    s.textContent = parseInt(s.getAttribute('data-n')) <= n ? '★' : '☆';
  });
}
function submitReview(mid){
  var t = (document.getElementById('revText')||{}).value || '';
  t = t.trim();
  if(!t) return;
  fetch('/api/reviews', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ movie_id: mid, text: t, rating: _revStars })
  }).then(function(r){return r.json();}).then(function(d){
    if(d.ok){ loadReviews(mid); }
    else if(d.logged_in===false){ openLogin(); }
    else if(d.error){ toast(d.error, true); }
  }).catch(function(){});
}
function delReview(rid, mid){
  fetch('/api/reviews/'+rid, {method:'DELETE'}).then(function(r){return r.json();}).then(function(){
    loadReviews(mid);
  }).catch(function(){});
}

async function openMovie(id){
  const modal = document.getElementById('movieModal');
  const box = document.getElementById('movieBox');
  box.innerHTML = skeletonModal();
  modal.classList.add('open'); document.body.style.overflow='hidden';
  try {
    const r = await fetch('/api/movie/'+id);
    const d = await r.json();
    if (!d.found) { box.innerHTML = '<div class="state-msg">Topilmadi</div>'; return; }
    const m = d.movie;
    pushRecent(m);
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
        ${(m.trailer && ytId(m.trailer))?`
        <div class="mm-trailer" data-yt="${ytId(m.trailer)}" onclick="playTrailer(this)">
          <img src="https://img.youtube.com/vi/${ytId(m.trailer)}/hqdefault.jpg" alt="Treyler" loading="lazy">
          <div class="mm-play">▶</div>
          <span class="mm-trailer-lbl">🎬 Treyler</span>
        </div>`:''}
        ${m.description?`<p class="mm-desc">${esc(m.description)}</p>`:'<p class="mm-desc" style="opacity:0.5">Tavsif yo\'q.</p>'}
        <a href="${botLink}" target="_blank" class="mm-watch">
          ${isSeries?'📺 To\'liq qismlarni botda ko\'rish':'🎬 To\'liqini botda ko\'rish / yuklab olish'}
        </a>
        <button class="mm-share" onclick="shareMovie(${m.id})">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/></svg>
          Do'stga yuborish
        </button>
        <p class="mm-note">👁 ${m.views} marta ko'rilgan · Telegram bot orqali ochiladi</p>
        <div id="mmReviews" class="mm-reviews"></div>
        ${similarHtml(m)}
      </div>`;
    loadReviews(m.id);
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
document.addEventListener('keydown', e=>{ if(e.key==='Escape'){ closeShareSheet(); closeMovie(); closeSearch(); closeMenu(); } });

// ── Ulashish — "Do'stga yuborish" (Telegram / nusxa) ─────────────────────────
function shareMovie(id, title){
  if (!title){
    var h = document.querySelector('#movieBox .mm-info h2');
    title = h ? h.textContent.trim() : 'ASTRA';
  }
  var url = location.origin + '/kino/' + id;
  showShareSheet(url, title);
}
function showShareSheet(url, title){
  closeShareSheet();
  var enc = encodeURIComponent(url);
  var tg = 'https://t.me/share/url?url=' + enc + '&text=' + encodeURIComponent(title + ' — ASTRA da ko\'ring 🎬');
  var safeUrl = url.replace(/'/g, '');
  var nativeBtn = navigator.share
    ? '<button class="share-opt" onclick="nativeShare(this)" data-url="'+esc(safeUrl)+'" data-title="'+esc(title)+'"><span class="share-ic">📲</span> Boshqa ilovalar</button>'
    : '';
  var bg = document.createElement('div');
  bg.className = 'share-sheet-bg';
  bg.id = 'shareSheetBg';
  bg.onclick = function(e){ if (e.target === bg) closeShareSheet(); };
  bg.innerHTML =
    '<div class="share-sheet">'
    + '<div class="share-title">📤 Do\'stga yuborish</div>'
    + '<div class="share-name">' + esc(title) + '</div>'
    + '<div class="share-url" id="shareUrlText">' + esc(url) + '</div>'
    + '<div class="share-opts">'
    +   '<a class="share-opt tg" href="' + tg + '" target="_blank" rel="noopener" onclick="closeShareSheet()"><span class="share-ic">✈️</span> Telegram</a>'
    +   '<button class="share-opt copy" onclick="copyShareLink(\'' + safeUrl + '\', this)"><span class="share-ic">🔗</span> Havoladan nusxa</button>'
    +   nativeBtn
    + '</div>'
    + '<button class="share-cancel" onclick="closeShareSheet()">Yopish</button>'
    + '</div>';
  document.body.appendChild(bg);
  requestAnimationFrame(function(){ bg.classList.add('show'); });
}
function closeShareSheet(){
  var s = document.getElementById('shareSheetBg');
  if (!s) return;
  s.classList.remove('show');
  setTimeout(function(){ if (s.parentNode) s.parentNode.removeChild(s); }, 220);
}
function copyShareLink(url, btn){
  function done(){
    if (!btn) return;
    var old = btn.innerHTML;
    btn.innerHTML = '<span class="share-ic">✅</span> Nusxa olindi!';
    btn.classList.add('done');
    setTimeout(function(){ btn.innerHTML = old; btn.classList.remove('done'); }, 1600);
  }
  if (navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(url).then(done).catch(function(){ _fallbackCopy(url); done(); });
  } else { _fallbackCopy(url); done(); }
}
function _fallbackCopy(text){
  try {
    var t = document.createElement('textarea');
    t.value = text; t.style.position = 'fixed'; t.style.opacity = '0';
    document.body.appendChild(t); t.focus(); t.select();
    document.execCommand('copy'); document.body.removeChild(t);
  } catch(e){}
}
function nativeShare(btn){
  var url = btn.getAttribute('data-url'), title = btn.getAttribute('data-title');
  if (navigator.share){ navigator.share({ title: title, text: title, url: url }).catch(function(){}); }
  closeShareSheet();
}

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

// "Yuqoriga" tugmasi
(function(){
  var b = document.createElement('button');
  b.className = 'to-top';
  b.setAttribute('aria-label', 'Yuqoriga');
  b.innerHTML = '↑';
  b.onclick = function(){ window.scrollTo({top:0, behavior:'smooth'}); };
  document.body.appendChild(b);
  window.addEventListener('scroll', function(){
    if (window.scrollY > 500) b.classList.add('show'); else b.classList.remove('show');
  }, {passive:true});
})();

// Google qidiruv qutisi / ulashilgan havola: astramovie.com/?q=...
(function(){
  try {
    var qp = new URLSearchParams(window.location.search).get('q');
    if (qp) {
      openSearch();
      var inp = document.getElementById('searchInput');
      if (inp) inp.value = qp;
      doSearch();
    }
  } catch(e){}
})();

// ── Qidiruvda avtomatik to'ldirish (live takliflar) ──────────────────────────
(function(){
  var inp = document.getElementById('searchInput');
  var wrap = inp ? inp.closest('.search-input-wrap') : null;
  if (!inp || !wrap) return;

  // CSS — faqat app.js o'zgarsin uchun JS orqali qo'shamiz
  var css = ''
    + '.search-suggest{position:absolute;top:100%;left:0;right:0;margin-top:8px;'
    + 'background:#161430;border:1px solid #2a2750;border-radius:12px;overflow:hidden auto;'
    + 'box-shadow:0 14px 44px rgba(0,0,0,.55);z-index:5;display:none;max-height:62vh;}'
    + '.search-suggest.show{display:block;}'
    + '.ss-item{display:flex;align-items:center;gap:12px;padding:9px 12px;cursor:pointer;'
    + 'border-bottom:1px solid rgba(255,255,255,.05);transition:background .12s;}'
    + '.ss-item:last-child{border-bottom:none;}'
    + '.ss-item:hover,.ss-item.active{background:rgba(124,92,255,.18);}'
    + '.ss-poster{width:38px;height:54px;border-radius:6px;background:#252154;flex:0 0 auto;'
    + 'display:flex;align-items:center;justify-content:center;overflow:hidden;font-size:18px;}'
    + '.ss-poster img{width:100%;height:100%;object-fit:cover;}'
    + '.ss-main{min-width:0;flex:1;}'
    + '.ss-title{font-size:15px;color:#fff;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}'
    + '.ss-meta{font-size:12px;color:#8c87b8;margin-top:2px;}'
    + '.ss-empty{padding:15px 16px;color:#8c87b8;font-size:14px;}'
    + '.ss-foot{padding:11px 14px;font-size:13.5px;color:#b6a4ff;cursor:pointer;text-align:center;'
    + 'border-top:1px solid rgba(255,255,255,.06);background:rgba(124,92,255,.06);}'
    + '.ss-foot:hover{background:rgba(124,92,255,.14);}';
  var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  var box = document.createElement('div');
  box.className = 'search-suggest';
  box.id = 'searchSuggest';
  wrap.appendChild(box);

  var timer = null, items = [], active = -1, lastQ = '';
  function hide(){ box.classList.remove('show'); active = -1; }
  function show(){ box.classList.add('show'); }
  function typeUz(t){ return ({movie:'Kino',series:'Serial',anime:'Anime',cartoon:'Multfilm'})[t] || 'Kino'; }

  function render(list){
    items = list; active = -1;
    if (!list.length){ box.innerHTML = '<div class="ss-empty">Hech narsa topilmadi 😕</div>'; show(); return; }
    var html = list.map(function(m, i){
      var p = posterUrl(m);
      var pos = '<div class="ss-poster">' + (p
        ? '<img src="'+esc(p)+'" loading="lazy" onerror="this.remove()">'
        : '🎬') + '</div>';
      var meta = [typeUz(m.type), m.year||'', (m.rating?('⭐ '+(+m.rating).toFixed(1)):'')].filter(Boolean).join(' · ');
      return '<div class="ss-item" data-i="'+i+'" onclick="pickSuggest('+m.id+')">'+pos
        + '<div class="ss-main"><div class="ss-title">'+esc(m.title)+'</div>'
        + '<div class="ss-meta">'+esc(meta)+'</div></div></div>';
    }).join('');
    html += '<div class="ss-foot" onclick="searchAll()">Barcha natijalarni ko\'rish →</div>';
    box.innerHTML = html; show();
  }

  function fetchSuggest(q){
    fetchMovies({ q: q, page: 1 }).then(function(d){
      if (lastQ !== q) return;            // eskirgan so'rov natijasini tashlaymiz
      render((d.movies||[]).slice(0, 8));
    }).catch(function(){});
  }

  inp.addEventListener('input', function(){
    var q = inp.value.trim();
    lastQ = q;
    clearTimeout(timer);
    if (q.length < 2){ hide(); return; }
    timer = setTimeout(function(){ fetchSuggest(q); }, 200);
  });

  // ↑ ↓ Enter Esc bilan boshqarish (inline onkeydown'ni almashtiramiz)
  inp.onkeydown = function(e){
    var open = box.classList.contains('show') && items.length;
    if (e.key === 'ArrowDown' && open){ e.preventDefault(); setActive(active + 1); }
    else if (e.key === 'ArrowUp' && open){ e.preventDefault(); setActive(active - 1); }
    else if (e.key === 'Enter'){
      if (open && active >= 0 && items[active]){ e.preventDefault(); pickSuggest(items[active].id); }
      else { hide(); doSearch(); }
    } else if (e.key === 'Escape'){ hide(); }
  };

  function setActive(i){
    var els = box.querySelectorAll('.ss-item');
    if (!els.length) return;
    active = (i + els.length) % els.length;
    els.forEach(function(el, j){ el.classList.toggle('active', j === active); });
    if (els[active]) els[active].scrollIntoView({ block: 'nearest' });
  }

  document.addEventListener('click', function(e){
    if (!box.contains(e.target) && e.target !== inp) hide();
  });

  // Globallar
  window.pickSuggest = function(id){ hide(); if (window.closeSearch) window.closeSearch(); openMovie(id); };
  window.searchAll = function(){ hide(); doSearch(); };
  var _origCloseSearch = window.closeSearch;
  window.closeSearch = function(){ hide(); if (_origCloseSearch) _origCloseSearch(); };
})();
