using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;
using System;
using System.Collections.Generic;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Channels;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Live;

/// <summary>
/// 实时会话 WebSocket 端点：推送画面/指导/解说，接收指导、解说和动作块。
/// </summary>
public sealed class LiveWebSocketHandler(ILiveSessionService liveService, ILogger<LiveWebSocketHandler> logger)
{
    /// <summary>串行化动作发送与断线，避免多个客户端交错控制。</summary>
    private readonly SemaphoreSlim _actionGate = new(1, 1);

    /// <summary>当前拥有动作控制权的连接，断线时释放。</summary>
    private string? _actionOwner;

    /// <summary>该连接绑定的凭据仅用于失联时有条件撤销，绝不广播。</summary>
    private LiveLeaseCredentialsDto? _actionOwnerLease;

    /// <summary>当前绑定成功的请求标识，用于丢包后只读查询。</summary>
    private string? _actionOwnerLeaseRequestId;

    /// <summary>单条客户端消息的最大字节数。</summary>
    private const int MaxMessageBytes = 4 * 1024 * 1024;

    /// <summary>每个连接最多保留的停止请求标识数量，满后拒绝新的关联请求。</summary>
    private const int MaxStopRequestIds = 1024;

    /// <summary>每个连接最多保留的租约绑定请求标识数量。</summary>
    private const int MaxLeaseRequestIds = 1024;

