'use strict';
(function () {
  var root = document.getElementById('root');
  var state = { period: (location.hash || '#day').slice(1), category: null, query: '', data: null, business: null, scope: 'business', businesses: [] };
  if (['day', 'week', 'month'].indexOf(state.period) < 0) state.period = 'day';
  var SYMBOLS = { THB: '฿', KZT: '₸', RUB: '₽', USD: '$', EUR: '€' };
  var CHANNELS = { line: 'LINE', whatsapp: 'WhatsApp', telegram: 'Telegram' };
  var PERIODS = [['day', 'День'], ['week', 'Неделя'], ['month', 'Месяц']];
  var timer = null;

  function h(tag, props) {
    var node = document.createElement(tag);
    Object.keys(props || {}).forEach(function (key) {
      var value = props[key];
      if (value === null || value === undefined || value === false) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.indexOf('on') === 0) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    });
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }
  function append(node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) child.forEach(function (c) { append(node, c); });
    else node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  function svg(tag, attrs) {
    var node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }
  var nf = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
  function money(value, cur) { return nf.format(Math.round(value || 0)) + ' ' + (SYMBOLS[cur] || cur || ''); }
  function shortDate(iso) { var p = String(iso).split('-'); return p[2] + '.' + p[1]; }
  function fullDate(iso) { var p = String(iso).split('-'); return p[2] + '.' + p[1] + '.' + p[0]; }

  function notice(title, text, action) {
    root.replaceChildren(h('div', { class: 'notice' }, h('h1', { text: title }), h('p', { class: 'muted', text: text }), action || null));
  }

  function kpi(title, value, sub, cls) {
    return h('div', { class: 'card s4' }, h('h2', { text: title }), h('div', { class: 'kpi ' + (cls || ''), text: value }, sub ? h('small', { text: sub }) : null));
  }

  function trendChart(data) {
    var days = data.trend || [];
    var width = 640, height = 190, padL = 8, padB = 22, padT = 8;
    var max = Math.max.apply(null, days.map(function (d) { return Math.max(d.revenue, d.expenses); }).concat([1]));
    var slot = (width - padL) / Math.max(days.length, 1);
    var bw = Math.max(3, Math.min(14, slot / 2.6));
    var chart = svg('svg', { viewBox: '0 0 ' + width + ' ' + height, width: '100%', role: 'img', 'aria-label': 'Динамика выручки и расходов' });
    days.forEach(function (d, i) {
      var x = padL + i * slot + slot / 2;
      [[d.revenue, 'var(--rev)', -bw - 1], [d.expenses, 'var(--exp)', 1]].forEach(function (b) {
        var bh = Math.max(b[0] > 0 ? 2 : 0, (b[0] / max) * (height - padB - padT));
        chart.appendChild(svg('rect', { x: x + b[2], y: height - padB - bh, width: bw, height: bh, rx: 2, fill: b[1] }, svg('title', {}, shortDate(d.date) + ': ' + money(b[0], data.currency))));
      });
      if (i % Math.ceil(days.length / 7) === 0) chart.appendChild(svg('text', { x: x, y: height - 6, 'text-anchor': 'middle' }, shortDate(d.date)));
    });
    return chart;
  }

  function paymentsBlock(data) {
    var colors = { cash: 'var(--rev)', card: '#4a6fa5', qr: 'var(--warn)' };
    var names = { cash: 'Наличные', card: 'Карта', qr: 'QR' };
    var items = (data.payments || []).filter(function (p) { return p.amount > 0; });
    var total = items.reduce(function (s, p) { return s + p.amount; }, 0);
    if (!total) return h('div', { class: 'empty', text: 'Сегодня оплат пока нет.' });
    return h('div', {},
      h('div', { class: 'bar' }, items.map(function (p) { return h('span', { style: 'width:' + (p.amount / total * 100) + '%;background:' + colors[p.label], title: names[p.label] }); })),
      h('div', { class: 'legend' }, items.map(function (p) { return h('span', {}, h('i', { style: 'background:' + colors[p.label] }), names[p.label] + ' ' + money(p.amount, data.currency)); })));
  }

  function categoriesBlock(data) {
    var cats = data.expensesByCategory || [];
    if (!cats.length) return h('div', { class: 'empty', text: 'За этот период расходов нет.' });
    return h('div', {}, cats.map(function (c) {
      return h('button', { class: 'cat', type: 'button', 'aria-pressed': state.category === c.category ? 'true' : 'false',
        onclick: function () { state.category = state.category === c.category ? null : c.category; render(); } },
        h('b', { text: c.category }), h('span', { text: money(c.amount, data.currency) + ' · ' + Math.round(c.share * 100) + '%' }),
        h('span', { class: 'track' }, h('i', { style: 'width:' + Math.max(2, c.share * 100) + '%' })));
    }));
  }

  function expensesBlock(data) {
    var rows = (data.expenses || []).filter(function (r) {
      if (state.category && r.category !== state.category) return false;
      if (!state.query) return true;
      return (r.label + ' ' + r.supplier + ' ' + r.category).toLowerCase().indexOf(state.query) >= 0;
    });
    var search = h('input', { type: 'search', placeholder: 'Поиск по расходам…', value: state.query, 'aria-label': 'Поиск по расходам',
      oninput: function (e) { state.query = e.target.value.trim().toLowerCase(); renderExpenseTable(); } });
    var holder = h('div', { class: 'scroll', id: 'expense-table' });
    function renderExpenseTable() {
      var list = (data.expenses || []).filter(function (r) {
        if (state.category && r.category !== state.category) return false;
        return !state.query || (r.label + ' ' + r.supplier + ' ' + r.category).toLowerCase().indexOf(state.query) >= 0;
      });
      if (!list.length) { holder.replaceChildren(h('div', { class: 'empty', text: 'Ничего не найдено.' })); return; }
      holder.replaceChildren(h('table', {}, h('thead', {}, h('tr', {}, h('th', { text: 'Дата' }), h('th', { text: 'Расход' }), h('th', { text: 'Статья' }), h('th', { class: 'num', text: 'Сумма' }))),
        h('tbody', {}, list.slice(0, 100).map(function (r) {
          return h('tr', {}, h('td', { text: shortDate(r.date) }), h('td', {}, r.label, r.supplier && r.supplier !== r.label ? h('div', { class: 'muted', text: r.supplier }) : null), h('td', {}, h('span', { class: 'tag', text: r.category })), h('td', { class: 'num', text: money(r.amount, data.currency) }));
        }))));
    }
    renderExpenseTable();
    return h('div', {}, search, state.category ? h('p', {}, h('button', { class: 'chip', type: 'button', onclick: function () { state.category = null; render(); } }, 'Статья: ' + state.category + ' ✕')) : null, holder);
  }

  function listBlock(items, empty, mapper) {
    if (!items || !items.length) return h('div', { class: 'empty', text: empty });
    return h('ul', { class: 'plain' }, items.map(mapper));
  }

  function locationsBlock(data) {
    var rows = data.locations || [];
    if (!rows.length) return null;
    return h('div', { class: 'card' }, h('h2', { text: 'Точки' }), h('div', { class: 'scroll' }, h('table', {},
      h('thead', {}, h('tr', {}, ['Точка', 'Выручка', 'Наличные', 'Безнал', 'Выплаты', 'Отчётов'].map(function (t, i) { return h('th', { class: i ? 'num' : '', text: t }); }))),
      h('tbody', {}, rows.map(function (r) {
        return h('tr', {}, h('td', {}, h('b', { text: r.name }), r.reports ? null : h('span', { class: 'tag neg', text: ' нет отчёта' })),
          h('td', { class: 'num', text: money(r.revenue, data.currency) }), h('td', { class: 'num', text: money(r.cash, data.currency) }),
          h('td', { class: 'num', text: money(r.nonCash, data.currency) }), h('td', { class: 'num', text: money(r.payouts, data.currency) }), h('td', { class: 'num', text: String(r.reports) }));
      })))));
  }

  function accountingBlock(data) {
    var a = data.accounting;
    if (!a) return null;
    if (!a.available) return h('div', { class: 'card s6' }, h('h2', { text: 'Бухгалтерия' }), h('div', { class: 'empty', text: 'Данные временно недоступны.' }));
    var status = { sent: 'отправлен', accepted: 'принят бухгалтером' }[a.previousStatus] || 'не отправлялся';
    return h('div', { class: 'card s6' }, h('h2', { text: 'Бухгалтерия · ' + a.period }),
      h('div', { class: 'kpi', text: String(a.documents) }, h('small', { text: 'документов на ' + money(a.total, data.currency) })),
      h('p', { class: a.missing ? 'neg' : 'pos', text: a.missing ? 'Не хватает / под вопросом: ' + a.missing : 'Недостающих документов нет' }),
      a.findings && a.findings.length ? h('ul', { class: 'plain' }, a.findings.map(function (t) { return h('li', {}, h('span', { text: t })); })) : null,
      h('p', { class: 'muted', text: 'Пакет за ' + a.previousPeriod + ': ' + status }),
      a.packageUrl ? h('a', { class: 'btn', href: a.packageUrl, text: 'Скачать пакет за ' + a.previousPeriod }) : null);
  }


  function switcher() {
    if (!state.businesses || state.businesses.length < 2) return null;
    var select = h('select', { 'aria-label': 'Бизнес', class: 'chip', style: 'padding:6px 10px', onchange: function (e) {
      var value = e.target.value;
      state.category = null;
      if (value === '__all__') { state.scope = 'group'; state.business = null; }
      else { state.scope = 'business'; state.business = value; }
      load();
    } }, h('option', { value: '__all__', selected: state.scope === 'group' ? true : null, text: 'Все бизнесы' }),
      state.businesses.map(function (b) { return h('option', { value: b.id, selected: state.scope !== 'group' && (state.business || state.anchor) === b.id ? true : null, text: b.name }); }));
    return select;
  }

  function periodTabs() {
    return h('div', { class: 'tabs', role: 'tablist' }, PERIODS.map(function (p) {
      return h('button', { role: 'tab', 'aria-selected': state.period === p[0] ? 'true' : 'false', onclick: function () { state.period = p[0]; state.category = null; location.hash = p[0]; load(); }, text: p[1] });
    }));
  }

  function renderGroup() {
    var d = state.data;
    var labels = { day: 'сегодня', week: 'за 7 дней', month: 'за месяц' };
    var head = h('header', {}, h('h1', {}, 'Все бизнесы', h('small', { text: 'Группа «' + d.group + '» · ' + labels[state.period] })),
      h('div', { class: 'chips' }, switcher(), h('button', { class: 'chip', type: 'button', onclick: load, text: 'Обновить' }), h('button', { class: 'chip', type: 'button', onclick: logout, text: 'Выйти' })), periodTabs());
    var totals = (d.totalsByCurrency || []).map(function (t) {
      return h('div', { class: 'card s4' }, h('h2', { text: 'Итого · ' + t.currency }),
        h('div', { class: 'kpi', text: money(t.revenue, t.currency) }, h('small', { text: 'выручка ' + labels[state.period] })),
        h('p', { text: 'Расходы: ' + money(t.expenses, t.currency) }),
        h('p', { class: t.result >= 0 ? 'pos' : 'neg', text: 'Результат: ' + (t.result >= 0 ? '+' : '−') + money(Math.abs(t.result), t.currency) }),
        h('p', { class: 'muted', text: 'Месяц: выручка ' + money(t.monthRevenue, t.currency) + ', расходы ' + money(t.monthExpenses, t.currency) }));
    });
    var table = h('div', { class: 'card' }, h('h2', { text: 'По бизнесам' }), h('div', { class: 'scroll' }, h('table', {},
      h('thead', {}, h('tr', {}, ['Бизнес', 'Выручка', 'Расходы', 'Результат', 'За месяц'].map(function (t, i) { return h('th', { class: i ? 'num' : '', text: t }); }))),
      h('tbody', {}, (d.businesses || []).map(function (b) {
        var s = b.selected;
        return h('tr', {}, h('td', {}, h('button', { class: 'chip', type: 'button', onclick: function () { state.scope = 'business'; state.business = b.id; load(); }, text: b.name }), ' ', (b.channels || []).map(function (c) { return h('span', { class: 'tag', text: CHANNELS[c] || c }); })),
          h('td', { class: 'num', text: money(s.revenue, b.currency) }), h('td', { class: 'num', text: money(s.expenses, b.currency) }),
          h('td', { class: 'num ' + (s.result >= 0 ? 'pos' : 'neg'), text: (s.result >= 0 ? '+' : '−') + money(Math.abs(s.result), b.currency) }),
          h('td', { class: 'num', text: money(b.month.revenue, b.currency) }));
      })))));
    root.replaceChildren(head, h('div', { class: 'grid' }, totals, table), h('footer', { text: 'Суммы складываются только внутри одной валюты. Нажмите на название бизнеса, чтобы открыть его панель.' }));
  }

  function render() {
    var d = state.data;
    if (!d) return;
    if (d.totalsByCurrency) { state.businesses = (d.businesses || []).map(function (b) { return { id: b.id, name: b.name }; }); renderGroup(); return; }
    if (d.group) { state.businesses = d.group.businesses; if (!state.anchor) state.anchor = state.business || (d.group.businesses[0] && d.group.businesses[0].id); }
    var sel = d.selected || {};
    var labels = { day: 'сегодня', week: 'за 7 дней', month: 'за месяц' };
    var head = h('header', {},
      h('h1', {}, d.tenantName || 'NotiMate', h('small', { text: 'Обновлено ' + new Date(d.generatedAt).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) + ' · ' + d.timezone })),
      h('div', { class: 'chips' },
        switcher(),
        (d.channels || []).map(function (c) { return h('span', { class: 'chip', text: CHANNELS[c] || c }); }),
        d.sheetUrl ? h('a', { class: 'chip', href: d.sheetUrl, target: '_blank', rel: 'noopener noreferrer', text: 'Таблица ↗' }) : null,
        h('button', { class: 'chip', type: 'button', onclick: load, text: 'Обновить' }),
        h('button', { class: 'chip', type: 'button', onclick: logout, text: 'Выйти' })),
      periodTabs());
    var grid = h('div', { class: 'grid' },
      kpi('Выручка ' + labels[state.period], money(sel.revenue, d.currency), fullDate(sel.from) + ' — ' + fullDate(sel.to)),
      kpi('Расходы ' + labels[state.period], money(sel.expenses, d.currency), 'Месяц: ' + money(d.month.expenses, d.currency)),
      kpi('Результат', (sel.result >= 0 ? '+' : '−') + money(Math.abs(sel.result), d.currency), 'Месяц: ' + money(d.month.result, d.currency), sel.result >= 0 ? 'pos' : 'neg'),
      h('div', { class: 'card s8' }, h('h2', { text: 'Динамика · выручка и расходы' }), trendChart(d), h('div', { class: 'legend' }, h('span', {}, h('i', { style: 'background:var(--rev)' }), 'Выручка'), h('span', {}, h('i', { style: 'background:var(--exp)' }), 'Расходы'))),
      h('div', { class: 'card s4' }, h('h2', { text: 'Оплаты сегодня' }), paymentsBlock(d)),
      locationsBlock(d),
      h('div', { class: 'card s6' }, h('h2', { text: 'Статьи расходов' }), categoriesBlock(d)),
      h('div', { class: 'card s6' }, h('h2', { text: 'Расходы' }), expensesBlock(d)),
      h('div', { class: 'card s4' }, h('h2', { text: 'Критичные остатки' }), listBlock(d.criticalStock, 'Всё в норме.', function (i) {
        return h('li', {}, h('span', { text: i.product }), h('span', { class: 'tag', text: { out: 'закончилось', low: 'мало', expiry: 'срок' }[i.status] + ' · ' + i.amount }));
      })),
      h('div', { class: 'card s4' }, h('h2', { text: 'Сроки · 14 дней' }), listBlock(d.deadlines, 'Ближайших сроков нет.', function (i) {
        return h('li', {}, h('span', { text: i.title }), h('span', { class: 'tag', text: i.daysLeft === 0 ? 'сегодня' : 'через ' + i.daysLeft + ' дн.' }));
      })),
      h('div', { class: 'card s4' }, h('h2', { text: 'Проблемы · 7 дней' }), listBlock(d.problems, 'Проблем не зафиксировано.', function (i) {
        return h('li', {}, h('span', { text: i.title }), h('span', { class: 'tag', text: i.status }));
      })),
      accountingBlock(d),
      h('div', { class: 'card s6' }, h('h2', { text: 'Последние операции' }), listBlock(d.recentOperations, 'Операций пока нет.', function (i) {
        return h('li', {}, h('span', { text: i.label }), h('b', { class: i.kind === 'revenue' ? 'pos' : '', text: (i.kind === 'revenue' ? '+' : '−') + money(i.amount, d.currency) }));
      })));
    root.replaceChildren(head, grid, h('footer', { text: 'Расходы по статьям определяются автоматически по названию и поставщику. Данные обновляются раз в минуту.' }));
  }

  function logout() {
    fetch('/v1/owner-dashboard/logout', { method: 'POST', credentials: 'same-origin' }).finally(function () {
      notice('Вы вышли', 'Чтобы открыть панель снова, запросите ссылку в чате бота командой «Дашборд».');
    });
  }

  function load() {
    fetch('/v1/owner-dashboard?period=' + state.period + (state.scope === 'group' ? '&scope=group' : (state.business ? '&business=' + encodeURIComponent(state.business) : '')), { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (response) {
        if (response.status === 401) { state.data = null; notice('Нужна ссылка из чата', 'Отправьте боту слово «Дашборд» — придёт личная ссылка, которая действует 5 минут. После входа панель открывается на 12 часов.'); return null; }
        if (!response.ok) throw new Error('http ' + response.status);
        return response.json();
      })
      .then(function (data) { if (data) { state.data = data; render(); } })
      .catch(function () { if (!state.data) notice('Не удалось загрузить данные', 'Попробуйте обновить страницу через минуту.', h('button', { class: 'btn', type: 'button', onclick: load, text: 'Повторить' })); });
  }

  window.addEventListener('hashchange', function () { var p = location.hash.slice(1); if (p !== state.period && ['day', 'week', 'month'].indexOf(p) >= 0) { state.period = p; load(); } });
  load();
  timer = setInterval(function () { if (!document.hidden && state.data) load(); }, 60000);
})();
