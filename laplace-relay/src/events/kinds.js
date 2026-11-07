/**
 * Event taxonomy: every LAPLACE raw event maps into one (kind, family) pair.
 * The taxonomy is deliberately small and stable — downstream services filter on
 * `family` for coarse routing and on `kind` for exact behaviour.
 */

export const GUARD_NAMES = { 0: '', 1: '总督', 2: '提督', 3: '舰长' };

export const GUARD_PRICES_CNY = { 1: 19998, 2: 1998, 3: 138 };

/** interaction.action values from Bilibili INTERACT_WORD. */
export const INTERACTION_ACTIONS = {
  1: 'enter',
  2: 'follow',
  3: 'share',
  4: 'special_follow',
  5: 'mutual_follow',
};

/** Families group kinds for subscription filters and dashboard colour coding. */
export const FAMILIES = {
  chat: { label: '聊天', color: '#5eead4' },
  money: { label: '付费', color: '#fbbf24' },
  audience: { label: '观众', color: '#a78bfa' },
  room: { label: '房间', color: '#60a5fa' },
  lifecycle: { label: '开播', color: '#f472b6' },
  moderation: { label: '管理', color: '#94a3b8' },
  system: { label: '系统', color: '#cbd5e1' },
  unknown: { label: '未知', color: '#64748b' },
};

/**
 * Capability metadata per kind. `weight` is 0-100 and answers one question:
 * "how much does the streamer need to react to this, right now?"
 * `suggested` gives downstream agents/automation a hint about the action.
 */
export const KIND_META = {
  message: { family: 'chat', label: '弹幕', weight: 0 },
  effect_message: { family: 'chat', label: '特效弹幕', weight: 28 },
  gift: { family: 'money', label: '礼物', weight: 60 },
  superchat: { family: 'money', label: '醒目留言', weight: 100 },
  membership: { family: 'money', label: '大航海', weight: 88 },
  mvp: { family: 'money', label: 'MVP', weight: 80 },
  red_envelope_start: { family: 'money', label: '红包' , weight: 72 },
  red_envelope_result: { family: 'money', label: '红包结果', weight: 40 },
  lottery_start: { family: 'money', label: '天选时刻', weight: 72 },
  lottery_result: { family: 'money', label: '天选结果', weight: 45 },
  interaction: { family: 'audience', label: '互动', weight: 0 },
  entry_effect: { family: 'audience', label: '进场特效', weight: 0 },
  like: { family: 'audience', label: '点赞', weight: 0 },
  likes_update: { family: 'audience', label: '点赞数', weight: 0 },
  watched_update: { family: 'audience', label: '观看数', weight: 0 },
  online_update: { family: 'audience', label: '在线数', weight: 0 },
  online_rank: { family: 'audience', label: '高能榜', weight: 0 },
  room_meta: { family: 'room', label: '房间数据', weight: 0 },
  room_title: { family: 'room', label: '标题变更', weight: 20 },
  live_start: { family: 'lifecycle', label: '开播', weight: 70 },
  live_end: { family: 'lifecycle', label: '下播', weight: 55 },
  live_warning: { family: 'lifecycle', label: '警告', weight: 95 },
  live_cutoff: { family: 'lifecycle', label: '切断', weight: 95 },
  notice: { family: 'system', label: '通告', weight: 45 },
  system: { family: 'system', label: '系统消息', weight: 0 },
  recall: { family: 'moderation', label: '弹幕撤回', weight: 0 },
  sc_delete: { family: 'moderation', label: 'SC 删除', weight: 10 },
  user_block: { family: 'moderation', label: '禁言', weight: 30 },
  mute_on: { family: 'moderation', label: '开启禁言', weight: 35 },
  mute_off: { family: 'moderation', label: '关闭禁言', weight: 35 },
  mod_assign: { family: 'moderation', label: '新增房管', weight: 25 },
  mod_revoke: { family: 'moderation', label: '撤销房管', weight: 25 },
  mod_list: { family: 'moderation', label: '房管列表', weight: 0 },
  unknown: { family: 'unknown', label: '未识别', weight: 0 },
};

export const KINDS = Object.keys(KIND_META);

export function familyOf(kind) {
  return (KIND_META[kind] && KIND_META[kind].family) || 'unknown';
}

/** Threshold used for the `interaction.needs_reaction` flag. */
export const REACTION_THRESHOLD = 30;
