using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

internal static class SetupRegistration
{
    private const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\GitgoManaged";
    internal static string LocalData
    {
        get
        {
            var folder = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (String.IsNullOrEmpty(folder)) folder = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            if (!LocalPath(folder)) throw new InvalidOperationException("Windows Local AppData is unavailable.");
            return folder;
        }
    }
    internal static string Maintenance { get { return Path.Combine(LocalData, "GitgoSetup"); } }
    internal static string DefaultRoot { get { return Path.Combine(LocalData, "GitgoInstalled"); } }
    private static string Uninstaller { get { return Path.Combine(Maintenance, "GitgoUninstall.exe"); } }

    internal static bool SamePath(string left, string right)
    {
        return String.Equals(left == null ? null : left.TrimEnd('\\'),
            right == null ? null : right.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);
    }

    private static Dictionary<string, object> ReadObject(string file)
    {
        if (!File.Exists(file) || (File.GetAttributes(file) & FileAttributes.ReparsePoint) != 0
            || new FileInfo(file).Length > 262144) return null;
        return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(file));
    }

    private static string Field(Dictionary<string, object> record, string name)
    {
        object value;
        return record != null && record.TryGetValue(name, out value) ? value as string : null;
    }

    private static bool LocalPath(string path)
    {
        return !String.IsNullOrEmpty(path) && Regex.IsMatch(path, @"^[A-Za-z]:\\[^\r\n""]+$")
            && SamePath(Path.GetFullPath(path), path.TrimEnd('\\'));
    }

    internal static string InstalledId(string root)
    {
        try
        {
            if (!LocalPath(root)) return null;
            var program = Path.Combine(root, "program");
            var record = ReadObject(Path.Combine(program, "control", "registration.json"));
            var id = Field(record, "installation_id");
            return Field(record, "product_id") == "gitgo-terminal" && SamePath(Field(record, "root"), program)
                && id != null && Regex.IsMatch(id, @"\A[0-9a-f]{32}\z") ? id : null;
        }
        catch (ArgumentException) { return null; }
    }

    private static string RegisteredValue(string field)
    {
        using (var key = Registry.CurrentUser.OpenSubKey(UninstallKey))
            return key == null ? null : key.GetValue(field) as string;
    }

    internal static string ExistingRoot(string defaultRoot, string lastAttempt)
    {
        var registered = RegisteredValue("InstallLocation");
        if (InstalledId(registered) != null) return registered;
        if (InstalledId(defaultRoot) != null) return defaultRoot;
        try
        {
            var recent = Field(ReadObject(lastAttempt), "install_root");
            if (InstalledId(recent) != null) return recent;
        }
        catch (ArgumentException) { }
        var fragments = Path.Combine(LocalData, "Microsoft", "Windows Terminal", "Fragments", "Gitgo");
        if (!Directory.Exists(fragments)) return null;
        foreach (var path in Directory.EnumerateFiles(fragments, "*.json"))
        {
            try
            {
                object rawProfiles;
                var data = ReadObject(path);
                if (data == null || !data.TryGetValue("profiles", out rawProfiles)) continue;
                var profiles = rawProfiles as System.Collections.IEnumerable;
                if (profiles == null) continue;
                foreach (var item in profiles)
                {
                    var command = Field(item as Dictionary<string, object>, "commandline");
                    var match = Regex.Match(command ?? "",
                        @"\A(?:""([^""]+)""|(\S+)) --gitgo-internal-role managed-launch\z");
                    if (!match.Success) continue;
                    var host = match.Groups[1].Success ? match.Groups[1].Value : match.Groups[2].Value;
                    const string suffix = @"\program\bootstrap\gitgo-host\gitgo-host.exe";
                    if (!host.EndsWith(suffix, StringComparison.OrdinalIgnoreCase)) continue;
                    var root = host.Substring(0, host.Length - suffix.Length);
                    if (InstalledId(root) != null) return root;
                }
            }
            catch (ArgumentException) { }
        }
        return null;
    }

    internal static T Exclusive<T>(Func<T> run)
    {
        var sid = WindowsIdentity.GetCurrent().User.Value;
        using (var mutex = new Mutex(false, @"Global\Gitgo.Setup." + sid))
        {
            try
            {
                if (!mutex.WaitOne(0)) throw new InvalidOperationException("Another Gitgo installation operation is in progress.");
            }
            catch (AbandonedMutexException) { }
            try { return run(); }
            finally { mutex.ReleaseMutex(); }
        }
    }

    private static void AssertUnredirected(string path)
    {
        for (var current = path; !String.IsNullOrEmpty(current); current = Path.GetDirectoryName(current))
            if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidOperationException("An installation path is redirected.");
    }

    internal static void Publish(string root)
    {
        var id = InstalledId(root);
        if (id == null) throw new InvalidOperationException("Could not verify the installed Gitgo identity.");
        var other = RegisteredValue("InstallLocation");
        if (other != null && !SamePath(other, root) && Directory.Exists(other))
            throw new InvalidOperationException("Gitgo already has an entry in Installed Apps.");
        var version = Field(ReadObject(Path.Combine(root, "program", "active", "product.json")), "version");
        if (String.IsNullOrEmpty(version)) throw new InvalidOperationException("Could not verify the installed version.");
        Directory.CreateDirectory(Maintenance);
        AssertUnredirected(Maintenance);
        if (File.Exists(Uninstaller)) AssertUnredirected(Uninstaller);
        var temporary = Uninstaller + "." + Guid.NewGuid().ToString("N");
        try
        {
            using (var source = Assembly.GetExecutingAssembly().GetManifestResourceStream("Gitgo.Uninstaller"))
            using (var output = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                source.CopyTo(output);
            if (File.Exists(Uninstaller)) File.Replace(temporary, Uninstaller, null);
            else File.Move(temporary, Uninstaller);
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
        using (var key = Registry.CurrentUser.CreateSubKey(UninstallKey))
        {
            key.SetValue("DisplayName", "Gitgo");
            key.SetValue("Publisher", "Gitgo");
            key.SetValue("DisplayVersion", version);
            key.SetValue("InstallLocation", root);
            key.SetValue("DisplayIcon", "\"" + Path.Combine(root, "program", "active", "gitgo.exe") + "\",0");
            key.SetValue("UninstallString", "\"" + Uninstaller + "\"");
            key.SetValue("QuietUninstallString", "\"" + Uninstaller + "\" --quiet");
            key.SetValue("NoModify", 1, RegistryValueKind.DWord);
            key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
            key.SetValue("GitgoInstallationId", id);
        }
    }

#if UNINSTALL
    private static string Local(string chinese, string english)
    {
        return System.Globalization.CultureInfo.CurrentUICulture.TwoLetterISOLanguageName == "zh" ? chinese : english;
    }

    private static void AssertIdle(string root)
    {
        var lockPath = Path.Combine(root, "program", "control", "runtime.lock");
        try
        {
            using (new FileStream(lockPath, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) { }
        }
        catch (IOException error)
        {
            throw new InvalidOperationException("Close Gitgo before uninstalling it.", error);
        }
    }

    private static bool ChooseRemoval(string title, out bool removeConfig, out bool removeMetadata)
    {
        using (var form = new Form { Text = title, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink,
            StartPosition = FormStartPosition.CenterScreen, FormBorderStyle = FormBorderStyle.FixedDialog,
            MinimizeBox = false, MaximizeBox = false })
        {
            var content = new FlowLayoutPanel { AutoSize = true, FlowDirection = FlowDirection.TopDown,
                WrapContents = false, Padding = new Padding(16) };
            var keep = new RadioButton { AutoSize = true, Checked = true,
                Text = Local("仅卸载软件，保留配置", "Remove software and keep settings") };
            var clean = new RadioButton { AutoSize = true,
                Text = Local("卸载软件并清除配置与已保存的凭据", "Remove software, settings, and saved credentials") };
            var metadata = new CheckBox { AutoSize = true, Enabled = false, Margin = new Padding(24, 4, 3, 4),
                Text = Local("同时清除已登记项目中可核验的 .gitgo 目录", "Also remove verified .gitgo folders in registered projects") };
            clean.CheckedChanged += (sender, args) => {
                metadata.Enabled = clean.Checked;
                if (!clean.Checked) metadata.Checked = false;
            };
            var note = new Label { AutoSize = true, MaximumSize = new Size(420, 0),
                Text = Local("两种方式都保留项目目录和源码。项目外的会话数据库也会保留。",
                    "Both options preserve project folders and source files. Conversation databases outside projects are also kept.") };
            var buttons = new FlowLayoutPanel { AutoSize = true, FlowDirection = FlowDirection.RightToLeft, Dock = DockStyle.Fill };
            var cancel = new Button { Text = Local("取消", "Cancel"), AutoSize = true, DialogResult = DialogResult.Cancel };
            var next = new Button { Text = Local("继续", "Continue"), AutoSize = true, DialogResult = DialogResult.OK };
            buttons.Controls.AddRange(new Control[] { cancel, next });
            content.Controls.AddRange(new Control[] { keep, clean, metadata, note, buttons });
            form.Controls.Add(content);
            form.AcceptButton = next;
            form.CancelButton = cancel;
            var confirmed = form.ShowDialog() == DialogResult.OK;
            removeConfig = clean.Checked;
            removeMetadata = clean.Checked && metadata.Checked;
            return confirmed;
        }
    }

    private static Dictionary<string, object> RunDataCleanup(string host, string arguments)
    {
        string script;
        using (var data = Assembly.GetExecutingAssembly().GetManifestResourceStream("Gitgo.UninstallData"))
        using (var reader = new StreamReader(data)) script = reader.ReadToEnd();
        var quotedScript = "exec(__import__('base64').b64decode('" + Convert.ToBase64String(Encoding.UTF8.GetBytes(script)) + "'))";
        var start = new ProcessStartInfo(host, "--gitgo-internal-role python -c \"" + quotedScript + "\" " + arguments) {
            UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Path.GetDirectoryName(host),
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
        };
        start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        using (var child = Process.Start(start))
        {
            var output = child.StandardOutput.ReadToEndAsync();
            var error = child.StandardError.ReadToEndAsync();
            if (!child.WaitForExit(60000))
                throw new InvalidOperationException(Local("配置清理仍未完成。请稍后重试。", "Settings cleanup has not finished. Try again later."));
            if (child.ExitCode != 0)
                throw new InvalidOperationException(Local("无法清除 Gitgo 配置。", "Could not remove Gitgo settings.") + "\r\n" + error.Result);
            return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(output.Result);
        }
    }

    private static IEnumerable<string> Paths(Dictionary<string, object> plan, string key)
    {
        return ((System.Collections.IEnumerable)plan[key]).Cast<string>();
    }

    private static string FormatPaths(IEnumerable<string> paths)
    {
        return String.Join("\r\n", paths.Select(path => "  " + path));
    }

    private static bool ConfirmCleanup(string title, string summary)
    {
        using (var form = new Form { Text = title, ClientSize = new Size(590, 300), MinimumSize = new Size(390, 230),
            StartPosition = FormStartPosition.CenterScreen, MinimizeBox = false })
        {
            var content = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2,
                Padding = new Padding(16) };
            content.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            content.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            var paths = new TextBox { Text = summary, ReadOnly = true, Multiline = true,
                ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill };
            var buttons = new FlowLayoutPanel { AutoSize = true, FlowDirection = FlowDirection.RightToLeft, Dock = DockStyle.Fill };
            var cancel = new Button { Text = Local("取消", "Cancel"), AutoSize = true, DialogResult = DialogResult.Cancel };
            var remove = new Button { Text = Local("确认卸载", "Confirm uninstall"), AutoSize = true, DialogResult = DialogResult.OK };
            buttons.Controls.AddRange(new Control[] { cancel, remove });
            content.Controls.Add(paths, 0, 0);
            content.Controls.Add(buttons, 0, 1);
            form.Controls.Add(content);
            form.ActiveControl = cancel;
            form.CancelButton = cancel;
            return form.ShowDialog() == DialogResult.OK;
        }
    }

    private static int Uninstall(bool removeConfig, bool removeMetadata, string title)
    {
        var root = RegisteredValue("InstallLocation");
        var id = RegisteredValue("GitgoInstallationId");
        if (id == null || InstalledId(root) != id)
            throw new InvalidOperationException("The registered Gitgo installation could not be verified.");
        AssertUnredirected(root);
        if (Directory.EnumerateFileSystemEntries(root).Any(path =>
                !Directory.Exists(path) || (Path.GetFileName(path) != "program" && Path.GetFileName(path) != "commands")))
            throw new InvalidOperationException("The installation folder contains unrelated files.");
        var folders = new Stack<string>();
        folders.Push(root);
        while (folders.Count != 0)
        {
            foreach (var path in Directory.EnumerateFileSystemEntries(folders.Pop()))
            {
                var attributes = File.GetAttributes(path);
                if ((attributes & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidOperationException("The installation contains a redirected item.");
                if (Path.GetFileName(path).Equals(".git", StringComparison.OrdinalIgnoreCase)
                    || (Path.GetFileName(path) == "project-id" && Path.GetFileName(Path.GetDirectoryName(path)) == ".gitgo"))
                    throw new InvalidOperationException("A project was found inside the installation folder and was kept.");
                if ((attributes & FileAttributes.Directory) != 0) folders.Push(path);
            }
        }
        AssertIdle(root);
        var program = Path.Combine(root, "program");
        var host = Path.Combine(program, "active", "internal", "gitgo-host", "gitgo-host.exe");
        if (removeConfig)
        {
            var profilePath = Path.Combine(program, "control", "setup-profile.json");
            var options = removeMetadata ? " --project-metadata" : "";
            if (File.Exists(profilePath))
            {
                var profile = ReadObject(profilePath);
                if (Field(profile, "profile") != "fresh")
                    throw new InvalidOperationException("The installed Gitgo profile could not be verified.");
                options += " --profile-id " + id;
            }
            var plan = RunDataCleanup(host, "preview" + options);
            var token = Field(plan, "token");
            if (token == null || !Regex.IsMatch(token, @"\A[0-9a-f]{64}\z"))
                throw new InvalidOperationException("The verified Gitgo cleanup plan is unavailable.");
            var files = Paths(plan, "files").Concat(Paths(plan, "metadata")).ToArray();
            var skipped = Paths(plan, "skipped").ToArray();
            if (files.Length != 0 || skipped.Length != 0)
            {
                var summary = Local("以下 Gitgo 数据将随软件删除。", "The following Gitgo data will be removed with the software.")
                    + "\r\n" + FormatPaths(files);
                if (skipped.Length != 0)
                    summary += "\r\n\r\n" + Local("无法核验并会保留的项目元数据：", "Unverified project metadata will be kept:")
                        + "\r\n" + FormatPaths(skipped);
                if (!ConfirmCleanup(title, summary)) return 1;
                RunDataCleanup(host, "clean" + options + " --expect " + token);
            }
        }
        using (var child = Process.Start(new ProcessStartInfo(host,
            "--gitgo-internal-role native-entry --action remove --root \"" + program + "\"")
            { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Maintenance }))
        {
            if (!child.WaitForExit(30000) || child.ExitCode != 0)
                throw new InvalidOperationException("Gitgo could not verify and remove its launch entries.");
        }
        AssertIdle(root);
        SafeRecycle.Send(root);
        if (Directory.Exists(root)) throw new InvalidOperationException("The Gitgo installation was not removed.");
        Registry.CurrentUser.DeleteSubKey(UninstallKey);
        return 0;
    }

    [STAThread]
    private static int Main(string[] args)
    {
        var quiet = args.Length == 1 && args[0] == "--quiet";
        if (!quiet && args.Length != 0) return 2;
        var title = Local("卸载 Gitgo", "Gitgo Uninstall");
        var removeConfig = false;
        var removeMetadata = false;
        if (!quiet)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            if (!ChooseRemoval(title, out removeConfig, out removeMetadata)) return 1;
        }
        try { return Exclusive(() => Uninstall(removeConfig, removeMetadata, title)); }
        catch (Exception error)
        {
            if (!quiet) MessageBox.Show(error.Message, title, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }
    }
#endif
}
