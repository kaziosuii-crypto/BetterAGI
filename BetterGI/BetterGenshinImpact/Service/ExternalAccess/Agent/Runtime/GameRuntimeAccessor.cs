using BetterGenshinImpact.GameTask.Runtime;
using System.Threading;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Runtime;

/// <summary>
/// 基于 <see cref="GameRuntimeService"/> 的运行环境访问器。
/// </summary>
public sealed class GameRuntimeAccessor(GameRuntimeService gameRuntimeService, AgentControlState controlState) : IGameRuntimeAccessor
{
    /// <inheritdoc />
    public GameRuntime? Current => gameRuntimeService.Current;

    /// <inheritdoc />
    public bool IsRunning => gameRuntimeService.IsRunning;

    /// <inheritdoc />
    public async Task<GameRuntime?> StartAsync(CancellationToken cancellationToken)
    {
        using var operation = controlState.EnterStartOperation();
        return await gameRuntimeService.StartAsync(cancellationToken).ConfigureAwait(false);
    }

    /// <inheritdoc />
    public async Task StopAsync(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        await gameRuntimeService.StopAsync().ConfigureAwait(false);
        cancellationToken.ThrowIfCancellationRequested();
    }
}
