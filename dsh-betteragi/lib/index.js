/**
 * dsh-betteragi —— 把 BetterGI 的游戏画面与模拟输入接进 DeepSeek Harness。
 *
 * 设计要点：
 * 1. 画面即图片：`betteragi_observe` 抓到帧后用 `ctx.attachments.saveImage`
 *    落盘成不可变附件，再在 `finalizeContent` 里追加一个 `image` 内容块，
 *    模型看到的是真实截图（不是 base64 文本）。
 * 2. 指令即动作：`betteragi_act` 把结构化动作数组交给 BetterGI 的模拟输入层，
 *    返回每个动作的结果，以及（可选）执行后的新画面。
 * 3. 主动推送：`betteragi_push` 把当前画面 + 一段说明作为 user 消息写进会话，
 *    模型下一轮就能看到（用于"我自己发现有情况"的主动汇报）。
 * 4. 插件不持有全局状态：每次调用都带会话/参数，失败原样上报，不吞异常。
 *
 * 安全边界：只做屏幕截图与模拟输入（SendInput / PostMessage），不碰游戏内存。
 * @module dsh-betteragi
 */

import { randomUUID } from 'node:crypto'
import { spawn } from 'node:child_process'
import { readFile } from 'node:fs/promises'

/** Cordis 插件名。 */
export const name = 'dsh-betteragi'

/** 依赖的宿主服务：工具注册表与附件存储。 */
export const inject = ['tools', 'attachments']

const DEFAULTS = {
  baseUrl: 'http://127.0.0.1:30648',
  tokenFile: 'E:/BetterAGI/tools/bettergi_token.txt',
  token: '',
  timeoutMs: 20000,
  maxWidth: 960,
  format: 'jpg',
  quality: 70,
  // 手动输入策略，三态：
  //   'discouraged'（默认）——允许，但每次都会在结果里带一条警告，要求优先用 lumine；
  //   'off'——硬禁（只有显式配置才这样）；
  //   'on'——不提示。
  // 设计理由：单键/点击打不出连续操作，也会和策略模型抢控制权，所以默认劝阻；
  // 但"关闭菜单""点一下对话框"这类场景确实只能手动，禁用会把人堵死。
  manualInput: 'discouraged',
  // Lumine 通道：桥接的意图总线 + 本机跑租约链的 Python 入口。
  bridgeUrl: 'http://127.0.0.1:18311',
  bridgeTokenFile: 'E:/BetterAGI/tools/intent_token.txt',
  bridgeToken: '',
  pythonExe: 'E:/BetterAGI/Amaidesu/.venv/Scripts/pythonw.exe',
  intentCli: 'E:/BetterAGI/BetterGI/integrations/player/live_intent_cli.py',
}

const SCHEMA_TYPES = new Set(['string', 'number', 'integer', 'boolean', 'null', 'object', 'array'])

/** 会真正驱动游戏输入的动作类型：默认策略下禁止模型逐个调用。 */
const INPUT_ACTION_TYPES = new Set([
  'key',
  'text',
  'click',
  'doubleClick',
  'rightClick',
  'moveMouse',
  'moveMouseBy',
  'drag',
  'scroll',
  'clickAtText',
])

/** 把手动输入配置归一成三态：'discouraged' / 'off' / 'on'。 */
function manualInputMode(config) {
  const raw = config?.manualInput
  if (raw === true || raw === 'on') return 'on'
  if (raw === false || raw === 'off') return 'off'
  return 'discouraged'
}

/**
 * 手动输入闸门。
 *
 * - 'off'：抛错（只有用户显式配置才走这条）；
 * - 'discouraged'：放行，但返回一句警告，模型会看到"这属于例外"；
 * - 'on'：放行且不提示。
 */
function manualInputNotice(config, what) {
  const mode = manualInputMode(config)
  if (mode === 'off') {
    throw new Error(
      `${what} 属于手动输入，当前插件配置为 manualInput=off（显式禁用）。` +
        '请改用 betteragi_lumine 下发目标；确实必须手动时把配置改成 discouraged 或 on。',
    )
  }
  if (mode === 'on') return null
  return (
    `${what} 属于手动输入，属于例外手段：连续移动/探索请优先用 betteragi_lumine，` +
    '由策略模型连续输出动作；只有在 lumine 做不到的场合（例如关掉菜单、点一下必须点的界面）才这样用一次。'
  )
}

/** 把参数说明编译成对象根 JSON Schema（宿主 register 要的就是这个形状）。 */
function compileParameters(spec) {
  const properties = {}
  const required = []
  for (const [key, raw] of Object.entries(spec || {})) {
    const definition = raw && typeof raw === 'object' ? raw : {}
    const type = SCHEMA_TYPES.has(definition.type) ? definition.type : 'json'
    const property = { type }
    if (typeof definition.description === 'string') property.description = definition.description
    if (definition.default !== undefined) property.default = definition.default
    if (Array.isArray(definition.enum)) property.enum = definition.enum
    properties[key] = property
    if (definition.required === true) required.push(key)
  }
  const schema = { type: 'object', properties, additionalProperties: false }
  if (required.length > 0) schema.required = required
  return schema
}

