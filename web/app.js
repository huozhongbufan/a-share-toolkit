/* ETF 工作台前端逻辑
 * 零依赖、零 CDN、fetch 相对路径 /api/...
 *
 * 铁律：红涨绿跌。
 *   带符号数字（pct、盈亏）一律按正负上色：正=红 .up，负=绿 .down，零=灰 .flat。
 *   这是 A 股惯例，与「好=绿」的国际惯例相反，别改回去。
 *
 * 错误显式：任何接口失败都显示红字，绝不显示假数据。
 */
(function () {
  'use strict';

  var MODEL = 'qwen3.5:4b';
  var overviewTimer = null;
  var trainTimer = null;
  var askBusy = false;
  var findBusy = false;
  var lookBusy = false;

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function showError(msg) {
    var bar = $('errorbar');
    bar.textContent = '⚠ ' + msg;
    bar.classList.remove('hidden');
    clearTimeout(showError._t);
    showError._t = setTimeout(function () { bar.classList.add('hidden'); }, 8000);
  }

  function setOut(id, html) { $(id).innerHTML = html; }
  function setOutErr(id, msg) {
    $(id).innerHTML = '<div class="err">' + esc(msg) + '</div>';
  }

  /* ---------- 格式化 ---------- */

  function money(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return '—';
    var n = Number(v);
    var abs = Math.abs(n);
    if (abs >= 10000) {
      return '¥' + (n / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) + ' 万';
    }
    if (abs >= 1) {
      return '¥' + n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    return '¥' + n.toFixed(3);   // 小额（API 花费 ¥0.088）显示三位
  }

  function pnlMoney(v) {
    // 盈亏专用：必须带符号，前端按符号上色（红涨绿跌）
    if (v === null || v === undefined || isNaN(Number(v))) return '—';
    var n = Number(v);
    var sign = n > 0 ? '+' : '';
    var abs = Math.abs(n);
    var body = abs >= 10000
      ? (abs / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) + ' 万'
      : abs.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return '¥' + sign + body;
  }

  function pct(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return '—';
    var n = Number(v);
    return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
  }

  function signClass(v) {
    // 红涨绿跌：正=up(红) 负=down(绿) 零/空=flat(灰)
    if (v === null || v === undefined || isNaN(Number(v))) return 'flat';
    var n = Number(v);
    if (n > 0) return 'up';
    if (n < 0) return 'down';
    return 'flat';
  }

  function kindText(k) {
    if (k === 'BUY') return '买入';
    if (k === 'SELL') return '卖出';
    return String(k || '—');
  }

  /* ---------- fetch ---------- */

  // 口令：手机访问时后端要求每个 /api/ 请求带 ?token=xxx。
  // 入口 URL 带 token 就存 localStorage，之后所有请求自动附加，URL 清理掉。
  var TOKEN = '';
  function bootToken() {
    var m = location.search.match(/[?&]token=([^&]+)/);
    if (m) {
      TOKEN = decodeURIComponent(m[1]);
      try { localStorage.setItem('etf_token', TOKEN); } catch (e) { /* 隐私模式 */ }
      try { history.replaceState(null, '', location.pathname); } catch (e) { /* 忽略 */ }
    } else {
      try { TOKEN = localStorage.getItem('etf_token') || ''; } catch (e) { TOKEN = ''; }
    }
  }

  function apiPath(path) {
    if (!TOKEN) return path;
    return path + (path.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN);
  }

  function get(path) {
    return fetch(apiPath(path)).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    });
  }

  function post(path) {
    return fetch(apiPath(path), { method: 'POST' }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    });
  }

  /* ---------- 总览 ---------- */

  function renderOverview(d) {
    // 行情新鲜度胶囊（顶部标题栏右侧）
    var f = d.fresh || {};
    var pill = $('fresh');
    if (f.err) {
      pill.textContent = '行情不可用';
      pill.className = 'pill level-bad';
    } else {
      var html = esc(f.text || '—');
      if (f.price !== null && f.price !== undefined) {
        html += '　农行 <span class="' + signClass(f.pct) + '">'
          + esc(f.price) + ' ' + esc(pct(f.pct)) + '</span>';
      }
      pill.innerHTML = html;
      var lvl = (f.level === 'ok' || f.level === 'warn' || f.level === 'bad') ? f.level : 'dim';
      pill.className = 'pill level-' + lvl;
    }

    // 路 A 定投
    var p = d.plan || {};
    if (!p.name && !p.code) {
      setOutErr('plan', '没有定投计划数据');
    } else {
      setOut('plan',
        '<div class="big">' + esc(p.name || '—') + ' <span class="dim">' + esc(p.code || '') + '</span></div>'
        + '<div class="kv">' + esc(p.years || '—') + ' 年投入 <b>' + esc(money(p.invest)) + '</b>'
        + ' → 市值 <b>' + esc(money(p.final_value)) + '</b></div>'
        + '<div class="kv">期末月分红 <b>' + esc(money(p.monthly_dividend)) + '</b>'
        + ' · 手续费 <b>' + esc(money(p.fees)) + '</b></div>'
        + (p.note ? '<div class="note">' + esc(p.note) + '</div>' : '')
      );
    }

    // 路 B 训练
    var t = d.train || {};
    var td = d.todos || {};
    var b = [];
    b.push('<div class="kv">训练 <b>' + esc(t.runs || 0) + '</b> 次 / <b>' + esc(t.days || 0) + '</b> 天</div>');
    b.push('<div class="kv">未完成待办 <b>' + esc(td.open || 0) + '</b> 条</div>');
    if (t.top_gate) {
      b.push('<div class="note warn">最常拦的闸门：' + esc(t.top_gate) + '</div>');
    }
    setOut('train-summary', b.join(''));

    // 账户（盈亏按符号上色：红涨绿跌）
    var a = d.account || {};
    var pnl = a.realized_pnl;
    setOut('account',
      '<div class="cell"><div class="num">' + esc(money(a.cash)) + '</div><div class="lab">现金</div></div>'
      + '<div class="cell"><div class="num">' + esc(a.positions || 0) + '</div><div class="lab">持仓数</div></div>'
      + '<div class="cell"><div class="num ' + signClass(pnl) + '">' + esc(pnlMoney(pnl)) + '</div><div class="lab">已实现盈亏</div></div>'
      + '<div class="cell"><div class="num">' + esc(a.month_rounds || 0) + '</div><div class="lab">本月往返</div></div>'
    );

    // 花费
    var s = d.spend || {};
    setOut('spend',
      '<div class="kv">今日 <b>' + esc(money(s.today)) + '</b> · 本月 <b>' + esc(money(s.month))
      + '</b> / <b>¥' + esc(s.cap == null ? '30' : s.cap) + '</b></div>'
    );

    // 底部：模型状态 + 更新时间
    var m = d.models || {};
    var names = (m.loaded && m.loaded.length) ? m.loaded.join(', ') : '无';
    $('model-status').textContent = '模型：' + names + (m.pinned ? '（已钉住）' : '（未钉住）');
    $('updated').textContent = '更新于 ' + (d.at || '—');
  }

  /* ---------- 指数 / 涨跌家数 ---------- */

  function renderIndices(d) {
    var list = d.indices || [];
    if (!list.length) {
      setOut('indexbar', '<span class="dim">指数暂无数据</span>');
      return;
    }
    var items = list.map(function (it) {
      var pct = (it.pct === null || it.pct === undefined) ? null : Number(it.pct);
      var cls = (pct === null || pct === 0) ? 'dim' : (pct > 0 ? 'up' : 'down');
      var sign = pct > 0 ? '+' : '';
      var price = (it.price === null || it.price === undefined)
        ? '—' : Number(it.price).toFixed(2);
      return '<span class="idx">'
        + '<span class="idx-name">' + esc(it.name) + '</span>'
        + '<span class="idx-price">' + price + '</span>'
        + '<span class="idx-pct ' + cls + '">'
        + (pct === null ? '—' : sign + pct.toFixed(2) + '%')
        + '</span></span>';
    }).join('');
    setOut('indexbar', items + '<span id="breadth" class="breadth dim"></span>');
    loadBreadth();
  }

  function loadBreadth() {
    get('/api/breadth').then(function (d) {
      var el = $('breadth');
      if (!el) return;
      var up = d.up, down = d.down, flat = d.flat;
      if (up === null || up === undefined) { el.textContent = ''; return; }
      var total = (up || 0) + (down || 0) + (flat || 0);
      var w = total ? Math.round((up || 0) / total * 100) : 0;
      el.innerHTML = '<span class="up">↑' + up + '</span>'
        + '<span class="vbar"><i style="width:' + w + '%"></i></span>'
        + '<span class="down">↓' + down + '</span>';
    }).catch(function () {});
  }

  function loadIndices() {
    get('/api/indices').then(renderIndices).catch(function (e) {
      setOutErr('indexbar', '指数接口失败：' + e.message);
    });
  }

  function loadOverview() {
    get('/api/overview').then(renderOverview).catch(function (e) {
      showError('总览接口失败：' + e.message);
      setOutErr('plan', '加载失败');
      setOutErr('train-summary', '加载失败');
      setOutErr('account', '加载失败');
      setOutErr('spend', '加载失败');
      $('fresh').textContent = '接口失败';
      $('fresh').className = 'pill level-bad';
    });
  }

  /* ---------- 待办 ---------- */

  function renderTodos(d) {
    var open = d.open || [];
    if (!open.length) {
      setOut('todos', '<div class="dim">没有待办</div>');
      return;
    }
    var rows = open.map(function (t) {
      return '<tr>'
        + '<td>' + esc(kindText(t.kind)) + '</td>'
        + '<td>' + esc(t.code) + '</td>'
        + '<td>' + esc(t.name) + '</td>'
        + '<td>' + esc(t.state) + '</td>'
        + '<td class="num">' + (t.notional ? esc(money(t.notional)) : '—') + '</td>'
        + '</tr>';
    }).join('');
    setOut('todos',
      '<table class="tbl"><thead><tr><th>类型</th><th>代码</th><th>名称</th><th>状态</th><th>金额</th></tr></thead>'
      + '<tbody>' + rows + '</tbody></table>');
  }

  function loadTodos() {
    get('/api/todos?limit=20').then(renderTodos).catch(function (e) {
      setOutErr('todos', '待办接口失败：' + e.message);
    });
  }

  /* ---------- 模型 / 内存 ---------- */

  function loadWarm() {
    get('/api/warm').then(function (d) {
      var mem = d.memory || {};
      if (mem.total_gb) {
        $('model-status').textContent += ' · 内存 ' + mem.avail_gb + '/' + mem.total_gb + ' GB';
      }
    }).catch(function () { /* 次要信息，失败不打扰 */ });
  }

  /* ---------- 问行情 ---------- */

  function ask() {
    var q = $('q').value.trim();
    if (!q || askBusy) return;
    askBusy = true;
    var btn = $('btn-ask');
    btn.disabled = true;
    btn.textContent = '思考中…';
    setOut('query-out', '<div class="dim">思考中…</div>');
    get('/api/ask?q=' + encodeURIComponent(q) + '&model=' + encodeURIComponent(MODEL))
      .then(function (d) {
        if (d.error) throw new Error(d.error);
        setOut('query-out',
          '<div class="ans">' + esc(d.answer || '（空回答）') + '</div>'
          + '<div class="meta">耗时 ' + esc(d.elapsed == null ? '—' : d.elapsed + 's')
          + ' · ' + esc(d.rounds == null ? '—' : d.rounds) + ' 轮 · 模型 ' + esc(d.model || '—') + '</div>'
        );
      })
      .catch(function (e) {
        setOutErr('query-out', '问行情失败：' + e.message);
        showError('问行情失败：' + e.message);
      })
      .then(function () {
        askBusy = false;
        btn.disabled = !$('q').value.trim();
        btn.textContent = '问行情';
      });
  }

  /* ---------- 找标的 ---------- */

  function find() {
    var q = $('q').value.trim();
    if (!q || findBusy) return;
    findBusy = true;
    var btn = $('btn-find');
    btn.disabled = true;
    btn.textContent = '搜索中…';
    setOut('query-out', '<div class="dim">搜索中…</div>');
    get('/api/find?q=' + encodeURIComponent(q) + '&top=8')
      .then(function (d) {
        if (d.error) throw new Error(d.error);
        var hits = d.hits || [];
        var html = '';
        if (!hits.length) {
          html = '<div class="dim">没有找到</div>';
        } else {
          var rows = hits.map(function (h) {
            var gray = h.source !== '实时';
            var score = (h.score === null || h.score === undefined) ? '—' : Number(h.score).toFixed(2);
            var price = (h.price === null || h.price === undefined || h.price === '') ? '—' : esc(h.price);
            return '<tr class="' + (gray ? 'stale' : '') + '">'
              + '<td>' + esc(h.code) + '</td>'
              + '<td>' + esc(h.name) + '</td>'
              + '<td class="num">' + price + '</td>'
              + '<td class="num">' + esc(score) + '</td>'
              + '<td>' + (h.matched ? '命中' : '<span class="dim">推断</span>') + '</td>'
              + '<td class="dim">' + esc(h.why || '') + '</td>'
              + '</tr>';
          }).join('');
          html = '<table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>现价</th>'
            + '<th>相关度</th><th>命中</th><th>说明</th></tr></thead><tbody>' + rows + '</tbody></table>';
        }
        if (d.note) html += '<div class="meta">' + esc(d.note) + '</div>';
        setOut('query-out', html);
      })
      .catch(function (e) {
        setOutErr('query-out', '找标失败：' + e.message);
        showError('找标失败：' + e.message);
      })
      .then(function () {
        findBusy = false;
        btn.disabled = !$('q').value.trim();
        btn.textContent = '找标的';
      });
  }

  /* ---------- 跑训练 ---------- */

  function startTrain() {
    if (trainTimer) {
      setOut('train-out', '<div class="warn-text">已有训练在跑</div>');
      return;
    }
    post('/api/train')
      .then(function (d) {
        if (d.status === 'busy') {
          setOut('train-out', '<div class="warn-text">已有训练在跑</div>');
          pollTrain();   // 已经开始，直接跟进状态
          return;
        }
        if (d.status !== 'started') throw new Error('未知返回：' + JSON.stringify(d));
        $('btn-train').textContent = '训练中…';
        $('btn-train').disabled = true;
        setOut('train-out', '<div class="dim">训练中…</div>');
        pollTrain();
      })
      .catch(function (e) {
        setOutErr('train-out', '启动训练失败：' + e.message);
        showError('启动训练失败：' + e.message);
      });
  }

  function pollTrain() {
    if (trainTimer) return;   // 防重入
    trainTimer = setInterval(function () {
      get('/api/train/status').then(function (d) {
        var last = d.last || {};
        if (!d.running) {
          clearInterval(trainTimer);
          trainTimer = null;
          $('btn-train').textContent = '跑训练';
          $('btn-train').disabled = false;
          renderTrainResult(last);
        } else {
          setOut('train-out', '<div class="dim">训练中…</div>');
        }
      }).catch(function (e) {
        clearInterval(trainTimer);
        trainTimer = null;
        $('btn-train').textContent = '跑训练';
        $('btn-train').disabled = false;
        setOutErr('train-out', '查询训练状态失败：' + e.message);
      });
    }, 5000);
  }

  function renderTrainResult(last) {
    if (!last || !last.headline) {
      setOut('train-out', '<div class="dim">还没有训练记录</div>');
      return;
    }
    var lvl = last.headline_level || '';
    var cls = lvl === 'fail' ? 'bad-text' : (lvl === 'warn' ? 'warn-text' : 'ok-text');
    var html = '<div class="' + cls + '">' + esc(last.headline) + '</div>'
      + '<div class="meta">' + esc(last.at || '—') + ' · 耗时 '
      + esc(last.elapsed == null ? '—' : last.elapsed + 's') + '</div>';
    var stages = last.stages || [];
    if (stages.length) {
      var rows = stages.map(function (s) {
        var c = s.status === 'ok' ? 'ok-text'
          : s.status === 'warn' ? 'warn-text'
          : s.status === 'fail' ? 'bad-text'
          : 'dim';
        return '<tr><td class="num">' + esc(s.no) + '</td>'
          + '<td>' + esc(s.name) + '</td>'
          + '<td class="' + c + '">' + esc(s.status) + '</td>'
          + '<td class="dim">' + esc(s.detail || '') + '</td></tr>';
      }).join('');
      html += '<table class="tbl"><thead><tr><th>#</th><th>阶段</th><th>状态</th><th>说明</th></tr></thead>'
        + '<tbody>' + rows + '</tbody></table>';
    }
    setOut('train-out', html);
  }

  /* ---------- 读自选股 ---------- */

  function look() {
    if (lookBusy) return;
    lookBusy = true;
    var btn = $('btn-look');
    btn.disabled = true;
    btn.textContent = '读取中…';
    setOut('look-out', '<div class="dim">读取中…</div>');
    get('/api/look')
      .then(function (d) {
        if (d.error) { throw d.error; }
        var items = d.items || [];
        var html = '<div class="meta">' + esc(d.window || '') + '</div>';
        if (!items.length) {
          html += '<div class="dim">没读到内容</div>';
        } else {
          var rows = items.map(function (i) {
            var price = (i.api_price === null || i.api_price === undefined) ? '—' : esc(i.api_price);
            var chg = (i.api_pct === null || i.api_pct === undefined)
              ? '—'
              : '<span class="' + signClass(i.api_pct) + '">' + esc(pct(i.api_pct)) + '</span>';
            var ok = i.verified
              ? '<span class="ok-text">OK</span>'
              : '<span class="bad-text">✗</span>';
            return '<tr>'
              + '<td>' + esc(i.code || '—') + '</td>'
              + '<td>' + esc(i.name || '—') + '</td>'
              + '<td class="num">' + price + '</td>'
              + '<td class="num">' + chg + '</td>'
              + '<td>' + ok + '</td>'
              + '<td class="dim">' + esc(i.parse_error || i.note || '') + '</td>'
              + '</tr>';
          }).join('');
          html += '<table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>接口价</th>'
            + '<th>涨幅</th><th>核对</th><th>备注</th></tr></thead><tbody>' + rows + '</tbody></table>';
        }
        setOut('look-out', html);
      })
      .catch(function (e) {
        var msg = (e === 'process_not_running')
          ? '同花顺没在跑，终端跑 python run.py ths'
          : '读自选股失败：' + String(e && e.message ? e.message : e);
        setOutErr('look-out', msg);
        showError(msg);
      })
      .then(function () {
        lookBusy = false;
        btn.disabled = false;
        btn.textContent = '读自选股';
      });
  }

  /* ---------- 初始化 ---------- */

  function init() {
    bootToken();
    var input = $('q');
    input.addEventListener('input', function () {
      var has = !!input.value.trim();
      $('btn-ask').disabled = !has || askBusy;
      $('btn-find').disabled = !has || findBusy;
    });
    $('btn-ask').addEventListener('click', ask);
    $('btn-find').addEventListener('click', find);
    $('btn-train').addEventListener('click', startTrain);
    $('btn-look').addEventListener('click', look);

    loadOverview();
    loadIndices();
    loadTodos();
    loadWarm();
    overviewTimer = setInterval(function () {
      loadOverview();
      loadIndices();
    }, 60000);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
