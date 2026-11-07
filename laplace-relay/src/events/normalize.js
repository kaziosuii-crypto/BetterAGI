/**
 * Raw LAPLACE event -> structured envelope.
 *
 * Raw shapes follow @laplace.live/event-types v2.0.21 as relayed by the Event
 * Bridge (which adds a "source" field). Normalisation keeps everything a
 * downstream service needs and optionally the untouched original object.
 */

import { GUARD_NAMES, INTERACTION_ACTIONS, REACTION_THRESHOLD, familyOf } from './kinds.js';

const PLATFORM = 'bilibili';
const NS_PER_MS = 1e6;

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : Number(v) || 0);
const str = (v) => (v === undefined || v === null ? '' : String(v));
const round2 = (v) => Math.round(num(v) * 100) / 100;
const cat = (...parts) => parts.filter((p) => p !== null && p !== undefined && p !== '').join(' ');

function guardName(level) {
  return GUARD_NAMES[num(level)] || '';
}

function actorOf(raw) {
  const level = num(raw.guardType);
  const userType = num(raw.userType);
  const medal = raw.medal && typeof raw.medal === 'object' ? raw.medal : {};
  const role = userType === 100 ? 'anchor' : userType === 1 ? 'moderator' : 'viewer';
  const badgeName = str(medal.name || medal.medal_name);
  const badgeLevel = num(medal.level !== undefined ? medal.level : medal.medal_level);
  return {
    uid: num(raw.uid),
    name: str(raw.username),
    avatar: str(raw.avatar) || null,
    role: role,
    is_anchor: role === 'anchor',
    is_moderator: role === 'moderator',
    guard_level: level,
    guard_name: guardName(level),
    wealth_level: num(raw.wealthMedalLevel),
    badge: badgeName || badgeLevel ? { name: badgeName, level: badgeLevel } : null,
  };
}

function tsOf(raw) {
  const normalised = num(raw.timestampNormalized);
  if (normalised > 1e12) return Math.round(normalised);
  const rawTs = num(raw.timestamp);
  if (rawTs > 1e15) return Math.round(rawTs / NS_PER_MS);
  if (rawTs > 1e12) return Math.round(rawTs);
  if (rawTs > 0) return Math.round(rawTs * 1000);
  return Date.now();
}

function amountOf(value, currency, cny) {
  const v = num(value);
  const c = num(cny);
  const derived = v > 0 ? v / 1000 : null;
  const realCny = c > 0 ? round2(c) : derived === null ? null : round2(derived);
  return { value: round2(v), currency: currency, cny: realCny, approx: currency === 'silver' };
}

function reaction(weight, suggested) {
  const w = Math.max(0, Math.min(100, Math.round(weight)));
  return { needs_reaction: w >= REACTION_THRESHOLD, weight: w, suggested: suggested || [] };
}

function giftWeight(raw, cny) {
  if (str(raw.coinType) === 'silver') return 22;
  return 55 + Math.min(30, num(cny) * 1.5);
}

function replyOf(reply) {
  if (!reply || typeof reply !== 'object' || !reply.text) return null;
  return {
    user: str(reply.user && (reply.user.uname || reply.user.username)),
    uid: num(reply.user && reply.user.uid),
    text: str(reply.text),
  };
}

function trimOnlineRank(list) {
  if (!Array.isArray(list)) return [];
  return list.slice(0, 20).map((item) => ({
    uid: num(item && item.uid),
    name: str(item && item.uinfo && item.uinfo.base && item.uinfo.base.name) || str(item && item.name),
    face: str(item && item.uinfo && item.uinfo.base && item.uinfo.base.face) || null,
    score: str(item && item.score),
  }));
}

function trimWinners(list) {
  if (!Array.isArray(list)) return [];
  return list.slice(0, 20).map((item) => ({
    uid: num(item && item.uid),
    name: str(item && (item.name || item.uname)),
    face: str(item && item.face) || null,
    level: num(item && item.level),
    reward: str(item && (item.award_name || item.gift_name)) || null,
  }));
}

