/**
 * Mock event source.
 *
 * Produces raw LAPLACE-shaped payloads (camelCase, same field names as
 * @laplace.live/event-types) so the whole pipeline can be exercised without a
 * live Bilibili room or a running Event Bridge.
 */

const NAMES = [
  ['夜航星', 12001, 24, 3, 18],
  ['橘子汽水', 12002, 12, 0, 5],
  ['Kuma今天也很困', 12003, 31, 2, 26],
  ['しろい', 12004, 8, 0, 2],
  ['主播的狗', 12005, 19, 3, 14],
  ['Anon', 12006, 3, 0, 1],
  ['路过的风', 12007, 0, 0, 0],
  ['提督的小号', 12008, 27, 2, 21],
  ['momo', 12009, 14, 1, 9],
  ['星见', 12010, 33, 1, 30],
];

const GIFT_POOL = [
  ['小心心', 1, 100, 0.1],
  ['辣条', 2, 100, 0.1],
  ['小花花', 10, 100, 1],
  ['牛哇牛哇', 100, 100, 10],
  ['干杯', 500, 100, 50],
  ['舰长', 198000, 1000, 198],
  ['提督', 1998000, 1000, 1998],
];

const MESSAGES = [
  '主播今天状态不错啊',
  '这首歌唱得也太好听了吧',
  '来了来了，蹲一个',
  '哈哈哈哈哈哈笑死',
  '求个歌单',
  '刚下班，赶上了',
  '这个游戏我玩过，后面有陷阱',
  '晚上好呀',
  '好家伙，这波操作',
  '下一首唱什么',
  '求翻牌',
  '已经三连了',
];

const SUPERS = ['多喝点热水', '点首歌吧', '主播加油，冲鸭', '这个月的舰长续上了'];

