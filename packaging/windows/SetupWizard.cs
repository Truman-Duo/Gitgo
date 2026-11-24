using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed class SetupProgress
{
    public readonly string Stage;
    public readonly int Completed, Total;

    public SetupProgress(string stage, int completed = 0, int total = 0)
    {
        Stage = stage;
        Completed = completed;
        Total = total;
    }
}

internal sealed class SetupResult
{
    public readonly int ExitCode;
    public readonly string Output, Errors, Launcher;

    public SetupResult(int exitCode, string output, string errors, string launcher)
    {
        ExitCode = exitCode;
        Output = output;
        Errors = errors;
        Launcher = launcher;
    }

    private Dictionary<string, object> Receipt()
    {
        try { return new JavaScriptSerializer().DeserializeObject(Output) as Dictionary<string, object>; }
        catch (ArgumentException) { return null; }
    }

    public bool ManagedInstallConfirmed()
    {
        var receipt = Receipt();
        object managed, version;
        return receipt != null && receipt.TryGetValue("managed", out managed) && Equals(managed, true)
            && receipt.TryGetValue("version", out version) && !String.IsNullOrWhiteSpace(version as string);
    }

    public bool RepairedEntries()
    {
        var receipt = Receipt();
        object action;
        return receipt != null && receipt.TryGetValue("action", out action) && Equals(action, "repaired_entries");
    }

    public string DesktopState()
    {
        var receipt = Receipt();
        object value;
        if (receipt != null && receipt.TryGetValue("native_entry", out value))
        {
            var entry = value as Dictionary<string, object>;
            if (entry != null && entry.TryGetValue("desktop_shortcut", out value))
            {
                var shortcut = value as Dictionary<string, object>;
                if (shortcut != null && shortcut.TryGetValue("state", out value))
                    return value as string;
            }
        }
        return null;
    }
}

internal sealed class SetupWizard : Form
{
    private enum Page { Welcome, License, Configuration, Progress }

    private readonly Panel content = new Panel { Dock = DockStyle.Fill };
    private readonly Label heading = new Label { AutoSize = true };
    private readonly Button primary = new Button { AutoSize = true };
    private readonly Button secondary = new Button { AutoSize = true };
    private readonly TextBox directory = new TextBox { Dock = DockStyle.Fill };
    private readonly CheckBox consent = new CheckBox { AutoSize = true };
    private readonly CheckBox desktop = new CheckBox { AutoSize = true };
    private readonly Label message = new Label { Dock = DockStyle.Fill, AutoEllipsis = true };
    private readonly Label welcome = new Label { Dock = DockStyle.Fill };
    private readonly Label installation = new Label { AutoSize = true, Margin = new Padding(0, 12, 0, 0) };
    private readonly ProgressBar progress = new ProgressBar { Dock = DockStyle.Top, Style = ProgressBarStyle.Marquee };
    private readonly TableLayoutPanel agreement, options;
    private readonly Func<string, string, Action<SetupProgress>, SetupResult> install;
    private readonly Action<string> open;
    private readonly string statusPath;
    private string selectedRoot;
    private SetupResult result;
    private Page page;
    private bool busy, failed;
    private readonly Action<int> applyLayoutMetrics;
    private Font bodyFont, headingFont;
    private int currentDpi;
    private const int WmDpiChanged = 0x02E0;
    public int ExitCode { get; private set; }

    private static string Local(string chinese, string english)
    {
        return CultureInfo.CurrentUICulture.TwoLetterISOLanguageName == "zh" ? chinese : english;
    }

