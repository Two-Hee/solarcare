/* ==========================================================================
   site.js — 솔라케어 공통 스크립트
   1) site.json 의 회사정보를 페이지에 주입
   2) 모바일 드로어 메뉴
   3) 스크롤 등장 애니메이션
   4) 현재 메뉴 활성화 표시
   5) 문의 폼 전송 처리(백엔드 없음 → Formspree 또는 메일 방식)

   ※ 모든 경로는 상대경로로 처리합니다. 사이트 주소가
     /solarcare/ → 자체도메인으로 바뀌어도 수정할 곳이 없습니다.
   ========================================================================== */
(function () {
  'use strict';

  /* ------------------------------------------------------------------
     현재 페이지 깊이에 맞는 루트 상대경로 계산
     예) /solarcare/services/index.html → "../"
     각 페이지의 <html data-root="../"> 값을 사용합니다.
     ------------------------------------------------------------------ */
  var ROOT = document.documentElement.getAttribute('data-root') || './';

  /* ==================================================================
     1) 회사정보 주입
     ================================================================== */
  var SITE = null;

  function applySiteData(data) {
    SITE = data;

    // <span data-site="tel"> → 텍스트 치환
    document.querySelectorAll('[data-site]').forEach(function (el) {
      var key = el.getAttribute('data-site');
      if (data[key]) el.textContent = data[key];
    });

    // <a data-site-href="telHref"> → href 치환
    document.querySelectorAll('[data-site-href]').forEach(function (el) {
      var key = el.getAttribute('data-site-href');
      if (data[key]) el.setAttribute('href', data[key]);
    });

    // 이메일 링크 (mailto 자동 구성)
    document.querySelectorAll('[data-site-mailto]').forEach(function (el) {
      if (data.email) el.setAttribute('href', 'mailto:' + data.email);
    });

    // 카카오 채널 — 주소가 비어 있으면 버튼을 숨김
    document.querySelectorAll('[data-site-kakao]').forEach(function (el) {
      if (data.kakaoChannel) {
        el.setAttribute('href', data.kakaoChannel);
      } else {
        el.hidden = true;
      }
    });

    // 사업자 정보 한 줄
    document.querySelectorAll('[data-site-bizline]').forEach(function (el) {
      var parts = [];
      if (data.legalName) parts.push(data.legalName);
      if (data.ceo)       parts.push('대표 ' + data.ceo);
      if (data.bizNo)     parts.push('사업자등록번호 ' + data.bizNo);
      el.textContent = parts.join(' · ');
    });

    // 저작권 연도 자동
    document.querySelectorAll('[data-site-year]').forEach(function (el) {
      el.textContent = String(new Date().getFullYear());
    });
  }

  fetch(ROOT + 'site.json', { cache: 'no-cache' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (data) { if (data) applySiteData(data); })
    .catch(function () {
      /* file:// 로 직접 열면 fetch 가 차단됩니다.
         HTML 안의 기본값이 그대로 보이므로 화면은 정상 동작합니다.
         로컬 확인은 README 의 로컬 서버 실행법을 참고하세요. */
    });

  /* ==================================================================
     2) 모바일 드로어
     ================================================================== */
  var burger = document.querySelector('.hamburger');
  var drawer = document.getElementById('drawer');

  function setDrawer(open) {
    if (!burger || !drawer) return;
    burger.setAttribute('aria-expanded', open ? 'true' : 'false');
    drawer.classList.toggle('is-open', open);
    document.body.style.overflow = open ? 'hidden' : '';
  }

  if (burger && drawer) {
    burger.addEventListener('click', function () {
      setDrawer(burger.getAttribute('aria-expanded') !== 'true');
    });
    drawer.addEventListener('click', function (e) {
      if (e.target.closest('a')) setDrawer(false);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') setDrawer(false);
    });
    window.addEventListener('resize', function () {
      if (window.innerWidth > 1000) setDrawer(false);
    });
  }

  /* ==================================================================
     3) 스크롤 등장 애니메이션
     ================================================================== */
  var targets = document.querySelectorAll('.reveal');

  if (!('IntersectionObserver' in window) ||
      window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    targets.forEach(function (el) { el.classList.add('is-visible'); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          io.unobserve(entry.target);
        }
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });

    targets.forEach(function (el, i) {
      el.style.transitionDelay = Math.min(i % 4, 3) * 70 + 'ms';
      io.observe(el);
    });

    /* 안전장치 — 어떤 이유로든 관찰이 동작하지 않는 환경에서
       콘텐츠가 계속 숨겨져 있지 않도록 3초 뒤 전부 표시합니다. */
    setTimeout(function () {
      targets.forEach(function (el) { el.classList.add('is-visible'); });
    }, 3000);
  }

  /* ==================================================================
     4) 현재 메뉴 활성화
        각 페이지 <body data-page="services"> 와
        메뉴 <a data-nav="services"> 를 매칭합니다.
     ================================================================== */
  var page = document.body.getAttribute('data-page');
  if (page) {
    document.querySelectorAll('[data-nav="' + page + '"]').forEach(function (el) {
      el.setAttribute('aria-current', 'page');
    });
  }

  /* ==================================================================
     5) 문의 폼
        - site.json 의 formEndpoint 가 있으면 그곳으로 전송
        - 없으면 입력내용을 정리해 메일 프로그램으로 넘김
     ================================================================== */
  var form = document.getElementById('inquiry-form');
  if (form) {
    var status = document.getElementById('form-status');

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (!form.reportValidity()) return;

      var fd = new FormData(form);
      var endpoint = SITE && SITE.formEndpoint;

      if (endpoint) {
        if (status) status.textContent = '전송 중입니다…';
        fetch(endpoint, { method: 'POST', body: fd, headers: { Accept: 'application/json' } })
          .then(function (r) {
            if (!r.ok) throw new Error('failed');
            form.reset();
            if (status) status.textContent = '문의가 정상적으로 접수되었습니다. 영업일 기준 1일 내 연락드립니다.';
          })
          .catch(function () {
            if (status) status.textContent = '전송에 실패했습니다. 전화 또는 이메일로 문의해 주세요.';
          });
        return;
      }

      // 폴백: 메일 프로그램으로 넘기기
      var lines = [];
      var labels = {
        name: '성명/담당자', company: '회사/발전소명', phone: '연락처', email: '이메일',
        region: '지역', capacity: '설비용량(kW)', builtYear: '준공연도', siteType: '설치유형',
        inverter: '인버터 제조사/모델', service: '문의유형', message: '증상 및 요청사항'
      };
      Object.keys(labels).forEach(function (k) {
        var v = fd.get(k);
        if (v) lines.push(labels[k] + ': ' + v);
      });

      var to = (SITE && SITE.email) || 'contact@example.com';
      var subject = '[솔라케어 문의] ' + (fd.get('company') || fd.get('name') || '');
      window.location.href = 'mailto:' + to +
        '?subject=' + encodeURIComponent(subject) +
        '&body=' + encodeURIComponent(lines.join('\n'));

      if (status) {
        status.textContent = '메일 작성 창이 열립니다. 열리지 않으면 아래 이메일로 직접 보내주세요.';
      }
    });
  }
})();
