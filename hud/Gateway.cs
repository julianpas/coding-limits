using System.Diagnostics;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;
using Microsoft.Win32;

namespace CodingLimitsHud;

public enum GatewayState
{
    Stopped,
    Starting,
    Running,
}

/// <summary>
/// Starts, monitors and stops the Python coding-limits gateway (<c>server.py</c>)
/// as a child process, logging its output to <c>logs/gateway.log</c>.
/// </summary>
public sealed class Gateway
{
    private readonly string _python;
    private readonly string _repoRoot;
    private readonly int _port;

    private readonly object _sync = new();
    private readonly object _logSync = new();
    private Process? _proc;
    private GatewayState _state = GatewayState.Stopped;
    private string? _lastError;

    public Gateway(string python, string repoRoot, int port)
    {
        _python = python;
        _repoRoot = repoRoot;
        _port = port;
    }

    public int Port { get { lock (_sync) return _port; } }
    public string RepoRoot { get { lock (_sync) return _repoRoot; } }
    public string LogPath => Path.Combine(RepoRoot, "logs", "gateway.log");

    public GatewayState State
    {
        get { lock (_sync) return _state; }
    }

    public string? LastError
    {
        get { lock (_sync) return _lastError; }
    }

    /// <summary>PID of the child process this app started (null when adopted/external).</summary>
    public int? ChildPid
    {
        get { lock (_sync) return _proc?.HasExited == false ? _proc.Id : null; }
    }

    // ── resolution ────────────────────────────────────────────────────────────

    /// <summary>Build a gateway from the current config; null when python or server.py cannot be found.</summary>
    public static Gateway? CreateFromConfig(HudConfig? cfg = null)
    {
        var c = cfg ?? HudConfig.Load();
        var repo = FindRepoRoot(c);
        var py = ResolvePython(c);
        if (repo is null || py is null)
            return null;
        return new Gateway(py, repo, PortFromUrl(c.Url));
    }