    private static TableLayoutPanel Grid(int columns)
    {
        return new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = columns, Padding = new Padding(0) };
    }

    private static Label LabelText(string text)
    {
        return new Label { Text = text, AutoSize = true, Margin = new Padding(0, 10, 0, 6) };
    }

    [DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(IntPtr window);

    [StructLayout(LayoutKind.Sequential)]
    private struct NativeBounds { public int Left, Top, Right, Bottom; }

    private static int Pixels(int logical, int dpi) { return (logical * dpi + 48) / 96; }

    protected override void OnLoad(EventArgs args)
    {
        int dpi;
        try { dpi = (int)GetDpiForWindow(Handle); }
        catch (EntryPointNotFoundException) { using (var graphics = CreateGraphics()) dpi = (int)graphics.DpiY; }
        ApplyDpi(dpi, null);
        base.OnLoad(args);
    }

    protected override void WndProc(ref Message message)
    {
        if (message.Msg == WmDpiChanged && applyLayoutMetrics != null)
        {
            var rect = (NativeBounds)Marshal.PtrToStructure(message.LParam, typeof(NativeBounds));
            ApplyDpi((int)(message.WParam.ToInt64() & 0xffff), Rectangle.FromLTRB(rect.Left, rect.Top, rect.Right, rect.Bottom));
            message.Result = IntPtr.Zero;
            return;
        }
        base.WndProc(ref message);
    }

    private void ApplyDpi(int dpi, Rectangle? suggested)
    {
        if (dpi <= 0 || dpi == currentDpi) return;
        SuspendLayout();
        try
        {
            currentDpi = dpi;
            var oldBody = bodyFont;
            var oldHeading = headingFont;
            // Pixel fonts explicitly regenerated at the monitor's native DPI.
            var family = Local("Microsoft YaHei UI", "Segoe UI");
            bodyFont = new Font(family, 10f * dpi / 72f, FontStyle.Regular, GraphicsUnit.Pixel);
            headingFont = new Font(family, 16f * dpi / 72f, FontStyle.Bold, GraphicsUnit.Pixel);
            Font = bodyFont;
            heading.Font = headingFont;
            if (oldBody != null) oldBody.Dispose();
            if (oldHeading != null) oldHeading.Dispose();
            applyLayoutMetrics(dpi);
            var work = (suggested.HasValue ? Screen.FromRectangle(suggested.Value) : Screen.FromHandle(Handle)).WorkingArea;
            MinimumSize = Size.Empty;
            if (!suggested.HasValue) ClientSize = new Size(Pixels(650, dpi), Pixels(488, dpi));
            var desired = suggested ?? Bounds;
            var width = Math.Min(desired.Width, work.Width);
            var height = Math.Min(desired.Height, work.Height);
            Bounds = new Rectangle(Math.Max(work.Left, Math.Min(desired.Left, work.Right - width)),
                Math.Max(work.Top, Math.Min(desired.Top, work.Bottom - height)), width, height);
            MinimumSize = new Size(Math.Min(Pixels(550, dpi), width), Math.Min(Pixels(440, dpi), height));
        }
        finally { ResumeLayout(true); }
    }

    protected override void Dispose(bool disposing)
    {
        base.Dispose(disposing);
        if (disposing)
        {
            if (bodyFont != null) bodyFont.Dispose();
            if (headingFont != null) headingFont.Dispose();
        }
    }

    private static bool HasProfile()
    {
        var configured = Environment.GetEnvironmentVariable("GITGO_CONFIG_PATH");
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        if (!String.IsNullOrWhiteSpace(configured))
        {
            var path = configured.Trim();
            if (path.StartsWith("~/") || path.StartsWith("~\\")) path = Path.Combine(home, path.Substring(2));
            return File.Exists(path);
        }
        return File.Exists(Path.Combine(home, ".gitgo", "config.json"))
            || File.Exists(Path.Combine(home, ".vernier", "gitgo_config.json"))
            || File.Exists(Path.Combine(Environment.CurrentDirectory, "gitgo_config.json"))
            || File.Exists(Path.Combine(Environment.CurrentDirectory, "sync_config.json"));
    }

    private static bool HasRegistration(string root)
    {
        return root != null && File.Exists(Path.Combine(root, "program", "control", "registration.json"));
    }

    private static string LocalRoot(string value)
    {
        var selected = value.Trim();
        if (selected.Length < 3 || selected[1] != ':' || (selected[2] != '\\' && selected[2] != '/')) return null;
        try { return Path.GetFullPath(selected); }
        catch (ArgumentException) { return null; }
        catch (NotSupportedException) { return null; }
        catch (PathTooLongException) { return null; }
    }

    public SetupWizard(string defaultRoot, string license,
        Func<string, string, Action<SetupProgress>, SetupResult> install, Action<string> open, string statusPath = null)
    {
        this.install = install;
        this.open = open;
        this.statusPath = statusPath;
        Text = Local("Gitgo 安装", "Gitgo Setup");
        Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        AutoScaleMode = AutoScaleMode.None;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(650, 488);
        MinimumSize = new Size(550, 440);
        MaximizeBox = false;

        var layout = Grid(1);
        layout.Padding = new Padding(22, 18, 22, 14);
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        heading.Margin = new Padding(0, 0, 0, 8);
        var actions = new FlowLayoutPanel { AutoSize = true, FlowDirection = FlowDirection.RightToLeft,
            WrapContents = false, Dock = DockStyle.Fill, Padding = new Padding(0, 10, 0, 0) };
        primary.MinimumSize = secondary.MinimumSize = new Size(92, 30);
        actions.Controls.Add(primary);
        actions.Controls.Add(secondary);
        layout.Controls.Add(heading, 0, 0);
        layout.Controls.Add(content, 0, 1);
        layout.Controls.Add(actions, 0, 2);
        Controls.Add(layout);
        AcceptButton = primary;
        CancelButton = secondary;

        welcome.Text = Local("Gitgo 将安装到当前 Windows 用户的账户。向导会检查安装位置，并设置启动入口。",
            "Gitgo will be installed for this Windows account. Setup checks the installation folder and registers launch entries.");
        if (HasProfile())
            welcome.Text += Local("\r\n\r\n已检测到以前的 Gitgo 配置。安装会保留这些设置。",
                "\r\n\r\nExisting Gitgo configuration was detected. Setup preserves those settings.");
        if (HasRegistration(defaultRoot))
            welcome.Text += Local("\r\n\r\n默认目录已有受管安装。继续可检查并补全启动入口。",
                "\r\n\r\nA managed installation exists in the default folder. Continue to verify and restore launch entries.");

        agreement = Grid(1);
        agreement.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        agreement.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        var terms = new TextBox { Text = license, ReadOnly = true, Multiline = true,
            ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill, BackColor = SystemColors.Window };
        consent.Text = Local("我已阅读并同意 Apache 2.0 许可条款", "I have read and agree to the Apache 2.0 license terms");
        consent.Margin = new Padding(0, 8, 0, 0);
        agreement.Controls.Add(terms, 0, 0);
        agreement.Controls.Add(consent, 0, 1);

        options = Grid(1);
        for (int index = 0; index < 4; index++) options.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        options.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        directory.Text = defaultRoot;
        var path = Grid(2);
        path.AutoSize = true;
        path.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        path.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        var browse = new Button { AutoSize = true, Text = Local("浏览…", "Browse…") };
        browse.Click += (sender, args) => Browse();
        path.Controls.Add(directory, 0, 0);
        path.Controls.Add(browse, 1, 0);
        desktop.Text = Local("创建桌面快捷方式", "Create a desktop shortcut");
        desktop.Margin = new Padding(0, 8, 0, 0);
        var folderLabel = LabelText(Local("安装目录", "Installation folder"));
        options.Controls.Add(folderLabel, 0, 0);
        options.Controls.Add(path, 0, 1);
        options.Controls.Add(desktop, 0, 2);
        options.Controls.Add(installation, 0, 3);
        applyLayoutMetrics = dpi => {
            Func<int, int> px = value => Pixels(value, dpi);
            layout.Padding = new Padding(px(22), px(18), px(22), px(14));
            heading.Margin = new Padding(0, 0, 0, px(8));
            actions.Padding = new Padding(0, px(10), 0, 0);
            primary.MinimumSize = secondary.MinimumSize = new Size(px(92), px(30));
            consent.Margin = desktop.Margin = new Padding(0, px(8), 0, 0);
            installation.Margin = new Padding(0, px(12), 0, 0);
            folderLabel.Margin = new Padding(0, px(10), 0, px(6));
            progress.Height = px(23);
        };

        consent.CheckedChanged += (sender, args) => UpdateInstallButton();
        directory.TextChanged += (sender, args) => UpdateInstallButton();
        primary.Click += (sender, args) => OnPrimary();
        secondary.Click += (sender, args) => OnSecondary();
        FormClosing += (sender, args) => { if (busy) args.Cancel = true; };
        ShowPage(Page.Welcome);
    }

    private void Browse()
    {
        using (var picker = new FolderBrowserDialog { Description = Local("选择 Gitgo 安装目录", "Choose the Gitgo installation folder") })
        {
            if (Directory.Exists(directory.Text)) picker.SelectedPath = directory.Text;
            if (picker.ShowDialog(this) == DialogResult.OK) directory.Text = picker.SelectedPath;
        }
    }

    private void ShowPage(Page next)
    {
        page = next;
        failed = false;
        result = null;
        content.Controls.Clear();
        secondary.Text = next == Page.Welcome ? Local("取消", "Cancel") : Local("上一步", "Back");
        primary.Text = Local("下一步", "Next");
        if (next == Page.Welcome)
        {
            heading.Text = Local("安装 Gitgo", "Install Gitgo");
            content.Controls.Add(welcome);
            ActiveControl = primary;
        }
        else if (next == Page.License)
        {
            heading.Text = Local("许可协议", "License agreement");
            content.Controls.Add(agreement);
            ActiveControl = consent;
        }
        else
        {
            heading.Text = Local("安装选项", "Installation options");
            content.Controls.Add(options);
            ActiveControl = directory;
        }
        UpdateInstallButton();
    }

    private void UpdateInstallButton()
    {
        if (page == Page.Welcome) primary.Enabled = true;
        if (page == Page.License) primary.Enabled = consent.Checked;
        if (page == Page.Configuration)
        {
            var root = LocalRoot(directory.Text);
            var managed = HasRegistration(root);
            primary.Enabled = root != null;
            primary.Text = managed ? Local("检查并补全", "Verify and restore") : Local("开始安装", "Install");
            installation.Text = managed
                ? Local("已检测到安装记录。将检查程序，并补全缺失的启动入口。", "An installation was detected. Setup verifies it and restores missing launch entries.")
                : Local("开始菜单和 Windows Terminal 入口将随安装创建。", "Setup creates Start menu and Windows Terminal entries.");
        }
    }

    private void OnSecondary()
    {
        if (page == Page.License) ShowPage(Page.Welcome);
        else if (page == Page.Configuration) ShowPage(Page.License);
        else Close();
    }

    private void ShowStatus(Control detail)
    {
        var panel = Grid(1);
        var working = detail == progress;
        message.AutoSize = working;
        if (working)
        {
            panel.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            panel.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            panel.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        }
        else panel.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        panel.Controls.Add(message, 0, 0);
        if (detail != null) panel.Controls.Add(detail, 0, 1);
        content.Controls.Clear();
        content.Controls.Add(panel);
    }

    private void UpdateProgress(SetupProgress value)
    {
        if (value.Stage == "extract" && value.Total > 0)
        {
            progress.Style = ProgressBarStyle.Continuous;
            progress.Maximum = value.Total;
            progress.Value = value.Completed;
            message.Text = String.Format(Local("解包 · {0} / {1} 项", "Extracting · {0} / {1} entries"), value.Completed, value.Total);
            return;
        }
        progress.Style = ProgressBarStyle.Marquee;
        message.Text = value.Stage == "repair" ? Local("正在检查安装并补全启动入口…", "Verifying the installation and restoring launch entries…")
            : value.Stage == "install" ? Local("正在安装程序并注册启动入口…", "Installing the program and registering launch entries…")
            : Local("正在校验安装包…", "Verifying the installation package…");
    }

    private async void OnPrimary()
    {
        if (busy) return;
        if (failed) { ShowPage(Page.Configuration); return; }
        if (result != null && result.ExitCode == 0)
        {
            try { open(result.Launcher); }
            catch (Exception error) { MessageBox.Show(this, error.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error); return; }
            Close();
            return;
        }
        if (page == Page.Welcome) { ShowPage(Page.License); return; }
        if (page == Page.License)
        {
            if (consent.Checked) ShowPage(Page.Configuration);
            return;
        }
        var root = LocalRoot(directory.Text);
        if (root == null)
        {
            MessageBox.Show(this, Local("请选择本机磁盘上的绝对路径。", "Choose a full path on a local drive."),
                Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }

        var createDesktop = desktop.Checked;
        var desktopFolder = createDesktop ? Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory) : null;
        selectedRoot = root;
        RecordAttempt("installing", null);
        page = Page.Progress;
        busy = true;
        primary.Enabled = secondary.Enabled = false;
        heading.Text = Local("安装进度", "Installation progress");
        UpdateProgress(new SetupProgress("verify"));
        ShowStatus(progress);
        try
        {
            result = await Task.Run(() => install(root, desktopFolder, phase => BeginInvoke((Action)(() => UpdateProgress(phase)))));
            if (result.ExitCode != 0)
            {
                var code = Regex.Match(result.Output + "\n" + result.Errors, @"\b(?:UPDATE|NATIVE_ENTRY)_[A-Z0-9_]+\b");
                var detail = code.Success ? code.Value : Local("退出码 ", "Exit code ") + result.ExitCode;
                if (detail == "UPDATE_ACL_INVALID")
                    detail += Local("\r\n现有配置或安装文件的访问权限未通过校验。请检查所选目录及配置文件的权限。",
                        "\r\nAccess checks failed for the configuration or installation files. Check permissions for the selected folder and configuration file.");
                if (detail == "UPDATE_INSTALL_INVALID" || detail == "UPDATE_PROTECTED_PATHS")
                    detail += Local("\r\n请选择新的本机安装目录，且与项目和配置分开。", "\r\nChoose a new local installation folder outside project and configuration locations.");
                if (detail == "UPDATE_INSTALL_PAYLOAD_INCOMPLETE")
                    detail += Local("\r\n现有程序文件缺失或已变更。请选择新目录安装。", "\r\nInstalled program files are incomplete or modified. Choose a new folder.");
                ShowFailure(detail, code.Success ? code.Value : "SETUP_HOST_EXIT_" + result.ExitCode);
                return;
            }
            var shortcut = createDesktop ? result.DesktopState() : null;
            ExitCode = 0;
            RecordAttempt(result.RepairedEntries() ? "entries_restored" : "installed", null);
            heading.Text = result.RepairedEntries() ? Local("检查与补全完成", "Verification and repair complete") : Local("安装完成", "Installation complete");
            message.Text = Local("安装位置\r\n", "Installed in\r\n") + root
                + Local("\r\n\r\n可从开始菜单或 Windows Terminal 启动 Gitgo。", "\r\n\r\nOpen Gitgo from Start or Windows Terminal.");
            if (createDesktop && shortcut != "ready")
                message.Text += shortcut == "conflict" ? Local("\r\n桌面已有 Gitgo 快捷方式，原快捷方式已保留。", "\r\nThe desktop already has a Gitgo shortcut. It was preserved.")
                    : Local("\r\n桌面快捷方式未能创建。", "\r\nThe desktop shortcut could not be created.");
            primary.Text = Local("启动 Gitgo", "Launch Gitgo");
            secondary.Text = Local("关闭", "Close");
            ShowStatus(null);
        }
        catch (Exception error) { ShowFailure(error.Message, "SETUP_" + error.GetType().Name); }
        finally
        {
            busy = false;
            primary.Enabled = secondary.Enabled = true;
        }
    }

    private bool RecordAttempt(string state, string code)
    {
        if (statusPath == null) return false;
        var temporary = statusPath + "." + Guid.NewGuid().ToString("N");
        var temporaryCreated = false;
        try
        {
            var directoryPath = Path.GetDirectoryName(statusPath);
            Directory.CreateDirectory(directoryPath);
            if ((File.GetAttributes(directoryPath) & FileAttributes.ReparsePoint) != 0
                || (File.Exists(statusPath) && (File.GetAttributes(statusPath) & FileAttributes.ReparsePoint) != 0))
                return false;
            var receipt = new { at_utc = DateTime.UtcNow.ToString("o"), state, code, install_root = selectedRoot };
            using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            using (var writer = new StreamWriter(stream))
            {
                temporaryCreated = true;
                writer.Write(new JavaScriptSerializer().Serialize(receipt));
            }
            if (File.Exists(statusPath)) File.Replace(temporary, statusPath, null);
            else File.Move(temporary, statusPath);
            return true;
        }
        catch (IOException) { return false; }
        catch (UnauthorizedAccessException) { return false; }
        finally
        {
            try { if (temporaryCreated && File.Exists(temporary)) File.Delete(temporary); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }

    private void ShowFailure(string detail, string code)
    {
        failed = true;
        ExitCode = 1;
        heading.Text = Local("安装未完成", "Installation did not complete");
        message.Text = detail;
        if (RecordAttempt("failed", code))
            message.Text += Local("\r\n\r\n安装记录\r\n", "\r\n\r\nInstallation record\r\n") + statusPath;
        primary.Text = Local("返回", "Back");
        secondary.Text = Local("关闭", "Close");
        ShowStatus(null);
    }
}
