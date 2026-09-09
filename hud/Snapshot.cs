using System.Net.Http;
using System.Text.Json;

namespace CodingLimitsHud;

/// <summary>One rate-limit window (5h / 7d / RPM / RPD …) as returned by /api/v1/snapshot.</summary>
public sealed class WindowInfo
{
    public string? Label { get; set; }
    public float? UsedPercent { get; set; }
    public float? RemainingPercent { get; set; }
    public int? WindowDurationMins { get; set; }
    public long? ResetsAt { get; set; }

    /// <summary>Remaining percent, falling back to 100 - usedPercent when the gateway omits it.</summary>
    public float? Remaining =>
        RemainingPercent
        ?? (UsedPercent != null ? Math.Clamp(100f - UsedPercent.Value, 0f, 100f) : null);
}

/// <summary>Credit/overage info when the provider exposes it.</summary>
public sealed class CreditsInfo
{
    public bool? Enabled { get; set; }
    public bool? Unlimited { get; set; }
    public bool? HasCredits { get; set; }

    /// <summary>Balance may arrive as a JSON number or a string (e.g. "0").</summary>
    public JsonElement? Balance { get; set; }
    public long? UsedCreditsCents { get; set; }
    public long? MonthlyLimitCents { get; set; }

    /// <summary>Human-readable one-liner, or null when there is nothing worth showing.</summary>
    public string? Format()
    {
        if (Unlimited == true)
            return "Unlimited credits";

        if (HasCredits == true && Balance.HasValue && TryParseMoney(Balance.Value, out decimal dollars))
            return $"Credit balance · ${dollars:N2}";

        if (UsedCreditsCents.HasValue)
        {
            string used = (UsedCreditsCents.Value / 100m).ToString("N2");
            return MonthlyLimitCents.HasValue
                ? $"Monthly credits · ${used} of ${(MonthlyLimitCents.Value / 100m):N2} used"
                : $"Monthly credits · ${used} used";
        }

        return null;
    }

    private static bool TryParseMoney(JsonElement el, out decimal dollars)
    {
        dollars = 0m;
        try
        {
            switch (el.ValueKind)
            {
                case JsonValueKind.Number:
                    dollars = el.GetDecimal();
                    return true;
                case JsonValueKind.String:
                    return decimal.TryParse(el.GetString(), out dollars);
                default:
                    return false;
            }
        }
        catch
        {
            return false;
        }
    }
}

/// <summary>One provider block (codex / claude / gemini / …).</summary>
public sealed class ProviderInfo
{
    public bool? Enabled { get; set; }
    public bool? Ok { get; set; }
    public string? Source { get; set; }
    public string? PlanType { get; set; }
    public WindowInfo? ShortWindow { get; set; }
    public WindowInfo? LongWindow { get; set; }
    public CreditsInfo? Credits { get; set; }
    public bool Stale { get; set; }
    public string? Error { get; set; }
}

/// <summary>The normalized snapshot document from GET /api/v1/snapshot.</summary>
public sealed class Snapshot
{
    public bool Ok { get; set; }
    public string? FetchedAt { get; set; }
    public string? Version { get; set; }
    public string? DeviceName { get; set; }
    public Dictionary<string, ProviderInfo> Providers { get; set; } = new();
}

public sealed record FetchResult(Snapshot? Data, string? Error)
{
    public static FetchResult Success(Snapshot s) => new(s, null);
    public static FetchResult Failure(string e) => new(null, e);
}

public static class SnapshotFetcher
{
    private static readonly HttpClient Http = new(new SocketsHttpHandler
    {
        PooledConnectionLifetime = TimeSpan.FromMinutes(5),
    })
    {
        Timeout = TimeSpan.FromSeconds(25),
    };

    private static readonly JsonSerializerOptions JsonOpts = new(JsonSerializerDefaults.Web)
    {
        PropertyNameCaseInsensitive = true,
        ReadCommentHandling = JsonCommentHandling.Skip,
        AllowTrailingCommas = true,
    };

    public static async Task<FetchResult> FetchAsync(string baseUrl, string? token, CancellationToken ct = default)
    {
        string url = baseUrl.TrimEnd('/') + "/api/v1/snapshot";
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, url);
            if (!string.IsNullOrWhiteSpace(token))
                req.Headers.Add("X-Gauge-Token", token!);

            using var res = await Http.SendAsync(req, ct).ConfigureAwait(false);
            if (!res.IsSuccessStatusCode)
                return FetchResult.Failure($"HTTP {(int)res.StatusCode}");

            string body = await res.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            var snap = JsonSerializer.Deserialize<Snapshot>(body, JsonOpts);
            return snap == null ? FetchResult.Failure("empty response") : FetchResult.Success(snap);
        }
        catch (OperationCanceledException)
        {
            return FetchResult.Failure("cancelled");
        }
        catch (Exception ex)
        {
            return FetchResult.Failure(ex.Message);
        }
    }
}
