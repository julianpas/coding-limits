using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Drawing.Drawing2D;

namespace CodingLimitsHud;

/// <summary>
/// Borderless, semi-transparent, always-on-top HUD in the corner of the screen
/// that mirrors the coding-limits dashboard. Tray icon toggles show/hide/quit.
/// </summary>
public sealed class HudForm : Form
{
    // ── palette (mirrors dashboard.html) ────────────────────────────────────
    private static readonly Color Bg = Color.FromArgb(11, 15, 20);
    private static readonly Color CardColor = Color.FromArgb(19, 26, 34);
    private static readonly Color CardEdge = Color.FromArgb(34, 48, 64);
    private static readonly Color Ink = Color.FromArgb(230, 237, 243);
    private static readonly Color Muted = Color.FromArgb(139, 152, 165);
    private static readonly Color OkColor = Color.FromArgb(52, 211, 153);
    private static readonly Color WarnColor = Color.FromArgb(251, 191, 36);
    private static readonly Color BadColor = Color.FromArgb(248, 113, 113);
    private static readonly Color BadSoft = Color.FromArgb(252, 165, 165);
    private static readonly Color RingTrack = Color.FromArgb(26, 255, 255, 255);

    private static readonly string[] PreferredOrder = { "codex", "claude", "gemini" };
    private static readonly Dictionary<string, (string Label, Color Dot)> ProviderMeta = new()
    {
        ["codex"]  = ("Codex",  Color.FromArgb(52, 211, 153)),
        ["claude"] = ("Claude", Color.FromArgb(251, 146, 60)),
        ["gemini"] = ("Gemini", Color.FromArgb(96, 165, 250)),
    };

    // ── state ────────────────────────────────────────────────────────────────
    private readonly HudConfig _cfg;
    private readonly System.Windows.Forms.Timer _tick;
    private readonly CancellationTokenSource _cts = new();
    private readonly List<Font> _fonts = new();

    private Snapshot? _snapshot;
    private string? _lastError;
    private DateTime? _lastGoodFetch;
    private bool _fetchBusy;
    private DateTime _lastFetchStart = DateTime.MinValue;

    private NotifyIcon _tray = null!;
    private ToolStripMenuItem _showHideItem = null!;
    private ToolStripMenuItem _startItem = null!;
    private ToolStripMenuItem _stopItem = null!;
    private ToolStripMenuItem _autostartItem = null!;
    private Icon _trayIcon = null!;

    // ── gateway management ─────────────────────────────────────────────────
    private readonly Gateway? _gw;
    private bool _userStoppedGateway;
    private bool _exitStopsGateway;

    private bool _dragging;
    private Point _dragOrigin;
    private Point _dragStart;

    // ── DPI scale (1.0 at 96 dpi), derived from font metrics ───────────────
    private float S = 1f;

    // ── fonts ────────────────────────────────────────────────────────────────
    private readonly Font _fTitle = new("Segoe UI", 11f, FontStyle.Bold);
    private readonly Font _fName = new("Segoe UI", 9.75f, FontStyle.Bold);
    private readonly Font _fPill = new("Segoe UI", 7.5f, FontStyle.Bold);
    private readonly Font _fRing = new("Segoe UI", 10f, FontStyle.Bold);
    private readonly Font _fLabel = new("Segoe UI", 8.5f, FontStyle.Bold);
    private readonly Font _fReset = new("Segoe UI", 7.75f);
    private readonly Font _fSmall = new("Segoe UI", 8.25f);
    private readonly Font _fCredits = new("Segoe UI", 8.25f);
    private readonly Font _fFooter = new("Segoe UI", 7.75f);

    public HudForm()
    {
        _cfg = HudConfig.Load();
        _gw = Gateway.CreateFromConfig(_cfg);
        _fonts.AddRange([_fTitle, _fName, _fPill, _fRing, _fLabel, _fReset, _fSmall, _fCredits, _fFooter]);
        S = Math.Clamp(_fSmall.Height / 14f, 0.8f, 3f);

        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint
               | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);

        Text = "AI Limits HUD";
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.Manual;
        ShowInTaskbar = false;
        TopMost = true;
        BackColor = Bg;
        try { Opacity = _cfg.Opacity; } catch { Opacity = 0.85f; }

