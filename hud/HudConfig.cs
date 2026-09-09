using System.Text.Json;

namespace CodingLimitsHud;

/// <summary>
/// HUD settings. Loaded from <c>hud.config.json</c> next to the executable,
/// overridable via CL_HUD_* environment variables.
/// </summary>
public sealed class HudConfig
{
    public string Url { get; set; } = "http://127.0.0.1:8765";
    public string Token { get; set; } = "";
    public int RefreshSeconds { get; set; } = 30;
    public float Opacity { get; set; } = 0.85f;
    public int X { get; set; } = 8;
    public int Y { get; set; } = 8;
    public bool StartVisible { get; set; } = true;

    // ── gateway management ─────────────────────────────────────────────────
    /// <summary>Path to the Python interpreter that runs <c>server.py</c> (default: auto-detect).</summary>
    public string? PythonPath { get; set; }
    /// <summary>Directory containing <c>server.py</c> (default: walk up from the exe).</summary>
    public string? RepoRoot { get; set; }
    /// <summary>Start the gateway automatically when it is not running (default: true).</summary>
    public bool AutoStartGateway { get; set; } = true;
    /// <summary>Keep the login autostart entry in place (default: true).</summary>
    public bool Autostart { get; set; } = true;

    public static string ConfigPath => Path.Combine(AppContext.BaseDirectory, "hud.config.json");

    public static HudConfig Load()
    {
        var cfg = new HudConfig();
        try
        {
            if (File.Exists(ConfigPath))
            {
                var loaded = JsonSerializer.Deserialize<HudConfig>(File.ReadAllText(ConfigPath));
                if (loaded != null)
                    cfg = loaded;
            }
        }
        catch
        {
            // Corrupt or unreadable config: fall back to defaults.
        }

        var envUrl = Environment.GetEnvironmentVariable("CL_HUD_URL");
        if (!string.IsNullOrWhiteSpace(envUrl))
            cfg.Url = envUrl.Trim();

        var envToken = Environment.GetEnvironmentVariable("CL_HUD_TOKEN");
        if (!string.IsNullOrWhiteSpace(envToken))
            cfg.Token = envToken;

        var envRefresh = Environment.GetEnvironmentVariable("CL_HUD_REFRESH_SECONDS");
        if (int.TryParse(envRefresh, out int r) && r >= 5)
            cfg.RefreshSeconds = r;

        cfg.RefreshSeconds = Math.Clamp(cfg.RefreshSeconds, 5, 3600);
        cfg.Opacity = Math.Clamp(cfg.Opacity, 0.2f, 1f);
        if (string.IsNullOrWhiteSpace(cfg.Url))
            cfg.Url = "http://127.0.0.1:8765";
        return cfg;
    }

    /// <summary>Best-effort persistence (used when the HUD is dragged to a new spot).</summary>
    public void Save()
    {
        try
        {
            File.WriteAllText(ConfigPath, JsonSerializer.Serialize(this, new JsonSerializerOptions
            {
                WriteIndented = true,
            }));
        }
        catch
        {
            // Read-only location etc.: not fatal.
        }
    }
}