    public static string? ResolvePython(HudConfig cfg)
    {
        string?[] candidates =
        {
            cfg.PythonPath,
            Environment.GetEnvironmentVariable("CL_GATEWAY_PYTHON"),
            @"C:\Python313\python.exe",
        };
        foreach (var c in candidates)
        {
            if (!string.IsNullOrWhiteSpace(c))
            {
                var p = c.Trim().Trim('"');
                if (File.Exists(p))
                    return p;
            }
        }

        // Last resort: plain `python` / `python3` on the PATH — verify it runs.
        foreach (var name in new[] { "python", "python3" })
        {
            try
            {
                var psi = new ProcessStartInfo(name, "--version")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                };
                using var p = Process.Start(psi);
                if (p is null)
                    continue;
                if (p.WaitForExit(3000) && p.ExitCode == 0)
                {
                    string outp = p.StandardOutput.ReadToEnd() + p.StandardError.ReadToEnd();
                    if (outp.Contains("Python"))
                        return name;
                }
            }
            catch
            {
                // not on PATH — try the next name
            }
        }
        return null;
    }

    /// <summary>Locate the directory containing <c>server.py</c>: config first, then walk up from the exe.</summary>
    public static string? FindRepoRoot(HudConfig cfg)
    {
        if (!string.IsNullOrWhiteSpace(cfg.RepoRoot))
        {
            var p = cfg.RepoRoot.Trim().Trim('"');
            if (File.Exists(Path.Combine(p, "server.py")))
                return Path.GetFullPath(p);
        }

        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (int i = 0; i < 8 && dir is not null; i++, dir = dir.Parent)
        {
            if (File.Exists(Path.Combine(dir.FullName, "server.py")))
                return dir.FullName;
        }
        return null;
    }

    public static int PortFromUrl(string url)
    {
        return Uri.TryCreate(url, UriKind.Absolute, out var u) && u.Port > 0 ? u.Port : 8765;
    }

    // ── status ────────────────────────────────────────────────────────────────

    public bool IsPortUp(int timeoutMs = 750)
    {
        try
        {
            using var c = new TcpClient();
            var ar = c.BeginConnect("127.0.0.1", _port, null, null);
            if (ar.AsyncWaitHandle.WaitOne(timeoutMs) && c.Connected)
            {
                c.EndConnect(ar);
                return true;
            }
            return false;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>PID of whatever process is LISTENING on the gateway port (may be an external gateway).</summary>
    public static int? FindListeningPid(int port)
    {
        try
        {
            var psi = new ProcessStartInfo("netstat", "-ano -p tcp")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
            };
            using var p = Process.Start(psi);
            if (p is null)
                return null;
            string outp = p.StandardOutput.ReadToEnd();
            p.WaitForExit(5000);
            var re = new Regex(
                $@"TCP\s+\S*:{port}\s+\S+\s+LISTENING\s+(\d+)",
                RegexOptions.IgnoreCase | RegexOptions.Multiline);
            var m = re.Match(outp);
            if (m.Success && int.TryParse(m.Groups[1].Value, out int pid))
                return pid;
        }
        catch
        {
            // netstat unavailable — treat as no listener
        }
        return null;
    }

    /// <summary>Reconcile state with reality (light: one local TCP probe).</summary>
    public void Refresh()
    {
        lock (_sync)
        {
            // Short timeout: this runs on the UI thread every tick; a dead
            // port must not freeze the window for hundreds of ms.
            if (IsPortUp(50))
            {
                _state = GatewayState.Running;
                _lastError = null;
            }
            else if (_proc is null || _proc.HasExited)
            {
                _state = GatewayState.Stopped;
            }
            // else: child alive, port not bound yet → still Starting
        }
    }

    // ── lifecycle ─────────────────────────────────────────────────────────────

    /// <summary>Start the gateway if it is not already serving. Safe to call repeatedly.</summary>
    public void Start()
    {
        lock (_sync)
        {
            if (IsPortUp())
            {
                _state = GatewayState.Running; // someone else's gateway — adopt it
                _lastError = null;
                return;
            }
            if (_proc is { HasExited: false })
                return; // already starting / running as our child

            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = _python,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WorkingDirectory = _repoRoot,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                };
                psi.ArgumentList.Add("server.py");

                var proc = Process.Start(psi)
                    ?? throw new InvalidOperationException("Process.Start failed for " + _python);
                proc.OutputDataReceived += (_, e) => AppendLog(e.Data);
                proc.ErrorDataReceived += (_, e) => AppendLog(e.Data);
                proc.EnableRaisingEvents = true;

                _proc = proc;
                _state = GatewayState.Starting;
                _lastError = null;

                AppendLog($"\n--- gateway started by CodingLimitsHud {DateTime.Now:yyyy-MM-dd HH:mm:ss} (pid {proc.Id}) ---");

                proc.BeginOutputReadLine();
                proc.BeginErrorReadLine();
            }
            catch (Exception ex)
            {
                _state = GatewayState.Stopped;
                _lastError = ex.Message;
            }
        }
    }

    /// <summary>
    /// Stop the gateway: kills our child process; if a foreign gateway is still
    /// serving the port, terminates the process that owns it (netstat lookup).
    /// </summary>
    public void Stop()
    {
        Process? proc;
        lock (_sync)
        {
            proc = _proc;
            _proc = null;
        }

        if (proc is { HasExited: false })
        {
            try
            {
                proc.Kill(entireProcessTree: true);
                proc.WaitForExit(5000);
            }
            catch
            {
                // best effort
            }
        }

        if (IsPortUp())
        {
            int? pid = FindListeningPid(_port);
            if (pid is not null)
            {
                try
                {
                    using var external = Process.GetProcessById(pid.Value);
                    external.Kill();
                }
                catch
                {
                    // not our business — leave it running
                }
            }
        }

        lock (_sync)
        {
            _state = GatewayState.Stopped;
        }
    }

    // ── logging ───────────────────────────────────────────────────────────────

    private static readonly Encoding LogEncoding = new UTF8Encoding(false);

    private void AppendLog(string? line)
    {
        if (string.IsNullOrEmpty(line))
            return;
        lock (_logSync)
        {
            try
            {
                var dir = Path.Combine(_repoRoot, "logs");
                Directory.CreateDirectory(dir);
                var path = Path.Combine(dir, "gateway.log");
                // Open-append-close per line instead of holding a long-lived
                // write handle: a resident .NET write handle makes the file
                // unreadable for default-share readers (ReadAllBytes, editors),
                // while a python-held handle does not. The volume is tiny
                // (a handful of lines per fetch cycle), so the churn is free.
                using var fs = new FileStream(path, FileMode.Append, FileAccess.Write,
                    FileShare.Read | FileShare.Write | FileShare.Delete);
                using var sw = new StreamWriter(fs, LogEncoding) { AutoFlush = true };
                sw.WriteLine(line);
            }
            catch
            {
                // disk full / locked — logging is best-effort, never crash over it
            }
        }
    }
}

