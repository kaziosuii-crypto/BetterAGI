/**
 * 插件自测：用一个假的 ctx 驱动 lib/index.js 里的工具，打到真实的 BetterGI HTTP 上。
 * 只验证三件事：工具能注册、能拿到数据、图片能进内容块。
 */
import assert from 'node:assert/strict'
import { apply, name, inject } from '../lib/index.js'

const calls = []
const savedImages = []

/** 造一个假 ctx（工具表 + 附件 + effect），供不同配置的实例各用一份。 */
function makeCtx(map) {
  return {
    effect(fn) {
      const dispose = fn()
      calls.push({ disposed: typeof dispose === 'function' })
    },
    tools: {
      register(tool) {
        map.set(tool.name, tool)
      },
    },
    attachments: {
      async saveImage({ data, mediaType, name: imageName }) {
        const bytes = Buffer.from(data)
        assert.ok(bytes.length > 1000, `图片太小：${bytes.length}`)
        const magic = bytes.subarray(0, 3)
        const isJpeg = magic[0] === 0xff && magic[1] === 0xd8
        const isPng = magic[0] === 0x89 && magic[1] === 0x50
        assert.ok(isJpeg || isPng, `不是 JPEG/PNG：${bytes.subarray(0, 4).toString('hex')}`)
        const ref = {
          attachmentId: `local:${savedImages.length + 1}`,
          mediaType,
          bytes: bytes.length,
          width: 960,
          height: 540,
          name: imageName,
        }
        savedImages.push(ref)
        return ref
      },
    },
  }
}

/** 按给定配置注册一份工具实例。 */
function collectTools(config) {
  const map = new Map()
  apply(makeCtx(map), config)
  return map
}

const tools = collectTools({})

assert.equal(name, 'dsh-betteragi')
assert.ok(Array.isArray(inject) && inject.includes('attachments'))
assert.deepEqual(
  [...tools.keys()].sort(),
  [
    'betteragi_act',
    'betteragi_live',
    'betteragi_lumine',
    'betteragi_observe',
    'betteragi_play',
    'betteragi_push',
    'betteragi_scan',
    'betteragi_status',
    'betteragi_watch',
  ],
)

const exec = { callId: 'call-1', signal: new AbortController().signal }
const seen = []

function finalize(tool, result, target = exec) {
  const replaced = tool.finalizeContent(target, result)
  return replaced ?? result.content
}

// 1) observe：必须带回图片块
const observe = tools.get('betteragi_observe')
const observeText = await observe.execute({ includeImage: true }, exec)
const observeJson = JSON.parse(observeText)
assert.ok(observeJson.frameId, 'observe 没有返回 frameId')
assert.ok(observeJson.runtime, 'observe 没有返回 runtime')
const observeContent = finalize(observe, { content: [{ type: 'text', text: observeText }] })
assert.equal(observeContent.length, 2, 'observe 应该产出 文本 + 图片 两个块')
assert.equal(observeContent[1].type, 'image')
assert.ok(observeContent[1].attachment.attachmentId.startsWith('local:'))
seen.push({ tool: 'observe', vision: observeJson.vision?.uiCategory, bytes: observeContent[1].attachment.bytes })

// 2) act：执行一个无副作用动作，并要求截图（actions 用 JSON 文本传）
const act = tools.get('betteragi_act')
const actText = await act.execute(
  { actions: JSON.stringify([{ type: 'activateWindow' }]), captureAfter: true },
  exec,
)
const actJson = JSON.parse(actText)
assert.equal(actJson.ok, true, `act 失败：${actText}`)
assert.equal(actJson.steps.length, 1)
const actContent = finalize(act, { content: [{ type: 'text', text: actText }] })
assert.equal(actContent.length, 2, 'act 带 captureAfter 时应该产出 文本 + 图片')
seen.push({ tool: 'act', steps: actJson.steps.length, bytes: actContent[1].attachment.bytes })

// 3) live status
const live = tools.get('betteragi_live')
const liveText = await live.execute({ action: 'status' }, exec)
const liveJson = JSON.parse(liveText)
assert.ok(typeof liveJson.running === 'boolean', `live status 结构异常：${liveText.slice(0, 200)}`)
seen.push({ tool: 'live', running: liveJson.running, gate: liveJson.safety?.dialogueGate })

// 4) status
const status = tools.get('betteragi_status')
const statusJson = JSON.parse(await status.execute({}, exec))
assert.ok('agent' in statusJson && 'live' in statusJson)
seen.push({ tool: 'status', runtimeRunning: statusJson.agent.runtimeRunning })

// 5) push：没有真实会话时应明确报错；有会话时写入一条 user 消息（来源必须是本插件）
const push = tools.get('betteragi_push')
await assert.rejects(() => push.execute({ note: '自测' }, exec), /没有会话/)

const appended = []
const pushExec = {
  callId: 'call-2',
  signal: new AbortController().signal,
  agent: { session: { append: (type, data, opts) => appended.push({ type, data, opts }) } },
}
const pushJson = JSON.parse(await push.execute({ note: '插件自测推送' }, pushExec))
assert.equal(pushJson.pushed, true)
assert.equal(appended.length, 1)
assert.equal(appended[0].type, 'user/message')
assert.deepEqual(appended[0].opts, { surfaceOp: 'append' })
assert.equal(appended[0].data.role, 'user')
assert.equal(appended[0].data.source.kind, 'dsh-betteragi')
assert.equal(appended[0].data.content[0].type, 'text')
assert.equal(appended[0].data.content[1].type, 'image')