function actionLabel(action) {
  const map = {
    enter: '进入直播间',
    follow: '关注了主播',
    share: '分享了直播间',
    special_follow: '特别关注了主播',
    mutual_follow: '互相关注',
  };
  return map[action] || '互动';
}

const MAPPERS = {
  message(raw) {
    const guard = num(raw.guardType);
    return {
      kind: 'message',
      text: str(raw.message),
      interaction: reaction(guard > 0 ? 12 : 0, guard > 0 ? ['greet_guard'] : []),
      data: {
        user_level: num(raw.userLvl),
        send_type: num(raw.sendType),
        id_str: str(raw.idStr) || null,
        current_rank: num(raw.currentRank),
        mirror: Boolean(raw.isMirror),
        reply: replyOf(raw.reply),
        emote: raw.emote && raw.emote.url
          ? { url: str(raw.emote.url), text: str(raw.emote.emoticon_unique || raw.emote.emoji) }
          : null,
      },
    };
  },

  effect_message(raw) {
    return {
      kind: 'effect_message',
      text: str(raw.message),
      interaction: reaction(28, ['read_message']),
      data: { raw_segments: raw.messageRaw || null, guard_level: num(raw.guardType) },
    };
  },

  gift(raw) {
    const coinType = str(raw.coinType) === 'silver' ? 'silver' : 'gold';
    const amount = amountOf(raw.price, coinType, num(raw.priceNormalized));
    const suggested = ['thank_gift'];
    if ((amount.cny || 0) >= 50) suggested.push('call_out_amount');
    return {
      kind: 'gift',
      text: str(raw.message) || cat(str(raw.username), '送出', str(raw.giftName), 'x' + (num(raw.giftAmount) || 1)),
      amount: amount,
      duration_ms: num(raw.duration) > 0 ? Math.round(num(raw.duration) * 1000) : null,
      interaction: reaction(giftWeight(raw, amount.cny), suggested),
      data: {
        gift_id: num(raw.giftId),
        gift_name: str(raw.giftName),
        count: num(raw.giftAmount) || 1,
        coin_type: coinType,
        icon: str(raw.giftIcon) || null,
        biz_source: str(raw.bizSource) || null,
        receiver: raw.receiver
          ? { uid: num(raw.receiver.uid), name: str(raw.receiver.uname), room_id: num(raw.receiver.master && raw.receiver.master.room_id) }
          : null,
        blind_gift: raw.blindGift
          ? {
              config_id: num(raw.blindGift.blind_gift_config_id || (raw.blindGift.blind_gift_config && raw.blindGift.blind_gift_config.id)),
              name: str(raw.blindGift.original_gift_name || (raw.blindGift.blind_gift_config && raw.blindGift.blind_gift_config.name)) || null,
            }
          : null,
      },
    };
  },

  superchat(raw) {
    return {
      kind: 'superchat',
      text: str(raw.message),
      amount: amountOf(raw.price, 'gold', num(raw.priceNormalized)),
      duration_ms: num(raw.duration) > 0 ? Math.round(num(raw.duration) * 1000) : null,
      interaction: reaction(100, ['read_superchat', 'thank_superchat']),
      data: {
        sc_id: num(raw.scId),
        sc_name: str(raw.scName) || null,
        duration_s: num(raw.duration),
        message_color: str(raw.messageColor) || null,
        translated: str(raw.messageTrans) || null,
        translated_on: num(raw.transMark) === 1,
        audited: num(raw.isAudited) === 1,
        guard_background: str(raw.guardBackground) || null,
      },
    };
  },

  toast(raw) {
    return {
      kind: 'membership',
      text: str(raw.message) || cat(str(raw.username), '开通/续费', str(raw.toastName)),
      amount: amountOf(raw.price, 'gold', num(raw.priceNormalized)),
      duration_ms: num(raw.duration) > 0 ? Math.round(num(raw.duration) * 1000) : null,
      interaction: reaction(num(raw.toastType) === 1 ? 95 : 88, ['welcome_membership']),
      data: {
        toast_type: num(raw.toastType),
        toast_name: str(raw.toastName),
        amount: num(raw.toastAmount),
        amount_unit: str(raw.toastAmountUnit),
        total_count: num(raw.toastTotalCount),
        stable_key: str(raw.stableKey) || null,
        effect_id: num(raw.effectId),
        color: str(raw.color) || null,
      },
    };
  },

  mvp(raw) {
    return {
      kind: 'mvp',
      text: str(raw.message) || '守护圣法师',
      amount: amountOf(raw.price, 'gold', num(raw.priceNormalized)),
      interaction: reaction(80, ['thank_gift']),
      data: {
        action: str(raw.action),
        amount: num(raw.mvpAmount),
        icon: str(raw.mvpIcon) || null,
        effect_id: num(raw.effectId),
      },
    };
  },

  'red-envelope-start'(raw) {
    return {
      kind: 'red_envelope_start',
      text: str(raw.message) || cat(str(raw.username), '发红包'),
      amount: amountOf(raw.price, 'gold', num(raw.priceNormalized)),
      duration_ms: num(raw.duration) > 0 ? Math.round(num(raw.duration) * 1000) : null,
      interaction: reaction(72, ['promote_red_envelope']),
      data: {
        price_for_users: num(raw.priceForUsers),
        price_for_streamer: num(raw.priceForStreamer),
        duration_s: num(raw.duration),
        icon: str(raw.icon) || null,
        awards: Array.isArray(raw.list) ? raw.list.slice(0, 20) : [],
      },
    };
  },

  'red-envelope-result'(raw) {
    return {
      kind: 'red_envelope_result',
      text: str(raw.message) || '红包开奖',
      interaction: reaction(40, ['announce_winner']),
      data: {
        winners: trimWinners(raw.list),
        rewards: Array.isArray(raw.rewards) ? raw.rewards.slice(0, 20) : [],
        cny: round2(raw.priceNormalized),
      },
    };
  },

  'lottery-start'(raw) {
    return {
      kind: 'lottery_start',
      text: str(raw.message) || '天选时刻: ' + str(raw.rewardName),
      duration_ms: num(raw.duration) > 0 ? Math.round(num(raw.duration) * 1000) : null,
      interaction: reaction(72, ['promote_lottery']),
      data: {
        reward_name: str(raw.rewardName),
        reward_num: num(raw.rewardNum),
        reward_image: str(raw.rewardImage) || null,
        requirement: str(raw.requirement) || null,
        join_command: str(raw.messageToJoin) || null,
        gift_name: str(raw.giftName) || null,
        gift_count: num(raw.giftCount),
        gift_price_cny: round2(raw.giftPriceNormalized),
      },
    };
  },

  'lottery-result'(raw) {
    return {
      kind: 'lottery_result',
      text: str(raw.message) || '天选时刻开奖',
      interaction: reaction(45, ['announce_winner']),
      data: {
        reward_name: str(raw.rewardName),
        reward_num: num(raw.rewardNum),
        winners: trimWinners(raw.list),
      },
    };
  },

  interaction(raw) {
    const action = num(raw.action);
    const actionName = INTERACTION_ACTIONS[action] || 'unknown';
    const weights = { enter: 0, follow: 38, share: 30, special_follow: 42, mutual_follow: 42 };
    const suggested = {
      follow: ['ack_follow'],
      special_follow: ['ack_follow'],
      mutual_follow: ['ack_follow'],
      share: ['ack_share'],
    };
    const who = str(raw.username);
    return {
      kind: 'interaction',
      text: who ? who + ' ' + actionLabel(actionName) : actionLabel(actionName),
      interaction: reaction(weights[actionName] || 0, suggested[actionName] || []),
      data: {
        action: action,
        action_name: actionName,
        relation: raw.relation ? { type: num(raw.relation.type), text: str(raw.relation.text) } : null,
      },
    };
  },

  'entry-effect'(raw) {
    return {
      kind: 'entry_effect',
      text: str(raw.message) || cat(str(raw.username), '进入直播间'),
      interaction: reaction(num(raw.guardType) > 0 ? 15 : 0, []),
      data: {
        effect_id: num(raw.effectId),
        business: num(raw.business),
        duration_s: num(raw.duration),
        colors: { text: str(raw.textColor) || null, name: str(raw.nameColor) || null },
        background: str(raw.background) || null,
        background_animated: str(raw.backgroundAnimated) || null,
        guard_level: num(raw.guardType),
      },
    };
  },

  'like-click'(raw) {
    return {
      kind: 'like',
      text: str(raw.message) || '点赞了主播',
      interaction: reaction(0, []),
      data: { guard_level: num(raw.guardType) },
    };
  },

  'likes-update'(raw) {
    return {
      kind: 'likes_update',
      text: str(raw.message) || '点赞数更新',
      interaction: reaction(0, []),
      data: { likes: num(raw.likes) },
    };
  },

  'watched-update'(raw) {
    return {
      kind: 'watched_update',
      text: str(raw.message) || '累计观看人数更新',
      interaction: reaction(0, []),
      data: { watched: num(raw.watched) },
    };
  },

  'online-update'(raw) {
    return {
      kind: 'online_update',
      text: str(raw.message) || '在线人数更新',
      interaction: reaction(0, []),
      data: { online: num(raw.online), count: num(raw.count) },
    };
  },

  'online-rank-update'(raw) {
    return {
      kind: 'online_rank',
      text: str(raw.message) || '高能榜更新',
      interaction: reaction(0, []),
      data: { list: trimOnlineRank(raw.list) },
    };
  },

  'room-meta-update'(raw) {
    return {
      kind: 'room_meta',
      text: str(raw.message) || '房间数据更新',
      interaction: reaction(0, []),
      data: { fans: num(raw.fans), fansclub: num(raw.fansclub) },
    };
  },

  'room-name-update'(raw) {
    return {
      kind: 'room_title',
      text: str(raw.message) || '直播间标题变更',
      interaction: reaction(20, ['update_overlay']),
      data: { area: str(raw.area) || null, parent_area: str(raw.parentArea) || null },
    };
  },

  'live-start'(raw) {
    return {
      kind: 'live_start',
      text: str(raw.message) || '开播',
      interaction: reaction(70, ['open_stream']),
      data: { initial: Boolean(raw.initial), special_types: raw.specialTypes || null },
    };
  },

  'live-end'(raw) {
    return {
      kind: 'live_end',
      text: str(raw.message) || '下播',
      interaction: reaction(55, ['close_stream']),
      data: {},
    };
  },

  'live-warning'(raw) {
    return {
      kind: 'live_warning',
      text: str(raw.message) || '直播间被警告',
      interaction: reaction(95, ['check_stream_health']),
      data: {},
    };
  },

  'live-cutoff'(raw) {
    return {
      kind: 'live_cutoff',
      text: str(raw.message) || '直播间被切断',
      interaction: reaction(95, ['check_stream_health']),
      data: {},
    };
  },

  notice(raw) {
    return {
      kind: 'notice',
      text: str(raw.message),
      interaction: reaction(45, ['announce']),
      data: {
        segments: Array.isArray(raw.messageRaw) ? raw.messageRaw.slice(0, 20) : [],
        terminals: Array.isArray(raw.terminals) ? raw.terminals.slice(0, 20) : [],
      },
    };
  },

  system(raw) {
    return {
      kind: 'system',
      text: str(raw.message),
      interaction: reaction(0, []),
      data: {},
    };
  },

  'message-recall'(raw) {
    return {
      kind: 'recall',
      text: str(raw.message) || '弹幕被撤回',
      interaction: reaction(0, []),
      data: { recall_type: num(raw.recallType) },
    };
  },

  'superchat-delete'(raw) {
    return {
      kind: 'sc_delete',
      text: '醒目留言被删除',
      interaction: reaction(10, []),
      data: { sc_ids: Array.isArray(raw.scIds) ? raw.scIds.map(num) : [] },
    };
  },

  'user-block'(raw) {
    return {
      kind: 'user_block',
      text: str(raw.message) || cat(str(raw.username), '被禁言'),
      interaction: reaction(30, ['notify_moderation']),
      data: {
        operator: num(raw.operator),
        block_expired: raw.blockExpired === undefined ? null : raw.blockExpired,
        valid_period: str(raw.vaildPeriod) || null,
      },
    };
  },

  'room-mute-on'(raw) {
    return {
      kind: 'mute_on',
      text: str(raw.message) || '开启直播间禁言',
      interaction: reaction(35, ['notify_moderation']),
      data: { mute_by: str(raw.muteBy) || null, mute_level: num(raw.muteLevel), mute_length: num(raw.muteLength) },
    };
  },

  'room-mute-off'(raw) {
    return {
      kind: 'mute_off',
      text: str(raw.message) || '关闭直播间禁言',
      interaction: reaction(35, ['notify_moderation']),
      data: {},
    };
  },

  'mod-assign'(raw) {
    return {
      kind: 'mod_assign',
      text: str(raw.message) || '新增房管',
      interaction: reaction(25, ['ack_mod_change']),
      data: { mod: num(raw.mod) },
    };
  },

  'mod-revoke'(raw) {
    return {
      kind: 'mod_revoke',
      text: str(raw.message) || '撤销房管',
      interaction: reaction(25, ['ack_mod_change']),
      data: { mod: num(raw.mod) },
    };
  },

  'mod-list'(raw) {
    return {
      kind: 'mod_list',
      text: str(raw.message) || '房管列表',
      interaction: reaction(0, []),
      data: { mods: Array.isArray(raw.mods) ? raw.mods.map(num) : [] },
    };
  },
};

