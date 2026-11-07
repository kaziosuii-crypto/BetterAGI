/**
 * Runtime configuration. CLI flags win over environment variables, which win
 * over the defaults. Everything has a sane local-development default so the
 * service can be started with no arguments at all.
 */

const DEFAULTS = {
  host: '127.0.0.1',
  port: 8787,
  bridgeUrl: 'ws://127.0.0.1:9696',
  bridgeToken: '',
  source: 'bridge', // bridge | mock | both
  roomId: 0, // 0 = accept every room the bridge relays
  mockIntervalMs: 700,
  mockSeed: 20261104,
  includeRaw: false,
  dataDir: 'data',
  webhookUrls: [],
  webhookSecret: '',
  webhookTimeoutMs: 5000,
  ringSize: 500,
  ackTtlMs: 120000,
};

const FLAG_ALIASES = {
  '--port': 'port',
  '--host': 'host',
  '--bridge-url': 'bridgeUrl',
  '--bridge-token': 'bridgeToken',
  '--source': 'source',
  '--room': 'roomId',
  '--mock-interval': 'mockIntervalMs',
  '--mock-seed': 'mockSeed',
  '--data-dir': 'dataDir',
  '--ring-size': 'ringSize',
  '--webhook': 'webhookUrls',
  '--webhook-secret': 'webhookSecret',
};

const ENV_ALIASES = {
  LAPLACE_RELAY_HOST: 'host',
  LAPLACE_RELAY_PORT: 'port',
  LAPLACE_RELAY_BRIDGE_URL: 'bridgeUrl',
  LAPLACE_RELAY_BRIDGE_TOKEN: 'bridgeToken',
  LAPLACE_RELAY_SOURCE: 'source',
  LAPLACE_RELAY_ROOM_ID: 'roomId',
  LAPLACE_RELAY_DATA_DIR: 'dataDir',
  LAPLACE_RELAY_WEBHOOK_URLS: 'webhookUrls',
  LAPLACE_RELAY_WEBHOOK_SECRET: 'webhookSecret',
};

/**
 * @param {string[]} argv raw process.argv slice(2)
 * @param {Record<string, string|undefined>} env
 */
export function loadConfig(argv = process.argv.slice(2), env = process.env) {
  const cfg = { ...DEFAULTS, webhookUrls: [] };
  let includeRaw = DEFAULTS.includeRaw;
  const errors = [];

  for (const [key, name] of Object.entries(ENV_ALIASES)) {
    const value = env[key];
    if (value === undefined || value === '') continue;
    if (name === 'webhookUrls') cfg.webhookUrls = value.split(',').map((v) => v.trim()).filter(Boolean);
    else cfg[name] = coerce(name, value);
  }

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--include-raw') {
      includeRaw = true;
      continue;
    }
    if (arg === '--help' || arg === '-h') {
      cfg.help = true;
      continue;
    }
    const [key, inlineValue] = arg.split('=', 2);
    const name = FLAG_ALIASES[key];
    if (!name) {
      errors.push(`未知参数: ${arg}`);
      continue;
    }
    const value = inlineValue !== undefined ? inlineValue : argv[++i];
    if (value === undefined) {
      errors.push(`参数 ${key} 缺少取值`);
      continue;
    }
    if (name === 'webhookUrls') cfg.webhookUrls.push(value);
    else cfg[name] = coerce(name, value);
  }

  cfg.includeRaw = includeRaw;
  if (!['bridge', 'mock', 'both'].includes(cfg.source)) {
    errors.push(`--source 只能是 bridge / mock / both，收到 ${cfg.source}`);
  }
  if (errors.length) {
    const err = new Error(errors.join('; '));
    err.code = 'CONFIG_INVALID';
    throw err;
  }
  return cfg;
}

function coerce(name, value) {
  switch (name) {
    case 'port':
    case 'roomId':
    case 'mockIntervalMs':
    case 'mockSeed':
    case 'ringSize':
      return Number(value);
    default:
      return value;
  }
}

export const HELP = `laplace-relay — LAPLACE 弹幕事件中继服务

用法: node src/server.js [选项]

  --port <n>               HTTP/WS 监听端口 (默认 8787)
  --host <addr>            监听地址 (默认 127.0.0.1)
  --bridge-url <ws://..>   LAPLACE Event Bridge 地址 (默认 ws://127.0.0.1:9696)
  --bridge-token <token>   桥接服务器 token
  --source <bridge|mock|both>  事件来源 (默认 bridge)
  --room <id>              只保留该房间的事件，0 表示不过滤
  --mock-interval <ms>     模拟事件平均间隔 (默认 700)
  --mock-seed <n>          模拟事件随机种子
  --include-raw            在输出里附带原始事件
  --data-dir <dir>         JSONL 落盘目录 (默认 data)
  --webhook <url>          追加 webhook 订阅，可重复
  --webhook-secret <s>     webhook HMAC-SHA256 签名密钥
  --ring-size <n>          内存环形缓冲条数 (默认 500)

环境变量: LAPLACE_RELAY_PORT / _HOST / _BRIDGE_URL / _BRIDGE_TOKEN /
          _SOURCE / _ROOM_ID / _DATA_DIR / _WEBHOOK_URLS / _WEBHOOK_SECRET
`;
