namespace CodingLimitsHud;

internal static class Program
{
    private static Mutex? _instanceMutex;

    [STAThread]
    private static void Main(string[] args)
    {
        // Headless actions first (run from a console, scripts, or shortcuts).
        foreach (var a in args)
        {
            if (a is "--help" or "-h")
            {
                GatewayCli.PrintHelp();
                return;
            }

            int? code = a.ToLowerInvariant() switch
            {
                "--start-gateway" or "--stop-gateway" or "--autostart-on" or "--autostart-off" => RunCli(a),
                _ => null,
            };
            if (code is not null)
                Environment.Exit(code.Value);
        }

        // Keep the login autostart entry in place (self-heals on every launch).
        if (HudConfig.Load().Autostart)
            Autostart.Set(true);

        // Single instance: the tray icon belongs to one process only.
        _instanceMutex = new Mutex(true, @"Local\CodingLimitsHud.SingleInstance", out bool createdNew);
        if (!createdNew)
            return;

        ApplicationConfiguration.Initialize();

        // Never let a background fetch exception kill the HUD.
        Application.ThreadException += (_, _) => { };
        TaskScheduler.UnobservedTaskException += (_, e) => e.SetObserved();

        Application.Run(new HudForm());
    }

    private static int RunCli(string a) => a.ToLowerInvariant() switch
    {
        "--start-gateway" => GatewayCli.Start(),
        "--stop-gateway" => GatewayCli.Stop(),
        "--autostart-on" => GatewayCli.SetAutostart(true),
        "--autostart-off" => GatewayCli.SetAutostart(false),
        _ => 1,
    };
}