/// <summary>Login autostart via the HKCU Run key.</summary>
public static class Autostart
{
    private const string RunSubKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string ValueName = "CodingLimitsHud";

    public static string ExePath =>
        Environment.ProcessPath
        ?? Path.Combine(AppContext.BaseDirectory, "CodingLimitsHud.exe");

    public static bool IsEnabled()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunSubKey);
            return key?.GetValue(ValueName) is string s && s.Length > 0;
        }
        catch
        {
            return false;
        }
    }

    public static void Set(bool enable)
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunSubKey, writable: true)
                ?? Registry.CurrentUser.CreateSubKey(RunSubKey);
            if (enable)
                key.SetValue(ValueName, $"\"{ExePath}\"");
            else
                key.DeleteValue(ValueName, throwOnMissingValue: false);
        }
        catch
        {
            // best-effort (odd registry state, policy, etc.)
        }
    }
}

/// <summary>Command-line gateway actions (run headless, exit codes 0/1).</summary>
public static class GatewayCli
{
    public static int Start()
    {
        var gw = Gateway.CreateFromConfig();
        if (gw is null)
        {
            Console.Error.WriteLine("gateway unavailable: could not locate python.exe or server.py");
            return 1;
        }
        if (gw.IsPortUp())
        {
            Console.WriteLine($"gateway already running (pid {PidText(Gateway.FindListeningPid(gw.Port))})");
            return 0;
        }

        gw.Start();
        for (int i = 0; i < 24 && !gw.IsPortUp(); i++)
            Thread.Sleep(500);

        if (gw.IsPortUp())
        {
            Console.WriteLine($"gateway is running on port {gw.Port} (pid {PidText(Gateway.FindListeningPid(gw.Port))})");
            return 0;
        }
        Console.Error.WriteLine($"gateway failed to start: {gw.LastError ?? "port never came up"}");
        return 1;
    }

    public static int Stop()
    {
        var gw = Gateway.CreateFromConfig();
        if (gw is null)
        {
            Console.Error.WriteLine("gateway unavailable: could not locate server.py");
            return 1;
        }
        if (!gw.IsPortUp())
        {
            Console.WriteLine("gateway is not running");
            return 0;
        }

        int? pid = Gateway.FindListeningPid(gw.Port);
        gw.Stop();
        for (int i = 0; i < 10 && gw.IsPortUp(); i++)
            Thread.Sleep(500);

        if (gw.IsPortUp())
        {
            Console.Error.WriteLine($"could not stop the gateway (pid {PidText(pid)})");
            return 1;
        }
        Console.WriteLine($"gateway stopped (pid {PidText(pid)})");
        return 0;
    }

    public static int SetAutostart(bool enable)
    {
        Autostart.Set(enable);
        bool ok = enable ? Autostart.IsEnabled() : !Autostart.IsEnabled();
        Console.WriteLine(ok
            ? (enable ? $"registered at login: {Autostart.ExePath}" : "login autostart removed")
            : (enable ? "failed to register autostart" : "failed to remove autostart"));
        return ok ? 0 : 1;
    }

    private static string PidText(int? pid) => pid?.ToString() ?? "?";

    public static void PrintHelp()
    {
        Console.WriteLine("""
            CodingLimitsHud — AI Limits HUD + gateway manager

            With no arguments, runs the HUD (single instance; tray icon in the system tray).

            Gateway actions (run headless):
              --start-gateway    Start the gateway if it is not already serving, then exit
              --stop-gateway     Stop the gateway (ours or the process on port 8765), then exit
              --autostart-on     Register this app to start at login
              --autostart-off    Remove the login autostart entry
            """);
    }
}