export function normalizeEvent(raw, options) {
  const opts = options || {};
  const includeRaw = Boolean(opts.includeRaw);
  const roomId = num(opts.roomId);
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new TypeError('event must be a JSON object');
  }
  const rawType = str(raw.type);
  const mapper = MAPPERS[rawType];
  const mapped = mapper
    ? mapper(raw)
    : {
        kind: 'unknown',
        text: str(raw.message),
        interaction: reaction(0, []),
        data: { raw_type: rawType || null, keys: Object.keys(raw).slice(0, 30) },
      };

  const kind = mapped.kind;
  const ts = tsOf(raw);
  const room = num(raw.origin);
  const rawId = str(raw.id || raw.idStr || raw.stableKey) || null;

  if (roomId && room && room !== roomId) return null;

  const envelope = {
    v: 1,
    id: rawId ? kind + ':' + rawId : kind + ':' + (rawType || 'unknown') + ':' + ts + ':' + num(raw.uid),
    seq: null,
    kind: kind,
    family: familyOf(kind),
    ts: ts,
    room: { id: room, platform: PLATFORM },
    actor: actorOf(raw),
    text: mapped.text || '',
    amount: mapped.amount || null,
    duration_ms: mapped.duration_ms === undefined ? null : mapped.duration_ms,
    interaction: mapped.interaction,
    data: mapped.data || {},
    source: {
      transport: 'laplace-bridge',
      raw_type: rawType || null,
      raw_id: rawId,
      bridge_source: str(raw.source) || null,
    },
  };
  if (includeRaw) envelope.raw = raw;
  return envelope;
}

export const _internals = { tsOf: tsOf, amountOf: amountOf, actorOf: actorOf, actionLabel: actionLabel };
