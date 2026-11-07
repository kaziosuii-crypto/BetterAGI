// 弹幕位置映射扩展（浏览器端）——通过 ObsFusion 扩展 API 渲染弹幕
(function () {
  if (!window.ObsFusion) return;
  var STYLE_ID = 'of-danmaku-style';
  var DEMO_LINES = [
    '这个转场也太丝滑了',
    '文字和画面终于对上了',
    'TTS 听起来很自然',
    '求这套字幕模板',
    'SC：把弹幕钉在右上角试试',
    '来了来了',
    '这个镜头有电影感',
    '弹幕映射到左上角也太酷了'
  ];

  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = [
      '.of-danmaku-layer{position:absolute;inset:0;overflow:hidden;pointer-events:none;z-index:60;font-family:"Microsoft YaHei UI","PingFang SC",sans-serif;}',
      '.of-danmaku-item{position:absolute;display:flex;align-items:baseline;gap:10px;white-space:nowrap;will-change:transform,opacity;text-shadow:0 2px 12px rgba(0,0,0,.8),0 0 2px rgba(0,0,0,.9);}',
      '.of-danmaku-item .dm-user{font-size:.66em;color:#E8B05A;letter-spacing:.04em;opacity:.9;}',
      '.of-danmaku-item .dm-text{font-weight:600;letter-spacing:.02em;}',
      '.of-danmaku-item.is-fixed{padding:10px 18px;border-radius:10px;background:linear-gradient(135deg,rgba(12,14,20,.72),rgba(12,14,20,.42));border:1px solid rgba(232,176,90,.22);backdrop-filter:blur(8px);white-space:normal;max-width:520px;line-height:1.35;}',
      '.of-danmaku-item.is-fixed .dm-text{font-weight:500;}'
    ].join('');
    document.head.appendChild(style);
  }

  function register(ctx) {
    injectStyle();
    var layer = ctx.layers.create({ id: 'danmaku', className: 'of-danmaku-layer', z: 60 });
    var W = ctx.canvas.width;
    var H = ctx.canvas.height;
    var items = [];
    var laneBusy = {};

    function laneTop(lane, msg) {
      var gap = 62;
      var base;
      if (msg.zone === 'bottom' || msg.zone === 'bottom-left' || msg.zone === 'bottom-right') {
        base = H - 300;
      } else if (msg.zone === 'center' || msg.zone === 'mid-left' || msg.zone === 'mid-right') {
        base = H * 0.5 - 100;
      } else {
        base = 86;
      }
      return base + lane * gap;
    }

    function pickLane(msg) {
      var now = performance.now();
      var key = msg.zone + '|' + msg.mode;
      var lane = Number(msg.lane) || 0;
      for (var i = 0; i < msg.lanes; i++) {
        var probe = (lane + i) % msg.lanes;
        if (!laneBusy[key + '|' + probe] || laneBusy[key + '|' + probe] < now) {
          laneBusy[key + '|' + probe] = now + (msg.mode === 'fixed' ? msg.duration * 1000 : 900);
          return probe;
        }
      }
      return lane;
    }

    function removeItem(item) {
      var index = items.indexOf(item);
      if (index >= 0) items.splice(index, 1);
      if (item.parentNode) item.parentNode.removeChild(item);
    }

    function spawn(msg) {
      if (!msg || !msg.text) return;
      var lane = pickLane(msg);
      var el = document.createElement('div');
      el.className = 'of-danmaku-item ' + (msg.mode === 'fixed' ? 'is-fixed' : 'is-free');
      el.style.color = msg.color || '#F4F1EA';
      el.style.fontSize = (msg.size || 34) + 'px';
      el.style.opacity = String(msg.opacity === undefined ? 0.92 : msg.opacity);
      if (msg.user) {
        var user = document.createElement('span');
        user.className = 'dm-user';
        user.textContent = msg.user;
        el.appendChild(user);
      }
      var text = document.createElement('span');
      text.className = 'dm-text';
      text.textContent = msg.text;
      el.appendChild(text);
      layer.appendChild(el);
      items.push(el);
      while (items.length > 40) removeItem(items[0]);

      var top = laneTop(lane, msg);
      var duration = Math.max(1200, (msg.duration || 6) * 1000);
      if (msg.mode === 'fixed') {
        // 坐标一律用 1920x1080 逻辑画布（层属于舞台，而非浏览器窗口）
        var anchorRight = msg.x > 0.5;
        var maxW = Math.max(320, Math.min(W * 0.6, 900));
        var fixedTop = Math.max(40, Math.min(H - 160, msg.y * H - 40));
        if (anchorRight) {
          // 右锚点：贴住右边缘向左展开（宽度由内容决定，用 left 估算会偏）
          el.style.right = Math.max(24, (1 - msg.x) * W) + 'px';
          el.style.left = 'auto';
        } else {
          var left = Math.max(40, Math.min(W - maxW, msg.x * W - 280));
          el.style.left = left + 'px';
          el.style.right = 'auto';
        }
        el.style.top = fixedTop + 'px';
        el.animate([
          { opacity: 0, transform: 'translateY(12px) scale(.98)' },
          { opacity: msg.opacity || 0.92, transform: 'translateY(0) scale(1)', offset: 0.08 },  // 入场后保持
          { opacity: msg.opacity || 0.92, transform: 'translateY(0) scale(1)', offset: 0.86 },
          { opacity: 0, transform: 'translateY(-8px) scale(.99)' }
        ], { duration: duration, easing: 'cubic-bezier(.22,.61,.36,1)' }).onfinish = function () { removeItem(el); };
      } else {
        el.style.top = top + 'px';
        // 用 offsetWidth（布局像素，不受舞台 scale 影响）而不是 getBoundingClientRect
        var width = Math.max(120, el.offsetWidth || 120);
        // 起点离右边缘 8% 画布宽、终点出左边缘后完全消失，避免"卡半截"和一闪而过
        var from = msg.mode === 'reverse' ? -width - 40 : W - Math.max(80, Math.min(W * 0.08, 200));
        var to = msg.mode === 'reverse' ? W + 80 : -width - 40;
        el.style.left = '0px';
        var anim = el.animate([
          { transform: 'translateX(' + from + 'px)' },
          { transform: 'translateX(' + to + 'px)' }
        ], { duration: duration, easing: 'linear' });
        anim.onfinish = function () { removeItem(el); };
        anim.oncancel = function () { removeItem(el); };
      }
    }

    ctx.on('danmaku.add', spawn);

    var demoStarted = false;
    function startDemo() {
      if (demoStarted) return;
      demoStarted = true;
      var i = 0;
      var timer = setInterval(function () {
        if (!document.body.contains(layer)) { clearInterval(timer); return; }
        var msg = {
          text: DEMO_LINES[i % DEMO_LINES.length],
          user: i % 3 === 0 ? '主播' : ('观众' + String.fromCharCode(65 + (i % 6))),
          zone: i % 4 === 1 ? 'bottom-left' : (i % 5 === 2 ? 'top-right' : 'top'),
          mode: i % 4 === 1 ? 'fixed' : (i % 5 === 2 ? 'fixed' : 'scroll'),
          lanes: 6, lane: i % 6, x: 0.5, y: 0.2, duration: 7,
          color: i % 3 === 0 ? '#E8B05A' : '#F4F1EA', size: 32, opacity: 0.92
        };
        spawn(msg);
        i += 1;
        if (i > 24) clearInterval(timer);
      }, 650);
    }

    ctx.on('timeline.ready', function () {
      if (new URLSearchParams(location.search).get('danmaku') === 'demo') startDemo();
    });

    ctx.emit('extension.ready', { id: 'danmaku' });
  }

  ObsFusion.registerExtension({ id: 'danmaku', setup: register });
})();