    /// <summary>WebSocket JSON 选项。</summary>
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
    };

    /// <summary>
    /// 处理 WebSocket 握手与消息循环。
    /// </summary>
    /// <param name="context">HTTP 上下文。</param>
    public async Task HandleAsync(HttpContext context)
    {
        if (!context.WebSockets.IsWebSocketRequest)
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            await context.Response.WriteAsync("需要 WebSocket 连接。", context.RequestAborted).ConfigureAwait(false);
            return;
        }

        using var socket = await context.WebSockets.AcceptWebSocketAsync().ConfigureAwait(false);
        var channel = Channel.CreateBounded<LiveServerMessage>(new BoundedChannelOptions(32)
        {
            FullMode = BoundedChannelFullMode.DropOldest,
            SingleReader = true,
            SingleWriter = false
        });
        var client = new LiveClientConnection
        {
            Id = Guid.NewGuid().ToString("N"),
            Messages = channel
        };

        liveService.AttachClient(client);
        using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(context.RequestAborted);
        var sendTask = SendLoopAsync(socket, client, cancellation.Token);
        var receiveTask = ReceiveLoopAsync(socket, client, cancellation.Token);
        try
        {
            await Task.WhenAny(sendTask, receiveTask).ConfigureAwait(false);
        }
        finally
        {
            cancellation.Cancel();
            await _actionGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
            try
            {
                if (_actionOwner == client.Id)
                {
                    if (_actionOwnerLease is null) liveService.StopLegacyActions();
                    else liveService.ReleaseActionLease(_actionOwnerLease);
                    _actionOwner = null;
                    _actionOwnerLease = null;
                    _actionOwnerLeaseRequestId = null;
                }
            }
            finally
            {
                _actionGate.Release();
            }

            liveService.DetachClient(client);
            client.Dispose();
            try
            {
                await Task.WhenAll(sendTask, receiveTask).WaitAsync(TimeSpan.FromSeconds(2)).ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                // 取消可能无法立即中断底层 I/O；强制终止以免遗留后台循环。
                logger.LogDebug(exception, "结束实时会话 WebSocket 循环");
                socket.Abort();
            }

            if (socket.State is WebSocketState.Open or WebSocketState.CloseReceived)
            {
                try
                {
                    using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(2));
                    await socket.CloseAsync(WebSocketCloseStatus.NormalClosure, "closed", timeout.Token)
                        .ConfigureAwait(false);
                }
                catch (Exception exception)
                {
                    logger.LogDebug(exception, "关闭实时会话 WebSocket 失败");
                }
            }
        }
    }

    /// <summary>发送循环。</summary>
    private static async Task SendLoopAsync(WebSocket socket, LiveClientConnection client,
        CancellationToken cancellationToken)
    {
        await foreach (var message in client.Messages.Reader.ReadAllAsync(cancellationToken).ConfigureAwait(false))
        {
            var json = SerializeServerMessage(message);
            var bytes = Encoding.UTF8.GetBytes(json);
            await socket.SendAsync(bytes, WebSocketMessageType.Text, true, cancellationToken).ConfigureAwait(false);
        }
    }

    /// <summary>绑定回执仅含公开的关联标识，普通消息沿用原有空值语义。</summary>
    private static string SerializeServerMessage(LiveServerMessage message)
    {
        if (message.Type == "lease.bound")
        {
            return JsonSerializer.Serialize(new
            {
                type = message.Type,
                leaseRequestId = message.LeaseRequestId,
                leaseInstanceId = message.LeaseInstanceId,
                leaseEpoch = message.LeaseEpoch,
                sessionId = message.SessionId,
                leaseConnectionId = message.LeaseConnectionId,
                leaseStatus = message.LeaseStatus,
                leaseProbeId = message.LeaseProbeId,
                leaseExpiresInMs = message.LeaseExpiresInMs
            }, JsonOptions);
        }
        return JsonSerializer.Serialize(message, JsonOptions);
    }

    /// <summary>接收循环。</summary>
    private async Task ReceiveLoopAsync(WebSocket socket, LiveClientConnection client,
        CancellationToken cancellationToken)
    {
        var buffer = new byte[64 * 1024];
        using var messageBuffer = new MemoryStream();

        while (!cancellationToken.IsCancellationRequested && socket.State == WebSocketState.Open)
        {
            WebSocketReceiveResult result;
            try
            {
                result = await socket.ReceiveAsync(buffer, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                break;
            }

            if (result.MessageType == WebSocketMessageType.Close)
            {
                break;
            }

            if (result.MessageType != WebSocketMessageType.Text)
            {
                client.TryEnqueue(new LiveServerMessage
                {
                    Type = "error",
                    Code = "UNSUPPORTED_MESSAGE",
                    Message = "实时会话只接受文本 JSON 消息。"
                });
                continue;
            }

            messageBuffer.Write(buffer, 0, result.Count);
            if (messageBuffer.Length > MaxMessageBytes)
            {
                client.TryEnqueue(new LiveServerMessage
                {
                    Type = "error",
                    Code = "MESSAGE_TOO_LARGE",
                    Message = "消息超过 4MB。"
                });
                break;
            }

            if (!result.EndOfMessage)
            {
                continue;
            }

            var json = Encoding.UTF8.GetString(messageBuffer.GetBuffer(), 0, (int)messageBuffer.Length);
            messageBuffer.SetLength(0);
            await DispatchAsync(client, json, cancellationToken).ConfigureAwait(false);
        }
    }

    /// <summary>创建沿用原错误码和说明的错误消息，只附带已验证的原始动作归属。</summary>
    private static LiveServerMessage CreateError(string code, string message, LiveActionOwnership? ownership,
        string? stopRequestId = null, string? leaseRequestId = null)
    {
        return new LiveServerMessage
        {
            Type = "error",
            Code = code,
            Message = message,
            Seq = ownership?.Seq,
            IntentId = ownership?.IntentId,
            MotorGeneration = ownership?.MotorGeneration,
            ActionGroupId = ownership?.ActionGroupId,
            StopRequestId = stopRequestId,
            LeaseRequestId = leaseRequestId
        };
    }

    /// <summary>从完整 JSON 中提取本次 actions 消息的归属；不修改请求或参与动作授权。</summary>
    private static LiveActionOwnership? ReadActionOwnership(string json)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object ||
                !TryGetUniqueProperty(root, "type", out var type) || type.ValueKind != JsonValueKind.String ||
                !string.Equals(type.GetString()?.Trim(), "actions", StringComparison.OrdinalIgnoreCase) ||
                !TryGetUniqueProperty(root, "block", out var block))
            {
                return null;
            }

            if (block.ValueKind is not (JsonValueKind.Undefined or JsonValueKind.Null or JsonValueKind.Object))
            {
                return null;
            }

            var hasBlock = block.ValueKind == JsonValueKind.Object;
            var seqValid = TryResolveNumber(root, block, "seq", !hasBlock, false, out var seq);
            var intentValid = TryResolveIdentifier(root, block, "intentId", out var intentId);
            var generationValid = TryResolveNumber(root, block, "motorGeneration", true, true, out var generation);
            var groupValid = TryResolveIdentifier(root, block, "actionGroupId", out var groupId);
            // 任一归属字段存在歧义时，不得因省略非法组标识而误归属于无组的意图。
            var attributionValid = intentValid && generationValid && groupValid;
            return new LiveActionOwnership
            {
                Seq = seqValid ? seq : null,
                IntentId = attributionValid ? intentId : null,
                MotorGeneration = attributionValid ? generation : null,
                ActionGroupId = attributionValid ? groupId : null
            };
        }
        catch (JsonException)
        {
            // 不完整或非法 JSON 无法可靠确定归属，也不记录原始内容。
            return null;
        }
    }

    /// <summary>识别原始绑定消息，避免重复 type 字段将校验降级为其他消息。</summary>
    private static bool IsPotentialLeaseBind(string json)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            if (document.RootElement.ValueKind != JsonValueKind.Object) return false;
            foreach (var property in document.RootElement.EnumerateObject())
            {
                if (string.Equals(property.Name, "leaseRequestId", StringComparison.OrdinalIgnoreCase)) return true;
                if (string.Equals(property.Name, "type", StringComparison.OrdinalIgnoreCase)
                    && property.Value.ValueKind == JsonValueKind.String
                    && string.Equals(property.Value.GetString()?.Trim(), "lease.bind", StringComparison.OrdinalIgnoreCase))
                    return true;
            }
        }
        catch (JsonException) { }
        return false;
    }

    /// <summary>校验完整绑定消息及其唯一请求标识，不记录或回显凭据。</summary>
    private static bool TryReadLeaseBind(string json, out string? requestId)
    {
        requestId = null;
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object) return false;
            // 即使其他字段无效，只回传唯一、格式正确的请求标识。
            if (TryGetUniqueProperty(root, "leaseRequestId", out var id) && id.ValueKind == JsonValueKind.String)
            {
                var value = id.GetString();
                if (value is { Length: 32 })
                {
                    var valid = true;
                    foreach (var ch in value)
                        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) { valid = false; break; }
                    if (valid) requestId = value;
                }
            }
            if (requestId is null || !HasOnlyUniqueFields(root, "type", "leaseRequestId", "lease")
                || !TryGetUniqueProperty(root, "type", out var type) || type.ValueKind != JsonValueKind.String
                || !string.Equals(type.GetString()?.Trim(), "lease.bind", StringComparison.OrdinalIgnoreCase)
                || !TryGetUniqueProperty(root, "lease", out var lease) || lease.ValueKind != JsonValueKind.Object
                || !HasOnlyUniqueFields(lease, "secret", "instanceId", "epoch")
                || !TryGetUniqueProperty(lease, "secret", out var secret) || secret.ValueKind != JsonValueKind.String
                || !TryGetUniqueProperty(lease, "instanceId", out var instance) || instance.ValueKind != JsonValueKind.String
                || !TryGetUniqueProperty(lease, "epoch", out var epoch) || epoch.ValueKind != JsonValueKind.Number
                || !epoch.TryGetInt64(out var generation) || generation < 0)
                return false;
            return true;
        }
        catch (JsonException) { return false; }
    }

    /// <summary>仅允许要求的字段且每个字段出现一次，包括大小写变体。</summary>
    private static bool HasOnlyUniqueFields(JsonElement element, params string[] fields)
    {
        if (element.ValueKind != JsonValueKind.Object) return false;
        var expected = new HashSet<string>(fields, StringComparer.OrdinalIgnoreCase);
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var property in element.EnumerateObject())
            if (!expected.Contains(property.Name) || !seen.Add(property.Name)) return false;
        return seen.Count == expected.Count;
    }

    /// <summary>只接受唯一的租约查询标识；查询本身不得续期。</summary>
    private static bool TryReadLeaseProbe(string json, out string? requestId, out string? probeId)
    {
        requestId = null;
        probeId = null;
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (!HasOnlyUniqueFields(root, "type", "leaseRequestId", "leaseProbeId")
                || !TryGetUniqueProperty(root, "type", out var type) || type.ValueKind != JsonValueKind.String
                || type.GetString() is not ("lease.probe" or "lease.activate")
                || !TryGetUniqueProperty(root, "leaseRequestId", out var id) || id.ValueKind != JsonValueKind.String
                || !TryGetUniqueProperty(root, "leaseProbeId", out var nonce) || nonce.ValueKind != JsonValueKind.String)
                return false;
            var value = id.GetString();
            var nonceValue = nonce.GetString();
            if (value is not { Length: 32 } || nonceValue is not { Length: 32 }) return false;
            foreach (var ch in value)
                if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) return false;
            foreach (var ch in nonceValue)
                if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) return false;
            requestId = value;
            probeId = nonceValue;
            return true;
        }
        catch (JsonException) { return false; }
    }

    /// <summary>只从明确的 stop_actions 原始消息提取唯一、有界的请求标识。</summary>
    private static bool TryReadStopRequestId(string json, out string? requestId)
    {
        requestId = null;
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object ||
                !TryGetUniqueProperty(root, "type", out var type) || type.ValueKind != JsonValueKind.String ||
                !string.Equals(type.GetString()?.Trim(), "stop_actions", StringComparison.OrdinalIgnoreCase) ||
                !TryGetUniqueProperty(root, "stopRequestId", out var id))
            {
                return false;
            }

            if (id.ValueKind == JsonValueKind.Undefined)
            {
                return true;
            }

            return id.ValueKind == JsonValueKind.String && TryReadIdentifier(id, out requestId);
        }
        catch (JsonException)
        {
            return false;
        }
    }

    /// <summary>为当前连接登记唯一请求标识；容量耗尽或重复时不得产生可混淆的回执。</summary>
    private static bool RegisterStopRequestId(LiveClientConnection client, string? requestId)
    {
        return requestId is null ||
            (client.StopRequestIds.Count < MaxStopRequestIds && client.StopRequestIds.Add(requestId));
    }

    /// <summary>大小写不敏感地查找唯一字段；重复字段一律视为不明确。</summary>
    private static bool TryGetUniqueProperty(JsonElement element, string name, out JsonElement value)
    {
        value = default;
        if (element.ValueKind != JsonValueKind.Object)
        {
            return true;
        }

        var found = false;
        foreach (var property in element.EnumerateObject())
        {
            if (!string.Equals(property.Name, name, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            if (found)
            {
                return false;
            }

            found = true;
            value = property.Value;
        }

        return true;
    }

    /// <summary>读取并核对扁平和嵌套的标识；无效或冲突时返回 false。</summary>
    private static bool TryResolveIdentifier(JsonElement root, JsonElement block, string name, out string? value)
    {
        value = null;
        if (!TryGetUniqueProperty(root, name, out var flat) || !TryGetUniqueProperty(block, name, out var nested) ||
            !TryReadIdentifier(flat, out var flatValue) || !TryReadIdentifier(nested, out var nestedValue) ||
            (flatValue is not null && nestedValue is not null && flatValue != nestedValue))
        {
            return false;
        }

        value = nestedValue ?? flatValue;
        return true;
    }

    /// <summary>标识验证与服务端入队约束一致，保持合法原值。</summary>
    private static bool TryReadIdentifier(JsonElement element, out string? value)
    {
        value = null;
        if (element.ValueKind is JsonValueKind.Undefined or JsonValueKind.Null)
        {
            return true;
        }

        if (element.ValueKind != JsonValueKind.String)
        {
            return false;
        }

        var text = element.GetString();
        if (string.IsNullOrWhiteSpace(text) || text.Length > 128)
        {
            return false;
        }

        foreach (var character in text)
        {
            if (char.IsControl(character))
            {
                return false;
            }
        }

        value = text;
        return true;
    }

    /// <summary>读取严格整数并核对两种结构；有 block 时 seq 不继承扁平值。</summary>
    private static bool TryResolveNumber(JsonElement root, JsonElement block, string name, bool allowFlatFallback,
        bool nonNegative, out long? value)
    {
        value = null;
        if (!TryGetUniqueProperty(root, name, out var flat) || !TryGetUniqueProperty(block, name, out var nested) ||
            !TryReadNumber(flat, nonNegative, out var flatValue) ||
            !TryReadNumber(nested, nonNegative, out var nestedValue) ||
            (flatValue.HasValue && nestedValue.HasValue && flatValue != nestedValue))
        {
            return false;
        }

        value = nestedValue ?? (allowFlatFallback ? flatValue : null);
        return true;
    }

    /// <summary>仅接受 JSON 整数及可选的非负约束，不将字符串或浮点值转换为整数。</summary>
    private static bool TryReadNumber(JsonElement element, bool nonNegative, out long? value)
    {
        value = null;
        if (element.ValueKind is JsonValueKind.Undefined or JsonValueKind.Null)
        {
            return true;
        }

        if (element.ValueKind != JsonValueKind.Number || !element.TryGetInt64(out var number) ||
            (nonNegative && number < 0))
        {
            return false;
        }

        value = number;
        return true;
    }

    /// <summary>只保存从该次原始请求独立验证的可选归属，不包含动作内容。</summary>
    private sealed class LiveActionOwnership
    {
        /// <summary>动作序号。</summary>
        public long? Seq { get; init; }
        /// <summary>意图标识。</summary>
        public string? IntentId { get; init; }
        /// <summary>动作代次。</summary>
        public long? MotorGeneration { get; init; }
        /// <summary>动作组标识。</summary>
        public string? ActionGroupId { get; init; }
    }

    /// <summary>分发客户端消息。</summary>
    private async Task DispatchAsync(LiveClientConnection client, string json,
        CancellationToken cancellationToken)
    {
        LiveClientMessage? message;
        LiveActionOwnership? ownership = null;
        string? stopRequestId = null;
        string? leaseRequestId = null;
        var processingStop = false;
        var processingLease = !TryReadLeaseProbe(json, out _, out _) && IsPotentialLeaseBind(json);
        if (processingLease && !TryReadLeaseBind(json, out leaseRequestId))
        {
            client.TryEnqueue(CreateError("LEASE_REJECTED", "租约绑定请求格式无效。", null, leaseRequestId: leaseRequestId));
            return;
        }
        try
        {
            message = JsonSerializer.Deserialize<LiveClientMessage>(json, JsonOptions);
        }
        catch (JsonException)
        {
            // JSON 结构完整但其他字段无法反序列化时，仍可回传独立验证过的动作归属。
            ownership = ReadActionOwnership(json);
            if (!TryReadStopRequestId(json, out stopRequestId) || !RegisterStopRequestId(client, stopRequestId))
            {
                stopRequestId = null;
            }
            client.TryEnqueue(CreateError("INVALID_JSON", "JSON 格式或字段无效。", ownership, stopRequestId,
                leaseRequestId: leaseRequestId));
            return;
        }

        if (message is null)
        {
            return;
        }

        try
        {
            switch (message.Type?.Trim().ToLowerInvariant())
            {
                case "ping":
                    client.TryEnqueue(new LiveServerMessage
                    {
                        Type = "pong",
                        TimestampUtc = DateTimeOffset.UtcNow
                    });
                    break;
                case "guidance":
                    await liveService.SetGuidanceAsync(new LiveGuidanceRequest
                    {
                        Text = message.Text ?? string.Empty,
                        Source = message.Source()
                    }, cancellationToken).ConfigureAwait(false);
                    break;
                case "commentary":
                    await liveService.AddCommentaryAsync(new LiveCommentaryRequest
                    {
                        Kind = message.Kind ?? "reaction",
                        Text = message.Text ?? string.Empty,
                        Emotion = message.Emotion,
                        FrameSeq = message.FrameSeq,
                        Meta = message.Meta
                    }, cancellationToken).ConfigureAwait(false);
                    break;
                case "lease.bind":
                    await _actionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                    try
                    {
                        if (leaseRequestId is null || client.LeaseRequestIds.Count >= MaxLeaseRequestIds
                            || !client.LeaseRequestIds.Add(leaseRequestId))
                        {
                            client.TryEnqueue(CreateError("LEASE_REJECTED", "租约绑定请求标识重复或容量已满。", null,
                                leaseRequestId: leaseRequestId));
                            break;
                        }
                        var credentials = message.Lease;
                        if (credentials is null || message.UnsupportedFields is { Count: > 0 }
                            || (_actionOwner is not null && _actionOwner != client.Id && _actionOwnerLease is not null
                                && _actionOwnerLease.InstanceId == credentials.InstanceId
                                && _actionOwnerLease.Epoch == credentials.Epoch))
                        {
                            client.TryEnqueue(CreateError("LEASE_REJECTED", "Live 输入租约不可用。", null,
                                leaseRequestId: leaseRequestId));
                            break;
                        }
                        var prepared = liveService.BindPreparedLease(credentials, client.Id, leaseRequestId);
                        if (prepared is null || prepared.Status != "prepared")
                        {
                            client.TryEnqueue(CreateError("LEASE_REJECTED", "Live 预备租约不可用。", null,
                                leaseRequestId: leaseRequestId));
                            break;
                        }
                        _actionOwner = client.Id;
                        _actionOwnerLeaseRequestId = leaseRequestId;
                        _actionOwnerLease = new LiveLeaseCredentialsDto
                        {
                            Secret = credentials.Secret, InstanceId = credentials.InstanceId, Epoch = credentials.Epoch
                        };
                        client.TryEnqueue(new LiveServerMessage
                        {
                            Type = "lease.bound", LeaseRequestId = leaseRequestId, SessionId = prepared.SessionId,
                            LeaseInstanceId = credentials.InstanceId, LeaseEpoch = credentials.Epoch,
                            LeaseConnectionId = prepared.ConnectionId, LeaseStatus = prepared.Status,
                            LeaseProbeId = leaseRequestId, LeaseExpiresInMs = prepared.ExpiresInMs
                        });
                    }
                    finally { _actionGate.Release(); }
                    break;
                case "lease.probe":
                case "lease.activate":
                    if (!TryReadLeaseProbe(json, out var probeId, out var probeNonce))
                    {
                        client.TryEnqueue(CreateError("LEASE_REJECTED", "租约查询格式无效。", null));
                        break;
                    }
                    await _actionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                    try
                    {
                        if (_actionOwner != client.Id || _actionOwnerLease is null
                            || _actionOwnerLeaseRequestId != probeId)
                        {
                            client.TryEnqueue(CreateError("LEASE_REJECTED", "租约绑定不可用。", null,
                                leaseRequestId: probeId));
                            break;
                        }
                        var bound = message.Type == "lease.activate"
                            ? liveService.ActivateBoundLease(_actionOwnerLease, client.Id, probeId)
                            : liveService.ProbeBoundLease(_actionOwnerLease, client.Id, probeId);
                        if (bound is null)
                        {
                            client.TryEnqueue(CreateError("LEASE_REJECTED", "租约绑定不可用。", null,
                                leaseRequestId: probeId));
                            break;
                        }
                        client.TryEnqueue(new LiveServerMessage
                        {
                            Type = "lease.bound", LeaseRequestId = probeId, SessionId = bound.SessionId,
                            LeaseInstanceId = bound.InstanceId, LeaseEpoch = bound.Epoch,
                            LeaseConnectionId = bound.ConnectionId, LeaseStatus = bound.Status,
                            LeaseProbeId = probeNonce, LeaseExpiresInMs = bound.ExpiresInMs
                        });
                    }
                    finally { _actionGate.Release(); }
                    break;
                case "actions":
                    // 在任何拒绝或字段合并之前，从原始请求提取有界且无歧义的归属。
                    ownership = ReadActionOwnership(json);
                    var block = message.Block ?? new LiveActionBlockDto
                    {
                        Seq = message.Seq,
                        Chunks = message.Chunks ?? [],
                        Absolute = message.Absolute,
                        Thought = message.Thought
                    };
                    // 非动作通用字段或未知字段不能被忽略并降级成普通键鼠。
                    if (message.ActionKind is not null || message.Kind is not null || message.Meta is not null
                        || message.Text is not null || message.Emotion is not null || message.FrameSeq is not null
                        || message.UnsupportedFields is { Count: > 0 })
                    {
                        block.ActionKind = message.ActionKind ?? message.Kind ?? "unsupported";
                    }
                    // 嵌套与扁平字段同时提供时不能产生歧义，否则无法准确关联意图。
                    if ((message.IntentId is not null && block.IntentId is not null && message.IntentId != block.IntentId) ||
                        (message.MotorGeneration is not null && block.MotorGeneration is not null && message.MotorGeneration != block.MotorGeneration) ||
                        (message.ActionGroupId is not null && block.ActionGroupId is not null && message.ActionGroupId != block.ActionGroupId))
                    {
                        throw AgentException.InvalidRequest("动作元数据的扁平字段与 block 字段不一致。");
                    }

                    block.IntentId ??= message.IntentId;
                    block.MotorGeneration ??= message.MotorGeneration;
                    block.ActionGroupId ??= message.ActionGroupId;
                    await _actionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                    try
                    {
                        // 租约动作必须来自绑定该代次的连接；服务仍逐次验证秘密。
                        if (message.Lease is not null && (_actionOwner != client.Id || _actionOwnerLease is null
                            || _actionOwnerLease.InstanceId != message.Lease.InstanceId
                            || _actionOwnerLease.Epoch != message.Lease.Epoch))
                        {
                            client.TryEnqueue(CreateError("LEASE_REJECTED", "Live 输入租约尚未绑定当前连接。", ownership));
                            break;
                        }
                        // 同一时刻只允许一个客户端控制动作，避免多路输入互相覆盖。
                        if (_actionOwner is not null && _actionOwner != client.Id && message.Lease is null)
                        {
                            client.TryEnqueue(CreateError("ACTION_REJECTED", "已有其他客户端在控制动作。", ownership));
                            break;
                        }

                        // 带序号的动作块必须严格递增，防止重放或乱序。
                        if (block.Seq is { } seq && client.LastActionSeq is { } last && seq <= last)
                        {
                            client.TryEnqueue(CreateError("ACTION_REJECTED", "动作序号必须严格递增。", ownership));
                            break;
                        }

                        var ack = await liveService.EnqueueActionsAsync(block, cancellationToken, message.Lease)
                            .ConfigureAwait(false);
                        if (ack.Accepted)
                        {
                            _actionOwner = client.Id;
                            if (message.Lease is null)
                            {
                                _actionOwnerLease = null;
                                _actionOwnerLeaseRequestId = null;
                            }
                            if (block.Seq is { } accepted)
                            {
                                client.LastActionSeq = accepted;
                            }
                        }
                        else
                        {
                            client.TryEnqueue(CreateError("ACTION_REJECTED", ack.Message ?? "动作块被拒绝。", ownership));
                        }
                    }
                    finally
                    {
                        _actionGate.Release();
                    }

                    break;
                case "dialogue":
                case "dialogue.wait":
                case "dialogue.advance":
                case "dialogue.select":
                case "dialogue_wait":
                case "dialogue_advance":
                case "dialogue_select":
                    // 目前没有对话授权与校准映射；走统一拒绝路径并停输入。
                    await liveService.EnqueueActionsAsync(new LiveActionBlockDto { ActionKind = message.Type },
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "stop_actions":
                    processingStop = true;
                    if (!TryReadStopRequestId(json, out stopRequestId))
                    {
                        throw AgentException.InvalidRequest("停止请求标识无效或不明确。");
                    }
                    if (!RegisterStopRequestId(client, stopRequestId))
                    {
                        stopRequestId = null;
                        throw AgentException.InvalidRequest("停止请求标识重复或连接的标识容量已满。");
                    }

                    // 所有 WebSocket 动作、停止与断线串行化；不改变任何客户端停止的既有权限。
                    await _actionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                    try
                    {
                        cancellationToken.ThrowIfCancellationRequested();
                        LiveStopReceiptDto? receipt = null;
                        if (stopRequestId is null) liveService.StopActions();
                        else receipt = liveService.StopActionsWithEvidence(stopRequestId);
                        _actionOwner = null;
                        _actionOwnerLease = null;
                        cancellationToken.ThrowIfCancellationRequested();
                        if (receipt is not null)
                        {
                            client.TryEnqueue(new LiveServerMessage
                            {
                                Type = "stop_actions_ack",
                                SessionId = receipt.SessionId,
                                StopRequestId = receipt.StopRequestId,
                                StopCallReturned = true,
                                Safety = receipt.Safety
                            });
                        }
                    }
                    finally
                    {
                        _actionGate.Release();
                    }
                    break;
                default:
                    client.TryEnqueue(new LiveServerMessage
                    {
                        Type = "error",
                        Code = "UNKNOWN_MESSAGE",
                        Message = $"未知消息类型：{message.Type}"
                    });
                    break;
            }
        }
        catch (AgentException exception)
        {
            client.TryEnqueue(processingLease
                ? CreateError("LEASE_REJECTED", "Live 输入租约不可用。", null, leaseRequestId: leaseRequestId)
                : CreateError(exception.Code, exception.Message, ownership, stopRequestId));
        }
        catch (OperationCanceledException) when (processingStop)
        {
            // 连接取消时尽力排队错误；发送循环可能已经关闭。
            client.TryEnqueue(CreateError("STOP_CANCELLED", "停止请求已取消，停止结果未知。", null, stopRequestId));
        }
        catch (Exception exception)
        {
            if (processingLease)
            {
                logger.LogWarning("处理租约绑定失败");
                client.TryEnqueue(CreateError("LEASE_REJECTED", "租约绑定未确认。", null, leaseRequestId: leaseRequestId));
            }
            else
            {
                logger.LogWarning(exception, "处理实时会话消息失败");
                client.TryEnqueue(CreateError("INTERNAL_ERROR", exception.Message, ownership, stopRequestId));
            }
        }
    }
}

/// <summary>LiveClientMessage 的便捷扩展。</summary>
internal static class LiveClientMessageExtensions
{
    /// <summary>读取来源字段；协议未单独定义 Source，使用 Meta.source 或默认 dsh。</summary>
    public static string Source(this LiveClientMessage message)
    {
        return message.Meta?["source"]?.GetValue<string>() ?? "dsh";
    }
}