function mulberry32(seed) {
  let a = seed >>> 0;
  return function rand() {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function pick(rand, list) {
  return list[Math.floor(rand() * list.length) % list.length];
}

function weighted(rand, table) {
  const total = table.reduce((sum, row) => sum + row[1], 0);
  let roll = rand() * total;
  for (const row of table) {
    roll -= row[1];
    if (roll <= 0) return row[0];
  }
  return table[table.length - 1][0];
}

export function createMockSource(options) {
  const opts = options || {};
  const roomId = Number(opts.roomId) || 1001;
  const seed = Number(opts.seed) || 20261104;
  const rand = mulberry32(seed);
  let counter = 0;
  let likes = 10240;
  let online = 1280;
  let watched = 84521;
  let fans = 23110;

  const nextId = () => 'mock-' + Date.now().toString(36) + '-' + (++counter);
  const base = () => {
    const person = pick(rand, NAMES);
    const guard = rand() < 0.18 ? person[3] : 0;
    return {
      id: nextId(),
      origin: roomId,
      originIdx: 0,
      uid: person[1],
      username: person[0],
      timestamp: Date.now(),
      timestampNormalized: Date.now(),
      read: true,
      avatar: 'https://i0.hdslb.com/bfs/face/member/noface.jpg',
      guardType: guard,
      userLvl: person[2],
      wealthMedalLevel: person[4],
      userType: person[1] === 12005 ? 1 : 0,
      medal: rand() < 0.6 ? { name: '星见团', level: 8 + Math.floor(rand() * 20), up_name: '星见' } : null,
    };
  };

  function message() {
    const m = base();
    return {
      ...m,
      type: 'message',
      message: pick(rand, MESSAGES),
      sendType: 0,
      nameColor: '#00AEEC',
      idStr: m.id,
      currentRank: Math.floor(rand() * 40) + 1,
      isMirror: false,
      emote: null,
      reply: rand() < 0.08 ? { text: '这首歌叫什么', user: { uid: 12007, uname: '路过的风' } } : null,
    };
  }

  function gift() {
    const m = base();
    const item = pick(rand, GIFT_POOL);
    const count = item[1] >= 1000 ? 1 : 1 + Math.floor(rand() * 5);
    const total = item[1] * count;
    return {
      ...m,
      type: 'gift',
      message: null,
      giftId: 30000 + Math.floor(rand() * 100),
      giftName: item[0],
      giftAmount: count,
      giftIcon: 'https://i0.hdslb.com/bfs/live/gift.png',
      coinType: 'gold',
      price: total,
      priceNormalized: Math.round((item[3] * count) * 100) / 100,
      bizSource: 'pc',
      receiver: { uid: 90001, uname: '星见', master: { room_id: roomId } },
      blindGift: null,
    };
  }

  function superchat() {
    const m = base();
    const price = pick(rand, [50, 100, 500, 1000, 2000]);
    return {
      ...m,
      type: 'superchat',
      message: pick(rand, SUPERS),
      price: price * 1000,
      priceNormalized: price,
      duration: 60,
      messageColor: '#5C2D91',
      messageTrans: null,
      transMark: 0,
      isAudited: 1,
      scId: Math.floor(rand() * 1e6),
      scName: '醒目留言',
      guardBackground: null,
    };
  }

  function toast() {
    const m = base();
    const isGuard = rand() < 0.25;
    return {
      ...m,
      type: 'toast',
      message: null,
      stableKey: 'toast-' + m.id,
      price: isGuard ? 198000 : 1000,
      priceNormalized: isGuard ? 198 : 1,
      toastType: isGuard ? 1 : 2,
      toastAmount: 1,
      toastAmountUnit: '舰长',
      toastName: isGuard ? '舰长' : '大航海体验券',
      toastTotalCount: 1,
      effectId: 0,
      color: '#F5C542',
      duration: 3,
    };
  }

  function interaction() {
    const m = base();
    const action = weighted(rand, [[1, 70], [2, 16], [3, 8], [4, 4], [5, 2]]);
    return { ...m, type: 'interaction', action, relation: null };
  }

  function likeClick() {
    const m = base();
    return { ...m, type: 'like-click', message: null, likeCount: 1 };
  }

  function likesUpdate() {
    likes += Math.floor(rand() * 30);
    return { id: nextId(), type: 'likes-update', origin: roomId, timestamp: Date.now(), likes };
  }

  function onlineUpdate() {
    online = Math.max(200, online + Math.floor(rand() * 60) - 20);
    watched += Math.floor(rand() * 12);
    return { id: nextId(), type: 'online-update', origin: roomId, timestamp: Date.now(), online, count: online };
  }

  function watchedUpdate() {
    return { id: nextId(), type: 'watched-update', origin: roomId, timestamp: Date.now(), watched };
  }

  function roomMeta() {
    fans += Math.floor(rand() * 6);
    return {
      id: nextId(), type: 'room-meta-update', origin: roomId, timestamp: Date.now(),
      fans, fansclub: Math.floor(fans * 0.31),
    };
  }

  function entryEffect() {
    const m = base();
    return {
      ...m, type: 'entry-effect', message: null, effectId: 1, business: 1,
      duration: 2, textColor: '#FFFFFF', nameColor: '#F5C542',
      background: null, backgroundAnimated: null,
    };
  }

  function notice() {
    return {
      id: nextId(), type: 'notice', origin: roomId, timestamp: Date.now(),
      message: '【系统提示】请文明发言，共同维护直播间氛围',
      messageRaw: [{ type: 'text', text: '请文明发言' }],
      terminals: [0],
    };
  }

  function redEnvelopeStart() {
    const m = base();
    return {
      ...m, type: 'red-envelope-start', message: null,
      price: 50000, priceNormalized: 50, priceForUsers: 40000, priceForStreamer: 10000,
      duration: 60, icon: null, list: [],
    };
  }

  function lotteryStart() {
    return {
      id: nextId(), type: 'lottery-start', origin: roomId, timestamp: Date.now(), message: null,
      rewardName: '限定徽章', rewardNum: 1, rewardImage: null, requirement: '关注主播',
      messageToJoin: '发送「天选」参与', giftName: null, giftCount: 0, giftPriceNormalized: 0,
      duration: 120,
    };
  }

  function liveStart() {
    return { id: nextId(), type: 'live-start', origin: roomId, timestamp: Date.now(), message: null, initial: false };
  }

  const TABLE = [
    ['message', 46, message],
    ['gift', 13, gift],
    ['interaction', 12, interaction],
    ['like', 10, likeClick],
    ['likes', 5, likesUpdate],
    ['online', 4, onlineUpdate],
    ['entry', 4, entryEffect],
    ['toast', 2.5, toast],
    ['superchat', 1.4, superchat],
    ['roomMeta', 1.6, roomMeta],
    ['watch', 0.8, watchedUpdate],
    ['notice', 0.5, notice],
    ['red', 0.15, redEnvelopeStart],
    ['lottery', 0.12, lotteryStart],
    ['live', 0.02, liveStart],
  ];

  return {
    next() {
      const key = weighted(rand, TABLE.map((row) => [row[0], row[1]]));
      const factory = TABLE.find((row) => row[0] === key)[2];
      return factory();
    },
    roomId,
    seed,
  };
}
