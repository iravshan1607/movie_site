/* ASTRA — to'liq ekranli reklama (interstitial).
   - Sahifa ochilganda /api/ad dan reklama oladi.
   - Butun ekranni qoplaydi; X tugmasi bir necha soniya (LOCK_SECONDS) yopiq turadi (sanoq).
   - Sessiyada faqat BIR marta ko'rsatiladi (har sahifada bezdirmaslik uchun).
   - Adaptiv: rasm bo'lsa rasm bilan, bo'lmasa toza matn.
   - To'liq himoyalangan: xato bo'lsa sayt ishiga ta'sir qilmaydi. */
(function () {
  var LOCK_SECONDS = 5;   // necha soniya yopib bo'lmaydi
  try {
    if (sessionStorage.getItem('astra_ad_shown') === '1') return;
  } catch (e) {}

  function esc(s) {
    return String(s == null ? '' : s).replace(/[<>&"]/g, function (c) {
      return ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' })[c];
    });
  }

  function show(ad) {
    if (!ad || !ad.title) return;
    try { sessionStorage.setItem('astra_ad_shown', '1'); } catch (e) {}

    var ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;z-index:99999;background:rgba(6,4,16,0.9);' +
      'display:flex;align-items:center;justify-content:center;padding:20px;' +
      '-webkit-backdrop-filter:blur(5px);backdrop-filter:blur(5px);';

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
        btn.onclick = function () { try { ov.remove(); } catch (e) {} };
      } else {
        btn.textContent = left;
      }
    }, 1000);
  }

  function init() {
    try {
      fetch('/api/ad')
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.ad) show(d.ad); })
        .catch(function () {});
    } catch (e) {}
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
