// Playlist Downloader — Windows tray controller.
//
// Sits in the notification area, owns the local Python backend and opens the
// web UI in the default browser. The Windows counterpart of the macOS menu bar
// app: same menu, same status lines, same behaviour.
//
// Built with launcher/windows/build.ps1 (or the GitHub Actions release job).

using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Windows.Forms;
using Microsoft.Win32;

namespace PlaylistDownloader;

internal static class Program
{
    internal const string Repo = "brianbcyangmusic-pixel/playlist-downloader";
    internal const int PreferredPort = 8765;

    [STAThread]
    private static void Main()
    {
        using var mutex = new Mutex(true, "PlaylistDownloaderTray", out var isFirst);
        if (!isFirst) return; // already in the tray; the old instance owns the menu

        Application.EnableVisualStyles();
        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new TrayApp());
    }
}

internal sealed class TrayApp : ApplicationContext
{
    private readonly NotifyIcon _tray;
    private readonly ContextMenuStrip _menu = new();
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(5) };
    // A forced update check goes out to GitHub (the backend allows it 15 s).
    private readonly HttpClient _slowHttp = new() { Timeout = TimeSpan.FromSeconds(20) };
    private readonly SynchronizationContext? _ui;

    private readonly ToolStripMenuItem _header = Row("Playlist Downloader", bold: true);
    private readonly ToolStripMenuItem _backendRow = Row("Backend: starting…");
    private readonly ToolStripMenuItem _queueRow = Row("Queue: —");
    private readonly ToolStripMenuItem _toolsRow = Row("Tools: checking…");
    private readonly ToolStripMenuItem _updatesRow = Row("Updates: checking…");
    private readonly ToolStripMenuItem _openRow = Row("Open web page");
    private readonly ToolStripMenuItem _toggleRow = Row("Stop backend");
    private readonly ToolStripMenuItem _autostartRow = Row("Start with Windows", checkable: true);

    private Process? _server;
    private bool _ownsServer;
    private bool _starting;
    private bool _polling;
    private int _port = Program.PreferredPort;
    private string _version = "?";
    private DateTime _lastUpdateCheck = DateTime.MinValue;
    private StreamWriter? _log;

    private string BaseUrl => $"http://127.0.0.1:{_port}";

    public TrayApp()
    {
        _ui = SynchronizationContext.Current;
        _tray = new NotifyIcon
        {
            Icon = LoadIcon(),
            Text = "Playlist Downloader",
            Visible = true,
            ContextMenuStrip = _menu,
        };
        _tray.DoubleClick += (_, _) => OpenWeb();

        BuildMenu();
        StartBackend();

        var timer = new System.Windows.Forms.Timer { Interval = 5000 };
        timer.Tick += async (_, _) => await RefreshAsync();
        timer.Start();
    }

    // ---------------------------------------------------------------- menu

    private static ToolStripMenuItem Row(string text, bool bold = false, bool checkable = false)
    {
        var prototype = SystemFonts.MenuFont ?? SystemFonts.DefaultFont;
        return new(text)
        {
            Enabled = false,
            Font = bold ? new Font(prototype, FontStyle.Bold) : prototype,
            CheckOnClick = checkable,
        };
    }

    private void BuildMenu()
    {
        foreach (var row in new[] { _header, _backendRow, _queueRow, _toolsRow, _updatesRow })
            _menu.Items.Add(row);
        _menu.Items.Add(new ToolStripSeparator());

        _openRow.Enabled = false;
        _openRow.Click += (_, _) => OpenWeb();
        _menu.Items.Add(_openRow);

        _toggleRow.Click += (_, _) => ToggleBackend();
        _menu.Items.Add(_toggleRow);

        var dataRow = new ToolStripMenuItem("Open data folder");
        dataRow.Click += (_, _) => OpenFolder(DataDir());
        _menu.Items.Add(dataRow);

        _autostartRow.CheckOnClick = true;
        _autostartRow.Checked = AutostartEnabled();
        _autostartRow.Click += (_, _) => SetAutostart(_autostartRow.Checked);
        _menu.Items.Add(_autostartRow);

        var updateRow = new ToolStripMenuItem("Check for updates now");
        updateRow.Click += async (_, _) => await CheckUpdatesAsync(force: true);
        _menu.Items.Add(updateRow);

        _menu.Items.Add(new ToolStripSeparator());
        var quit = new ToolStripMenuItem("Quit Playlist Downloader");
        quit.Click += (_, _) => Quit();
        _menu.Items.Add(quit);
    }

    private void SetStatus(string text)
    {
        _backendRow.Text = text;
        var tip = $"Playlist Downloader — {text}";
        _tray.Text = tip.Length > 63 ? tip[..63] : tip;
    }

    // ------------------------------------------------------------- backend

    private void StartBackend()
    {
        if (_server != null || _starting) return;
        _starting = true;
        SetStatus("Backend: starting…");

        if (Ping(_port))
        {
            _ownsServer = false;
            _starting = false;
            SetStatus($"Backend: running (attached) · port {_port}");
            _openRow.Enabled = true;
            _toggleRow.Text = "Stop backend";
            _ = RefreshAsync();
            return;
        }

        var launch = FindBackend();
        if (launch == null)
        {
            _starting = false;
            SetStatus("Backend: not installed");
            _toggleRow.Text = "Show setup instructions";
            _toggleRow.Click += (_, _) => ShowSetup();
            return;
        }

        _port = FreePort(Program.PreferredPort);
        var info = new ProcessStartInfo
        {
            FileName = launch.Value.Exe,
            Arguments = launch.Value.Args + $" --port {_port} --no-browser",
            WorkingDirectory = launch.Value.WorkingDir,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        info.Environment["PYTHONUNBUFFERED"] = "1";
        info.Environment["PYTHONDONTWRITEBYTECODE"] = "1";
        info.Environment["PYTHONPATH"] = launch.Value.WorkingDir;
        info.Environment["PLAYLIST_DOWNLOADER_PROJECT"] = launch.Value.WorkingDir;

        try
        {
            _log = new StreamWriter(Path.Combine(DataDir(), "launcher.log"), append: true) { AutoFlush = true };
            _log.WriteLine($"\n=== launcher start {DateTime.Now:u} (port {_port}) ===");

            var process = new Process { StartInfo = info, EnableRaisingEvents = true };
            process.OutputDataReceived += (_, e) => Log(e.Data);
            process.ErrorDataReceived += (_, e) => Log(e.Data);
            process.Exited += (_, _) => _ui?.Post(_ =>
            {
                if (_ownsServer && _server == process)
                {
                    _server = null;
                    _ownsServer = false;
                    SetStatus("Backend: stopped");
                    _openRow.Enabled = false;
                    _toggleRow.Text = "Start backend";
                }
            }, null);

            if (!process.Start())
            {
                _starting = false;
                SetStatus("Backend: failed to launch");
                return;
            }
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            _server = process;
            _ownsServer = true;
        }
        catch (Exception ex)
        {
            _starting = false;
            SetStatus("Backend: failed to launch");
            Log($"launch failed: {ex.Message}");
            return;
        }

        _toggleRow.Text = "Stop backend";
        WaitUntilReady(100);
    }

    private readonly record struct Launch(string Exe, string Args, string WorkingDir);

    private Launch? FindBackend()
    {
        var baseDir = AppContext.BaseDirectory;
        var onedir = Path.Combine(baseDir, "backend", "PlaylistDownloaderBackend", "PlaylistDownloaderBackend.exe");
        if (File.Exists(onedir)) return new Launch(onedir, "", Path.GetDirectoryName(onedir)!);

        var onefile = Path.Combine(baseDir, "backend", "PlaylistDownloaderBackend.exe");
        if (File.Exists(onefile)) return new Launch(onefile, "", baseDir);

        // Development layout: `python -m app.server` from the repo.
        foreach (var exe in new[] { Path.Combine(baseDir, "python.exe"), "python.exe", "py.exe" })
        {
            if (exe.Contains(Path.DirectorySeparatorChar) && !File.Exists(exe)) continue;
            if (Directory.Exists(Path.Combine(baseDir, "app")) || File.Exists(Path.Combine(baseDir, "app", "server.py")))
                return new Launch(exe, "-m app.server", baseDir);
        }
        return null;
    }

    private void WaitUntilReady(int attempts)
    {
        if (attempts <= 0)
        {
            _starting = false;
            SetStatus("Backend: did not start");
            MessageBox.Show(
                $"The backend did not answer on port {_port}.\n\nSee {Path.Combine(DataDir(), "launcher.log")} for details.",
                "Playlist Downloader", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }
        Task.Delay(300).ContinueWith(_ =>
        {
            if (Ping(_port))
            {
                _ui?.Post(__ =>
                {
                    _starting = false;
                    SetStatus($"Backend: running · port {_port}");
                    _openRow.Enabled = true;
                    _ = RefreshAsync();
                }, null);
            }
            else
            {
                WaitUntilReady(attempts - 1);
            }
        });
    }

    private void StopBackend()
    {
        if (!_ownsServer) return;
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, $"{BaseUrl}/api/shutdown");
            _http.Send(request);
        }
        catch
        {
            // The server may already be gone; the process check below decides.
        }

        try
        {
            if (_server is { HasExited: false })
            {
                if (!_server.WaitForExit(3000)) _server.Kill(entireProcessTree: true);
            }
        }
        catch
        {
            // Nothing else to do: never block Quit on a stuck child.
        }
        _server = null;
        _ownsServer = false;
    }

    private void ToggleBackend()
    {
        if (_ownsServer)
        {
            StopBackend();
            SetStatus("Backend: stopped");
            _openRow.Enabled = false;
            _toggleRow.Text = "Start backend";
        }
        else
        {
            StartBackend();
        }
    }

    // -------------------------------------------------------------- polling

    private async Task RefreshAsync()
    {
        if (_polling) return;
        _polling = true;
        try
        {
            var state = await GetJsonAsync("/api/state");
            if (state is null) return;
            Render(state.Value);

            if (DateTime.UtcNow - _lastUpdateCheck > TimeSpan.FromMinutes(5))
            {
                _lastUpdateCheck = DateTime.UtcNow;
                var update = await GetJsonAsync("/api/update/check", _slowHttp);
                if (update is not null) RenderUpdate(update.Value);
            }
        }
        finally
        {
            _polling = false;
        }
    }

    private void Render(JsonElement state)
    {
        _version = Text(state, "version");
        _header.Text = $"Playlist Downloader v{_version}";
        SetStatus($"Backend: running · port {_port}");

        var run = Prop(state, "run");
        var stats = Prop(state, "stats");
        var downloading = Bool(run, "running");
        _queueRow.Text =
            $"{(downloading ? "Downloading" : "Idle")} · {Int(stats, "done")} done · " +
            $"{Int(stats, "pending")} pending · {Int(stats, "failed")} failed";

        var tools = Prop(state, "tools");
        var yt = Bool(tools, "ytdlp_ok");
        var ff = Bool(tools, "ffmpeg_ok");
        var js = Bool(tools, "js_ok");
        _toolsRow.Text = $"Tools: yt-dlp {(yt ? "OK" : "MISSING")} · FFmpeg {(ff ? "OK" : "MISSING")}" +
                         (js ? "" : " · JS runtime missing");
        if (!yt || !ff) _toolsRow.Enabled = true;
    }

    private void RenderUpdate(JsonElement info)
    {
        if (Bool(info, "available"))
        {
            var latest = Text(info, "latest");
            _updatesRow.Text = $"Update available: v{latest} (you have v{_version})";
            _updatesRow.Enabled = true;
            _updatesRow.Click -= OpenRelease;
            _updatesRow.Click += OpenRelease;
        }
        else
        {
            _updatesRow.Text = $"Updates: v{_version} is up to date";
            _updatesRow.Enabled = false;
        }
    }

    private async Task CheckUpdatesAsync(bool force)
    {
        if (!Ping(_port))
        {
            // The usual cause: the backend quit (e.g. after an update attempt)
            // while the tray app kept running. Bring it back.
            StartBackend();
            MessageBox.Show("The app's background service had stopped. It is starting again now; " +
                "choose \"Check for updates now\" again in a few seconds.",
                "Playlist Downloader", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        var info = await GetJsonAsync("/api/update/check" + (force ? "?force=1" : ""), _slowHttp);
        if (info is null)
        {
            MessageBox.Show("GitHub did not answer in time. Check your internet connection and try again.",
                "Playlist Downloader", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }
        RenderUpdate(info.Value);
        if (Bool(info.Value, "available"))
        {
            var latest = Text(info.Value, "latest");
            var answer = MessageBox.Show(
                $"Version {latest} is available.\n\nOpen the release page?",
                "Update available", MessageBoxButtons.YesNo, MessageBoxIcon.Information);
            if (answer == DialogResult.Yes) OpenRelease();
        }
        else
        {
            MessageBox.Show($"This build is v{_version}.", "You're up to date",
                MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
    }

    private void OpenRelease(object? sender = null, EventArgs? e = null) =>
        Shell("https://github.com/" + Program.Repo + "/releases/latest");

    // --------------------------------------------------------------- actions

    private void OpenWeb()
    {
        if (!Ping(_port))
        {
            MessageBox.Show("The backend is not running. Start it from the tray menu first.",
                "Playlist Downloader", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        Shell(BaseUrl);
    }

    private void ShowSetup()
    {
        var text = "No backend was found next to this app.\n\n" +
                   "Run scripts\\build_windows.ps1 on a Windows machine (or use the " +
                   "zip from GitHub Releases), keeping the backend folder beside PlaylistDownloader.exe.";
        MessageBox.Show(text, "Backend not installed", MessageBoxButtons.OK, MessageBoxIcon.Information);
    }

    private void Quit()
    {
        StopBackend();
        _tray.Visible = false;
        _tray.Dispose();
        _log?.Dispose();
        ExitThread();
    }

    // --------------------------------------------------------------- helpers

    private bool Ping(int port)
    {
        try
        {
            using var client = new TcpClient();
            var task = client.ConnectAsync(IPAddress.Loopback, port);
            if (!task.Wait(800)) return false;
            using var stream = client.GetStream();
            var request = Encoding.ASCII.GetBytes($"GET /api/ping HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n");
            stream.Write(request, 0, request.Length);
            using var reader = new StreamReader(stream);
            var head = reader.ReadToEnd();
            return head.StartsWith("HTTP/1.0 200") || head.StartsWith("HTTP/1.1 200");
        }
        catch
        {
            return false;
        }
    }

    private async Task<JsonElement?> GetJsonAsync(string path, HttpClient? client = null)
    {
        try
        {
            var text = await (client ?? _http).GetStringAsync(BaseUrl + path);
            using var doc = JsonDocument.Parse(text);
            return doc.RootElement.Clone();
        }
        catch
        {
            return null;
        }
    }

    private static JsonElement Prop(JsonElement element, string name) =>
        element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var value)
            ? value : default;

    private static string Text(JsonElement element, string name) =>
        Prop(element, name) is { ValueKind: JsonValueKind.String } value ? value.GetString() ?? "" : "";

    private static bool Bool(JsonElement element, string name) =>
        Prop(element, name) is { ValueKind: JsonValueKind.True };

    private static int Int(JsonElement element, string name) =>
        Prop(element, name) is { ValueKind: JsonValueKind.Number } value && value.TryGetInt32(out var number)
            ? number : 0;

    private static int FreePort(int start)
    {
        for (var port = start; port < start + 20; port++)
        {
            try
            {
                var listener = new TcpListener(IPAddress.Loopback, port);
                listener.Start();
                listener.Stop();
                return port;
            }
            catch (SocketException)
            {
                // Busy; try the next one.
            }
        }
        return start;
    }

    private static string DataDir()
    {
        var path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            "PlaylistDownloader");
        Directory.CreateDirectory(path);
        return path;
    }

    private static Icon LoadIcon()
    {
        try
        {
            using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("PlaylistDownloader.app.ico");
            if (stream != null) return new Icon(stream);
        }
        catch
        {
            // Fall through to the stock icon.
        }
        return SystemIcons.Application;
    }

    private void Log(string? line)
    {
        if (string.IsNullOrEmpty(line) || _log == null) return;
        lock (_log) _log.WriteLine(line);
    }

    private static void Shell(string target)
    {
        try
        {
            Process.Start(new ProcessStartInfo(target) { UseShellExecute = true });
        }
        catch
        {
            // No default browser/folder handler: silently ignore.
        }
    }

    private static void OpenFolder(string path) => Shell(path);

    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string RunValue = "PlaylistDownloader";

    private static bool AutostartEnabled()
    {
        using var key = Registry.CurrentUser.OpenSubKey(RunKey);
        return key?.GetValue(RunValue) is string value && value.Contains("PlaylistDownloader", StringComparison.OrdinalIgnoreCase);
    }

    private static void SetAutostart(bool enabled)
    {
        using var key = Registry.CurrentUser.CreateSubKey(RunKey);
        if (enabled) key.SetValue(RunValue, $"\"{Application.ExecutablePath}\"");
        else key.DeleteValue(RunValue, throwOnMissingValue: false);
    }
}