// 6) 手动输入闸门：默认是"劝阻但可用"（结果带警告），显式 off 才硬禁
await assert.rejects(() => act.execute({ actions: '[{' }, exec), /不是合法 JSON/)
const discouragedJson = JSON.parse(
  await act.execute({ actions: JSON.stringify([{ type: 'key', key: 'W', keyAction: 'press' }]) }, exec),
)
assert.ok(
  typeof discouragedJson.manualInputWarning === 'string' && /lumine/.test(discouragedJson.manualInputWarning),
  '默认配置下手动输入必须放行并带 lumine 劝阻提示',
)
const discouragedPlayJson = JSON.parse(
  await tools.get('betteragi_play').execute({ keys: 'W', durationMs: 400, sampleMs: 300 }, exec),
)
assert.ok(/lumine/.test(discouragedPlayJson.manualInputWarning || ''), 'play 默认也应带劝阻提示')
const offTools = collectTools({ manualInput: 'off' })
await assert.rejects(
  () => offTools.get('betteragi_act').execute({ actions: JSON.stringify([{ type: 'key', key: 'W' }]) }, exec),
  /手动输入/,
)
await assert.rejects(() => offTools.get('betteragi_play').execute({ keys: 'W', durationMs: 400 }, exec), /手动输入/)
assert.equal(tools.get('betteragi_act').parameters.properties.actions.type, 'string')

// 6b) 显式 on 时不提示（用独立实例，避免影响默认实例）
const manualTools = collectTools({ manualInput: 'on' })
const play = manualTools.get('betteragi_play')
const playExec = { callId: 'call-4', signal: new AbortController().signal }
const manualAct = manualTools.get('betteragi_act')
const manualActJson = JSON.parse(
  await manualAct.execute({ actions: JSON.stringify([{ type: 'key', key: 'S', keyAction: 'press' }]) }, exec),
)
assert.equal(manualActJson.ok, true, '放行后单键仍失败：' + JSON.stringify(manualActJson))
assert.equal(manualActJson.manualInputWarning, null, 'manualInput=on 不应再提示')
seen.push({ tool: 'manual-gate', allowed: manualActJson.ok })

// 7) watch：一次调用必须带回多帧图片（上限 4 张）
const watch = tools.get('betteragi_watch')
const watchExec = { callId: 'call-3', signal: new AbortController().signal }
const watchJson = JSON.parse(await watch.execute({ frames: 3, intervalMs: 200 }, watchExec))
assert.equal(watchJson.frames.length, 3)
const watchContent = finalize(watch, { content: [{ type: 'text', text: 'x' }] }, watchExec)
assert.equal(watchContent.length, 4, 'watch 应该返回 文本 + 3 张图')
seen.push({ tool: 'watch', frames: watchJson.frames.length, blocks: watchContent.length })

// 8) play：显式放行后，按住 → 采样 → 松键，且图片不超过上限
const playJson = JSON.parse(await play.execute({ keys: 'S', durationMs: 900, sampleMs: 300 }, playExec))
assert.ok(playJson.samples.length >= 1, 'play 没有采到帧')
assert.equal(playJson.keys[0], 'S')
assert.equal(playJson.releaseError, null, `松键失败：${playJson.releaseError}`)
const playContent = finalize(play, { content: [{ type: 'text', text: 'y' }] }, playExec)
assert.ok(playContent.length <= 1 + 4)
seen.push({ tool: 'play', samples: playJson.samples.length, heldMs: playJson.heldMs })

// 9) scan：结构化识别（UI 类别 + 具名 OCR 区域）
const scan = tools.get('betteragi_scan')
const scanJson = JSON.parse(await scan.execute({ regions: 'quest,message,options' }, exec))
assert.ok(scanJson.uiCategory, 'scan 没有返回 uiCategory')
assert.ok(scanJson.regions && typeof scanJson.regions === 'object', 'scan 没有返回 regions')
for (const key of Object.keys(scanJson.regions)) {
  assert.ok(Array.isArray(scanJson.regions[key].texts), `${key} 缺少 texts`)
}
seen.push({ tool: 'scan', ui: scanJson.uiCategory, regions: Object.keys(scanJson.regions) })

// 10) lumine status：桥接没跑时必须给出可读错误，而不是静默成功
const lumine = tools.get('betteragi_lumine')
try {
  const lumineJson = JSON.parse(await lumine.execute({ action: 'status' }, exec))
  assert.ok('bridge' in lumineJson && 'live' in lumineJson)
  seen.push({ tool: 'lumine', bridge: lumineJson.bridge.motor, gate: lumineJson.live.gate })
} catch (error) {
  assert.ok(/桥接/.test(String(error.message)), `lumine status 错误信息不清晰：${error.message}`)
  seen.push({ tool: 'lumine', error: 'bridge-down(ok)' })
}


//    否则模型接口会拒绝整个请求（本轮就是这样挂的）。
for (const tool of tools.values()) {
  const text = JSON.stringify(tool.parameters)
  assert.ok(!/"type":"json"/.test(text), `${tool.name} 的参数里还有 type:json`)
  assert.equal(tool.parameters.type, 'object')
}

console.log(JSON.stringify({ ok: true, seen, images: savedImages.length, effects: calls.length }, null, 2))
