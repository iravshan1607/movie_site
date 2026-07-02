/* ASTRA — to'liq ekranli MAJBURIY reklama (interstitial).
   - Sayt ochilganda DARROV emas — bir necha soniyadan keyin chiqadi (FIRST_DELAY_MS).
   - Keyin har bir necha daqiqada qaytadan chiqadi (REPEAT_MS).
   - X tugmasi LOCK_SECONDS soniya yopiq turadi (sanoq), keyin bosiladi.
   - Vaqt sahifalar orasida saqlanadi (sessionStorage) — navigatsiyada qaytadan boshlanmaydi.
   - Adaptiv: rasm bo'lsa rasm bilan, bo'lmasa toza matn.
   - To'liq himoyalangan: istalgan xato saytga ta'sir qilmaydi. */
(function () {
  var FIRST_DELAY_MS = 15000;   // kirgandan keyin birinchi marta (~12 soniya)
  var REPEAT_MS      = 300000;  // keyin har ~3 daqiqada qaytadan
  var LOCK_SECONDS   = 5;       // X necha soniya yopiq tursin

  var isOpen = false;

  function nowMs(){ return Date.now(); }
  function getLast(){ try { return parseInt(sessionStorage.getItem('astra_ad_last') || '0', 10) || 0; } catch (e) { return 0; } }
  function setLast(t){ try { sessionStorage.setItem('astra_ad_last', String(t)); } catch (e) {} }

  function esc(s){
    return String(s == null ? '' : s).replace(/[<>&"]/g, function (c) {
      return ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' })[c];
    });
  }

  function buildAndShow(ad){
    if (!ad || !ad.title || isOpen) return;
    isOpen = true;
    setLast(nowMs());

    var ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;z-index:99999;background:rgba(6,4,16,0.93);' +
      'display:flex;align-items:center;justify-content:center;padding:20px;';

    var img = ad.image_url
      ? '<img src="' + esc(ad.image_url) + '" alt="" style="width:100%;max-height:300px;' +
        'object-fit:cover;border-radius:12px;margin-bottom:16px;display:block;">'
      : '';
    var cta = ad.link
      ? '<div style="margin-top:18px;text-align:center;"><span style="display:inline-block;' +
        'background:#7c5cff;color:#fff;padding:13px 32px;border-radius:10px;font-weight:600;' +
        'font-size:15px;">Batafsil →</span></div>'
      : '';
    var openTag = ad.link
      ? '<a href="' + esc(ad.link) + '" target="_blank" rel="noopener nofollow sponsored" ' +
        'style="text-decoration:none;color:inherit;display:block;">'
      : '<div>';
    var closeTag = ad.link ? '</a>' : '</div>';

    ov.innerHTML =
      '<div style="position:relative;max-width:430px;width:100%;background:#1a1640;' +
        'border:1px solid rgba(124,92,255,0.45);border-radius:18px;padding:24px;' +
        'box-shadow:0 24px 70px rgba(0,0,0,0.65);">' +
        '<div style="position:absolute;top:15px;left:18px;font-size:10px;color:#8a82b8;' +
          'text-transform:uppercase;letter-spacing:1px;">Reklama</div>' +
        '<button id="astraAdClose" disabled aria-label="Yopish" style="position:absolute;' +
          'top:11px;right:13px;width:34px;height:34px;border-radius:50%;border:none;' +
          'background:rgba(255,255,255,0.10);color:#9b93c4;font-size:14px;font-weight:600;' +
          'cursor:default;display:flex;align-items:center;justify-content:center;">' +
          LOCK_SECONDS + '</button>' +
        '<div style="margin-top:16px;">' +
          openTag + img +
          '<div style="font-size:17px;color:#fff;font-weight:500;line-height:1.5;text-align:center;">' +
            esc(ad.title) + '</div>' + cta + closeTag +
        '</div>' +
      '</div>';

    document.body.appendChild(ov);

    var btn = ov.querySelector('#astraAdClose');
    function close(){ try { ov.remove(); } catch (e) {} isOpen = false; }
    var left = LOCK_SECONDS;
    var timer = setInterval(function () {
      left--;
      if (left <= 0) {
        clearInterval(timer);
        btn.textContent = '✕';
        btn.disabled = false;
        btn.style.cursor = 'pointer';
        btn.style.background = 'rgba(255,255,255,0.18)';
        btn.style.color = '#fff';
        btn.onclick = close;
      } else {
        btn.textContent = left;
      }
    }, 1000);
  }

  function tick(){
    try {
      fetch('/api/ad')
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.ad) buildAndShow(d.ad); })
        .catch(function () {});
    } catch (e) {}
    // Keyingi takror — ko'rsatilsa ham, ko'rsatilmasa ham
    setTimeout(tick, REPEAT_MS);
  }

  function schedule(){
    var last = getLast();
    var delay;
    if (!last) {
      delay = FIRST_DELAY_MS;
    } else {
      var elapsed = nowMs() - last;
      delay = Math.max(REPEAT_MS - elapsed, 3000);
    }
    setTimeout(tick, delay);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', schedule);
  else schedule();
})();
