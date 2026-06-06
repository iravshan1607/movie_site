let curType = 'all', curGenre = 'all', curQuery = '', curPage = 1, BOT = '';

const typeLabel = { movie: 'Kino', series: 'Serial', anime: 'Anime', cartoon: 'Multfilm' };
const typeColor = { movie: '#E50914', series: '#2BA8FF', anime: '#ff5fa2', cartoon: '#FFC940' };

function esc(s) { return String(s == null ? '' : s).replace(/[<>&"]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c])); }

// Bot username
fetch('/api/botlink').then(r => r.json()).then(d => { BOT = d.bot || ''; }).catch(() => {});

// Janrlar
fetch('/api/genres').then(r => r.json()).then(d => {
  if (d.genres && d.genres.length) {
    const row = document.getElementById('genre-row');
    row.innerHTML = '<button class="chip active" data-genre="all" onclick="setGenre(\'all\')">Barcha janr</button>' +
      d.genres.map(g => `<button class="chip" data-genre="${esc(g)}" onclick="setGenre('${esc(g)}')">${esc(g)}</button>`).join('');
  }
}).catch(() => {});

function setType(t) {
  curType = t; curPage = 1;
  document.querySelectorAll('[data-type]').forEach(b => b.classList.toggle('active', b.dataset.type === t));
  loadMovies();
}
function setGenre(g) {
  curGenre = g; curPage = 1;
  document.querySelectorAll('[data-genre]').forEach(b => b.classList.toggle('active', b.dataset.genre === g));
  loadMovies();
}
function doSearch() {
  curQuery = document.getElementById('search').value.trim(); curPage = 1;
  loadMovies();
}

async function loadMovies() {
  const grid = document.getElementById('grid');
  const status = document.getElementById('status');
  const pager = document.getElementById('pager');
  grid.innerHTML = ''; pager.innerHTML = '';
  status.innerHTML = '<div class="spinner"></div>Yuklanmoqda...';
  try {
    const params = new URLSearchParams({ type: curType, genre: curGenre, q: curQuery, page: curPage });
    const r = await fetch('/api/movies?' + params);
    const d = await r.json();
    if (!d.movies || !d.movies.length) {
      if (d.error) {
        status.innerHTML = '⚠️ Baza xatosi: ' + d.error;
      } else {
        status.innerHTML = '😕 Hech narsa topilmadi. Boshqa qidiruv yoki filtr bilan urinib ko\'ring.';
      }
      return;
    }
    status.innerHTML = '';
    grid.innerHTML = d.movies.map(m => {
      const poster = m.has_poster ? `/api/poster/${m.id}` : (m.poster_url || '');
      const posterHtml = poster
        ? `<img src="${esc(poster)}" loading="lazy" onerror="this.parentElement.innerHTML='<div class=ph>🎬</div>'">`
        : `<div class="ph">🎬</div>`;
      const tcol = typeColor[m.type] || '#E50914';
      return `<div class="movie" onclick="openMovie(${m.id})">
        <div class="poster">
          ${posterHtml}
          <span class="badge-type" style="color:${tcol}">${typeLabel[m.type] || 'Kino'}</span>
          ${m.quality ? `<span class="badge-q">${esc(m.quality)}</span>` : ''}
          <div class="poster-ov"><div class="play">▶</div></div>
        </div>
        <div class="m-title">${esc(m.title)}</div>
        <div class="m-meta">${m.year || ''} ${m.rating ? `· <span class="m-rating">★ ${m.rating}</span>` : ''}</div>
      </div>`;
    }).join('');
    // Pager
    if (d.pages > 1) {
      let html = '';
      html += `<button onclick="goPage(${curPage - 1})" ${curPage <= 1 ? 'disabled' : ''}>‹</button>`;
      const start = Math.max(1, curPage - 2), end = Math.min(d.pages, curPage + 2);
      if (start > 1) html += `<button onclick="goPage(1)">1</button>`;
      if (start > 2) html += `<button disabled>…</button>`;
      for (let i = start; i <= end; i++) html += `<button class="${i === curPage ? 'active' : ''}" onclick="goPage(${i})">${i}</button>`;
      if (end < d.pages - 1) html += `<button disabled>…</button>`;
      if (end < d.pages) html += `<button onclick="goPage(${d.pages})">${d.pages}</button>`;
      html += `<button onclick="goPage(${curPage + 1})" ${curPage >= d.pages ? 'disabled' : ''}>›</button>`;
      pager.innerHTML = html;
    }
  } catch (e) {
    status.innerHTML = '❌ Yuklashda xatolik. Internetni tekshiring.';
  }
}
function goPage(p) { curPage = p; loadMovies(); window.scrollTo({ top: 0, behavior: 'smooth' }); }

async function openMovie(id) {
  const modal = document.getElementById('movie-modal');
  const box = document.getElementById('movie-box');
  box.innerHTML = '<div class="status"><div class="spinner"></div></div>';
  modal.classList.add('open'); document.body.style.overflow = 'hidden';
  try {
    const r = await fetch('/api/movie/' + id);
    const d = await r.json();
    if (!d.found) { box.innerHTML = '<div class="status">Topilmadi</div>'; return; }
    const m = d.movie;
    const poster = m.has_poster ? `/api/poster/${m.id}` : (m.poster_url || '');
    const isSeries = m.type === 'series' || m.type === 'anime';
    // Botga havola: start parametri bilan
    const botLink = BOT ? `https://t.me/${BOT}?start=movie_${m.id}` : '#';
    const tcol = typeColor[m.type] || '#E50914';
    box.innerHTML = `
      <div class="m-hero" style="${poster ? `background-image:url('${esc(poster)}')` : ''}">
        <button class="m-close" onclick="closeMovie()">×</button>
        <div class="m-hero-content">
          ${poster ? `<img class="m-hero-poster" src="${esc(poster)}" onerror="this.outerHTML='<div class=\\'m-hero-poster ph\\'>🎬</div>'">` : `<div class="m-hero-poster ph">🎬</div>`}
          <div class="m-hero-info">
            <h2>${esc(m.title)}</h2>
            <div class="m-tags">
              <span class="m-tag r" style="background:${tcol}">${typeLabel[m.type] || 'Kino'}</span>
              ${m.year ? `<span class="m-tag">${m.year}</span>` : ''}
              ${m.quality ? `<span class="m-tag">${esc(m.quality)}</span>` : ''}
              ${m.language ? `<span class="m-tag">${esc(m.language)}</span>` : ''}
              ${m.rating ? `<span class="m-tag">★ ${m.rating}</span>` : ''}
            </div>
            ${m.genre ? `<div style="color:var(--dim);font-size:14px">${esc(m.genre)}</div>` : ''}
          </div>
        </div>
      </div>
      <div class="mbox-body">
        ${m.description ? `<p class="desc">${esc(m.description)}</p>` : '<p class="desc" style="opacity:0.6">Tavsif yo\'q.</p>'}
        <a href="${botLink}" target="_blank" class="watch-btn">
          ${isSeries ? '📺 Botda epizodlarni ko\'rish' : '▶ Botda ko\'rish / yuklab olish'}
        </a>
        <p class="watch-note">👁 ${m.views} marta ko'rilgan · Telegram bot orqali ochiladi</p>
      </div>`;
  } catch (e) {
    box.innerHTML = '<div class="status">❌ Xatolik</div>';
  }
}
function closeMovie() {
  document.getElementById('movie-modal').classList.remove('open');
  document.body.style.overflow = '';
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeMovie(); });

// Boshlang'ich yuklash
loadMovies();