/** 取宿主的 defineTool；沙箱半把它挂在 `harness` 上，进程内半可以 import。 */
function resolveDefineTool() {
  const harness = globalThis.harness
  if (harness && typeof harness.defineTool === 'function') return harness.defineTool
  return function localDefineTool(options) {
    const tool = {
      name: options.name,
      description: options.description,
      parameters: compileParameters(options.parameters),
      output: {
        schema: options.output.schema,
        render: options.output.render,
      },
      async execute(args, exec) {
        return await options.execute(args || {}, exec)
      },
    }
    if (typeof options.finalizeContent === 'function') {
      tool.finalizeContent = (exec, result) => options.finalizeContent(exec, result)
    }
    return tool
  }
}

/** 单次调用最多回传的图片数：太多会撑爆上下文。 */
const MAX_IMAGES_PER_CALL = 4

/** 把 base64 图片存进附件库，返回可放进内容块的引用。 */
async function saveImage(ctx, base64, format, note) {
  const mediaType = format === 'png' ? 'image/png' : 'image/jpeg'
  const data = Buffer.from(base64, 'base64')
  if (data.length === 0) throw new Error('BetterGI 返回了空图片')
  return await ctx.attachments.saveImage({ data, mediaType, name: note })
}

/** `image` 内容块（模型可见）。 */
function imageBlock(ref) {
  return { type: 'image', attachment: ref }
}

/** 纯文本块。 */
function textBlock(text) {
  return { type: 'text', text }
}

/** 渲染任意值成 JSON 文本块。 */
function renderJson(_args, value) {
  return [textBlock(typeof value === 'string' ? value : JSON.stringify(value, null, 2))]
}

/** 读取 token：配置优先，其次 token 文件；结果缓存 5 秒。 */
function makeTokenReader(config) {
  let cached = null
  let cachedAt = 0
  return async function token() {
    if (config.token) return config.token
    const now = Date.now()
    if (cached !== null && now - cachedAt < 5000) return cached
    cached = (await readFile(config.tokenFile, 'utf8')).trim()
    cachedAt = now
    return cached
  }
}

/** 一次 BetterGI HTTP 调用；非 2xx 时抛出带状态码的错误。 */
async function request(config, token, path, { method = 'GET', body, signal } = {}) {
  const url = config.baseUrl.replace(/\/+$/, '') + path
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(new Error('BetterGI 请求超时')), config.timeoutMs)
  const onAbort = () => controller.abort(signal.reason)
  if (signal) signal.addEventListener('abort', onAbort, { once: true })
  try {
    const response = await fetch(url, {
      method,
      headers: {
        Authorization: 'Bearer ' + token,
        'Content-Type': 'application/json',
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    })
    const text = await response.text()
    let payload = null
    try {
      payload = text ? JSON.parse(text) : null
    } catch {
      payload = { raw: text }
    }
    if (!response.ok) {
      const message = payload && payload.error ? String(payload.error) : text.slice(0, 300)
      throw new Error(`BetterGI ${path} 返回 ${response.status}：${message}`)
    }
    return payload
  } catch (error) {
    // fetch 在宿主没起来时只给一个笼统的 failed to fetch，这里补一句可操作的提示。
    const code = error && error.cause ? error.cause.code : undefined
    if (code === 'ECONNREFUSED' || code === 'ECONNRESET') {
      throw new Error(
        `无法连接 BetterGI 本地接口 ${config.baseUrl}（${code}）。` +
          '请先用命令行启动无界面宿主：betteragi host start --exe <BetterGI.exe> --config <配置> --token-file <token>，' +
          '并确认游戏已启动、runtime 已 start。',
      )
    }
    throw error
  } finally {
    clearTimeout(timer)
    if (signal) signal.removeEventListener('abort', onAbort)
  }
}

/** 把模型给的动作（JSON 文本或数组）解析成动作数组。 */
function parseActions(value) {
  let parsed = value
  if (typeof value === 'string') {
    try {
      parsed = JSON.parse(value)
    } catch (error) {
      throw new Error(`actions 不是合法 JSON：${error.message}`)
    }
  }
  if (!Array.isArray(parsed) || parsed.length === 0) throw new Error('actions 必须是非空数组')
  for (const [index, action] of parsed.entries()) {
    if (!action || typeof action !== 'object' || Array.isArray(action)) {
      throw new Error(`actions[${index}] 必须是对象`)
    }
    if (typeof action.type !== 'string' || action.type.length === 0) {
      throw new Error(`actions[${index}] 缺少 type`)
    }
  }
  return parsed
}