        ClientSize = new Size((int)(330 * S), 120);
        Location = new Point(_cfg.X, _cfg.Y);

        SetupTray();
        _tick = new System.Windows.Forms.Timer { Interval = 1000 };
        _tick.Tick += OnTick;
        _tick.Start();
    }

    // ── window chrome ────────────────────────────────────────────────────────

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        // Hide from Alt-Tab: it lives in the tray.
        int style = GetWindowLong(Handle, GWL_EXSTYLE);
        SetWindowLong(Handle, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW);
    }

    protected override void OnLoad(EventArgs e)
    {
        base.OnLoad(e);
        if (!_cfg.StartVisible)
            Visible = false;
        Relayout();
        FetchNow();
        if (_cfg.AutoStartGateway && _gw is not null)
            Task.Run(() => _gw.Start());
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        if (_exitStopsGateway && _gw is not null)
        {
            try { _gw.Stop(); } catch { /* best effort on the way out */ }
        }
        _cts.Cancel();
        _tick.Stop();
        if (_tray is not null)
        {
            _tray.Visible = false;
            _tray.Dispose();
        }
        _trayIcon?.Dispose();
        foreach (var f in _fonts)
            f.Dispose();
        _cts.Dispose();
        base.OnFormClosed(e);
    }

    // ── tray ─────────────────────────────────────────────────────────────────

    private void SetupTray()
    {
        var menu = new ContextMenuStrip();
        _showHideItem = new ToolStripMenuItem(_cfg.StartVisible ? "Hide HUD" : "Show HUD");
        _showHideItem.Click += (_, _) => ToggleVisible();

        var refreshItem = new ToolStripMenuItem("Refresh now");
        refreshItem.Click += (sender, e) => FetchNow();

        var openItem = new ToolStripMenuItem("Open dashboard");
        openItem.Click += (_, _) =>
        {
            try { Process.Start(new ProcessStartInfo(_cfg.Url) { UseShellExecute = true }); }
            catch { /* no browser / bad URL — ignore */ }
        };

        var logsItem = new ToolStripMenuItem("View logs", null, (_, _) => ViewLogs());

        _startItem = new ToolStripMenuItem("Start gateway", null, (_, _) =>
        {
            if (_gw is null)
            {
                NotifyUser("Gateway unavailable — could not find python.exe or server.py.");
                return;
            }
            _userStoppedGateway = false;
            Task.Run(() => _gw.Start());
        });

        _stopItem = new ToolStripMenuItem("Stop gateway", null, (_, _) =>
        {
            if (_gw is null)
                return;
            _userStoppedGateway = true;
            Task.Run(() => _gw.Stop());
        });

        _autostartItem = new ToolStripMenuItem("Start at login") { CheckOnClick = true };
        _autostartItem.Checked = Autostart.IsEnabled();
        _autostartItem.Click += (_, _) =>
        {
            Autostart.Set(_autostartItem.Checked);
            _cfg.Autostart = _autostartItem.Checked;
            _cfg.Save();
        };

        menu.Items.Add(_showHideItem);
        menu.Items.Add(refreshItem);
        menu.Items.Add(openItem);
        menu.Items.Add(logsItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(_startItem);
        menu.Items.Add(_stopItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(_autostartItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem("Exit (keep gateway)", null, (_, _) => Application.Exit()));
        menu.Items.Add(new ToolStripMenuItem("Exit & stop gateway", null, (_, _) =>
        {
            _exitStopsGateway = true;
            Application.Exit();
        }));

        _trayIcon = CreateTrayIcon();
        _tray = new NotifyIcon
        {
            Icon = _trayIcon,
            Text = "AI Limits",
            ContextMenuStrip = menu,
            Visible = true,
        };
        _tray.DoubleClick += (_, _) => ToggleVisible();
    }

    private void ToggleVisible()
    {
        Visible = !Visible;
        _showHideItem.Text = Visible ? "Hide HUD" : "Show HUD";
    }

    private void UpdateTrayState()
    {
        if (_tray is null)
            return;

        string title = "AI Limits";
        if (_gw is not null)
        {
            title += _gw.State switch
            {
                GatewayState.Running => " — gateway running",
                GatewayState.Starting => " — gateway starting…",
                _ => " — gateway stopped",
            };
        }
        if (_lastError is not null)
            title += " (offline)";
        if (_tray.Text != title)
            _tray.Text = title;

        if (_gw is not null)
        {
            _startItem.Enabled = _gw.State != GatewayState.Running;
            _stopItem.Enabled = _gw.State != GatewayState.Stopped;
        }
        _autostartItem.Checked = Autostart.IsEnabled();
    }

    private void ViewLogs()
    {
        if (_gw is null)
        {
            NotifyUser("Gateway unavailable — no logs to show.");
            return;
        }
        string path = _gw.LogPath;
        try
        {
            if (File.Exists(path))
                Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
            else
            {
                string dir = Path.GetDirectoryName(path) ?? ".";
                Directory.CreateDirectory(dir);
                Process.Start(new ProcessStartInfo(dir) { UseShellExecute = true });
            }
        }
        catch
        {
            NotifyUser("Could not open the logs.");
        }
    }

    private void NotifyUser(string message)
    {
        try
        {
            _tray?.ShowBalloonTip(3000, "AI Limits", message, ToolTipIcon.Info);
        }
        catch
        {
            // no tray yet — ignore
        }
    }

    private Icon CreateTrayIcon()
    {
        using var bmp = new Bitmap(32, 32);
        using (var g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.Clear(Color.Transparent);
            using (var bg = new SolidBrush(Color.FromArgb(235, 19, 26, 34)))
            using (var path = new GraphicsPath())
            {
                path.AddArc(1, 1, 12, 12, 180, 90);
                path.AddArc(19, 1, 12, 12, 270, 90);
                path.AddArc(19, 19, 12, 12, 0, 90);
                path.AddArc(1, 19, 12, 12, 90, 90);
                path.CloseFigure();
                g.FillPath(bg, path);
            }
            // Three usage bars, one per provider colour.
            DrawBar(g, 8, 16, OkColor);
            DrawBar(g, 14, 11, Color.FromArgb(251, 146, 60));
            DrawBar(g, 20, 7, Color.FromArgb(96, 165, 250));
        }
        IntPtr h = bmp.GetHicon();
        try
        {
            using var tmp = Icon.FromHandle(h);
            return (Icon)tmp.Clone();
        }
        finally
        {
            DestroyIcon(h);
        }
    }

    private static void DrawBar(Graphics g, int x, int h, Color c)
    {
        using var b = new SolidBrush(c);
        g.FillRectangle(b, x, 26 - h, 4, h);
    }

    // ── data ─────────────────────────────────────────────────────────────────

    private void OnTick(object? sender, EventArgs e)
    {
        if (_gw is not null)
        {
            _gw.Refresh();
            if (_cfg.AutoStartGateway && !_userStoppedGateway && _gw.State == GatewayState.Stopped)
                Task.Run(() => _gw.Start());
        }

        bool due = !_fetchBusy && DateTime.UtcNow - _lastFetchStart >= TimeSpan.FromSeconds(_cfg.RefreshSeconds);
        if (due)
            FetchNow();

        UpdateTrayState();
        Invalidate();
    }

    private async void FetchNow()
    {
        if (_fetchBusy || _cts.IsCancellationRequested)
            return;
        _fetchBusy = true;
        _lastFetchStart = DateTime.UtcNow;
        try
        {
            var result = await SnapshotFetcher.FetchAsync(_cfg.Url, _cfg.Token, _cts.Token);
            if (result.Data != null)
            {
                _snapshot = result.Data;
                _lastError = null;
                _lastGoodFetch = DateTime.UtcNow;
            }
            else
            {
                _lastError = result.Error;
            }
        }
        finally
        {
            _fetchBusy = false;
            UpdateTrayState();
            Relayout();
            Invalidate();
        }
    }

    // ── layout (shared by measure + paint so they can never drift) ─────────

    private sealed class HudLayout
    {
        public int H;
        public Rectangle Header = Rectangle.Empty;
        public Rectangle? Empty;
        public Rectangle Footer = Rectangle.Empty;
        public List<Card> Cards = new();
    }

    private sealed class Card
    {
        public string Name = "";
        public ProviderInfo P = null!;
        public Rectangle Bounds = Rectangle.Empty;
        public Color Edge = CardEdge;

        public RectangleF Dot;
        public Color DotColor = Muted;
        public string HeadTextStr = "";
        public Rectangle HeadText = Rectangle.Empty;

        public Rectangle Pill = Rectangle.Empty;
        public string PillText = "";
        public Color PillFg = Muted;
        public Color PillBorder = Muted;
        public Color PillFill = Color.Empty;

        public RectangleF Ring1, Ring2;
        public float? Ring1Pct, Ring2Pct;
        public string Ring1Value = "—", Ring2Value = "—";
        public string Ring1Label = "—", Ring2Label = "—";
        public string Ring1Reset = "reset n/a", Ring2Reset = "reset n/a";
        public Rectangle Label1, Label2, Reset1, Reset2;

        public Rectangle? Body;
        public string? BodyText;
        public Color BodyFg = Muted;

        public Rectangle? Separator;
        public Rectangle? Credits;
        public string? CreditsText;
    }

    private HudLayout BuildLayout()
    {
        var L = new HudLayout();
        int pad = (int)(12 * S);
        int x = pad;
        int w = (int)(330 * S) - 2 * pad;
        int y = pad;

        L.Header = new Rectangle(x, y, w, Math.Max(_fTitle.Height, _fSmall.Height) + 2);
        y += L.Header.Height + (int)(12 * S);

        var provs = _snapshot?.Providers;
        if (provs is not { Count: > 0 })
        {
            var empty = new Rectangle(x, y, w, _fSmall.Height + 10);
            L.Empty = empty;
            y += empty.Height + (int)(8 * S);
        }
        else
        {
            var order = new List<string>();
            foreach (var n in PreferredOrder)
                if (provs.ContainsKey(n))
                    order.Add(n);
            foreach (var n in provs.Keys)
                if (!order.Contains(n))
                    order.Add(n);

            foreach (var name in order)
            {
                y += BuildCard(L, x, y, w, name, provs[name]) + (int)(8 * S);
            }
            y -= (int)(8 * S);
        }

        L.Footer = new Rectangle(x, y, w, _fFooter.Height + 2);
        L.H = L.Footer.Bottom + (int)(6 * S);
        return L;
    }

    private int BuildCard(HudLayout L, int x, int y, int w, string name, ProviderInfo p)
    {
        var c = new Card { Name = name, P = p };
        int pad = (int)(10 * S);
        int innerX = x + pad;
        int innerW = w - 2 * pad;
        int iy = y + pad;

        bool disabled = p.Enabled == false;
        bool ok = p.Ok == true;

        // Pill first (right-aligned), so the header text can avoid it.
        (string text, Color fg, Color border, Color fill) = disabled
            ? ("OFF", Muted, Color.FromArgb(90, 139, 152, 165), Color.FromArgb(18, 139, 152, 165))
            : !ok
                ? ("ERROR", BadColor, Color.FromArgb(100, 248, 113, 113), Color.FromArgb(18, 248, 113, 113))
                : p.Stale
                    ? ("STALE", WarnColor, Color.FromArgb(100, 251, 191, 36), Color.FromArgb(18, 251, 191, 36))
                    : ("LIVE", OkColor, Color.FromArgb(100, 52, 211, 153), Color.FromArgb(18, 52, 211, 153));

        Size pillText = TextRenderer.MeasureText(text, _fPill);
        int pillW = pillText.Width + (int)(14 * S);
        int pillH = Math.Max(pillText.Height + (int)(3 * S), (int)(14 * S));
        int headerH = Math.Max(_fName.Height, pillH);

        c.Pill = new Rectangle(x + w - pad - pillW, iy + (headerH - pillH) / 2, pillW, pillH);
        c.PillText = text;
        c.PillFg = fg;
        c.PillBorder = border;
        c.PillFill = fill;

        int dotD = (int)(9 * S);
        c.Dot = new RectangleF(innerX, iy + (headerH - dotD) / 2f, dotD, dotD);
        c.DotColor = ProviderMeta.TryGetValue(name, out var meta) ? meta.Dot : Muted;

        string label = ProviderMeta.TryGetValue(name, out var m) ? m.Label : TitleCase(name);
        if (!string.IsNullOrWhiteSpace(p.PlanType))
            label += $"  ·  {p.PlanType!.ToLowerInvariant()} plan";
        int textX = (int)(innerX + dotD + 6 * S);
        c.HeadText = new Rectangle(textX, iy, Math.Max(1, c.Pill.Left - textX - (int)(8 * S)), headerH);
        c.HeadTextStr = label;

        iy += headerH;

        if (disabled)
        {
            iy += (int)(6 * S);
            var body = new Rectangle(innerX, iy, innerW, _fSmall.Height + 2);
            c.Body = body;
            c.BodyText = "Not configured on this gateway.";
            c.BodyFg = Muted;
            iy = body.Bottom + pad;
        }
        else if (!ok)
        {
            iy += (int)(8 * S);
            string msg = string.IsNullOrWhiteSpace(p.Error) ? "fetch failed" : p.Error!;
            var sz = TextRenderer.MeasureText(msg, _fSmall, new Size(innerW, 0),
                TextFormatFlags.WordBreak | TextFormatFlags.PathEllipsis);
            int maxH = (_fSmall.Height + 2) * 2;
            var body = new Rectangle(innerX, iy, innerW, Math.Min(sz.Height, maxH));
            c.Body = body;
            c.BodyText = msg;
            c.BodyFg = BadSoft;
            iy = body.Bottom + pad;
        }
        else
        {
            iy += (int)(10 * S);
            int ringD = (int)(60 * S);
            int colW = innerW / 2;

            c.Ring1 = RingRect(innerX, iy, colW, ringD);
            c.Ring2 = RingRect(innerX + colW, iy, colW, ringD);
            c.Ring1Pct = p.ShortWindow?.Remaining;
            c.Ring2Pct = p.LongWindow?.Remaining;
            c.Ring1Value = RingValue(p.ShortWindow);
            c.Ring2Value = RingValue(p.LongWindow);
            c.Ring1Label = p.ShortWindow?.Label ?? "short";
            c.Ring2Label = p.LongWindow?.Label ?? "long";
            c.Ring1Reset = ResetText(p.ShortWindow);
            c.Ring2Reset = ResetText(p.LongWindow);

            int ly = iy + ringD + (int)(4 * S);
            c.Label1 = new Rectangle(innerX, ly, colW, _fLabel.Height + 1);
            c.Label2 = new Rectangle(innerX + colW, ly, colW, _fLabel.Height + 1);
            c.Reset1 = new Rectangle(innerX, c.Label1.Bottom, colW, _fReset.Height + 1);
            c.Reset2 = new Rectangle(innerX + colW, c.Label1.Bottom, colW, _fReset.Height + 1);
            iy = c.Reset1.Bottom + (int)(6 * S);

            string? credits = p.Credits?.Format();
            if (!string.IsNullOrWhiteSpace(credits))
            {
                c.Separator = new Rectangle(innerX, iy, innerW, 1);
                iy += (int)(5 * S);
                var creditsRect = new Rectangle(innerX, iy, innerW, _fCredits.Height + 1);
                c.Credits = creditsRect;
                c.CreditsText = credits;
                iy = creditsRect.Bottom;
            }
            iy += pad;
        }

        c.Bounds = new Rectangle(x, y, w, iy - y);
        if (!disabled && !ok)
            c.Edge = Color.FromArgb(128, 248, 113, 113);

        L.Cards.Add(c);
        return c.Bounds.Height;
    }

    private static RectangleF RingRect(int colX, int y, int colW, int d)
        => new(colX + (colW - d) / 2f, y, d, d);

    private static string RingValue(WindowInfo? w)
    {
        float? r = w?.Remaining;
        return r is null ? "—" : $"{(int)Math.Round(r.Value)}%";
    }

    private static string ResetText(WindowInfo? w)
    {
        string? cd = FormatCountdown(w?.ResetsAt);
        return cd == null ? "reset time n/a" : "resets in " + cd;
    }

    private static string? FormatCountdown(long? resetsAt)
    {
        if (resetsAt == null)
            return null;
        long ms = resetsAt.Value * 1000 - DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        if (ms <= 0)
            return "just now";
        int m = (int)(ms / 60000);
        if (m < 60)
            return m + "m";
        int h = m / 60;
        if (h < 24)
            return h + "h " + (m % 60) + "m";
        int d = h / 24;
        return d + "d " + (h % 24) + "h";
    }

    private static string TitleCase(string s) =>
        s.Length == 0 ? s : char.ToUpperInvariant(s[0]) + s.Substring(1);

    private void Relayout()
    {
        int h = BuildLayout().H;
        if (h > 0 && Math.Abs(h - ClientSize.Height) > 1)
            ClientSize = new Size(ClientSize.Width, h);
    }

    // ── painting ─────────────────────────────────────────────────────────────

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;
        g.Clear(Bg);

        var L = BuildLayout();

        string title = _snapshot?.DeviceName is { Length: > 0 } d ? d : "AI Limits";
        TextRenderer.DrawText(g, title, _fTitle, L.Header, Ink, TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
        string updated = _lastGoodFetch is { } t ? "updated " + SinceText(t) : "waiting for data…";
        TextRenderer.DrawText(g, updated, _fSmall, L.Header, Muted,
            TextFormatFlags.Right | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis);

        if (L.Empty is { } emp)
            TextRenderer.DrawText(g, "Waiting for the first snapshot…", _fSmall, emp, Muted,
                TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);

        foreach (var c in L.Cards)
            DrawCard(g, c);

        Color footFg = _lastError != null ? BadSoft : Muted;
        string left = _lastError != null
            ? "gateway unreachable" + (Truncate(_lastError!, 32) is { Length: > 0 } tr ? " — " + tr : "")
            : "gateway online";
        TextRenderer.DrawText(g, left, _fFooter, L.Footer, footFg,
            TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis);
        TextRenderer.DrawText(g, $"refresh {_cfg.RefreshSeconds}s", _fFooter, L.Footer, Muted,
            TextFormatFlags.Right | TextFormatFlags.VerticalCenter);
    }

    private void DrawCard(Graphics g, Card c)
    {
        using (var path = RoundRect(c.Bounds, (int)(12 * S)))
        {
            using var fill = new SolidBrush(c.P.Enabled == false ? Color.FromArgb(14, 20, 27) : CardColor);
            g.FillPath(fill, path);
            using var pen = new Pen(c.Edge, 1f);
            g.DrawPath(pen, path);
        }

        using (var b = new SolidBrush(c.DotColor))
            g.FillEllipse(b, c.Dot);

        TextRenderer.DrawText(g, c.HeadTextStr, _fName, c.HeadText,
            c.P.Enabled == false ? Muted : Ink,
            TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis);

        using (var path = RoundRect(c.Pill, c.Pill.Height / 2))
        {
            using var fill = new SolidBrush(c.PillFill);
            g.FillPath(fill, path);
            using var pen = new Pen(c.PillBorder, 1f);
            g.DrawPath(pen, path);
        }
        TextRenderer.DrawText(g, c.PillText, _fPill, c.Pill, c.PillFg,
            TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);

        if (c.Body is { } body)
            TextRenderer.DrawText(g, c.BodyText!, _fSmall, body, c.BodyFg,
                TextFormatFlags.Left | TextFormatFlags.VerticalCenter
                | TextFormatFlags.WordBreak | TextFormatFlags.EndEllipsis);

        DrawRing(g, c.Ring1, c.Ring1Pct, c.Ring1Value);
        DrawRing(g, c.Ring2, c.Ring2Pct, c.Ring2Value);
        TextRenderer.DrawText(g, c.Ring1Label, _fLabel, c.Label1, Ink,
            TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
        TextRenderer.DrawText(g, c.Ring2Label, _fLabel, c.Label2, Ink,
            TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
        TextRenderer.DrawText(g, c.Ring1Reset, _fReset, c.Reset1, Muted,
            TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
        TextRenderer.DrawText(g, c.Ring2Reset, _fReset, c.Reset2, Muted,
            TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);

        if (c.Credits is { } cr)
        {
            var sep = c.Separator!.Value;
            using var pen = new Pen(Color.FromArgb(16, 255, 255, 255));
            g.DrawLine(pen, sep.Left, sep.Top, sep.Right, sep.Top);
            TextRenderer.DrawText(g, c.CreditsText!, _fCredits, cr, Muted,
                TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis);
        }
    }

    private void DrawRing(Graphics g, RectangleF r, float? pct, string valueText)
    {
        using var trackPen = new Pen(RingTrack, (float)(6 * S));
        g.DrawArc(trackPen, r, 0f, 359.9f);

        if (pct is { } p)
        {
            p = Math.Clamp(p, 0f, 100f);
            Color col = p >= 50 ? OkColor : p >= 20 ? WarnColor : BadColor;
            using var pen = new Pen(col, (float)(6 * S))
            {
                StartCap = LineCap.Round,
                EndCap = LineCap.Round,
            };
            g.DrawArc(pen, r, -90f, 360f * p / 100f);
        }

        var valueRect = new Rectangle(
            (int)Math.Round(r.Left), (int)Math.Round(r.Top),
            (int)Math.Round(r.Width), (int)Math.Round(r.Height));
        TextRenderer.DrawText(g, valueText, _fRing, valueRect, Ink,
            TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
    }

    private static GraphicsPath RoundRect(Rectangle r, int radius)
    {
        int d = radius * 2;
        var path = new GraphicsPath();
        path.AddArc(r.Left, r.Top, d, d, 180, 90);
        path.AddArc(r.Right - d, r.Top, d, d, 270, 90);
        path.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
        path.AddArc(r.Left, r.Bottom - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }

    private static string Truncate(string s, int n) =>
        s.Length <= n ? s : s[..n] + "…";

    private static string SinceText(DateTime utc)
    {
        int s = (int)(DateTime.UtcNow - utc).TotalSeconds;
        if (s < 5) return "just now";
        if (s < 60) return s + "s ago";
        int m = s / 60;
        if (m < 60) return m + "m ago";
        return (m / 60) + "h ago";
    }

    // ── dragging ─────────────────────────────────────────────────────────────

    protected override void OnMouseDown(MouseEventArgs e)
    {
        base.OnMouseDown(e);
        if (e.Button == MouseButtons.Left)
        {
            _dragging = true;
            _dragStart = e.Location;
            _dragOrigin = Location;
        }
    }

    protected override void OnMouseMove(MouseEventArgs e)
    {
        base.OnMouseMove(e);
        if (!_dragging)
            return;
        var wa = Screen.FromPoint(Location).WorkingArea;
        int nx = _dragOrigin.X + (e.X - _dragStart.X);
        int ny = _dragOrigin.Y + (e.Y - _dragStart.Y);
        nx = Math.Clamp(nx, wa.Left - ClientSize.Width / 2, wa.Right - ClientSize.Width / 2);
        ny = Math.Clamp(ny, wa.Top, wa.Bottom - 32);
        Location = new Point(nx, ny);
    }

    protected override void OnMouseUp(MouseEventArgs e)
    {
        base.OnMouseUp(e);
        if (_dragging && e.Button == MouseButtons.Left)
        {
            _dragging = false;
            if (Math.Abs(e.X - _dragStart.X) + Math.Abs(e.Y - _dragStart.Y) > 3)
            {
                _cfg.X = Location.X;
                _cfg.Y = Location.Y;
                _cfg.Save();
            }
        }
    }

    // ── win32 ────────────────────────────────────────────────────────────────

    private const int GWL_EXSTYLE = -20;
    private const int WS_EX_TOOLWINDOW = 0x00800000;

    [DllImport("user32.dll")]
    private static extern int GetWindowLong(IntPtr hWnd, int nIndex);

    [DllImport("user32.dll")]
    private static extern int SetWindowLong(IntPtr hWnd, int nIndex, int dwNewLong);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyIcon(IntPtr hIcon);
}