/** 注册全部模型可见工具。 */
export function apply(ctx, configInput = {}) {
  const config = { ...DEFAULTS, ...configInput }
  // 环境变量作为兜底开关：调试单键时不必改 profile 配置。
  if (process.env.BETTERAGI_ALLOW_MANUAL === '1') config.manualInput = 'on'
  const defineTool = resolveDefineTool()
  const token = makeTokenReader(config)
  /** 每次调用的附带图片：callId → ImageAttachmentRef[]。 */
  const pendingImages = new Map()
  ctx.effect(() => () => pendingImages.clear())

  const withImage = (exec, ref) => {
    if (!ref) return
    const callId = exec && exec.callId
    if (callId === undefined || callId === null) return
    const key = String(callId)
    const list = pendingImages.get(key) ?? []
    if (list.length >= MAX_IMAGES_PER_CALL) return
    list.push(ref)
    pendingImages.set(key, list)
  }

  const finalizeContent = (exec, result) => {
    const callId = exec && exec.callId
    const key = callId === undefined || callId === null ? null : String(callId)
    if (key === null || !pendingImages.has(key)) return undefined
    const refs = pendingImages.get(key)
    pendingImages.delete(key)
    if (refs.length === 0) return undefined
    return [...result.content, ...refs.map(imageBlock)]
  }

  const observeTool = defineTool({
    name: 'betteragi_observe',
    description:
      '截取原神当前画面并作为图片返回，同时给出 UI 状态（主界面/大地图/对话）、窗口是否前台、帧号。' +
      '需要看画面时优先调用它；返回的帧号可用于判断两次观察是否同一帧。',
    parameters: {
      maxWidth: { type: 'number', description: '返回图片最大宽度，默认 960。' },
      format: { type: 'string', description: "图片格式：'jpg'（默认）或 'png'。", enum: ['jpg', 'png'] },
      quality: { type: 'number', description: 'JPEG 质量 1–100，默认 70。' },
      includeOcr: { type: 'boolean', description: '是否一并返回 OCR 文本，默认 false。' },
      includeImage: { type: 'boolean', description: '是否附带图片，默认 true。' },
    },
    output: { schema: { type: 'string' }, render: renderJson },
    async execute(args, exec) {
      const format = args.format === 'png' ? 'png' : 'jpg'
      const payload = await request(config, await token(), '/api/v1/agent/observe', {
        method: 'POST',
        signal: exec.signal,
        body: {
          maxWidth: args.maxWidth ?? config.maxWidth,
          format,
          quality: args.quality ?? config.quality,
          includeOcr: args.includeOcr === true,
          includeImageBase64: args.includeImage !== false,
        },
      })
      if (args.includeImage !== false && payload && payload.imageBase64) {
        const ref = await saveImage(ctx, payload.imageBase64, format, `betteragi-${payload.frameId ?? 'frame'}`)
        withImage(exec, ref)
      }
      const summary = {
        frameId: payload?.frameId ?? null,
        capturedAtUtc: payload?.capturedAtUtc ?? null,
        eventSeq: payload?.eventSeq ?? null,
        image: payload?.image ?? null,
        vision: payload?.vision ?? null,
        runtime: payload?.runtime
          ? {
              isForeground: payload.runtime.isForeground ?? null,
              isMinimized: payload.runtime.isMinimized ?? null,
              windowHandle: payload.runtime.windowHandle ?? null,
              captureArea: payload.runtime.captureArea ?? null,
            }
          : null,
      }
      return JSON.stringify(summary, null, 2)
    },
    finalizeContent,
  })

  const actTool = defineTool({
    name: 'betteragi_act',
    description:
      '执行一组模拟输入动作（点击/按键/等待/拖动/滚轮），返回每个动作的结果；可选择执行后再截一帧。' +
      '坐标用归一化 0–1。动作格式：[{"type":"click","x":0.5,"y":0.5},{"type":"key","key":"W","keyAction":"down"}]。' +
      'Live 会话运行期间所有动作都会被拒绝（BETTERGI_TASK_RUNNING），需要先停 Live。',
    parameters: {
      actions: {
        type: 'string',
        required: true,
        description:
          '动作数组的 JSON 文本，例如 "[{\\"type\\":\\"key\\",\\"key\\":\\"W\\",\\"keyAction\\":\\"down\\"}]"。' +
          '用字符串传是为了兼容只接受标准 JSON Schema 的模型接口。',
      },
      channel: { type: 'string', description: "输入通道：'foreground'（默认）或 'background'。", enum: ['foreground', 'background'] },
      stopOnError: { type: 'boolean', description: '某个动作失败时是否停止，默认 true。' },
      captureAfter: { type: 'boolean', description: '执行后是否截一帧并作为图片返回，默认 false。' },
      maxWidth: { type: 'number', description: '截图最大宽度，默认 960。' },
    },
    output: { schema: { type: 'string' }, render: renderJson },
    async execute(args, exec) {
      const actions = parseActions(args.actions)
      // 非输入类动作（activateWindow / wait / screenshot / observe）永不受限；
      // 输入类走三态闸门：默认劝阻但放行，并在结果里带回提示。
      const inputActions = actions.filter((action) => INPUT_ACTION_TYPES.has(action.type))
      const manualWarning =
        inputActions.length > 0
          ? manualInputNotice(config, `动作 ${inputActions.map((item) => item.type).join('、')}`)
          : null
      const captureAfter = args.captureAfter === true
      const payload = await request(config, await token(), '/api/v1/agent/actions', {
        method: 'POST',
        signal: exec.signal,
        body: {
          channel: args.channel === 'background' ? 'background' : 'foreground',
          actions,
          stopOnError: args.stopOnError !== false,
          captureAfter,
          maxWidth: args.maxWidth ?? config.maxWidth,
          format: 'jpg',
          quality: config.quality,
          includeImageBase64: captureAfter,
          includeUiState: true,
        },
      })
      if (captureAfter && payload && payload.observation && payload.observation.imageBase64) {
        const ref = await saveImage(ctx, payload.observation.imageBase64, 'jpg', 'betteragi-after')
        withImage(exec, ref)
      }
      const summary = {
        ok: payload?.ok ?? null,
        error: payload?.error ?? null,
        // 手动输入的劝阻提示：模型据此改用 lumine；没有输入类动作时为 null。
        manualInputWarning: manualWarning,
        steps: (payload?.steps ?? []).map((step) => ({
          index: step.index,
          type: step.type,
          ok: step.ok,
          error: step.error ?? null,
          code: step.code ?? null,
          durationMs: step.durationMs ?? null,
        })),
        observation: payload?.observation
          ? { frameId: payload.observation.frameId, vision: payload.observation.vision ?? null }
          : null,
      }
      return JSON.stringify(summary, null, 2)
    },
    finalizeContent,
  })

  const liveTool = defineTool({
    name: 'betteragi_live',
    description:
      'Live 会话控制：status 查看状态、start 开始持续采集、stop 停止、frame 取最新帧。' +
      'start 默认开启严格安全与输入租约（requireStrictSafety/requireControlLease），' +
      '严格安全要求游戏窗口在前台且识别到主界面，否则不会授予输入租约。',
    parameters: {
      action: {
        type: 'string',
        required: true,
        description: 'status | start | stop | frame',
        enum: ['status', 'start', 'stop', 'frame'],
      },
      fps: { type: 'number', description: 'start 时的采集帧率，2–15，默认 5。' },
      maxWidth: { type: 'number', description: 'start 时画面宽度，默认 960。' },
      quality: { type: 'number', description: 'start 时 JPEG 质量，默认 65。' },
      strictSafety: { type: 'boolean', description: 'start 时是否要求严格安全，默认 true。' },
      requireControlLease: { type: 'boolean', description: 'start 时是否要求输入租约，默认 true。' },
    },
    output: { schema: { type: 'string' }, render: renderJson },
    async execute(args, exec) {
      const action = String(args.action || '').toLowerCase()
      if (action === 'status') {
        const payload = await request(config, await token(), '/api/v1/agent/live/status', { signal: exec.signal })
        return JSON.stringify(payload, null, 2)
      }
      if (action === 'frame') {
        const response = await fetch(`${config.baseUrl.replace(/\/+$/, '')}/api/v1/agent/live/frame`, {
          headers: { Authorization: 'Bearer ' + (await token()) },
          signal: exec.signal,
        })
        if (!response.ok) throw new Error(`取帧失败：${response.status}`)
        const bytes = Buffer.from(await response.arrayBuffer())
        if (bytes.length === 0) throw new Error('Live 会话没有可用帧')
        const ref = await ctx.attachments.saveImage({ data: bytes, mediaType: 'image/jpeg', name: 'betteragi-live' })
        withImage(exec, ref)
        return JSON.stringify({ bytes: bytes.length, mediaType: 'image/jpeg' }, null, 2)
      }
      if (action === 'stop') {
        const payload = await request(config, await token(), '/api/v1/agent/live/stop', {
          method: 'POST',
          body: {},
          signal: exec.signal,
        })
        return JSON.stringify(payload ?? { stopped: true }, null, 2)
      }
      if (action === 'start') {
        const payload = await request(config, await token(), '/api/v1/agent/live/start', {
          method: 'POST',
          signal: exec.signal,
          body: {
            sessionId: config.sessionId ?? 'integration',
            fps: args.fps ?? 5,
            quality: args.quality ?? 65,
            maxWidth: args.maxWidth ?? config.maxWidth,
            requireStrictSafety: args.strictSafety !== false,
            requireControlLease: args.requireControlLease !== false,
            ...(config.dialogueCalibration ? { dialogueCalibration: config.dialogueCalibration } : {}),
          },
        })
        return JSON.stringify(payload, null, 2)
      }
      throw new Error(`未知 action：${args.action}`)
    },
    finalizeContent,
  })

  const statusTool = defineTool({
    name: 'betteragi_status',
    description: '一次性返回智能体运行时状态与 Live 会话状态（是否运行、前台、安全门、已应用动作数）。',
    parameters: {},
    output: { schema: { type: 'string' }, render: renderJson },
    async execute(_args, exec) {
      const auth = await token()
      const [agent, live] = await Promise.all([
        request(config, auth, '/api/v1/agent/status', { signal: exec.signal }),
        request(config, auth, '/api/v1/agent/live/status', { signal: exec.signal }).catch((error) => ({
          error: String(error && error.message ? error.message : error),
        })),
      ])
      return JSON.stringify(
        {
          agent: {
            runtimeRunning: agent?.runtimeRunning ?? null,
            stopRequested: agent?.stopRequested ?? null,
            runtime: agent?.runtime
              ? {
                  isForeground: agent.runtime.isForeground ?? null,
                  isMinimized: agent.runtime.isMinimized ?? null,
                  windowHandle: agent.runtime.windowHandle ?? null,
                }
              : null,
          },
          live,
        },
        null,
        2,
      )
    },
  })

  const pushTool = defineTool({
    name: 'betteragi_push',
    description:
      '主动汇报：截取当前游戏画面，连同你写的一段说明作为一条用户消息写入当前会话，' +
      '这样模型在下一轮就能"看到"并回应（也可当作给自己留的可视上下文）。',
    parameters: {
      note: { type: 'string', required: true, description: '要写进会话的说明文字。' },
      maxWidth: { type: 'number', description: '截图最大宽度，默认 960。' },
    },
    output: { schema: { type: 'string' }, render: renderJson },
    async execute(args, exec) {
      if (typeof args.note !== 'string' || args.note.length === 0) throw new Error('note 不能为空')
      const payload = await request(config, await token(), '/api/v1/agent/observe', {
        method: 'POST',
        signal: exec.signal,
        body: {
          maxWidth: args.maxWidth ?? config.maxWidth,
          format: 'jpg',
          quality: config.quality,
          includeImageBase64: true,
        },
      })
      if (!payload || !payload.imageBase64) throw new Error('没有拿到可推送的画面')
      const ref = await saveImage(ctx, payload.imageBase64, 'jpg', `betteragi-push-${payload.frameId ?? 'frame'}`)
      const session = exec.agent && exec.agent.session
      if (!session || typeof session.append !== 'function') throw new Error('当前执行上下文没有会话，无法推送')
      const message = {
        id: randomUUID(),
        role: 'user',
        content: [textBlock(args.note), imageBlock(ref)],
        // 消息来源必须是"产出者自己的 kind"（DSH 会话格式 v4 不允许 kind:'plugin'）。
        source: { kind: name },
      }
      session.append('user/message', message, { surfaceOp: 'append' })
      return JSON.stringify({ pushed: true, frameId: payload.frameId ?? null, bytes: ref.bytes ?? null }, null, 2)
    },
  })

  /** 抓一帧并（可选）存成附件；返回观测摘要，供 watch/play 复用。 */
  async function captureFrame(exec, { maxWidth, keepImage }) {
    const payload = await request(config, await token(), '/api/v1/agent/observe', {
      method: 'POST',
      signal: exec.signal,
      body: {
        maxWidth: maxWidth ?? config.maxWidth,
        format: 'jpg',
        quality: config.quality,
        includeImageBase64: keepImage !== false,
      },
    })
    let ref = null
    if (keepImage !== false && payload && payload.imageBase64) {
      ref = await saveImage(ctx, payload.imageBase64, 'jpg', `betteragi-${payload.frameId ?? 'frame'}`)
    }
    return {
      frameId: payload?.frameId ?? null,
      capturedAtUtc: payload?.capturedAtUtc ?? null,
      uiCategory: payload?.vision?.uiCategory ?? null,
      isInMainUi: payload?.vision?.isInMainUi ?? null,
      isInTalkUi: payload?.vision?.isInTalkUi ?? null,
      isForeground: payload?.runtime?.isForeground ?? null,
      ref,
    }
  }

  const watchTool = defineTool({
    name: 'betteragi_watch',
    description:
      '连续抓 2–6 帧并一起返回，用来判断画面到底有没有变化（例如角色是否真的在移动）。' +
      '一次调用把多张图放进同一条结果里，你直接对比即可，不必来回调用 observe。',
    parameters: {
      frames: { type: 'number', description: '抓几帧，2–6，默认 3。' },
      intervalMs: { type: 'number', description: '帧间隔毫秒，100–5000，默认 800。' },
      maxWidth: { type: 'number', description: '每帧最大宽度，默认 960。' },
    },
    output: { schema: { type: 'string' }, render: renderJson },
    async execute(args, exec) {
      const frames = Math.min(6, Math.max(2, Math.round(args.frames ?? 3)))
      const intervalMs = Math.min(5000, Math.max(100, Math.round(args.intervalMs ?? 800)))
      const captured = []
      for (let index = 0; index < frames; index += 1) {
        if (index > 0) await new Promise((resolve) => setTimeout(resolve, intervalMs))
        const frame = await captureFrame(exec, { maxWidth: args.maxWidth, keepImage: true })
        if (frame.ref) withImage(exec, frame.ref)
        captured.push({
          index,
          frameId: frame.frameId,
          capturedAtUtc: frame.capturedAtUtc,
          uiCategory: frame.uiCategory,
          isInMainUi: frame.isInMainUi,
          isInTalkUi: frame.isInTalkUi,
          isForeground: frame.isForeground,
        })
      }
      return JSON.stringify({ frames: captured, intervalMs, images: captured.length }, null, 2)
    },
    finalizeContent,
  })

  const playTool = defineTool({
    name: 'betteragi_play',
    description:
      '按住若干个键一段时间并采帧。属于手动输入的例外手段（连续移动请优先 betteragi_lumine）：' +
      '默认配置下可以调用，但结果里会带 manualInputWarning；只有把插件配置 manualInput 设为 off 才硬禁。',
    parameters: {
      keys: {
        type: 'string',
        description: '要按住的键，逗号分隔，默认 "W"；例如 "W" 或 "W,Shift"。',
      },
      durationMs: { type: 'number', description: '按住总时长毫秒，200–15000，默认 3000。' },
      sampleMs: { type: 'number', description: '每隔多少毫秒抓一帧，200–5000，默认 1000。' },
      maxWidth: { type: 'number', description: '每帧最大宽度，默认 960。' },
    },
    output: { schema: { type: 'string' }, render: renderJson },
    async execute(args, exec) {
      const manualWarning = manualInputNotice(config, 'betteragi_play 的按键')
      const keys = String(args.keys ?? 'W')
        .split(',')
        .map((key) => key.trim())
        .filter((key) => key.length > 0)
      if (keys.length === 0) throw new Error('keys 不能为空')
      const durationMs = Math.min(15000, Math.max(200, Math.round(args.durationMs ?? 3000)))
      const sampleMs = Math.min(5000, Math.max(200, Math.round(args.sampleMs ?? 1000)))
      const samples = []
      const startedAt = Date.now()
      let pressed = false
      let releaseError = null
      try {
        const down = await request(config, await token(), '/api/v1/agent/actions', {
          method: 'POST',
          signal: exec.signal,
          body: {
            channel: 'foreground',
            actions: keys.map((key) => ({ type: 'key', key, keyAction: 'down' })),
            stopOnError: true,
            captureAfter: false,
          },
        })
        if (!down || down.ok !== true) {
          throw new Error('按键按下失败：' + JSON.stringify(down && down.steps ? down.steps : down))
        }
        pressed = true
        while (Date.now() - startedAt < durationMs) {
          const frame = await captureFrame(exec, { maxWidth: args.maxWidth, keepImage: true })
          if (frame.ref) withImage(exec, frame.ref)
          samples.push({
            atMs: Date.now() - startedAt,
            frameId: frame.frameId,
            uiCategory: frame.uiCategory,
            isInMainUi: frame.isInMainUi,
            isForeground: frame.isForeground,
          })
          const remaining = durationMs - (Date.now() - startedAt)
          if (remaining <= 0) break
          await new Promise((resolve) => setTimeout(resolve, Math.min(sampleMs, remaining)))
        }
      } finally {
        if (pressed) {
          try {
            const up = await request(config, await token(), '/api/v1/agent/actions', {
              method: 'POST',
              body: {
                channel: 'foreground',
                actions: keys.map((key) => ({ type: 'key', key, keyAction: 'up' })),
                stopOnError: false,
                captureAfter: false,
              },
            })
            if (!up || up.ok !== true) releaseError = JSON.stringify(up && up.steps ? up.steps : up)
          } catch (error) {
            releaseError = String(error && error.message ? error.message : error)
          }
        }
      }
      return JSON.stringify(
        {
          keys,
          heldMs: Date.now() - startedAt,
          manualInputWarning: manualWarning,
          samples,
          releaseError,
        },
        null,
        2,
      )
    },
    finalizeContent,
  })

  /** 具名 OCR 区域预设（归一化坐标）：只覆盖 UI 里有文字的稳定区块，互不重叠。 */
  const OCR_PRESETS = {
    quest: { x: 0.02, y: 0.045, width: 0.26, height: 0.085 },
    options: { x: 0.18, y: 0.585, width: 0.42, height: 0.155 },
    message: { x: 0.2, y: 0.745, width: 0.42, height: 0.135 },
    hp: { x: 0.4, y: 0.92, width: 0.2, height: 0.05 },
    // UID 字号很小，区域给足余量（右下角），否则 Paddle 常在缩小后丢字。
    uid: { x: 0.85, y: 0.945, width: 0.145, height: 0.045 },
  }

  /** 把归一化预设换算成原帧像素区域，并保证不越界、不重叠。 */
  function buildOcrRegions(names, width, height) {
    const regions = []
    for (const regionName of names) {
      const preset = OCR_PRESETS[regionName]
      if (!preset) throw new Error(`未知 OCR 区域：${regionName}`)
      const x = Math.max(0, Math.min(width - 1, Math.round(preset.x * width)))
      const y = Math.max(0, Math.min(height - 1, Math.round(preset.y * height)))
      const w = Math.max(1, Math.min(width - x, Math.round(preset.width * width)))
      const h = Math.max(1, Math.min(height - y, Math.round(preset.height * height)))
      const overlaps = regions.some(
        (other) => x < other.roi.x + other.roi.width && other.roi.x < x + w &&
          y < other.roi.y + other.roi.height && other.roi.y < y + h,
      )
      if (overlaps) throw new Error(`OCR 区域 ${regionName} 与其它区域重叠，请减少区域数量`)
      regions.push({ name: regionName, roi: { x, y, width: w, height: h } })
    }
    return regions
  }

  /** 解析 PyPI 风格的一行 JSON 输出：CLI 最后一行才是结果。 */
  function lastJsonLine(text) {
    const lines = String(text || '').split(/\r?\n/).filter((line) => line.trim().startsWith('{'))
    for (let index = lines.length - 1; index >= 0; index -= 1) {
      try {
        return JSON.parse(lines[index])
      } catch {
        /* 继续往前找 */
      }
    }
    return null
  }

  /** 在隐藏窗口里跑一次 Python 入口，收集 stdout/stderr 与退出码。 */
  function runPython(command, args, timeoutMs) {
    return new Promise((resolve, reject) => {
      const child = spawn(command, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
      let stdout = ''
      let stderr = ''
      const timer = setTimeout(() => {
        child.kill()
        reject(new Error(`Lumine 执行超时（${Math.round(timeoutMs / 1000)} 秒）`))
      }, timeoutMs)
      child.stdout.on('data', (chunk) => {
        stdout += chunk.toString('utf8')
      })
      child.stderr.on('data', (chunk) => {
        stderr += chunk.toString('utf8')
      })
      child.on('error', (error) => {
        clearTimeout(timer)
        reject(error)
      })
      child.on('close', (code) => {
        clearTimeout(timer)
        resolve({ code, stdout, stderr })
      })
    })
  }

  const bridgeConfig = {
    get token() {
      return config.bridgeToken
    },
    get tokenFile() {
      return config.bridgeTokenFile
    },
  }
  const bridgeToken = makeTokenReader(bridgeConfig)

  /** 读桥接的 /feedback；失败时返回 null，让调用方自己决定怎么报错。 */
  async function bridgeFeedback(signal) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(new Error('桥接请求超时')), 8000)
    const onAbort = () => controller.abort(signal?.reason)
    if (signal) signal.addEventListener('abort', onAbort, { once: true })
    try {
      const response = await fetch(config.bridgeUrl.replace(/\/+$/, '') + '/feedback', {
        headers: { Authorization: 'Bearer ' + (await bridgeToken()) },
        signal: controller.signal,
      })
      if (!response.ok) return null
      return await response.json()
    } catch {
      return null
    } finally {
      clearTimeout(timer)
      if (signal) signal.removeEventListener('abort', onAbort)
    }
  }

  const scanTool = defineTool({
    name: 'betteragi_scan',
    description:
      '读结构化画面数据：UI 类别与布尔状态，加上具名 OCR 区域（任务追踪 quest、对话正文 message、' +
      '选项 options、血量 hp、UID uid）的文字与坐标。默认不返回图片，比 observe 更省上下文；' +
      '需要看画面时再用 betteragi_observe。空候选只代表"这次没识别到字"，不等于界面里没字。',
    parameters: {
      regions: {
        type: 'string',
        description: '逗号分隔的区域名，默认 "quest,message,options,hp,uid"；可选 quest/message/options/hp/uid。',
      },
      includeImage: { type: 'boolean', description: '是否同时返回这一帧的图片，默认 false。' },
      maxWidth: { type: 'number', description: '附带图片时的最大宽度，默认 960。' },
    },
    output: { schema: { type: 'string' }, render: renderJson },
    async execute(args, exec) {
      const names = String(args.regions ?? 'quest,message,options,hp,uid')
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean)
      if (names.length === 0) throw new Error('regions 不能为空')
      if (names.length > 8) throw new Error('最多 8 个 OCR 区域')

      // 第一次只取尺寸：具名区域要求原帧像素坐标，必须先知道画面多大。
      const probe = await request(config, await token(), '/api/v1/agent/observe', {
        method: 'POST',
        signal: exec.signal,
        body: { maxWidth: config.maxWidth, format: 'jpg', quality: 60, includeImageBase64: false },
      })
      const width = probe?.image?.originalWidth
      const height = probe?.image?.originalHeight
      if (!width || !height) throw new Error('没能拿到原帧尺寸，无法计算 OCR 区域')

      const payload = await request(config, await token(), '/api/v1/agent/observe', {
        method: 'POST',
        signal: exec.signal,
        body: {
          maxWidth: args.maxWidth ?? config.maxWidth,
          format: 'jpg',
          quality: config.quality,
          includeImageBase64: args.includeImage === true,
          includeOcr: false,
          includeUiState: true,
          ocrRegions: buildOcrRegions(names, width, height),
        },
      })
      if (args.includeImage === true && payload?.imageBase64) {
        withImage(exec, await saveImage(ctx, payload.imageBase64, 'jpg', `betteragi-scan-${payload.frameId}`))
      }

      const regions = {}
      for (const item of payload?.ocrRegions ?? []) {
        regions[item.name] = {
          status: item.status,
          completed: item.completed,
          texts: (item.candidates ?? []).map((candidate) => candidate.text),
          boxes: (item.candidates ?? []).map((candidate) => ({
            x: candidate.normalizedX,
            y: candidate.normalizedY,
            width: candidate.normalizedWidth,
            height: candidate.normalizedHeight,
          })),
        }
      }
      const vision = payload?.vision ?? {}
      return JSON.stringify(
        {
          frameId: payload?.frameId ?? null,
          uiCategory: vision.uiCategory ?? null,
          flags: {
            main: vision.isInMainUi ?? null,
            talk: vision.isInTalkUi ?? null,
            bigMap: vision.isInBigMapUi ?? null,
            domain: vision.isInDomain ?? null,
            closableUi: vision.isInAnyClosableUi ?? null,
            gameReadyUi: vision.isInGameReadyUi ?? null,
            partyView: vision.isInPartyViewUi ?? null,
          },
          isForeground: payload?.runtime?.isForeground ?? null,
          regions,
          note: '空 candidates 表示这次 OCR 没有识别到文字，不等于区域里没有文字。',
        },
        null,
        2,
      )
    },
    finalizeContent,
  })

  const lumineTool = defineTool({
    name: 'betteragi_lumine',
    description:
      '让 Lumine（游戏操作策略模型）接手执行：给它一句话目标 + 时长，它按自己的策略连续输出按键，' +
      '本工具负责走完本机 Live 输入租约链并持续续期，最后汇报实际应用的动作数与途中状态。' +
      'action=run 会阻塞到目标时长结束（上限 120 秒）；action=status 只看现状，不动游戏。',
    parameters: {
      action: { type: 'string', description: 'run（默认）或 status。' },
      goal: { type: 'string', description: '给 Lumine 的目标，例如"沿着街道向前走到开阔处"。' },
      motorMode: { type: 'string', description: 'navigate/explore/combat/interact/wait，默认 navigate。' },
      seconds: { type: 'number', description: '执行时长秒数，1–120，默认 15。' },
      reason: { type: 'string', description: '为什么做这件事，可空。' },
    },
    output: { schema: { type: 'string' }, render: renderJson },
    async timeoutMs() {
      return 150000
    },
    async execute(args, exec) {
      const action = String(args.action ?? 'run').toLowerCase()
      if (action === 'status') {
        const feedback = await bridgeFeedback(exec.signal)
        if (!feedback) throw new Error('桥接没有响应：确认 lumine_bridge 已在 18311 运行')
        const live = await request(config, await token(), '/api/v1/agent/live/status', { signal: exec.signal })
        return JSON.stringify(
          {
            bridge: {
              bettergi: feedback.bettergiConnectionActive === true,
              motor: feedback.motorConnectionActive === true,
              session: feedback.verifiedLiveSessionId ?? null,
              currentIntent: (feedback.current ?? {}).intent_id ?? null,
              inputReleaseUnconfirmed: feedback.inputReleaseUnconfirmed === true,
            },
            live: {
              running: live?.running === true,
              gate: live?.safety?.dialogueGate ?? null,
              ready: live?.safety?.readyForV2 === true,
              appliedActions: live?.appliedActions ?? null,
              pressedKeys: live?.pressedKeys ?? null,
              starved: live?.starved ?? null,
            },
          },
          null,
          2,
        )
      }
      if (action !== 'run') throw new Error('action 只能是 run 或 status')

      const goal = String(args.goal ?? '').trim()
      if (!goal) throw new Error('action=run 时必须给 goal')
      const seconds = Math.min(120, Math.max(1, Math.round(args.seconds ?? 15)))
      const cliArgs = [
        config.intentCli,
        '--goal',
        goal,
        '--seconds',
        String(seconds),
        '--motor-mode',
        String(args.motorMode ?? 'navigate'),
        '--sample',
        '2',
      ]
      if (args.reason) cliArgs.push('--reason', String(args.reason))

      const run = await runPython(config.pythonExe, cliArgs, (seconds + 30) * 1000)
      const payload = lastJsonLine(run.stdout)
      if (!payload) {
        throw new Error(`Lumine 没有返回结果（exit=${run.code}）：${run.stderr.slice(-400)}`)
      }
      if (payload.ok !== true) {
        return JSON.stringify({ ok: false, stage: payload.stage, error: payload.error }, null, 2)
      }
      const samples = Array.isArray(payload.samples) ? payload.samples : []
      const first = samples[0] ?? null
      const last = samples[samples.length - 1] ?? null
      return JSON.stringify(
        {
          ok: true,
          intentId: payload.intentId,
          heldSec: payload.heldSec,
          appliedActions: { from: first?.appliedActions ?? null, to: last?.appliedActions ?? null },
          pressedKeys: last?.pressedKeys ?? null,
          gate: last?.dialogueGate ?? null,
          starved: last?.starved ?? null,
          note: 'appliedActions 表示输入块被应用，不等于画面真的变化；要看画面请再调用 betteragi_watch。',
        },
        null,
        2,
      )
    },
    finalizeContent,
  })

  ctx.tools.register(observeTool)
  ctx.tools.register(actTool)
  ctx.tools.register(liveTool)
  ctx.tools.register(statusTool)
  ctx.tools.register(pushTool)
  ctx.tools.register(watchTool)
  ctx.tools.register(playTool)
  ctx.tools.register(scanTool)
  ctx.tools.register(lumineTool)
}
